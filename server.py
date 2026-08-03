#!/usr/bin/env python3
"""
ZCode MCP server — expose ZCode as an agent to any MCP client.

Mirrors `codex mcp-server` (which exposes the `codex` and `codex-reply` tools)
with two equivalent tools backed by ZCode's headless CLI:

  * `zcode`       — run a ZCode session (new conversation)
  * `zcode-reply` — continue a ZCode session by threadId

Backed by ZCode's bundled CLI (`glm/zcode.cjs` inside ZCode.app), driven through
the bundled Electron runtime with `ELECTRON_RUN_AS_NODE=1`, so the server needs
no separate Node.js/Python dependencies at runtime.

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
import subprocess
import sys
import threading
import time
import traceback

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TIMEOUT_DEFAULT = int(os.environ.get("ZCODE_MCP_TIMEOUT", "900") or 900)      # seconds per tool call
MAX_CONCURRENCY = int(os.environ.get("ZCODE_MCP_MAX_CONCURRENCY", "2") or 2)  # parallel ZCode sessions
PROTOCOL_VERSION = "2025-03-26"
SERVER_VERSION = "0.1.0"
STDERR_LOG = os.environ.get("ZCODE_MCP_LOG", "")

MODES = ("build", "edit", "plan", "yolo", "auto")

# Options advertised in `zcode --help` but NOT implemented by the 0.16.1
# argument parser (passing them aborts with exit code 1). Accepted in tool
# schemas for API compatibility but ignored at runtime.
UNSUPPORTED_OPTIONS = {"max-turns", "allowed-tools", "settings", "permission-mode"}


def _log(msg: str) -> None:
    if STDERR_LOG:
        try:
            with open(STDERR_LOG, "a") as fh:
                fh.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
        except OSError:
            pass


# ---------------------------------------------------------------------------
# ZCode discovery
# ---------------------------------------------------------------------------

def _first_existing(paths):
    for p in paths:
        if os.path.exists(p):
            return p
    return None


def discover_zcode():
    """Locate the ZCode app binary and the bundled CLI bundle."""
    app = os.environ.get("ZCODE_APP_PATH") or _first_existing([
        "/Applications/ZCode.app",
        os.path.expanduser("~/Applications/ZCode.app"),
    ])
    if not app:
        raise RuntimeError(
            "ZCode.app not found. Install it or set ZCODE_APP_PATH=/path/to/ZCode.app"
        )
    binary = os.path.join(app, "Contents", "MacOS", "ZCode")
    bundle = os.environ.get("ZCODE_CLI_BUNDLE") or os.path.join(
        app, "Contents", "Resources", "glm", "zcode.cjs"
    )
    for p, label in ((binary, "ZCode binary"), (bundle, "CLI bundle")):
        if not os.path.isfile(p):
            raise RuntimeError("%s not found at %s" % (label, p))
    return binary, bundle


# ---------------------------------------------------------------------------
# ZCode session runner
# ---------------------------------------------------------------------------

class SessionError(RuntimeError):
    def __init__(self, message, code="zcode_error", data=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.data = data or {}


def _extract_json(text: str):
    """Return the last complete top-level JSON object found in `text`."""
    t = text.strip()
    try:
        obj = json.loads(t)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass
    depth = 0
    start = None
    last_obj = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                candidate = text[start:i + 1]
                try:
                    obj = json.loads(candidate)
                    if isinstance(obj, dict):
                        last_obj = obj
                except (json.JSONDecodeError, ValueError):
                    pass
                start = None
    return last_obj


def run_zcode(prompt, *, thread_id=None, cwd=None, mode="yolo", model=None,
              max_turns=None, allowed_tools=None, disallowed_tools=None,
              timeout=None, zcode_bin=None, zcode_bundle=None):
    """Run a ZCode headless session; return the parsed result dict.

    Mirrors `codex mcp-server`'s `codex`/`codex-reply` tools:
      * thread_id set  -> `zcode --resume <thread_id> --prompt ...`  (reply)
      * thread_id None -> `zcode --prompt ...`                       (new session)
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise SessionError("prompt must be a non-empty string", code="invalid_params")
    if not zcode_bin or not zcode_bundle:
        raise SessionError("ZCode runtime not initialized", code="zcode_not_found")

    cmd = [zcode_bin, zcode_bundle, "--prompt", prompt, "--json", "--no-color"]
    if thread_id:
        cmd += ["--resume", thread_id]
    if cwd:
        cmd += ["--cwd", cwd]
    if mode and mode in MODES:
        cmd += ["--mode", mode]

    def _to_list(v):
        if v is None:
            return None
        if isinstance(v, list):
            return [str(x) for x in v]
        return [str(v)]

    dt = _to_list(disallowed_tools)
    if dt:
        # --disallowed-tools is a real flag; --max-turns/--allowed-tools are
        # advertised but rejected by the 0.16.1 parser, so they are ignored.
        cmd += ["--disallowed-tools", ",".join(dt)]

    env = dict(os.environ)
    env["ELECTRON_RUN_AS_NODE"] = "1"
    env["NO_COLOR"] = "1"
    if model:
        # ZCODE_MODEL accepts "provider/model" or a model id
        env["ZCODE_MODEL"] = model

    timeout = timeout or TIMEOUT_DEFAULT
    _log("RUN %s" % " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.communicate(timeout=10)
        except Exception:
            proc.terminate()
        raise SessionError(
            "ZCode session timed out after %ss" % timeout, code="timeout"
        )

    _log("EXIT %s out=%s err=%s" % (proc.returncode, len(out), len(err)))
    if proc.returncode != 0:
        raise SessionError(
            "ZCode exited with code %s: %s" % (proc.returncode, (err or out)[-2000:]),
            code="zcode_exit_error",
        )
    result = _extract_json(out)
    if not result:
        raise SessionError(
            "ZCode produced no parseable JSON output",
            code="parse_error",
            data={"stdout": out[-2000:], "stderr": err[-2000:]},
        )
    return result


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

ZCODE_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "prompt": {
            "type": "string",
            "description": "The initial user prompt to start the ZCode conversation.",
        },
        "threadId": {
            "type": "string",
            "description": "Existing ZCode session id (sess_...) to resume instead of starting a new conversation.",
        },
        "model": {
            "type": "string",
            "description": "Optional model override, e.g. 'deepseek/deepseek-v4-flash' or a model id.",
        },
        "cwd": {
            "type": "string",
            "description": "Working directory for the session.",
        },
        "sandbox": {
            "type": "string",
            "enum": ["read-only", "workspace-write", "danger-full-access"],
            "description": "Maps to ZCode permission mode: read-only -> plan, workspace-write -> build, danger-full-access -> yolo.",
        },
        "mode": {
            "type": "string",
            "enum": ["build", "edit", "plan", "yolo"],
            "description": "ZCode permission mode for the session (default yolo).",
        },
        "maxTurns": {
            "type": "integer",
            "description": "Maximum model turns for the session. NOTE: accepted for compatibility; ignored by ZCode CLI 0.16.1 (flag not implemented).",
        },
        "allowedTools": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Tool allowlist, e.g. ['Bash', 'Read', 'Edit']. NOTE: accepted for compatibility; ignored by ZCode CLI 0.16.1 (flag not implemented).",
        },
        "disallowedTools": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Tool denylist, e.g. ['Bash(git push *)']. Passed through as --disallowed-tools.",
        },
        "timeout": {
            "type": "integer",
            "description": "Timeout in seconds (default %s, ZCODE_MCP_TIMEOUT)." % TIMEOUT_DEFAULT,
        },
    },
    "required": ["prompt"],
}

