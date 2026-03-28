# Streaming

a2akit supports real-time streaming of artifacts and status updates via Server-Sent Events (SSE). This lets clients display partial results as they arrive — word by word, chunk by chunk.

## Example

```python
import asyncio
from a2akit import A2AServer, AgentCardConfig, CapabilitiesConfig, TaskContext, Worker


class StreamingWorker(Worker):
    async def handle(self, ctx: TaskContext) -> None:
        words = ctx.user_text.split()
        await ctx.send_status(f"Streaming {len(words)} words...")  # (1)!

        for i, word in enumerate(words):
            is_last = i == len(words) - 1
            await ctx.emit_text_artifact(
                text=word + ("" if is_last else " "),
                artifact_id="stream",  # (2)!
                append=(i > 0),  # (3)!
                last_chunk=is_last,  # (4)!
            )
            await asyncio.sleep(0.1)

        await ctx.complete()  # (5)!


server = A2AServer(
    worker=StreamingWorker(),
    agent_card=AgentCardConfig(
        name="Streamer",
        description="Word-by-word streaming",
        version="0.1.0",
        capabilities=CapabilitiesConfig(streaming=True),  # (6)!
    ),
)
app = server.as_fastapi_app()
```

1. `send_status()` emits an intermediate status update. When a message is provided, it's persisted in `task.status.message` so polling clients also see it. In deferred-storage mode (streaming clients), the DB write is skipped — see [Deferred Storage](#deferred-storage-v0019) below.
2. All chunks with the same `artifact_id` belong to the same artifact.
3. `append=True` means this chunk extends the existing artifact rather than replacing it.
4. `last_chunk=True` signals that this artifact is complete.
5. `complete()` without text marks the task as completed without adding another artifact.
6. Streaming must be explicitly enabled via `CapabilitiesConfig(streaming=True)`. Without this, the server rejects streaming requests.

## Streaming Endpoints

Use `POST /v1/message:stream` to receive SSE events:

```bash
curl -N -X POST http://localhost:8000/v1/message:stream \
  -H "Content-Type: application/json" \
  -d '{"message":{"role":"user","parts":[{"text":"hello world"}],"messageId":"1"}}'
```

The response is a stream of SSE events:

1. **Task snapshot** — the initial task state
2. **Status updates** — `TaskStatusUpdateEvent` with `state: working`
3. **Artifact updates** — `TaskArtifactUpdateEvent` with partial content
4. **Final status** — `TaskStatusUpdateEvent` with `state: completed` and `final: true`

To subscribe to an existing task's events:

```bash
curl -N -X POST http://localhost:8000/v1/tasks/{task_id}:subscribe
```

## Streaming Methods

### `ctx.send_status(message)`

Emits an intermediate status update while the task stays in `working` state.

```python
await ctx.send_status("Processing step 1 of 3...")
```

When `message` is provided, it's persisted in Storage so polling clients can see progress. When `None`, only a bare working-state event is broadcast.

### `ctx.emit_text_artifact(text, *, artifact_id, append, last_chunk)`

Emits a single text chunk as an artifact update.

```python
await ctx.emit_text_artifact(
    text="Hello ",
    artifact_id="response",
    append=False,    # first chunk
    last_chunk=False,
)
await ctx.emit_text_artifact(
    text="world!",
    artifact_id="response",
    append=True,     # extends existing
    last_chunk=True, # signals completion
)
```

### `ctx.emit_data_artifact(data, *, artifact_id, media_type, append, last_chunk)`

Emits structured data as an artifact update.

```python
await ctx.emit_data_artifact(
    {"result": 42, "status": "ok"},
    artifact_id="analysis",
)
```

### `ctx.emit_artifact(...)`

The general-purpose method that supports text, data, file bytes, and file URLs in a single call.

```python
await ctx.emit_artifact(
    artifact_id="report",
    text="Report summary",
    name="Monthly Report",
    description="Generated analysis",
    last_chunk=True,
)
```

!!! tip "Artifact IDs"
    All chunks sharing the same `artifact_id` are grouped into one artifact. Use different IDs to emit multiple independent artifacts from the same worker.

!!! warning "Always call a lifecycle method"
    Streaming methods (`send_status`, `emit_text_artifact`, etc.) do **not** end the task. You must still call `ctx.complete()`, `ctx.fail()`, or another lifecycle method when done.

## Deferred Storage (v0.0.19+)

When a client uses `POST /v1/message:stream`, intermediate DB writes are automatically deferred. SSE subscribers receive every chunk in real-time via the EventBus, so periodic storage flushes are unnecessary overhead.

| Client endpoint | DB write strategy |
|-----------------|-------------------|
| `POST /v1/message:stream` (SSE) | **Deferred** — 1 atomic write at terminal state |
| `POST /v1/message:send` (blocking) | **Eager** — periodic flushes so polling clients see progress |

This is fully transparent to worker code — `emit_artifact()`, `send_status()`, and lifecycle methods work identically in both modes. The terminal method (`complete`, `fail`, `reject`, etc.) always persists the full state including all buffered artifacts.

!!! tip "Performance impact"
    A streaming task with 50 chunks drops from ~7–9 DB writes to exactly 1. For PostgreSQL, this also avoids repeated read-modify-write cycles on the JSON columns.
