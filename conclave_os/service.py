"""Service wiring — one object that owns the store, manager, registry, and
governance, used by both the FastAPI app and the CLI.

Backends:
  mock — deterministic offline adapter (default; tests, Phase 0)
  cli  — Conclave OS runs the local claude/codex/gemini CLIs itself, in plain
         generation mode → real file content; fully self-contained
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from . import cancellation, config, intake, reporting
from .adapters.cli import CliAdapter
from .adapters.mock import MockAdapter
from .adapters.openrouter import OpenRouterAdapter
from .secrets import SecretStore
from .composer import fallback_final
from .governance import Governance
from .logstore import LogStore
from .loop import (
    SessionCancelled,
    resume_deliberation,
    resume_session,
    resume_with_input,
    run_session,
)
from .models import Budgets, Risk, Role, Session, SessionStatus, utcnow
from .paths import extract_established_root
from .registry import AgentError
from .registry import AgentRegistry
from .sessions import SessionManager
from .settings import Settings, budgets_overrides, load_settings, save_settings
from .uploads import UploadStore, attachment_context
from .workspaces import WorkspaceError, WorkspaceStore


class ConclaveService:
    def __init__(
        self,
        data_dir: Optional[Path] = None,
        backend: Optional[str] = None,
        role_agents: Optional[dict[Role, str]] = None,
        panel: Optional[list[str]] = None,
    ):
        self._data_dir = Path(data_dir) if data_dir else config.DATA_DIR
        # Persisted settings layer over config/env. With no settings.json this
        # returns the pure config/env defaults, so behaviour is unchanged.
        self.settings = load_settings(self._data_dir)
        self._explicit_role_agents = role_agents  # explicit arg always wins
        # Explicit panel roster: None ⇒ derive per backend; [] ⇒ no panel
        # (lead-only solo mode — fast runs and focused tests).
        self._explicit_panel = panel

        self.store = LogStore(self._data_dir)
        self.manager = SessionManager(self.store)
        self.governance = Governance(self.store)
        self.workspaces = WorkspaceStore(self._data_dir)
        self.uploads = UploadStore(self._data_dir)
        self.secrets = SecretStore(self._data_dir)
        # background workers for service mode — sessions on real backends take
        # minutes, so the dashboard submits and polls instead of blocking
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="conclave-os")
        self._apply_settings(backend=backend)

    def _apply_settings(self, backend: Optional[str] = None) -> None:
        """(Re)derive backend, role mapping and registry from
        the explicit args + current self.settings. Precedence for backend:
        explicit arg › settings.json › env/config default."""
        self.backend = backend or self.settings.backend
        if self.backend not in config.ROLE_AGENTS_BY_BACKEND:
            raise ValueError(f"unknown backend '{self.backend}' (mock | cli)")
        # role mapping: explicit arg › settings (non-empty) › backend default
        if self._explicit_role_agents:
            self.role_agents = self._explicit_role_agents
        elif self.settings.role_agents:
            base = dict(config.ROLE_AGENTS_BY_BACKEND[self.backend])
            base.update({
                Role(role): agent for role, agent in self.settings.role_agents.items()
            })
            self.role_agents = base
        else:
            self.role_agents = config.ROLE_AGENTS_BY_BACKEND[self.backend]

        # Push governance/composer tunables into the config module so the loop,
        # classifier and composer (which read config.* at call time) honour
        # settings. With no settings.json these equal the existing config
        # values, so this is a no-op and behaviour is unchanged.
        config.RISK_BOUNDARY = Risk(self.settings.risk_boundary)
        config.COMPOSER_PROSE_MIN_CHARS = self.settings.composer.prose_min_chars
        config.COMPOSER_RESERVED_CALLS = self.settings.composer.reserved_calls
        config.MAX_CRITIC_TESTS_PER_ROUND = self.settings.composer.max_critic_tests
        config.ROUNDS_PER_CONSENT = self.settings.rounds_per_consent
        config.BUDGETS_BY_COMPLEXITY = budgets_overrides(self.settings)

        self.registry = AgentRegistry()
        if self.backend == "cli":
            # OpenRouter seats: enabled ones + any referenced in the role map.
            enabled = {n for n, on in (self.settings.openrouter_enabled or {}).items() if on}
            referenced = {a for a in self.role_agents.values() if a in config.OPENROUTER_SEATS}
            for seat in sorted(enabled | referenced):
                self._register_openrouter(seat)
            # CLI adapters for every non-OpenRouter agent in the role map.
            for agent in sorted(set(self.role_agents.values())):
                if agent not in config.OPENROUTER_SEATS:
                    self.registry.register(CliAdapter(agent=agent))
        else:
            self.registry.register(MockAdapter())
        self.panel = self._effective_panel()

    def _effective_panel(self) -> list[str]:
        """The seats that contribute every round. Explicit ctor arg › settings
        roster › backend default (installed CLI agents + enabled, keyed
        OpenRouter seats). Degrades gracefully: no OpenRouter key ⇒ CLI-only;
        a seat with no registered adapter is dropped."""
        import shutil

        if self._explicit_panel is not None:
            # trusted as-is: the caller (tests, embedders) registers its own
            # adapters, possibly after construction
            return list(self._explicit_panel)
        if self.settings.panel_seats:
            return [s for s in self.settings.panel_seats if s in self.registry.names()]
        seats = list(config.PANEL_SEATS_BY_BACKEND.get(self.backend, ["mock"]))
        if self.backend == "cli":
            seats = [s for s in seats if shutil.which(s)]
            if self.secrets.has("openrouter"):
                seats += sorted(
                    n for n, on in (self.settings.openrouter_enabled or {}).items()
                    if on and self._openrouter_slug(n)
                )
        return [s for s in seats if s in self.registry.names()]

    def _openrouter_slug(self, seat: str) -> Optional[str]:
        """Effective model slug for a seat: a user override (settings) wins over
        the built-in default in config.OPENROUTER_SEATS."""
        override = (self.settings.openrouter_models or {}).get(seat)
        if override and override.strip():
            return override.strip()
        spec = config.OPENROUTER_SEATS.get(seat)
        return spec["model_slug"] if spec else None

    def _register_openrouter(self, seat: str) -> None:
        slug = self._openrouter_slug(seat)
        if not slug:
            return
        self.registry.register(OpenRouterAdapter(
            name=seat, model_slug=slug,
            api_key_getter=lambda: self.secrets.get("openrouter"),
            endpoint=config.OPENROUTER_ENDPOINT,
            data_collection=config.OPENROUTER_DATA_COLLECTION,
        ))

    # role_agents/budgets are the COMPLETE intended set (the dashboard sends all
    # non-default picks each save), so replace them wholesale — merging would
    # make stale entries linger and break "reset to backend default". Nested
    # composer/ui are partial-friendly and still merge.
    _REPLACE_KEYS = {"role_agents", "budgets", "openrouter_enabled", "openrouter_models"}

    def set_openrouter_key(self, value: str) -> dict:
        self.secrets.set("openrouter", value or "")
        return self.openrouter_key_status()

    def clear_openrouter_key(self) -> dict:
        self.secrets.clear("openrouter")
        return self.openrouter_key_status()

    def openrouter_key_status(self) -> dict:
        """Masked status of the OpenRouter key — never returns the full key."""
        return {
            "present": self.secrets.has("openrouter"),
            "source": self.secrets.source("openrouter"),  # 'env' | 'stored' | None
            "masked": SecretStore.mask(self.secrets.get("openrouter")),
        }

    def update_settings(self, patch: dict) -> Settings:
        """Apply a partial settings patch, persist it, and re-derive the
        backend/role mapping/registry. Some changes (backend, role mapping)
        affect new sessions; in-flight sessions keep their own backend."""
        merged = self.settings.model_dump()
        for key, value in (patch or {}).items():
            if key not in merged:
                continue
            if key not in self._REPLACE_KEYS and isinstance(merged[key], dict) and isinstance(value, dict):
                merged[key].update(value)
            else:
                merged[key] = value
        self.settings = Settings.model_validate(merged)
        save_settings(self.settings, self._data_dir)
        self._apply_settings()
        return self.settings

    def _open(self, text: str, source: str, budgets: Optional[Budgets],
              attachments: Optional[list[str]] = None) -> Session:
        """Create a session, stamping the backend, the active workspace root
        (so file skills operate in that project; None ⇒ per-session sandbox),
        and folding any attachment text into the task the council reads."""
        full_text = (text or "") + attachment_context(self.uploads, attachments or [])
        session = intake.receive(full_text, source, self.manager, budgets)
        session.backend = self.backend
        session.panel = list(self.panel)
        active = self.workspaces.active()
        session.workspace_root = active.root if active else None
        # Established folder is PER TASK: interpret a path the user referenced in
        # the prompt (a file → its parent). None ⇒ the greenfield gate may ask.
        session.established_root = extract_established_root(text or "")
        for uid in attachments or []:
            rec = self.uploads.get(uid)
            if rec:
                session.attachments.append({"id": rec["id"], "name": rec["name"], "kind": rec["kind"]})
        self.store.save_session(session)
        return session

    def run(self, text: str, source: str = "cli", budgets: Optional[Budgets] = None,
            attachments: Optional[list[str]] = None) -> Session:
        session = self._open(text, source, budgets, attachments)
        return run_session(
            session, self.manager, self.registry, self.governance, self.store,
            role_agents=self.role_agents,
        )

    def submit_background(self, text: str, source: str = "api",
                          budgets: Optional[Budgets] = None,
                          attachments: Optional[list[str]] = None) -> Session:
        """Create the session and run it on a worker thread; the caller polls
        GET /sessions/{id} for progress."""
        session = self._open(text, source, budgets, attachments)
        self._pool.submit(self._safely, session, self._run_full)
        return session

    def save_upload(self, name: str, content_b64: str) -> dict:
        return self.uploads.save(name, content_b64)

    def pick_folder(self) -> dict:
        """Open the host's native folder dialog and return the chosen absolute
        path. Localhost-dashboard convenience (browsers can't expose a real path
        from a folder picker). Windows only; the dialog requires user selection."""
        import base64
        import subprocess
        import sys

        if sys.platform != "win32":
            return {"path": None, "error": "folder picker is available on Windows only"}
        # The dialog is spawned by the (background, no-console) server process,
        # which can't steal focus from the foreground browser (Windows
        # foreground lock), so it opens BEHIND it. A background thread finds the
        # dialog window (class #32770) once it appears and repeatedly forces it
        # to the front (AttachThreadInput + foreground-lock-timeout disabled).
        ps = r'''
Add-Type -AssemblyName System.Windows.Forms
Add-Type -TypeDefinition @"
using System;
using System.Text;
using System.Threading;
using System.Diagnostics;
using System.Runtime.InteropServices;
public static class Fg {
  delegate bool EnumProc(IntPtr h, IntPtr l);
  [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc cb, IntPtr l);
  [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] static extern int GetClassName(IntPtr h, StringBuilder s, int max);
  [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] static extern uint GetCurrentThreadId();
  [DllImport("user32.dll")] static extern bool AttachThreadInput(uint a, uint b, bool f);
  [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] static extern bool BringWindowToTop(IntPtr h);
  [DllImport("user32.dll")] static extern bool SystemParametersInfo(int a, int b, IntPtr c, int d);
  public static void StartForcer() {
    var t = new Thread(() => {
      int mypid = Process.GetCurrentProcess().Id;
      for (int i = 0; i < 30; i++) {
        Thread.Sleep(120);
        IntPtr dlg = IntPtr.Zero;
        EnumWindows((h, l) => {
          uint pid; GetWindowThreadProcessId(h, out pid);
          if ((int)pid == mypid && IsWindowVisible(h)) {
            var sb = new StringBuilder(64); GetClassName(h, sb, 64);
            if (sb.ToString() == "#32770") { dlg = h; return false; }
          }
          return true;
        }, IntPtr.Zero);
        if (dlg != IntPtr.Zero) {
          SystemParametersInfo(0x2001, 0, IntPtr.Zero, 0);
          uint fg; GetWindowThreadProcessId(GetForegroundWindow(), out fg);
          uint cur = GetCurrentThreadId();
          AttachThreadInput(fg, cur, true);
          BringWindowToTop(dlg); SetForegroundWindow(dlg);
          AttachThreadInput(fg, cur, false);
        }
      }
    });
    t.IsBackground = true; t.Start();
  }
}
"@
[Fg]::StartForcer()
$d = New-Object System.Windows.Forms.FolderBrowserDialog
$d.Description = 'Select a workspace folder for Conclave OS'
$d.ShowNewFolderButton = $true
$r = $d.ShowDialog()
if ($r -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Out.Write($d.SelectedPath) }
'''
        enc = base64.b64encode(ps.encode("utf-16-le")).decode("ascii")
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-STA", "-EncodedCommand", enc],
                capture_output=True, text=True, timeout=300,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # no console flash
            )
        except Exception as e:  # noqa: BLE001 — never crash the dashboard
            return {"path": None, "error": str(e)}
        path = (proc.stdout or "").strip()
        return {"path": path or None}

    # ---- Workspaces ----------------------------------------------------------

    def list_workspaces(self) -> dict:
        active = self.workspaces.active()
        return {
            "workspaces": [w.model_dump() for w in self.workspaces.list()],
            "active": active.id if active else None,
            # neutral, isolated scratch location — never under a project folder
            "sandbox_root": str(config.SANDBOX_ROOT.resolve()),
        }

    def create_workspace(self, name: str, root: str):
        return self.workspaces.add(name, root)

    def set_active_workspace(self, workspace_id):
        return self.workspaces.set_active(workspace_id)

    def remove_workspace(self, workspace_id: str) -> None:
        self.workspaces.remove(workspace_id)

    def empty_workspace(self, workspace_id: Optional[str] = None) -> dict:
        """Delete the CONTENTS of a workspace dir (default: the active one). The
        workspace is the council's own accumulation area — emptying it starts a
        fresh project in the same folder. Does NOT touch any established folder."""
        import shutil

        ws = self.workspaces.get(workspace_id) if workspace_id else self.workspaces.active()
        if ws is None:
            raise WorkspaceError("no workspace to empty")
        root = Path(ws.root)
        removed = 0
        if root.is_dir():
            for child in root.iterdir():
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink()
                removed += 1
        self.store.log_event("-", "workspace_emptied", {"id": ws.id, "removed": removed})
        return {"emptied": ws.id, "removed": removed}

    def _run_full(self, session: Session) -> Session:
        return run_session(
            session, self.manager, self.registry, self.governance, self.store,
            role_agents=self.role_agents,
        )

    def _resume_full(self, session: Session) -> Session:
        return resume_session(
            session, self.manager, self.registry, self.governance, self.store,
            role_agents=self.role_agents,
        )

    def _safely(self, session: Session, fn, *args) -> Session:
        """Background guard: a session must never die silently in a thread."""
        try:
            return fn(session, *args)
        except SessionCancelled:
            cancellation.clear(session.session_id)
            session.stop_reason = "cancelled by user"
            try:
                self.manager.transition(session, SessionStatus.cancelled)
            except ValueError:
                session.status = SessionStatus.cancelled
                self.store.save_session(session)
            self.store.log_event(session.session_id, "session_cancelled", {})
            return session
        except Exception as e:  # noqa: BLE001 — last-resort containment
            self.store.log_event(session.session_id, "internal_error", {"detail": str(e)})
            try:
                self.manager.transition(session, SessionStatus.failed)
            except ValueError:
                session.status = SessionStatus.failed
                self.store.save_session(session)
            return session

    def _ensure_adapters(self, session: Session) -> None:
        """A loaded session must be resumable regardless of how this service
        instance was configured — register the adapters its agents need."""
        if session.backend != "cli":
            return
        needed = {m.agent for m in session.council.members if m.agent and m.agent != "system"}
        needed |= {r.agent for r in session.input_requests if r.agent}
        for agent in sorted(needed):
            if agent in self.registry.names() or agent in ("mock", "unknown"):
                continue
            if agent in config.OPENROUTER_SEATS:
                self._register_openrouter(agent)
            else:
                self.registry.register(CliAdapter(agent=agent))

    # ---- CLI seats (settings panel) ------------------------------------------

    def seats(self) -> dict:
        """All seats the council can use, with availability — used to populate the
        role→agent dropdowns. CLI seats are available when on PATH; OpenRouter
        seats when enabled AND an API key is present."""
        import shutil

        cli = [
            {"name": a, "available": shutil.which(a) is not None, "kind": "cli", "label": a}
            for a in ("claude", "codex", "gemini")
        ]
        key_present = self.secrets.has("openrouter")
        enabled = self.settings.openrouter_enabled or {}
        openrouter = [
            {"name": name, "kind": "openrouter", "label": spec["label"],
             "model_slug": self._openrouter_slug(name),
             "default_slug": spec["model_slug"],
             "enabled": bool(enabled.get(name)),
             "available": bool(enabled.get(name)) and key_present}
            for name, spec in config.OPENROUTER_SEATS.items()
        ]
        return {"seats": cli + openrouter, "openrouter_key": key_present}

    def list_dir(self, path: Optional[str] = None) -> dict:
        """List sub-directories of `path` for the in-page folder browser. With no
        path, list drive roots (Windows) or '/'. Folders only — never reads file
        contents. Localhost convenience for picking a workspace."""
        import os
        import string
        import sys

        if not path:
            if sys.platform == "win32":
                drives = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
                return {"path": "", "parent": None, "dirs": drives}
            path = "/"
        p = Path(path)
        if not p.is_dir():
            return {"path": str(path), "parent": "", "dirs": [], "error": "not a directory"}
        p = p.resolve()
        parent = "" if p.parent == p else str(p.parent)
        dirs: list[str] = []
        try:
            for child in p.iterdir():
                try:
                    if child.is_dir():
                        dirs.append(str(child))
                except OSError:
                    continue  # unreadable entry — skip
        except (PermissionError, OSError) as e:
            return {"path": str(p), "parent": parent, "dirs": [], "error": str(e)}
        dirs.sort(key=str.lower)
        return {"path": str(p), "parent": parent, "dirs": dirs}

    def fs_shortcuts(self) -> dict:
        """Quick-access locations for the folder browser sidebar: Home + the
        common user folders that exist, then This PC (drives)."""
        home = Path.home()
        items = [{"label": "Home", "icon": "🏠", "path": str(home)}]
        for label, sub, icon in [
            ("Desktop", "Desktop", "🖥"), ("Documents", "Documents", "📄"),
            ("Downloads", "Downloads", "⬇"), ("Pictures", "Pictures", "🖼"),
            ("Videos", "Videos", "🎬"), ("Music", "Music", "🎵"),
        ]:
            p = home / sub
            if p.is_dir():
                items.append({"label": label, "icon": icon, "path": str(p)})
        items.append({"label": "This PC", "icon": "💻", "path": ""})
        return {"shortcuts": items}

    def make_dir(self, path: str, name: str) -> dict:
        """Create a sub-folder for the in-page browser's New-folder button, then
        return the refreshed listing of the parent."""
        name = (name or "").strip()
        if not name or any(c in name for c in '<>:"/\\|?*'):
            return {"error": "invalid folder name"}
        base = Path(path)
        if not base.is_dir():
            return {"error": "parent is not a directory"}
        try:
            (base / name).mkdir(exist_ok=True)
        except OSError as e:
            return {"error": str(e)}
        return self.list_dir(str(base))

    def get(self, session_id: str) -> Optional[dict]:
        return self.store.load_session(session_id)

    def delete_session(self, session_id: str) -> bool:
        """Delete a session from the store (DB + JSONL log)."""
        return self.store.delete_session(session_id)

    def continue_session(self, session_id: str, text: str, background: bool = True) -> Session:
        """Continue the conversation: the human responds to the council's
        conclusion and the council deliberates AGAIN with the full thread as
        context — no starting over. Re-opens a settled (done) session."""
        if not (text or "").strip():
            raise ValueError("response text required")
        session = self.manager.load(session_id)
        if session is None:
            raise KeyError(f"session {session_id} not found")
        if session.status != SessionStatus.done:
            raise ValueError(f"cannot continue a session in status '{session.status.value}'")
        self._ensure_adapters(session)
        # seed turn-one history for sessions created before the conversation feature
        if not session.turns:
            session.turns.append({"role": "user", "text": session.task.text})
            if session.final:
                session.turns.append({"role": "council", "text": session.final.answer})
        session.turns.append({"role": "user", "text": text.strip()})
        # reset per-turn deliberation state (keep turns, backend, roots, files)
        session.rounds = []
        session.contributions = []
        session.disagreements = []
        session.truth_claims = []
        session.proposed_actions = []
        session.approvals = []
        session.input_requests = []
        session.unresolved = []
        session.tools_called = []
        session.agent_calls = 0
        session.final = None
        session.stop_reason = None
        session.current_round = 0
        session.classification = None
        session.risk_exceeds_boundary = False
        session.blocked_on_missing_info = False
        session.status = SessionStatus.received  # re-open (bypass terminal transition)
        cancellation.clear(session_id)
        self.store.log_event(session_id, "conversation_continued", {"turn": len(session.turns)})
        self.store.save_session(session)
        if background:
            self._pool.submit(self._safely, session, self._run_full)
            return session
        return self._safely(session, self._run_full)

    def timeline(self, session_id: str) -> dict:
        """A readable run timeline built from the session's JSONL event log."""
        import json as _json

        path = self.store.session_log_path(session_id)
        events: list[dict] = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    events.append(_json.loads(line))
                except _json.JSONDecodeError:
                    continue
        return {"session_id": session_id, "events": reporting.format_timeline(events)}

    _TERMINAL = {SessionStatus.done, SessionStatus.failed, SessionStatus.cancelled}
    _PAUSED = {SessionStatus.awaiting_approval, SessionStatus.awaiting_input}

    def cancel_session(self, session_id: str) -> dict:
        """Cancel a session. If it's paused (awaiting approval/input) no worker is
        running, so cancel it immediately. If it's mid-run, request cooperative
        cancellation — the worker stops at the next agent-call checkpoint (an
        in-flight CLI call finishes first)."""
        session = self.manager.load(session_id)
        if session is None:
            raise KeyError(f"session {session_id} not found")
        if session.status in self._TERMINAL:
            return {"session_id": session_id, "status": session.status.value, "note": "already finished"}
        if session.status in self._PAUSED:
            for a in session.approvals:
                if a.status == "pending":
                    a.status = "denied"
            for r in session.input_requests:
                if r.status == "pending":
                    r.status = "declined"
            session.stop_reason = "cancelled by user"
            cancellation.clear(session_id)
            self.manager.transition(session, SessionStatus.cancelled)
            self.store.log_event(session_id, "session_cancelled", {"from": "paused"})
            return {"session_id": session_id, "status": "cancelled"}
        # running — flag it; the worker finalizes to cancelled at the next checkpoint
        cancellation.request(session_id)
        self.store.log_event(session_id, "cancel_requested", {})
        return {"session_id": session_id, "status": "cancelling"}

    def list(self) -> list[dict]:
        return self.store.list_sessions()

    def approve(self, session_id: str, approval_id: str, approved: bool,
                by: str = "user", background: bool = False,
                approve_all: bool = False) -> Session:
        """Resolve an approval. Approving the last pending approval on a paused
        session resumes it. Denying a session gate cancels the session; denying
        an action approval (action_ref set) skips just that action — the
        session resumes and completes without the artifact. `approve_all`
        grants a session-wide standing approval for the category (and clears
        its pending siblings) so N identical gates need one decision."""
        session = self.manager.load(session_id)
        if session is None:
            raise KeyError(f"session {session_id} not found")
        self._ensure_adapters(session)
        approval = self.governance.resolve(session, approval_id, approved, by=by,
                                           approve_all=approve_all)
        if session.status != SessionStatus.awaiting_approval:
            return session  # nothing to resume — approval was informational
        if not approved and approval.action_ref is None:
            session.stop_reason = "approval denied"
            self.manager.transition(session, SessionStatus.cancelled)
            return session
        if session.has_pending_approval:
            return session  # other gates still open; stay paused
        if background:
            self._pool.submit(self._safely, session, self._resume_full)
            return session
        return self._resume_full(session)

    def pending_approvals(self) -> list[dict]:
        return self._pending(SessionStatus.awaiting_approval, "approvals")

    def pending_inputs(self) -> list[dict]:
        return self._pending(SessionStatus.awaiting_input, "input_requests")

    def _pending(self, status: SessionStatus, field: str) -> list[dict]:
        pending = []
        for meta in self.store.list_sessions():
            if meta["status"] != status.value:
                continue
            data = self.store.load_session(meta["session_id"])
            if not data:
                continue
            pending.extend(
                {**item, "task_text": data["task"]["text"]}
                for item in data.get(field, [])
                if item.get("status") == "pending"
            )
        return pending

    def _load_input(self, session_id: str, input_id: str):
        session = self.manager.load(session_id)
        if session is None:
            raise KeyError(f"session {session_id} not found")
        self._ensure_adapters(session)
        req = next((r for r in session.input_requests if r.input_id == input_id), None)
        if req is None:
            raise KeyError(f"no input request {input_id} on session {session_id}")
        if req.status != "pending":
            raise ValueError(f"input request {input_id} already {req.status}")
        return session, req

    def answer(self, session_id: str, input_id: str, answer_text: str,
               by: str = "user", background: bool = False) -> Session:
        """Answer an agent's question: the paused backend call is resumed with
        the human's answer and the session continues to completion."""
        if not (answer_text or "").strip():
            raise ValueError("answer text required")
        session, req = self._load_input(session_id, input_id)
        req.status = "answered"
        req.answer = answer_text
        req.resolved_at = utcnow()
        req.resolved_by = by
        self.store.log_event(session_id, "input_answered", req.model_dump())
        self.store.save_session(session)
        if background:
            self._pool.submit(self._safely, session, self._answer_continue, req)
            return session
        return self._answer_continue(session, req)

    # answers that keep the build in the council's own spaces (no delivery target)
    _WORKSPACE_ANSWERS = {"workspace", "sandbox", "none", "skip", "no", "keep", "here"}
    # answers that end the rotation and compose from the work done so far
    _STOP_ANSWERS = {"no", "n", "stop", "finish", "done", "compose", "wrap up", "enough"}

    def _answer_continue(self, session: Session, req) -> Session:
        # Round-consent question: 'yes' (or anything unrecognized) grants another
        # block of rounds, a number grants exactly that many, 'no'/'stop' composes
        # the final answer from the work so far.
        if req.agent == "system" and req.purpose == "continue_rounds":
            import re as _re

            ans = (req.answer or "").strip().lower()
            m = _re.match(r"^\s*(\d+)", ans)
            if ans in self._STOP_ANSWERS:
                session.compose_now = True
            elif m:
                session.consent_extra_rounds += int(m.group(1))
            else:
                session.consent_extra_rounds += config.ROUNDS_PER_CONSENT
            self.store.save_session(session)
            return resume_deliberation(
                session, self.manager, self.registry, self.governance,
                self.store, role_agents=self.role_agents,
            )

        # Delivery-target question, asked at promote time: 'workspace' keeps the
        # files in the council's spaces (promotes skipped); a path becomes the
        # established root and each promote then flows through the ONE hard gate
        # (the diff-carrying promote approval).
        if req.agent == "system" and req.purpose == "promote_target":
            session.established_asked = True
            ans = (req.answer or "").strip()
            if ans.lower() in self._WORKSPACE_ANSWERS:
                for a in session.proposed_actions:
                    if a.kind == "promote" and a.status == "proposed":
                        a.status = "denied"
                        a.error = "user kept the files in the council workspace"
            else:
                picked = extract_established_root(ans)
                if picked is None and ("/" in ans or "\\" in ans):
                    picked = str(Path(ans).expanduser().resolve())
                session.established_root = picked
            self.store.save_session(session)
            return resume_deliberation(
                session, self.manager, self.registry, self.governance,
                self.store, role_agents=self.role_agents,
            )

        # System greenfield-target question (legacy — sessions paused on disk
        # before the promote-time ask replaced the up-front gate): interpret the
        # answer, set the established folder, then start deliberation.
        if req.agent == "system" and req.purpose == "establish_target":
            session.established_asked = True
            ans = (req.answer or "").strip()
            if ans.lower() not in self._WORKSPACE_ANSWERS:
                picked = extract_established_root(ans)
                if picked is None and ("/" in ans or "\\" in ans):
                    picked = str(Path(ans).expanduser().resolve())
                session.established_root = picked
            self.store.save_session(session)
            return run_session(
                session, self.manager, self.registry, self.governance,
                self.store, role_agents=self.role_agents,
            )
        try:
            result = self.registry.resume(req.agent, req.resume_token, req.answer)
        except AgentError as e:
            session.unresolved.append(f"resume after user input failed: {e}")
            self.store.log_event(session.session_id, "agent_error", {"detail": str(e)})
            self.manager.transition(session, SessionStatus.composing)
            session.final = fallback_final(session, "agent resume failed")
            self.manager.transition(session, SessionStatus.done)
            self.store.save_session(session)
            return session
        return resume_with_input(
            session, self.manager, self.registry, self.governance, self.store,
            self.role_agents, req, result,
        )

    def decline_input(self, session_id: str, input_id: str, by: str = "user") -> Session:
        """Decline to answer: the paused backend call is cancelled (best
        effort) and the session is cancelled."""
        session, req = self._load_input(session_id, input_id)
        req.status = "declined"
        req.resolved_at = utcnow()
        req.resolved_by = by
        self.store.log_event(session_id, "input_declined", req.model_dump())
        self.registry.cancel(req.agent, req.resume_token)
        session.stop_reason = "input declined"
        self.manager.transition(session, SessionStatus.cancelled)
        return session
