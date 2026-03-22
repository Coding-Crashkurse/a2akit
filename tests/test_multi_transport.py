"""Tests for simultaneous multi-transport server (Spec §3.4, §5.5)."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from a2akit import A2AServer, AgentCardConfig, CapabilitiesConfig, TaskContext, Worker


class EchoWorker(Worker):
    async def handle(self, ctx: TaskContext) -> None:
        await ctx.complete(f"Echo: {ctx.user_text}")


def _send_body(text="hello"):
    return {
        "message": {
            "role": "user",
            "parts": [{"kind": "text", "text": text}],
            "messageId": "1",
        },
        "configuration": {"blocking": True},
    }


@pytest.fixture
async def multi_client():
    """Client for a server with both JSON-RPC and REST."""
    server = A2AServer(
        worker=EchoWorker(),
        agent_card=AgentCardConfig(
            name="Multi",
            description="Test",
            protocol="jsonrpc",
            capabilities=CapabilitiesConfig(streaming=True),
        ),
        additional_protocols=["HTTP"],
    )
    app = server.as_fastapi_app()

    from asgi_lifespan import LifespanManager

    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@pytest.fixture
async def single_client():
    """Client for a server with only JSON-RPC (no additional)."""
    server = A2AServer(
        worker=EchoWorker(),
        agent_card=AgentCardConfig(
            name="Single",
            description="Test",
            protocol="jsonrpc",
        ),
    )
    app = server.as_fastapi_app()

    from asgi_lifespan import LifespanManager

    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@pytest.mark.asyncio
async def test_jsonrpc_works(multi_client):
    """JSON-RPC message/send works on multi-transport server."""
    envelope = {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "message/send",
        "params": _send_body(),
    }
    resp = await multi_client.post("/", json=envelope)
    assert resp.status_code == 200
    data = resp.json()
    assert "result" in data


@pytest.mark.asyncio
async def test_rest_works(multi_client):
    """REST message:send works on multi-transport server."""
    resp = await multi_client.post("/v1/message:send", json=_send_body())
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_agent_card_has_additional_interfaces(multi_client):
    """Agent card includes additionalInterfaces for both transports."""
    resp = await multi_client.get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    card = resp.json()
    interfaces = card.get("additionalInterfaces", [])
    transports = {i["transport"] for i in interfaces}
    assert "JSONRPC" in transports or "jsonrpc" in transports
    assert "HTTP+JSON" in transports or "http+json" in transports


@pytest.mark.asyncio
async def test_single_transport_no_rest(single_client):
    """Server with only jsonrpc returns 404/405 for REST endpoints."""
    resp = await single_client.post("/v1/message:send", json=_send_body())
    assert resp.status_code in (404, 405)
