"""Derived, redaction-safe diagnostics for a running Gang of 8 service."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Callable

from . import config


def _path_status(path: Path) -> dict:
    path = Path(path)
    check = path if path.exists() else path.parent
    return {
        "path": str(path),
        "exists": path.exists(),
        "is_dir": path.is_dir(),
        "writable": bool(check.exists() and os.access(check, os.W_OK)),
    }


def collect_runtime_diagnostics(
    *, data_dir: Path, backend: str, settings, active_workspace, workspace_count: int,
    panel: list[str], role_agents: dict, api_key_status: Callable[[str], dict],
    api_key_names: tuple[str, ...],
) -> dict:
    """Build the diagnostics payload without coupling it to ``GangOf8Service``."""
    return {
        "backend": backend,
        "settings_version": settings.settings_version,
        "data_dir": _path_status(data_dir),
        "sandbox_root": _path_status(config.SANDBOX_ROOT),
        "active_workspace": active_workspace.model_dump() if active_workspace else None,
        "registered_workspaces": workspace_count,
        "panel": panel,
        "role_agents": {role.value: agent for role, agent in role_agents.items()},
        "cli": {
            name: {
                "available": shutil.which(name) is not None,
                "path": shutil.which(name),
                "enabled": (settings.cli_enabled or {}).get(name, True),
                "model": (settings.cli_models or {}).get(name) or None,
                "timeout_s": (settings.cli_timeouts or {}).get(name)
                or config.agent_timeout(name),
            }
            for name in ("claude", "codex", "gemini")
        },
        "build_call_limits": {
            "author_timeout_s": config.PANEL_AUTHOR_TIMEOUT,
            "retry_timeout_s": config.PANEL_RETRY_TIMEOUT,
            "frontier_author_seats": list(config.FRONTIER_AUTHOR_SEATS),
            "frontier_author_timeout_s": config.FRONTIER_AUTHOR_TIMEOUT,
            "frontier_verify_timeout_s": config.FRONTIER_VERIFY_TIMEOUT,
            "frontier_recovery_attempts": config.FRONTIER_AUTHOR_RECOVERY_ATTEMPTS,
            "note": (
                "per-seat settings apply only to routine/non-code sessions; coding "
                "uses stage policies, and frontier timeout 0 means no coordinator "
                "deadline while remaining user-cancellable"
            ),
        },
        "api_keys": {name: api_key_status(name) for name in api_key_names},
        "web_enabled": config.WEB_ENABLED,
        "remote_access_enabled": os.environ.get(config.ALLOW_REMOTE_ENV, "").strip().lower()
        in {"1", "true", "yes"},
    }
