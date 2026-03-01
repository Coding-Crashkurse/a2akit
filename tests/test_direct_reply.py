"""Tests for direct reply (Message-level response, no task wrapper)."""

from __future__ import annotations

import uuid

import httpx
import pytest
from asgi_lifespan import LifespanManager

from conftest import DirectReplyWorker, _make_app


@pytest.fixture
async def client():
    app = _make_app(DirectReplyWorker())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def test_direct_reply_returns_message(client: httpx.AsyncClient):
    """DirectReplyWorker should return a Message (not a Task)."""
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

    data = resp.json()
    # A Message has "role" but no "kind": "task"
    assert "role" in data
    assert data.get("kind") != "task"
    # Verify the direct reply text
    texts = [p["text"] for p in data["parts"] if p.get("kind") == "text"]
    assert any("Direct: hello" in t for t in texts)
