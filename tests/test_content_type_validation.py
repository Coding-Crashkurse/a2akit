"""Tests for Content-Type request validation (Spec §3.2 MUST)."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_post_with_json_content_type(client, make_send_params):
    """POST with application/json Content-Type succeeds."""
    body = make_send_params(text="hello")
    resp = await client.post(
        "/v1/message:send",
        json=body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_post_with_xml_content_type(client, make_send_params):
    """POST with text/xml Content-Type returns 415."""
    import json

    body = make_send_params(text="hello")
    resp = await client.post(
        "/v1/message:send",
        content=json.dumps(body),
        headers={"Content-Type": "text/xml"},
    )
    assert resp.status_code == 415
    data = resp.json()
    assert data["error"]["code"] == -32600


@pytest.mark.asyncio
async def test_post_with_charset_accepted(client, make_send_params):
    """POST with application/json; charset=utf-8 succeeds."""
    body = make_send_params(text="hello")
    resp = await client.post(
        "/v1/message:send",
        json=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_get_agent_card_exempt(client):
    """GET agent card without Content-Type succeeds (exempt)."""
    resp = await client.get("/.well-known/agent-card.json")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_get_health_exempt(client):
    """GET health without Content-Type succeeds (exempt)."""
    resp = await client.get("/v1/health")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_post_cancel_no_body_exempt(client):
    """POST cancel without body passes through (no content-length)."""
    resp = await client.post("/v1/tasks/nonexistent:cancel")
    # 404 = task not found, not 415 = content-type error
    assert resp.status_code == 404
