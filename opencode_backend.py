"""OpenCode adapter using the headless HTTP server and SSE event stream."""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import threading
import time
import queue
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from agent_control_plane import AdapterControlPlane, bounded
from control_plane import ControlPlaneError
from zcode_protocol import ProtocolError


def discover_opencode():
    configured = os.environ.get("OPENCODE_BINARY")
    binary = configured or shutil.which("opencode")
    if not binary or not os.path.isfile(binary):
        raise RuntimeError("OpenCode executable not found; install it or set OPENCODE_BINARY")
    return os.path.realpath(binary)


class OpenCodeServer:
    """One local OpenCode server shared by all sessions in this Bridge."""

    LISTEN_RE = re.compile(r"listening on (https?://[^\s]+)", re.I)

    def __init__(self, binary, *, logger=None):
        self.binary = binary
        self.logger = logger or (lambda _message: None)
        self.process = None
        self.base_url = None
        self.username = "opencode"
        self.password = secrets.token_urlsafe(24)
        self._lock = threading.RLock()
        self._stderr = []

    def start(self):
        with self._lock:
            if self.process and self.process.poll() is None and self.base_url:
                return self.base_url
            env = dict(os.environ)
            env["OPENCODE_SERVER_USERNAME"] = self.username
            env["OPENCODE_SERVER_PASSWORD"] = self.password
            kwargs = {}
            if os.name != "nt":
                kwargs["start_new_session"] = True
            self.process = subprocess.Popen(
                [self.binary, "serve", "--hostname", "127.0.0.1", "--port", "0", "--no-mdns"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                **kwargs,
            )
            lines = queue.Queue()

            def read_stdout():
                if not self.process or not self.process.stdout:
                    return
                for stdout_line in self.process.stdout:
                    lines.put(stdout_line)

            threading.Thread(target=read_stdout, daemon=True).start()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    break
                try:
                    line = lines.get(timeout=0.1)
                except queue.Empty:
                    continue
                if line:
                    self.logger("opencode: %s" % line.rstrip())
                    match = self.LISTEN_RE.search(line)
                    if match:
                        self.base_url = match.group(1).rstrip("/")
                        threading.Thread(target=self._read_stderr, daemon=True).start()
                        self.request("GET", "/global/health", timeout=15)
                        return self.base_url
            error = "\n".join(self._stderr[-12:])
            self.close()
            raise ProtocolError("OpenCode server failed to start: %s" % (error or "no listen address"))

    def request(self, method, path, *, cwd=None, body=None, timeout=30, raw=False):
        self.start_if_needed()
        url = self.base_url + path
        headers = {
            "Accept": "application/json",
            "Authorization": "Basic " + base64.b64encode(
                (self.username + ":" + self.password).encode("utf-8")
            ).decode("ascii"),
        }
        if cwd:
            headers["x-opencode-directory"] = os.path.realpath(cwd)
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            response = urlopen(request, timeout=timeout)
            if raw:
                return response
            payload = response.read()
            if not payload:
                return None
            return json.loads(payload.decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise ProtocolError(
                "OpenCode HTTP %s %s failed (%s): %s" % (method, path, exc.code, bounded(detail, 1200)),
                code=exc.code,
            ) from exc
        except (OSError, URLError, ValueError) as exc:
            raise ProtocolError("OpenCode request failed: %s" % exc) from exc

    def start_if_needed(self):
        with self._lock:
            running = self.process and self.process.poll() is None and self.base_url
        if not running:
            self.start()

    def close(self):
        with self._lock:
            process = self.process
            self.process = None
            self.base_url = None
        if not process or process.poll() is not None:
            return
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=8)
        except Exception:
            try:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except Exception:
                pass

    def _read_stderr(self):
        process = self.process
        if not process or not process.stderr:
            return
        for line in process.stderr:
            text = line.rstrip()
            if text:
                self._stderr.append(text)
                if len(self._stderr) > 100:
                    del self._stderr[:20]
                self.logger("opencode: %s" % text)


class OpenCodeRuntime:
    def __init__(self, backend, args, emit, disconnect):
        self.backend = backend
        self.server = backend.server
        self.args = dict(args)
        self.cwd = os.path.realpath(args.get("cwd") or os.getcwd())
        self.emit = emit
        self.disconnect = disconnect
        self.session_id = args.get("threadId")
        self._closed = False
        self._settled = False
        self._cancelled = False
        self._prompt_admitted_at = None
        self._busy_seen = False
        self._guidance = []
        self._lock = threading.RLock()
        self._sse_response = None
        self._sse_thread = None

    def start(self, prompt):
        if self.args.get("toolAllowlist"):
            raise ControlPlaneError(
                "OpenCode cannot enforce a closed tool allowlist; use toolDenylist",
                "unsupported_capability",
            )
        self.server.start()
        permission = self._session_permission_rules()
        if self.session_id:
            session = self._request("GET", "/session/%s" % quote(str(self.session_id), safe=""))
            if permission:
                self._request(
                    "PATCH", "/session/%s" % quote(str(self.session_id), safe=""),
                    body={"permission": permission},
                )
        else:
            payload = {
                "title": "Codex Bridge %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
                "permission": permission,
            }
            session = self._request("POST", "/session", body=payload)
            self.session_id = session.get("id")
        if not self.session_id:
            raise ProtocolError("OpenCode did not return a session id")
        self._sse_thread = threading.Thread(target=self._sse_loop, daemon=True)
        self._sse_thread.start()
        if prompt is not None:
            self._send_prompt(prompt)
            threading.Thread(target=self._monitor, daemon=True).start()
        return {
            "threadId": self.session_id,
            "model": self._session_model(session),
        }

    def refresh(self):
        session = self._request("GET", "/session/%s" % quote(str(self.session_id), safe=""))
        statuses = self._request("GET", "/session/status") or {}
        status = statuses.get(self.session_id, {"type": "idle"})
        self.emit({
            "type": "native.refreshed",
            "threadId": self.session_id,
            "phase": status.get("type"),
            "model": self._session_model(session),
        })

    def guide(self, prompt, *, interrupt=False):
        with self._lock:
            self._guidance.append(prompt)
        if interrupt:
            self._request("POST", "/session/%s/abort" % quote(str(self.session_id), safe=""))
        self.emit({"type": "guidance.queued", "detail": {"interrupt": interrupt}})

    def cancel(self):
        self._cancelled = True
        self._request("POST", "/session/%s/abort" % quote(str(self.session_id), safe=""))

    def branch(self, *, target_kind, target_id, turn_index):
        payload = {}
        if target_kind in {"message", "checkpoint"}:
            if not target_id:
                raise ControlPlaneError("targetId is required", "invalid_params")
            payload["messageID"] = target_id
        session = self._request(
            "POST", "/session/%s/fork" % quote(str(self.session_id), safe=""), body=payload
        )
        return {
            "threadId": session.get("id"),
            "parentThreadId": self.session_id,
            "targetMessageId": payload.get("messageID"),
        }

    def context(self, *, action, instructions=None):
        messages = self._messages()
        usage = self._aggregate_usage(messages)
        if action == "inspect":
            return {"context": {}, "usage": usage, "messageCount": len(messages)}
        if action != "compact":
            raise ControlPlaneError("context action must be inspect or compact", "invalid_params")
        model = None
        for message in reversed(messages):
            info = message.get("info") or {}
            if info.get("role") == "assistant" and info.get("providerID") and info.get("modelID"):
                model = {"providerID": info["providerID"], "modelID": info["modelID"]}
                break
        if not model:
            raise ControlPlaneError("OpenCode session has no model to compact with", "model_not_ready")
        self._request(
            "POST",
            "/session/%s/summarize" % quote(str(self.session_id), safe=""),
            body={**model, "auto": False},
            timeout=180,
        )
        return {"context": {}, "usage": usage, "compacted": True}

    def close(self):
        self._closed = True
        response = self._sse_response
        if response:
            try:
                response.close()
            except Exception:
                pass
        thread = self._sse_thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=1)

    def _send_prompt(self, prompt):
        payload = {"parts": [{"type": "text", "text": prompt}]}
        model = self.args.get("model") or {}
        if model.get("providerId") and model.get("modelId"):
            payload["model"] = {
                "providerID": model["providerId"],
                "modelID": model["modelId"],
            }
        if self.args.get("mode") in {"plan", "build"}:
            payload["agent"] = self.args["mode"]
        if self.args.get("thoughtLevel"):
            payload["variant"] = self.args["thoughtLevel"]
        self._request(
            "POST",
            "/session/%s/prompt_async" % quote(str(self.session_id), safe=""),
            body=payload,
        )
        self._prompt_admitted_at = time.monotonic()
        self._busy_seen = False
        self._settled = False
        self.emit({"type": "model.started", "threadId": self.session_id})

    def _monitor(self):
        consecutive_idle = 0
        while not self._closed and not self._settled:
            try:
                statuses = self._request("GET", "/session/status") or {}
                status = statuses.get(self.session_id, {"type": "idle"})
                kind = status.get("type")
                if kind == "busy":
                    self._busy_seen = True
                    consecutive_idle = 0
                elif kind == "retry":
                    consecutive_idle = 0
                    self.emit({
                        "type": "model.retrying",
                        "phase": "retrying",
                        "detail": {"attempt": status.get("attempt"), "next": status.get("next")},
                    })
                else:
                    consecutive_idle += 1
                    admitted_for = time.monotonic() - (self._prompt_admitted_at or time.monotonic())
                    if consecutive_idle >= 2 and (self._busy_seen or admitted_for >= 0.6):
                        with self._lock:
                            next_prompt = self._guidance.pop(0) if self._guidance else None
                        if next_prompt and not self._cancelled:
                            self._send_prompt(next_prompt)
                            consecutive_idle = 0
                            continue
                        self._finalize()
                        return
            except Exception as exc:  # noqa: BLE001
                if not self._closed:
                    self.emit({"type": "monitor.error", "detail": {"message": bounded(exc, 500)}})
            time.sleep(0.25)

    def _finalize(self):
        if self._settled:
            return
        self._settled = True
        try:
            messages = self._messages()
            result = ""
            error = None
            model = {}
            for message in reversed(messages):
                info = message.get("info") or {}
                if info.get("role") != "assistant":
                    continue
                result = self._text(message.get("parts") or [])
                error = info.get("error")
                model = {
                    "model": {
                        "providerId": info.get("providerID"),
                        "modelId": info.get("modelID"),
                    },
                    "status": "idle",
                }
                break
            self.emit({
                "type": "settled",
                "status": "cancelled" if self._cancelled else ("failed" if error else "completed"),
                "threadId": self.session_id,
                "result": result,
                "error": error,
                "model": model,
                "usage": self._aggregate_usage(messages),
            })
        except Exception as exc:  # noqa: BLE001
            self.emit({"type": "settled", "status": "failed", "error": bounded(exc)})

    def _sse_loop(self):
        retries = 0
        while not self._closed:
            try:
                query = "?" + urlencode({"directory": self.cwd})
                response = self.server.request(
                    "GET", "/event" + query, cwd=self.cwd, raw=True, timeout=120
                )
                self._sse_response = response
                retries = 0
                data_lines = []
                for raw in response:
                    if self._closed:
                        return
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if not line:
                        if data_lines:
                            self._handle_sse_data("\n".join(data_lines))
                            data_lines = []
                        continue
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
                retries += 1
            except Exception as exc:  # noqa: BLE001
                if self._closed:
                    return
                retries += 1
                self.emit({"type": "transport.reconnecting", "detail": {"attempt": retries}})
                if retries >= 5:
                    self.disconnect("OpenCode SSE disconnected: %s" % bounded(exc, 800))
                    return
            time.sleep(min(0.25 * (2 ** min(retries, 4)), 4))

    def _handle_sse_data(self, data):
        try:
            event = json.loads(data)
        except ValueError:
            return
        props = event.get("properties") or {}
        if props.get("sessionID") != self.session_id:
            return
        kind = event.get("type")
        if kind == "session.status":
            status = props.get("status") or {}
            if status.get("type") == "busy":
                self._busy_seen = True
        elif kind == "message.part.updated":
            part = props.get("part") or {}
            ptype = part.get("type")
            if ptype == "reasoning":
                if part.get("time", {}).get("end"):
                    self.emit({"type": "reasoning.ended"})
                else:
                    self.emit({"type": "reasoning.started"})
            elif ptype == "tool":
                state = part.get("state") or {}
                tool_id = part.get("callID") or part.get("id")
                if state.get("status") in {"pending", "running"}:
                    self.emit({
                        "type": "tool.started",
                        "toolCallId": tool_id,
                        "toolName": part.get("tool"),
                    })
                elif state.get("status") in {"completed", "error"}:
                    self.emit({
                        "type": "tool.ended",
                        "toolCallId": tool_id,
                        "toolName": part.get("tool"),
                        "detail": {"isError": state.get("status") == "error"},
                    })
        elif kind == "permission.asked":
            self._reply_permission(props)
        elif kind in {"question.asked", "question.v2.asked"}:
            self._reject_question(props)
        elif kind == "session.error":
            self.emit({"type": "model.error", "error": props.get("error")})

    def _reply_permission(self, props):
        request_id = props.get("id") or props.get("requestID")
        if not request_id:
            return
        permission = str(props.get("permission") or "")
        reject = self._permission_denied(permission, props)
        reply = "reject" if reject else "once"
        try:
            self._request(
                "POST", "/permission/%s/reply" % quote(str(request_id), safe=""),
                body={"reply": reply},
            )
            self.emit({
                "type": "interaction.permission-%s" % ("denied" if reject else "approved"),
                "detail": {"permission": permission},
            })
        except Exception as exc:  # noqa: BLE001
            self.emit({"type": "interaction.error", "detail": {"message": bounded(exc, 500)}})

    def _permission_denied(self, permission, props):
        if self.args.get("workspaceAccess") == "shared" and permission in {
            "edit", "write", "patch", "bash", "shell",
        }:
            return True
        if permission in {"edit", "write", "patch"}:
            candidates = self._permission_paths(props)
            if not candidates:
                return True
            return not all(self._path_mode(value) == "exclusive" for value in candidates)
        if permission != "external_directory":
            return False
        candidates = self._permission_paths(props)
        if not candidates:
            return True
        return not all(self._path_mode(value) is not None for value in candidates)

    @staticmethod
    def _permission_paths(props):
        candidates = []
        metadata = props.get("metadata") or {}
        for key in ("filepath", "filePath", "path", "parentDir"):
            if isinstance(metadata.get(key), str):
                candidates.append(metadata[key])
        candidates.extend(
            value for value in (props.get("patterns") or []) if isinstance(value, str)
        )
        return candidates

    def _session_permission_rules(self):
        rules = [
            {"permission": "external_directory", "pattern": "*", "action": "ask"},
            {"permission": "question", "pattern": "*", "action": "deny"},
        ]
        read_only = (
            self.args.get("workspaceAccess") == "shared"
            or self.args.get("mode") == "plan"
        )
        if read_only:
            rules.extend([
                {"permission": "edit", "pattern": "*", "action": "deny"},
                {"permission": "bash", "pattern": "*", "action": "deny"},
            ])
        else:
            rules.append({"permission": "edit", "pattern": "*", "action": "ask"})
        aliases = {"write": "edit", "patch": "edit", "shell": "bash"}
        rules.extend(
            {"permission": aliases.get(name, name), "pattern": "*", "action": "deny"}
            for name in self.args.get("toolDenylist") or []
        )
        return rules

    def _allowed_roots(self):
        return list(self._root_modes())

    def _root_modes(self):
        roots = {self.cwd: self.args.get("workspaceAccess") or "exclusive"}
        for item in self.args.get("resources") or []:
            key = item.get("key") if isinstance(item, dict) else None
            if key and os.path.isabs(str(key)):
                root = os.path.realpath(str(key))
                mode = item.get("mode") or "exclusive"
                previous = roots.get(root)
                roots[root] = "exclusive" if "exclusive" in {previous, mode} else "shared"
        return roots

    def _path_mode(self, value):
        # OpenCode patterns usually end in "/*"; compare the non-glob prefix.
        raw = re.split(r"[*?[]", str(value), maxsplit=1)[0].rstrip(os.sep) or os.sep
        target = os.path.realpath(raw if os.path.isabs(raw) else os.path.join(self.cwd, raw))
        matches = []
        for root, mode in self._root_modes().items():
            try:
                if os.path.commonpath([target, root]) == root:
                    matches.append((len(root), mode))
            except ValueError:
                continue
        return max(matches)[1] if matches else None

    def _reject_question(self, props):
        request_id = props.get("id") or props.get("requestID")
        if not request_id:
            return
        try:
            self._request("POST", "/question/%s/reject" % quote(str(request_id), safe=""))
            self.emit({"type": "interaction.user-input-declined"})
        except Exception as exc:  # noqa: BLE001
            self.emit({"type": "interaction.error", "detail": {"message": bounded(exc, 500)}})

    def _messages(self):
        value = self._request(
            "GET", "/session/%s/message?limit=100" % quote(str(self.session_id), safe="")
        )
        return value if isinstance(value, list) else []

    def _request(self, method, path, *, body=None, timeout=30):
        separator = "&" if "?" in path else "?"
        if "directory=" not in path:
            path += separator + urlencode({"directory": self.cwd})
        return self.server.request(method, path, cwd=self.cwd, body=body, timeout=timeout)

    @staticmethod
    def _text(parts):
        return "\n".join(
            str(part.get("text")) for part in parts
            if part.get("type") == "text" and part.get("text")
        )

    @staticmethod
    def _session_model(session):
        model = session.get("model") or {}
        return {
            "model": {
                "providerId": model.get("providerID"),
                "modelId": model.get("modelID"),
            }
        }

    @staticmethod
    def _aggregate_usage(messages):
        result = {
            "totalTokens": 0,
            "inputTokens": 0,
            "outputTokens": 0,
            "reasoningTokens": 0,
            "cacheReadTokens": 0,
            "cacheWriteTokens": 0,
            "modelRequests": 0,
        }
        for message in messages:
            info = message.get("info") or {}
            if info.get("role") != "assistant":
                continue
            result["modelRequests"] += 1
            tokens = info.get("tokens") or {}
            result["inputTokens"] += int(tokens.get("input") or 0)
            result["outputTokens"] += int(tokens.get("output") or 0)
            result["reasoningTokens"] += int(tokens.get("reasoning") or 0)
            cache = tokens.get("cache") or {}
            result["cacheReadTokens"] += int(cache.get("read") or 0)
            result["cacheWriteTokens"] += int(cache.get("write") or 0)
        result["totalTokens"] = sum(
            result[key] for key in (
                "inputTokens", "outputTokens", "reasoningTokens",
                "cacheReadTokens", "cacheWriteTokens",
            )
        )
        return result


