"""JSON-RPC 2.0 protocol binding for A2A v0.3."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from a2a.types import MessageSendParams, Task
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.responses import StreamingResponse

from a2akit.endpoints import _check_a2a_version, _sanitize_task_for_client
from a2akit.middleware import A2AMiddleware, RequestEnvelope
from a2akit.schema import DirectReply
from a2akit.storage import ContextMismatchError, TaskNotAcceptingMessagesError
from a2akit.storage.base import (
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


def _serialize(obj: Task | Any) -> Any:
    """Serialize a pydantic model to a JSON-compatible dict."""
    if isinstance(obj, Task):
        obj = _sanitize_task_for_client(obj)
    return json.loads(obj.model_dump_json(by_alias=True, exclude_none=True))


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
    from a2akit.push.endpoints import PushConfigNotFoundError

    if isinstance(exc, PushConfigNotFoundError):
        return _error_response(req_id, TASK_NOT_FOUND, str(exc))
    return _error_response(req_id, INTERNAL_ERROR, str(exc))


def _get_tm(request: Request) -> TaskManager:
    """Extract the TaskManager from app state."""
    tm: TaskManager | None = getattr(request.app.state, "task_manager", None)
    if tm is None:
        return None  # type: ignore[return-value]
    return tm


def build_jsonrpc_router() -> APIRouter:
    """Build the JSON-RPC 2.0 A2A router."""
    router = APIRouter(dependencies=[Depends(_check_a2a_version)])

    async def _parse_body(request: Request) -> tuple[Any, dict[str, Any]] | JSONResponse:
        """Parse and validate the JSON-RPC envelope. Returns (req_id, body) or error response."""
        try:
            body = await request.json()
        except Exception:
            return _error_response(None, PARSE_ERROR, "Parse error")

        if not isinstance(body, dict):
            return _error_response(None, INVALID_REQUEST, "Invalid Request")

        if body.get("jsonrpc") != "2.0":
            return _error_response(
                body.get("id"), INVALID_REQUEST, "Invalid Request: jsonrpc must be '2.0'"
            )

        if not isinstance(body.get("method"), str):
            return _error_response(
                body.get("id"), INVALID_REQUEST, "Invalid Request: method must be a string"
            )

        return body.get("id"), body

    @router.post("/")
    async def jsonrpc_endpoint(request: Request) -> Any:
        """Single JSON-RPC 2.0 endpoint."""
        parsed = await _parse_body(request)
        if isinstance(parsed, JSONResponse):
            return parsed

        req_id, body = parsed
        method = body["method"]
        params = body.get("params", {})

        dispatch = {
            "message/send": _handle_message_send,
            "message/sendStream": _handle_message_send_stream,
            "tasks/get": _handle_tasks_get,
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

        handler = dispatch.get(method)
        if handler is None:
            return _error_response(
                req_id, METHOD_NOT_FOUND, f"Method not found: {method}", {"method": method}
            )

        return await handler(request, req_id, params)

    return router


async def _handle_message_send(
    request: Request, req_id: Any, params: dict[str, Any]
) -> JSONResponse:
    """Handle message/send."""
    try:
        send_params = MessageSendParams.model_validate(params)
    except (ValidationError, Exception):
        return _error_response(req_id, INVALID_PARAMS, "Invalid params for message/send")

    msg = send_params.message
    if not msg.message_id or not msg.message_id.strip():
        return _error_response(req_id, INVALID_PARAMS, "messageId is required")

    tm = _get_tm(request)
    middlewares: list[A2AMiddleware] = getattr(request.app.state, "middlewares", [])

    try:
        envelope = RequestEnvelope(params=send_params)
        for mw in middlewares:
            await mw.before_dispatch(envelope, request)

        result = await tm.send_message(envelope.params, request_context=envelope.context)

        for mw in reversed(middlewares):
            await mw.after_dispatch(envelope, result)

        return _result_response(req_id, _serialize(result))
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
    """Handle message/sendStream — returns SSE."""
    err = _check_streaming(request, req_id)
    if err is not None:
        return err
    try:
        send_params = MessageSendParams.model_validate(params)
    except (ValidationError, Exception):
        return _error_response(req_id, INVALID_PARAMS, "Invalid params for message/sendStream")

    msg = send_params.message
    if not msg.message_id or not msg.message_id.strip():
        return _error_response(req_id, INVALID_PARAMS, "messageId is required")

    tm = _get_tm(request)
    middlewares: list[A2AMiddleware] = getattr(request.app.state, "middlewares", [])

    try:
        envelope = RequestEnvelope(params=send_params)
        for mw in middlewares:
            await mw.before_dispatch(envelope, request)

        agen = tm.stream_message(envelope.params, request_context=envelope.context)
    except Exception as exc:
        return _map_exception_to_error(req_id, exc)

    async def _sse_generator() -> AsyncIterator[str]:
        try:
            async for event in agen:
                if isinstance(event, DirectReply):
                    continue
                payload = {"jsonrpc": "2.0", "id": req_id, "result": _serialize(event)}
                yield f"data: {json.dumps(payload)}\n\n"
        except Exception:
            logger.exception("JSON-RPC SSE stream aborted")

    return StreamingResponse(_sse_generator(), media_type="text/event-stream")


async def _handle_tasks_get(request: Request, req_id: Any, params: dict[str, Any]) -> JSONResponse:
    """Handle tasks/get."""
    task_id = params.get("id")
    if not task_id:
        return _error_response(req_id, INVALID_PARAMS, "Missing 'id' in params")

    history_length = params.get("historyLength")
    tm = _get_tm(request)

    try:
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
    tm = _get_tm(request)

    try:
        query = ListTasksQuery(
            context_id=params.get("contextId"),
            status=params.get("status"),
            page_size=params.get("pageSize", 50),
            page_token=params.get("pageToken"),
            history_length=params.get("historyLength"),
            status_timestamp_after=params.get("statusTimestampAfter"),
            include_artifacts=params.get("includeArtifacts", False),
        )
    except (ValidationError, Exception):
        return _error_response(req_id, INVALID_PARAMS, "Invalid params for tasks/list")

    try:
        result = await tm.list_tasks(query)
        result.tasks = [_sanitize_task_for_client(t) for t in result.tasks]
        return _result_response(
            req_id,
            json.loads(result.model_dump_json(by_alias=True, exclude_none=True)),
        )
    except Exception as exc:
        return _map_exception_to_error(req_id, exc)


async def _handle_tasks_cancel(
    request: Request, req_id: Any, params: dict[str, Any]
) -> JSONResponse:
    """Handle tasks/cancel."""
    task_id = params.get("id")
    if not task_id:
        return _error_response(req_id, INVALID_PARAMS, "Missing 'id' in params")

    tm = _get_tm(request)

    try:
        result = await tm.cancel_task(task_id)
        return _result_response(req_id, _serialize(result))
    except Exception as exc:
        return _map_exception_to_error(req_id, exc)


async def _handle_tasks_resubscribe(request: Request, req_id: Any, params: dict[str, Any]) -> Any:
    """Handle tasks/resubscribe — returns SSE."""
    err = _check_streaming(request, req_id)
    if err is not None:
        return err
    task_id = params.get("id")
    if not task_id:
        return _error_response(req_id, INVALID_PARAMS, "Missing 'id' in params")

    tm = _get_tm(request)

    try:
        agen = tm.subscribe_task(task_id)
    except Exception as exc:
        return _map_exception_to_error(req_id, exc)

    async def _sse_generator() -> AsyncIterator[str]:
        try:
            async for event in agen:
                if isinstance(event, DirectReply):
                    continue
                payload = {"jsonrpc": "2.0", "id": req_id, "result": _serialize(event)}
                yield f"data: {json.dumps(payload)}\n\n"
        except Exception:
            logger.exception("JSON-RPC SSE resubscribe stream aborted")

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
        from a2akit.push.endpoints import _serialize_tpnc, handle_set_config

        result = await handle_set_config(push_store, storage, task_id, config_data)
        return _result_response(req_id, _serialize_tpnc(result))
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
        from a2akit.push.endpoints import _serialize_tpnc, handle_get_config

        result = await handle_get_config(push_store, storage, task_id, config_id)
        return _result_response(req_id, _serialize_tpnc(result))
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
        from a2akit.push.endpoints import _serialize_tpnc, handle_list_configs

        configs = await handle_list_configs(push_store, storage, task_id)
        return _result_response(req_id, [_serialize_tpnc(c) for c in configs])
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
        card = build_agent_card(extended_config, base_url)
        return _result_response(
            req_id,
            json.loads(card.model_dump_json(by_alias=True, exclude_none=True)),
        )
    except Exception as exc:
        return _error_response(req_id, INTERNAL_ERROR, str(exc))
