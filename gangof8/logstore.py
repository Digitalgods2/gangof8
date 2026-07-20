"""Memory/Log Store — SQLite for session state + per-session JSONL trail."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Optional

from .models import Session, utcnow


class LogStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.sessions_dir = self.data_dir / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "gangof8.db"
        # Parallel panel threads can log in the same microsecond. Windows text
        # append handles are not record-atomic; without a process lock one JSON
        # line could be overwritten by a blank/partial sibling write.
        self._event_lock = threading.Lock()
        self._feed_cond = threading.Condition()
        self._feed: list[dict] = []
        self._feed_seq = 0
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        # timeout covers writer contention between the API thread and
        # background session workers (Phase 5 service mode)
        return sqlite3.connect(self.db_path, timeout=10)

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS sessions (
                       session_id TEXT PRIMARY KEY,
                       status     TEXT NOT NULL,
                       created_at TEXT NOT NULL,
                       updated_at TEXT NOT NULL,
                       json       TEXT NOT NULL
                   )"""
            )

    def save_session(self, session: Session) -> bool:
        """Persist a session, rejecting writes from a superseded worker lease.

        Ordinary synchronous sessions have no lease and retain the historical
        behaviour.  Background workers carry a token claimed in SQLite; a
        restart/retry revokes it, so an old thread can finish its subprocess but
        can never overwrite the authoritative session record afterwards.
        """
        session.updated_at = utcnow()
        with self._conn() as conn:
            if session.worker_lease:
                row = conn.execute(
                    "SELECT json FROM sessions WHERE session_id = ?", (session.session_id,)
                ).fetchone()
                if row is None:
                    return False
                try:
                    stored_lease = json.loads(row[0]).get("worker_lease", "")
                except (json.JSONDecodeError, AttributeError):
                    return False
                if stored_lease != session.worker_lease:
                    return False
            conn.execute(
                "INSERT OR REPLACE INTO sessions (session_id, status, created_at, updated_at, json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    session.session_id,
                    session.status.value,
                    session.created_at,
                    session.updated_at,
                    session.model_dump_json(),
                ),
            )
        return True

    def claim_worker_lease(self, session_id: str) -> Optional[str]:
        """Atomically claim a live session for one background worker."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT status, created_at, json FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is None or row[0] in ("done", "failed", "cancelled"):
                return None
            try:
                data = json.loads(row[2])
            except json.JSONDecodeError:
                return None
            if data.get("worker_lease"):
                return None
            token = uuid.uuid4().hex
            data["worker_lease"] = token
            data["updated_at"] = utcnow()
            conn.execute(
                "UPDATE sessions SET updated_at = ?, json = ? WHERE session_id = ?",
                (data["updated_at"], json.dumps(data), session_id),
            )
        return token

    def lease_is_current(self, session_id: str, token: str) -> bool:
        """Return whether ``token`` still owns the persisted session."""
        if not token:
            return True
        with self._conn() as conn:
            row = conn.execute("SELECT json FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        if row is None:
            return False
        try:
            return json.loads(row[0]).get("worker_lease", "") == token
        except (json.JSONDecodeError, AttributeError):
            return False

    def release_worker_lease(self, session_id: str, token: str) -> bool:
        """Release a lease only when it is still ours; stale workers are no-ops."""
        if not token:
            return True
        with self._conn() as conn:
            row = conn.execute("SELECT json FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
            if row is None:
                return False
            try:
                data = json.loads(row[0])
            except json.JSONDecodeError:
                return False
            if data.get("worker_lease", "") != token:
                return False
            data["worker_lease"] = ""
            data["updated_at"] = utcnow()
            conn.execute(
                "UPDATE sessions SET updated_at = ?, json = ? WHERE session_id = ?",
                (data["updated_at"], json.dumps(data), session_id),
            )
        return True

    def revoke_worker_lease(self, session_id: str) -> bool:
        """Invalidate a worker during restart/cancellation reconciliation."""
        with self._conn() as conn:
            row = conn.execute("SELECT json FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
            if row is None:
                return False
            try:
                data = json.loads(row[0])
            except json.JSONDecodeError:
                return False
            data["worker_lease"] = ""
            data["updated_at"] = utcnow()
            conn.execute(
                "UPDATE sessions SET updated_at = ?, json = ? WHERE session_id = ?",
                (data["updated_at"], json.dumps(data), session_id),
            )
        return True

    def load_session(self, session_id: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT json FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def list_sessions(self, limit: Optional[int] = 100) -> list[dict]:
        with self._conn() as conn:
            if limit is None:
                rows = conn.execute(
                    "SELECT session_id, status, created_at, updated_at, json "
                    "FROM sessions ORDER BY created_at DESC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT session_id, status, created_at, updated_at, json "
                    "FROM sessions ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        out = []
        for r in rows:
            task_text, pending_approvals, pending_inputs = "", 0, 0
            goal_id, package_id, package_owner = None, "", ""
            goal_epoch, goal_milestone, goal_release = None, None, False
            active_agent_calls: list[dict] = []
            agent_calls, agent_call_attempts = 0, 0
            successful_agent_calls: dict[str, int] = {}
            agent_attempt_duration_ms = 0
            package_output_authors: dict[str, str] = {}
            package_output_attempts: dict[str, int] = {}
            package_output_history: dict[str, list[dict]] = {}
            package_call_failures: dict[str, str] = {}
            resource_roster: list[str] = []
            participation_mode = "focused"
            collaboration_assignments: list[dict] = []
            collaboration_integrated_files: list[str] = []
            collaboration_integration_status = "not_started"
            package_started_at = None
            package_deadline_at = None
            try:
                data = json.loads(r[4])
                task_text = (data.get("task") or {}).get("text", "")
                goal_id = data.get("goal_id")
                package_id = data.get("work_package_id") or ""
                package_owner = data.get("work_package_owner") or ""
                goal_epoch = data.get("goal_epoch")
                goal_milestone = data.get("goal_milestone")
                goal_release = bool(data.get("goal_release"))
                active_agent_calls = list(data.get("active_agent_calls") or [])
                agent_calls = int(data.get("agent_calls") or 0)
                agent_call_attempts = int(data.get("agent_call_attempts") or agent_calls)
                successful_agent_calls = dict(data.get("successful_agent_calls") or {})
                agent_attempt_duration_ms = int(
                    data.get("agent_attempt_duration_ms") or 0
                )
                package_output_authors = dict(data.get("package_output_authors") or {})
                package_output_attempts = dict(data.get("package_output_attempts") or {})
                package_output_history = dict(data.get("package_output_history") or {})
                package_call_failures = dict(data.get("package_call_failures") or {})
                resource_roster = list(data.get("resource_roster") or [])
                participation_mode = data.get("participation_mode") or "focused"
                collaboration_assignments = list(
                    data.get("collaboration_assignments") or []
                )
                collaboration_integrated_files = list(
                    data.get("collaboration_integrated_files") or []
                )
                collaboration_integration_status = (
                    data.get("collaboration_integration_status") or "not_started"
                )
                package_started_at = data.get("package_started_at")
                package_deadline_at = data.get("package_deadline_at")
                pending_approvals = sum(
                    1 for a in data.get("approvals", []) if a.get("status") == "pending"
                )
                pending_inputs = sum(
                    1 for i in data.get("input_requests", []) if i.get("status") == "pending"
                )
            except (json.JSONDecodeError, AttributeError):
                pass
            out.append(
                {
                    "session_id": r[0], "status": r[1],
                    "created_at": r[2], "updated_at": r[3],
                    "task_text": task_text[:160],
                    "pending_approvals": pending_approvals,
                    "pending_inputs": pending_inputs,
                    "goal_id": goal_id,
                    "work_package_id": package_id,
                    "work_package_owner": package_owner,
                    "goal_epoch": goal_epoch,
                    "goal_milestone": goal_milestone,
                    "goal_release": goal_release,
                    "active_agent_calls": active_agent_calls,
                    "agent_calls": agent_calls,
                    "agent_call_attempts": agent_call_attempts,
                    "successful_agent_calls": successful_agent_calls,
                    "agent_attempt_duration_ms": agent_attempt_duration_ms,
                    "package_output_authors": package_output_authors,
                    "package_output_attempts": package_output_attempts,
                    "package_output_history": package_output_history,
                    "package_call_failures": package_call_failures,
                    "resource_roster": resource_roster,
                    "participation_mode": participation_mode,
                    "collaboration_assignments": collaboration_assignments,
                    "collaboration_integrated_files": collaboration_integrated_files,
                    "collaboration_integration_status": collaboration_integration_status,
                    "package_started_at": package_started_at,
                    "package_deadline_at": package_deadline_at,
                }
            )
        return out

    def session_log_path(self, session_id: str) -> Path:
        return self.sessions_dir / f"{session_id}.jsonl"

    def delete_session(self, session_id: str) -> bool:
        """Remove a session from the store (DB row + its JSONL log). Returns
        True if a row was deleted."""
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            deleted = cur.rowcount > 0
        try:
            self.session_log_path(session_id).unlink(missing_ok=True)
        except OSError:
            pass
        return deleted

    def delete_all_sessions(self) -> int:
        """Remove every persisted session and its audit log.

        Callers must cancel live workers before invoking this method. Keep the
        feed sequence monotonic so an already-connected EventSource continues
        to receive future events after its visible history is cleared.
        """
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM sessions")
            deleted = max(int(cur.rowcount or 0), 0)
        for log_path in self.sessions_dir.glob("*.jsonl"):
            try:
                log_path.unlink(missing_ok=True)
            except OSError:
                pass
        with self._feed_cond:
            self._feed.clear()
            self._feed_cond.notify_all()
        return deleted

    def log_event(self, session_id: str, event: str, payload: Optional[dict] = None) -> None:
        record = {"ts": utcnow(), "event": event, "payload": payload or {}}
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        with self._event_lock:
            with open(self.session_log_path(session_id), "a", encoding="utf-8") as f:
                f.write(line)
        self._publish_event(session_id, record)

    # ---- live feed -------------------------------------------------------
    # Every event already flows through log_event; keeping an in-memory ring
    # of the most recent ones (with a monotonically increasing cursor) lets
    # the dashboard STREAM the run instead of reconstructing it from state
    # snapshots — the "black hole" fix from NEXT-LEVEL.md R1.

    _FEED_CAPACITY = 500

    def _publish_event(self, session_id: str, record: dict) -> None:
        with self._feed_cond:
            self._feed_seq += 1
            entry = {
                "seq": self._feed_seq,
                "session_id": session_id,
                **record,
            }
            self._feed.append(entry)
            if len(self._feed) > self._FEED_CAPACITY:
                del self._feed[: len(self._feed) - self._FEED_CAPACITY]
            self._feed_cond.notify_all()

    def feed_since(self, cursor: int, limit: int = 100) -> list[dict]:
        """Events with seq > cursor (oldest first), without blocking."""
        with self._feed_cond:
            return [dict(e) for e in self._feed if e["seq"] > cursor][:limit]

    def feed_wait(self, cursor: int, timeout_s: float = 25.0) -> list[dict]:
        """Block up to timeout_s for events with seq > cursor; [] on timeout."""
        with self._feed_cond:
            fresh = [dict(e) for e in self._feed if e["seq"] > cursor]
            if fresh:
                return fresh
            self._feed_cond.wait(timeout=timeout_s)
            return [dict(e) for e in self._feed if e["seq"] > cursor]

    @property
    def feed_cursor(self) -> int:
        with self._feed_cond:
            return self._feed_seq
