"""Native A2A v1.0 REST transport for :class:`A2AClient`.

Differences from the v0.3 ``RestTransport``:

- Paths do not carry the ``/v1/`` prefix.
- Message-send returns a ``SendMessageResponse`` (``{"task": {...}}`` or
  ``{"message": {...}}`` oneof), not a bare Task/Message.
- Error bodies follow ``google.rpc.Status`` (``{"error": {"code", "status",
  "message", "details": [{"@type", "reason", ...}]}}``); the reason string
  drives exception selection, not a numeric JSON-RPC code.
- SSE events use the wrapped discriminator:
  ``{"taskStatusUpdate": {...}}``, ``{"taskArtifactUpdate": {...}, "index": N}``,
  bare Task/Message.
- Stream close = terminal event. No ``final`` flag on the wire.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

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
    """Accept either v03.MessageSendParams or v10.SendMessageRequest.

    The :class:`A2AClient` today builds v0.3 ``MessageSendParams``; when it
    picks a native v1.0 transport we convert here so callers don't need to.
    """
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


def _exc_from_reason(reason: str, task_id: str, message: str) -> Exception:
    """Map a ``google.rpc.ErrorInfo.reason`` string to the client's exception type."""
    if reason == "TASK_NOT_FOUND":
        return TaskNotFoundError(task_id)
    if reason == "TASK_NOT_CANCELABLE":
        return TaskNotCancelableError(task_id)
    if reason == "TASK_TERMINAL_STATE":
        return TaskTerminalError(task_id)
    return A2AClientError(message or reason)


