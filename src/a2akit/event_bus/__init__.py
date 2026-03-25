"""EventBus package — 1:N event fan-out for task streaming."""

from a2akit.event_bus.base import EventBus
from a2akit.event_bus.memory import InMemoryEventBus

__all__ = [
    "EventBus",
    "InMemoryEventBus",
]


def __getattr__(name: str) -> object:
    """Lazy-load Redis implementation to avoid hard dependency on redis-py."""
    if name == "RedisEventBus":
        from a2akit.event_bus.redis import RedisEventBus

        globals()["RedisEventBus"] = RedisEventBus
        return RedisEventBus
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
