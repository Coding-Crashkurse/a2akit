"""JSON-RPC 2.0 protocol binding for A2A v1.0.

Spec §5.2 — PascalCase method names (``SendMessage``, ``GetTask``, …)
and the ``google.rpc.ErrorInfo`` detail shape for errors. SSE responses
carry the same wrapped-discriminator event form as the v1.0 REST router
(``{"taskStatusUpdate": {...}}``), wrapped one more time in the JSON-RPC
success envelope.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from a2a_pydantic import v10
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from starlette.responses import StreamingResponse

from a2akit._errors_v10 import (
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    VALIDATION_ERROR,
    descriptor_for,
    jsonrpc_error_from_exception,
)
from a2akit._stream_v10 import sanitize_task_v10, wrap_stream_event_v10
from a2akit.endpoints_v10 import _check_a2a_version_v10
from a2akit.middleware import A2AMiddleware, RequestEnvelope
from a2akit.schema import DirectReply, TerminalMarker
from a2akit.storage.base import (
    ListTasksQuery,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from a2akit.task_manager import TaskManager

logger = logging.getLogger(__name__)


def _error_response(
    req_id: Any,
    *,
    code: int,
    message: str,
    reason: str,
    metadata: dict[str, str] | None = None,
) -> JSONResponse:
    """Build a JSON-RPC error response with ``google.rpc.ErrorInfo`` in ``data``."""
    info: dict[str, Any] = {
        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
        "reason": reason,
        "domain": "a2a-protocol.org",
    }
    if metadata:
        info["metadata"] = metadata
    return JSONResponse(
        content={
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": code, "message": message, "data": [info]},
        }
    )


def _result_response(req_id: Any, result: Any) -> JSONResponse:
    return JSONResponse(content={"jsonrpc": "2.0", "id": req_id, "result": result})


def _serialize_task(task: v10.Task) -> dict[str, Any]:
    return sanitize_task_v10(task).model_dump(  # type: ignore[no-any-return]
        mode="json", by_alias=True, exclude_none=True
    )


def _map_exception(req_id: Any, exc: Exception) -> JSONResponse:
    """Route a framework exception to its JSON-RPC error shape."""
    return JSONResponse(content=jsonrpc_error_from_exception(exc, req_id))


def _get_tm(request: Request) -> TaskManager:
    tm: TaskManager | None = getattr(request.app.state, "task_manager", None)
    if tm is None:
        raise RuntimeError("TaskManager not initialized")
    return tm


def _get_push_store(request: Request) -> Any:
    return getattr(request.app.state, "push_store", None)


def _get_storage(request: Request) -> Any:
    return getattr(request.app.state, "storage", None)


# Methods whose handlers run the middleware pipeline themselves (they need
# params-aware envelopes) or return SSE streams (after_dispatch must fire
# from inside the generator's finally, not from the dispatcher).
_MIDDLEWARE_SELF_HANDLED: frozenset[str] = frozenset(
    {
        "SendMessage",
        "SendStreamingMessage",
        "SubscribeToTask",
    }
)

_PUBLIC_METHODS: frozenset[str] = frozenset({"health"})

_STREAMING_METHODS: frozenset[str] = frozenset({"SendStreamingMessage", "SubscribeToTask"})


_JSONRPC_DISPATCH: dict[str, Any] = {}


def build_jsonrpc_router_v10() -> APIRouter:
    """Build the v1.0 JSON-RPC router."""
    router = APIRouter(dependencies=[Depends(_check_a2a_version_v10)])

    async def _parse_body(
        request: Request,
    ) -> tuple[Any, bool, dict[str, Any]] | JSONResponse | Response:
        try:
            body = await request.json()
        except Exception:
            return _error_response(
                None,
                code=PARSE_ERROR.json_rpc_code,
                message=PARSE_ERROR.default_message,
                reason=PARSE_ERROR.reason,
            )
        if not isinstance(body, dict):
            return _error_response(
                None,
                code=VALIDATION_ERROR.json_rpc_code,
                message=VALIDATION_ERROR.default_message,
                reason=VALIDATION_ERROR.reason,
            )

        is_notification = "id" not in body
        req_id = body.get("id")

        if body.get("jsonrpc") != "2.0":
            if is_notification:
                return Response(status_code=204)
            return _error_response(
                req_id,
                code=VALIDATION_ERROR.json_rpc_code,
                message="Invalid Request: jsonrpc must be '2.0'",
                reason=VALIDATION_ERROR.reason,
            )

        if not isinstance(body.get("method"), str):
            if is_notification:
                return Response(status_code=204)
            return _error_response(
                req_id,
                code=VALIDATION_ERROR.json_rpc_code,
                message="Invalid Request: method must be a string",
                reason=VALIDATION_ERROR.reason,
            )

        return req_id, is_notification, body

    @router.post("/")
    async def jsonrpc_endpoint(request: Request) -> Any:
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
            return _error_response(
                req_id,
                code=-32602,
                message="Invalid params: must be an object",
                reason="INVALID_PARAMS",
            )

        handler = _JSONRPC_DISPATCH.get(method)
        if handler is None:
            if is_notification:
                return Response(status_code=204)
            return _error_response(
                req_id,
                code=METHOD_NOT_FOUND.json_rpc_code,
                message=f"Method not found: {method}",
                reason=METHOD_NOT_FOUND.reason,
                metadata={"method": method},
            )

        if is_notification and method in _STREAMING_METHODS:
            return Response(status_code=204)

        # Apply middleware pipeline for non-self-handled + non-public methods.
        if method not in _MIDDLEWARE_SELF_HANDLED and method not in _PUBLIC_METHODS:
            middlewares: list[A2AMiddleware] = getattr(request.app.state, "middlewares", [])
            if middlewares:
                envelope = RequestEnvelope()
                envelope.context["a2a_version"] = "1.0"
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
                        return _map_exception(req_id, exc)
                    result = await handler(request, req_id, params)
                    if is_notification:
                        return Response(status_code=204)
                    return result
                finally:
                    for mw in reversed(started):
                        await mw.after_dispatch(envelope)

        result = await handler(request, req_id, params)
        if is_notification:
            return Response(status_code=204)
        return result

    return router


async def _handle_send_message(
    request: Request, req_id: Any, params: dict[str, Any]
) -> JSONResponse:
    try:
        send_params = v10.SendMessageRequest.model_validate(params)
    except (ValidationError, Exception):
        return _error_response(
            req_id,
            code=VALIDATION_ERROR.json_rpc_code,
            message="Invalid params for SendMessage",
            reason=VALIDATION_ERROR.reason,
        )

    msg = send_params.message
    if not msg.message_id or not msg.message_id.strip():
        return _error_response(
            req_id,
            code=VALIDATION_ERROR.json_rpc_code,
            message="messageId is required",
            reason=VALIDATION_ERROR.reason,
        )

    middlewares: list[A2AMiddleware] = getattr(request.app.state, "middlewares", [])
    try:
        tm = _get_tm(request)
        envelope = RequestEnvelope(params=send_params, tenant=send_params.tenant or None)
        envelope.context["a2a_version"] = "1.0"
        started: list[A2AMiddleware] = []
        try:
            for mw in middlewares:
                await mw.before_dispatch(envelope, request)
                started.append(mw)
            assert envelope.params is not None
            result = await tm.send_message(envelope.params, request_context=envelope.context)
        except Exception as inner_exc:
            # Surface the failure to error-aware middleware (TracingMiddleware).
            envelope.context["_a2a_error"] = inner_exc
            for mw in reversed(started):
                await mw.after_dispatch(envelope)
            raise

        for mw in reversed(started):
            await mw.after_dispatch(envelope, result)

        if isinstance(result, v10.Task):
            return _result_response(req_id, {"task": _serialize_task(result)})
        return _result_response(
            req_id,
            {"message": result.model_dump(mode="json", by_alias=True, exclude_none=True)},
        )
    except Exception as exc:
        return _map_exception(req_id, exc)


def _check_streaming(request: Request, req_id: Any) -> JSONResponse | None:
    caps = getattr(request.app.state, "capabilities", None)
    if caps is not None and not caps.streaming:
        from a2akit.storage.base import UnsupportedOperationError

        unsupported = UnsupportedOperationError("Streaming is not supported by this agent")
        desc = descriptor_for(unsupported)
        return _error_response(
            req_id,
            code=desc.json_rpc_code,
            message=str(unsupported),
            reason=desc.reason,
        )
    return None


async def _handle_send_streaming_message(
    request: Request, req_id: Any, params: dict[str, Any]
) -> Any:
    err = _check_streaming(request, req_id)
    if err is not None:
        return err
    try:
        send_params = v10.SendMessageRequest.model_validate(params)
    except (ValidationError, Exception):
        return _error_response(
            req_id,
            code=VALIDATION_ERROR.json_rpc_code,
            message="Invalid params for SendStreamingMessage",
            reason=VALIDATION_ERROR.reason,
        )

    msg = send_params.message
    if not msg.message_id or not msg.message_id.strip():
        return _error_response(
            req_id,
            code=VALIDATION_ERROR.json_rpc_code,
            message="messageId is required",
            reason=VALIDATION_ERROR.reason,
        )

    middlewares: list[A2AMiddleware] = getattr(request.app.state, "middlewares", [])
    started: list[A2AMiddleware] = []
    try:
        tm = _get_tm(request)
        envelope = RequestEnvelope(params=send_params, tenant=send_params.tenant or None)
        envelope.context["a2a_version"] = "1.0"
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
                raise
        except BaseException as inner_exc:
            # Surface the failure to error-aware middleware (TracingMiddleware).
            envelope.context["_a2a_error"] = inner_exc
            for mw in reversed(started):
                await mw.after_dispatch(envelope)
            raise
    except Exception as exc:
        return _map_exception(req_id, exc)

    async def _sse_gen() -> AsyncIterator[str]:
        task_cache: dict[str, v10.Task] = {}
        try:
            eid, first = first_pair
            # Convention: a DirectReply arriving as the first event
            # (reachable via Last-Event-ID replay) is emitted as a message
            # event — uniform across all SSE endpoints.
            payload = wrap_stream_event_v10(first, task_cache)
            if payload is not None:
                envelope_out = {"jsonrpc": "2.0", "id": req_id, "result": payload}
                yield f"id: {eid or ''}\ndata: {json.dumps(envelope_out)}\n\n"
            async for event_id, event in agen:
                if isinstance(event, DirectReply):
                    continue
                payload = wrap_stream_event_v10(event, task_cache)
                if payload is not None:
                    envelope_out = {"jsonrpc": "2.0", "id": req_id, "result": payload}
                    yield f"id: {event_id or ''}\ndata: {json.dumps(envelope_out)}\n\n"
                if isinstance(event, TerminalMarker):
                    break
        except Exception:
            logger.exception("JSON-RPC v1.0 SSE stream aborted")
        finally:
            try:
                await agen.aclose()
            finally:
                for mw in reversed(started):
                    await mw.after_dispatch(envelope)

    return StreamingResponse(_sse_gen(), media_type="text/event-stream")


async def _handle_get_task(request: Request, req_id: Any, params: dict[str, Any]) -> JSONResponse:
    task_id = params.get("id")
    if not task_id:
        return _error_response(
            req_id,
            code=VALIDATION_ERROR.json_rpc_code,
            message="Missing 'id' in params",
            reason=VALIDATION_ERROR.reason,
        )
    history_length = params.get("historyLength")
    if history_length is None:  # explicit None check — 0 is a valid value
        history_length = params.get("history_length")
    try:
        tm = _get_tm(request)
        t = await tm.get_task(task_id, history_length)
        if not t:
            return _error_response(
                req_id,
                code=-32001,
                message="Task not found",
                reason="TASK_NOT_FOUND",
                metadata={"taskId": task_id},
            )
        return _result_response(req_id, _serialize_task(t))
    except Exception as exc:
        return _map_exception(req_id, exc)


async def _handle_list_tasks(
    request: Request, req_id: Any, params: dict[str, Any]
) -> JSONResponse:
    try:
        tm = _get_tm(request)
        status_raw = params.get("status")
        status_v10: v10.TaskState | None = None
        if status_raw is not None:
            try:
                status_v10 = v10.TaskState(status_raw)
            except ValueError:
                return _error_response(
                    req_id,
                    code=VALIDATION_ERROR.json_rpc_code,
                    message=f"Invalid status: {status_raw!r}",
                    reason=VALIDATION_ERROR.reason,
                )
        query = ListTasksQuery(
            context_id=params.get("contextId") or params.get("context_id"),
            tenant=params.get("tenant"),
            status=status_v10,
            page_size=params.get("pageSize", params.get("page_size", 50)),
            page_token=params.get("pageToken") or params.get("page_token"),
            # Explicit None checks — 0 is a valid historyLength.
            history_length=(
                params.get("historyLength")
                if params.get("historyLength") is not None
                else params.get("history_length")
            ),
            status_timestamp_after=(
                params.get("statusTimestampAfter") or params.get("status_timestamp_after")
            ),
            include_artifacts=bool(
                params.get("includeArtifacts") or params.get("include_artifacts") or False
            ),
        )
        result = await tm.list_tasks(query)
        result.tasks = [sanitize_task_v10(t) for t in result.tasks]
        return _result_response(
            req_id, result.model_dump(mode="json", by_alias=True, exclude_none=True)
        )
    except Exception as exc:
        return _map_exception(req_id, exc)


async def _handle_cancel_task(
    request: Request, req_id: Any, params: dict[str, Any]
) -> JSONResponse:
    task_id = params.get("id")
    if not task_id:
        return _error_response(
            req_id,
            code=VALIDATION_ERROR.json_rpc_code,
            message="Missing 'id' in params",
            reason=VALIDATION_ERROR.reason,
        )
    try:
        tm = _get_tm(request)
        result = await tm.cancel_task(task_id)
        return _result_response(req_id, _serialize_task(result))
    except Exception as exc:
        return _map_exception(req_id, exc)


async def _handle_subscribe_to_task(request: Request, req_id: Any, params: dict[str, Any]) -> Any:
    err = _check_streaming(request, req_id)
    if err is not None:
        return err
    task_id = params.get("id")
    if not task_id:
        return _error_response(
            req_id,
            code=VALIDATION_ERROR.json_rpc_code,
            message="Missing 'id' in params",
            reason=VALIDATION_ERROR.reason,
        )
    after_event_id = request.headers.get("Last-Event-ID")
    middlewares: list[A2AMiddleware] = getattr(request.app.state, "middlewares", [])
    envelope = RequestEnvelope()
    envelope.context["a2a_version"] = "1.0"
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
            # Surface the failure to error-aware middleware (TracingMiddleware).
            envelope.context["_a2a_error"] = inner_exc
            for mw in reversed(started):
                await mw.after_dispatch(envelope)
            raise
    except Exception as exc:
        return _map_exception(req_id, exc)

    async def _sse_gen() -> AsyncIterator[str]:
        task_cache: dict[str, v10.Task] = {}
        try:
            eid, first = first_pair
            # Convention: a DirectReply arriving as the first event
            # (reachable via Last-Event-ID replay) is emitted as a message
            # event — uniform across all SSE endpoints.
            payload = wrap_stream_event_v10(first, task_cache)
            if payload is not None:
                envelope_out = {"jsonrpc": "2.0", "id": req_id, "result": payload}
                yield f"id: {eid or ''}\ndata: {json.dumps(envelope_out)}\n\n"
            async for event_id, event in agen:
                if isinstance(event, DirectReply):
                    continue
                payload = wrap_stream_event_v10(event, task_cache)
                if payload is not None:
                    envelope_out = {"jsonrpc": "2.0", "id": req_id, "result": payload}
                    yield f"id: {event_id or ''}\ndata: {json.dumps(envelope_out)}\n\n"
                if isinstance(event, TerminalMarker):
                    break
        except Exception:
            logger.exception("JSON-RPC v1.0 SSE subscribe stream aborted")
        finally:
            try:
                await agen.aclose()
            finally:
                for mw in reversed(started):
                    await mw.after_dispatch(envelope)

    return StreamingResponse(_sse_gen(), media_type="text/event-stream")


async def _handle_health(_req: Request, req_id: Any, _params: dict[str, Any]) -> JSONResponse:
    return _result_response(req_id, {"status": "ok"})


def _check_push_supported(request: Request, req_id: Any) -> JSONResponse | None:
    """Return an error response if push notifications are not enabled, else None."""
    caps = getattr(request.app.state, "capabilities", None)
    if not caps or not caps.push_notifications:
        return _error_response(
            req_id,
            code=-32003,
            message="Push notifications are not supported",
            reason="PUSH_NOTIFICATIONS_NOT_SUPPORTED",
        )
    return None


async def _handle_push_create(
    request: Request, req_id: Any, params: dict[str, Any]
) -> JSONResponse:
    err = _check_push_supported(request, req_id)
    if err is not None:
        return err
    # v1.0 body is a flat TaskPushNotificationConfig.
    task_id = params.get("taskId") or params.get("task_id")
    if not task_id:
        return _error_response(
            req_id,
            code=VALIDATION_ERROR.json_rpc_code,
            message="Missing taskId",
            reason=VALIDATION_ERROR.reason,
        )
    push_store = _get_push_store(request)
    storage = _get_storage(request)
    # v1.0 params carry the flat webhook fields; the shared handler
    # validates them as a flat PushNotificationConfig.
    config_data = {
        "id": params.get("id"),
        "url": params.get("url"),
        "token": params.get("token"),
        "authentication": params.get("authentication"),
    }
    try:
        from a2akit.push.endpoints import _serialize_tpnc, handle_set_config

        result = await handle_set_config(push_store, storage, task_id, config_data)
        return _result_response(req_id, _serialize_tpnc(result))
    except Exception as exc:
        return _map_exception(req_id, exc)


async def _handle_push_get(request: Request, req_id: Any, params: dict[str, Any]) -> JSONResponse:
    err = _check_push_supported(request, req_id)
    if err is not None:
        return err
    task_id = params.get("taskId") or params.get("task_id") or params.get("id")
    if not task_id:
        return _error_response(
            req_id,
            code=VALIDATION_ERROR.json_rpc_code,
            message="Missing taskId",
            reason=VALIDATION_ERROR.reason,
        )
    config_id = params.get("id") or params.get("configId")
    push_store = _get_push_store(request)
    storage = _get_storage(request)
    try:
        from a2akit.push.endpoints import _serialize_tpnc, handle_get_config

        result = await handle_get_config(push_store, storage, task_id, config_id)
        return _result_response(req_id, _serialize_tpnc(result))
    except Exception as exc:
        return _map_exception(req_id, exc)


async def _handle_push_list(request: Request, req_id: Any, params: dict[str, Any]) -> JSONResponse:
    err = _check_push_supported(request, req_id)
    if err is not None:
        return err
    task_id = params.get("taskId") or params.get("task_id") or params.get("id")
    if not task_id:
        return _error_response(
            req_id,
            code=VALIDATION_ERROR.json_rpc_code,
            message="Missing taskId",
            reason=VALIDATION_ERROR.reason,
        )
    push_store = _get_push_store(request)
    storage = _get_storage(request)
    try:
        from a2akit.push.endpoints import _serialize_tpnc, handle_list_configs

        configs = await handle_list_configs(push_store, storage, task_id)
        return _result_response(req_id, {"configs": [_serialize_tpnc(c) for c in configs]})
    except Exception as exc:
        return _map_exception(req_id, exc)


async def _handle_push_delete(
    request: Request, req_id: Any, params: dict[str, Any]
) -> JSONResponse:
    err = _check_push_supported(request, req_id)
    if err is not None:
        return err
    task_id = params.get("taskId") or params.get("task_id")
    config_id = params.get("id") or params.get("configId")
    if not task_id or not config_id:
        return _error_response(
            req_id,
            code=VALIDATION_ERROR.json_rpc_code,
            message="Missing taskId or config id",
            reason=VALIDATION_ERROR.reason,
        )
    push_store = _get_push_store(request)
    storage = _get_storage(request)
    try:
        from a2akit.push.endpoints import handle_delete_config

        await handle_delete_config(push_store, storage, task_id, config_id)
        return _result_response(req_id, None)
    except Exception as exc:
        return _map_exception(req_id, exc)


async def _handle_get_extended_card(
    request: Request, req_id: Any, _params: dict[str, Any]
) -> JSONResponse:
    provider = getattr(request.app.state, "extended_card_provider", None)
    if provider is None:
        return _error_response(
            req_id,
            code=-32007,
            message="Authenticated Extended Card not configured",
            reason="EXTENDED_CARD_NOT_CONFIGURED",
        )
    try:
        from a2akit.agent_card import AgentCardConfig, build_agent_card_v10, external_base_url

        extended_config: AgentCardConfig = await provider(request)
        base_url = external_base_url(
            dict(request.headers),
            request.url.scheme,
            request.url.netloc,
        )
        extra_protos = getattr(request.app.state, "additional_protocols", None)
        card = build_agent_card_v10(extended_config, base_url, extra_protos)
        return _result_response(
            req_id, card.model_dump(mode="json", by_alias=True, exclude_none=True)
        )
    except Exception as exc:
        # Never echo str(exc) for arbitrary exceptions — log server-side.
        logger.error("Failed to build the authenticated extended card", exc_info=exc)
        return _error_response(
            req_id,
            code=-32603,
            message="Internal error",
            reason="INTERNAL_ERROR",
        )


_JSONRPC_DISPATCH.update(
    {
        "SendMessage": _handle_send_message,
        "SendStreamingMessage": _handle_send_streaming_message,
        "GetTask": _handle_get_task,
        "ListTasks": _handle_list_tasks,
        "CancelTask": _handle_cancel_task,
        "SubscribeToTask": _handle_subscribe_to_task,
        "CreateTaskPushNotificationConfig": _handle_push_create,
        "GetTaskPushNotificationConfig": _handle_push_get,
        "ListTaskPushNotificationConfigs": _handle_push_list,
        "DeleteTaskPushNotificationConfig": _handle_push_delete,
        "GetExtendedAgentCard": _handle_get_extended_card,
        "health": _handle_health,
    }
)


__all__ = ["build_jsonrpc_router_v10"]
