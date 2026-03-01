"""Test for the health check endpoint."""

from __future__ import annotations


async def test_health_check(client):
    """GET /v1/health returns 200 with status ok."""
    r = await client.get("/v1/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
