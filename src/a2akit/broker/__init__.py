"""Broker package — task scheduling, cancellation, and event fan-out."""

from a2akit.broker.base import (
    Broker,
    CancelRegistry,
    CancelScope,
    OperationHandle,
    TaskOperation,
)
from a2akit.broker.memory import InMemoryBroker, InMemoryCancelRegistry

__all__ = [
    "Broker",
    "CancelRegistry",
    "CancelScope",
    "InMemoryBroker",
    "InMemoryCancelRegistry",
    "OperationHandle",
    "TaskOperation",
]


def __getattr__(name: str) -> object:
    """Lazy-load Redis implementations to avoid hard dependency on redis-py."""
    if name in ("RedisBroker", "RedisCancelRegistry"):
        from a2akit.broker.redis import RedisBroker, RedisCancelRegistry

        globals()["RedisBroker"] = RedisBroker
        globals()["RedisCancelRegistry"] = RedisCancelRegistry
        return globals()[name]
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
