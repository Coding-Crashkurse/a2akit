"""Redis-backed event bus for multi-process deployments.

Uses Redis Pub/Sub for live fan-out and Redis Streams for replay buffer,
enabling Last-Event-ID based reconnection.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Self

from a2a.types import (
    Message,
    Task,
    TaskArtifactUpdateEvent,
    TaskStatusUpdateEvent,
)

from a2akit.config import Settings, get_settings
from a2akit.event_bus.base import EventBus
from a2akit.schema import DirectReply, StreamEvent

try:
    import redis.asyncio as aioredis
except ImportError as _import_error:
    raise ImportError(
        "Redis backend requires additional dependencies. "
        "Install them with: pip install a2akit[redis]"
    ) from _import_error

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)


def _serialize_event(event: StreamEvent) -> str:
    """Serialize a StreamEvent to JSON."""
    if isinstance(event, DirectReply):
        return json.dumps(
            {
                "_type": "DirectReply",
                "message": event.message.model_dump(mode="json", by_alias=True, exclude_none=True),
            }
        )
    if isinstance(event, Task | Message | TaskStatusUpdateEvent | TaskArtifactUpdateEvent):
        return event.model_dump_json(by_alias=True, exclude_none=True)
    msg = f"Unknown event type: {type(event)}"
    raise TypeError(msg)


def _deserialize_event(data: str) -> StreamEvent:
    """Deserialize a JSON string into a StreamEvent."""
    raw = json.loads(data)
    _type = raw.get("_type")
    if _type == "DirectReply":
        return DirectReply(message=Message.model_validate(raw["message"]))
    kind = raw.get("kind")
    if kind == "status-update":
        return TaskStatusUpdateEvent.model_validate(raw)
    if kind == "artifact-update":
        return TaskArtifactUpdateEvent.model_validate(raw)
    # Task and Message don't have a 'kind' field — distinguish by structure
    if "status" in raw and "id" in raw:
        return Task.model_validate(raw)
    if "role" in raw and "parts" in raw:
        return Message.model_validate(raw)
    # Fallback: try Task first
    try:
        return Task.model_validate(raw)
    except Exception:
        pass
    msg = f"Cannot deserialize event: {data[:200]}"
    raise ValueError(msg)


def _is_final(event: StreamEvent) -> bool:
    """Check if an event signals the end of the stream."""
    return isinstance(event, TaskStatusUpdateEvent) and event.final


class RedisEventBus(EventBus):
    """Redis-backed event bus using Pub/Sub + Streams.

    - **Pub/Sub** delivers events to live subscribers with minimal latency.
    - **Streams** provide a replay buffer so reconnecting clients can resume
      from a ``Last-Event-ID`` without missing events.
    """

    def __init__(
        self,
        url: str | None = None,
        *,
        pool: aioredis.ConnectionPool | None = None,
        channel_prefix: str | None = None,
        stream_prefix: str | None = None,
        stream_maxlen: int | None = None,
        key_prefix: str | None = None,
        settings: Settings | None = None,
    ) -> None:
        s = settings or get_settings()
        prefix = key_prefix or s.redis_key_prefix
        self._channel_prefix = channel_prefix or f"{prefix}{s.redis_event_bus_channel_prefix}"
        self._stream_prefix = stream_prefix or f"{prefix}{s.redis_event_bus_stream_prefix}"
        self._stream_maxlen = (
            stream_maxlen if stream_maxlen is not None else s.redis_event_bus_stream_maxlen
        )
        self._owns_connection = pool is None
        self._url = url or s.redis_url
        self._pool = pool
        self._redis: aioredis.Redis | None = None

    @property
    def _r(self) -> aioredis.Redis:
        if self._redis is None:
            raise RuntimeError("RedisEventBus not connected — use 'async with event_bus' first")
        return self._redis

    async def __aenter__(self) -> Self:
        """Create Redis client and verify connectivity."""
        if self._pool is not None:
            self._redis = aioredis.Redis(connection_pool=self._pool)
        else:
            self._redis = aioredis.from_url(self._url)
        await self._redis.ping()
        logger.info("Redis event bus connected")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        """Close Redis connection if we own it."""
        if self._redis and self._owns_connection:
            await self._redis.aclose()

    async def health_check(self) -> dict[str, Any]:
        """Ping Redis to verify connectivity."""
        try:
            if self._redis:
                await self._redis.ping()
            return {"status": "ok"}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    async def publish(self, task_id: str, event: StreamEvent) -> str | None:
        """Dual-write: XADD to replay stream + PUBLISH wakeup to Pub/Sub channel.

        Uses a Redis pipeline to combine XADD + PUBLISH into a single
        roundtrip.  The Pub/Sub message is a lightweight wakeup signal
        ("1"); live subscribers read the actual payload via XREAD from
        the stream, avoiding double serialization.
        """
        serialized = _serialize_event(event)
        stream_key = f"{self._stream_prefix}{task_id}"
        channel = f"{self._channel_prefix}{task_id}"

        async with self._r.pipeline(transaction=False) as pipe:
            pipe.xadd(
                stream_key,
                {"data": serialized},
                maxlen=self._stream_maxlen,
                approximate=True,
            )
            pipe.publish(channel, "1")
            results = await pipe.execute()

        eid = results[0].decode() if isinstance(results[0], bytes) else results[0]
        return eid

    @asynccontextmanager
    async def subscribe(
        self, task_id: str, *, after_event_id: str | None = None
    ) -> AsyncIterator[AsyncIterator[tuple[str | None, StreamEvent]]]:
        """Subscribe to events for a task.

        Replays missed events from the stream, then switches to
        live Pub/Sub delivery. The gap between replay and live is
        handled by a gap-fill check after subscribing.
        """
        pubsub = self._r.pubsub()
        channel = f"{self._channel_prefix}{task_id}"
        await pubsub.subscribe(channel)

        try:
            yield self._iter_events(pubsub, task_id, after_event_id)
        finally:
            try:
                await pubsub.unsubscribe(channel)
                await pubsub.aclose()
            except Exception:
                pass

    async def _iter_events(
        self,
        pubsub: aioredis.client.PubSub,
        task_id: str,
        after_event_id: str | None,
    ) -> AsyncIterator[tuple[str | None, StreamEvent]]:
        """Replay → gap-fill → live Pub/Sub."""
        stream_key = f"{self._stream_prefix}{task_id}"
        last_seen_id: str = after_event_id or "0-0"

        # Phase 1: Replay from stream
        if after_event_id is not None:
            entries = await self._r.xrange(stream_key, min=f"({after_event_id}", max="+")
            for entry_id, fields in entries:
                eid = entry_id.decode() if isinstance(entry_id, bytes) else entry_id
                data = fields.get(b"data", b"").decode()
                try:
                    event = _deserialize_event(data)
                except Exception:
                    logger.exception("Failed to deserialize replay event %s", eid)
                    continue
                last_seen_id = eid
                yield (eid, event)
                if _is_final(event):
                    return

        # Phase 2: Gap-fill — events added between replay and subscribe
        gap_entries = await self._r.xrange(stream_key, min=f"({last_seen_id}", max="+")
        for entry_id, fields in gap_entries:
            eid = entry_id.decode() if isinstance(entry_id, bytes) else entry_id
            data = fields.get(b"data", b"").decode()
            try:
                event = _deserialize_event(data)
            except Exception:
                logger.exception("Failed to deserialize gap-fill event %s", eid)
                continue
            last_seen_id = eid
            yield (eid, event)
            if _is_final(event):
                return

        # Phase 3: Live — Pub/Sub wakeup + XREAD for actual data
        _safety_poll_interval = 30  # Only poll stream every Nth timeout (~30s)
        _poll_ticks = 0
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message is not None and message["type"] != "message":
                continue
            if message is None:
                _poll_ticks += 1
                if _poll_ticks < _safety_poll_interval:
                    continue
                _poll_ticks = 0
            else:
                _poll_ticks = 0
            # Wakeup or safety poll — read stream (Pub/Sub is at-most-once)
            try:
                entries = await self._r.xrange(stream_key, min=f"({last_seen_id}", max="+")
                for entry_id, fields in entries:
                    eid = entry_id.decode() if isinstance(entry_id, bytes) else entry_id
                    last_seen_id = eid
                    data = fields.get(b"data", b"").decode()
                    event = _deserialize_event(data)
                    yield (eid, event)
                    if _is_final(event):
                        return
            except Exception:
                logger.exception("Failed to read live events from stream")
                continue

    async def cleanup(self, task_id: str) -> None:
        """Delete the replay stream for a completed task."""
        stream_key = f"{self._stream_prefix}{task_id}"
        await self._r.delete(stream_key)
