#!/usr/bin/env python3
"""
queue_io.py — SQLite-backed reference implementation for TaskMesh.

Provides atomic operations on a single-file SQLite database following the
TaskMesh protocol specification.

Protocol:
- Status sections: queued, in_progress, completed, failed
- Methods: claim(), complete(), fail(), retry()
- Task required fields: id, title, status, queuedBy, queuedAt
- Storage: SQLite database with WAL mode
- Deduplication: check all sections unless explicit re-queue is requested
"""

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, List, Optional

VALID_STATUSES = {"queued", "in_progress", "completed", "failed"}
VALID_ROUTING_ACTIONS = {"archive", "qa-gate", "review", "escalate"}
TERMINAL_STATUSES = {"completed", "failed"}
STATUS_ORDER = ("queued", "in_progress", "completed", "failed")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('queued', 'in_progress', 'completed', 'failed')),
    priority TEXT,
    queued_by TEXT,
    queued_at TEXT,
    context TEXT,
    deliverable TEXT,
    depends_on TEXT,
    not_before TEXT,
    blocked_by TEXT,
    gate_condition TEXT,
    source_channel TEXT,
    source_session_key TEXT,
    source_message_id TEXT,
    mirror_channels TEXT,
    started_at TEXT,
    claimed_by TEXT,
    completed_at TEXT,
    last_activity TEXT,
    stale_days INTEGER,
    retry_count INTEGER,
    max_retries INTEGER,
    result TEXT,
    error TEXT,
    routing_action TEXT,
    routing_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_status_queued_at ON tasks(status, queued_at);
