from __future__ import annotations

import os
import json
import sys
import threading
import time
import unittest
from unittest import mock

from pi_backend import PiRpcClient, PiRuntime


HERE = os.path.dirname(os.path.abspath(__file__))
FAKE_PI = os.path.join(HERE, "fixtures", "fake_pi_rpc.py")


class PiRpcTest(unittest.TestCase):
    def test_background_request_returns_before_delayed_response(self):
        client = PiRpcClient([sys.executable, FAKE_PI], cwd=HERE)
        completed = threading.Event()
        result = {}
        try:
            client.start()
            started = time.monotonic()
            client.request_background(
                "delay",
                {"seconds": 0.5},
                callback=lambda data, error: (
                    result.update({"data": data, "error": error}), completed.set()
                ),
            )
            self.assertLess(time.monotonic() - started, 0.25)
            self.assertTrue(completed.wait(2))
            self.assertIsNone(result["error"])
            self.assertTrue(result["data"]["delayed"])
        finally:
            client.close()

    def test_strict_lf_transport_preserves_unicode_separators_and_concurrency(self):
        client = PiRpcClient([sys.executable, FAKE_PI], cwd=HERE)
        try:
            client.start()
            output = {}

            def request(index):
                output[index] = client.request("echo", {"value": index})

            threads = [threading.Thread(target=request, args=(index,)) for index in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(set(range(12)), set(output))
            self.assertEqual("left\u2028right", output[5]["unicode"])
        finally:
            client.close()

    def test_extension_ui_dialogs_are_answered_headlessly(self):
        events = []
        sent = []
        client = PiRpcClient([sys.executable, FAKE_PI], cwd=HERE, on_event=events.append)
        client._send = sent.append
        client._handle_line(b'{"type":"extension_ui_request","id":"one","method":"confirm"}')
        client._handle_line(b'{"type":"extension_ui_request","id":"two","method":"input"}')
        self.assertEqual(False, sent[0]["confirmed"])
        self.assertTrue(sent[1]["cancelled"])
        self.assertEqual("interaction.declined", events[0]["type"])

    def test_pi_events_normalize_reasoning_tools_and_usage(self):
        emitted = []
        backend = type("Backend", (), {"binary": "pi", "session_dir": "/tmp", "logger": lambda *_: None})()
        runtime = PiRuntime(backend, {}, emitted.append, lambda _message: None)
        runtime._on_event({"type": "agent_start"})
        runtime._on_event({
            "type": "message_update",
            "assistantMessageEvent": {"type": "thinking_start"},
        })
        runtime._on_event({"type": "tool_execution_start", "toolCallId": "t", "toolName": "bash"})
        runtime._on_event({"type": "tool_execution_end", "toolCallId": "t", "toolName": "bash"})
        runtime._on_event({
            "type": "message_end",
            "message": {
                "role": "assistant",
                "provider": "deepseek",
                "model": "deepseek-v4-flash",
                "usage": {"input": 2, "output": 3, "reasoning": 4, "totalTokens": 9},
            },
        })
        self.assertEqual(
            ["model.started", "reasoning.started", "tool.started", "tool.ended", "message.completed"],
            [event["type"] for event in emitted],
        )
        self.assertEqual(4, emitted[-1]["usage"]["reasoningTokens"])

    def test_runtime_forces_bridge_policy_extension_and_per_run_environment(self):
        captured = {}

        class FakeClient:
            def __init__(self, command, **kwargs):
                captured["command"] = command
                captured.update(kwargs)

            def start(self):
                return None

            def request(self, command, params=None, timeout=30):
                if command == "get_state":
                    return {"sessionId": "session-one", "model": {}}
                return {}

            def close(self):
                return None

        backend = type(
            "Backend",
            (),
            {
                "binary": "/fake/pi",
                "session_dir": "/tmp/pi-sessions",
                "policy_extension": "/bridge/pi-policy.mjs",
                "logger": lambda *_args: None,
            },
        )()
        runtime = PiRuntime(
            backend,
            {
                "cwd": "/tmp/worktree",
                "workspaceAccess": "shared",
                "mode": "plan",
                "thoughtLevel": "max",
                "toolAllowlist": ["read", "grep"],
                "toolDenylist": ["bash"],
                "resources": [{"key": "/tmp/output", "mode": "exclusive"}],
            },
            lambda _event: None,
            lambda _message: None,
        )
        with mock.patch("pi_backend.PiRpcClient", FakeClient):
            runtime.start(None)

        self.assertIn("--extension", captured["command"])
        self.assertIn("/bridge/pi-policy.mjs", captured["command"])
        self.assertEqual(
            "xhigh", captured["command"][captured["command"].index("--thinking") + 1]
        )
        self.assertEqual(
            ["read", "grep"],
            captured["command"][captured["command"].index("--tools") + 1].split(","),
        )
        self.assertEqual(
            "bash", captured["command"][captured["command"].index("--exclude-tools") + 1]
        )
        self.assertEqual("shared", captured["env"]["AGENT_BRIDGE_WORKSPACE_ACCESS"])
        self.assertEqual("plan", captured["env"]["AGENT_BRIDGE_MODE"])
        self.assertEqual(
            [os.path.realpath("/tmp/worktree"), os.path.realpath("/tmp/output")],
            json.loads(captured["env"]["AGENT_BRIDGE_ALLOWED_ROOTS"]),
        )
        self.assertEqual(
            {
                os.path.realpath("/tmp/worktree"): "shared",
                os.path.realpath("/tmp/output"): "exclusive",
            },
            json.loads(captured["env"]["AGENT_BRIDGE_ROOT_MODES"]),
        )

    def test_branch_uses_a_temporary_process_and_keeps_parent_runtime_bound(self):
        class BranchClient:
            closed = False

            def __init__(self, command, **_kwargs):
                self.command = command

            def start(self):
                return None

            def request(self, command, params=None, timeout=30):
                if command == "get_state":
                    return {"sessionFile": "/sessions/child.jsonl"}
                if command == "clone":
                    return {"cancelled": False}
                return {}

            def close(self):
                self.closed = True

        backend = type(
            "Backend",
            (),
            {
                "binary": "/fake/pi",
                "session_dir": "/tmp/pi-sessions",
                "policy_extension": "/bridge/pi-policy.mjs",
                "logger": lambda *_args: None,
            },
        )()
        runtime = PiRuntime(backend, {"cwd": "/tmp"}, lambda _event: None, lambda _message: None)
        parent_client = object()
        runtime.client = parent_client
        runtime.state = {"sessionFile": "/sessions/parent.jsonl"}
        with mock.patch("pi_backend.PiRpcClient", BranchClient):
            result = runtime.branch(target_kind="latestCheckpoint", target_id=None, turn_index=None)
        self.assertIs(parent_client, runtime.client)
        self.assertEqual("/sessions/parent.jsonl", result["parentThreadId"])
        self.assertEqual("/sessions/child.jsonl", result["threadId"])

    def test_interrupt_returns_immediately_and_resumes_from_settled_event(self):
        emitted = []
        prompt_sent = threading.Event()

        class InterruptClient:
            def __init__(self):
                self.abort_callback = None
                self.prompts = []

            def request_background(self, command, params=None, timeout=30, callback=None):
                self.abort_callback = callback
                self.abort_timeout = timeout
                return "abort-one"

            def request(self, command, params=None, timeout=30):
                if command == "prompt":
                    self.prompts.append(params["message"])
                    prompt_sent.set()
                return {}

        backend = type("Backend", (), {"logger": lambda *_args: None})()
        runtime = PiRuntime(backend, {"timeout": 900}, emitted.append, lambda _message: None)
        client = InterruptClient()
        runtime.client = client

        started = time.monotonic()
        runtime.guide("write the final summary", interrupt=True)
        self.assertLess(time.monotonic() - started, 0.15)
        self.assertIsNotNone(client.abort_callback)

        # Pi emits agent_settled before its abort RPC response. The first
        # settled event must continue the interrupted run, not terminate it.
        runtime._on_event({"type": "agent_settled"})
        self.assertTrue(prompt_sent.wait(1))
        self.assertEqual(["write the final summary"], client.prompts)
        self.assertFalse(any(event.get("type") == "settled" for event in emitted))

        # A later abort response is harmless and cannot submit the prompt twice.
        client.abort_callback({}, None)
        time.sleep(0.05)
        self.assertEqual(["write the final summary"], client.prompts)

    def test_cancel_is_async_and_aborted_message_becomes_cancelled(self):
        emitted = []
        settled = threading.Event()

        class CancelClient:
            background_requests = 0

            def request_background(self, command, params=None, timeout=30, callback=None):
                self.background_requests += 1
                self.abort_callback = callback
                return "abort-cancel"

            def request(self, command, params=None, timeout=30):
                if command == "get_last_assistant_text":
                    return {"text": "partial summary"}
                if command == "get_session_stats":
                    return {}
                if command == "get_state":
                    return {"sessionId": "session-one", "model": {}}
                if command == "get_messages":
                    return {"messages": [{"role": "assistant", "stopReason": "aborted"}]}
                return {}

        def capture(event):
            emitted.append(event)
            if event.get("type") == "settled":
                settled.set()

        backend = type("Backend", (), {"logger": lambda *_args: None})()
        runtime = PiRuntime(backend, {"timeout": 900}, capture, lambda _message: None)
        client = CancelClient()
        runtime.client = client

        started = time.monotonic()
        runtime.cancel()
        runtime.cancel()
        self.assertLess(time.monotonic() - started, 0.15)
        self.assertEqual(1, client.background_requests)
        runtime._on_event({"type": "agent_settled"})
        self.assertTrue(settled.wait(1))
        self.assertEqual("cancelled", emitted[-1]["status"])
        client.abort_callback({}, None)
        time.sleep(0.05)
        self.assertEqual(1, sum(event.get("type") == "settled" for event in emitted))


if __name__ == "__main__":
    unittest.main()
