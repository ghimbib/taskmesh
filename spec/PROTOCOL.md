# TaskMesh Protocol

> Version: 0.3.0
> Status: Public working spec

---

## 1. Purpose

The TaskMesh Protocol defines a minimal, portable task queue contract for multi-agent AI systems.

It standardizes:
- task schema
- SQLite storage expectations
- state transitions
- deduplication behavior
- routing declarations on completion
- origin-aware lifecycle metadata
- dependency-gate metadata
- stale-task detection inputs

The goal is to provide a queue contract that can be adopted by orchestrators, agents, or lightweight local runtimes without requiring external queue infrastructure.

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
| `notBefore` | ISO-8601 string | Earliest timestamp when the task may be dispatched |
| `blockedBy` | string[] | Task IDs, gates, or external prerequisites currently holding dispatch |
| `gateCondition` | string | Human-readable prerequisite for dispatch |
| `sourceChannel` | string | Origin channel, chat, or route where lifecycle notifications should return |
| `sourceSessionKey` | string | Origin session identifier, when known |
| `sourceMessageId` | string | Origin message identifier, when known |
| `mirrorChannels` | string[] | Additional channels or routes that should receive lifecycle notifications |
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

## 3. Canonical Storage Format

TaskMesh uses **SQLite as the canonical backend**.

### Required storage rules
- A queue is represented by a single SQLite database file.
- A task MUST exist in exactly one status at a time.
- Status is stored as task row state, not as duplicated copies of the same task.
- Queue-level metadata such as `version` and `agent` MUST be persisted in SQLite metadata.
- Implementations MUST treat SQLite as the source of truth.

### Required SQLite behavior
- WAL mode SHOULD be enabled for concurrent local access.
- Writes MUST happen inside explicit transaction boundaries.
- Implementations MUST NOT split one logical state transition across multiple commits.
- Implementations MUST NOT rely on raw sidecar files as the canonical queue state.

### Logical queue view
Implementations MAY expose a grouped read adapter like this:

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

That grouped view is an API convenience, not the canonical on-disk format.

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

## 5. Concurrency Contract

All queue writes MUST be concurrency-safe.

### Required Behavior
- Transaction scope MUST cover read -> validate -> mutate as one critical section.
- Implementations MUST prevent lost updates and duplicate claims under concurrent writers.
- Implementations MUST NOT hold transactions across network calls, LLM calls, or unrelated I/O.

### Reference Approach
The reference implementation uses:
- SQLite transactions
- WAL mode
- a busy timeout for local contention

---

## 6. Deduplication

Before inserting a task, a compliant queue MUST check whether the same task ID already exists.

### Default Rule
A task ID is considered duplicate if it already exists in any status:
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

---

## 8. Stall Detection

A task is considered stale when:
- `status = in_progress`
- `lastActivity` is older than `staleDays`
- if `lastActivity` is missing, `startedAt` is used as fallback

Default `staleDays` is 3 when no override is present.

---

## 9. Origin-Aware Lifecycle Routing

Queue entries MAY carry origin metadata so a queue can send lifecycle updates back to the place where work was requested.

```json
{
  "sourceChannel": "discord:1489064609355927632",
  "sourceSessionKey": "session:agent-queue",
  "sourceMessageId": "1501269514405412904",
  "mirrorChannels": ["discord:ops-log"]
}
```

### Resolution Rules
- `sourceChannel` is the default destination for routine lifecycle events
- `mirrorChannels` receive copies only when explicitly requested by the producer
- Explicit escalation or review targets MAY override routine source routing
- Missing origin metadata MUST NOT prevent queue operations
- Implementations SHOULD avoid sending duplicate notifications for the same task and event type

### Lifecycle Events
Implementations MAY emit lifecycle events for:
- task added
- task claimed
- task completed
- task failed
- task became stale
- task became dispatch-blocked by a gate or dependency

The protocol defines the metadata contract. Notification transport, formatting, and retry behavior are implementation concerns.

---

## 10. Gate Conditions And Dependencies

Tasks MAY declare prerequisites that must be satisfied before dispatch.

Example:

```json
{
  "dependsOn": ["research-market-001"],
  "notBefore": "2026-05-05T18:00:00+00:00",
  "gateCondition": "Only dispatch after migration validation passes"
}
```

Dependency and gate evaluation are orchestrator concerns. The queue protocol stores the condition, but does not define an automated evaluator.

If a task is not dispatchable because a prerequisite is unsatisfied, implementations MAY store `blockedBy` to explain the wait. A dependency wait is not a failure by itself.

---

## 11. Reference API Surface

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

### Optional Read Adapter
Implementations MAY expose a grouped `read_queue()` view for human inspection or compatibility helpers.

---

## Changelog

- `0.3.0` (2026-05-05) — Added origin-aware lifecycle metadata, mirror channels, dependency-gate fields, and SQLite schema migration for those optional fields
- `0.2-release-candidate` (2026-04-09) — Canonicalized SQLite as the sole backend, retained grouped read adapter, and aligned concurrency language with transactional storage
- `0.1-release-candidate` (2026-04-06) — Canonicalized routing object, claim metadata, dedup semantics, and reference API
- `0.1-draft` (2026-04-01) — Initial spec draft
