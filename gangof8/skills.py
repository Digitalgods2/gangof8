"""Skill Registry — the data-driven catalogue of side-effecting capabilities.

Each Skill lifts what used to be hardcoded write_file literals (category,
risk, approval requirement, allowed roles) into metadata the permission
kernel (gangof8.governance.authorize_action) reads instead of branching
on action.kind. HANDLERS maps a skill name to the function that performs the
effect, sharing the sandbox helpers in gangof8.executor.

executor.py must NOT import this module at top level — execute() imports
HANDLERS lazily to keep the dependency one-directional.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

from pydantic import BaseModel, ConfigDict, Field

from . import config
from .executor import (
    ESTABLISHED,
    SANDBOX,
    WORKSPACE,
    ExecutionError,
    artifacts_dir,
    resolve_in_workspace,
    resolve_space,
    space_root,
)
from .models import ProposedAction, Risk, Role, Session
from .paths import extract_delivery_target
from . import validation

# Directories never worth searching (vendored / generated / VCS noise).
_SEARCH_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env",
    "__pycache__", ".mypy_cache", ".pytest_cache", "dist", "build",
    ".idea", ".vscode", ".next", "target",
}


class Skill(BaseModel):
    """Metadata for one registry-driven capability. The kernel role-gates and
    gates on this (allowed_roles + requires_approval + category/risk) instead
    of branching on the action kind."""

    model_config = ConfigDict(populate_by_name=True)

    name: str
    description: str
    category: str
    risk: Risk
    requires_approval: bool
    allowed_roles: list[Role]
    inputs: list[str]
    blocked_by_default: bool = True
    # Manifest metadata is deliberately additive: callers which construct a
    # Skill using the original fields continue to work.  The conservative
    # defaults never advertise an unclassified capability as read-only or
    # idempotent.
    manifest_schema: str = Field(
        default="gangof8.capability",
        validation_alias="schema",
        serialization_alias="schema",
    )
    version: int = 1
    provider: str = "gangof8.core"
    invocation: str = "coordinator_action"
    primary_input: Optional[str] = None
    permitted_spaces: list[str] = Field(default_factory=list)
    mutates: bool = True
    idempotency: str = "unknown"

    @property
    def schema(self) -> str:
        """Public spelling without shadowing BaseModel.schema at import time."""
        return self.manifest_schema


# A handler performs one skill's effect and returns a result string (the
# written path, file contents, etc.). It may raise ExecutionError.
Handler = Callable[[Session, ProposedAction, Path], str]


def _arg(action: ProposedAction, key: str, legacy: str = "") -> str:
    """Prefer the registry-style args dict; fall back to the legacy
    filename/content fields for back-compat with the write_file path."""
    if key in action.args:
        return action.args[key]
    return getattr(action, legacy or key, "")


# --- space targeting (sandbox | workspace | established) ----------------------
# write/edit/run_tests act ONLY in the council's own spaces (sandbox|workspace);
# established is read-only and reached for real only via the gated `promote`.
_WRITE_SPACES = {SANDBOX, WORKSPACE}
_READ_SPACES = {SANDBOX, WORKSPACE, ESTABLISHED}


def _space_arg(action: ProposedAction, default: str, allowed: set[str]) -> str:
    raw = action.args.get("target") or action.args.get("space") or default
    s = str(raw).strip().lower()
    if s not in allowed:
        raise ExecutionError(
            f"invalid target {s!r} (allowed: {', '.join(sorted(allowed))})")
    return s


def _default_read_space(session: Session) -> str:
    """Where a bare read/search/list lands when no target is given: the richest
    bound space — the established folder being examined, else the workspace,
    else the ephemeral sandbox."""
    if session.delivery_mode == "final_batch" and session.workspace_root:
        return WORKSPACE
    if session.established_root:
        return ESTABLISHED
    if session.workspace_root:
        return WORKSPACE
    return SANDBOX


def _assert_outside_established(session: Session, path: Path) -> None:
    """Hard guard: refuse any free (council-space) write that would resolve INSIDE
    the established folder OR ANY SUBFOLDER of it (e.g. a workspace mistakenly set
    under it). A subfolder of the source IS the source — it can only be reached by
    an APPROVED promote, never by a free write."""
    if not session.established_root:
        return
    est = Path(session.established_root).resolve()
    p = Path(path).resolve()
    if p == est or est in p.parents:
        raise ExecutionError(
            f"refusing to write inside the established folder ({est}); a subfolder "
            "of the source is still the source — it is reachable only via an "
            "approved promote, never a free write")


def _write_file(session: Session, action: ProposedAction, data_dir: Path) -> str:
    """Write content into a council space (sandbox default, or workspace). Free —
    no approval; the established folder (and any subfolder) is never a write target."""
    raw_name = _arg(action, "filename")
    content = _arg(action, "content")
    if not content.strip():
        raise ExecutionError(
            f"refusing to write empty artifact: {raw_name!r} "
            "(the agent produced no real file body)"
        )
    target = _space_arg(action, SANDBOX, _WRITE_SPACES)
    path = resolve_space(session, data_dir, target, raw_name)
    _assert_outside_established(session, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" writes the model's bytes verbatim — no CRLF translation. Windows'
    # default would rewrite every \n to \r\n (turning emitted CRLF into \r\r\n and
    # breaking LF-critical files: shebangs, .sh scripts).
    path.write_text(content, encoding="utf-8", newline="")
    return str(path)


def _abs_read_inside_space(session: Session, data_dir: Path, raw: str) -> Optional[Path]:
    """If `raw` is an ABSOLUTE path that resolves INSIDE a bound space
    (established, workspace, or sandbox), return it resolved. Reads are
    non-destructive, so a model that cites the full path the USER wrote in the
    task should get the file rather than a 'must be relative' refusal (live:
    every seat was refused the source file it named in full, then invented the
    story from scratch). The boundary holds: anything outside every bound root
    returns None, and `..` is neutralized by resolving before the containment
    test."""
    raw = (raw or "").strip().strip("\"'`")
    if not raw:
        return None
    is_abs = ((len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha())
              or raw.startswith("\\\\") or raw.startswith("//") or Path(raw).is_absolute())
    if not is_abs:
        return None
    try:
        target = Path(raw).resolve()
    except OSError:
        return None
    for space in (ESTABLISHED, WORKSPACE, SANDBOX):
        if space == ESTABLISHED and not session.established_root:
            continue
        if space == WORKSPACE and not session.workspace_root:
            continue
        try:
            root = space_root(session, data_dir, space).resolve()
        except (ExecutionError, OSError):
            continue
        if (target == root or root in target.parents) and target.is_file():
            return target
    return None


def _sandbox_candidate_bases(session: Session, data_dir: Path) -> set[str]:
    """Deliverable basenames the panel drafted to the sandbox THIS round — files
    saved namespaced '<agent>__<base>' → '<base>'. These are the candidates being
    judged; a file by the same name elsewhere is a copy of one, not source."""
    out: set[str] = set()
    d = artifacts_dir(data_dir, session.session_id)
    if not d.is_dir():
        return out
    for f in d.iterdir():
        if f.is_file() and "__" in f.name:
            out.add(f.name.split("__", 1)[1])
    return out


def _guard_rival_read(session: Session, data_dir: Path, path: Path, raw_name: str) -> None:
    """Refuse a panel read of a file that sits INSIDE the source folder but whose
    name is a candidate deliverable being produced and judged this round — a prior
    or rival answer to the very task at hand. Left open, a seat can pull a prior
    run's 'Benny's First Car Ride.txt' out of the source folder and crib it,
    contaminating the blind best-of-N (this is exactly what happened: three seats
    read a prior version nobody authorized). The task-NAMED source (its full name,
    with extension, appears in the task) is always allowed — that is real source,
    not a rival answer, so genuine 'read the file I told you to' reads still work."""
    if not session.established_root:
        return
    try:
        est = Path(session.established_root).resolve()
        p = Path(path).resolve()
    except OSError:
        return
    if not (p == est or est in p.parents):
        return  # not inside the source folder — sandbox/workspace reads are free
    if p.name and p.name in (session.task.text or ""):
        return  # the task named this exact file as source — authorized
    if p.name not in _sandbox_candidate_bases(session, data_dir):
        return  # not a candidate deliverable — an ordinary source read, allowed
    session.unresolved.append(
        f"blocked a panel read of '{p.name}' from the source folder — it matches a "
        "candidate being produced and judged this round (a prior/rival answer)")
    raise ExecutionError(
        f"refusing to read {raw_name!r}: a file by that name is a candidate "
        "deliverable being produced and judged this round — author your own; do "
        "not copy a prior or rival answer sitting in the source folder")


def _read_file(session: Session, action: ProposedAction, data_dir: Path) -> str:
    """Read a file from any space. With an explicit target, that space only.
    With none, every bound space is tried — default (richest) first, then the
    others — because the old single-space default made council-authored
    SANDBOX drafts unreadable whenever an established folder was bound (live:
    'Skill failed: read_file centipede.html' while the file sat right there
    in the sandbox, twice across two runs). An absolute path the user named in
    the task is honored when it lands inside a bound space (reads are safe)."""
    raw_name = _arg(action, "filename")
    abs_hit = _abs_read_inside_space(session, data_dir, raw_name)
    if abs_hit is not None:
        _guard_rival_read(session, data_dir, abs_hit, raw_name)
        return abs_hit.read_text(encoding="utf-8")
    if action.args.get("target") or action.args.get("space"):
        target = _space_arg(action, _default_read_space(session), _READ_SPACES)
        path = resolve_space(session, data_dir, target, raw_name)
        if not path.is_file():
            raise ExecutionError(f"file not found: {raw_name!r}")
        _guard_rival_read(session, data_dir, path, raw_name)
        return path.read_text(encoding="utf-8")
    default = _default_read_space(session)
    order = [default] + [s for s in (SANDBOX, WORKSPACE, ESTABLISHED) if s != default]
    tried: list[str] = []
    for target in order:
        if target == WORKSPACE and not session.workspace_root:
            continue
        if target == ESTABLISHED and not session.established_root:
            continue
        try:
            path = resolve_space(session, data_dir, target, raw_name)
        except ExecutionError:
            continue  # e.g. an absolute/escaping path for this space
        tried.append(target)
        if path.is_file():
            _guard_rival_read(session, data_dir, path, raw_name)
            return path.read_text(encoding="utf-8")
    raise ExecutionError(
        f"file not found in any space ({', '.join(tried) or 'none bound'}): {raw_name!r}")


def _edit_file(session: Session, action: ProposedAction, data_dir: Path) -> str:
    """Surgically replace a unique OLD snippet with NEW in an existing file in a
    council space (sandbox default, or workspace). Fails if the file is missing
    or OLD is absent / not unique — never a blind overwrite. The established
    folder is never edited directly (changes land there only via `promote`)."""
    raw_name = _arg(action, "filename")
    # Normalize CRLF in the model-supplied snippets to \n; the on-disk file is
    # read with universal newlines (→ \n), so a CRLF-emitting backend would
    # otherwise never match an otherwise-correct OLD snippet.
    old = _arg(action, "old").replace("\r\n", "\n").replace("\r", "\n")
    new = _arg(action, "new").replace("\r\n", "\n").replace("\r", "\n")
    if not old:
        raise ExecutionError("edit_file requires non-empty OLD text")
    target = _space_arg(action, SANDBOX, _WRITE_SPACES)
    path = resolve_space(session, data_dir, target, raw_name)
    _assert_outside_established(session, path)
    if not path.is_file():
        raise ExecutionError(f"file not found to edit: {raw_name!r}")
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0:
        raise ExecutionError(f"OLD text not found in {raw_name!r}")
    if count > 1:
        raise ExecutionError(f"OLD text not unique in {raw_name!r} ({count} matches)")
    path.write_text(text.replace(old, new, 1), encoding="utf-8", newline="")
    return str(path)


def _run_tests(session: Session, action: ProposedAction, data_dir: Path) -> str:
    """Run a parsed validation command without shell interpolation.

    Static parse/compile checks are allowed automatically. Functional commands
    only reach this handler after Governance has shown the exact command in an
    approval card, and are still limited to direct test tools.
    """
    cmd = (_arg(action, "command") or "").strip()
    target = _space_arg(action, SANDBOX, _WRITE_SPACES)
    cwd = space_root(session, data_dir, target)
    _assert_outside_established(session, cwd)
    cwd.mkdir(parents=True, exist_ok=True)
    try:
        try:
            argv = validation.static_check_argv(cmd, cwd)
        except validation.ValidationCommandError:
            argv = validation.approved_test_argv(cmd)
        return validation.run(argv, cwd, config.RUN_TESTS_TIMEOUT, config.RUN_TESTS_OUTPUT_MAX_CHARS)
    except validation.ValidationCommandError as e:
        raise ExecutionError(str(e)) from e


def session_deps_dir(session: Session, data_dir: Path) -> Path:
    """Where a session's approved third-party packages live.

    Inside the session's own sandbox, never the coordinator's environment:
    approving a build's dependencies must not permanently mutate the Python
    the coordinator itself runs on, and it must be undoable. Living in the
    sandbox means the packages are retired by the same sweep as the rest of
    the session's scratch."""
    return artifacts_dir(data_dir, session.session_id) / "_deps"


