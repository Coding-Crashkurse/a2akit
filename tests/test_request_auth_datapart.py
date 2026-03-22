"""Tests for request_auth() with structured DataPart (Spec §4.5 SHOULD)."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from a2akit import A2AServer, AgentCardConfig, TaskContext, Worker


class AuthRequestWorker(Worker):
    """Worker that requests auth with structured details."""

    def __init__(self, *, use_schemes: bool = False, text_only: bool = False):
        self._use_schemes = use_schemes
        self._text_only = text_only

    async def handle(self, ctx: TaskContext) -> None:
        if self._text_only:
            await ctx.request_auth("Please login")
        elif self._use_schemes:
            await ctx.request_auth(
                "Login required",
                schemes=["Bearer", "OAuth2"],
                auth_url="https://auth.example.com/token",
                credentials_hint="Use your OAuth2 credentials",
            )
        else:
            await ctx.request_auth(
                schemes=["Bearer"],
                auth_url="https://auth.example.com/token",
            )


def _send_body():
    return {
        "message": {
            "role": "user",
            "parts": [{"kind": "text", "text": "hi"}],
            "messageId": "1",
        },
        "configuration": {"blocking": True},
    }


async def _make_client(worker):
    server = A2AServer(
        worker=worker,
        agent_card=AgentCardConfig(
            name="Auth Test",
            description="Test",
            protocol="http+json",
        ),
    )
    app = server.as_fastapi_app()

    from asgi_lifespan import LifespanManager

    manager = LifespanManager(app)
    started = await manager.__aenter__()
    transport = ASGITransport(app=started.app)
    client = AsyncClient(transport=transport, base_url="http://test")
    return client, manager


@pytest.mark.asyncio
async def test_request_auth_text_only():
    """request_auth('text') produces only a TextPart."""
    client, mgr = await _make_client(AuthRequestWorker(text_only=True))
    try:
        resp = await client.post("/v1/message:send", json=_send_body())
        assert resp.status_code == 200
        task = resp.json()
        assert task["status"]["state"] == "auth-required"
        msg = task["status"]["message"]
        parts = msg["parts"]
        assert len(parts) == 1
        assert parts[0]["kind"] == "text"
        assert parts[0]["text"] == "Please login"
    finally:
        await client.aclose()
        await mgr.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_request_auth_with_schemes_and_text():
    """request_auth with text + schemes produces TextPart + DataPart."""
    client, mgr = await _make_client(AuthRequestWorker(use_schemes=True))
    try:
        resp = await client.post("/v1/message:send", json=_send_body())
        assert resp.status_code == 200
        task = resp.json()
        assert task["status"]["state"] == "auth-required"
        msg = task["status"]["message"]
        parts = msg["parts"]
        assert len(parts) == 2
        assert parts[0]["kind"] == "text"
        assert parts[0]["text"] == "Login required"
        assert parts[1]["kind"] == "data"
        data = parts[1]["data"]
        assert data["schemes"] == ["Bearer", "OAuth2"]
        assert data["authUrl"] == "https://auth.example.com/token"
        assert data["credentialsHint"] == "Use your OAuth2 credentials"
    finally:
        await client.aclose()
        await mgr.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_request_auth_schemes_only():
    """request_auth with schemes only (no text) produces default TextPart + DataPart."""
    client, mgr = await _make_client(AuthRequestWorker())
    try:
        resp = await client.post("/v1/message:send", json=_send_body())
        assert resp.status_code == 200
        task = resp.json()
        assert task["status"]["state"] == "auth-required"
        msg = task["status"]["message"]
        parts = msg["parts"]
        # DataPart only (no details text provided, no default text added)
        assert len(parts) == 1
        assert parts[0]["kind"] == "data"
        assert parts[0]["data"]["schemes"] == ["Bearer"]
    finally:
        await client.aclose()
        await mgr.__aexit__(None, None, None)
