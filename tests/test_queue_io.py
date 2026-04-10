#!/usr/bin/env python3
"""
Unit tests for the TaskMesh SQLite reference implementation.

Tests cover: add, dedup, claim, complete, fail, retry, list, stale detection,
is_duplicate, concurrent write safety, and full lifecycle.
"""

import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from multiprocessing import Process, Value

from taskmesh import queue_io


# Top-level functions for multiprocessing (macOS spawn can't pickle closures)
def _mp_add_task(qpath, task_id, counter):
    try:
        result = queue_io.add(qpath, _make_task(task_id))
        if result:
            with counter.get_lock():
                counter.value += 1
    except Exception:
        pass


def _mp_add_dup(qpath, counter):
    try:
        result = queue_io.add(qpath, _make_task("dup-same"))
        if result:
            with counter.get_lock():
                counter.value += 1
    except Exception:
        pass


def _make_queue(tmpdir, initial=None):
    """Create a queue database with optional initial data."""
    path = os.path.join(tmpdir, "test-queue.db")
    if initial is None:
        queue_io.read_queue(path)
    else:
        def _seed(q):
            q.clear()
            q.update(queue_io._empty_queue())
            q.update(initial)
        queue_io.update_queue(path, _seed)
    return path


def _make_task(task_id="test-001", title="Test task", priority="P2", queued_by="test"):
    """Create a minimal valid task dict."""
    return {
        "id": task_id,
        "title": title,
        "status": "queued",
        "priority": priority,
        "queuedBy": queued_by,
        "queuedAt": queue_io._now_iso(),
    }


