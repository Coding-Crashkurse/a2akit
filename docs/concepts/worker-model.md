# Worker Model

The Worker is the only class you implement to build an A2A agent. Everything else is handled by the framework.

## The Worker ABC

```python
from a2akit import Worker, TaskContext


class MyWorker(Worker):
    async def handle(self, ctx: TaskContext) -> None:
        await ctx.complete(f"Hello, {ctx.user_text}!")
```

The `Worker` abstract base class has a single required method: `handle(ctx)`. That's it. No event queues, no state machines, no protocol wiring.

## The Contract

Your `handle()` method **MUST** call exactly one lifecycle method before returning:

| Method | Effect |
|--------|--------|
| `ctx.complete(text?)` | Mark task completed with optional text artifact |
| `ctx.complete_json(data)` | Complete with a JSON data artifact |
| `ctx.respond(text?)` | Complete with a status message (no artifact) |
| `ctx.reply_directly(text)` | Return a Message directly without task tracking |
| `ctx.fail(reason)` | Mark task failed |
| `ctx.reject(reason?)` | Reject the task |
| `ctx.request_input(question)` | Ask user for more input |
| `ctx.request_auth(details?)` | Request authentication |

!!! warning "Auto-fail on missing lifecycle call"
    If your `handle()` method returns without calling any lifecycle method, the framework automatically marks the task as failed with the message: *"Worker returned without calling a lifecycle method."*

## TaskContext

`TaskContext` is the single interface your worker interacts with. It abstracts away all A2A protocol details — storage writes, event broadcasting, artifact serialization, state transitions.

### Properties

| Property | Type | Description |
|----------|------|-------------|
| `ctx.user_text` | `str` | The user's input as plain text |
| `ctx.parts` | `list[Any]` | Raw message parts (text, files, etc.) |
| `ctx.files` | `list[FileInfo]` | File parts with content, url, filename, media_type |
| `ctx.data_parts` | `list[dict]` | Structured data parts |
| `ctx.task_id` | `str` | Current task UUID |
| `ctx.context_id` | `str \| None` | Conversation / context identifier |
| `ctx.message_id` | `str` | ID of the triggering message |
| `ctx.metadata` | `dict[str, Any]` | Persisted metadata from the request |
| `ctx.request_context` | `dict[str, Any]` | Transient data from middleware (never persisted) |
| `ctx.is_cancelled` | `bool` | Whether cancellation was requested |
| `ctx.turn_ended` | `bool` | Whether a lifecycle method was called |
| `ctx.history` | `list[HistoryMessage]` | Previous messages in this task |
| `ctx.previous_artifacts` | `list[PreviousArtifact]` | Artifacts from prior turns |
| `ctx.deps` | `DependencyContainer` | Dependency container from the server |

### Output Negotiation

| Method | Description |
|--------|-------------|
| `ctx.accepts(mime_type)` | Check if client accepts the given MIME type (`True` when listed or no filter) |

### Streaming Methods

| Method | Description |
|--------|-------------|
| `ctx.send_status(msg)` | Emit intermediate status update |
| `ctx.emit_text_artifact(...)` | Emit a text artifact chunk |
| `ctx.emit_data_artifact(data)` | Emit a structured data artifact chunk |
| `ctx.emit_artifact(...)` | Emit an artifact with any content type |

### Context Methods

| Method | Description |
|--------|-------------|
| `ctx.load_context()` | Load stored context for this conversation |
| `ctx.update_context(data)` | Store context for this conversation |

For full API details, see the [TaskContext Reference](../reference/taskcontext.md).

## Worker Testing

Workers are easy to test because they only depend on `TaskContext`. In tests, you can use the real framework with `InMemoryStorage`:

```python
import pytest
from a2akit import A2AServer, AgentCardConfig
from httpx import ASGITransport, AsyncClient
from asgi_lifespan import LifespanManager


@pytest.fixture
async def client():
    server = A2AServer(
        worker=MyWorker(),
        agent_card=AgentCardConfig(
            name="Test", description="Test agent", version="0.1.0"
        ),
    )
    app = server.as_fastapi_app()
    async with LifespanManager(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as c:
            yield c


async def test_echo(client):
    resp = await client.post(
        "/v1/message:send",
        json={
            "message": {
                "role": "user",
                "parts": [{"text": "hello"}],
                "messageId": "test-1",
            }
        },
    )
    assert resp.status_code == 200
    assert "hello" in resp.json()["artifacts"][0]["parts"][0]["text"]
```
