"""Task submission, streaming, querying, and cancellation."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from a2a_pydantic import v10

from a2akit._parts import part_kind
from a2akit.cancel import cancel_task_in_storage
from a2akit.event_emitter import DefaultEventEmitter, EventEmitter
from a2akit.schema import DIRECT_REPLY_KEY, DirectReply, StreamEvent, TerminalMarker
from a2akit.storage.base import (
    TERMINAL_STATES,
    ConcurrencyError,
    ContentTypeNotSupportedError,
    ContextMismatchError,
    ListTasksQuery,
    ListTasksResult,
    TaskNotAcceptingMessagesError,
    TaskNotCancelableError,
    TaskNotFoundError,
    TaskTerminalStateError,
    UnsupportedOperationError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from a2akit.broker import Broker, CancelRegistry
    from a2akit.event_bus.base import EventBus
    from a2akit.push.store import PushConfigStore
    from a2akit.storage import Storage

logger = logging.getLogger(__name__)


def _is_blocking(cfg: v10.SendMessageConfiguration | None) -> bool:
    """Map v10.SendMessageConfiguration.return_immediately → the blocking semantics.

    v0.3 had ``blocking: bool`` (default True = wait). v10 inverts this to
    ``return_immediately: bool``. Per spec the v10 default is "wait" (same
    behaviour), so we treat unset as blocking.
    """
    if cfg is None or cfg.return_immediately is None:
        return True
    return not cfg.return_immediately


def _is_agent_role(role: str | v10.Role | None) -> bool:
    """Check whether a role value represents the agent role.

    v10 enum members are ``role_user`` / ``role_agent`` with value strings
    ``ROLE_USER`` / ``ROLE_AGENT``. Accept both the raw v10 enum and the
    v0.3 legacy ``"agent"`` string for robustness across the v0.3-compat
    boundary (section 11).
    """
    if role is None:
        return False
    if isinstance(role, v10.Role):
        return bool(role == v10.Role.role_agent)
    return role in {"agent", "ROLE_AGENT", "role_agent"}


def _find_direct_reply(task: v10.Task) -> v10.Message | None:
    """Extract direct-reply message if the worker used ``reply_directly()``.

    Checks ``task.metadata`` for the ``_a2akit_direct_reply`` marker
    whose value is the ``message_id`` of the direct-reply message.
    Returns ``None`` for normal task responses.
    """
    task_md = getattr(task, "metadata", None) or {}
    direct_reply_msg_id = task_md.get(DIRECT_REPLY_KEY)
    if not direct_reply_msg_id:
        return None
    if not task.history:
        return None
    for msg in reversed(task.history):
        if getattr(msg, "message_id", None) == direct_reply_msg_id:
            return msg
    return None


def _extract_inline_push_config(params: Any) -> Any:
    """Return the inline push config from ``SendMessageRequest.configuration``.

    Accepts both shapes so this code works whether the caller passed a v1.0
    ``SendMessageConfiguration`` (flat ``task_push_notification_config``
    carrying a ``v10.TaskPushNotificationConfig``) or a legacy v0.3
    ``MessageSendConfiguration`` (nested ``push_notification_config``
    carrying a bare webhook payload). Returns a framework-level
    ``PushNotificationConfig`` ready to hand to ``push_store.set_config``.
    """
    from a2akit.push.models import (
        PushNotificationAuthenticationInfo,
        PushNotificationConfig,
    )

    cfg = getattr(params, "configuration", None)
    if cfg is None:
        return None

    # v1.0 flat shape: configuration.task_push_notification_config
    tpnc = getattr(cfg, "task_push_notification_config", None)
    if tpnc is not None:
        auth = None
        tpnc_auth = getattr(tpnc, "authentication", None)
        if tpnc_auth is not None:
            scheme = getattr(tpnc_auth, "scheme", None)
            auth = PushNotificationAuthenticationInfo(
                schemes=[scheme] if scheme else [],
                credentials=getattr(tpnc_auth, "credentials", None),
            )
        return PushNotificationConfig(
            id=getattr(tpnc, "id", None),
            url=tpnc.url,
            token=getattr(tpnc, "token", None),
            authentication=auth,
        )

    # v0.3 nested shape: configuration.push_notification_config (bare webhook payload).
    pnc = getattr(cfg, "push_notification_config", None)
    if pnc is not None:
        auth = None
        pnc_auth = getattr(pnc, "authentication", None)
        if pnc_auth is not None:
            schemes = list(getattr(pnc_auth, "schemes", None) or [])
            auth = PushNotificationAuthenticationInfo(
                schemes=schemes,
                credentials=getattr(pnc_auth, "credentials", None),
            )
        return PushNotificationConfig(
            id=getattr(pnc, "id", None),
            url=pnc.url,
            token=getattr(pnc, "token", None),
            authentication=auth,
        )

    return None


@dataclass
class TaskManager:
    """High-level API for submitting, streaming, and managing tasks.

    Knows: Broker, Storage, EventBus, CancelRegistry.
    Also creates a ``DefaultEventEmitter`` locally in
    ``_force_cancel_after`` to ensure the cancel write path goes
    through the same Storage+EventBus pipeline as the worker side.
    """

    broker: Broker
    storage: Storage
    event_bus: EventBus
    cancel_registry: CancelRegistry
    default_blocking_timeout_s: float = 30.0
    cancel_force_timeout_s: float = 60.0
    emitter: EventEmitter | None = None
    push_store: PushConfigStore | None = None
    input_modes: list[str] = field(default_factory=list)
    _background_tasks: set[asyncio.Task[Any]] = field(default_factory=set, init=False, repr=False)

    def _track_background(self, coro: Any) -> asyncio.Task[Any]:
        """Create a tracked background task with exception logging."""
        fut = asyncio.create_task(coro)
        self._background_tasks.add(fut)
        fut.add_done_callback(self._on_background_done)
        return fut

    def _on_background_done(self, fut: asyncio.Task[Any]) -> None:
        """Log exceptions from background tasks and remove from tracking set."""
        self._background_tasks.discard(fut)
        if not fut.cancelled() and fut.exception():
            logger.error("Background task failed: %s", fut.exception(), exc_info=fut.exception())

    async def _enqueue_or_fail(self, task_id: str, coro: Any) -> None:
        """Await *coro* (broker.run_task); on failure mark the task as failed.

        Without this wrapper a broker error (e.g. Redis down) would leave
        the task stuck in ``submitted`` forever.  Routes through the emitter
        pipeline so that lifecycle hooks, push delivery, and telemetry all
        fire correctly for broker-failure scenarios.
        """
        try:
            await coro
        except Exception:
            logger.exception("Broker enqueue failed for task %s, marking as failed", task_id)
            try:
                # Guard against stomping a live task: the enqueue may have
                # actually taken effect (error after the send) and a worker
                # may already be processing it. Only mark failed if the task
                # is still in `submitted`, and pin the write with the OCC
                # version (captured BEFORE the load — TOCTOU-safe ordering,
                # see _submit_task) so a concurrent transition rejects us.
                version = await self.storage.get_version(task_id)
                task = await self.storage.load_task(task_id)
                if task is None or task.status.state != v10.TaskState.task_state_submitted:
                    logger.info(
                        "Skipping failed-mark for task %s after broker error: "
                        "task is no longer in submitted state",
                        task_id,
                    )
                    return
                error_msg = v10.Message(
                    role=v10.Role.role_agent,
                    parts=[v10.Part(text="Failed to enqueue task")],
                    message_id=str(uuid.uuid4()),
                    task_id=task_id,
                    context_id=task.context_id if task else "",
                )
                emitter = self.emitter or DefaultEventEmitter(self.event_bus, self.storage)
                try:
                    await emitter.update_task(
                        task_id,
                        state=v10.TaskState.task_state_failed,
                        status_message=error_msg,
                        messages=[error_msg],
                        expected_version=version,
                    )
                except (ConcurrencyError, TaskTerminalStateError):
                    # A worker (or another writer) advanced the task between
                    # our check and the write — its result wins, do nothing.
                    logger.info(
                        "Skipping failed-mark for task %s: concurrent writer advanced the task",
                        task_id,
                    )
                    return
                task = await self.storage.load_task(task_id)
                if task is not None:
                    await emitter.send_event(
                        task_id,
                        TerminalMarker(
                            event=v10.TaskStatusUpdateEvent(
                                task_id=task_id,
                                context_id=task.context_id or "",
                                status=task.status,
                            )
                        ),
                    )
            except Exception:
                logger.exception("Could not mark task %s as failed after broker error", task_id)

    def _validate_input_modes(self, message: v10.Message) -> None:
        """Validate message parts against declared input modes (A2A §8.2 -32005).

        v10 drops TextPart/FilePart/DataPart — use flat ``part_kind`` and
        ``media_type`` attributes from v10.Part instead.
        """
        if not self.input_modes:
            return
        for part in message.parts:
            kind = part_kind(part)
            if kind == "text":
                effective = "text/plain"
            elif kind == "data":
                effective = part.media_type or "application/json"
            elif kind in ("raw", "url"):
                effective = part.media_type or "application/octet-stream"
            else:
                continue
            if effective not in self.input_modes:
                raise ContentTypeNotSupportedError(effective)

    async def _submit_task(self, context_id: str, message: v10.Message) -> tuple[v10.Task, bool]:
        """Route, validate, and persist a user message submission.

        Returns ``(task, should_enqueue)``.  ``should_enqueue`` is False
        when a duplicate follow-up message was detected (idempotency).

        All business rules live here — Storage is pure CRUD.

        For new tasks (no ``message.task_id``): delegates to
        ``storage.create_task``.

        For follow-ups (``message.task_id`` set): loads the task,
        validates preconditions, computes the state transition, and
        persists the message via ``storage.update_task``.
        """
        self._validate_input_modes(message)
        if not message.task_id:
            task = await self.storage.create_task(
                context_id, message, idempotency_key=message.message_id
            )
            # Storage signals genuine insert vs idempotent hit via a
            # transient metadata marker (see storage/base.py contract).
            # Pop it here so it never leaks further into the pipeline.
            # A state-based check (``state == submitted``) is insufficient
            # because a brand-new task AND an idempotent hit on a task
            # whose worker has not yet picked it up are both in the
            # submitted state — that would cause a double enqueue on
            # client retries, and on multi-worker Redis deployments two
            # workers could process the same task in parallel.
            md: dict[str, Any] = dict(task.metadata or {})
            just_created = bool(md.pop("_a2akit_just_created", False))
            # a2a-pydantic ≥0.0.6 coerces dict → Struct on assignment.
            task.metadata = md or None
            return task, just_created

        # Capture the OCC version BEFORE loading and validating state.
        # This closes a TOCTOU window: if a concurrent writer commits
        # between our version read and our update_task write,
        # expected_version will be stale and the storage layer will
        # raise ConcurrencyError (correct rejection).  Fetching the
        # version AFTER validation (the old order) allowed a concurrent
        # writer to slip in a newer version, causing our write to
        # succeed despite validating against stale state — two
        # concurrent follow-ups could both pass the input_required
        # check and both succeed instead of one getting rejected.
        version = await self.storage.get_version(message.task_id)
        task = await self._load_and_validate(message)
        # Idempotency: skip if this message was already appended (client retry).
        if task.history and any(m.message_id == message.message_id for m in task.history):
            return task, False
        # Bind message to task before persisting (message binding contract).
        # Use model_copy to avoid mutating the caller's message object.
        bound_message = message.model_copy(
            update={"context_id": message.context_id or task.context_id}
        )
        new_state = self._compute_state_transition(task)
        # Route through the EventEmitter so lifecycle hooks, SSE subscribers,
        # and push notifications see the follow-up state transition. Writing
        # directly via storage.update_task here would silently skip all three.
        emitter = self.emitter or DefaultEventEmitter(self.event_bus, self.storage)
        try:
            await emitter.update_task(
                task.id,
                state=new_state,
                messages=[bound_message],
                expected_version=version,
            )
        except ConcurrencyError:
            # Parallel retry race: check if our twin request already wrote this message
            reloaded = await self.storage.load_task(task.id)
            if (
                reloaded
                and reloaded.history
                and any(m.message_id == message.message_id for m in reloaded.history)
            ):
                return reloaded, False  # Idempotent duplicate resolved
            # If the task became terminal between our read and write, raise
            # the correct error so clients don't get a misleading "retry" hint.
            if reloaded and reloaded.status.state in TERMINAL_STATES:
                raise TaskTerminalStateError("task is terminal") from None
            raise
        # Broadcast the transition so SSE subscribers, hooks, and push
        # notifications see the state change (this is the mirror image of
        # what WorkerAdapter does after transitioning submitted -> working).
        # v10: no `kind` / `final` fields on the event — intermediate state
        # updates go on the wire as bare TaskStatusUpdateEvent.
        if new_state is not None:
            status = v10.TaskStatus(
                state=new_state,
                timestamp=datetime.now(UTC).isoformat(),
            )
            await emitter.send_event(
                task.id,
                v10.TaskStatusUpdateEvent(
                    task_id=task.id,
                    context_id=task.context_id,
                    status=status,
                ),
            )
        # Re-load to return the updated Task object.
        updated = await self.storage.load_task(task.id)
        if updated is None:
            raise RuntimeError(f"Task {task.id} vanished after update")
        return updated, True

    async def _load_and_validate(self, message: v10.Message) -> v10.Task:
        """Load task and enforce all preconditions.

        Raises:
            TaskNotFoundError: If the task doesn't exist.
            TaskTerminalStateError: If the task is in a terminal state.
            ContextMismatchError: If context IDs don't match.
            TaskNotAcceptingMessagesError: If a non-agent message is sent
                to a task not in ``input_required``.
        """
        assert message.task_id is not None
        task = await self.storage.load_task(message.task_id)
        if task is None:
            raise TaskNotFoundError(f"Task {message.task_id} not found")

        current = task.status.state

        if current in TERMINAL_STATES:
            raise TaskTerminalStateError("task is terminal")

        if message.context_id and task.context_id != message.context_id:
            raise ContextMismatchError(
                f"contextId {message.context_id!r} does not match "
                f"task {message.task_id!r} contextId {task.context_id!r}"
            )

        # v1.0 drops the TaskState.unknown sentinel — only the two interrupt
        # states accept user follow-ups without a prior role=agent handoff.
        if current not in {
            v10.TaskState.task_state_input_required,
            v10.TaskState.task_state_auth_required,
        } and not _is_agent_role(getattr(message, "role", None)):
            raise TaskNotAcceptingMessagesError(current)

        return task

    @staticmethod
    def _compute_state_transition(task: v10.Task) -> v10.TaskState | None:
        """Determine the new state based on current task state."""
        if task.status.state in {
            v10.TaskState.task_state_input_required,
            v10.TaskState.task_state_auth_required,
        }:
            return v10.TaskState.task_state_submitted
        return None

    async def send_message(
        self,
        params: v10.SendMessageRequest,
        request_context: dict[str, Any] | None = None,
    ) -> v10.Task | v10.Message:
        """Submit a task and optionally block until completion.

        Returns a ``Message`` when the worker used ``reply_directly()``
        (direct-message response without task tracking).
        Otherwise returns the ``Task``.
        """
        msg = params.message
        is_new = not msg.task_id
        context_id = msg.context_id or str(uuid.uuid4())
        task, should_enqueue = await self._submit_task(context_id, msg)

        # Persist the tenant from the v10 request onto the task's metadata so
        # ``list_tasks(tenant=...)`` can filter on it later. v10.Message itself
        # has no tenant field, so the only place it's authoritative is
        # ``SendMessageRequest.tenant``.
        if is_new and should_enqueue and params.tenant:
            from a2akit.storage.base import META_TENANT_KEY

            await self.storage.update_task(
                task.id,
                task_metadata={META_TENANT_KEY: params.tenant},
            )

        # Follow-up: use the task's real context_id, not the generated one
        if not is_new:
            context_id = task.context_id

        # Idempotent duplicate follow-up — return current state, don't re-enqueue
        if not should_enqueue:
            # Load FULL task first so we can detect a direct reply even when
            # the client passed history_length=0. Trimming before the lookup
            # would hide the reply message.
            latest_full = await self.storage.load_task(task.id)
            if latest_full is not None:
                reply = _find_direct_reply(latest_full)
                if reply is not None:
                    return reply
            history_len = getattr(getattr(params, "configuration", None), "history_length", None)
            if history_len is not None:
                trimmed = await self.storage.load_task(task.id, history_length=history_len)
                return trimmed or latest_full or task
            return latest_full or task

        # Inline push notification config (A2A v1.0 §3.2.2). The wire model is
        # the flat ``v10.TaskPushNotificationConfig``; we map it onto the
        # framework's :class:`PushNotificationConfig` (which is v1.0-aligned:
        # flat URL/token/authentication, single-scheme wrapping).
        if self.push_store is not None:
            inline = _extract_inline_push_config(params)
            if inline is not None:
                await self.push_store.set_config(task.id, inline)

        params = self._bind_message(params, context_id, task.id)

        direct_message: v10.Message | None = None
        if _is_blocking(params.configuration):
            # Subscribe BEFORE starting broker to avoid race condition:
            # events published between broker.run_task and subscribe would
            # be lost if we subscribed after.
            async with self.event_bus.subscribe(task.id) as sub:
                self._track_background(
                    self._enqueue_or_fail(
                        task.id,
                        self.broker.run_task(
                            params,
                            is_new_task=is_new,
                            request_context=request_context,
                        ),
                    )
                )

                try:
                    async with asyncio.timeout(self.default_blocking_timeout_s):
                        async for _eid, ev in sub:
                            if isinstance(ev, DirectReply):
                                direct_message = ev.message
                            if isinstance(ev, TerminalMarker):
                                break
                except TimeoutError:
                    logger.info("Blocking wait timed out for task %s", task.id)
        else:
            # Non-blocking: just enqueue and return immediately.
            # Wrapped in _enqueue_or_fail so a broker error marks the
            # task as failed instead of leaving it stuck in submitted.
            self._track_background(
                self._enqueue_or_fail(
                    task.id,
                    self.broker.run_task(
                        params,
                        is_new_task=is_new,
                        request_context=request_context,
                    ),
                )
            )

        if direct_message is not None:
            return direct_message

        # Check for direct reply on the FULL task (before history trimming)
        latest_full = await self.storage.load_task(task.id)
        if latest_full is not None:
            reply = _find_direct_reply(latest_full)
            if reply is not None:
                return reply

        history_len = getattr(getattr(params, "configuration", None), "history_length", None)
        if history_len is not None:
            trimmed = await self.storage.load_task(task.id, history_length=history_len)
            return trimmed or latest_full or task
        return latest_full or task

    @staticmethod
    def _bind_message(
        params: v10.SendMessageRequest, context_id: str, task_id: str
    ) -> v10.SendMessageRequest:
        """Return a copy of params with context_id and task_id bound.

        Avoids mutating the caller's SendMessageRequest object.
        """
        updated_msg = params.message.model_copy(
            update={"context_id": context_id, "task_id": task_id}
        )
        return params.model_copy(update={"message": updated_msg})

    async def stream_message(
        self,
        params: v10.SendMessageRequest,
        request_context: dict[str, Any] | None = None,
    ) -> AsyncGenerator[tuple[str | None, StreamEvent], None]:
        """Submit a task, yield initial snapshot, then stream live events.

        Yields ``(event_id, event)`` tuples.  The snapshot has
        ``event_id=None``; bus events carry the bus-assigned ID so that
        SSE endpoints can use it as the ``id:`` field for correct
        ``Last-Event-ID`` reconnection.

        Subscribes to the event bus BEFORE starting the broker to prevent
        a race condition where early events could be lost.
        """
        msg = params.message
        is_new = not msg.task_id
        context_id = msg.context_id or str(uuid.uuid4())
        task, should_enqueue = await self._submit_task(context_id, msg)

        # Persist the tenant onto the task's metadata — same as send_message,
        # so streamed tasks are visible to ``list_tasks(tenant=...)`` too.
        # getattr: legacy v0.3 MessageSendParams (no tenant field) can reach
        # this method through the compat layer.
        tenant = getattr(params, "tenant", None)
        if is_new and should_enqueue and tenant:
            from a2akit.storage.base import META_TENANT_KEY

            await self.storage.update_task(
                task.id,
                task_metadata={META_TENANT_KEY: tenant},
            )

        # Follow-up: use the task's real context_id, not the generated one
        if not is_new:
            context_id = task.context_id

        # REQ-08: Inline push notification config on message/stream.
        if self.push_store is not None:
            inline = _extract_inline_push_config(params)
            if inline is not None:
                await self.push_store.set_config(task.id, inline)

        history_len = getattr(getattr(params, "configuration", None), "history_length", None)
        if history_len is not None:
            trimmed = await self.storage.load_task(task.id, history_length=history_len)
            if trimmed is not None:
                task = trimmed

        if should_enqueue:
            params = self._bind_message(params, context_id, task.id)

        # Subscribe BEFORE yielding snapshot — prevents event loss between
        # the DB read and the subscription setup (same pattern as subscribe_task).
        async with self.event_bus.subscribe(task.id) as sub:
            # For retries/duplicates, re-load the snapshot inside the
            # subscription context so it includes the latest state,
            # honoring history_length like the first-request path above.
            if not should_enqueue:
                fresh = await self.storage.load_task(task.id, history_length=history_len)
                if fresh is not None:
                    task = fresh

            # Enqueue BEFORE the first yield — if the client disconnects
            # right after receiving the snapshot, the task is already in the
            # broker queue and won't become a zombie.
            if should_enqueue:
                self._track_background(
                    self._enqueue_or_fail(
                        task.id,
                        self.broker.run_task(
                            params,
                            is_new_task=is_new,
                            request_context=request_context,
                        ),
                    )
                )

            yield (None, task)

            # Terminal tasks have no further events — end stream immediately.
            if not should_enqueue and task.status.state in TERMINAL_STATES:
                return

            async for event_id, ev in sub:
                yield (event_id, ev)

    async def subscribe_task(
        self, task_id: str, *, after_event_id: str | None = None
    ) -> AsyncGenerator[tuple[str | None, StreamEvent], None]:
        """Subscribe to updates for an existing task.

        Yields ``(event_id, event)`` tuples.  The initial task snapshot
        has ``event_id=None``; bus events carry the bus-assigned ID.
        When ``after_event_id`` is provided (from SSE ``Last-Event-ID``
        header), backends that support replay (e.g. Redis Streams)
        deliver events published after that ID.
        Raises ``UnsupportedOperationError`` if the task is in a terminal state.
        """
        # Subscribe BEFORE loading — guarantees no events are missed
        # between the DB read and the subscription setup.
        async with self.event_bus.subscribe(task_id, after_event_id=after_event_id) as sub:
            task = await self.storage.load_task(task_id)
            if task is None:
                raise TaskNotFoundError(f"Task {task_id} not found")
            if task.status.state in TERMINAL_STATES and after_event_id is None:
                raise UnsupportedOperationError("Task is in a terminal state; cannot subscribe")

            # On reconnect (after_event_id set), skip the snapshot to avoid
            # data duplication — the replay events already cover the gap.
            if after_event_id is None:
                yield (None, task)

            # Reconnect to terminal task after cleanup: replay buffer is gone,
            # no live events will arrive. Yield final snapshot instead of hanging.
            if after_event_id is not None and task.status.state in TERMINAL_STATES:
                yield (None, task)
                return

            async for event_id, ev in sub:
                yield (event_id, ev)

    async def get_task(self, task_id: str, history_length: int | None = None) -> v10.Task | None:
        """Load a single task by ID."""
        return await self.storage.load_task(task_id, history_length)

    async def list_tasks(self, query: ListTasksQuery) -> ListTasksResult:
        """Return filtered and paginated tasks."""
        return await self.storage.list_tasks(query)

    async def cancel_task(self, task_id: str) -> v10.Task:
        """Request cancellation of a task and return its current state.

        Signals the cancel registry so the worker can cooperatively cancel.
        If the worker does not transition to ``canceled`` within
        ``cancel_force_timeout_s`` seconds, a background task will
        force the state transition to prevent tasks from being stuck
        forever.

        Cancel always goes through the CancelRegistry — there is no
        instant-cancel path for ``submitted`` tasks.  This avoids a
        race condition where both the TaskManager and the WorkerAdapter
        could write to the same task concurrently (the worker may
        dequeue the task between load_task and the state write).

        The worker checks ``is_cancelled`` before transitioning to
        ``working``, so submitted tasks are canceled promptly when
        dequeued.

        Raises:
            TaskNotFoundError: If the task does not exist.
            TaskNotCancelableError: If the task is already in a terminal state
                (A2A §3.1.5 — 409 Conflict).
        """
        task = await self.storage.load_task(task_id)
        if task is None:
            raise TaskNotFoundError(f"Task {task_id} not found")

        if task.status.state in TERMINAL_STATES:
            raise TaskNotCancelableError(
                f"Task {task_id} is in terminal state {task.status.state.value}"
            )

        # Deduplicate: if already being cancelled, just return current state
        if await self.cancel_registry.is_cancelled(task_id):
            latest = await self.storage.load_task(task_id)
            if latest is None:
                raise TaskNotFoundError(f"Task {task_id} disappeared during cancel")
            return latest

        await self.cancel_registry.request_cancel(task_id)

        # Force-cancel fallback for the case where the worker doesn't react.
        self._track_background(self._force_cancel_after(task_id, self.cancel_force_timeout_s))

        latest = await self.storage.load_task(task_id)
        if latest is None:
            raise TaskNotFoundError(f"Task {task_id} disappeared during cancel")
        return latest

    async def _force_cancel_after(self, task_id: str, deadline: float) -> None:
        """Force-cancel a task if it hasn't reached a terminal state.

        Waits ``deadline`` seconds, then checks Storage.  If the task is
        still non-terminal, transitions it to ``canceled`` directly,
        publishes a final status event so SSE subscribers can close,
        and cleans up EventBus and CancelRegistry resources.
        """
        await asyncio.sleep(deadline)
        try:
            task = await self.storage.load_task(task_id)
            if task is None:
                return
            if task.status.state not in TERMINAL_STATES:
                logger.warning(
                    "Force-canceling task %s after %ss timeout (worker did not cooperate)",
                    task_id,
                    deadline,
                )
                emitter = self.emitter or DefaultEventEmitter(self.event_bus, self.storage)
                await cancel_task_in_storage(
                    self.storage,
                    emitter,
                    task_id,
                    task.context_id,
                    reason="Task was force-canceled after timeout.",
                )
            # Clean up resources that the worker would normally own.
            # If the worker never dequeued this task, these would leak.
            # Runs regardless of terminal state: a task that died before
            # dequeue (e.g. failed by _enqueue_or_fail) is already terminal
            # here, but its replay buffer and cancel key still exist.
            # Cleanup is idempotent — safe even if the worker also calls it.
            # Each operation is isolated so a transient failure in one
            # (e.g. Redis blip during event_bus.cleanup) does not skip
            # the other — otherwise the CancelRegistry key + Pub/Sub
            # listener would leak until the next process restart.
            try:
                await self.event_bus.cleanup(task_id)
            except Exception:
                logger.exception("event_bus cleanup failed for %s during force-cancel", task_id)
            try:
                await self.cancel_registry.cleanup(task_id)
            except Exception:
                logger.exception(
                    "cancel_registry cleanup failed for %s during force-cancel", task_id
                )
        except Exception:
            logger.exception("Force-cancel failed for task %s", task_id)
