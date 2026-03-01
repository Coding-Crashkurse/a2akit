"""End-to-end idempotency test via HTTP."""

from __future__ import annotations

import uuid

import httpx
from asgi_lifespan import LifespanManager

from conftest import EchoWorker, _make_app


async def test_duplicate_message_id_returns_same_task():
    """Sending the same messageId twice with the same contextId returns the same task id."""
    app = _make_app(EchoWorker(), blocking_timeout_s=2)
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            msg_id = str(uuid.uuid4())
            ctx_id = str(uuid.uuid4())
            body = {
                "message": {
                    "role": "user",
                    "messageId": msg_id,
                    "contextId": ctx_id,
                    "parts": [{"kind": "text", "text": "hi"}],
                },
                "configuration": {"blocking": True},
            }
            r1 = await client.post("/v1/message:send", json=body)
            r2 = await client.post("/v1/message:send", json=body)

            assert r1.status_code == 200
            assert r2.status_code == 200
            assert r1.json()["id"] == r2.json()["id"]
