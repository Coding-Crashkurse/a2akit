"""Integration tests for lifecycle hooks via the A2A HTTP API."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from a2a.types import TaskState
from asgi_lifespan import LifespanManager

from a2akit import A2AServer, AgentCardConfig, TaskContext, Worker
from a2akit.hooks import LifecycleHooks

_EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"


class EchoWorker(Worker):
    """Worker that echoes input."""

    async def handle(self, ctx: TaskContext) -> None:
        """Echo the user text back."""
        await ctx.complete(f"Echo: {ctx.user_text}")


class CrashWorker(Worker):
    """Worker that raises an exception."""

    async def handle(self, ctx: TaskContext) -> None:
        """Raise an unhandled exception."""
        raise RuntimeError("Worker crashed!")


class RejectWorker(Worker):
    """Worker that rejects tasks."""

    async def handle(self, ctx: TaskContext) -> None:
        """Reject with a reason."""
        await ctx.reject("Not my job")


class SlowWorker(Worker):
    """Worker that polls for cancellation and fails to complete if cancelled."""

    async def handle(self, ctx: TaskContext) -> None:
        """Poll for cancellation, then complete if not cancelled."""
        for _ in range(200):
            if ctx.is_cancelled:
                raise asyncio.CancelledError
            await asyncio.sleep(0.01)
        await ctx.complete("done")


class InputRequiredWorker(Worker):
    """Worker that requests additional input."""

    async def handle(self, ctx: TaskContext) -> None:
        """Request input on first turn, complete on follow-up."""
        if ctx.history:
            await ctx.complete(f"Got follow-up: {ctx.user_text}")
        else:
            await ctx.request_input("What is your name?")


class StatusUpdateWorker(Worker):
    """Worker that sends status updates before completing."""

    async def handle(self, ctx: TaskContext) -> None:
        """Send a status update then complete."""
        await ctx.send_status("Thinking...")
        await ctx.complete(f"Done: {ctx.user_text}")


def _make_send_params(
    text: str = "hello",
    task_id: str | None = None,
    context_id: str | None = None,
    blocking: bool = True,
) -> dict:
    """Create MessageSendParams dict."""
    msg: dict = {
        "role": "user",
        "messageId": str(uuid.uuid4()),
        "parts": [{"kind": "text", "text": text}],
    }
    if task_id:
        msg["taskId"] = task_id
    if context_id:
        msg["contextId"] = context_id
    body: dict = {"message": msg}
    if blocking:
        body["configuration"] = {"blocking": True}
    return body


async def _make_hooked_client(
    worker: Worker,
    hooks: LifecycleHooks,
) -> tuple[httpx.AsyncClient, LifespanManager]:
    """Create an httpx client with a hooked A2AServer."""
    server = A2AServer(
        worker=worker,
        agent_card=AgentCardConfig(
            name="Test Agent",
            description="Hook test agent",
            version="0.0.1",
        ),
        hooks=hooks,
    )
    app = server.as_fastapi_app()
    manager = await LifespanManager(app).__aenter__()
    transport = httpx.ASGITransport(app=manager.app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    return client, manager


@pytest.mark.asyncio
async def test_hook_fires_on_echo_worker_complete():
    """Full HTTP round-trip: send message -> EchoWorker completes -> hook fires with completed."""
    hook = AsyncMock()
    client, manager = await _make_hooked_client(EchoWorker(), LifecycleHooks(on_terminal=hook))

    try:
        resp = await client.post("/v1/message:send", json=_make_send_params(text="hi"))
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"]["state"] == "completed"

        hook.assert_awaited_once()
        call_args = hook.call_args
        assert call_args[0][0] == data["id"]  # task_id
        assert call_args[0][1] == TaskState.completed
    finally:
        await client.aclose()
        await manager.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_hook_fires_on_worker_crash():
    """Worker raises -> adapter marks failed -> hook fires with failed state."""
    hook = AsyncMock()
    client, manager = await _make_hooked_client(CrashWorker(), LifecycleHooks(on_terminal=hook))

    try:
        resp = await client.post("/v1/message:send", json=_make_send_params(text="crash"))
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"]["state"] == "failed"

        hook.assert_awaited_once()
        assert hook.call_args[0][1] == TaskState.failed
    finally:
        await client.aclose()
        await manager.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_hook_fires_on_worker_reject():
    """Worker calls reject() -> hook fires with rejected state."""
    hook = AsyncMock()
    client, manager = await _make_hooked_client(RejectWorker(), LifecycleHooks(on_terminal=hook))

    try:
        resp = await client.post("/v1/message:send", json=_make_send_params(text="reject"))
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"]["state"] == "rejected"

        hook.assert_awaited_once()
        assert hook.call_args[0][1] == TaskState.rejected
    finally:
        await client.aclose()
        await manager.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_hook_fires_on_cancel():
    """Cancel flow -> hook fires with canceled state."""
    hook = AsyncMock()
    client, manager = await _make_hooked_client(SlowWorker(), LifecycleHooks(on_terminal=hook))

    try:
        resp = await client.post(
            "/v1/message:send", json=_make_send_params(text="slow", blocking=False)
        )
        assert resp.status_code == 200
        task_id = resp.json()["id"]

        await asyncio.sleep(0.1)

        cancel_resp = await client.post(f"/v1/tasks/{task_id}:cancel")
        assert cancel_resp.status_code == 200

        await asyncio.sleep(0.3)

        assert hook.await_count >= 1
        states = [call[0][1] for call in hook.call_args_list]
        assert TaskState.canceled in states
    finally:
        await client.aclose()
        await manager.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_hook_fires_on_input_required():
    """Worker calls request_input() -> on_turn_end fires with input_required."""
    hook = AsyncMock()
    client, manager = await _make_hooked_client(
        InputRequiredWorker(), LifecycleHooks(on_turn_end=hook)
    )

    try:
        resp = await client.post("/v1/message:send", json=_make_send_params(text="first"))
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"]["state"] == "input-required"

        hook.assert_awaited_once()
        assert hook.call_args[0][1] == TaskState.input_required
    finally:
        await client.aclose()
        await manager.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_hook_fires_exactly_once_on_force_cancel():
    """Force cancel after worker completes -> hook fires once (for completed), not twice."""
    hook = AsyncMock()
    client, manager = await _make_hooked_client(EchoWorker(), LifecycleHooks(on_terminal=hook))

    try:
        resp = await client.post("/v1/message:send", json=_make_send_params(text="fast"))
        assert resp.status_code == 200
        task_id = resp.json()["id"]
        assert resp.json()["status"]["state"] == "completed"

        cancel_resp = await client.post(f"/v1/tasks/{task_id}:cancel")
        assert cancel_resp.status_code == 409

        await asyncio.sleep(0.1)

        assert hook.await_count == 1
        assert hook.call_args[0][1] == TaskState.completed
    finally:
        await client.aclose()
        await manager.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_state_change_hook_sees_all_transitions():
    """on_state_change records all states in order for a status-update worker."""
    hook = AsyncMock()
    client, manager = await _make_hooked_client(
        StatusUpdateWorker(), LifecycleHooks(on_state_change=hook)
    )

    try:
        resp = await client.post("/v1/message:send", json=_make_send_params(text="go"))
        assert resp.status_code == 200
        assert resp.json()["status"]["state"] == "completed"

        # Should have seen: working, working (status update), completed
        assert hook.await_count >= 3
        states = [call[0][1] for call in hook.call_args_list]
        assert states[0] == TaskState.working
        assert states[-1] == TaskState.completed
    finally:
        await client.aclose()
        await manager.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_all_hooks_fire_full_lifecycle():
    """Working -> status updates -> completed: all hooks fire at correct moments."""
    sc_hook = AsyncMock()
    w_hook = AsyncMock()
    t_hook = AsyncMock()
    hooks = LifecycleHooks(on_state_change=sc_hook, on_working=w_hook, on_terminal=t_hook)
    client, manager = await _make_hooked_client(StatusUpdateWorker(), hooks)

    try:
        resp = await client.post("/v1/message:send", json=_make_send_params(text="go"))
        assert resp.status_code == 200
        assert resp.json()["status"]["state"] == "completed"

        # on_state_change fires for every transition
        assert sc_hook.await_count >= 3

        # on_working fires for every working write (initial + status updates)
        assert w_hook.await_count >= 2

        # on_terminal fires once for completed
        t_hook.assert_awaited_once()
        assert t_hook.call_args[0][1] == TaskState.completed
    finally:
        await client.aclose()
        await manager.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_hook_printer_example_runs():
    """Import and run examples/hook_printer.py end-to-end."""
    import importlib.util
    import sys

    # Compute path at module level to avoid ASYNC240 (no I/O in async functions)
    example_path = str(_EXAMPLES_DIR / "hook_printer.py")
    spec = importlib.util.spec_from_file_location("hook_printer", example_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hook_printer"] = mod
    spec.loader.exec_module(mod)
    app = mod.app

    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/v1/message:send", json=_make_send_params(text="test"))
            assert resp.status_code == 200
            assert resp.json()["status"]["state"] == "completed"


@pytest.mark.asyncio
async def test_multiple_tasks_independent_hooks():
    """Two concurrent tasks -> each gets its own hook call."""
    hook = AsyncMock()
    client, manager = await _make_hooked_client(EchoWorker(), LifecycleHooks(on_terminal=hook))

    try:
        resp1 = await client.post("/v1/message:send", json=_make_send_params(text="one"))
        resp2 = await client.post("/v1/message:send", json=_make_send_params(text="two"))

        assert resp1.status_code == 200
        assert resp2.status_code == 200
        assert resp1.json()["status"]["state"] == "completed"
        assert resp2.json()["status"]["state"] == "completed"

        assert hook.await_count == 2
        task_ids = {call[0][0] for call in hook.call_args_list}
        assert resp1.json()["id"] in task_ids
        assert resp2.json()["id"] in task_ids
    finally:
        await client.aclose()
        await manager.__aexit__(None, None, None)
