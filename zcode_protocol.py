"""Small NDJSON client for ZCode's bundled ``app-server`` protocol.

The client deliberately keeps ZCode protocol payloads private to the bridge.
Callers receive parsed dictionaries and compact them before exposing anything
through MCP.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import threading
import time
import uuid


class ProtocolError(RuntimeError):
    def __init__(self, message, code=-32603, data=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.data = data


def resolve_runtime_model(model_ref=None, thought_level=None, *, config_path=None,
                          settings_db_path=None):
    """Build ZCode's secret-bearing runtime model locally for cold resumes.

    The app-server intentionally omits provider secrets from workspace state.
    A persisted session therefore needs this runtime payload after the server
    process restarts.  The payload is passed only to ZCode and is never exposed
    through MCP state or logs.
    """
    config_path = config_path or os.path.expanduser("~/.zcode/cli/config.json")
    try:
        with open(config_path, encoding="utf-8") as handle:
            config = json.load(handle)
    except (OSError, TypeError, ValueError):
        return None

    if isinstance(model_ref, dict):
        provider_id = model_ref.get("providerId")
        model_id = model_ref.get("modelId")
    else:
        selected = ((config.get("model") or {}).get("main") or "").strip()
        provider_id, separator, model_id = selected.partition("/")
        if not separator:
            return None
    providers = config.get("provider") or {}
    provider = providers.get(provider_id)
    if not isinstance(provider, dict):
        return None
    models = provider.get("models") or {}
    model = models.get(model_id)
    if not isinstance(model, dict):
        return None

    if not thought_level:
        settings_db_path = settings_db_path or os.path.expanduser("~/.zcode/cli/db/db.sqlite")
        try:
            connection = sqlite3.connect(
                "file:%s?mode=ro" % settings_db_path, uri=True, timeout=1
            )
            try:
                row = connection.execute(
                    "select value from local_setting where key='reasoningLevel'"
                ).fetchone()
            finally:
                connection.close()
            value = json.loads(row[0]) if row else {}
            thought_level = value.get("level") if isinstance(value, dict) else None
        except (OSError, sqlite3.Error, TypeError, ValueError):
            thought_level = None

    options = provider.get("options") if isinstance(provider.get("options"), dict) else {}
    limits = model.get("limit") if isinstance(model.get("limit"), dict) else {}
    modalities = model.get("modalities") if isinstance(model.get("modalities"), dict) else {}
    inputs = modalities.get("input") if isinstance(modalities.get("input"), list) else []
    runtime_model = {
        "revision": "bridge:%s" % uuid.uuid4().hex,
        "generatedAt": int(time.time() * 1000),
        "model": {"providerId": provider_id, "modelId": model_id},
        "provider": {
            "providerId": provider_id,
            "kind": provider.get("kind") or "openai-compatible",
            "label": provider.get("name") or provider_id,
            "source": "workspace",
            "models": [{
                "modelId": model_id,
                "label": model.get("name") or model_id,
                **({"contextWindow": limits["context"]} if limits.get("context") else {}),
                **({"maxOutputTokens": limits["output"]} if limits.get("output") else {}),
                **({"supportsImages": "image" in inputs} if modalities else {}),
                **({"supportsPdf": "pdf" in inputs} if modalities else {}),
            }],
        },
    }
    native_provider = runtime_model["provider"]
    for target, source in (
        ("baseURL", "baseURL"),
        ("apiFormat", "apiFormat"),
        ("apiKeyRequired", "apiKeyRequired"),
        ("headers", "headers"),
    ):
        value = options.get(source)
        if value is not None:
            native_provider[target] = value
    api_key = options.get("apiKey")
    if isinstance(api_key, str) and api_key:
        native_provider["apiKey"] = {"source": "inline", "value": api_key}

    reasoning = model.get("reasoning") if isinstance(model.get("reasoning"), dict) else None
    if reasoning:
        variants = reasoning.get("variants") or []
        native_reasoning = {
            "enabled": bool(reasoning.get("enabled", True)),
            "levels": [{"value": str(item), "label": str(item)} for item in variants],
        }
        if reasoning.get("defaultVariant"):
            native_reasoning["defaultLevel"] = reasoning["defaultVariant"]
        native_provider["models"][0]["reasoning"] = native_reasoning
    if thought_level:
        runtime_model["thoughtLevel"] = thought_level
    return runtime_model


class ZCodeProtocolClient:
    """Persistent client for ``zcode app-server``.

    ZCode may issue reverse requests while materializing or running a session.
    The transport answers the non-interactive runtime preference request and
    delegates interaction policy to the control plane so a headless run never
    hangs waiting for an invisible dialog.
    """

    def __init__(self, zcode_bin, zcode_bundle, *, on_notification=None,
                 on_disconnect=None, on_server_request=None, logger=None):
        self.zcode_bin = zcode_bin
        self.zcode_bundle = zcode_bundle
        self.on_notification = on_notification
        self.on_disconnect = on_disconnect
        self.on_server_request = on_server_request
        self.logger = logger or (lambda _msg: None)
        self._proc = None
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._pending = {}
        self._next_id = 0
        self._closed = False

    def start(self):
        with self._state_lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            if self._closed:
                raise ProtocolError("ZCode protocol client is closed")
            env = dict(os.environ)
            env["ELECTRON_RUN_AS_NODE"] = "1"
            env["NO_COLOR"] = "1"
            env.pop("ZCODE_MODEL", None)
            popen_options = {}
            if os.name == "nt":
                creation_flag = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                if creation_flag:
                    popen_options["creationflags"] = creation_flag
            else:
                popen_options["start_new_session"] = True
            self._proc = subprocess.Popen(
                [self.zcode_bin, self.zcode_bundle, "app-server"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                **popen_options,
            )
            threading.Thread(target=self._read_stdout, daemon=True).start()
            threading.Thread(target=self._read_stderr, daemon=True).start()

    def request(self, method, params=None, timeout=30):
        self.start()
        with self._state_lock:
            self._next_id += 1
            request_id = "bridge-%s" % self._next_id
            event = threading.Event()
            self._pending[request_id] = {"event": event, "message": None}
        try:
            self._send({"id": request_id, "method": method, "params": params or {}})
        except Exception:
            with self._state_lock:
                self._pending.pop(request_id, None)
            raise
        if not event.wait(timeout):
            with self._state_lock:
                self._pending.pop(request_id, None)
            raise ProtocolError(
                "ZCode protocol request timed out: %s" % method,
                code=-32022,
                data={"method": method, "timeoutSeconds": timeout},
            )
        with self._state_lock:
            entry = self._pending.pop(request_id, None)
        message = (entry or {}).get("message") or {}
        if "error" in message:
            error = message.get("error") or {}
            raise ProtocolError(
                error.get("message") or "ZCode protocol error",
                code=error.get("code", -32603),
                data=error.get("data"),
            )
        return message.get("result") or {}

    def close(self):
        with self._state_lock:
            self._closed = True
            proc = self._proc
            self._proc = None
        if proc is not None and proc.poll() is None:
            if os.name == "nt":
                proc.terminate()
            else:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except (OSError, ProcessLookupError, TypeError):
                    proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    proc.kill()
                else:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except (OSError, ProcessLookupError, TypeError):
                        proc.kill()
        self._fail_pending("ZCode app-server closed")

    def _send(self, message):
        payload = json.dumps(message, ensure_ascii=False)
        with self._write_lock:
            proc = self._proc
            if proc is None or proc.poll() is not None or proc.stdin is None:
                raise ProtocolError("ZCode app-server is not running")
            proc.stdin.write(payload + "\n")
            proc.stdin.flush()

    def _read_stdout(self):
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in proc.stdout:
            try:
                message = json.loads(line)
            except (TypeError, ValueError):
                self.logger("protocol invalid JSON: %s" % line[-500:])
                continue
            if "id" in message and ("result" in message or "error" in message):
                request_id = str(message.get("id"))
                with self._state_lock:
                    entry = self._pending.get(request_id)
                    if entry is not None:
                        entry["message"] = message
                        entry["event"].set()
                continue
            if "id" in message and "method" in message:
                self._handle_server_request(message)
                continue
            callback = self.on_notification
            if callback is not None:
                try:
                    callback(message)
                except Exception as exc:  # notification handling must not kill transport
                    self.logger("notification handler failed: %s" % exc)
        self._fail_pending("ZCode app-server exited")
        with self._state_lock:
            unexpected = not self._closed
        if unexpected and self.on_disconnect is not None:
            try:
                self.on_disconnect("ZCode app-server exited")
            except Exception as exc:
                self.logger("disconnect handler failed: %s" % exc)

    def _read_stderr(self):
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            text = line.strip()
            if text:
                self.logger("protocol stderr: %s" % text[-1000:])

    def _handle_server_request(self, message):
        method = message.get("method")
        request_id = message.get("id")
        if method == "session/requestRuntimePreferences":
            self._send({
                "id": request_id,
                "result": {"nativeSearchEnhancementsEnabled": False},
            })
            return
        callback = self.on_server_request
        if callback is None:
            self._send({
                "id": request_id,
                "error": {
                    "code": -32030,
                    "message": "Headless bridge cannot satisfy interaction: %s" % method,
                },
            })
            return
        try:
            result = callback(method, message.get("params") or {})
            if not isinstance(result, dict):
                raise ProtocolError(
                    "Headless interaction handler returned an invalid response",
                    code=-32603,
                )
            self._send({"id": request_id, "result": result})
        except ProtocolError as exc:
            error = {"code": exc.code, "message": exc.message}
            if exc.data is not None:
                error["data"] = exc.data
            self._send({"id": request_id, "error": error})
        except Exception as exc:
            self._send({
                "id": request_id,
                "error": {
                    "code": -32603,
                    "message": "Headless interaction handler failed: %s" % exc,
                },
            })

    def _fail_pending(self, message):
        with self._state_lock:
            entries = list(self._pending.values())
            for entry in entries:
                entry["message"] = {
                    "error": {"code": -32020, "message": message}
                }
                entry["event"].set()


def now_ms():
    return int(time.time() * 1000)
