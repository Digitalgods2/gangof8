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
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._conn() as conn:
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

    def list_sessions(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT session_id, status, created_at, updated_at FROM sessions ORDER BY created_at DESC"
            ).fetchall()
        return [
            {"session_id": r[0], "status": r[1], "created_at": r[2], "updated_at": r[3]}
            for r in rows
        ]

    def session_log_path(self, session_id: str) -> Path:
        return self.sessions_dir / f"{session_id}.jsonl"

    def log_event(self, session_id: str, event: str, payload: Optional[dict] = None) -> None:
        record = {"ts": utcnow(), "event": event, "payload": payload or {}}
        with open(self.session_log_path(session_id), "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
