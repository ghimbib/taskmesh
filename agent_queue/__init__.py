"""
agent-queue — Clean reference implementation for agent task queue operations.

Public API for atomic, file-lock-based operations on JSON queue files.
"""

from .queue_io import (
    add,
    claim,
    complete,
    fail,
    retry,
    list_tasks,
    is_stale,
    is_duplicate,
    read_queue,
    update_queue,
)


class Queue:
    """Convenience wrapper around queue_io functions."""

    def __init__(self, queue_path: str):
        self.queue_path = queue_path

    def add(self, task, *, allow_requeue: bool = False):
        return add(self.queue_path, task, allow_requeue=allow_requeue)

    def claim(self, task_id=None, agent=None):
        return claim(self.queue_path, task_id, agent=agent)

    def complete(self, task_id: str, summary: str = "", routing=None):
        if isinstance(routing, str):
            raise ValueError("routing must be a dict with action and reason")
        return complete(self.queue_path, task_id, result=summary, routing=routing)

    def fail(self, task_id: str, error: str = ""):
        return fail(self.queue_path, task_id, error=error)

    def retry(self, task_id: str):
        return retry(self.queue_path, task_id)

    def list(self, status=None):
        return list_tasks(self.queue_path, status)

    def read(self):
        return read_queue(self.queue_path)


__version__ = "0.1.0"

__all__ = [
    "Queue",
    "add",
    "claim",
    "complete",
    "fail",
    "retry",
    "list_tasks",
    "is_stale",
    "is_duplicate",
    "read_queue",
    "update_queue",
]
