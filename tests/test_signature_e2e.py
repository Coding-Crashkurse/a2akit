"""End-to-end Agent Card JWS verification through ``A2AClient.connect()``.

Regression tests for the v1.0 discovery path: the client must verify
signatures against the parsed v1.0 card (which carries ``signatures``),
not the v0.3 projection (which never does).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

pytest.importorskip("jwcrypto")
pytest.importorskip("rfc8785")

from a2a_pydantic import v10
from fastapi import FastAPI
from fastapi.responses import Response
from jwcrypto import jwk, jws

from a2akit.client import A2AClient
from a2akit.client.errors import AgentNotFoundError


def _make_rsa_key(kid: str = "e2e-key-1") -> jwk.JWK:
    return jwk.JWK.generate(kty="RSA", size=2048, kid=kid)


def _v10_card_dict() -> dict[str, Any]:
    card = v10.AgentCard(
        name="SignedAgent",
        description="signed v1.0 card",
        version="1",
        capabilities=v10.AgentCapabilities(),
        default_input_modes=[],
        default_output_modes=[],
        supported_interfaces=[
            v10.AgentInterface(
                protocol_binding="JSONRPC",
                protocol_version="1.0",
                url="http://test",
                tenant="",
            )
        ],
        skills=[],
        security_requirements=[],
        security_schemes={},
        signatures=[],
    )
    return card.model_dump(mode="json", by_alias=True, exclude_none=True)


def _signed_card_body(card_dict: dict[str, Any], key: jwk.JWK) -> bytes:
    """Sign ``card_dict`` (detached JWS over JCS bytes) and return the wire body."""
    import rfc8785

    for_signing = {k: v for k, v in card_dict.items() if k != "signatures"}
    payload = bytes(rfc8785.dumps(for_signing))

    protected = {"alg": "RS256", "kid": key.kid}
    sig_obj = jws.JWS(payload)
    sig_obj.add_signature(key, alg="RS256", protected=json.dumps(protected))
    serialized = json.loads(sig_obj.serialize())

    full_card = dict(card_dict)
    full_card["signatures"] = [
        {"protected": serialized["protected"], "signature": serialized["signature"]}
    ]
    return json.dumps(full_card).encode("utf-8")


def _card_app(body: bytes) -> FastAPI:
    """Minimal app that only serves the discovery card."""
    app = FastAPI()

    @app.get("/.well-known/agent-card.json")
    async def card() -> Response:
        return Response(content=body, media_type="application/json")

    return app


async def _connect(body: bytes, **client_kwargs: Any) -> A2AClient:
    transport = httpx.ASGITransport(app=_card_app(body))
    http = httpx.AsyncClient(transport=transport, base_url="http://test")
    client = A2AClient("http://test", httpx_client=http, **client_kwargs)
    try:
        await client.connect()
    finally:
        if not client.is_connected:
            await client.close()
            await http.aclose()
    return client


async def test_connect_soft_mode_verifies_signed_v10_card() -> None:
    """Soft mode must actually verify a signed v1.0 card (not skip it)."""
    key = _make_rsa_key()
    body = _signed_card_body(_v10_card_dict(), key)

    client = await _connect(body, verify_signatures="soft", trusted_signing_keys=[key])
    try:
        assert client.is_connected
        assert client._card_v10 is not None
        assert client._card_v10.signatures
    finally:
        await client.close()


async def test_connect_strict_mode_accepts_signed_v10_card() -> None:
    """Strict mode must not raise 'no signatures' for a validly signed v1.0 card."""
    key = _make_rsa_key()
    body = _signed_card_body(_v10_card_dict(), key)

    client = await _connect(body, verify_signatures="strict", trusted_signing_keys=[key])
    try:
        assert client.is_connected
    finally:
        await client.close()


async def test_connect_soft_mode_rejects_untrusted_signature() -> None:
    """Soft mode raises when the signature does not verify against trusted keys."""
    key = _make_rsa_key()
    body = _signed_card_body(_v10_card_dict(), key)
    wrong_key = _make_rsa_key(kid="someone-else")

    with pytest.raises(AgentNotFoundError, match="Signature verification failed"):
        await _connect(body, verify_signatures="soft", trusted_signing_keys=[wrong_key])


async def test_connect_rejects_tampered_card() -> None:
    """A card modified after signing must be rejected."""
    key = _make_rsa_key()
    body = _signed_card_body(_v10_card_dict(), key)
    tampered = body.replace(b"SignedAgent", b"EvilAgent")
    assert tampered != body

    with pytest.raises(AgentNotFoundError, match="Signature verification failed"):
        await _connect(tampered, verify_signatures="strict", trusted_signing_keys=[key])
