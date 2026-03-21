"""Tests for agent/getAuthenticatedExtendedCard."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import httpx
from asgi_lifespan import LifespanManager

if TYPE_CHECKING:
    from fastapi import Request

from a2akit import (
    A2AClient,
    A2AServer,
    AgentCardConfig,
    CapabilitiesConfig,
    SkillConfig,
    TaskContext,
    Worker,
)


class _EchoWorker(Worker):
    async def handle(self, ctx: TaskContext) -> None:
        await ctx.complete(f"Echo: {ctx.user_text}")


async def _extended_provider(request: Request) -> AgentCardConfig:
    return AgentCardConfig(
        name="Extended Agent",
        description="Extended description",
        version="2.0.0",
        protocol="http+json",
        skills=[
            SkillConfig(id="s1", name="Skill One", description="Public skill", tags=[]),
            SkillConfig(id="s2", name="Skill Two", description="Secret skill", tags=["premium"]),
        ],
    )


def _make_app(*, with_provider: bool = True, protocol: str = "http+json"):
    server = A2AServer(
        worker=_EchoWorker(),
        agent_card=AgentCardConfig(
            name="Test Agent",
            description="Test",
            version="1.0.0",
            protocol=protocol,
            skills=[SkillConfig(id="s1", name="Skill One", description="Public", tags=[])],
        ),
        extended_card_provider=_extended_provider if with_provider else None,
    )
    return server.as_fastapi_app()


# --- REST tests ---


class TestRestExtendedCard:
    async def test_get_extended_card_success(self):
        app = _make_app(with_provider=True)
        async with LifespanManager(app) as manager:
            transport = httpx.ASGITransport(app=manager.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/v1/card")
                assert resp.status_code == 200
                card = resp.json()
                assert card["name"] == "Extended Agent"
                assert len(card["skills"]) == 2

    async def test_get_extended_card_not_configured(self):
        app = _make_app(with_provider=False)
        async with LifespanManager(app) as manager:
            transport = httpx.ASGITransport(app=manager.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/v1/card")
                assert resp.status_code == 404
                data = resp.json()
                assert data["detail"]["code"] == -32007

    async def test_public_card_reflects_flag(self):
        app = _make_app(with_provider=True)
        async with LifespanManager(app) as manager:
            transport = httpx.ASGITransport(app=manager.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/.well-known/agent-card.json")
                assert resp.status_code == 200
                card = resp.json()
                assert card["supportsAuthenticatedExtendedCard"] is True

    async def test_public_card_flag_false_when_no_provider(self):
        app = _make_app(with_provider=False)
        async with LifespanManager(app) as manager:
            transport = httpx.ASGITransport(app=manager.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/.well-known/agent-card.json")
                card = resp.json()
                assert card.get("supportsAuthenticatedExtendedCard") in (None, False)


# --- JSON-RPC tests ---


class TestJsonRpcExtendedCard:
    def _rpc(self, method, params=None):
        body = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method}
        if params is not None:
            body["params"] = params
        return body

    async def test_get_extended_card_jsonrpc_success(self):
        app = _make_app(with_provider=True, protocol="jsonrpc")
        async with LifespanManager(app) as manager:
            transport = httpx.ASGITransport(app=manager.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post("/", json=self._rpc("agent/getAuthenticatedExtendedCard"))
                data = resp.json()
                assert "result" in data
                assert data["result"]["name"] == "Extended Agent"
                assert len(data["result"]["skills"]) == 2

    async def test_get_extended_card_jsonrpc_not_configured(self):
        app = _make_app(with_provider=False, protocol="jsonrpc")
        async with LifespanManager(app) as manager:
            transport = httpx.ASGITransport(app=manager.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post("/", json=self._rpc("agent/getAuthenticatedExtendedCard"))
                data = resp.json()
                assert data["error"]["code"] == -32007


# --- Client integration tests ---


class TestClientExtendedCard:
    async def test_client_get_extended_card_rest(self):
        app = _make_app(with_provider=True, protocol="http+json")
        async with LifespanManager(app) as manager:
            transport = httpx.ASGITransport(app=manager.app)
            http = httpx.AsyncClient(transport=transport, base_url="http://test")
            async with A2AClient("http://test", httpx_client=http) as client:
                extended = await client.get_extended_card()
                assert extended.name == "Extended Agent"
                assert len(extended.skills) == 2
            await http.aclose()

    async def test_client_get_extended_card_jsonrpc(self):
        app = _make_app(with_provider=True, protocol="jsonrpc")
        async with LifespanManager(app) as manager:
            transport = httpx.ASGITransport(app=manager.app)
            http = httpx.AsyncClient(transport=transport, base_url="http://test")
            async with A2AClient("http://test", httpx_client=http, protocol="jsonrpc") as client:
                extended = await client.get_extended_card()
                assert extended.name == "Extended Agent"
            await http.aclose()


# --- AgentCardConfig field tests ---


class TestAgentCardConfigFlag:
    def test_default_false(self):
        cfg = AgentCardConfig(name="X", description="X", version="0.1.0")
        assert cfg.supports_authenticated_extended_card is False

    def test_explicit_true(self):
        cfg = AgentCardConfig(
            name="X",
            description="X",
            version="0.1.0",
            supports_authenticated_extended_card=True,
        )
        assert cfg.supports_authenticated_extended_card is True


# --- CapabilitiesConfig validator relaxed ---


class TestCapabilitiesConfigRelaxed:
    def test_extended_agent_card_no_longer_raises(self):
        """extended_agent_card=True no longer raises NotImplementedError."""
        caps = CapabilitiesConfig(extended_agent_card=True)
        assert caps.extended_agent_card is True
