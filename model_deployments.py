"""On-demand lifecycle management for locally served model deployments.

Deployments are opt-in and selected by the exact Pi provider/model pair.  The
configuration contains argv arrays rather than shell strings so loading a
local model never adds an implicit shell-evaluation boundary to the bridge.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.request
import uuid

from resource_leases import ResourceLeaseStore


DEFAULT_CONFIG_PATH = os.path.expanduser(
    "~/.config/coder-mcp-bridge/model-deployments.json"
)


class DeploymentLease:
    def __init__(self, manager, key, token):
        self.manager = manager
        self.key = key
        self.token = token
        self._released = False

    def release(self):
        if self._released:
            return
        self._released = True
        self.manager.release(self.key, self.token)


class ModelDeploymentManager:
    """Start matched model servers on demand and stop them after idle time."""

    def __init__(self, config_path=None, *, logger=None, run=None, opener=None,
                 lease_store=None):
        self.logger = logger or (lambda _message: None)
        self.config_path = os.path.realpath(
            os.path.expanduser(
                config_path
                or os.environ.get("PI_MODEL_DEPLOYMENTS_FILE")
                or DEFAULT_CONFIG_PATH
            )
        )
        self._run = run or subprocess.run
        self._opener = opener or urllib.request.urlopen
        self._cv = threading.Condition(threading.RLock())
        self._states = {}
        self._closed = False
        self._lease_store = lease_store or ResourceLeaseStore()
        self.deployments = self._load()

    def _load(self):
        if not os.path.isfile(self.config_path):
            return {}
        with open(self.config_path, encoding="utf-8") as handle:
            document = json.load(handle)
        raw = document.get("deployments") if isinstance(document, dict) else None
        if not isinstance(raw, dict):
            raise RuntimeError("model deployment config requires an object named deployments")
        result = {}
        for key, value in raw.items():
            if not isinstance(key, str) or "/" not in key or not isinstance(value, dict):
                raise RuntimeError("invalid model deployment entry: %r" % key)
            start = value.get("start")
            stop = value.get("stop")
            health_url = value.get("healthUrl")
            if not self._argv(start) or not self._argv(stop) or not isinstance(health_url, str):
                raise RuntimeError(
                    "deployment %s requires start/stop argv arrays and healthUrl" % key
                )
            result[key] = {
                **value,
                "start": list(start),
                "stop": list(stop),
                "startupTimeoutSeconds": min(
                    max(int(value.get("startupTimeoutSeconds", 1800)), 1), 3600
                ),
                "idleTimeoutSeconds": min(
                    max(int(value.get("idleTimeoutSeconds", 600)), 0), 86400
                ),
                "commandTimeoutSeconds": min(
                    max(int(value.get("commandTimeoutSeconds", 180)), 1), 1800
                ),
                "stopOnBridgeExit": value.get("stopOnBridgeExit", True) is not False,
            }
        return result

    @staticmethod
    def _argv(value):
        return (
            isinstance(value, list)
            and bool(value)
            and all(isinstance(part, str) and part for part in value)
        )

    @staticmethod
    def model_key(model):
        if not isinstance(model, dict):
            return None
        provider = model.get("providerId")
        model_id = model.get("modelId")
        if not provider or not model_id:
            return None
        return "%s/%s" % (provider, model_id)

    def configured(self, model):
        key = self.model_key(model)
        return key if key in self.deployments else None

    def acquire(self, model):
        key = self.configured(model)
        if not key:
            return None
        token = uuid.uuid4().hex
        config = self.deployments[key]
        resource = self._resource_key(key)
        while True:
            lease = self._lease_store.try_acquire(token, {resource: "shared"})
            if lease.get("acquired"):
                break
            with self._cv:
                if self._closed:
                    raise RuntimeError("model deployment manager is closed")
            self._lease_store.wait_poll()
        with self._cv:
            if self._closed:
                self._lease_store.release(token)
                raise RuntimeError("model deployment manager is closed")
            state = self._states.setdefault(
                key,
                {"holders": set(), "starting": False, "error": None, "generation": 0},
            )
            state["holders"].add(token)
            state["generation"] += 1
            while state["starting"]:
                self._cv.wait()
                if state.get("error"):
                    state["holders"].discard(token)
                    self._lease_store.release(token)
                    raise RuntimeError(state["error"])
            if self._healthy(config):
                state["error"] = None
                return DeploymentLease(self, key, token)
            state["starting"] = True
            state["error"] = None

        error = None
        try:
            self._command(config["start"], config, "start")
            self._wait_healthy(key, config)
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
            try:
                self._command(config["stop"], config, "rollback stop")
            except Exception as stop_exc:  # noqa: BLE001
                self.logger("model deployment rollback failed for %s: %s" % (key, stop_exc))
        with self._cv:
            state = self._states[key]
            state["starting"] = False
            state["error"] = error
            if error:
                state["holders"].discard(token)
            self._cv.notify_all()
        if error:
            self._lease_store.release(token)
            raise RuntimeError(error)
        return DeploymentLease(self, key, token)

    def release(self, key, token):
        schedule = False
        generation = None
        delay = 0
        with self._cv:
            state = self._states.get(key)
            if state:
                state["holders"].discard(token)
                state["generation"] += 1
                generation = state["generation"]
                if not state["holders"] and not self._closed:
                    schedule = True
                    delay = self.deployments[key]["idleTimeoutSeconds"]
        self._lease_store.release(token)
        if not schedule:
            return
        timer = threading.Timer(delay, self._stop_if_idle, args=(key, generation))
        timer.daemon = True
        timer.start()

    def _stop_if_idle(self, key, generation):
        with self._cv:
            state = self._states.get(key)
            if (
                not state
                or state["holders"]
                or state["starting"]
                or state["generation"] != generation
                or self._closed
            ):
                return
        stop_token = "deployment-stop-" + uuid.uuid4().hex
        exclusive = self._lease_store.try_acquire(
            stop_token, {self._resource_key(key): "exclusive"}
        )
        if not exclusive.get("acquired"):
            return
        try:
            with self._cv:
                state = self._states.get(key)
                if (
                    not state
                    or state["holders"]
                    or state["starting"]
                    or state["generation"] != generation
                    or self._closed
                ):
                    return
                state["generation"] += 1
            self._command(self.deployments[key]["stop"], self.deployments[key], "stop")
            self.logger("stopped idle model deployment %s" % key)
        except Exception as exc:  # noqa: BLE001
            self.logger("failed to stop idle model deployment %s: %s" % (key, exc))
        finally:
            self._lease_store.release(stop_token)

    @staticmethod
    def _resource_key(key):
        return "model-deployment:%s" % key

    def _wait_healthy(self, key, config):
        deadline = time.monotonic() + config["startupTimeoutSeconds"]
        last_error = "health endpoint did not respond"
        while time.monotonic() < deadline:
            if self._healthy(config):
                self.logger("model deployment ready: %s" % key)
                return
            time.sleep(1)
        raise RuntimeError(
            "model deployment %s did not become healthy within %ss: %s"
            % (key, config["startupTimeoutSeconds"], last_error)
        )

    def _healthy(self, config):
        try:
            response = self._opener(config["healthUrl"], timeout=2)
            try:
                status = getattr(response, "status", 200)
                return 200 <= int(status) < 300
            finally:
                close = getattr(response, "close", None)
                if close:
                    close()
        except Exception:  # noqa: BLE001
            return False

    def _command(self, argv, config, action):
        cwd = config.get("cwd")
        if cwd:
            cwd = os.path.realpath(os.path.expanduser(cwd))
        result = self._run(
            argv,
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=config["commandTimeoutSeconds"],
        )
        if result.returncode:
            detail = (result.stderr or result.stdout or "command failed").strip()
            raise RuntimeError("model deployment %s failed: %s" % (action, detail[-1600:]))

    def close(self):
        with self._cv:
            if self._closed:
                return
            self._closed = True
            targets = [
                key for key, state in self._states.items()
                if not state["holders"] and self.deployments[key]["stopOnBridgeExit"]
            ]
        for key in targets:
            stop_token = "deployment-close-" + uuid.uuid4().hex
            try:
                exclusive = self._lease_store.try_acquire(
                    stop_token, {self._resource_key(key): "exclusive"}
                )
                if exclusive.get("acquired"):
                    self._command(
                        self.deployments[key]["stop"], self.deployments[key], "stop"
                    )
            except Exception as exc:  # noqa: BLE001
                self.logger("failed to stop model deployment on bridge exit %s: %s" % (key, exc))
            finally:
                self._lease_store.release(stop_token)
        self._lease_store.close()
