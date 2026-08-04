#!/usr/bin/env python3
"""Event-driven ZCode scheduling bridge for MCP clients.

The bridge uses ZCode's persistent app-server protocol.  It exposes compact
run state and control operations while keeping raw model streams and native
session snapshots private.  Codex owns global concurrency; the bridge only
serializes declared worktree and resource conflicts.

Requirements
------------
* ZCode.app installed at /Applications/ZCode.app (or ZCODE_APP_PATH env override).
* `~/.zcode/cli/config.json` configured with a model provider (run with
  `--ensure-config` once to copy the provider from the desktop config).

Usage
-----
  python3 server.py                 # run as an MCP stdio server
  python3 server.py --ensure-config # one-time setup of ~/.zcode/cli/config.json
  python3 server.py --probe         # print ZCode discovery info and exit
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import traceback
from control_plane import ControlPlaneError, ZCodeControlPlane
from zcode_protocol import ProtocolError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TIMEOUT_DEFAULT = int(os.environ.get("ZCODE_MCP_TIMEOUT", "900") or 900)      # seconds per tool call
# Zero means the MCP client owns global scheduling. The bridge still serializes
# conflicting sessions/worktrees/resources; a positive env override is only an
# optional operator safety cap.
MAX_CONCURRENCY = int(os.environ.get("ZCODE_MCP_MAX_CONCURRENCY", "0") or 0)
PROTOCOL_VERSION = "2025-03-26"
SERVER_VERSION = "0.4.0"
STDERR_LOG = os.environ.get("ZCODE_MCP_LOG", "")

def _log(msg: str) -> None:
    if STDERR_LOG:
        try:
            with open(STDERR_LOG, "a", encoding="utf-8", errors="backslashreplace") as fh:
                fh.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
        except OSError:
            pass


def _configure_stdio() -> None:
    """Use MCP's required UTF-8 wire encoding on Windows and other locales."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="strict")


# ---------------------------------------------------------------------------
# ZCode discovery
# ---------------------------------------------------------------------------

def _first_existing(paths):
    for p in paths:
        if os.path.exists(p):
            return p
    return None


def discover_zcode():
    """Locate the ZCode runtime and bundled CLI on macOS or Linux."""
    app = os.environ.get("ZCODE_APP_PATH") or _first_existing([
        "/Applications/ZCode.app",
        os.path.expanduser("~/Applications/ZCode.app"),
        "/opt/ZCode",
        os.path.expanduser("~/.local/opt/ZCode"),
    ])
    if not app:
        raise RuntimeError(
            "ZCode runtime not found. Install it or set ZCODE_APP_PATH=/path/to/ZCode"
        )

    binary = os.environ.get("ZCODE_BINARY") or _first_existing([
        os.path.join(app, "Contents", "MacOS", "ZCode"),
        os.path.join(app, "zcode"),
    ])
    bundle = os.environ.get("ZCODE_CLI_BUNDLE") or _first_existing([
        os.path.join(app, "Contents", "Resources", "glm", "zcode.cjs"),
        os.path.join(app, "resources", "glm", "zcode.cjs"),
    ])
    for p, label in ((binary, "ZCode binary"), (bundle, "CLI bundle")):
        if not p or not os.path.isfile(p):
            raise RuntimeError("%s not found at %s" % (label, p))
    return binary, bundle


# ---------------------------------------------------------------------------
# One-time config bootstrap
# ---------------------------------------------------------------------------