"""


def read_queue(queue_path: str) -> Dict[str, List[Dict[str, Any]]]:
    """Read queue state from SQLite."""
    try:
        with closing(_connect(queue_path)) as conn:
            return _read_queue_from_conn(conn)
    except sqlite3.DatabaseError:
        return _empty_queue()


def update_queue(queue_path: str, updater_fn: Callable[[Dict], None]) -> Dict:
    """
    Compatibility helper: read queue view, mutate in memory, then replace DB state.

    This preserves the previous public escape hatch while keeping SQLite as the
    canonical backend.
    """
    with closing(_connect(queue_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        q = _read_queue_from_conn(conn)
        updater_fn(q)
        _replace_all_from_dict(conn, q)
        conn.commit()
        return q


def add(queue_path: str, task: Dict[str, Any], *, allow_requeue: bool = False) -> bool:
    """Add task to queue with deduplication."""
    _validate_task_for_add(task)
    task = dict(task)
    task.setdefault("status", "queued")
    task.setdefault("queuedAt", _now_iso())

    if task["status"] != "queued":
        raise ValueError("Newly added tasks must have status 'queued'")

    with closing(_connect(queue_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = _get_task(conn, task["id"])
        if existing is not None:
            if not allow_requeue or existing.get("status") not in TERMINAL_STATUSES:
                conn.rollback()
                return False
            conn.execute("DELETE FROM tasks WHERE id = ?", (task["id"],))

        _insert_task(conn, task)
        conn.commit()
        return True


def claim(queue_path: str, task_id: Optional[str] = None, *, agent: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Transition task from queued to in_progress."""
    with closing(_connect(queue_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")

        if task_id is None:
            row = conn.execute(
                """
                SELECT * FROM tasks
                WHERE status = 'queued'
                ORDER BY CASE WHEN queued_at IS NULL THEN 1 ELSE 0 END, queued_at, rowid
                LIMIT 1
                """
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id = ? AND status = 'queued'",
                (task_id,),
            ).fetchone()

        if row is None:
            conn.rollback()
            return None

        now = _now_iso()
        conn.execute(
            """
            UPDATE tasks
            SET status = 'in_progress', started_at = ?, last_activity = ?, claimed_by = ?
            WHERE id = ? AND status = 'queued'
            """,
            (now, now, agent, row["id"]),
        )

        task = _get_task(conn, row["id"])
        conn.commit()
        return task


def complete(
    queue_path: str,
    task_id: str,
    result: str = "",
    routing: Optional[Dict[str, str]] = None,
) -> Optional[Dict[str, Any]]:
    """Transition task from in_progress to completed."""
    if routing is None:
        raise ValueError("Completed tasks must include routing")
    _validate_routing(routing)

    with closing(_connect(queue_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ? AND status = 'in_progress'",
            (task_id,),
        ).fetchone()
        if row is None:
            conn.rollback()
            return None

        now = _now_iso()
        conn.execute(
            """
            UPDATE tasks
            SET status = 'completed',
                completed_at = ?,
                last_activity = ?,
                result = ?,
                routing_action = ?,
                routing_reason = ?
            WHERE id = ?
            """,
            (
                now,
                now,
                result or None,
                routing["action"],
                routing["reason"].strip(),
                task_id,
            ),
        )
        task = _get_task(conn, task_id)
        conn.commit()
        return task


def fail(queue_path: str, task_id: str, error: str = "") -> Optional[Dict[str, Any]]:
    """Transition task from in_progress to failed."""
    with closing(_connect(queue_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ? AND status = 'in_progress'",
            (task_id,),
        ).fetchone()
        if row is None:
            conn.rollback()
            return None

        now = _now_iso()
        conn.execute(
            """
            UPDATE tasks
            SET status = 'failed',
                completed_at = ?,
                last_activity = ?,
                error = ?
            WHERE id = ?
            """,
            (now, now, error or None, task_id),
        )
        task = _get_task(conn, task_id)
        conn.commit()
        return task


def retry(queue_path: str, task_id: str) -> Optional[Dict[str, Any]]:
    """Transition task from failed back to queued for retry."""
    with closing(_connect(queue_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        task = _get_task(conn, task_id)
        if task is None or task.get("status") != "failed":
            conn.rollback()
            return None

        retry_count = task.get("retryCount", 0)
        max_retries = task.get("maxRetries", 3)
        if retry_count >= max_retries:
            conn.rollback()
            return None

        now = _now_iso()
        conn.execute(
            """
            UPDATE tasks
            SET status = 'queued',
                retry_count = ?,
                last_activity = ?,
                started_at = NULL,
                claimed_by = NULL,
                completed_at = NULL,
                result = NULL,
                error = NULL,
                routing_action = NULL,
                routing_reason = NULL
            WHERE id = ?
            """,
            (retry_count + 1, now, task_id),
        )
        retried = _get_task(conn, task_id)
        conn.commit()
        return retried


def list_tasks(queue_path: str, status: Optional[str] = None) -> List[Dict[str, Any]]:
    """List all tasks, optionally filtered by status."""
    with closing(_connect(queue_path)) as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY rowid",
                (status,),
            ).fetchall()
            return [_row_to_task(row) for row in rows]

        rows = conn.execute("SELECT * FROM tasks ORDER BY rowid").fetchall()
        return [_row_to_task(row) for row in rows]


def is_stale(task: Dict[str, Any], days: int = 3) -> bool:
    """Check if in_progress task is stale based on lastActivity."""
    if task.get("status") != "in_progress":
        return False

    last_activity = task.get("lastActivity") or task.get("startedAt")
    if not last_activity:
        return False

    try:
        last_dt = datetime.fromisoformat(last_activity.replace("Z", "+00:00"))
        age = datetime.now(timezone.utc) - last_dt
        return age > timedelta(days=days)
    except (ValueError, AttributeError):
        return False


def is_duplicate(queue_path: str, task_id: str, *, include_terminal: bool = True) -> bool:
    """Check if task ID already exists in queue sections."""
    with closing(_connect(queue_path)) as conn:
        if include_terminal:
            row = conn.execute("SELECT 1 FROM tasks WHERE id = ? LIMIT 1", (task_id,)).fetchone()
        else:
            row = conn.execute(
                "SELECT 1 FROM tasks WHERE id = ? AND status IN ('queued', 'in_progress') LIMIT 1",
                (task_id,),
            ).fetchone()
        return row is not None


# Private helpers

def _empty_queue() -> Dict[str, List]:
    return {
        "version": "1",
        "agent": None,
        "queued": [],
        "in_progress": [],
        "completed": [],
        "failed": [],
    }


def _connect(queue_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(queue_path) or ".", exist_ok=True)
    return _open_db(queue_path, recover=True)


def _open_db(queue_path: str, recover: bool) -> sqlite3.Connection:
    def _connect_once() -> sqlite3.Connection:
        conn = sqlite3.connect(queue_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA_SQL)
        _ensure_schema_columns(conn)
        conn.execute("INSERT OR IGNORE INTO metadata(key, value) VALUES ('version', '1')")
        conn.execute("INSERT OR IGNORE INTO metadata(key, value) VALUES ('agent', NULL)")
        return conn

    try:
        return _connect_once()
    except sqlite3.DatabaseError:
        if not recover:
            raise
        try:
            os.unlink(queue_path)
        except FileNotFoundError:
            pass
        return _connect_once()


def _read_queue_from_conn(conn: sqlite3.Connection) -> Dict[str, List[Dict[str, Any]]]:
    q = _empty_queue()
    metadata = dict(conn.execute("SELECT key, value FROM metadata").fetchall())
    q["version"] = metadata.get("version") or "1"
    q["agent"] = metadata.get("agent")

    rows = conn.execute(
        "SELECT * FROM tasks ORDER BY CASE status WHEN 'queued' THEN 0 WHEN 'in_progress' THEN 1 WHEN 'completed' THEN 2 ELSE 3 END, rowid"
    ).fetchall()
    for row in rows:
        task = _row_to_task(row)
        q[task["status"]].append(task)
    return q


def _replace_all_from_dict(conn: sqlite3.Connection, q: Dict[str, Any]) -> None:
    q = dict(q)
    version = str(q.get("version", "1"))
    agent = q.get("agent")

    conn.execute("DELETE FROM tasks")
    conn.execute("REPLACE INTO metadata(key, value) VALUES ('version', ?)", (version,))
    conn.execute("REPLACE INTO metadata(key, value) VALUES ('agent', ?)", (agent,))

    for status in STATUS_ORDER:
        for task in q.get(status, []):
            task = dict(task)
            task["status"] = status
            _validate_task_for_add(task)
            _insert_task(conn, task)


def _insert_task(conn: sqlite3.Connection, task: Dict[str, Any]) -> None:
    record = _task_to_record(task)
    conn.execute(
        """
        INSERT INTO tasks (
            id, title, status, priority, queued_by, queued_at,
            context, deliverable, depends_on, not_before, blocked_by,
            gate_condition, source_channel, source_session_key,
            source_message_id, mirror_channels,
            started_at, claimed_by, completed_at, last_activity,
            stale_days, retry_count, max_retries, result, error,
            routing_action, routing_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record["id"],
            record["title"],
            record["status"],
            record["priority"],
            record["queued_by"],
            record["queued_at"],
            record["context"],
            record["deliverable"],
            record["depends_on"],
            record["not_before"],
            record["blocked_by"],
            record["gate_condition"],
            record["source_channel"],
            record["source_session_key"],
            record["source_message_id"],
            record["mirror_channels"],
            record["started_at"],
            record["claimed_by"],
            record["completed_at"],
            record["last_activity"],
            record["stale_days"],
            record["retry_count"],
            record["max_retries"],
            record["result"],
            record["error"],
            record["routing_action"],
            record["routing_reason"],
        ),
    )


def _get_task(conn: sqlite3.Connection, task_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return _row_to_task(row) if row else None


def _task_to_record(task: Dict[str, Any]) -> Dict[str, Any]:
    status = task.get("status", "queued")
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid task status: {status}")

    routing = task.get("routing")
    routing_action = None
    routing_reason = None
    if routing is not None:
        _validate_routing(routing)
        routing_action = routing["action"]
        routing_reason = routing["reason"].strip()

    depends_on = task.get("dependsOn")
    if depends_on is not None and not isinstance(depends_on, list):
        raise ValueError("dependsOn must be a list when provided")

    blocked_by = task.get("blockedBy")
    if blocked_by is not None and not isinstance(blocked_by, list):
        raise ValueError("blockedBy must be a list when provided")

    mirror_channels = task.get("mirrorChannels")
    if mirror_channels is not None and not isinstance(mirror_channels, list):
        raise ValueError("mirrorChannels must be a list when provided")

    return {
        "id": task["id"],
        "title": task["title"],
        "status": status,
        "priority": task.get("priority"),
        "queued_by": task.get("queuedBy"),
        "queued_at": task.get("queuedAt"),
        "context": task.get("context"),
        "deliverable": task.get("deliverable"),
        "depends_on": json.dumps(depends_on) if depends_on is not None else None,
        "not_before": task.get("notBefore"),
        "blocked_by": json.dumps(blocked_by) if blocked_by is not None else None,
        "gate_condition": task.get("gateCondition"),
        "source_channel": task.get("sourceChannel"),
        "source_session_key": task.get("sourceSessionKey"),
        "source_message_id": task.get("sourceMessageId"),
        "mirror_channels": json.dumps(mirror_channels) if mirror_channels is not None else None,
        "started_at": task.get("startedAt"),
        "claimed_by": task.get("claimedBy"),
        "completed_at": task.get("completedAt"),
        "last_activity": task.get("lastActivity"),
        "stale_days": task.get("staleDays"),
        "retry_count": task.get("retryCount", 0),
        "max_retries": task.get("maxRetries"),
        "result": task.get("result"),
        "error": task.get("error"),
        "routing_action": routing_action,
        "routing_reason": routing_reason,
    }


def _row_to_task(row: sqlite3.Row) -> Dict[str, Any]:
    task: Dict[str, Any] = {
        "id": row["id"],
        "title": row["title"],
        "status": row["status"],
    }

    mapping = {
        "priority": "priority",
        "queued_by": "queuedBy",
        "queued_at": "queuedAt",
        "context": "context",
        "deliverable": "deliverable",
        "not_before": "notBefore",
        "gate_condition": "gateCondition",
        "source_channel": "sourceChannel",
        "source_session_key": "sourceSessionKey",
        "source_message_id": "sourceMessageId",
        "started_at": "startedAt",
        "claimed_by": "claimedBy",
        "completed_at": "completedAt",
        "last_activity": "lastActivity",
        "stale_days": "staleDays",
        "max_retries": "maxRetries",
        "result": "result",
        "error": "error",
    }

    for column, key in mapping.items():
        value = row[column]
        if value is not None:
            task[key] = value

    if row["queued_by"] is not None:
        task["queuedBy"] = row["queued_by"]
    if row["retry_count"] not in (None, 0):
        task["retryCount"] = row["retry_count"]

    if row["depends_on"]:
        task["dependsOn"] = json.loads(row["depends_on"])
    if row["blocked_by"]:
        task["blockedBy"] = json.loads(row["blocked_by"])
    if row["mirror_channels"]:
        task["mirrorChannels"] = json.loads(row["mirror_channels"])

    if row["routing_action"] is not None:
        task["routing"] = {
            "action": row["routing_action"],
            "reason": row["routing_reason"],
        }

    return task


def _ensure_schema_columns(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    additions = {
        "not_before": "TEXT",
        "blocked_by": "TEXT",
        "source_channel": "TEXT",
        "source_session_key": "TEXT",
        "source_message_id": "TEXT",
        "mirror_channels": "TEXT",
    }
    for column, column_type in additions.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {column_type}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_task_for_add(task: Dict[str, Any]) -> None:
    for field in ("id", "title"):
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
