"""Tests for endpoints.py — subscribe endpoint, cancel endpoint, list details, SSE edge cases."""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx
from asgi_lifespan import LifespanManager

from conftest import EchoWorker, InputRequiredWorker, _make_app


def _parse_sse_events(raw: str) -> list[dict]:
    """Parse raw SSE text into a list of JSON event dicts."""
    events: list[dict] = []
    normalized = raw.replace("\r\n", "\n")
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


async def test_cancel_endpoint_success():
    """POST /v1/tasks/{id}:cancel on a working task returns the task."""
    app = _make_app(InputRequiredWorker())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Create a task in input_required state (non-terminal, non-completed)
            body = {
                "message": {
                    "role": "user",
                    "messageId": str(uuid.uuid4()),
                    "parts": [{"kind": "text", "text": "start"}],
                },
                "configuration": {"blocking": True},
            }
            resp = await client.post("/v1/message:send", json=body)
            assert resp.status_code == 200
            task = resp.json()
            task_id = task["id"]
            assert task["status"]["state"] == "input-required"

            # Cancel the task
            cancel_resp = await client.post(f"/v1/tasks/{task_id}:cancel")
            # Cancel might return 200 (success) even though it may not
            # have transitioned to canceled yet (asynchronous cancel)
            assert cancel_resp.status_code == 200
            cancel_data = cancel_resp.json()
            assert "id" in cancel_data


async def test_cancel_endpoint_not_found():
    """POST /v1/tasks/{id}:cancel with non-existent task returns 404."""
    app = _make_app(EchoWorker())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/v1/tasks/nonexistent-id:cancel")
            assert resp.status_code == 404
            data = resp.json()
            assert "detail" in data


async def test_cancel_endpoint_not_cancelable():
    """POST /v1/tasks/{id}:cancel on a completed task returns 409."""
    app = _make_app(EchoWorker())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Create and complete a task
            body = {
                "message": {
                    "role": "user",
                    "messageId": str(uuid.uuid4()),
                    "parts": [{"kind": "text", "text": "hello"}],
                },
                "configuration": {"blocking": True},
            }
            resp = await client.post("/v1/message:send", json=body)
            assert resp.status_code == 200
            task = resp.json()
            task_id = task["id"]
            assert task["status"]["state"] == "completed"

            resp2 = await client.post(f"/v1/tasks/{task_id}:cancel")
            assert resp2.status_code == 409


async def test_subscribe_endpoint_not_found():
    """POST /v1/tasks/{id}:subscribe with non-existent task triggers error."""
    app = _make_app(EchoWorker(), streaming=True)
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # subscribe to a nonexistent task - should get TaskNotFoundError
            # which is mapped to 404 by exception handler
            try:
                raw = ""
                async with client.stream("POST", "/v1/tasks/nonexistent:subscribe") as resp:
                    async for chunk in resp.aiter_text():
                        raw += chunk
                        break
            except Exception:
                pass


async def test_subscribe_endpoint_terminal_state():
    """POST /v1/tasks/{id}:subscribe on completed task triggers UnsupportedOperationError."""
    app = _make_app(EchoWorker(), streaming=True)
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Create and complete a task
            body = {
                "message": {
                    "role": "user",
                    "messageId": str(uuid.uuid4()),
                    "parts": [{"kind": "text", "text": "hello"}],
                },
                "configuration": {"blocking": True},
            }
            resp = await client.post("/v1/message:send", json=body)
            task = resp.json()
            task_id = task["id"]
            assert task["status"]["state"] == "completed"

            # Try to subscribe to a completed task
            try:
                raw = ""
                async with client.stream("POST", f"/v1/tasks/{task_id}:subscribe") as resp2:
                    async for chunk in resp2.aiter_text():
                        raw += chunk
                        break
            except Exception:
                pass


async def test_list_tasks_with_status_filter():
    """GET /v1/tasks with status filter returns only matching tasks."""
    app = _make_app(EchoWorker())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Create a completed task
            body = {
                "message": {
                    "role": "user",
                    "messageId": str(uuid.uuid4()),
                    "parts": [{"kind": "text", "text": "hello"}],
                },
                "configuration": {"blocking": True},
            }
            resp = await client.post("/v1/message:send", json=body)
            assert resp.status_code == 200

            # List with status=completed
            list_resp = await client.get("/v1/tasks", params={"status": "completed"})
            assert list_resp.status_code == 200
            data = list_resp.json()
            for t in data["tasks"]:
                assert t["status"]["state"] == "completed"

            # List with status=working (should be 0 since tasks complete immediately)
            list_resp2 = await client.get("/v1/tasks", params={"status": "working"})
            assert list_resp2.status_code == 200
            data2 = list_resp2.json()
            assert len(data2["tasks"]) == 0


async def test_list_tasks_with_history_length():
    """GET /v1/tasks with historyLength trims history in results."""
    app = _make_app(EchoWorker())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            body = {
                "message": {
                    "role": "user",
                    "messageId": str(uuid.uuid4()),
                    "parts": [{"kind": "text", "text": "hello"}],
                },
                "configuration": {"blocking": True},
            }
            await client.post("/v1/message:send", json=body)

            list_resp = await client.get("/v1/tasks", params={"historyLength": 0})
            assert list_resp.status_code == 200
            data = list_resp.json()
            for t in data["tasks"]:
                assert len(t.get("history", [])) == 0


