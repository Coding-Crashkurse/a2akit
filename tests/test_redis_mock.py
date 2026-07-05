"""Mock-based unit tests for Redis backends — no Redis server required.

Tests serialization/deserialization, class construction, and logic paths
that don't require a live Redis connection.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from a2a_pydantic.v10 import (
    Message,
    Part,
    Role,
    SendMessageRequest,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)

from a2akit.config import Settings


def _mock_redis_with_pipeline() -> tuple[AsyncMock, AsyncMock]:
    """AsyncMock Redis client whose .pipeline() yields a mock pipe."""
    mock_redis = AsyncMock()
    mock_pipe = AsyncMock()
    mock_ctx = MagicMock()
    mock_ctx.__aenter__ = AsyncMock(return_value=mock_pipe)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)
    mock_redis.pipeline = MagicMock(return_value=mock_ctx)
    return mock_redis, mock_pipe


def _params(text: str = "hello") -> SendMessageRequest:
    msg = Message(
        role=Role.role_user,
        parts=[Part(text=text)],
        message_id="msg1",
    )
    return SendMessageRequest(message=msg)


def _status_event(task_id: str = "t1", *, final: bool = False) -> TaskStatusUpdateEvent:
    state = TaskState.task_state_completed if final else TaskState.task_state_working
    return TaskStatusUpdateEvent(
        task_id=task_id,
        context_id="ctx-1",
        status=TaskStatus(state=state),
    )


class TestBrokerSerialization:
    """Test _serialize_operation / _deserialize_operation roundtrip."""

    def test_roundtrip(self):
        from a2akit.broker.redis import _deserialize_operation, _serialize_operation

        params = _params("roundtrip-test")
        serialized = _serialize_operation(
            params, is_new_task=True, request_context={"user": "alice"}
        )
        fields = {b"op": serialized.encode()}
        op = _deserialize_operation(fields)
        assert op.operation == "run"
        assert op.params.message.parts[0].text == "roundtrip-test"
        assert op.is_new_task is True
        assert op.request_context == {"user": "alice"}

    def test_defaults(self):
        from a2akit.broker.redis import _deserialize_operation, _serialize_operation

        params = _params("defaults")
        serialized = _serialize_operation(params, is_new_task=False, request_context={})
        raw = json.loads(serialized)
        assert raw["is_new_task"] is False
        assert raw["request_context"] == {}

        fields = {b"op": serialized.encode()}
        op = _deserialize_operation(fields)
        assert op.is_new_task is False
        assert op.request_context == {}

    def test_json_structure(self):
        from a2akit.broker.redis import _serialize_operation

        params = _params("test")
        serialized = _serialize_operation(params, is_new_task=True, request_context={"k": "v"})
        raw = json.loads(serialized)
        assert raw["operation"] == "run"
        assert "params" in raw
        assert raw["is_new_task"] is True
        assert raw["request_context"] == {"k": "v"}


class TestEventBusSerialization:
    """Test _serialize_event / _deserialize_event roundtrip."""

    def test_status_update_roundtrip(self):
        from a2akit.event_bus.redis import _deserialize_event, _serialize_event

        event = _status_event("t1", final=False)
        serialized = _serialize_event(event)
        restored = _deserialize_event(serialized)
        assert isinstance(restored, TaskStatusUpdateEvent)
        assert restored.status.state == TaskState.task_state_working

    def test_final_status_roundtrip(self):
        from a2akit.event_bus.redis import _deserialize_event, _serialize_event
        from a2akit.schema import TerminalMarker

        event = TerminalMarker(event=_status_event("t1", final=True))
        serialized = _serialize_event(event)
        restored = _deserialize_event(serialized)
        assert isinstance(restored, TerminalMarker)

    def test_direct_reply_roundtrip(self):
        from a2akit.event_bus.redis import _deserialize_event, _serialize_event
        from a2akit.schema import DirectReply

        msg = Message(
            role=Role.role_agent,
            parts=[Part(text="hello back")],
            message_id="m1",
        )
        event = DirectReply(message=msg)
        serialized = _serialize_event(event)
        restored = _deserialize_event(serialized)
        assert isinstance(restored, DirectReply)
        assert restored.message.parts[0].text == "hello back"

    def test_message_roundtrip(self):
        from a2akit.event_bus.redis import _deserialize_event, _serialize_event

        msg = Message(
            role=Role.role_agent,
            parts=[Part(text="response")],
            message_id="m2",
        )
        serialized = _serialize_event(msg)
        restored = _deserialize_event(serialized)
        assert isinstance(restored, Message)
        assert restored.parts[0].text == "response"

    def test_unknown_type_raises(self):
        from a2akit.event_bus.redis import _deserialize_event

        with pytest.raises(ValueError, match="Cannot deserialize"):
            _deserialize_event('{"unknown": true}')

    def test_unknown_event_type_serialize_raises(self):
        from a2akit.event_bus.redis import _serialize_event

        with pytest.raises(TypeError, match="Unknown event type"):
            _serialize_event("not an event")  # type: ignore[arg-type]

    def test_is_final_true(self):
        from a2akit.event_bus.redis import _is_final
        from a2akit.schema import TerminalMarker

        assert _is_final(TerminalMarker(event=_status_event("t", final=True))) is True

    def test_is_final_false(self):
        from a2akit.event_bus.redis import _is_final

        assert _is_final(_status_event("t", final=False)) is False

    def test_is_final_non_status(self):
        from a2akit.event_bus.redis import _is_final

        msg = Message(
            role=Role.role_agent,
            parts=[Part(text="hi")],
            message_id="m1",
        )
        assert _is_final(msg) is False


class TestRedisOperationHandle:
    """Test ack/nack logic with mocked Redis."""

    def _make_handle(
        self, *, attempt: int = 1, max_retries: int = 3, stream_maxlen: int | None = None
    ):
        from a2akit.broker.redis import RedisOperationHandle, _RunTask

        mock_redis, mock_pipe = _mock_redis_with_pipeline()

        op = _RunTask(
            operation="run",
            params=_params("test"),
            is_new_task=False,
            request_context={},
        )

        handle = RedisOperationHandle(
            redis_client=mock_redis,
            stream_key="stream:tasks",
            dlq_key="dlq:tasks",
            group_name="workers",
            msg_id=b"1234-0",
            op=op,
            serialized_op=b'{"test": true}',
            attempt=attempt,
            max_retries=max_retries,
            stream_maxlen=stream_maxlen,
        )
        return handle, mock_redis, mock_pipe

    async def test_operation_property(self):
        handle, _, _ = self._make_handle()
        assert handle.operation.operation == "run"

    async def test_attempt_property(self):
        handle, _, _ = self._make_handle(attempt=3)
        assert handle.attempt == 3

    async def test_ack(self):
        handle, mock_redis, _ = self._make_handle()
        await handle.ack()
        mock_redis.xack.assert_awaited_once_with("stream:tasks", "workers", b"1234-0")

    async def test_nack_requeues(self):
        handle, _mock_redis, mock_pipe = self._make_handle(attempt=1, max_retries=3)
        await handle.nack()
        # Should use pipeline for ACK + XADD
        mock_pipe.xack.assert_called_once()
        mock_pipe.xadd.assert_called_once()

    async def test_nack_max_retries_to_dlq(self):
        handle, mock_redis, _ = self._make_handle(attempt=3, max_retries=3)
        await handle.nack()
        # Should move to DLQ
        mock_redis.xadd.assert_awaited_once()
        call_args = mock_redis.xadd.call_args
        assert call_args[0][0] == "dlq:tasks"  # DLQ key
        mock_redis.xack.assert_awaited_once()

    async def test_nack_with_delay_sets_not_before(self):
        handle, _, mock_pipe = self._make_handle(attempt=1, max_retries=3)
        await handle.nack(delay_seconds=30)
        fields = mock_pipe.xadd.call_args[0][1]
        assert b"not_before" in fields
        assert float(fields[b"not_before"]) > time.time()
        assert fields[b"attempt"] == b"2"

    async def test_nack_readd_trims_stream(self):
        handle, _, mock_pipe = self._make_handle(attempt=1, max_retries=3, stream_maxlen=42)
        await handle.nack()
        kwargs = mock_pipe.xadd.call_args.kwargs
        assert kwargs["maxlen"] == 42
        assert kwargs["approximate"] is True

    async def test_nack_dlq_trims_stream(self):
        handle, mock_redis, _ = self._make_handle(attempt=3, max_retries=3, stream_maxlen=42)
        await handle.nack()
        kwargs = mock_redis.xadd.call_args.kwargs
        assert kwargs["maxlen"] == 42
        assert kwargs["approximate"] is True


class TestRedisBrokerConstruction:
    """Test RedisBroker init and configuration."""

    def test_defaults_from_settings(self):
        from a2akit.broker.redis import RedisBroker

        settings = Settings(
            redis_url="redis://testhost:1234/1",
            redis_key_prefix="test:",
            redis_broker_stream="mystream",
            redis_broker_group="mygroup",
            redis_broker_block_ms=1000,
        )
        broker = RedisBroker(settings=settings)
        assert broker._url == "redis://testhost:1234/1"
        assert broker._key_prefix == "test:"
        assert broker._stream_key == "test:stream:mystream"
        assert broker._block_ms == 1000
        assert broker._shutdown_flag is False

    def test_explicit_params_override_settings(self):
        from a2akit.broker.redis import RedisBroker

        broker = RedisBroker(
            "redis://override:6379/0",
            key_prefix="custom:",
            stream_name="custom-stream",
            block_ms=500,
        )
        assert broker._url == "redis://override:6379/0"
        assert broker._key_prefix == "custom:"
        assert broker._stream_key == "custom:stream:custom-stream"
        assert broker._block_ms == 500

    def test_consumer_name_includes_host_pid(self):
        from a2akit.broker.redis import RedisBroker

        broker = RedisBroker("redis://localhost:6379/0")
        import os
        import socket

        assert socket.gethostname() in broker._consumer_name
        assert str(os.getpid()) in broker._consumer_name

    async def test_shutdown_sets_flag(self):
        from a2akit.broker.redis import RedisBroker

        broker = RedisBroker("redis://localhost:6379/0")
        assert broker._shutdown_flag is False
        await broker.shutdown()
        assert broker._shutdown_flag is True


class TestRedisEventBusConstruction:
    """Test RedisEventBus init and configuration."""

    def test_defaults_from_settings(self):
        from a2akit.event_bus.redis import RedisEventBus

        settings = Settings(
            redis_url="redis://testhost:1234/1",
            redis_key_prefix="test:",
            redis_event_bus_channel_prefix="ch:",
            redis_event_bus_stream_prefix="log:",
            redis_event_bus_stream_maxlen=500,
        )
        eb = RedisEventBus(settings=settings)
        assert eb._url == "redis://testhost:1234/1"
        assert eb._channel_prefix == "test:ch:"
        assert eb._stream_prefix == "test:log:"
        assert eb._stream_maxlen == 500

    def test_explicit_params_override(self):
        from a2akit.event_bus.redis import RedisEventBus

        eb = RedisEventBus(
            "redis://override:6379/0",
            key_prefix="custom:",
            stream_maxlen=100,
        )
        assert eb._url == "redis://override:6379/0"
        assert eb._stream_maxlen == 100


class TestRedisCancelRegistryConstruction:
    """Test RedisCancelRegistry init and configuration."""

    def test_defaults_from_settings(self):
        from a2akit.broker.redis import RedisCancelRegistry

        settings = Settings(
            redis_url="redis://testhost:1234/1",
            redis_key_prefix="test:",
            redis_cancel_ttl_s=3600,
        )
        cr = RedisCancelRegistry(settings=settings)
        assert cr._key_prefix == "test:"
        assert cr._ttl_s == 3600

    def test_explicit_params_override(self):
        from a2akit.broker.redis import RedisCancelRegistry

        cr = RedisCancelRegistry(
            "redis://override:6379/0",
            key_prefix="custom:",
            ttl_s=100,
        )
        assert cr._key_prefix == "custom:"
        assert cr._ttl_s == 100

    async def test_request_cancel_uses_pipeline(self):
        from a2akit.broker.redis import RedisCancelRegistry

        cr = RedisCancelRegistry("redis://localhost:6379/0", key_prefix="test:")
        mock_redis = AsyncMock()
        mock_pipe = AsyncMock()
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_pipe)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_redis.pipeline = MagicMock(return_value=mock_ctx)
        cr._redis = mock_redis

        await cr.request_cancel("task-1")
        mock_pipe.set.assert_called_once()
        mock_pipe.publish.assert_called_once()

    async def test_is_cancelled_checks_key(self):
        from a2akit.broker.redis import RedisCancelRegistry

        cr = RedisCancelRegistry("redis://localhost:6379/0", key_prefix="test:")
        mock_redis = AsyncMock()
        mock_redis.exists.return_value = 1
        cr._redis = mock_redis

        result = await cr.is_cancelled("task-1")
        assert result is True
        mock_redis.exists.assert_awaited_once_with("test:cancel:task-1")

    async def test_cleanup_deletes_key(self):
        from a2akit.broker.redis import RedisCancelRegistry

        cr = RedisCancelRegistry("redis://localhost:6379/0", key_prefix="test:")
        mock_redis = AsyncMock()
        cr._redis = mock_redis

        await cr.cleanup("task-1")
        mock_redis.delete.assert_awaited_once_with("test:cancel:task-1")

    async def test_cleanup_idempotent(self):
        from a2akit.broker.redis import RedisCancelRegistry

        cr = RedisCancelRegistry("redis://localhost:6379/0", key_prefix="test:")
        mock_redis = AsyncMock()
        cr._redis = mock_redis

        await cr.cleanup("task-1")
        await cr.cleanup("task-1")
        assert mock_redis.delete.await_count == 2


class TestRedisCancelScope:
    """Test RedisCancelScope with mocked Redis."""

    def test_initial_state(self):
        from a2akit.broker.redis import RedisCancelScope

        mock_redis = AsyncMock()
        scope = RedisCancelScope(mock_redis, "task-1", "test:")
        assert scope.is_set() is False
        assert scope._started is False

    async def test_start_detects_existing_cancel(self):
        from a2akit.broker.redis import RedisCancelScope

        mock_redis = AsyncMock()
        mock_redis.exists.return_value = 1
        scope = RedisCancelScope(mock_redis, "task-1", "test:")
        await scope._start()
        assert scope.is_set() is True

    async def test_start_subscribes_if_not_cancelled(self):
        from a2akit.broker.redis import RedisCancelScope

        mock_redis = AsyncMock()
        mock_redis.exists.return_value = 0
        mock_pubsub = AsyncMock()

        async def fake_listen():
            await asyncio.sleep(10)
            yield  # pragma: no cover

        mock_pubsub.listen = fake_listen
        mock_redis.pubsub = MagicMock(return_value=mock_pubsub)

        scope = RedisCancelScope(mock_redis, "task-1", "test:")
        await scope._start()
        assert scope._started is True
        mock_pubsub.subscribe.assert_awaited_once()

        # Cleanup
        if scope._listener_task:
            scope._listener_task.cancel()
            from contextlib import suppress

            with suppress(asyncio.CancelledError):
                await scope._listener_task

    async def test_double_start_is_noop(self):
        from a2akit.broker.redis import RedisCancelScope

        mock_redis = AsyncMock()
        mock_redis.exists.return_value = 1
        scope = RedisCancelScope(mock_redis, "task-1", "test:")
        await scope._start()
        await scope._start()  # Second call should be no-op
        # exists should only be called once
        assert mock_redis.exists.await_count == 1

    async def test_start_does_not_false_cancel_on_connection_error(self):
        """Redis failure during _start must NOT set the cancel event."""
        from a2akit.broker.redis import RedisCancelScope

        mock_redis = AsyncMock()
        mock_redis.exists.side_effect = ConnectionError("Redis down")
        scope = RedisCancelScope(mock_redis, "task-1", "test:")
        await scope._start()
        assert scope.is_set() is False, "Connection error must not falsely signal cancellation"

    async def test_listen_does_not_false_cancel_on_unexpected_error(self):
        """Unexpected error in _listen must NOT set the cancel event."""
        from a2akit.broker.redis import RedisCancelScope

        mock_redis = AsyncMock()
        mock_pubsub = AsyncMock()

        async def exploding_listen():
            raise RuntimeError("unexpected auth error")
            yield  # pragma: no cover

        mock_pubsub.listen = exploding_listen
        mock_redis.pubsub = MagicMock(return_value=mock_pubsub)
        mock_redis.exists.return_value = 0

        scope = RedisCancelScope(mock_redis, "task-1", "test:")
        scope._pubsub = mock_pubsub
        scope._started = True
        scope._listener_task = asyncio.create_task(scope._listen())
        await scope._listener_task
        assert scope.is_set() is False, "Unexpected error must not falsely signal cancellation"


class TestServerFactoryMethods:
    """Test that A2AServer resolves Redis URLs to Redis backends."""

    def test_build_broker_redis_url(self):
        from a2akit.server import A2AServer
        from a2akit.worker import Worker

        class DummyWorker(Worker):
            async def handle(self, ctx):
                pass

        from a2akit.agent_card import AgentCardConfig

        server = A2AServer(
            worker=DummyWorker(),
            agent_card=AgentCardConfig(name="test", description="test", version="0.0.1"),
            broker="redis://localhost:6379/0",
        )
        broker = server._build_broker()
        from a2akit.broker.redis import RedisBroker

        assert isinstance(broker, RedisBroker)

    def test_build_event_bus_redis_url(self):
        from a2akit.server import A2AServer
        from a2akit.worker import Worker

        class DummyWorker(Worker):
            async def handle(self, ctx):
                pass

        from a2akit.agent_card import AgentCardConfig

        server = A2AServer(
            worker=DummyWorker(),
            agent_card=AgentCardConfig(name="test", description="test", version="0.0.1"),
            event_bus="redis://localhost:6379/0",
        )
        eb = server._build_event_bus()
        from a2akit.event_bus.redis import RedisEventBus

        assert isinstance(eb, RedisEventBus)

    def test_build_cancel_registry_auto_redis(self):
        from a2akit.server import A2AServer
        from a2akit.worker import Worker

        class DummyWorker(Worker):
            async def handle(self, ctx):
                pass

        from a2akit.agent_card import AgentCardConfig

        server = A2AServer(
            worker=DummyWorker(),
            agent_card=AgentCardConfig(name="test", description="test", version="0.0.1"),
            broker="redis://localhost:6379/0",
        )
        cr = server._build_cancel_registry()
        from a2akit.broker.redis import RedisCancelRegistry

        assert isinstance(cr, RedisCancelRegistry)

    def test_build_broker_invalid_raises(self):
        from a2akit.server import A2AServer
        from a2akit.worker import Worker

        class DummyWorker(Worker):
            async def handle(self, ctx):
                pass

        from a2akit.agent_card import AgentCardConfig

        server = A2AServer(
            worker=DummyWorker(),
            agent_card=AgentCardConfig(name="test", description="test", version="0.0.1"),
            broker="unknown://foo",
        )
        with pytest.raises(ValueError, match="Unknown broker"):
            server._build_broker()

    def test_build_event_bus_invalid_raises(self):
        from a2akit.server import A2AServer
        from a2akit.worker import Worker

        class DummyWorker(Worker):
            async def handle(self, ctx):
                pass

        from a2akit.agent_card import AgentCardConfig

        server = A2AServer(
            worker=DummyWorker(),
            agent_card=AgentCardConfig(name="test", description="test", version="0.0.1"),
            event_bus="unknown://foo",
        )
        with pytest.raises(ValueError, match="Unknown event bus"):
            server._build_event_bus()

    def test_build_broker_rediss_url(self):
        """TLS URL (rediss://) also works."""
        from a2akit.server import A2AServer
        from a2akit.worker import Worker

        class DummyWorker(Worker):
            async def handle(self, ctx):
                pass

        from a2akit.agent_card import AgentCardConfig

        server = A2AServer(
            worker=DummyWorker(),
            agent_card=AgentCardConfig(name="test", description="test", version="0.0.1"),
            broker="rediss://localhost:6379/0",
        )
        broker = server._build_broker()
        from a2akit.broker.redis import RedisBroker

        assert isinstance(broker, RedisBroker)


class TestLazyImports:
    """Test that lazy __getattr__ in __init__.py works."""

    def test_broker_lazy_import(self):
        from a2akit.broker import RedisBroker

        assert RedisBroker is not None

    def test_cancel_registry_lazy_import(self):
        from a2akit.broker import RedisCancelRegistry

        assert RedisCancelRegistry is not None

    def test_event_bus_lazy_import(self):
        from a2akit.event_bus import RedisEventBus

        assert RedisEventBus is not None

    def test_broker_unknown_attr_raises(self):
        with pytest.raises((AttributeError, ImportError)):
            from a2akit.broker import NonExistentThing  # noqa: F401

    def test_event_bus_unknown_attr_raises(self):
        with pytest.raises((AttributeError, ImportError)):
            from a2akit.event_bus import NonExistentThing  # noqa: F401


class TestRedisTaskLockFactory:
    """Test the convenience lock factory."""

    def test_returns_callable(self):
        from a2akit.broker.redis import redis_task_lock_factory

        mock_redis = MagicMock()
        mock_redis.lock.return_value = "lock-obj"

        factory = redis_task_lock_factory(mock_redis, timeout=120)
        result = factory("task-123")
        mock_redis.lock.assert_called_once_with("a2akit:tasklock:task-123", timeout=120)
        assert result == "lock-obj"

    def test_respects_key_prefix(self):
        from a2akit.broker.redis import redis_task_lock_factory

        mock_redis = MagicMock()
        factory = redis_task_lock_factory(mock_redis, timeout=60, key_prefix="custom:")
        factory("task-1")
        mock_redis.lock.assert_called_once_with("custom:tasklock:task-1", timeout=60)


class TestBrokerStreamTrimming:
    """Task stream and DLQ XADDs carry an approximate MAXLEN."""

    def test_maxlen_from_settings(self):
        from a2akit.broker.redis import RedisBroker

        settings = Settings(redis_broker_stream_maxlen=77)
        broker = RedisBroker(settings=settings)
        assert broker._stream_maxlen == 77

    def test_claim_timeout_default_is_10_minutes(self):
        assert Settings().redis_broker_claim_timeout_ms == 600000

    async def test_run_task_trims_stream(self):
        from a2akit.broker.redis import RedisBroker

        broker = RedisBroker("redis://localhost:6379/0", key_prefix="t:", stream_maxlen=42)
        mock_redis = AsyncMock()
        broker._redis = mock_redis
        await broker.run_task(_params("x"))
        kwargs = mock_redis.xadd.call_args.kwargs
        assert kwargs["maxlen"] == 42
        assert kwargs["approximate"] is True


class TestNotBeforeHandling:
    """Backoff (not_before) entries are re-queued instead of slept on."""

    def _broker(self, stream_maxlen: int = 50):
        from a2akit.broker.redis import RedisBroker

        broker = RedisBroker(
            "redis://localhost:6379/0",
            key_prefix="t:",
            stream_maxlen=stream_maxlen,
            block_ms=10,
            max_retries=3,
        )
        mock_redis, mock_pipe = _mock_redis_with_pipeline()
        broker._redis = mock_redis
        return broker, mock_redis, mock_pipe

    def _serialized_op(self, text: str = "op") -> bytes:
        from a2akit.broker.redis import _serialize_operation

        return _serialize_operation(_params(text), is_new_task=False, request_context={}).encode()

    async def test_consume_requeues_not_due_entry(self):
        import redis.asyncio as aioredis

        broker, mock_redis, mock_pipe = self._broker()
        mock_redis.xautoclaim.side_effect = aioredis.ResponseError("NOGROUP")

        fields = {
            b"op": self._serialized_op(),
            b"attempt": b"2",
            b"not_before": str(time.time() + 3600).encode(),
        }
        calls = 0

        async def fake_xreadgroup(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return [(b"t:stream:tasks", [(b"1-0", fields)])]
            broker._shutdown_flag = True
            return []

        mock_redis.xreadgroup = AsyncMock(side_effect=fake_xreadgroup)

        handles = [h async for h in broker.receive_task_operations()]
        # Never yielded — re-added (fields preserved) + ACKed instead.
        assert handles == []
        xadd_call = mock_pipe.xadd.call_args
        assert xadd_call[0][1] == fields
        assert xadd_call.kwargs["maxlen"] == 50
        assert xadd_call.kwargs["approximate"] is True
        mock_pipe.xack.assert_called_once()

    async def test_consume_yields_due_entry(self):
        import redis.asyncio as aioredis

        broker, mock_redis, _ = self._broker()
        mock_redis.xautoclaim.side_effect = aioredis.ResponseError("NOGROUP")

        fields = {
            b"op": self._serialized_op("due"),
            b"attempt": b"2",
            b"not_before": str(time.time() - 5).encode(),
        }
        calls = 0

        async def fake_xreadgroup(**kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return [(b"t:stream:tasks", [(b"1-0", fields)])]
            broker._shutdown_flag = True
            return []

        mock_redis.xreadgroup = AsyncMock(side_effect=fake_xreadgroup)

        handles = [h async for h in broker.receive_task_operations()]
        assert len(handles) == 1
        assert handles[0].attempt == 2

    async def test_claim_requeues_not_due_entry(self):
        broker, mock_redis, mock_pipe = self._broker()
        fields = {
            b"op": self._serialized_op(),
            b"attempt": b"2",
            b"not_before": str(time.time() + 3600).encode(),
        }
        mock_redis.xautoclaim = AsyncMock(return_value=(b"0-0", [(b"9-0", fields)], []))

        handles = [h async for h in broker._claim_stale_messages()]
        assert handles == []
        xadd_call = mock_pipe.xadd.call_args
        assert xadd_call[0][1] == fields
        mock_pipe.xack.assert_called_once()
        # A not-yet-due claim must not count toward the poison-pill limit.
        mock_redis.hincrby.assert_not_awaited()


class TestClaimPath:
    """XAUTOCLAIM recovery: normal re-delivery and poison-pill DLQ."""

    def _broker(self):
        from a2akit.broker.redis import RedisBroker

        broker = RedisBroker(
            "redis://localhost:6379/0", key_prefix="t:", stream_maxlen=42, max_retries=3
        )
        mock_redis, mock_pipe = _mock_redis_with_pipeline()
        broker._redis = mock_redis
        return broker, mock_redis, mock_pipe

    def _fields(self) -> dict[bytes, bytes]:
        from a2akit.broker.redis import _serialize_operation

        serialized = _serialize_operation(
            _params("claimed"), is_new_task=False, request_context={}
        )
        return {b"op": serialized.encode(), b"attempt": b"1"}

    async def test_claim_yields_with_effective_attempt(self):
        broker, mock_redis, _ = self._broker()
        mock_redis.xautoclaim = AsyncMock(return_value=(b"0-0", [(b"9-0", self._fields())], []))
        mock_redis.hincrby = AsyncMock(return_value=1)

        handles = [h async for h in broker._claim_stale_messages()]
        assert len(handles) == 1
        assert handles[0].attempt == 2  # payload attempt 1 + 1 crash claim

    async def test_claim_poison_pill_moves_to_dlq(self):
        broker, mock_redis, _ = self._broker()
        mock_redis.xautoclaim = AsyncMock(return_value=(b"0-0", [(b"9-0", self._fields())], []))
        mock_redis.hincrby = AsyncMock(return_value=5)  # 1 + 5 > max_retries=3

        handles = [h async for h in broker._claim_stale_messages()]
        # Handle is still yielded so the WorkerAdapter can fail the task.
        assert len(handles) == 1
        assert handles[0].attempt == 6
        dlq_call = mock_redis.xadd.call_args
        assert dlq_call[0][0] == broker._dlq_key
        assert dlq_call.kwargs["maxlen"] == 42
        assert dlq_call.kwargs["approximate"] is True
        mock_redis.xack.assert_awaited_once()
        mock_redis.hdel.assert_awaited_once()


class TestRedisStorageMocked:
    """RedisStorage logic paths that don't need a live server."""

    def _task_hash(self, *, decoded: bool) -> dict:
        fields = {
            "id": "t1",
            "context_id": "c1",
            "status_state": TaskState.task_state_submitted.value,
            "status_timestamp": "2026-01-01T00:00:00+00:00",
            "history": "[]",
            "artifacts": "[]",
            "version": "1",
        }
        if decoded:
            return fields
        return {k.encode(): v.encode() for k, v in fields.items()}

    def _storage(self, **settings_kwargs):
        from a2akit.storage.redis import RedisStorage

        settings = Settings(redis_url="redis://localhost:6379/0", **settings_kwargs)
        storage = RedisStorage(settings=settings, key_prefix="t:")
        storage._redis = AsyncMock()
        return storage

    async def test_load_task_with_decoded_responses(self):
        """Pools created with decode_responses=True return str, not bytes."""
        storage = self._storage()
        storage._redis.hgetall = AsyncMock(return_value=self._task_hash(decoded=True))
        task = await storage.load_task("t1")
        assert task is not None
        assert task.id == "t1"

    async def test_load_task_with_bytes_responses(self):
        storage = self._storage()
        storage._redis.hgetall = AsyncMock(return_value=self._task_hash(decoded=False))
        task = await storage.load_task("t1")
        assert task is not None
        assert task.id == "t1"

    async def test_update_task_with_decoded_responses(self):
        storage = self._storage()
        storage._redis.hgetall = AsyncMock(return_value=self._task_hash(decoded=True))

        async def fake_script(keys, args, client):
            return 2

        storage._update_script = fake_script
        version = await storage.update_task("t1", state=TaskState.task_state_working)
        assert version == 2

    async def test_get_version_with_decoded_responses(self):
        storage = self._storage()
        storage._redis.hget = AsyncMock(return_value="5")
        assert await storage.get_version("t1") == 5

    async def test_update_script_receives_all_terminal_states(self):
        """The Lua terminal guard list is derived from TERMINAL_STATES."""
        from a2akit.storage.base import TERMINAL_STATES

        storage = self._storage()
        storage._redis.hgetall = AsyncMock(return_value=self._task_hash(decoded=False))
        captured: dict = {}

        async def fake_script(keys, args, client):
            captured["args"] = args
            return 2

        storage._update_script = fake_script
        await storage.update_task("t1", state=TaskState.task_state_working)

        terminals = set(json.loads(captured["args"][3]))
        assert terminals == {s.value for s in TERMINAL_STATES}

    async def test_create_idempotent_passes_ttl(self):
        storage = self._storage(redis_idempotency_ttl_s=123)
        storage._redis.hgetall = AsyncMock(return_value=self._task_hash(decoded=False))
        captured: dict = {}

        async def fake_script(keys, args, client):
            captured["args"] = args
            return args[0]  # our own task_id → "newly created" path

        storage._create_idem_script = fake_script
        msg = Message(role=Role.role_user, parts=[Part(text="x")], message_id="m1")
        await storage.create_task("c1", msg, idempotency_key="k1")
        assert captured["args"][2] == "123"

    async def test_delete_task_returns_false_when_del_races(self):
        """DEL count is authoritative: a racing deleter means False."""
        storage = self._storage()
        mock_redis, mock_pipe = _mock_redis_with_pipeline()
        storage._redis = mock_redis
        mock_redis.hmget = AsyncMock(return_value=["c1", None])
        mock_pipe.execute = AsyncMock(return_value=[0, 0])  # SREM, DEL(task)

        assert await storage.delete_task("t1") is False

    async def test_delete_task_returns_true_when_deleted(self):
        storage = self._storage()
        mock_redis, mock_pipe = _mock_redis_with_pipeline()
        storage._redis = mock_redis
        mock_redis.hmget = AsyncMock(return_value=["c1", None])
        mock_pipe.execute = AsyncMock(return_value=[1, 1])

        assert await storage.delete_task("t1") is True

    async def test_delete_task_missing_returns_false(self):
        storage = self._storage()
        storage._redis.hmget = AsyncMock(return_value=[None, None])
        assert await storage.delete_task("t1") is False
