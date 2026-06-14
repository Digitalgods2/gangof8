"""Memory/Log Store — SQLite for session state + per-session JSONL trail."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

from .models import Session, utcnow


class LogStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.sessions_dir = self.data_dir / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "conclave_os.db"
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

    def save_session(self, session: Session) -> None:
        session.updated_at = utcnow()
        with self._conn() as conn:
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

    def load_session(self, session_id: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT json FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def list_sessions(self, limit: int = 100) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT session_id, status, created_at, updated_at, json "
                "FROM sessions ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for r in rows:
            task_text, pending_approvals, pending_inputs = "", 0, 0
            try:
                data = json.loads(r[4])
                task_text = (data.get("task") or {}).get("text", "")
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

    def log_event(self, session_id: str, event: str, payload: Optional[dict] = None) -> None:
        record = {"ts": utcnow(), "event": event, "payload": payload or {}}
        with open(self.session_log_path(session_id), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
