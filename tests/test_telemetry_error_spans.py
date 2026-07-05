"""Tests for TracingMiddleware error spans and path-segment method detection."""

from __future__ import annotations

from unittest.mock import MagicMock

from opentelemetry.trace import StatusCode

from a2akit.middleware import RequestEnvelope
from a2akit.telemetry._middleware import TracingMiddleware, _detect_method


def _make_request(path="/v1/message:send", headers=None):
    req = MagicMock()
    req.url = MagicMock()
    req.url.path = path
    req.headers = headers or {}
    return req


class TestDetectMethod:
    def test_custom_method_verbs(self):
        assert _detect_method(_make_request("/v1/message:send")) == "message/send"
        assert _detect_method(_make_request("/message:stream")) == "message/stream"
        assert _detect_method(_make_request("/v1/tasks/t-1:cancel")) == "tasks/cancel"
        assert _detect_method(_make_request("/tasks/t-1:subscribe")) == "tasks/resubscribe"
        assert _detect_method(_make_request("/v1/tasks/t-1")) == "tasks/get"

    def test_task_id_containing_verb_does_not_misclassify(self):
        """A task id containing 'subscribe' must not be detected as resubscribe."""
        assert _detect_method(_make_request("/v1/tasks/my-subscribe-task")) == "tasks/get"
        assert _detect_method(_make_request("/v1/tasks/send-me-cancel")) == "tasks/get"


class TestErrorSpans:
    async def test_after_dispatch_error_param_marks_span_failed(self, otel_setup):
        exporter = otel_setup
        mw = TracingMiddleware()
        envelope = RequestEnvelope()

        await mw.before_dispatch(envelope, _make_request("/v1/tasks/t-1"))
        await mw.after_dispatch(envelope, error=RuntimeError("boom"))

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].status.status_code == StatusCode.ERROR
        assert any(e.name == "exception" for e in spans[0].events)

    async def test_after_dispatch_context_error_marks_span_failed(self, otel_setup):
        """Endpoints surface failures via envelope.context['_a2a_error']."""
        exporter = otel_setup
        mw = TracingMiddleware()
        envelope = RequestEnvelope()

        await mw.before_dispatch(envelope, _make_request("/v1/tasks/t-1"))
        envelope.context["_a2a_error"] = ValueError("nope")
        await mw.after_dispatch(envelope)

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].status.status_code == StatusCode.ERROR

    async def test_after_dispatch_without_error_stays_ok(self, otel_setup):
        exporter = otel_setup
        mw = TracingMiddleware()
        envelope = RequestEnvelope()

        await mw.before_dispatch(envelope, _make_request("/v1/tasks/t-1"))
        await mw.after_dispatch(envelope)

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].status.status_code == StatusCode.OK