ZCODE_REPLY_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "threadId": {
            "type": "string",
            "description": "The ZCode session id (sess_...) to continue.",
        },
        "prompt": {
            "type": "string",
            "description": "The next user prompt to continue the ZCode conversation.",
        },
        "model": {
            "type": "string",
            "description": "Optional model override, e.g. 'deepseek/deepseek-v4-flash'.",
        },
        "cwd": {
            "type": "string",
            "description": "Working directory for the session.",
        },
        "mode": {
            "type": "string",
            "enum": ["build", "edit", "plan", "yolo"],
            "description": "ZCode permission mode for the session.",
        },
        "maxTurns": {
            "type": "integer",
            "description": "Maximum model turns for the session. NOTE: accepted for compatibility; ignored by ZCode CLI 0.16.1 (flag not implemented).",
        },
        "timeout": {
            "type": "integer",
            "description": "Timeout in seconds (default %s)." % TIMEOUT_DEFAULT,
        },
    },
    "required": ["threadId", "prompt"],
}

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "threadId": {"type": "string"},
        "content": {"type": "string"},
    },
    "required": ["threadId", "content"],
}

SANDBOX_TO_MODE = {
    "read-only": "plan",
    "workspace-write": "build",
    "danger-full-access": "yolo",
}


class ZCodeMcpServer:
    def __init__(self, zcode_bin, zcode_bundle):
        self.zcode_bin = zcode_bin
        self.zcode_bundle = zcode_bundle
        self._write_lock = threading.Lock()
        self._sem = threading.Semaphore(MAX_CONCURRENCY)
        self._calls = {}  # request_id -> {"proc": Popen or None, "cancel": Event}

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
                "name": "zcode",
                "title": "ZCode",
                "description": (
                    "Run a ZCode session. Starts a new ZCode conversation with the "
                    "given prompt (pass threadId to resume an existing session). "
                    "ZCode is a coding agent that can read/write files, run shell "
                    "commands, search code, and use MCP tools."
                ),
                "inputSchema": ZCODE_TOOL_SCHEMA,
                "outputSchema": OUTPUT_SCHEMA,
            },
            {
                "name": "zcode-reply",
                "title": "ZCode Reply",
                "description": (
                    "Continue a ZCode conversation by providing the thread id "
                    "(the sessionId returned by a previous zcode call) and a prompt."
                ),
                "inputSchema": ZCODE_REPLY_TOOL_SCHEMA,
                "outputSchema": OUTPUT_SCHEMA,
            },
        ]
        self.send_response(request_id, {"tools": tools})

    def handle_tools_call(self, request_id, params):
        name = params.get("name")
        args = params.get("arguments") or {}
        if name not in ("zcode", "zcode-reply"):
            self.send_error(request_id, -32602, "Unknown tool: %s" % name)
            return
        threading.Thread(
            target=self._run_tool, args=(request_id, name, args), daemon=True
        ).start()

    # -- tool execution ------------------------------------------------------

    def _run_tool(self, request_id, name, args):
        proc_ref = {"proc": None}
        cancel = threading.Event()
        self._calls[request_id] = (proc_ref, cancel)
        try:
            with self._sem:
                if cancel.is_set():
                    raise SessionError("cancelled", code="cancelled")
                result = self._execute(name, args, proc_ref)
            self.send_response(request_id, {
                "content": [{"type": "text", "text": result}],
                "isError": False,
            })
        except SessionError as e:
            self.send_response(request_id, {
                "content": [{
                    "type": "text",
                    "text": "[zcode-error:%s] %s" % (e.code, e.message),
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

    def _execute(self, name, args, proc_ref):
        if name == "zcode":
            prompt = args.get("prompt", "")
            thread_id = args.get("threadId")
            kwargs = self._common_kwargs(args)
        else:
            prompt = args.get("prompt", "")
            thread_id = args.get("threadId")
            kwargs = self._common_kwargs(args)
            if not thread_id:
                raise SessionError(
                    "threadId is required for zcode-reply", code="invalid_params"
                )

        mode = args.get("mode")
        sandbox = args.get("sandbox")
        if not mode and sandbox in SANDBOX_TO_MODE:
            mode = SANDBOX_TO_MODE[sandbox]
        kwargs["mode"] = mode or "yolo"

        result = run_zcode(
            prompt,
            thread_id=thread_id,
            model=args.get("model"),
            cwd=args.get("cwd"),
            mode=kwargs["mode"],
            max_turns=args.get("maxTurns"),
            timeout=args.get("timeout"),
            zcode_bin=self.zcode_bin,
            zcode_bundle=self.zcode_bundle,
        )
        return self._format_result(result)

    @staticmethod
    def _common_kwargs(args):
        return {}

    @staticmethod
    def _format_result(result):
        response = result.get("response", "")
        meta = {
            "threadId": result.get("sessionId"),
            "traceId": result.get("traceId"),
            "turnId": result.get("turnId"),
            "usage": result.get("usage"),
            "projection": result.get("projection"),
        }
        return "%s\n\n--- zcode metadata ---\n%s" % (
            response,
            json.dumps(meta, ensure_ascii=False, indent=2),
        )

    # -- cancellation --------------------------------------------------------

    def _cancel(self, request_id):
        entry = self._calls.get(request_id)
        if not entry:
            return
        proc_ref, cancel = entry
        cancel.set()
        proc = proc_ref.get("proc")
        if proc and proc.poll() is None:
            _log("cancelling request %s (pid %s)" % (request_id, proc.pid))
            proc.kill()

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


def main(argv):
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
