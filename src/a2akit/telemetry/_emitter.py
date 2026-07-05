"""TracingEmitter — adds OTel span events to state transitions."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from a2a_pydantic import v10

from a2akit.event_emitter import EventEmitter
from a2akit.telemetry._instruments import OTEL_ENABLED, get_meter_instance
from a2akit.telemetry._semantic import (
    ATTR_TASK_ID,
    ATTR_TASK_STATE,
    EVENT_STATE_TRANSITION,
    METRIC_TASK_ACTIVE,
    METRIC_TASK_DURATION,
    METRIC_TASK_ERRORS,
    METRIC_TASK_TOTAL,
)

if TYPE_CHECKING:
    from a2akit.schema import StreamEvent
    from a2akit.storage.base import ArtifactWrite

logger = logging.getLogger(__name__)

TERMINAL_STATES: set[Any] = set()

if OTEL_ENABLED:
    from a2akit.storage.base import TERMINAL_STATES

# Production passes v1.0 TaskState values (e.g. "TASK_STATE_WORKING").
_STATE_WORKING = v10.TaskState.task_state_working.value
_STATE_FAILED = v10.TaskState.task_state_failed.value
_TERMINAL_STATE_VALUES = {s.value if hasattr(s, "value") else str(s) for s in TERMINAL_STATES}

# Cap for _task_timers so tasks that never reach a terminal state don't leak
# memory in long-lived processes.
_MAX_TASK_TIMERS = 10_000


class TracingEmitter(EventEmitter):
    """Decorator that adds OTel span events to state transitions.

    Wraps an inner EventEmitter (which may already be a HookableEmitter).
    Adds span events for state transitions and maintains metrics counters.

    Stacking order: TracingEmitter(HookableEmitter(DefaultEventEmitter))
    """

    def __init__(self, inner: EventEmitter) -> None:
        self._inner = inner
        self._task_timers: dict[str, float] = {}
        # Lazy metric instruments
        self._duration_hist: Any = None
        self._active_counter: Any = None
        self._total_counter: Any = None
        self._error_counter: Any = None
        self._metrics_initialized = False

    def _ensure_metrics(self) -> None:
        """Lazily initialize metric instruments."""
        if self._metrics_initialized:
            return
        meter = get_meter_instance()
        if meter is None:
            self._metrics_initialized = True
            return
        self._duration_hist = meter.create_histogram(
            METRIC_TASK_DURATION,
            unit="s",
            description="Task processing duration in seconds",
        )
        self._active_counter = meter.create_up_down_counter(
            METRIC_TASK_ACTIVE,
            description="Number of currently active tasks",
        )
        self._total_counter = meter.create_counter(
            METRIC_TASK_TOTAL,
            description="Total number of completed tasks",
        )
        self._error_counter = meter.create_counter(
            METRIC_TASK_ERRORS,
            description="Total number of failed tasks",
        )
        self._metrics_initialized = True

    async def update_task(
        self,
        task_id: str,
        state: Any = None,
        *,
        status_message: Any = None,
        artifacts: list[ArtifactWrite] | None = None,
        messages: list[Any] | None = None,
        task_metadata: dict[str, Any] | None = None,
        expected_version: int | None = None,
    ) -> int:
        """Delegate to inner emitter, then record OTel span events and metrics."""
        result = await self._inner.update_task(
            task_id,
            state=state,
            status_message=status_message,
            artifacts=artifacts,
            messages=messages,
            task_metadata=task_metadata,
            expected_version=expected_version,
        )

        if state is not None and OTEL_ENABLED:
            # Span event
            from opentelemetry import trace

            span = trace.get_current_span()
            if span and span.is_recording():
                span.add_event(
                    EVENT_STATE_TRANSITION,
                    attributes={
                        ATTR_TASK_ID: task_id,
                        ATTR_TASK_STATE: state.value if hasattr(state, "value") else str(state),
                    },
                )

            # Metrics
            self._record_state_metric(state, task_id)

        return result

    async def send_event(self, task_id: str, event: StreamEvent) -> None:
        """Delegate send_event unchanged."""
        await self._inner.send_event(task_id, event)

    def _record_state_metric(self, state: Any, task_id: str) -> None:
        """Record metrics based on state transitions."""
        self._ensure_metrics()

        state_val = state.value if hasattr(state, "value") else str(state)

        if state_val == _STATE_WORKING:
            if task_id not in self._task_timers and len(self._task_timers) >= _MAX_TASK_TIMERS:
                self._task_timers.pop(next(iter(self._task_timers)))
            self._task_timers[task_id] = time.monotonic()
            if self._active_counter:
                self._active_counter.add(1)

        elif state_val in _TERMINAL_STATE_VALUES:
            if self._active_counter:
                self._active_counter.add(-1)
            if self._total_counter:
                self._total_counter.add(1, {"state": state_val})

            start = self._task_timers.pop(task_id, None)
            if start is not None and self._duration_hist:
                self._duration_hist.record(time.monotonic() - start, {"state": state_val})

            if state_val == _STATE_FAILED and self._error_counter:
                self._error_counter.add(1)
