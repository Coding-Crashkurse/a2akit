"""Tests for Agent Card JWS signature verification (spec §19)."""

from __future__ import annotations

import json
from typing import Any

import pytest

pytest.importorskip("jwcrypto")
pytest.importorskip("rfc8785")

import rfc8785
from a2a_pydantic import v10
from jwcrypto import jwk, jws

from a2akit._signatures import (
    AgentCardSignatureError,
    canonicalize_card_for_signing,
    verify_agent_card,
)


def _make_rsa_key(kid: str = "test-key-1") -> jwk.JWK:
    key = jwk.JWK.generate(kty="RSA", size=2048, kid=kid)
    return key


def _sign_card(card_dict: dict[str, Any], key: jwk.JWK) -> tuple[bytes, v10.AgentCardSignature]:
    """Build a detached JWS signature over the JCS-canonical card bytes.

    Returns ``(card_bytes_as_sent_on_the_wire, AgentCardSignature)``. The
    signature MUST be computed over the canonicalized card WITHOUT the
    ``signatures`` field, but the returned bytes are what a real server
    would send (which includes signatures). ``verify_agent_card`` handles
    the exclude-signatures canonicalization internally.
    """
    # Drop signatures before signing (they wouldn't exist yet anyway).
    for_signing = {k: v for k, v in card_dict.items() if k != "signatures"}
    payload = bytes(rfc8785.dumps(for_signing))

    protected = {"alg": "RS256", "kid": key.kid}
    sig_obj = jws.JWS(payload)
    sig_obj.add_signature(
        key,
        alg="RS256",
        protected=json.dumps(protected),
    )
    # jwcrypto serializes to a full JWS JSON; we need detached protected + signature.
    serialized = json.loads(sig_obj.serialize())
    sig = v10.AgentCardSignature(
        protected=serialized["protected"],
        signature=serialized["signature"],
    )

    # Inject the signature into the card bytes we "send".
    full_card = dict(card_dict)
    full_card["signatures"] = [sig.model_dump(mode="json", by_alias=True, exclude_none=True)]
    return bytes(rfc8785.dumps(full_card)), sig


def _minimal_card_dict() -> dict[str, Any]:
    """Build a minimal v10.AgentCard JSON dict suitable for JCS canonicalization."""
    card = v10.AgentCard(
        name="Test",
        description="t",
        version="1",
        capabilities=v10.AgentCapabilities(),
        default_input_modes=[],
        default_output_modes=[],
        supported_interfaces=[
            v10.AgentInterface(
                protocol_binding="JSONRPC",
                protocol_version="1.0",
                url="http://example.test",
                tenant="",
            )
        ],
        skills=[],
        security_requirements=[],
        security_schemes={},
        signatures=[],
    )
    return card.model_dump(mode="json", by_alias=True, exclude_none=True)


def test_canonicalize_strips_signatures_field() -> None:
    card = {"a": 1, "b": 2, "signatures": [{"junk": True}]}
    canonical = canonicalize_card_for_signing(card)
    # signatures must not appear in the canonical form
    assert b"junk" not in canonical
    assert b'"a"' in canonical
    assert b'"b"' in canonical


async def test_valid_signature_verifies() -> None:
    key = _make_rsa_key()
    card_dict = _minimal_card_dict()
    raw_body, sig = _sign_card(card_dict, key)

    # Reconstruct the AgentCard with the signature attached.
    card_dict_with_sig = dict(card_dict)
    card_dict_with_sig["signatures"] = [
        sig.model_dump(mode="json", by_alias=True, exclude_none=True)
    ]
    card = v10.AgentCard.model_validate(card_dict_with_sig)

    # Should not raise.
    await verify_agent_card(card, raw_body, mode="soft", trusted_keys=[key])


async def test_tampered_body_raises() -> None:
    key = _make_rsa_key()
    card_dict = _minimal_card_dict()
    raw_body, sig = _sign_card(card_dict, key)

    # Tamper: change a field in the raw bytes.
    tampered = raw_body.replace(b'"Test"', b'"Evil"')

    card_dict_with_sig = dict(card_dict)
    card_dict_with_sig["signatures"] = [
        sig.model_dump(mode="json", by_alias=True, exclude_none=True)
    ]
    card = v10.AgentCard.model_validate(card_dict_with_sig)

    with pytest.raises(AgentCardSignatureError):
        await verify_agent_card(card, tampered, mode="soft", trusted_keys=[key])


async def test_strict_mode_rejects_unsigned_card() -> None:
    card_dict = _minimal_card_dict()
    card = v10.AgentCard.model_validate(card_dict)
    assert not card.signatures
    raw_body = bytes(rfc8785.dumps(card_dict))

    with pytest.raises(AgentCardSignatureError, match="no signatures"):
        await verify_agent_card(card, raw_body, mode="strict")


