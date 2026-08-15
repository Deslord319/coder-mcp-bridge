"""Pi coding-agent adapter using its strict LF-delimited RPC protocol."""

from __future__ import annotations

import glob
import json
import os
import shutil
import signal
import subprocess
import threading
import time
import uuid

from agent_control_plane import AdapterControlPlane, bounded
from control_plane import ControlPlaneError
from model_deployments import ModelDeploymentManager
from zcode_protocol import ProtocolError


def discover_pi():
    configured = os.environ.get("PI_BINARY")
    binary = configured or shutil.which("pi")
    if not binary or not os.path.isfile(binary):
        raise RuntimeError("Pi executable not found; install Pi or set PI_BINARY")
    return os.path.realpath(binary)


class PiRpcClient:
    """One Pi RPC process. Requests and asynchronous events share stdout."""

    def __init__(self, command, *, cwd, env=None, on_event=None, on_disconnect=None, logger=None):
        self.command = list(command)
        self.cwd = cwd
        self.env = env
        self.on_event = on_event or (lambda _event: None)
        self.on_disconnect = on_disconnect or (lambda _message: None)
        self.logger = logger or (lambda _message: None)
        self.process = None
        self._write_lock = threading.Lock()
        self._lock = threading.RLock()
        self._pending = {}
        self._closed = False
        self._stderr = []

    @property
    def process_id(self):
        return self.process.pid if self.process else None

    def start(self):
        with self._lock:
            if self.process and self.process.poll() is None:
                return
            kwargs = {}
            if os.name != "nt":
                kwargs["start_new_session"] = True
            self.process = subprocess.Popen(
                self.command,
                cwd=self.cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                env=self.env,
                **kwargs,
            )
            threading.Thread(target=self._read_stdout, daemon=True).start()
            threading.Thread(target=self._read_stderr, daemon=True).start()
        self.request("get_state", timeout=30)

    def request(self, command_type, params=None, timeout=30):
        request_id, entry = self._begin_request(command_type, params)
        return self._await_response(request_id, entry, command_type, timeout)

    def request_background(self, command_type, params=None, timeout=30, callback=None):
        """Send an RPC command now and wait for its response off the caller thread."""
        request_id, entry = self._begin_request(command_type, params)

        def await_response():
            try:
                data = self._await_response(request_id, entry, command_type, timeout)
            except Exception as exc:  # noqa: BLE001
                if callback:
                    callback(None, exc)
            else:
                if callback:
                    callback(data, None)

        threading.Thread(target=await_response, daemon=True).start()
        return request_id

    def _begin_request(self, command_type, params=None):
        self.start_if_needed()
        request_id = "bridge-" + uuid.uuid4().hex
        event = threading.Event()
        entry = {"event": event, "response": None}
        with self._lock:
            self._pending[request_id] = entry
        payload = {"id": request_id, "type": command_type}
        payload.update(params or {})
        try:
            self._send(payload)
        except Exception:
            with self._lock:
                self._pending.pop(request_id, None)
            raise
        return request_id, entry

    def _await_response(self, request_id, entry, command_type, timeout):
        try:
            if not entry["event"].wait(timeout):
                raise ProtocolError("Pi RPC request timed out: %s" % command_type)
            response = entry["response"] or {}
            if not response.get("success", False):
                raise ProtocolError(
                    response.get("error") or "Pi RPC command failed: %s" % command_type,
                    data=response,
                )
            return response.get("data") or {}
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def start_if_needed(self):
        with self._lock:
            running = self.process and self.process.poll() is None
        if not running:
            self.start()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            process = self.process
        if not process:
            return
        try:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
            process.wait(timeout=8)
        except Exception:
            try:
                if os.name != "nt":
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
                process.wait(timeout=5)
            except Exception:
                try:
                    if os.name != "nt":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                except Exception:
                    pass
        finally:
            for stream in (process.stdout, process.stderr):
                try:
                    if stream:
                        stream.close()
                except Exception:
                    pass

    def _send(self, payload):
        data = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._write_lock:
            process = self.process
            if not process or process.poll() is not None or not process.stdin:
                raise ProtocolError("Pi RPC process is not running")
            process.stdin.write(data)
            process.stdin.flush()

    def _read_stdout(self):
        process = self.process
        buffer = b""
        try:
            while process and process.stdout:
                chunk = process.stdout.read(4096)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    if raw.endswith(b"\r"):
                        raw = raw[:-1]
                    if raw:
                        self._handle_line(raw)
            if buffer.strip():
                self.logger("Pi RPC ended with an unterminated JSON record")
        except Exception as exc:  # noqa: BLE001
            self.logger("Pi RPC reader failed: %s" % exc)
        finally:
            self._fail_pending("Pi RPC transport closed")
            if not self._closed:
                self.on_disconnect("Pi RPC process exited: %s" % self._stderr_text())

    def _handle_line(self, raw):
        try:
            message = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            self.logger("invalid Pi RPC JSON: %s" % exc)
            return
        if message.get("type") == "response" and message.get("id"):
            with self._lock:
                entry = self._pending.get(message["id"])
                if entry:
                    entry["response"] = message
                    entry["event"].set()
            return
        if message.get("type") == "extension_ui_request":
            self._handle_extension_ui(message)
            return
        self.on_event(message)

    def _handle_extension_ui(self, request):
        method = request.get("method")
        if method in {"notify", "setStatus", "setWidget", "setTitle", "set_editor_text"}:
            self.on_event({"type": "extension.ui", "detail": {"method": method}})
            return
        response = {"type": "extension_ui_response", "id": request.get("id"), "cancelled": True}
        if method == "confirm":
            response = {"type": "extension_ui_response", "id": request.get("id"), "confirmed": False}
        self._send(response)
        self.on_event({"type": "interaction.declined", "detail": {"method": method}})

    def _read_stderr(self):
        process = self.process
        if not process or not process.stderr:
            return
        for raw in iter(process.stderr.readline, b""):
            text = raw.decode("utf-8", "replace").rstrip()
            if text:
                self._stderr.append(text)
                if len(self._stderr) > 80:
                    del self._stderr[:20]
                self.logger("pi: %s" % text)

    def _stderr_text(self):
        return bounded("\n".join(self._stderr[-12:]), 1600) or "no stderr"

    def _fail_pending(self, message):
        with self._lock:
            for entry in self._pending.values():
                if entry["response"] is None:
                    entry["response"] = {"success": False, "error": message}
                    entry["event"].set()


