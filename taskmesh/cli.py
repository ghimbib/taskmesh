#!/usr/bin/env python3
"""
CLI entrypoint for TaskMesh operations.

Commands:
  add <queue> <task_id> <title> [options]
  list <queue> [--status <status>]
  claim <queue> [task_id] [--agent <agent>]
  complete <queue> <task_id> [--summary <summary>] --routing <action> --reason <reason>
  fail <queue> <task_id> [--error <error>]
  retry <queue> <task_id>
  stale <queue> [--days <days>]
"""

import argparse
import json
import sys
from typing import Any, Dict

try:
    from . import queue_io
except ImportError:
    import queue_io


def main():
    """Main CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description="TaskMesh task management",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # add command
    add_parser = subparsers.add_parser("add", help="Add task to queue")
    add_parser.add_argument("queue", help="Path to queue file")
    add_parser.add_argument("task_id", help="Task ID")
    add_parser.add_argument("title", help="Task title")
    add_parser.add_argument("--priority", choices=["P0", "P1", "P2", "P3"], help="Priority")
    add_parser.add_argument("--context", help="Task context")
    add_parser.add_argument("--deliverable", help="Expected deliverable")
    add_parser.add_argument("--queued-by", default="cli", help="Who queued the task")
    add_parser.add_argument("--allow-requeue", action="store_true", help="Allow re-adding completed/failed task IDs")
    add_parser.add_argument("--json", action="store_true", help="Output JSON format")

    # list command
    list_parser = subparsers.add_parser("list", help="List tasks")
    list_parser.add_argument("queue", help="Path to queue file")
    list_parser.add_argument(
        "--status",
        choices=["queued", "in_progress", "completed", "failed"],
        help="Filter by status",
    )
    list_parser.add_argument("--json", action="store_true", help="Output JSON format")

    # claim command
    claim_parser = subparsers.add_parser("claim", help="Claim a task")
    claim_parser.add_argument("queue", help="Path to queue file")
    claim_parser.add_argument("task_id", nargs="?", help="Task ID to claim (defaults to first queued task)")
    claim_parser.add_argument("--agent", help="Agent claiming the task")
    claim_parser.add_argument("--json", action="store_true", help="Output JSON format")

    # complete command
    complete_parser = subparsers.add_parser("complete", help="Complete a task")
    complete_parser.add_argument("queue", help="Path to queue file")
    complete_parser.add_argument("task_id", help="Task ID to complete")
    complete_parser.add_argument("--summary", default="", help="Completion summary")
    complete_parser.add_argument("--routing", required=True, choices=["archive", "qa-gate", "review", "escalate"], help="Routing action")
    complete_parser.add_argument("--reason", required=True, help="Routing rationale")
    complete_parser.add_argument("--json", action="store_true", help="Output JSON format")

    # fail command
    fail_parser = subparsers.add_parser("fail", help="Fail a task")
    fail_parser.add_argument("queue", help="Path to queue file")
    fail_parser.add_argument("task_id", help="Task ID to fail")
    fail_parser.add_argument("--error", default="", help="Error message")
    fail_parser.add_argument("--json", action="store_true", help="Output JSON format")

    # retry command
    retry_parser = subparsers.add_parser("retry", help="Retry a failed task")
    retry_parser.add_argument("queue", help="Path to queue file")
    retry_parser.add_argument("task_id", help="Task ID to retry")
    retry_parser.add_argument("--json", action="store_true", help="Output JSON format")

    # stale command
    stale_parser = subparsers.add_parser("stale", help="List stale tasks")
    stale_parser.add_argument("queue", help="Path to queue file")
    stale_parser.add_argument("--days", type=int, default=3, help="Days before stale")
    stale_parser.add_argument("--json", action="store_true", help="Output JSON format")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    try:
        # Execute command
        if args.command == "add":
            result = cmd_add(args)
        elif args.command == "list":
            result = cmd_list(args)
        elif args.command == "claim":
            result = cmd_claim(args)
        elif args.command == "complete":
            result = cmd_complete(args)
        elif args.command == "fail":
            result = cmd_fail(args)
        elif args.command == "retry":
            result = cmd_retry(args)
        elif args.command == "stale":
            result = cmd_stale(args)
        else:
            print(f"Unknown command: {args.command}", file=sys.stderr)
            return 1

        # Output result
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print_human(args.command, result)

        return 0

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_add(args) -> Dict[str, Any]:
    """Execute add command."""
    task = {
        "id": args.task_id,
        "title": args.title,
        "status": "queued",
        "queuedBy": args.queued_by,
        "queuedAt": queue_io._now_iso(),
    }

    if args.priority:
        task["priority"] = args.priority
    if args.context:
        task["context"] = args.context
    if args.deliverable:
        task["deliverable"] = args.deliverable

    added = queue_io.add(args.queue, task, allow_requeue=args.allow_requeue)

    return {
        "success": added,
        "message": "Task added" if added else "Task already exists (duplicate)",
        "task": task if added else None,
    }


def cmd_list(args) -> Dict[str, Any]:
    """Execute list command."""
    tasks = queue_io.list_tasks(args.queue, args.status)
    return {
        "count": len(tasks),
        "tasks": tasks,
    }


def cmd_claim(args) -> Dict[str, Any]:
    """Execute claim command."""
    task = queue_io.claim(args.queue, args.task_id, agent=args.agent)
    return {
        "success": task is not None,
        "message": "Task claimed" if task else "Task not found",
        "task": task,
    }


def cmd_complete(args) -> Dict[str, Any]:
    """Execute complete command."""
    task = queue_io.complete(
        args.queue,
        args.task_id,
        args.summary,
        routing={"action": args.routing, "reason": args.reason},
    )
    return {
        "success": task is not None,
        "message": "Task completed" if task else "Task not found",
        "task": task,
    }


def cmd_fail(args) -> Dict[str, Any]:
    """Execute fail command."""
    task = queue_io.fail(args.queue, args.task_id, args.error)
    return {
        "success": task is not None,
        "message": "Task failed" if task else "Task not found",
        "task": task,
    }


def cmd_retry(args) -> Dict[str, Any]:
    """Execute retry command."""
    task = queue_io.retry(args.queue, args.task_id)
    if task is None:
        # Check if it's max retries or not found
        q = queue_io.read_queue(args.queue)
        for t in q.get("failed", []):
            if t.get("id") == args.task_id:
                return {
                    "success": False,
                    "message": "Max retries exceeded",
                    "task": None,
                }
        return {
            "success": False,
            "message": "Task not found in failed section",
            "task": None,
        }

    return {
        "success": True,
        "message": "Task retried",
        "task": task,
    }


def cmd_stale(args) -> Dict[str, Any]:
    """Execute stale command."""
    tasks = queue_io.list_tasks(args.queue, "in_progress")
    stale_tasks = [t for t in tasks if queue_io.is_stale(t, args.days)]

    return {
        "count": len(stale_tasks),
        "days": args.days,
        "tasks": stale_tasks,
    }


def print_human(command: str, result: Dict[str, Any]) -> None:
    """Print human-readable output."""
    if command == "add":
        print(result["message"])
        if result["task"]:
            print(f"  ID: {result['task']['id']}")
            print(f"  Title: {result['task']['title']}")

    elif command == "list":
        print(f"Found {result['count']} task(s)")
        for task in result["tasks"]:
            print(f"\n  [{task.get('status', 'unknown')}] {task.get('id', 'no-id')}")
            print(f"    Title: {task.get('title', 'No title')}")
            if "priority" in task:
                print(f"    Priority: {task['priority']}")

    elif command in ["claim", "complete", "fail", "retry"]:
        print(result["message"])
        if result["task"]:
            print(f"  ID: {result['task']['id']}")
            print(f"  Status: {result['task']['status']}")

    elif command == "stale":
        print(f"Found {result['count']} stale task(s) (>{result['days']} days)")
        for task in result["tasks"]:
            print(f"\n  {task.get('id', 'no-id')}")
            print(f"    Title: {task.get('title', 'No title')}")
            print(f"    Last activity: {task.get('lastActivity', 'unknown')}")


if __name__ == "__main__":
    sys.exit(main())