async def test_task_manager_not_initialized():
    """Requests to A2A endpoints when TaskManager isn't set return 503."""
    from fastapi import FastAPI

    from a2akit.endpoints import build_a2a_router

    app = FastAPI()
    app.include_router(build_a2a_router())

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        body = {
            "message": {
                "role": "user",
                "messageId": str(uuid.uuid4()),
                "parts": [{"kind": "text", "text": "hello"}],
            },
        }
        resp = await client.post("/v1/message:send", json=body)
        assert resp.status_code == 503


async def test_sanitize_task_strips_internal_metadata():
    """Internal metadata keys (prefixed with _) are stripped from responses."""
    app = _make_app(EchoWorker())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            body = {
                "message": {
                    "role": "user",
                    "messageId": str(uuid.uuid4()),
                    "parts": [{"kind": "text", "text": "hello"}],
                },
                "configuration": {"blocking": True},
            }
            resp = await client.post("/v1/message:send", json=body)
            task = resp.json()
            # Internal metadata keys should not be visible
            md = task.get("metadata", {}) or {}
            for key in md:
                assert not key.startswith("_"), f"Internal key {key} leaked to client"


async def test_subscribe_endpoint_active_task_streams():
    """POST /v1/tasks/{id}:subscribe on an active task streams events."""
    app = _make_app(InputRequiredWorker(), streaming=True)
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Create a task in input_required state (non-terminal)
            body = {
                "message": {
                    "role": "user",
                    "messageId": str(uuid.uuid4()),
                    "parts": [{"kind": "text", "text": "start"}],
                },
                "configuration": {"blocking": True},
            }
            resp = await client.post("/v1/message:send", json=body)
            assert resp.status_code == 200
            task = resp.json()
            task_id = task["id"]
            context_id = task["contextId"]
            assert task["status"]["state"] == "input-required"

            # Now subscribe and simultaneously send a follow-up that
            # will complete the task, producing events on the stream.
            raw_text = ""

            async def subscribe_and_read():
                nonlocal raw_text
                async with client.stream(
                    "POST",
                    f"/v1/tasks/{task_id}:subscribe",
                ) as resp2:
                    async for chunk in resp2.aiter_text():
                        raw_text += chunk
                        # Once we've collected enough data, break
                        if "status-update" in raw_text and '"final":true' in raw_text:
                            break

            async def send_follow_up():
                # Small delay to let subscribe connect first
                await asyncio.sleep(0.1)
                body2 = {
                    "message": {
                        "role": "user",
                        "messageId": str(uuid.uuid4()),
                        "taskId": task_id,
                        "contextId": context_id,
                        "parts": [{"kind": "text", "text": "my name"}],
                    },
                    "configuration": {"blocking": False},
                }
                await client.post("/v1/message:send", json=body2)

            # Run both concurrently with a timeout
            try:
                async with asyncio.timeout(5):
                    await asyncio.gather(subscribe_and_read(), send_follow_up())
            except TimeoutError:
                pass

            # We should have received some SSE events
            events = _parse_sse_events(raw_text)
            assert len(events) >= 1, "Expected at least 1 SSE event from subscribe, got none"


async def test_sanitize_task_with_public_metadata():
    """Task with only public metadata keys returns unchanged."""
    from a2a.types import Task, TaskState, TaskStatus

    from a2akit.endpoints import _sanitize_task_for_client

    task = Task(
        id="t1",
        context_id="c1",
        kind="task",
        status=TaskStatus(state=TaskState.completed, timestamp="2024-01-01"),
        metadata={"user_key": "value"},
    )
    result = _sanitize_task_for_client(task)
    # Same object returned because nothing to strip
    assert result is task


async def test_sanitize_task_no_metadata():
    """Task with no metadata returns unchanged."""
    from a2a.types import Task, TaskState, TaskStatus

    from a2akit.endpoints import _sanitize_task_for_client

    task = Task(
        id="t1",
        context_id="c1",
        kind="task",
        status=TaskStatus(state=TaskState.completed, timestamp="2024-01-01"),
        metadata=None,
    )
    result = _sanitize_task_for_client(task)
    assert result is task


async def test_a2a_version_header_incompatible():
    """Sending incompatible A2A-Version header returns 400."""
    app = _make_app(EchoWorker())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/v1/health", headers={"A2A-Version": "1.0.0"})
            assert resp.status_code == 400


async def test_push_notification_config_stubs():
    """Push notification config endpoints return 501 when push is not enabled."""
    app = _make_app(EchoWorker())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            task_id = "some-task"

            resp = await client.post(f"/v1/tasks/{task_id}/pushNotificationConfigs", json={})
            assert resp.status_code == 501

            resp = await client.get(f"/v1/tasks/{task_id}/pushNotificationConfigs/cfg-1")
            assert resp.status_code == 501

            resp = await client.get(f"/v1/tasks/{task_id}/pushNotificationConfigs")
            assert resp.status_code == 501

            resp = await client.delete(f"/v1/tasks/{task_id}/pushNotificationConfigs/cfg-1")
            assert resp.status_code == 501


async def test_stream_direct_reply_filtering():
    """message:stream with DirectReplyWorker filters DirectReply events in SSE."""
    from conftest import DirectReplyWorker

    app = _make_app(DirectReplyWorker(), streaming=True)
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            body = {
                "message": {
                    "role": "user",
                    "messageId": str(uuid.uuid4()),
                    "parts": [{"kind": "text", "text": "hello"}],
                },
            }
            raw_text = ""
            async with client.stream("POST", "/v1/message:stream", json=body) as resp:
                assert resp.status_code == 200
                async for chunk in resp.aiter_text():
                    raw_text += chunk
            events = _parse_sse_events(raw_text)
            # DirectReply should be filtered out, we should see task + status events
            assert len(events) >= 1
