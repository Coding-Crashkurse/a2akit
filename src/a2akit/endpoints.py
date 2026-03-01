"""FastAPI router with all A2A protocol endpoints."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from a2a.types import (
    MessageSendParams,
    Task,
    TaskState,
)
from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from sse_starlette import EventSourceResponse

from a2akit.agent_card import AgentCardConfig, build_agent_card, external_base_url
from a2akit.schema import DirectReply, StreamEvent
from a2akit.storage.base import ListTasksQuery, TaskNotCancelableError, TaskNotFoundError

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

    # Pre-1.0: minor versions are breaking, match both
    # Post-1.0: only major needs to match (standard semver)
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
    tm = getattr(request.app.state, "task_manager", None)
    if tm is None:
        raise HTTPException(status_code=503, detail="TaskManager not initialized")
    return tm


def _validate_ids(params: MessageSendParams) -> MessageSendParams:
    """Validate that messageId is present."""
    msg = params.message
    if not msg.message_id or not msg.message_id.strip():
        raise HTTPException(
            status_code=400,
            detail={"code": -32600, "message": "messageId is required."},
        )
    return params


def build_a2a_router() -> APIRouter:
    """Build and return the complete A2A API router."""
    router = APIRouter(dependencies=[Depends(_check_a2a_version)])

    @router.post("/v1/message:send")
    async def message_send(request: Request, params: MessageSendParams) -> JSONResponse:
        """Submit a message and return the task or message directly.

        Returns a JSON-serialized ``Task`` object in the normal case.
        When the worker used ``reply_directly()``, returns a JSON-serialized
        ``Message`` instead (A2A spec §3.1.1).  Both are wrapped in a
        ``JSONResponse`` — callers should inspect the ``kind`` field
        (present on Task, absent on Message) to discriminate.
        """
        params = _validate_ids(params)
        tm = _get_tm(request)
        result = await tm.send_message(params)
        if isinstance(result, Task):
            result = _sanitize_task_for_client(result)
        return JSONResponse(
            content=json.loads(result.model_dump_json(by_alias=True, exclude_none=True))
        )

    @router.post("/v1/message:stream")
    async def message_stream(request: Request, params: MessageSendParams) -> EventSourceResponse:
        """Submit a message and stream events via SSE."""
        params = _validate_ids(params)
        tm = _get_tm(request)
        agen = tm.stream_message(params)
        first_event = await anext(agen)

        async def sse_gen() -> AsyncIterator[str]:
            """Yield JSON-serialized events for the SSE response."""
            try:
                yield _wrap_stream_event(first_event)
                async for ev in agen:
                    # DirectReply is an internal framework event for the
                    # non-streaming path. In SSE, skip it — the client
                    # sees the completed status via TaskStatusUpdateEvent.
                    if isinstance(ev, DirectReply):
                        continue
                    yield _wrap_stream_event(ev)
            except Exception:
                logger.exception("SSE stream aborted")
                return

        return EventSourceResponse(sse_gen())

    @router.get("/v1/tasks/{task_id}")
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

    @router.get("/v1/tasks")
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

    @router.post("/v1/tasks/{task_id}:cancel")
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

    @router.post("/v1/tasks/{task_id}:subscribe")
    async def tasks_subscribe(
        request: Request,
        task_id: str = Path(),
        last_event_id: str | None = Header(None, alias="Last-Event-ID"),
    ) -> EventSourceResponse:
        """Subscribe to updates for an existing task via SSE."""
        tm = _get_tm(request)
        agen = tm.subscribe_task(task_id, after_event_id=last_event_id)
        first_event = await anext(agen)

        async def sse_gen() -> AsyncIterator[str]:
            """Yield JSON-serialized events for the SSE response."""
            try:
                yield _wrap_stream_event(first_event)
                async for ev in agen:
                    if isinstance(ev, DirectReply):
                        continue
                    yield _wrap_stream_event(ev)
            except Exception:
                logger.exception("SSE subscribe stream aborted")
                return

        return EventSourceResponse(sse_gen())

    @router.get("/v1/health")
    async def health_check():
        """Return a simple health status."""
        return {"status": "ok"}

    return router


def build_discovery_router(card_config: AgentCardConfig) -> APIRouter:
    """Build the agent card discovery router."""

    router = APIRouter()

    @router.get("/.well-known/agent-card.json")
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
