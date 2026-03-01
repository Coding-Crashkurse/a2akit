"""Tests for input-required workflow (multi-turn conversation)."""

from __future__ import annotations

import uuid

import httpx
import pytest
from asgi_lifespan import LifespanManager

from conftest import InputRequiredWorker, _make_app


@pytest.fixture
async def client():
    app = _make_app(InputRequiredWorker())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def test_input_required_then_follow_up(client: httpx.AsyncClient):
    """First message -> input_required, follow-up with taskId -> completed."""
    # Step 1: send initial message
    body1 = {
        "message": {
            "role": "user",
            "messageId": str(uuid.uuid4()),
            "parts": [{"kind": "text", "text": "start"}],
        },
        "configuration": {"blocking": True},
    }
    resp1 = await client.post("/v1/message:send", json=body1)
    assert resp1.status_code == 200

    task1 = resp1.json()
    assert task1["status"]["state"] == "input-required"
    task_id = task1["id"]

    # Step 2: send follow-up with the taskId
    body2 = {
        "message": {
            "role": "user",
            "messageId": str(uuid.uuid4()),
            "taskId": task_id,
            "parts": [{"kind": "text", "text": "my follow-up"}],
        },
        "configuration": {"blocking": True},
    }
    resp2 = await client.post("/v1/message:send", json=body2)
    assert resp2.status_code == 200

    task2 = resp2.json()
    assert task2["status"]["state"] == "completed"

    # The completed response should contain the follow-up text
    texts = []
    for artifact in task2.get("artifacts", []):
        for part in artifact.get("parts", []):
            if part.get("kind") == "text":
                texts.append(part["text"])
    assert any("Got follow-up: my follow-up" in t for t in texts)