class TestAdd(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.qpath = _make_queue(self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_add_basic(self):
        task = _make_task()
        result = queue_io.add(self.qpath, task)
        self.assertTrue(result)
        q = queue_io.read_queue(self.qpath)
        self.assertEqual(len(q["queued"]), 1)
        self.assertEqual(q["queued"][0]["id"], "test-001")

    def test_add_sets_defaults(self):
        task = {"id": "test-002", "title": "No defaults"}
        queue_io.add(self.qpath, task)
        q = queue_io.read_queue(self.qpath)
        self.assertEqual(q["queued"][0]["status"], "queued")
        self.assertIn("queuedAt", q["queued"][0])

    def test_add_requires_id(self):
        with self.assertRaises(ValueError):
            queue_io.add(self.qpath, {"title": "No ID"})

    def test_add_creates_file(self):
        new_path = os.path.join(self.tmpdir, "new-queue.db")
        task = _make_task()
        queue_io.add(new_path, task)
        self.assertTrue(os.path.exists(new_path))
        q = queue_io.read_queue(new_path)
        self.assertEqual(len(q["queued"]), 1)


class TestDedup(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.qpath = _make_queue(self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_dedup_queued(self):
        """Same ID in queued blocks re-add."""
        queue_io.add(self.qpath, _make_task("dup-001"))
        result = queue_io.add(self.qpath, _make_task("dup-001"))
        self.assertFalse(result)
        q = queue_io.read_queue(self.qpath)
        self.assertEqual(len(q["queued"]), 1)

    def test_dedup_in_progress(self):
        """Same ID in in_progress blocks re-add."""
        queue_io.add(self.qpath, _make_task("dup-002"))
        queue_io.claim(self.qpath, "dup-002")
        result = queue_io.add(self.qpath, _make_task("dup-002"))
        self.assertFalse(result)

    def test_requeue_after_completed_blocked_by_default(self):
        """Completed tasks block re-add unless explicitly allowed."""
        queue_io.add(self.qpath, _make_task("requeue-001"))
        queue_io.claim(self.qpath, "requeue-001")
        queue_io.complete(self.qpath, "requeue-001", "done", routing={"action": "archive", "reason": "done"})
        result = queue_io.add(self.qpath, _make_task("requeue-001"))
        self.assertFalse(result)

    def test_requeue_after_completed_with_explicit_override(self):
        queue_io.add(self.qpath, _make_task("requeue-001b"))
        queue_io.claim(self.qpath, "requeue-001b")
        queue_io.complete(self.qpath, "requeue-001b", "done", routing={"action": "archive", "reason": "done"})
        result = queue_io.add(self.qpath, _make_task("requeue-001b"), allow_requeue=True)
        self.assertTrue(result)

    def test_requeue_after_failed_blocked_by_default(self):
        """Failed tasks block re-add unless explicitly allowed."""
        queue_io.add(self.qpath, _make_task("requeue-002"))
        queue_io.claim(self.qpath, "requeue-002")
        queue_io.fail(self.qpath, "requeue-002", "oops")
        result = queue_io.add(self.qpath, _make_task("requeue-002"))
        self.assertFalse(result)

    def test_requeue_after_failed_with_explicit_override(self):
        queue_io.add(self.qpath, _make_task("requeue-002b"))
        queue_io.claim(self.qpath, "requeue-002b")
        queue_io.fail(self.qpath, "requeue-002b", "oops")
        result = queue_io.add(self.qpath, _make_task("requeue-002b"), allow_requeue=True)
        self.assertTrue(result)

    def test_is_duplicate_check(self):
        """is_duplicate returns True across all sections by default."""
        queue_io.add(self.qpath, _make_task("check-001"))
        self.assertTrue(queue_io.is_duplicate(self.qpath, "check-001"))
        self.assertFalse(queue_io.is_duplicate(self.qpath, "nonexistent"))


class TestClaim(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.qpath = _make_queue(self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_claim_basic(self):
        queue_io.add(self.qpath, _make_task("claim-001"))
        task = queue_io.claim(self.qpath, "claim-001")
        self.assertIsNotNone(task)
        self.assertEqual(task["status"], "in_progress")
        self.assertIn("startedAt", task)
        self.assertIn("lastActivity", task)

    def test_claim_records_agent(self):
        queue_io.add(self.qpath, _make_task("claim-agent-001"))
        task = queue_io.claim(self.qpath, "claim-agent-001", agent="researcher")
        self.assertEqual(task["claimedBy"], "researcher")

    def test_claim_first_queued_when_no_task_id(self):
        queue_io.add(self.qpath, _make_task("claim-first-001"))
        queue_io.add(self.qpath, _make_task("claim-first-002"))
        task = queue_io.claim(self.qpath, agent="worker-1")
        self.assertEqual(task["id"], "claim-first-001")

    def test_claim_moves_section(self):
        queue_io.add(self.qpath, _make_task("claim-002"))
        queue_io.claim(self.qpath, "claim-002")
        q = queue_io.read_queue(self.qpath)
        self.assertEqual(len(q["queued"]), 0)
        self.assertEqual(len(q["in_progress"]), 1)
        self.assertEqual(q["in_progress"][0]["id"], "claim-002")

    def test_claim_nonexistent(self):
        task = queue_io.claim(self.qpath, "ghost-001")
        self.assertIsNone(task)

    def test_claim_already_in_progress(self):
        """Can't claim a task that's already in_progress."""
        queue_io.add(self.qpath, _make_task("claim-003"))
        queue_io.claim(self.qpath, "claim-003")
        task = queue_io.claim(self.qpath, "claim-003")
        self.assertIsNone(task)


class TestComplete(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.qpath = _make_queue(self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_complete_basic(self):
        queue_io.add(self.qpath, _make_task("comp-001"))
        queue_io.claim(self.qpath, "comp-001")
        task = queue_io.complete(self.qpath, "comp-001", "all done", routing={"action": "archive", "reason": "work complete"})
        self.assertIsNotNone(task)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["result"], "all done")
        self.assertIn("completedAt", task)

    def test_complete_moves_section(self):
        queue_io.add(self.qpath, _make_task("comp-002"))
        queue_io.claim(self.qpath, "comp-002")
        queue_io.complete(self.qpath, "comp-002", "done", routing={"action": "archive", "reason": "done"})
        q = queue_io.read_queue(self.qpath)
        self.assertEqual(len(q["in_progress"]), 0)
        self.assertEqual(len(q["completed"]), 1)

    def test_complete_nonexistent(self):
        task = queue_io.complete(self.qpath, "ghost-001", "done", routing={"action": "archive", "reason": "done"})
        self.assertIsNone(task)

    def test_complete_from_queued_fails(self):
        """Can't complete a task that's still queued."""
        queue_io.add(self.qpath, _make_task("comp-003"))
        task = queue_io.complete(self.qpath, "comp-003", "done", routing={"action": "archive", "reason": "done"})
        self.assertIsNone(task)


class TestFail(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.qpath = _make_queue(self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_fail_basic(self):
        queue_io.add(self.qpath, _make_task("fail-001"))
        queue_io.claim(self.qpath, "fail-001")
        task = queue_io.fail(self.qpath, "fail-001", "broke it")
        self.assertIsNotNone(task)
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["error"], "broke it")
        self.assertIn("completedAt", task)

    def test_fail_moves_section(self):
        queue_io.add(self.qpath, _make_task("fail-002"))
        queue_io.claim(self.qpath, "fail-002")
        queue_io.fail(self.qpath, "fail-002", "error")
        q = queue_io.read_queue(self.qpath)
        self.assertEqual(len(q["in_progress"]), 0)
        self.assertEqual(len(q["failed"]), 1)


class TestRetry(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.qpath = _make_queue(self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_retry_basic(self):
        queue_io.add(self.qpath, _make_task("retry-001"))
        queue_io.claim(self.qpath, "retry-001")
        queue_io.fail(self.qpath, "retry-001", "oops")
        task = queue_io.retry(self.qpath, "retry-001")
        self.assertIsNotNone(task)
        self.assertEqual(task["status"], "queued")
        self.assertEqual(task["retryCount"], 1)

    def test_retry_moves_section(self):
        queue_io.add(self.qpath, _make_task("retry-002"))
        queue_io.claim(self.qpath, "retry-002")
        queue_io.fail(self.qpath, "retry-002", "oops")
        queue_io.retry(self.qpath, "retry-002")
        q = queue_io.read_queue(self.qpath)
        self.assertEqual(len(q["failed"]), 0)
        self.assertEqual(len(q["queued"]), 1)

    def test_retry_clears_error(self):
        queue_io.add(self.qpath, _make_task("retry-003"))
        queue_io.claim(self.qpath, "retry-003")
        queue_io.fail(self.qpath, "retry-003", "oops")
        task = queue_io.retry(self.qpath, "retry-003")
        self.assertNotIn("error", task)
        self.assertNotIn("completedAt", task)
        self.assertNotIn("routing", task)
        self.assertNotIn("result", task)
        self.assertNotIn("claimedBy", task)

    def test_retry_max_retries(self):
        """Respects maxRetries limit."""
        task = _make_task("retry-004")
        task["maxRetries"] = 2
        queue_io.add(self.qpath, task)

        # Retry cycle 1
        queue_io.claim(self.qpath, "retry-004")
        queue_io.fail(self.qpath, "retry-004", "oops1")
        r1 = queue_io.retry(self.qpath, "retry-004")
        self.assertIsNotNone(r1)

        # Retry cycle 2
        queue_io.claim(self.qpath, "retry-004")
        queue_io.fail(self.qpath, "retry-004", "oops2")
        r2 = queue_io.retry(self.qpath, "retry-004")
        self.assertIsNotNone(r2)

        # Retry cycle 3 — should be blocked (count=2 >= max=2)
        queue_io.claim(self.qpath, "retry-004")
        queue_io.fail(self.qpath, "retry-004", "oops3")
        r3 = queue_io.retry(self.qpath, "retry-004")
        self.assertIsNone(r3)

    def test_retry_nonexistent(self):
        task = queue_io.retry(self.qpath, "ghost-001")
        self.assertIsNone(task)


class TestListTasks(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.qpath = _make_queue(self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_list_all(self):
        queue_io.add(self.qpath, _make_task("list-001"))
        queue_io.add(self.qpath, _make_task("list-002"))
        queue_io.claim(self.qpath, "list-002")
        tasks = queue_io.list_tasks(self.qpath)
        self.assertEqual(len(tasks), 2)

    def test_list_filtered(self):
        queue_io.add(self.qpath, _make_task("list-003"))
        queue_io.add(self.qpath, _make_task("list-004"))
        queue_io.claim(self.qpath, "list-004")
        queued = queue_io.list_tasks(self.qpath, "queued")
        in_progress = queue_io.list_tasks(self.qpath, "in_progress")
        self.assertEqual(len(queued), 1)
        self.assertEqual(len(in_progress), 1)
        self.assertEqual(queued[0]["id"], "list-003")
        self.assertEqual(in_progress[0]["id"], "list-004")

    def test_list_empty(self):
        tasks = queue_io.list_tasks(self.qpath)
        self.assertEqual(len(tasks), 0)


class TestStaleDetection(unittest.TestCase):
    def test_stale_task(self):
        task = {
            "id": "stale-001",
            "status": "in_progress",
            "lastActivity": (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(),
        }
        self.assertTrue(queue_io.is_stale(task, days=3))

    def test_fresh_task(self):
        task = {
            "id": "fresh-001",
            "status": "in_progress",
            "lastActivity": datetime.now(timezone.utc).isoformat(),
        }
        self.assertFalse(queue_io.is_stale(task, days=3))

    def test_not_in_progress(self):
        task = {
            "id": "queued-001",
            "status": "queued",
            "lastActivity": (datetime.now(timezone.utc) - timedelta(days=10)).isoformat(),
        }
        self.assertFalse(queue_io.is_stale(task))

    def test_no_last_activity_uses_started_at(self):
        task = {
            "id": "stale-002",
            "status": "in_progress",
            "startedAt": (datetime.now(timezone.utc) - timedelta(days=5)).isoformat(),
        }
        self.assertTrue(queue_io.is_stale(task, days=3))

    def test_custom_days(self):
        task = {
            "id": "stale-003",
            "status": "in_progress",
            "lastActivity": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),
        }
        self.assertFalse(queue_io.is_stale(task, days=1))
        # But stale at 0 days
        self.assertTrue(queue_io.is_stale(task, days=0))


class TestConcurrentLockSafety(unittest.TestCase):
    """Test that concurrent writes don't corrupt the queue."""

    def test_concurrent_adds(self):
        tmpdir = tempfile.mkdtemp()
        qpath = _make_queue(tmpdir)
        success_count = Value("i", 0)

        # Spawn 10 concurrent adds (top-level fn for macOS spawn pickle)
        procs = []
        for i in range(10):
            p = Process(target=_mp_add_task, args=(qpath, f"concurrent-{i:03d}", success_count))
            procs.append(p)
            p.start()

        for p in procs:
            p.join(timeout=10)

        # All 10 should succeed (unique IDs)
        self.assertEqual(success_count.value, 10)

        # Queue should have exactly 10 tasks
        q = queue_io.read_queue(qpath)
        self.assertEqual(len(q["queued"]), 10)

        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    def test_concurrent_dedup(self):
        """Same ID from concurrent processes — only one should win."""
        tmpdir = tempfile.mkdtemp()
        qpath = _make_queue(tmpdir)
        success_count = Value("i", 0)

        procs = []
        for _ in range(5):
            p = Process(target=_mp_add_dup, args=(qpath, success_count))
            procs.append(p)
            p.start()

        for p in procs:
            p.join(timeout=10)

        # Exactly 1 should succeed
        self.assertEqual(success_count.value, 1)

        q = queue_io.read_queue(qpath)
        self.assertEqual(len(q["queued"]), 1)

        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


class TestFullLifecycle(unittest.TestCase):
    """End-to-end lifecycle: add → claim → complete/fail → retry."""

    def test_happy_path(self):
        tmpdir = tempfile.mkdtemp()
        qpath = _make_queue(tmpdir)

        # Add
        queue_io.add(qpath, _make_task("lifecycle-001", "Happy path test"))

        # Claim
        task = queue_io.claim(qpath, "lifecycle-001")
        self.assertEqual(task["status"], "in_progress")

        # Complete
        task = queue_io.complete(qpath, "lifecycle-001", "Success!", routing={"action": "archive", "reason": "success path"})
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["result"], "Success!")

        # Verify final state
        q = queue_io.read_queue(qpath)
        self.assertEqual(len(q["queued"]), 0)
        self.assertEqual(len(q["in_progress"]), 0)
        self.assertEqual(len(q["completed"]), 1)
        self.assertEqual(len(q["failed"]), 0)

        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    def test_fail_and_retry_path(self):
        tmpdir = tempfile.mkdtemp()
        qpath = _make_queue(tmpdir)

        # Add → Claim → Fail
        queue_io.add(qpath, _make_task("lifecycle-002", "Fail+retry test"))
        queue_io.claim(qpath, "lifecycle-002")
        queue_io.fail(qpath, "lifecycle-002", "First attempt failed")

        # Retry → Claim → Complete
        queue_io.retry(qpath, "lifecycle-002")
        task = queue_io.claim(qpath, "lifecycle-002")
        self.assertEqual(task["retryCount"], 1)
        task = queue_io.complete(qpath, "lifecycle-002", "Second attempt worked", routing={"action": "archive", "reason": "retry succeeded"})

        # Verify
        q = queue_io.read_queue(qpath)
        self.assertEqual(len(q["completed"]), 1)
        self.assertEqual(q["completed"][0]["retryCount"], 1)

        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    def test_queue_file_version(self):
        """Queue preserves version field."""
        tmpdir = tempfile.mkdtemp()
        qpath = os.path.join(tmpdir, "versioned.db")
        initial = {"version": "1", "agent": "test", "queued": [], "in_progress": [], "completed": [], "failed": []}

        def _seed(q):
            q.clear()
            q.update(queue_io._empty_queue())
            q.update(initial)

        queue_io.update_queue(qpath, _seed)
        queue_io.add(qpath, _make_task("ver-001"))
        q = queue_io.read_queue(qpath)
        self.assertEqual(q.get("version"), "1")
        self.assertEqual(q.get("agent"), "test")

        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


class TestRoutingRequirements(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.qpath = _make_queue(self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_complete_requires_routing(self):
        queue_io.add(self.qpath, _make_task("route-001"))
        queue_io.claim(self.qpath, "route-001")
        with self.assertRaises(ValueError):
            queue_io.complete(self.qpath, "route-001", "done")

    def test_complete_rejects_invalid_routing_action(self):
        queue_io.add(self.qpath, _make_task("route-002"))
        queue_io.claim(self.qpath, "route-002")
        with self.assertRaises(ValueError):
            queue_io.complete(self.qpath, "route-002", "done", routing={"action": "ship", "reason": "bad"})


class TestEdgeCases(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.qpath = _make_queue(self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_corrupted_file_recovery(self):
        """Handles corrupted/empty queue file gracefully."""
        with open(self.qpath, "w") as f:
            f.write("not json{{{")
        q = queue_io.read_queue(self.qpath)
        self.assertEqual(len(q["queued"]), 0)

    def test_missing_file_recovery(self):
        """Handles missing queue file gracefully."""
        os.unlink(self.qpath)
        q = queue_io.read_queue(self.qpath)
        self.assertEqual(len(q["queued"]), 0)

    def test_empty_result_string(self):
        queue_io.add(self.qpath, _make_task("edge-001"))
        queue_io.claim(self.qpath, "edge-001")
        task = queue_io.complete(self.qpath, "edge-001", "", routing={"action": "archive", "reason": "done"})
        self.assertIsNotNone(task)
        self.assertNotIn("result", task)  # empty string → not stored

    def test_optional_fields_preserved(self):
        """Optional protocol fields survive the lifecycle."""
        task = _make_task("edge-002")
        task["context"] = "Some context"
        task["deliverable"] = "A file"
        task["dependsOn"] = ["other-001"]
        task["gateCondition"] = "Wait for approval"
        task["staleDays"] = 7
        queue_io.add(self.qpath, task)
        queue_io.claim(self.qpath, "edge-002")
        result = queue_io.complete(self.qpath, "edge-002", "done", routing={"action": "review", "reason": "optional fields verified"})
        self.assertEqual(result["context"], "Some context")
        self.assertEqual(result["dependsOn"], ["other-001"])
        self.assertEqual(result["staleDays"], 7)


if __name__ == "__main__":
    unittest.main()
