# Storage

Storage is the authoritative persistence layer for tasks, artifacts, and messages. It handles CRUD operations and enforces data-integrity constraints.

## Storage ABC

::: a2akit.storage.base.Storage
    options:
      members:
        - load_task
        - create_task
        - update_task
        - list_tasks
        - delete_task
        - delete_context
        - get_version
        - load_context
        - update_context

### Required Methods

Subclasses MUST implement these three methods:

#### `load_task(task_id, history_length=None, *, include_artifacts=True) -> Task | None`

Load a task by ID. Returns `None` if not found. `history_length` limits the number of history messages returned.

#### `create_task(context_id, message, *, idempotency_key=None) -> Task`

Create a new task from an initial message. When `idempotency_key` is provided with a matching `context_id`, returns the existing task instead of creating a duplicate.

#### `update_task(task_id, state=None, *, status_message=None, artifacts=None, messages=None, task_metadata=None, expected_version=None) -> int`

Persist state changes, artifacts, and messages atomically. Returns the new version number.

### Optional Methods

These have sensible defaults but can be overridden:

| Method | Default | Description |
|--------|---------|-------------|
| `list_tasks(query)` | Returns empty | Filtered and paginated task listing |
| `delete_task(task_id)` | `NotImplementedError` | Delete a task |
| `delete_context(context_id)` | `NotImplementedError` | Delete all tasks in a context |
| `get_version(task_id)` | `None` | Current OCC version |
| `load_context(context_id)` | `None` | Load stored conversation context |
| `update_context(context_id, context)` | No-op | Store conversation context |

## InMemoryStorage

The default storage backend for development. Stores everything in Python dictionaries.

```python
from a2akit import A2AServer, InMemoryStorage

server = A2AServer(
    worker=MyWorker(),
    agent_card=AgentCardConfig(...),
    storage="memory",  # equivalent to InMemoryStorage()
)
```

!!! warning "Not for production"
    `InMemoryStorage` loses all data on restart and doesn't support multi-process deployments. Use PostgreSQL or SQLite for production.

## PostgreSQLStorage

Persistent storage using PostgreSQL via SQLAlchemy + asyncpg.

```bash
pip install a2akit[postgres]
```

```python
server = A2AServer(
    worker=MyWorker(),
    agent_card=AgentCardConfig(...),
    storage="postgresql+asyncpg://user:pass@localhost/mydb",
)
```

## SQLiteStorage

Persistent storage using SQLite via SQLAlchemy + aiosqlite.

```bash
pip install a2akit[sqlite]
```

```python
server = A2AServer(
    worker=MyWorker(),
    agent_card=AgentCardConfig(...),
    storage="sqlite+aiosqlite:///tasks.db",
)
```

## ArtifactWrite

Per-artifact write descriptor with individual append semantics.

```python
@dataclass(frozen=True)
class ArtifactWrite:
    artifact: Artifact
    append: bool = False
```

Each `ArtifactWrite` carries its own `append` flag, allowing callers to mix append and replace operations in a single `update_task` call.

## Data Integrity

### Terminal-State Guard

Storage MUST reject state transitions on tasks in terminal states (`completed`, `canceled`, `failed`, `rejected`) by raising `TaskTerminalStateError`. This prevents concurrent writers from corrupting terminal states.

### Optimistic Concurrency Control

When `expected_version` is provided to `update_task`, Storage MUST verify it matches the stored version. On mismatch, raise `ConcurrencyError`.

### Consistency Requirement

Implementations MUST provide read-your-writes consistency. A `load_task()` call following `update_task()` on the same connection MUST reflect the preceding write.

## Exceptions

| Exception | Description |
|-----------|-------------|
| `TaskNotFoundError` | Referenced task does not exist |
| `TaskTerminalStateError` | Operation attempted on a terminal task |
| `TaskNotAcceptingMessagesError` | Task not in a state that accepts user input |
| `TaskNotCancelableError` | Cancel attempted on a terminal task |
| `ContextMismatchError` | Message contextId doesn't match task's contextId |
| `ConcurrencyError` | Expected version doesn't match stored version. Carries `current_version: int | None` from the already-loaded row to avoid extra SELECTs on retry. |
| `UnsupportedOperationError` | Operation not supported for current task state |

## ListTasksQuery

```python
class ListTasksQuery(BaseModel):
    context_id: str | None = None
    status: TaskState | None = None
    page_size: int = 50  # 1-100
    page_token: str | None = None
    history_length: int | None = None
    status_timestamp_after: str | None = None
    include_artifacts: bool = False
```

## ListTasksResult

```python
class ListTasksResult(BaseModel):
    tasks: list[Task] = []
    next_page_token: str = ""
    page_size: int = 50
    total_size: int = 0
```
