"""Tests for the v1.0 client SSE parsers — bare Message events and trailing flush."""

from __future__ import annotations

import json
from typing import Any

import httpx
from a2a_pydantic import v10
from a2a_pydantic.v03 import Message as V03Message

from a2akit.client.result import StreamEvent
from a2akit.client.transport.jsonrpc_v10 import JsonRpcV10Transport
from a2akit.client.transport.rest_v10 import _parse_v10_sse


def _v10_message_json(text: str = "hi") -> dict[str, Any]:
    msg = v10.Message(
        message_id="m1",
        role=v10.Role.role_agent,
        parts=[v10.Part(text=text)],
        task_id="t1",
        context_id="c1",
    )
    return msg.model_dump(mode="json", by_alias=True, exclude_none=True)


def _v10_status_json(state: v10.TaskState) -> dict[str, Any]:
    evt = v10.TaskStatusUpdateEvent(
        task_id="t1",
        context_id="c1",
        status=v10.TaskStatus(state=state),
    )
    return evt.model_dump(mode="json", by_alias=True, exclude_none=True)


def _sse_response(body: str) -> httpx.Response:
    return httpx.Response(
        200,
        content=body.encode("utf-8"),
        headers={"content-type": "text/event-stream"},
    )


class TestStreamEventFromMessage:
    def test_from_raw_handles_v03_message(self):
        """A bare Message must produce a message-kind event, not crash."""
        msg = V03Message.model_validate(
            {
                "kind": "message",
                "messageId": "m1",
                "role": "agent",
                "parts": [{"kind": "text", "text": "direct reply"}],
                "taskId": "t9",
            }
        )
        event = StreamEvent.from_raw(msg)
        assert event.kind == "message"
        assert event.text == "direct reply"
        assert event.task_id == "t9"
        assert event.is_final is True
        assert event.artifact_id is None


class TestRestV10SseParser:
    async def test_bare_message_event(self):
        """v1.0 SSE with a bare Message payload yields a message event."""
        body = f"data: {json.dumps(_v10_message_json('direct'))}\n\n"
        events = [e async for e in _parse_v10_sse(_sse_response(body))]
        assert len(events) == 1
        assert events[0].kind == "message"
        assert events[0].text == "direct"

    async def test_trailing_event_without_blank_line_is_flushed(self):
        """A stream ending without a trailing blank line still delivers the last event."""
        first = json.dumps(
            {"taskStatusUpdate": _v10_status_json(v10.TaskState.task_state_working)}
        )
        last = json.dumps(
            {"taskStatusUpdate": _v10_status_json(v10.TaskState.task_state_completed)}
        )
        # No trailing blank line after the final data line.
        body = f"data: {first}\n\ndata: {last}"
        events = [e async for e in _parse_v10_sse(_sse_response(body))]
        assert len(events) == 2
        assert events[0].state == "working"
        assert events[1].state == "completed"


class TestJsonRpcV10SseParser:
    def _transport(self, sse_body: str) -> JsonRpcV10Transport:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=sse_body.encode("utf-8"),
                headers={"content-type": "text/event-stream"},
            )

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return JsonRpcV10Transport(http, "http://test")

    async def test_bare_message_event(self):
        """JSON-RPC envelope carrying a Message result yields a message event."""
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": "1", "result": {"message": _v10_message_json("reply")}}
        )
        transport = self._transport(f"data: {payload}\n\n")
        events = [e async for e in transport.subscribe_task("t1")]
        assert len(events) == 1
        assert events[0].kind == "message"
        assert events[0].text == "reply"

    async def test_trailing_event_without_blank_line_is_flushed(self):
        """The final SSE event must be delivered even without a trailing blank line."""
        first = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "1",
                "result": {"taskStatusUpdate": _v10_status_json(v10.TaskState.task_state_working)},
            }
        )
        last = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "1",
                "result": {
                    "taskStatusUpdate": _v10_status_json(v10.TaskState.task_state_completed)
                },
            }
        )
        transport = self._transport(f"data: {first}\n\ndata: {last}")
        events = [e async for e in transport.subscribe_task("t1")]
        assert len(events) == 2
        assert events[0].state == "working"
        assert events[1].state == "completed"
