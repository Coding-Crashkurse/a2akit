"""Tests for built-in auth middlewares (Bearer + ApiKey)."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from a2akit import (
    A2AServer,
    AgentCardConfig,
    ApiKeyMiddleware,
    BearerTokenMiddleware,
    TaskContext,
    Worker,
)


class ClaimsEchoWorker(Worker):
    async def handle(self, ctx: TaskContext) -> None:
        claims = ctx.request_context.get("auth_claims")
        api_key = ctx.request_context.get("api_key")
        if claims:
            await ctx.complete(f"claims={claims}")
        elif api_key:
            await ctx.complete(f"key={api_key}")
        else:
            await ctx.complete("no auth")


async def _verify_bearer(token: str) -> dict | None:
    if token == "good-token":
        return {"sub": "user1", "role": "admin"}
    return None


async def _make_app_client(middlewares):
    server = A2AServer(
        worker=ClaimsEchoWorker(),
        agent_card=AgentCardConfig(
            name="Auth Test",
            description="Test",
            protocol="http+json",
        ),
        middlewares=middlewares,
    )
    app = server.as_fastapi_app()

    from asgi_lifespan import LifespanManager

    manager = LifespanManager(app)
    started = await manager.__aenter__()
    transport = ASGITransport(app=started.app)
    client = AsyncClient(transport=transport, base_url="http://test")
    return client, manager


def _send_body():
    return {
        "message": {
            "role": "user",
            "parts": [{"kind": "text", "text": "hi"}],
            "messageId": "1",
        },
        "configuration": {"blocking": True},
    }


# -- Bearer tests --


@pytest.mark.asyncio
async def test_bearer_valid_token():
    client, mgr = await _make_app_client([BearerTokenMiddleware(verify=_verify_bearer)])
    try:
        resp = await client.post(
            "/v1/message:send",
            json=_send_body(),
            headers={"Authorization": "Bearer good-token"},
        )
        assert resp.status_code == 200
    finally:
        await client.aclose()
        await mgr.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_bearer_missing_token():
    client, mgr = await _make_app_client([BearerTokenMiddleware(verify=_verify_bearer)])
    try:
        resp = await client.post("/v1/message:send", json=_send_body())
        assert resp.status_code == 401
        assert "Bearer" in resp.headers.get("WWW-Authenticate", "")
    finally:
        await client.aclose()
        await mgr.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_bearer_invalid_token():
    client, mgr = await _make_app_client([BearerTokenMiddleware(verify=_verify_bearer)])
    try:
        resp = await client.post(
            "/v1/message:send",
            json=_send_body(),
            headers={"Authorization": "Bearer bad-token"},
        )
        assert resp.status_code == 401
    finally:
        await client.aclose()
        await mgr.__aexit__(None, None, None)


# -- ApiKey tests --


@pytest.mark.asyncio
async def test_apikey_valid():
    client, mgr = await _make_app_client([ApiKeyMiddleware(valid_keys={"sk-123", "sk-456"})])
    try:
        resp = await client.post(
            "/v1/message:send",
            json=_send_body(),
            headers={"X-API-Key": "sk-123"},
        )
        assert resp.status_code == 200
    finally:
        await client.aclose()
        await mgr.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_apikey_missing():
    client, mgr = await _make_app_client([ApiKeyMiddleware(valid_keys={"sk-123"})])
    try:
        resp = await client.post("/v1/message:send", json=_send_body())
        assert resp.status_code == 401
        assert "ApiKey" in resp.headers.get("WWW-Authenticate", "")
    finally:
        await client.aclose()
        await mgr.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_apikey_invalid():
    client, mgr = await _make_app_client([ApiKeyMiddleware(valid_keys={"sk-123"})])
    try:
        resp = await client.post(
            "/v1/message:send",
            json=_send_body(),
            headers={"X-API-Key": "wrong-key"},
        )
        assert resp.status_code == 401
    finally:
        await client.aclose()
        await mgr.__aexit__(None, None, None)


# -- Health excluded --


@pytest.mark.asyncio
async def test_health_excluded_from_auth():
    client, mgr = await _make_app_client([BearerTokenMiddleware(verify=_verify_bearer)])
    try:
        resp = await client.get("/v1/health")
        assert resp.status_code == 200
    finally:
        await client.aclose()
        await mgr.__aexit__(None, None, None)
