"""A2AServer - one-liner setup for a fully functional A2A agent."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from a2akit.agent_card import validate_protocol
from a2akit.broker import (
    Broker,
    CancelRegistry,
    InMemoryBroker,
    InMemoryCancelRegistry,
)
from a2akit.config import Settings, get_settings
from a2akit.dependencies import DependencyContainer
from a2akit.endpoints import build_a2a_router, build_discovery_router
from a2akit.event_bus import EventBus, InMemoryEventBus
from a2akit.event_emitter import DefaultEventEmitter, EventEmitter
from a2akit.hooks import HookableEmitter, LifecycleHooks
from a2akit.jsonrpc import build_jsonrpc_router
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
    from collections.abc import AsyncIterator, Awaitable, Callable

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
        dependencies: dict[Any, Any] | None = None,
        enable_telemetry: bool | None = None,
        # Push notification options
        push_max_retries: int | None = None,
        push_retry_delay: float | None = None,
        push_timeout: float | None = None,
        push_max_concurrent: int | None = None,
        push_allow_http: bool | None = None,
        push_allowed_hosts: set[str] | None = None,
        push_blocked_hosts: set[str] | None = None,
        extended_card_provider: Callable[[Request], Awaitable[AgentCardConfig]] | None = None,
    ) -> None:
        """Store configuration for lazy initialization at startup."""
        validate_protocol(agent_card.protocol)
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
        self._enable_telemetry = enable_telemetry
        self._deps = DependencyContainer(dependencies)
        # Push notification config
        self._push_max_retries = (
            push_max_retries if push_max_retries is not None else s.push_max_retries
        )
        self._push_retry_delay = (
            push_retry_delay if push_retry_delay is not None else s.push_retry_delay
        )
        self._push_timeout = push_timeout if push_timeout is not None else s.push_timeout
        self._push_max_concurrent = (
            push_max_concurrent if push_max_concurrent is not None else s.push_max_concurrent
        )
        self._push_allow_http = (
            push_allow_http if push_allow_http is not None else s.push_allow_http
        )
        self._push_allowed_hosts = push_allowed_hosts
        self._push_blocked_hosts = push_blocked_hosts
        self._extended_card_provider = extended_card_provider
        if extended_card_provider is not None:
            self._card_config.supports_authenticated_extended_card = True

    def _is_telemetry_enabled(self) -> bool:
        """Determine if OTel instrumentation should be active."""
        if self._enable_telemetry is False:
            return False
        from a2akit.telemetry._instruments import OTEL_ENABLED

        if self._enable_telemetry is True and not OTEL_ENABLED:
            msg = (
                "enable_telemetry=True but OpenTelemetry is not installed. "
                "Install with: pip install a2akit[otel]"
            )
            raise RuntimeError(msg)
        return OTEL_ENABLED

    def _build_storage(self) -> Storage:
        """Resolve the storage spec into a Storage instance."""
        if isinstance(self._storage_spec, Storage):
            return self._storage_spec
        if self._storage_spec == "memory":
            return InMemoryStorage()
        if isinstance(self._storage_spec, str):
            if self._storage_spec.startswith("postgresql"):
                from a2akit.storage.postgres import PostgreSQLStorage

                return PostgreSQLStorage(self._storage_spec)
            if self._storage_spec.startswith("sqlite"):
                from a2akit.storage.sqlite import SQLiteStorage

                return SQLiteStorage(self._storage_spec)
        msg = (
            f"Unknown storage backend: {self._storage_spec!r}. "
            "Use 'memory', a connection string "
            "('postgresql+asyncpg://...', 'sqlite+aiosqlite:///...'), "
            "or pass a Storage instance."
        )
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

    def as_fastapi_app(self, *, debug: bool = False, **fastapi_kwargs: Any) -> FastAPI:
        """Create a fully configured FastAPI application."""
        server = self

        @asynccontextmanager
        async def lifespan(app: FastAPI) -> AsyncIterator[None]:
            """Initialize and tear down storage, broker, event bus, and worker adapter."""
            storage = server._build_storage()
            broker = server._build_broker()
            event_bus = server._build_event_bus()
            cancel_registry = server._cancel_registry or InMemoryCancelRegistry()
            base_emitter = DefaultEventEmitter(event_bus, storage)
            emitter: EventEmitter = (
                HookableEmitter(base_emitter, server._hooks) if server._hooks else base_emitter
            )
            if server._is_telemetry_enabled():
                from a2akit.telemetry._emitter import TracingEmitter

                emitter = TracingEmitter(emitter)

            # Push notification setup
            push_store = None
            delivery_service = None
            caps = server._card_config.capabilities
            if caps.push_notifications:
                from a2akit.push.delivery import WebhookDeliveryService
                from a2akit.push.emitter import PushDeliveryEmitter
                from a2akit.push.store import InMemoryPushConfigStore

                push_store = InMemoryPushConfigStore()
                delivery_service = WebhookDeliveryService(
                    max_retries=server._push_max_retries,
                    retry_base_delay=server._push_retry_delay,
                    timeout=server._push_timeout,
                    max_concurrent_deliveries=server._push_max_concurrent,
                    allow_http=server._push_allow_http,
                    allowed_hosts=server._push_allowed_hosts,
                    blocked_hosts=server._push_blocked_hosts,
                )
                await delivery_service.startup()
                emitter = PushDeliveryEmitter(emitter, push_store, delivery_service, storage)

            adapter = WorkerAdapter(
                server._worker,
                broker,
                storage,
                event_bus,
                cancel_registry,
                max_concurrent_tasks=server._max_concurrent_tasks,
                max_retries=server._max_retries,
                emitter=emitter,
                deps=server._deps,
            )
            tm = TaskManager(
                broker=broker,
                storage=storage,
                event_bus=event_bus,
                cancel_registry=cancel_registry,
                default_blocking_timeout_s=server._blocking_timeout_s,
                cancel_force_timeout_s=server._cancel_force_timeout_s,
                emitter=emitter,
                push_store=push_store,
            )

            app.state.task_manager = tm
            app.state.storage = storage
            app.state.broker = broker
            app.state.event_bus = event_bus
            app.state.push_store = push_store
            middlewares = list(server._middlewares)
            if server._is_telemetry_enabled():
                from a2akit.telemetry._middleware import TracingMiddleware

                middlewares.insert(0, TracingMiddleware())
            app.state.middlewares = middlewares
            app.state.capabilities = server._card_config.capabilities
            app.state.extended_card_provider = server._extended_card_provider

            await server._deps.startup()
            async with storage, broker, event_bus, adapter.run():
                try:
                    yield
                finally:
                    if delivery_service:
                        await delivery_service.shutdown()
                    del app.state.task_manager
                    del app.state.broker
                    del app.state.storage
                    del app.state.event_bus
                    del app.state.push_store
                    del app.state.middlewares
                    del app.state.capabilities
                    del app.state.extended_card_provider
                    await server._deps.shutdown()

        fastapi_kwargs.setdefault("title", self._card_config.name)
        fastapi_kwargs.setdefault("version", self._card_config.version)
        fastapi_kwargs.setdefault("description", self._card_config.description)

        app = FastAPI(lifespan=lifespan, **fastapi_kwargs)
        _register_exception_handlers(app)

        if debug:
            from a2akit._chat_ui import mount_chat_ui

            mount_chat_ui(app)
            logger.info("Debug UI available at /chat")

        protocol = self._card_config.protocol
        if protocol == "jsonrpc":
            app.include_router(build_jsonrpc_router())
        elif protocol == "http+json":
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

    from a2akit.push.endpoints import PushConfigNotFoundError

    @app.exception_handler(PushConfigNotFoundError)
    async def handle_push_config_not_found(
        _req: Request, exc: PushConfigNotFoundError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={"code": -32001, "message": str(exc)},
        )