def ensure_cli_config(cli_config_path=None, desktop_config_path=None):
    """Copy the model provider from the desktop config into the CLI config.

    The headless CLI refuses to run without `model.main` in
    `~/.zcode/cli/config.json`. This helper imports the provider + model that
    the desktop app is already using.
    """
    import copy

    cli_config_path = cli_config_path or os.path.expanduser("~/.zcode/cli/config.json")
    desktop_config_path = desktop_config_path or os.path.expanduser("~/.zcode/v2/config.json")

    try:
        with open(desktop_config_path) as fh:
            desktop = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        print("error: cannot read desktop config %s: %s" % (desktop_config_path, e))
        return 1

    providers = desktop.get("provider") or {}

    def _usable(p):
        return (isinstance(p, dict) and p.get("enabled", True) is not False
                and not p.get("systemDisabledReason"))

    usable = {k: v for k, v in providers.items() if _usable(v)}
    if not usable:
        print("error: no usable (enabled) providers found in %s" % desktop_config_path)
        return 1
    # Prefer a provider that carries a static API key (works headless out of the
    # box), then any enabled provider (e.g. OAuth-only official providers).
    with_key = {k: v for k, v in usable.items()
                if isinstance(v, dict) and (v.get("options") or {}).get("apiKey")}
    pool = with_key or usable
    if not pool:
        print("error: no usable provider found")
        return 1

    try:
        with open(cli_config_path) as fh:
            cli = json.load(fh)
    except (OSError, json.JSONDecodeError):
        cli = {}

    for pid, prov in pool.items():
        if not isinstance(prov, dict):
            continue
        models = prov.get("models") or {}
        if not models:
            continue
        model_id = next(iter(models))
        opts = copy.deepcopy(prov.get("options") or {})
        # Use a slug of the provider display name as the config id. Raw desktop
        # provider ids (e.g. UUIDs or "builtin:zai") are rejected/mis-parsed by
        # the CLI's model-target parser, so normalize to [a-z0-9._-].
        base = re.sub(r"[^a-z0-9._-]+", "-", (prov.get("name") or pid).lower()).strip("-")
        target_id = base or "provider"
        if target_id in cli.get("provider", {}):
            target_id = "%s-%s" % (target_id, "1")
        entry = {
            "name": prov.get("name", target_id),
            "kind": prov.get("kind", "openai"),
            "options": opts,
            "models": {model_id: {"id": model_id}},
        }
        # Keep only this provider so the CLI model-target parser sees exactly one.
        cli["provider"] = {target_id: entry}
        cli["model"] = {"main": "%s/%s" % (target_id, model_id)}
        backup = cli_config_path + ".bak"
        if os.path.exists(cli_config_path) and not os.path.exists(backup):
            import shutil
            shutil.copy2(cli_config_path, backup)
        with open(cli_config_path, "w") as fh:
            json.dump(cli, fh, indent=2)
        print("wrote %s (provider=%s model=%s/%s)" % (cli_config_path, target_id, target_id, model_id))
        return 0
    print("error: no usable provider found")
    return 1


# ---------------------------------------------------------------------------
# MCP (JSON-RPC 2.0 over stdio) server
# ---------------------------------------------------------------------------

RUN_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "runId": {"type": "string"},
        "status": {"type": "string"},
        "revision": {"type": "integer"},
        "threadId": {"type": ["string", "null"]},
        "phase": {"type": "string"},
        "elapsedMs": {"type": "integer"},
        "result": {"type": "string"},
    },
    "required": ["runId", "status", "revision"],
    "additionalProperties": True,
}

