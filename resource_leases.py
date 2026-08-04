"""Cross-process resource leases for local ZCode MCP bridge instances."""

from __future__ import annotations

from contextlib import contextmanager
import os
import sqlite3
import threading
import time
import uuid


def _now_ms():
    return int(time.time() * 1000)


def _pid_is_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except (AttributeError, OSError):
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # Windows may not support signal 0 for every foreign process. A fresh
        # heartbeat is still authoritative; stale rows are reclaimed later.
        return True
    return True


class ResourceLeaseStore:
    """Coordinate shared/exclusive resources across MCP server processes.

    SQLite provides the short atomic critical section. The bridge process owns
    a renewable lease, so an ordinary close releases immediately and a crashed
    owner is reclaimed after its heartbeat becomes stale and its PID is gone.
    """

    def __init__(self, path=None, *, owner_id=None, heartbeat_seconds=2.0,
                 stale_seconds=30.0, poll_seconds=0.2):
        default_path = os.path.expanduser("~/.zcode/cli/zcode-mcp-resource-leases.sqlite")
        self.path = os.path.realpath(path or os.environ.get("ZCODE_MCP_LEASE_DB") or default_path)
        self.owner_id = owner_id or "bridge_%s_%s" % (os.getpid(), uuid.uuid4().hex)
        self.pid = os.getpid()
        self.guard_pid = None
        self.heartbeat_seconds = max(float(heartbeat_seconds), 0.05)
        self.stale_ms = max(int(float(stale_seconds) * 1000), 100)
        self.poll_seconds = max(float(poll_seconds), 0.02)
        self._closed = False
        self._lock = threading.RLock()
        self._wake = threading.Event()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._initialize()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name="zcode-resource-lease-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.execute("pragma busy_timeout=5000")
        connection.execute("pragma journal_mode=WAL")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self):
        with self._connection() as connection:
            connection.executescript(
                """
                create table if not exists bridge_owner (
                    owner_id text primary key,
                    pid integer not null,
                    heartbeat_ms integer not null,
                    guard_pid integer
                );
                create table if not exists resource_lease (
                    owner_id text not null,
                    run_id text not null,
                    resource_key text not null,
                    mode text not null check(mode in ('shared', 'exclusive')),
                    acquired_ms integer not null,
                    updated_ms integer not null,
                    primary key(owner_id, run_id, resource_key),
                    foreign key(owner_id) references bridge_owner(owner_id) on delete cascade
                );
                create index if not exists resource_lease_key
                    on resource_lease(resource_key);
                """
            )
            columns = {
                row[1] for row in connection.execute("pragma table_info(bridge_owner)")
            }
            if "guard_pid" not in columns:
                connection.execute("alter table bridge_owner add column guard_pid integer")

    def try_acquire(self, run_id, resource_modes):
        """Atomically acquire every declared resource or none of them."""
        if not resource_modes:
            return {"acquired": True, "blockers": []}
        requested = {
            str(key): str(mode)
            for key, mode in resource_modes.items()
        }
        if any(mode not in {"shared", "exclusive"} for mode in requested.values()):
            raise ValueError("resource mode must be shared or exclusive")
        now = _now_ms()
        with self._lock:
            if self._closed:
                raise RuntimeError("resource lease store is closed")
            connection = self._connect()
            try:
                connection.execute("begin immediate")
                self._reclaim_stale(connection, now)
                connection.execute(
                    "insert into bridge_owner(owner_id,pid,heartbeat_ms,guard_pid) values(?,?,?,?) "
                    "on conflict(owner_id) do update set pid=excluded.pid, "
                    "heartbeat_ms=excluded.heartbeat_ms, guard_pid=excluded.guard_pid",
                    (self.owner_id, self.pid, now, self.guard_pid),
                )
                placeholders = ",".join("?" for _ in requested)
                rows = connection.execute(
                    "select owner_id,run_id,resource_key,mode from resource_lease "
                    "where resource_key in (%s) and not (owner_id=? and run_id=?)" % placeholders,
                    (*requested.keys(), self.owner_id, run_id),
                ).fetchall()
                blockers = [
                    {
                        "ownerId": owner_id,
                        "runId": other_run,
                        "key": key,
                        "mode": mode,
                    }
                    for owner_id, other_run, key, mode in rows
                    if "exclusive" in {mode, requested[key]}
                ]
                if blockers:
                    connection.rollback()
                    return {"acquired": False, "blockers": blockers[:12]}
                for key, mode in requested.items():
                    connection.execute(
                        "insert into resource_lease(owner_id,run_id,resource_key,mode,acquired_ms,updated_ms) "
                        "values(?,?,?,?,?,?) on conflict(owner_id,run_id,resource_key) do update set "
                        "mode=excluded.mode, updated_ms=excluded.updated_ms",
                        (self.owner_id, run_id, key, mode, now, now),
                    )
                connection.commit()
                return {"acquired": True, "blockers": []}
            finally:
                connection.close()

    def release(self, run_id):
        with self._lock:
            if self._closed:
                return
            with self._connection() as connection:
                connection.execute(
                    "delete from resource_lease where owner_id=? and run_id=?",
                    (self.owner_id, run_id),
                )
                self._remove_owner_if_empty(connection)
        self._wake.set()

    def set_guard_pid(self, pid):
        """Record the native app-server PID that must also be gone before reclaim."""
        normalized = int(pid) if pid else None
        with self._lock:
            if self._closed:
                return
            self.guard_pid = normalized
            with self._connection() as connection:
                connection.execute(
                    "update bridge_owner set guard_pid=? where owner_id=?",
                    (normalized, self.owner_id),
                )

    def snapshot(self, run_id):
        with self._connection() as connection:
            rows = connection.execute(
                "select resource_key,mode,acquired_ms,updated_ms from resource_lease "
                "where owner_id=? and run_id=? order by resource_key",
                (self.owner_id, run_id),
            ).fetchall()
        return [
            {"key": key, "mode": mode, "acquiredAtMs": acquired, "updatedAtMs": updated}
            for key, mode, acquired, updated in rows
        ]

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            with self._connection() as connection:
                connection.execute("delete from resource_lease where owner_id=?", (self.owner_id,))
                connection.execute("delete from bridge_owner where owner_id=?", (self.owner_id,))
        self._wake.set()
        self._heartbeat_thread.join(timeout=1)

    def wait_poll(self):
        self._wake.wait(self.poll_seconds)
        self._wake.clear()

    def _heartbeat_loop(self):
        while not self._wake.wait(self.heartbeat_seconds):
            with self._lock:
                if self._closed:
                    return
                now = _now_ms()
                try:
                    with self._connection() as connection:
                        updated = connection.execute(
                            "update bridge_owner set heartbeat_ms=? where owner_id=?",
                            (now, self.owner_id),
                        ).rowcount
                        if updated:
                            connection.execute(
                                "update resource_lease set updated_ms=? where owner_id=?",
                                (now, self.owner_id),
                            )
                        self._reclaim_stale(connection, now)
                except sqlite3.Error:
                    # Acquisition remains conservative: an unavailable registry
                    # never grants a conflicting lease.
                    continue

    def _reclaim_stale(self, connection, now):
        rows = connection.execute(
            "select owner_id,pid,heartbeat_ms,guard_pid from bridge_owner "
            "where owner_id<>? and heartbeat_ms<?",
            (self.owner_id, now - self.stale_ms),
        ).fetchall()
        for owner_id, pid, _heartbeat, guard_pid in rows:
            if not _pid_is_alive(pid) and not _pid_is_alive(guard_pid):
                connection.execute("delete from resource_lease where owner_id=?", (owner_id,))
                connection.execute("delete from bridge_owner where owner_id=?", (owner_id,))

    def _remove_owner_if_empty(self, connection):
        count = connection.execute(
            "select count(*) from resource_lease where owner_id=?", (self.owner_id,)
        ).fetchone()[0]
        if not count:
            connection.execute("delete from bridge_owner where owner_id=?", (self.owner_id,))

    def diagnostics(self):
        with self._connection() as connection:
            owners = connection.execute("select count(*) from bridge_owner").fetchone()[0]
            leases = connection.execute("select count(*) from resource_lease").fetchone()[0]
        return {
            "path": self.path,
            "ownerId": self.owner_id,
            "guardPid": self.guard_pid,
            "owners": owners,
            "leases": leases,
            "staleAfterMs": self.stale_ms,
        }


class NullResourceLeaseStore:
    """Test/helper store that keeps scheduling local to one control plane."""

    poll_seconds = 0.05

    def try_acquire(self, _run_id, _resource_modes):
        return {"acquired": True, "blockers": []}

    def release(self, _run_id):
        return None

    def set_guard_pid(self, _pid):
        return None

    def snapshot(self, _run_id):
        return []

    def close(self):
        return None

    def wait_poll(self):
        time.sleep(self.poll_seconds)

    def diagnostics(self):
        return {"scope": "process-local"}
