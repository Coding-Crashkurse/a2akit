# A2AServer & Config

## A2AServer

The high-level server class that wires all components together and produces a FastAPI app.

::: a2akit.server.A2AServer
    options:
      members:
        - __init__
        - as_fastapi_app

### Constructor Parameters

```python
server = A2AServer(
    worker=MyWorker(),
    agent_card=AgentCardConfig(
        name="My Agent",
        description="What your agent does.",
        version="0.1.0",
    ),
    middlewares=[SecretExtractor()],
    storage="memory",
    broker="memory",
    event_bus="memory",
    cancel_registry=None,
    blocking_timeout_s=30.0,
    cancel_force_timeout_s=60.0,
    max_concurrent_tasks=None,
    hooks=LifecycleHooks(...),
    settings=Settings(...),
    dependencies={...},
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `worker` | `Worker` | *required* | Your worker implementation |
| `agent_card` | `AgentCardConfig` | *required* | Agent discovery card configuration |
| `middlewares` | `list[A2AMiddleware]` | `[]` | Middleware pipeline |
| `storage` | `str \| Storage` | `"memory"` | Storage backend (`"memory"`, connection string, or instance) |
| `broker` | `str \| Broker` | `"memory"` | Broker backend (`"memory"` or instance) |
| `event_bus` | `str \| EventBus` | `"memory"` | Event bus backend (`"memory"` or instance) |
| `cancel_registry` | `CancelRegistry \| None` | `None` | Cancel registry (auto-created if None) |
| `blocking_timeout_s` | `float \| None` | `None` | Timeout for `message:send` blocking (falls back to Settings) |
| `cancel_force_timeout_s` | `float \| None` | `None` | Force-cancel timeout (falls back to Settings) |
| `max_concurrent_tasks` | `int \| None` | `None` | Worker parallelism limit |
| `hooks` | `LifecycleHooks \| None` | `None` | Lifecycle hook callbacks |
| `settings` | `Settings \| None` | `None` | Override default settings |
| `dependencies` | `dict[Any, Any] \| None` | `None` | Dependency injection registry |

### Storage Backend Resolution

The `storage` parameter accepts:

- `"memory"` — `InMemoryStorage` (default)
- `"postgresql+asyncpg://..."` — `PostgreSQLStorage` (requires `a2akit[postgres]`)
- `"sqlite+aiosqlite:///..."` — `SQLiteStorage` (requires `a2akit[sqlite]`)
- A `Storage` instance — used directly

### `as_fastapi_app(**fastapi_kwargs)`

Returns a fully configured `FastAPI` application with:

- All A2A protocol endpoints
- Agent card discovery endpoint
- Exception handlers for A2A errors
- Lifespan management (startup/shutdown of storage, broker, event bus, dependencies)

Extra keyword arguments are passed to the `FastAPI` constructor.

## AgentCardConfig

User-friendly configuration for the agent discovery card.

::: a2akit.agent_card.AgentCardConfig

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | `str` | *required* | Agent name |
| `description` | `str` | *required* | Agent description |
| `version` | `str` | `"1.0.0"` | Agent version |
| `protocol_version` | `str` | `"0.3.0"` | A2A protocol version |
| `skills` | `list[SkillConfig]` | `[]` | Agent skills |
| `extensions` | `list[ExtensionConfig]` | `[]` | Agent extensions |
| `capabilities` | `CapabilitiesConfig` | `CapabilitiesConfig()` | Agent capabilities (see below) |
| `protocol` | `Literal["jsonrpc", "http+json"]` | `"jsonrpc"` | Transport protocol |
| `input_modes` | `list[str]` | `["application/json", "text/plain"]` | Accepted input modes |
| `output_modes` | `list[str]` | `["application/json", "text/plain"]` | Produced output modes |

## CapabilitiesConfig

Declares which A2A protocol features this agent supports. All capabilities default to `False` (opt-in).

```python
from a2akit import CapabilitiesConfig

caps = CapabilitiesConfig(streaming=True)
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `streaming` | `bool` | `False` | Enable SSE streaming (`message:stream`, `tasks:subscribe`) |
| `state_transition_history` | `bool` | `False` | Advertise state transition history in Agent Card (transitions are always recorded) |
| `push_notifications` | `bool` | `False` | Push notifications (not yet implemented, raises `NotImplementedError`) |
| `extended_agent_card` | `bool` | `False` | Extended agent card (not yet implemented, raises `NotImplementedError`) |
| `extensions` | `list[AgentExtension] \| None` | `None` | Protocol extensions (not yet implemented, raises `NotImplementedError`) |

!!! warning "Streaming is opt-in"
    Since v0.0.7, streaming defaults to `False`. Agents that use streaming must explicitly enable it:

    ```python
    AgentCardConfig(
        name="My Agent",
        description="...",
        version="0.1.0",
        capabilities=CapabilitiesConfig(streaming=True),
    )
    ```

    Without this, the server rejects streaming requests with `UnsupportedOperationError`, and the client raises `AgentCapabilityError` before making the request.

## SkillConfig

```python
from a2akit import SkillConfig

skill = SkillConfig(
    id="translate",
    name="Translation",
    description="Translates text between languages",
    tags=["translation", "nlp"],
    examples=["Translate 'hello' to French"],
)
```

## ExtensionConfig

```python
from a2akit import ExtensionConfig

ext = ExtensionConfig(
    uri="urn:a2a:ext:custom-feature",
    description="Custom extension",
    params={"setting": "value"},
)
```

## Settings

::: a2akit.config.Settings

See [Configuration](../configuration.md) for full details on environment variables and overrides.
