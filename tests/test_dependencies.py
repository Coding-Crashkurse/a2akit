"""Tests for the dependency injection system."""

from __future__ import annotations

import uuid

import httpx
import pytest
from asgi_lifespan import LifespanManager

from a2akit import (
    A2AServer,
    AgentCardConfig,
    Dependency,
    DependencyContainer,
    TaskContext,
    Worker,
)


class MockDatabase(Dependency):
    """Test dependency with lifecycle tracking."""

    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def startup(self) -> None:
        self.started = True

    async def shutdown(self) -> None:
        self.stopped = True


class FailingDependency(Dependency):
    """Dependency whose startup() always fails."""

    async def startup(self) -> None:
        raise RuntimeError("startup failed")

    async def shutdown(self) -> None:
        pass


class OrderTracker(Dependency):
    """Records startup/shutdown order into a shared list."""

    def __init__(self, name: str, log: list[str]) -> None:
        self.name = name
        self._log = log

    async def startup(self) -> None:
        self._log.append(f"{self.name}:start")

    async def shutdown(self) -> None:
        self._log.append(f"{self.name}:stop")


class DepAwareWorker(Worker):
    """Worker that reads ctx.deps and returns info as completion text."""

    async def handle(self, ctx: TaskContext) -> None:
        db: MockDatabase = ctx.deps[MockDatabase]
        api_key: str = ctx.deps.get("api_key", "none")
        lines = [
            f"db_started={db.started}",
            f"api_key={api_key}",
            f"input={ctx.user_text}",
        ]
        await ctx.complete("\n".join(lines))


class NoDepsWorker(Worker):
    """Worker that checks deps is an empty container."""

    async def handle(self, ctx: TaskContext) -> None:
        has_mock = MockDatabase in ctx.deps
        await ctx.complete(f"has_mock={has_mock}")


class TestContainerBasics:
    async def test_get_by_type(self) -> None:
        db = MockDatabase()
        c = DependencyContainer({MockDatabase: db})
        assert c.get(MockDatabase) is db

    async def test_get_by_string(self) -> None:
        c = DependencyContainer({"api_key": "sk-123"})
        assert c.get("api_key") == "sk-123"

    async def test_get_missing_returns_none(self) -> None:
        c = DependencyContainer()
        assert c.get("nope") is None

    async def test_get_missing_returns_default(self) -> None:
        c = DependencyContainer()
        assert c.get("nope", "fallback") == "fallback"

    async def test_getitem_missing_raises(self) -> None:
        c = DependencyContainer()
        with pytest.raises(KeyError):
            c[MockDatabase]

    async def test_getitem_success(self) -> None:
        db = MockDatabase()
        c = DependencyContainer({MockDatabase: db})
        assert c[MockDatabase] is db

    async def test_contains(self) -> None:
        c = DependencyContainer({MockDatabase: MockDatabase(), "key": "val"})
        assert MockDatabase in c
        assert "key" in c
        assert "missing" not in c

    async def test_empty_container(self) -> None:
        c = DependencyContainer()
        assert c.get("anything") is None
        assert "anything" not in c


class TestContainerLifecycle:
    async def test_startup_calls_dependencies(self) -> None:
        db = MockDatabase()
        c = DependencyContainer({MockDatabase: db})
        await c.startup()
        assert db.started is True

    async def test_shutdown_calls_dependencies(self) -> None:
        db = MockDatabase()
        c = DependencyContainer({MockDatabase: db})
        await c.startup()
        await c.shutdown()
        assert db.stopped is True

    async def test_startup_skips_plain_values(self) -> None:
        c = DependencyContainer({"key": "value", "num": 42})
        await c.startup()
        await c.shutdown()

    async def test_startup_order(self) -> None:
        log: list[str] = []
        a = OrderTracker("A", log)
        b = OrderTracker("B", log)
        c_dep = OrderTracker("C", log)
        c = DependencyContainer({"a": a, "b": b, "c": c_dep})
        await c.startup()
        assert log == ["A:start", "B:start", "C:start"]

    async def test_shutdown_reverse_order(self) -> None:
        log: list[str] = []
        a = OrderTracker("A", log)
        b = OrderTracker("B", log)
        c_dep = OrderTracker("C", log)
        c = DependencyContainer({"a": a, "b": b, "c": c_dep})
        await c.startup()
        log.clear()
        await c.shutdown()
        assert log == ["C:stop", "B:stop", "A:stop"]

    async def test_startup_failure_rolls_back(self) -> None:
        log: list[str] = []
        a = OrderTracker("A", log)
        b = OrderTracker("B", log)
        fail = FailingDependency()
        c = DependencyContainer({"a": a, "b": b, "fail": fail})
        with pytest.raises(RuntimeError, match="startup failed"):
            await c.startup()
        assert "A:start" in log
        assert "B:start" in log
        assert "B:stop" in log
        assert "A:stop" in log

    async def test_double_startup_is_noop(self) -> None:
        log: list[str] = []
        a = OrderTracker("A", log)
        c = DependencyContainer({"a": a})
        await c.startup()
        await c.startup()
        assert log.count("A:start") == 1

    async def test_shutdown_without_startup_is_noop(self) -> None:
        db = MockDatabase()
        c = DependencyContainer({MockDatabase: db})
        await c.shutdown()
        assert db.stopped is False


def _make_send_body(text: str = "hello") -> dict:
    return {
        "message": {
            "role": "user",
            "messageId": str(uuid.uuid4()),
            "parts": [{"kind": "text", "text": text}],
        },
        "configuration": {"blocking": True},
    }


def _card() -> AgentCardConfig:
    return AgentCardConfig(name="Test", description="DI test", version="0.0.1")


class TestIntegration:
    async def test_deps_available_in_worker(self) -> None:
        db = MockDatabase()
        server = A2AServer(
            worker=DepAwareWorker(),
            agent_card=_card(),
            dependencies={MockDatabase: db, "api_key": "sk-test-123"},
        )
        app = server.as_fastapi_app()

        async with LifespanManager(app) as manager:
            transport = httpx.ASGITransport(app=manager.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post("/v1/message:send", json=_make_send_body("hello"))
                assert resp.status_code == 200
                task = resp.json()
                text = task["artifacts"][0]["parts"][0]["text"]
                assert "db_started=True" in text
                assert "api_key=sk-test-123" in text

        assert db.stopped is True

    async def test_no_deps_gives_empty_container(self) -> None:
        server = A2AServer(worker=NoDepsWorker(), agent_card=_card())
        app = server.as_fastapi_app()

        async with LifespanManager(app) as manager:
            transport = httpx.ASGITransport(app=manager.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post("/v1/message:send", json=_make_send_body())
                assert resp.status_code == 200
                text = resp.json()["artifacts"][0]["parts"][0]["text"]
                assert "has_mock=False" in text

    async def test_lifecycle_dependency_started_before_first_request(self) -> None:
        db = MockDatabase()
        server = A2AServer(
            worker=DepAwareWorker(),
            agent_card=_card(),
            dependencies={MockDatabase: db, "api_key": "x"},
        )
        app = server.as_fastapi_app()

        async with LifespanManager(app):
            assert db.started is True

    async def test_lifecycle_dependency_stopped_after_shutdown(self) -> None:
        db = MockDatabase()
        server = A2AServer(
            worker=DepAwareWorker(),
            agent_card=_card(),
            dependencies={MockDatabase: db, "api_key": "x"},
        )
        app = server.as_fastapi_app()

        async with LifespanManager(app):
            assert db.stopped is False
        assert db.stopped is True
