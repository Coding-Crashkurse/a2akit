"""Cancellation tests via the A2A HTTP API."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_cancel_not_found(client):
    """Canceling a nonexistent task returns 404."""
    resp = await client.post("/v1/tasks/nonexistent-cancel-id:cancel")
    assert resp.status_code == 404

    data = resp.json()
    assert "detail" in data


@pytest.mark.asyncio
async def test_cancel_terminal_task(client, make_send_params):
    """Canceling a task already in a terminal state (completed) returns 409."""
    # Create and complete a task via blocking send
    body = make_send_params(text="complete me")
    send_resp = await client.post("/v1/message:send", json=body)
    assert send_resp.status_code == 200

    task = send_resp.json()
    task_id = task["id"]

    # Verify the task is completed
    assert task["status"]["state"] == "completed", (
        f"Expected completed state, got {task['status']['state']}"
    )

    # Attempt to cancel the completed task -- should get 409 Conflict
    cancel_resp = await client.post(f"/v1/tasks/{task_id}:cancel")
    assert cancel_resp.status_code == 409

    data = cancel_resp.json()
    assert "detail" in data
