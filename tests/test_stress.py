#!/usr/bin/env python3
"""Optional live stress tests for the event-driven MCP contract."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time

from test_mcp import McpClient, wait_terminal


def run_task(client, prompt, cwd, *, thread_id=None):
    args = {"prompt": prompt, "cwd": cwd, "mode": "plan"}
    if thread_id:
        args["threadId"] = thread_id
    started = client.tool("zcode-start", args, timeout=30)
    final = {}
    wait_terminal(client, started["runId"], started, final)
    if final.get("status") != "completed":
        raise AssertionError(final)
    return final


def concurrency(client, count, root):
    results = [None] * count

    def worker(index):
        cwd = os.path.join(root, "parallel-%s" % index)
        os.makedirs(cwd)
        results[index] = run_task(
            client, "Reply exactly STRESS_PARALLEL_%s. Do not use tools." % index, cwd
        )

    started = time.monotonic()
    threads = [threading.Thread(target=worker, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(300)
    if any(thread.is_alive() for thread in threads):
        raise AssertionError("parallel stress timed out")
    for index, result in enumerate(results):
        if not result or "STRESS_PARALLEL_%s" % index not in result.get("result", ""):
            raise AssertionError(result)
    print("PASS %s concurrent sessions in %.2fs" % (count, time.monotonic() - started))


def stability(client, count, root):
    for index in range(count):
        result = run_task(client, "Reply exactly STABLE_%s. Do not use tools." % index, root)
        if "STABLE_%s" % index not in result.get("result", ""):
            raise AssertionError(result)
        client.tool("zcode-close", {"runId": result["runId"]}, timeout=30)
    print("PASS %s sequential lifecycle rounds" % count)


def multi_turn(client, turns, root):
    current = run_task(client, "Reply exactly TURN_0 and remember 42.", root)
    thread_id = current["threadId"]
    for index in range(1, turns):
        current = run_task(
            client,
            "Reply exactly TURN_%s_42 using the remembered number." % index,
            root,
            thread_id=thread_id,
        )
        if current["threadId"] != thread_id or "TURN_%s_42" % index not in current.get("result", ""):
            raise AssertionError(current)
        if current["usage"]["modelRequests"] < 1:
            raise AssertionError("resumed run did not report new model usage: %r" % current)
        if current["sessionUsage"]["modelRequests"] <= current["usage"]["modelRequests"]:
            raise AssertionError("session cumulative usage did not preserve prior turns: %r" % current)
    client.tool("zcode-close", {"runId": current["runId"]}, timeout=30)
    print("PASS %s turns reused one native session" % turns)


def option(name, default):
    return int(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default


def main():
    client = McpClient()
    try:
        with tempfile.TemporaryDirectory(prefix="zcode-stress-") as root:
            concurrency(client, option("--concurrency", 4), root)
            stability(client, option("--stability", 4), root)
            multi_turn(client, option("--turns", 3), root)
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
