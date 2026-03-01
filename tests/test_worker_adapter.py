"""Tests for WorkerAdapter error-handling behaviour, exercised through HTTP."""

from __future__ import annotations

import uuid

import httpx
from asgi_lifespan import LifespanManager

from conftest import CrashWorker, NoLifecycleWorker, _make_app


async def test_adapter_marks_failed_on_crash():
    """Worker crash -> task marked failed by adapter."""
    app = _make_app(CrashWorker())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            body = {
                "message": {
                    "role": "user",
                    "messageId": str(uuid.uuid4()),
                    "parts": [{"kind": "text", "text": "boom"}],
                },
                "configuration": {"blocking": True},
            }
            r = await client.post("/v1/message:send", json=body)
            assert r.status_code == 200
            data = r.json()
            assert data["status"]["state"] == "failed"


async def test_adapter_marks_failed_on_no_lifecycle():
    """Worker returns without lifecycle call -> adapter marks failed."""
    app = _make_app(NoLifecycleWorker())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            body = {
                "message": {
                    "role": "user",
                    "messageId": str(uuid.uuid4()),
                    "parts": [{"kind": "text", "text": "hello"}],
                },
                "configuration": {"blocking": True},
            }
            r = await client.post("/v1/message:send", json=body)
            assert r.status_code == 200
            data = r.json()
            assert data["status"]["state"] == "failed"
