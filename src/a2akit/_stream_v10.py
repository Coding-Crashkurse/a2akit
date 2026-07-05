"""Shared v1.0 stream-event serialization helpers.

Used by the v1.0 REST router (``endpoints_v10.py``) and the v1.0 JSON-RPC
router (``jsonrpc_v10.py``) so both transports build the same
wrapped-discriminator payloads for SSE events.
"""

from __future__ import annotations

from typing import Any

from a2a_pydantic import v10

from a2akit.schema import DirectReply, TerminalMarker


def sanitize_task_v10(task: v10.Task) -> v10.Task:
    """Strip framework-internal metadata keys before sending a v10 Task on the wire."""
    md = task.metadata
    if not md:
        return task
    cleaned = {k: v for k, v in md.items() if not k.startswith("_")}
    if len(cleaned) == len(md):
        return task
    return task.model_copy(update={"metadata": cleaned or None})


def wrap_stream_event_v10(event: Any, task_cache: dict[str, v10.Task]) -> dict[str, Any] | None:
    """Return the v1.0 wrapped-discriminator payload for a stream event.

    - ``v10.Task`` → ``{"task": {...}}`` (sanitized, cached for artifact
      indexing).
    - ``v10.Message`` / ``DirectReply`` → ``{"message": {...}}``.
    - ``v10.TaskStatusUpdateEvent`` → ``{"taskStatusUpdate": {...}}``.
    - ``v10.TaskArtifactUpdateEvent`` → ``{"taskArtifactUpdate": {...},
      "index": N}`` where ``N`` is the artifact's position in the owning
      task's ``artifacts`` array (0-based), tracked via ``task_cache``
      keyed by ``task_id`` so repeated updates to the same artifact keep a
      stable index.
    - ``TerminalMarker`` → unwrap to the inner status event; the caller
      closes the stream after emitting this payload.

    Returns ``None`` for events that have no v1.0 wire representation.
    """
    if isinstance(event, DirectReply):
        return {"message": event.message.model_dump(mode="json", by_alias=True, exclude_none=True)}
    if isinstance(event, TerminalMarker):
        return {
            "taskStatusUpdate": event.event.model_dump(
                mode="json", by_alias=True, exclude_none=True
            )
        }
    if isinstance(event, v10.Task):
        sanitized = sanitize_task_v10(event)
        task_cache[sanitized.id] = sanitized
        return {"task": sanitized.model_dump(mode="json", by_alias=True, exclude_none=True)}
    if isinstance(event, v10.Message):
        return {"message": event.model_dump(mode="json", by_alias=True, exclude_none=True)}
    if isinstance(event, v10.TaskStatusUpdateEvent):
        return {
            "taskStatusUpdate": event.model_dump(mode="json", by_alias=True, exclude_none=True)
        }
    if isinstance(event, v10.TaskArtifactUpdateEvent):
        idx: int | None = None
        cached = task_cache.get(event.task_id)
        if cached and cached.artifacts:
            for i, a in enumerate(cached.artifacts):
                if a.artifact_id == event.artifact.artifact_id:
                    idx = i
                    break
        payload: dict[str, Any] = {
            "taskArtifactUpdate": event.model_dump(mode="json", by_alias=True, exclude_none=True)
        }
        if idx is not None:
            payload["index"] = idx
        return payload
    return None


__all__ = ["sanitize_task_v10", "wrap_stream_event_v10"]
