"""Tests for a2akit.config — Settings, get_settings, env-var binding."""

from __future__ import annotations

import logging

from a2akit import A2AServer, AgentCardConfig, TaskContext, Worker
from a2akit.config import Settings, get_settings


class _EchoWorker(Worker):
    async def handle(self, ctx: TaskContext) -> None:
        await ctx.complete(f"Echo: {ctx.user_text}")


_CARD = AgentCardConfig(name="T", description="T", version="0.1.0")


class TestSettingsDefaults:
    """Built-in defaults without env-vars."""

    def test_blocking_timeout_default(self):
        s = Settings()
        assert s.blocking_timeout == 30.0

    def test_cancel_force_timeout_default(self):
        s = Settings()
        assert s.cancel_force_timeout == 60.0

    def test_max_concurrent_tasks_default_none(self):
        s = Settings()
        assert s.max_concurrent_tasks is None

    def test_max_retries_default(self):
        s = Settings()
        assert s.max_retries == 3

    def test_broker_buffer_default(self):
        s = Settings()
        assert s.broker_buffer == 1000

    def test_event_buffer_default(self):
        s = Settings()
        assert s.event_buffer == 200

    def test_log_level_default_none(self):
        s = Settings()
        assert s.log_level is None


class TestEnvVarOverride:
    """Env-vars override defaults."""

    def test_blocking_timeout_from_env(self, monkeypatch):
        monkeypatch.setenv("A2AKIT_BLOCKING_TIMEOUT", "5.0")
        s = Settings()
        assert s.blocking_timeout == 5.0

    def test_max_concurrent_tasks_from_env(self, monkeypatch):
        monkeypatch.setenv("A2AKIT_MAX_CONCURRENT_TASKS", "8")
        s = Settings()
        assert s.max_concurrent_tasks == 8

    def test_max_retries_from_env(self, monkeypatch):
        monkeypatch.setenv("A2AKIT_MAX_RETRIES", "10")
        s = Settings()
        assert s.max_retries == 10

    def test_log_level_from_env(self, monkeypatch):
        monkeypatch.setenv("A2AKIT_LOG_LEVEL", "DEBUG")
        s = Settings()
        assert s.log_level == "DEBUG"


class TestGetSettings:
    """get_settings() caching and logging bootstrap."""

    def test_get_settings_returns_settings(self):
        get_settings.cache_clear()
        s = get_settings()
        assert isinstance(s, Settings)

    def test_get_settings_is_cached(self):
        get_settings.cache_clear()
        s1 = get_settings()
        s2 = get_settings()
        assert s1 is s2

    def test_get_settings_log_level_bootstrap(self, monkeypatch):
        monkeypatch.setenv("A2AKIT_LOG_LEVEL", "DEBUG")
        get_settings.cache_clear()
        get_settings()
        assert logging.getLogger("a2akit").level == logging.DEBUG
        get_settings.cache_clear()


class TestConstructorPriority:
    """Explicit constructor parameters beat env-vars."""

    def test_explicit_param_wins(self, monkeypatch):
        monkeypatch.setenv("A2AKIT_BLOCKING_TIMEOUT", "99.0")
        s = Settings()
        # Simulate what A2AServer.__init__ does:
        explicit = 5.0
        resolved = explicit if explicit is not None else s.blocking_timeout
        assert resolved == 5.0

    def test_none_param_falls_through_to_env(self, monkeypatch):
        monkeypatch.setenv("A2AKIT_BLOCKING_TIMEOUT", "99.0")
        s = Settings()
        explicit = None
        resolved = explicit if explicit is not None else s.blocking_timeout
        assert resolved == 99.0


class TestSettingsInjection:
    """Settings object passed directly (for tests)."""

    def test_custom_settings_object(self):
        custom = Settings(blocking_timeout=1.0, max_retries=1)
        assert custom.blocking_timeout == 1.0
        assert custom.max_retries == 1
        # Default for unset fields remains
        assert custom.broker_buffer == 1000


class TestIntegrationA2AServer:
    """A2AServer picks up Settings correctly."""

    def test_server_uses_env_var(self, monkeypatch):
        monkeypatch.setenv("A2AKIT_BLOCKING_TIMEOUT", "7.5")

        server = A2AServer(
            worker=_EchoWorker(),
            agent_card=_CARD,
            settings=Settings(),  # fresh instance reads env
        )
        assert server._blocking_timeout_s == 7.5

    def test_server_explicit_param_wins(self, monkeypatch):
        monkeypatch.setenv("A2AKIT_BLOCKING_TIMEOUT", "99.0")

        server = A2AServer(
            worker=_EchoWorker(),
            agent_card=_CARD,
            blocking_timeout_s=5.0,
            settings=Settings(),
        )
        assert server._blocking_timeout_s == 5.0

    def test_server_cancel_force_timeout_from_settings(self):
        custom = Settings(cancel_force_timeout=120.0)
        server = A2AServer(
            worker=_EchoWorker(),
            agent_card=_CARD,
            settings=custom,
        )
        assert server._cancel_force_timeout_s == 120.0

    def test_server_max_retries_from_settings(self):
        custom = Settings(max_retries=7)
        server = A2AServer(
            worker=_EchoWorker(),
            agent_card=_CARD,
            settings=custom,
        )
        assert server._max_retries == 7
