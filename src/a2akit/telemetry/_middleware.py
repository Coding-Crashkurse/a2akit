"""TracingMiddleware — creates root spans per incoming A2A request."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from a2akit.middleware import A2AMiddleware
from a2akit.telemetry._instruments import OTEL_ENABLED, get_tracer
from a2akit.telemetry._semantic import (
    ATTR_A2A_VERSION,
    ATTR_ARTIFACT_COUNT,
    ATTR_CONTEXT_ID,
    ATTR_MESSAGE_ID,
    ATTR_METHOD,
    ATTR_TASK_ID,
    ATTR_TASK_STATE,
    SPAN_HTTP_REQUEST,
)

if TYPE_CHECKING:
    from a2a_pydantic import v10
    from fastapi import Request

    from a2akit.middleware import RequestEnvelope

if OTEL_ENABLED:
    from opentelemetry import context as otel_context
    from opentelemetry.propagate import extract
    from opentelemetry.trace import SpanKind, StatusCode, set_span_in_context


def _detect_method(request: Request) -> str:
    """Detect the A2A method from the request path.

    Matches on path segments and the Google-style custom-method verb
    (the part after ``:`` in the final segment) rather than raw substrings,
    so task IDs containing e.g. "subscribe" don't misclassify the request.
    """
    path = request.url.path
    segments = [s for s in path.split("/") if s]
    last = segments[-1] if segments else ""
    verb = last.rsplit(":", 1)[1] if ":" in last else None
    if verb == "send":
        return "message/send"
    if verb == "stream":
        return "message/stream"
    if verb == "cancel":
        return "tasks/cancel"
    if verb == "subscribe":
        return "tasks/resubscribe"
    if "tasks" in segments:
        return "tasks/get"
    return path


class TracingMiddleware(A2AMiddleware):
    """Creates a root span per incoming A2A request.

    Extracts W3C trace context from HTTP headers (context propagation).
    Sets task_id, context_id, method as span attributes.
    """

    async def before_dispatch(
        self,
        envelope: RequestEnvelope,
        request: Request,
    ) -> None:
        """Start a server span for the incoming request."""
        tracer = get_tracer()
        if tracer is None:
            return

        # Extract W3C trace context from incoming headers
        headers: dict[str, str] = dict(request.headers)
        ctx = extract(headers)

        # Non-message endpoints (tasks/get, tasks/cancel, push, extended card)
        # run the middleware pipeline for auth but carry no MessageSendParams.
        # Emit a span with path-derived attributes only.
        # A2A version tag: :class:`A2AServer` sets ``app.state.protocol_version``
        # to the single configured :class:`ProtocolVersion` during startup.
        configured = getattr(request.app.state, "protocol_version", None)
        resolved_version = envelope.context.get("a2a_version") or (
            configured.value if configured is not None else None
        )
        attr_version = resolved_version or "1.0"

        if envelope.params is None:
            attributes: dict[str, Any] = {
                ATTR_TASK_ID: "",
                ATTR_CONTEXT_ID: "",
                ATTR_MESSAGE_ID: "",
                ATTR_METHOD: _detect_method(request),
                ATTR_A2A_VERSION: attr_version,
            }
        else:
            msg = envelope.params.message
            attributes = {
                ATTR_TASK_ID: msg.task_id or "",
                ATTR_CONTEXT_ID: msg.context_id or "",
                ATTR_MESSAGE_ID: msg.message_id or "",
                ATTR_METHOD: _detect_method(request),
                ATTR_A2A_VERSION: attr_version,
            }

        span = tracer.start_span(
            SPAN_HTTP_REQUEST,
            context=ctx,
            kind=SpanKind.SERVER,
            attributes=attributes,
        )
        envelope.context["_otel_span"] = span
        token = otel_context.attach(set_span_in_context(span))
        envelope.context["_otel_token"] = token

    async def after_dispatch(
        self,
        envelope: RequestEnvelope,
        result: v10.Task | v10.Message | None = None,
        error: BaseException | None = None,
    ) -> None:
        """End the server span with result attributes.

        ``error`` marks the span as failed. Because the generic middleware
        loop calls ``after_dispatch(envelope)`` without extra arguments,
        endpoints also surface failures via ``envelope.context["_a2a_error"]``
        — either channel flips the span to ``StatusCode.ERROR``.
        """
        span: Any = envelope.context.get("_otel_span")
        token: Any = envelope.context.get("_otel_token")
        if span is None:
            return

        if result is not None:
            if hasattr(result, "status") and result.status:
                span.set_attribute(
                    ATTR_TASK_STATE,
                    result.status.state.value
                    if hasattr(result.status.state, "value")
                    else str(result.status.state),
                )
            if hasattr(result, "artifacts") and result.artifacts:
                span.set_attribute(ATTR_ARTIFACT_COUNT, len(result.artifacts))

        if error is None:
            error = envelope.context.get("_a2a_error")
        if error is not None:
            span.record_exception(error)
            span.set_status(StatusCode.ERROR, str(error))
        else:
            span.set_status(StatusCode.OK)
        span.end()
        if token is not None:
            otel_context.detach(token)
