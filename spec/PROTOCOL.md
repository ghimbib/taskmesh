# Agent Queue Protocol

> Version: 0.1-release-candidate
> Status: Release candidate

---

## 1. Purpose

The Agent Queue Protocol defines a minimal, portable task queue contract for multi-agent AI systems.

It standardizes:
- task schema
- queue file layout
- state transitions
- locking expectations
- deduplication behavior
- routing declarations on completion

The goal is to provide a queue format and reference behavior that can be adopted by orchestrators, agents, or lightweight local runtimes without depending on external queue infrastructure.

---

## 2. Task Schema

All tasks MUST conform to this schema.

### Required Fields
| Field | Type | Description |
|---|---|---|
| `id` | string | Unique task identifier |
| `title` | string | Human-readable task name |
| `status` | enum | `queued` \| `in_progress` \| `completed` \| `failed` |
| `queuedBy` | string | Agent, user, or system that created the task |
| `queuedAt` | ISO-8601 string | Timestamp of queue insertion |

### Optional Fields
| Field | Type | Description |
|---|---|---|
| `priority` | enum | `P0` \| `P1` \| `P2` \| `P3` |
| `context` | string | Background context for the executing agent |
| `deliverable` | string | Expected output description |
| `dependsOn` | string[] | Task IDs that must complete before this runs |
| `gateCondition` | string | Human-readable prerequisite for dispatch |
| `startedAt` | ISO-8601 string | Timestamp when the task was claimed |
| `claimedBy` | string | Agent or worker that claimed the task |
| `completedAt` | ISO-8601 string | Timestamp when the task completed or failed |
| `lastActivity` | ISO-8601 string | Last update timestamp |
| `staleDays` | number | Days before task is considered stale |
| `retryCount` | number | Number of retry attempts already used |
| `maxRetries` | number | Maximum retry attempts allowed |
| `result` | string | Completion summary |
| `error` | string | Failure reason |
| `routing` | object | Structured routing declaration, required on completion |

### Routing Object
When `status = completed`, `routing` is REQUIRED.

```json
{
  "action": "archive",
  "reason": "Routine work, no follow-up needed"
}
```

Valid `routing.action` values:
- `archive`
- `qa-gate`
- `review`
- `escalate`

---

## 3. Queue File Format

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

### Rules
- A task MUST exist in exactly one section at a time
- Queue files SHOULD include `version`
- Queue files MAY include `agent` as descriptive metadata for a queue owner or lane
- Missing sections MUST be treated as empty by compliant implementations

---

## 4. Status Lifecycle

```text
queued -> in_progress -> completed
                     -> failed
failed -> queued      (retry)
```

### Transition Rules
- `add()` inserts into `queued`
- `claim()` moves `queued` -> `in_progress`
- `complete()` moves `in_progress` -> `completed`
- `fail()` moves `in_progress` -> `failed`
- `retry()` moves `failed` -> `queued`

### Required Transition Effects
- `claim()` MUST set `startedAt`
- `claim()` MUST set `lastActivity`
- `claim()` SHOULD set `claimedBy` when claimant identity is known
- `complete()` MUST set `completedAt`
- `complete()` MUST set `lastActivity`
- `complete()` MUST persist `routing`
- `fail()` MUST set `completedAt`
- `fail()` MUST set `lastActivity`
- `retry()` MUST increment `retryCount`
- `retry()` MUST clear completion-only fields such as `completedAt`, `error`, `result`, and `routing`

---

## 5. Locking Protocol

All queue writes MUST use file locking to prevent race conditions and lost updates.

### Required Behavior
- Lock scope MUST cover read -> modify -> write as one critical section
- Implementations MUST write atomically, for example temp file + rename on the same filesystem
- Implementations MUST NOT hold locks across network calls, LLM calls, or unrelated I/O

### Reference Approach
On POSIX systems, the reference implementation uses:
- lock file + `fcntl.flock()`
- temp file write + atomic rename

---

## 6. Deduplication

Before inserting a task, a compliant queue MUST check whether the same task ID already exists.

### Default Rule
A task ID is considered duplicate if it appears in any section:
- `queued`
- `in_progress`
- `completed`
- `failed`

### Explicit Re-queue Exception
Implementations MAY allow re-adding an ID from terminal states only when the caller explicitly requests it.

Examples:
- `allow_requeue=True`
- `force=true`
- separate `requeue()` operation

Silent duplicate rejection is acceptable. Returning a boolean is acceptable. Raising an explicit duplicate error is also acceptable.

---

## 7. Routing Contract

Routing is a first-class part of the protocol.

When a task is completed, the queue entry MUST store a structured routing object:

```json
{
  "routing": {
    "action": "review",
    "reason": "Requires human approval before downstream work"
  }
}
```

### Semantics
| Action | Meaning |
|---|---|
| `archive` | No follow-up required |
| `qa-gate` | Send to verification or QA step |
| `review` | Send to reviewer, orchestrator, or approver |
| `escalate` | Human or orchestrator attention required |

### Optional Text Adapter
Systems MAY additionally render the routing object into a text footer such as:

```text
[ROUTING]
action: review
reason: Requires human approval before downstream work
```

That footer is an adapter format for logs or messages. The canonical protocol representation is the structured `routing` object on the task.

---

## 8. Stall Detection

A task is considered stale when:
- `status = in_progress`
- `lastActivity` is older than `staleDays`
- if `lastActivity` is missing, `startedAt` is used as fallback

Default `staleDays` is 3 when no override is present.

---

## 9. Gate Conditions

Tasks MAY declare a `gateCondition` describing a prerequisite that must be satisfied before dispatch.

Example:

```json
{
  "gateCondition": "Only dispatch after migration validation passes"
}
```

Gate evaluation is an orchestrator concern. The queue protocol stores the condition, but does not define an automated evaluator.

---

## 10. Reference API Surface

A compliant Python implementation may expose both:

### Functional API
- `add(queue_path, task, allow_requeue=False)`
- `claim(queue_path, task_id=None, agent=None)`
- `complete(queue_path, task_id, result="", routing={...})`
- `fail(queue_path, task_id, error="")`
- `retry(queue_path, task_id)`
- `list_tasks(queue_path, status=None)`

### Convenience Wrapper
A higher-level `Queue` object MAY wrap the same behavior for easier use.

---

## Changelog

- `0.1-release-candidate` (2026-04-06) — Canonicalized routing object, claim metadata, dedup semantics, and reference API
- `0.1-draft` (2026-04-01) — Initial spec draft
