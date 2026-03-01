"""Tests for artifact emission (text, data, file) and JSON completion."""

from __future__ import annotations

import uuid

import httpx
import pytest
from asgi_lifespan import LifespanManager

from conftest import ArtifactWorker, JsonCompleteWorker, _make_app


@pytest.fixture
async def artifact_client():
    app = _make_app(ArtifactWorker())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


@pytest.fixture
async def json_client():
    app = _make_app(JsonCompleteWorker())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def test_artifact_emit_types(artifact_client: httpx.AsyncClient):
    """ArtifactWorker should produce 3 artifacts: text, data, and file."""
    body = {
        "message": {
            "role": "user",
            "messageId": str(uuid.uuid4()),
            "parts": [{"kind": "text", "text": "emit artifacts"}],
        },
        "configuration": {"blocking": True},
    }
    resp = await artifact_client.post("/v1/message:send", json=body)
    assert resp.status_code == 200

    task = resp.json()
    assert task["status"]["state"] == "completed"

    artifacts = task.get("artifacts", [])
    assert len(artifacts) == 3

    artifact_ids = [a["artifactId"] for a in artifacts]
    assert "text-art" in artifact_ids
    assert "data-art" in artifact_ids
    assert "file-art" in artifact_ids


async def test_json_complete(json_client: httpx.AsyncClient):
    """JsonCompleteWorker should produce an artifact with data part containing expected JSON."""
    body = {
        "message": {
            "role": "user",
            "messageId": str(uuid.uuid4()),
            "parts": [{"kind": "text", "text": "json please"}],
        },
        "configuration": {"blocking": True},
    }
    resp = await json_client.post("/v1/message:send", json=body)
    assert resp.status_code == 200

    task = resp.json()
    assert task["status"]["state"] == "completed"

    artifacts = task.get("artifacts", [])
    assert len(artifacts) >= 1

    # Find the data part in the artifacts
    found_data = False
    for artifact in artifacts:
        for part in artifact.get("parts", []):
            if part.get("kind") == "data":
                assert part["data"] == {"result": "ok", "value": 42}
                found_data = True
    assert found_data, "No data part with expected JSON content found in artifacts"