def build_env(session: Session, data_dir: Path) -> Optional[dict]:
    """Environment for a BUILD: the session's own packages on PYTHONPATH.

    Returns None when the session has installed nothing, so an ordinary build
    inherits the environment unchanged."""
    deps = session_deps_dir(session, data_dir)
    if not deps.is_dir():
        return None
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{deps}{os.pathsep}{existing}" if existing else str(deps)
    return env


def _install_deps(session: Session, action: ProposedAction, data_dir: Path) -> str:
    """Install the third-party packages a build needs, after human approval.

    This is a real escalation and is treated as one. Running a script the human
    just read is not the same as fetching and executing arbitrary package code
    from the network, so INSTALL is a separate action with its own approval
    card naming exactly which packages it will fetch.

    Three things keep it bounded:

    - Only package NAMES, extras, and a version bound survive validation. A URL,
      a VCS ref, a local path, or a pip option would let the code come from
      somewhere the human never saw on the card.
    - Packages install into the SESSION's directory, not the coordinator's
      environment, so an approval cannot permanently change the interpreter the
      coordinator runs on and the sweep can undo it.
    - --no-input and a finite timeout: a prompt nobody can answer must fail, not
      hang the run.
    """
    raw = _arg(action, "packages")
    try:
        specs = validation.approved_package_specs(raw)
    except validation.ValidationCommandError as e:
        raise ExecutionError(str(e)) from e
    target = session_deps_dir(session, data_dir)
    target.mkdir(parents=True, exist_ok=True)
    argv = [
        sys.executable, "-m", "pip", "install",
        "--target", str(target),
        "--no-input", "--disable-pip-version-check", "--no-color",
        *specs,
    ]
    try:
        report = validation.run(
            argv, target, config.INSTALL_TIMEOUT, config.RUN_TESTS_OUTPUT_MAX_CHARS)
    except validation.ValidationCommandError as e:
        raise ExecutionError(f"install failed: {e}") from e
    # validation.run reports the exit status on its own line; a non-zero pip is
    # a failed install, not an install that merely printed warnings.
    if "\n[exit " in report:
        raise ExecutionError(f"install failed for {', '.join(specs)}\n{report}")
    installed = sorted(p.name for p in target.glob("*.dist-info"))
    action.args["installed_into"] = str(target)
    return f"installed into the session: {', '.join(specs)}\n{report}\ndist-info: {', '.join(installed)}"


