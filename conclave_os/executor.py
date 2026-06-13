"""Action executor — the ONLY code that performs side effects for agents.

Phase 4 supports exactly one action kind: write_file, confined to the
session's own artifacts folder (data/artifacts/<session_id>/). Every call
must already carry an approved ApprovalRequest — the loop enforces that;
this module enforces the sandbox.
"""

from __future__ import annotations

import re
from pathlib import Path

from .models import ProposedAction, Session

# Conservative filename whitelist: word chars, dot, dash, space. No path
# separators, no expansion tricks.
_SAFE_NAME = re.compile(r"^[\w.\- ]{1,100}$")


class ExecutionError(Exception):
    pass


def _safe_filename(raw: str) -> str:
    name = Path(raw.strip()).name  # drops any directory components
    if not name or set(name) <= {"."}:
        raise ExecutionError(f"unusable filename: {raw!r}")
    if not _SAFE_NAME.match(name):
        raise ExecutionError(f"filename contains disallowed characters: {name!r}")
    return name


def artifacts_dir(data_dir: Path, session_id: str) -> Path:
    return Path(data_dir) / "artifacts" / session_id


def execute(session: Session, action: ProposedAction, data_dir: Path) -> Path:
    if action.kind != "write_file":
        raise ExecutionError(f"unsupported action kind: {action.kind!r}")
    name = _safe_filename(action.filename)
    out_dir = artifacts_dir(data_dir, session.session_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = (out_dir / name).resolve()
    if path.parent != out_dir.resolve():
        raise ExecutionError(f"path escapes the artifacts sandbox: {name!r}")
    path.write_text(action.content, encoding="utf-8")
    return path
