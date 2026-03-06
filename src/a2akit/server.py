"""A2AServer - one-liner setup for a fully functional A2A agent."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from a2akit.broker import (
    Broker,
    CancelRegistry,
    InMemoryBroker,
    InMemoryCancelRegistry,
)
from a2akit.config import Settings, get_settings
from a2akit.endpoints import build_a2a_router, build_discovery_router
from a2akit.event_bus import EventBus, InMemoryEventBus
from a2akit.event_emitter import DefaultEventEmitter
from a2akit.hooks import HookableEmitter, LifecycleHooks
from a2akit.storage import (
    ContextMismatchError,
    InMemoryStorage,
    Storage,
    TaskNotAcceptingMessagesError,
    TaskNotFoundError,
    TaskTerminalStateError,
    UnsupportedOperationError,
)
from a2akit.task_manager import TaskManager
from a2akit.worker import Worker, WorkerAdapter

if TYPE_CHECKING:
    from a2akit.agent_card import AgentCardConfig
    from a2akit.middleware import A2AMiddleware

logger = logging.getLogger(__name__)


class A2AServer:
    """High-level server that wires storage, broker, event bus, worker, and endpoints."""

    def __init__(
        self,
        *,
        worker: Worker,
        agent_card: AgentCardConfig,
        middlewares: list[A2AMiddleware] | None = None,
        storage: str | Storage = "memory",
        broker: str | Broker = "memory",
        event_bus: str | EventBus = "memory",
        cancel_registry: CancelRegistry | None = None,
        blocking_timeout_s: float | None = None,
        cancel_force_timeout_s: float | None = None,
        max_concurrent_tasks: int | None = None,
        hooks: LifecycleHooks | None = None,
        settings: Settings | None = None,
    ) -> None:
        """Store configuration for lazy initialization at startup."""
        s = settings or get_settings()
        self._worker = worker
        self._card_config = agent_card
        self._middlewares = middlewares or []
        self._storage_spec = storage
        self._broker_spec = broker
        self._event_bus_spec = event_bus
        self._cancel_registry = cancel_registry
        self._blocking_timeout_s = (
            blocking_timeout_s if blocking_timeout_s is not None else s.blocking_timeout
        )
        self._cancel_force_timeout_s = (
            cancel_force_timeout_s
            if cancel_force_timeout_s is not None
            else s.cancel_force_timeout
        )
        self._max_concurrent_tasks = (
            max_concurrent_tasks if max_concurrent_tasks is not None else s.max_concurrent_tasks
        )
        self._max_retries = s.max_retries
        self._settings = s
        self._hooks = hooks

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
            return InMemoryBroker(settings=self._settings)
        msg = f"Unknown broker backend: {self._broker_spec!r}. Use 'memory' or pass a Broker instance."
        raise ValueError(msg)

    def _build_event_bus(self) -> EventBus:
        """Resolve the event bus spec into an EventBus instance."""
        if isinstance(self._event_bus_spec, EventBus):
            return self._event_bus_spec
        if self._event_bus_spec == "memory":
            return InMemoryEventBus(settings=self._settings)
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
            base_emitter = DefaultEventEmitter(event_bus, storage)
            emitter = (
                HookableEmitter(base_emitter, server._hooks) if server._hooks else base_emitter
            )
            adapter = WorkerAdapter(
                server._worker,
                broker,
                storage,
                event_bus,
                cancel_registry,
                max_concurrent_tasks=server._max_concurrent_tasks,
                max_retries=server._max_retries,
                emitter=emitter,
            )
            tm = TaskManager(
                broker=broker,
                storage=storage,
                event_bus=event_bus,
                cancel_registry=cancel_registry,
                default_blocking_timeout_s=server._blocking_timeout_s,
                cancel_force_timeout_s=server._cancel_force_timeout_s,
                emitter=emitter,
            )

            app.state.task_manager = tm
            app.state.storage = storage
            app.state.broker = broker
            app.state.event_bus = event_bus
            app.state.middlewares = server._middlewares

            async with storage, broker, event_bus, adapter.run():
                try:
                    yield
                finally:
                    del app.state.task_manager
                    del app.state.broker
                    del app.state.storage
                    del app.state.event_bus
                    del app.state.middlewares

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
    async def handle_task_not_found(_req: Request, _exc: TaskNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"code": -32001, "message": "Task not found"})

    @app.exception_handler(TaskTerminalStateError)
    async def handle_task_terminal(_req: Request, _exc: TaskTerminalStateError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"code": -32004, "message": "Task is terminal; cannot continue"},
        )

    @app.exception_handler(ContextMismatchError)
    async def handle_context_mismatch(_req: Request, _exc: ContextMismatchError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"code": -32602, "message": "contextId does not match task"},
        )

    @app.exception_handler(TaskNotAcceptingMessagesError)
    async def handle_not_accepting(
        _req: Request, exc: TaskNotAcceptingMessagesError
    ) -> JSONResponse:
        state = getattr(exc, "state", None)
        msg = (
            f"Task is in state {state} and does not accept messages."
            if state
            else "Task does not accept messages."
        )
        return JSONResponse(status_code=422, content={"code": -32602, "message": msg})

    @app.exception_handler(UnsupportedOperationError)
    async def handle_unsupported(_req: Request, exc: UnsupportedOperationError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"code": -32004, "message": str(exc)},
        )
