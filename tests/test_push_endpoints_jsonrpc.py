"""Integration tests for push notification JSON-RPC endpoints."""

from __future__ import annotations

import uuid

import httpx
import pytest
from asgi_lifespan import LifespanManager

from a2akit import A2AServer, AgentCardConfig, CapabilitiesConfig, TaskContext, Worker


class EchoWorker(Worker):
    async def handle(self, ctx: TaskContext) -> None:
        await ctx.complete(f"Echo: {ctx.user_text}")


def _make_push_app(*, push_enabled: bool = True) -> object:
    server = A2AServer(
        worker=EchoWorker(),
        agent_card=AgentCardConfig(
            name="Push Test Agent",
            description="Test agent for push notification JSON-RPC tests",
            version="0.0.1",
            protocol="jsonrpc",
            capabilities=CapabilitiesConfig(push_notifications=push_enabled),
        ),
        push_allow_http=True,
    )
    return server.as_fastapi_app()


def _rpc(method, params=None, req_id=None):
    return {
        "jsonrpc": "2.0",
        "id": req_id or str(uuid.uuid4()),
        "method": method,
        "params": params or {},
    }


def _send_body(text="hello"):
    return {
        "message": {
            "role": "user",
            "messageId": str(uuid.uuid4()),
            "parts": [{"kind": "text", "text": text}],
        },
        "configuration": {"blocking": True},
    }


@pytest.fixture
async def push_app():
    raw_app = _make_push_app(push_enabled=True)
    async with LifespanManager(raw_app) as manager:
        yield manager.app


@pytest.fixture
async def push_client(push_app):
    transport = httpx.ASGITransport(app=push_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def no_push_app():
    raw_app = _make_push_app(push_enabled=False)
    async with LifespanManager(raw_app) as manager:
        yield manager.app


@pytest.fixture
async def no_push_client(no_push_app):
    transport = httpx.ASGITransport(app=no_push_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _create_task(client):
    """Helper: create a task via JSON-RPC and return task_id."""
    resp = await client.post("/", json=_rpc("message/send", _send_body()))
    data = resp.json()
    return data["result"]["id"]


async def test_set_returns_error_when_disabled(no_push_client):
    resp = await no_push_client.post(
        "/",
        json=_rpc(
            "tasks/pushNotificationConfig/set",
            {
                "taskId": "task-1",
                "pushNotificationConfig": {"url": "https://example.com"},
            },
        ),
    )
    data = resp.json()
    assert data["error"]["code"] == -32003


async def test_get_returns_error_when_disabled(no_push_client):
    resp = await no_push_client.post(
        "/", json=_rpc("tasks/pushNotificationConfig/get", {"id": "task-1"})
    )
    assert resp.json()["error"]["code"] == -32003


async def test_list_returns_error_when_disabled(no_push_client):
    resp = await no_push_client.post(
        "/", json=_rpc("tasks/pushNotificationConfig/list", {"id": "task-1"})
    )
    assert resp.json()["error"]["code"] == -32003


async def test_delete_returns_error_when_disabled(no_push_client):
    resp = await no_push_client.post(
        "/",
        json=_rpc(
            "tasks/pushNotificationConfig/delete",
            {
                "id": "task-1",
                "pushNotificationConfigId": "cfg-1",
            },
        ),
    )
    assert resp.json()["error"]["code"] == -32003


async def test_set_config(push_client):
    task_id = await _create_task(push_client)

    resp = await push_client.post(
        "/",
        json=_rpc(
            "tasks/pushNotificationConfig/set",
            {
                "taskId": task_id,
                "pushNotificationConfig": {
                    "url": "https://example.com/webhook",
                    "token": "secret",
                },
            },
        ),
    )
    data = resp.json()
    assert "result" in data
    result = data["result"]
    assert result["taskId"] == task_id
    assert result["pushNotificationConfig"]["url"] == "https://example.com/webhook"


async def test_get_config(push_client):
    task_id = await _create_task(push_client)

    # Set
    await push_client.post(
        "/",
        json=_rpc(
            "tasks/pushNotificationConfig/set",
            {
                "taskId": task_id,
                "pushNotificationConfig": {
                    "id": "cfg-1",
                    "url": "https://example.com/webhook",
                },
            },
        ),
    )

    # Get
    resp = await push_client.post(
        "/",
        json=_rpc(
            "tasks/pushNotificationConfig/get",
            {
                "id": task_id,
                "pushNotificationConfigId": "cfg-1",
            },
        ),
    )
    result = resp.json()["result"]
    assert result["pushNotificationConfig"]["id"] == "cfg-1"


async def test_list_configs(push_client):
    task_id = await _create_task(push_client)

    # Set two configs
    for cid in ("a", "b"):
        await push_client.post(
            "/",
            json=_rpc(
                "tasks/pushNotificationConfig/set",
                {
                    "taskId": task_id,
                    "pushNotificationConfig": {
                        "id": cid,
                        "url": f"https://{cid}.com",
                    },
                },
            ),
        )

    resp = await push_client.post(
        "/", json=_rpc("tasks/pushNotificationConfig/list", {"id": task_id})
    )
    result = resp.json()["result"]
    assert len(result) == 2


async def test_delete_config(push_client):
    task_id = await _create_task(push_client)

    # Set
    await push_client.post(
        "/",
        json=_rpc(
            "tasks/pushNotificationConfig/set",
            {
                "taskId": task_id,
                "pushNotificationConfig": {
                    "id": "cfg-1",
                    "url": "https://example.com",
                },
            },
        ),
    )

    # Delete
    resp = await push_client.post(
        "/",
        json=_rpc(
            "tasks/pushNotificationConfig/delete",
            {
                "id": task_id,
                "pushNotificationConfigId": "cfg-1",
            },
        ),
    )
    assert resp.json()["result"] is None

    # Verify gone
    resp = await push_client.post(
        "/", json=_rpc("tasks/pushNotificationConfig/list", {"id": task_id})
    )
    assert resp.json()["result"] == []


async def test_set_missing_task_id(push_client):
    resp = await push_client.post(
        "/",
        json=_rpc(
            "tasks/pushNotificationConfig/set",
            {
                "pushNotificationConfig": {"url": "https://example.com"},
            },
        ),
    )
    assert resp.json()["error"]["code"] == -32602


async def test_set_missing_config(push_client):
    resp = await push_client.post(
        "/",
        json=_rpc("tasks/pushNotificationConfig/set", {"taskId": "task-1"}),
    )
    assert resp.json()["error"]["code"] == -32602


async def test_set_task_not_found(push_client):
    resp = await push_client.post(
        "/",
        json=_rpc(
            "tasks/pushNotificationConfig/set",
            {
                "taskId": "nonexistent",
                "pushNotificationConfig": {"url": "https://example.com"},
            },
        ),
    )
    assert resp.json()["error"]["code"] == -32001


async def test_delete_missing_config_id(push_client):
    resp = await push_client.post(
        "/",
        json=_rpc("tasks/pushNotificationConfig/delete", {"id": "task-1"}),
    )
    assert resp.json()["error"]["code"] == -32602
