"""Redis-backed storage backend for distributed deployments."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Self

from a2a.types import Artifact, Message, Task, TaskState, TaskStatus

from a2akit.config import Settings, get_settings
from a2akit.storage.base import (
    TERMINAL_STATES,
    ArtifactWrite,
    ConcurrencyError,
    ContextT,
    ListTasksQuery,
    ListTasksResult,
    Storage,
    TaskNotFoundError,
    TaskTerminalStateError,
    _build_transition_record,
)

try:
    import redis.asyncio as aioredis
except ImportError as _import_error:
    raise ImportError(
        "Redis storage requires additional dependencies. "
        "Install them with: pip install a2akit[redis]"
    ) from _import_error

logger = logging.getLogger(__name__)

# Lua script for atomic update_task with OCC + terminal-state guard.
# KEYS[1] = task hash key
# ARGV[1] = expected_version ("" if not set)
# ARGV[2] = new state ("" if not changing)
# ARGV[3] = JSON-encoded values dict to HSET
# Returns: new version (int) or error string
_UPDATE_TASK_LUA = """
local key = KEYS[1]
local expected_version = ARGV[1]
local new_state = ARGV[2]
local values_json = ARGV[3]

-- Check task exists
local current_version = redis.call('HGET', key, 'version')
if not current_version then
    return redis.error_reply('TASK_NOT_FOUND')
end
current_version = tonumber(current_version)

-- OCC check
if expected_version ~= '' then
    if current_version ~= tonumber(expected_version) then
        return redis.error_reply('VERSION_MISMATCH:' .. tostring(current_version))
    end
end

-- Terminal state guard
if new_state ~= '' then
    local current_state = redis.call('HGET', key, 'status_state')
    if current_state == 'completed' or current_state == 'canceled'
       or current_state == 'failed' or current_state == 'rejected' then
        return redis.error_reply('TERMINAL_STATE:' .. current_state .. ':' .. new_state)
    end
end