class OpenCodeBackend:
    name = "opencode"

    def __init__(self, binary=None, *, logger=None, server=None):
        self.binary = binary
        self.logger = logger or (lambda _message: None)
        self.server = server
        self._lock = threading.Lock()

    @property
    def capabilities(self):
        return {
            "prompt": True,
            "durableGoal": False,
            "guidance": ["guide", "interrupt", "cancel"],
            "reasoningEvents": True,
            "usage": "exact",
            "branch": True,
            "compact": True,
            "backgroundTasks": False,
            "permissionProtocol": "http-event",
            "filesystemBoundary": "native-permission-events",
            "shellBoundary": "advisory-in-exclusive; denied-in-shared",
            "toolPolicy": "deny-list-only",
        }

    def probe(self):
        try:
            self.binary = self.binary or discover_opencode()
            result = subprocess.run(
                [self.binary, "--version"], capture_output=True, text=True, timeout=10
            )
            version = (result.stdout or result.stderr).strip().splitlines()[-1]
            return {"available": result.returncode == 0, "version": version, "binary": self.binary}
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "reason": bounded(exc, 500)}

    def _server(self):
        with self._lock:
            if self.server is None:
                self.binary = self.binary or discover_opencode()
                self.server = OpenCodeServer(self.binary, logger=self.logger)
            return self.server

    def create_runtime(self, args, emit, disconnect):
        self._server().start()
        return OpenCodeRuntime(self, args, emit, disconnect)

    def list_sessions(self, args):
        server = self._server()
        cwd = os.path.realpath(args.get("cwd") or args.get("workspace") or os.getcwd())
        query = "?" + urlencode({"directory": cwd, "limit": min(max(int(args.get("limit", 20)), 1), 100)})
        sessions = server.request("GET", "/session" + query, cwd=cwd) or []
        return [
            {
                "threadId": item.get("id"),
                "status": "idle",
                "title": item.get("title"),
                "cwd": item.get("directory") or cwd,
                "createdAtMs": (item.get("time") or {}).get("created"),
                "updatedAtMs": (item.get("time") or {}).get("updated"),
            }
            for item in sessions
        ]

    def close(self):
        with self._lock:
            server = self.server
            self.server = None
        if server:
            server.close()

    def control_plane(self, *, max_concurrency=0, logger=None):
        return AdapterControlPlane(
            self, max_concurrency=max_concurrency, logger=logger or self.logger
        )
