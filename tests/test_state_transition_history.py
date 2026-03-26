"""Tests for stateTransitionHistory capability and transition recording."""

from __future__ import annotations

import httpx
import pytest
from a2a.types import Message, Part, Role, TaskState, TextPart
from asgi_lifespan import LifespanManager

from a2akit import AgentCardConfig, CapabilitiesConfig
from a2akit.agent_card import build_agent_card
from conftest import EchoWorker, StatusWorker, _make_app


def _msg(text: str = "hello", msg_id: str = "msg1") -> Message:
    return Message(
        role=Role.user,
        parts=[Part(root=TextPart(text=text))],
        message_id=msg_id,
    )


def _status_msg(text: str) -> Message:
    return Message(
        role=Role.agent,
        parts=[Part(root=TextPart(text=text))],
        message_id="status-msg",
    )


def test_capability_default_false():
    """Default: state_transition_history is False."""
    caps = CapabilitiesConfig()
    assert caps.state_transition_history is False


def test_capability_enabled():
    """state_transition_history=True is accepted without error."""
    caps = CapabilitiesConfig(state_transition_history=True)
    assert caps.state_transition_history is True


def test_capability_reflected_in_agent_card():
    """CapabilitiesConfig(state_transition_history=True) flows to AgentCard."""
    config = AgentCardConfig(
        name="Test",
        description="Test",
        protocol="http+json",
        capabilities=CapabilitiesConfig(state_transition_history=True),
    )
    card = build_agent_card(config, "http://localhost:8000")
    assert card.capabilities.state_transition_history is True


def test_capability_default_false_in_agent_card():
    """Default config has stateTransitionHistory=False in AgentCard."""
    config = AgentCardConfig(name="Test", description="Test", protocol="http+json")
    card = build_agent_card(config, "http://localhost:8000")
    assert card.capabilities.state_transition_history is False


async def test_transitions_recorded_in_memory(storage):
    """InMemoryStorage records state transitions in metadata."""
    task = await storage.create_task("ctx-1", _msg())
    await storage.update_task(task.id, state=TaskState.working)
    await storage.update_task(task.id, state=TaskState.completed)

    loaded = await storage.load_task(task.id)
    assert loaded is not None
    transitions = loaded.metadata["stateTransitions"]
    assert len(transitions) == 3
    assert [t["state"] for t in transitions] == ["submitted", "working", "completed"]
    for t in transitions:
        assert "state" in t
        assert "timestamp" in t


async def test_transition_with_status_message(storage):
    """Transition record includes messageText when status_message is present."""
    task = await storage.create_task("ctx-1", _msg())
    await storage.update_task(
        task.id,
        state=TaskState.failed,
        status_message=_status_msg("Error!"),
    )

    loaded = await storage.load_task(task.id)
    transitions = loaded.metadata["stateTransitions"]
    last = transitions[-1]
    assert last["state"] == "failed"
    assert last["messageText"] == "Error!"


async def test_transition_without_status_message(storage):
    """Transition record has no messageText when status_message is absent."""
    task = await storage.create_task("ctx-1", _msg())
    await storage.update_task(task.id, state=TaskState.working)

    loaded = await storage.load_task(task.id)
    transitions = loaded.metadata["stateTransitions"]
    last = transitions[-1]
    assert "messageText" not in last


async def test_transitions_survive_task_metadata_merge(storage):
    """External task_metadata merge does not overwrite stateTransitions."""
    task = await storage.create_task("ctx-1", _msg())
    await storage.update_task(task.id, state=TaskState.working)
    await storage.update_task(task.id, task_metadata={"custom": "value"})
    await storage.update_task(task.id, state=TaskState.completed)

    loaded = await storage.load_task(task.id)
    assert loaded.metadata["custom"] == "value"
    transitions = loaded.metadata["stateTransitions"]
    assert len(transitions) == 3
    assert [t["state"] for t in transitions] == ["submitted", "working", "completed"]


async def test_transitions_recorded_in_sql(sql_storage):
    """SQL storage records state transitions in metadata."""
    task = await sql_storage.create_task("ctx-1", _msg())
    await sql_storage.update_task(task.id, state=TaskState.working)
    await sql_storage.update_task(task.id, state=TaskState.completed)

    loaded = await sql_storage.load_task(task.id)
    assert loaded is not None
    transitions = loaded.metadata["stateTransitions"]
    assert len(transitions) == 3
    assert [t["state"] for t in transitions] == ["submitted", "working", "completed"]


async def test_sql_transition_with_status_message(sql_storage):
    """SQL storage transition record includes messageText."""
    task = await sql_storage.create_task("ctx-1", _msg())
    await sql_storage.update_task(
        task.id,
        state=TaskState.failed,
        status_message=_status_msg("Boom!"),
    )

    loaded = await sql_storage.load_task(task.id)
    transitions = loaded.metadata["stateTransitions"]
    assert transitions[-1]["messageText"] == "Boom!"


async def test_sql_transitions_survive_metadata_merge(sql_storage):
    """SQL storage: external metadata merge preserves stateTransitions."""
    task = await sql_storage.create_task("ctx-1", _msg())
    await sql_storage.update_task(task.id, state=TaskState.working)
    await sql_storage.update_task(task.id, task_metadata={"custom": "value"})
    await sql_storage.update_task(task.id, state=TaskState.completed)

    loaded = await sql_storage.load_task(task.id)
    assert loaded.metadata["custom"] == "value"
    transitions = loaded.metadata["stateTransitions"]
    assert len(transitions) == 3
    assert [t["state"] for t in transitions] == ["submitted", "working", "completed"]


@pytest.fixture
async def client_with_transitions():
    """HTTP client against EchoWorker app."""
    app = _make_app(EchoWorker())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def test_transitions_visible_via_http(client_with_transitions, make_send_params):
    """E2E: message/send response contains stateTransitions in metadata."""
    body = make_send_params(text="hello")
    resp = await client_with_transitions.post("/v1/message:send", json=body)
    assert resp.status_code == 200

    data = resp.json()
    assert data["status"]["state"] == "completed"
    transitions = data.get("metadata", {}).get("stateTransitions", [])
    assert len(transitions) >= 2  # at least submitted + completed
    states = [t["state"] for t in transitions]
    assert states[0] == "submitted"
    assert states[-1] == "completed"


async def test_transitions_in_tasks_get(client_with_transitions, make_send_params):
    """E2E: tasks/get returns stateTransitions in metadata."""
    body = make_send_params(text="world")
    resp = await client_with_transitions.post("/v1/message:send", json=body)
    task_id = resp.json()["id"]

    resp2 = await client_with_transitions.get(f"/v1/tasks/{task_id}")
    assert resp2.status_code == 200
    data = resp2.json()
    transitions = data.get("metadata", {}).get("stateTransitions", [])
    assert len(transitions) >= 2
    assert transitions[0]["state"] == "submitted"
    assert transitions[-1]["state"] == "completed"


@pytest.fixture
async def status_client():
    """HTTP client against StatusWorker (sends intermediate statuses)."""
    app = _make_app(StatusWorker())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def test_transitions_include_working_states(status_client, make_send_params):
    """StatusWorker sends status updates -> working transitions are recorded."""
    body = make_send_params(text="go")
    resp = await status_client.post("/v1/message:send", json=body)
    assert resp.status_code == 200

    data = resp.json()
    transitions = data.get("metadata", {}).get("stateTransitions", [])
    states = [t["state"] for t in transitions]
    assert "submitted" in states
    assert "working" in states
    assert "completed" in states