-- Apply updates
local new_version = current_version + 1
local values = cjson.decode(values_json)
values['version'] = tostring(new_version)
local flat = {}
for k, v in pairs(values) do
    flat[#flat + 1] = k
    flat[#flat + 1] = v
end
if #flat > 0 then
    redis.call('HSET', key, unpack(flat))
end

return new_version
"""

# Lua script for atomic idempotent create.
# KEYS[1] = idempotency key (idem:{ctx}:{key})
# KEYS[2] = task hash key
# KEYS[3] = context set key
# ARGV[1] = task_id
# ARGV[2] = JSON-encoded hash fields
# Returns: task_id of existing or newly created task
_CREATE_IDEMPOTENT_LUA = """
local idem_key = KEYS[1]
local task_key = KEYS[2]
local ctx_set_key = KEYS[3]
local task_id = ARGV[1]
local fields_json = ARGV[2]

-- Check idempotency key
local existing = redis.call('GET', idem_key)
if existing then
    return existing
end

-- Create task
local fields = cjson.decode(fields_json)
local flat = {}
for k, v in pairs(fields) do
    flat[#flat + 1] = k
    flat[#flat + 1] = v
end
redis.call('HSET', task_key, unpack(flat))
redis.call('SADD', ctx_set_key, task_id)
redis.call('SET', idem_key, task_id, 'EX', 86400)

return task_id
"""

_TERMINAL_STATE_VALUES = {s.value for s in TERMINAL_STATES}


class RedisStorage(Storage[ContextT]):
    """Redis-backed storage for distributed multi-process deployments.

    Data model:
    - ``{prefix}task:{id}`` — Hash with task fields
    - ``{prefix}ctx:{context_id}`` — Set of task IDs in that context
    - ``{prefix}idem:{context_id}:{key}`` — Idempotency mapping
    - ``{prefix}context:{context_id}`` — Hash for user context data

    OCC is enforced atomically via Lua scripts.
    """

    def __init__(
        self,
        url: str | None = None,
        *,
        pool: aioredis.ConnectionPool | None = None,
        key_prefix: str | None = None,
        settings: Settings | None = None,
    ) -> None:
        s = settings or get_settings()
        self._key_prefix = key_prefix or s.redis_key_prefix
        self._owns_connection = pool is None
        self._url = url or s.redis_url
        self._pool = pool
        self._redis: aioredis.Redis | None = None
        self._update_script: Any = None
        self._create_idem_script: Any = None

    @property
    def _r(self) -> aioredis.Redis:
        if self._redis is None:
            raise RuntimeError("RedisStorage not connected — use 'async with storage' first")
        return self._redis

    async def __aenter__(self) -> Self:
        if self._pool is not None:
            self._redis = aioredis.Redis(connection_pool=self._pool)
        else:
            self._redis = aioredis.from_url(self._url)
        try:
            await self._redis.ping()
            # register_script() handles NOSCRIPT retry automatically after Redis restarts
            self._update_script = self._redis.register_script(_UPDATE_TASK_LUA)
            self._create_idem_script = self._redis.register_script(_CREATE_IDEMPOTENT_LUA)
        except Exception:
            if self._owns_connection:
                await self._redis.aclose()
            raise
        logger.info("Redis storage connected (prefix=%s)", self._key_prefix)
        return self

    async def __aexit__(self, *args: Any) -> bool:
        if self._redis and self._owns_connection:
            await self._redis.aclose()
        return False

    async def health_check(self) -> dict[str, Any]:
        """Ping Redis to verify connectivity."""
        try:
            if self._redis:
                await self._redis.ping()
            return {"status": "ok"}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    def _task_key(self, task_id: str) -> str:
        return f"{self._key_prefix}task:{task_id}"

    def _ctx_set_key(self, context_id: str) -> str:
        return f"{self._key_prefix}ctx:{context_id}"

    def _idem_key(self, context_id: str, idempotency_key: str) -> str:
        return f"{self._key_prefix}idem:{context_id}:{idempotency_key}"

    def _context_data_key(self, context_id: str) -> str:
        return f"{self._key_prefix}context:{context_id}"

    @staticmethod
    def _serialize_message(msg: Message | None) -> str | None:
        if msg is None:
            return None
        return msg.model_dump_json(by_alias=True, exclude_none=True)

    @staticmethod
    def _deserialize_message(data: str | None) -> Message | None:
        if not data:
            return None
        return Message.model_validate_json(data)

    @staticmethod
    def _serialize_messages(msgs: list[Message]) -> str:
        return json.dumps(
            [m.model_dump(mode="json", by_alias=True, exclude_none=True) for m in msgs]
        )

    @staticmethod
    def _deserialize_messages(data: str) -> list[Message]:
        raw = json.loads(data)
        return [Message.model_validate(m) for m in raw]

    @staticmethod
    def _serialize_artifacts(artifacts: list[Artifact]) -> str:
        return json.dumps(
            [a.model_dump(mode="json", by_alias=True, exclude_none=True) for a in artifacts]
        )

    @staticmethod
    def _deserialize_artifacts(data: str) -> list[Artifact]:
        raw = json.loads(data)
        return [Artifact.model_validate(a) for a in raw]

    def _hash_to_task(
        self,
        data: dict[str, str],
        history_length: int | None = None,
        include_artifacts: bool = True,
    ) -> Task:
        """Convert a Redis hash dict to a Task object."""
        history = self._deserialize_messages(data.get("history") or "[]")
        if history_length is not None:
            history = history[-history_length:] if history_length > 0 else []

        artifacts_list: list[Artifact] | None = None
        if include_artifacts:
            raw_artifacts = data.get("artifacts") or "[]"
            artifacts_list = self._deserialize_artifacts(raw_artifacts)

        metadata_raw = json.loads(data["metadata_json"]) if data.get("metadata_json") else None

        status = TaskStatus(
            state=TaskState(data["status_state"]),
            timestamp=data.get("status_timestamp", ""),
            message=self._deserialize_message(data.get("status_message")),
        )

        return Task(
            id=data["id"],
            context_id=data["context_id"],
            kind="task",
            status=status,
            history=history,
            artifacts=artifacts_list if artifacts_list else None,
            metadata=metadata_raw,
        )

    async def load_task(
        self,
        task_id: str,
        history_length: int | None = None,
        *,
        include_artifacts: bool = True,
    ) -> Task | None:
        data = await self._r.hgetall(self._task_key(task_id))
        if not data:
            return None
        # Decode bytes to str
        decoded = {k.decode(): v.decode() for k, v in data.items()}
        return self._hash_to_task(
            decoded, history_length=history_length, include_artifacts=include_artifacts
        )

    async def create_task(
        self,
        context_id: str,
        message: Message,
        *,
        idempotency_key: str | None = None,
    ) -> Task:
        task_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        history_msg = message.model_copy(update={"task_id": task_id, "context_id": context_id})

        initial_meta: dict[str, Any] = {}
        if idempotency_key:
            initial_meta["_idempotency_key"] = idempotency_key
        initial_meta["stateTransitions"] = [
            _build_transition_record(TaskState.submitted.value, now),
        ]

        fields: dict[str, str] = {
            "id": task_id,
            "context_id": context_id,
            "status_state": TaskState.submitted.value,
            "status_timestamp": now,
            "status_message": "",
            "history": self._serialize_messages([history_msg]),
            "artifacts": "[]",
            "metadata_json": json.dumps(initial_meta),
            "version": "1",
            "created_at": now,
        }

        if idempotency_key:
            # Atomic idempotent create via Lua (register_script handles NOSCRIPT)
            if self._create_idem_script is None:
                raise RuntimeError("RedisStorage not connected — Lua scripts not registered")
            result_id = await self._create_idem_script(
                keys=[
                    self._idem_key(context_id, idempotency_key),
                    self._task_key(task_id),
                    self._ctx_set_key(context_id),
                ],
                args=[task_id, json.dumps(fields)],
                client=self._r,
            )
            returned_id = result_id.decode() if isinstance(result_id, bytes) else result_id
            if returned_id != task_id:
                # Existing task found via idempotency key
                existing = await self.load_task(returned_id)
                if existing is None:
                    raise TaskNotFoundError(
                        f"Idempotent task {returned_id} vanished between create and load"
                    )
                return existing
        else:
            # Non-idempotent: just create
            await self._r.hset(self._task_key(task_id), mapping=fields)
            await self._r.sadd(self._ctx_set_key(context_id), task_id)

        loaded = await self.load_task(task_id)
        if loaded is None:
            raise TaskNotFoundError(f"Task {task_id} vanished immediately after create")
        return loaded

    async def update_task(
        self,
        task_id: str,
        state: TaskState | None = None,
        *,
        status_message: Message | None = None,
        artifacts: list[ArtifactWrite] | None = None,
        messages: list[Message] | None = None,
        task_metadata: dict[str, Any] | None = None,
        expected_version: int | None = None,
    ) -> int:
        # Build values to update — we need current data for merging
        current_data = await self._r.hgetall(self._task_key(task_id))
        if not current_data:
            raise TaskNotFoundError(f"Task {task_id} not found")

        decoded = {k.decode(): v.decode() for k, v in current_data.items()}

        values: dict[str, str] = {}

        if messages:
            existing_history = self._deserialize_messages(decoded.get("history") or "[]")
            existing_history.extend(messages)
            values["history"] = self._serialize_messages(existing_history)

        if artifacts:
            existing_artifacts = self._deserialize_artifacts(decoded.get("artifacts") or "[]")
            for aw in artifacts:
                existing_artifacts = self._apply_artifact(
                    existing_artifacts, aw.artifact, append=aw.append
                )
            values["artifacts"] = self._serialize_artifacts(existing_artifacts)

        if task_metadata:
            existing_meta = (
                json.loads(decoded["metadata_json"]) if decoded.get("metadata_json") else {}
            )
            existing_meta.update(task_metadata)
            values["metadata_json"] = json.dumps(existing_meta)

        if state is not None:
            ts = datetime.now(UTC).isoformat()
            values["status_state"] = state.value
            values["status_timestamp"] = ts
            values["status_message"] = self._serialize_message(status_message) or ""
            # Append state-transition record
            existing_meta = json.loads(
                values.get("metadata_json") or decoded.get("metadata_json") or "{}"
            )
            existing_meta.setdefault("stateTransitions", []).append(
                _build_transition_record(state.value, ts, status_message),
            )
            values["metadata_json"] = json.dumps(existing_meta)
        elif status_message is not None:
            # Update status message without a state transition (e.g. progress text)
            values["status_message"] = self._serialize_message(status_message) or ""
            values["status_timestamp"] = datetime.now(UTC).isoformat()

        try:
            if self._update_script is None:
                raise RuntimeError("RedisStorage not connected — Lua scripts not registered")
            new_version: int = await self._update_script(
                keys=[self._task_key(task_id)],
                args=[
                    str(expected_version) if expected_version is not None else "",
                    state.value if state is not None else "",
                    json.dumps(values),
                ],
                client=self._r,
            )
        except aioredis.ResponseError as e:
            err = str(e)
            if "TASK_NOT_FOUND" in err:
                raise TaskNotFoundError(f"Task {task_id} not found") from e
            if "VERSION_MISMATCH" in err:
                current = int(err.split(":")[-1])
                raise ConcurrencyError(
                    f"Version mismatch for task {task_id}: "
                    f"expected {expected_version}, current {current}",
                    current_version=current,
                ) from e
            if "TERMINAL_STATE" in err:
                parts = err.split(":")
                raise TaskTerminalStateError(
                    f"Cannot transition terminal task {task_id} from {parts[1]} to {parts[2]}"
                ) from e
            raise

        return int(new_version)

    @staticmethod
    def _apply_artifact(
        existing: list[Artifact], artifact: Artifact, *, append: bool
    ) -> list[Artifact]:
        idx = next(
            (i for i, a in enumerate(existing) if a.artifact_id == artifact.artifact_id),
            None,
        )
        if idx is not None:
            if append:
                existing[idx].parts.extend(artifact.parts)
            else:
                existing[idx] = artifact
        else:
            existing.append(artifact)
        return existing

    async def list_tasks(self, query: ListTasksQuery) -> ListTasksResult:
        # Determine candidate task IDs
        if query.context_id:
            task_ids_bytes = await self._r.smembers(self._ctx_set_key(query.context_id))
            task_ids = [tid.decode() for tid in task_ids_bytes]
        else:
            # Scan for all task keys
            task_ids_set: set[str] = set()
            pattern = f"{self._key_prefix}task:*"
            async for key in self._r.scan_iter(match=pattern, count=200):
                key_str = key.decode() if isinstance(key, bytes) else key
                task_ids_set.add(key_str[len(f"{self._key_prefix}task:") :])
            task_ids = list(task_ids_set)

        # Load and filter tasks
        filtered: list[Task] = []
        for tid in task_ids:
            data = await self._r.hgetall(self._task_key(tid))
            if not data:
                continue
            decoded = {k.decode(): v.decode() for k, v in data.items()}

            if query.status and decoded.get("status_state") != query.status.value:
                continue
            if query.status_timestamp_after and (
                decoded.get("status_timestamp", "") <= query.status_timestamp_after
            ):
                continue

            task = self._hash_to_task(
                decoded,
                history_length=query.history_length,
                include_artifacts=query.include_artifacts,
            )
            filtered.append(task)

        # Sort by timestamp descending
        filtered.sort(key=lambda t: t.status.timestamp or "", reverse=True)

        total_size = len(filtered)
        try:
            offset = int(query.page_token) if query.page_token else 0
        except ValueError:
            offset = 0
        page = filtered[offset : offset + query.page_size]

        next_offset = offset + query.page_size
        next_token = str(next_offset) if next_offset < total_size else ""

        return ListTasksResult(
            tasks=page,
            next_page_token=next_token,
            page_size=query.page_size,
            total_size=total_size,
        )

    async def delete_task(self, task_id: str) -> bool:
        task_key = self._task_key(task_id)

        # Get context_id and idempotency_key before deleting
        fields = await self._r.hmget(task_key, "context_id", "metadata_json")
        context_id_raw = fields[0]
        if context_id_raw is None:
            return False

        ctx_id = context_id_raw.decode() if isinstance(context_id_raw, bytes) else context_id_raw

        # Clean up idempotency key if present
        keys_to_delete = [task_key]
        if fields[1]:
            import json as _json

            meta = _json.loads(fields[1].decode() if isinstance(fields[1], bytes) else fields[1])
            idem_key = meta.get("_idempotency_key")
            if idem_key:
                keys_to_delete.append(self._idem_key(ctx_id, idem_key))

        # Remove from context set and delete hash + idem key
        await self._r.srem(self._ctx_set_key(ctx_id), task_id)
        await self._r.delete(*keys_to_delete)
        return True

    async def delete_context(self, context_id: str) -> int:
        ctx_set_key = self._ctx_set_key(context_id)

        task_ids_bytes = await self._r.smembers(ctx_set_key)
        if not task_ids_bytes:
            # Also delete context data
            await self._r.delete(self._context_data_key(context_id))
            return 0

        task_ids = [tid.decode() for tid in task_ids_bytes]

        # Delete all task hashes + idempotency keys + context set + context data
        keys_to_delete = []
        for tid in task_ids:
            keys_to_delete.append(self._task_key(tid))
            # Clean up idempotency key if present
            meta_raw = await self._r.hget(self._task_key(tid), "metadata_json")
            if meta_raw:
                meta = json.loads(meta_raw.decode() if isinstance(meta_raw, bytes) else meta_raw)
                idem_key = meta.get("_idempotency_key")
                if idem_key:
                    keys_to_delete.append(self._idem_key(context_id, idem_key))
        keys_to_delete.append(ctx_set_key)
        keys_to_delete.append(self._context_data_key(context_id))
        await self._r.delete(*keys_to_delete)

        return len(task_ids)

    async def get_version(self, task_id: str) -> int | None:
        version = await self._r.hget(self._task_key(task_id), "version")
        if version is None:
            return None
        raw = version.decode() if isinstance(version, bytes) else version
        return int(raw)

    async def load_context(self, context_id: str) -> ContextT | None:
        data = await self._r.get(self._context_data_key(context_id))
        if data is None:
            return None
        raw = data.decode() if isinstance(data, bytes) else data
        return json.loads(raw)  # type: ignore[no-any-return]

    async def update_context(self, context_id: str, context: ContextT) -> None:
        await self._r.set(self._context_data_key(context_id), json.dumps(context))
