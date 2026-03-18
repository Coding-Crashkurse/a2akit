# TaskContext API Reference

`TaskContext` is the execution context passed to `Worker.handle()`. It provides the complete interface for interacting with tasks — reading input, emitting results, controlling lifecycle, and accessing dependencies.

::: a2akit.worker.base.TaskContext
    options:
      members:
        - user_text
        - parts
        - files
        - data_parts
        - task_id
        - context_id
        - message_id
        - metadata
        - request_context
        - is_cancelled
        - turn_ended
        - history
        - previous_artifacts
        - deps
        - accepts
        - complete
        - complete_json
        - fail
        - reject
        - request_input
        - request_auth
        - respond
        - reply_directly
        - send_status
        - emit_artifact
        - emit_text_artifact
        - emit_data_artifact
        - load_context
        - update_context

## Properties

### Input

| Property | Type | Description |
|----------|------|-------------|
| `user_text` | `str` | The user's input as plain text (all text parts joined) |
| `parts` | `list[Any]` | Raw A2A message parts (text, files, data) |
| `files` | `list[FileInfo]` | File parts as typed wrappers |
| `data_parts` | `list[dict]` | Structured data parts extracted from the message |

### Identifiers

| Property | Type | Description |
|----------|------|-------------|
| `task_id` | `str` | Current task UUID |
| `context_id` | `str \| None` | Conversation / context identifier |
| `message_id` | `str` | ID of the triggering message |

### Metadata

| Property | Type | Description |
|----------|------|-------------|
| `metadata` | `dict[str, Any]` | Persisted metadata from `message.metadata` |
| `request_context` | `dict[str, Any]` | Transient data from middleware (never persisted) |

### State

| Property | Type | Description |
|----------|------|-------------|
| `is_cancelled` | `bool` | Whether cancellation was requested |
| `turn_ended` | `bool` | Whether a lifecycle method was called |

### History

| Property | Type | Description |
|----------|------|-------------|
| `history` | `list[HistoryMessage]` | Previous messages in this task |
| `previous_artifacts` | `list[PreviousArtifact]` | Artifacts from prior turns |

### Dependencies

| Property | Type | Description |
|----------|------|-------------|
| `deps` | `DependencyContainer` | Dependency container from the server |

## Output Negotiation

### `accepts(mime_type) -> bool`

Check whether the client accepts the given output MIME type. Returns `True` if the type is in `acceptedOutputModes`, or if no filter was specified (absent or empty means "accept everything"). Case-sensitive per RFC 2045.

```python
if ctx.accepts("application/json"):
    await ctx.complete_json({"revenue": 42000})
elif ctx.accepts("text/csv"):
    await ctx.complete(to_csv(data))
else:
    await ctx.complete("Revenue: 42,000 €")
```

## Lifecycle Methods

These methods transition the task to a new state. Exactly one must be called per `handle()` invocation.

### `complete(text=None, *, artifact_id="final-answer")`

Mark the task as **completed**. Optionally attach a text artifact.

```python
await ctx.complete("Here is your answer.")
await ctx.complete()  # completed without artifact
```

### `complete_json(data, *, artifact_id="final-answer")`

Complete with a JSON data artifact.

```python
await ctx.complete_json({"score": 0.95, "label": "positive"})
```

### `respond(text=None)`

Complete with a status message only — no artifact is created.

```python
await ctx.respond("Task acknowledged.")
```

### `reply_directly(text)`

Return a `Message` directly without task tracking. The HTTP response to `message:send` will be `{"message": {...}}` instead of `{"task": {...}}`.

```python
await ctx.reply_directly("Quick answer without task overhead.")
```

### `fail(reason)`

Mark the task as **failed**.

```python
await ctx.fail("External API returned 500.")
```

### `reject(reason=None)`

**Reject** the task — the agent decides not to perform it.

```python
await ctx.reject("I can only process English text.")
```

### `request_input(question)`

Transition to **input-required**. The client should send a follow-up message.

```python
await ctx.request_input("Which language should I translate to?")
```

### `request_auth(details=None)`

Transition to **auth-required** for secondary credentials.

```python
await ctx.request_auth("Please provide your GitHub token.")
```

## Streaming Methods

These methods emit events while the task stays in `working` state.

### `send_status(message=None)`

Emit an intermediate status update. When `message` is provided, it's persisted in `task.status.message`.

```python
await ctx.send_status("Processing 3 of 10 files...")
```

### `emit_text_artifact(text, *, artifact_id="answer", append=False, last_chunk=False)`

Emit a text chunk as an artifact update.

### `emit_data_artifact(data, *, artifact_id="answer", media_type="application/json", append=False, last_chunk=False)`

Emit structured data as an artifact update.

### `emit_artifact(*, artifact_id, text=None, data=None, file_bytes=None, file_url=None, media_type=None, filename=None, name=None, description=None, append=False, last_chunk=False, metadata=None)`

General-purpose artifact emission supporting text, data, file bytes, and file URLs.

## Context Methods

### `load_context()`

Load stored context for this task's `context_id`. Returns `None` if no context exists or `context_id` is `None`.

### `update_context(context)`

Store context for this task's `context_id`. No-op if `context_id` is `None`.

## Helper Types

### FileInfo

```python
@dataclass(frozen=True)
class FileInfo:
    content: bytes | None
    url: str | None
    filename: str | None
    media_type: str | None
```

### HistoryMessage

```python
@dataclass(frozen=True)
class HistoryMessage:
    role: str
    text: str
    parts: list[Any]
    message_id: str
```

### PreviousArtifact

```python
@dataclass(frozen=True)
class PreviousArtifact:
    artifact_id: str
    name: str | None
    parts: list[Any]
```