def _build_artifact(session: Session, action: ProposedAction, data_dir: Path) -> str:
    """Run an approved build in a council space and capture the files it PRODUCES.

    The council can only type text, so ARTIFACT can never deliver a PDF, an
    archive, or any other binary. This is the governed route to one: the human
    approves an exact command, the coordinator runs it bounded and without a
    shell, and every file the seat declared must actually appear. The declared
    outputs then behave like any other authored artifact — hashed, recorded in
    files_changed, and promotable through the same approval gate.

    Declaring the outputs up front is what makes this verifiable rather than a
    licence to run code: a build that exits 0 but produces nothing, or produces
    something other than what it promised, fails here instead of being reported
    as a success."""
    cmd = (_arg(action, "command") or "").strip()
    produces = [f for f in (p.strip() for p in (_arg(action, "produces") or "").split(","))
                if f]
    if not produces:
        raise ExecutionError("BUILD must declare the files it PRODUCES")
    target = _space_arg(action, SANDBOX, _WRITE_SPACES)
    cwd = space_root(session, data_dir, target)
    _assert_outside_established(session, cwd)
    cwd.mkdir(parents=True, exist_ok=True)
    # Resolve every declared output inside the space FIRST: a build must not be
    # able to name its way out of the council's own area.
    outputs = {name: resolve_in_workspace(cwd, name) for name in produces}
    for path in outputs.values():
        _assert_outside_established(session, path)
    try:
        argv = validation.approved_build_argv(cmd)
        report = validation.run(argv, cwd, config.BUILD_TIMEOUT, config.BUILD_OUTPUT_MAX_CHARS,
                                env=build_env(session, data_dir))
    except validation.ValidationCommandError as e:
        raise ExecutionError(str(e)) from e

    missing = [n for n, path in outputs.items() if not path.is_file()]
    if missing:
        raise ExecutionError(
            f"build did not produce {', '.join(missing)}\n{report}")
    empty = [n for n, path in outputs.items() if path.stat().st_size == 0]
    if empty:
        raise ExecutionError(f"build produced empty file(s): {', '.join(empty)}\n{report}")

    digests = {}
    for name, path in outputs.items():
        digests[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    # The loop reads these back to register the outputs as real deliverables.
    action.args["produced_paths"] = json.dumps([str(p) for p in outputs.values()])
    action.args["produced_hashes"] = json.dumps(digests)
    listing = "\n".join(
        f"  {name}  {outputs[name].stat().st_size} bytes  sha256={digests[name][:16]}…"
        for name in produces)
    return f"{report}\nPRODUCED:\n{listing}"


def is_automatic_static_test(session: Session, action: ProposedAction, data_dir: Path) -> bool:
    """Whether a RUNTESTS action is a parse/compile-only safe check."""
    try:
        target = _space_arg(action, SANDBOX, _WRITE_SPACES)
        return validation.is_static_check(
            _arg(action, "command"), space_root(session, data_dir, target))
    except (ExecutionError, OSError):
        return False


def _stage(session: Session, action: ProposedAction, data_dir: Path) -> str:
    """Move a file UP from the ephemeral sandbox into the permanent workspace —
    the council keeping work worth carrying across sessions. Free (no approval);
    both are the council's own spaces."""
    raw_name = _arg(action, "filename")
    src = resolve_space(session, data_dir, SANDBOX, raw_name)
    if not src.is_file():
        raise ExecutionError(f"nothing to stage (not in sandbox): {raw_name!r}")
    dst = resolve_space(session, data_dir, WORKSPACE, raw_name)
    _assert_outside_established(session, dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(src.read_bytes())
    return str(dst)


def _promote_source(session: Session, data_dir: Path, raw_name: str) -> Optional[Path]:
    """The council file `promote` would deliver: prefer the permanent workspace,
    fall back to the sandbox (so an ARTIFACT written to scratch can promote
    without an explicit stage)."""
    normalized = (raw_name or "").strip().replace("\\", "/")
    if normalized in {
        name.replace("\\", "/") for name in session.revision_targets
    }:
        # A surgical revision is seeded and edited in the session sandbox. The
        # bound workspace may be immutable goal staging from the prior release;
        # preferring it would silently re-deliver the old bytes.
        revised = resolve_space(session, data_dir, SANDBOX, raw_name)
        if revised.is_file():
            return revised
    if session.workspace_root:
        try:
            ws_root = Path(session.workspace_root).resolve()
            est_root = Path(session.established_root).resolve() if session.established_root else None
        except OSError:
            ws_root = None
            est_root = None
        # If the active workspace is the established folder (or inside it), it is
        # not council-owned source material. Promotion must come from sandbox.
        workspace_is_established = (
            ws_root is not None
            and est_root is not None
            and (ws_root == est_root or est_root in ws_root.parents)
        )
        if not workspace_is_established:
            ws = resolve_space(session, data_dir, WORKSPACE, raw_name)
            if ws.is_file():
                return ws
    sb = resolve_space(session, data_dir, SANDBOX, raw_name)
    return sb if sb.is_file() else None


def _promote_dest(session: Session, data_dir: Path, raw_name: str) -> Path:
    """Where `promote` LANDS. An explicit delivery target the task named ("save
    it in <X>") WINS over the established source folder, so a "read from A, save
    to B" task delivers to B and never overwrites the source A. Falls back to the
    established folder (the historical in-place promote target)."""
    # Re-evaluate explicit task wording at delivery time. This repairs a session
    # persisted before a target-parser fix and protects the read source when a
    # later, unambiguous "save to <path>" instruction was present.
    delivery_root = extract_delivery_target(session.task.text) or session.delivery_root
    if delivery_root:
        root = Path(delivery_root)
        root.mkdir(parents=True, exist_ok=True)
        return resolve_in_workspace(root, raw_name)
    return resolve_space(session, data_dir, ESTABLISHED, raw_name)


def promote_shrink(session: Session, data_dir: Path, raw_name: str) -> Optional[tuple[int, int, float]]:
    """(old_bytes, new_bytes, fraction_removed) when a promote SHRINKS an
    existing file materially, else None.

    A unified diff tells the whole truth but does not make this particular truth
    legible: replacing a 49,283-byte file with 514 bytes renders as hundreds of
    red lines that read like any other large edit. One live promote did exactly
    that — a parser bug had truncated the council's copy, the diff was approved,
    and the good delivered file was destroyed. The gate needs to SAY the number.
    """
    try:
        src = _promote_source(session, data_dir, raw_name)
        dst = _promote_dest(session, data_dir, raw_name)
        if src is None or not dst.is_file():
            return None
        old_size = dst.stat().st_size
        new_size = src.stat().st_size
    except (OSError, ExecutionError):
        return None
    if old_size < config.PROMOTE_SHRINK_MIN_BYTES or new_size >= old_size:
        return None
    removed = (old_size - new_size) / old_size
    if removed < config.PROMOTE_SHRINK_FRACTION:
        return None
    return old_size, new_size, removed


def promote_diff(session: Session, data_dir: Path, raw_name: str) -> str:
    """Unified diff of what `promote` would change at the delivery target: the
    existing file there (if any) → the council version. Shown in the approval so
    the human sees exactly what lands in their real folder."""
    import difflib

    src = _promote_source(session, data_dir, raw_name)
    if src is not None and src.stat().st_size == 0:
        return f"REFUSING PROMOTE: council/{raw_name} is empty (0 bytes)"
    new = src.read_text(encoding="utf-8", errors="replace") if src else ""
    dst = _promote_dest(session, data_dir, raw_name)
    old = dst.read_text(encoding="utf-8", errors="replace") if dst.is_file() else ""
    label = "new file" if not old else "modified"
    diff = "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"established/{raw_name} ({label})", tofile=f"council/{raw_name}",
    ))
    return (diff or "(no textual difference)")[: config.PROMOTE_DIFF_MAX_CHARS]


