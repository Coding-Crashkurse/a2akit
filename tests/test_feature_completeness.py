"""Tests for A2A v0.3.0 feature completeness (REQ-01 through REQ-10)."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
from asgi_lifespan import LifespanManager

from a2akit import (
    A2AServer,
    AgentCardConfig,
    CapabilitiesConfig,
    ContentTypeNotSupportedError,
    InvalidAgentResponseError,
    TaskContext,
    Worker,
)
from a2akit.client.result import ClientResult
from a2akit.event_bus.memory import InMemoryEventBus
from a2akit.storage.base import TERMINAL_STATES
from conftest import EchoWorker, StreamingWorker, _make_app


def _send_body(
    text: str = "hello",
    *,
    blocking: bool = True,
    task_id: str | None = None,
    context_id: str | None = None,
    reference_task_ids: list[str] | None = None,
    extensions: list[str] | None = None,
    parts: list[dict] | None = None,
) -> dict:
    """Build a message:send request body."""
    msg: dict[str, Any] = {
        "role": "user",
        "messageId": str(uuid.uuid4()),
        "parts": parts or [{"kind": "text", "text": text}],
    }
    if task_id:
        msg["taskId"] = task_id
    if context_id:
        msg["contextId"] = context_id
    if reference_task_ids is not None:
        msg["referenceTaskIds"] = reference_task_ids
    if extensions is not None:
        msg["extensions"] = extensions

    body: dict[str, Any] = {"message": msg}
    if blocking:
        body["configuration"] = {"blocking": True}
    return body


class ArtifactExtensionWorker(Worker):
    """Worker that emits an artifact with extensions."""

    async def handle(self, ctx: TaskContext) -> None:
        await ctx.emit_artifact(
            artifact_id="art-1",
            text="hello",
            extensions=["urn:example:v1"],
        )
        await ctx.complete()


class ContextPropertyWorker(Worker):
    """Worker that reports reference_task_ids and message_extensions in the response."""

    async def handle(self, ctx: TaskContext) -> None:
        ref_ids = ctx.reference_task_ids
        exts = ctx.message_extensions
        await ctx.complete(f"refs={ref_ids} exts={exts}")


class DataOnlyWorker(Worker):
    """Worker that echoes — used for input mode tests."""

    async def handle(self, ctx: TaskContext) -> None:
        await ctx.complete(f"got: {ctx.user_text}")


class InvalidResponseWorker(Worker):
    """Worker that triggers an InvalidAgentResponseError."""

    async def handle(self, ctx: TaskContext) -> None:
        raise InvalidAgentResponseError("bad output structure")


def _make_custom_app(worker: Worker, **kwargs: Any):
    """Create an app with custom settings."""
    card_kwargs = {}
    for k in (
        "input_modes",
        "output_modes",
        "name",
        "description",
        "version",
        "protocol",
        "capabilities",
        "skills",
    ):
        if k in kwargs:
            card_kwargs[k] = kwargs.pop(k)

    card_kwargs.setdefault("name", "Test Agent")
    card_kwargs.setdefault("description", "Test")
    card_kwargs.setdefault("version", "0.0.1")
    card_kwargs.setdefault("protocol", "http+json")
    card_kwargs.setdefault("capabilities", CapabilitiesConfig(streaming=False))

    server = A2AServer(
        worker=worker,
        agent_card=AgentCardConfig(**card_kwargs),
        **kwargs,
    )
    return server.as_fastapi_app()


async def test_reference_task_ids_passthrough():
    """referenceTaskIds on a message survives round-trip through tasks/get."""
    app = _make_app(EchoWorker())
    async with LifespanManager(app) as mgr:
        transport = httpx.ASGITransport(app=mgr.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            body = _send_body(reference_task_ids=["task-A", "task-B"])
            r = await c.post("/v1/message:send", json=body)
            assert r.status_code == 200
            task = r.json()
            task_id = task["id"]

            r2 = await c.get(f"/v1/tasks/{task_id}")
            assert r2.status_code == 200
            history = r2.json().get("history", [])
            user_msgs = [m for m in history if m.get("role") == "user"]
            assert len(user_msgs) >= 1
            assert user_msgs[0].get("referenceTaskIds") == ["task-A", "task-B"]


async def test_extensions_on_message_passthrough():
    """extensions on a message survives round-trip through tasks/get."""
    app = _make_app(EchoWorker())
    async with LifespanManager(app) as mgr:
        transport = httpx.ASGITransport(app=mgr.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            body = _send_body(extensions=["urn:example:logging"])
            r = await c.post("/v1/message:send", json=body)
            assert r.status_code == 200
            task_id = r.json()["id"]

            r2 = await c.get(f"/v1/tasks/{task_id}")
            history = r2.json().get("history", [])
            user_msgs = [m for m in history if m.get("role") == "user"]
            assert user_msgs[0].get("extensions") == ["urn:example:logging"]


async def test_ctx_reference_task_ids_property():
    """TaskContext exposes reference_task_ids from the triggering message."""
    app = _make_custom_app(ContextPropertyWorker())
    async with LifespanManager(app) as mgr:
        transport = httpx.ASGITransport(app=mgr.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            body = _send_body(
                reference_task_ids=["t1", "t2"],
                extensions=["urn:x"],
            )
            r = await c.post("/v1/message:send", json=body)
            assert r.status_code == 200
            data = r.json()
            # The worker puts refs and exts into the completed text
            artifacts = data.get("artifacts", [])
            assert any("t1" in str(a) and "t2" in str(a) for a in artifacts)
            assert any("urn:x" in str(a) for a in artifacts)


async def test_artifact_extensions_passthrough():
    """Artifact emitted with extensions has them in the response."""
    app = _make_custom_app(ArtifactExtensionWorker())
    async with LifespanManager(app) as mgr:
        transport = httpx.ASGITransport(app=mgr.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            body = _send_body()
            r = await c.post("/v1/message:send", json=body)
            assert r.status_code == 200
            task = r.json()
            artifacts = task.get("artifacts", [])
            assert len(artifacts) >= 1
            art = artifacts[0]
            assert art.get("extensions") == ["urn:example:v1"]


async def test_input_mode_rejects_incompatible_type():
    """Agent with defaultInputModes: [text/plain] rejects DataPart."""
    app = _make_custom_app(
        DataOnlyWorker(),
        input_modes=["text/plain"],
    )
    async with LifespanManager(app) as mgr:
        transport = httpx.ASGITransport(app=mgr.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            body = _send_body(
                parts=[{"kind": "data", "data": {"key": "value"}}],
            )
            r = await c.post("/v1/message:send", json=body)
            assert r.status_code == 400
            assert r.json()["code"] == -32005


async def test_input_mode_accepts_compatible_type():
    """Agent with defaultInputModes: [text/plain, application/json] accepts both."""
    app = _make_custom_app(
        DataOnlyWorker(),
        input_modes=["text/plain", "application/json"],
    )
    async with LifespanManager(app) as mgr:
        transport = httpx.ASGITransport(app=mgr.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/v1/message:send", json=_send_body())
            assert r.status_code == 200


async def test_input_mode_no_filter_accepts_all():
    """Agent without specific input modes accepts all part types."""
    app = _make_custom_app(
        DataOnlyWorker(),
        input_modes=[],
    )
    async with LifespanManager(app) as mgr:
        transport = httpx.ASGITransport(app=mgr.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            body = _send_body(parts=[{"kind": "data", "data": {"x": 1}}])
            r = await c.post("/v1/message:send", json=body)
            assert r.status_code == 200


async def test_input_mode_error_contains_rejected_type():
    """Error response contains the rejected MIME type in data."""
    app = _make_custom_app(
        DataOnlyWorker(),
        input_modes=["text/plain"],
    )
    async with LifespanManager(app) as mgr:
        transport = httpx.ASGITransport(app=mgr.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            body = _send_body(parts=[{"kind": "data", "data": {"x": 1}}])
            r = await c.post("/v1/message:send", json=body)
            assert r.status_code == 400
            data = r.json()
            assert data.get("data", {}).get("mimeType") == "application/json"


async def test_input_mode_file_part_uses_mime_type():
    """FilePart validation uses the file's mimeType."""
    app = _make_custom_app(
        DataOnlyWorker(),
        input_modes=["text/plain"],
    )
    async with LifespanManager(app) as mgr:
        transport = httpx.ASGITransport(app=mgr.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            import base64

            body = _send_body(
                parts=[
                    {
                        "kind": "file",
                        "file": {
                            "bytes": base64.b64encode(b"hello").decode(),
                            "mimeType": "image/png",
                        },
                    }
                ]
            )
            r = await c.post("/v1/message:send", json=body)
            assert r.status_code == 400
            assert r.json().get("data", {}).get("mimeType") == "image/png"


async def test_input_mode_file_part_defaults_to_octet_stream():
    """FilePart without mimeType defaults to application/octet-stream."""
    app = _make_custom_app(
        DataOnlyWorker(),
        input_modes=["text/plain"],
    )
    async with LifespanManager(app) as mgr:
        transport = httpx.ASGITransport(app=mgr.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            import base64

            body = _send_body(
                parts=[
                    {
                        "kind": "file",
                        "file": {
                            "bytes": base64.b64encode(b"hello").decode(),
                        },
                    }
                ]
            )
            r = await c.post("/v1/message:send", json=body)
            assert r.status_code == 400
            assert r.json().get("data", {}).get("mimeType") == "application/octet-stream"


async def test_input_mode_rejects_incompatible_type_jsonrpc():
    """JSON-RPC transport returns -32005 for incompatible input modes."""
    server = A2AServer(
        worker=DataOnlyWorker(),
        agent_card=AgentCardConfig(
            name="Test",
            description="Test",
            version="0.0.1",
            protocol="jsonrpc",
            input_modes=["text/plain"],
        ),
    )
    app = server.as_fastapi_app()
    async with LifespanManager(app) as mgr:
        transport = httpx.ASGITransport(app=mgr.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "message/send",
                    "params": {
                        "message": {
                            "role": "user",
                            "messageId": str(uuid.uuid4()),
                            "parts": [{"kind": "data", "data": {"x": 1}}],
                        },
                        "configuration": {"blocking": True},
                    },
                },
            )
            assert r.status_code == 200
            data = r.json()
            assert data["error"]["code"] == -32005


def test_invalid_agent_response_error_class():
    """Exception class exists with correct attributes."""
    err = InvalidAgentResponseError("bad structure")
    assert err.detail == "bad structure"
    assert str(err) == "bad structure"


def test_content_type_not_supported_error_class():
    """Exception class exists with correct attributes."""
    err = ContentTypeNotSupportedError("image/png")
    assert err.mime_type == "image/png"
    assert "image/png" in str(err)


def test_unknown_state_not_terminal():
    """TaskState.unknown is not in TERMINAL_STATES."""
    from a2a.types import TaskState

    assert TaskState.unknown not in TERMINAL_STATES


def test_client_result_unknown_state():
    """ClientResult.is_terminal returns False for 'unknown'."""
    from a2a.types import Task, TaskState, TaskStatus

    task = Task(
        id="t1",
        context_id="c1",
        status=TaskStatus(state=TaskState.unknown),
        kind="task",
    )
    result = ClientResult.from_task(task)
    assert result.state == "unknown"
    assert result.is_terminal is False


async def test_event_bus_replay_after_id():
    """subscribe with after_event_id replays buffered events after that ID."""
    from a2a.types import TaskState, TaskStatus, TaskStatusUpdateEvent

    bus = InMemoryEventBus(replay_buffer_size=100)
    async with bus:
        # Publish 5 events.
        for i in range(1, 6):
            ev = TaskStatusUpdateEvent(
                kind="status-update",
                task_id="t1",
                context_id="c1",
                status=TaskStatus(state=TaskState.working),
                final=(i == 5),
            )
            event_id = await bus.publish("t1", ev)
            assert event_id == str(i)

        # Subscribe with after_event_id="3" — should get events 4, 5.
        async with bus.subscribe("t1", after_event_id="3") as stream:
            events = []
            async for ev in stream:
                events.append(ev)
            assert len(events) == 2  # events 4 and 5


async def test_event_bus_replay_no_id_skips_replay():
    """subscribe without after_event_id gets only live events."""
    from a2a.types import TaskState, TaskStatus, TaskStatusUpdateEvent

    bus = InMemoryEventBus(replay_buffer_size=100)
    async with bus:
        # Publish some events (not subscribed yet).
        for _i in range(3):
            await bus.publish(
                "t1",
                TaskStatusUpdateEvent(
                    kind="status-update",
                    task_id="t1",
                    context_id="c1",
                    status=TaskStatus(state=TaskState.working),
                    final=False,
                ),
            )

        # Subscribe without after_event_id — should NOT get old events.
        # Publish a final event after subscribing.
        async with bus.subscribe("t1") as stream:
            await bus.publish(
                "t1",
                TaskStatusUpdateEvent(
                    kind="status-update",
                    task_id="t1",
                    context_id="c1",
                    status=TaskStatus(state=TaskState.completed),
                    final=True,
                ),
            )
            events = []
            async for ev in stream:
                events.append(ev)
            assert len(events) == 1  # only the live final event


async def test_event_bus_replay_old_id_replays_from_oldest():
    """subscribe with after_event_id older than buffer replays from oldest."""
    from a2a.types import TaskState, TaskStatus, TaskStatusUpdateEvent

    bus = InMemoryEventBus(replay_buffer_size=3)
    async with bus:
        # Publish 5 events into a size-3 buffer (keeps events 3, 4, 5).
        for i in range(1, 6):
            await bus.publish(
                "t1",
                TaskStatusUpdateEvent(
                    kind="status-update",
                    task_id="t1",
                    context_id="c1",
                    status=TaskStatus(state=TaskState.working),
                    final=(i == 5),
                ),
            )

        # Subscribe with after_event_id="1" — event 1 and 2 are gone.
        # Should get events 3, 4, 5 (all that remain in buffer).
        async with bus.subscribe("t1", after_event_id="1") as stream:
            events = []
            async for ev in stream:
                events.append(ev)
            assert len(events) == 3


async def test_event_bus_replay_buffer_cleanup():
    """cleanup removes buffer for a task."""
    from a2a.types import TaskState, TaskStatus, TaskStatusUpdateEvent

    bus = InMemoryEventBus(replay_buffer_size=100)
    async with bus:
        await bus.publish(
            "t1",
            TaskStatusUpdateEvent(
                kind="status-update",
                task_id="t1",
                context_id="c1",
                status=TaskStatus(state=TaskState.working),
                final=False,
            ),
        )
        assert "t1" in bus._replay_buffers
        await bus.cleanup("t1")
        assert "t1" not in bus._replay_buffers
        assert "t1" not in bus._event_counters


async def test_event_bus_publish_returns_event_id():
    """publish returns a monotonic string event ID."""
    from a2a.types import TaskState, TaskStatus, TaskStatusUpdateEvent

    bus = InMemoryEventBus()
    async with bus:
        ev = TaskStatusUpdateEvent(
            kind="status-update",
            task_id="t1",
            context_id="c1",
            status=TaskStatus(state=TaskState.working),
            final=False,
        )
        id1 = await bus.publish("t1", ev)
        id2 = await bus.publish("t1", ev)
        assert id1 == "1"
        assert id2 == "2"


async def test_jsonrpc_list_tasks_status_filter():
    """tasks/list via JSON-RPC filters by status."""
    server = A2AServer(
        worker=EchoWorker(),
        agent_card=AgentCardConfig(
            name="Test",
            description="Test",
            version="0.0.1",
            protocol="jsonrpc",
        ),
    )
    app = server.as_fastapi_app()
    async with LifespanManager(app) as mgr:
        transport = httpx.ASGITransport(app=mgr.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            # Create a task.
            await c.post(
                "/",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "message/send",
                    "params": {
                        "message": {
                            "role": "user",
                            "messageId": str(uuid.uuid4()),
                            "parts": [{"kind": "text", "text": "hi"}],
                        },
                        "configuration": {"blocking": True},
                    },
                },
            )
            # List with status filter.
            r = await c.post(
                "/",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tasks/list",
                    "params": {"status": "completed"},
                },
            )
            data = r.json()
            result = data["result"]
            assert len(result["tasks"]) >= 1
            for t in result["tasks"]:
                assert t["status"]["state"] == "completed"


