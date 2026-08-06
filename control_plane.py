"""Event-driven orchestration over ZCode's native app-server protocol."""

from __future__ import annotations

from collections import deque
import json
import os
import threading
import time
import uuid

from resource_leases import ResourceLeaseStore
from zcode_protocol import (
    ProtocolError,
    ZCodeProtocolClient,
    now_ms,
    resolve_runtime_model,
)


TERMINAL_STATES = {"completed", "failed", "cancelled", "timed_out", "closed"}
MAX_EVENT_TEXT = 600
MAX_STORED_RESULT = 12000


class ControlPlaneError(RuntimeError):
    def __init__(self, message, code="control_error", data=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.data = data or {}


def _bounded(value, limit=MAX_EVENT_TEXT):
    if value is None:
        return None
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            value = str(value)
    return value if len(value) <= limit else value[:limit] + "…"


def _find_key(value, names):
    if isinstance(value, dict):
        for key in names:
            found = value.get(key)
            if found is not None and not isinstance(found, (dict, list)):
                return found
        for child in value.values():
            found = _find_key(child, names)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_key(child, names)
            if found is not None:
                return found
    return None


def _find_list(value, names):
    if isinstance(value, dict):
        for key in names:
            found = value.get(key)
            if isinstance(found, list):
                return found
        for child in value.values():
            found = _find_list(child, names)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_list(child, names)
            if found is not None:
                return found
    return None


def extract_session_id(snapshot):
    if isinstance(snapshot, dict):
        session = snapshot.get("session")
        if isinstance(session, dict):
            value = session.get("sessionId")
            if isinstance(value, str) and value.startswith("sess_"):
                return value
        value = snapshot.get("sessionId")
        if isinstance(value, str) and value.startswith("sess_"):
            return value
    return _find_key(snapshot, {"sessionId"})


def _text_parts(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        result = []
        for child in value:
            result.extend(_text_parts(child))
        return result
    if isinstance(value, dict):
        kind = str(value.get("type") or "").lower()
        if kind in {"thinking", "reasoning", "tool_use", "tool_result"}:
            return []
        for key in ("text", "content", "value", "message", "parts"):
            if key in value:
                return _text_parts(value[key])
    return []


def extract_last_assistant_text(snapshot):
    messages = _find_list(snapshot, {"messages", "items"}) or []
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        info = message.get("info") if isinstance(message.get("info"), dict) else {}
        role = str(message.get("role") or info.get("role") or message.get("author") or "").lower()
        if role not in {"assistant", "agent"}:
            continue
        parts = _text_parts(message.get(
            "content", message.get("message", message.get("parts", ""))
        ))
        text = "\n".join(part.strip() for part in parts if part and part.strip())
        if text:
            return text
    return ""


def _compact_background(job):
    if not isinstance(job, dict):
        return None
    result = {}
    aliases = {
        "taskId": ("taskId", "id"),
        "toolCallId": ("toolCallId", "toolUseId"),
        "toolName": ("toolName", "name"),
        "kind": ("taskKind", "kind", "type"),
        "status": ("status", "state"),
        "description": ("description", "command", "title"),
        "blockedReason": ("blockedReason", "reason"),
        "cancellable": ("cancellable",),
        "startedAt": ("startedAt", "startTime"),
        "completedAt": ("completedAt", "endTime"),
        "outputBytes": ("outputBytes",),
        "outputTruncated": ("outputTruncated", "truncated"),
        "outputTail": ("outputTail", "stdoutTail"),
        "stderrTail": ("stderrTail",),
    }
    for target, names in aliases.items():
        value = _find_key(job, set(names))
        if value not in (None, "", [], {}):
            result[target] = _bounded(value, 1200 if target.endswith("Tail") else 300)
            if isinstance(value, (bool, int, float)):
                result[target] = value
    return result or None


def _compact_agent(agent):
    if not isinstance(agent, dict):
        return None
    result = {}
    for target, names in {
        "agentId": {"agentId", "id"},
        "sessionId": {"childSessionId", "sessionId"},
        "status": {"status", "state"},
        "type": {"subagentType", "agentType", "type"},
        "title": {"title", "label", "name"},
        "summary": {"summary", "result"},
        "startedAt": {"startedAt", "startTime"},
        "endedAt": {"endedAt", "completedAt", "endTime"},
    }.items():
        value = _find_key(agent, names)
        if value not in (None, ""):
            result[target] = _bounded(value, 500 if target == "summary" else 200)
            if isinstance(value, (bool, int, float)):
                result[target] = value
    return result or None


def _normalize_resources(cwd, workspace_access, resources):
    modes = {os.path.realpath(cwd): workspace_access}
    for resource in resources or []:
        if not isinstance(resource, dict):
            raise ControlPlaneError("resources entries must be objects", "invalid_params")
        key = resource.get("key")
        mode = resource.get("mode", "exclusive")
        if not isinstance(key, str) or not key.strip():
            raise ControlPlaneError("resource key must be non-empty", "invalid_params")
        if os.path.isabs(key):
            key = os.path.realpath(key)
        if mode not in {"shared", "exclusive"}:
            raise ControlPlaneError("resource mode must be shared or exclusive", "invalid_params")
        previous = modes.get(key)
        modes[key] = "exclusive" if "exclusive" in {previous, mode} else "shared"
    return modes


_STRUCTURED_PATH_KEYS = {
    "path", "filepath", "file_path", "cwd", "workingdirectory",
    "working_directory", "destination", "destinationpath", "sourcepath",
}
_STRUCTURED_WRITE_TOOLS = {
    "write", "edit", "multiedit", "applypatch", "apply_patch",
    "createfile", "deletefile", "movefile", "renamefile",
}


def _structured_paths(value, cwd, parent_key=None):
    paths = []
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).replace("-", "").lower()
            if lowered in _STRUCTURED_PATH_KEYS and isinstance(child, str):
                candidate = child.strip()
                if candidate and "://" not in candidate:
                    if not os.path.isabs(candidate):
                        candidate = os.path.join(cwd, candidate)
                    paths.append(os.path.realpath(candidate))
            else:
                paths.extend(_structured_paths(child, cwd, key))
    elif isinstance(value, list):
        for child in value:
            paths.extend(_structured_paths(child, cwd, parent_key))
    return list(dict.fromkeys(paths))


def _path_resource_mode(path, resource_modes):
    matches = []
    for root, mode in resource_modes.items():
        if not os.path.isabs(root):
            continue
        try:
            if os.path.commonpath([path, root]) == root:
                matches.append((len(root), mode))
        except ValueError:
            continue
    return max(matches)[1] if matches else None


class RunRecord:
    def __init__(self, args, *, recovered=False):
        self.run_id = "run_" + uuid.uuid4().hex
        self.prompt = args.get("prompt") or ""
        self.cwd_supplied = bool(args.get("cwd"))
        self.cwd = os.path.realpath(args.get("cwd") or os.getcwd())
        self.thread_id = args.get("threadId")
        self.resume_requested = bool(self.thread_id)
        self.mode = args.get("mode")
        self.thought_level = args.get("thoughtLevel")
        self.model = args.get("model")
        self.runtime_model = None
        self.tool_allowlist = args.get("toolAllowlist")
        self.tool_denylist = args.get("toolDenylist")
        self.workspace_access = args.get("workspaceAccess") or "exclusive"
        self.resource_modes = _normalize_resources(
            self.cwd, self.workspace_access, args.get("resources")
        )
        self.goal = args.get("goal")
        self.timeout_seconds = args.get("timeout")
        self.recovered = recovered
        self.status = "queued"
        self.phase = "waiting-for-resource"
        self.revision = 1
        self.seq = 0
        self.created_ms = now_ms()
        self.started_ms = None
        self.finished_ms = None
        self.session_id = None
        self.error = None
        self.result = ""
        self.events = deque(maxlen=160)
        self.active_tools = {}
        self.pending_guidance = deque()
        self.guidance_dispatching = False
        self.last_terminal_status = None
        self.control_failures = deque(maxlen=8)
        self.cancel_requested = False
        self.timeout_requested = False
        self.stop_generation = 0
        self.turn_count = 0
        self.released = False
        self.lease_acquired = False
        self.lease_blockers = []
        self.closed = False
        self.input_id = "input_" + uuid.uuid4().hex
        self.native_status = None
        self.native_revision = None
        self.native_event_seq = 0
        self.last_activity_ms = self.created_ms
        self.last_progress_ms = self.created_ms
        self.model_state = {
            "status": "idle",
            "reasoningActive": False,
            "lastChannel": None,
            "requestId": None,
            "model": None,
            "thoughtLevel": self.thought_level,
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
        self.session_usage = dict(self.usage)
        self.usage_baseline = dict(self.usage)
        self.usage_baseline_captured = False
        self.counts = {"toolCalls": 0, "subagents": 0}
        self.background_tasks = []
        self.subagents = {"running": [], "ended": [], "endedTotal": 0}
        self.context = {}
        self.goal_state = None
        self.last_checkpoint = None
        self.pre_compact_status = None


class ZCodeControlPlane:
    """Own ZCode runs while exposing compact, replayable scheduling state."""

    def __init__(self, zcode_bin=None, zcode_bundle=None, *, max_concurrency=0,
                 protocol=None, protocol_factory=None, runtime_model_resolver=None,
                 lease_store=None, guidance_retry_seconds=10.0,
                 native_stop_wait_seconds=10.0, logger=None):
        self._lock = threading.RLock()
        self._cv = threading.Condition(self._lock)
        self._runs = {}
        self._session_runs = {}
        self._session_bindings = {}
        self._queue = []
        self._active = set()
        self._max_concurrency = max(0, int(max_concurrency))
        self._lease_store = lease_store or ResourceLeaseStore()
        self._guidance_retry_seconds = max(float(guidance_retry_seconds), 0.1)
        self._native_stop_wait_seconds = max(float(native_stop_wait_seconds), 0.1)
        self._logger = logger or (lambda _message: None)
        self._transport_recovering = False
        self._runtime_model_resolver = runtime_model_resolver or resolve_runtime_model
        if protocol is None:
            factory = protocol_factory or ZCodeProtocolClient
            protocol = factory(
                zcode_bin, zcode_bundle,
                on_notification=self._on_notification,
                on_disconnect=self._on_disconnect,
                on_server_request=self._on_server_request,
                logger=self._logger,
            )
        else:
            protocol.on_notification = self._on_notification
            if hasattr(protocol, "on_disconnect"):
                protocol.on_disconnect = self._on_disconnect
            protocol.on_server_request = self._on_server_request
        self._protocol = protocol

    def _on_server_request(self, method, params):
        """Resolve ZCode interactions under the declared headless run policy."""
        if not isinstance(params, dict):
            raise ProtocolError("invalid ZCode interaction parameters", code=-32602)
        session_id = params.get("sessionId")
        with self._cv:
            run_id = self._session_runs.get(session_id)
            run = self._runs.get(run_id) if run_id else None
            if run is None or run.status in TERMINAL_STATES:
                raise ProtocolError(
                    "interaction does not belong to an active managed run",
                    code=-32031,
                    data={"method": method, "sessionId": session_id},
                )

            if method == "interaction/requestPermission":
                tool_name = str(params.get("toolName") or "unknown")
                normalized_tool = tool_name.replace("-", "").replace("_", "").lower()
                if run.mode == "plan":
                    self._event(run, "interaction.permission-denied", {
                        "toolName": tool_name,
                        "reason": "plan-mode run",
                    })
                    return {
                        "decision": "deny",
                        "reason": "Headless plan-mode runs may not execute approval-gated tools",
                    }
                candidate_paths = _structured_paths(params, run.cwd)
                outside = [path for path in candidate_paths if _path_resource_mode(
                    path, run.resource_modes
                ) is None]
                read_only_paths = [path for path in candidate_paths if _path_resource_mode(
                    path, run.resource_modes
                ) != "exclusive"]
                write_denied = normalized_tool in _STRUCTURED_WRITE_TOOLS and (
                    bool(read_only_paths)
                    or (not candidate_paths and run.workspace_access != "exclusive")
                )
                if write_denied:
                    self._event(run, "interaction.permission-denied", {
                        "toolName": tool_name,
                        "reason": "structured write requires an exclusive declared root",
                        "paths": read_only_paths[:8],
                    })
                    return {
                        "decision": "deny",
                        "reason": "Structured writes require an exclusive declared path root",
                    }
                if outside:
                    self._event(run, "interaction.permission-denied", {
                        "toolName": tool_name,
                        "reason": "structured path outside declared workspace/resources",
                        "paths": outside[:8],
                    })
                    return {
                        "decision": "deny",
                        "reason": "Requested path is outside the declared workspace/resources",
                    }
                self._event(run, "interaction.permission-approved", {
                    "toolName": tool_name,
                    "riskLevel": params.get("riskLevel"),
                    "scopeChecked": bool(candidate_paths),
                    "paths": candidate_paths[:8],
                })
                return {
                    "decision": "allow",
                    "reason": "Approved by Codex for this managed headless run",
                }

            if method == "interaction/requestUserInput":
                schema = params.get("schema") if isinstance(params.get("schema"), dict) else {}
                if schema.get("interaction") == "plan_approval" and run.mode != "plan":
                    self._event(run, "interaction.plan-approved", {})
                    return {
                        "action": "accept",
                        "content": {"answer": "approve"},
                        "reason": "Implementation was already authorized by the Codex task",
                    }
                self._event(run, "interaction.user-input-declined", {
                    "toolName": params.get("toolName"),
                    "reason": "no safe headless answer",
                })
                return {
                    "action": "decline",
                    "reason": "Headless bridge cannot invent an answer to user questions",
                }

            if method == "interaction/requestProviderRuntimeHeaders":
                return {
                    "headersApplied": False,
                    "errorMessage": "No dynamic provider headers are configured by the bridge",
                }

        raise ProtocolError(
            "Headless bridge cannot satisfy interaction: %s" % method,
            code=-32030,
        )

    def close(self):
        with self._lock:
            sessions = []
            for run in self._runs.values():
                if run.session_id and run.status not in TERMINAL_STATES:
                    sessions.append((run.session_id, [
                        item.get("taskId") for item in run.background_tasks
                        if item.get("taskId")
                    ]))
        for session_id, task_ids in sessions:
            try:
                self._stop_native_session(session_id, task_ids, wait_seconds=5)
            except Exception as exc:
                self._logger("shutdown stop failed for %s: %s" % (session_id, exc))
        try:
            self._protocol.close()
        finally:
            self._lease_store.close()

    def start(self, args):
        prompt = args.get("prompt")
        goal = args.get("goal")
        has_prompt = isinstance(prompt, str) and bool(prompt.strip())
        has_goal = isinstance(goal, str) and bool(goal.strip())
        if has_prompt == has_goal:
            raise ControlPlaneError(
                "provide exactly one non-empty prompt or goal; native goal creation starts its own turn",
                "invalid_params",
            )
        self._validate_run_args(args)
        normalized = self._bound_args(args)
        run = RunRecord(normalized)
        return self._enqueue(run)

    def recover(self, args):
        thread_id = args.get("adoptThreadId")
        workspace_filter = None
        if args.get("workspace"):
            workspace_path = os.path.realpath(args["workspace"])
            workspace_filter = {
                "workspacePath": workspace_path,
                "workspaceKey": workspace_path,
            }
        listed = self._protocol.request(
            "session/list",
            {
                **({"workspace": workspace_filter} if workspace_filter else {}),
                "includeArchived": bool(args.get("includeArchived", False)),
                "limit": min(max(int(args.get("limit", 20)), 1), 100),
            },
            timeout=30,
        )
        sessions = listed.get("sessions") or listed.get("items") or []
        compact = [self._compact_session(item) for item in sessions]
        compact = [item for item in compact if item]
        if not thread_id:
            return {"sessions": compact, "count": len(compact)}
        with self._lock:
            existing_id = self._session_runs.get(thread_id)
            if existing_id:
                existing = self._runs.get(existing_id)
                if existing and existing.status not in TERMINAL_STATES:
                    result = self._snapshot(existing, result_chars=0)
                    result["alreadyManaged"] = True
                    return result
        match = next((item for item in sessions if extract_session_id(item) == thread_id), None)
        if not match:
            raise ControlPlaneError("session not found: %s" % thread_id, "session_not_found")
        workspace = match.get("workspace") if isinstance(match, dict) else None
        cwd = args.get("cwd") or (workspace or {}).get("workspacePath")
        normalized = dict(args)
        normalized["threadId"] = thread_id
        normalized["cwd"] = cwd or os.getcwd()
        normalized.pop("adoptThreadId", None)
        self._validate_run_args(normalized)
        run = RunRecord(normalized, recovered=True)
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
                self._refresh_native(run_id)
            except Exception as exc:
                refresh_error = _bounded(str(exc), 800)
        max_events = min(max(int(max_events), 0), 30)
        result_chars = min(max(int(result_chars), 0), MAX_STORED_RESULT)
        with self._lock:
            run = self._get(run_id)
            result = self._snapshot(run, result_chars=result_chars)
            oldest_seq = run.events[0]["seq"] if run.events else run.seq + 1
            result["eventsDropped"] = max(0, oldest_seq - int(after_seq) - 1)
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
        if action == "guide":
            return self._guide(
                run_id, prompt, interrupt=False,
                if_revision=if_revision, if_status=if_status,
            )
        if action == "interrupt":
            return self._guide(
                run_id, prompt, interrupt=True,
                if_revision=if_revision, if_status=if_status,
            )
        if action == "cancel":
            return self.cancel(run_id)
        with self._lock:
            run = self._get(run_id)
            session_id = run.session_id
            revision = run.native_revision
        if not session_id:
            raise ControlPlaneError("run has no ZCode session yet", "session_not_ready")
        if action == "cancel-background":
            if not isinstance(task_id, str) or not task_id:
                raise ControlPlaneError("taskId is required", "invalid_params")
            native = self._protocol.request(
                "session/cancelBackgroundTask",
                {"sessionId": session_id, "taskId": task_id},
                timeout=15,
            )
            with self._cv:
                run = self._get(run_id)
                self._event(run, "background.cancel-requested", {"taskId": task_id})
            result = self.observe(run_id, refresh=True, result_chars=0)
            result["controlResult"] = {
                "action": action,
                "taskId": task_id,
                "cancelled": native.get("cancelled") if isinstance(native, dict) else None,
                "status": native.get("status") if isinstance(native, dict) else None,
                "reason": native.get("reason") if isinstance(native, dict) else None,
            }
            return result
        if action in {"pause-goal", "resume-goal"}:
            params = {
                "sessionId": session_id,
                "action": "pause" if action == "pause-goal" else "resume",
            }
            if revision is not None:
                params["expectedRevision"] = revision
            native = self._protocol.request("session/goal", params, timeout=30)
            with self._cv:
                run = self._get(run_id)
                self._apply_snapshot(run, native.get("snapshot") or native)
                self._event(run, "goal.%s" % params["action"], {})
            result = self.snapshot(run_id, result_chars=0)
            result["controlResult"] = {"action": action, "goal": result.get("goal")}
            return result
        raise ControlPlaneError("unknown control action: %s" % action, "invalid_params")

    def branch(self, run_id, *, target_kind="latestCheckpoint", target_id=None,
               turn_index=None):
        with self._lock:
            run = self._get(run_id)
            if run.status not in TERMINAL_STATES:
                raise ControlPlaneError("branch requires an idle/terminal run", "run_active")
            if run.status == "closed":
                raise ControlPlaneError("session is closed", "session_closed")
            session_id = run.session_id
            revision = run.native_revision
        if not session_id:
            raise ControlPlaneError("run has no ZCode session", "session_not_ready")
        if target_kind == "latestCheckpoint":
            target = {"kind": "latestCheckpoint"}
        elif target_kind == "turn":
            if turn_index is None:
                raise ControlPlaneError("turnIndex is required", "invalid_params")
            target = {"kind": "turn", "turnIndex": int(turn_index)}
        elif target_kind in {"message", "checkpoint"}:
            if not isinstance(target_id, str) or not target_id:
                raise ControlPlaneError("targetId is required", "invalid_params")
            target = {"kind": target_kind}
            target["messageId" if target_kind == "message" else "checkpointId"] = target_id
        else:
            raise ControlPlaneError("invalid branch target", "invalid_params")
        params = {"sessionId": session_id, "target": target}
        if revision is not None:
            params["expectedRevision"] = revision
        native = self._protocol.request("session/fork", params, timeout=60)
        return {
            "threadId": native.get("forkedSessionId"),
            "parentThreadId": native.get("parentSessionId") or session_id,
            "targetMessageId": native.get("targetMessageId"),
            "targetCheckpointId": native.get("targetCheckpointId"),
            "response": _bounded(native.get("response"), 1200),
        }

    def context(self, run_id, *, action="inspect", instructions=None):
        if action == "inspect":
            state = self.observe(run_id, refresh=True, max_events=0, result_chars=0)
            return {
                "runId": run_id,
                "threadId": state.get("threadId"),
                "context": state.get("context", {}),
                "usage": state.get("usage", {}),
                "checkpoint": state.get("lastCheckpoint"),
            }
        if action != "compact":
            raise ControlPlaneError("context action must be inspect or compact", "invalid_params")
        with self._cv:
            run = self._get(run_id)
            if run.status not in TERMINAL_STATES:
                raise ControlPlaneError("compact requires an idle/terminal run", "run_active")
            if run.status == "closed":
                raise ControlPlaneError("session is closed", "session_closed")
            for active_id in self._active:
                active = self._runs.get(active_id)
                if active and active.run_id != run_id and self._conflicts(active, run):
                    raise ControlPlaneError(
                        "declared resources are active; compact after they are released",
                        "resource_busy",
                    )
            params = {
                "sessionId": run.session_id,
                "inputId": "compact_" + uuid.uuid4().hex,
            }
            if instructions:
                params["instructions"] = instructions
            if run.native_revision is not None:
                params["expectedRevision"] = run.native_revision
            if run.runtime_model:
                params["runtimeModel"] = run.runtime_model
            run.released = False
            run.pre_compact_status = run.status
            run.finished_ms = None
            run.status = "compacting"
            run.phase = "context-compaction"
            self._active.add(run_id)
            self._event(run, "context.compact-requested", {})
        try:
            native = self._protocol.request("session/compact", params, timeout=60)
        except Exception:
            with self._cv:
                run = self._get(run_id)
                run.status = run.pre_compact_status or "completed"
                run.phase = "terminal"
                run.finished_ms = now_ms()
                run.pre_compact_status = None
                self._release(run)
            raise
        with self._cv:
            run = self._get(run_id)
            self._apply_snapshot(run, native.get("snapshot") or native)
            compact = native.get("compact") or {}
            self._event(run, "context.compact", {"state": compact.get("state")})
        return {
            "run": self.snapshot(run_id, result_chars=0),
            "operation": native.get("compact"),
            "response": _bounded(native.get("response"), 1200),
        }

    def close_run(self, run_id=None, *, thread_id=None):
        if not run_id:
            if not isinstance(thread_id, str) or not thread_id:
                raise ControlPlaneError("runId or threadId is required", "invalid_params")
            with self._lock:
                managed_id = self._session_runs.get(thread_id)
                managed = self._runs.get(managed_id) if managed_id else None
                if managed and managed.status not in TERMINAL_STATES:
                    raise ControlPlaneError("managed session is still active", "run_active")
            stopped = self._stop_native_session(thread_id, [], wait_seconds=10)
            native = self._protocol.request("session/close", {"sessionId": thread_id}, timeout=30)
            return {
                "threadId": thread_id,
                "status": "closed",
                "stop": stopped,
                "native": native,
            }
        with self._lock:
            run = self._get(run_id)
            if run.status not in TERMINAL_STATES:
                raise ControlPlaneError("cancel or finish the run before closing", "run_active")
            if run.closed:
                return self._snapshot(run, result_chars=0)
            session_id = run.session_id
            task_ids = [
                item.get("taskId") for item in run.background_tasks if item.get("taskId")
            ]
        stopped = self._stop_native_session(session_id, task_ids, wait_seconds=10)
        native = self._protocol.request("session/close", {"sessionId": session_id}, timeout=30)
        with self._cv:
            run = self._get(run_id)
            run.closed = True
            run.status = "closed"
            run.phase = "closed"
            self._event(run, "session.closed", {})
            self._session_runs.pop(session_id, None)
            self._session_bindings.pop(session_id, None)
            self._release(run)
        result = self.snapshot(run_id, result_chars=0)
        if native:
            result["native"] = native
        result["stop"] = stopped
        return result

    def cancel(self, run_id):
        stop = False
        background_only = False
        background_ids = []
        session_id = None
        with self._cv:
            run = self._get(run_id)
            if run.status in TERMINAL_STATES:
                return self._snapshot(run, result_chars=0)
            background_only = run.status == "background"
            background_ids = [
                item.get("taskId") for item in run.background_tasks
                if item.get("taskId") and str(item.get("status") or "").lower()
                in {"pending", "queued", "starting", "running", "waiting", "blocked"}
            ]
            session_id = run.session_id
            run.cancel_requested = True
            run.pending_guidance.clear()
            run.stop_generation += 1
            if run.status == "queued":
                run.status = "cancelled"
                run.phase = "cancelled-before-start"
                run.finished_ms = now_ms()
                self._event(run, "run.cancelled", {"beforeStart": True})
                self._release(run)
            else:
                run.status = "stopping"
                run.phase = "cancelling"
                stop = bool(run.session_id)
                self._event(run, "run.stop-requested", {})
        if background_only:
            cleanup_error = None
            try:
                self._stop_native_session(
                    session_id,
                    background_ids,
                    wait_seconds=self._native_stop_wait_seconds,
                )
            except Exception as exc:
                cleanup_error = _bounded(str(exc), 1200)
            with self._cv:
                run = self._get(run_id)
                run.status = "failed" if cleanup_error else "cancelled"
                run.phase = "resource-cleanup-required" if cleanup_error else "terminal"
                run.finished_ms = now_ms()
                if cleanup_error:
                    run.error = cleanup_error
                    self._event(run, "resource.cleanup-failed", {
                        "message": cleanup_error,
                        "leaseRetained": True,
                    })
                else:
                    self._event(run, "run.cancelled", {"backgroundTasks": len(background_ids)})
                    self._release(run)
            return self.snapshot(run_id, result_chars=0)
        self._cancel_native_background(session_id, background_ids)
        if stop:
            self._stop_session(run_id, cancel=True)
        return self.snapshot(run_id, result_chars=0)

    def snapshot(self, run_id, *, result_chars=2000):
        with self._lock:
            return self._snapshot(self._get(run_id), result_chars=result_chars)

    def _validate_run_args(self, args):
        if args.get("workspaceAccess", "exclusive") not in {"shared", "exclusive"}:
            raise ControlPlaneError("workspaceAccess must be shared or exclusive", "invalid_params")
        if args.get("mode") not in {None, "build", "edit", "plan", "yolo", "auto"}:
            raise ControlPlaneError("invalid ZCode mode", "invalid_params")
        if args.get("goal") and args.get("mode") == "plan":
            raise ControlPlaneError(
                "native durable goals do not execute in plan mode; use build, edit, yolo, auto, or omit mode",
                "invalid_params",
            )
        if args.get("thoughtLevel") not in {None, "high", "max"}:
            raise ControlPlaneError("thoughtLevel must be high or max", "invalid_params")
        _normalize_resources(
            args.get("cwd") or os.getcwd(),
            args.get("workspaceAccess", "exclusive"),
            args.get("resources"),
        )

    def _bound_args(self, args):
        normalized = dict(args)
        with self._lock:
            binding = self._session_bindings.get(normalized.get("threadId"))
        if binding:
            bound_cwd, bound_resources = binding
            if normalized.get("cwd") and os.path.realpath(normalized["cwd"]) != bound_cwd:
                raise ControlPlaneError(
                    "threadId is bound to a different worktree: %s" % bound_cwd,
                    "session_worktree_mismatch",
                )
            normalized.setdefault("cwd", bound_cwd)
            normalized.setdefault(
                "workspaceAccess", bound_resources.get(bound_cwd, "exclusive")
            )
            normalized.setdefault(
                "resources",
                [{"key": key, "mode": mode} for key, mode in bound_resources.items()
                 if key != bound_cwd],
            )
        return normalized

    def _enqueue(self, run):
        with self._cv:
            self._runs[run.run_id] = run
            self._queue.append(run.run_id)
            self._event(run, "run.queued", {"resources": run.resource_modes})
        threading.Thread(target=self._launch, args=(run.run_id,), daemon=True).start()
        return self.snapshot(run.run_id, result_chars=0)

    def _get(self, run_id):
        run = self._runs.get(run_id)
        if run is None:
            raise ControlPlaneError("unknown runId: %s" % run_id, "run_not_found")
        return run

    def _launch(self, run_id):
        run = None
        try:
            with self._cv:
                run = self._get(run_id)
                while True:
                    if run.status in TERMINAL_STATES:
                        return
                    if not self._can_start(run):
                        self._cv.wait(self._lease_store.poll_seconds)
                        continue
                    lease = self._lease_store.try_acquire(run.run_id, run.resource_modes)
                    if lease.get("acquired"):
                        run.lease_acquired = True
                        run.lease_blockers = []
                        break
                    blockers = lease.get("blockers") or []
                    if blockers != run.lease_blockers:
                        run.lease_blockers = blockers
                        run.phase = "waiting-for-global-resource"
                        self._event(run, "resource.waiting", {"blockers": blockers})
                    self._cv.wait(self._lease_store.poll_seconds)
                self._queue.remove(run_id)
                self._active.add(run_id)
                run.status = "starting"
                run.phase = "opening-session"
                run.started_ms = now_ms()
                self._event(run, "run.started", {"recovered": run.recovered})

            run.runtime_model = self._runtime_model_resolver(
                run.model, run.thought_level
            )
            if run.runtime_model and not run.thought_level:
                run.thought_level = run.runtime_model.get("thoughtLevel")
            session = self._open_session(run)
            self._lease_store.set_guard_pid(getattr(self._protocol, "process_id", None))
            session_id = extract_session_id(session) or run.thread_id
            if not session_id:
                raise ControlPlaneError("ZCode did not return a session id", "protocol_error")
            workspace = (session.get("session") or {}).get("workspace", {}) if isinstance(session, dict) else {}
            actual_cwd = workspace.get("workspacePath") if isinstance(workspace, dict) else None
            if actual_cwd:
                actual_cwd = os.path.realpath(actual_cwd)
                if run.cwd_supplied and actual_cwd != run.cwd:
                    raise ControlPlaneError(
                        "threadId belongs to %s, not requested cwd %s" % (actual_cwd, run.cwd),
                        "session_worktree_mismatch",
                    )
            with self._cv:
                run.session_id = str(session_id)
                run.thread_id = str(session_id)
                self._session_runs[run.session_id] = run.run_id
                self._session_bindings[run.session_id] = (
                    actual_cwd or run.cwd, dict(run.resource_modes)
                )
                self._apply_snapshot(run, session)
                self._event(run, "session.ready", {"threadId": run.thread_id})

            self._subscribe(run_id, after_seq=None, include_snapshot=True)
            if run.recovered:
                with self._cv:
                    run = self._get(run_id)
                    native_status = str(run.native_status or "idle").lower()
                    if native_status in {"running", "waiting", "paused"}:
                        run.status = "running" if native_status != "paused" else "paused"
                        run.phase = "recovered-%s" % native_status
                        self._event(run, "run.recovered", {"nativeStatus": native_status})
                    else:
                        run.status = "finalizing"
                        run.phase = "reading-result"
                        self._touch(run)
                        threading.Thread(target=self._finalize, args=(run_id, "success"), daemon=True).start()
                return

            self._apply_session_preferences(run)
            if run.resume_requested:
                self._capture_usage_baseline(run)
            if run.goal:
                params = {
                    "sessionId": run.session_id,
                    "inputId": run.input_id,
                    "action": "set",
                    "objective": run.goal,
                }
                if run.native_revision is not None:
                    params["expectedRevision"] = run.native_revision
                goal = self._protocol.request("session/goal", params, timeout=15)
                with self._cv:
                    self._apply_snapshot(run, goal.get("snapshot") or goal)
            with self._cv:
                run.status = "running"
                run.phase = "model"
                run.turn_count = 1
                self._touch(run)
            if run.prompt:
                send_params = {
                    "sessionId": run.session_id,
                    "inputId": run.input_id,
                    "queryId": run.input_id,
                    "content": run.prompt,
                }
                if run.runtime_model:
                    send_params["runtimeModel"] = run.runtime_model
                sent = self._protocol.request("session/send", send_params, timeout=30)
                with self._cv:
                    revision = sent.get("stateRevision") if isinstance(sent, dict) else None
                    if isinstance(revision, int):
                        run.native_revision = revision
            if run.timeout_seconds:
                threading.Thread(target=self._timeout_watch, args=(run_id,), daemon=True).start()
        except Exception as exc:
            with self._cv:
                if run is None:
                    return
                run.status = "stopping" if run.session_id else "failed"
                run.phase = "launch-cleanup" if run.session_id else "launch-failed"
                run.error = _bounded(str(exc), 1200)
                self._event(run, "run.launch-failed", {"message": run.error})
            cleanup = self._cleanup_launch_failure(run)
            with self._cv:
                run.status = "failed"
                run.phase = "launch-failed"
                run.finished_ms = now_ms()
                self._event(run, "run.failed", {
                    "message": run.error,
                    "nativeStopped": cleanup.get("stopped"),
                    "nativeClosed": cleanup.get("closed"),
                })
                self._release(run)

    def _open_session(self, run):
        if run.thread_id:
            params = {"sessionId": run.thread_id}
            if run.runtime_model:
                params["runtimeModel"] = run.runtime_model
            if run.thought_level:
                params["thoughtLevel"] = run.thought_level
            if run.tool_allowlist is not None:
                params["toolAllowlist"] = run.tool_allowlist
            if run.tool_denylist is not None:
                params["toolDenylist"] = run.tool_denylist
            return self._protocol.request("session/resume", params, timeout=30)
        params = {
            "workspace": {"workspacePath": run.cwd, "workspaceKey": run.cwd},
            "persistence": "immediate",
            "titleGenerationEnabled": False,
        }
        if run.runtime_model:
            params["runtimeModel"] = run.runtime_model
            params["model"] = run.runtime_model["model"]
        for key, value in (
            ("mode", run.mode),
            ("model", run.model),
            ("thoughtLevel", run.thought_level),
            ("toolAllowlist", run.tool_allowlist),
            ("toolDenylist", run.tool_denylist),
        ):
            if value is not None:
                params[key] = value
        return self._protocol.request("session/create", params, timeout=30)

    def _apply_session_preferences(self, run):
        if not run.recovered and run.resume_requested and run.model:
            params = {"sessionId": run.session_id, "model": run.model}
            if run.runtime_model:
                params["runtimeModel"] = run.runtime_model
            if run.native_revision is not None:
                params["expectedRevision"] = run.native_revision
            response = self._protocol.request("session/setModel", params, timeout=30)
            self._apply_snapshot(run, response)
        if not run.recovered and run.resume_requested and run.mode:
            params = {"sessionId": run.session_id, "mode": run.mode}
            if run.native_revision is not None:
                params["expectedRevision"] = run.native_revision
            response = self._protocol.request("session/setMode", params, timeout=30)
            self._apply_snapshot(run, response)

    def _subscribe(self, run_id, *, after_seq, include_snapshot):
        with self._lock:
            run = self._get(run_id)
            session_id = run.session_id
        params = {
            "sessionId": session_id,
            "deliveryKind": "web-remote-replayable",
            "includeSnapshot": bool(include_snapshot),
        }
        if after_seq is not None:
            params["afterSeq"] = max(0, int(after_seq))
        result = self._protocol.request(
            "session/subscribe", params, timeout=30
        )
        with self._cv:
            run = self._get(run_id)
            self._apply_snapshot(run, result.get("snapshot") or {})
            event_seq = result.get("eventSeq")
            if isinstance(event_seq, int):
                run.native_event_seq = max(run.native_event_seq, event_seq)
            for event in result.get("events") or []:
                self._apply_native_session_event(run, event, replayed=True)
            self._event(run, "session.subscribed", {
                "eventSeq": run.native_event_seq,
                "replayed": len(result.get("events") or []),
            })
        return result

    def _can_start(self, run):
        if run.status != "queued":
            return False
        if self._max_concurrency and len(self._active) >= self._max_concurrency:
            return False
        try:
            position = self._queue.index(run.run_id)
        except ValueError:
            return False
        for earlier_id in self._queue[:position]:
            earlier = self._runs.get(earlier_id)
            if earlier and self._conflicts(earlier, run):
                return False
        return not any(self._conflicts(self._runs[active_id], run) for active_id in self._active)

    @staticmethod
    def _conflicts(first, second):
        if first.thread_id and first.thread_id == second.thread_id:
            return True
        for key in set(first.resource_modes).intersection(second.resource_modes):
            if "exclusive" in {first.resource_modes[key], second.resource_modes[key]}:
                return True
        return False

    def _timeout_watch(self, run_id):
        with self._lock:
            run = self._get(run_id)
            timeout = max(1, int(run.timeout_seconds))
            started = run.started_ms or now_ms()
        delay = max(0, (started + timeout * 1000 - now_ms()) / 1000.0)
        if delay:
            time.sleep(delay)
        background_only = False
        task_ids = []
        session_id = None
        with self._cv:
            run = self._get(run_id)
            if run.status in TERMINAL_STATES:
                return
            if run.status == "background":
                task_ids = [
                    item.get("taskId") for item in run.background_tasks if item.get("taskId")
                ]
                session_id = run.session_id
                run.timeout_requested = True
                run.status = "stopping"
                run.phase = "background-timeout-cleanup"
                self._event(run, "run.timeout", {"timeoutSeconds": timeout})
                background_only = True
            else:
                run.timeout_requested = True
                run.status = "stopping"
                run.phase = "timeout"
                run.stop_generation += 1
                self._event(run, "run.timeout", {"timeoutSeconds": timeout})
        if background_only:
            cleanup_error = None
            try:
                self._stop_native_session(
                    session_id,
                    task_ids,
                    wait_seconds=self._native_stop_wait_seconds,
                )
            except Exception as exc:
                cleanup_error = _bounded(str(exc), 1200)
            with self._cv:
                run = self._get(run_id)
                run.status = "failed" if cleanup_error else "timed_out"
                run.phase = "resource-cleanup-required" if cleanup_error else "terminal"
                run.finished_ms = now_ms()
                if cleanup_error:
                    run.error = cleanup_error
                    self._event(run, "resource.cleanup-failed", {
                        "message": cleanup_error,
                        "leaseRetained": True,
                    })
                else:
                    self._event(run, "run.timeout-cleanup-complete", {
                        "backgroundTasks": len(task_ids),
                    })
                    self._release(run)
            return
        self._stop_session(run_id, cancel=True)

    def _cancel_native_background(self, session_id, task_ids):
        if not session_id:
            return
        for task_id in task_ids:
            try:
                self._protocol.request(
                    "session/cancelBackgroundTask",
                    {"sessionId": session_id, "taskId": task_id},
                    timeout=15,
                )
            except Exception as exc:
                self._logger("background cancel failed for %s: %s" % (task_id, exc))

    def _stop_native_session(self, session_id, task_ids, *, wait_seconds):
        if not session_id:
            return {"requested": False, "stopped": True}
        self._cancel_native_background(session_id, task_ids)
        stop_error = None
        try:
            self._protocol.request("session/stop", {"sessionId": session_id}, timeout=15)
        except Exception as exc:
            stop_error = _bounded(str(exc), 500)
        stopped = wait_seconds <= 0
        deadline = time.monotonic() + max(0, wait_seconds)
        while not stopped and time.monotonic() < deadline:
            try:
                snapshot = self._protocol.request(
                    "session/read", {"sessionId": session_id, "messageLimit": 1}, timeout=5
                )
                stopped = self._native_snapshot_stopped(snapshot)
            except Exception as exc:
                stop_error = stop_error or _bounded(str(exc), 500)
                break
            if not stopped:
                time.sleep(0.1)
        if wait_seconds > 0 and not stopped:
            raise ControlPlaneError(
                "ZCode session did not stop before close: %s" % session_id,
                "native_stop_timeout",
            )
        result = {"requested": True, "stopped": stopped}
        if stop_error:
            result["warning"] = stop_error
        return result

    @staticmethod
    def _native_snapshot_stopped(snapshot):
        session = snapshot.get("session") if isinstance(snapshot, dict) else None
        projection = snapshot.get("projection") if isinstance(snapshot, dict) else None
        native_status = (
            (session or {}).get("status") or (projection or {}).get("status") or "idle"
        )
        background = (projection or {}).get("backgroundJobs") or []
        live_background = any(
            str((item or {}).get("status") or "").lower() in {
                "pending", "queued", "starting", "running", "waiting", "blocked"
            }
            for item in background if isinstance(item, dict)
        )
        return not live_background and str(native_status).lower() not in {
            "running", "waiting", "starting", "stopping", "recovering"
        }

    def _native_session_stopped(self, session_id):
        snapshot = self._protocol.request(
            "session/read", {"sessionId": session_id, "messageLimit": 1}, timeout=5
        )
        return self._native_snapshot_stopped(snapshot)

    def _cleanup_launch_failure(self, run):
        session_id = run.session_id
        if not session_id:
            return {"stopped": True, "closed": False}
        task_ids = [item.get("taskId") for item in run.background_tasks if item.get("taskId")]
        stopped = False
        closed = False
        try:
            result = self._stop_native_session(session_id, task_ids, wait_seconds=10)
            stopped = bool(result.get("stopped"))
        except Exception as exc:
            self._logger("launch cleanup stop failed for %s: %s" % (session_id, exc))
        if not run.resume_requested and not run.recovered:
            try:
                self._protocol.request("session/close", {"sessionId": session_id}, timeout=30)
                closed = True
            except Exception as exc:
                self._logger("launch cleanup close failed for %s: %s" % (session_id, exc))
        return {"stopped": stopped, "closed": closed}

    def _guide(self, run_id, prompt, *, interrupt, if_revision=None, if_status=None):
        if not isinstance(prompt, str) or not prompt.strip():
            raise ControlPlaneError("prompt is required for guidance", "invalid_params")
        direct = False
        stop = False
        with self._cv:
            run = self._get(run_id)
            if run.status in TERMINAL_STATES:
                raise ControlPlaneError("run is already terminal", "run_terminal")
            if if_revision is not None and int(if_revision) != run.revision:
                raise ControlPlaneError(
                    "control snapshot is stale; observe the run before guiding",
                    "stale_control",
                    {"expectedRevision": int(if_revision), "actualRevision": run.revision},
                )
            if if_status is not None and str(if_status) != run.status:
                raise ControlPlaneError(
                    "run status changed from %s to %s" % (if_status, run.status),
                    "stale_control",
                    {"expectedStatus": str(if_status), "actualStatus": run.status},
                )
            delivery = "interrupt" if interrupt else "after-turn"
            run.pending_guidance.append({"prompt": prompt, "delivery": delivery})
            self._event(run, "guidance.queued", {"delivery": delivery})
            if run.status in {"idle", "paused"} and run.session_id:
                direct = True
            elif interrupt and run.session_id and run.status in {"running", "starting"}:
                run.status = "stopping"
                run.phase = "interrupting-for-guidance"
                run.stop_generation += 1
                stop = True
                self._touch(run)
        if direct:
            self._dispatch_guidance(run_id)
        elif stop:
            self._stop_session(run_id, cancel=False)
        return self.snapshot(run_id, result_chars=0)

    def _stop_session(self, run_id, *, cancel):
        try:
            with self._lock:
                run = self._get(run_id)
                session_id = run.session_id
                generation = run.stop_generation
            if session_id:
                self._protocol.request("session/stop", {"sessionId": session_id}, timeout=15)
        except Exception as exc:
            self._logger("session/stop failed: %s" % exc)
        threading.Thread(
            target=self._stop_fallback,
            args=(run_id, generation, cancel),
            daemon=True,
        ).start()

    def _stop_fallback(self, run_id, generation, cancel):
        deadline = time.monotonic() + 10
        with self._cv:
            run = self._get(run_id)
            while (run.status not in TERMINAL_STATES and
                   run.stop_generation == generation and time.monotonic() < deadline):
                self._cv.wait(max(0, deadline - time.monotonic()))
            if run.status in TERMINAL_STATES or run.stop_generation != generation:
                return
            if run.pending_guidance and not cancel:
                threading.Thread(target=self._dispatch_guidance, args=(run_id,), daemon=True).start()
                return
            run.status = "timed_out" if run.timeout_requested else "cancelled"
            run.phase = "stopped"
            run.finished_ms = now_ms()
            self._event(run, "run.terminal", {"status": run.status, "fallback": True})
            self._release(run)

    def _dispatch_guidance(self, run_id):
        with self._cv:
            run = self._get(run_id)
            if (not run.pending_guidance or run.cancel_requested or
                    run.guidance_dispatching):
                return
            guidance = run.pending_guidance[0]
            run.guidance_dispatching = True
            run.stop_generation += 1
            run.phase = "guidance-waiting-for-ready"
            input_id = "input_" + uuid.uuid4().hex
            self._event(run, "guidance.waiting", {"delivery": guidance["delivery"]})
            session_id = run.session_id
            runtime_model = run.runtime_model
        sent = None
        deadline = time.monotonic() + self._guidance_retry_seconds
        delay = 0.05
        try:
            while True:
                with self._lock:
                    current = self._get(run_id)
                    if current.cancel_requested or not current.pending_guidance:
                        current.guidance_dispatching = False
                        return
                send_params = {
                    "sessionId": session_id,
                    "inputId": input_id,
                    "queryId": input_id,
                    "content": guidance["prompt"],
                }
                if runtime_model:
                    send_params["runtimeModel"] = runtime_model
                try:
                    sent = self._protocol.request("session/send", send_params, timeout=30)
                    break
                except Exception as exc:
                    message = str(exc).lower()
                    retryable = (
                        "prompt is already running" in message or
                        "already has a running prompt" in message
                    )
                    if not retryable or time.monotonic() >= deadline:
                        raise
                    with self._cv:
                        current = self._get(run_id)
                        self._event(current, "guidance.retrying", {
                            "reason": _bounded(str(exc), 300),
                            "retryInMs": int(delay * 1000),
                        })
                    time.sleep(delay)
                    delay = min(delay * 2, 1.0)
            with self._cv:
                run = self._get(run_id)
                if run.pending_guidance and run.pending_guidance[0] is guidance:
                    run.pending_guidance.popleft()
                run.guidance_dispatching = False
                run.last_terminal_status = None
                run.status = "running"
                run.phase = "guided-turn"
                run.turn_count += 1
                if isinstance(sent.get("stateRevision"), int):
                    run.native_revision = sent["stateRevision"]
                self._event(run, "guidance.sent", {"delivery": guidance["delivery"]})
        except Exception as exc:
            finalize_status = None
            native_stopped = False
            try:
                native_stopped = self._native_session_stopped(session_id)
            except Exception:
                pass
            with self._cv:
                run = self._get(run_id)
                if run.pending_guidance and run.pending_guidance[0] is guidance:
                    run.pending_guidance.popleft()
                run.guidance_dispatching = False
                failure = {
                    "action": guidance["delivery"],
                    "message": _bounded(str(exc), 1200),
                    "atMs": now_ms(),
                }
                run.control_failures.append(failure)
                self._event(run, "guidance.failed", failure)
                if native_stopped and str(run.last_terminal_status or "").lower() in {
                    "success", "completed", "complete", "end"
                }:
                    finalize_status = run.last_terminal_status
                    run.status = "finalizing"
                    run.phase = "reading-result-after-control-failure"
                    self._touch(run)
                else:
                    run.status = "failed"
                    run.phase = (
                        "guidance-failed" if native_stopped else
                        "resource-cleanup-required"
                    )
                    run.error = failure["message"]
                    run.finished_ms = now_ms()
                    self._event(run, "run.failed", {
                        "message": run.error,
                        "leaseRetained": not native_stopped,
                    })
                    if native_stopped:
                        self._release(run)
            if finalize_status is not None:
                self._finalize(run_id, finalize_status)

    def _on_notification(self, message):
        if not isinstance(message, dict):
            return
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        session_id = params.get("sessionId") or _find_key(params, {"sessionId"})
        if not session_id:
            return
        with self._cv:
            run_id = self._session_runs.get(str(session_id))
            if not run_id:
                return
            run = self._runs.get(run_id)
            if not run or run.status in TERMINAL_STATES:
                return
            run.last_activity_ms = now_ms()
            method = message.get("method")
            if method == "state.updated":
                revision = params.get("revision")
                if isinstance(revision, int):
                    run.native_revision = max(run.native_revision or 0, revision)
                self._apply_snapshot(run, params.get("patch") or {})
                return
            if method == "session/event":
                self._apply_native_session_event(run, params, replayed=False)
                return
            name = str(params.get("kind") or params.get("type") or params.get("name") or "")
            lowered = name.lower()
            if lowered == "stream.chunk":
                self._stream_event(run, params)
            elif "usage" in lowered:
                self._add_usage(run, params)
            elif "model.request" in lowered:
                self._model_event(run, params)
            elif "tool.lifecycle" in lowered or lowered.startswith("tool."):
                self._tool_event(run, params)
            elif "subagent.lifecycle" in lowered or "subagent" in lowered:
                self._subagent_event(run, params)
            elif "turn.started" in lowered:
                run.native_status = "running"
                if run.status != "compacting":
                    run.phase = "model"
                self._event(run, "turn.started", {"turnId": params.get("turnId")})
            elif "compaction.terminal" in lowered:
                self._compact_terminal(run, str(params.get("status") or "completed").lower())
            elif "turn.terminal" in lowered or lowered.endswith("turn_terminal"):
                status = str(params.get("status") or "completed").lower()
                self._turn_terminal(run, status)

    def _apply_native_session_event(self, run, event, *, replayed):
        if not isinstance(event, dict):
            return
        seq = event.get("seq")
        if isinstance(seq, int):
            if seq <= run.native_event_seq and not replayed:
                return
            run.native_event_seq = max(run.native_event_seq, seq)
        event_type = str(event.get("type") or "session.event")
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        is_goal_verification = bool(
            payload.get("targetId") and payload.get("verificationId") and
            str(payload.get("status") or "").lower() in {
                "started", "completed", "complete", "failed_closed", "cancelled"
            }
        )
        is_target_change = isinstance(payload.get("target"), dict)
        if event_type == "turn.started":
            run.native_status = "running"
        elif event_type in {"turn.completed", "turn.failed", "turn.cancelled"}:
            run.native_status = "idle"
            if run.status not in {"finalizing"} and run.status not in TERMINAL_STATES:
                status = "success" if event_type == "turn.completed" else event_type.split(".")[-1]
                self._turn_terminal(run, status)
                return
        elif event_type == "checkpoint.created":
            run.last_checkpoint = {
                key: payload.get(key) for key in (
                    "checkpointId", "messageId", "targetMessageId", "fileCount", "scope"
                ) if payload.get(key) is not None
            }
        elif is_goal_verification:
            verification = payload.get("verification") if isinstance(payload.get("verification"), dict) else {}
            verification_status = str(payload.get("status") or "").lower()
            # Both TargetCompletionVerification and TargetChanged are mapped by
            # ZCode's public protocol to session.updated.  Preserve the current
            # target here and wait for the following payload.target update;
            # completion verification passing precedes target persistence.
            target = dict(run.goal_state or {})
            if payload.get("targetId"):
                target["targetId"] = payload["targetId"]
            if verification:
                target["verification"] = verification
            run.goal_state = target
            self._event(run, "goal.verification-%s" % (verification_status or "updated"), {
                "passed": verification.get("passed"),
                "verificationId": payload.get("verificationId"),
            })
        elif is_target_change or event_type.startswith("goal.") or "target" in event_type:
            target = payload.get("target") if is_target_change else payload
            if target is None:
                run.goal_state = None
                self._event(run, "goal.cleared", {})
                return
            run.goal_state = target
            if (run.goal and str(target.get("status") or "").lower() == "complete" and
                    str(run.native_status or "").lower() == "idle" and
                    run.status not in TERMINAL_STATES and run.status != "finalizing"):
                self._turn_terminal(run, "success")
                return
        task_status = str(payload.get("status") or "").lower()
        if (run.status == "background" and payload.get("taskId") and
                task_status in {"completed", "failed", "timed_out", "cancelled", "spawn_error", "lost"}):
            threading.Thread(
                target=self._refresh_after_background_event,
                args=(run.run_id,),
                daemon=True,
            ).start()
        self._event(run, "native.%s" % event_type, {
            "nativeSeq": seq,
            "replayed": replayed or None,
        })

    def _stream_event(self, run, params):
        channel = str(params.get("channel") or "unknown")
        first = bool(params.get("firstChunk"))
        previous = run.model_state.get("lastChannel")
        run.model_state["lastChannel"] = channel
        run.model_state["status"] = "streaming"
        run.model_state["reasoningActive"] = channel in {"reasoning", "analysis", "thought"}
        run.phase = "reasoning" if run.model_state["reasoningActive"] else "model-output"
        run.last_progress_ms = now_ms()
        if first or channel != previous:
            self._event(run, "model.stream", {"channel": channel, "first": first or None})

    def _model_event(self, run, params):
        status = str(params.get("status") or "update").lower()
        if status in {"started", "start", "running", "model_request_started"} or status.endswith("_started"):
            # A new model iteration cannot begin until all foreground tools from
            # the previous iteration have ended. Durable background work is
            # represented separately by projection.backgroundJobs.
            run.active_tools.clear()
            run.usage["modelRequests"] += 1
            run.session_usage["modelRequests"] += 1
            run.model_state.update({
                "status": "running",
                "reasoningActive": False,
                "lastChannel": None,
                "requestId": params.get("requestId"),
                "startedAt": params.get("occurredAt") or now_ms(),
            })
            run.phase = "model"
            self._event(run, "model.started", {
                "modelId": params.get("modelId"),
                "providerId": params.get("providerId"),
            })
        elif "error" in status or "failed" in status:
            run.usage["modelErrors"] += 1
            run.session_usage["modelErrors"] += 1
            run.model_state["status"] = "error"
            self._event(run, "model.error", {"status": status})
        elif "completed" in status or status in {"success", "done"}:
            run.model_state["status"] = "completed"
            run.model_state["reasoningActive"] = False
            self._event(run, "model.completed", {"durationMs": params.get("durationMs")})

    def _add_usage(self, run, params):
        aliases = {
            "totalTokens": {"totalTokens", "total_tokens"},
            "inputTokens": {"inputTokens", "input_tokens", "promptTokens"},
            "outputTokens": {"outputTokens", "output_tokens", "completionTokens"},
            "reasoningTokens": {"reasoningTokens", "reasoning_tokens"},
            "cacheReadTokens": {"cacheReadTokens", "cache_read_tokens"},
            "cacheWriteTokens": {"cacheWriteTokens", "cache_write_tokens", "cacheCreationTokens"},
        }
        for target, names in aliases.items():
            value = _find_key(params, names)
            if isinstance(value, (int, float)):
                run.usage[target] += int(value)
                run.session_usage[target] += int(value)

    def _tool_event(self, run, params):
        tool_id = str(params.get("toolCallId") or params.get("toolUseId") or params.get("id") or "unknown")
        name = str(params.get("toolName") or params.get("name") or "tool")
        status = str(params.get("status") or params.get("state") or params.get("phase") or "update").lower()
        if status in {"scheduled", "started", "running", "start", "tool_started"}:
            if tool_id not in run.active_tools:
                run.counts["toolCalls"] += 1
                run.active_tools[tool_id] = {
                    "id": tool_id, "name": name, "status": "running", "startedAt": now_ms()
                }
                run.phase = "tool"
                self._event(run, "tool.started", {"name": name, "id": tool_id})
        elif status in {"completed", "success", "failed", "cancelled", "error", "end", "tool_completed"}:
            existed = run.active_tools.pop(tool_id, None)
            if existed:
                self._event(run, "tool.%s" % status, {"name": name, "id": tool_id})
            if run.status == "background":
                threading.Thread(
                    target=self._refresh_after_background_event,
                    args=(run.run_id,),
                    daemon=True,
                ).start()

    def _refresh_after_background_event(self, run_id):
        try:
            self._refresh_native(run_id)
        except Exception as exc:
            self._logger("background completion refresh failed: %s" % exc)

    def _settle_background_projection(self, run_id):
        time.sleep(0.2)
        self._refresh_after_background_event(run_id)

    def _subagent_event(self, run, params):
        agent_id = str(params.get("agentId") or params.get("taskId") or params.get("id") or "unknown")
        status = str(params.get("status") or params.get("state") or params.get("phase") or "update").lower()
        if status in {"scheduled", "started", "running", "start"}:
            run.counts["subagents"] += 1
            run.phase = "subagent"
            self._event(run, "subagent.started", {"agentId": agent_id})
        elif status in {"completed", "success", "failed", "cancelled", "error", "end", "lost"}:
            self._event(run, "subagent.%s" % status, {"agentId": agent_id})

    def _turn_terminal(self, run, status):
        if run.status == "finalizing" or run.status in TERMINAL_STATES:
            return
        run.active_tools.clear()
        run.model_state["status"] = "idle"
        run.model_state["reasoningActive"] = False
        run.native_status = "idle"
        run.last_terminal_status = status
        self._event(run, "turn.terminal", {"status": status})
        goal_status = str((run.goal_state or {}).get("status") or "").lower()
        if run.goal and run.status == "starting" and goal_status in {"", "active"}:
            run.phase = "goal-starting"
            self._event(run, "goal.control-terminal", {"status": status})
            return
        if run.pending_guidance and not run.cancel_requested:
            threading.Thread(target=self._dispatch_guidance, args=(run.run_id,), daemon=True).start()
            return
        if run.cancel_requested or run.timeout_requested or status in {"cancelled", "interrupted", "stopped"}:
            run.status = "timed_out" if run.timeout_requested else "cancelled"
            run.phase = "terminal"
            run.finished_ms = now_ms()
            self._release(run)
            return
        if run.goal and goal_status == "active":
            run.status = "running"
            run.phase = "goal-continuing"
            run.finished_ms = None
            self._event(run, "goal.iteration-terminal", {"status": status})
            return
        run.status = "finalizing"
        run.phase = "reading-result"
        self._touch(run)
        threading.Thread(target=self._finalize, args=(run.run_id, status), daemon=True).start()

    def _compact_terminal(self, run, status):
        if run.status != "compacting":
            return
        run.model_state["status"] = "idle"
        run.model_state["reasoningActive"] = False
        run.native_status = "idle"
        failed = status in {"failed", "error", "cancelled"}
        run.status = "failed" if failed else (run.pre_compact_status or "completed")
        run.phase = "compact-failed" if failed else "terminal"
        run.finished_ms = now_ms()
        run.pre_compact_status = None
        self._event(run, "context.compact-terminal", {"status": status})
        self._release(run)
        threading.Thread(target=self._refresh_after_compact, args=(run.run_id,), daemon=True).start()

    def _refresh_after_compact(self, run_id):
        try:
            self._refresh_native(run_id)
        except Exception as exc:
            self._logger("post-compact refresh failed: %s" % exc)

    def _finalize(self, run_id, terminal_status):
        text = ""
        error = None
        snapshot = {}
        try:
            with self._lock:
                session_id = self._get(run_id).session_id
            snapshot = self._protocol.request(
                "session/messages", {"sessionId": session_id, "limit": 6}, timeout=30
            )
            text = extract_last_assistant_text(snapshot)
            self._refresh_native(run_id)
        except Exception as exc:
            error = _bounded(str(exc), 1200)
            if not text:
                try:
                    snapshot = self._protocol.request(
                        "session/read", {"sessionId": session_id, "messageLimit": 6}, timeout=30
                    )
                    text = extract_last_assistant_text(snapshot)
                except Exception:
                    pass
        with self._cv:
            run = self._get(run_id)
            self._apply_snapshot(run, snapshot)
            run.result = text[:MAX_STORED_RESULT]
            if error and not text:
                run.error = error
            failed = terminal_status in {"failed", "error"}
            has_background = self._has_live_background(run)
            run.status = "failed" if failed else ("background" if has_background else "completed")
            run.phase = "background" if has_background and not failed else "terminal"
            run.native_status = "idle"
            run.model_state["status"] = "idle"
            run.model_state["reasoningActive"] = False
            run.finished_ms = None if has_background and not failed else now_ms()
            self._event(run, "run.terminal", {"status": run.status})
            if not has_background or failed:
                self._release(run)
            else:
                # Raw turn telemetry can precede the final projection update.
                # Re-read once after it settles so a completed foreground tool
                # is not mistaken for durable background work.
                threading.Thread(
                    target=self._settle_background_projection,
                    args=(run.run_id,),
                    daemon=True,
                ).start()

    def _refresh_native(self, run_id):
        with self._lock:
            run = self._get(run_id)
            session_id = run.session_id
            if not session_id:
                raise ControlPlaneError("session is not ready", "session_not_ready")
        snapshot = self._protocol.request(
            "session/read", {"sessionId": session_id, "messageLimit": 1}, timeout=30
        )
        usage = self._protocol.request("session/usage", {"sessionId": session_id}, timeout=30)
        agents = self._protocol.request(
            "session/subagents", {"sessionId": session_id, "endedLimit": 20}, timeout=30
        )
        with self._cv:
            run = self._get(run_id)
            self._apply_snapshot(run, snapshot)
            self._apply_usage_snapshot(run, usage)
            self._apply_subagents(run, agents)
            goal_status = str((run.goal_state or {}).get("status") or "").lower()
            if (run.goal and goal_status == "complete" and
                    run.status not in TERMINAL_STATES and run.status != "finalizing"):
                self._turn_terminal(run, "success")
            if run.status == "background" and not self._has_live_background(run):
                run.status = "completed"
                run.phase = "terminal"
                run.finished_ms = now_ms()
                self._event(run, "background.all-terminal", {})
                self._release(run)
            self._touch(run)

    def _apply_usage_snapshot(self, run, usage):
        if not isinstance(usage, dict):
            return
        mapping = {
            "totalTokens": "totalTokens",
            "inputTokens": "inputTokens",
            "outputTokens": "outputTokens",
            "reasoningTokens": "reasoningTokens",
            "cacheCreationTokens": "cacheWriteTokens",
            "cacheReadTokens": "cacheReadTokens",
            "modelRequestCount": "modelRequests",
            "modelErrorCount": "modelErrors",
        }
        for source, target in mapping.items():
            value = usage.get(source)
            if isinstance(value, (int, float)):
                total = int(value)
                run.session_usage[target] = total
                if run.usage_baseline_captured:
                    run.usage[target] = max(0, total - run.usage_baseline.get(target, 0))
                else:
                    run.usage[target] = total

    def _capture_usage_baseline(self, run):
        usage = self._protocol.request(
            "session/usage", {"sessionId": run.session_id}, timeout=15
        )
        mapping = {
            "totalTokens": "totalTokens",
            "inputTokens": "inputTokens",
            "outputTokens": "outputTokens",
            "reasoningTokens": "reasoningTokens",
            "cacheCreationTokens": "cacheWriteTokens",
            "cacheReadTokens": "cacheReadTokens",
            "modelRequestCount": "modelRequests",
            "modelErrorCount": "modelErrors",
        }
        for source, target in mapping.items():
            value = usage.get(source) if isinstance(usage, dict) else None
            if isinstance(value, (int, float)):
                run.usage_baseline[target] = int(value)
                run.session_usage[target] = int(value)
        run.usage_baseline_captured = True

    def _apply_subagents(self, run, value):
        if not isinstance(value, dict):
            return
        running = [_compact_agent(item) for item in value.get("running") or []]
        ended_value = value.get("ended") if isinstance(value.get("ended"), dict) else {}
        ended = [_compact_agent(item) for item in ended_value.get("items") or []]
        run.subagents = {
            "running": [item for item in running if item],
            "ended": [item for item in ended if item],
            "endedTotal": ended_value.get("total", len(ended)),
        }

    def _apply_snapshot(self, run, snapshot):
        if not isinstance(snapshot, dict):
            return
        session = snapshot.get("session") if isinstance(snapshot.get("session"), dict) else {}
        projection = snapshot.get("projection") if isinstance(snapshot.get("projection"), dict) else {}
        runtime = snapshot.get("runtime") if isinstance(snapshot.get("runtime"), dict) else {}
        settings = snapshot.get("settings") if isinstance(snapshot.get("settings"), dict) else {}
        status = session.get("status") or projection.get("status")
        if status:
            run.native_status = status
        selected_model = session.get("model")
        if not isinstance(selected_model, dict):
            model_settings = settings.get("model") if isinstance(settings.get("model"), dict) else {}
            selected_model = model_settings.get("current")
        if isinstance(selected_model, dict):
            run.model_state["model"] = {
                key: selected_model.get(key) for key in ("providerId", "modelId", "variant")
                if selected_model.get(key) is not None
            }
        thought_settings = settings.get("thoughtLevel") if isinstance(settings.get("thoughtLevel"), dict) else {}
        if thought_settings.get("current"):
            run.model_state["thoughtLevel"] = thought_settings["current"]
        state_revision = runtime.get("stateRevision") or snapshot.get("stateRevision")
        if isinstance(state_revision, int):
            run.native_revision = max(run.native_revision or 0, state_revision)
        event_seq = runtime.get("eventSeq") or snapshot.get("eventSeq")
        if isinstance(event_seq, int):
            run.native_event_seq = max(run.native_event_seq, event_seq)
        jobs = projection.get("backgroundJobs")
        if isinstance(jobs, list):
            compact = [_compact_background(item) for item in jobs]
            run.background_tasks = [item for item in compact if item]
        target = session.get("target") or projection.get("target")
        if target is not None:
            run.goal_state = target
        context = runtime.get("contextUsage") if isinstance(runtime.get("contextUsage"), dict) else {}
        size = context.get("size") or projection.get("contextWindow")
        used = context.get("used") or projection.get("contextUsed")
        cache = context.get("cache") if isinstance(context.get("cache"), dict) else {}
        if size is not None or used is not None or cache:
            run.context = {
                "window": size,
                "used": used,
                "usedRatio": round(float(used) / float(size), 4) if size and used is not None else None,
                "cacheHitRate": cache.get("hitRate"),
                "latestCacheHitRate": cache.get("latestHitRate"),
                "cacheReadTokens": cache.get("totalCacheReadTokens") or cache.get("cacheReadTokens"),
                "cacheWriteTokens": cache.get("totalCacheWriteTokens") or cache.get("cacheWriteTokens"),
            }
        checkpoint = projection.get("lastCheckpoint") or session.get("lastCheckpoint")
        if isinstance(checkpoint, dict):
            run.last_checkpoint = checkpoint

    def _on_disconnect(self, message):
        with self._cv:
            if self._transport_recovering:
                return
            candidates = []
            for run_id in list(self._active):
                run = self._runs.get(run_id)
                if not run or run.status in TERMINAL_STATES or not run.session_id:
                    continue
                run.status = "recovering"
                run.phase = "transport-recovering"
                self._event(run, "transport.lost", {"message": _bounded(message, 400)})
                candidates.append(run_id)
            if not candidates:
                return
            self._transport_recovering = True
        threading.Thread(target=self._recover_transport, args=(candidates,), daemon=True).start()

    def _recover_transport(self, run_ids):
        try:
            for run_id in run_ids:
                try:
                    with self._lock:
                        run = self._get(run_id)
                        session_id = run.session_id
                        after_seq = run.native_event_seq
                    resume_params = {"sessionId": session_id}
                    if run.runtime_model:
                        resume_params["runtimeModel"] = run.runtime_model
                    if run.thought_level:
                        resume_params["thoughtLevel"] = run.thought_level
                    resumed = self._protocol.request(
                        "session/resume", resume_params, timeout=30
                    )
                    self._lease_store.set_guard_pid(
                        getattr(self._protocol, "process_id", None)
                    )
                    with self._cv:
                        run = self._get(run_id)
                        self._apply_snapshot(run, resumed)
                    self._subscribe(run_id, after_seq=after_seq, include_snapshot=True)
                    with self._cv:
                        run = self._get(run_id)
                        if str(run.native_status).lower() in {"running", "waiting", "paused"}:
                            run.status = "running" if run.native_status != "paused" else "paused"
                            run.phase = "transport-recovered"
                            self._event(run, "transport.recovered", {})
                        else:
                            self._turn_terminal(run, "success")
                except Exception as exc:
                    with self._cv:
                        run = self._get(run_id)
                        run.status = "failed"
                        run.phase = "transport-recovery-failed"
                        run.error = _bounded(str(exc), 1200)
                        run.finished_ms = now_ms()
                        self._event(run, "run.failed", {"message": run.error})
                        self._release(run)
        finally:
            with self._cv:
                self._transport_recovering = False

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

    @staticmethod
    def _has_live_background(run):
        live = {"pending", "queued", "starting", "running", "waiting", "blocked"}
        return any(
            str(item.get("status") or "").lower() in live
            for item in run.background_tasks
        )

    def _event(self, run, kind, detail):
        run.seq += 1
        event = {"seq": run.seq, "type": kind, "atMs": now_ms()}
        compact = {key: value for key, value in (detail or {}).items() if value not in (None, "", {})}
        if compact:
            event["detail"] = compact
        run.events.append(event)
        run.last_progress_ms = event["atMs"]
        self._touch(run)

    def _touch(self, run):
        run.revision += 1
        self._cv.notify_all()

    def _snapshot(self, run, *, result_chars=2000):
        end = run.finished_ms or now_ms()
        result = {
            "runId": run.run_id,
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
            "native": {
                "status": run.native_status,
                "stateRevision": run.native_revision,
                "eventSeq": run.native_event_seq,
                "lastActivityAtMs": run.last_activity_ms,
                "lastProgressAtMs": run.last_progress_ms,
            },
            "model": dict(run.model_state),
            "usage": dict(run.usage),
            "sessionUsage": dict(run.session_usage),
            "counts": dict(run.counts),
            "activeTools": list(run.active_tools.values())[:12],
            "backgroundTasks": run.background_tasks[:16],
            "controlFailures": list(run.control_failures),
            "permissionPolicy": {
                "headless": True,
                "workspaceAccess": run.workspace_access,
                "structuredPathsEnforced": True,
                "shellBoundary": "advisory",
                "allowedRoots": [run.cwd] + [
                    key for key in run.resource_modes
                    if os.path.isabs(key) and key != run.cwd
                ],
                "rootModes": {
                    key: mode for key, mode in run.resource_modes.items()
                    if os.path.isabs(key)
                },
            },
            "subagents": run.subagents,
            "context": run.context,
            "goal": run.goal_state,
            "lastCheckpoint": run.last_checkpoint,
            "lastSeq": run.seq,
        }
        if run.error:
            result["error"] = run.error
        result_chars = min(max(int(result_chars), 0), MAX_STORED_RESULT)
        if result_chars and run.result:
            result["result"] = run.result[:result_chars]
            result["resultTruncated"] = len(run.result) > result_chars
        result["next"] = (
            "wait with afterRevision=%s" % run.revision
            if run.status not in TERMINAL_STATES else
            "observe for bounded detail, branch/compact, or close"
        )
        return result

    @staticmethod
    def _compact_session(value):
        if not isinstance(value, dict):
            return None
        workspace = value.get("workspace") if isinstance(value.get("workspace"), dict) else {}
        return {
            "threadId": extract_session_id(value),
            "status": value.get("status"),
            "title": _bounded(value.get("title"), 240),
            "mode": value.get("mode"),
            "sessionKind": value.get("sessionKind"),
            "cwd": workspace.get("workspacePath"),
            "createdAt": value.get("createdAt"),
            "updatedAt": value.get("updatedAt"),
        }
