from __future__ import annotations

import os
import tempfile
import time
import unittest
from control_plane import ControlPlaneError

from opencode_backend import OpenCodeRuntime


def eventually(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    return predicate()


class FakeOpenCodeServer:
    def __init__(self):
        self.calls = []
        self.status_reads = 0

    def start(self):
        return "http://127.0.0.1:1"

    def request(self, method, path, *, cwd=None, body=None, timeout=30, raw=False):
        self.calls.append((method, path, cwd, body))
        if method == "POST" and path.startswith("/session?"):
            return {"id": "session-one", "model": {}}
        if method == "GET" and path.startswith("/session/session-one?"):
            return {"id": "session-one", "model": {}}
        if path.startswith("/session/status"):
            self.status_reads += 1
            status = "busy" if self.status_reads <= 2 else "idle"
            return {"session-one": {"type": status}}
        if path.startswith("/session/session-one/message") and method == "GET":
            return [{
                "info": {
                    "role": "assistant",
                    "providerID": "deepseek",
                    "modelID": "deepseek-v4-flash",
                    "tokens": {
                        "input": 2,
                        "output": 3,
                        "reasoning": 4,
                        "cache": {"read": 5, "write": 6},
                    },
                },
                "parts": [{"type": "text", "text": "OPEN_CODE_DONE"}],
            }]
        if path.startswith("/session/session-one/fork"):
            return {"id": "session-fork"}
        return None


class OpenCodeRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = FakeOpenCodeServer()
        self.backend = type(
            "Backend", (), {"server": self.server, "logger": lambda *_: None}
        )()
        self.events = []
        self.runtime = OpenCodeRuntime(
            self.backend,
            {
                "cwd": self.temp.name,
                "thoughtLevel": "max",
                "model": {
                    "providerId": "deepseek",
                    "modelId": "deepseek-v4-flash",
                },
                "workspaceAccess": "exclusive",
            },
            self.events.append,
            lambda message: self.events.append({"type": "disconnect", "message": message}),
        )
        self.runtime._sse_loop = lambda: None

    def tearDown(self):
        self.runtime.close()
        self.temp.cleanup()

    def test_async_prompt_waits_for_idle_and_preserves_reasoning_variant(self):
        state = self.runtime.start("do work")
        self.assertEqual("session-one", state["threadId"])
        settled = eventually(
            lambda: next((event for event in self.events if event["type"] == "settled"), None)
        )
        self.assertEqual("completed", settled["status"])
        self.assertEqual("OPEN_CODE_DONE", settled["result"])
        self.assertEqual(4, settled["usage"]["reasoningTokens"])
        prompt = next(call for call in self.server.calls if "prompt_async" in call[1])
        self.assertEqual("max", prompt[3]["variant"])
        self.assertEqual("deepseek-v4-flash", prompt[3]["model"]["modelID"])

    def test_guidance_queues_until_idle_and_permission_is_answered(self):
        self.runtime.start("first")
        self.runtime.guide("second", interrupt=False)
        self.assertTrue(eventually(lambda: len([
            call for call in self.server.calls if "prompt_async" in call[1]
        ]) == 2))
        self.runtime._reply_permission({
            "id": "permission-one",
            "permission": "edit",
            "metadata": {"filepath": os.path.join(self.temp.name, "app.py")},
        })
        permission = next(call for call in self.server.calls if "/permission/" in call[1])
        self.assertEqual("once", permission[3]["reply"])

    def test_shared_workspace_rejects_edit_permission(self):
        self.runtime.args["workspaceAccess"] = "shared"
        self.runtime.start(None)
        self.runtime._reply_permission({"id": "permission-two", "permission": "edit"})
        permission = next(call for call in self.server.calls if "/permission/" in call[1])
        self.assertEqual("reject", permission[3]["reply"])
        create = next(call for call in self.server.calls if call[0] == "POST" and "/session?" in call[1])
        rules = create[3]["permission"]
        self.assertIn(
            {"permission": "edit", "pattern": "*", "action": "deny"}, rules
        )
        self.assertIn(
            {"permission": "bash", "pattern": "*", "action": "deny"}, rules
        )

    def test_external_directory_requires_a_declared_absolute_resource(self):
        outside = os.path.join(os.path.dirname(self.temp.name), "outside", "*")
        self.runtime._reply_permission({
            "id": "permission-outside",
            "permission": "external_directory",
            "patterns": [outside],
            "metadata": {"parentDir": os.path.dirname(outside)},
        })
        self.assertEqual("reject", self.server.calls[-1][3]["reply"])

        allowed = os.path.join(self.temp.name, "generated")
        self.runtime.args["resources"] = [{"key": allowed, "mode": "exclusive"}]
        self.runtime._reply_permission({
            "id": "permission-allowed",
            "permission": "external_directory",
            "patterns": [allowed + "/*"],
            "metadata": {"parentDir": allowed},
        })
        self.assertEqual("once", self.server.calls[-1][3]["reply"])

    def test_external_directory_without_structured_paths_is_rejected(self):
        self.runtime._reply_permission({
            "id": "permission-empty",
            "permission": "external_directory",
        })
        self.assertEqual("reject", self.server.calls[-1][3]["reply"])

    def test_tool_denylist_is_persisted_on_session_without_replacing_base_policy(self):
        self.runtime.args["toolDenylist"] = ["bash", "write"]
        self.runtime.start("work")
        create = next(call for call in self.server.calls if call[0] == "POST" and "/session?" in call[1])
        rules = create[3]["permission"]
        self.assertIn(
            {"permission": "external_directory", "pattern": "*", "action": "ask"}, rules
        )
        self.assertIn(
            {"permission": "edit", "pattern": "*", "action": "deny"}, rules
        )
        prompt = next(call for call in self.server.calls if "prompt_async" in call[1])
        self.assertNotIn("tools", prompt[3])

    def test_resumed_session_receives_bridge_permission_rules_before_prompt(self):
        self.runtime.session_id = "session-one"
        self.runtime.args["workspaceAccess"] = "shared"
        self.runtime.start("continue")
        patch = next(call for call in self.server.calls if call[0] == "PATCH")
        prompt = next(call for call in self.server.calls if "prompt_async" in call[1])
        self.assertLess(self.server.calls.index(patch), self.server.calls.index(prompt))
        self.assertIn(
            {"permission": "edit", "pattern": "*", "action": "deny"},
            patch[3]["permission"],
        )

    def test_closed_tool_allowlist_is_rejected_instead_of_silently_ignored(self):
        self.runtime.args["toolAllowlist"] = ["read"]
        with self.assertRaises(ControlPlaneError) as caught:
            self.runtime.start("work")
        self.assertEqual("unsupported_capability", caught.exception.code)

    def test_edit_permission_respects_the_longest_matching_resource_mode(self):
        shared = os.path.join(self.temp.name, "shared")
        self.runtime.args["resources"] = [{"key": shared, "mode": "shared"}]
        self.runtime._reply_permission({
            "id": "edit-worktree",
            "permission": "edit",
            "metadata": {"filepath": os.path.join(self.temp.name, "app.py")},
        })
        self.assertEqual("once", self.server.calls[-1][3]["reply"])
        self.runtime._reply_permission({
            "id": "edit-shared",
            "permission": "edit",
            "metadata": {"filepath": os.path.join(shared, "generated.py")},
        })
        self.assertEqual("reject", self.server.calls[-1][3]["reply"])


if __name__ == "__main__":
    unittest.main()
