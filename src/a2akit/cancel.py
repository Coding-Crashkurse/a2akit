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
    from a2akit.storage.base import ArtifactWrite

logger = logging.getLogger(__name__)


async def cancel_task_in_storage(
    storage: Storage,
    emitter: EventEmitter,
    task_id: str,
    context_id: str | None,
    reason: str = "Task was canceled.",
    *,
    artifacts: list[ArtifactWrite] | None = None,
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

    ``artifacts`` are written atomically with the cancel transition so
    buffered artifacts from a mid-run cancel are not lost.
    """
    # Capture OCC version BEFORE loading state to close the TOCTOU
    # window (see _submit_task for the full explanation).
    version = await storage.get_version(task_id)
    task = await storage.load_task(task_id)
    if task is None:
        return
    if task.status.state in TERMINAL_STATES:
        return

    # Use the caller-supplied context_id if available, otherwise fall back
    # to the task's context_id (the caller may pass None when the
    # cancellation path doesn't have it).
    resolved_ctx = context_id or task.context_id

    cancel_message = Message(
        role=Role.agent,
        parts=[Part(TextPart(text=reason))],
        message_id=str(uuid.uuid4()),
        task_id=task_id,
        context_id=resolved_ctx,
    )
    try:
        await emitter.update_task(
            task_id,
            state=TaskState.canceled,
            status_message=cancel_message,
            messages=[cancel_message],
            artifacts=artifacts or None,
            expected_version=version,
        )
    except (ConcurrencyError, TaskTerminalStateError):
        # Another writer changed the task between our load and write.
        # Re-load and retry once if the task is still non-terminal.
        # Capture version before load to maintain TOCTOU-safe ordering.
        version = await storage.get_version(task_id)
        task = await storage.load_task(task_id)
        if task is None or task.status.state in TERMINAL_STATES:
            # Task is terminal — write artifacts separately (state=None
            # bypasses the terminal guard) so buffered artifacts from
            # a mid-run cancel aren't silently lost.
            if artifacts:
                try:
                    await emitter.update_task(task_id, artifacts=artifacts)
                except Exception:
                    logger.warning("Artifact-only fallback failed for task %s", task_id)
            return
        try:
            await emitter.update_task(
                task_id,
                state=TaskState.canceled,
                status_message=cancel_message,
                messages=[cancel_message],
                artifacts=artifacts or None,
                expected_version=version,
            )
        except (ConcurrencyError, TaskTerminalStateError):
            logger.warning("Cancel retry failed for task %s, task may have been modified", task_id)
            # Last resort: write artifacts even if state transition failed.
            if artifacts:
                try:
                    await emitter.update_task(task_id, artifacts=artifacts)
                except Exception:
                    logger.warning("Artifact-only fallback failed for task %s", task_id)
            return

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
            context_id=resolved_ctx,
            status=status,
            final=True,
        ),
    )