async def test_stream_message_registers_push_config():
    """stream_message() with inline pushNotificationConfig stores the config."""
    from a2a.types import Message, MessageSendParams, Part, Role, TextPart

    from a2akit.broker import InMemoryBroker, InMemoryCancelRegistry
    from a2akit.event_bus.memory import InMemoryEventBus
    from a2akit.event_emitter import DefaultEventEmitter
    from a2akit.push.store import InMemoryPushConfigStore
    from a2akit.storage.memory import InMemoryStorage
    from a2akit.task_manager import TaskManager

    storage = InMemoryStorage()
    broker = InMemoryBroker()
    event_bus = InMemoryEventBus()
    cancel_registry = InMemoryCancelRegistry()
    push_store = InMemoryPushConfigStore()
    emitter = DefaultEventEmitter(event_bus, storage)

    async with storage, broker, event_bus:
        tm = TaskManager(
            broker=broker,
            storage=storage,
            event_bus=event_bus,
            cancel_registry=cancel_registry,
            emitter=emitter,
            push_store=push_store,
        )

        msg = Message(
            role=Role.user,
            message_id=str(uuid.uuid4()),
            parts=[Part(TextPart(text="hello"))],
        )
        params = MessageSendParams(
            message=msg,
            configuration={
                "blocking": False,
                "pushNotificationConfig": {
                    "url": "https://example.com/webhook",
                    "token": "test-token",
                },
            },
        )

        # stream_message yields the task snapshot first.
        agen = tm.stream_message(params)
        task = await anext(agen)
        task_id = task.id

        # Verify push config was stored by stream_message.
        configs = await push_store.list_configs(task_id)
        assert len(configs) >= 1
        assert configs[0].push_notification_config.url == "https://example.com/webhook"

        # Clean up the generator.
        await agen.aclose()


