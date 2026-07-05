"""SSE streaming tests for the A2A v1.0 wire (REST + JSON-RPC).

Covers the previously untested v1.0 streaming surface:

- JSON-RPC ``SendStreamingMessage`` happy path (events until terminal).
- REST ``POST /message:stream`` happy path + stream close on terminal.
- REST ``POST /tasks/{id}:subscribe`` with ``Last-Event-ID`` reconnect.
- JSON-RPC ``SubscribeToTask`` with ``Last-Event-ID`` reconnect.
- JSON-RPC ``CancelTask`` + REST ``POST /tasks/{id}:cancel``.

Every streaming read is wrapped in ``asyncio.timeout`` so a regression
that stops the terminal event from closing the stream fails fast instead
of hanging the suite.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from asgi_lifespan import LifespanManager

from a2akit import A2AServer, AgentCardConfig, CapabilitiesConfig, Worker
from conftest import InputRequiredWorker, StreamingWorker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from a2akit.worker import TaskContext

STREAM_TIMEOUT = 10


class _Echo(Worker):
    async def handle(self, ctx: TaskContext) -> None:
        await ctx.complete(f"Echo: {ctx.user_text}")


def _make_app(worker: Worker, protocol: str) -> Any:
    server = A2AServer(
        worker=worker,
        agent_card=AgentCardConfig(
            name="Test",
            description="v1.0 streaming test server",
            version="1.0.0",
            protocol=protocol,  # type: ignore[arg-type]
            capabilities=CapabilitiesConfig(streaming=True),
        ),
        protocol_version="1.0",
    )
    return server.as_fastapi_app()


@pytest.fixture
async def rest_stream_client() -> AsyncIterator[httpx.AsyncClient]:
    app = _make_app(StreamingWorker(), "http+json")
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest.fixture
async def jsonrpc_stream_client() -> AsyncIterator[httpx.AsyncClient]:
    app = _make_app(StreamingWorker(), "jsonrpc")
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest.fixture
async def jsonrpc_input_client() -> AsyncIterator[httpx.AsyncClient]:
    app = _make_app(InputRequiredWorker(), "jsonrpc")
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def _message(text: str, message_id: str) -> dict[str, Any]:
    return {"role": "ROLE_USER", "parts": [{"text": text}], "messageId": message_id}


def _parse_sse(raw: str) -> list[dict[str, Any]]:
    """Parse raw SSE text into JSON payload dicts (one per ``data:`` line)."""
    events: list[dict[str, Any]] = []
    for line in raw.replace("\r\n", "\n").split("\n"):
        line = line.strip()
        if line.startswith("data:"):
            payload = line[len("data:") :].strip()
            if payload:
                events.append(json.loads(payload))
    return events


async def test_v10_rest_message_stream_events_until_terminal(
    rest_stream_client: httpx.AsyncClient,
) -> None:
    """REST message:stream yields snapshot, artifact updates, then the terminal
    status update — and closes the stream afterwards (no final flag)."""
    raw = ""
    async with asyncio.timeout(STREAM_TIMEOUT):
        async with rest_stream_client.stream(
            "POST",
            "/message:stream",
            json={"message": _message("hello streaming world", "v10-rest-s1")},
        ) as resp:
            assert resp.status_code == 200
            # Reading to exhaustion proves the server closes on terminal.
            async for chunk in resp.aiter_text():
                raw += chunk

    events = _parse_sse(raw)
    assert events, raw
    # First event: bare Task snapshot (v1.0 REST emits Task/Message bare).
    assert "status" in events[0], events[0]
    assert "id" in events[0], events[0]
    # Artifact updates use the wrapped-discriminator form.
    artifact_updates = [e for e in events if "taskArtifactUpdate" in e]
    assert artifact_updates, f"expected taskArtifactUpdate events, got {events}"
    # Terminal status update closes the stream.
    status_updates = [e["taskStatusUpdate"] for e in events if "taskStatusUpdate" in e]
    assert status_updates, events
    assert status_updates[-1]["status"]["state"] == "TASK_STATE_COMPLETED"


async def test_v10_jsonrpc_send_streaming_message_happy_path(
    jsonrpc_stream_client: httpx.AsyncClient,
) -> None:
    """SendStreamingMessage streams JSON-RPC-enveloped events until terminal."""
    raw = ""
    async with asyncio.timeout(STREAM_TIMEOUT):
        async with jsonrpc_stream_client.stream(
            "POST",
            "/",
            json={
                "jsonrpc": "2.0",
                "id": 5,
                "method": "SendStreamingMessage",
                "params": {"message": _message("hello streaming world", "v10-jrpc-s1")},
            },
        ) as resp:
            assert resp.status_code == 200
            async for chunk in resp.aiter_text():
                raw += chunk

    events = _parse_sse(raw)
    assert events, raw
    # Every event is a JSON-RPC success envelope echoing the request id.
    assert all(e.get("jsonrpc") == "2.0" and e.get("id") == 5 for e in events), events
    results = [e["result"] for e in events]
    # First event: wrapped Task snapshot.
    assert "task" in results[0], results[0]
    assert any("taskArtifactUpdate" in r for r in results), results
    status_updates = [r["taskStatusUpdate"] for r in results if "taskStatusUpdate" in r]
    assert status_updates, results
    assert status_updates[-1]["status"]["state"] == "TASK_STATE_COMPLETED"


async def test_v10_rest_subscribe_with_last_event_id_replays_snapshot(
    rest_stream_client: httpx.AsyncClient,
) -> None:
    """Reconnecting to a terminal task with Last-Event-ID yields the final
    snapshot and closes instead of hanging (replay buffer may be gone)."""
    # Create a task and let it complete.
    r = await rest_stream_client.post(
        "/message:send",
        json={
            "message": _message("one two", "v10-rest-sub1"),
            "configuration": {"blocking": True},
        },
    )
    task_id = r.json()["task"]["id"]

    raw = ""
    async with asyncio.timeout(STREAM_TIMEOUT):
        async with rest_stream_client.stream(
            "POST",
            f"/tasks/{task_id}:subscribe",
            headers={"Last-Event-ID": "0"},
        ) as resp:
            assert resp.status_code == 200
            async for chunk in resp.aiter_text():
                raw += chunk

    events = _parse_sse(raw)
    assert events, raw
    # Final snapshot of the terminal task, bare Task JSON.
    assert events[-1]["id"] == task_id
    assert events[-1]["status"]["state"] == "TASK_STATE_COMPLETED"


async def test_v10_jsonrpc_subscribe_terminal_task_rejected(
    jsonrpc_stream_client: httpx.AsyncClient,
) -> None:
    """SubscribeToTask on a terminal task without Last-Event-ID errors out."""
    r = await jsonrpc_stream_client.post(
        "/",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "SendMessage",
            "params": {
                "message": _message("one two", "v10-jrpc-sub1"),
                "configuration": {"blocking": True},
            },
        },
    )
    task_id = r.json()["result"]["task"]["id"]

    r = await jsonrpc_stream_client.post(
        "/",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "SubscribeToTask",
            "params": {"id": task_id},
        },
    )
    body = r.json()
    assert body["error"]["code"] == -32004
    assert body["error"]["data"][0]["reason"] == "UNSUPPORTED_OPERATION"


async def test_v10_jsonrpc_subscribe_with_last_event_id_replays_to_terminal(
    jsonrpc_stream_client: httpx.AsyncClient,
) -> None:
    """SubscribeToTask with Last-Event-ID streams JSON-RPC-enveloped events
    for a terminal task and closes instead of hanging."""
    r = await jsonrpc_stream_client.post(
        "/",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "SendMessage",
            "params": {
                "message": _message("one two", "v10-jrpc-sub2"),
                "configuration": {"blocking": True},
            },
        },
    )
    task_id = r.json()["result"]["task"]["id"]

    raw = ""
    async with asyncio.timeout(STREAM_TIMEOUT):
        async with jsonrpc_stream_client.stream(
            "POST",
            "/",
            json={
                "jsonrpc": "2.0",
                "id": 7,
                "method": "SubscribeToTask",
                "params": {"id": task_id},
            },
            headers={"Last-Event-ID": "0"},
        ) as resp:
            assert resp.status_code == 200
            async for chunk in resp.aiter_text():
                raw += chunk

    events = _parse_sse(raw)
    assert events, raw
    assert all(e.get("jsonrpc") == "2.0" and e.get("id") == 7 for e in events), events
    results = [e["result"] for e in events]
    # Final event: wrapped snapshot of the terminal task.
    assert "task" in results[-1], results
    assert results[-1]["task"]["id"] == task_id
    assert results[-1]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"


@pytest.fixture
async def rest_input_client() -> AsyncIterator[httpx.AsyncClient]:
    app = _make_app(InputRequiredWorker(), "http+json")
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


async def test_v10_rest_cancel_task(rest_input_client: httpx.AsyncClient) -> None:
    """POST /tasks/{id}:cancel on a non-terminal task succeeds."""
    r = await rest_input_client.post(
        "/message:send",
        json={
            "message": _message("hi", "v10-rest-cancel-1"),
            "configuration": {"blocking": True},
        },
    )
    task = r.json()["task"]
    assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"

    r = await rest_input_client.post(f"/tasks/{task['id']}:cancel")
    assert r.status_code == 200
    assert r.json()["id"] == task["id"]


async def test_v10_rest_cancel_task_not_found(rest_input_client: httpx.AsyncClient) -> None:
    r = await rest_input_client.post("/tasks/nope:cancel")
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["status"] == "NOT_FOUND"
    assert err["details"][0]["reason"] == "TASK_NOT_FOUND"


async def test_v10_jsonrpc_cancel_task(jsonrpc_input_client: httpx.AsyncClient) -> None:
    """CancelTask on a non-terminal (input-required) task succeeds."""
    r = await jsonrpc_input_client.post(
        "/",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "SendMessage",
            "params": {
                "message": _message("hi", "v10-cancel-1"),
                "configuration": {"blocking": True},
            },
        },
    )
    task = r.json()["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"

    r = await jsonrpc_input_client.post(
        "/",
        json={"jsonrpc": "2.0", "id": 2, "method": "CancelTask", "params": {"id": task["id"]}},
    )
    body = r.json()
    assert "error" not in body, body
    assert body["result"]["id"] == task["id"]


async def test_v10_jsonrpc_cancel_task_not_found(
    jsonrpc_input_client: httpx.AsyncClient,
) -> None:
    r = await jsonrpc_input_client.post(
        "/",
        json={"jsonrpc": "2.0", "id": 3, "method": "CancelTask", "params": {"id": "nope"}},
    )
    body = r.json()
    assert body["error"]["code"] == -32001
    assert body["error"]["data"][0]["reason"] == "TASK_NOT_FOUND"
