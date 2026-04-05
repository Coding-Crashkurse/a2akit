"""End-to-end idempotency test via HTTP."""

from __future__ import annotations

import uuid

import httpx
from a2a.types import Message, Part, Role, TextPart
from asgi_lifespan import LifespanManager

from a2akit.storage.memory import InMemoryStorage
from conftest import EchoWorker, _make_app


async def test_duplicate_message_id_returns_same_task():
    """Sending the same messageId twice with the same contextId returns the same task id."""
    app = _make_app(EchoWorker(), blocking_timeout_s=2)
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            msg_id = str(uuid.uuid4())
            ctx_id = str(uuid.uuid4())
            body = {
                "message": {
                    "role": "user",
                    "messageId": msg_id,
                    "contextId": ctx_id,
                    "parts": [{"kind": "text", "text": "hi"}],
                },
                "configuration": {"blocking": True},
            }
            r1 = await client.post("/v1/message:send", json=body)
            r2 = await client.post("/v1/message:send", json=body)

            assert r1.status_code == 200
            assert r2.status_code == 200
            assert r1.json()["id"] == r2.json()["id"]


async def test_create_task_sets_just_created_marker_on_fresh_insert():
    """Regression: Storage must signal fresh insert vs idempotent hit via a
    transient metadata marker.

    A state-based heuristic (``state == submitted``) is insufficient because
    both a brand-new task AND an idempotent hit on a task whose worker has
    not yet picked it up are in the ``submitted`` state. Using the state
    heuristic would cause double enqueues on client retries and — on
    multi-worker Redis deployments — parallel processing of the same task.
    """
    storage = InMemoryStorage()
    msg = Message(
        role=Role.user,
        parts=[Part(TextPart(text="hi"))],
        message_id="msg-1",
    )

    fresh = await storage.create_task("ctx-1", msg, idempotency_key="msg-1")
    assert fresh.metadata is not None
    assert fresh.metadata.get("_a2akit_just_created") is True

    # Second call with same idempotency_key hits the cache — marker MUST be absent.
    hit = await storage.create_task("ctx-1", msg, idempotency_key="msg-1")
    assert hit.id == fresh.id
    assert not (hit.metadata and hit.metadata.get("_a2akit_just_created"))


async def test_create_task_marker_not_persisted_in_storage():
    """The just-created marker is a transient signal, not stored state.

    If it were persisted, a subsequent ``load_task`` would see it and
    callers would incorrectly conclude the task is freshly created.
    """
    storage = InMemoryStorage()
    msg = Message(
        role=Role.user,
        parts=[Part(TextPart(text="hi"))],
        message_id="msg-persist",
    )

    created = await storage.create_task("ctx-persist", msg)
    assert created.metadata is not None
    assert created.metadata.get("_a2akit_just_created") is True

    reloaded = await storage.load_task(created.id)
    assert reloaded is not None
    assert not (reloaded.metadata and reloaded.metadata.get("_a2akit_just_created"))


async def test_double_enqueue_race_prevented_via_marker():
    """Regression: two ``create_task`` calls with the same idempotency_key
    within the worker-pickup window (task still in ``submitted``) must
    only enqueue once.

    Before the fix, TaskManager decided ``should_enqueue`` by checking
    ``task.status.state == submitted``. Both calls saw ``submitted`` and
    both enqueued, so on multi-worker deployments two workers could
    process the same task in parallel.
    """
    from a2akit.broker.memory import InMemoryBroker, InMemoryCancelRegistry
    from a2akit.event_bus.memory import InMemoryEventBus
    from a2akit.task_manager import TaskManager

    storage = InMemoryStorage()
    async with InMemoryBroker() as broker, InMemoryEventBus() as event_bus:
        tm = TaskManager(
            storage=storage,
            broker=broker,
            event_bus=event_bus,
            cancel_registry=InMemoryCancelRegistry(),
        )

        msg = Message(
            role=Role.user,
            parts=[Part(TextPart(text="hi"))],
            message_id="msg-race",
        )

        _task1, should_enqueue_1 = await tm._submit_task("ctx-race", msg)
        _task2, should_enqueue_2 = await tm._submit_task("ctx-race", msg)

        assert should_enqueue_1 is True
        assert should_enqueue_2 is False
