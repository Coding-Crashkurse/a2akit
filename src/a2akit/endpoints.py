"""FastAPI router with all A2A protocol endpoints."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterable
from typing import TYPE_CHECKING, Any

from a2a.types import (
    MessageSendParams,
    Task,
    TaskState,
)
from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent

from a2akit.agent_card import AgentCardConfig, build_agent_card, external_base_url
from a2akit.middleware import A2AMiddleware, RequestEnvelope
from a2akit.schema import DirectReply, StreamEvent
from a2akit.storage.base import (
    ListTasksQuery,
    TaskNotCancelableError,
    TaskNotFoundError,
    UnsupportedOperationError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from a2akit.task_manager import TaskManager

SUPPORTED_A2A_VERSION = "0.3.0"

logger = logging.getLogger(__name__)


def _sanitize_task_for_client(task: Task) -> Task:
    """Strip framework-internal metadata keys before client serialization.

    Keys prefixed with ``_`` are internal (e.g. ``_a2akit_direct_reply``,
    ``_idempotency_key``) and must not leak to external clients.
    """
    md = task.metadata
    if not md:
        return task
    cleaned = {k: v for k, v in md.items() if not k.startswith("_")}
    if len(cleaned) == len(md):
        return task  # nothing to strip
    return task.model_copy(update={"metadata": cleaned or None})


def _wrap_stream_event(event: StreamEvent) -> str:
    """Serialize a stream event for SSE (HTTP+JSON/REST transport).

    REST transport uses the object's ``kind`` field as discriminator.
    No wrapper envelope needed (unlike JSON-RPC transport).
    """
    if isinstance(event, DirectReply):
        return event.message.model_dump_json(by_alias=True, exclude_none=True)
    if isinstance(event, Task):
        event = _sanitize_task_for_client(event)
    return event.model_dump_json(by_alias=True, exclude_none=True)


def _check_a2a_version(
    a2a_version: str | None = Header(None, alias="A2A-Version"),
) -> None:
    """Validate the A2A-Version request header (spec §3.6.2).

    Pre-1.0: match Major.Minor exactly (breaking changes between minors).
    Post-1.0: match Major only (semver guarantees within major).
    Missing header is tolerated — spec says assume 0.3.
    """
    if a2a_version is None:
        return

    def _major_minor(v: str) -> tuple[str, str]:
        parts = v.strip().split(".")
        return (parts[0], parts[1] if len(parts) > 1 else "0")

    client = _major_minor(a2a_version)
    supported = _major_minor(SUPPORTED_A2A_VERSION)

    if supported[0] == "0":
        compatible = client == supported
    else:
        compatible = client[0] == supported[0]

    if not compatible:
        raise HTTPException(
            status_code=400,
            detail={
                "code": -32009,
                "message": (
                    f"Unsupported A2A version: {a2a_version}. "
                    f"This server supports {SUPPORTED_A2A_VERSION}."
                ),
            },
        )


def _get_tm(request: Request) -> TaskManager:
    """Extract the TaskManager from app state."""
    tm: TaskManager | None = getattr(request.app.state, "task_manager", None)
    if tm is None:
        raise HTTPException(status_code=503, detail="TaskManager not initialized")
    return tm


def _check_streaming(request: Request) -> None:
    """Raise UnsupportedOperationError if streaming is not enabled."""
    caps = getattr(request.app.state, "capabilities", None)
    if caps is not None and not caps.streaming:
        raise UnsupportedOperationError("Streaming is not supported by this agent")


def _check_push_supported(request: Request) -> None:
    """Raise 501 if push notifications are not enabled."""
    caps = getattr(request.app.state, "capabilities", None)
    if not caps or not caps.push_notifications:
        raise HTTPException(
            status_code=501,
            detail={"code": -32003, "message": "Push notifications are not supported"},
        )


def _get_push_store(request: Request) -> Any:
    """Extract the PushConfigStore from app state."""
    store = getattr(request.app.state, "push_store", None)
    if store is None:
        raise HTTPException(
            status_code=501,
            detail={"code": -32003, "message": "Push notifications are not configured"},
        )
    return store


def _get_storage(request: Request) -> Any:
    """Extract the Storage from app state."""
    storage = getattr(request.app.state, "storage", None)
    if storage is None:
        raise HTTPException(status_code=503, detail="Storage not initialized")
    return storage


def _validate_ids(params: MessageSendParams) -> MessageSendParams:
    """Validate that messageId is present."""
    msg = params.message
    if not msg.message_id or not msg.message_id.strip():
        raise HTTPException(
            status_code=400,
            detail={"code": -32600, "message": "messageId is required."},
        )
    return params


async def _stream_setup(
    request: Request,
    params: MessageSendParams,
) -> tuple[StreamEvent, AsyncIterator[StreamEvent]]:
    """Dependency: validate, run middleware, and fetch the first stream event.

    Runs in normal async context so that exceptions produce proper
    HTTP error responses instead of being wrapped in ExceptionGroup.
    """
    _check_streaming(request)
    params = _validate_ids(params)
    tm = _get_tm(request)
    middlewares: list[A2AMiddleware] = getattr(request.app.state, "middlewares", [])

    envelope = RequestEnvelope(params=params)
    for mw in middlewares:
        await mw.before_dispatch(envelope, request)

    agen = tm.stream_message(envelope.params, request_context=envelope.context)
    first_event = await anext(agen)
    return first_event, agen


async def _subscribe_setup(
    request: Request,
    task_id: str = Path(),
    last_event_id: str | None = Header(None, alias="Last-Event-ID"),
) -> tuple[StreamEvent, AsyncIterator[StreamEvent]]:
    """Dependency: look up task, validate state, and fetch the first event.

    Runs in normal async context so that TaskNotFoundError and
    UnsupportedOperationError produce proper HTTP error responses.
    """
    _check_streaming(request)
    tm = _get_tm(request)
    agen = tm.subscribe_task(task_id, after_event_id=last_event_id)
    first_event = await anext(agen)
    return first_event, agen


def build_a2a_router() -> APIRouter:
    """Build and return the complete A2A API router."""
    router = APIRouter(dependencies=[Depends(_check_a2a_version)])

    @router.post("/v1/message:send", tags=["Messages"])
    async def message_send(request: Request, params: MessageSendParams) -> JSONResponse:
        """Submit a message and return the task or message directly."""
        params = _validate_ids(params)
        tm = _get_tm(request)
        middlewares: list[A2AMiddleware] = getattr(request.app.state, "middlewares", [])

        envelope = RequestEnvelope(params=params)

        for mw in middlewares:
            await mw.before_dispatch(envelope, request)

        result = await tm.send_message(envelope.params, request_context=envelope.context)

        for mw in reversed(middlewares):
            await mw.after_dispatch(envelope, result)

        if isinstance(result, Task):
            result = _sanitize_task_for_client(result)
        return JSONResponse(
            content=json.loads(result.model_dump_json(by_alias=True, exclude_none=True))
        )

    @router.post("/v1/message:stream", response_class=EventSourceResponse, tags=["Messages"])
    async def message_stream(
        setup: tuple[StreamEvent, AsyncIterator[StreamEvent]] = Depends(_stream_setup),
    ) -> AsyncIterable[ServerSentEvent]:
        """Submit a message and stream events via SSE.

        All fallible setup (validation, middleware, first event) runs in
        the ``_stream_setup`` dependency — in the normal request context
        where exceptions produce proper HTTP error responses.

        This generator body runs inside FastAPI's SSE producer TaskGroup
        and only contains yield statements.
        """
        first_event, agen = setup
        try:
            yield ServerSentEvent(raw_data=_wrap_stream_event(first_event))
            async for ev in agen:
                if isinstance(ev, DirectReply):
                    continue
                yield ServerSentEvent(raw_data=_wrap_stream_event(ev))
        except Exception:
            logger.exception("SSE stream aborted")

    @router.get("/v1/tasks/{task_id}", tags=["Tasks"])
    async def tasks_get(
        request: Request,
        task_id: str = Path(),
        history_length: int | None = Query(None, alias="historyLength"),
    ) -> JSONResponse:
        """Get a single task by ID."""
        tm = _get_tm(request)
        t = await tm.get_task(task_id, history_length)
        if not t:
            raise HTTPException(
                status_code=404, detail={"code": -32001, "message": "Task not found"}
            )
        t = _sanitize_task_for_client(t)
        return JSONResponse(
            content=json.loads(t.model_dump_json(by_alias=True, exclude_none=True))
        )

    @router.get("/v1/tasks", tags=["Tasks"])
    async def tasks_list(
        request: Request,
        context_id: str | None = Query(None, alias="contextId"),
        status: TaskState | None = None,
        page_size: int = Query(50, alias="pageSize"),
        page_token: str | None = Query(None, alias="pageToken"),
        history_length: int | None = Query(None, alias="historyLength"),
        status_timestamp_after: str | None = Query(None, alias="statusTimestampAfter"),
        include_artifacts: bool = Query(False, alias="includeArtifacts"),
    ) -> JSONResponse:
        """List tasks with optional filters and pagination."""
        tm = _get_tm(request)
        query = ListTasksQuery(
            context_id=context_id,
            status=status,
            page_size=page_size,
            page_token=page_token,
            history_length=history_length,
            status_timestamp_after=status_timestamp_after,
            include_artifacts=include_artifacts,
        )
        result = await tm.list_tasks(query)
        result.tasks = [_sanitize_task_for_client(t) for t in result.tasks]
        return JSONResponse(
            content=json.loads(result.model_dump_json(by_alias=True, exclude_none=True))
        )

    @router.post("/v1/tasks/{task_id}:cancel", tags=["Tasks"])
    async def tasks_cancel(
        request: Request,
        task_id: str = Path(),
    ) -> JSONResponse:
        """Cancel a task by ID."""
        tm = _get_tm(request)
        try:
            result = await tm.cancel_task(task_id)
            result = _sanitize_task_for_client(result)
            return JSONResponse(
                content=json.loads(result.model_dump_json(by_alias=True, exclude_none=True))
            )
        except TaskNotFoundError as err:
            raise HTTPException(
                status_code=404, detail={"code": -32001, "message": "Task not found"}
            ) from err
        except TaskNotCancelableError as err:
            raise HTTPException(
                status_code=409, detail={"code": -32002, "message": "Task is not cancelable"}
            ) from err

    @router.post(
        "/v1/tasks/{task_id}:subscribe", response_class=EventSourceResponse, tags=["Tasks"]
    )
    async def tasks_subscribe(
        setup: tuple[StreamEvent, AsyncIterator[StreamEvent]] = Depends(_subscribe_setup),
    ) -> AsyncIterable[ServerSentEvent]:
        """Subscribe to updates for an existing task via SSE.

        All fallible setup (task lookup, terminal-state check, first event)
        runs in the ``_subscribe_setup`` dependency — in the normal request
        context where exceptions produce proper HTTP error responses.
        """
        first_event, agen = setup
        try:
            yield ServerSentEvent(raw_data=_wrap_stream_event(first_event))
            async for ev in agen:
                if isinstance(ev, DirectReply):
                    continue
                yield ServerSentEvent(raw_data=_wrap_stream_event(ev))
        except Exception:
            logger.exception("SSE subscribe stream aborted")

    @router.post("/v1/tasks/{task_id}/pushNotificationConfigs", tags=["Push Notifications"])
    async def push_config_set(request: Request, task_id: str = Path()) -> JSONResponse:
        """Set a push notification config for a task."""
        _check_push_supported(request)
        push_store = _get_push_store(request)
        storage = _get_storage(request)
        body = await request.json()
        from a2akit.push.endpoints import _serialize_tpnc, handle_set_config

        result = await handle_set_config(push_store, storage, task_id, body)
        return JSONResponse(content=_serialize_tpnc(result))

    @router.get(
        "/v1/tasks/{task_id}/pushNotificationConfigs/{config_id}",
        tags=["Push Notifications"],
    )
    async def push_config_get_by_id(
        request: Request,
        task_id: str = Path(),
        config_id: str = Path(),
    ) -> JSONResponse:
        """Get a specific push notification config."""
        _check_push_supported(request)
        push_store = _get_push_store(request)
        storage = _get_storage(request)
        from a2akit.push.endpoints import _serialize_tpnc, handle_get_config

        result = await handle_get_config(push_store, storage, task_id, config_id)
        return JSONResponse(content=_serialize_tpnc(result))

    @router.get("/v1/tasks/{task_id}/pushNotificationConfigs", tags=["Push Notifications"])
    async def push_config_list(request: Request, task_id: str = Path()) -> JSONResponse:
        """List all push notification configs for a task."""
        _check_push_supported(request)
        push_store = _get_push_store(request)
        storage = _get_storage(request)
        from a2akit.push.endpoints import _serialize_tpnc, handle_list_configs

        configs = await handle_list_configs(push_store, storage, task_id)
        return JSONResponse(content=[_serialize_tpnc(c) for c in configs])

    @router.delete(
        "/v1/tasks/{task_id}/pushNotificationConfigs/{config_id}",
        tags=["Push Notifications"],
    )
    async def push_config_delete(
        request: Request,
        task_id: str = Path(),
        config_id: str = Path(),
    ) -> JSONResponse:
        """Delete a push notification config."""
        _check_push_supported(request)
        push_store = _get_push_store(request)
        storage = _get_storage(request)
        from a2akit.push.endpoints import handle_delete_config

        await handle_delete_config(push_store, storage, task_id, config_id)
        return JSONResponse(status_code=204, content=None)

    @router.get("/v1/card", tags=["Discovery"])
    async def get_authenticated_extended_card(request: Request) -> JSONResponse:
        """Return the authenticated extended agent card."""
        provider = getattr(request.app.state, "extended_card_provider", None)
        if provider is None:
            raise HTTPException(
                status_code=404,
                detail={"code": -32007, "message": "Authenticated Extended Card not configured"},
            )
        extended_config: AgentCardConfig = await provider(request)
        base_url = external_base_url(
            dict(request.headers),
            request.url.scheme,
            request.url.netloc,
        )
        card = build_agent_card(extended_config, base_url)
        return JSONResponse(
            content=json.loads(card.model_dump_json(by_alias=True, exclude_none=True))
        )

    @router.get("/v1/health", tags=["Health"])
    async def health_check() -> dict[str, str]:
        """Return a simple health status."""
        return {"status": "ok"}

    return router


def build_discovery_router(card_config: AgentCardConfig) -> APIRouter:
    """Build the agent card discovery router."""

    router = APIRouter()

    @router.get("/.well-known/agent-card.json", tags=["Discovery"])
    async def get_agent_card(request: Request) -> JSONResponse:
        """Serve the agent discovery card with the correct base URL."""
        base_url = external_base_url(
            dict(request.headers),
            request.url.scheme,
            request.url.netloc,
        )
        card = build_agent_card(card_config, base_url)
        return JSONResponse(
            content=json.loads(card.model_dump_json(by_alias=True, exclude_none=True))
        )

    return router
