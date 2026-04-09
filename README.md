# TaskMesh

> An agent task queue protocol and Python reference implementation.

Most multi-agent AI systems bolt on task coordination as an afterthought: raw JSON writes, race conditions, inconsistent task schemas, and vague completion handling. `taskmesh` defines a simple portable protocol for agent task state and ships a reference Python implementation that is safe for concurrent local use.

---

## What it includes

1. **Protocol spec** for task schema, lifecycle, locking, deduplication, and routing
2. **Python reference implementation** with atomic file-locked writes
3. **CLI** for queue operations
4. **Tests** covering lifecycle, retries, stale detection, and concurrent writes

---

## Design goals

- **Portable**: file-based JSON queue, easy to inspect and adopt
- **Safe**: lock-protected read-modify-write and atomic file replacement
- **Agent-native**: explicit task states, retry behavior, and routing declarations
- **Minimal**: standard library only for the core implementation

---

## Install

```bash
pip install git+https://github.com/ghimbib/taskmesh.git
```

For local development from source:

```bash
python3 -m unittest discover -s tests -q
```

---

## Protocol overview

### Queue file

```json
{
  "version": "1",
  "agent": null,
  "queued": [],
  "in_progress": [],
  "completed": [],
  "failed": []
}
```

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

q = Queue("./my-queue.json")

q.add({
    "id": "research-topic-001",
    "title": "Research competitor pricing",
    "priority": "P1",
    "queuedBy": "orchestrator",
    "queuedAt": "2026-04-06T16:00:00+00:00"
})

# Claim first queued task and persist claimant identity
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

queue_path = "./my-queue.json"

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

---

## CLI

```bash
# Add a task
taskmesh add ./queue.json task-001 "Do the thing" --priority P1 --queued-by orchestrator

# List tasks
taskmesh list ./queue.json
taskmesh list ./queue.json --status queued

# Claim first queued task
taskmesh claim ./queue.json --agent researcher

# Claim a specific task
taskmesh claim ./queue.json task-001 --agent researcher

# Complete a task
taskmesh complete ./queue.json task-001 \
  --summary "Done" \
  --routing review \
  --reason "Needs approval before next stage"

# Fail a task
taskmesh fail ./queue.json task-001 --error "Dependency unavailable"

# Retry a failed task
taskmesh retry ./queue.json task-001

# Show stale tasks
taskmesh stale ./queue.json --days 3
```

---

## Behavior guarantees

- Tasks live in exactly one section at a time
- State transitions are atomic
- Duplicate task IDs are blocked across all sections by default
- Explicit re-queue is supported when requested by the caller
- Completed tasks require structured routing metadata
- Claim operations record timestamps and may record claimant identity
- Retry clears completion-only fields before re-queueing

---

## Current status

Current release candidate includes:
- spec-aligned Python API
- spec-aligned CLI
- routing stored as structured task metadata
- concurrent write tests
- retry and stale detection coverage

---

## Roadmap

- package metadata and PyPI publishing
- more worked examples
- optional SQLite backend
- TypeScript port

---

## Contributing

Protocol changes should update `spec/PROTOCOL.md` before implementation changes are merged.

---

## License

MIT
