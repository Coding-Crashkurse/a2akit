"""Integration tests for push notification REST endpoints."""

from __future__ import annotations

import uuid

import httpx
import pytest
from asgi_lifespan import LifespanManager

from a2akit import A2AServer, AgentCardConfig, CapabilitiesConfig, TaskContext, Worker


class EchoWorker(Worker):
    async def handle(self, ctx: TaskContext) -> None:
        await ctx.complete(f"Echo: {ctx.user_text}")


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """Deterministic DNS for registration-time webhook URL validation."""

    async def _fake_getaddrinfo(hostname):
        return [(2, 1, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr("a2akit.push.validation._getaddrinfo", _fake_getaddrinfo)


def _make_push_app(*, push_enabled: bool = True, streaming: bool = False) -> object:
    server = A2AServer(
        worker=EchoWorker(),
        agent_card=AgentCardConfig(
            name="Push Test Agent",
            description="Test agent for push notification tests",
            version="0.0.1",
            protocol="http+json",
            capabilities=CapabilitiesConfig(
                streaming=streaming,
                push_notifications=push_enabled,
            ),
        ),
        push_allow_http=True,
    )
    return server.as_fastapi_app()


def _send_body(text="hello", blocking=True):
    return {
        "message": {
            "role": "user",
            "messageId": str(uuid.uuid4()),
            "parts": [{"kind": "text", "text": text}],
        },
        "configuration": {"blocking": blocking},
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


async def test_set_config_returns_501_when_disabled(no_push_client):
    resp = await no_push_client.post(
        "/v1/tasks/task-1/pushNotificationConfigs",
        json={"url": "https://example.com/webhook"},
    )
    assert resp.status_code == 501


async def test_get_config_returns_501_when_disabled(no_push_client):
    resp = await no_push_client.get("/v1/tasks/task-1/pushNotificationConfigs/cfg-1")
    assert resp.status_code == 501


async def test_list_configs_returns_501_when_disabled(no_push_client):
    resp = await no_push_client.get("/v1/tasks/task-1/pushNotificationConfigs")
    assert resp.status_code == 501


async def test_delete_config_returns_501_when_disabled(no_push_client):
    resp = await no_push_client.delete("/v1/tasks/task-1/pushNotificationConfigs/cfg-1")
    assert resp.status_code == 501


async def test_set_config_creates_config(push_client):
    # First create a task
    resp = await push_client.post("/v1/message:send", json=_send_body())
    assert resp.status_code == 200
    task_id = resp.json()["id"]

    # Set push config
    resp = await push_client.post(
        f"/v1/tasks/{task_id}/pushNotificationConfigs",
        json={"url": "https://example.com/webhook", "token": "secret"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["taskId"] == task_id
    assert data["pushNotificationConfig"]["url"] == "https://example.com/webhook"
    assert data["pushNotificationConfig"]["token"] == "secret"
    assert data["pushNotificationConfig"]["id"] is not None


async def test_set_config_auto_generates_id(push_client):
    resp = await push_client.post("/v1/message:send", json=_send_body())
    task_id = resp.json()["id"]

    resp = await push_client.post(
        f"/v1/tasks/{task_id}/pushNotificationConfigs",
        json={"url": "https://example.com/webhook"},
    )
    assert resp.status_code == 200
    config_id = resp.json()["pushNotificationConfig"]["id"]
    assert config_id is not None
    assert len(config_id) > 0


async def test_get_config_returns_stored(push_client):
    resp = await push_client.post("/v1/message:send", json=_send_body())
    task_id = resp.json()["id"]

    # Set config with explicit ID
    resp = await push_client.post(
        f"/v1/tasks/{task_id}/pushNotificationConfigs",
        json={"id": "my-cfg", "url": "https://example.com/webhook"},
    )
    assert resp.status_code == 200

    # Get by ID
    resp = await push_client.get(f"/v1/tasks/{task_id}/pushNotificationConfigs/my-cfg")
    assert resp.status_code == 200
    assert resp.json()["pushNotificationConfig"]["id"] == "my-cfg"


async def test_list_configs_returns_all(push_client):
    resp = await push_client.post("/v1/message:send", json=_send_body())
    task_id = resp.json()["id"]

    # Add two configs
    await push_client.post(
        f"/v1/tasks/{task_id}/pushNotificationConfigs",
        json={"id": "cfg-a", "url": "https://a.com"},
    )
    await push_client.post(
        f"/v1/tasks/{task_id}/pushNotificationConfigs",
        json={"id": "cfg-b", "url": "https://b.com"},
    )

    resp = await push_client.get(f"/v1/tasks/{task_id}/pushNotificationConfigs")
    assert resp.status_code == 200
    configs = resp.json()
    assert len(configs) == 2


async def test_delete_config_removes(push_client):
    resp = await push_client.post("/v1/message:send", json=_send_body())
    task_id = resp.json()["id"]

    await push_client.post(
        f"/v1/tasks/{task_id}/pushNotificationConfigs",
        json={"id": "cfg-1", "url": "https://example.com"},
    )

    resp = await push_client.delete(f"/v1/tasks/{task_id}/pushNotificationConfigs/cfg-1")
    assert resp.status_code == 200

    # Verify it's gone
    resp = await push_client.get(f"/v1/tasks/{task_id}/pushNotificationConfigs")
    assert resp.json() == []


async def test_set_config_task_not_found(push_client):
    resp = await push_client.post(
        "/v1/tasks/nonexistent/pushNotificationConfigs",
        json={"url": "https://example.com/webhook"},
    )
    assert resp.status_code == 404


async def test_get_config_not_found(push_client):
    resp = await push_client.post("/v1/message:send", json=_send_body())
    task_id = resp.json()["id"]

    resp = await push_client.get(f"/v1/tasks/{task_id}/pushNotificationConfigs/nonexistent")
    assert resp.status_code == 404


async def test_delete_config_not_found(push_client):
    resp = await push_client.post("/v1/message:send", json=_send_body())
    task_id = resp.json()["id"]

    resp = await push_client.delete(f"/v1/tasks/{task_id}/pushNotificationConfigs/nonexistent")
    assert resp.status_code == 404


async def test_set_config_rejects_loopback_url(push_client):
    """SSRF-blocked URLs are rejected with 400 at registration instead of
    being accepted and silently dropped at delivery time."""
    resp = await push_client.post("/v1/message:send", json=_send_body())
    task_id = resp.json()["id"]

    resp = await push_client.post(
        f"/v1/tasks/{task_id}/pushNotificationConfigs",
        json={"url": "https://127.0.0.1/webhook"},
    )
    assert resp.status_code == 400

    # Nothing was stored.
    resp = await push_client.get(f"/v1/tasks/{task_id}/pushNotificationConfigs")
    assert resp.json() == []


async def test_set_config_rejects_host_resolving_to_private_ip(push_client, monkeypatch):
    async def _private(hostname):
        return [(2, 1, 6, "", ("169.254.169.254", 0))]

    monkeypatch.setattr("a2akit.push.validation._getaddrinfo", _private)

    resp = await push_client.post("/v1/message:send", json=_send_body())
    task_id = resp.json()["id"]

    resp = await push_client.post(
        f"/v1/tasks/{task_id}/pushNotificationConfigs",
        json={"url": "https://internal.example.com/webhook"},
    )
    assert resp.status_code == 400


async def test_set_config_rejects_bad_scheme(push_client):
    resp = await push_client.post("/v1/message:send", json=_send_body())
    task_id = resp.json()["id"]

    resp = await push_client.post(
        f"/v1/tasks/{task_id}/pushNotificationConfigs",
        json={"url": "ftp://example.com/webhook"},
    )
    assert resp.status_code == 400


async def test_agent_card_shows_push_enabled(push_client):
    resp = await push_client.get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    card = resp.json()
    assert card["capabilities"]["pushNotifications"] is True


async def test_agent_card_shows_push_disabled(no_push_client):
    resp = await no_push_client.get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    card = resp.json()
    assert card["capabilities"]["pushNotifications"] is False