class PiRuntime:
    def __init__(self, backend, args, emit, disconnect):
        self.backend = backend
        self.args = dict(args)
        self.emit = emit
        self.disconnect = disconnect
        self.client = None
        self.state = {}
        self._settling = False
        self._closed = False
        self._control_lock = threading.RLock()
        self._pending_interrupt = None
        self._cancel_requested = False
        self._settled_event_seen = False
        self._deployment_lease = None

    def start(self, prompt):
        cwd = os.path.realpath(self.args.get("cwd") or os.getcwd())
        deployments = getattr(self.backend, "deployments", None)
        deployment_key = (
            deployments.configured(self.args.get("model")) if deployments else None
        )
        if deployment_key:
            self.emit({"type": "deployment.starting", "phase": "model-cold-start"})
            self._deployment_lease = deployments.acquire(self.args.get("model"))
            self.emit({"type": "deployment.ready", "phase": "starting-runtime"})
        command = self._build_command(cwd, self.args.get("threadId"))
        env = self._build_env(cwd)
        self.client = PiRpcClient(
            command,
            cwd=cwd,
            env=env,
            on_event=self._on_event,
            on_disconnect=self.disconnect,
            logger=self.backend.logger,
        )
        self.client.start()
        self.state = self.client.request("get_state")
        thread_id = self.state.get("sessionFile") or self.state.get("sessionId")
        result = {
            "threadId": thread_id,
            "model": self._model_projection(self.state),
        }
        if prompt is not None:
            self.client.request("prompt", {"message": prompt})
        else:
            self._hibernate()
            self._release_deployment()
        return result

    def _build_command(self, cwd, thread_id=None):
        command = [
            self.backend.binary, "--mode", "rpc", "--approve",
            "--extension", self.backend.policy_extension,
        ]
        session_dir = self.backend.session_dir
        os.makedirs(session_dir, exist_ok=True)
        command += ["--session-dir", session_dir]
        if thread_id:
            command += ["--session", str(thread_id)]
        model = self.args.get("model") or {}
        if model.get("providerId") and model.get("modelId"):
            command += ["--provider", model["providerId"], "--model", model["modelId"]]
        if self.args.get("thoughtLevel"):
            thought_level = "xhigh" if self.args["thoughtLevel"] == "max" else self.args["thoughtLevel"]
            command += ["--thinking", thought_level]
        if self.args.get("toolAllowlist"):
            command += ["--tools", ",".join(self.args["toolAllowlist"])]
        if self.args.get("toolDenylist"):
            command += ["--exclude-tools", ",".join(self.args["toolDenylist"])]
        return command

    def _build_env(self, cwd):
        env = dict(os.environ)
        env.update({
            "AGENT_BRIDGE_CWD": cwd,
            "AGENT_BRIDGE_WORKSPACE_ACCESS": self.args.get("workspaceAccess") or "exclusive",
            "AGENT_BRIDGE_MODE": self.args.get("mode") or "auto",
            "AGENT_BRIDGE_ALLOWED_ROOTS": json.dumps(
                self._allowed_roots(cwd), ensure_ascii=False, separators=(",", ":")
            ),
            "AGENT_BRIDGE_ROOT_MODES": json.dumps(
                self._root_modes(cwd), ensure_ascii=False, separators=(",", ":")
            ),
        })
        return env

    def _allowed_roots(self, cwd):
        return list(self._root_modes(cwd))

    def _root_modes(self, cwd):
        roots = {cwd: self.args.get("workspaceAccess") or "exclusive"}
        for item in self.args.get("resources") or []:
            key = item.get("key") if isinstance(item, dict) else None
            if key and os.path.isabs(str(key)):
                root = os.path.realpath(str(key))
                mode = item.get("mode") or "exclusive"
                previous = roots.get(root)
                roots[root] = "exclusive" if "exclusive" in {previous, mode} else "shared"
        return roots

    def refresh(self):
        if not self.client:
            return
        state = self.client.request("get_state", timeout=15)
        stats = self.client.request("get_session_stats", timeout=15)
        self.state = state
        self.emit({
            "type": "native.refreshed",
            "threadId": state.get("sessionFile") or state.get("sessionId"),
            "model": self._model_projection(state),
            "usage": self._usage(stats),
            "context": self._context(stats),
        })

    def guide(self, prompt, *, interrupt=False):
        if not self.client:
            raise ProtocolError("Pi session is not ready")
        with self._control_lock:
            if self._cancel_requested:
                raise ProtocolError("Pi cancellation is already pending")
        if interrupt:
            with self._control_lock:
                if self._pending_interrupt:
                    raise ProtocolError("Pi interrupt is already pending")
                token = uuid.uuid4().hex
                self._pending_interrupt = {"token": token, "prompt": prompt}
                self._cancel_requested = False
            self._abort_background(token, "interrupt")
        else:
            self.client.request("follow_up", {"message": prompt}, timeout=30)

    def cancel(self):
        if not self.client:
            return
        with self._control_lock:
            if self._cancel_requested:
                return
            token = uuid.uuid4().hex
            self._pending_interrupt = None
            self._cancel_requested = True
        self._abort_background(token, "cancel")

    def _abort_background(self, token, action):
        timeout = max(60, min(int(self.args.get("timeout") or 900), 3600))
        self.client.request_background(
            "abort",
            timeout=timeout,
            callback=lambda _data, error: self._on_abort_response(token, action, error),
        )

    def _on_abort_response(self, token, action, error):
        if error:
            self.backend.logger("Pi %s abort response failed: %s" % (action, error))
            return
        if action == "interrupt":
            # An already-idle Pi returns from abort without another settled event.
            self._resume_interrupt(token)
        else:
            # Normally agent_settled arrives before the abort response. This is
            # the fallback for an already-idle session or a missed native event.
            with self._control_lock:
                settled_event_seen = self._settled_event_seen
            if not settled_event_seen:
                with self._control_lock:
                    self._settled_event_seen = True
                self._start_settling()

    def _resume_interrupt(self, token):
        with self._control_lock:
            pending = self._pending_interrupt
            if not pending or pending["token"] != token:
                return
            self._pending_interrupt = None
            prompt = pending["prompt"]
        threading.Thread(
            target=self._send_interrupt_prompt, args=(prompt,), daemon=True
        ).start()

    def _send_interrupt_prompt(self, prompt):
        try:
            self.client.request("prompt", {"message": prompt}, timeout=30)
        except Exception as exc:  # noqa: BLE001
            self.emit({"type": "settled", "status": "failed", "error": bounded(exc)})

    def _start_settling(self):
        with self._control_lock:
            if self._settling:
                return
            self._settling = True
        threading.Thread(target=self._finish_settled, daemon=True).start()

    def branch(self, *, target_kind, target_id, turn_index):
        parent_thread = self.state.get("sessionFile") or self.state.get("sessionId")
        if not parent_thread:
            raise ProtocolError("Pi parent session is not ready")
        cwd = os.path.realpath(self.args.get("cwd") or os.getcwd())
        branch_client = PiRpcClient(
            self._build_command(cwd, parent_thread),
            cwd=cwd,
            env=self._build_env(cwd),
            logger=self.backend.logger,
        )
        try:
            branch_client.start()
            if target_kind in {"message", "checkpoint"}:
                if not target_id:
                    raise ControlPlaneError("targetId is required", "invalid_params")
                data = branch_client.request("fork", {"entryId": target_id}, timeout=60)
            else:
                data = branch_client.request("clone", timeout=60)
            state = branch_client.request("get_state", timeout=15)
            return {
                "threadId": state.get("sessionFile") or state.get("sessionId"),
                "parentThreadId": parent_thread,
                "cancelled": data.get("cancelled", False),
            }
        finally:
            branch_client.close()

    def context(self, *, action, instructions=None):
        if action not in {"inspect", "compact"}:
            raise ControlPlaneError("context action must be inspect or compact", "invalid_params")
        deployments = getattr(self.backend, "deployments", None)
        lease = deployments.acquire(self.args.get("model")) if deployments else None
        client = self.client
        temporary = None
        try:
            if client is None:
                parent_thread = self.state.get("sessionFile") or self.state.get("sessionId")
                if not parent_thread:
                    raise ProtocolError("Pi session is not ready")
                cwd = os.path.realpath(self.args.get("cwd") or os.getcwd())
                temporary = PiRpcClient(
                    self._build_command(cwd, parent_thread),
                    cwd=cwd,
                    env=self._build_env(cwd),
                    logger=self.backend.logger,
                )
                temporary.start()
                client = temporary
            if action == "inspect":
                stats = client.request("get_session_stats", timeout=30)
                return {"context": self._context(stats), "usage": self._usage(stats)}
            params = {"customInstructions": instructions} if instructions else None
            data = client.request("compact", params, timeout=180)
        finally:
            if temporary:
                temporary.close()
            if lease:
                lease.release()
        return {
            "context": {
                "tokensBefore": data.get("tokensBefore"),
                "estimatedTokensAfter": data.get("estimatedTokensAfter"),
            },
            "summary": bounded(data.get("summary"), 2000),
        }

    def close(self):
        self._closed = True
        self._hibernate()
        self._release_deployment()

    def _hibernate(self):
        """Drop the live Pi RPC process while retaining its durable session."""
        with self._control_lock:
            client = self.client
            self.client = None
        if client:
            client.close()

    def _release_deployment(self):
        lease = self._deployment_lease
        self._deployment_lease = None
        if lease:
            lease.release()

    def _on_event(self, event):
        kind = event.get("type")
        if kind == "agent_start":
            self.emit({"type": "model.started", "phase": "agent"})
        elif kind == "message_update":
            delta = event.get("assistantMessageEvent") or {}
            dtype = delta.get("type")
            if dtype == "thinking_start":
                self.emit({"type": "reasoning.started"})
            elif dtype == "thinking_end":
                self.emit({"type": "reasoning.ended"})
        elif kind == "message_end":
            message = event.get("message") or {}
            if message.get("role") == "assistant":
                self.emit({
                    "type": "message.completed",
                    "model": self._assistant_model(message),
                    "usage": self._assistant_usage(message),
                })
        elif kind == "tool_execution_start":
            self.emit({
                "type": "tool.started",
                "toolCallId": event.get("toolCallId"),
                "toolName": event.get("toolName"),
            })
        elif kind == "tool_execution_end":
            self.emit({
                "type": "tool.ended",
                "toolCallId": event.get("toolCallId"),
                "toolName": event.get("toolName"),
                "detail": {"isError": event.get("isError")},
            })
        elif kind == "auto_retry_start":
            self.emit({"type": "model.retrying", "phase": "retrying"})
        elif kind == "compaction_start":
            self.emit({"type": "context.compacting", "phase": "compacting"})
        elif kind == "extension_error":
            self.emit({"type": "extension.error", "error": event.get("error")})
        elif kind == "agent_settled":
            with self._control_lock:
                pending = self._pending_interrupt
            if pending:
                self._resume_interrupt(pending["token"])
            else:
                with self._control_lock:
                    self._settled_event_seen = True
                self._start_settling()
        else:
            self.emit({"type": "native.%s" % kind, "detail": {}})

    def _finish_settled(self):
        try:
            text = self.client.request("get_last_assistant_text", timeout=30).get("text") or ""
            stats = self.client.request("get_session_stats", timeout=30)
            state = self.client.request("get_state", timeout=30)
            messages = self.client.request("get_messages", timeout=30).get("messages") or []
            stop_reason = None
            error = None
            for message in reversed(messages):
                if message.get("role") == "assistant":
                    stop_reason = message.get("stopReason")
                    error = message.get("errorMessage")
                    break
            with self._control_lock:
                cancelled = self._cancel_requested
            status = "cancelled" if cancelled else (
                "failed" if stop_reason in {"error", "aborted"} else "completed"
            )
            self.emit({
                "type": "settled",
                "status": status,
                "result": text,
                "error": error,
                "threadId": state.get("sessionFile") or state.get("sessionId"),
                "model": self._model_projection(state),
                "usage": self._usage(stats),
                "context": self._context(stats),
            })
        except Exception as exc:  # noqa: BLE001
            self.emit({"type": "settled", "status": "failed", "error": bounded(exc)})
        finally:
            with self._control_lock:
                self._settling = False
            self._hibernate()
            self._release_deployment()

    @staticmethod
    def _assistant_model(message):
        return {
            "providerId": message.get("provider"),
            "modelId": message.get("model"),
            "status": "running",
        }

    @staticmethod
    def _assistant_usage(message):
        usage = message.get("usage") or {}
        return {
            "totalTokens": usage.get("totalTokens") or 0,
            "inputTokens": usage.get("input") or 0,
            "outputTokens": usage.get("output") or 0,
            "reasoningTokens": usage.get("reasoning") or 0,
            "cacheReadTokens": usage.get("cacheRead") or 0,
            "cacheWriteTokens": usage.get("cacheWrite") or 0,
        }

    @staticmethod
    def _model_projection(state):
        model = state.get("model") or {}
        return {
            "model": {
                "providerId": model.get("provider"),
                "modelId": model.get("id"),
            },
            "thoughtLevel": state.get("thinkingLevel"),
        }

    @staticmethod
    def _usage(stats):
        tokens = stats.get("tokens") or {}
        return {
            "totalTokens": tokens.get("total") or tokens.get("totalTokens") or 0,
            "inputTokens": tokens.get("input") or 0,
            "outputTokens": tokens.get("output") or 0,
            "reasoningTokens": tokens.get("reasoning") or 0,
            "cacheReadTokens": tokens.get("cacheRead") or 0,
            "cacheWriteTokens": tokens.get("cacheWrite") or 0,
            "modelRequests": stats.get("assistantMessages") or 0,
        }

    @staticmethod
    def _context(stats):
        usage = stats.get("contextUsage") or {}
        return {
            "used": usage.get("tokens"),
            "window": usage.get("contextWindow"),
            "usedRatio": (
                round(float(usage["percent"]) / 100.0, 4)
                if isinstance(usage.get("percent"), (int, float)) else None
            ),
        }


