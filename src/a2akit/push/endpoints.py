"""Push notification config endpoint handlers (shared by REST and JSON-RPC)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from a2akit.push.models import PushNotificationConfig, TaskPushNotificationConfig

if TYPE_CHECKING:
    from a2akit.push.store import PushConfigStore
    from a2akit.storage.base import Storage


class PushConfigNotFoundError(Exception):
    """Raised when a specific push config is not found."""


def _serialize_tpnc(tpnc: TaskPushNotificationConfig) -> dict[str, Any]:
    """Serialize a TaskPushNotificationConfig in v1.0 flat wire shape.

    v1.0 puts ``url``, ``token``, ``id``, ``authentication`` directly on the
    ``TaskPushNotificationConfig``. This matches the internal representation.
    """
    return tpnc.model_dump(mode="json", by_alias=True, exclude_none=True)


def _serialize_tpnc_v03(tpnc: TaskPushNotificationConfig) -> dict[str, Any]:
    """Serialize a TaskPushNotificationConfig in v0.3 nested wire shape.

    v0.3 wraps the webhook fields inside ``pushNotificationConfig`` — we
    build that shape explicitly so the v0.3 REST + JSON-RPC endpoints emit
    what their clients expect. Returns ``{"taskId", "pushNotificationConfig": {...}}``.
    """
    nested: dict[str, Any] = {"url": tpnc.url}
    if tpnc.id is not None:
        nested["id"] = tpnc.id
    if tpnc.token is not None:
        nested["token"] = tpnc.token
    if tpnc.authentication is not None:
        nested["authentication"] = tpnc.authentication.model_dump(mode="json", exclude_none=True)
    return {"taskId": tpnc.task_id, "pushNotificationConfig": nested}


async def handle_set_config(
    push_store: PushConfigStore,
    storage: Storage,
    task_id: str,
    config_data: dict[str, Any],
) -> TaskPushNotificationConfig:
    """Create or update a push config for a task.

    The webhook URL is validated against the active SSRF policy (published
    by the delivery service) so unsafe URLs are rejected with a 400 at
    registration instead of being accepted and silently dropped at
    delivery time.
    """
    from a2akit.storage.base import TaskNotFoundError

    task = await storage.load_task(task_id)
    if task is None:
        raise TaskNotFoundError(f"Task {task_id} not found")

    from pydantic import ValidationError

    try:
        config = PushNotificationConfig.model_validate(config_data)
    except ValidationError as exc:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=400,
            detail={"code": -32602, "message": f"Invalid push config: {exc.error_count()} errors"},
        ) from exc

    from a2akit.push.validation import get_registration_policy, validate_webhook_url

    policy = get_registration_policy()
    if policy is not None and not await validate_webhook_url(
        config.url,
        allow_http=policy.allow_http,
        allowed_hosts=policy.allowed_hosts,
        blocked_hosts=policy.blocked_hosts,
    ):
        from fastapi import HTTPException

        raise HTTPException(
            status_code=400,
            detail={
                "code": -32602,
                "message": f"Webhook URL rejected (SSRF protection): {config.url}",
            },
        )

    return await push_store.set_config(task_id, config)


async def handle_get_config(
    push_store: PushConfigStore,
    storage: Storage,
    task_id: str,
    config_id: str | None = None,
) -> TaskPushNotificationConfig:
    """Get a push config by task_id and optional config_id."""
    from a2akit.storage.base import TaskNotFoundError

    task = await storage.load_task(task_id)
    if task is None:
        raise TaskNotFoundError(f"Task {task_id} not found")

    result = await push_store.get_config(task_id, config_id)
    if result is None:
        raise PushConfigNotFoundError(
            f"Push config {config_id or 'default'} not found for task {task_id}"
        )
    return result


async def handle_list_configs(
    push_store: PushConfigStore,
    storage: Storage,
    task_id: str,
) -> list[TaskPushNotificationConfig]:
    """List all push configs for a task."""
    from a2akit.storage.base import TaskNotFoundError

    task = await storage.load_task(task_id)
    if task is None:
        raise TaskNotFoundError(f"Task {task_id} not found")

    return await push_store.list_configs(task_id)


async def handle_delete_config(
    push_store: PushConfigStore,
    storage: Storage,
    task_id: str,
    config_id: str,
) -> bool:
    """Delete a push config. Returns True if it existed."""
    from a2akit.storage.base import TaskNotFoundError

    task = await storage.load_task(task_id)
    if task is None:
        raise TaskNotFoundError(f"Task {task_id} not found")

    deleted = await push_store.delete_config(task_id, config_id)
    if not deleted:
        raise PushConfigNotFoundError(f"Push config {config_id} not found for task {task_id}")
    return True
