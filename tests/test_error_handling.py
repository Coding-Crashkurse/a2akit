"""Tests for error handling: fail, reject, crash, no-lifecycle, and invalid requests."""

from __future__ import annotations

import uuid

import httpx
import pytest
from asgi_lifespan import LifespanManager

from conftest import (
    CrashWorker,
    FailWorker,
    NoLifecycleWorker,
    RejectWorker,
    _make_app,
)


@pytest.fixture
async def fail_client():
    app = _make_app(FailWorker())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@pytest.fixture
async def reject_client():
    app = _make_app(RejectWorker())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@pytest.fixture
async def crash_client():
    app = _make_app(CrashWorker())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@pytest.fixture
async def no_lifecycle_client():
    app = _make_app(NoLifecycleWorker())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


def _send_body(text: str = "hello") -> dict:
    return {
        "message": {
            "role": "user",
            "messageId": str(uuid.uuid4()),
            "parts": [{"kind": "text", "text": text}],
        },
        "configuration": {"blocking": True},
    }


async def test_worker_fail(fail_client: httpx.AsyncClient):
    """FailWorker should produce a task in 'failed' state with the failure message."""
    resp = await fail_client.post("/v1/message:send", json=_send_body())
    assert resp.status_code == 200

    task = resp.json()
    assert task["status"]["state"] == "failed"

    # Check that the failure message is present
    status_msg = task["status"].get("message", {})
    texts = [p.get("text", "") for p in status_msg.get("parts", []) if p.get("kind") == "text"]
    assert any("Something went wrong" in t for t in texts)


async def test_worker_reject(reject_client: httpx.AsyncClient):
    """RejectWorker should produce a task in 'rejected' state."""
    resp = await reject_client.post("/v1/message:send", json=_send_body())
    assert resp.status_code == 200

    task = resp.json()
    assert task["status"]["state"] == "rejected"


async def test_worker_crash(crash_client: httpx.AsyncClient):
    """CrashWorker raises an exception; the framework should catch it and fail the task."""
    resp = await crash_client.post("/v1/message:send", json=_send_body())
    assert resp.status_code == 200

    task = resp.json()
    assert task["status"]["state"] == "failed"


async def test_worker_no_lifecycle(no_lifecycle_client: httpx.AsyncClient):
    """NoLifecycleWorker returns without calling a lifecycle method; task should be failed."""
    resp = await no_lifecycle_client.post("/v1/message:send", json=_send_body())
    assert resp.status_code == 200

    task = resp.json()
    assert task["status"]["state"] == "failed"


async def test_invalid_request_missing_message_id(fail_client: httpx.AsyncClient):
    """Sending a request with empty messageId should return 400."""
    body = {
        "message": {
            "role": "user",
            "messageId": "",
            "parts": [{"kind": "text", "text": "hello"}],
        },
        "configuration": {"blocking": True},
    }
    resp = await fail_client.post("/v1/message:send", json=body)
    assert resp.status_code == 400
