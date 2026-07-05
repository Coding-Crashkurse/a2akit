"""Built-in auth middlewares for Bearer tokens and API keys."""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING, Any

from a2akit.errors import AuthenticationRequiredError
from a2akit.middleware.base import A2AMiddleware

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import Request

    from a2akit.middleware.base import RequestEnvelope


class BearerTokenMiddleware(A2AMiddleware):
    """Validates Bearer tokens via a user-provided verify function.

    Note on path exclusion:
        Agent Card Discovery (GET /.well-known/agent-card.json) does NOT
        pass through the A2A middleware pipeline — it's a plain FastAPI
        endpoint. No exclusion needed for it.

        For JSON-RPC, all calls go through POST /, but before_dispatch
        only fires for actual A2A method calls, not for discovery.

        exclude_paths is only relevant for REST-specific routes that
        pass through the pipeline but should remain public (e.g. /v1/health).

    Usage::

        async def verify(token: str) -> dict | None:
            # Return claims dict or None if invalid
            ...


        server = A2AServer(
            worker=...,
            middlewares=[BearerTokenMiddleware(verify=verify)],
        )
    """

    def __init__(
        self,
        verify: Callable[[str], Awaitable[dict[str, Any] | None]],
        *,
        realm: str = "a2a",
        exclude_paths: set[str] | None = None,
    ) -> None:
        self._verify = verify
        self._realm = realm
        self._exclude_paths = exclude_paths or {"/v1/health", "/v1/health/ready"}

    async def before_dispatch(
        self,
        envelope: RequestEnvelope,
        request: Request,
    ) -> None:
        """Validate the Authorization: Bearer <token> header."""
        if request.url.path in self._exclude_paths:
            return
        auth = request.headers.get("Authorization", "")
        # RFC 7235: the auth scheme is case-insensitive.
        scheme, _, token = auth.partition(" ")
        if scheme.casefold() != "bearer" or not token:
            raise AuthenticationRequiredError(scheme="Bearer", realm=self._realm)
        claims = await self._verify(token)
        if claims is None:
            raise AuthenticationRequiredError(scheme="Bearer", realm=self._realm)
        envelope.context["auth_claims"] = claims
        envelope.context["auth_token"] = token


class ApiKeyMiddleware(A2AMiddleware):
    """Validates API keys from a configurable header.

    See BearerTokenMiddleware docstring for notes on path exclusion
    and how the middleware pipeline relates to Agent Card Discovery.

    Usage::

        server = A2AServer(
            middlewares=[
                ApiKeyMiddleware(
                    valid_keys={"sk-abc123", "sk-def456"},
                )
            ],
        )
    """

    def __init__(
        self,
        valid_keys: set[str],
        *,
        header: str = "X-API-Key",
        exclude_paths: set[str] | None = None,
    ) -> None:
        self._valid_keys = valid_keys
        self._header = header
        self._exclude_paths = exclude_paths or {"/v1/health", "/v1/health/ready"}

    async def before_dispatch(
        self,
        envelope: RequestEnvelope,
        request: Request,
    ) -> None:
        """Validate the API key header."""
        if request.url.path in self._exclude_paths:
            return
        key = request.headers.get(self._header)
        # Constant-time comparison over all keys to avoid timing side channels.
        if not key or not any(hmac.compare_digest(key, k) for k in self._valid_keys):
            raise AuthenticationRequiredError(scheme="ApiKey")
        envelope.context["api_key"] = key