class PiBackend:
    name = "pi"

    def __init__(self, binary=None, *, session_dir=None, policy_extension=None,
                 deployments=None, logger=None):
        self.binary = binary
        self.session_dir = os.path.realpath(
            session_dir or os.environ.get("PI_BRIDGE_SESSION_DIR")
            or os.path.expanduser("~/.pi/agent/bridge-sessions")
        )
        self.policy_extension = os.path.realpath(
            policy_extension or os.path.join(os.path.dirname(__file__), "pi_bridge_extension.mjs")
        )
        self.logger = logger or (lambda _message: None)
        self.deployments = deployments or ModelDeploymentManager(logger=self.logger)

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
            "permissionProtocol": "tool-call-extension",
            "filesystemBoundary": "enforced-for-native-file-tools",
            "shellBoundary": "advisory-in-exclusive; denied-in-shared",
            "toolPolicy": "native-allow-and-deny-list",
        }

    def probe(self):
        try:
            self.binary = self.binary or discover_pi()
            result = subprocess.run(
                [self.binary, "--version"], capture_output=True, text=True, timeout=10
            )
            version = (result.stdout or result.stderr).strip().splitlines()[-1]
            return {"available": result.returncode == 0, "version": version, "binary": self.binary}
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "reason": bounded(exc, 500)}

    def create_runtime(self, args, emit, disconnect):
        self.binary = self.binary or discover_pi()
        return PiRuntime(self, args, emit, disconnect)

    def list_sessions(self, args):
        result = []
        for path in sorted(glob.glob(os.path.join(self.session_dir, "**", "*.jsonl"), recursive=True), reverse=True):
            try:
                with open(path, encoding="utf-8") as handle:
                    header = json.loads(handle.readline())
                stat = os.stat(path)
                result.append({
                    "threadId": path,
                    "sessionId": header.get("id"),
                    "cwd": header.get("cwd"),
                    "createdAt": header.get("timestamp"),
                    "updatedAtMs": int(stat.st_mtime * 1000),
                    "status": "idle",
                })
            except (OSError, ValueError):
                continue
            if len(result) >= min(max(int(args.get("limit", 20)), 1), 100):
                break
        return result

    def close(self):
        self.deployments.close()

    def control_plane(self, *, max_concurrency=0, logger=None):
        return AdapterControlPlane(
            self, max_concurrency=max_concurrency, logger=logger or self.logger
        )
