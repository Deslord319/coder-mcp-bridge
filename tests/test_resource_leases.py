from __future__ import annotations

from contextlib import closing
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from resource_leases import ResourceLeaseStore
from control_plane import ZCodeControlPlane
from tests.test_control_plane import FakeProtocol, eventually


class ResourceLeaseStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="zcode-lease-test-")
        self.path = os.path.join(self.temp.name, "leases.sqlite")
        self.stores = []

    def tearDown(self):
        for store in reversed(self.stores):
            store.close()
        self.temp.cleanup()

    def store(self, owner):
        store = ResourceLeaseStore(
            self.path,
            owner_id=owner,
            heartbeat_seconds=0.05,
            stale_seconds=0.3,
            poll_seconds=0.02,
        )
        self.stores.append(store)
        return store

    def test_exclusive_conflict_is_atomic_and_release_unblocks(self):
        first = self.store("first")
        second = self.store("second")
        self.assertTrue(first.try_acquire("run_a", {"sim:A": "exclusive"})["acquired"])
        blocked = second.try_acquire("run_b", {"sim:A": "exclusive", "repo:B": "exclusive"})
        self.assertFalse(blocked["acquired"])
        self.assertEqual([], second.snapshot("run_b"), "all-or-none acquisition must not leak repo:B")
        self.assertTrue(second.try_acquire("run_independent", {
            "sim:B": "exclusive"
        })["acquired"])
        second.release("run_independent")
        first.release("run_a")
        self.assertTrue(second.try_acquire("run_b", {"sim:A": "exclusive"})["acquired"])

    def test_shared_readers_overlap_and_writer_waits(self):
        first = self.store("reader-one")
        second = self.store("reader-two")
        writer = self.store("writer")
        self.assertTrue(first.try_acquire("r1", {"repo": "shared"})["acquired"])
        self.assertTrue(second.try_acquire("r2", {"repo": "shared"})["acquired"])
        self.assertFalse(writer.try_acquire("w", {"repo": "exclusive"})["acquired"])
        first.release("r1")
        self.assertFalse(writer.try_acquire("w", {"repo": "exclusive"})["acquired"])
        second.release("r2")
        self.assertTrue(writer.try_acquire("w", {"repo": "exclusive"})["acquired"])

    def test_real_second_process_observes_the_same_lease(self):
        script = """
import sys
from resource_leases import ResourceLeaseStore
store = ResourceLeaseStore(sys.argv[1], heartbeat_seconds=0.05, stale_seconds=1)
assert store.try_acquire('child-run', {'sim:shared-machine': 'exclusive'})['acquired']
print('READY', flush=True)
sys.stdin.readline()
store.close()
"""
        child = subprocess.Popen(
            [sys.executable, "-c", script, self.path],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual("READY", child.stdout.readline().strip())
            parent = self.store("parent")
            self.assertFalse(parent.try_acquire(
                "parent-run", {"sim:shared-machine": "exclusive"}
            )["acquired"])
            child.stdin.write("stop\n")
            child.stdin.flush()
            child.stdin.close()
            self.assertEqual(0, child.wait(timeout=3), child.stderr.read())
            child.stdout.close()
            child.stderr.close()
            deadline = time.monotonic() + 2
            acquired = False
            while time.monotonic() < deadline and not acquired:
                acquired = parent.try_acquire(
                    "parent-run", {"sim:shared-machine": "exclusive"}
                )["acquired"]
                if not acquired:
                    time.sleep(0.02)
            self.assertTrue(acquired)
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=3)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None and not stream.closed:
                    stream.close()

    def test_two_control_planes_serialize_one_declared_worktree(self):
        first_protocol = FakeProtocol()
        second_protocol = FakeProtocol()
        first_control = ZCodeControlPlane(
            protocol=first_protocol,
            lease_store=self.store("control-one"),
        )
        second_control = ZCodeControlPlane(
            protocol=second_protocol,
            lease_store=self.store("control-two"),
        )
        try:
            first = first_control.start({
                "prompt": "first",
                "cwd": "/tmp/cross-process-worktree",
            })
            first_id = first["runId"]
            first_session = eventually(
                lambda: first_control.snapshot(first_id).get("threadId")
            )
            self.assertTrue(first_session)
            second = second_control.start({
                "prompt": "second",
                "cwd": "/tmp/cross-process-worktree",
            })
            second_id = second["runId"]
            self.assertTrue(eventually(
                lambda: second_control.snapshot(second_id)["phase"] ==
                "waiting-for-global-resource"
            ))
            first_protocol.emit(first_session, "turn.terminal", status="success")
            self.assertTrue(eventually(
                lambda: second_control.snapshot(second_id).get("threadId"),
                timeout=2,
            ))
        finally:
            first_control.close()
            second_control.close()

    def test_crashed_bridge_lease_is_reclaimed_after_stale_heartbeat(self):
        script = """
import sys, time
from resource_leases import ResourceLeaseStore
store = ResourceLeaseStore(sys.argv[1], heartbeat_seconds=0.05, stale_seconds=0.3)
assert store.try_acquire('crashed-run', {'sim:crash': 'exclusive'})['acquired']
print('READY', flush=True)
while True:
    time.sleep(1)
"""
        child = subprocess.Popen(
            [sys.executable, "-c", script, self.path],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual("READY", child.stdout.readline().strip())
            parent = self.store("crash-reclaimer")
            self.assertFalse(parent.try_acquire(
                "replacement", {"sim:crash": "exclusive"}
            )["acquired"])
            child.terminate()
            child.wait(timeout=3)
            deadline = time.monotonic() + 2
            acquired = False
            while time.monotonic() < deadline and not acquired:
                acquired = parent.try_acquire(
                    "replacement", {"sim:crash": "exclusive"}
                )["acquired"]
                if not acquired:
                    time.sleep(0.03)
            self.assertTrue(acquired)
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=3)
            child.stdout.close()
            child.stderr.close()

    def test_live_native_guard_prevents_reclaim_of_crashed_bridge_owner(self):
        parent = self.store("guard-checker")
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "insert into bridge_owner(owner_id,pid,heartbeat_ms,guard_pid) values(?,?,?,?)",
                ("dead-bridge", 99999999, 0, os.getpid()),
            )
            connection.execute(
                "insert into resource_lease(owner_id,run_id,resource_key,mode,acquired_ms,updated_ms) "
                "values(?,?,?,?,?,?)",
                ("dead-bridge", "native-live", "sim:guarded", "exclusive", 0, 0),
            )
        self.assertFalse(parent.try_acquire(
            "replacement", {"sim:guarded": "exclusive"}
        )["acquired"])
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "update bridge_owner set guard_pid=? where owner_id='dead-bridge'",
                (99999998,),
            )
        self.assertTrue(parent.try_acquire(
            "replacement", {"sim:guarded": "exclusive"}
        )["acquired"])


if __name__ == "__main__":
    unittest.main()