def _promote(session: Session, action: ProposedAction, data_dir: Path) -> str:
    """Copy a council file (workspace, else sandbox) INTO the external established
    folder. The ONLY skill that writes real, user-owned code — approval-gated."""
    raw_name = _arg(action, "filename")
    src = _promote_source(session, data_dir, raw_name)
    if src is None:
        raise ExecutionError(f"nothing to promote (not in workspace/sandbox): {raw_name!r}")
    if src.stat().st_size == 0:
        raise ExecutionError(f"refusing to promote empty artifact: {raw_name!r}")
    dst = _promote_dest(session, data_dir, raw_name)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(src.read_bytes())
    return str(dst)


def _batch_manifest(
    action: ProposedAction,
) -> tuple[list[str], dict[str, Optional[str]], dict[str, str]]:
    try:
        files = json.loads(action.args.get("files", "[]"))
        baselines = json.loads(action.args.get("baselines", "{}"))
        source_hashes = json.loads(action.args.get("source_hashes", "{}"))
    except (json.JSONDecodeError, TypeError) as e:
        raise ExecutionError(f"invalid final-batch manifest: {e}") from e
    if (not isinstance(files, list) or not isinstance(baselines, dict)
            or not isinstance(source_hashes, dict)):
        raise ExecutionError("invalid final-batch manifest shape")
    normalized: list[str] = []
    for raw in files:
        if not isinstance(raw, str):
            raise ExecutionError("final-batch filenames must be strings")
        # resolve_in_workspace performs the actual containment validation.
        name = raw.replace("\\", "/")
        if name not in normalized:
            normalized.append(name)
    missing_hashes = [name for name in normalized if not source_hashes.get(name)]
    if missing_hashes:
        raise ExecutionError(
            "final-batch manifest is not bound to verified staged bytes: "
            + ", ".join(missing_hashes)
        )
    return normalized, baselines, source_hashes


def batch_promote_diff(session: Session, data_dir: Path, action: ProposedAction) -> str:
    """One aggregate review document for every file in the final release."""
    import difflib

    files, _, source_hashes = _batch_manifest(action)
    if not session.workspace_root:
        return "(goal staging workspace is unavailable)"
    dest_root = session.delivery_root or session.established_root
    if not dest_root:
        return "(final delivery folder has not been selected)"
    stage = Path(session.workspace_root)
    dest = Path(dest_root)
    blocks: list[str] = []
    for name in files:
        src = resolve_in_workspace(stage, name)
        dst = resolve_in_workspace(dest, name)
        if not src.is_file():
            blocks.append(f"===== {name} =====\nMISSING FROM STAGING\n")
            continue
        try:
            new = src.read_text(encoding="utf-8", errors="replace")
            old = dst.read_text(encoding="utf-8", errors="replace") if dst.is_file() else ""
            label = "modified" if dst.is_file() else "new file"
            diff = "".join(difflib.unified_diff(
                old.splitlines(keepends=True), new.splitlines(keepends=True),
                fromfile=f"project/{name} ({label})", tofile=f"staging/{name}",
            )) or "(no textual difference)"
        except OSError as e:
            diff = f"(could not preview: {e})"
        blocks.append(
            f"===== {name} (verified SHA-256 {source_hashes[name]}) =====\n{diff}"
        )
    header = f"FINAL BATCH: {len(files)} file(s)\n\n"
    return (header + "\n\n".join(blocks))[: config.BATCH_PROMOTE_DIFF_MAX_CHARS]


def _promote_batch(session: Session, action: ProposedAction, data_dir: Path) -> str:
    """Validate and release all staged files as one rollback-protected unit."""
    files, baselines, source_hashes = _batch_manifest(action)
    if not files:
        raise ExecutionError("final batch is empty")
    if not session.workspace_root:
        raise ExecutionError("goal staging workspace is unavailable")
    dest_root = session.delivery_root or session.established_root
    if not dest_root:
        raise ExecutionError("final delivery folder has not been selected")
    stage = Path(session.workspace_root).resolve()
    dest = Path(dest_root).resolve()

    sources: dict[str, Path] = {}
    targets: dict[str, Path] = {}
    for name in files:
        src = resolve_in_workspace(stage, name)
        dst = resolve_in_workspace(dest, name)
        if not src.is_file() or src.stat().st_size == 0:
            raise ExecutionError(f"staged release file is missing/empty: {name}")
        actual_source = hashlib.sha256(src.read_bytes()).hexdigest()
        if actual_source != source_hashes[name]:
            raise ExecutionError(
                f"staged release changed after verification/approval: {name}"
            )
        expected = baselines.get(name)
        if expected is None:
            if dst.exists():
                raise ExecutionError(
                    f"project changed after final review: new target now exists: {name}")
        else:
            if not dst.is_file():
                raise ExecutionError(
                    f"project changed after final review: target disappeared: {name}")
            actual = hashlib.sha256(dst.read_bytes()).hexdigest()
            if actual != expected:
                raise ExecutionError(
                    f"project changed after final review: target contents changed: {name}")
        sources[name], targets[name] = src, dst

    tx = Path(data_dir) / "release-transactions" / f"{session.session_id}_{action.action_id}"
    backups = tx / "backups"
    tx.mkdir(parents=True, exist_ok=True)
    replaced: list[str] = []
    existed: set[str] = set()
    temps: list[Path] = []
    try:
        # Prepare every byte before touching the destination.
        for name in files:
            dst = targets[name]
            prepared = resolve_in_workspace(tx / "prepared", name)
            prepared.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sources[name], prepared)
            prepared_hash = hashlib.sha256(prepared.read_bytes()).hexdigest()
            if prepared_hash != source_hashes[name]:
                raise ExecutionError(
                    f"staged release changed while preparing transaction: {name}"
                )
            if dst.is_file():
                backup = resolve_in_workspace(backups, name)
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dst, backup)
                existed.add(name)
        for name in files:
            dst = targets[name]
            dst.parent.mkdir(parents=True, exist_ok=True)
            prepared = resolve_in_workspace(tx / "prepared", name)
            temp = dst.with_name(f".{dst.name}.{action.action_id}.tmp")
            shutil.copy2(prepared, temp)
            temps.append(temp)
            os.replace(temp, dst)
            replaced.append(name)
            released_hash = hashlib.sha256(dst.read_bytes()).hexdigest()
            if released_hash != source_hashes[name]:
                raise ExecutionError(
                    f"released file hash mismatch after copy: {name}"
                )
    except Exception as e:  # noqa: BLE001 - rollback must contain any filesystem failure
        rollback_errors: list[str] = []
        for name in reversed(replaced):
            dst = targets[name]
            try:
                if name in existed:
                    os.replace(resolve_in_workspace(backups, name), dst)
                elif dst.exists():
                    dst.unlink()
            except OSError as rollback_error:
                rollback_errors.append(f"{name}: {rollback_error}")
        suffix = ("; rollback problems: " + "; ".join(rollback_errors)) if rollback_errors else ""
        raise ExecutionError(f"final batch failed and was rolled back: {e}{suffix}") from e
    finally:
        for temp in temps:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
        shutil.rmtree(tx, ignore_errors=True)
    return str(dest)


