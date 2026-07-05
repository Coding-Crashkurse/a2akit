"""Native A2A v1.0 JSON-RPC transport for :class:`A2AClient`.

PascalCase method names (``SendMessage``, ``GetTask``, …) and
``google.rpc.ErrorInfo`` detail shape on errors. Otherwise mirrors the v0.3
transport contract.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

import httpx
from a2a_pydantic import convert_to_v03
from a2a_pydantic.v10 import (
    AgentCard,
    Message,
    SendMessageConfiguration,
    SendMessageRequest,
    Task,
    TaskArtifactUpdateEvent,
    TaskStatusUpdateEvent,
)

from a2akit.client.errors import (
    A2AClientError,
    ProtocolError,
    ProtocolVersionMismatchError,
    TaskNotCancelableError,
    TaskNotFoundError,
    TaskTerminalError,
)
from a2akit.client.transport.base import Transport

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from a2a_pydantic.v03 import AgentCard as V03AgentCard
    from a2a_pydantic.v03 import Message as V03Message
    from a2a_pydantic.v03 import MessageSendParams as V03MessageSendParams
    from a2a_pydantic.v03 import Task as V03Task

    from a2akit.client.result import StreamEvent as ClientStreamEvent

A2A_VERSION = "1.0"


def _to_v10_request(params: Any) -> SendMessageRequest:
    """Accept v03.MessageSendParams or v10.SendMessageRequest, return v10."""
    if isinstance(params, SendMessageRequest):
        return params
    from a2a_pydantic import convert_to_v10

    v10_msg = convert_to_v10(params.message)
    config = getattr(params, "configuration", None)
    v10_config: SendMessageConfiguration | None = None
    if config is not None:
        cfg_kwargs: dict[str, Any] = {}
        if getattr(config, "blocking", None) is not None:
            cfg_kwargs["returnImmediately"] = not config.blocking
        if getattr(config, "accepted_output_modes", None):
            cfg_kwargs["acceptedOutputModes"] = list(config.accepted_output_modes)
        if getattr(config, "history_length", None) is not None:
            cfg_kwargs["historyLength"] = config.history_length
        if cfg_kwargs:
            v10_config = SendMessageConfiguration(**cfg_kwargs)
    return SendMessageRequest(message=v10_msg, configuration=v10_config)


def _reason_from_error(err: dict[str, Any]) -> str:
    """Pull the ``reason`` off the first ``google.rpc.ErrorInfo`` detail."""
    for info in err.get("data") or []:
        if isinstance(info, dict) and info.get("@type", "").endswith("google.rpc.ErrorInfo"):
            return str(info.get("reason", ""))
    return ""


def _exc_from_reason(reason: str, task_id: str, message: str) -> Exception:
    if reason == "TASK_NOT_FOUND":
        return TaskNotFoundError(task_id)
    if reason == "TASK_NOT_CANCELABLE":
        return TaskNotCancelableError(task_id)
    if reason == "TASK_TERMINAL_STATE":
        return TaskTerminalError(task_id)
    return A2AClientError(message or reason)


class JsonRpcV10Transport(Transport):
    """JSON-RPC 2.0 transport speaking A2A v1.0 natively."""

    def __init__(self, http_client: httpx.AsyncClient, base_url: str) -> None:
        self._http = http_client
        self._url = base_url.rstrip("/") or "/"

    def _headers(self) -> dict[str, str]:
        headers = {"A2A-Version": A2A_VERSION}
        from a2akit.telemetry._client import inject_trace_context

        inject_trace_context(headers)
        return headers

    def _envelope(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        env: dict[str, Any] = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method}
        if params is not None:
            env["params"] = params
        return env

    def _handle_response(self, data: dict[str, Any], task_id: str | None = None) -> Any:
        """Return ``result`` or raise a typed exception from the ``error.data`` ErrorInfo."""
        if "error" in data:
            err = data["error"]
            message = str(err.get("message", "Unknown error"))
            reason = _reason_from_error(err)
            tid = task_id or "unknown"
            # Spec §3.6.2 — A2A-Version rejection.
            if "Unsupported A2A version" in message:
                raise ProtocolVersionMismatchError(
                    client_version=A2A_VERSION,
                    server_version="unknown",
                    detail=message,
                )
            if reason:
                raise _exc_from_reason(reason, tid, message)
            # Fallback when the server didn't emit ErrorInfo.
            code = err.get("code", 0)
            if code == -32001:
                raise TaskNotFoundError(tid)
            raise ProtocolError(f"JSON-RPC error {code}: {message}")
        return data.get("result")

    async def _call(
        self, method: str, params: dict[str, Any] | None = None, task_id: str | None = None
    ) -> Any:
        envelope = self._envelope(method, params)
        try:
            response = await self._http.post(self._url, json=envelope, headers=self._headers())
        except httpx.RequestError as exc:
            raise ProtocolError(f"Request failed: {exc}") from exc

        if not response.is_success:
            # v1.0 can also return HTTP 400 with a google.rpc.Status for
            # A2A-Version rejection (before the request reaches the JSON-RPC
            # dispatcher). Surface that as a typed mismatch.
            body_text = response.text or ""
            if response.status_code == 400 and "Unsupported A2A version" in body_text:
                raise ProtocolVersionMismatchError(
                    client_version=A2A_VERSION,
                    server_version="unknown",
                    detail=body_text,
                )
            raise ProtocolError(f"HTTP {response.status_code}: {response.text}")

        try:
            data = response.json()
        except Exception as exc:
            raise ProtocolError(f"Invalid JSON response: {exc}") from exc
        return self._handle_response(data, task_id=task_id)

    # -- unary -----------------------------------------------------------------

    async def send_message(
        self, params: V03MessageSendParams | SendMessageRequest
    ) -> V03Task | V03Message:
        v10_params = _to_v10_request(params)
        body = v10_params.model_dump(mode="json", by_alias=True, exclude_none=True)
        result = await self._call("SendMessage", body)
        if not isinstance(result, dict):
            raise ProtocolError(f"Unexpected result shape: {type(result).__name__}")
        if "task" in result:
            return convert_to_v03(Task.model_validate(result["task"]))
        if "message" in result:
            return convert_to_v03(Message.model_validate(result["message"]))
        raise ProtocolError("SendMessageResponse missing 'task'/'message'")

    async def get_task(self, task_id: str, history_length: int | None = None) -> V03Task:
        params: dict[str, Any] = {"id": task_id}
        if history_length is not None:
            params["historyLength"] = history_length
        result = await self._call("GetTask", params, task_id=task_id)
        return convert_to_v03(Task.model_validate(result))

    async def list_tasks(self, query: dict[str, Any]) -> dict[str, Any]:
        result = await self._call("ListTasks", query)
        if isinstance(result, dict) and isinstance(result.get("tasks"), list):
            v03_tasks = []
            for t in result["tasks"]:
                v10_t = Task.model_validate(t)
                v03_t = convert_to_v03(v10_t)
                v03_tasks.append(v03_t.model_dump(mode="json", by_alias=True, exclude_none=True))
            result = {**result, "tasks": v03_tasks}
        return result  # type: ignore[no-any-return]

    async def cancel_task(self, task_id: str) -> V03Task:
        result = await self._call("CancelTask", {"id": task_id}, task_id=task_id)
        return convert_to_v03(Task.model_validate(result))

    # -- streaming -------------------------------------------------------------

    async def stream_message(
        self, params: V03MessageSendParams | SendMessageRequest
    ) -> AsyncIterator[ClientStreamEvent]:
        v10_params = _to_v10_request(params)
        body = v10_params.model_dump(mode="json", by_alias=True, exclude_none=True)
        envelope = self._envelope("SendStreamingMessage", body)
        async for event in self._sse_stream(envelope):
            yield event

    async def subscribe_task(
        self, task_id: str, *, last_event_id: str | None = None
    ) -> AsyncIterator[ClientStreamEvent]:
        envelope = self._envelope("SubscribeToTask", {"id": task_id})
        headers = self._headers()
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
        async for event in self._sse_stream(envelope, extra_headers=headers, task_id=task_id):
            yield event

    async def _sse_stream(
        self,
        envelope: dict[str, Any],
        *,
        extra_headers: dict[str, str] | None = None,
        task_id: str | None = None,
    ) -> AsyncIterator[ClientStreamEvent]:
        headers = extra_headers or self._headers()
        async with self._http.stream(
            "POST",
            self._url,
            json=envelope,
            headers=headers,
            timeout=httpx.Timeout(5.0, read=None),
        ) as response:
            if not response.is_success:
                await response.aread()
                raise ProtocolError(f"HTTP {response.status_code}: {response.text}")
            content_type = response.headers.get("content-type", "")
            if "text/event-stream" not in content_type:
                await response.aread()
                try:
                    data = response.json()
                except Exception as exc:
                    raise ProtocolError(f"Expected SSE stream, got {content_type}") from exc
                self._handle_response(data, task_id=task_id)
                return

            data_lines: list[str] = []
            current_event_id: str | None = None
            async for line in response.aiter_lines():
                line = line.strip()
                if line.startswith("id:"):
                    current_event_id = line[len("id:") :].strip() or None
                elif line.startswith("data:"):
                    data_lines.append(line[len("data:") :].strip())
                elif not line and data_lines:
                    payload = "\n".join(data_lines)
                    data_lines = []
                    if payload:
                        event = self._decode_sse_payload(
                            payload, event_id=current_event_id, task_id=task_id
                        )
                        if event is None:
                            return
                        yield event
                    current_event_id = None

            # Flush remaining data (stream may end without a trailing blank line).
            if data_lines:
                payload = "\n".join(data_lines)
                if payload:
                    event = self._decode_sse_payload(
                        payload, event_id=current_event_id, task_id=task_id
                    )
                    if event is not None:
                        yield event

    def _decode_sse_payload(
        self,
        payload: str,
        *,
        event_id: str | None,
        task_id: str | None,
    ) -> ClientStreamEvent | None:
        """Decode one SSE data payload into a StreamEvent.

        Returns ``None`` when the payload was a JSON-RPC error envelope that
        ``_handle_response`` swallowed (caller should stop the stream).
        """
        from a2akit.client.result import StreamEvent as ClientStreamEvent

        try:
            outer = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ProtocolError(f"Invalid JSON in SSE data: {exc}") from exc
        # Unwrap JSON-RPC success envelope → ``result`` carries the
        # v1.0 wrapped discriminator form.
        if isinstance(outer, dict) and "error" in outer:
            self._handle_response(outer, task_id=task_id)
            return None
        raw = outer.get("result", outer) if isinstance(outer, dict) else outer
        if not isinstance(raw, dict):
            raise ProtocolError(f"Unexpected SSE payload: {type(raw).__name__}")
        if "taskStatusUpdate" in raw:
            v10_evt = TaskStatusUpdateEvent.model_validate(raw["taskStatusUpdate"])
            return ClientStreamEvent.from_raw(convert_to_v03(v10_evt), event_id=event_id)
        if "taskArtifactUpdate" in raw:
            v10_art = TaskArtifactUpdateEvent.model_validate(raw["taskArtifactUpdate"])
            return ClientStreamEvent.from_raw(convert_to_v03(v10_art), event_id=event_id)
        if "task" in raw:
            return ClientStreamEvent.from_raw(
                convert_to_v03(Task.model_validate(raw["task"])),
                event_id=event_id,
            )
        if "message" in raw:
            return ClientStreamEvent.from_raw(
                convert_to_v03(Message.model_validate(raw["message"])),
                event_id=event_id,
            )
        if "status" in raw and "id" in raw:
            return ClientStreamEvent.from_raw(
                convert_to_v03(Task.model_validate(raw)),
                event_id=event_id,
            )
        if "role" in raw and "parts" in raw:
            return ClientStreamEvent.from_raw(
                convert_to_v03(Message.model_validate(raw)),
                event_id=event_id,
            )
        raise ProtocolError(f"Unknown v1.0 JSON-RPC SSE event shape: {list(raw)}")

    # -- push + card + health --------------------------------------------------

    async def set_push_config(self, task_id: str, config: dict[str, Any]) -> dict[str, Any]:
        # v1.0 expects a flat TaskPushNotificationConfig; the caller's dict may
        # already be flat or carry a legacy wrapper — pass through either way.
        params = {"taskId": task_id, **config}
        result = await self._call("CreateTaskPushNotificationConfig", params, task_id=task_id)
        return result  # type: ignore[no-any-return]

    async def get_push_config(self, task_id: str, config_id: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"taskId": task_id}
        if config_id:
            params["id"] = config_id
        result = await self._call("GetTaskPushNotificationConfig", params, task_id=task_id)
        return result  # type: ignore[no-any-return]

    async def list_push_configs(self, task_id: str) -> list[dict[str, Any]]:
        result = await self._call(
            "ListTaskPushNotificationConfigs", {"taskId": task_id}, task_id=task_id
        )
        if isinstance(result, dict):
            configs = result.get("configs") or []
            return list(configs)
        return list(result)

    async def delete_push_config(self, task_id: str, config_id: str) -> None:
        await self._call(
            "DeleteTaskPushNotificationConfig",
            {"taskId": task_id, "id": config_id},
            task_id=task_id,
        )

    async def get_extended_card(self) -> V03AgentCard:
        result = await self._call("GetExtendedAgentCard")
        return convert_to_v03(AgentCard.model_validate(result))

    async def health_check(self) -> None:
        await self._call("health")

    async def close(self) -> None:
        """No-op — the caller owns the ``httpx.AsyncClient``."""


__all__ = ["JsonRpcV10Transport"]
