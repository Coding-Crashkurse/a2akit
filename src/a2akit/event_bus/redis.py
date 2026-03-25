"""Redis-backed event bus for multi-process deployments.

Uses Redis Pub/Sub for live fan-out and Redis Streams for replay buffer,
enabling Last-Event-ID based reconnection.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Self

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


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# RedisEventBus
# ---------------------------------------------------------------------------


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

    async def publish(self, task_id: str, event: StreamEvent) -> str | None:
        """Dual-write: XADD to replay stream + PUBLISH to Pub/Sub channel."""
        assert self._redis is not None
        serialized = _serialize_event(event)
        stream_key = f"{self._stream_prefix}{task_id}"
        channel = f"{self._channel_prefix}{task_id}"

        # 1. Append to replay stream
        event_id = await self._redis.xadd(
            stream_key,
            {"data": serialized},
            maxlen=self._stream_maxlen,
            approximate=True,
        )

        # 2. Publish to live subscribers
        # Decode event_id if bytes
        eid = event_id.decode() if isinstance(event_id, bytes) else event_id
        await self._redis.publish(
            channel,
            json.dumps({"event_id": eid, "data": serialized}),
        )

        return eid

    @asynccontextmanager
    async def subscribe(
        self, task_id: str, *, after_event_id: str | None = None
    ) -> AsyncIterator[AsyncIterator[StreamEvent]]:
        """Subscribe to events for a task.

        Replays missed events from the stream, then switches to
        live Pub/Sub delivery. The gap between replay and live is
        handled by a gap-fill check after subscribing.
        """
        assert self._redis is not None
        pubsub = self._redis.pubsub()
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
    ) -> AsyncIterator[StreamEvent]:
        """Replay → gap-fill → live Pub/Sub."""
        assert self._redis is not None
        stream_key = f"{self._stream_prefix}{task_id}"
        last_seen_id: str = after_event_id or "0-0"

        # Phase 1: Replay from stream
        if after_event_id is not None:
            entries = await self._redis.xrange(stream_key, min=f"({after_event_id}", max="+")
            for entry_id, fields in entries:
                eid = entry_id.decode() if isinstance(entry_id, bytes) else entry_id
                data = fields.get(b"data", b"").decode()
                try:
                    event = _deserialize_event(data)
                except Exception:
                    logger.exception("Failed to deserialize replay event %s", eid)
                    continue
                last_seen_id = eid
                yield event
                if _is_final(event):
                    return

        # Phase 2: Gap-fill — events added between replay and subscribe
        gap_entries = await self._redis.xrange(stream_key, min=f"({last_seen_id}", max="+")
        for entry_id, fields in gap_entries:
            eid = entry_id.decode() if isinstance(entry_id, bytes) else entry_id
            data = fields.get(b"data", b"").decode()
            try:
                event = _deserialize_event(data)
            except Exception:
                logger.exception("Failed to deserialize gap-fill event %s", eid)
                continue
            last_seen_id = eid
            yield event
            if _is_final(event):
                return

        # Phase 3: Live Pub/Sub
        while True:
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if message is None:
                continue
            if message["type"] != "message":
                continue
            try:
                payload = json.loads(message["data"])
                event_id = payload["event_id"]
                # Deduplicate: skip events we've already seen
                if event_id <= last_seen_id:
                    continue
                last_seen_id = event_id
                event = _deserialize_event(payload["data"])
            except Exception:
                logger.exception("Failed to deserialize live event")
                continue
            yield event
            if _is_final(event):
                return

    async def cleanup(self, task_id: str) -> None:
        """Delete the replay stream for a completed task."""
        assert self._redis is not None
        stream_key = f"{self._stream_prefix}{task_id}"
        await self._redis.delete(stream_key)