class RestV10Transport(Transport):
    """HTTP+JSON/REST transport speaking A2A v1.0 natively."""

    def __init__(self, http_client: httpx.AsyncClient, base_url: str) -> None:
        self._http = http_client
        self._base = base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        headers = {"A2A-Version": A2A_VERSION}
        from a2akit.telemetry._client import inject_trace_context

        inject_trace_context(headers)
        return headers

    def _url(self, path: str) -> str:
        return f"{self._base}{path}"

    def _check_error(self, response: httpx.Response, task_id: str | None = None) -> None:
        """Parse a ``google.rpc.Status`` error envelope and raise the mapped exception."""
        if response.is_success:
            return
        status = response.status_code
        reason = ""
        message = ""
        try:
            body = response.json()
            err = body.get("error") if isinstance(body, dict) else None
            if isinstance(err, dict):
                message = str(err.get("message", ""))
                for info in err.get("details") or []:
                    if isinstance(info, dict) and info.get("@type", "").endswith(
                        "google.rpc.ErrorInfo"
                    ):
                        reason = str(info.get("reason", ""))
                        break
        except Exception:
            message = response.text or f"HTTP {status}"

        tid = task_id or "unknown"
        # Spec §3.6.2 — server rejected the A2A-Version header. The v0.3 server
        # uses the legacy ``{"code": -32009, "message": ...}`` shape; the v1.0
        # server emits INVALID_ARGUMENT with "Unsupported A2A version" in the
        # message. Either way the client should surface a typed mismatch.
        if status == 400 and "Unsupported A2A version" in message:
            raise ProtocolVersionMismatchError(
                client_version=A2A_VERSION,
                server_version="unknown",
                detail=message,
            )
        if reason:
            raise _exc_from_reason(reason, tid, message)
        # Fallback when the server didn't include ErrorInfo.
        if status == 404:
            raise TaskNotFoundError(tid)
        if status == 400:
            raise A2AClientError(message or "Bad request")
        raise ProtocolError(f"HTTP {status}: {message}")

    # -- unary -----------------------------------------------------------------

    async def send_message(
        self, params: V03MessageSendParams | SendMessageRequest
    ) -> V03Task | V03Message:
        v10_params = _to_v10_request(params)
        body = v10_params.model_dump(mode="json", by_alias=True, exclude_none=True)
        response = await self._http.post(
            self._url("/message:send"), json=body, headers=self._headers()
        )
        self._check_error(response)
        data = response.json()
        # SendMessageResponse oneof: {"task": {...}} or {"message": {...}}.
        if not isinstance(data, dict):
            raise ProtocolError(f"Unexpected response body: {type(data).__name__}")
        if "task" in data:
            return convert_to_v03(Task.model_validate(data["task"]))
        if "message" in data:
            return convert_to_v03(Message.model_validate(data["message"]))
        raise ProtocolError("SendMessageResponse missing 'task'/'message'")

    async def get_task(self, task_id: str, history_length: int | None = None) -> V03Task:
        params: dict[str, Any] = {}
        if history_length is not None:
            params["historyLength"] = history_length
        response = await self._http.get(
            self._url(f"/tasks/{quote(task_id, safe='')}"), params=params, headers=self._headers()
        )
        self._check_error(response, task_id=task_id)
        return convert_to_v03(Task.model_validate(response.json()))

    async def list_tasks(self, query: dict[str, Any]) -> dict[str, Any]:
        response = await self._http.get(
            self._url("/tasks"),
            params={k: v for k, v in query.items() if v is not None},
            headers=self._headers(),
        )
        self._check_error(response)
        body = response.json()
        # v1.0 returns v10.Task shapes; convert each for the v03-shaped client layer.
        if isinstance(body, dict) and isinstance(body.get("tasks"), list):
            v03_tasks = []
            for t in body["tasks"]:
                v10_t = Task.model_validate(t)
                v03_t = convert_to_v03(v10_t)
                v03_tasks.append(v03_t.model_dump(mode="json", by_alias=True, exclude_none=True))
            body = {**body, "tasks": v03_tasks}
        return body  # type: ignore[no-any-return]

    async def cancel_task(self, task_id: str) -> V03Task:
        response = await self._http.post(
            self._url(f"/tasks/{quote(task_id, safe='')}:cancel"), headers=self._headers()
        )
        self._check_error(response, task_id=task_id)
        return convert_to_v03(Task.model_validate(response.json()))

    # -- streaming -------------------------------------------------------------

    async def stream_message(
        self, params: V03MessageSendParams | SendMessageRequest
    ) -> AsyncIterator[ClientStreamEvent]:
        v10_params = _to_v10_request(params)
        body = v10_params.model_dump(mode="json", by_alias=True, exclude_none=True)
        async with self._http.stream(
            "POST",
            self._url("/message:stream"),
            json=body,
            headers=self._headers(),
            timeout=httpx.Timeout(5.0, read=None),
        ) as response:
            if not response.is_success:
                await response.aread()
                self._check_error(response)
            content_type = response.headers.get("content-type", "")
            if "text/event-stream" not in content_type:
                await response.aread()
                self._check_error(response)
                return
            async for event in _parse_v10_sse(response):
                yield event

    async def subscribe_task(
        self, task_id: str, *, last_event_id: str | None = None
    ) -> AsyncIterator[ClientStreamEvent]:
        headers = self._headers()
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
        async with self._http.stream(
            "POST",
            self._url(f"/tasks/{quote(task_id, safe='')}:subscribe"),
            headers=headers,
            timeout=httpx.Timeout(5.0, read=None),
        ) as response:
            if not response.is_success:
                await response.aread()
                self._check_error(response, task_id=task_id)
            content_type = response.headers.get("content-type", "")
            if "text/event-stream" not in content_type:
                await response.aread()
                self._check_error(response, task_id=task_id)
                return
            async for event in _parse_v10_sse(response):
                yield event

    # -- push + card + health --------------------------------------------------

    async def set_push_config(self, task_id: str, config: dict[str, Any]) -> dict[str, Any]:
        response = await self._http.post(
            self._url(f"/tasks/{quote(task_id, safe='')}/pushNotificationConfigs"),
            json=config,
            headers=self._headers(),
        )
        self._check_error(response, task_id=task_id)
        return response.json()  # type: ignore[no-any-return]

    async def get_push_config(self, task_id: str, config_id: str | None = None) -> dict[str, Any]:
        path = f"/tasks/{quote(task_id, safe='')}/pushNotificationConfigs"
        if config_id:
            path = f"{path}/{quote(config_id, safe='')}"
        response = await self._http.get(self._url(path), headers=self._headers())
        self._check_error(response, task_id=task_id)
        return response.json()  # type: ignore[no-any-return]

    async def list_push_configs(self, task_id: str) -> list[dict[str, Any]]:
        response = await self._http.get(
            self._url(f"/tasks/{quote(task_id, safe='')}/pushNotificationConfigs"),
            headers=self._headers(),
        )
        self._check_error(response, task_id=task_id)
        body = response.json()
        # v1.0 returns ``{"configs": [...]}``; be permissive and accept bare lists.
        if isinstance(body, dict):
            configs = body.get("configs") or []
            return list(configs)
        return list(body)

    async def delete_push_config(self, task_id: str, config_id: str) -> None:
        response = await self._http.delete(
            self._url(
                f"/tasks/{quote(task_id, safe='')}/pushNotificationConfigs/{quote(config_id, safe='')}"
            ),
            headers=self._headers(),
        )
        self._check_error(response, task_id=task_id)

    async def get_extended_card(self) -> V03AgentCard:
        response = await self._http.get(self._url("/card"), headers=self._headers())
        self._check_error(response)
        v10_card = AgentCard.model_validate(response.json())
        return convert_to_v03(v10_card)

    async def health_check(self) -> None:
        resp = await self._http.get(self._url("/health"), headers=self._headers())
        resp.raise_for_status()

    async def close(self) -> None:
        """No-op — the caller owns the ``httpx.AsyncClient``."""


