"""Tests for agent card discovery endpoint."""

from __future__ import annotations

import httpx
import pytest
from asgi_lifespan import LifespanManager

from conftest import EchoWorker, _make_app


@pytest.fixture
async def client():
    app = _make_app(EchoWorker())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def test_agent_card_discovery(client: httpx.AsyncClient):
    """GET /.well-known/agent-card.json should return the agent card with expected fields."""
    resp = await client.get("/.well-known/agent-card.json")
    assert resp.status_code == 200

    card = resp.json()
    assert card["name"] == "Test Agent"
    assert card["description"] == "Test agent for unit tests"
    assert card["version"] == "0.0.1"
    assert "/v1" in card["url"]
    assert card["protocolVersion"] == "0.3.0"
