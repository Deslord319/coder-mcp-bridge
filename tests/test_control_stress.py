from __future__ import annotations

import threading
import time
import unittest

from control_plane import ZCodeControlPlane


def eventually(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.005)
    return predicate()


class AutoProtocol:
    def __init__(self, duration=0.005):
        self.on_notification = None
        self.on_disconnect = None
        self.lock = threading.RLock()
        self.next_session = 0
        self.sessions = {}
        self.active = 0
        self.max_active = 0
        self.active_resources = set()
        self.resource_violations = []
        self.duration = duration

    def _snapshot(self, session_id):
        item = self.sessions[session_id]
        return {
            "session": {
                "sessionId": session_id, "status": item["status"],
                "workspace": {"workspacePath": item["resource"], "workspaceKey": item["resource"]},
            },
            "projection": {"sessionId": session_id, "status": item["status"], "backgroundJobs": []},
            "runtime": {"eventSeq": 0, "stateRevision": 1, "contextUsage": {"size": 1000000, "used": 1000, "cache": {}}},
        }

    def request(self, method, params=None, timeout=30):
        params = params or {}
        if method == "session/create":
            with self.lock:
                self.next_session += 1
                session_id = "sess_stress_%s" % self.next_session
                self.sessions[session_id] = {"resource": params["workspace"]["workspacePath"], "status": "idle"}
            return self._snapshot(session_id)
        if method == "session/resume":
            return self._snapshot(params["sessionId"])
        if method == "session/subscribe":
            return {"eventSeq": 0, "events": [], "snapshot": self._snapshot(params["sessionId"])}
        if method == "session/send":
            session_id = params["sessionId"]
            with self.lock:
                resource = self.sessions[session_id]["resource"]
                if resource in self.active_resources:
                    self.resource_violations.append(resource)
                self.active_resources.add(resource)
                self.sessions[session_id]["status"] = "running"
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            threading.Thread(target=self._finish, args=(session_id,), daemon=True).start()
            return {"accepted": True, "stateRevision": 2}
        if method == "session/messages":
            return {"messages": [{"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "stress-ok"}]}]}
        if method == "session/read":
            return self._snapshot(params["sessionId"])
        if method == "session/usage":
            return {
                "sessionId": params["sessionId"], "totalTokens": 12,
                "inputTokens": 10, "outputTokens": 2, "reasoningTokens": 1,
                "cacheCreationTokens": 0, "cacheReadTokens": 0,
                "modelRequestCount": 1, "modelErrorCount": 0,
            }
        if method == "session/subagents":
            return {"revision": 1, "childSessionIds": [], "running": [], "ended": {"total": 0, "items": []}}
        if method == "session/stop":
            return {"accepted": True}
        return {"accepted": True}

    def _finish(self, session_id):
        time.sleep(self.duration)
        self.on_notification({
            "method": "v4/telemetry/event",
            "params": {"sessionId": session_id, "kind": "usage.delta", "inputTokens": 10, "outputTokens": 2, "reasoningTokens": 1},
        })
        with self.lock:
            self.active -= 1
            self.active_resources.discard(self.sessions[session_id]["resource"])
            self.sessions[session_id]["status"] = "idle"
        self.on_notification({
            "method": "v4/telemetry/event",
            "params": {"sessionId": session_id, "kind": "turn.terminal", "status": "success"},
        })

    def close(self):
        return None


class BlockingProtocol(AutoProtocol):
    def __init__(self):
        super().__init__()
        self.release = threading.Event()

    def request(self, method, params=None, timeout=30):
        if method == "session/create":
            self.release.wait(2)
        return super().request(method, params, timeout)


class ControlStressTest(unittest.TestCase):
    def test_zero_limit_leaves_twenty_independent_runs_to_codex(self):
        protocol = AutoProtocol(duration=2)
        control = ZCodeControlPlane(protocol=protocol, max_concurrency=0)
        runs = [control.start({"prompt": "parallel %s" % index, "cwd": "/tmp/codex-owned-%s" % index})["runId"] for index in range(20)]
        self.assertTrue(eventually(lambda: all(control.snapshot(run_id)["status"] == "running" for run_id in runs)))
        self.assertEqual(20, protocol.max_active)
        self.assertEqual([], protocol.resource_violations)

    def test_measured_wall_clock_proves_independent_runs_are_parallel(self):
        protocol = AutoProtocol(duration=0.08)
        control = ZCodeControlPlane(protocol=protocol, max_concurrency=0)
        started = time.monotonic()
        runs = [control.start({"prompt": "parallel", "cwd": "/tmp/wall-%s" % index})["runId"] for index in range(8)]
        self.assertTrue(eventually(lambda: all(control.snapshot(run_id)["status"] == "completed" for run_id in runs)))
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(protocol.max_active, 6)
        self.assertLess(elapsed, 0.35, "parallel wall clock %.3fs looks serialized" % elapsed)

    def test_one_hundred_runs_obey_optional_cap_and_worktree_locks(self):
        protocol = AutoProtocol(duration=0.004)
        control = ZCodeControlPlane(protocol=protocol, max_concurrency=8)
        runs = [control.start({"prompt": "task %s" % index, "cwd": "/tmp/stress-worktree-%s" % (index % 10)})["runId"] for index in range(100)]
        self.assertTrue(eventually(lambda: all(control.snapshot(run_id)["status"] == "completed" for run_id in runs)))
        self.assertLessEqual(protocol.max_active, 8)
        self.assertGreater(protocol.max_active, 1)
        self.assertEqual([], protocol.resource_violations)
        self.assertTrue(all(control.snapshot(run_id)["usage"]["reasoningTokens"] == 1 for run_id in runs))

    def test_thirty_two_waiters_wake_on_one_meaningful_event(self):
        protocol = AutoProtocol(duration=2)
        control = ZCodeControlPlane(protocol=protocol, max_concurrency=1)
        run_id = control.start({"prompt": "wait", "cwd": "/tmp/waiters"})["runId"]
        self.assertTrue(eventually(lambda: control.snapshot(run_id)["status"] == "running"))
        revision = control.snapshot(run_id)["revision"]
        results = []

        def wait_once():
            results.append(control.wait(run_id, after_revision=revision, timeout_ms=2000))

        threads = [threading.Thread(target=wait_once) for _ in range(32)]
        for thread in threads:
            thread.start()
        time.sleep(0.03)
        session_id = control.snapshot(run_id)["threadId"]
        control._on_notification({
            "method": "v4/telemetry/event",
            "params": {"sessionId": session_id, "kind": "tool.lifecycle", "status": "started", "toolCallId": "one", "toolName": "Read"},
        })
        for thread in threads:
            thread.join(2)
        self.assertEqual(32, len(results))
        self.assertTrue(all(result["changed"] for result in results))

    def test_start_returns_while_native_session_open_is_blocked(self):
        protocol = BlockingProtocol()
        control = ZCodeControlPlane(protocol=protocol, max_concurrency=1)
        started = time.monotonic()
        run = control.start({"prompt": "nonblocking", "cwd": "/tmp/nonblocking"})
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertIn(run["status"], {"queued", "starting"})
        protocol.release.set()


if __name__ == "__main__":
    unittest.main()
