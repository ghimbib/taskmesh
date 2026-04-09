#!/usr/bin/env python3
"""
queue_io.py — Clean reference implementation for agent task queue operations.

Provides atomic, file-lock-based operations on JSON queue files following the
TaskMesh protocol specification.

Protocol:
- Status sections: queued, in_progress, completed, failed
- Methods: claim(), complete(), fail(), retry()
- Task required fields: id, title, status, queuedBy, queuedAt
- File locking: fcntl.flock(LOCK_EX) for all writes
- Deduplication: check all sections unless explicit re-queue is requested
"""

import json
import fcntl
import os
import tempfile
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any, Callable

VALID_STATUSES = {"queued", "in_progress", "completed", "failed"}
VALID_ROUTING_ACTIONS = {"archive", "qa-gate", "review", "escalate"}


def read_queue(queue_path: str) -> Dict[str, List[Dict[str, Any]]]:
    """
    Read queue state with shared lock.

    Returns dict with sections: queued, in_progress, completed, failed.
    Creates empty structure if file doesn't exist.
    """
    lock_path = _ensure_lock_file(queue_path)
    with open(lock_path, "w") as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_SH)
        try:
            with open(queue_path) as f:
                q = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return _empty_queue()

        # Ensure all required sections exist
        for section in ["queued", "in_progress", "completed", "failed"]:
            if section not in q:
                q[section] = []

        return q


def update_queue(queue_path: str, updater_fn: Callable[[Dict], None]) -> Dict:
    """
    Atomic read-modify-write with exclusive lock.

    Args:
        queue_path: Path to queue JSON file
        updater_fn: Function that receives queue dict and modifies it in place

    Returns:
        Updated queue dict
    """
    lock_path = _ensure_lock_file(queue_path)
    with open(lock_path, "w") as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        # Read current state
        try:
            with open(queue_path) as f:
                q = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            q = _empty_queue()

        # Ensure structure
        if not isinstance(q, dict):
            q = _empty_queue()
        for section in ["queued", "in_progress", "completed", "failed"]:
            if section not in q:
                q[section] = []

        # Apply update
        updater_fn(q)

        # Write atomically
        _write_atomic(queue_path, q)

    return q


def add(queue_path: str, task: Dict[str, Any], *, allow_requeue: bool = False) -> bool:
    """
    Add task to queue with deduplication.

    By default checks all sections for duplicate task IDs. To explicitly re-queue
    a previously completed or failed task, pass allow_requeue=True.

    Args:
        queue_path: Path to queue JSON file
        task: Task dict with at least: id, title, status, queuedBy, queuedAt
        allow_requeue: Allow re-adding when matching ID exists in completed/failed

    Returns:
        True if added, False if duplicate
    """
    _validate_task_for_add(task)
    task = dict(task)
    task_id = task["id"]

    # Ensure required/default fields
    task.setdefault("status", "queued")
    task.setdefault("queuedAt", _now_iso())

    if task["status"] != "queued":
        raise ValueError("Newly added tasks must have status 'queued'")

    added = False

    def _add(q: Dict) -> None:
        nonlocal added
        sections = ["queued", "in_progress"]
        if not allow_requeue:
            sections.extend(["completed", "failed"])

        for section in sections:
            for existing in q.get(section, []):
                if existing.get("id") == task_id:
                    added = False
                    return

        q["queued"].append(task)
        added = True

    update_queue(queue_path, _add)
    return added


