"""A2AServer – one-liner setup for a fully functional A2A agent."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agentserve.agent_card import AgentCardConfig
from agentserve.broker import (
    Broker,
    CancelRegistry,
    InMemoryBroker,
    InMemoryCancelRegistry,
)
from agentserve.endpoints import build_a2a_router, build_discovery_router
from agentserve.event_bus import EventBus, InMemoryEventBus
from agentserve.storage import (
    ContextMismatchError,
    InMemoryStorage,
    Storage,
    TaskNotAcceptingMessagesError,
    TaskNotFoundError,
    TaskTerminalStateError,
)
from agentserve.task_manager import TaskManager
from agentserve.worker import Worker, WorkerAdapter

logger = logging.getLogger(__name__)


class A2AServer:
    """High-level server that wires storage, broker, event bus, worker, and endpoints."""

    def __init__(
        self,
        *,
        worker: Worker,
        agent_card: AgentCardConfig,
        storage: str | Storage = "memory",
        broker: str | Broker = "memory",
        event_bus: str | EventBus = "memory",
        cancel_registry: CancelRegistry | None = None,
        blocking_timeout_s: float = 30.0,
        max_concurrent_tasks: int | None = None,
    ) -> None:
        """Store configuration for lazy initialization at startup."""
        self._worker = worker
        self._card_config = agent_card
        self._storage_spec = storage
        self._broker_spec = broker
        self._event_bus_spec = event_bus
        self._cancel_registry = cancel_registry
        self._blocking_timeout_s = blocking_timeout_s
        self._max_concurrent_tasks = max_concurrent_tasks

    def _build_storage(self) -> Storage:
        """Resolve the storage spec into a Storage instance."""
        if isinstance(self._storage_spec, Storage):
            return self._storage_spec
        if self._storage_spec == "memory":
            return InMemoryStorage()
        msg = f"Unknown storage backend: {self._storage_spec!r}. Use 'memory' or pass a Storage instance."
        raise ValueError(msg)

    def _build_broker(self) -> Broker:
        """Resolve the broker spec into a Broker instance."""
        if isinstance(self._broker_spec, Broker):
            return self._broker_spec
        if self._broker_spec == "memory":
            return InMemoryBroker()
        msg = f"Unknown broker backend: {self._broker_spec!r}. Use 'memory' or pass a Broker instance."
        raise ValueError(msg)

    def _build_event_bus(self) -> EventBus:
        """Resolve the event bus spec into an EventBus instance."""
        if isinstance(self._event_bus_spec, EventBus):
            return self._event_bus_spec
        if self._event_bus_spec == "memory":
            return InMemoryEventBus()
        msg = f"Unknown event bus backend: {self._event_bus_spec!r}. Use 'memory' or pass an EventBus instance."
        raise ValueError(msg)

    def as_fastapi_app(self, **fastapi_kwargs: Any) -> FastAPI:
        """Create a fully configured FastAPI application."""
        server = self

        @asynccontextmanager
        async def lifespan(app: FastAPI):
            """Initialize and tear down storage, broker, event bus, and worker adapter."""
            storage = server._build_storage()
            broker = server._build_broker()
            event_bus = server._build_event_bus()
            cancel_registry = server._cancel_registry or InMemoryCancelRegistry()
            adapter = WorkerAdapter(
                server._worker,
                broker,
                storage,
                event_bus,
                cancel_registry,
                max_concurrent_tasks=server._max_concurrent_tasks,
            )
            tm = TaskManager(
                broker=broker,
                storage=storage,
                event_bus=event_bus,
                cancel_registry=cancel_registry,
                default_blocking_timeout_s=server._blocking_timeout_s,
            )

            app.state.task_manager = tm
            app.state.storage = storage
            app.state.broker = broker
            app.state.event_bus = event_bus

            async with storage, broker, event_bus, adapter.run():
                try:
                    yield
                finally:
                    del app.state.task_manager
                    del app.state.broker
                    del app.state.storage
                    del app.state.event_bus

        fastapi_kwargs.setdefault("title", self._card_config.name)
        fastapi_kwargs.setdefault("version", self._card_config.version)
        fastapi_kwargs.setdefault("description", self._card_config.description)

        app = FastAPI(lifespan=lifespan, **fastapi_kwargs)
        _register_exception_handlers(app)
        app.include_router(build_a2a_router())
        app.include_router(build_discovery_router(self._card_config))

        return app


def _register_exception_handlers(app: FastAPI) -> None:
    """Register JSON-RPC style exception handlers for A2A storage errors."""

    @app.exception_handler(TaskNotFoundError)
    async def _(_req: Request, _exc: TaskNotFoundError):
        return JSONResponse(
            status_code=404, content={"code": -32001, "message": "Task not found"}
        )

    @app.exception_handler(TaskTerminalStateError)
    async def _(_req: Request, _exc: TaskTerminalStateError):
        return JSONResponse(
            status_code=409,
            content={"code": -32004, "message": "Task is terminal; cannot continue"},
        )

    @app.exception_handler(ContextMismatchError)
    async def _(_req: Request, _exc: ContextMismatchError):
        return JSONResponse(
            status_code=400,
            content={"code": -32602, "message": "contextId does not match task"},
        )

    @app.exception_handler(TaskNotAcceptingMessagesError)
    async def _(_req: Request, exc: TaskNotAcceptingMessagesError):
        state = getattr(exc, "state", None)
        msg = (
            f"Task is in state {state} and does not accept messages."
            if state
            else "Task does not accept messages."
        )
        return JSONResponse(status_code=422, content={"code": -32602, "message": msg})
