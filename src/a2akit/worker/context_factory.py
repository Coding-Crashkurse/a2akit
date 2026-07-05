"""ContextFactory — builds TaskContextImpl from A2A Message objects."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from a2akit._parts import extract_text
from a2akit.worker.base import (
    HistoryMessage,
    PreviousArtifact,
    TaskContextImpl,
)

if TYPE_CHECKING:
    from a2a_pydantic import v10

    from a2akit.broker.base import CancelScope
    from a2akit.dependencies import DependencyContainer
    from a2akit.event_emitter import EventEmitter
    from a2akit.storage import Storage


class ContextFactory:
    """Translates an A2A Message into a clean TaskContextImpl."""

    def __init__(
        self,
        emitter: EventEmitter,
        storage: Storage,
        *,
        deps: DependencyContainer | None = None,
    ) -> None:
        self._emitter = emitter
        self._storage = storage
        self._deps = deps

    async def build(
        self,
        message: v10.Message,
        cancel_event: CancelScope,
        *,
        is_new_task: bool = False,
        request_context: dict[str, Any] | None = None,
        configuration: v10.SendMessageConfiguration | None = None,
    ) -> TaskContextImpl:
        """Construct a TaskContextImpl from a broker message."""
        user_text = extract_text(list(message.parts))

        history: list[HistoryMessage] = []
        previous_artifacts: list[PreviousArtifact] = []

        if not is_new_task and message.task_id:
            task = await self._storage.load_task(message.task_id)
            if task:
                history = self._convert_history(task.history or [], message.message_id or "")
                previous_artifacts = self._convert_artifacts(task.artifacts or [])

        accepted: list[str] | None = None
        if configuration and configuration.accepted_output_modes:
            accepted = list(configuration.accepted_output_modes)

        # initial_version starts as None; WorkerAdapter seeds ctx._version
        # from the working-state transition's return value before calling
        # the user worker, closing the OCC chain.
        ctx = TaskContextImpl(
            task_id=message.task_id or "",
            context_id=message.context_id,
            message_id=message.message_id or "",
            user_text=user_text,
            parts=message.parts,
            metadata=dict(message.metadata or {}),
            emitter=self._emitter,
            cancel_event=cancel_event,
            storage=self._storage,
            history=history,
            previous_artifacts=previous_artifacts,
            request_context=request_context,
            deps=self._deps,
            accepted_output_modes=accepted,
        )
        # REQ-01: Extract message-level fields for worker access.
        ctx._reference_task_ids = list(message.reference_task_ids or [])
        ctx._message_extensions = list(message.extensions or [])
        return ctx

    @staticmethod
    def _convert_history(
        messages: list[v10.Message], current_message_id: str
    ) -> list[HistoryMessage]:
        """Convert v10 Messages to HistoryMessage wrappers, excluding current.

        v10 Role values are ``ROLE_USER`` / ``ROLE_AGENT`` on the wire. Normalize
        to the ergonomic ``"user"`` / ``"agent"`` form that worker authors expect
        — the spec calls this out explicitly (§3.5) as a deliberate papercut
        removal.
        """
        result: list[HistoryMessage] = []
        for msg in messages:
            msg_id = msg.message_id or ""
            if msg_id == current_message_id and current_message_id:
                continue
            text_parts = [p.text for p in msg.parts if p.text]
            raw_role = msg.role.name if msg.role else ""
            normalized_role = "user" if raw_role == "role_user" else "agent"
            result.append(
                HistoryMessage(
                    role=normalized_role,
                    text="\n".join(text_parts),
                    parts=list(msg.parts),
                    message_id=msg_id,
                )
            )
        return result

    @staticmethod
    def _convert_artifacts(artifacts: list[v10.Artifact]) -> list[PreviousArtifact]:
        """Convert v10.Artifacts to PreviousArtifact wrappers."""
        return [
            PreviousArtifact(
                artifact_id=a.artifact_id,
                name=a.name or None,
                parts=list(a.parts) if a.parts else [],
            )
            for a in artifacts
        ]