def claim(queue_path: str, task_id: Optional[str] = None, *, agent: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Transition task from queued to in_progress.

    Sets startedAt, lastActivity, and optionally claimedBy.
    If task_id is omitted, claims the first queued task.

    Args:
        queue_path: Path to queue JSON file
        task_id: Task ID to claim, or first queued task when omitted
        agent: Optional claimant identity to persist as claimedBy

    Returns:
        Claimed task dict, or None if not found
    """
    claimed_task = None

    def _claim(q: Dict) -> None:
        nonlocal claimed_task

        selected_index = None
        for i, task in enumerate(q["queued"]):
            if task_id is None or task.get("id") == task_id:
                selected_index = i
                break

        if selected_index is None:
            return

        task = q["queued"].pop(selected_index)
        now = _now_iso()
        task["status"] = "in_progress"
        task["startedAt"] = now
        task["lastActivity"] = now
        if agent:
            task["claimedBy"] = agent

        q["in_progress"].append(task)
        claimed_task = task

    update_queue(queue_path, _claim)
    return claimed_task


def complete(
    queue_path: str,
    task_id: str,
    result: str = "",
    routing: Optional[Dict[str, str]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Transition task from in_progress to completed.

    Sets completedAt timestamp, optional result field, and required routing.

    Args:
        queue_path: Path to queue JSON file
        task_id: Task ID to complete
        result: Completion result or message
        routing: Required routing declaration dict with action + reason

    Returns:
        Completed task dict, or None if not found
    """
    if routing is None:
        raise ValueError("Completed tasks must include routing")
    _validate_routing(routing)

    completed_task = None

    def _complete(q: Dict) -> None:
        nonlocal completed_task

        for i, task in enumerate(q["in_progress"]):
            if task.get("id") == task_id:
                task = q["in_progress"].pop(i)
                now = _now_iso()
                task["status"] = "completed"
                task["completedAt"] = now
                task["lastActivity"] = now
                task["routing"] = dict(routing)
                if result:
                    task["result"] = result
                q["completed"].append(task)
                completed_task = task
                break

    update_queue(queue_path, _complete)
    return completed_task


def fail(queue_path: str, task_id: str, error: str = "") -> Optional[Dict[str, Any]]:
    """
    Transition task from in_progress to failed.

    Sets completedAt timestamp and error field.

    Args:
        queue_path: Path to queue JSON file
        task_id: Task ID to fail
        error: Error message

    Returns:
        Failed task dict, or None if not found
    """
    failed_task = None

    def _fail(q: Dict) -> None:
        nonlocal failed_task

        # Find task in in_progress
        for i, task in enumerate(q["in_progress"]):
            if task.get("id") == task_id:
                # Remove from in_progress
                task = q["in_progress"].pop(i)

                # Update status and timestamps
                task["status"] = "failed"
                task["completedAt"] = _now_iso()
                task["lastActivity"] = _now_iso()
                if error:
                    task["error"] = error

                # Add to failed
                q["failed"].append(task)
                failed_task = task
                break

    update_queue(queue_path, _fail)
    return failed_task


def retry(queue_path: str, task_id: str) -> Optional[Dict[str, Any]]:
    """
    Transition task from failed back to queued for retry.

    Increments retryCount and checks against maxRetries.

    Args:
        queue_path: Path to queue JSON file
        task_id: Task ID to retry

    Returns:
        Retried task dict, or None if not found or max retries exceeded
    """
    retried_task = None

    def _retry(q: Dict) -> None:
        nonlocal retried_task

        # Find task in failed
        for i, task in enumerate(q["failed"]):
            if task.get("id") == task_id:
                # Check retry limit
                retry_count = task.get("retryCount", 0)
                max_retries = task.get("maxRetries", 3)

                if retry_count >= max_retries:
                    return  # Max retries exceeded

                # Remove from failed
                task = q["failed"].pop(i)

                # Update for retry
                task["status"] = "queued"
                task["retryCount"] = retry_count + 1
                task["lastActivity"] = _now_iso()

                # Remove completion fields
                task.pop("completedAt", None)
                task.pop("error", None)
                task.pop("result", None)
                task.pop("routing", None)
                task.pop("startedAt", None)
                task.pop("claimedBy", None)

                # Add to queued
                q["queued"].append(task)
                retried_task = task
                break

    update_queue(queue_path, _retry)
    return retried_task


def list_tasks(queue_path: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    List all tasks, optionally filtered by status.

    Args:
        queue_path: Path to queue JSON file
        status: Filter by status section (queued, in_progress, completed, failed)

    Returns:
        List of task dicts
    """
    q = read_queue(queue_path)

    if status:
        return q.get(status, [])

    # Return all tasks
    tasks = []
    for section in ["queued", "in_progress", "completed", "failed"]:
        tasks.extend(q.get(section, []))
    return tasks


def is_stale(task: Dict[str, Any], days: int = 3) -> bool:
    """
    Check if in_progress task is stale based on lastActivity.

    Args:
        task: Task dict
        days: Number of days before considering stale

    Returns:
        True if stale, False otherwise
    """
    if task.get("status") != "in_progress":
        return False

    last_activity = task.get("lastActivity")
    if not last_activity:
        # No lastActivity, check startedAt
        last_activity = task.get("startedAt")

    if not last_activity:
        return False

    try:
        last_dt = datetime.fromisoformat(last_activity.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        age = now - last_dt
        return age > timedelta(days=days)
    except (ValueError, AttributeError):
        return False


def is_duplicate(queue_path: str, task_id: str, *, include_terminal: bool = True) -> bool:
    """
    Check if task ID already exists in queue sections.

    Args:
        queue_path: Path to queue JSON file
        task_id: Task ID to check
        include_terminal: When True, also check completed and failed sections

    Returns:
        True if duplicate, False otherwise
    """
    q = read_queue(queue_path)

    sections = ["queued", "in_progress"]
    if include_terminal:
        sections.extend(["completed", "failed"])

    for section in sections:
        for task in q.get(section, []):
            if task.get("id") == task_id:
                return True

    return False


# Private helpers

def _empty_queue() -> Dict[str, List]:
    """Create empty queue structure."""
    return {
        "version": "1",
        "agent": None,
        "queued": [],
        "in_progress": [],
        "completed": [],
        "failed": []
    }


def _ensure_lock_file(queue_path: str) -> str:
    """Ensure lock file exists and return its path."""
    lock_path = queue_path + ".lock"
    if not os.path.exists(lock_path):
        open(lock_path, "a").close()
    return lock_path


def _write_atomic(queue_path: str, data: Dict) -> None:
    """Write JSON to file via atomic temp+rename."""
    if not isinstance(data, dict):
        raise TypeError(f"Queue data must be dict, got {type(data).__name__}")

    dir_name = os.path.dirname(queue_path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp", prefix=".queue-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.rename(tmp_path, queue_path)  # atomic on same filesystem
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _now_iso() -> str:
    """Return current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat()


def _validate_task_for_add(task: Dict[str, Any]) -> None:
    required = ["id", "title"]
    for field in required:
        if not task.get(field):
            raise ValueError(f"Task must have '{field}' field")

    status = task.get("status", "queued")
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid task status: {status}")


def _validate_routing(routing: Dict[str, str]) -> None:
    if not isinstance(routing, dict):
        raise ValueError("routing must be a dict")

    action = routing.get("action")
    reason = routing.get("reason")

    if action not in VALID_ROUTING_ACTIONS:
        raise ValueError(f"Invalid routing action: {action}")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("routing.reason must be a non-empty string")
