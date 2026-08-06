"""Backend registry and per-MCP-connection backend selection."""

from __future__ import annotations

import os
import threading

from control_plane import ControlPlaneError, ZCodeControlPlane
from opencode_backend import OpenCodeBackend
from pi_backend import PiBackend


ZCODE_CAPABILITIES = {
    "prompt": True,
    "durableGoal": True,
    "guidance": [
        "guide", "interrupt", "cancel", "cancel-background",
        "pause-goal", "resume-goal",
    ],
    "reasoningEvents": True,
    "usage": "exact",
    "branch": True,
    "compact": True,
    "backgroundTasks": True,
    "permissionProtocol": "reverse-request",
    "filesystemBoundary": "structured-permission-roots",
    "shellBoundary": "advisory",
    "toolPolicy": "native-allow-and-deny-list",
}


class ZCodeBackend:
    name = "zcode"
    capabilities = ZCODE_CAPABILITIES

    def __init__(self, discover, *, max_concurrency=0, logger=None):
        self.discover = discover
        self.max_concurrency = max_concurrency
        self.logger = logger or (lambda _message: None)
        self.paths = None

    def probe(self):
        try:
            self.paths = self.paths or self.discover()
            return {
                "available": True,
                "version": "desktop-bundled",
                "binary": self.paths[0],
            }
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "reason": str(exc)[:500]}

    def control_plane(self):
        self.paths = self.paths or self.discover()
        return ZCodeControlPlane(
            self.paths[0], self.paths[1],
            max_concurrency=self.max_concurrency,
            logger=self.logger,
        )


class BackendManager:
    """Route runs to the backend selected for this MCP server connection."""

    def __init__(self, zcode_discover, *, max_concurrency=0, logger=None,
                 backends=None, default_backend=None):
        self.logger = logger or (lambda _message: None)
        self.max_concurrency = max_concurrency
        self.default_backend = (
            default_backend or os.environ.get("AGENT_MCP_DEFAULT_BACKEND") or "zcode"
        ).lower()
        if backends is None:
            backends = {
                "zcode": ZCodeBackend(
                    zcode_discover, max_concurrency=max_concurrency, logger=self.logger
                ),
                "opencode": OpenCodeBackend(logger=self.logger),
                "pi": PiBackend(logger=self.logger),
            }
        self.backends = dict(backends)
        if self.default_backend not in self.backends:
            self.default_backend = "zcode" if "zcode" in self.backends else next(iter(self.backends))
        self.selected_backend = self.default_backend
        self.selection_source = "default"
        self._controls = {}
        self._run_backends = {}
        self._lock = threading.RLock()

    def configure(self, args):
        action = args.get("action") or "get"
        if action == "list":
            return self._configuration(include_all=True)
        if action == "get":
            return self._configuration(include_all=False)
        if action == "reset":
            with self._lock:
                self.selected_backend = self.default_backend
                self.selection_source = "default"
            return self._configuration(include_all=False)
        if action != "set":
            raise ControlPlaneError("unknown agent-config action", "invalid_params")
        backend_name = str(args.get("backend") or "").lower()
        if backend_name not in self.backends:
            raise ControlPlaneError("unknown backend: %s" % backend_name, "invalid_backend")
        probe = self.backends[backend_name].probe()
        if not probe.get("available"):
            raise ControlPlaneError(
                "backend %s is unavailable: %s" % (backend_name, probe.get("reason") or "probe failed"),
                "backend_unavailable",
                data=probe,
            )
        with self._lock:
            self.selected_backend = backend_name
            self.selection_source = "task"
        result = self._configuration(include_all=False)
        result["probe"] = probe
        return result

    def start(self, args):
        with self._lock:
            backend_name = self.selected_backend
        control = self._control(backend_name)
        result = control.start(args)
        self._remember(result, backend_name)
        return self._annotate(result, backend_name)

    def recover(self, args):
        with self._lock:
            backend_name = self.selected_backend
        control = self._control(backend_name)
        result = control.recover(args)
        if args.get("adoptThreadId"):
            self._remember(result, backend_name)
        return self._annotate(result, backend_name)

    def wait(self, run_id, **kwargs):
        backend_name, control = self._run_control(run_id)
        return self._annotate(control.wait(run_id, **kwargs), backend_name)

    def observe(self, run_id, **kwargs):
        backend_name, control = self._run_control(run_id)
        return self._annotate(control.observe(run_id, **kwargs), backend_name)

    def control(self, run_id, action, **kwargs):
        backend_name, control = self._run_control(run_id)
        return self._annotate(control.control(run_id, action, **kwargs), backend_name)

    def branch(self, run_id, **kwargs):
        backend_name, control = self._run_control(run_id)
        return self._annotate(control.branch(run_id, **kwargs), backend_name)

    def context(self, run_id, **kwargs):
        backend_name, control = self._run_control(run_id)
        return self._annotate(control.context(run_id, **kwargs), backend_name)

    def close_run(self, run_id=None, *, thread_id=None):
        if run_id:
            backend_name, control = self._run_control(run_id)
        else:
            with self._lock:
                backend_name = self.selected_backend
            control = self._control(backend_name)
        result = control.close_run(run_id, thread_id=thread_id)
        return self._annotate(result, backend_name)

    def close(self):
        with self._lock:
            controls = list(self._controls.values())
            self._controls.clear()
        for control in controls:
            try:
                control.close()
            except Exception as exc:  # noqa: BLE001
                self.logger("backend control close failed: %s" % exc)

    def _control(self, backend_name):
        with self._lock:
            control = self._controls.get(backend_name)
            if control is not None:
                return control
            backend = self.backends[backend_name]
            if backend_name == "zcode":
                control = backend.control_plane()
            else:
                control = backend.control_plane(
                    max_concurrency=self.max_concurrency, logger=self.logger
                )
            self._controls[backend_name] = control
            return control

    def _run_control(self, run_id):
        with self._lock:
            backend_name = self._run_backends.get(run_id)
            controls = dict(self._controls)
        if backend_name:
            return backend_name, self._control(backend_name)
        for name, control in controls.items():
            owns = getattr(control, "owns", None)
            if owns and owns(run_id):
                with self._lock:
                    self._run_backends[run_id] = name
                return name, control
        raise ControlPlaneError("run not found: %s" % run_id, "run_not_found")

    def _remember(self, result, backend_name):
        run_id = result.get("runId") if isinstance(result, dict) else None
        if run_id:
            with self._lock:
                self._run_backends[run_id] = backend_name

    def _configuration(self, *, include_all):
        with self._lock:
            selected = self.selected_backend
            source = self.selection_source
        selected_backend = self.backends[selected]
        result = {
            "selectedBackend": selected,
            "source": source,
            "capabilities": dict(selected_backend.capabilities),
        }
        if include_all:
            result["availableBackends"] = {
                name: {**backend.probe(), "capabilities": dict(backend.capabilities)}
                for name, backend in self.backends.items()
            }
        return result

    @staticmethod
    def _annotate(result, backend_name):
        if isinstance(result, dict):
            result.setdefault("backend", backend_name)
        return result
