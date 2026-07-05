"""Tests for TracingEmitter — span events + metrics on state transitions."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from a2a_pydantic import v10
from opentelemetry import trace

from a2akit.event_emitter import EventEmitter
from a2akit.telemetry._emitter import TracingEmitter

TaskState = v10.TaskState


class FakeEmitter(EventEmitter):
    """Minimal emitter for testing."""

    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    async def update_task(self, task_id, state=None, **kwargs) -> int:
        self.calls.append({"task_id": task_id, "state": state, **kwargs})
        return len(self.calls)

    async def send_event(self, task_id, event) -> None:
        pass


class TestTracingEmitter:
    async def test_emitter_delegates_to_inner(self, otel_setup):
        """Inner emitter is always called."""
        inner = FakeEmitter()
        emitter = TracingEmitter(inner)
        await emitter.update_task("task-1", state=TaskState.task_state_working)
        assert len(inner.calls) == 1
        assert inner.calls[0]["task_id"] == "task-1"

    async def test_emitter_adds_state_transition_event(self, otel_setup):
        """Span event is added when state changes."""
        exporter = otel_setup
        inner = FakeEmitter()
        emitter = TracingEmitter(inner)

        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("test-span"):
            await emitter.update_task("task-1", state=TaskState.task_state_working)

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        events = spans[0].events
        assert len(events) == 1
        assert events[0].name == "state_transition"
        assert events[0].attributes["a2akit.task.id"] == "task-1"
        assert events[0].attributes["a2akit.task.state"] == "TASK_STATE_WORKING"

    async def test_emitter_skips_when_no_state(self, otel_setup):
        """No span event when state is None."""
        exporter = otel_setup
        inner = FakeEmitter()
        emitter = TracingEmitter(inner)

        tracer = trace.get_tracer("test")
        with tracer.start_as_current_span("test-span"):
            await emitter.update_task("task-1", state=None)

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert len(spans[0].events) == 0

    async def test_emitter_send_event_delegates(self, otel_setup):
        """send_event passes through to inner."""
        inner = FakeEmitter()
        inner.send_event = AsyncMock()  # type: ignore[method-assign]
        emitter = TracingEmitter(inner)
        await emitter.send_event("task-1", {"kind": "status-update"})
        inner.send_event.assert_awaited_once_with("task-1", {"kind": "status-update"})


@pytest.fixture
def metric_reader():
    """InMemoryMetricReader wired as the global MeterProvider.

    Force-resets the global provider (OTel only allows set_meter_provider
    once) and a2akit's lazy meter singleton, mirroring ``otel_setup``.
    """
    from opentelemetry import metrics as metrics_api
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    import a2akit.telemetry._instruments as inst

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])

    metrics_api._internal._METER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
    metrics_api._internal._METER_PROVIDER = None  # type: ignore[attr-defined]
    metrics_api.set_meter_provider(provider)

    old_enabled = inst.OTEL_ENABLED
    inst.OTEL_ENABLED = True
    inst._meter = None

    yield reader

    inst.OTEL_ENABLED = old_enabled
    inst._meter = None
    provider.shutdown()


def _data_points(reader, name: str) -> list[Any]:
    """Collect all data points for a metric by name (empty if never recorded)."""
    data = reader.get_metrics_data()
    points: list[Any] = []
    if data is None:
        return points
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name == name:
                    points.extend(metric.data.data_points)
    return points


def _sum_value(reader, name: str) -> int | None:
    points = _data_points(reader, name)
    if not points:
        return None
    return sum(p.value for p in points)


class TestTracingEmitterMetrics:
    """Real metric assertions — production passes v1.0 TaskState values."""

    async def test_active_counter_up_and_down(self, metric_reader):
        inner = FakeEmitter()
        emitter = TracingEmitter(inner)

        await emitter.update_task("t1", state=TaskState.task_state_working)
        assert _sum_value(metric_reader, "a2akit.task.active") == 1

        await emitter.update_task("t1", state=TaskState.task_state_completed)
        assert _sum_value(metric_reader, "a2akit.task.active") == 0

    async def test_duration_and_total_recorded_on_completion(self, metric_reader):
        inner = FakeEmitter()
        emitter = TracingEmitter(inner)

        await emitter.update_task("t1", state=TaskState.task_state_working)
        await emitter.update_task("t1", state=TaskState.task_state_completed)

        duration_points = _data_points(metric_reader, "a2akit.task.duration")
        assert len(duration_points) == 1
        assert duration_points[0].count == 1
        assert duration_points[0].attributes["state"] == "TASK_STATE_COMPLETED"

        assert _sum_value(metric_reader, "a2akit.task.total") == 1
        # No errors on a successful task.
        assert _sum_value(metric_reader, "a2akit.task.errors") in (None, 0)

    async def test_error_counter_on_failure(self, metric_reader):
        inner = FakeEmitter()
        emitter = TracingEmitter(inner)

        await emitter.update_task("t1", state=TaskState.task_state_working)
        await emitter.update_task("t1", state=TaskState.task_state_failed)

        assert _sum_value(metric_reader, "a2akit.task.errors") == 1
        assert _sum_value(metric_reader, "a2akit.task.active") == 0

    async def test_task_timers_capped(self, metric_reader):
        """Timers for tasks that never terminate are evicted oldest-first."""
        import a2akit.telemetry._emitter as emitter_mod

        inner = FakeEmitter()
        emitter = TracingEmitter(inner)
        original_cap = emitter_mod._MAX_TASK_TIMERS
        emitter_mod._MAX_TASK_TIMERS = 3
        try:
            for i in range(5):
                await emitter.update_task(f"t{i}", state=TaskState.task_state_working)
        finally:
            emitter_mod._MAX_TASK_TIMERS = original_cap

        assert len(emitter._task_timers) == 3
        assert set(emitter._task_timers) == {"t2", "t3", "t4"}
