"""Tests for the JSON-RPC 2.0 protocol binding."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from asgi_lifespan import LifespanManager
from pydantic import ValidationError

from a2akit import A2AServer, AgentCardConfig, CapabilitiesConfig
from conftest import (
    DirectReplyWorker,
    EchoWorker,
    InputRequiredWorker,
)


def _make_jsonrpc_app(worker, *, streaming=False, **server_kwargs):
    """Create a FastAPI app with JSON-RPC protocol (default)."""
    server = A2AServer(
        worker=worker,
        agent_card=AgentCardConfig(
            name="Test Agent",
            description="Test agent for unit tests",
            version="0.0.1",
            capabilities=CapabilitiesConfig(streaming=streaming),
            # protocol defaults to "jsonrpc"
        ),
        **server_kwargs,
    )
    return server.as_fastapi_app()


@pytest.fixture
async def jrpc_app():
    raw_app = _make_jsonrpc_app(EchoWorker())
    async with LifespanManager(raw_app) as manager:
        yield manager.app


@pytest.fixture
async def jrpc_client(jrpc_app):
    transport = httpx.ASGITransport(app=jrpc_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def jrpc_streaming_app():
    raw_app = _make_jsonrpc_app(EchoWorker(), streaming=True)
    async with LifespanManager(raw_app) as manager:
        yield manager.app


@pytest.fixture
async def jrpc_streaming_client(jrpc_streaming_app):
    transport = httpx.ASGITransport(app=jrpc_streaming_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def jrpc_input_app():
    raw_app = _make_jsonrpc_app(InputRequiredWorker())
    async with LifespanManager(raw_app) as manager:
        yield manager.app


@pytest.fixture
async def jrpc_input_client(jrpc_input_app):
    transport = httpx.ASGITransport(app=jrpc_input_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def jrpc_direct_app():
    raw_app = _make_jsonrpc_app(DirectReplyWorker())
    async with LifespanManager(raw_app) as manager:
        yield manager.app


@pytest.fixture
async def jrpc_direct_client(jrpc_direct_app):
    transport = httpx.ASGITransport(app=jrpc_direct_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _send_params(text="hello", message_id=None, task_id=None, context_id=None, blocking=True):
    """Build MessageSendParams dict."""
    msg = {
        "role": "user",
        "messageId": message_id or str(uuid.uuid4()),
        "parts": [{"kind": "text", "text": text}],
    }
    if task_id:
        msg["taskId"] = task_id
    if context_id:
        msg["contextId"] = context_id
    body = {"message": msg}
    if blocking:
        body["configuration"] = {"blocking": True}
    return body


def _rpc(method, params=None, req_id=1):
    """Build a JSON-RPC request body."""
    body = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        body["params"] = params
    return body


class TestEnvelopeValidation:
    async def test_invalid_json(self, jrpc_client):
        resp = await jrpc_client.post(
            "/", content=b"{bad json", headers={"Content-Type": "application/json"}
        )
        data = resp.json()
        assert data["error"]["code"] == -32700

    async def test_missing_jsonrpc_field(self, jrpc_client):
        resp = await jrpc_client.post("/", json={"id": 1, "method": "tasks/get"})
        data = resp.json()
        assert data["error"]["code"] == -32600

    async def test_wrong_jsonrpc_version(self, jrpc_client):
        resp = await jrpc_client.post("/", json={"jsonrpc": "1.0", "id": 1, "method": "tasks/get"})
        data = resp.json()
        assert data["error"]["code"] == -32600

    async def test_unknown_method(self, jrpc_client):
        resp = await jrpc_client.post("/", json=_rpc("unknown/method"))
        data = resp.json()
        assert data["error"]["code"] == -32601
        assert "unknown/method" in data["error"]["data"]["method"]

    async def test_method_not_string(self, jrpc_client):
        resp = await jrpc_client.post("/", json={"jsonrpc": "2.0", "id": 1, "method": 123})
        data = resp.json()
        assert data["error"]["code"] == -32600


class TestMessageSend:
    async def test_echo_complete(self, jrpc_client):
        params = _send_params("hello world")
        resp = await jrpc_client.post("/", json=_rpc("message/send", params))
        data = resp.json()
        assert "result" in data
        result = data["result"]
        assert result["status"]["state"] == "completed"
        # check artifact text
        assert any("hello world" in str(a) for a in result.get("artifacts", []))

    async def test_response_has_id_and_context(self, jrpc_client):
        params = _send_params("hi")
        resp = await jrpc_client.post("/", json=_rpc("message/send", params))
        result = resp.json()["result"]
        assert "id" in result
        assert "contextId" in result

    async def test_empty_message_id(self, jrpc_client):
        params = _send_params("hello")
        params["message"]["messageId"] = ""
        resp = await jrpc_client.post("/", json=_rpc("message/send", params))
        data = resp.json()
        assert data["error"]["code"] == -32602

    async def test_invalid_params(self, jrpc_client):
        resp = await jrpc_client.post("/", json=_rpc("message/send", {"garbage": True}))
        data = resp.json()
        assert data["error"]["code"] == -32602

    async def test_direct_reply(self, jrpc_direct_client):
        params = _send_params("hello")
        resp = await jrpc_direct_client.post("/", json=_rpc("message/send", params))
        result = resp.json()["result"]
        # DirectReply returns a Message (has role), not a Task (has kind: "task")
        assert "role" in result

    async def test_internal_metadata_stripped(self, jrpc_client):
        params = _send_params("hello")
        resp = await jrpc_client.post("/", json=_rpc("message/send", params))
        result = resp.json()["result"]
        metadata = result.get("metadata", {}) or {}
        assert not any(k.startswith("_") for k in metadata)


class TestTasksGet:
    async def test_get_existing(self, jrpc_client):
        # Create a task first
        params = _send_params("hello")
        send_resp = await jrpc_client.post("/", json=_rpc("message/send", params))
        task_id = send_resp.json()["result"]["id"]

        resp = await jrpc_client.post("/", json=_rpc("tasks/get", {"id": task_id}))
        data = resp.json()
        assert "result" in data
        assert data["result"]["id"] == task_id

    async def test_get_with_history_length_zero(self, jrpc_client):
        params = _send_params("hello")
        send_resp = await jrpc_client.post("/", json=_rpc("message/send", params))
        task_id = send_resp.json()["result"]["id"]

        resp = await jrpc_client.post(
            "/", json=_rpc("tasks/get", {"id": task_id, "historyLength": 0})
        )
        result = resp.json()["result"]
        history = result.get("history", [])
        assert history == [] or history is None

    async def test_get_nonexistent(self, jrpc_client):
        resp = await jrpc_client.post("/", json=_rpc("tasks/get", {"id": "nonexistent-id"}))
        data = resp.json()
        assert data["error"]["code"] == -32001

    async def test_missing_id(self, jrpc_client):
        resp = await jrpc_client.post("/", json=_rpc("tasks/get", {}))
        data = resp.json()
        assert data["error"]["code"] == -32602


class TestTasksCancel:
    async def test_cancel_nonexistent(self, jrpc_client):
        resp = await jrpc_client.post("/", json=_rpc("tasks/cancel", {"id": "nonexistent"}))
        data = resp.json()
        assert data["error"]["code"] == -32001

    async def test_cancel_completed_task(self, jrpc_client):
        params = _send_params("hello")
        send_resp = await jrpc_client.post("/", json=_rpc("message/send", params))
        task_id = send_resp.json()["result"]["id"]

        resp = await jrpc_client.post("/", json=_rpc("tasks/cancel", {"id": task_id}))
        data = resp.json()
        assert data["error"]["code"] == -32002

    async def test_cancel_missing_id(self, jrpc_client):
        resp = await jrpc_client.post("/", json=_rpc("tasks/cancel", {}))
        data = resp.json()
        assert data["error"]["code"] == -32602

    async def test_cancel_active_task(self, jrpc_input_client):
        # InputRequiredWorker puts task in input_required state
        params = _send_params("hello")
        send_resp = await jrpc_input_client.post("/", json=_rpc("message/send", params))
        task_id = send_resp.json()["result"]["id"]

        resp = await jrpc_input_client.post("/", json=_rpc("tasks/cancel", {"id": task_id}))
        data = resp.json()
        assert "result" in data
        # cancel_task returns current state; cancellation happens asynchronously
        assert "error" not in data


class TestTasksList:
    async def test_list_empty(self, jrpc_client):
        resp = await jrpc_client.post("/", json=_rpc("tasks/list", {}))
        data = resp.json()
        assert "result" in data
        assert data["result"]["tasks"] == []
        assert data["result"]["totalSize"] == 0

    async def test_list_returns_created_tasks(self, jrpc_client):
        # Create two tasks
        params1 = _send_params("first")
        params2 = _send_params("second")
        resp1 = await jrpc_client.post("/", json=_rpc("message/send", params1))
        resp2 = await jrpc_client.post("/", json=_rpc("message/send", params2))
        task_id_1 = resp1.json()["result"]["id"]
        task_id_2 = resp2.json()["result"]["id"]

        resp = await jrpc_client.post("/", json=_rpc("tasks/list", {}))
        data = resp.json()
        assert "result" in data
        result = data["result"]
        assert result["totalSize"] == 2
        returned_ids = {t["id"] for t in result["tasks"]}
        assert task_id_1 in returned_ids
        assert task_id_2 in returned_ids

    async def test_list_with_page_size(self, jrpc_client):
        # Create two tasks, request pageSize=1
        await jrpc_client.post("/", json=_rpc("message/send", _send_params("a")))
        await jrpc_client.post("/", json=_rpc("message/send", _send_params("b")))

        resp = await jrpc_client.post("/", json=_rpc("tasks/list", {"pageSize": 1}))
        data = resp.json()["result"]
        assert len(data["tasks"]) == 1
        assert data["pageSize"] == 1
        # Should have a next page token
        assert data["nextPageToken"] != ""

    async def test_list_no_params(self, jrpc_client):
        """tasks/list with no params at all should work (all optional)."""
        resp = await jrpc_client.post("/", json=_rpc("tasks/list"))
        data = resp.json()
        assert "result" in data


class TestMessageSendStream:
    async def test_stream_returns_sse(self, jrpc_streaming_client):
        params = _send_params("hello world")
        params.pop("configuration", None)  # non-blocking for stream
        resp = await jrpc_streaming_client.post("/", json=_rpc("message/sendStream", params))
        assert resp.status_code == 200
        # Parse SSE lines
        text = resp.text
        events = []
        for line in text.split("\n"):
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
        assert len(events) > 0
        # First event should be a task snapshot
        first = events[0]
        assert first["jsonrpc"] == "2.0"
        assert "result" in first

    async def test_stream_invalid_params(self, jrpc_streaming_client):
        resp = await jrpc_streaming_client.post(
            "/", json=_rpc("message/sendStream", {"garbage": True})
        )
        data = resp.json()
        assert data["error"]["code"] == -32602


class TestPushNotificationStubs:
    @pytest.mark.parametrize(
        "method",
        [
            "tasks/pushNotificationConfig/set",
            "tasks/pushNotificationConfig/get",
            "tasks/pushNotificationConfig/list",
            "tasks/pushNotificationConfig/delete",
        ],
    )
    async def test_push_not_supported(self, jrpc_client, method):
        resp = await jrpc_client.post("/", json=_rpc(method, {}))
        data = resp.json()
        assert data["error"]["code"] == -32003


class TestHealth:
    async def test_health(self, jrpc_client):
        resp = await jrpc_client.post("/", json=_rpc("health", {}))
        data = resp.json()
        assert "result" in data
        assert data["result"]["status"] == "ok"


class TestMultiTurn:
    async def test_input_required_then_complete(self, jrpc_input_client):
        # First message → input_required
        params = _send_params("hello")
        resp = await jrpc_input_client.post("/", json=_rpc("message/send", params))
        result = resp.json()["result"]
        assert result["status"]["state"] == "input-required"
        task_id = result["id"]
        context_id = result["contextId"]

        # Follow-up with taskId → completed
        params2 = _send_params("my name", task_id=task_id, context_id=context_id)
        resp2 = await jrpc_input_client.post("/", json=_rpc("message/send", params2))
        result2 = resp2.json()["result"]
        assert result2["status"]["state"] == "completed"


class TestAgentCardDiscovery:
    async def test_agent_card_url_points_to_root(self, jrpc_client):
        resp = await jrpc_client.get("/.well-known/agent-card.json")
        assert resp.status_code == 200
        card = resp.json()
        # jsonrpc protocol → url should be the root, not /v1
        assert not card["url"].endswith("/v1")


class TestVersionHeader:
    async def test_compatible_version(self, jrpc_client):
        resp = await jrpc_client.post(
            "/",
            json=_rpc("tasks/get", {"id": "x"}),
            headers={"A2A-Version": "0.3"},
        )
        # Should not be a 400 from version check (might be -32001 for not found)
        data = resp.json()
        assert data.get("error", {}).get("code") != -32009

    async def test_incompatible_version(self, jrpc_client):
        resp = await jrpc_client.post(
            "/",
            json=_rpc("tasks/get", {"id": "x"}),
            headers={"A2A-Version": "1.0"},
        )
        assert resp.status_code == 400


class TestProtocolConfig:
    def test_grpc_raises(self):
        with pytest.raises(ValueError, match="grpc"):
            A2AServer(
                worker=EchoWorker(),
                agent_card=AgentCardConfig(
                    name="X",
                    description="X",
                    version="0.1.0",
                    protocol="grpc",
                ),
            )

    def test_unknown_protocol_raises(self):
        with pytest.raises(ValidationError, match="protocol"):
            AgentCardConfig(
                name="X",
                description="X",
                version="0.1.0",
                protocol="websocket",
            )

    def test_default_is_jsonrpc(self):
        cfg = AgentCardConfig(name="X", description="X", version="0.1.0")
        assert cfg.protocol == "jsonrpc"

    async def test_http_json_mounts_rest_not_jsonrpc(self):
        """When protocol='http+json', POST / should 404/405 (no JSON-RPC route)."""
        from conftest import EchoWorker

        server = A2AServer(
            worker=EchoWorker(),
            agent_card=AgentCardConfig(
                name="X",
                description="X",
                version="0.1.0",
                protocol="http+json",
            ),
        )
        app = server.as_fastapi_app()
        async with LifespanManager(app) as manager:
            transport = httpx.ASGITransport(app=manager.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.post("/", json=_rpc("message/send"))
                assert resp.status_code in (404, 405)
