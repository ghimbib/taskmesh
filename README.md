# TaskMesh

> An agent task queue protocol and SQLite reference implementation.

Most multi-agent AI systems bolt on task coordination as an afterthought: ad hoc files, race conditions, inconsistent task schemas, and muddy completion handling. `taskmesh` defines a simple portable protocol for agent task state and ships a reference Python implementation backed by a single-file SQLite database.

---

## What it includes

1. **Protocol spec** for task schema, lifecycle, deduplication, retries, and completion routing
2. **Python reference implementation** using SQLite transactions and WAL mode
3. **CLI** for queue operations
4. **Tests** covering lifecycle, retries, stale detection, and concurrent writers

---

## Design goals

- **Portable**: single-file SQLite queue, easy to copy, inspect, and adopt
- **Safe**: atomic state transitions with transaction boundaries
- **Agent-native**: explicit task states, retry behavior, and routing declarations
- **Minimal**: Python standard library only

---

## Install

```bash
pip install taskmesh-py
```

Import stays `taskmesh`. The PyPI distribution name is `taskmesh-py` because `taskmesh` is already taken on PyPI.

For local development from source:

```bash
python3 -m unittest discover -s tests -q
```

---

## Protocol overview

### Canonical storage

TaskMesh stores queue state in a **SQLite database**.

- one row per task
- `status` determines the current section
- SQLite metadata stores queue-level fields like `version` and `agent`
- WAL mode is enabled for concurrent local access

### Task shape

```json
{
  "id": "research-pricing-001",
  "title": "Research competitor pricing",
  "status": "queued",
  "queuedBy": "orchestrator",
  "queuedAt": "2026-04-06T16:00:00+00:00",
  "priority": "P1",
  "context": "Compare top 3 competitors",
  "deliverable": "Short markdown summary"
}
```

### Completion routing

Completed tasks must persist structured routing metadata:

```json
{
  "routing": {
    "action": "review",
    "reason": "Needs human approval before downstream execution"
  }
}
```

Valid routing actions:
- `archive`
- `qa-gate`
- `review`
- `escalate`

---

## Python API

### Convenience wrapper

```python
from taskmesh import Queue

q = Queue("./my-queue.db")

q.add({
    "id": "research-topic-001",
    "title": "Research competitor pricing",
    "priority": "P1",
    "queuedBy": "orchestrator",
    "queuedAt": "2026-04-06T16:00:00+00:00"
})

claimed = q.claim(agent="researcher")

q.complete(
    claimed["id"],
    summary="Found 3 competitors. Median price is $49/mo.",
    routing={
        "action": "review",
        "reason": "Needs approval before publishing"
    }
)
```

### Functional API

```python
from taskmesh import add, claim, complete

queue_path = "./my-queue.db"

add(queue_path, {
    "id": "task-001",
    "title": "Do the thing",
    "status": "queued",
    "queuedBy": "orchestrator",
    "queuedAt": "2026-04-06T16:00:00+00:00"
})

task = claim(queue_path, agent="worker-1")

complete(
    queue_path,
    task["id"],
    result="Done",
    routing={"action": "archive", "reason": "No follow-up needed"}
)
```

### Read view

`read_queue()` returns a grouped in-memory view for convenience:

```python
{
  "version": "1",
  "agent": None,
  "queued": [...],
  "in_progress": [...],
  "completed": [...],
  "failed": [...]
}
```

That grouped structure is a read adapter, not the canonical on-disk format.

---

## CLI

```bash
# Add a task
taskmesh add ./queue.db task-001 "Do the thing" --priority P1 --queued-by orchestrator

# List tasks
taskmesh list ./queue.db
taskmesh list ./queue.db --status queued

# Claim first queued task
taskmesh claim ./queue.db --agent researcher

# Claim a specific task
taskmesh claim ./queue.db task-001 --agent researcher

# Complete a task
taskmesh complete ./queue.db task-001 \
  --summary "Done" \
  --routing review \
  --reason "Needs approval before next stage"

# Fail a task
taskmesh fail ./queue.db task-001 --error "Dependency unavailable"

# Retry a failed task
taskmesh retry ./queue.db task-001

# Show stale tasks
taskmesh stale ./queue.db --days 3
```

---

## Behavior guarantees

- Tasks live in exactly one status at a time
- State transitions are atomic
- Duplicate task IDs are blocked across all statuses by default
- Explicit re-queue is supported when requested by the caller
- Completed tasks require structured routing metadata
- Claim operations record timestamps and may record claimant identity
- Retry clears completion-only fields before re-queueing
- SQLite WAL mode is enabled for concurrent local writers

---

## Current status

Current release candidate includes:
- spec-aligned Python API
- spec-aligned CLI
- routing stored as structured task metadata
- concurrent write tests
- retry and stale detection coverage
- SQLite as the canonical backend

---

## Roadmap

- package metadata and PyPI publishing
- more worked examples
- migration helpers for older file-based prototypes
- TypeScript port

---

## Contributing

Protocol changes should update `spec/PROTOCOL.md` before implementation changes are merged.

---

## License

MIT
