"""A2AServer - one-liner setup for a fully functional A2A agent."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

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
from a2akit.errors import AuthenticationRequiredError
from a2akit.event_bus import EventBus, InMemoryEventBus
from a2akit.event_emitter import DefaultEventEmitter, EventEmitter
from a2akit.hooks import HookableEmitter, LifecycleHooks
from a2akit.jsonrpc import build_jsonrpc_router
from a2akit.storage import (
    ContentTypeNotSupportedError,
    ContextMismatchError,
    InMemoryStorage,
    InvalidAgentResponseError,
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

    from starlette.responses import Response

    from a2akit.agent_card import AgentCardConfig
    from a2akit.middleware import A2AMiddleware

logger = logging.getLogger(__name__)


class ContentTypeValidationMiddleware(BaseHTTPMiddleware):
    """Reject requests without ``application/json`` Content-Type (Spec §3.2)."""

    _EXEMPT_PATHS: frozenset[str] = frozenset(
        {
            "/.well-known/agent-card.json",
            "/v1/health",
            "/chat",
        }
    )
    _EXEMPT_METHODS: frozenset[str] = frozenset({"GET", "DELETE", "OPTIONS", "HEAD"})

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        has_body = int(request.headers.get("content-length", "0")) > 0
        if (
            request.method not in self._EXEMPT_METHODS
            and request.url.path not in self._EXEMPT_PATHS
            and not request.url.path.startswith("/chat/")
            and has_body
        ):
            content_type = request.headers.get("content-type", "")
            if content_type.split(";")[0].strip().lower() != "application/json":
                return JSONResponse(
                    status_code=415,
                    content={
                        "jsonrpc": "2.0",
                        "error": {
                            "code": -32600,
                            "message": (
                                f"Unsupported Content-Type: {content_type}. "
                                f"Expected application/json."
                            ),
                        },
                        "id": None,
                    },
                )
        response: Response = await call_next(request)
        return response


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
        # Multi-transport (Spec §3.4, §5.5)
        additional_protocols: list[str] | None = None,
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
        self._additional_protocols = additional_protocols or []
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
        if isinstance(self._broker_spec, str) and self._broker_spec.startswith(
            ("redis://", "rediss://")
        ):
            from a2akit.broker.redis import RedisBroker

            return RedisBroker(self._broker_spec, settings=self._settings)
        msg = (
            f"Unknown broker backend: {self._broker_spec!r}. "
            "Use 'memory', a Redis URL ('redis://...'), or pass a Broker instance."
        )
        raise ValueError(msg)

    def _build_event_bus(self) -> EventBus:
        """Resolve the event bus spec into an EventBus instance."""
        if isinstance(self._event_bus_spec, EventBus):
            return self._event_bus_spec
        if self._event_bus_spec == "memory":
            return InMemoryEventBus(settings=self._settings)
        if isinstance(self._event_bus_spec, str) and self._event_bus_spec.startswith(
            ("redis://", "rediss://")
        ):
            from a2akit.event_bus.redis import RedisEventBus

            return RedisEventBus(self._event_bus_spec, settings=self._settings)
        msg = (
            f"Unknown event bus backend: {self._event_bus_spec!r}. "
            "Use 'memory', a Redis URL ('redis://...'), or pass an EventBus instance."
        )
        raise ValueError(msg)

    def _build_cancel_registry(self) -> CancelRegistry:
        """Resolve the cancel registry, defaulting to Redis when broker is Redis."""
        if self._cancel_registry is not None:
            return self._cancel_registry
        if isinstance(self._broker_spec, str) and self._broker_spec.startswith(
            ("redis://", "rediss://")
        ):
            from a2akit.broker.redis import RedisCancelRegistry

            return RedisCancelRegistry(self._broker_spec, settings=self._settings)
        return InMemoryCancelRegistry()

    def as_fastapi_app(self, *, debug: bool = False, **fastapi_kwargs: Any) -> FastAPI:
        """Create a fully configured FastAPI application."""
        server = self

        @asynccontextmanager
        async def lifespan(app: FastAPI) -> AsyncIterator[None]:
            """Initialize and tear down storage, broker, event bus, and worker adapter."""
            storage = server._build_storage()
            broker = server._build_broker()
            event_bus = server._build_event_bus()
            cancel_registry = server._build_cancel_registry()
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
                input_modes=server._card_config.input_modes,
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
                    for attr in (
                        "task_manager",
                        "broker",
                        "storage",
                        "event_bus",
                        "push_store",
                        "middlewares",
                        "capabilities",
                        "extended_card_provider",
                    ):
                        if hasattr(app.state, attr):
                            delattr(app.state, attr)
                    await server._deps.shutdown()

        fastapi_kwargs.setdefault("title", self._card_config.name)
        fastapi_kwargs.setdefault("version", self._card_config.version)
        fastapi_kwargs.setdefault("description", self._card_config.description)

        app = FastAPI(lifespan=lifespan, **fastapi_kwargs)

        # Content-Type validation (Spec §3.2 MUST)
        app.add_middleware(ContentTypeValidationMiddleware)

        # REQ-09: A2A-Version response header on all responses.
        @app.middleware("http")
        async def _add_a2a_version_header(request: Request, call_next: Any) -> Any:
            response = await call_next(request)
            response.headers["A2A-Version"] = "0.3.0"
            return response

        _register_exception_handlers(app)

        if debug:
            from a2akit._chat_ui import mount_chat_ui

            mount_chat_ui(app)
            logger.info("Debug UI available at /chat")

        # Mount routers: preferred protocol + additional protocols
        protocol = self._card_config.protocol
        mounted: set[str] = set()

        if protocol == "jsonrpc":
            app.include_router(build_jsonrpc_router())
            mounted.add("jsonrpc")
        elif protocol == "http+json":
            app.include_router(build_a2a_router())
            mounted.add("http+json")

        for proto in self._additional_protocols:
            normalized = proto.lower().replace(" ", "")
            if normalized in ("jsonrpc",) and "jsonrpc" not in mounted:
                app.include_router(build_jsonrpc_router())
                mounted.add("jsonrpc")
            elif normalized in ("http+json", "http", "rest") and "http+json" not in mounted:
                app.include_router(build_a2a_router())
                mounted.add("http+json")

        app.include_router(
            build_discovery_router(self._card_config, self._additional_protocols or None)
        )

        return app


def _register_exception_handlers(app: FastAPI) -> None:
    """Register JSON-RPC style exception handlers for A2A storage errors."""

    @app.exception_handler(AuthenticationRequiredError)
    async def handle_auth_required(
        _req: Request, exc: AuthenticationRequiredError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content={
                "jsonrpc": "2.0",
                "error": {"code": -32603, "message": str(exc)},
                "id": None,
            },
            headers={"WWW-Authenticate": f'{exc.scheme} realm="{exc.realm}"'},
        )

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

    @app.exception_handler(ContentTypeNotSupportedError)
    async def handle_content_type_not_supported(
        _req: Request, exc: ContentTypeNotSupportedError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={
                "code": -32005,
                "message": "Incompatible content types",
                "data": {"mimeType": exc.mime_type},
            },
        )

    @app.exception_handler(InvalidAgentResponseError)
    async def handle_invalid_agent_response(
        _req: Request, exc: InvalidAgentResponseError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={
                "code": -32006,
                "message": "Invalid agent response",
                "data": {"detail": exc.detail},
            },
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
