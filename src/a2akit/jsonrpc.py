"""JSON-RPC 2.0 protocol binding for A2A v0.3."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from a2a_pydantic import convert_to_v03, convert_to_v10, v03
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from starlette.responses import StreamingResponse

from a2akit.endpoints import _check_a2a_version, _sanitize_task_for_client
from a2akit.middleware import A2AMiddleware, RequestEnvelope
from a2akit.schema import DirectReply, TerminalMarker
from a2akit.storage import ContextMismatchError, TaskNotAcceptingMessagesError
from a2akit.storage.base import (
    ContentTypeNotSupportedError,
    InvalidAgentResponseError,
    ListTasksQuery,
    TaskNotCancelableError,
    TaskNotFoundError,
    TaskTerminalStateError,
    UnsupportedOperationError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from a2akit.task_manager import TaskManager

logger = logging.getLogger(__name__)

# JSON-RPC 2.0 standard error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# A2A-specific error codes (§5.4)
TASK_NOT_FOUND = -32001
TASK_NOT_CANCELABLE = -32002
PUSH_NOT_SUPPORTED = -32003
UNSUPPORTED_OPERATION = -32004
CONTENT_TYPE_NOT_SUPPORTED = -32005
INVALID_AGENT_RESPONSE = -32006
AUTHENTICATED_EXTENDED_CARD_NOT_CONFIGURED = -32007


def _error_response(req_id: Any, code: int, message: str, data: Any = None) -> JSONResponse:
    """Build a JSON-RPC error response."""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return JSONResponse(content={"jsonrpc": "2.0", "id": req_id, "error": error})


def _result_response(req_id: Any, result: Any) -> JSONResponse:
    """Build a JSON-RPC success response."""
    return JSONResponse(content={"jsonrpc": "2.0", "id": req_id, "result": result})


def _serialize(obj: Any) -> dict[str, Any] | None:
    """Serialize a pydantic model to a JSON-compatible dict.

    Accepts both v10 (internal) and v03 (wire) models. v10 objects are
    converted to v03 first, then Task-shaped payloads are sanitized to
    strip framework-internal metadata keys. Returns ``None`` when the
    object has no v0.3 wire representation — callers skip such events
    instead of shipping the wrong schema.
    """
    # Unwrap internal wrappers first so downstream logic sees concrete models.
    final = False
    if isinstance(obj, TerminalMarker):
        obj = obj.event
        final = True
    if isinstance(obj, DirectReply):
        obj = obj.message
    # Anything v10 → v03 for the wire. Already-v03 objects pass through.
    if not type(obj).__module__.startswith("a2a_pydantic.v03"):
        try:
            obj = convert_to_v03(obj)
        except Exception:
            logger.exception(
                "Cannot convert %s to the v0.3 wire shape; dropping payload",
                type(obj).__name__,
            )
            return None
    if final and isinstance(obj, v03.TaskStatusUpdateEvent):
        # v0.3 marks the closing status event with ``final: true`` so
        # clients know the stream is done (mirrors the REST path).
        obj = obj.model_copy(update={"final": True})
    if isinstance(obj, v03.Task):
        obj = _sanitize_task_for_client(obj)
    return obj.model_dump(mode="json", by_alias=True, exclude_none=True)  # type: ignore[no-any-return]


def _map_exception_to_error(req_id: Any, exc: Exception) -> JSONResponse:
    """Map known A2A exceptions to JSON-RPC error responses."""
    if isinstance(exc, TaskNotFoundError):
        return _error_response(req_id, TASK_NOT_FOUND, "Task not found")
    if isinstance(exc, TaskNotCancelableError):
        return _error_response(req_id, TASK_NOT_CANCELABLE, "Task is not cancelable")
    if isinstance(exc, TaskTerminalStateError):
        return _error_response(req_id, UNSUPPORTED_OPERATION, "Task is terminal; cannot continue")
    if isinstance(exc, ContextMismatchError):
        return _error_response(req_id, INVALID_PARAMS, "contextId does not match task")
    if isinstance(exc, TaskNotAcceptingMessagesError):
        return _error_response(req_id, INVALID_PARAMS, "Task does not accept messages")
    if isinstance(exc, UnsupportedOperationError):
        return _error_response(req_id, UNSUPPORTED_OPERATION, str(exc))
    if isinstance(exc, ContentTypeNotSupportedError):
        return _error_response(
            req_id,
            CONTENT_TYPE_NOT_SUPPORTED,
            "Incompatible content types",
            {"mimeType": exc.mime_type},
        )
    if isinstance(exc, InvalidAgentResponseError):
        return _error_response(
            req_id,
            INVALID_AGENT_RESPONSE,
            "Invalid agent response",
            {"detail": exc.detail},
        )
    from a2akit.errors import AuthenticationRequiredError
    from a2akit.push.endpoints import PushConfigNotFoundError
    from a2akit.storage.base import ConcurrencyError

    if isinstance(exc, ConcurrencyError):
        return _error_response(
            req_id, UNSUPPORTED_OPERATION, "Concurrent modification, please retry"
        )
    if isinstance(exc, AuthenticationRequiredError):
        return _error_response(req_id, INVALID_REQUEST, f"{exc.scheme} authentication required")
    if isinstance(exc, PushConfigNotFoundError):
        return _error_response(req_id, TASK_NOT_FOUND, str(exc))
    # Uncataloged exception: never echo str(exc) to the client — it may
    # leak internals. Log the detail server-side instead.
    logger.error("Unhandled exception in JSON-RPC handler", exc_info=exc)
    return _error_response(req_id, INTERNAL_ERROR, "Internal error")


def _get_tm(request: Request) -> TaskManager:
    """Extract the TaskManager from app state."""
    tm: TaskManager | None = getattr(request.app.state, "task_manager", None)
    if tm is None:
        raise RuntimeError("TaskManager not initialized")
    return tm


_JSONRPC_DISPATCH: dict[str, Any] = {}

# Methods that run the A2A middleware pipeline themselves inside their
# handler. The dispatcher MUST NOT double-process these.
#
# Two reasons a method lives here:
#   1. It builds a params-aware envelope (message/send, message/stream).
#   2. It returns a StreamingResponse and must run ``after_dispatch`` from
#      inside the SSE generator's ``finally`` — otherwise the dispatcher's
#      ``finally`` would fire after ``return StreamingResponse(...)`` but
#      before the first SSE event is produced, ending tracing spans and
#      detaching OTel context while the stream is still live
#      (tasks/resubscribe).
_MIDDLEWARE_SELF_HANDLED_METHODS: frozenset[str] = frozenset(
    {
        "message/send",
        "message/sendStream",
        "message/stream",
        "tasks/resubscribe",
    }
)

# Methods that are intentionally unauthenticated.
_MIDDLEWARE_PUBLIC_METHODS: frozenset[str] = frozenset({"health"})

# Streaming methods cannot meaningfully respond to a JSON-RPC Notification
# (no id ⇒ MUST NOT reply per JSON-RPC 2.0 §4.1). When invoked without an
# id, the dispatcher returns 204 No Content without starting the stream.
_STREAMING_METHODS: frozenset[str] = frozenset(
    {"message/sendStream", "message/stream", "tasks/resubscribe"}
)


def build_jsonrpc_router() -> APIRouter:
    """Build the JSON-RPC 2.0 A2A router."""
    router = APIRouter(dependencies=[Depends(_check_a2a_version)])

    async def _parse_body(
        request: Request,
    ) -> tuple[Any, bool, dict[str, Any]] | JSONResponse | Response:
        """Parse and validate the JSON-RPC envelope.

        Returns ``(req_id, is_notification, body)`` or an error response.

        Per JSON-RPC 2.0 §4.1 a request that omits the ``id`` member is a
        Notification and the server MUST NOT reply. ``is_notification`` is
        ``True`` only when ``id`` was absent from the envelope — an
        explicit ``"id": null`` is a valid request and still expects a
        response object with ``"id": null``.
        """
        try:
            body = await request.json()
        except Exception:
            return _error_response(None, PARSE_ERROR, "Parse error")

        if not isinstance(body, dict):
            return _error_response(None, INVALID_REQUEST, "Invalid Request")

        is_notification = "id" not in body
        req_id = body.get("id")

        if body.get("jsonrpc") != "2.0":
            if is_notification:
                return Response(status_code=204)
            return _error_response(
                req_id, INVALID_REQUEST, "Invalid Request: jsonrpc must be '2.0'"
            )

        if not isinstance(body.get("method"), str):
            if is_notification:
                return Response(status_code=204)
            return _error_response(
                req_id, INVALID_REQUEST, "Invalid Request: method must be a string"
            )

        return req_id, is_notification, body

    @router.post("/")
    async def jsonrpc_endpoint(request: Request) -> Any:
        """Single JSON-RPC 2.0 endpoint."""
        parsed = await _parse_body(request)
        if isinstance(parsed, (JSONResponse, Response)):
            return parsed

        req_id, is_notification, body = parsed
        method = body["method"]
        params = body.get("params")
        if params is None:
            params = {}
        elif not isinstance(params, dict):
            # A2A methods take named params only — arrays/scalars are invalid.
            if is_notification:
                return Response(status_code=204)
            return _error_response(req_id, INVALID_PARAMS, "Invalid params: must be an object")

        handler = _JSONRPC_DISPATCH.get(method)
        if handler is None:
            if is_notification:
                return Response(status_code=204)
            return _error_response(
                req_id, METHOD_NOT_FOUND, f"Method not found: {method}", {"method": method}
            )

        # Streaming method as a notification is meaningless and the server
        # MUST NOT reply — drop it without touching the worker pipeline.
        if is_notification and method in _STREAMING_METHODS:
            return Response(status_code=204)

        # Spec §4.4: authenticate EVERY incoming request. Message methods
        # build a params-aware envelope in their own handler and run the
        # middleware pipeline themselves. Every other method (tasks/*,
        # pushNotificationConfig/*, agent/getAuthenticatedExtendedCard)
        # runs the pipeline here with an empty envelope so that auth
        # middlewares fire uniformly across the JSON-RPC surface.
        if (
            method not in _MIDDLEWARE_SELF_HANDLED_METHODS
            and method not in _MIDDLEWARE_PUBLIC_METHODS
        ):
            middlewares: list[A2AMiddleware] = getattr(request.app.state, "middlewares", [])
            if middlewares:
                envelope = RequestEnvelope()
                started: list[A2AMiddleware] = []
                try:
                    try:
                        for mw in middlewares:
                            await mw.before_dispatch(envelope, request)
                            started.append(mw)
                    except Exception as exc:
                        # Surface the failure to error-aware middleware
                        # (TracingMiddleware) before after_dispatch runs.
                        envelope.context["_a2a_error"] = exc
                        if is_notification:
                            return Response(status_code=204)
                        return _map_exception_to_error(req_id, exc)
                    result = await handler(request, req_id, params)
                    if is_notification:
                        return Response(status_code=204)
                    return result
                finally:
                    # Only after_dispatch for middlewares whose before_dispatch
                    # completed — anything upstream of a failing middleware
                    # would otherwise leak (OTel span, context token, ...).
                    for mw in reversed(started):
                        await mw.after_dispatch(envelope)

        result = await handler(request, req_id, params)
        if is_notification:
            return Response(status_code=204)
        return result

    return router


async def _handle_message_send(
    request: Request, req_id: Any, params: dict[str, Any]
) -> JSONResponse:
    """Handle message/send."""
    try:
        send_params_v03 = v03.MessageSendParams.model_validate(params)
    except (ValidationError, Exception):
        return _error_response(req_id, INVALID_PARAMS, "Invalid params for message/send")

    msg = send_params_v03.message
    if not msg.message_id or not msg.message_id.strip():
        return _error_response(req_id, INVALID_PARAMS, "messageId is required")

    # v0.3 wire → v1.0 internal
    send_params_v10 = convert_to_v10(send_params_v03)

    middlewares: list[A2AMiddleware] = getattr(request.app.state, "middlewares", [])

    try:
        tm = _get_tm(request)
        envelope = RequestEnvelope(params=send_params_v10)
        started: list[A2AMiddleware] = []
        try:
            for mw in middlewares:
                await mw.before_dispatch(envelope, request)
                started.append(mw)

            assert envelope.params is not None  # message endpoints always carry params
            result_v10 = await tm.send_message(envelope.params, request_context=envelope.context)
        except Exception as inner_exc:
            # Surface the failure to error-aware middleware (TracingMiddleware).
            envelope.context["_a2a_error"] = inner_exc
            for mw in reversed(started):
                await mw.after_dispatch(envelope)
            raise

        for mw in reversed(started):
            await mw.after_dispatch(envelope, result_v10)

        return _result_response(req_id, _serialize(result_v10))
    except Exception as exc:
        return _map_exception_to_error(req_id, exc)


def _check_streaming(request: Request, req_id: Any) -> JSONResponse | None:
    """Return an error response if streaming is not enabled, else None."""
    caps = getattr(request.app.state, "capabilities", None)
    if caps is not None and not caps.streaming:
        return _error_response(
            req_id, UNSUPPORTED_OPERATION, "Streaming is not supported by this agent"
        )
    return None


async def _handle_message_send_stream(
    request: Request, req_id: Any, params: dict[str, Any]
) -> Any:
    """Handle message/stream (alias: message/sendStream) — returns SSE."""
    err = _check_streaming(request, req_id)
    if err is not None:
        return err
    try:
        send_params_v03 = v03.MessageSendParams.model_validate(params)
    except (ValidationError, Exception):
        return _error_response(req_id, INVALID_PARAMS, "Invalid params for message/stream")

    msg = send_params_v03.message
    if not msg.message_id or not msg.message_id.strip():
        return _error_response(req_id, INVALID_PARAMS, "messageId is required")

    send_params_v10 = convert_to_v10(send_params_v03)
    middlewares: list[A2AMiddleware] = getattr(request.app.state, "middlewares", [])
    started: list[A2AMiddleware] = []

    try:
        tm = _get_tm(request)
        envelope = RequestEnvelope(params=send_params_v10)
        try:
            for mw in middlewares:
                await mw.before_dispatch(envelope, request)
                started.append(mw)

            assert envelope.params is not None  # message endpoints always carry params
            agen = tm.stream_message(envelope.params, request_context=envelope.context)
            # Eagerly fetch the first event so that _submit_task errors
            # (TaskTerminalStateError, ContextMismatchError, etc.) produce
            # a proper JSON-RPC error response instead of a broken SSE stream.
            try:
                first_pair = await anext(agen)
            except BaseException:
                await agen.aclose()
                raise
        except BaseException as inner_exc:
            # Surface the failure to error-aware middleware (TracingMiddleware),
            # then only roll back middlewares whose before_dispatch completed.
            envelope.context["_a2a_error"] = inner_exc
            for mw in reversed(started):
                await mw.after_dispatch(envelope)
            raise
    except Exception as exc:
        return _map_exception_to_error(req_id, exc)

    async def _sse_generator() -> AsyncIterator[str]:
        try:
            first_eid, first_event = first_pair
            # Convention: a DirectReply arriving as the first event
            # (reachable via Last-Event-ID replay) is emitted as a message
            # event — uniform across all SSE endpoints.
            result = _serialize(first_event)
            if result is not None:
                payload = {"jsonrpc": "2.0", "id": req_id, "result": result}
                yield f"id: {first_eid or ''}\ndata: {json.dumps(payload)}\n\n"
            async for event_id, event in agen:
                if isinstance(event, DirectReply):
                    continue
                result = _serialize(event)
                if result is not None:
                    payload = {"jsonrpc": "2.0", "id": req_id, "result": result}
                    yield f"id: {event_id or ''}\ndata: {json.dumps(payload)}\n\n"
                if isinstance(event, TerminalMarker):
                    break
        except Exception:
            logger.exception("JSON-RPC SSE stream aborted")
        finally:
            try:
                await agen.aclose()
            finally:
                # All middlewares in ``started`` successfully completed
                # before_dispatch (otherwise we would have returned above).
                for mw in reversed(started):
                    await mw.after_dispatch(envelope)

    return StreamingResponse(_sse_generator(), media_type="text/event-stream")


async def _handle_tasks_get(request: Request, req_id: Any, params: dict[str, Any]) -> JSONResponse:
    """Handle tasks/get."""
    task_id = params.get("id")
    if not task_id:
        return _error_response(req_id, INVALID_PARAMS, "Missing 'id' in params")

    history_length = params.get("historyLength")

    try:
        tm = _get_tm(request)
        t = await tm.get_task(task_id, history_length)
        if not t:
            return _error_response(req_id, TASK_NOT_FOUND, "Task not found")
        return _result_response(req_id, _serialize(t))
    except Exception as exc:
        return _map_exception_to_error(req_id, exc)


async def _handle_tasks_list(
    request: Request, req_id: Any, params: dict[str, Any]
) -> JSONResponse:
    """Handle tasks/list."""
    try:
        tm = _get_tm(request)
        # Client sends v0.3 TaskState strings ("submitted", "working", ...).
        # Convert to v10 TaskState before hitting storage.
        raw_status = params.get("status")
        status_v10 = None
        if raw_status is not None:
            try:
                status_v10 = convert_to_v10(v03.TaskState(raw_status))
            except ValueError:
                return _error_response(req_id, INVALID_PARAMS, f"Invalid status: {raw_status!r}")
        query = ListTasksQuery(
            context_id=params.get("contextId"),
            status=status_v10,
            page_size=params.get("pageSize", 50),
            page_token=params.get("pageToken"),
            history_length=params.get("historyLength"),
            status_timestamp_after=params.get("statusTimestampAfter"),
            include_artifacts=params.get("includeArtifacts", False),
        )
        result_v10 = await tm.list_tasks(query)
        v03_tasks = [_sanitize_task_for_client(convert_to_v03(t)) for t in result_v10.tasks]
        payload = {
            "tasks": [
                t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in v03_tasks
            ],
            "nextPageToken": result_v10.next_page_token,
            "pageSize": result_v10.page_size,
            "totalSize": result_v10.total_size,
        }
        return _result_response(req_id, payload)
    except Exception as exc:
        return _map_exception_to_error(req_id, exc)


async def _handle_tasks_cancel(
    request: Request, req_id: Any, params: dict[str, Any]
) -> JSONResponse:
    """Handle tasks/cancel."""
    task_id = params.get("id")
    if not task_id:
        return _error_response(req_id, INVALID_PARAMS, "Missing 'id' in params")

    try:
        tm = _get_tm(request)
        result = await tm.cancel_task(task_id)
        return _result_response(req_id, _serialize(result))
    except Exception as exc:
        return _map_exception_to_error(req_id, exc)


async def _handle_tasks_resubscribe(request: Request, req_id: Any, params: dict[str, Any]) -> Any:
    """Handle tasks/resubscribe — returns SSE.

    The ``TaskIdParams`` schema (spec §7.4.1, referenced by §7.9) contains
    only ``id`` and ``metadata``. The resume point for an interrupted SSE
    stream is carried over HTTP via the standard ``Last-Event-ID`` header
    (W3C EventSource), not in the JSON-RPC payload.

    Listed in ``_MIDDLEWARE_SELF_HANDLED_METHODS`` so the dispatcher does
    not wrap it in its own middleware ``try/finally``. We run the pipeline
    here instead — ``before_dispatch`` up front, ``after_dispatch`` from
    inside the SSE generator's ``finally`` — so that tracing spans and
    OTel context stay attached for the lifetime of the actual stream
    rather than being torn down the moment the handler returns a
    ``StreamingResponse`` to Starlette.
    """
    err = _check_streaming(request, req_id)
    if err is not None:
        return err
    task_id = params.get("id")
    if not task_id:
        return _error_response(req_id, INVALID_PARAMS, "Missing 'id' in params")

    after_event_id = request.headers.get("Last-Event-ID")

    middlewares: list[A2AMiddleware] = getattr(request.app.state, "middlewares", [])
    envelope = RequestEnvelope()
    started: list[A2AMiddleware] = []

    try:
        try:
            for mw in middlewares:
                await mw.before_dispatch(envelope, request)
                started.append(mw)

            tm = _get_tm(request)
            agen = tm.subscribe_task(task_id, after_event_id=after_event_id)
            try:
                first_pair = await anext(agen)
            except BaseException:
                await agen.aclose()
                raise
        except BaseException as inner_exc:
            # Surface the failure to error-aware middleware (TracingMiddleware),
            # then only roll back middlewares whose before_dispatch completed.
            envelope.context["_a2a_error"] = inner_exc
            for mw in reversed(started):
                await mw.after_dispatch(envelope)
            raise
    except Exception as exc:
        return _map_exception_to_error(req_id, exc)

    async def _sse_generator() -> AsyncIterator[str]:
        try:
            first_eid, first_event = first_pair
            # Convention: a DirectReply arriving as the first event
            # (reachable via Last-Event-ID replay) is emitted as a message
            # event — uniform across all SSE endpoints.
            result = _serialize(first_event)
            if result is not None:
                payload = {"jsonrpc": "2.0", "id": req_id, "result": result}
                yield f"id: {first_eid or ''}\ndata: {json.dumps(payload)}\n\n"
            async for event_id, event in agen:
                if isinstance(event, DirectReply):
                    continue
                result = _serialize(event)
                if result is not None:
                    payload = {"jsonrpc": "2.0", "id": req_id, "result": result}
                    yield f"id: {event_id or ''}\ndata: {json.dumps(payload)}\n\n"
        except Exception:
            logger.exception("JSON-RPC SSE resubscribe stream aborted")
        finally:
            try:
                await agen.aclose()
            finally:
                # All middlewares in ``started`` successfully completed
                # before_dispatch (otherwise we would have returned above).
                for mw in reversed(started):
                    await mw.after_dispatch(envelope)

    return StreamingResponse(_sse_generator(), media_type="text/event-stream")


async def _handle_health(_request: Request, req_id: Any, _params: dict[str, Any]) -> JSONResponse:
    """Handle health check."""
    return _result_response(req_id, {"status": "ok"})


def _check_push_supported(request: Request, req_id: Any) -> JSONResponse | None:
    """Return an error response if push notifications are not enabled, else None."""
    caps = getattr(request.app.state, "capabilities", None)
    if not caps or not caps.push_notifications:
        return _error_response(req_id, PUSH_NOT_SUPPORTED, "Push notifications are not supported")
    return None


def _get_push_store(request: Request) -> Any:
    """Extract the PushConfigStore from app state."""
    return getattr(request.app.state, "push_store", None)


def _get_storage(request: Request) -> Any:
    """Extract the Storage from app state."""
    return getattr(request.app.state, "storage", None)


async def _handle_push_set(request: Request, req_id: Any, params: dict[str, Any]) -> JSONResponse:
    """Handle tasks/pushNotificationConfig/set."""
    err = _check_push_supported(request, req_id)
    if err is not None:
        return err

    task_id = params.get("taskId") or params.get("id")
    if not task_id:
        return _error_response(req_id, INVALID_PARAMS, "Missing 'taskId' in params")

    config_data = params.get("pushNotificationConfig")
    if not config_data:
        return _error_response(req_id, INVALID_PARAMS, "Missing 'pushNotificationConfig'")

    push_store = _get_push_store(request)
    storage = _get_storage(request)

    try:
        from a2akit.push.endpoints import _serialize_tpnc_v03, handle_set_config

        result = await handle_set_config(push_store, storage, task_id, config_data)
        return _result_response(req_id, _serialize_tpnc_v03(result))
    except Exception as exc:
        return _map_exception_to_error(req_id, exc)


async def _handle_push_get(request: Request, req_id: Any, params: dict[str, Any]) -> JSONResponse:
    """Handle tasks/pushNotificationConfig/get."""
    err = _check_push_supported(request, req_id)
    if err is not None:
        return err

    task_id = params.get("id")
    if not task_id:
        return _error_response(req_id, INVALID_PARAMS, "Missing 'id' in params")

    config_id = params.get("pushNotificationConfigId")
    push_store = _get_push_store(request)
    storage = _get_storage(request)

    try:
        from a2akit.push.endpoints import _serialize_tpnc_v03, handle_get_config

        result = await handle_get_config(push_store, storage, task_id, config_id)
        return _result_response(req_id, _serialize_tpnc_v03(result))
    except Exception as exc:
        return _map_exception_to_error(req_id, exc)


async def _handle_push_list(request: Request, req_id: Any, params: dict[str, Any]) -> JSONResponse:
    """Handle tasks/pushNotificationConfig/list."""
    err = _check_push_supported(request, req_id)
    if err is not None:
        return err

    task_id = params.get("id")
    if not task_id:
        return _error_response(req_id, INVALID_PARAMS, "Missing 'id' in params")

    push_store = _get_push_store(request)
    storage = _get_storage(request)

    try:
        from a2akit.push.endpoints import _serialize_tpnc_v03, handle_list_configs

        configs = await handle_list_configs(push_store, storage, task_id)
        return _result_response(req_id, [_serialize_tpnc_v03(c) for c in configs])
    except Exception as exc:
        return _map_exception_to_error(req_id, exc)


async def _handle_push_delete(
    request: Request, req_id: Any, params: dict[str, Any]
) -> JSONResponse:
    """Handle tasks/pushNotificationConfig/delete."""
    err = _check_push_supported(request, req_id)
    if err is not None:
        return err

    task_id = params.get("id")
    if not task_id:
        return _error_response(req_id, INVALID_PARAMS, "Missing 'id' in params")

    config_id = params.get("pushNotificationConfigId")
    if not config_id:
        return _error_response(req_id, INVALID_PARAMS, "Missing 'pushNotificationConfigId'")

    push_store = _get_push_store(request)
    storage = _get_storage(request)

    try:
        from a2akit.push.endpoints import handle_delete_config

        await handle_delete_config(push_store, storage, task_id, config_id)
        return _result_response(req_id, None)
    except Exception as exc:
        return _map_exception_to_error(req_id, exc)


async def _handle_get_extended_card(
    request: Request, req_id: Any, params: dict[str, Any]
) -> JSONResponse:
    """Handle agent/getAuthenticatedExtendedCard."""
    provider = getattr(request.app.state, "extended_card_provider", None)
    if provider is None:
        return _error_response(
            req_id,
            AUTHENTICATED_EXTENDED_CARD_NOT_CONFIGURED,
            "Authenticated Extended Card not configured",
        )
    try:
        from a2akit.agent_card import AgentCardConfig, build_agent_card, external_base_url

        extended_config: AgentCardConfig = await provider(request)
        base_url = external_base_url(
            dict(request.headers),
            request.url.scheme,
            request.url.netloc,
        )
        extra_protos = getattr(request.app.state, "additional_protocols", None)
        card = build_agent_card(extended_config, base_url, extra_protos)
        return _result_response(
            req_id,
            card.model_dump(mode="json", by_alias=True, exclude_none=True),
        )
    except Exception as exc:
        logger.error("Failed to build the authenticated extended card", exc_info=exc)
        return _error_response(req_id, INTERNAL_ERROR, "Internal error")


_JSONRPC_DISPATCH.update(
    {
        "message/send": _handle_message_send,
        # Spec §3.5.6: the streaming method is "message/stream".
        # "message/sendStream" is kept as a backwards-compatible alias.
        "message/stream": _handle_message_send_stream,
        "message/sendStream": _handle_message_send_stream,
        "tasks/get": _handle_tasks_get,
        # Spec v0.3 §3.5.6 marks tasks/list as gRPC/REST only — we expose
        # it anyway for Debug UI support.  Spec v1.0 §9.4.4 added it to
        # the JSON-RPC binding officially.
        "tasks/list": _handle_tasks_list,
        "tasks/cancel": _handle_tasks_cancel,
        "tasks/resubscribe": _handle_tasks_resubscribe,
        "tasks/pushNotificationConfig/set": _handle_push_set,
        "tasks/pushNotificationConfig/get": _handle_push_get,
        "tasks/pushNotificationConfig/list": _handle_push_list,
        "tasks/pushNotificationConfig/delete": _handle_push_delete,
        "agent/getAuthenticatedExtendedCard": _handle_get_extended_card,
        "health": _handle_health,
    }
)
