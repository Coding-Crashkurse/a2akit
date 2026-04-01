"""Task submission, streaming, querying, and cancellation."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from a2a.types import (
    DataPart,
    FilePart,
    Message,
    MessageSendParams,
    Role,
    Task,
    TaskState,
    TaskStatusUpdateEvent,
    TextPart,
)

from a2akit.cancel import cancel_task_in_storage
from a2akit.event_emitter import DefaultEventEmitter, EventEmitter
from a2akit.schema import DIRECT_REPLY_KEY, DirectReply, StreamEvent
from a2akit.storage.base import (
    TERMINAL_STATES,
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
    from a2akit.storage import Storage

logger = logging.getLogger(__name__)


def _is_agent_role(role: str | Role | None) -> bool:
    """Check whether a role value represents the agent role."""
    if role is None:
        return False
    return role == "agent" or getattr(role, "value", None) == "agent"


def _find_direct_reply(task: Task) -> Message | None:
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
    push_store: Any = None
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
        the task stuck in ``submitted`` forever.  Uses storage + event_bus
        directly (not the full emitter) to publish a final SSE event so
        blocking and streaming subscribers see the failure immediately.
        """
        try:
            await coro
        except Exception:
            logger.error("Broker enqueue failed for task %s, marking as failed", task_id)
            try:
                await self.storage.update_task(task_id, state=TaskState.failed)
                # Publish final event so SSE/blocking subscribers see the failure
                task = await self.storage.load_task(task_id)
                if task:
                    await self.event_bus.publish(
                        task_id,
                        TaskStatusUpdateEvent(
                            kind="status-update",
                            task_id=task_id,
                            context_id=task.context_id or "",
                            status=task.status,
                            final=True,
                        ),
                    )
            except Exception:
                logger.exception("Could not mark task %s as failed after broker error", task_id)

    def _validate_input_modes(self, message: Message) -> None:
        """Validate message parts against declared input modes (A2A §8.2 -32005)."""
        if not self.input_modes:
            return
        for part in message.parts:
            root = getattr(part, "root", part)
            if isinstance(root, TextPart):
                effective = "text/plain"
            elif isinstance(root, DataPart):
                effective = "application/json"
            elif isinstance(root, FilePart):
                f = root.file
                effective = getattr(f, "mime_type", None) or "application/octet-stream"
            else:
                continue
            if effective not in self.input_modes:
                raise ContentTypeNotSupportedError(effective)

    async def _submit_task(self, context_id: str, message: Message) -> tuple[Task, bool]:
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
            return (
                await self.storage.create_task(
                    context_id, message, idempotency_key=message.message_id
                ),
                True,
            )

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
        # Pass the current version for OCC — prevents two concurrent
        # follow-ups from silently overwriting each other's history.
        version = await self.storage.get_version(task.id)
        await self.storage.update_task(
            task.id,
            state=new_state,
            messages=[bound_message],
            expected_version=version,
        )
        # Re-load to return the updated Task object.
        updated = await self.storage.load_task(task.id)
        if updated is None:
            raise RuntimeError(f"Task {task.id} vanished after update")
        return updated, True

    async def _load_and_validate(self, message: Message) -> Task:
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

        if current not in {
            TaskState.input_required,
            TaskState.auth_required,
            TaskState.unknown,
        } and not _is_agent_role(getattr(message, "role", None)):
            raise TaskNotAcceptingMessagesError(current)

        return task

    @staticmethod
    def _compute_state_transition(task: Task) -> TaskState | None:
        """Determine the new state based on current task state."""
        if task.status.state in {
            TaskState.input_required,
            TaskState.auth_required,
            TaskState.unknown,
        }:
            return TaskState.submitted
        return None

    async def send_message(
        self,
        params: MessageSendParams,
        request_context: dict[str, Any] | None = None,
    ) -> Task | Message:
        """Submit a task and optionally block until completion.

        Returns a ``Message`` when the worker used ``reply_directly()``
        (direct-message response without task tracking).
        Otherwise returns the ``Task``.
        """
        msg = params.message
        is_new = not msg.task_id
        context_id = msg.context_id or str(uuid.uuid4())
        task, should_enqueue = await self._submit_task(context_id, msg)

        # Idempotent duplicate follow-up — return current state, don't re-enqueue
        if not should_enqueue:
            history_len = getattr(getattr(params, "configuration", None), "history_length", None)
            latest = await self.storage.load_task(task.id, history_length=history_len)
            if latest is not None:
                reply = _find_direct_reply(latest)
                if reply is not None:
                    return reply
            return latest or task

        # Inline push notification config (A2A spec)
        if params.configuration and hasattr(params.configuration, "push_notification_config"):
            pnc = params.configuration.push_notification_config
            if pnc is not None and self.push_store is not None:
                from a2akit.push.models import PushNotificationConfig

                config = PushNotificationConfig.model_validate(
                    pnc if isinstance(pnc, dict) else pnc.model_dump(by_alias=True)
                )
                await self.push_store.set_config(task.id, config)

        params = self._bind_message(params, context_id, task.id)

        direct_message: Message | None = None
        if params.configuration and params.configuration.blocking:
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
                            if isinstance(ev, TaskStatusUpdateEvent) and ev.final:
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

        history_len = getattr(getattr(params, "configuration", None), "history_length", None)
        latest = await self.storage.load_task(task.id, history_length=history_len)
        if latest is not None:
            reply = _find_direct_reply(latest)
            if reply is not None:
                return reply
        return latest or task

    @staticmethod
    def _bind_message(
        params: MessageSendParams, context_id: str, task_id: str
    ) -> MessageSendParams:
        """Return a copy of params with context_id and task_id bound.

        Avoids mutating the caller's MessageSendParams object.
        """
        updated_msg = params.message.model_copy(
            update={"context_id": context_id, "task_id": task_id}
        )
        return params.model_copy(update={"message": updated_msg})

    async def stream_message(
        self,
        params: MessageSendParams,
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

        # REQ-08: Inline push notification config on message/stream.
        if params.configuration and hasattr(params.configuration, "push_notification_config"):
            pnc = params.configuration.push_notification_config
            if pnc is not None and self.push_store is not None:
                from a2akit.push.models import PushNotificationConfig

                config = PushNotificationConfig.model_validate(
                    pnc if isinstance(pnc, dict) else pnc.model_dump(by_alias=True)
                )
                await self.push_store.set_config(task.id, config)

        history_len = getattr(getattr(params, "configuration", None), "history_length", None)
        if history_len is not None:
            trimmed = await self.storage.load_task(task.id, history_length=history_len)
            if trimmed is not None:
                task = trimmed

        yield (None, task)

        if not should_enqueue:
            return  # Idempotent duplicate — end stream after snapshot

        params = self._bind_message(params, context_id, task.id)

        # Subscribe BEFORE starting broker — prevents race condition
        # where events published between run_task and subscribe are lost.
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
        task = await self.storage.load_task(task_id)
        if task is None:
            raise TaskNotFoundError(f"Task {task_id} not found")
        if task.status.state in TERMINAL_STATES:
            raise UnsupportedOperationError("Task is in a terminal state; cannot subscribe")

        # Subscribe BEFORE yielding — prevents event loss between
        # load_task and subscribe.
        async with self.event_bus.subscribe(task_id, after_event_id=after_event_id) as sub:
            yield (None, task)
            async for event_id, ev in sub:
                yield (event_id, ev)

    async def get_task(self, task_id: str, history_length: int | None = None) -> Task | None:
        """Load a single task by ID."""
        return await self.storage.load_task(task_id, history_length)

    async def list_tasks(self, query: ListTasksQuery) -> ListTasksResult:
        """Return filtered and paginated tasks."""
        return await self.storage.list_tasks(query)

    async def cancel_task(self, task_id: str) -> Task:
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
                # Cleanup is idempotent — safe even if the worker also calls it.
                await self.event_bus.cleanup(task_id)
                await self.cancel_registry.cleanup(task_id)
        except Exception:
            logger.exception("Force-cancel failed for task %s", task_id)
