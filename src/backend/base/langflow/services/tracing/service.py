from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from loguru import logger

from langflow.services.base import Service

if TYPE_CHECKING:
    from uuid import UUID

    from langchain.callbacks.base import BaseCallbackHandler

    from langflow.custom.custom_component.component import Component
    from langflow.graph.vertex.base import Vertex
    from langflow.services.settings.service import SettingsService
    from langflow.services.tracing.base import BaseTracer
    from langflow.services.tracing.schema import Log


def _get_langsmith_tracer():
    from langflow.services.tracing.langsmith import LangSmithTracer

    return LangSmithTracer


def _get_langwatch_tracer():
    from langflow.services.tracing.langwatch import LangWatchTracer

    return LangWatchTracer


def _get_langfuse_tracer():
    from langflow.services.tracing.langfuse import LangFuseTracer

    return LangFuseTracer


def _get_arize_phoenix_tracer():
    from langflow.services.tracing.arize_phoenix import ArizePhoenixTracer

    return ArizePhoenixTracer


class TracingService(Service):
    name = "tracing_service"

    def __init__(self, settings_service: SettingsService):
        self.settings_service = settings_service
        self.inputs: dict[str, dict] = defaultdict(dict)
        self.inputs_metadata: dict[str, dict] = defaultdict(dict)
        self.outputs: dict[str, dict] = defaultdict(dict)
        self.outputs_metadata: dict[str, dict] = defaultdict(dict)
        self.run_name: str | None = None
        self.run_id: UUID | None = None
        self.project_name: str | None = None
        self._tracers: dict[str, BaseTracer] = {}
        self._logs: dict[str, list[Log | dict[Any, Any]]] = defaultdict(list)
        self.end_trace_tasks: set[asyncio.Task] = set()
        self.deactivated = self.settings_service.settings.deactivate_tracing
        self.session_id: str | None = None

    def _reset_io(self) -> None:
        self.inputs = defaultdict(dict)
        self.inputs_metadata = defaultdict(dict)
        self.outputs = defaultdict(dict)
        self.outputs_metadata = defaultdict(dict)

    async def initialize_tracers(self) -> None:
        if self.deactivated:
            return
        try:
            self._initialize_langsmith_tracer()
            self._initialize_langwatch_tracer()
            self._initialize_langfuse_tracer()
            self._initialize_arize_phoenix_tracer()
        except Exception:  # noqa: BLE001
            logger.opt(exception=True).debug("Error initializing tracers")

    def _initialize_langsmith_tracer(self) -> None:
        project_name = os.getenv("LANGCHAIN_PROJECT", "Langflow")
        self.project_name = project_name
        langsmith_tracer = _get_langsmith_tracer()
        self._tracers["langsmith"] = langsmith_tracer(
            trace_name=self.run_name,
            trace_type="chain",
            project_name=self.project_name,
            trace_id=self.run_id,
        )

    def _initialize_langwatch_tracer(self) -> None:
        if "langwatch" not in self._tracers or self._tracers["langwatch"].trace_id != self.run_id:
            langwatch_tracer = _get_langwatch_tracer()
            self._tracers["langwatch"] = langwatch_tracer(
                trace_name=self.run_name,
                trace_type="chain",
                project_name=self.project_name,
                trace_id=self.run_id,
            )

    def _initialize_langfuse_tracer(self) -> None:
        self.project_name = os.getenv("LANGCHAIN_PROJECT", "Langflow")
        langfuse_tracer = _get_langfuse_tracer()
        self._tracers["langfuse"] = langfuse_tracer(
            trace_name=self.run_name,
            trace_type="chain",
            project_name=self.project_name,
            trace_id=self.run_id,
        )

    def _initialize_arize_phoenix_tracer(self) -> None:
        self.project_name = os.getenv("ARIZE_PHOENIX_PROJECT", "Langflow")
        arize_phoenix_tracer = _get_arize_phoenix_tracer()
        self._tracers["arize_phoenix"] = arize_phoenix_tracer(
            trace_name=self.run_name,
            trace_type="chain",
            project_name=self.project_name,
            trace_id=self.run_id,
            session_id=self.session_id,
        )

    def set_run_name(self, name: str) -> None:
        self.run_name = name

    def set_run_id(self, run_id: UUID) -> None:
        self.run_id = run_id

    def _start_traces(
        self,
        trace_id: str,
        trace_name: str,
        trace_type: str,
        inputs: dict[str, Any],
        metadata: dict[str, Any] | None = None,
        vertex: Vertex | None = None,
    ) -> None:
        inputs = self._cleanup_inputs(inputs)
        self.inputs[trace_name] = inputs
        self.inputs_metadata[trace_name] = metadata or {}
        for tracer in self._tracers.values():
            if not tracer.ready:
                continue
            try:
                tracer.add_trace(trace_id, trace_name, trace_type, inputs, metadata, vertex)
            except Exception:  # noqa: BLE001
                logger.exception(f"Error starting trace {trace_name}")

    def _end_traces(self, trace_id: str, trace_name: str, error: Exception | None = None) -> None:
        for tracer in self._tracers.values():
            if tracer.ready:
                try:
                    tracer.end_trace(
                        trace_id=trace_id,
                        trace_name=trace_name,
                        outputs=self.outputs[trace_name],
                        error=error,
                        logs=self._logs[trace_name],
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(f"Error ending trace {trace_name}")
        self._reset_io()

    def _end_all_traces(self, outputs: dict, error: Exception | None = None) -> None:
        for tracer in self._tracers.values():
            if tracer.ready:
                try:
                    tracer.end(self.inputs, outputs=self.outputs, error=error, metadata=outputs)
                except Exception:  # noqa: BLE001
                    logger.exception("Error ending all traces")
        self._reset_io()

    async def end(self, outputs: dict, error: Exception | None = None) -> None:
        await asyncio.to_thread(self._end_all_traces, outputs, error)
        try:
            # Wait for any pending trace tasks to complete
            await asyncio.gather(*self.end_trace_tasks)
        except Exception:  # noqa: BLE001
            logger.exception("Error flushing logs")

    def add_log(self, trace_name: str, log: Log) -> None:
        self._logs[trace_name].append(log)

    @asynccontextmanager
    async def trace_context(
        self,
        component: Component,
        trace_name: str,
        inputs: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ):
        if self.deactivated:
            yield self
            return
        trace_id = trace_name
        if component._vertex:
            trace_id = component._vertex.id
        trace_type = component.trace_type
        self._start_traces(
            trace_id,
            trace_name,
            trace_type,
            inputs,
            metadata,
            component._vertex,
        )
        try:
            yield self
        except Exception as e:
            self._end_and_reset(trace_id, trace_name, e)
            raise
        else:
            self._end_and_reset(trace_id, trace_name)

    def _end_and_reset(self, trace_id: str, trace_name: str, error: Exception | None = None) -> None:
        task = asyncio.create_task(asyncio.to_thread(self._end_traces, trace_id, trace_name, error))
        self.end_trace_tasks.add(task)
        task.add_done_callback(self.end_trace_tasks.discard)

    def set_outputs(
        self,
        trace_name: str,
        outputs: dict[str, Any],
        output_metadata: dict[str, Any] | None = None,
    ) -> None:
        self.outputs[trace_name] |= outputs or {}
        self.outputs_metadata[trace_name] |= output_metadata or {}

    @staticmethod
    def _cleanup_inputs(inputs: dict[str, Any]):
        inputs = inputs.copy()
        for key in inputs:
            if "api_key" in key:
                inputs[key] = "*****"  # avoid logging api_keys for security reasons
        return inputs

    def get_langchain_callbacks(self) -> list[BaseCallbackHandler]:
        if self.deactivated:
            return []
        callbacks = []
        for tracer in self._tracers.values():
            if not tracer.ready:  # type: ignore[truthy-function]
                continue
            langchain_callback = tracer.get_langchain_callback()
            if langchain_callback:
                callbacks.append(langchain_callback)
        return callbacks

    def set_session_id(self, session_id: str) -> None:
        """Set the session ID for tracing."""
        self.session_id = session_id
