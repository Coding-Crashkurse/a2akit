"""End-to-end tests for the EchoWorker via the A2A HTTP API."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_echo_complete(client, make_send_params):
    """Sending a blocking message returns a completed task with 'Echo: hello' artifact."""
    body = make_send_params(text="hello")
    resp = await client.post("/v1/message:send", json=body)

    assert resp.status_code == 200
    data = resp.json()

    # Task must be in completed state
    assert data["status"]["state"] == "completed"

    # Must have at least one artifact with "Echo: hello"
    assert data.get("artifacts"), "Expected at least one artifact"
    parts = data["artifacts"][0]["parts"]
    text_parts = [p for p in parts if p.get("kind") == "text"]
    assert len(text_parts) >= 1
    assert text_parts[0]["text"] == "Echo: hello"


@pytest.mark.asyncio
async def test_echo_task_has_id(client, make_send_params):
    """The returned task must have id, contextId, and status fields."""
    body = make_send_params(text="world")
    resp = await client.post("/v1/message:send", json=body)

    assert resp.status_code == 200
    data = resp.json()

    assert "id" in data, "Task must have an 'id' field"
    assert data["id"], "Task id must not be empty"
    assert "contextId" in data, "Task must have a 'contextId' field"
    assert data["contextId"], "contextId must not be empty"
    assert "status" in data, "Task must have a 'status' field"
    assert "state" in data["status"], "Status must have a 'state' field"


@pytest.mark.asyncio
async def test_echo_non_blocking(client, make_send_params):
    """Sending without blocking config returns the task in submitted state."""
    body = make_send_params(text="async hello", blocking=False)
    resp = await client.post("/v1/message:send", json=body)

    assert resp.status_code == 200
    data = resp.json()

    # Non-blocking returns immediately; task should be in submitted state
    # (the worker may not have finished yet)
    assert data["status"]["state"] == "submitted"
    assert "id" in data
