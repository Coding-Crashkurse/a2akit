"""Integration tests for ctx.accepts() — output mode negotiation via HTTP."""

from __future__ import annotations

import uuid

import httpx
from asgi_lifespan import LifespanManager

from a2akit import TaskContext, Worker
from conftest import _make_app


class OutputNegotiationWorker(Worker):
    """Worker that adapts output format based on client preferences."""

    async def handle(self, ctx: TaskContext) -> None:
        if ctx.accepts("application/json"):
            await ctx.complete_json({"result": "ok"})
        else:
            await ctx.complete("result: ok")


def _body(
    text: str = "hello",
    *,
    accepted_output_modes: list[str] | None = None,
    blocking: bool = True,
) -> dict:
    msg: dict = {
        "role": "user",
        "messageId": str(uuid.uuid4()),
        "parts": [{"kind": "text", "text": text}],
    }
    body: dict = {"message": msg}
    config: dict = {}
    if blocking:
        config["blocking"] = True
    if accepted_output_modes is not None:
        config["acceptedOutputModes"] = accepted_output_modes
    if config:
        body["configuration"] = config
    return body


async def test_accepts_json_via_http():
    """Client with acceptedOutputModes=["application/json"] gets JSON."""
    app = _make_app(OutputNegotiationWorker())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/v1/message:send",
                json=_body(accepted_output_modes=["application/json"]),
            )
            assert resp.status_code == 200
            data = resp.json()
            # Should have a DataPart artifact (JSON)
            artifacts = data.get("artifacts") or data.get("result", {}).get("artifacts", [])
            assert len(artifacts) >= 1
            first_part = artifacts[0]["parts"][0]
            assert "data" in first_part
            assert first_part["data"]["result"] == "ok"


async def test_accepts_text_via_http():
    """Client with acceptedOutputModes=["text/plain"] gets text."""
    app = _make_app(OutputNegotiationWorker())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/v1/message:send",
                json=_body(accepted_output_modes=["text/plain"]),
            )
            assert resp.status_code == 200
            data = resp.json()
            artifacts = data.get("artifacts") or data.get("result", {}).get("artifacts", [])
            assert len(artifacts) >= 1
            first_part = artifacts[0]["parts"][0]
            assert "text" in first_part
            assert first_part["text"] == "result: ok"


async def test_accepts_no_filter_via_http():
    """Client without acceptedOutputModes gets default (JSON)."""
    app = _make_app(OutputNegotiationWorker())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/v1/message:send",
                json=_body(),
            )
            assert resp.status_code == 200
            data = resp.json()
            artifacts = data.get("artifacts") or data.get("result", {}).get("artifacts", [])
            assert len(artifacts) >= 1
            first_part = artifacts[0]["parts"][0]
            # No filter → accepts() returns True → worker chooses JSON
            assert "data" in first_part
            assert first_part["data"]["result"] == "ok"
