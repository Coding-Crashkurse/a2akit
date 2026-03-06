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
