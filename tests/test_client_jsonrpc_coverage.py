"""Coverage tests for the JSON-RPC client transport (push config + subscribe).

These methods on ``JsonRpcTransport`` are not exercised by the protocol-agnostic
integration tests, which only cover ``send`` / ``stream`` / ``get_task`` /
``cancel_task``. The uncovered branches concern push-notification config CRUD
and ``tasks/resubscribe`` streaming — both user-visible features that should
not regress silently.
"""

from __future__ import annotations

import httpx
import pytest
from asgi_lifespan import LifespanManager

from a2akit import A2AServer, AgentCardConfig, CapabilitiesConfig, TaskContext, Worker
from a2akit.client import A2AClient


class _EchoWorker(Worker):
    async def handle(self, ctx: TaskContext) -> None:
        await ctx.complete(f"Echo: {ctx.user_text}")


def _make_push_app():
    server = A2AServer(
        worker=_EchoWorker(),
        agent_card=AgentCardConfig(
            name="Push Agent",
            description="push test",
            version="0.0.1",
            protocol="jsonrpc",
            capabilities=CapabilitiesConfig(
                streaming=True,
                push_notifications=True,
            ),
        ),
        push_allow_http=True,
    )
    return server.as_fastapi_app()


@pytest.fixture
async def jsonrpc_push_client():
    app = _make_push_app()
    manager = LifespanManager(app)
    await manager.__aenter__()
    http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=manager.app),
        base_url="http://test",
    )
    client = A2AClient("http://test", httpx_client=http_client, protocol="jsonrpc")
    await client.connect()
    yield client
    await client.close()
    await http_client.aclose()
    await manager.__aexit__(None, None, None)


async def test_push_config_crud_over_jsonrpc(jsonrpc_push_client):
    """set / get / list / delete push config round-trip via JSON-RPC transport."""
    # Need a task id — create one via send().
    result = await jsonrpc_push_client.send("hi", blocking=True)
    task_id = result.task_id
    assert task_id is not None

    # SET — exercises JsonRpcTransport.set_push_config
    set_result = await jsonrpc_push_client.set_push_config(
        task_id,
        url="http://example.com/webhook",
        token="t",
        config_id="cfg-1",
    )
    assert set_result is not None

    # GET by id — exercises JsonRpcTransport.get_push_config
    got = await jsonrpc_push_client.get_push_config(task_id, "cfg-1")
    assert got is not None

    # LIST — exercises JsonRpcTransport.list_push_configs
    configs = await jsonrpc_push_client.list_push_configs(task_id)
    assert isinstance(configs, list)
    assert len(configs) >= 1

    # DELETE — exercises JsonRpcTransport.delete_push_config
    await jsonrpc_push_client.delete_push_config(task_id, "cfg-1")

    # After delete the list should be empty
    configs_after = await jsonrpc_push_client.list_push_configs(task_id)
    assert len(configs_after) == 0


async def test_subscribe_terminal_task_raises_over_jsonrpc(jsonrpc_push_client):
    """Subscribe to a completed task via JSON-RPC — server rejects with -32004.

    Exercises ``JsonRpcTransport.subscribe_task`` up to and including the
    non-SSE error-response branch (``_handle_response``), which maps the
    spec's -32004 code to ``TaskTerminalError``.
    """
    from a2akit.client.errors import TaskTerminalError

    result = await jsonrpc_push_client.send("hello", blocking=True)
    task_id = result.task_id
    assert task_id is not None

    with pytest.raises(TaskTerminalError):
        async for _ev in jsonrpc_push_client.subscribe(task_id):
            pass


async def test_get_push_config_without_id_over_jsonrpc(jsonrpc_push_client):
    """get_push_config with no config_id returns the default/first config.

    Exercises the ``if config_id:`` branch in
    ``JsonRpcTransport.get_push_config``.
    """
    result = await jsonrpc_push_client.send("hi", blocking=True)
    task_id = result.task_id
    assert task_id is not None

    await jsonrpc_push_client.set_push_config(task_id, url="http://example.com/w", token="x")
    # No config_id parameter — exercises the default branch.
    cfg = await jsonrpc_push_client.get_push_config(task_id)
    assert cfg is not None
