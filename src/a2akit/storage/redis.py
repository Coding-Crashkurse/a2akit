"""Redis-backed storage backend for distributed deployments."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Self, TypeVar, cast

from a2a_pydantic import v10

from a2akit.config import Settings, get_settings
from a2akit.storage.base import (
    META_CREATED_AT_KEY,
    META_LAST_MODIFIED_KEY,
    META_TENANT_KEY,
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
    _coerce_v10_artifact,
    _coerce_v10_message,
    _coerce_v10_messages,
)

try:
    import redis.asyncio as aioredis
except ImportError as _import_error:
    raise ImportError(
        "Redis storage requires additional dependencies. "
        "Install them with: pip install a2akit[redis]"
    ) from _import_error

if TYPE_CHECKING:
    from collections.abc import Awaitable

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


def _aw(value: Awaitable[_T] | _T) -> Awaitable[_T]:
    """Narrow redis-py's ``Awaitable[T] | T`` return annotation.

    redis-py shares command stubs between its sync and async clients, so
    async methods are annotated as returning ``Awaitable[T] | T``. On
    ``redis.asyncio`` the result is always awaitable.
    """
    return cast("Awaitable[_T]", value)


def _as_str(value: Any) -> str:
    """Decode a Redis reply that may be ``bytes`` or ``str``.

    Pools created with ``decode_responses=True`` return ``str``; default
    pools return ``bytes``. Support both instead of blindly ``.decode()``.
    """
    return value.decode() if isinstance(value, bytes) else str(value)


# Lua script for atomic update_task with OCC + terminal-state guard.
# KEYS[1] = task hash key
# ARGV[1] = expected_version ("" if not set)
# ARGV[2] = new state ("" if not changing)
# ARGV[3] = JSON-encoded values dict to HSET
# ARGV[4] = JSON array of terminal state values (from TERMINAL_STATES)
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

-- Terminal state guard. The terminal-state list is passed as ARGV[4]
-- (JSON array) so it is always derived from TERMINAL_STATES in
-- storage/base.py — enum changes cannot silently diverge from this guard.
if new_state ~= '' then
    local current_state = redis.call('HGET', key, 'status_state')
    for _, terminal in ipairs(cjson.decode(ARGV[4])) do
        if current_state == terminal then
            return redis.error_reply('TERMINAL_STATE:' .. current_state .. ':' .. new_state)
        end
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
# ARGV[3] = idempotency-key TTL in seconds
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
redis.call('SET', idem_key, task_id, 'EX', tonumber(ARGV[3]))

return task_id
"""

