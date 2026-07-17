"""Workspace registry — the named project directories ("allowed work areas")
the council may operate in, instead of the throwaway per-session sandbox.

Persisted to DATA_DIR/workspaces.json: a list of workspaces plus the id of the
active one. A session captures the active workspace's root at submit time, so
file skills (write_file/read_file) resolve inside that root, governed by the
permission kernel + human approval. No secrets here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

from .models import Workspace


class WorkspaceError(Exception):
    pass


def _normalize_root(root: str) -> str:
    """Backslash is a path separator on Windows but an ordinary filename
    character everywhere else. A pasted Windows-style path (or a stray typo
    like "/Users/me\\project") would otherwise resolve to a single bogus path
    component containing a literal backslash instead of the intended nested
    folder — so on macOS/Linux, treat "\\" as "/" before resolving."""
    if sys.platform == "win32":
        return root
    return root.replace("\\", "/")


class WorkspaceStore:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "workspaces.json"

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        return {"active": None, "workspaces": []}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def list(self) -> list[Workspace]:
        return [Workspace.model_validate(w) for w in self._load().get("workspaces", [])]

    def get(self, workspace_id: str) -> Optional[Workspace]:
        return next((w for w in self.list() if w.id == workspace_id), None)

    def add(self, name: str, root: str) -> Workspace:
        """Register a workspace. The root is resolved to an absolute path and
        created if missing; pointing at an existing FILE is rejected."""
        name = (name or "").strip()
        if not name:
            raise WorkspaceError("workspace name is required")
        resolved = Path(_normalize_root(root or "")).expanduser().resolve()
        if resolved.exists() and not resolved.is_dir():
            raise WorkspaceError(f"workspace root is not a directory: {resolved}")
        resolved.mkdir(parents=True, exist_ok=True)
        data = self._load()
        # Idempotent: re-registering the same folder returns the existing entry
        # (no duplicates) so "set the workspace folder" is a stable operation.
        for w in data["workspaces"]:
            if Path(w["root"]) == resolved:
                return Workspace.model_validate(w)
        ws = Workspace(name=name, root=str(resolved))
        data["workspaces"].append(ws.model_dump())
        self._save(data)
        return ws

    def remove(self, workspace_id: str) -> None:
        data = self._load()
        data["workspaces"] = [w for w in data["workspaces"] if w["id"] != workspace_id]
        if data.get("active") == workspace_id:
            data["active"] = None
        self._save(data)

    def active(self) -> Optional[Workspace]:
        active_id = self._load().get("active")
        return self.get(active_id) if active_id else None

    def set_active(self, workspace_id: Optional[str]) -> Optional[Workspace]:
        """Activate a workspace by id, or clear the active workspace with None.
        New sessions then run against it (or the sandbox when cleared)."""
        data = self._load()
        if workspace_id is None:
            data["active"] = None
            self._save(data)
            return None
        if not any(w["id"] == workspace_id for w in data["workspaces"]):
            raise WorkspaceError(f"no workspace {workspace_id!r}")
        data["active"] = workspace_id
        self._save(data)
        return self.get(workspace_id)
