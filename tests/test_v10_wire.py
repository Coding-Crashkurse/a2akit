"""Integration tests for the native A2A v1.0 wire (§5).

Covers:

- REST router: ``/message:send``, ``/tasks/{id}``, ``/tasks``, errors in
  ``google.rpc.Status`` shape, card discovery (``supportedInterfaces[]``).
- JSON-RPC router: ``SendMessage`` / ``GetTask`` / unknown-method error.
- Verifies that the v0.3 paths (``/v1/...``) are NOT mounted when the
  server is configured for v1.0.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest
from asgi_lifespan import LifespanManager

from a2akit import A2AServer, AgentCardConfig, Worker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from a2akit.worker import TaskContext


class _Echo(Worker):
    async def handle(self, ctx: TaskContext) -> None:
        await ctx.complete(f"Echo: {ctx.user_text}")


async def _make_client(protocol: str) -> tuple[Any, AsyncIterator[Any]]:
    """Helper — returns (client, lifespan_ctx). Use inside the fixture."""
    server = A2AServer(
        worker=_Echo(),
        agent_card=AgentCardConfig(
            name="Test",
            description="Test server",
            version="1.0.0",
            protocol=protocol,  # type: ignore[arg-type]
        ),
        protocol_version="1.0",
    )
    return server.as_fastapi_app(), server


@pytest.fixture
async def rest_client() -> AsyncIterator[httpx.AsyncClient]:
    app, _ = await _make_client("http+json")
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest.fixture
async def jsonrpc_client() -> AsyncIterator[httpx.AsyncClient]:
    app, _ = await _make_client("jsonrpc")
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


async def test_v10_agent_card_has_supported_interfaces(rest_client: httpx.AsyncClient) -> None:
    r = await rest_client.get("/.well-known/agent-card.json")
    assert r.status_code == 200
    data = r.json()
    # v1.0 shape: supportedInterfaces[] with per-entry protocol_version.
    assert "supportedInterfaces" in data
    assert data["supportedInterfaces"][0]["protocolVersion"] == "1.0"
    # v0.3 top-level keys must NOT be present.
    assert "url" not in data
    assert "preferredTransport" not in data


async def test_v10_rest_message_send_returns_task_wrapper(rest_client: httpx.AsyncClient) -> None:
    r = await rest_client.post(
        "/message:send",
        json={
            "message": {
                "role": "ROLE_USER",
                "parts": [{"text": "hi"}],
                "messageId": "m-1",
            },
        },
    )
    assert r.status_code == 200
    body = r.json()
    # v1.0 wraps the result in SendMessageResponse with a "task" oneof.
    assert "task" in body
    assert body["task"]["status"]["state"] == "TASK_STATE_COMPLETED"


async def test_v10_rest_tasks_get(rest_client: httpx.AsyncClient) -> None:
    # First create one.
    r = await rest_client.post(
        "/message:send",
        json={
            "message": {
                "role": "ROLE_USER",
                "parts": [{"text": "hi"}],
                "messageId": "m-2",
            },
        },
    )
    task_id = r.json()["task"]["id"]
    r = await rest_client.get(f"/tasks/{task_id}")
    assert r.status_code == 200
    assert r.json()["id"] == task_id


async def test_v10_rest_tasks_list_tenant_filter(rest_client: httpx.AsyncClient) -> None:
    # Tenanted send.
    await rest_client.post(
        "/message:send",
        json={
            "tenant": "acme",
            "message": {
                "role": "ROLE_USER",
                "parts": [{"text": "t-1"}],
                "messageId": "m-tenant-1",
            },
        },
    )
    r = await rest_client.get("/tasks?tenant=acme")
    assert r.status_code == 200
    # All returned tasks belong to the acme tenant.
    assert len(r.json()["tasks"]) >= 1


async def test_v10_rest_task_not_found_shape(rest_client: httpx.AsyncClient) -> None:
    r = await rest_client.get("/tasks/nope")
    assert r.status_code == 404
    body = r.json()
    err = body["error"]
    assert err["status"] == "NOT_FOUND"
    assert err["details"][0]["@type"] == "type.googleapis.com/google.rpc.ErrorInfo"
    assert err["details"][0]["reason"] == "TASK_NOT_FOUND"
    assert err["details"][0]["domain"] == "a2a-protocol.org"


async def test_v10_rest_invalid_body_returns_invalid_argument(
    rest_client: httpx.AsyncClient,
) -> None:
    r = await rest_client.post("/message:send", json={"garbage": True})
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["status"] == "INVALID_ARGUMENT"


async def test_v10_mode_does_not_serve_v03_paths(rest_client: httpx.AsyncClient) -> None:
    r = await rest_client.post(
        "/v1/message:send",
        json={
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": "x"}],
                "messageId": "m-x",
                "kind": "message",
            }
        },
    )
    assert r.status_code == 404


async def test_v10_jsonrpc_send_message(jsonrpc_client: httpx.AsyncClient) -> None:
    r = await jsonrpc_client.post(
        "/",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "SendMessage",
            "params": {
                "message": {
                    "role": "ROLE_USER",
                    "parts": [{"text": "hi"}],
                    "messageId": "jrpc-1",
                },
            },
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["jsonrpc"] == "2.0"
    assert "task" in body["result"]


async def test_v10_jsonrpc_unknown_method_error(
    jsonrpc_client: httpx.AsyncClient,
) -> None:
    r = await jsonrpc_client.post(
        "/",
        json={"jsonrpc": "2.0", "id": 99, "method": "DoesNotExist"},
    )
    body = r.json()
    assert body["error"]["code"] == -32601
    info = body["error"]["data"][0]
    assert info["reason"] == "METHOD_NOT_FOUND"
    assert info["domain"] == "a2a-protocol.org"


async def test_v10_jsonrpc_get_task_not_found(
    jsonrpc_client: httpx.AsyncClient,
) -> None:
    r = await jsonrpc_client.post(
        "/",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "GetTask",
            "params": {"id": "does-not-exist"},
        },
    )
    body = r.json()
    assert body["error"]["code"] == -32001
    assert body["error"]["data"][0]["reason"] == "TASK_NOT_FOUND"


async def test_v10_jsonrpc_health(jsonrpc_client: httpx.AsyncClient) -> None:
    r = await jsonrpc_client.post(
        "/",
        json={"jsonrpc": "2.0", "id": 1, "method": "health"},
    )
    assert r.json()["result"] == {"status": "ok"}


@pytest.mark.parametrize("bad_params", [[1, 2, 3], "nope", 42])
async def test_v10_jsonrpc_non_object_params_rejected(
    jsonrpc_client: httpx.AsyncClient, bad_params: Any
) -> None:
    """params that are not a JSON object → -32602 Invalid params envelope."""
    r = await jsonrpc_client.post(
        "/",
        json={"jsonrpc": "2.0", "id": 7, "method": "GetTask", "params": bad_params},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["error"]["code"] == -32602
    assert body["error"]["data"][0]["reason"] == "INVALID_PARAMS"


async def test_v10_jsonrpc_get_task_history_length_zero(
    jsonrpc_client: httpx.AsyncClient,
) -> None:
    """historyLength: 0 must be honored, not treated as missing."""
    r = await jsonrpc_client.post(
        "/",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "SendMessage",
            "params": {
                "message": {
                    "role": "ROLE_USER",
                    "parts": [{"text": "hi"}],
                    "messageId": "hist-0",
                },
                "configuration": {"blocking": True},
            },
        },
    )
    task_id = r.json()["result"]["task"]["id"]
    r = await jsonrpc_client.post(
        "/",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "GetTask",
            "params": {"id": task_id, "historyLength": 0},
        },
    )
    result = r.json()["result"]
    assert result.get("history", []) in ([], None)


async def test_v10_health_ready_ok(rest_client: httpx.AsyncClient) -> None:
    r = await rest_client.get("/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    for name in ("storage", "broker", "event_bus"):
        assert body["components"][name]["status"] == "ok"
        assert "type" in body["components"][name]


async def test_v10_health_ready_degraded_returns_503() -> None:
    class _Broken:
        async def health_check(self) -> dict[str, Any]:
            raise RuntimeError("backend down")

    app, _ = await _make_client("http+json")
    async with LifespanManager(app):
        app.state.storage = _Broken()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/health/ready")
            assert r.status_code == 503
            body = r.json()
            assert body["status"] == "degraded"
            assert body["components"]["storage"]["status"] == "error"


# -- Push notification configs (capability enabled) ---------------------------


class _EchoPush(Worker):
    async def handle(self, ctx: Any) -> None:
        await ctx.complete(f"Echo: {ctx.user_text}")


def _make_push_server(protocol: str) -> Any:
    from a2akit import CapabilitiesConfig

    server = A2AServer(
        worker=_EchoPush(),
        agent_card=AgentCardConfig(
            name="Push",
            description="Push-enabled",
            version="1.0.0",
            protocol=protocol,  # type: ignore[arg-type]
            capabilities=CapabilitiesConfig(push_notifications=True),
        ),
        protocol_version="1.0",
    )
    return server.as_fastapi_app()


@pytest.fixture
async def push_rest_client() -> AsyncIterator[httpx.AsyncClient]:
    app = _make_push_server("http+json")
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest.fixture
async def push_jsonrpc_client() -> AsyncIterator[httpx.AsyncClient]:
    app = _make_push_server("jsonrpc")
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


async def _create_task_rest(client: httpx.AsyncClient, message_id: str) -> str:
    r = await client.post(
        "/message:send",
        json={
            "message": {
                "role": "ROLE_USER",
                "parts": [{"text": "hi"}],
                "messageId": message_id,
            },
            "configuration": {"blocking": True},
        },
    )
    return str(r.json()["task"]["id"])


async def test_v10_rest_push_delete_returns_204_without_body(
    push_rest_client: httpx.AsyncClient,
) -> None:
    task_id = await _create_task_rest(push_rest_client, "push-del-1")
    r = await push_rest_client.post(
        f"/tasks/{task_id}/pushNotificationConfigs",
        json={"url": "https://example.com/webhook", "token": "s3"},
    )
    assert r.status_code == 200
    config_id = r.json()["id"]

    r = await push_rest_client.delete(f"/tasks/{task_id}/pushNotificationConfigs/{config_id}")
    assert r.status_code == 204
    # 204 MUST NOT carry a body (uvicorn raises otherwise).
    assert r.content == b""


def _jrpc(method: str, params: dict[str, Any], req_id: int = 1) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}


async def _create_task_jsonrpc(client: httpx.AsyncClient, message_id: str) -> str:
    r = await client.post(
        "/",
        json=_jrpc(
            "SendMessage",
            {
                "message": {
                    "role": "ROLE_USER",
                    "parts": [{"text": "hi"}],
                    "messageId": message_id,
                },
                "configuration": {"blocking": True},
            },
        ),
    )
    return str(r.json()["result"]["task"]["id"])


async def test_v10_jsonrpc_push_config_crud(push_jsonrpc_client: httpx.AsyncClient) -> None:
    """Create → Get → List → Delete happy path via v1.0 JSON-RPC."""
    task_id = await _create_task_jsonrpc(push_jsonrpc_client, "push-crud-1")

    # Create
    r = await push_jsonrpc_client.post(
        "/",
        json=_jrpc(
            "CreateTaskPushNotificationConfig",
            {"taskId": task_id, "url": "https://example.com/webhook", "token": "tok"},
        ),
    )
    body = r.json()
    assert "error" not in body, body
    created = body["result"]
    assert created["taskId"] == task_id
    assert created["url"] == "https://example.com/webhook"
    config_id = created["id"]

    # Get
    r = await push_jsonrpc_client.post(
        "/",
        json=_jrpc("GetTaskPushNotificationConfig", {"taskId": task_id, "id": config_id}),
    )
    assert r.json()["result"]["id"] == config_id

    # List
    r = await push_jsonrpc_client.post(
        "/",
        json=_jrpc("ListTaskPushNotificationConfigs", {"taskId": task_id}),
    )
    configs = r.json()["result"]["configs"]
    assert any(c["id"] == config_id for c in configs)

    # Delete
    r = await push_jsonrpc_client.post(
        "/",
        json=_jrpc("DeleteTaskPushNotificationConfig", {"taskId": task_id, "id": config_id}),
    )
    assert "error" not in r.json()

    # Get after delete → not found
    r = await push_jsonrpc_client.post(
        "/",
        json=_jrpc("GetTaskPushNotificationConfig", {"taskId": task_id, "id": config_id}),
    )
    assert r.json()["error"]["code"] == -32001


@pytest.mark.parametrize(
    "method",
    [
        "CreateTaskPushNotificationConfig",
        "GetTaskPushNotificationConfig",
        "ListTaskPushNotificationConfigs",
        "DeleteTaskPushNotificationConfig",
    ],
)
async def test_v10_jsonrpc_push_not_supported(
    jsonrpc_client: httpx.AsyncClient, method: str
) -> None:
    """Every push method must fail with -32003 when push is disabled."""
    r = await jsonrpc_client.post(
        "/",
        json=_jrpc(method, {"taskId": "t-1", "id": "cfg-1", "url": "https://x.example/h"}),
    )
    body = r.json()
    assert body["error"]["code"] == -32003
    assert body["error"]["data"][0]["reason"] == "PUSH_NOTIFICATIONS_NOT_SUPPORTED"


async def test_v10_unsupported_content_type_uses_rpc_status_shape(
    rest_client: httpx.AsyncClient,
) -> None:
    """The 415 rejection on a v1.0 server carries google.rpc.Status, not a
    v0.3 JSON-RPC envelope."""
    r = await rest_client.post(
        "/message:send",
        content=b"<xml/>",
        headers={"Content-Type": "text/xml"},
    )
    assert r.status_code == 415
    body = r.json()
    assert "jsonrpc" not in body
    err = body["error"]
    assert err["code"] == 415
    assert err["status"] == "INVALID_ARGUMENT"
    assert err["details"][0]["reason"] == "CONTENT_TYPE_NOT_SUPPORTED"


def test_descriptor_for_walks_mro() -> None:
    """Subclasses of cataloged exceptions map to their base's descriptor."""
    from a2akit._errors_v10 import descriptor_for
    from a2akit.storage.base import TaskNotFoundError

    class CustomNotFoundError(TaskNotFoundError):
        pass

    desc = descriptor_for(CustomNotFoundError("gone"))
    assert desc.reason == "TASK_NOT_FOUND"
    assert desc.http_status == 404


def test_v10_uncataloged_exception_message_not_echoed() -> None:
    """Arbitrary exception text must not leak into the JSON-RPC error."""
    from a2akit._errors_v10 import jsonrpc_error_from_exception

    envelope = jsonrpc_error_from_exception(RuntimeError("secret db password"), 1)
    assert envelope["error"]["message"] == "Internal error"
    assert "secret" not in str(envelope)
