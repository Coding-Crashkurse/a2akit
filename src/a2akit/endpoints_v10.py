"""FastAPI router for A2A v1.0 REST (HTTP+JSON) endpoints.

Spec §5.1 routes (no ``/v1/`` prefix):

- ``POST /message:send`` / ``POST /message:stream``
- ``GET /tasks/{id}``, ``GET /tasks``, ``POST /tasks/{id}:cancel``
- ``POST /tasks/{id}:subscribe``
- ``POST /tasks/{id}/pushNotificationConfigs`` (+ GET/LIST/DELETE)
- ``GET /card``, ``GET /health``, ``GET /health/ready``

Differences from the v0.3 router (``endpoints.py``):

- Incoming bodies are already ``v10.SendMessageRequest`` / ``v10.*`` — no
  v03→v10 upconversion at the boundary.
- Outgoing SSE uses the v1.0 wrapped-discriminator form
  (``{"taskStatusUpdate": {...}}``) and closes the stream on terminal
  events instead of emitting a ``final=True`` flag.
- Errors use the ``google.rpc.Status`` envelope from ``_errors_v10``.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from a2a_pydantic import v10
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent

from a2akit._errors_v10 import (
    VALIDATION_ERROR,
    build_error,
    build_error_from_exception,
)
from a2akit._stream_v10 import sanitize_task_v10, wrap_stream_event_v10
from a2akit.agent_card import AgentCardConfig, build_agent_card_v10, external_base_url
from a2akit.middleware import A2AMiddleware, RequestEnvelope
from a2akit.schema import DirectReply, TerminalMarker
from a2akit.storage.base import (
    ListTasksQuery,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterable

    from a2akit.schema import StreamEvent
    from a2akit.task_manager import TaskManager

SUPPORTED_A2A_VERSION = "1.0"

logger = logging.getLogger(__name__)


def _sse_data_v10(event: StreamEvent, task_cache: dict[str, v10.Task]) -> str | None:
    """Serialize a v10 stream event for REST SSE.

    Payloads come from the shared :func:`wrap_stream_event_v10`; the REST
    wire emits ``Task`` / ``Message`` (and ``DirectReply``) snapshots bare
    while status/artifact updates keep the wrapped-discriminator form.
    """
    payload = wrap_stream_event_v10(event, task_cache)
    if payload is None:
        return None
    if len(payload) == 1 and ("task" in payload or "message" in payload):
        (inner,) = payload.values()
        return json.dumps(inner)
    return json.dumps(payload)


def _check_a2a_version_v10(
    a2a_version: str | None = Header(None, alias="A2A-Version"),
) -> None:
    """Validate the A2A-Version header for v1.0 (spec §3.6.2).

    Major must match ``1``. Minor mismatch is tolerated. Missing header
    defaults to 1.0.
    """
    if a2a_version is None:
        return
    parts = a2a_version.strip().split(".")
    if not parts or parts[0] != "1":
        raise HTTPException(
            status_code=400,
            detail={
                "code": 400,
                "status": "INVALID_ARGUMENT",
                "message": (
                    f"Unsupported A2A version: {a2a_version}. "
                    f"This server supports A2A {SUPPORTED_A2A_VERSION}."
                ),
            },
        )


def _get_tm(request: Request) -> TaskManager:
    tm: TaskManager | None = getattr(request.app.state, "task_manager", None)
    if tm is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": 503,
                "status": "UNAVAILABLE",
                "message": "TaskManager not initialized",
            },
        )
    return tm


def _check_streaming(request: Request) -> None:
    caps = getattr(request.app.state, "capabilities", None)
    if caps is not None and not caps.streaming:
        # Routed through the standard exception handler so the response uses
        # the google.rpc.Status shape consistently.
        from a2akit.storage.base import UnsupportedOperationError

        raise UnsupportedOperationError("Streaming is not supported by this agent")


def _check_push_supported(request: Request) -> None:
    caps = getattr(request.app.state, "capabilities", None)
    if not caps or not caps.push_notifications:
        raise HTTPException(
            status_code=501,
            detail={
                "code": 501,
                "status": "UNIMPLEMENTED",
                "message": "Push notifications are not supported",
            },
        )


def _get_push_store(request: Request) -> Any:
    store = getattr(request.app.state, "push_store", None)
    if store is None:
        raise HTTPException(
            status_code=501,
            detail={
                "code": 501,
                "status": "UNIMPLEMENTED",
                "message": "Push notifications are not configured",
            },
        )
    return store


def _get_storage(request: Request) -> Any:
    storage = getattr(request.app.state, "storage", None)
    if storage is None:
        raise HTTPException(
            status_code=503,
            detail={
                "code": 503,
                "status": "UNAVAILABLE",
                "message": "Storage not initialized",
            },
        )
    return storage


# Paths whose handlers run their own middleware pipeline (message/send,
# message/stream). Every other endpoint uses the router-level
# ``_enforce_middleware_pipeline`` dependency so auth middlewares fire
# uniformly.
_MIDDLEWARE_SELF_HANDLED_PATHS: frozenset[str] = frozenset(
    {
        "/message:send",
        "/message:stream",
    }
)

_MIDDLEWARE_PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        "/health",
        "/health/ready",
    }
)


async def _enforce_middleware_pipeline(
    request: Request,
) -> AsyncGenerator[None, None]:
    """Router-level dependency: run middleware on every non-self-handled endpoint."""
    path = request.url.path
    if path in _MIDDLEWARE_SELF_HANDLED_PATHS or path in _MIDDLEWARE_PUBLIC_PATHS:
        yield
        return

    middlewares: list[A2AMiddleware] = getattr(request.app.state, "middlewares", [])
    if not middlewares:
        yield
        return

    envelope = RequestEnvelope()
    envelope.context["a2a_version"] = "1.0"
    started: list[A2AMiddleware] = []
    try:
        for mw in middlewares:
            await mw.before_dispatch(envelope, request)
            started.append(mw)
        yield
    except BaseException as exc:
        # Surface the failure to error-aware middleware (TracingMiddleware).
        envelope.context["_a2a_error"] = exc
        raise
    finally:
        for mw in reversed(started):
            await mw.after_dispatch(envelope)


async def _stream_setup_v10(
    request: Request,
    params: v10.SendMessageRequest,
) -> AsyncGenerator[
    tuple[
        tuple[str | None, StreamEvent],
        AsyncGenerator[tuple[str | None, StreamEvent], None],
        list[A2AMiddleware],
        RequestEnvelope,
    ],
    None,
]:
    """Validate, run middleware, start streaming, and fetch the first event."""
    _check_streaming(request)
    msg = params.message
    if not msg.message_id or not msg.message_id.strip():
        raise HTTPException(
            status_code=400,
            detail={
                "code": 400,
                "status": "INVALID_ARGUMENT",
                "message": "messageId is required.",
            },
        )
    tm = _get_tm(request)
    middlewares: list[A2AMiddleware] = getattr(request.app.state, "middlewares", [])

    envelope = RequestEnvelope(params=params, tenant=params.tenant or None)
    envelope.context["a2a_version"] = "1.0"
    started: list[A2AMiddleware] = []
    agen: AsyncGenerator[tuple[str | None, StreamEvent], None] | None = None
    try:
        for mw in middlewares:
            await mw.before_dispatch(envelope, request)
            started.append(mw)

        assert envelope.params is not None
        agen = tm.stream_message(envelope.params, request_context=envelope.context)
        try:
            first_pair = await anext(agen)
        except BaseException:
            await agen.aclose()
            agen = None
            raise
    except BaseException as exc:
        # Surface the failure to error-aware middleware (TracingMiddleware).
        envelope.context["_a2a_error"] = exc
        for mw in reversed(started):
            await mw.after_dispatch(envelope)
        raise

    try:
        yield first_pair, agen, started, envelope
    finally:
        try:
            if agen is not None:
                await agen.aclose()
        finally:
            for mw in reversed(started):
                await mw.after_dispatch(envelope)


async def _subscribe_setup_v10(
    request: Request,
    task_id: str = Path(),
    last_event_id: str | None = Header(None, alias="Last-Event-ID"),
) -> AsyncGenerator[
    tuple[tuple[str | None, StreamEvent], AsyncGenerator[tuple[str | None, StreamEvent], None]],
    None,
]:
    _check_streaming(request)
    tm = _get_tm(request)
    agen = tm.subscribe_task(task_id, after_event_id=last_event_id)
    try:
        first_pair = await anext(agen)
    except BaseException:
        await agen.aclose()
        raise

    try:
        yield first_pair, agen
    finally:
        await agen.aclose()


def build_a2a_router_v10() -> APIRouter:
    """Build the v1.0 REST router."""
    router = APIRouter(
        dependencies=[
            Depends(_check_a2a_version_v10),
            Depends(_enforce_middleware_pipeline),
        ]
    )

    @router.post("/message:send", tags=["Messages"])
    async def message_send(request: Request, params: v10.SendMessageRequest) -> JSONResponse:
        """Submit a message and return the task or message directly (v1.0)."""
        msg = params.message
        if not msg.message_id or not msg.message_id.strip():
            return build_error(
                http_status=400,
                grpc_status="INVALID_ARGUMENT",
                message="messageId is required",
                reason=VALIDATION_ERROR.reason,
            )
        tm = _get_tm(request)
        middlewares: list[A2AMiddleware] = getattr(request.app.state, "middlewares", [])

        envelope = RequestEnvelope(params=params, tenant=params.tenant or None)
        envelope.context["a2a_version"] = "1.0"
        started: list[A2AMiddleware] = []
        try:
            for mw in middlewares:
                await mw.before_dispatch(envelope, request)
                started.append(mw)
            assert envelope.params is not None
            result = await tm.send_message(envelope.params, request_context=envelope.context)
        except Exception as exc:
            # Surface the failure to error-aware middleware (TracingMiddleware).
            envelope.context["_a2a_error"] = exc
            for mw in reversed(started):
                await mw.after_dispatch(envelope)
            raise

        for mw in reversed(started):
            await mw.after_dispatch(envelope, result)

        # v1.0 wraps the result in SendMessageResponse (task oneof or message oneof).
        response: dict[str, Any]
        if isinstance(result, v10.Task):
            sanitized = sanitize_task_v10(result)
            response = {
                "task": sanitized.model_dump(mode="json", by_alias=True, exclude_none=True)
            }
        else:  # v10.Message (direct reply)
            response = {
                "message": result.model_dump(mode="json", by_alias=True, exclude_none=True)
            }
        return JSONResponse(content=response)

    @router.post("/message:stream", response_class=EventSourceResponse, tags=["Messages"])
    async def message_stream(
        setup: tuple[
            tuple[str | None, StreamEvent],
            AsyncGenerator[tuple[str | None, StreamEvent], None],
            list[A2AMiddleware],
            RequestEnvelope,
        ] = Depends(_stream_setup_v10),
    ) -> AsyncIterable[ServerSentEvent]:
        """Submit a message and stream events via SSE (v1.0 shape)."""
        first_pair, agen, _mws, _envelope = setup
        task_cache: dict[str, v10.Task] = {}
        try:
            eid, first_event = first_pair
            # Convention: a DirectReply arriving as the first event
            # (reachable via Last-Event-ID replay) is emitted as a message
            # event — uniform across all SSE endpoints.
            payload = _sse_data_v10(first_event, task_cache)
            if payload is not None:
                yield ServerSentEvent(raw_data=payload, id=eid)
            async for eid, ev in agen:
                if isinstance(ev, DirectReply):
                    continue
                payload = _sse_data_v10(ev, task_cache)
                if payload is not None:
                    yield ServerSentEvent(raw_data=payload, id=eid)
                if isinstance(ev, TerminalMarker):
                    # v1.0 closes the stream after the terminal event — no
                    # `final: true` flag on the wire.
                    break
        except Exception:
            logger.exception("SSE stream aborted")

    @router.get("/tasks/{task_id}", tags=["Tasks"])
    async def tasks_get(
        request: Request,
        task_id: str = Path(),
        history_length: int | None = Query(None, alias="historyLength"),
    ) -> JSONResponse:
        tm = _get_tm(request)
        t = await tm.get_task(task_id, history_length)
        if not t:
            return build_error(
                http_status=404,
                grpc_status="NOT_FOUND",
                message="Task not found",
                reason="TASK_NOT_FOUND",
                metadata={"taskId": task_id},
            )
        t = sanitize_task_v10(t)
        return JSONResponse(content=t.model_dump(mode="json", by_alias=True, exclude_none=True))

    @router.get("/tasks", tags=["Tasks"])
    async def tasks_list(
        request: Request,
        context_id: str | None = Query(None, alias="contextId"),
        tenant: str | None = Query(None),
        status: str | None = Query(None),
        page_size: int = Query(50, alias="pageSize"),
        page_token: str | None = Query(None, alias="pageToken"),
        history_length: int | None = Query(None, alias="historyLength"),
        status_timestamp_after: str | None = Query(None, alias="statusTimestampAfter"),
        include_artifacts: bool = Query(False, alias="includeArtifacts"),
    ) -> JSONResponse:
        """List tasks. v1.0 accepts ``TASK_STATE_*`` enum strings (uppercase)."""
        tm = _get_tm(request)
        status_v10: v10.TaskState | None = None
        if status is not None:
            try:
                status_v10 = v10.TaskState(status)
            except ValueError:
                return build_error(
                    http_status=400,
                    grpc_status="INVALID_ARGUMENT",
                    message=f"Invalid status: {status!r}",
                    reason=VALIDATION_ERROR.reason,
                )
        query = ListTasksQuery(
            context_id=context_id,
            tenant=tenant,
            status=status_v10,
            page_size=page_size,
            page_token=page_token,
            history_length=history_length,
            status_timestamp_after=status_timestamp_after,
            include_artifacts=include_artifacts,
        )
        result = await tm.list_tasks(query)
        result.tasks = [sanitize_task_v10(t) for t in result.tasks]
        return JSONResponse(
            content=result.model_dump(mode="json", by_alias=True, exclude_none=True)
        )

    @router.post("/tasks/{task_id}:cancel", tags=["Tasks"])
    async def tasks_cancel(request: Request, task_id: str = Path()) -> JSONResponse:
        tm = _get_tm(request)
        result = await tm.cancel_task(task_id)
        result = sanitize_task_v10(result)
        return JSONResponse(
            content=result.model_dump(mode="json", by_alias=True, exclude_none=True)
        )

    @router.post(
        "/tasks/{task_id}:subscribe",
        response_class=EventSourceResponse,
        tags=["Tasks"],
    )
    async def tasks_subscribe(
        setup: tuple[
            tuple[str | None, StreamEvent],
            AsyncGenerator[tuple[str | None, StreamEvent], None],
        ] = Depends(_subscribe_setup_v10),
    ) -> AsyncIterable[ServerSentEvent]:
        first_pair, agen = setup
        task_cache: dict[str, v10.Task] = {}
        try:
            eid, first_event = first_pair
            # Convention: a DirectReply arriving as the first event
            # (reachable via Last-Event-ID replay) is emitted as a message
            # event — uniform across all SSE endpoints.
            payload = _sse_data_v10(first_event, task_cache)
            if payload is not None:
                yield ServerSentEvent(raw_data=payload, id=eid)
            async for eid, ev in agen:
                if isinstance(ev, DirectReply):
                    continue
                payload = _sse_data_v10(ev, task_cache)
                if payload is not None:
                    yield ServerSentEvent(raw_data=payload, id=eid)
                if isinstance(ev, TerminalMarker):
                    break
        except Exception:
            logger.exception("SSE subscribe stream aborted")

    # -- Push notification configs (v1.0 flat shape) --------------------------

    @router.post("/tasks/{task_id}/pushNotificationConfigs", tags=["Push Notifications"])
    async def push_config_set(request: Request, task_id: str = Path()) -> JSONResponse:
        _check_push_supported(request)
        push_store = _get_push_store(request)
        storage = _get_storage(request)
        body = await request.json()
        # v1.0 body is a flat TaskPushNotificationConfig; the shared handler
        # validates the flat webhook fields (id/url/token/authentication).
        from a2akit.push.endpoints import _serialize_tpnc, handle_set_config

        result = await handle_set_config(push_store, storage, task_id, body)
        return JSONResponse(content=_serialize_tpnc(result))

    @router.get(
        "/tasks/{task_id}/pushNotificationConfigs/{config_id}",
        tags=["Push Notifications"],
    )
    async def push_config_get_by_id(
        request: Request, task_id: str = Path(), config_id: str = Path()
    ) -> JSONResponse:
        _check_push_supported(request)
        push_store = _get_push_store(request)
        storage = _get_storage(request)
        from a2akit.push.endpoints import _serialize_tpnc, handle_get_config

        result = await handle_get_config(push_store, storage, task_id, config_id)
        return JSONResponse(content=_serialize_tpnc(result))

    @router.get("/tasks/{task_id}/pushNotificationConfigs", tags=["Push Notifications"])
    async def push_config_list(request: Request, task_id: str = Path()) -> JSONResponse:
        _check_push_supported(request)
        push_store = _get_push_store(request)
        storage = _get_storage(request)
        from a2akit.push.endpoints import _serialize_tpnc, handle_list_configs

        configs = await handle_list_configs(push_store, storage, task_id)
        return JSONResponse(content={"configs": [_serialize_tpnc(c) for c in configs]})

    @router.delete(
        "/tasks/{task_id}/pushNotificationConfigs/{config_id}",
        tags=["Push Notifications"],
        status_code=204,
    )
    async def push_config_delete(
        request: Request, task_id: str = Path(), config_id: str = Path()
    ) -> Response:
        _check_push_supported(request)
        push_store = _get_push_store(request)
        storage = _get_storage(request)
        from a2akit.push.endpoints import handle_delete_config

        await handle_delete_config(push_store, storage, task_id, config_id)
        # 204 responses MUST NOT carry a body — JSONResponse would emit "null".
        return Response(status_code=204)

    # -- Discovery + health ---------------------------------------------------

    @router.get("/card", tags=["Discovery"])
    async def get_authenticated_extended_card(request: Request) -> JSONResponse:
        provider = getattr(request.app.state, "extended_card_provider", None)
        if provider is None:
            return build_error(
                http_status=404,
                grpc_status="NOT_FOUND",
                message="Authenticated Extended Card not configured",
                reason="EXTENDED_CARD_NOT_CONFIGURED",
            )
        extended_config: AgentCardConfig = await provider(request)
        base_url = external_base_url(
            dict(request.headers),
            request.url.scheme,
            request.url.netloc,
        )
        extra_protos = getattr(request.app.state, "additional_protocols", None)
        card = build_agent_card_v10(extended_config, base_url, extra_protos)
        return JSONResponse(content=card.model_dump(mode="json", by_alias=True, exclude_none=True))

    @router.get("/health", tags=["Health"])
    async def health_check() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/health/ready", tags=["Health"])
    async def readiness_check(request: Request) -> JSONResponse:
        """Deep readiness check — pings all backend components."""
        components: dict[str, Any] = {}
        for name in ("storage", "broker", "event_bus"):
            backend = getattr(request.app.state, name, None)
            if backend is None:
                components[name] = {"status": "unavailable"}
                continue
            try:
                check = getattr(backend, "health_check", None)
                result = await check() if check is not None else {"status": "ok"}
            except Exception as exc:
                result = {"status": "error", "error": str(exc)}
            result["type"] = type(backend).__name__
            components[name] = result

        all_ok = all(c.get("status") == "ok" for c in components.values())
        return JSONResponse(
            content={"status": "ok" if all_ok else "degraded", "components": components},
            status_code=200 if all_ok else 503,
        )

    return router


def register_exception_handlers_v10(app: FastAPI) -> None:
    """Register FastAPI exception handlers that emit google.rpc.Status shape.

    Called by ``A2AServer.as_fastapi_app`` when the server is configured
    for ``protocol_version="1.0"``.
    """

    async def _on_validation(_req: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, RequestValidationError)
        return build_error(
            http_status=VALIDATION_ERROR.http_status,
            grpc_status=VALIDATION_ERROR.grpc_status,
            message=VALIDATION_ERROR.default_message,
            reason=VALIDATION_ERROR.reason,
            metadata={"errors": str(exc.errors()[:3])},
        )

    async def _on_http(_req: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, HTTPException)
        detail = exc.detail
        if isinstance(detail, dict) and "status" in detail and "message" in detail:
            return JSONResponse(status_code=exc.status_code, content={"error": detail})
        message = detail if isinstance(detail, str) else str(detail)
        return build_error(
            http_status=exc.status_code,
            grpc_status="UNKNOWN",
            message=message,
            reason="HTTP_ERROR",
        )

    async def _on_framework(_req: Request, exc: Exception) -> JSONResponse:
        return build_error_from_exception(exc)

    app.add_exception_handler(RequestValidationError, _on_validation)
    app.add_exception_handler(HTTPException, _on_http)

    # A single catch-all that routes any known framework exception to the
    # google.rpc.Status builder. Using type() lookup via ``descriptor_for``
    # covers every entry in ERROR_CATALOG without a per-exception handler.
    from a2akit._errors_v10 import ERROR_CATALOG

    for exc_type in ERROR_CATALOG:
        app.add_exception_handler(exc_type, _on_framework)


__all__ = [
    "build_a2a_router_v10",
    "register_exception_handlers_v10",
]
