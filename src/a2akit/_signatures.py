"""Agent Card JWS signature verification (RFC 7515 + RFC 8785 / JCS).

Spec §19: v1.0 Agent Cards may carry one or more detached JWS signatures in
the ``signatures[]`` field. Clients verify the signatures against the
RFC-8785-canonicalized card body (with ``signatures`` excluded from
canonicalization) before trusting the card.

``a2akit.client.A2AClient`` wires this in with a ``verify_signatures=`` kwarg:
- ``"off"`` — skip verification entirely.
- ``"soft"`` (default) — verify IF signatures present, warn on missing, raise
  on verification failure.
- ``"strict"`` — require at least one signature AND verify; raise on missing.

Key resolution priority:
1. ``kid`` in the JWS header matches an entry in ``trusted_keys``.
2. ``jku`` header (RFC 7517 JWKS URL) — only honored when the caller provides
   an explicit ``allowed_jku_hosts`` allowlist (and ``allow_jku`` is True);
   fetched over HTTPS only, host must be in the allowlist. Without an
   allowlist, jku is ignored: the key must come from ``trusted_keys``.
   (The jku URL is attacker-controlled — fetching keys from it without an
   allowlist would make verification self-referential.)
3. Otherwise raise :class:`AgentCardSignatureError`.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

try:
    import rfc8785
    from jwcrypto import jwk, jws
except ImportError as exc:  # pragma: no cover - exercised only without the extra
    raise ImportError(
        "Signature verification requires the [signatures] extra. "
        "Install with: pip install a2akit[signatures]"
    ) from exc

if TYPE_CHECKING:
    from a2a_pydantic import v10

logger = logging.getLogger(__name__)


class AgentCardSignatureError(Exception):
    """Raised when Agent Card signature verification fails."""


def canonicalize_card_for_signing(card_dict: dict[str, Any]) -> bytes:
    """Produce the canonical byte form used for detached-JWS verification.

    Per A2A v1.0 §Agent Card Signature: serialize the AgentCard object
    using RFC 8785 JCS with the ``signatures`` field excluded.
    """
    clone = {k: v for k, v in card_dict.items() if k != "signatures"}
    return bytes(rfc8785.dumps(clone))


def _b64url_decode(value: str) -> bytes:
    """jwcrypto exposes ``base64url_decode`` on ``jws``; shim for older versions."""
    decoder = getattr(jws, "base64url_decode", None)
    if decoder is not None:
        return bytes(decoder(value))
    from jwcrypto.common import base64url_decode as _bud

    return bytes(_bud(value))


async def _resolve_key(
    header: dict[str, Any],
    *,
    trusted_keys: list[jwk.JWK] | None,
    allow_jku: bool,
    allowed_jku_hosts: set[str] | None,
) -> jwk.JWK:
    """Pick a verification key from the header.

    Priority: trusted_keys[kid] > jku-fetched JWKS > raise.

    jku fetching is only performed when the caller provides an explicit
    ``allowed_jku_hosts`` allowlist — with ``allowed_jku_hosts=None`` the
    jku header is ignored and the key must come from ``trusted_keys``.
    """
    kid = header.get("kid")
    if trusted_keys and kid:
        for k in trusted_keys:
            if k.kid == kid:
                return k

    if allow_jku and allowed_jku_hosts is not None and "jku" in header:
        from urllib.parse import urlparse

        import httpx

        url = header["jku"]
        parsed = urlparse(url)
        if parsed.scheme != "https":
            raise AgentCardSignatureError("jku must be HTTPS for safety")
        if parsed.hostname not in allowed_jku_hosts:
            raise AgentCardSignatureError(f"jku host {parsed.hostname!r} not in allowed_jku_hosts")
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
            resp.raise_for_status()
            jwks = jwk.JWKSet.from_json(resp.text)
        except Exception as exc:
            raise AgentCardSignatureError(f"Failed to fetch JWKS: {exc}") from exc
        if kid:
            key = jwks.get_key(kid)
            if key is None:
                raise AgentCardSignatureError(f"Key {kid!r} not in JWKS")
            return key
        keys = list(jwks)
        if len(keys) == 1:
            return keys[0]
        raise AgentCardSignatureError("Cannot pick JWKS key without kid")

    raise AgentCardSignatureError(
        "No signature key resolvable: provide trusted_keys or an allowed_jku_hosts allowlist"
    )


async def verify_signature(
    card_dict: dict[str, Any],
    signature: v10.AgentCardSignature,
    *,
    trusted_keys: list[jwk.JWK] | None = None,
    allow_jku: bool = True,
    allowed_jku_hosts: set[str] | None = None,
) -> bool:
    """Verify one detached JWS signature against the canonicalized card.

    Returns True on success; raises :class:`AgentCardSignatureError` otherwise.
    """
    try:
        detached = jws.JWS()
        detached.deserialize(
            json.dumps(
                {
                    "protected": signature.protected,
                    "signature": signature.signature,
                }
            )
        )
    except Exception as exc:
        raise AgentCardSignatureError(f"Malformed JWS: {exc}") from exc

    payload = canonicalize_card_for_signing(card_dict)
    header = json.loads(_b64url_decode(signature.protected).decode("utf-8"))
    key = await _resolve_key(
        header,
        trusted_keys=trusted_keys,
        allow_jku=allow_jku,
        allowed_jku_hosts=allowed_jku_hosts,
    )

    try:
        detached.verify(key, detached_payload=payload)
    except Exception as exc:
        raise AgentCardSignatureError(f"JWS verification failed: {exc}") from exc

    return True


async def verify_agent_card(
    card: v10.AgentCard,
    raw_body: bytes,
    *,
    mode: str = "soft",
    trusted_keys: list[jwk.JWK] | None = None,
    allow_jku: bool = True,
    allowed_jku_hosts: set[str] | None = None,
) -> None:
    """Verify all signatures on an Agent Card.

    ``mode``:
      - ``"off"`` — skip.
      - ``"soft"`` — verify IF signatures present; warn on missing; raise on
        verification failure.
      - ``"strict"`` — require at least one signature AND verify; raise on any
        missing/failure.

    jku fetching is disabled unless an explicit ``allowed_jku_hosts``
    allowlist is provided — with ``allowed_jku_hosts=None`` verification
    keys must come from ``trusted_keys``.

    ``raw_body`` is the bytes the server sent (before any client-side
    re-serialization). Required because JCS canonicalization is sensitive to
    key ordering and whitespace, so verifying against a Pydantic
    ``model_dump`` of the parsed card can spuriously fail.
    """
    if mode == "off":
        return

    signatures = card.signatures or []
    if not signatures:
        if mode == "strict":
            raise AgentCardSignatureError("Agent Card has no signatures (strict mode)")
        logger.warning("Agent Card has no signatures; proceeding (soft mode)")
        return

    card_dict = json.loads(raw_body)

    last_error: AgentCardSignatureError | None = None
    for sig in signatures:
        try:
            await verify_signature(
                card_dict,
                sig,
                trusted_keys=trusted_keys,
                allow_jku=allow_jku,
                allowed_jku_hosts=allowed_jku_hosts,
            )
            return
        except AgentCardSignatureError as exc:
            last_error = exc
            logger.warning("One signature failed: %s", exc)

    raise AgentCardSignatureError(f"No signature verified successfully (last error: {last_error})")


__all__ = [
    "AgentCardSignatureError",
    "canonicalize_card_for_signing",
    "verify_agent_card",
    "verify_signature",
]
