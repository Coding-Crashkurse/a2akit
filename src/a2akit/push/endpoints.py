"""Push notification config endpoint handlers (shared by REST and JSON-RPC)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from a2akit.push.models import PushNotificationConfig, TaskPushNotificationConfig

if TYPE_CHECKING:
    from a2akit.push.store import PushConfigStore
    from a2akit.storage.base import Storage


class PushNotificationNotSupportedError(Exception):
    """Raised when push notification endpoints are called but not enabled."""


class PushConfigNotFoundError(Exception):
    """Raised when a specific push config is not found."""


def _serialize_tpnc(tpnc: TaskPushNotificationConfig) -> dict[str, Any]:
    """Serialize a TaskPushNotificationConfig to a JSON-compatible dict."""
    return tpnc.model_dump(mode="json", by_alias=True, exclude_none=True)


async def handle_set_config(
    push_store: PushConfigStore,
    storage: Storage,
    task_id: str,
    config_data: dict[str, Any],
) -> TaskPushNotificationConfig:
    """Create or update a push config for a task."""
    from a2akit.storage.base import TaskNotFoundError

    task = await storage.load_task(task_id)
    if task is None:
        raise TaskNotFoundError(f"Task {task_id} not found")

    config = PushNotificationConfig.model_validate(config_data)
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