ZCODE_START_SCHEMA = {
    "type": "object",
    "properties": {
        "prompt": {
            "type": "string",
            "description": "One coherent one-turn task. Mutually exclusive with goal because native goal creation starts its own turn.",
        },
        "threadId": {"type": "string", "description": "Continue an existing session on its bound worktree."},
        "cwd": {"type": "string", "description": "Task worktree or working directory."},
        "mode": {
            "type": "string",
            "enum": ["build", "edit", "plan", "yolo", "auto"],
            "description": "Execution mode. Managed non-plan runs resolve headless tool and implementation-plan permissions inside declared structured path roots; arbitrary user questions are declined. Native durable goals cannot use plan.",
        },
        "thoughtLevel": {"type": "string", "enum": ["high", "max"]},
        "model": {
            "type": "object",
            "properties": {"providerId": {"type": "string"}, "modelId": {"type": "string"}},
            "required": ["providerId", "modelId"],
            "additionalProperties": False,
        },
        "toolAllowlist": {"type": "array", "items": {"type": "string"}},
        "toolDenylist": {"type": "array", "items": {"type": "string"}},
        "workspaceAccess": {
            "type": "string",
            "enum": ["shared", "exclusive"],
            "default": "exclusive",
            "description": "Cross-Bridge scheduling lease. Shared denies structured write permissions; exclusive permits them inside declared roots. Shell command boundaries remain advisory.",
        },
        "resources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "mode": {"type": "string", "enum": ["shared", "exclusive"], "default": "exclusive"},
                },
                "required": ["key"],
                "additionalProperties": False,
            },
            "description": "Additional cross-Bridge conflict domains such as simulator or DerivedData. Absolute path keys also authorize structured writes under that root. Codex owns global concurrency.",
        },
        "goal": {
            "type": "string",
            "description": "One durable native objective. Mutually exclusive with prompt; setting a native goal starts the run.",
        },
        "timeout": {"type": "integer", "minimum": 1, "maximum": 86400, "default": TIMEOUT_DEFAULT, "description": "Whole-run timeout in seconds."},
    },
    "oneOf": [
        {"required": ["prompt"], "not": {"required": ["goal"]}},
        {"required": ["goal"], "not": {"required": ["prompt"]}},
    ],
    "allOf": [
        {
            "if": {"required": ["goal"]},
            "then": {
                "properties": {
                    "mode": {"enum": ["build", "edit", "yolo", "auto"]},
                }
            },
        }
    ],
    "additionalProperties": False,
}

ZCODE_WAIT_SCHEMA = {
    "type": "object",
    "properties": {
        "runId": {"type": "string"},
        "afterRevision": {"type": "integer", "minimum": 0, "default": 0},
        "timeoutMs": {"type": "integer", "minimum": 0, "maximum": 60000, "default": 30000},
        "resultChars": {"type": "integer", "minimum": 0, "maximum": 12000, "default": 2000},
    },
    "required": ["runId"],
    "additionalProperties": False,
}

ZCODE_OBSERVE_SCHEMA = {
    "type": "object",
    "properties": {
        "runId": {"type": "string"},
        "afterSeq": {"type": "integer", "minimum": 0, "default": 0},
        "refresh": {"type": "boolean", "default": True},
        "maxEvents": {"type": "integer", "minimum": 0, "maximum": 30, "default": 12},
        "resultChars": {"type": "integer", "minimum": 0, "maximum": 12000, "default": 2000},
    },
    "required": ["runId"],
    "additionalProperties": False,
}

ZCODE_CONTROL_SCHEMA = {
    "type": "object",
    "properties": {
        "runId": {"type": "string"},
        "action": {
            "type": "string",
            "enum": ["guide", "interrupt", "cancel", "cancel-background", "pause-goal", "resume-goal"],
        },
        "prompt": {"type": "string", "description": "Required for guide or interrupt."},
        "taskId": {"type": "string", "description": "Required for cancel-background."},
        "ifRevision": {
            "type": "integer",
            "minimum": 1,
            "description": "Optional optimistic guard copied from the latest wait/observe revision.",
        },
        "ifStatus": {
            "type": "string",
            "description": "Optional optimistic guard copied from the latest wait/observe status.",
        },
    },
    "required": ["runId", "action"],
    "additionalProperties": False,
}

ZCODE_RECOVER_SCHEMA = {
    "type": "object",
    "properties": {
        "adoptThreadId": {"type": "string", "description": "Adopt one persisted session into a managed run; omit to list candidates."},
        "workspace": {"type": "string"},
        "cwd": {"type": "string"},
        "includeArchived": {"type": "boolean", "default": False},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
        "workspaceAccess": {"type": "string", "enum": ["shared", "exclusive"], "default": "exclusive"},
        "resources": ZCODE_START_SCHEMA["properties"]["resources"],
    },
    "additionalProperties": False,
}