async def test_soft_mode_allows_unsigned_card() -> None:
    card_dict = _minimal_card_dict()
    card = v10.AgentCard.model_validate(card_dict)
    raw_body = bytes(rfc8785.dumps(card_dict))

    # Should NOT raise — only warn.
    await verify_agent_card(card, raw_body, mode="soft")


async def test_off_mode_skips_everything() -> None:
    card_dict = _minimal_card_dict()
    card = v10.AgentCard.model_validate(card_dict)
    # Mode "off" must never raise, even with garbage bytes.
    await verify_agent_card(card, b"garbage", mode="off")


async def test_unknown_kid_without_jku_raises() -> None:
    key = _make_rsa_key(kid="signing-key")
    card_dict = _minimal_card_dict()
    raw_body, sig = _sign_card(card_dict, key)

    card_dict_with_sig = dict(card_dict)
    card_dict_with_sig["signatures"] = [
        sig.model_dump(mode="json", by_alias=True, exclude_none=True)
    ]
    card = v10.AgentCard.model_validate(card_dict_with_sig)

    # Pass a different key — verification should fail because kid doesn't match
    # any trusted key and no jku is allowed.
    other_key = _make_rsa_key(kid="other-key")
    with pytest.raises(AgentCardSignatureError):
        await verify_agent_card(
            card,
            raw_body,
            mode="strict",
            trusted_keys=[other_key],
            allow_jku=False,
        )


def _sign_card_with_jku(
    card_dict: dict[str, Any], key: jwk.JWK, jku: str
) -> tuple[bytes, v10.AgentCard]:
    """Sign the card with a ``jku`` header; return (raw_body, parsed card)."""
    payload = bytes(rfc8785.dumps({k: v for k, v in card_dict.items() if k != "signatures"}))

    protected = {"alg": "RS256", "kid": key.kid, "jku": jku}
    sig_obj = jws.JWS(payload)
    sig_obj.add_signature(key, alg="RS256", protected=json.dumps(protected))
    serialized = json.loads(sig_obj.serialize())
    sig = v10.AgentCardSignature(
        protected=serialized["protected"],
        signature=serialized["signature"],
    )

    card_dict_with_sig = dict(card_dict)
    card_dict_with_sig["signatures"] = [
        sig.model_dump(mode="json", by_alias=True, exclude_none=True)
    ]
    card = v10.AgentCard.model_validate(card_dict_with_sig)
    return bytes(rfc8785.dumps(card_dict_with_sig)), card


async def test_jku_host_allowlist_enforced() -> None:
    """Signature with jku pointing outside the allowlist must be rejected."""
    key = _make_rsa_key()
    raw_body, card = _sign_card_with_jku(
        _minimal_card_dict(), key, "https://evil.example/jwks.json"
    )

    # trusted_keys has no match → falls through to jku → host blocked.
    with pytest.raises(AgentCardSignatureError, match="not in allowed_jku_hosts"):
        await verify_agent_card(
            card,
            raw_body,
            mode="strict",
            trusted_keys=[_make_rsa_key(kid="other")],
            allow_jku=True,
            allowed_jku_hosts={"trusted.example"},
        )


async def test_jku_ignored_without_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """With allowed_jku_hosts=None (default), jku is never fetched.

    The jku URL is attacker-controlled — without an explicit allowlist the
    key must come from trusted_keys, and no HTTP request may happen.
    """
    import httpx

    def _no_fetch(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("jku fetch attempted without allowed_jku_hosts")

    monkeypatch.setattr(httpx, "AsyncClient", _no_fetch)

    key = _make_rsa_key()
    raw_body, card = _sign_card_with_jku(
        _minimal_card_dict(), key, "https://attacker.example/jwks.json"
    )

    with pytest.raises(AgentCardSignatureError, match="No signature key resolvable"):
        await verify_agent_card(
            card,
            raw_body,
            mode="strict",
            trusted_keys=None,
            allow_jku=True,
            allowed_jku_hosts=None,
        )


async def test_jku_honored_with_matching_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    """jku fetch happens (and verifies) when the host is explicitly allowlisted."""
    import httpx

    key = _make_rsa_key()
    jwks_json = json.dumps({"keys": [json.loads(key.export_public())]})

    class _FakeResponse:
        text = jwks_json

        def raise_for_status(self) -> None:
            pass

    class _FakeAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def get(self, url: str) -> _FakeResponse:
            assert url == "https://trusted.example/jwks.json"
            return _FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    raw_body, card = _sign_card_with_jku(
        _minimal_card_dict(), key, "https://trusted.example/jwks.json"
    )

    # Should not raise — key resolved via the allowlisted jku.
    await verify_agent_card(
        card,
        raw_body,
        mode="strict",
        trusted_keys=None,
        allow_jku=True,
        allowed_jku_hosts={"trusted.example"},
    )