_GIT_READ_SPACES = {WORKSPACE, ESTABLISHED}
_GIT_TIMEOUT_SECONDS = 4
_GIT_COMMAND_OUTPUT_MAX_BYTES = 256 * 1024
_GIT_RESULT_MAX_CHARS = 64_000
_GIT_DISPLAY_PATHS = 30
_GIT_DISPLAY_RECORDS = 40
_GIT_PATH_MAX_CHARS = 240

# Every switch is coordinator-owned.  In particular, neither action.args nor
# repository config can add a command, ref, pager, external diff, or fsmonitor.
_GIT_SAFE_OPTIONS = (
    "--no-pager",
    "-c", "core.pager=cat",
    "-c", "pager.status=false",
    "-c", "pager.diff=false",
    "-c", "diff.external=",
    "-c", "interactive.diffFilter=",
    "-c", "core.fsmonitor=false",
    "-c", "core.untrackedCache=false",
)


def _inside_root(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _git_environment() -> dict[str, str]:
    """A non-interactive, read-only-oriented Git environment.

    All inherited GIT_* variables are discarded so a caller cannot redirect
    the index, object database, work tree, config, pager, or executable through
    the coordinator process environment.  The fixed commands below do not
    invoke diff, so textconv is never eligible to run; the config overrides
    additionally disable external diff and interactive diff filters.
    """
    env = {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    env.update({
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_PAGER": "cat",
        "GIT_EXTERNAL_DIFF": "",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_PROTOCOL_FROM_USER": "0",
        "PAGER": "cat",
    })
    return env


def _run_fixed_git(
    git: str,
    cwd: Path,
    safe_directory: Path,
    fixed_args: tuple[str, ...],
) -> tuple[str, bool]:
    """Run one coordinator-authored Git query with an actual output ceiling."""
    argv = [
        git,
        *_GIT_SAFE_OPTIONS,
        "-c", f"safe.directory={safe_directory}",
        "-C", str(cwd),
        *fixed_args,
    ]
    try:
        proc = subprocess.Popen(
            argv,
            shell=False,
            cwd=str(cwd),
            env=_git_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as e:
        raise ExecutionError(f"could not start git: {e}") from e

    buffers = [bytearray(), bytearray()]
    overflow = [False, False]

    def drain(stream, index: int) -> None:
        if stream is None:
            return
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    break
                remaining = _GIT_COMMAND_OUTPUT_MAX_BYTES - len(buffers[index])
                if remaining > 0:
                    buffers[index].extend(chunk[:remaining])
                if len(chunk) > max(remaining, 0):
                    overflow[index] = True
        finally:
            stream.close()

    readers = [
        threading.Thread(target=drain, args=(proc.stdout, 0), daemon=True),
        threading.Thread(target=drain, args=(proc.stderr, 1), daemon=True),
    ]
    for reader in readers:
        reader.start()
    try:
        returncode = proc.wait(timeout=_GIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as e:
        proc.kill()
        proc.wait()
        for reader in readers:
            reader.join()
        raise ExecutionError(
            f"git snapshot timed out after {_GIT_TIMEOUT_SECONDS}s"
        ) from e
    for reader in readers:
        reader.join()

    stdout = bytes(buffers[0]).decode("utf-8", errors="replace")
    stderr = bytes(buffers[1]).decode("utf-8", errors="replace")
    if returncode != 0:
        detail = (stderr or stdout).strip()
        if len(detail) > 800:
            detail = detail[:797] + "..."
        suffix = f": {detail}" if detail else ""
        raise ExecutionError(f"git snapshot query failed (exit {returncode}){suffix}")
    return stdout, overflow[0]


def _git_target(
    session: Session,
    action: ProposedAction,
    data_dir: Path,
) -> tuple[str, Path, Path, str]:
    """Resolve the requested directory and bind it to workspace|established."""
    workspace_has_repo = False
    if session.workspace_root:
        try:
            marker = Path(session.workspace_root).resolve(strict=True) / ".git"
            workspace_has_repo = marker.is_dir() and not marker.is_symlink()
        except (OSError, RuntimeError, ValueError):
            workspace_has_repo = False
    if workspace_has_repo:
        default = WORKSPACE
    elif session.established_root:
        default = ESTABLISHED
    elif session.workspace_root:
        default = WORKSPACE
    else:
        default = WORKSPACE
    space = _space_arg(action, default, _GIT_READ_SPACES)
    try:
        root = space_root(session, data_dir, space).resolve(strict=True)
    except FileNotFoundError as e:
        raise ExecutionError(f"{space} root does not exist") from e
    except (OSError, RuntimeError) as e:
        raise ExecutionError(f"could not resolve {space} root: {e}") from e
    if not root.is_dir():
        raise ExecutionError(f"{space} root is not a directory")

    raw = (_arg(action, "path") or "").strip().replace("\\", "/")
    if raw in {"", ".", "./"}:
        target = root
        relative = "."
    else:
        try:
            target = resolve_in_workspace(root, raw)
        except (OSError, RuntimeError) as e:
            raise ExecutionError(f"could not resolve git snapshot path: {e}") from e
        relative = target.relative_to(root).as_posix()
    if not target.is_dir():
        raise ExecutionError(f"git snapshot path is not a directory: {raw or '.'!r}")
    # resolve_in_workspace already performs this check for a non-root path.  Do
    # it again explicitly so a symlink swapped between checks fails closed.
    try:
        target = target.resolve(strict=True)
    except (OSError, RuntimeError) as e:
        raise ExecutionError(f"could not resolve git snapshot path: {e}") from e
    if not _inside_root(target, root):
        raise ExecutionError("git snapshot path escapes its bound space")
    return space, root, target, relative


def _short_git_path(path: str) -> str:
    if len(path) <= _GIT_PATH_MAX_CHARS:
        return path
    return path[: _GIT_PATH_MAX_CHARS - 3] + "..."


def _standard_git_top(target: Path, root: Path) -> Path:
    """Find a normal in-space .git directory without following indirection."""
    candidate = target
    while True:
        marker = candidate / ".git"
        if marker.exists() or marker.is_symlink():
            if marker.is_symlink() or marker.is_file() or not marker.is_dir():
                raise ExecutionError(
                    "refusing git snapshot: linked or redirected .git metadata"
                )
            return candidate
        if candidate == root:
            break
        parent = candidate.parent
        if not _inside_root(parent, root):
            break
        candidate = parent
    raise ExecutionError(
        "refusing git snapshot: no repository metadata exists inside the bound space"
    )


def _append_unique(items: list[str], path: str) -> None:
    if path not in items:
        items.append(path)


def _parse_porcelain_v2(raw: str, command_truncated: bool) -> dict:
    """Turn NUL-delimited porcelain-v2 into JSON-safe summary data."""
    records = raw.split("\0")
    # A capped command may end halfway through a pathname or record.  Never
    # present that fragment as a real status entry.
    if command_truncated and raw and not raw.endswith("\0"):
        records = records[:-1]

    branch: dict[str, str] = {}
    staged: list[str] = []
    unstaged: list[str] = []
    untracked: list[str] = []
    porcelain: list[dict[str, str]] = []
    status_records = 0
    malformed = False
    i = 0
    while i < len(records):
        record = records[i]
        i += 1
        if not record:
            continue
        if record.startswith("# "):
            key_value = record[2:].split(" ", 1)
            branch[key_value[0]] = key_value[1] if len(key_value) == 2 else ""
            continue

        kind = record[:1]
        xy = ""
        path = ""
        original = ""
        if kind == "1":
            fields = record.split(" ", 8)
            if len(fields) != 9:
                malformed = True
                continue
            xy, path = fields[1], fields[8]
        elif kind == "2":
            fields = record.split(" ", 9)
            if len(fields) != 10 or i >= len(records):
                malformed = True
                continue
            xy, path = fields[1], fields[9]
            original = records[i]
            i += 1
        elif kind == "u":
            fields = record.split(" ", 10)
            if len(fields) != 11:
                malformed = True
                continue
            xy, path = fields[1], fields[10]
        elif kind == "?":
            path = record[2:] if record.startswith("? ") else ""
            if not path:
                malformed = True
                continue
            _append_unique(untracked, path)
        elif kind == "!":
            continue
        else:
            malformed = True
            continue

        if kind in {"1", "2", "u"}:
            if kind == "u" or (xy and xy[0] != "."):
                _append_unique(staged, path)
            if kind == "u" or (len(xy) > 1 and xy[1] != "."):
                _append_unique(unstaged, path)

        status_records += 1
        if len(porcelain) < _GIT_DISPLAY_RECORDS:
            item = {"kind": kind, "path": _short_git_path(path)}
            if xy:
                item["xy"] = xy
            if original:
                item["original_path"] = _short_git_path(original)
            porcelain.append(item)

    ahead: Optional[int] = None
    behind: Optional[int] = None
    for item in branch.get("branch.ab", "").split():
        try:
            if item.startswith("+"):
                ahead = int(item[1:])
            elif item.startswith("-"):
                behind = int(item[1:])
        except ValueError:
            malformed = True

    def group(paths: list[str]) -> dict:
        return {
            "count": len(paths),
            "paths": [_short_git_path(path) for path in paths[:_GIT_DISPLAY_PATHS]],
            "paths_truncated": len(paths) > _GIT_DISPLAY_PATHS or command_truncated,
        }

    oid = branch.get("branch.oid")
    if oid == "(initial)":
        oid = None
    head = branch.get("branch.head")
    if head == "(detached)":
        head = "detached"
    return {
        "branch": head,
        "head": oid,
        "upstream": branch.get("branch.upstream"),
        "ahead": ahead,
        "behind": behind,
        "status": {
            "format": "porcelain-v2",
            "counts_exact": not command_truncated and not malformed,
            "command_output_truncated": command_truncated,
            "staged": group(staged),
            "unstaged": group(unstaged),
            "untracked": group(untracked),
            "records": porcelain,
            "records_truncated": (
                status_records > len(porcelain)
                or command_truncated
            ),
        },
    }


def _git_snapshot(session: Session, action: ProposedAction, data_dir: Path) -> str:
    """Return a bounded, read-only snapshot of a repository in a bound space.

    The only accepted action inputs select workspace|established and an
    optional contained directory.  Git subcommands, flags, and refs are fixed
    here; there is no network operation and no model-supplied argv.
    """
    git = shutil.which("git")
    if not git:
        raise ExecutionError("git is not available on PATH")
    space, root, target, relative = _git_target(session, action, data_dir)
    expected_top = _standard_git_top(target, root)

    metadata, metadata_truncated = _run_fixed_git(
        git,
        target,
        expected_top,
        (
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
            "--absolute-git-dir",
            "--git-common-dir",
        ),
    )
    if metadata_truncated:
        raise ExecutionError("git repository metadata exceeded the safe output limit")
    lines = [line.strip() for line in metadata.splitlines() if line.strip()]
    if len(lines) != 3:
        raise ExecutionError("git returned an unexpected repository metadata shape")
    try:
        top = Path(lines[0]).resolve(strict=True)
        git_dir_raw = Path(lines[1])
        git_dir = git_dir_raw.resolve(strict=True)
        common_dir = Path(lines[2]).resolve(strict=True)
    except (OSError, RuntimeError) as e:
        raise ExecutionError(f"could not validate git repository metadata: {e}") from e

    if not _inside_root(top, root):
        raise ExecutionError(
            "refusing git snapshot: repository top-level is outside the bound space"
        )
    if top != expected_top:
        raise ExecutionError(
            "refusing git snapshot: discovered repository does not match contained metadata"
        )
    if not _inside_root(git_dir, root) or not _inside_root(common_dir, root):
        raise ExecutionError(
            "refusing git snapshot: git metadata is outside the bound space"
        )
    expected_git_dir = top / ".git"
    # A .git file denotes a linked worktree/submodule; a symlink can redirect
    # after containment validation.  This first version intentionally refuses
    # both instead of following indirection into a shared repository.
    if (expected_git_dir.is_symlink() or expected_git_dir.is_file()
            or not expected_git_dir.is_dir()):
        raise ExecutionError(
            "refusing git snapshot: linked, redirected, or missing .git metadata"
        )
    expected_resolved = expected_git_dir.resolve(strict=True)
    if git_dir != expected_resolved or common_dir != git_dir:
        raise ExecutionError(
            "refusing git snapshot: linked or shared git metadata is not supported"
        )
    alternates = git_dir / "objects" / "info" / "alternates"
    if alternates.exists():
        raise ExecutionError(
            "refusing git snapshot: an alternate object database is configured"
        )
    for metadata_name in (
        "HEAD", "config", "index", "objects", "refs", "logs", "packed-refs",
    ):
        if (git_dir / metadata_name).is_symlink():
            raise ExecutionError(
                "refusing git snapshot: git metadata contains redirected paths"
            )

    status_raw, status_truncated = _run_fixed_git(
        git,
        top,
        top,
        (
            "status",
            "--porcelain=v2",
            "--branch",
            "-z",
            "--untracked-files=normal",
            "--ignore-submodules=all",
        ),
    )
    parsed = _parse_porcelain_v2(status_raw, status_truncated)
    repository = "." if top == root else top.relative_to(root).as_posix()
    payload = {
        "schema": "gangof8.git-snapshot",
        "version": 1,
        "read_only": True,
        "space": space,
        "path": relative,
        "repository": repository,
        **parsed,
    }
    result = json.dumps(payload, ensure_ascii=False, indent=2)
    if len(result) > _GIT_RESULT_MAX_CHARS:
        # Keep valid JSON if exceptionally long path names fill the display
        # budget. Counts remain available even when examples are omitted.
        for key in ("staged", "unstaged", "untracked"):
            payload["status"][key]["paths"] = []
            payload["status"][key]["paths_truncated"] = True
        payload["status"]["records"] = []
        payload["status"]["records_truncated"] = True
        result = json.dumps(payload, ensure_ascii=False, indent=2)
    return result


def _web_search(session: Session, action: ProposedAction, data_dir: Path) -> str:
    """Answer a query with live web grounding (the coordinator does the search)."""
    from . import web

    if not config.WEB_ENABLED:
        raise ExecutionError("web access is disabled (GANGOF8_WEB=0)")
    query = _arg(action, "query").strip()
    if not query:
        raise ExecutionError("web_search requires a non-empty query")
    try:
        return web.web_search(query, data_dir=data_dir)
    except web.WebError as e:
        raise ExecutionError(str(e)) from e


def _web_fetch(session: Session, action: ProposedAction, data_dir: Path) -> str:
    """Fetch a public http(s) URL and return its readable text."""
    from . import web

    if not config.WEB_ENABLED:
        raise ExecutionError("web access is disabled (GANGOF8_WEB=0)")
    url = _arg(action, "url").strip()
    if not url:
        raise ExecutionError("web_fetch requires a URL")
    try:
        return web.web_fetch(url)
    except web.WebError as e:
        raise ExecutionError(str(e)) from e


def _search_root(session: Session, data_dir: Path, action: ProposedAction | None = None) -> Path:
    target = _space_arg(action, _default_read_space(session), _READ_SPACES) \
        if action is not None else _default_read_space(session)
    return space_root(session, data_dir, target)


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _list_dir(session: Session, action: ProposedAction, data_dir: Path) -> str:
    """List the workspace (or session sandbox) directory tree so agents can
    DISCOVER what files exist before reading or writing — the missing first step
    for open-ended 'examine this app' tasks. Read-only, no approval; bounded by
    entry count and depth. The positional arg is a subdirectory ('.' or empty =
    the workspace root)."""
    space = _space_arg(action, _default_read_space(session), _READ_SPACES)
    base = space_root(session, data_dir, space).resolve()
    if not base.is_dir():
        return "No directory to list."
    raw = (_arg(action, "path") or "").strip().strip("/").replace("\\", "/")
    if raw in ("", "."):
        target = base
    else:
        target = resolve_space(session, data_dir, space, raw)
    if not target.is_dir():
        raise ExecutionError(f"not a directory: {raw or '.'!r}")

    # Breadth-first (shallow entries first) so the top-level layout — the most
    # useful view for "what is this app" — survives the entry cap instead of
    # being crowded out by deeply-nested files.
    entries: list[str] = []
    truncated = False
    everything = sorted(
        target.rglob("*"),
        key=lambda p: (len(p.relative_to(target).parts),
                       p.relative_to(target).as_posix().lower()),
    )
    for p in everything:
        rel_parts = p.relative_to(target).parts
        if any(part in _SEARCH_SKIP_DIRS for part in rel_parts):
            continue
        if len(rel_parts) > config.LIST_DIR_MAX_DEPTH:
            continue
        if len(entries) >= config.LIST_DIR_MAX_ENTRIES:
            truncated = True
            break
        rel = p.relative_to(target).as_posix()
        if p.is_dir():
            entries.append(f"  {rel}/")
        else:
            try:
                size = _fmt_size(p.stat().st_size)
            except OSError:
                size = "?"
            entries.append(f"  {rel}  ({size})")

    label = target.name or "project"
    if not entries:
        return f"{label}/ is empty."
    head = (f"{label}/ — {len(entries)} entr{'y' if len(entries) == 1 else 'ies'}"
            + (" (truncated)" if truncated else "") + ":\n")
    return (head + "\n".join(entries))[: config.LIST_DIR_RESULT_MAX_CHARS]


def _search_project(session: Session, action: ProposedAction, data_dir: Path) -> str:
    """Search the workspace (or session sandbox) for a string in file names and
    contents — a bounded, read-only grep so agents can see existing code before
    writing. Returns matching file names and `path:line: text` content hits."""
    query = _arg(action, "query").strip()
    if not query:
        raise ExecutionError("search_project requires a non-empty query")
    root = _search_root(session, data_dir, action).resolve()
    if not root.is_dir():
        return f"No project directory to search for {query!r}."

    q = query.lower()
    name_hits: list[str] = []
    matches: list[str] = []
    scanned = 0
    for p in sorted(root.rglob("*")):
        if any(part in _SEARCH_SKIP_DIRS for part in p.relative_to(root).parts):
            continue
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if q in rel.lower():
            name_hits.append(rel)
        if scanned >= config.SEARCH_MAX_FILES or len(matches) >= config.SEARCH_MAX_MATCHES:
            continue
        try:
            if p.stat().st_size > config.SEARCH_MAX_FILE_BYTES:
                continue
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # unreadable / binary
        scanned += 1
        for lineno, line in enumerate(text.splitlines(), 1):
            if q in line.lower():
                matches.append(f"  {rel}:{lineno}: {line.strip()[:200]}")
                if len(matches) >= config.SEARCH_MAX_MATCHES:
                    break

    if not name_hits and not matches:
        return f"No matches for {query!r} in {root.name or 'the project'}."
    parts = []
    if name_hits:
        shown = name_hits[:30]
        parts.append("Files with matching names:\n" + "\n".join(f"  {n}" for n in shown)
                     + ("\n  …" if len(name_hits) > len(shown) else ""))
    if matches:
        parts.append(f"Content matches ({len(matches)}):\n" + "\n".join(matches))
    return "\n\n".join(parts)[: config.SEARCH_RESULT_MAX_CHARS]


SKILLS: dict[str, Skill] = {
    "write_file": Skill(
        name="write_file",
        description="Write a file into a council space (sandbox default, or workspace). Free, no approval.",
        category="file_write",
        risk=Risk.low,
        requires_approval=False,
        # Council-space writes are free for EVERY seat (owner directive: a role
        # unable to land its work in the sandbox is a design failure — the
        # roster is pointless if the lead ends up writing everything). The one
        # boundary is promote: the only path to the user's real folder, still
        # lead/implementer + a human approval.
        allowed_roles=list(Role),
        inputs=["filename", "content", "target"],
        primary_input="filename",
        permitted_spaces=[SANDBOX, WORKSPACE],
        mutates=True,
        idempotency="idempotent_for_same_input",
    ),
    "read_file": Skill(
        name="read_file",
        description="Read a file from any space (sandbox/workspace/established).",
        category="read",
        risk=Risk.low,
        requires_approval=False,
        allowed_roles=list(Role),  # discovery is free for every seat (incl. panelists)
        inputs=["filename", "target"],
        primary_input="filename",
        permitted_spaces=[SANDBOX, WORKSPACE, ESTABLISHED],
        mutates=False,
        idempotency="read_only",
    ),
    "search_project": Skill(
        name="search_project",
        description="Search a space for a string in file names and contents.",
        category="read",
        risk=Risk.low,
        requires_approval=False,
        allowed_roles=list(Role),  # discovery is free for every seat (incl. panelists)
        inputs=["query", "target"],
        primary_input="query",
        permitted_spaces=[SANDBOX, WORKSPACE, ESTABLISHED],
        mutates=False,
        idempotency="read_only",
    ),
    "list_dir": Skill(
        name="list_dir",
        description="List the files/folders in a space (sandbox/workspace/established) "
                    "so you can see what exists before reading or writing.",
        category="read",
        risk=Risk.low,
        requires_approval=False,
        allowed_roles=list(Role),  # discovery is free for every seat (incl. panelists)
        inputs=["path", "target"],
        primary_input="path",
        permitted_spaces=[SANDBOX, WORKSPACE, ESTABLISHED],
        mutates=False,
        idempotency="read_only",
    ),
    "git_snapshot": Skill(
        name="git_snapshot",
        description="Inspect branch, HEAD, upstream divergence, and working-tree "
                    "status for a contained Git repository without mutating it.",
        category="read",
        risk=Risk.low,
        requires_approval=False,
        allowed_roles=list(Role),
        inputs=["path", "target"],
        primary_input="path",
        permitted_spaces=[WORKSPACE, ESTABLISHED],
        mutates=False,
        idempotency="read_only",
    ),
    "web_search": Skill(
        name="web_search",
        description="Search the live web and get a cited answer (use for current "
                    "facts, docs, libraries, prior art).",
        category="web",
        risk=Risk.low,
        requires_approval=False,
        allowed_roles=list(Role),  # governed web lookups: free for every seat
        inputs=["query"],
        primary_input="query",
        permitted_spaces=[],
        mutates=False,
        idempotency="external_snapshot",
    ),
    "web_fetch": Skill(
        name="web_fetch",
        description="Fetch a specific public URL and read its text.",
        category="web",
        risk=Risk.low,
        requires_approval=False,
        allowed_roles=list(Role),  # governed web lookups: free for every seat
        inputs=["url"],
        primary_input="url",
        permitted_spaces=[],
        mutates=False,
        idempotency="external_snapshot",
    ),
    "edit_file": Skill(
        name="edit_file",
        description="Surgically replace a unique snippet in a council-space file (sandbox/workspace). Free, no approval.",
        category="file_edit",
        risk=Risk.low,
        requires_approval=False,
        allowed_roles=list(Role),  # council-space edits: free for every seat
        inputs=["filename", "old", "new", "target"],
        primary_input="filename",
        permitted_spaces=[SANDBOX, WORKSPACE],
        mutates=True,
        idempotency="non_idempotent",
    ),
    "run_tests": Skill(
        name="run_tests",
        description="Run a test command in a council space (static checks auto-run; functional tests require approval).",
        category="code_exec",
        risk=Risk.medium,
        requires_approval=True,
        allowed_roles=[Role.lead, Role.implementer, Role.critic, Role.code_generator],
        inputs=["command", "target"],
        primary_input="command",
        permitted_spaces=[SANDBOX, WORKSPACE],
        mutates=True,
        idempotency="unknown",
    ),
    "install_deps": Skill(
        name="install_deps",
        description=(
            "Install the third-party packages a build needs, into this session only. "
            "Always human-approved: it fetches and runs code from the network."
        ),
        category="install",
        risk=Risk.high,
        requires_approval=True,
        allowed_roles=[Role.lead, Role.implementer, Role.code_generator, Role.architect],
        inputs=["packages"],
        primary_input="packages",
        permitted_spaces=[SANDBOX],
        mutates=True,
        idempotency="idempotent",
    ),
    "build_artifact": Skill(
        name="build_artifact",
        description=(
            "Run an approved build command in a council space and capture the files it "
            "produces — the only governed route to a binary deliverable (PDF, archive)."
        ),
        category="code_exec",
        risk=Risk.high,
        requires_approval=True,
        allowed_roles=[Role.lead, Role.implementer, Role.code_generator, Role.architect],
        inputs=["command", "produces", "target"],
        primary_input="command",
        permitted_spaces=[SANDBOX, WORKSPACE],
        mutates=True,
        idempotency="unknown",
    ),
    "stage": Skill(
        name="stage",
        description="Keep a sandbox file by moving it up into the permanent workspace. Free, no approval.",
        category="stage",
        risk=Risk.low,
        requires_approval=False,
        allowed_roles=[Role.lead, Role.implementer],
        inputs=["filename"],
        primary_input="filename",
        permitted_spaces=[SANDBOX, WORKSPACE],
        mutates=True,
        idempotency="idempotent_for_same_input",
    ),
    "promote": Skill(
        name="promote",
        description="Copy a workspace file INTO the external established folder (real code). Requires human approval.",
        category="promote",
        risk=Risk.medium,
        requires_approval=True,
        allowed_roles=[Role.lead, Role.implementer],
        inputs=["filename"],
        primary_input="filename",
        permitted_spaces=[SANDBOX, WORKSPACE, ESTABLISHED],
        mutates=True,
        idempotency="idempotent_for_same_input",
    ),
    "promote_batch": Skill(
        name="promote_batch",
        description="Release an entire verified goal staging manifest in one rollback-protected transaction.",
        category="promote",
        risk=Risk.medium,
        requires_approval=True,
        allowed_roles=[Role.lead, Role.implementer],
        inputs=["files", "baselines"],
        primary_input="files",
        permitted_spaces=[WORKSPACE, ESTABLISHED],
        mutates=True,
        idempotency="conditional",
    ),
}

HANDLERS: dict[str, Handler] = {
    "write_file": _write_file,
    "read_file": _read_file,
    "search_project": _search_project,
    "list_dir": _list_dir,
    "git_snapshot": _git_snapshot,
    "web_search": _web_search,
    "web_fetch": _web_fetch,
    "edit_file": _edit_file,
    "run_tests": _run_tests,
    "build_artifact": _build_artifact,
    "install_deps": _install_deps,
    "stage": _stage,
    "promote": _promote,
    "promote_batch": _promote_batch,
}


def get_skill(name: str) -> Optional[Skill]:
    """Return the registered Skill, or None for an unknown name."""
    return SKILLS.get(name)


def capability_manifest() -> dict:
    """Return the public v1 capability catalogue as JSON-native data.

    The result is detached from the live registry and contains no handler
    references, enum instances, filesystem paths, secrets, or mutable model
    objects.  Sorting makes API responses and saved snapshots deterministic.
    """
    capabilities: list[dict] = []
    for name in sorted(SKILLS):
        skill = SKILLS[name]
        capabilities.append({
            "schema": skill.schema,
            "version": skill.version,
            "name": skill.name,
            "description": skill.description,
            "provider": skill.provider,
            "invocation": skill.invocation,
            "category": skill.category,
            "risk": skill.risk.value,
            "requires_approval": skill.requires_approval,
            "blocked_by_default": skill.blocked_by_default,
            "allowed_roles": [role.value for role in skill.allowed_roles],
            "inputs": list(skill.inputs),
            "primary_input": skill.primary_input,
            "permitted_spaces": list(skill.permitted_spaces),
            "mutates": skill.mutates,
            "idempotency": skill.idempotency,
        })
    return {
        "schema": "gangof8.capability-catalogue",
        "version": 1,
        "capabilities": capabilities,
    }
