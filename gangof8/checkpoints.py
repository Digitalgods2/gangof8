"""Immutable, content-addressed artifact checkpoints.

Session sandboxes and goal staging directories are working projections.  They
may be replaced during a repair, so they cannot be the evidence that a build
passed.  This store copies verified bytes into SHA-256 addressed blobs and
records a manifest in SQLite.  A repair creates a new candidate checkpoint;
the active verified checkpoint remains materializable until a replacement has
passed the same objective gates.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Iterable, Optional

from .models import utcnow


def _stable(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


class CheckpointStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.db_path = self.data_dir / "gangof8.db"
        self.blob_root = self.data_dir / "blobs" / "sha256"
        self.blob_root.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=10)

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS artifact_checkpoints (
                       checkpoint_id TEXT PRIMARY KEY,
                       goal_id TEXT NOT NULL,
                       package_id TEXT NOT NULL,
                       session_id TEXT NOT NULL,
                       parent_id TEXT NOT NULL,
                       state TEXT NOT NULL,
                       created_at TEXT NOT NULL,
                       manifest_json TEXT NOT NULL,
                       evidence_json TEXT NOT NULL
                   )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_checkpoint_goal "
                "ON artifact_checkpoints(goal_id, package_id, created_at)"
            )

    def _blob_path(self, digest: str) -> Path:
        return self.blob_root / digest[:2] / digest[2:]

    def _store_blob(self, source: Path, digest: str) -> Path:
        target = self._blob_path(digest)
        if target.is_file():
            observed = hashlib.sha256(target.read_bytes()).hexdigest()
            if observed != digest:
                raise OSError(
                    f"checkpoint blob is corrupt: expected {digest}, "
                    f"observed {observed}"
                )
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(
            prefix=f".{digest[:12]}-", dir=str(target.parent)
        )
        os.close(handle)
        temp_path = Path(temporary)
        try:
            shutil.copyfile(source, temp_path)
            copied = hashlib.sha256(temp_path.read_bytes()).hexdigest()
            if copied != digest:
                raise OSError(
                    f"checkpoint source changed while copying {source}: "
                    f"expected {digest}, observed {copied}"
                )
            os.replace(temp_path, target)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        return target

    def seal_paths(
        self,
        *,
        goal_id: str,
        package_id: str,
        session_id: str,
        paths: dict[str, Path | str],
        expected_hashes: Optional[dict[str, str]] = None,
        parent_id: str = "",
        state: str = "verified",
        evidence: Optional[dict] = None,
    ) -> dict:
        """Copy one coherent generation into blobs and persist its manifest."""
        expected = {
            str(name).replace("\\", "/"): str(digest)
            for name, digest in (expected_hashes or {}).items()
        }
        manifest: dict[str, dict] = {}
        for raw_name, raw_path in sorted(paths.items()):
            name = str(raw_name).replace("\\", "/")
            source = Path(raw_path)
            if not source.is_file():
                raise FileNotFoundError(f"checkpoint source missing: {source}")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            if expected.get(name) and expected[name] != digest:
                raise ValueError(
                    f"checkpoint hash mismatch for {name}: "
                    f"expected {expected[name]}, observed {digest}"
                )
            blob = self._store_blob(source, digest)
            manifest[name] = {
                "sha256": digest,
                "size": blob.stat().st_size,
                "blob": str(blob),
            }
        if not manifest:
            raise ValueError("cannot seal an empty checkpoint")
        identity = {
            "goal_id": goal_id,
            "package_id": package_id,
            "session_id": session_id,
            "parent_id": parent_id,
            "state": state,
            "manifest": manifest,
        }
        checkpoint_id = "cp_" + hashlib.sha256(
            _stable(identity).encode("utf-8")
        ).hexdigest()[:24]
        created_at = utcnow()
        record = {
            "checkpoint_id": checkpoint_id,
            "goal_id": goal_id,
            "package_id": package_id,
            "session_id": session_id,
            "parent_id": parent_id,
            "state": state,
            "created_at": created_at,
            "manifest": manifest,
            "evidence": dict(evidence or {}),
        }
        with self._conn() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO artifact_checkpoints
                   (checkpoint_id, goal_id, package_id, session_id, parent_id,
                    state, created_at, manifest_json, evidence_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    checkpoint_id, goal_id, package_id, session_id, parent_id,
                    state, created_at, _stable(manifest),
                    _stable(record["evidence"]),
                ),
            )
        return record

    def get(self, checkpoint_id: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                """SELECT goal_id, package_id, session_id, parent_id, state,
                          created_at, manifest_json, evidence_json
                   FROM artifact_checkpoints WHERE checkpoint_id = ?""",
                (checkpoint_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "checkpoint_id": checkpoint_id,
            "goal_id": row[0], "package_id": row[1], "session_id": row[2],
            "parent_id": row[3], "state": row[4], "created_at": row[5],
            "manifest": json.loads(row[6]), "evidence": json.loads(row[7]),
        }

    def materialize(
        self,
        checkpoint_id: str,
        destination: Path,
        names: Optional[Iterable[str]] = None,
    ) -> dict[str, str]:
        record = self.get(checkpoint_id)
        if record is None:
            raise KeyError(f"checkpoint {checkpoint_id} not found")
        wanted = {
            str(name).replace("\\", "/") for name in names
        } if names is not None else None
        root = Path(destination)
        restored: dict[str, str] = {}
        for name, item in record["manifest"].items():
            if wanted is not None and name not in wanted:
                continue
            target = (root / Path(name)).resolve()
            try:
                target.relative_to(root.resolve())
            except ValueError as exc:
                raise ValueError(f"unsafe checkpoint path: {name}") from exc
            blob = Path(item["blob"]).resolve()
            try:
                blob.relative_to(self.blob_root.resolve())
            except ValueError as exc:
                raise ValueError(
                    f"unsafe checkpoint blob path for {name}"
                ) from exc
            digest = hashlib.sha256(blob.read_bytes()).hexdigest()
            if digest != item["sha256"]:
                raise OSError(f"checkpoint blob is corrupt: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(blob, target)
            restored[name] = digest
        return restored
