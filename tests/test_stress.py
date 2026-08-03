#!/usr/bin/env python3
"""Stress tests for zcode-mcp-plugin/server.py.

Covers the heavier claims: higher concurrency, a longer stability loop, and
multi-turn conversations (repeated zcode-reply on the same session) — the
patterns that matter for 'complex task ready / stable'.

Run:
    python3 tests/test_stress.py [--concurrency N] [--stability N] [--turns N]
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


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL += 1
        print("  FAIL  %s  %s" % (name, ("(" + str(detail) + ")") if detail else ""))


class McpClient:
    def __init__(self):
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        self.proc = subprocess.Popen(
            [sys.executable, SERVER],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=env,
        )
        self._lock = threading.Lock()
        self._cv = threading.Condition()
        self._pending = {}
        self._results = {}
        self._next_id = 0
        threading.Thread(target=self._read_loop, daemon=True).start()

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

    def request(self, method, params=None, timeout=900):
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
            raise TimeoutError("request %s timed out" % rid)
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


def call_zcode(c, prompt, **kw):
    r = c.request("tools/call", {"name": "zcode", "arguments": {"prompt": prompt, **kw}}, timeout=900)
    res = r.get("result") or {}
    content = res.get("content") or []
    return res.get("isError") is False, content[0]["text"] if content else ""


def extract_thread_id(text):
    for line in text.splitlines():
        s = line.strip()
        if s.startswith('"threadId"'):
            val = s.split(":", 1)[-1].strip().strip('",')
            if val.startswith("sess_"):
                return val
    return None


def test_concurrency(c, n):
    print("\n== concurrency: %d parallel zcode sessions ==" % n)
    results = [None] * n

    def run(i):
        try:
            ok, text = call_zcode(c, "Reply with exactly: CONCURRENT_%d_DONE" % i)
            results[i] = (ok, text)
        except Exception as e:  # noqa: BLE001
            results[i] = (False, str(e))

    t0 = time.time()
    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - t0
    check("all %d parallel calls succeeded" % n,
          all(r is not None and r[0] for r in results), results)
    check("all parallel replies correct",
          all(("CONCURRENT_%d_DONE" % i) in r[1] for i, r in enumerate(results) if r), results)
    print("      elapsed: %.1fs" % elapsed)


def test_stability(c, n):
    print("\n== stability: %d sequential zcode calls ==" % n)
    ok = 0
    times = []
    for i in range(n):
        t0 = time.time()
        try:
            good, text = call_zcode(c, "Reply with exactly: STABLE_%d" % i)
            if good and ("STABLE_%d" % i) in text:
                ok += 1
        except Exception as e:  # noqa: BLE001
            print("      call %d exception: %s" % (i, e))
        times.append(time.time() - t0)
    check("all %d stability calls passed" % n, ok == n, "ok=%d/%d" % (ok, n))
    if times:
        print("      avg %.1fs, min %.1fs, max %.1fs" % (
            sum(times) / len(times), min(times), max(times)))


def test_multi_turn(c, turns):
    print("\n== multi-turn: one session, %d zcode-reply rounds ==" % turns)
    ok, text = call_zcode(c, "Reply with exactly: TURN_0_DONE. Remember the number 42.")
    check("turn 0 ok", ok and "TURN_0_DONE" in text, text[:200])
    thread_id = extract_thread_id(text)
    check("thread id captured", bool(thread_id), text[:300])
    for i in range(1, turns):
        r = c.request("tools/call", {
            "name": "zcode-reply",
            "arguments": {"threadId": thread_id,
                          "prompt": "Reply with exactly: TURN_%d_DONE. What number did I ask you to remember?" % i},
        }, timeout=900)
        res = r.get("result") or {}
        content = res.get("content") or []
        t = content[0]["text"] if content else ""
        if res.get("isError") is False and ("TURN_%d_DONE" % i) in t:
            check("turn %d ok" % i, True, t[:200])
        else:
            check("turn %d ok" % i, False, t[:300])
    check("same session reused across all turns",
          extract_thread_id(t) == thread_id, (thread_id, t[:200]))


def main():
    args = sys.argv[1:]
    conc = int(args[args.index("--concurrency") + 1]) if "--concurrency" in args else 4
    stab = int(args[args.index("--stability") + 1]) if "--stability" in args else 6
    turns = int(args[args.index("--turns") + 1]) if "--turns" in args else 3

    c = McpClient()
    try:
        test_concurrency(c, conc)
        test_stability(c, stab)
        test_multi_turn(c, turns)
    finally:
        c.close()
    print("\n==== STRESS RESULT: %d passed, %d failed ====" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
