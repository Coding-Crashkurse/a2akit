"""Central configuration — env-var binding via pydantic-settings."""

from __future__ import annotations

import logging
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


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

    # Honor X-Forwarded-Proto/X-Forwarded-Host when deriving external URLs
    # (agent card). Enable only behind a trusted reverse proxy.
    trust_proxy_headers: bool = False

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    redis_key_prefix: str = "a2akit:"
    redis_broker_stream: str = "tasks"
    redis_broker_group: str = "workers"
    redis_broker_consumer_prefix: str = "worker"
    redis_broker_block_ms: int = 5000
    # XAUTOCLAIM idle threshold. A message pending longer than this is
    # assumed to belong to a crashed worker and is re-delivered to another
    # consumer. Tradeoff: a lower value recovers from crashes faster but
    # DUPLICATES tasks that legitimately run long (an LLM turn routinely
    # exceeds 60s) — there is no worker heartbeat, so a busy worker is
    # indistinguishable from a dead one. The 10-minute default favors
    # avoiding duplicate execution over crash-recovery latency. For
    # multi-worker deployments combine this with per-task locking
    # (``redis_task_lock_factory``) so duplicate delivery stays harmless.
    redis_broker_claim_timeout_ms: int = 600000
    # Approximate MAXLEN for the broker task stream and its DLQ. XACK does
    # not remove entries from a stream, so without trimming they grow
    # forever. Applied (approximate=True) on every XADD.
    redis_broker_stream_maxlen: int = 10000
    redis_event_bus_channel_prefix: str = "events:"
    redis_event_bus_stream_prefix: str = "eventlog:"
    redis_event_bus_stream_maxlen: int = 1000
    redis_cancel_ttl_s: int = 86400  # 24h
    # TTL for idempotency-key mappings in RedisStorage. SQL and Memory
    # backends never expire idempotency keys; Redis does (see the
    # create_task contract in storage/base.py).
    redis_idempotency_ttl_s: int = 86400  # 24h

    # Push notification settings
    push_max_retries: int = 3
    push_retry_delay: float = 1.0
    push_timeout: float = 10.0
    push_max_concurrent: int = 50
    push_allow_http: bool = False
    push_idle_timeout: float = 300.0


@lru_cache
def get_settings() -> Settings:
    """Return cached Settings instance (lazy singleton).

    First call reads env-vars and caches the result.
    Tests can call ``get_settings.cache_clear()`` to force re-read,
    or simply pass a fresh ``Settings()`` instance directly.
    """
    s = Settings()
    if s.log_level:
        level = s.log_level.upper()
        if level not in logging.getLevelNamesMapping():
            # get_settings is called from arbitrary call sites — an invalid
            # A2AKIT_LOG_LEVEL must not raise ValueError there.
            logger.warning("Invalid A2AKIT_LOG_LEVEL %r, falling back to INFO", s.log_level)
            level = "INFO"
        logging.getLogger("a2akit").setLevel(level)
    return s
