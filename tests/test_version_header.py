"""Tests for A2A-Version header validation."""

from __future__ import annotations

import uuid

import httpx
from asgi_lifespan import LifespanManager

from conftest import EchoWorker, _make_app


def _send_body() -> dict:
    """Create a minimal message:send body."""
    return {
        "message": {
            "role": "user",
            "messageId": str(uuid.uuid4()),
            "parts": [{"kind": "text", "text": "hi"}],
        },
        "configuration": {"blocking": True},
    }


async def test_valid_version_header():
    """Sending with A2A-Version: 0.3.0 returns 200 OK."""
    app = _make_app(EchoWorker())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                "/v1/message:send",
                json=_send_body(),
                headers={"A2A-Version": "0.3.0"},
            )
            assert r.status_code == 200


async def test_incompatible_version_header():
    """Sending with A2A-Version: 1.0.0 returns 400."""
    app = _make_app(EchoWorker())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                "/v1/message:send",
                json=_send_body(),
                headers={"A2A-Version": "1.0.0"},
            )
            assert r.status_code == 400


async def test_missing_version_header():
    """Sending without A2A-Version header returns 200 OK (tolerated)."""
    app = _make_app(EchoWorker())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post("/v1/message:send", json=_send_body())
            assert r.status_code == 200
