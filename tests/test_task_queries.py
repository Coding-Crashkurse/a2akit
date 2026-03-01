"""Task query and listing tests via the A2A HTTP API."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_get_task_by_id(client, make_send_params):
    """Create a task via message:send, then GET /v1/tasks/{id} to retrieve it."""
    body = make_send_params(text="lookup me")
    send_resp = await client.post("/v1/message:send", json=body)
    assert send_resp.status_code == 200
    task = send_resp.json()
    task_id = task["id"]

    # Retrieve the task by ID
    get_resp = await client.get(f"/v1/tasks/{task_id}")
    assert get_resp.status_code == 200

    data = get_resp.json()
    assert data["id"] == task_id
    assert data["status"]["state"] == "completed"
    assert data.get("contextId") == task["contextId"]


@pytest.mark.asyncio
async def test_get_task_not_found(client):
    """GET /v1/tasks/{nonexistent} returns 404."""
    resp = await client.get("/v1/tasks/nonexistent-id-12345")
    assert resp.status_code == 404

    data = resp.json()
    assert "detail" in data


@pytest.mark.asyncio
async def test_get_task_with_history_length(client, make_send_params):
    """Verify historyLength query parameter trims the history list."""
    body = make_send_params(text="history test")
    send_resp = await client.post("/v1/message:send", json=body)
    assert send_resp.status_code == 200
    task = send_resp.json()
    task_id = task["id"]

    # Get full history (should have at least 1 message)
    full_resp = await client.get(f"/v1/tasks/{task_id}")
    assert full_resp.status_code == 200
    full_data = full_resp.json()
    full_history = full_data.get("history", [])
    assert len(full_history) >= 1, "Task should have at least 1 history message"

    # Get with historyLength=0 -- should return empty history
    trimmed_resp = await client.get(f"/v1/tasks/{task_id}", params={"historyLength": 0})
    assert trimmed_resp.status_code == 200
    trimmed_data = trimmed_resp.json()
    trimmed_history = trimmed_data.get("history", [])
    assert len(trimmed_history) == 0, (
        f"Expected 0 history entries with historyLength=0, got {len(trimmed_history)}"
    )

    # Get with historyLength=1 -- should return exactly 1 message
    one_resp = await client.get(f"/v1/tasks/{task_id}", params={"historyLength": 1})
    assert one_resp.status_code == 200
    one_data = one_resp.json()
    one_history = one_data.get("history", [])
    assert len(one_history) == 1, (
        f"Expected 1 history entry with historyLength=1, got {len(one_history)}"
    )


@pytest.mark.asyncio
async def test_list_tasks(client, make_send_params):
    """Create multiple tasks and verify GET /v1/tasks returns them with pagination."""
    # Create 3 tasks
    task_ids = []
    for i in range(3):
        body = make_send_params(text=f"task {i}")
        resp = await client.post("/v1/message:send", json=body)
        assert resp.status_code == 200
        task_ids.append(resp.json()["id"])

    # List all tasks
    list_resp = await client.get("/v1/tasks")
    assert list_resp.status_code == 200
    data = list_resp.json()

    assert "tasks" in data
    assert data["totalSize"] >= 3

    returned_ids = {t["id"] for t in data["tasks"]}
    for tid in task_ids:
        assert tid in returned_ids, f"Task {tid} not found in list response"

    # Test pagination with pageSize=1
    page_resp = await client.get("/v1/tasks", params={"pageSize": 1})
    assert page_resp.status_code == 200
    page_data = page_resp.json()

    assert len(page_data["tasks"]) == 1
    assert page_data["pageSize"] == 1
    # If there are more tasks, nextPageToken should be non-empty
    if page_data["totalSize"] > 1:
        assert page_data.get("nextPageToken"), "Expected nextPageToken for additional pages"


@pytest.mark.asyncio
async def test_list_tasks_filter_by_context(client, make_send_params):
    """Filter tasks by contextId -- only matching tasks are returned."""
    # Create a task with a specific contextId
    target_context = "ctx-filter-test-12345"
    body_with_ctx = make_send_params(text="context task", context_id=target_context)
    resp1 = await client.post("/v1/message:send", json=body_with_ctx)
    assert resp1.status_code == 200
    task_with_ctx = resp1.json()

    # Create another task without specifying contextId (gets a random one)
    body_no_ctx = make_send_params(text="other task")
    resp2 = await client.post("/v1/message:send", json=body_no_ctx)
    assert resp2.status_code == 200

    # Filter by the specific contextId
    filter_resp = await client.get("/v1/tasks", params={"contextId": target_context})
    assert filter_resp.status_code == 200
    filter_data = filter_resp.json()

    # All returned tasks must belong to the target context
    assert len(filter_data["tasks"]) >= 1, "Expected at least 1 task for the context"
    for t in filter_data["tasks"]:
        assert t["contextId"] == target_context, (
            f"Expected contextId={target_context}, got {t['contextId']}"
        )

    # The specific task we created should be in the results
    returned_ids = {t["id"] for t in filter_data["tasks"]}
    assert task_with_ctx["id"] in returned_ids
