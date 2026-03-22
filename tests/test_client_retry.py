"""Tests for client retry logic."""

from __future__ import annotations

import pytest

from a2akit.client.base import A2AClient


@pytest.mark.asyncio
async def test_with_retry_succeeds_after_failure():
    """Retry logic succeeds on second attempt."""
    attempts = []

    async def factory():
        attempts.append(1)
        if len(attempts) < 2:
            raise ConnectionError("fail")
        return "ok"

    client = A2AClient.__new__(A2AClient)
    client._max_retries = 2
    client._retry_delay = 0.01
    client._retry_on = (ConnectionError,)

    result = await client._with_retry(factory)
    assert result == "ok"
    assert len(attempts) == 2


@pytest.mark.asyncio
async def test_with_retry_exhausted():
    """Retry logic raises after all attempts fail."""
    attempts = []

    async def factory():
        attempts.append(1)
        raise ConnectionError("fail")

    client = A2AClient.__new__(A2AClient)
    client._max_retries = 1
    client._retry_delay = 0.01
    client._retry_on = (ConnectionError,)

    with pytest.raises(ConnectionError):
        await client._with_retry(factory)
    assert len(attempts) == 2  # initial + 1 retry


@pytest.mark.asyncio
async def test_no_retry_raises_immediately():
    """With max_retries=0, raises on first failure."""
    attempts = []

    async def factory():
        attempts.append(1)
        raise ConnectionError("fail")

    client = A2AClient.__new__(A2AClient)
    client._max_retries = 0
    client._retry_delay = 0.01
    client._retry_on = (ConnectionError,)

    with pytest.raises(ConnectionError):
        await client._with_retry(factory)
    assert len(attempts) == 1
