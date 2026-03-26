"""Client example — validate the agent card before connecting.

Demonstrates the ``card_validator`` hook on ``A2AClient``.  Three
validators are shown, from simplest to most advanced:

  1. **Name allowlist** — reject agents not on a trusted list.
  2. **Provider domain check** — require a specific provider URL.
  3. **Signature presence check** — ensure the card carries at least
     one JWS signature (actual cryptographic verification is left to
     the user; see the docstring on ``require_signatures``).

Start the server first:
    uvicorn examples.card_validator.server:app

Then run this client:
    python -m examples.card_validator.client
"""

import asyncio

from a2akit import A2AClient

TRUSTED = {"Signed Echo Agent", "Internal Router"}


def check_allowlist(card, raw_body):
    """Reject agents whose name is not in the trusted set."""
    if card.name not in TRUSTED:
        raise ValueError(f"Untrusted agent: {card.name}")


def check_provider(card, raw_body):
    """Require the agent's provider URL to contain a given domain."""
    if card.provider is None or "example.com" not in card.provider.url:
        raise ValueError("Agent must be provided by example.com")


def require_signatures(card, raw_body):
    """Ensure the card has at least one JWS signature.

    This check only verifies *presence*.  For real verification you
    would decode ``sig.protected`` to extract the ``alg``, ``kid``, and
    ``jku`` fields, fetch the JWK set, and verify the detached JWS
    against ``raw_body``.  That requires a library like ``jwcrypto``
    and enterprise-specific trust decisions (allowed jku domains, key
    rotation policy, etc.), which is why a2akit leaves it to you.
    """
    if not card.signatures:
        raise ValueError("Agent card has no JWS signatures")
    print(f"  Card has {len(card.signatures)} signature(s) — OK")


async def main() -> None:
    url = "http://localhost:8000"

    # 1. Allowlist validator — should pass for "Signed Echo Agent"
    print("1) Allowlist validator:")
    async with A2AClient(url, card_validator=check_allowlist) as client:
        print(f"   Connected to: {client.agent_name}")
        result = await client.send("Hello from allowlist check")
        print(f"   Response: {result.text}")

    # 2. Signature-presence validator
    print("\n2) Signature-presence validator:")
    async with A2AClient(url, card_validator=require_signatures) as client:
        print(f"   Connected to: {client.agent_name}")
        result = await client.send("Hello from signature check")
        print(f"   Response: {result.text}")

    # 3. Provider check — will fail because the demo server has no provider
    print("\n3) Provider-domain validator (expected to fail):")
    try:
        async with A2AClient(url, card_validator=check_provider) as client:
            print(f"   Connected to: {client.agent_name}")
    except ValueError as exc:
        print(f"   Rejected as expected: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
