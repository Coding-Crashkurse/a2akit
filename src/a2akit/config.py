"""Central configuration — env-var binding via pydantic-settings."""

from __future__ import annotations

import logging
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="A2AKIT_")

    blocking_timeout: float = 30.0
    cancel_force_timeout: float = 60.0
    max_concurrent_tasks: int | None = None
    max_retries: int = 3
    broker_buffer: int = 1000
    event_buffer: int = 200
    log_level: str | None = None

    # Event replay buffer for SSE Last-Event-ID
    event_replay_buffer: int = 100

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    redis_key_prefix: str = "a2akit:"
    redis_broker_stream: str = "tasks"
    redis_broker_group: str = "workers"
    redis_broker_consumer_prefix: str = "worker"
    redis_broker_block_ms: int = 5000
    redis_broker_claim_timeout_ms: int = 60000
    redis_event_bus_channel_prefix: str = "events:"
    redis_event_bus_stream_prefix: str = "eventlog:"
    redis_event_bus_stream_maxlen: int = 1000
    redis_cancel_key_prefix: str = "cancel:"
    redis_cancel_ttl_s: int = 86400  # 24h

    # Push notification settings
    push_max_retries: int = 3
    push_retry_delay: float = 1.0
    push_timeout: float = 10.0
    push_max_concurrent: int = 50
    push_allow_http: bool = False


@lru_cache
def get_settings() -> Settings:
    """Return cached Settings instance (lazy singleton).

    First call reads env-vars and caches the result.
    Tests can call ``get_settings.cache_clear()`` to force re-read,
    or simply pass a fresh ``Settings()`` instance directly.
    """
    s = Settings()
    if s.log_level:
        logging.getLogger("a2akit").setLevel(s.log_level.upper())
    return s
