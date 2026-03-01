"""Tests for context load/update persistence across messages."""

from __future__ import annotations

import uuid

import httpx
import pytest
from asgi_lifespan import LifespanManager

from conftest import ContextWorker, _make_app


@pytest.fixture
async def client():
    app = _make_app(ContextWorker())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def test_context_load_update(client: httpx.AsyncClient):
    """Context data should persist across separate tasks sharing the same contextId."""
    context_id = str(uuid.uuid4())

    # First message: creates a new task, context counter starts at 0, incremented to 1
    body1 = {
        "message": {
            "role": "user",
            "messageId": str(uuid.uuid4()),
            "contextId": context_id,
            "parts": [{"kind": "text", "text": "first"}],
        },
        "configuration": {"blocking": True},
    }
    resp1 = await client.post("/v1/message:send", json=body1)
    assert resp1.status_code == 200

    task1 = resp1.json()
    assert task1["status"]["state"] == "completed"

    # Extract text from artifacts to verify "Count: 1"
    texts1 = []
    for artifact in task1.get("artifacts", []):
        for part in artifact.get("parts", []):
            if part.get("kind") == "text":
                texts1.append(part["text"])
    assert any("Count: 1" in t for t in texts1)

    # Second message: same contextId but NO taskId (creates a new task).
    # The context_id ties the context data together across tasks.
    body2 = {
        "message": {
            "role": "user",
            "messageId": str(uuid.uuid4()),
            "contextId": context_id,
            "parts": [{"kind": "text", "text": "second"}],
        },
        "configuration": {"blocking": True},
    }
    resp2 = await client.post("/v1/message:send", json=body2)
    assert resp2.status_code == 200

    task2 = resp2.json()
    assert task2["status"]["state"] == "completed"

    # Extract text from artifacts to verify "Count: 2"
    texts2 = []
    for artifact in task2.get("artifacts", []):
        for part in artifact.get("parts", []):
            if part.get("kind") == "text":
                texts2.append(part["text"])
    assert any("Count: 2" in t for t in texts2)
