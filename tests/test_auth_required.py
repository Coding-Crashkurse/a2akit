"""Tests for the auth_required worker flow."""

from __future__ import annotations

import uuid

import httpx
from asgi_lifespan import LifespanManager

from conftest import AuthRequiredWorker, _make_app


async def test_auth_required_then_follow_up():
    app = _make_app(AuthRequiredWorker())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # First message -> auth_required
            body1 = {
                "message": {
                    "role": "user",
                    "messageId": str(uuid.uuid4()),
                    "parts": [{"kind": "text", "text": "do stuff"}],
                },
                "configuration": {"blocking": True},
            }
            r1 = await client.post("/v1/message:send", json=body1)
            assert r1.status_code == 200
            task1 = r1.json()
            assert task1["status"]["state"] == "auth-required"

            # Follow-up -> completed
            body2 = {
                "message": {
                    "role": "user",
                    "messageId": str(uuid.uuid4()),
                    "taskId": task1["id"],
                    "contextId": task1["contextId"],
                    "parts": [{"kind": "text", "text": "my-api-key"}],
                },
                "configuration": {"blocking": True},
            }
            r2 = await client.post("/v1/message:send", json=body2)
            assert r2.status_code == 200
            task2 = r2.json()
            assert task2["status"]["state"] == "completed"
