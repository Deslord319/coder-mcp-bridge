#!/usr/bin/env python3
"""Integration tests for zcode-mcp-plugin/server.py.

Spawns the MCP server over stdio and exercises the full protocol:
handshake, tool listing, new sessions, session resume, complex tasks,
error handling, concurrency, and a stability loop.

Run:
    python3 tests/test_mcp.py            # full suite
    python3 tests/test_mcp.py --fast     # skip complex + stability loops
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "server.py")

PASS = 0
FAIL = 0
FAILURES = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL += 1
        FAILURES.append((name, detail))
        print("  FAIL  %s  %s" % (name, ("(" + str(detail) + ")") if detail else ""))


class McpClient:
    """Minimal newline-delimited JSON-RPC client for testing the server."""

    def __init__(self, extra_env=None):
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        if extra_env:
            env.update(extra_env)
        self.proc = subprocess.Popen(
            [sys.executable, SERVER],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        self._next_id = 0
        self._lock = threading.Lock()
        self._cv = threading.Condition()
        self._pending = {}   # id -> threading.Event
        self._results = {}   # id -> response dict
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" not in msg:
                continue
            with self._cv:
                ev = self._pending.get(msg["id"])
                if ev is not None:
                    self._results[msg["id"]] = msg
                    ev.set()
                    self._cv.notify_all()

    def request(self, method, params=None, timeout=600):
        with self._lock:
            self._next_id += 1
            rid = self._next_id
        payload = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        ev = threading.Event()
        with self._cv:
            self._pending[rid] = ev
        with self._lock:
            self.proc.stdin.write(json.dumps(payload) + "\n")
            self.proc.stdin.flush()
        if not ev.wait(timeout):
            with self._cv:
                self._pending.pop(rid, None)
            raise TimeoutError("no response for request %s within %ss" % (rid, timeout))
        with self._cv:
            self._pending.pop(rid, None)
            return self._results.pop(rid)

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def section(title):
    print("\n== %s ==" % title)


def test_handshake(c):
    section("protocol handshake")
    r = c.request("initialize", {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "zcode-test", "version": "1.0"},
    }, timeout=30)
    res = r.get("result")
    check("initialize returns serverInfo",
          res and res.get("serverInfo", {}).get("name") == "zcode-mcp-server", r)
    check("initialize returns protocol version", res and "protocolVersion" in res, r)
    c.request("notifications/initialized")

    r = c.request("ping", {}, timeout=30)
    check("ping ok", r.get("result") == {}, r)

    r = c.request("tools/list", {}, timeout=30)
    tools = (r.get("result") or {}).get("tools") or []
    names = [t["name"] for t in tools]
    check("tools/list has zcode", "zcode" in names, names)
    check("tools/list has zcode-reply", "zcode-reply" in names, names)
    zcode_schema = next((t for t in tools if t["name"] == "zcode"), {})
    check("zcode inputSchema requires prompt",
          "prompt" in zcode_schema.get("inputSchema", {}).get("required", []), tools)
    check("zcode inputSchema has no model override",
          "model" not in zcode_schema.get("inputSchema", {}).get("properties", {}), tools)
    reply_schema = next((t for t in tools if t["name"] == "zcode-reply"), {})
    check("zcode-reply inputSchema has no model override",
          "model" not in reply_schema.get("inputSchema", {}).get("properties", {}), tools)
    return tools


def test_new_session(c):
    section("zcode tool: new session")
    r = c.request("tools/call", {
        "name": "zcode",
        "arguments": {"prompt": "Reply with exactly: MCP_ZCODE_OK"},
    }, timeout=300)
    res = r.get("result") or {}
    content = res.get("content") or []
    text = content[0]["text"] if content else ""
    check("isError false", res.get("isError") is False, res)
    check("reply contains MCP_ZCODE_OK", "MCP_ZCODE_OK" in text, text[:300])
    thread_id = _extract_thread_id(text)
    check("metadata has threadId", bool(thread_id), text[:500])
    return thread_id


def _extract_thread_id(text):
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('"threadId"') or stripped.startswith("threadId"):
            val = stripped.split(":", 1)[-1].strip().strip('",')
            if val.startswith("sess_"):
                return val
    return None


def test_reply(c, thread_id):
    section("zcode-reply tool: resume session")
    if not thread_id:
        check("zcode-reply skipped (no threadId)", False, "no threadId from previous test")
        return
    r = c.request("tools/call", {
        "name": "zcode-reply",
        "arguments": {"threadId": thread_id, "prompt": "Now reply with exactly: MCP_ZCODE_REPLY_OK"},
    }, timeout=300)
    res = r.get("result") or {}
    content = res.get("content") or []
    text = content[0]["text"] if content else ""
    check("reply isError false", res.get("isError") is False, res)
    check("reply continues same session", "MCP_ZCODE_REPLY_OK" in text, text[:300])
    check("reply threadId matches", _extract_thread_id(text) == thread_id, text[:300])


def test_complex_task(c):
    section("zcode tool: complex task with tool use")
    workdir = "/tmp/zcode-mcp-plugin-work"
    os.makedirs(workdir, exist_ok=True)
    marker = os.path.join(workdir, "mcp-result.txt")
    if os.path.exists(marker):
        os.remove(marker)
    r = c.request("tools/call", {
        "name": "zcode",
        "arguments": {
            "prompt": (
                "In %s, create a file named mcp-result.txt containing the text "
                "'complex-ok'. Then run `ls -la %s` and report the file size. "
                "Finish your reply with the token DONE_COMPLEX." % (workdir, workdir)
            ),
            "cwd": workdir,
            "mode": "yolo",
            "maxTurns": 10,
        },
    }, timeout=600)
    res = r.get("result") or {}
    content = res.get("content") or []
    text = content[0]["text"] if content else ""
    check("complex isError false", res.get("isError") is False, res)
    exists = os.path.exists(marker)
    file_ok = exists and open(marker).read().strip() == "complex-ok"
    check("file was created with content", file_ok,
          "marker=%s exists=%s" % (marker, exists))
    check("task completed token", "DONE_COMPLEX" in text, text[:400])


def test_error_handling(c):
    section("zcode tool: error handling")
    r = c.request("tools/call", {"name": "zcode", "arguments": {}}, timeout=60)
    res = r.get("result") or {}
    check("missing prompt -> isError true", res.get("isError") is True, res)
    text = (res.get("content") or [{"text": ""}])[0].get("text", "")
    check("error message mentions prompt", "prompt" in text, text)

    r = c.request("tools/call", {
        "name": "zcode-reply",
        "arguments": {"prompt": "hi"},
    }, timeout=60)
    res = r.get("result") or {}
    check("reply without threadId -> isError true", res.get("isError") is True, res)

    r = c.request("tools/call", {"name": "does-not-exist", "arguments": {}}, timeout=30)
    check("unknown tool -> protocol error", "error" in r, r)


def test_concurrency(c):
    section("concurrency: two parallel zcode sessions")
    results = [None, None]

    def run(i, tag):
        try:
            r = c.request("tools/call", {
                "name": "zcode",
                "arguments": {"prompt": "Reply with exactly: PARALLEL_%s_DONE" % tag},
            }, timeout=300)
            res = r.get("result") or {}
            content = res.get("content") or []
            results[i] = (res.get("isError"), content[0]["text"] if content else "")
        except Exception as e:  # noqa: BLE001
            results[i] = ("exception", str(e))

    threads = [threading.Thread(target=run, args=(0, "ONE")),
               threading.Thread(target=run, args=(1, "TWO"))]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - t0
    check("both parallel calls isError false",
          all(r is not None and r[0] is False for r in results), results)
    check("both replies correct",
          all(r[1].find("PARALLEL_") >= 0 and r[1].find("_DONE") >= 0 for r in results if r), results)
    print("      elapsed: %.1fs" % elapsed)


def test_stability(c, n=4):
    section("stability: %d sequential zcode calls" % n)
    ok = 0
    times = []
    for i in range(n):
        t0 = time.time()
        try:
            r = c.request("tools/call", {
                "name": "zcode",
                "arguments": {"prompt": "Reply with exactly: STABLE_%d" % i},
            }, timeout=300)
            res = r.get("result") or {}
            content = res.get("content") or []
            if res.get("isError") is False and ("STABLE_%d" % i) in (content[0]["text"] if content else ""):
                ok += 1
        except Exception as e:  # noqa: BLE001
            print("      call %d exception: %s" % (i, e))
        times.append(time.time() - t0)
    check("all %d stability calls passed" % n, ok == n, "ok=%d/%d" % (ok, n))
    if times:
        print("      avg %.1fs, min %.1fs, max %.1fs" % (
            sum(times) / len(times), min(times), max(times)))


def main():
    fast = "--fast" in sys.argv
    section("starting zcode-mcp-server")
    c = McpClient()
    try:
        test_handshake(c)
        thread_id = test_new_session(c)
        test_reply(c, thread_id)
        test_complex_task(c)
        test_error_handling(c)
        test_concurrency(c)
        if not fast:
            test_stability(c, n=4)
    finally:
        c.close()

    print("\n==== RESULT: %d passed, %d failed ====" % (PASS, FAIL))
    for name, detail in FAILURES:
        print("  FAILED: %s -- %s" % (name, str(detail)[:300]))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
