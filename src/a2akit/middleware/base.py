"""RequestEnvelope and A2AMiddleware for transient request context."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from a2a.types import Message, MessageSendParams, Task
    from fastapi import Request


@dataclass
class RequestEnvelope:
    """Wraps A2A protocol params with transient framework context.

    The envelope is the unit of work that flows through the middleware
    pipeline and into the broker. Storage only ever sees ``params``;
    ``context`` is transient and discarded after the Worker finishes.

    Attributes:
        params: The A2A protocol payload. Persisted by Storage.
        context: Framework-internal metadata. Never persisted.
                 Populated by middleware, consumed by the Worker
                 via ``ctx.request_context``.
    """

    params: MessageSendParams
    context: dict[str, Any] = field(default_factory=dict)


class A2AMiddleware:
    """Base class for request middleware.

    Middleware operates on the RequestEnvelope at the HTTP boundary,
    before TaskManager processes the request and after it returns.

    Both methods have default no-op implementations. Override only
    what you need — an auth middleware typically only needs
    ``before_dispatch``; a logging middleware might need both.
    """

    async def before_dispatch(
        self,
        envelope: RequestEnvelope,
        request: Request,
    ) -> None:
        """Called before TaskManager sees the request.

        Mutate the envelope in-place:
        - Extract secrets from ``envelope.params.message.metadata``
          and move them to ``envelope.context``
        - Extract HTTP headers from ``request`` into ``envelope.context``
        - Sanitize or validate ``envelope.params``
        - Enrich ``envelope.context`` with computed values

        Args:
            envelope: The request envelope. Mutate in-place.
            request: The raw FastAPI/Starlette request (headers, IP, etc.).
        """

    async def after_dispatch(
        self,
        envelope: RequestEnvelope,
        result: Task | Message,
    ) -> None:
        """Called after TaskManager returns, before the HTTP response is sent.

        Use for logging, metrics, cleanup. ``envelope.context`` is still
        available here (same object as in ``before_dispatch``).

        Args:
            envelope: The same envelope from ``before_dispatch``.
            result: The Task or Message returned by TaskManager.
        """
