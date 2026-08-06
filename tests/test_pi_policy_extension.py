from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTENSION = os.path.join(ROOT, "pi_bridge_extension.mjs")


class PiPolicyExtensionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = os.environ.get("NODE_BINARY") or shutil.which("node")
        if not cls.node:
            raise unittest.SkipTest("Node.js is required to execute the Pi policy extension")

    def decisions(self):
        script = """
import { policyDecision } from %s;
const policy = {cwd: '/workspace', roots: ['/workspace', '/output'], workspaceAccess: 'shared'};
const events = [
  {toolName: 'read', input: {path: 'src/app.ts'}},
  {toolName: 'read', input: {path: '/etc/passwd'}},
  {toolName: 'write', input: {path: '/workspace/new.txt'}},
  {toolName: 'bash', input: {command: 'pwd'}},
  {toolName: 'ls', input: {path: '/output'}},
];
console.log(JSON.stringify(events.map((event) => policyDecision(event, policy) || null)));
""" % json.dumps("file://" + EXTENSION)
        result = subprocess.run(
            [self.node, "--input-type=module", "--eval", script],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        return json.loads(result.stdout)

    def test_shared_policy_blocks_writes_shell_and_outside_reads(self):
        decisions = self.decisions()
        self.assertIsNone(decisions[0])
        self.assertTrue(decisions[1]["block"])
        self.assertTrue(decisions[2]["block"])
        self.assertTrue(decisions[3]["block"])
        self.assertIsNone(decisions[4])

    def test_nested_shared_root_blocks_write_under_exclusive_worktree(self):
        script = """
import { policyDecision } from %s;
const policy = {
  cwd: '/workspace',
  roots: ['/workspace', '/workspace/shared'],
  rootModes: {'/workspace': 'exclusive', '/workspace/shared': 'shared'},
  workspaceAccess: 'exclusive',
};
console.log(JSON.stringify([
  policyDecision({toolName: 'write', input: {path: '/workspace/new.txt'}}, policy) || null,
  policyDecision({toolName: 'write', input: {path: '/workspace/shared/new.txt'}}, policy) || null,
]));
""" % json.dumps("file://" + EXTENSION)
        result = subprocess.run(
            [self.node, "--input-type=module", "--eval", script],
            capture_output=True, text=True, timeout=15, check=True,
        )
        decisions = json.loads(result.stdout)
        self.assertIsNone(decisions[0])
        self.assertTrue(decisions[1]["block"])

    def test_plan_mode_is_read_only_even_with_exclusive_workspace(self):
        script = """
import { policyDecision } from %s;
const policy = {cwd: '/workspace', roots: ['/workspace'], workspaceAccess: 'exclusive', mode: 'plan'};
console.log(JSON.stringify([
  policyDecision({toolName: 'read', input: {path: 'src/app.ts'}}, policy) || null,
  policyDecision({toolName: 'edit', input: {path: 'src/app.ts'}}, policy) || null,
]));
""" % json.dumps("file://" + EXTENSION)
        result = subprocess.run(
            [self.node, "--input-type=module", "--eval", script],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        decisions = json.loads(result.stdout)
        self.assertIsNone(decisions[0])
        self.assertTrue(decisions[1]["block"])


if __name__ == "__main__":
    unittest.main()