ZCODE_BRANCH_SCHEMA = {
    "type": "object",
    "properties": {
        "runId": {"type": "string"},
        "targetKind": {"type": "string", "enum": ["latestCheckpoint", "checkpoint", "message", "turn"], "default": "latestCheckpoint"},
        "targetId": {"type": "string"},
        "turnIndex": {"type": "integer", "minimum": 0},
    },
    "required": ["runId"],
    "additionalProperties": False,
}

ZCODE_CONTEXT_SCHEMA = {
    "type": "object",
    "properties": {
        "runId": {"type": "string"},
        "action": {"type": "string", "enum": ["inspect", "compact"], "default": "inspect"},
        "instructions": {"type": "string"},
    },
    "required": ["runId"],
    "additionalProperties": False,
}

ZCODE_CLOSE_SCHEMA = {
    "type": "object",
    "properties": {"runId": {"type": "string"}, "threadId": {"type": "string"}},
    "anyOf": [{"required": ["runId"]}, {"required": ["threadId"]}],
    "additionalProperties": False,
}


class ZCodeMcpServer:
    def __init__(self, zcode_bin, zcode_bundle):
        self.zcode_bin = zcode_bin
        self.zcode_bundle = zcode_bundle
        self._write_lock = threading.Lock()
        self._calls = {}  # request_id -> cancellation Event
        self._control = None
        self._control_lock = threading.Lock()

    def _control_plane(self):
        with self._control_lock:
            if self._control is None:
                self._control = ZCodeControlPlane(
                    self.zcode_bin,
                    self.zcode_bundle,
                    max_concurrency=MAX_CONCURRENCY,
                    logger=_log,
                )
            return self._control

    # -- transport ----------------------------------------------------------

    def send(self, payload) -> None:
        line = json.dumps(payload, ensure_ascii=False)
        with self._write_lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    def send_response(self, request_id, result) -> None:
        self.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def send_error(self, request_id, code, message, data=None) -> None:
        err = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        self.send({"jsonrpc": "2.0", "id": request_id, "error": err})

    # -- protocol handlers ---------------------------------------------------

    def handle_initialize(self, request_id, params):
        client = params.get("clientInfo") or {}
        _log("initialize client=%s" % json.dumps(client, ensure_ascii=False))
        self.send_response(request_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {
                "name": "zcode-mcp-server",
                "title": "ZCode",
                "version": SERVER_VERSION,
            },
        })

    def handle_tools_list(self, request_id):
        tools = [
            {
                "name": "zcode-start",
                "title": "Start ZCode Run",
                "description": (
                    "Start one non-blocking ZCode task using exactly one of prompt (one turn) or goal (durable). "
                    "Codex owns global concurrency; independent worktrees and resources run concurrently while "
                    "conflicting exclusive resources queue across Bridge processes. Continue "
                    "with zcode-wait instead of polling or sleeping."
                ),
                "inputSchema": ZCODE_START_SCHEMA,
                "outputSchema": RUN_OUTPUT_SCHEMA,
            },
            {
                "name": "zcode-wait",
                "title": "Wait for ZCode Progress",
                "description": (
                    "Preferred progress path. Wait for a meaningful revision or terminal state; pass the "
                    "last revision as afterRevision. Native subscriptions and replay drive revisions; a "
                    "timeout means unchanged state, not failure."
                ),
                "inputSchema": ZCODE_WAIT_SCHEMA,
                "outputSchema": RUN_OUTPUT_SCHEMA,
            },
            {
                "name": "zcode-observe",
                "title": "Observe ZCode Run",
                "description": (
                    "Read compact model/reasoning activity, exact usage, background tasks, subagents, context, "
                    "goal, checkpoints and bounded events. Raw streams and full snapshots remain private."
                ),
                "inputSchema": ZCODE_OBSERVE_SCHEMA,
                "outputSchema": RUN_OUTPUT_SCHEMA,
            },
            {
                "name": "zcode-control",
                "title": "Control ZCode Run",
                "description": (
                    "Guide after the current turn, interrupt and guide, cancel a run or one native background "
                    "task, and pause/resume a durable goal. Busy guidance retries after native readiness, and a "
                    "control failure never overwrites an already successful turn. Optional ifRevision/ifStatus "
                    "guards reject stale decisions."
                ),
                "inputSchema": ZCODE_CONTROL_SCHEMA,
                "outputSchema": RUN_OUTPUT_SCHEMA,
            },
            {
                "name": "zcode-recover",
                "title": "Recover ZCode Session",
                "description": (
                    "List persisted ZCode sessions or adopt one after a bridge restart. Adoption resumes the "
                    "native session, restores event replay and reacquires declared resource leases."
                ),
                "inputSchema": ZCODE_RECOVER_SCHEMA,
                "outputSchema": {"type": "object", "additionalProperties": True},
            },
            {
                "name": "zcode-branch",
                "title": "Branch ZCode Session",
                "description": "Fork an idle session from a turn, message, workspace checkpoint, or latest checkpoint.",
                "inputSchema": ZCODE_BRANCH_SCHEMA,
                "outputSchema": {"type": "object", "additionalProperties": True},
            },
            {
                "name": "zcode-context",
                "title": "Inspect or Compact Context",
                "description": "Inspect native context/cache pressure or compact an idle session without lowering reasoning quality.",
                "inputSchema": ZCODE_CONTEXT_SCHEMA,
                "outputSchema": {"type": "object", "additionalProperties": True},
            },
            {
                "name": "zcode-close",
                "title": "Close ZCode Session",
                "description": "Release a terminal ZCode session runtime after results, branching and compaction are no longer needed.",
                "inputSchema": ZCODE_CLOSE_SCHEMA,
                "outputSchema": {"type": "object", "additionalProperties": True},
            },
        ]
        self.send_response(request_id, {"tools": tools})

    def handle_tools_call(self, request_id, params):
        name = params.get("name")
        args = params.get("arguments") or {}
        if name not in {
            "zcode-start", "zcode-wait", "zcode-observe", "zcode-control",
            "zcode-recover", "zcode-branch", "zcode-context", "zcode-close",
        }:
            self.send_error(request_id, -32602, "Unknown tool: %s" % name)
            return
        threading.Thread(
            target=self._run_tool, args=(request_id, name, args), daemon=True
        ).start()

    # -- tool execution ------------------------------------------------------

    def _run_tool(self, request_id, name, args):
        cancel = threading.Event()
        self._calls[request_id] = cancel
        try:
            result = self._execute_control(name, args)
            response = {
                "content": [{
                    "type": "text",
                    "text": result if isinstance(result, str) else json.dumps(
                        result, ensure_ascii=False, separators=(",", ":")
                    ),
                }],
                "isError": False,
            }
            if isinstance(result, dict):
                response["structuredContent"] = result
            self.send_response(request_id, response)
        except (ControlPlaneError, ProtocolError) as e:
            self.send_response(request_id, {
                "content": [{
                    "type": "text",
                    "text": "[zcode-error:%s] %s" % (getattr(e, "code", "protocol"), getattr(e, "message", str(e))),
                }],
                "isError": True,
            })
        except Exception as e:  # noqa: BLE001
            _log("internal error: %s\n%s" % (e, traceback.format_exc()))
            self.send_response(request_id, {
                "content": [{"type": "text", "text": "[zcode-internal-error] %s" % e}],
                "isError": True,
            })
        finally:
            self._calls.pop(request_id, None)

    def _execute_control(self, name, args):
        control = self._control_plane()
        if name == "zcode-start":
            mapped = dict(args)
            mapped.setdefault("timeout", TIMEOUT_DEFAULT)
            return control.start(mapped)
        if name == "zcode-wait":
            return control.wait(
                args.get("runId", ""),
                after_revision=args.get("afterRevision", 0),
                timeout_ms=args.get("timeoutMs", 30000),
                result_chars=args.get("resultChars", 2000),
            )
        if name == "zcode-observe":
            return control.observe(
                args.get("runId", ""),
                refresh=args.get("refresh", True),
                after_seq=args.get("afterSeq", 0),
                max_events=args.get("maxEvents", 12),
                result_chars=args.get("resultChars", 2000),
            )
        if name == "zcode-control":
            return control.control(
                args.get("runId", ""), args.get("action", ""),
                prompt=args.get("prompt"), task_id=args.get("taskId"),
                if_revision=args.get("ifRevision"), if_status=args.get("ifStatus"),
            )
        if name == "zcode-recover":
            return control.recover(args)
        if name == "zcode-branch":
            return control.branch(
                args.get("runId", ""),
                target_kind=args.get("targetKind", "latestCheckpoint"),
                target_id=args.get("targetId"),
                turn_index=args.get("turnIndex"),
            )
        if name == "zcode-context":
            return control.context(
                args.get("runId", ""), action=args.get("action", "inspect"),
                instructions=args.get("instructions"),
            )
        if name == "zcode-close":
            return control.close_run(args.get("runId"), thread_id=args.get("threadId"))
        raise ControlPlaneError("unknown control tool: %s" % name, "invalid_params")

    # -- cancellation --------------------------------------------------------

    def _cancel(self, request_id):
        cancel = self._calls.get(request_id)
        if not cancel:
            return
        cancel.set()

    # -- main loop -----------------------------------------------------------

    def dispatch(self, msg) -> None:
        method = msg.get("method")
        req_id = msg.get("id")
        params = msg.get("params") or {}
        if req_id is None:
            # notification
            if method == "notifications/cancelled":
                r = params.get("requestId")
                cancel_id = r.get("id") if isinstance(r, dict) else r
                if cancel_id is not None:
                    self._cancel(cancel_id)
            return
        if method == "initialize":
            self.handle_initialize(req_id, params)
        elif method == "tools/list":
            self.handle_tools_list(req_id)
        elif method == "tools/call":
            self.handle_tools_call(req_id, params)
        elif method == "ping":
            self.send_response(req_id, {})
        elif method in ("resources/list", "prompts/list"):
            self.send_response(req_id, {"resources": []} if method == "resources/list" else {"prompts": []})
        else:
            self.send_error(req_id, -32601, "Method not found: %s" % method)

    def serve(self) -> None:
        _log("starting zcode-mcp-server %s" % SERVER_VERSION)
        _log("zcode_bin=%s" % self.zcode_bin)
        _log("zcode_bundle=%s" % self.zcode_bundle)
        _log("max_concurrency=%s timeout=%s" % (MAX_CONCURRENCY, TIMEOUT_DEFAULT))
        try:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                try:
                    self.dispatch(msg)
                except Exception as e:  # noqa: BLE001
                    _log("dispatch error: %s\n%s" % (e, traceback.format_exc()))
        finally:
            if self._control is not None:
                self._control.close()


def main(argv):
    _configure_stdio()
    if "--ensure-config" in argv:
        return ensure_cli_config()
    if "--probe" in argv:
        try:
            binary, bundle = discover_zcode()
            print("ZCode binary : %s" % binary)
            print("ZCode bundle : %s" % bundle)
            print("CLI config   : %s" % os.path.expanduser("~/.zcode/cli/config.json"))
            return 0
        except RuntimeError as e:
            print("error: %s" % e)
            return 1
    try:
        binary, bundle = discover_zcode()
    except RuntimeError as e:
        _log("discovery failed: %s" % e)
        print("[zcode-mcp-server] error: %s" % e, file=sys.stderr)
        sys.stderr.flush()
        return 1
    ZCodeMcpServer(binary, bundle).serve()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
