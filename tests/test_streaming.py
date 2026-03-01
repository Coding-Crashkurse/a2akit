"""SSE streaming tests for the StreamingWorker via the A2A HTTP API."""

from __future__ import annotations

import json

import pytest


def _parse_sse_events(raw: str) -> list[dict]:
    """Parse raw SSE text into a list of JSON event dicts.

    SSE events are separated by blank lines (``\\r\\n\\r\\n`` or ``\\n\\n``).
    Each event block may contain one or more ``data:`` lines.
    """
    events: list[dict] = []
    # Normalize line endings to \n for consistent parsing
    normalized = raw.replace("\r\n", "\n")
    # Split on double newline (blank line separates events)
    blocks = normalized.split("\n\n")
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        for line in block.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                payload = line[len("data:") :].strip()
                if payload:
                    events.append(json.loads(payload))
    return events


@pytest.mark.asyncio
async def test_streaming_sse_events(streaming_client, make_send_params):
    """POST to /v1/message:stream returns SSE events including artifact updates and final status."""
    body = make_send_params(text="hello world")

    raw_text = ""
    async with streaming_client.stream("POST", "/v1/message:stream", json=body) as resp:
        assert resp.status_code == 200
        async for chunk in resp.aiter_text():
            raw_text += chunk

    events = _parse_sse_events(raw_text)

    # We should have received at least one event
    assert len(events) >= 1, f"Expected at least 1 SSE event, got {len(events)}"

    # Collect the kinds of events received
    event_kinds = [ev.get("kind") for ev in events]

    # The first event should be the initial task snapshot
    assert events[0].get("kind") == "task", (
        f"First event should be a task snapshot, got {events[0].get('kind')}"
    )

    # There should be artifact update events (StreamingWorker emits text artifact chunks)
    assert "artifact-update" in event_kinds, (
        f"Expected artifact-update events, got kinds: {event_kinds}"
    )

    # There should be a status-update event with final=true
    status_updates = [ev for ev in events if ev.get("kind") == "status-update"]
    assert len(status_updates) >= 1, "Expected at least one status-update event"

    # The last status-update should be final
    final_updates = [ev for ev in status_updates if ev.get("final") is True]
    assert len(final_updates) >= 1, f"Expected a final status-update event, got: {status_updates}"