async def test_response_includes_a2a_version_header():
    """All A2A endpoint responses include the A2A-Version header."""
    app = _make_app(EchoWorker())
    async with LifespanManager(app) as mgr:
        transport = httpx.ASGITransport(app=mgr.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            # message:send
            r = await c.post("/v1/message:send", json=_send_body())
            assert r.headers.get("a2a-version") == "0.3.0"

            # health
            r2 = await c.get("/v1/health")
            assert r2.headers.get("a2a-version") == "0.3.0"

            # agent card
            r3 = await c.get("/.well-known/agent-card.json")
            assert r3.headers.get("a2a-version") == "0.3.0"


async def test_response_includes_a2a_version_header_jsonrpc():
    """JSON-RPC responses also include the A2A-Version header."""
    server = A2AServer(
        worker=EchoWorker(),
        agent_card=AgentCardConfig(
            name="Test",
            description="Test",
            version="0.0.1",
            protocol="jsonrpc",
        ),
    )
    app = server.as_fastapi_app()
    async with LifespanManager(app) as mgr:
        transport = httpx.ASGITransport(app=mgr.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "health",
                    "params": {},
                },
            )
            assert r.headers.get("a2a-version") == "0.3.0"


async def test_task_response_has_kind_task():
    """message:send response Task objects include kind: 'task'."""
    app = _make_app(EchoWorker())
    async with LifespanManager(app) as mgr:
        transport = httpx.ASGITransport(app=mgr.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/v1/message:send", json=_send_body())
            data = r.json()
            assert data.get("kind") == "task"


async def test_message_response_has_kind_message():
    """Direct-reply Message response includes kind: 'message'."""
    from conftest import DirectReplyWorker

    app = _make_app(DirectReplyWorker())
    async with LifespanManager(app) as mgr:
        transport = httpx.ASGITransport(app=mgr.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/v1/message:send", json=_send_body())
            data = r.json()
            assert data.get("kind") == "message"


async def test_sse_status_event_has_kind():
    """Streaming status update events include kind: 'status-update'."""
    app = _make_app(StreamingWorker(), streaming=True)
    async with LifespanManager(app) as mgr:
        transport = httpx.ASGITransport(app=mgr.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            body = _send_body("a b")
            body.pop("configuration", None)  # non-blocking for stream
            async with c.stream("POST", "/v1/message:stream", json=body) as resp:
                status_events = []
                artifact_events = []
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        data = json.loads(line[5:].strip())
                        kind = data.get("kind")
                        if kind == "status-update":
                            status_events.append(data)
                        elif kind == "artifact-update":
                            artifact_events.append(data)
                assert len(status_events) > 0
                for ev in status_events:
                    assert ev["kind"] == "status-update"


async def test_sse_artifact_event_has_kind():
    """Streaming artifact update events include kind: 'artifact-update'."""
    app = _make_app(StreamingWorker(), streaming=True)
    async with LifespanManager(app) as mgr:
        transport = httpx.ASGITransport(app=mgr.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            body = _send_body("a b")
            body.pop("configuration", None)
            async with c.stream("POST", "/v1/message:stream", json=body) as resp:
                artifact_events = []
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        data = json.loads(line[5:].strip())
                        if data.get("kind") == "artifact-update":
                            artifact_events.append(data)
                assert len(artifact_events) > 0
                for ev in artifact_events:
                    assert ev["kind"] == "artifact-update"


async def test_sse_events_have_id_field():
    """SSE events include the id: field for Last-Event-ID support."""
    app = _make_app(StreamingWorker(), streaming=True)
    async with LifespanManager(app) as mgr:
        transport = httpx.ASGITransport(app=mgr.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            body = _send_body("a b")
            body.pop("configuration", None)
            async with c.stream("POST", "/v1/message:stream", json=body) as resp:
                raw_text = ""
                async for chunk in resp.aiter_text():
                    raw_text += chunk
                # SSE format: "id: N\ndata: ...\n\n"
                assert "id:" in raw_text or "id: " in raw_text


async def test_tasks_get_has_kind_task():
    """tasks/get response includes kind: 'task'."""
    app = _make_app(EchoWorker())
    async with LifespanManager(app) as mgr:
        transport = httpx.ASGITransport(app=mgr.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post("/v1/message:send", json=_send_body())
            task_id = r.json()["id"]
            r2 = await c.get(f"/v1/tasks/{task_id}")
            assert r2.json().get("kind") == "task"


async def test_jsonrpc_task_has_kind():
    """JSON-RPC message/send result includes kind: 'task'."""
    server = A2AServer(
        worker=EchoWorker(),
        agent_card=AgentCardConfig(
            name="Test",
            description="Test",
            version="0.0.1",
            protocol="jsonrpc",
        ),
    )
    app = server.as_fastapi_app()
    async with LifespanManager(app) as mgr:
        transport = httpx.ASGITransport(app=mgr.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.post(
                "/",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "message/send",
                    "params": {
                        "message": {
                            "role": "user",
                            "messageId": str(uuid.uuid4()),
                            "parts": [{"kind": "text", "text": "hi"}],
                        },
                        "configuration": {"blocking": True},
                    },
                },
            )
            result = r.json()["result"]
            assert result.get("kind") == "task"
