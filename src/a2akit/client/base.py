"""A2AClient — dev-first client for interacting with A2A agents."""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any, Self, TypeVar

import httpx
from a2a_pydantic.v03 import (
    AgentCapabilities,
    AgentCard,
    Message,
    MessageSendConfiguration,
    MessageSendParams,
    Part,
    PushNotificationConfig,
    Role,
    Task,
    TextPart,
    TransportProtocol,
)

# Re-exported above from ``a2a_pydantic.v03``. Spec §4 step 3 adds dedicated
# v1.0 client transports; the client keeps a v03-shaped view today so it
# interoperates with the v0.3 compat layer on the server side.
from a2akit.client.errors import (
    AgentCapabilityError,
    AgentNotFoundError,
    NotConnectedError,
    ProtocolVersionMismatchError,
)
from a2akit.client.result import ClientResult, ListResult, StreamEvent
from a2akit.client.transport.jsonrpc import JsonRpcTransport
from a2akit.client.transport.rest import RestTransport
from a2akit.telemetry._client import traced_client_method
from a2akit.telemetry._semantic import (
    SPAN_CLIENT_CANCEL,
    SPAN_CLIENT_CONNECT,
    SPAN_CLIENT_GET_TASK,
    SPAN_CLIENT_LIST_TASKS,
    SPAN_CLIENT_SEND,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from a2akit.client.transport.base import Transport

_T = TypeVar("_T")


def _project_v10_card_to_v03(v10_card: Any) -> AgentCard:
    """Build a v0.3 AgentCard view from a parsed v1.0 AgentCard.

    Used so existing callers that read ``client.agent_card.url`` and
    ``client.agent_card.additional_interfaces`` keep working even when the
    server sent a v1.0 card. Not a full conversion — only the fields the
    rest of the client reads.
    """
    from a2a_pydantic.v03 import (
        AgentCapabilities as V03Capabilities,
    )
    from a2a_pydantic.v03 import (
        AgentInterface as V03Interface,
    )
    from a2a_pydantic.v03 import (
        TransportProtocol as V03Transport,
    )

    ifaces = list(v10_card.supported_interfaces or [])
    primary = ifaces[0] if ifaces else None
    primary_url = (primary.url if primary else "") or ""
    primary_binding = (primary.protocol_binding if primary else "HTTP+JSON") or "HTTP+JSON"
    binding_upper = primary_binding.upper()
    transport = V03Transport.jsonrpc if binding_upper == "JSONRPC" else V03Transport.http_json

    additional: list[V03Interface] = []
    for iface in ifaces[1:]:
        add_t = (
            V03Transport.jsonrpc
            if (iface.protocol_binding or "").upper() == "JSONRPC"
            else V03Transport.http_json
        )
        additional.append(V03Interface(url=iface.url or primary_url, transport=add_t))

    caps = v10_card.capabilities
    return AgentCard(
        protocol_version="1.0.0",
        name=v10_card.name,
        description=v10_card.description,
        url=primary_url,
        preferred_transport=transport,
        additional_interfaces=additional or None,
        version=v10_card.version,
        capabilities=V03Capabilities(
            streaming=caps.streaming if caps else False,
            push_notifications=caps.push_notifications if caps else False,
        ),
        default_input_modes=list(v10_card.default_input_modes or []),
        default_output_modes=list(v10_card.default_output_modes or []),
        skills=[],
        supports_authenticated_extended_card=bool(
            caps and getattr(caps, "extended_agent_card", False)
        ),
    )


class A2AClient:
    """Client for interacting with A2A protocol agents.

    Usage::

        async with A2AClient("http://localhost:8000") as client:
            result = await client.send("Hello!")
            print(result.text)
    """

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        protocol: str | None = None,
        httpx_client: httpx.AsyncClient | None = None,
        card_validator: Callable[[AgentCard, bytes], None] | None = None,
        max_retries: int = 0,
        retry_delay: float = 1.0,
        retry_on: tuple[type[Exception], ...] = (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
        ),
        # Spec §19 — JWS signature verification on the discovery card.
        # ``"off"``: skip; ``"soft"`` (default): verify if present, warn on
        # missing; ``"strict"``: require at least one verifiable signature.
        # jku fetching only happens when an explicit ``allowed_jku_hosts``
        # allowlist is provided; with the default (``None``) verification
        # keys must come from ``trusted_signing_keys``.
        verify_signatures: str = "soft",
        trusted_signing_keys: list[Any] | None = None,
        allow_jku_fetch: bool = True,
        allowed_jku_hosts: set[str] | None = None,
    ) -> None:
        self._url = url.rstrip("/")
        self._headers = headers or {}
        self._timeout = timeout
        self._protocol_preference = protocol
        self._external_http = httpx_client is not None
        self._http_client = httpx_client
        self._card_validator = card_validator
        self._agent_card: AgentCard | None = None
        self._transport: Transport | None = None
        self._connected = False
        self._active_protocol: str = ""
        self._active_wire_version: str = "0.3"
        # Retry config
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        self._retry_on = retry_on
        # Signature-verification config
        if verify_signatures not in ("off", "soft", "strict"):
            raise ValueError(
                f"verify_signatures must be 'off', 'soft', or 'strict' (got {verify_signatures!r})"
            )
        self._verify_signatures = verify_signatures
        self._trusted_signing_keys = trusted_signing_keys
        self._allow_jku_fetch = allow_jku_fetch
        self._allowed_jku_hosts = allowed_jku_hosts

    @traced_client_method(SPAN_CLIENT_CONNECT)
    async def connect(self) -> None:
        """Discover agent and prepare transport.

        Implements transport fallback (Spec §5.6.3): if the preferred
        transport fails a health check, tries ``additionalInterfaces``
        from the agent card before giving up.
        """
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                headers=self._headers,
                timeout=self._timeout,
            )

        card_url = f"{self._url}/.well-known/agent-card.json"
        try:
            resp = await self._http_client.get(card_url)
        except httpx.RequestError as exc:
            raise AgentNotFoundError(self._url, f"Connection failed: {exc}") from exc

        if resp.status_code != 200:
            raise AgentNotFoundError(self._url, f"HTTP {resp.status_code}")

        try:
            card_data = resp.json()
        except Exception as exc:
            raise AgentNotFoundError(self._url, f"Invalid agent card: {exc}") from exc

        # Pick between v10.AgentCard (has ``supportedInterfaces[]``) and
        # v0.3 AgentCard (has top-level ``url`` + ``preferredTransport``).
        # Store both decisions on the client so transport construction can
        # pick the right client-side transport per interface.
        self._card_v10: Any = None
        if isinstance(card_data, dict) and "supportedInterfaces" in card_data:
            try:
                from a2a_pydantic.v10 import AgentCard as V10AgentCard

                self._card_v10 = V10AgentCard.model_validate(card_data)
            except Exception as exc:
                raise AgentNotFoundError(self._url, f"Invalid v1.0 agent card: {exc}") from exc
            # Also expose a v03-shaped view for existing callers that read
            # ``self.agent_card.url`` / ``.additional_interfaces``. Build a
            # best-effort v0.3 projection from the v1.0 card.
            self._agent_card = _project_v10_card_to_v03(self._card_v10)
        else:
            try:
                self._agent_card = AgentCard.model_validate(card_data)
            except Exception as exc:
                raise AgentNotFoundError(self._url, f"Invalid agent card: {exc}") from exc

        # JWS signature verification (spec §19). For v1.0 cards, verify
        # against the parsed v1.0 card — the v0.3 projection built above never
        # carries ``signatures``, so verifying against it would silently skip
        # (soft) or spuriously fail (strict) validly signed cards. For v0.3
        # cards the library doesn't define a signatures field, so the check is
        # a no-op unless the caller opts in explicitly.
        if self._verify_signatures != "off":
            card_to_verify = self._card_v10 if self._card_v10 is not None else self._agent_card
            sig_attr = getattr(card_to_verify, "signatures", None)
            if sig_attr or self._verify_signatures == "strict":
                try:
                    from a2akit._signatures import (
                        AgentCardSignatureError,
                        verify_agent_card,
                    )
                except ImportError as exc:
                    raise AgentNotFoundError(
                        self._url,
                        "Signature verification requires a2akit[signatures]. "
                        "Install with: pip install a2akit[signatures]",
                    ) from exc
                try:
                    await verify_agent_card(
                        card_to_verify,
                        resp.content,
                        mode=self._verify_signatures,
                        trusted_keys=self._trusted_signing_keys,
                        allow_jku=self._allow_jku_fetch,
                        allowed_jku_hosts=self._allowed_jku_hosts,
                    )
                except AgentCardSignatureError as exc:
                    raise AgentNotFoundError(
                        self._url, f"Signature verification failed: {exc}"
                    ) from exc

        if self._card_validator is not None:
            self._card_validator(self._agent_card, resp.content)

        # Build transport candidates: preferred first, then additionalInterfaces
        candidates = self._build_transport_candidates(self._agent_card)

        # Pre-flight: the server publishes exactly one A2A wire version on its
        # card. If none of the candidates speak a version this client supports
        # ("0.3" or "1.0"), fail fast with a typed mismatch rather than letting
        # the first request round-trip and get rejected.
        supported = {"0.3", "1.0"}
        card_versions = {wv for _, _, wv in candidates}
        unspeakable = card_versions - supported
        if card_versions and not (card_versions & supported):
            raise ProtocolVersionMismatchError(
                client_version="0.3|1.0",
                server_version=",".join(sorted(card_versions)) or "unknown",
                detail="Agent card advertises only unsupported A2A protocol versions.",
            )
        if unspeakable:
            logger_msg = (
                f"Ignoring unsupported interface versions on agent card: {sorted(unspeakable)}"
            )
            import logging

            logging.getLogger(__name__).info(logger_msg)

        errors: list[tuple[str, str, Exception]] = []
        for url, proto, wire_version in candidates:
            if wire_version not in supported:
                continue
            transport: Transport | None = None
            try:
                transport = self._create_transport(proto, url, wire_version=wire_version)
                await transport.health_check()
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                errors.append((url, proto, exc))
                continue
            except Exception as exc:
                # Transport construction itself failed — try next candidate.
                if transport is None:
                    errors.append((url, proto, exc))
                    continue
                # Server errors (5xx) indicate the backend is down —
                # skip to next transport for fallback.
                from a2akit.client.errors import ProtocolError

                if isinstance(exc, ProtocolError) and "HTTP 5" in str(exc):
                    errors.append((url, proto, exc))
                    continue
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500:
                    errors.append((url, proto, exc))
                    continue
                # Other errors (404, unexpected) — accept transport anyway
                # (health endpoint may not exist, backwards compat).

            self._transport = transport
            self._active_protocol = proto
            self._active_wire_version = wire_version
            self._connected = True
            return

        # All candidates failed — only happens when all raised connect errors
        if errors:
            detail = "; ".join(f"{proto}@{url}: {exc}" for url, proto, exc in errors)
            raise AgentNotFoundError(self._url, f"All transports failed: {detail}")

        # Fallback: should not reach here, but just in case
        raise AgentNotFoundError(self._url, "No suitable transport found")

    def _build_transport_candidates(self, card: AgentCard) -> list[tuple[str, str, str]]:
        """Build ordered list of ``(url, protocol, wire_version)`` candidates.

        ``wire_version`` is either ``"1.0"`` or ``"0.3"`` — picked from the
        v1.0 ``supported_interfaces[]`` entry if the card is v1.0, else
        always ``"0.3"``.
        """
        candidates: list[tuple[str, str, str]] = []

        v10_card = getattr(self, "_card_v10", None)
        if v10_card is not None:
            # v1.0 card: iterate ``supported_interfaces[]`` preserving order.
            for iface in v10_card.supported_interfaces or []:
                proto = self._protocol_from_binding(iface.protocol_binding)
                if proto:
                    entry = (iface.url or self._url, proto, iface.protocol_version or "1.0")
                    if entry not in candidates:
                        candidates.append(entry)
            if candidates:
                return candidates

        # v0.3 fallback (or v10 card with no usable interfaces).
        preferred_proto = self._detect_protocol(card)
        candidates.append((card.url, preferred_proto, "0.3"))

        for iface in card.additional_interfaces or []:
            proto = self._protocol_from_transport(iface.transport)
            if proto:
                entry = (iface.url or card.url, proto, "0.3")
                if entry not in candidates:
                    candidates.append(entry)

        return candidates

    @staticmethod
    def _protocol_from_binding(binding: Any) -> str | None:
        """Map a v1.0 ``protocol_binding`` string to our internal protocol name."""
        val = str(binding).strip().upper()
        if val == "HTTP+JSON":
            return "http+json"
        if val == "JSONRPC":
            return "jsonrpc"
        return None

    @staticmethod
    def _protocol_from_transport(transport: Any) -> str | None:
        """Map a TransportProtocol to our internal protocol string."""
        if isinstance(transport, TransportProtocol):
            val = transport.value
        else:
            val = str(transport)

        val_lower = val.lower()
        if "http" in val_lower or ("json" in val_lower and "rpc" not in val_lower):
            return "http+json"
        if "jsonrpc" in val_lower or "rpc" in val_lower:
            return "jsonrpc"
        return None

    def _create_transport(self, proto: str, url: str, *, wire_version: str = "0.3") -> Transport:
        """Create a transport instance for the given protocol + wire version."""
        assert self._http_client is not None
        if wire_version.startswith("1"):
            if proto == "http+json":
                from a2akit.client.transport.rest_v10 import RestV10Transport

                return RestV10Transport(self._http_client, url)
            from a2akit.client.transport.jsonrpc_v10 import JsonRpcV10Transport

            return JsonRpcV10Transport(self._http_client, url)
        if proto == "http+json":
            return RestTransport(self._http_client, url)
        return JsonRpcTransport(self._http_client, url)

    def _detect_protocol(self, card: AgentCard) -> str:
        """Determine which protocol to use."""
        if self._protocol_preference:
            return self._protocol_preference

        if card.preferred_transport is not None:
            result = self._protocol_from_transport(card.preferred_transport)
            if result:
                return result

        return "jsonrpc"

    async def _with_retry(self, coro_factory: Callable[[], Awaitable[_T]]) -> _T:
        """Execute with exponential backoff retries on network errors."""
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return await coro_factory()
            except self._retry_on as exc:
                last_exc = exc
                if attempt < self._max_retries:
                    await asyncio.sleep(self._retry_delay * (2**attempt))
        raise last_exc  # type: ignore[misc]

    async def close(self) -> None:
        """Clean up resources."""
        try:
            if self._transport is not None:
                await self._transport.close()
        finally:
            self._transport = None
            try:
                if self._http_client is not None and not self._external_http:
                    await self._http_client.aclose()
            finally:
                self._http_client = None
                self._connected = False

    async def __aenter__(self) -> Self:
        try:
            await self.connect()
        except BaseException:
            await self.close()
            raise
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    def _ensure_connected(self) -> Transport:
        """Return transport or raise NotConnectedError."""
        if not self._connected or self._transport is None:
            raise NotConnectedError
        return self._transport

    @property
    def agent_card(self) -> AgentCard:
        """The discovered agent card."""
        if self._agent_card is None:
            raise NotConnectedError
        return self._agent_card

    @property
    def agent_name(self) -> str:
        """Shortcut for agent_card.name."""
        name: str = self.agent_card.name
        return name

    @property
    def capabilities(self) -> AgentCapabilities | None:
        """Shortcut for agent_card.capabilities."""
        return self.agent_card.capabilities

    @property
    def protocol(self) -> str:
        """The active protocol ('jsonrpc' or 'http+json')."""
        return self._active_protocol

    @property
    def is_connected(self) -> bool:
        """Whether the client is connected."""
        return self._connected

    async def send(
        self,
        text: str,
        *,
        task_id: str | None = None,
        context_id: str | None = None,
        blocking: bool = True,
        metadata: dict[str, Any] | None = None,
        push_url: str | None = None,
        push_token: str | None = None,
    ) -> ClientResult:
        """Send a text message to the agent.

        If ``push_url`` is provided, a push notification config is sent
        inline with the message so the server registers it before processing.
        """
        push_config: PushNotificationConfig | None = None
        if push_url:
            push_kwargs: dict[str, Any] = {"url": push_url}
            if push_token:
                push_kwargs["token"] = push_token
            push_config = PushNotificationConfig(**push_kwargs)

        parts = [Part(root=TextPart(text=text))]
        return await self.send_parts(
            parts,
            task_id=task_id,
            context_id=context_id,
            blocking=blocking,
            metadata=metadata,
            push_notification_config=push_config,
        )

    @traced_client_method(SPAN_CLIENT_SEND)
    async def send_parts(
        self,
        parts: list[Part],
        *,
        task_id: str | None = None,
        context_id: str | None = None,
        blocking: bool = True,
        metadata: dict[str, Any] | None = None,
        push_notification_config: Any = None,
    ) -> ClientResult:
        """Send raw Part objects to the agent."""
        transport = self._ensure_connected()
        params = self._build_params(
            parts,
            task_id=task_id,
            context_id=context_id,
            blocking=blocking,
            metadata=metadata,
            push_notification_config=push_notification_config,
        )

        async def _do_send() -> Task | Message:
            return await transport.send_message(params)

        result = await self._with_retry(_do_send) if self._max_retries > 0 else await _do_send()
        if isinstance(result, Message):
            return ClientResult.from_message(result)
        return ClientResult.from_task(result)

    async def stream(
        self,
        text: str,
        *,
        task_id: str | None = None,
        context_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a message to the agent, yielding events."""
        transport = self._ensure_connected()
        card = self.agent_card
        if not card.capabilities or not card.capabilities.streaming:
            raise AgentCapabilityError(card.name, "streaming")

        parts = [Part(root=TextPart(text=text))]
        params = self._build_params(
            parts,
            task_id=task_id,
            context_id=context_id,
            blocking=False,
            metadata=metadata,
        )
        async for event in transport.stream_message(params):
            yield event

    async def stream_text(
        self,
        text: str,
        *,
        task_id: str | None = None,
        context_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        """Stream only text content — yields plain strings."""
        async for event in self.stream(
            text, task_id=task_id, context_id=context_id, metadata=metadata
        ):
            if event.kind == "artifact" and event.text:
                yield event.text

    @traced_client_method(SPAN_CLIENT_GET_TASK)
    async def get_task(
        self,
        task_id: str,
        *,
        history_length: int | None = None,
    ) -> ClientResult:
        """Fetch a task by ID."""
        transport = self._ensure_connected()

        async def _do_get() -> Task:
            return await transport.get_task(task_id, history_length)

        task = await self._with_retry(_do_get) if self._max_retries > 0 else await _do_get()
        return ClientResult.from_task(task)

    @traced_client_method(SPAN_CLIENT_LIST_TASKS)
    async def list_tasks(
        self,
        *,
        context_id: str | None = None,
        status: str | None = None,
        page_size: int = 50,
        page_token: str | None = None,
        history_length: int | None = None,
    ) -> ListResult:
        """List tasks with optional filters."""
        transport = self._ensure_connected()
        query: dict[str, Any] = {"pageSize": page_size}
        if context_id is not None:
            query["contextId"] = context_id
        if status is not None:
            query["status"] = status
        if page_token is not None:
            query["pageToken"] = page_token
        if history_length is not None:
            query["historyLength"] = history_length

        async def _do_list() -> dict[str, Any]:
            return await transport.list_tasks(query)

        raw = await self._with_retry(_do_list) if self._max_retries > 0 else await _do_list()
        tasks_data = raw.get("tasks", []) if isinstance(raw, dict) else []
        results = [ClientResult.from_task(Task.model_validate(t)) for t in tasks_data]
        return ListResult(
            tasks=results,
            next_page_token=raw.get("nextPageToken") if isinstance(raw, dict) else None,
            total_size=raw.get("totalSize") if isinstance(raw, dict) else None,
            page_size=page_size,
        )

    @traced_client_method(SPAN_CLIENT_CANCEL)
    async def cancel(self, task_id: str) -> ClientResult:
        """Cancel a task by ID."""
        transport = self._ensure_connected()

        async def _do_cancel() -> Task:
            return await transport.cancel_task(task_id)

        task = await self._with_retry(_do_cancel) if self._max_retries > 0 else await _do_cancel()
        return ClientResult.from_task(task)

    async def subscribe(
        self,
        task_id: str,
        *,
        last_event_id: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Subscribe to updates for an existing task.

        Args:
            task_id: The task to subscribe to.
            last_event_id: Resume from this event ID (SSE Last-Event-ID replay).
        """
        transport = self._ensure_connected()
        card = self.agent_card
        if not card.capabilities or not card.capabilities.streaming:
            raise AgentCapabilityError(card.name, "streaming")

        async for event in transport.subscribe_task(task_id, last_event_id=last_event_id):
            yield event

    async def set_push_config(
        self,
        task_id: str,
        *,
        url: str,
        token: str | None = None,
        config_id: str | None = None,
        authentication: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Set a push notification config for a task."""
        transport = self._ensure_connected()
        config: dict[str, Any] = {"url": url}
        if token:
            config["token"] = token
        if config_id:
            config["id"] = config_id
        if authentication:
            config["authentication"] = authentication
        return await transport.set_push_config(task_id, config)

    async def get_push_config(
        self,
        task_id: str,
        config_id: str | None = None,
    ) -> dict[str, Any]:
        """Get a push notification config."""
        transport = self._ensure_connected()
        return await transport.get_push_config(task_id, config_id)

    async def list_push_configs(
        self,
        task_id: str,
    ) -> list[dict[str, Any]]:
        """List all push configs for a task."""
        transport = self._ensure_connected()
        return await transport.list_push_configs(task_id)

    async def delete_push_config(
        self,
        task_id: str,
        config_id: str,
    ) -> None:
        """Delete a push notification config."""
        transport = self._ensure_connected()
        await transport.delete_push_config(task_id, config_id)

    async def get_extended_card(self) -> AgentCard:
        """Fetch the authenticated extended agent card."""
        transport = self._ensure_connected()
        return await transport.get_extended_card()

    @staticmethod
    def _build_params(
        parts: list[Part],
        *,
        task_id: str | None,
        context_id: str | None,
        blocking: bool,
        metadata: dict[str, Any] | None,
        push_notification_config: Any = None,
    ) -> MessageSendParams:
        """Build MessageSendParams from user inputs."""
        msg_kwargs: dict[str, Any] = {
            "role": Role.user,
            "parts": parts,
            "message_id": str(uuid.uuid4()),
        }
        if task_id is not None:
            msg_kwargs["task_id"] = task_id
        # New tasks: always set context_id so retries use the same idempotency scope.
        # Follow-ups (task_id set): only set context_id if explicitly provided,
        # otherwise let the server use the task's existing context_id.
        if context_id:
            msg_kwargs["context_id"] = context_id
        elif task_id is None:
            msg_kwargs["context_id"] = str(uuid.uuid4())
        if metadata is not None:
            msg_kwargs["metadata"] = metadata

        message = Message(**msg_kwargs)

        params_kwargs: dict[str, Any] = {"message": message}
        config_kwargs: dict[str, Any] = {}
        if blocking:
            config_kwargs["blocking"] = True
        if push_notification_config is not None:
            config_kwargs["push_notification_config"] = push_notification_config
        if config_kwargs:
            params_kwargs["configuration"] = MessageSendConfiguration(**config_kwargs)

        return MessageSendParams(**params_kwargs)
