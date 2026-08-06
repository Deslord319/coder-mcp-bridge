from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest

from agent_control_plane import AdapterControlPlane
from control_plane import ControlPlaneError
from resource_leases import ResourceLeaseStore


def eventually(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    return predicate()


class FakeRuntime:
    def __init__(self, backend, args, emit, disconnect):
        self.backend = backend
        self.args = args
        self.emit = emit
        self.disconnect = disconnect
        self.closed = False
        self.guidance = []
        self.thread_id = args.get("threadId") or "session-%s" % len(backend.runtimes)

    def start(self, prompt):
        self.backend.started.append((self.thread_id, time.monotonic(), self.args["cwd"]))
        if prompt is not None:
            threading.Thread(target=self._complete, args=(prompt,), daemon=True).start()
        return {"threadId": self.thread_id}

    def _complete(self, prompt):
        self.emit({"type": "reasoning.started"})
        self.emit({"type": "tool.started", "toolCallId": "tool", "toolName": "bash"})
        time.sleep(self.backend.delay)
        self.emit({"type": "tool.ended", "toolCallId": "tool", "toolName": "bash"})
        self.emit({"type": "reasoning.ended"})
        self.emit({
            "type": "settled",
            "status": "completed",
            "result": "done:" + prompt,
            "usage": {"modelRequests": 1, "totalTokens": 10},
        })

    def refresh(self):
        self.emit({"type": "native.refreshed", "context": {"used": 10}})

    def guide(self, prompt, *, interrupt=False):
        self.guidance.append((prompt, interrupt))

    def cancel(self):
        self.emit({"type": "settled", "status": "cancelled"})

    def branch(self, **_kwargs):
        return {"threadId": self.thread_id + "-fork", "parentThreadId": self.thread_id}

    def context(self, *, action, instructions=None):
        return {"context": {"action": action}, "instructions": instructions}

    def close(self):
        self.closed = True


class FakeBackend:
    name = "fake"
    capabilities = {"prompt": True}

    def __init__(self, delay=0.08):
        self.delay = delay
        self.runtimes = []
        self.started = []

    def create_runtime(self, args, emit, disconnect):
        runtime = FakeRuntime(self, args, emit, disconnect)
        self.runtimes.append(runtime)
        return runtime

    def list_sessions(self, _args):
        return [{"threadId": "saved", "cwd": os.getcwd(), "status": "idle"}]

    def close(self):
        pass


class AgentControlPlaneTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.lease = ResourceLeaseStore(
            os.path.join(self.temp.name, "leases.sqlite"),
            heartbeat_seconds=0.05,
            poll_seconds=0.02,
        )
        self.backend = FakeBackend()
        self.control = AdapterControlPlane(self.backend, lease_store=self.lease)

    def tearDown(self):
        self.control.close()
        self.temp.cleanup()

    def terminal(self, run_id):
        def read_terminal():
            state = self.control.snapshot(run_id, result_chars=12000)
            return state if state["status"] in {
                "completed", "failed", "cancelled", "timed_out"
            } else None

        return eventually(read_terminal)

    def test_normalized_events_usage_result_and_control(self):
        run = self.control.start({"prompt": "work", "cwd": self.temp.name, "timeout": 5})
        self.assertLess(run["elapsedMs"], 1000)
        self.assertTrue(eventually(lambda: self.backend.runtimes))
        guided = self.control.control(run["runId"], "guide", prompt="next")
        self.assertEqual("fake", guided["backend"])
        self.assertEqual([("next", False)], self.backend.runtimes[0].guidance)

        final = self.terminal(run["runId"])
        self.assertEqual("completed", final["status"])
        self.assertEqual("done:work", final["result"])
        self.assertEqual(1, final["usage"]["modelRequests"])
        self.assertEqual(1, final["counts"]["toolCalls"])
        self.assertFalse(final["resourceLease"]["acquired"])

    def test_independent_worktrees_overlap_and_conflict_serializes(self):
        other = os.path.join(self.temp.name, "other")
        os.mkdir(other)
        first = self.control.start({"prompt": "one", "cwd": self.temp.name, "timeout": 5})
        second = self.control.start({"prompt": "two", "cwd": other, "timeout": 5})
        self.assertTrue(eventually(lambda: len(self.backend.started) == 2))
        self.assertLess(abs(self.backend.started[0][1] - self.backend.started[1][1]), 0.07)
        self.terminal(first["runId"])
        self.terminal(second["runId"])

        self.backend.started.clear()
        third = self.control.start({"prompt": "three", "cwd": self.temp.name, "timeout": 5})
        fourth = self.control.start({"prompt": "four", "cwd": self.temp.name, "timeout": 5})
        self.assertTrue(eventually(lambda: len(self.backend.started) == 2))
        self.assertGreaterEqual(self.backend.started[1][1] - self.backend.started[0][1], 0.07)
        self.terminal(third["runId"])
        self.terminal(fourth["runId"])

    def test_recover_branch_context_cancel_and_close(self):
        listed = self.control.recover({"limit": 10})
        self.assertEqual("saved", listed["sessions"][0]["threadId"])
        adopted = self.control.recover({"adoptThreadId": "saved", "cwd": self.temp.name})
        final = self.terminal(adopted["runId"])
        self.assertEqual("recovered-idle", final["phase"])
        self.assertEqual("saved-fork", self.control.branch(final["runId"])["threadId"])
        self.assertEqual("inspect", self.control.context(final["runId"])["context"]["action"])
        closed = self.control.close_run(final["runId"])
        self.assertEqual("closed", closed["status"])
        self.assertTrue(self.backend.runtimes[-1].closed)

    def test_close_accepts_thread_id_for_a_managed_runtime(self):
        run = self.control.start({"prompt": "work", "cwd": self.temp.name, "timeout": 5})
        final = self.terminal(run["runId"])
        closed = self.control.close_run(thread_id=final["threadId"])
        self.assertEqual("closed", closed["status"])
        self.assertEqual(run["runId"], closed["runId"])

    def test_terminal_guidance_requires_a_new_run_to_reacquire_resources(self):
        run = self.control.start({"prompt": "work", "cwd": self.temp.name, "timeout": 5})
        final = self.terminal(run["runId"])
        with self.assertRaises(ControlPlaneError) as caught:
            self.control.control(final["runId"], "guide", prompt="continue")
        self.assertEqual("run_terminal", caught.exception.code)

    def test_launch_failure_closes_runtime_and_releases_lease(self):
        original = FakeRuntime.start

        def fail_start(_runtime, _prompt):
            raise RuntimeError("native launch failed")

        FakeRuntime.start = fail_start
        try:
            run = self.control.start({"prompt": "work", "cwd": self.temp.name, "timeout": 5})
            final = self.terminal(run["runId"])
        finally:
            FakeRuntime.start = original
        self.assertEqual("failed", final["status"])
        self.assertFalse(final["resourceLease"]["acquired"])
        self.assertTrue(self.backend.runtimes[-1].closed)


if __name__ == "__main__":
    unittest.main()
