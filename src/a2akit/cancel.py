"""Shared cancel helper — used by both TaskManager and WorkerAdapter."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from a2a.types import (
    Message,
    Part,
    Role,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    TextPart,
)

from a2akit.storage.base import (
    TERMINAL_STATES,
    ConcurrencyError,
    Storage,
    TaskTerminalStateError,
)

if TYPE_CHECKING:
    from a2akit.event_emitter import EventEmitter

logger = logging.getLogger(__name__)


async def cancel_task_in_storage(
    storage: Storage,
    emitter: EventEmitter,
    task_id: str,
    context_id: str | None,
    reason: str = "Task was canceled.",
) -> None:
    """Persist cancel state and broadcast final event.

    Used by both TaskManager (force cancel) and WorkerAdapter
    (cooperative cancel) to ensure consistent cancel semantics.

    All writes go through ``emitter`` so there is a single write
    path (Storage + EventBus) rather than parallel direct access.

    Loads the task first to check for terminal state.  If a
    :class:`ConcurrencyError` is raised (another writer changed the
    task between load and write), re-loads once and retries if the
    task is still non-terminal.
    """
    task = await storage.load_task(task_id)
    if task is None:
        return
    if task.status.state in TERMINAL_STATES:
        return

    cancel_message = Message(
        role=Role.agent,
        parts=[Part(TextPart(text=reason))],
        message_id=str(uuid.uuid4()),
        task_id=task_id,
        context_id=context_id,
    )
    version = await storage.get_version(task_id)
    try:
        await emitter.update_task(
            task_id,
            state=TaskState.canceled,
            status_message=cancel_message,
            messages=[cancel_message],
            expected_version=version,
        )
    except (ConcurrencyError, TaskTerminalStateError) as exc:
        # Another writer changed the task between our load and write.
        # Re-load and retry once if the task is still non-terminal.
        task = await storage.load_task(task_id)
        if task is None or task.status.state in TERMINAL_STATES:
            return
        version = (
            exc.current_version
            if isinstance(exc, ConcurrencyError) and exc.current_version is not None
            else await storage.get_version(task_id)
        )
        await emitter.update_task(
            task_id,
            state=TaskState.canceled,
            status_message=cancel_message,
            messages=[cancel_message],
            expected_version=version,
        )

    status = TaskStatus(
        state=TaskState.canceled,
        timestamp=datetime.now(UTC).isoformat(),
        message=cancel_message,
    )
    await emitter.send_event(
        task_id,
        TaskStatusUpdateEvent(
            kind="status-update",
            task_id=task_id,
            context_id=context_id,
            status=status,
            final=True,
        ),
    )
