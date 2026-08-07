#!/usr/bin/env python3
import json
import sys
import time


for raw in sys.stdin.buffer:
    try:
        command = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        response = {"type": "response", "command": "parse", "success": False, "error": str(exc)}
    else:
        kind = command.get("type")
        if kind == "get_state":
            data = {
                "sessionId": "fake-session",
                "sessionFile": "/tmp/fake-session.jsonl",
                "thinkingLevel": "max",
                "model": {"provider": "deepseek", "id": "deepseek-v4-flash"},
            }
        elif kind == "delay":
            time.sleep(float(command.get("seconds") or 0))
            data = {"delayed": True}
        else:
            data = {"echo": command.get("value"), "unicode": "left\u2028right"}
        response = {
            "id": command.get("id"),
            "type": "response",
            "command": kind,
            "success": True,
            "data": data,
        }
    sys.stdout.buffer.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
