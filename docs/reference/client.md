# A2AClient

Dev-first client for interacting with A2A agents. Auto-discovers capabilities, detects protocol, and provides high-level methods for send, stream, and task management.

## Quick Start

```python
from a2akit import A2AClient

async with A2AClient("http://localhost:8000") as client:
    result = await client.send("Hello!")
    print(result.text)
```

## Constructor

```python
A2AClient(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
    protocol: str | None = None,
    httpx_client: httpx.AsyncClient | None = None,
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `url` | `str` | required | Base URL of the agent (e.g. `http://localhost:8000`) |
| `headers` | `dict` | `None` | Extra HTTP headers for all requests |
| `timeout` | `float` | `30.0` | HTTP timeout in seconds |
| `protocol` | `str` | `None` | Force protocol (`"jsonrpc"` or `"http+json"`). Auto-detected from agent card if `None` |
| `httpx_client` | `AsyncClient` | `None` | Bring your own httpx client. Lifecycle is managed externally |

## Lifecycle

Use as an async context manager:

```python
async with A2AClient("http://localhost:8000") as client:
    ...
```

Or manage manually:

```python
client = A2AClient("http://localhost:8000")
await client.connect()
# ...
await client.close()
```

## Methods

### `send(text, *, task_id, context_id, blocking, metadata) -> ClientResult`

Send a text message. Returns a `ClientResult` with the agent's response.

```python
result = await client.send("Hello!")
print(result.text)
print(result.state)     # "completed", "failed", etc.
print(result.task_id)
```

### `send_parts(parts, *, task_id, context_id, blocking, metadata) -> ClientResult`

Send raw `Part` objects (text, data, files).

### `stream(text, *, task_id, context_id, metadata) -> AsyncIterator[StreamEvent]`

Stream a message, yielding `StreamEvent` objects with full event details.

!!! note "Requires streaming capability"
    Raises `AgentCapabilityError` if the agent does not support streaming.

```python
async for event in client.stream("Hello"):
    if event.kind == "artifact":
        print(event.text, end="")
    if event.is_final:
        print(f"\nFinal state: {event.state}")
```

### `stream_text(text, *, task_id, context_id, metadata) -> AsyncIterator[str]`

Stream only text content as plain strings.

```python
async for chunk in client.stream_text("Hello"):
    print(chunk, end="")
```

### `get_task(task_id, *, history_length) -> ClientResult`

Fetch a task by ID.

### `list_tasks(*, context_id, status, page_size, page_token, history_length) -> ListResult`

List tasks with optional filters and pagination.

### `cancel(task_id) -> ClientResult`

Cancel a task.

### `subscribe(task_id) -> AsyncIterator[StreamEvent]`

Subscribe to updates for an existing task.

!!! note "Requires streaming capability"
    Raises `AgentCapabilityError` if the agent does not support streaming.

### `set_push_config(task_id, *, url, token, config_id, authentication) -> dict`

Set a push notification config for a task.

```python
await client.set_push_config(
    task_id,
    url="https://my-app.com/webhook",
    token="my-secret",
)
```

### `get_push_config(task_id, config_id) -> dict`

Get a push notification config.

### `list_push_configs(task_id) -> list[dict]`

List all push configs for a task.

### `delete_push_config(task_id, config_id) -> None`

Delete a push notification config.

### `get_extended_card() -> AgentCard`

Fetch the authenticated extended agent card. Returns a full `AgentCard` with potentially richer information (e.g., premium skills) based on the caller's authentication.

```python
extended = await client.get_extended_card()
print(f"Extended skills: {len(extended.skills)}")
for skill in extended.skills:
    print(f"  - {skill.name}")
```

!!! note "Requires `supportsAuthenticatedExtendedCard`"
    Returns 404 (REST) or error code `-32007` (JSON-RPC) if the agent has not configured an `extended_card_provider`.

## Properties

| Property | Type | Description |
|----------|------|-------------|
| `agent_card` | `AgentCard` | The discovered agent card |
| `agent_name` | `str` | Shortcut for `agent_card.name` |
| `capabilities` | `AgentCapabilities \| None` | Shortcut for `agent_card.capabilities` |
| `protocol` | `str` | Active protocol (`"jsonrpc"` or `"http+json"`) |
| `is_connected` | `bool` | Connection status |

## ClientResult

Primary return type for non-streaming operations.

| Field | Type | Description |
|-------|------|-------------|
| `task_id` | `str` | Task identifier |
| `context_id` | `str \| None` | Context identifier |
| `state` | `str` | Task state (`"completed"`, `"failed"`, etc.) |
| `text` | `str \| None` | Extracted text from artifacts or status |
| `data` | `dict \| None` | Extracted data from first data artifact |
| `artifacts` | `list[ArtifactInfo]` | All artifacts |
| `raw_task` | `Task \| None` | Original Task object |
| `raw_message` | `Message \| None` | Original Message (direct replies) |

Convenience properties: `completed`, `failed`, `input_required`, `auth_required`, `canceled`, `rejected`, `is_terminal`.

## StreamEvent

Yielded during streaming operations.

| Field | Type | Description |
|-------|------|-------------|
| `kind` | `str` | `"task"`, `"status"`, or `"artifact"` |
| `state` | `str \| None` | Current task state |
| `text` | `str \| None` | Text content |
| `data` | `dict \| None` | Data content |
| `artifact_id` | `str \| None` | Artifact identifier |
| `is_final` | `bool` | Whether this is the final event |

## Errors

| Error | When |
|-------|------|
| `AgentNotFoundError` | Agent unreachable or invalid agent card |
| `AgentCapabilityError` | Streaming called on non-streaming agent |
| `NotConnectedError` | Method called before `connect()` |
| `TaskNotFoundError` | Task ID not found |
| `TaskNotCancelableError` | Task cannot be canceled |
| `TaskTerminalError` | Task is in a terminal state |
| `ProtocolError` | Transport-level error |
