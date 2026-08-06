"""Backend-neutral run scheduling for coding-agent adapters.

The native agents intentionally keep their own session protocols.  This module
owns only the contract shared by every backend: asynchronous starts, resource
leases, bounded observations, control dispatch, timeout handling and cleanup.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections import deque

from control_plane import ControlPlaneError
from resource_leases import ResourceLeaseStore


TERMINAL_STATES = {"completed", "failed", "cancelled", "timed_out", "closed"}
MAX_RESULT = 12000


def now_ms():
    return int(time.time() * 1000)


def bounded(value, limit=1200):
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "..."


def normalize_resources(cwd, workspace_access, resources):
    result = {os.path.realpath(cwd): workspace_access}
    for item in resources or []:
        if not isinstance(item, dict) or not str(item.get("key") or "").strip():
            raise ControlPlaneError("resource key must be non-empty", "invalid_params")
        key = str(item["key"]).strip()
        if os.path.isabs(key):
            key = os.path.realpath(key)
        mode = item.get("mode") or "exclusive"
        if mode not in {"shared", "exclusive"}:
            raise ControlPlaneError("resource mode must be shared or exclusive", "invalid_params")
        previous = result.get(key)
        result[key] = "exclusive" if "exclusive" in {previous, mode} else mode
    return result


class AgentRun:
    def __init__(self, backend, args, *, recovered=False):
        self.run_id = "run_" + uuid.uuid4().hex
        self.backend = backend
        self.prompt = str(args.get("prompt") or "")
        self.cwd = os.path.realpath(args.get("cwd") or os.getcwd())
        self.thread_id = args.get("threadId")
        self.workspace_access = args.get("workspaceAccess") or "exclusive"
        self.resource_modes = normalize_resources(
            self.cwd, self.workspace_access, args.get("resources")
        )
        self.timeout_seconds = int(args.get("timeout") or 900)
        self.args = dict(args)
        self.recovered = recovered
        self.runtime = None
        self.status = "queued"
        self.phase = "waiting-for-resource"
        self.revision = 1
        self.seq = 0
        self.created_ms = now_ms()
        self.started_ms = None
        self.finished_ms = None
        self.error = None
        self.result = ""
        self.events = deque(maxlen=200)
        self.model = {
            "status": "idle",
            "reasoningActive": False,
            "thoughtLevel": args.get("thoughtLevel"),
            "model": args.get("model"),
        }
        self.usage = {
            "totalTokens": 0,
            "inputTokens": 0,
            "outputTokens": 0,
            "reasoningTokens": 0,
            "cacheReadTokens": 0,
            "cacheWriteTokens": 0,
            "modelRequests": 0,
            "modelErrors": 0,
        }
        self.context = {}
        self.active_tools = {}
        self.counts = {"toolCalls": 0, "subagents": 0}
        self.subagents = {"running": [], "ended": [], "endedTotal": 0}
        self.pending_guidance = deque()
        self.cancel_requested = False
        self.timeout_requested = False
        self.lease_acquired = False
        self.lease_blockers = []
        self.released = False
        self.closed = False


class AdapterControlPlane:
    """Schedule runs for one backend whose runtimes emit normalized events."""

    def __init__(self, backend, *, max_concurrency=0, lease_store=None, logger=None):
        self.backend = backend
        self.backend_name = backend.name
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._runs = {}
        self._queue = []
        self._active = set()
        self._max_concurrency = max(0, int(max_concurrency))
        self._lease_store = lease_store or ResourceLeaseStore()
        self._logger = logger or (lambda _message: None)
        self._closed = False

    def start(self, args):
        prompt = args.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ControlPlaneError(
                "%s requires one non-empty prompt" % self.backend_name,
                "invalid_params",
            )
        if args.get("goal"):
            raise ControlPlaneError(
                "%s does not support native durable goals" % self.backend_name,
                "unsupported_capability",
            )
        self._validate(args)
        run = AgentRun(self.backend_name, args)
        return self._enqueue(run)

    def recover(self, args):
        thread_id = args.get("adoptThreadId")
        sessions = self.backend.list_sessions(args)
        if not thread_id:
            return {"backend": self.backend_name, "sessions": sessions, "count": len(sessions)}
        match = next(
            (item for item in sessions if str(item.get("threadId")) == str(thread_id)),
            None,
        )
        if not match:
            raise ControlPlaneError("session not found: %s" % thread_id, "session_not_found")
        normalized = dict(args)
        normalized["threadId"] = thread_id
        normalized["cwd"] = args.get("cwd") or match.get("cwd") or os.getcwd()
        normalized.setdefault("timeout", 900)
        run = AgentRun(self.backend_name, normalized, recovered=True)
        return self._enqueue(run)

    def wait(self, run_id, *, after_revision=0, timeout_ms=30000, result_chars=2000):
        timeout_ms = min(max(int(timeout_ms), 0), 60000)
        deadline = time.monotonic() + timeout_ms / 1000.0
        with self._cv:
            run = self._get(run_id)
            while run.revision <= int(after_revision) and run.status not in TERMINAL_STATES:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cv.wait(remaining)
            result = self._snapshot(run, result_chars=result_chars)
            result["changed"] = run.revision > int(after_revision)
            return result

    def observe(self, run_id, *, refresh=True, after_seq=0, max_events=12,
                result_chars=2000):
        refresh_error = None
        if refresh:
            try:
                with self._lock:
                    runtime = self._get(run_id).runtime
                if runtime:
                    runtime.refresh()
            except Exception as exc:  # noqa: BLE001
                refresh_error = bounded(exc, 800)
        with self._lock:
            run = self._get(run_id)
            result = self._snapshot(run, result_chars=result_chars)
            max_events = min(max(int(max_events), 0), 30)
            oldest = run.events[0]["seq"] if run.events else run.seq + 1
            result["eventsDropped"] = max(0, oldest - int(after_seq) - 1)
            selected = [event for event in run.events if event["seq"] > int(after_seq)]
            result["events"] = selected[:max_events]
            result["nextSeq"] = (
                selected[min(len(selected), max_events) - 1]["seq"]
                if selected and max_events else int(after_seq)
            )
            result["hasMoreEvents"] = len(selected) > max_events
            if refresh_error:
                result["nativeRefreshError"] = refresh_error
            return result

    def control(self, run_id, action, *, prompt=None, task_id=None,
                if_revision=None, if_status=None):
        with self._lock:
            run = self._get(run_id)
            if if_revision is not None and run.revision != int(if_revision):
                raise ControlPlaneError("stale run revision", "stale_control")
            if if_status is not None and run.status != if_status:
                raise ControlPlaneError("stale run status", "stale_control")
            runtime = run.runtime
        if action in {"guide", "interrupt"}:
            if not isinstance(prompt, str) or not prompt.strip():
                raise ControlPlaneError("prompt is required", "invalid_params")
            if run.status in TERMINAL_STATES:
                raise ControlPlaneError(
                    "run is terminal; start a new run with threadId to reacquire resources",
                    "run_terminal",
                )
            if not runtime:
                raise ControlPlaneError("run runtime is not ready", "session_not_ready")
            runtime.guide(prompt, interrupt=(action == "interrupt"))
            with self._cv:
                run = self._get(run_id)
                self._event(run, "control.%s" % action, {})
            return self.snapshot(run_id, result_chars=0)
        if action == "cancel":
            return self.cancel(run_id)
        raise ControlPlaneError(
            "%s does not support control action %s" % (self.backend_name, action),
            "unsupported_capability",
        )

    def branch(self, run_id, *, target_kind="latestCheckpoint", target_id=None,
               turn_index=None):
        with self._lock:
            run = self._get(run_id)
            if run.status not in TERMINAL_STATES or run.status == "closed":
                raise ControlPlaneError("branch requires a terminal run", "run_active")
            runtime = run.runtime
        if not runtime:
            raise ControlPlaneError("session runtime is not available", "session_not_ready")
        return runtime.branch(
            target_kind=target_kind, target_id=target_id, turn_index=turn_index
        )

    def context(self, run_id, *, action="inspect", instructions=None):
        with self._lock:
            run = self._get(run_id)
            runtime = run.runtime
            if action == "compact" and run.status not in TERMINAL_STATES:
                raise ControlPlaneError("compact requires a terminal run", "run_active")
        if not runtime:
            raise ControlPlaneError("session runtime is not available", "session_not_ready")
        native = runtime.context(action=action, instructions=instructions)
        with self._cv:
            run = self._get(run_id)
            if isinstance(native, dict) and isinstance(native.get("context"), dict):
                run.context.update(native["context"])
            self._event(run, "context.%s" % action, {})
        return {"runId": run_id, "threadId": run.thread_id, **(native or {})}

    def close_run(self, run_id=None, *, thread_id=None):
        if not run_id:
            with self._lock:
                matches = [
                    run.run_id for run in self._runs.values()
                    if str(run.thread_id) == str(thread_id)
                ]
            if not matches:
                raise ControlPlaneError(
                    "%s has no managed runtime for threadId %s"
                    % (self.backend_name, thread_id),
                    "run_not_found",
                )
            run_id = matches[-1]
        with self._cv:
            run = self._get(run_id)
            runtime = run.runtime
        if runtime:
            runtime.close()
        with self._cv:
            run = self._get(run_id)
            run.closed = True
            run.status = "closed"
            run.phase = "closed"
            run.finished_ms = run.finished_ms or now_ms()
            self._release(run)
            self._event(run, "session.closed", {})
            return self._snapshot(run, result_chars=0)

    def cancel(self, run_id):
        with self._cv:
            run = self._get(run_id)
            if run.status in TERMINAL_STATES:
                return self._snapshot(run, result_chars=0)
            run.cancel_requested = True
            runtime = run.runtime
            if run.status == "queued":
                run.status = "cancelled"
                run.phase = "terminal"
                run.finished_ms = now_ms()
                self._release(run)
                self._event(run, "run.cancelled", {})
                return self._snapshot(run, result_chars=0)
            run.phase = "cancelling"
            self._event(run, "run.cancel-requested", {})
        if runtime:
            runtime.cancel()
        return self.snapshot(run_id, result_chars=0)

    def snapshot(self, run_id, *, result_chars=2000):
        with self._lock:
            return self._snapshot(self._get(run_id), result_chars=result_chars)

    def owns(self, run_id):
        with self._lock:
            return run_id in self._runs

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            runtimes = [run.runtime for run in self._runs.values() if run.runtime]
        for runtime in runtimes:
            try:
                runtime.close()
            except Exception as exc:  # noqa: BLE001
                self._logger("%s runtime close failed: %s" % (self.backend_name, exc))
        self._lease_store.close()
        self.backend.close()

    def _validate(self, args):
        cwd = os.path.realpath(args.get("cwd") or os.getcwd())
        if not os.path.isdir(cwd):
            raise ControlPlaneError("cwd is not a directory: %s" % cwd, "invalid_params")
        if (args.get("workspaceAccess") or "exclusive") not in {"shared", "exclusive"}:
            raise ControlPlaneError("invalid workspaceAccess", "invalid_params")
        timeout = int(args.get("timeout") or 900)
        if timeout < 1 or timeout > 86400:
            raise ControlPlaneError("timeout must be 1..86400 seconds", "invalid_params")
        normalize_resources(cwd, args.get("workspaceAccess") or "exclusive", args.get("resources"))

    def _enqueue(self, run):
        with self._cv:
            self._runs[run.run_id] = run
            self._queue.append(run.run_id)
            self._event(run, "run.queued", {"backend": self.backend_name})
            threading.Thread(target=self._launch, args=(run.run_id,), daemon=True).start()
            return self._snapshot(run, result_chars=0)

    def _launch(self, run_id):
        while True:
            with self._cv:
                run = self._get(run_id)
                if run.status != "queued":
                    return
                if self._max_concurrency and len(self._active) >= self._max_concurrency:
                    self._cv.wait(0.2)
                    continue
                lease = self._lease_store.try_acquire(run.run_id, run.resource_modes)
                if not lease.get("acquired"):
                    blockers = lease.get("blockers") or []
                    if blockers != run.lease_blockers:
                        run.lease_blockers = blockers
                        self._event(run, "resource.waiting", {"blockers": blockers[:12]})
                    self._cv.wait(0.2)
                    continue
                run.lease_acquired = True
                run.lease_blockers = []
                self._active.add(run.run_id)
                if run.run_id in self._queue:
                    self._queue.remove(run.run_id)
                run.status = "starting"
                run.phase = "starting-runtime"
                run.started_ms = now_ms()
                self._event(run, "run.started", {"recovered": run.recovered})
                args = dict(run.args)
                break
        runtime = None
        try:
            runtime = self.backend.create_runtime(
                args,
                lambda event: self._on_runtime_event(run_id, event),
                lambda message: self._on_runtime_disconnect(run_id, message),
            )
            with self._cv:
                run = self._get(run_id)
                run.runtime = runtime
            state = runtime.start(None if run.recovered else run.prompt)
            with self._cv:
                run = self._get(run_id)
                if isinstance(state, dict):
                    run.thread_id = state.get("threadId") or run.thread_id
                    self._apply_projection(run, state)
                if run.recovered:
                    run.status = "completed"
                    run.phase = "recovered-idle"
                    run.finished_ms = now_ms()
                    self._event(run, "run.recovered", {})
                    self._release(run)
                elif run.status == "starting":
                    run.status = "running"
                    run.phase = "agent"
                    self._event(run, "agent.accepted", {})
            if not run.recovered:
                threading.Thread(target=self._timeout_watch, args=(run_id,), daemon=True).start()
        except Exception as exc:  # noqa: BLE001
            if runtime:
                try:
                    runtime.close()
                except Exception as close_exc:  # noqa: BLE001
                    self._logger(
                        "%s failed runtime cleanup: %s" % (self.backend_name, close_exc)
                    )
            with self._cv:
                run = self._get(run_id)
                run.status = "failed"
                run.phase = "launch-failed"
                run.error = bounded(exc)
                run.finished_ms = now_ms()
                self._event(run, "run.failed", {"message": run.error})
                self._release(run)

    def _timeout_watch(self, run_id):
        with self._lock:
            run = self._get(run_id)
            deadline = run.created_ms + run.timeout_seconds * 1000
        remaining = max(0, deadline - now_ms()) / 1000.0
        if remaining:
            time.sleep(remaining)
        with self._cv:
            run = self._get(run_id)
            if run.status in TERMINAL_STATES:
                return
            run.timeout_requested = True
            runtime = run.runtime
            self._event(run, "run.timeout", {})
        if runtime:
            try:
                runtime.cancel()
            except Exception:
                pass

    def _on_runtime_event(self, run_id, event):
        if not isinstance(event, dict):
            return
        with self._cv:
            run = self._runs.get(run_id)
            if not run or run.status == "closed":
                return
            kind = event.get("type") or "native.event"
            self._apply_projection(run, event)
            self._event(run, kind, event.get("detail") or {})
            if kind == "settled":
                outcome = event.get("status") or "completed"
                if run.timeout_requested:
                    outcome = "timed_out"
                elif run.cancel_requested and outcome == "completed":
                    outcome = "cancelled"
                if outcome not in {"completed", "failed", "cancelled", "timed_out"}:
                    outcome = "completed"
                run.status = outcome
                run.phase = "terminal"
                run.finished_ms = now_ms()
                run.model["status"] = "idle"
                run.model["reasoningActive"] = False
                self._release(run)

    def _apply_projection(self, run, value):
        if value.get("threadId"):
            run.thread_id = value["threadId"]
        if value.get("phase"):
            run.phase = value["phase"]
        if value.get("result") is not None:
            run.result = str(value.get("result") or "")[:MAX_RESULT]
        if value.get("error"):
            run.error = bounded(value["error"])
        if isinstance(value.get("model"), dict):
            run.model.update(value["model"])
        if isinstance(value.get("usage"), dict):
            for key in run.usage:
                number = value["usage"].get(key)
                if isinstance(number, (int, float)):
                    run.usage[key] = int(number)
        if isinstance(value.get("context"), dict):
            run.context.update(value["context"])
        if value.get("type") == "reasoning.started":
            run.model["reasoningActive"] = True
            run.model["status"] = "reasoning"
        elif value.get("type") == "reasoning.ended":
            run.model["reasoningActive"] = False
            run.model["status"] = "running"
        elif value.get("type") == "model.started":
            run.model["status"] = "running"
            run.usage["modelRequests"] += 1
        elif value.get("type") == "model.error":
            run.model["status"] = "error"
            run.usage["modelErrors"] += 1
        elif value.get("type") == "tool.started":
            tool_id = str(value.get("toolCallId") or uuid.uuid4().hex)
            run.active_tools[tool_id] = {
                "id": tool_id,
                "name": value.get("toolName") or "tool",
                "status": "running",
                "startedAt": now_ms(),
            }
            run.counts["toolCalls"] += 1
            run.phase = "tool"
        elif value.get("type") == "tool.ended":
            run.active_tools.pop(str(value.get("toolCallId") or ""), None)

    def _on_runtime_disconnect(self, run_id, message):
        with self._cv:
            run = self._runs.get(run_id)
            if not run or run.status in TERMINAL_STATES:
                return
            run.status = "failed"
            run.phase = "transport-lost"
            run.error = bounded(message)
            run.finished_ms = now_ms()
            self._event(run, "transport.lost", {"message": run.error})
            self._release(run)

    def _release(self, run):
        if run.released:
            return
        run.released = True
        if run.lease_acquired:
            self._lease_store.release(run.run_id)
            run.lease_acquired = False
        self._active.discard(run.run_id)
        if run.run_id in self._queue:
            self._queue.remove(run.run_id)
        self._touch(run)

    def _event(self, run, kind, detail):
        run.seq += 1
        event = {"seq": run.seq, "type": kind, "atMs": now_ms()}
        compact = {key: value for key, value in (detail or {}).items() if value not in (None, "", {})}
        if compact:
            event["detail"] = compact
        run.events.append(event)
        self._touch(run)

    def _touch(self, run):
        run.revision += 1
        self._cv.notify_all()

    def _get(self, run_id):
        run = self._runs.get(run_id)
        if not run:
            raise ControlPlaneError("run not found: %s" % run_id, "run_not_found")
        return run

    def _snapshot(self, run, *, result_chars=2000):
        end = run.finished_ms or now_ms()
        result = {
            "runId": run.run_id,
            "backend": run.backend,
            "status": run.status,
            "revision": run.revision,
            "threadId": run.thread_id,
            "phase": run.phase,
            "elapsedMs": max(0, end - run.created_ms),
            "createdAtMs": run.created_ms,
            "startedAtMs": run.started_ms,
            "finishedAtMs": run.finished_ms,
            "resources": [
                {"key": key, "mode": mode} for key, mode in run.resource_modes.items()
            ],
            "resourceLease": {
                "scope": "cross-process",
                "acquired": run.lease_acquired,
                "blockers": run.lease_blockers[:12],
            },
            "model": dict(run.model),
            "usage": dict(run.usage),
            "counts": dict(run.counts),
            "activeTools": list(run.active_tools.values())[:12],
            "subagents": run.subagents,
            "context": run.context,
            "lastSeq": run.seq,
            "permissionPolicy": {
                "headless": True,
                "workspaceAccess": run.workspace_access,
                "allowedRoots": [
                    key for key in run.resource_modes if os.path.isabs(key)
                ],
            },
        }
        if run.error:
            result["error"] = run.error
        result_chars = min(max(int(result_chars), 0), MAX_RESULT)
        if result_chars and run.result:
            result["result"] = run.result[:result_chars]
            result["resultTruncated"] = len(run.result) > result_chars
        result["next"] = (
            "wait with afterRevision=%s" % run.revision
            if run.status not in TERMINAL_STATES else
            "observe, branch/compact, or close"
        )
        return result
