"""Worker package — user-facing ABCs, execution context, and adapter."""

from a2akit.worker.adapter import WorkerAdapter
from a2akit.worker.base import (
    FileInfo,
    HistoryMessage,
    PreviousArtifact,
    TaskContext,
    TaskContextImpl,
    Worker,
)
from a2akit.worker.context_factory import ContextFactory

__all__ = [
    "ContextFactory",
    "FileInfo",
    "HistoryMessage",
    "PreviousArtifact",
    "TaskContext",
    "TaskContextImpl",
    "Worker",
    "WorkerAdapter",
]