# -- v1.0 SSE parser ----------------------------------------------------------


def _decode_v10_sse_payload(payload: str, event_id: str | None) -> ClientStreamEvent | None:
    """Decode one v1.0 SSE data payload into a StreamEvent (None if not a dict)."""
    from a2akit.client.result import StreamEvent as ClientStreamEvent

    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"Invalid JSON in SSE data: {exc}") from exc
    if not isinstance(raw, dict):
        return None
    if "taskStatusUpdate" in raw:
        v10_evt = TaskStatusUpdateEvent.model_validate(raw["taskStatusUpdate"])
        return ClientStreamEvent.from_raw(convert_to_v03(v10_evt), event_id=event_id)
    if "taskArtifactUpdate" in raw:
        v10_art = TaskArtifactUpdateEvent.model_validate(raw["taskArtifactUpdate"])
        return ClientStreamEvent.from_raw(convert_to_v03(v10_art), event_id=event_id)
    # Bare Task / Message — distinguish by structure (Task has ``status``).
    if "status" in raw and "id" in raw:
        return ClientStreamEvent.from_raw(
            convert_to_v03(Task.model_validate(raw)), event_id=event_id
        )
    if "role" in raw and "parts" in raw:
        return ClientStreamEvent.from_raw(
            convert_to_v03(Message.model_validate(raw)), event_id=event_id
        )
    raise ProtocolError(f"Unknown v1.0 SSE event shape: {list(raw)}")


async def _parse_v10_sse(
    response: httpx.Response,
) -> AsyncIterator[ClientStreamEvent]:
    """Parse a v1.0 SSE stream.

    Accepts:
    - bare ``Task`` / ``Message`` snapshot JSON
    - ``{"taskStatusUpdate": {...}}``
    - ``{"taskArtifactUpdate": {...}, "index": N}``
    Stream end = TCP close (no ``final`` flag).
    """
    current_event_id: str | None = None
    data_lines: list[str] = []

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
                event = _decode_v10_sse_payload(payload, current_event_id)
                if event is not None:
                    yield event
            current_event_id = None

    # Flush remaining data (stream may end without a trailing blank line).
    if data_lines:
        payload = "\n".join(data_lines)
        if payload:
            event = _decode_v10_sse_payload(payload, current_event_id)
            if event is not None:
                yield event


__all__ = ["RestV10Transport"]
