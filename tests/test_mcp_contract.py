from __future__ import annotations

import unittest

import server


class ToolCatalogServer(server.ZCodeMcpServer):
    def __init__(self):
        super().__init__("/fake/zcode", "/fake/zcode.cjs")
        self.result = None

    def send_response(self, request_id, result):
        self.result = result


def tool_catalog():
    instance = ToolCatalogServer()
    instance.handle_tools_list(1)
    return instance.result["tools"]


class McpContractTest(unittest.TestCase):
    def test_codex_owns_global_concurrency_by_default(self):
        self.assertEqual(0, server.MAX_CONCURRENCY)

    def test_only_new_aggregated_tools_are_exposed(self):
        names = [tool["name"] for tool in tool_catalog()]
        self.assertEqual([
            "zcode-start", "zcode-wait", "zcode-observe", "zcode-control",
            "zcode-recover", "zcode-branch", "zcode-context", "zcode-close",
        ], names)
        self.assertNotIn("zcode", names)
        self.assertNotIn("zcode-reply", names)
        self.assertNotIn("zcode-query", names)
        self.assertNotIn("zcode-guide", names)
        self.assertNotIn("zcode-cancel", names)

    def test_schemas_expose_native_scheduling_capabilities(self):
        tools = {tool["name"]: tool for tool in tool_catalog()}
        start = tools["zcode-start"]["inputSchema"]
        self.assertEqual(["prompt"], start["oneOf"][0]["required"])
        self.assertEqual(["goal"], start["oneOf"][1]["required"])
        goal_modes = start["allOf"][0]["then"]["properties"]["mode"]["enum"]
        self.assertNotIn("plan", goal_modes)
        self.assertFalse(start["additionalProperties"])
        for name in ("thoughtLevel", "model", "toolAllowlist", "toolDenylist", "workspaceAccess", "resources", "goal"):
            self.assertIn(name, start["properties"])
        self.assertIn("headless", start["properties"]["mode"]["description"])
        self.assertIn("Cross-Bridge", start["properties"]["workspaceAccess"]["description"])
        actions = tools["zcode-control"]["inputSchema"]["properties"]["action"]["enum"]
        self.assertEqual(
            {"guide", "interrupt", "cancel", "cancel-background", "pause-goal", "resume-goal"},
            set(actions),
        )
        controls = tools["zcode-control"]["inputSchema"]["properties"]
        self.assertIn("ifRevision", controls)
        self.assertIn("ifStatus", controls)
        targets = tools["zcode-branch"]["inputSchema"]["properties"]["targetKind"]["enum"]
        self.assertEqual({"latestCheckpoint", "checkpoint", "message", "turn"}, set(targets))

    def test_descriptions_assign_concurrency_and_observation_responsibility(self):
        tools = {tool["name"]: tool for tool in tool_catalog()}
        descriptions = " ".join(tool["description"] for tool in tools.values())
        self.assertLess(len(descriptions), 2200)
        self.assertIn("Codex owns global concurrency", descriptions)
        self.assertIn("run concurrently", descriptions)
        self.assertIn("across Bridge processes", descriptions)
        self.assertIn("zcode-wait instead of polling or sleeping", descriptions)
        self.assertIn("Native subscriptions and replay", descriptions)
        self.assertIn("model/reasoning activity", descriptions)
        self.assertIn("background task", descriptions)
        self.assertIn("bridge restart", descriptions)
        self.assertIn("checkpoint", descriptions)
        self.assertIn("compact", descriptions)

    def test_bounded_observation_and_wait_limits_are_schema_enforced(self):
        tools = {tool["name"]: tool for tool in tool_catalog()}
        observe = tools["zcode-observe"]["inputSchema"]["properties"]
        wait = tools["zcode-wait"]["inputSchema"]["properties"]
        self.assertEqual(30, observe["maxEvents"]["maximum"])
        self.assertEqual(12000, observe["resultChars"]["maximum"])
        self.assertEqual(60000, wait["timeoutMs"]["maximum"])


if __name__ == "__main__":
    unittest.main()
