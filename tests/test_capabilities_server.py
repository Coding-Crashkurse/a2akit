"""Server-side capability enforcement tests."""

from __future__ import annotations

import uuid

import httpx
from asgi_lifespan import LifespanManager

from a2akit import A2AServer, AgentCardConfig, CapabilitiesConfig
from conftest import EchoWorker, StreamingWorker


def _make_rest_app(worker, *, streaming=False):
    server = A2AServer(
        worker=worker,
        agent_card=AgentCardConfig(
            name="Test Agent",
            description="Test",
            version="0.0.1",
            protocol="http+json",
            capabilities=CapabilitiesConfig(streaming=streaming),
        ),
    )
    return server.as_fastapi_app()


def _make_jsonrpc_app(worker, *, streaming=False):
    server = A2AServer(
        worker=worker,
        agent_card=AgentCardConfig(
            name="Test Agent",
            description="Test",
            version="0.0.1",
            capabilities=CapabilitiesConfig(streaming=streaming),
        ),
    )
    return server.as_fastapi_app()


def _send_body(text="hello"):
    return {
        "message": {
            "role": "user",
            "messageId": str(uuid.uuid4()),
            "parts": [{"kind": "text", "text": text}],
        },
    }


def _rpc(method, params=None, req_id=1):
    body: dict = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        body["params"] = params
    return body


class TestRestStreamRejected:
    async def test_stream_endpoint_rejected_when_disabled(self):
        """POST /v1/message:stream returns error when streaming disabled."""
        app = _make_rest_app(EchoWorker(), streaming=False)
        async with LifespanManager(app) as manager:
            transport = httpx.ASGITransport(app=manager.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post("/v1/message:stream", json=_send_body())
                assert resp.status_code == 400
                data = resp.json()
                assert data["code"] == -32004
                assert "not supported" in data["message"].lower()

    async def test_subscribe_endpoint_rejected_when_disabled(self):
        """POST /v1/tasks/{id}:subscribe returns error when streaming disabled."""
        app = _make_rest_app(EchoWorker(), streaming=False)
        async with LifespanManager(app) as manager:
            transport = httpx.ASGITransport(app=manager.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post("/v1/tasks/some-id:subscribe")
                assert resp.status_code == 400
                data = resp.json()
                assert data["code"] == -32004

    async def test_stream_endpoint_works_when_enabled(self):
        """POST /v1/message:stream works when streaming enabled."""
        app = _make_rest_app(StreamingWorker(), streaming=True)
        async with LifespanManager(app) as manager:
            transport = httpx.ASGITransport(app=manager.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                raw = ""
                async with client.stream(
                    "POST", "/v1/message:stream", json=_send_body("hi")
                ) as resp:
                    assert resp.status_code == 200
                    async for chunk in resp.aiter_text():
                        raw += chunk
                assert len(raw) > 0


class TestJsonRpcStreamRejected:
    async def test_stream_rejected_when_disabled(self):
        """message/sendStream returns error when streaming disabled."""
        app = _make_jsonrpc_app(EchoWorker(), streaming=False)
        async with LifespanManager(app) as manager:
            transport = httpx.ASGITransport(app=manager.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post("/", json=_rpc("message/sendStream", _send_body()))
                data = resp.json()
                assert data["error"]["code"] == -32004
                assert "not supported" in data["error"]["message"].lower()

    async def test_resubscribe_rejected_when_disabled(self):
        """tasks/resubscribe returns error when streaming disabled."""
        app = _make_jsonrpc_app(EchoWorker(), streaming=False)
        async with LifespanManager(app) as manager:
            transport = httpx.ASGITransport(app=manager.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post("/", json=_rpc("tasks/resubscribe", {"id": "some-id"}))
                data = resp.json()
                assert data["error"]["code"] == -32004

    async def test_stream_works_when_enabled(self):
        """message/sendStream works when streaming enabled."""
        app = _make_jsonrpc_app(EchoWorker(), streaming=True)
        async with LifespanManager(app) as manager:
            transport = httpx.ASGITransport(app=manager.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                params = _send_body("hello")
                resp = await client.post("/", json=_rpc("message/sendStream", params))
                assert resp.status_code == 200


class TestAgentCardCapabilities:
    async def test_agent_card_streaming_false_by_default(self):
        """Default config has streaming: false in card."""
        app = _make_rest_app(EchoWorker())
        async with LifespanManager(app) as manager:
            transport = httpx.ASGITransport(app=manager.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/.well-known/agent-card.json")
                card = resp.json()
                assert card["capabilities"]["streaming"] is False

    async def test_agent_card_streaming_true(self):
        """CapabilitiesConfig(streaming=True) reflected in card."""
        app = _make_rest_app(EchoWorker(), streaming=True)
        async with LifespanManager(app) as manager:
            transport = httpx.ASGITransport(app=manager.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/.well-known/agent-card.json")
                card = resp.json()
                assert card["capabilities"]["streaming"] is True

    async def test_agent_card_reflects_capabilities(self):
        """Agent card capabilities match configured CapabilitiesConfig."""
        app = _make_rest_app(EchoWorker(), streaming=False)
        async with LifespanManager(app) as manager:
            transport = httpx.ASGITransport(app=manager.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/.well-known/agent-card.json")
                card = resp.json()
                caps = card["capabilities"]
                assert caps["streaming"] is False
                assert caps.get("pushNotifications") is None or caps["pushNotifications"] is False
