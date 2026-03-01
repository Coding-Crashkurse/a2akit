"""Union type for streaming events and shared constants."""

from __future__ import annotations

from dataclasses import dataclass

from a2a.types import Message, Task, TaskArtifactUpdateEvent, TaskStatusUpdateEvent


@dataclass(frozen=True)
class DirectReply:
    """Internal event wrapper marking a Message as a direct reply.

    Emitted by ``reply_directly()`` so that TaskManager and SSE
    endpoints can distinguish a direct-reply message from a normal
    agent message (emitted by ``respond()``).
    """

    message: Message


StreamEvent = (
    Task | Message | TaskStatusUpdateEvent | TaskArtifactUpdateEvent | DirectReply
)

# Internal task-metadata key set by reply_directly().
# Stored in Task.metadata (not Message.metadata) so it survives
# storage round-trips without polluting user-facing message data.
DIRECT_REPLY_KEY = "_a2akit_direct_reply"

__all__ = ["DirectReply", "StreamEvent", "DIRECT_REPLY_KEY"]
