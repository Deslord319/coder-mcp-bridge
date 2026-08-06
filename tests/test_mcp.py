#!/usr/bin/env python3
"""Live MCP integration and real ZCode concurrency test.

This test starts the bridge over stdio, launches two independent ZCode sessions,
and verifies that both native Bash sleep operations are active at the same time.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time


HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "server.py")
EXPECTED_TOOLS = [
    "agent-config", "agent-start", "agent-wait", "agent-observe", "agent-control",
    "agent-recover", "agent-branch", "agent-context", "agent-close",
]


class McpClient:
    def __init__(self):
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        self.proc = subprocess.Popen(
            [sys.executable, SERVER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1, env=env,
        )
        self._next_id = 0
        self._write_lock = threading.Lock()
        self._lock = threading.Lock()
        self._pending = {}
        threading.Thread(target=self._read_loop, daemon=True).start()

    def _read_loop(self):
        for line in self.proc.stdout:
            try:
                message = json.loads(line)
            except (TypeError, ValueError):
                continue
            request_id = message.get("id")
            with self._lock:
                entry = self._pending.get(request_id)
                if entry:
                    entry["message"] = message
                    entry["event"].set()

    def request(self, method, params=None, timeout=120):
        with self._write_lock:
            self._next_id += 1
            request_id = self._next_id
            event = threading.Event()
            with self._lock:
                self._pending[request_id] = {"event": event, "message": None}
            payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
            if params is not None:
                payload["params"] = params
            self.proc.stdin.write(json.dumps(payload) + "\n")
            self.proc.stdin.flush()
        if not event.wait(timeout):
            raise TimeoutError("MCP request %s timed out" % method)
        with self._lock:
            entry = self._pending.pop(request_id)
        return entry["message"]

    def tool(self, name, arguments, timeout=120):
        message = self.request("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)
        result = message.get("result") or {}
        if result.get("isError"):
            text = (result.get("content") or [{}])[0].get("text")
            raise RuntimeError("%s failed: %s" % (name, text))
        return result.get("structuredContent") or json.loads(result["content"][0]["text"])

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()


def require(condition, message, detail=None):
    if not condition:
        raise AssertionError("%s%s" % (message, ": %r" % detail if detail is not None else ""))
    print("PASS", message)


def wait_terminal(client, run_id, initial, output):
    state = initial
    while state["status"] not in {"completed", "failed", "cancelled", "timed_out", "closed"}:
        state = client.tool("agent-wait", {
            "runId": run_id,
            "afterRevision": state["revision"],
            "timeoutMs": 10000,
            "resultChars": 12000,
        }, timeout=30)
    output.update(state)


def main():
    client = McpClient()
    try:
        initialized = client.request("initialize", {
            "protocolVersion": "2025-03-26", "capabilities": {},
            "clientInfo": {"name": "zcode-live-test", "version": "1"},
        }, timeout=30)
        require(initialized.get("result", {}).get("serverInfo", {}).get("version") == "0.5.0-dev", "server version is 0.5.0-dev")
        listed = client.request("tools/list", {}, timeout=30)
        names = [item["name"] for item in listed["result"]["tools"]]
        require(names == EXPECTED_TOOLS, "only the new aggregated tools are exposed", names)

        with tempfile.TemporaryDirectory(prefix="zcode-live-permission-") as permission_cwd:
            permission_run = client.tool("agent-start", {
                "prompt": (
                    "Use the Write tool to create permission-proof.txt in the current "
                    "working directory with exactly HEADLESS_BUILD_OK followed by a newline. "
                    "Then reply exactly HEADLESS_BUILD_DONE."
                ),
                "cwd": permission_cwd,
                "mode": "build",
                "thoughtLevel": "high",
                "timeout": 180,
            }, timeout=30)
            permission_final = {}
            wait_terminal(client, permission_run["runId"], permission_run, permission_final)
            require(
                permission_final.get("status") == "completed",
                "managed build run completed after headless permission approval",
                permission_final,
            )
            proof_path = os.path.join(permission_cwd, "permission-proof.txt")
            with open(proof_path, encoding="utf-8") as handle:
                proof = handle.read()
            require(proof == "HEADLESS_BUILD_OK\n", "build-mode Write reached the declared worktree", proof)
            require(
                "HEADLESS_BUILD_DONE" in permission_final.get("result", ""),
                "build-mode result is preserved",
                permission_final,
            )
            client.tool("agent-close", {"runId": permission_run["runId"]}, timeout=30)

        with tempfile.TemporaryDirectory(prefix="zcode-live-goal-") as goal_cwd:
            goal = client.tool("agent-start", {
                "goal": "Reply exactly GOAL_ONLY_DONE and use no tools. The objective is complete after that reply.",
                "cwd": goal_cwd,
                "mode": "build",
                "thoughtLevel": "high",
                "timeout": 120,
            }, timeout=30)
            goal_final = {}
            wait_terminal(client, goal["runId"], goal, goal_final)
            require(goal_final.get("status") == "completed", "goal-only run completed", goal_final)
            require(
                "GOAL_ONLY_DONE" in goal_final.get("result", ""),
                "goal-only result is preserved",
                goal_final,
            )
            require("prompt is already running" not in str(goal_final).lower(), "goal-only run has no double-start race")
            client.tool("agent-close", {"runId": goal["runId"]}, timeout=30)

        with tempfile.TemporaryDirectory(prefix="zcode-live-guidance-") as guidance_cwd:
            guided_run = client.tool("agent-start", {
                "prompt": (
                    "Use the Bash tool to run exactly `sleep 2`. Wait for it, then "
                    "reply exactly GUIDANCE_FIRST."
                ),
                "cwd": guidance_cwd,
                "mode": "yolo",
                "thoughtLevel": "high",
                "timeout": 120,
            }, timeout=30)
            guided = client.tool("agent-control", {
                "runId": guided_run["runId"],
                "action": "guide",
                "prompt": "After the current turn, reply exactly GUIDANCE_SECOND and use no tools.",
            }, timeout=30)
            guided_final = {}
            wait_terminal(client, guided_run["runId"], guided, guided_final)
            require(guided_final.get("status") == "completed", "queued guidance completed", guided_final)
            require(
                "GUIDANCE_SECOND" in guided_final.get("result", ""),
                "queued guidance result is preserved",
                guided_final,
            )
            require(
                not guided_final.get("controlFailures"),
                "native-ready guidance has no control failure",
                guided_final,
            )
            client.tool("agent-close", {"runId": guided_run["runId"]}, timeout=30)

        with tempfile.TemporaryDirectory(prefix="zcode-live-a-") as first_cwd, tempfile.TemporaryDirectory(prefix="zcode-live-b-") as second_cwd:
            prompt_a = "Use the Bash tool to run exactly `sleep 6`. Wait for it to finish, then reply exactly PARALLEL_A_DONE."
            prompt_b = "Use the Bash tool to run exactly `sleep 6`. Wait for it to finish, then reply exactly PARALLEL_B_DONE."
            first = client.tool("agent-start", {
                "prompt": prompt_a, "cwd": first_cwd, "mode": "yolo", "thoughtLevel": "max"
            }, timeout=30)
            second = client.tool("agent-start", {
                "prompt": prompt_b, "cwd": second_cwd, "mode": "yolo", "thoughtLevel": "max"
            }, timeout=30)
            require(first["runId"] != second["runId"], "independent runs receive distinct IDs")

            first_final, second_final = {}, {}
            threads = [
                threading.Thread(target=wait_terminal, args=(client, first["runId"], first, first_final)),
                threading.Thread(target=wait_terminal, args=(client, second["runId"], second, second_final)),
            ]
            for thread in threads:
                thread.start()

            both_running = False
            both_bash = False
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline and any(thread.is_alive() for thread in threads):
                one = client.tool("agent-observe", {"runId": first["runId"], "refresh": False, "resultChars": 0}, timeout=30)
                two = client.tool("agent-observe", {"runId": second["runId"], "refresh": False, "resultChars": 0}, timeout=30)
                if one["status"] == "running" and two["status"] == "running":
                    both_running = True
                one_tools = {item.get("name") for item in one.get("activeTools", [])}
                two_tools = {item.get("name") for item in two.get("activeTools", [])}
                if "Bash" in one_tools and "Bash" in two_tools:
                    both_bash = True
                if both_bash:
                    break
                time.sleep(0.1)

            for thread in threads:
                thread.join(150)
            require(not any(thread.is_alive() for thread in threads), "both live runs terminate")
            require(both_running, "both native sessions are running concurrently")
            require(both_bash, "both six-second Bash operations overlap")
            require(first_final.get("status") == "completed", "first run completed", first_final)
            require(second_final.get("status") == "completed", "second run completed", second_final)
            require("PARALLEL_A_DONE" in first_final.get("result", ""), "first result is preserved")
            require("PARALLEL_B_DONE" in second_final.get("result", ""), "second result is preserved")
            require(first_final["usage"]["reasoningTokens"] >= 0, "reasoning usage is present")
            require(second_final["usage"]["modelRequests"] >= 1, "model request usage is present")
            require(first_final["model"].get("thoughtLevel") == "max", "configured max reasoning is preserved")

            client.tool("agent-close", {"runId": first["runId"]}, timeout=30)
            client.tool("agent-close", {"runId": second["runId"]}, timeout=30)
            print("LIVE_CONCURRENCY_OK")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("FAIL", type(exc).__name__, str(exc), file=sys.stderr)
        raise
