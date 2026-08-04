from __future__ import annotations

import json
import threading
import time
import unittest

from control_plane import ControlPlaneError, ZCodeControlPlane, extract_last_assistant_text


def eventually(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    return predicate()


class FakeProtocol:
    def __init__(self):
        self.on_notification = None
        self.on_disconnect = None
        self.on_server_request = None
        self.lock = threading.RLock()
        self.calls = []
        self.next_session = 0
        self.sessions = {}
        self.read_text = "done"
        self.background_jobs = []
        self.usage = {
            "totalTokens": 130,
            "inputTokens": 100,
            "outputTokens": 30,
            "reasoningTokens": 20,
            "cacheCreationTokens": 3,
            "cacheReadTokens": 80,
            "modelRequestCount": 2,
            "modelErrorCount": 0,
        }
        self.agents = {"revision": 1, "childSessionIds": [], "running": [], "ended": {"total": 0, "items": []}}
        self.goal_error_after_start = None
        self.goal_terminal_before_response = False

    def _snapshot(self, session_id):
        session = self.sessions[session_id]
        return {
            "session": session,
            "projection": {
                "sessionId": session_id,
                "status": session["status"],
                "backgroundJobs": list(self.background_jobs),
                "target": session.get("target"),
            },
            "runtime": {
                "eventSeq": session.get("eventSeq", 0),
                "stateRevision": session.get("revision", 0),
                "contextUsage": {
                    "size": 1000000,
                    "used": 250000,
                    "cache": {"hitRate": 0.7, "latestHitRate": 0.8, "totalCacheReadTokens": 80},
                },
            },
        }

    def request(self, method, params=None, timeout=30):
        params = params or {}
        with self.lock:
            self.calls.append((method, dict(params)))
            if method == "session/create":
                self.next_session += 1
                session_id = "sess_%s" % self.next_session
                workspace = params["workspace"]
                self.sessions[session_id] = {
                    "sessionId": session_id,
                    "status": "idle",
                    "workspace": workspace,
                    "mode": params.get("mode", "build"),
                    "revision": 0,
                    "eventSeq": 0,
                }
                return self._snapshot(session_id)
            if method == "session/resume":
                session_id = params["sessionId"]
                self.sessions.setdefault(session_id, {
                    "sessionId": session_id,
                    "status": "idle",
                    "workspace": {"workspacePath": "/tmp/recovered", "workspaceKey": "/tmp/recovered"},
                    "mode": "build", "revision": 0, "eventSeq": 0,
                })
                return self._snapshot(session_id)
            if method == "session/subscribe":
                session_id = params["sessionId"]
                return {"sessionId": session_id, "eventSeq": self.sessions[session_id]["eventSeq"], "events": [], "snapshot": self._snapshot(session_id)}
            if method == "session/send":
                session = self.sessions[params["sessionId"]]
                session["status"] = "running"
                session["revision"] += 1
                return {"accepted": True, "stateRevision": session["revision"]}
            if method == "session/messages":
                return {"messages": [{"info": {"role": "assistant"}, "parts": [{"type": "text", "text": self.read_text}]}]}
            if method == "session/read":
                value = self._snapshot(params["sessionId"])
                value["messages"] = [{"info": {"role": "assistant"}, "parts": [{"type": "text", "text": self.read_text}]}]
                return value
            if method == "session/usage":
                return {"sessionId": params["sessionId"], **self.usage}
            if method == "session/subagents":
                return self.agents
            if method == "session/list":
                return {"sessions": list(self.sessions.values())[:params.get("limit", 20)]}
            if method == "session/goal":
                session = self.sessions[params["sessionId"]]
                action = params["action"]
                if action == "set":
                    session["target"] = {"objective": params["objective"], "status": "active"}
                    session["status"] = "running"
                    if self.goal_terminal_before_response:
                        self.on_notification({
                            "method": "v4/telemetry/event",
                            "params": {
                                "sessionId": params["sessionId"],
                                "kind": "turn.terminal",
                                "status": "success",
                            },
                        })
                    if self.goal_error_after_start:
                        raise RuntimeError(self.goal_error_after_start)
                elif action == "pause" and session.get("target"):
                    session["target"]["status"] = "paused"
                elif action == "resume" and session.get("target"):
                    session["target"]["status"] = "active"
                result = self._snapshot(params["sessionId"])
                if action == "set":
                    result["startedTurn"] = True
                return result
            if method == "session/fork":
                return {
                    "forkedSessionId": "sess_forked", "parentSessionId": params["sessionId"],
                    "targetCheckpointId": "cp_latest", "response": "forked",
                }
            if method == "session/compact":
                return {"response": "compacted", "snapshot": self._snapshot(params["sessionId"]), "compact": {"state": "accepted", "operationId": "op_1"}}
            if method == "session/close":
                self.sessions[params["sessionId"]]["status"] = "closed"
                return {"closed": True}
            if method == "session/stop":
                self.sessions[params["sessionId"]]["status"] = "idle"
                return self._snapshot(params["sessionId"])
            if method == "session/cancelBackgroundTask":
                for job in self.background_jobs:
                    if job.get("taskId") == params["taskId"]:
                        job["status"] = "cancelled"
                return {"cancelled": True, "status": "cancelled", "taskId": params["taskId"]}
            return {"accepted": True}

    def close(self):
        return None

    def emit(self, session_id, kind, **detail):
        if kind == "turn.terminal":
            self.sessions[session_id]["status"] = "idle"
        self.on_notification({
            "method": "v4/telemetry/event",
            "params": {"sessionId": session_id, "kind": kind, **detail},
        })

    def emit_native(self, session_id, event_type, **payload):
        session = self.sessions[session_id]
        session["eventSeq"] += 1
        if isinstance(payload.get("target"), dict):
            session["target"] = dict(payload["target"])
        self.on_notification({
            "method": "session/event",
            "params": {
                "sessionId": session_id,
                "seq": session["eventSeq"],
                "type": event_type,
                "payload": payload,
            },
        })

    def methods(self, method):
        with self.lock:
            return [params for called, params in self.calls if called == method]


class ControlPlaneTest(unittest.TestCase):
    def setUp(self):
        self.protocol = FakeProtocol()
        self.control = ZCodeControlPlane(protocol=self.protocol, max_concurrency=0)

    def start(self, prompt, cwd, *, access="exclusive", resources=None, **extra):
        args = {
            "cwd": "/tmp/%s" % cwd,
            "workspaceAccess": access,
            "resources": resources or [],
            **extra,
        }
        if prompt is not None:
            args["prompt"] = prompt
        return self.control.start(args)

    def session(self, run_id):
        return eventually(lambda: self.control.snapshot(run_id).get("threadId"))

    def finish(self, run_id, status="success"):
        session_id = self.session(run_id)
        self.protocol.emit(session_id, "turn.terminal", status=status)
        self.assertTrue(eventually(lambda: self.control.snapshot(run_id)["status"] in {"completed", "failed", "cancelled"}))

    def test_independent_worktrees_and_resources_run_concurrently(self):
        first = self.start("one", "a", resources=[{"key": "sim:a", "mode": "exclusive"}])
        second = self.start("two", "b", resources=[{"key": "sim:b", "mode": "exclusive"}])
        self.assertTrue(self.session(first["runId"]))
        self.assertTrue(self.session(second["runId"]))
        self.assertEqual("running", self.control.snapshot(first["runId"])["status"])
        self.assertEqual("running", self.control.snapshot(second["runId"])["status"])

    def test_same_worktree_or_extra_resource_serializes_writers(self):
        first = self.start("writer one", "same")
        second = self.start("writer two", "same")
        self.assertTrue(self.session(first["runId"]))
        self.assertEqual("queued", self.control.snapshot(second["runId"])["status"])
        self.finish(first["runId"])
        self.assertTrue(self.session(second["runId"]))

        third = self.start("sim one", "c", resources=[{"key": "simulator", "mode": "exclusive"}])
        fourth = self.start("sim two", "d", resources=[{"key": "simulator", "mode": "exclusive"}])
        self.assertTrue(self.session(third["runId"]))
        self.assertEqual("queued", self.control.snapshot(fourth["runId"])["status"])

    def test_shared_readers_overlap_and_writer_waits(self):
        first = self.start("read one", "repo", access="shared")
        second = self.start("read two", "repo", access="shared")
        writer = self.start("write", "repo", access="exclusive")
        self.assertTrue(self.session(first["runId"]))
        self.assertTrue(self.session(second["runId"]))
        self.assertEqual("queued", self.control.snapshot(writer["runId"])["status"])
        self.finish(first["runId"])
        self.assertEqual("queued", self.control.snapshot(writer["runId"])["status"])
        self.finish(second["runId"])
        self.assertTrue(self.session(writer["runId"]))

    def test_same_session_is_always_serial(self):
        self.protocol.sessions["sess_existing"] = {
            "sessionId": "sess_existing", "status": "idle", "mode": "build",
            "workspace": {"workspacePath": "/tmp/a", "workspaceKey": "/tmp/a"},
            "revision": 0, "eventSeq": 0,
        }
        first = self.control.start({"prompt": "one", "threadId": "sess_existing", "cwd": "/tmp/a", "workspaceAccess": "shared"})
        second = self.control.start({"prompt": "two", "threadId": "sess_existing", "cwd": "/tmp/a", "workspaceAccess": "shared"})
        self.assertTrue(eventually(lambda: self.control.snapshot(first["runId"])["status"] == "running"))
        self.assertEqual("queued", self.control.snapshot(second["runId"])["status"])
        self.assertNotIn("afterSeq", self.protocol.methods("session/subscribe")[0])
        self.finish(first["runId"])
        self.assertTrue(self.session(second["runId"]))

    def test_native_start_options_subscription_and_idempotency(self):
        run = self.start(
            "configured", "options", mode="plan", thoughtLevel="max",
            model={"providerId": "deepseek-1", "modelId": "deepseek-v4-flash"},
            toolAllowlist=["Read"], toolDenylist=["Bash"],
        )
        self.assertTrue(self.session(run["runId"]))
        created = self.protocol.methods("session/create")[0]
        self.assertEqual("max", created["thoughtLevel"])
        self.assertEqual(["Read"], created["toolAllowlist"])
        self.assertEqual(1, len(self.protocol.methods("session/subscribe")))
        sent = self.protocol.methods("session/send")[0]
        self.assertEqual(sent["inputId"], sent["queryId"])
        self.assertTrue(sent["inputId"].startswith("input_"))
        self.assertNotIn("apiKey", json.dumps(self.control.snapshot(run["runId"])))

    def test_managed_build_resolves_headless_permissions_and_plan_approval(self):
        run = self.start("implement", "permissions", mode="build")
        run_id = run["runId"]
        session_id = self.session(run_id)

        permission = self.protocol.on_server_request(
            "interaction/requestPermission",
            {
                "sessionId": session_id,
                "toolName": "Write",
                "riskLevel": "write",
            },
        )
        self.assertEqual("allow", permission["decision"])

        plan = self.protocol.on_server_request(
            "interaction/requestUserInput",
            {
                "sessionId": session_id,
                "toolName": "ExitPlanMode",
                "schema": {"interaction": "plan_approval"},
            },
        )
        self.assertEqual("accept", plan["action"])
        self.assertEqual("approve", plan["content"]["answer"])
        events = self.control.observe(run_id, refresh=False)["events"]
        kinds = {event["type"] for event in events}
        self.assertIn("interaction.permission-approved", kinds)
        self.assertIn("interaction.plan-approved", kinds)

    def test_plan_mode_and_arbitrary_questions_are_not_auto_approved(self):
        plan_run = self.start("plan only", "plan-permissions", mode="plan")
        plan_session = self.session(plan_run["runId"])
        permission = self.protocol.on_server_request(
            "interaction/requestPermission",
            {"sessionId": plan_session, "toolName": "Write"},
        )
        self.assertEqual("deny", permission["decision"])

        build_run = self.start("implement", "question", mode="build")
        build_session = self.session(build_run["runId"])
        answer = self.protocol.on_server_request(
            "interaction/requestUserInput",
            {
                "sessionId": build_session,
                "toolName": "AskUserQuestion",
                "schema": {"toolName": "AskUserQuestion"},
            },
        )
        self.assertEqual("decline", answer["action"])

    def test_start_requires_exactly_one_prompt_or_goal(self):
        with self.assertRaises(ControlPlaneError):
            self.control.start({"cwd": "/tmp/missing"})
        with self.assertRaises(ControlPlaneError):
            self.control.start({
                "cwd": "/tmp/conflict", "prompt": "one turn", "goal": "durable"
            })

    def test_plan_mode_rejects_durable_goal_before_session_creation(self):
        with self.assertRaisesRegex(ControlPlaneError, "do not execute in plan mode"):
            self.control.start({
                "cwd": "/tmp/plan-goal", "mode": "plan", "goal": "durable"
            })
        self.assertEqual([], self.protocol.methods("session/create"))

    def test_goal_starts_one_native_turn_without_session_send(self):
        run = self.start(None, "goal-only", goal="finish the durable objective")
        run_id = run["runId"]
        self.assertTrue(eventually(lambda: self.control.snapshot(run_id)["status"] == "running"))
        self.assertEqual(1, len(self.protocol.methods("session/goal")))
        self.assertEqual([], self.protocol.methods("session/send"))
        goal = self.protocol.methods("session/goal")[0]
        self.assertTrue(goal["inputId"].startswith("input_"))

    def test_goal_control_terminal_before_response_does_not_finish_run(self):
        self.protocol.goal_terminal_before_response = True
        run = self.start(None, "goal-race", goal="continue after control turn")
        run_id = run["runId"]
        self.assertTrue(eventually(lambda: self.control.snapshot(run_id)["status"] == "running"))
        self.assertNotEqual("completed", self.control.snapshot(run_id)["status"])
        self.assertEqual([], self.protocol.methods("session/send"))

    def test_goal_completion_verification_finishes_durable_run(self):
        run = self.start(None, "goal-verified", goal="finish the durable objective")
        run_id = run["runId"]
        session_id = self.session(run_id)
        self.protocol.emit(session_id, "turn.terminal", status="success")
        self.assertEqual("running", self.control.snapshot(run_id)["status"])
        self.protocol.emit_native(
            session_id,
            "session.updated",
            status="completed",
            targetId="target_1",
            verificationId="verify_1",
            verification={"passed": True, "reason": "objective satisfied"},
        )
        self.assertEqual("running", self.control.snapshot(run_id)["status"])
        target = dict(self.protocol.sessions[session_id]["target"])
        target["status"] = "complete"
        self.protocol.emit_native(
            session_id,
            "session.updated",
            action="status_updated",
            target=target,
        )
        self.assertTrue(eventually(lambda: self.control.snapshot(run_id)["status"] == "completed"))
        self.assertEqual("complete", self.control.snapshot(run_id)["goal"]["status"])

    def test_native_turn_started_before_goal_error_is_stopped_and_closed(self):
        self.protocol.goal_error_after_start = "response transport failed"
        run = self.start(None, "goal-cleanup", goal="must not leak")
        run_id = run["runId"]
        self.assertTrue(eventually(lambda: self.control.snapshot(run_id)["status"] == "failed"))
        self.assertEqual(1, len(self.protocol.methods("session/stop")))
        self.assertEqual(1, len(self.protocol.methods("session/close")))
        self.assertIn("response transport failed", self.control.snapshot(run_id)["error"])

    def test_wait_wakes_from_native_lifecycle_event(self):
        run = self.start("wait", "wait")
        run_id = run["runId"]
        session_id = self.session(run_id)
        revision = self.control.snapshot(run_id)["revision"]
        result = {}
        thread = threading.Thread(target=lambda: result.update(self.control.wait(run_id, after_revision=revision, timeout_ms=1000)))
        thread.start()
        time.sleep(0.05)
        self.protocol.emit(session_id, "tool.lifecycle", toolName="Read", toolCallId="t1", status="started")
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertTrue(result["changed"])

    def test_reasoning_usage_background_subagents_and_context_are_observable(self):
        self.protocol.background_jobs = [{
            "taskId": "bg_1", "taskKind": "bash", "status": "running",
            "cancellable": True, "command": "xcodebuild test", "outputTail": "Building",
        }]
        self.protocol.agents = {
            "revision": 2, "childSessionIds": ["sess_child"],
            "running": [{"agentId": "agent_1", "childSessionId": "sess_child", "status": "blocked", "title": "UI audit"}],
            "ended": {"total": 1, "items": [{"agentId": "agent_0", "status": "success", "summary": "done"}]},
        }
        run = self.start("observe", "observe")
        run_id = run["runId"]
        session_id = self.session(run_id)
        self.protocol.emit(session_id, "model.request.status", status="model_request_started", requestId="req_1")
        self.protocol.emit(session_id, "stream.chunk", channel="reasoning", firstChunk=True, chunkLength=40)
        state = self.control.observe(run_id, refresh=True)
        self.assertEqual(20, state["usage"]["reasoningTokens"])
        self.assertEqual("reasoning", state["model"]["lastChannel"])
        self.assertTrue(state["model"]["reasoningActive"])
        self.assertEqual("bg_1", state["backgroundTasks"][0]["taskId"])
        self.assertEqual("blocked", state["subagents"]["running"][0]["status"])
        self.assertEqual(0.25, state["context"]["usedRatio"])

    def test_resumed_run_usage_is_delta_while_session_usage_remains_cumulative(self):
        self.protocol.sessions["sess_usage"] = {
            "sessionId": "sess_usage", "status": "idle", "mode": "build",
            "workspace": {"workspacePath": "/tmp/usage", "workspaceKey": "/tmp/usage"},
            "revision": 1, "eventSeq": 2,
        }
        run = self.control.start({
            "prompt": "next turn", "threadId": "sess_usage", "cwd": "/tmp/usage"
        })
        run_id = run["runId"]
        self.assertTrue(eventually(lambda: self.control.snapshot(run_id)["status"] == "running"))
        self.protocol.usage.update({
            "totalTokens": 150, "inputTokens": 115, "outputTokens": 35,
            "reasoningTokens": 25, "cacheReadTokens": 90, "modelRequestCount": 3,
        })
        self.finish(run_id)
        state = self.control.snapshot(run_id)
        self.assertEqual(5, state["usage"]["reasoningTokens"])
        self.assertEqual(1, state["usage"]["modelRequests"])
        self.assertEqual(25, state["sessionUsage"]["reasoningTokens"])
        self.assertEqual(3, state["sessionUsage"]["modelRequests"])

    def test_control_routes_background_goal_guidance_and_cancel(self):
        run = self.start(None, "control", goal="finish tests")
        run_id = run["runId"]
        session_id = self.session(run_id)
        self.control.control(run_id, "cancel-background", task_id="bg_1")
        self.assertEqual("bg_1", self.protocol.methods("session/cancelBackgroundTask")[0]["taskId"])
        self.control.control(run_id, "pause-goal")
        self.control.control(run_id, "resume-goal")
        self.control.control(run_id, "guide", prompt="use the minimal failing test")
        self.protocol.emit(session_id, "turn.terminal", status="success")
        self.assertTrue(eventually(lambda: len(self.protocol.methods("session/send")) == 1))
        self.assertEqual("use the minimal failing test", self.protocol.methods("session/send")[0]["content"])
        self.control.control(run_id, "cancel")
        self.protocol.emit(session_id, "turn.terminal", status="interrupted")
        self.assertTrue(eventually(lambda: self.control.snapshot(run_id)["status"] == "cancelled"))

    def test_new_model_iteration_clears_completed_foreground_tools(self):
        run = self.start("iterations", "iterations")
        run_id = run["runId"]
        session_id = self.session(run_id)
        self.protocol.emit(
            session_id, "tool.lifecycle", toolName="Bash", toolCallId="old", status="started"
        )
        self.assertEqual(1, len(self.control.snapshot(run_id)["activeTools"]))
        self.protocol.emit(
            session_id, "model.request.status", status="model_request_started", requestId="next"
        )
        self.assertEqual([], self.control.snapshot(run_id)["activeTools"])

    def test_live_background_task_keeps_resource_lease_until_cancelled(self):
        self.protocol.background_jobs = [{
            "taskId": "bg_lock", "taskKind": "bash", "status": "running",
            "cancellable": True, "command": "xcodebuild test",
        }]
        first = self.start("background build", "background-lock")
        first_id = first["runId"]
        session_id = self.session(first_id)
        second = self.start("next writer", "background-lock")
        self.protocol.emit(session_id, "turn.terminal", status="success")
        self.assertTrue(eventually(lambda: self.control.snapshot(first_id)["status"] == "background"))
        self.assertEqual("queued", self.control.snapshot(second["runId"])["status"])
        controlled = self.control.control(first_id, "cancel-background", task_id="bg_lock")
        self.assertEqual("completed", controlled["status"])
        self.assertTrue(controlled["controlResult"]["cancelled"])
        self.assertTrue(eventually(lambda: self.control.snapshot(second["runId"])["status"] == "running"))

    def test_stale_terminal_projection_is_rechecked_once(self):
        self.protocol.background_jobs = [{
            "taskId": "foreground_stale", "taskKind": "bash", "status": "running"
        }]
        run = self.start("foreground command", "stale-projection")
        run_id = run["runId"]
        self.protocol.emit(self.session(run_id), "turn.terminal", status="success")
        self.assertTrue(eventually(lambda: self.control.snapshot(run_id)["status"] == "background"))
        self.protocol.background_jobs = []
        self.assertTrue(eventually(lambda: self.control.snapshot(run_id)["status"] == "completed", timeout=2))

    def test_branch_compact_and_close_use_native_operations(self):
        run = self.start("finish", "lifecycle")
        self.finish(run["runId"])
        branch = self.control.branch(run["runId"])
        self.assertEqual("sess_forked", branch["threadId"])
        context = self.control.context(run["runId"], action="compact", instructions="retain test evidence")
        self.assertEqual("accepted", context["operation"]["state"])
        self.assertEqual("compacting", context["run"]["status"])
        self.protocol.emit(self.session(run["runId"]), "compaction.terminal", status="completed")
        self.assertTrue(eventually(lambda: self.control.snapshot(run["runId"])["status"] == "completed"))
        closed = self.control.close_run(run["runId"])
        self.assertEqual("closed", closed["status"])
        self.assertTrue(closed["stop"]["stopped"])
        self.assertEqual(1, len(self.protocol.methods("session/stop")))

    def test_recover_lists_and_adopts_a_persisted_session(self):
        self.protocol.sessions["sess_recover"] = {
            "sessionId": "sess_recover", "status": "running", "mode": "build",
            "workspace": {"workspacePath": "/tmp/recover", "workspaceKey": "/tmp/recover"},
            "revision": 4, "eventSeq": 8,
        }
        listed = self.control.recover({"limit": 10})
        self.assertIn("sess_recover", {item["threadId"] for item in listed["sessions"]})
        adopted = self.control.recover({"adoptThreadId": "sess_recover", "limit": 10})
        run_id = adopted["runId"]
        self.assertTrue(eventually(lambda: self.control.snapshot(run_id)["status"] == "running"))
        subscribe = self.protocol.methods("session/subscribe")[-1]
        self.assertEqual("web-remote-replayable", subscribe["deliveryKind"])

    def test_transport_loss_resumes_and_replays_instead_of_failing(self):
        run = self.start("long", "transport")
        run_id = run["runId"]
        session_id = self.session(run_id)
        self.protocol.sessions[session_id]["status"] = "running"
        self.control._on_disconnect("transport gone")
        self.assertTrue(eventually(lambda: self.control.snapshot(run_id)["phase"] == "transport-recovered"))
        self.assertEqual("running", self.control.snapshot(run_id)["status"])
        self.assertGreaterEqual(len(self.protocol.methods("session/resume")), 1)
        self.assertIn("afterSeq", self.protocol.methods("session/subscribe")[-1])

    def test_observation_and_result_are_bounded(self):
        run = self.start("large", "large")
        run_id = run["runId"]
        session_id = self.session(run_id)
        for index in range(300):
            self.protocol.emit(session_id, "tool.lifecycle", toolName="Bash", toolCallId="t%s" % index, status="started")
        self.protocol.read_text = "r" * 50000
        self.finish(run_id)
        observed = self.control.observe(run_id, refresh=False, max_events=30, result_chars=12000)
        self.assertLessEqual(len(observed["events"]), 30)
        self.assertGreater(observed["eventsDropped"], 0)
        self.assertEqual(12000, len(observed["result"]))
        self.assertLess(len(json.dumps(observed)), 30000)

    def test_extracts_native_message_parts(self):
        snapshot = {"messages": [{"info": {"role": "assistant"}, "parts": [{"type": "reasoning", "text": "hidden"}, {"type": "text", "text": "native-ok"}]}]}
        self.assertEqual("native-ok", extract_last_assistant_text(snapshot))

    def test_invalid_midturn_and_resource_inputs_fail_explicitly(self):
        with self.assertRaises(ControlPlaneError):
            self.control.start({"prompt": "x", "resources": [{"key": "sim", "mode": "invalid"}]})
        with self.assertRaises(ControlPlaneError):
            self.control.control("missing", "guide", prompt="x")


if __name__ == "__main__":
    unittest.main()
