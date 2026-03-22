"""Tests for WWW-Authenticate header on 401 (Spec §4.4 SHOULD)."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from a2akit import (
    A2AServer,
    AgentCardConfig,
    BearerTokenMiddleware,
    TaskContext,
    Worker,
)


class EchoWorker(Worker):
    async def handle(self, ctx: TaskContext) -> None:
        await ctx.complete("ok")


async def _verify(token: str) -> dict | None:
    if token == "valid":
        return {"sub": "user1"}
    return None


@pytest.fixture
async def auth_client():
    server = A2AServer(
        worker=EchoWorker(),
        agent_card=AgentCardConfig(
            name="Auth Test",
            description="Test agent",
            protocol="http+json",
        ),
        middlewares=[BearerTokenMiddleware(verify=_verify, realm="test-realm")],
    )
    app = server.as_fastapi_app()

    from asgi_lifespan import LifespanManager

    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@pytest.mark.asyncio
async def test_missing_bearer_returns_401_with_www_authenticate(auth_client):
    """Missing auth returns 401 with WWW-Authenticate header."""
    resp = await auth_client.post(
        "/v1/message:send",
        json={
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": "hi"}],
                "messageId": "1",
            }
        },
    )
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == 'Bearer realm="test-realm"'


@pytest.mark.asyncio
async def test_invalid_bearer_returns_401(auth_client):
    """Invalid token returns 401."""
    resp = await auth_client.post(
        "/v1/message:send",
        json={
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": "hi"}],
                "messageId": "1",
            }
        },
        headers={"Authorization": "Bearer invalid-token"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_valid_bearer_succeeds(auth_client):
    """Valid token passes through to the worker."""
    resp = await auth_client.post(
        "/v1/message:send",
        json={
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": "hi"}],
                "messageId": "1",
            },
            "configuration": {"blocking": True},
        },
        headers={"Authorization": "Bearer valid"},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_agent_card_no_auth_needed(auth_client):
    """Agent card discovery does not require auth."""
    resp = await auth_client.get("/.well-known/agent-card.json")
    assert resp.status_code == 200
