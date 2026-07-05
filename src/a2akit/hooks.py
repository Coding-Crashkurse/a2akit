"""Lifecycle hooks — callbacks awaited inline in the write path on state transitions.

Hooks are awaited before the write path returns, so they must be fast;
slow hooks delay task processing. Hook errors are logged and swallowed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from a2a_pydantic import v10

from a2akit.event_emitter import EventEmitter
from a2akit.storage.base import TERMINAL_STATES

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from a2akit.storage.base import ArtifactWrite

logger = logging.getLogger(__name__)

# States that pause the task and wait for external input
TURN_END_STATES: set[v10.TaskState] = {
    v10.TaskState.task_state_input_required,
    v10.TaskState.task_state_auth_required,
}


@dataclass
class LifecycleHooks:
    """Container for lifecycle hook callbacks.

    Hooks are simple async callables — no base class, no protocol,
    no registration ceremony.  The framework calls them after
    successful state transitions.  Hook errors are logged and
    swallowed; they never affect task processing.

    All hooks are optional.  Set only what you need.

    Attributes:
        on_state_change: Called on every state transition (state is not None).
            Catch-all for audit logs, debug tracing, state-machine visualization.
            Receives task_id, new state, and status message.

        on_working: Called when a task starts processing (state becomes working).
            Use for metrics (start duration timer), "agent is typing" indicators.
            Receives task_id only.

        on_turn_end: Called when a task pauses for input (input_required, auth_required).
            Use for user notifications, timeout timers, conversation tracking.
            Receives task_id, the paused state, and the status message.

        on_terminal: Called when a task reaches a terminal state
            (completed, failed, canceled, rejected).
            Use for metrics, alerting, cleanup.
            Receives task_id, terminal state, and status message.
            The consumer differentiates by state.
    """

    on_state_change: Callable[[str, v10.TaskState, v10.Message | None], Awaitable[None]] | None = (
        None
    )
    on_working: Callable[[str], Awaitable[None]] | None = None
    on_turn_end: Callable[[str, v10.TaskState, v10.Message | None], Awaitable[None]] | None = None
    on_terminal: Callable[[str, v10.TaskState, v10.Message | None], Awaitable[None]] | None = None


class HookableEmitter(EventEmitter):
    """Decorator that adds lifecycle hooks to any EventEmitter.

    Wraps an inner EventEmitter and fires hooks after successful
    state-transition writes.  The inner emitter can be any
    implementation (DefaultEventEmitter, a future RedisEventEmitter,
    etc.) — the ABC is unchanged.

    Hook invocation is synchronous with the write path: if the
    storage write succeeds, the hook fires.  If the write throws
    (ConcurrencyError, TaskTerminalStateError), the hook does not
    fire.  This gives exactly-once hook delivery per successful
    state transition without any external coordination.

    Args:
        inner: The EventEmitter to decorate.
        hooks: Optional lifecycle hooks.  When None, this class
            is a transparent passthrough.
    """

    def __init__(self, inner: EventEmitter, hooks: LifecycleHooks | None = None) -> None:
        self._inner = inner
        self._hooks = hooks or LifecycleHooks()

    async def _safe_call(self, coro: Awaitable[None]) -> None:
        """Call a hook coroutine, log and swallow any exception."""
        try:
            await coro
        except Exception:
            logger.exception("Lifecycle hook failed")

    async def update_task(
        self,
        task_id: str,
        state: v10.TaskState | None = None,
        *,
        status_message: v10.Message | None = None,
        artifacts: list[ArtifactWrite] | None = None,
        messages: list[v10.Message] | None = None,
        task_metadata: dict[str, Any] | None = None,
        expected_version: int | None = None,
    ) -> int:
        """Delegate to inner emitter, then fire hooks on state transitions.

        Hook dispatch order for a single update_task call:
        1. on_state_change (if state is not None)
        2. on_working / on_turn_end / on_terminal (exactly one, based on state)

        All hooks fire AFTER the successful storage write.
        """
        result = await self._inner.update_task(
            task_id,
            state=state,
            status_message=status_message,
            artifacts=artifacts,
            messages=messages,
            task_metadata=task_metadata,
            expected_version=expected_version,
        )

        if state is not None:
            h = self._hooks

            if h.on_state_change:
                await self._safe_call(h.on_state_change(task_id, state, status_message))

            if state in TERMINAL_STATES and h.on_terminal:
                await self._safe_call(h.on_terminal(task_id, state, status_message))
            elif state == v10.TaskState.task_state_working and h.on_working:
                await self._safe_call(h.on_working(task_id))
            elif state in TURN_END_STATES and h.on_turn_end:
                await self._safe_call(h.on_turn_end(task_id, state, status_message))

        return result

    async def send_event(self, task_id: str, event: Any) -> None:
        """Delegate send_event unchanged."""
        await self._inner.send_event(task_id, event)
