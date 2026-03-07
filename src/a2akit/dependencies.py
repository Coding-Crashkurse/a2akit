"""Dependency injection primitives for a2akit."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


class Dependency(ABC):
    """Base class for resources with lifecycle management (DB pools, HTTP sessions)."""

    @abstractmethod
    async def startup(self) -> None:
        """Called during server startup, before the first request."""

    @abstractmethod
    async def shutdown(self) -> None:
        """Called during server shutdown, after the last request."""


class DependencyContainer:
    """Simple key-value container for dependency injection.

    Keys can be types or strings::

        container[DatabasePool]  # type key
        container.get("api_key")  # string key

    Dependencies that subclass :class:`Dependency` get their
    ``startup()`` / ``shutdown()`` called automatically.
    """

    def __init__(self, registry: dict[Any, Any] | None = None) -> None:
        self._registry: dict[Any, Any] = dict(registry) if registry else {}
        self._started = False

    def get(self, key: Any, default: Any = None) -> Any:
        """Get a dependency by key, returning *default* if missing."""
        return self._registry.get(key, default)

    def __getitem__(self, key: Any) -> Any:
        """Get a dependency by key. Raises ``KeyError`` if not found."""
        return self._registry[key]

    def __contains__(self, key: Any) -> bool:
        """Check whether *key* is registered."""
        return key in self._registry

    async def startup(self) -> None:
        """Call ``startup()`` on all :class:`Dependency` instances (registration order).

        Idempotent — a second call is a no-op.  On failure, already-started
        dependencies are rolled back via ``shutdown()`` before re-raising.
        """
        if self._started:
            return
        started: list[Dependency] = []
        try:
            for value in self._registry.values():
                if isinstance(value, Dependency):
                    await value.startup()
                    started.append(value)
        except BaseException:
            for dep in reversed(started):
                try:
                    await dep.shutdown()
                except Exception:
                    logger.exception("Error during rollback shutdown of %s", type(dep).__name__)
            raise
        self._started = True

    async def shutdown(self) -> None:
        """Call ``shutdown()`` on all :class:`Dependency` instances (reverse order).

        No-op if ``startup()`` was never called.  Individual shutdown errors
        are logged but never raised — every dependency gets a chance to clean up.
        """
        if not self._started:
            return
        for value in reversed(list(self._registry.values())):
            if isinstance(value, Dependency):
                try:
                    await value.shutdown()
                except Exception:
                    logger.exception("Error shutting down dependency %s", type(value).__name__)
        self._started = False