_TERMINAL_STATE_VALUES = {s.value for s in TERMINAL_STATES}
# Wire form of the terminal-state guard list fed to _UPDATE_TASK_LUA (ARGV[4]).
_TERMINAL_STATES_JSON = json.dumps(sorted(_TERMINAL_STATE_VALUES))


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
        self._idempotency_ttl_s = s.redis_idempotency_ttl_s
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
            await _aw(self._redis.ping())
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
                await _aw(self._redis.ping())
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
    def _serialize_message(msg: v10.Message | None) -> str | None:
        if msg is None:
            return None
        out: str = msg.model_dump_json(by_alias=True, exclude_none=True)
        return out

    @staticmethod
    def _deserialize_message(data: str | None) -> v10.Message | None:
        if not data:
            return None
        return v10.Message.model_validate_json(data)

    @staticmethod
    def _serialize_messages(msgs: list[v10.Message]) -> str:
        return json.dumps(
            [m.model_dump(mode="json", by_alias=True, exclude_none=True) for m in msgs]
        )

    @staticmethod
    def _deserialize_messages(data: str) -> list[v10.Message]:
        raw = json.loads(data)
        return [v10.Message.model_validate(m) for m in raw]

    @staticmethod
    def _serialize_artifacts(artifacts: list[v10.Artifact]) -> str:
        return json.dumps(
            [a.model_dump(mode="json", by_alias=True, exclude_none=True) for a in artifacts]
        )

    @staticmethod
    def _deserialize_artifacts(data: str) -> list[v10.Artifact]:
        raw = json.loads(data)
        return [v10.Artifact.model_validate(a) for a in raw]

    def _hash_to_task(
        self,
        data: dict[str, str],
        history_length: int | None = None,
        include_artifacts: bool = True,
    ) -> v10.Task:
        """Convert a Redis hash dict to a Task object."""
        history = self._deserialize_messages(data.get("history") or "[]")
        if history_length is not None:
            history = history[-history_length:] if history_length > 0 else []

        artifacts_list: list[v10.Artifact] = []
        if include_artifacts:
            raw_artifacts = data.get("artifacts") or "[]"
            artifacts_list = self._deserialize_artifacts(raw_artifacts)

        metadata_raw = json.loads(data["metadata_json"]) if data.get("metadata_json") else None

        status = v10.TaskStatus(
            state=v10.TaskState(data["status_state"]),
            timestamp=data.get("status_timestamp", ""),
            message=self._deserialize_message(data.get("status_message")),
        )

        return v10.Task(
            id=data["id"],
            context_id=data["context_id"],
            status=status,
            history=history,
            artifacts=artifacts_list,
            metadata=metadata_raw,
        )

    async def load_task(
        self,
        task_id: str,
        history_length: int | None = None,
        *,
        include_artifacts: bool = True,
    ) -> v10.Task | None:
        data = await _aw(self._r.hgetall(self._task_key(task_id)))
        if not data:
            return None
        # Normalize to str keys/values (bytes on default pools, str with
        # decode_responses=True).
        decoded = {_as_str(k): _as_str(v) for k, v in data.items()}
        return self._hash_to_task(
            decoded, history_length=history_length, include_artifacts=include_artifacts
        )

    async def create_task(
        self,
        context_id: str,
        message: v10.Message,
        *,
        idempotency_key: str | None = None,
    ) -> v10.Task:
        # Compat: accept legacy v0.3 / a2a-sdk Messages.
        message = _coerce_v10_message(message)
        task_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        history_msg = message.model_copy(update={"task_id": task_id, "context_id": context_id})

        initial_meta: dict[str, Any] = {}
        if idempotency_key:
            initial_meta["_idempotency_key"] = idempotency_key
        initial_meta["stateTransitions"] = [
            _build_transition_record(v10.TaskState.task_state_submitted.value, now),
        ]
        initial_meta[META_CREATED_AT_KEY] = now
        initial_meta[META_LAST_MODIFIED_KEY] = now

        fields: dict[str, str] = {
            "id": task_id,
            "context_id": context_id,
            "status_state": v10.TaskState.task_state_submitted.value,
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
                args=[task_id, json.dumps(fields), str(self._idempotency_ttl_s)],
                client=self._r,
            )
            returned_id = _as_str(result_id)
            if returned_id != task_id:
                # Existing task found via idempotency key — no just-created marker.
                existing = await self.load_task(returned_id)
                if existing is None:
                    raise TaskNotFoundError(
                        f"Idempotent task {returned_id} vanished between create and load"
                    )
                return existing
        else:
            # Non-idempotent: HSET + SADD atomically via pipeline/MULTI so a
            # crash between the two commands cannot orphan the task (task
            # hash written, but not in the context set → invisible to
            # list_tasks by context_id).
            async with self._r.pipeline(transaction=True) as pipe:
                pipe.hset(self._task_key(task_id), mapping=fields)
                pipe.sadd(self._ctx_set_key(context_id), task_id)
                await pipe.execute()

        loaded = await self.load_task(task_id)
        if loaded is None:
            raise TaskNotFoundError(f"Task {task_id} vanished immediately after create")
        # Attach the transient just-created marker (see storage/base.py
        # contract). Not persisted — TaskManager pops it before further use.
        # a2a-pydantic ≥0.0.6 coerces dict → Struct on assignment.
        loaded.metadata = {**(loaded.metadata or {}), "_a2akit_just_created": True}
        return loaded

    async def update_task(
        self,
        task_id: str,
        state: v10.TaskState | None = None,
        *,
        status_message: v10.Message | None = None,
        artifacts: list[ArtifactWrite] | None = None,
        messages: list[v10.Message] | None = None,
        task_metadata: dict[str, Any] | None = None,
        expected_version: int | None = None,
    ) -> int:
        # Compat: coerce v0.3 / sdk-shaped inputs to v10.
        if status_message is not None:
            status_message = _coerce_v10_message(status_message)
        if messages:
            messages = _coerce_v10_messages(messages)
        if artifacts:
            artifacts = [
                ArtifactWrite(_coerce_v10_artifact(aw.artifact), append=aw.append)
                for aw in artifacts
            ]
        # The merge (history append, artifact apply, metadata merge) happens in
        # Python based on a read of the current hash. To prevent silent data loss
        # from concurrent writers we ALWAYS enforce OCC against the version we
        # just read. If the caller did not request a specific expected_version
        # we transparently retry on VERSION_MISMATCH so their semantics are
        # preserved ("blind write"). If the caller did request one, a mismatch
        # is surfaced as ConcurrencyError as before.
        if self._update_script is None:
            raise RuntimeError("RedisStorage not connected — Lua scripts not registered")

        max_attempts = 1 if expected_version is not None else 8
        for attempt in range(max_attempts):
            current_data = await _aw(self._r.hgetall(self._task_key(task_id)))
            if not current_data:
                raise TaskNotFoundError(f"Task {task_id} not found")

            decoded = {_as_str(k): _as_str(v) for k, v in current_data.items()}
            read_version = int(decoded["version"])
            occ_version = expected_version if expected_version is not None else read_version

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

            ts = datetime.now(UTC).isoformat()
            if state is not None:
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
                existing_meta[META_LAST_MODIFIED_KEY] = ts
                values["metadata_json"] = json.dumps(existing_meta)
            elif status_message is not None:
                # Update status message without a state transition (e.g. progress text)
                values["status_message"] = self._serialize_message(status_message) or ""
                values["status_timestamp"] = ts
                existing_meta = json.loads(
                    values.get("metadata_json") or decoded.get("metadata_json") or "{}"
                )
                existing_meta[META_LAST_MODIFIED_KEY] = ts
                values["metadata_json"] = json.dumps(existing_meta)

            try:
                new_version: int = await self._update_script(
                    keys=[self._task_key(task_id)],
                    args=[
                        str(occ_version),
                        state.value if state is not None else "",
                        json.dumps(values),
                        _TERMINAL_STATES_JSON,
                    ],
                    client=self._r,
                )
            except aioredis.ResponseError as e:
                err = str(e)
                if "TASK_NOT_FOUND" in err:
                    raise TaskNotFoundError(f"Task {task_id} not found") from e
                if "VERSION_MISMATCH" in err:
                    current = int(err.split(":")[-1])
                    if expected_version is not None:
                        # Caller requested a specific version — surface the conflict
                        raise ConcurrencyError(
                            f"Version mismatch for task {task_id}: "
                            f"expected {expected_version}, current {current}",
                            current_version=current,
                        ) from e
                    # Transparent retry: another writer beat us; re-read and re-merge
                    if attempt == max_attempts - 1:
                        raise ConcurrencyError(
                            f"Failed to update task {task_id} after {max_attempts} "
                            f"attempts due to persistent contention",
                            current_version=current,
                        ) from e
                    continue
                if "TERMINAL_STATE" in err:
                    parts = err.split(":")
                    raise TaskTerminalStateError(
                        f"Cannot transition terminal task {task_id} from {parts[1]} to {parts[2]}"
                    ) from e
                raise

            return int(new_version)

        # Unreachable: loop either returns or raises
        raise RuntimeError("update_task retry loop exited without result")

    @staticmethod
    def _apply_artifact(
        existing: list[v10.Artifact], artifact: v10.Artifact, *, append: bool
    ) -> list[v10.Artifact]:
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
            task_ids_raw = await _aw(self._r.smembers(self._ctx_set_key(query.context_id)))
            task_ids = [_as_str(tid) for tid in task_ids_raw]
        else:
            # Scan for all task keys
            task_ids_set: set[str] = set()
            pattern = f"{self._key_prefix}task:*"
            async for key in self._r.scan_iter(match=pattern, count=200):
                key_str = _as_str(key)
                task_ids_set.add(key_str[len(f"{self._key_prefix}task:") :])
            task_ids = list(task_ids_set)

        # Load and filter tasks
        filtered: list[v10.Task] = []
        for tid in task_ids:
            data = await _aw(self._r.hgetall(self._task_key(tid)))
            if not data:
                continue
            decoded = {_as_str(k): _as_str(v) for k, v in data.items()}

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
            if query.tenant and (task.metadata or {}).get(META_TENANT_KEY) != query.tenant:
                continue
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
        fields = await _aw(self._r.hmget(task_key, ["context_id", "metadata_json"]))
        context_id_raw = fields[0]
        if context_id_raw is None:
            return False

        ctx_id = _as_str(context_id_raw)

        # Clean up idempotency key if present
        extra_keys: list[str] = []
        if fields[1]:
            meta = json.loads(_as_str(fields[1]))
            idem_key = meta.get("_idempotency_key")
            if idem_key:
                extra_keys.append(self._idem_key(ctx_id, idem_key))

        # SREM + DEL in a single MULTI/EXEC so the context set and the task
        # hash cannot diverge, and use the DEL count of the task hash as the
        # authoritative "did it exist" answer — the HMGET above is only a
        # hint and can race with a concurrent deleter.
        async with self._r.pipeline(transaction=True) as pipe:
            pipe.srem(self._ctx_set_key(ctx_id), task_id)
            pipe.delete(task_key)
            if extra_keys:
                pipe.delete(*extra_keys)
            results = await pipe.execute()
        existed = bool(results[1])
        if existed:
            await self._cascade_push_delete_for_task(task_id)
        return existed

    async def delete_context(self, context_id: str) -> int:
        ctx_set_key = self._ctx_set_key(context_id)

        task_ids_raw = await _aw(self._r.smembers(ctx_set_key))
        task_ids = [_as_str(tid) for tid in task_ids_raw]

        # Delete each task via the atomic delete_task (SREM + DEL in one
        # MULTI) so the returned count only includes tasks that actually
        # existed — a concurrent deleter racing us is counted exactly once.
        # delete_task also handles idempotency-key cleanup and the
        # push-config cascade per task.
        deleted = 0
        for tid in task_ids:
            if await self.delete_task(tid):
                deleted += 1

        # Remove the (now empty) context set and the context data.
        await _aw(self._r.delete(ctx_set_key, self._context_data_key(context_id)))
        return deleted

    async def get_version(self, task_id: str) -> int | None:
        version = await _aw(self._r.hget(self._task_key(task_id), "version"))
        if version is None:
            return None
        return int(_as_str(version))

    async def load_context(self, context_id: str) -> ContextT | None:
        data = await _aw(self._r.get(self._context_data_key(context_id)))
        if data is None:
            return None
        return json.loads(_as_str(data))  # type: ignore[no-any-return]

    async def update_context(self, context_id: str, context: ContextT) -> None:
        await self._r.set(self._context_data_key(context_id), json.dumps(context))
