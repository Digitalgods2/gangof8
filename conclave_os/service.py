"""Service wiring — one object that owns the store, manager, registry, and
governance, used by both the FastAPI app and the CLI.

Backends:
  mock — deterministic offline adapter (default; tests, Phase 0)
  cli  — Conclave OS runs the local claude/codex/gemini CLIs itself, in plain
         generation mode → real file content; fully self-contained
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
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
from .paths import extract_delivery_target, extract_established_root
from .registry import AgentError
from .registry import AgentRegistry
from .sessions import SessionManager
from .settings import Settings, budgets_overrides, load_settings, save_settings
from .uploads import UploadStore, attachment_context
from .workspaces import WorkspaceError, WorkspaceStore


# The "Enhance" button runs the lead model with this to amplify a raw prompt into
# a sharper, more effective one — then returns ONLY the amplified prompt.
AMPLIFY_PROMPT = """\
You are a Prompt Amplification Engine. Your sole function is to receive a simple, raw prompt and return a dramatically \
superior version of it — one that will extract the deepest, most useful, and most precise response from any AI model.

PROCESS
Phase 1 — Intent Deconstruction
Before rewriting anything, silently analyze the original prompt across these dimensions:
- Core intent: What does the user actually want? What outcome are they after?
- Domain: Is this technical, creative, philosophical, practical, scientific, personal?
- Implicit assumptions: What is the user taking for granted or leaving unsaid?
- Gaps: What critical context, constraints, or specifications are missing that, if added, would sharply improve the output?
- Audience & tone: Who is this for? What register fits — formal, conversational, academic, raw?

Phase 2 — Strategic Amplification
Rewrite the prompt by applying ONLY the techniques relevant to the domain and intent. Do not apply all techniques universally — match the tool to the task:
- Precision language: Replace vague words with exact, high-signal terms.
- Scope framing: Define boundaries. Tell the model what to include AND what to exclude.
- Perspective injection: Where useful, specify a viewpoint, expertise level, or role the model should adopt.
- Output architecture: Specify the desired structure — numbered steps, comparative table, narrative arc, decision matrix, annotated code — whatever format best serves the intent.
- Depth calibration: Add directives like "explain the underlying mechanism," "include edge cases," "address common misconceptions," or "provide the non-obvious insight" — but only when the topic warrants depth.
- Constraint seeding: Add productive constraints that force quality — word limits, required examples, "avoid clichés," "no filler," "prioritize actionable specifics."
- Domain-matched descriptors: For scientific prompts, add rigor. For creative prompts, add sensory and emotional texture. For strategic prompts, add frameworks and tradeoffs. Never cross-contaminate.

Phase 3 — Compression & Polish
Remove any amplification that adds words without adding value. The amplified prompt must feel intentional, not bloated. It should read as if written by someone who deeply understands both the subject and how to communicate with AI.

RULES
- Never change the user's original intent. Amplify it, don't redirect it.
- Never add fluff. Every added word must earn its place.
- If the original prompt is already strong, make surgical improvements — don't rewrite for the sake of rewriting.
- Do not explain your process. Output ONLY the amplified prompt, ready to use.
- Preserve the user's voice where a clear voice exists.
- Preserve exact literals VERBATIM — file paths, filenames, URLs, commands, code, and identifiers must be copied character-for-character. Never reword, split, re-quote, or add drive/root mentions around them (e.g. do not turn "C:\\Users\\me\\proj\\index.html" into "the C:\\ drive … at C:\\Users\\me\\proj\\index.html"). Keep each such literal as a single unbroken token.

OUTPUT: Return ONLY the amplified prompt as plain text — no preamble, no commentary, no surrounding code fence."""


def _dir_mtime(p: Path) -> float:
    """A directory's mtime for GC ordering; 0 if it vanished under us."""
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _strip_fence(s: str) -> str:
    """Drop a wrapping ``` code fence if the model added one, so the textarea
    gets the clean prompt."""
    s = (s or "").strip()
    if s.startswith("```"):
        lines = s.split("\n")
        lines = lines[1:]  # opening ``` / ```text
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines)
    return s.strip()


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
        self._model_catalog_cache: Optional[tuple[float, dict]] = None
        self._or_catalog_cache: Optional[tuple[float, dict]] = None
        self._apply_settings(backend=backend)
        # Crash recovery: a previous process may have died mid-run (e.g. a
        # restart), leaving sessions stuck in a live state with no worker to
        # advance or cancel them. Finalize those now so they can't linger as
        # un-cancellable "deliberating" ghosts.
        self._reconcile_orphans()

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

        # Local CLI seats the user disabled in Settings fall back to an enabled
        # OpenRouter seat, so the council can run OpenRouter-only. Skipped for an
        # explicit role map (tests/embedders manage their own registration).
        if self.backend == "cli" and not self._explicit_role_agents:
            self.role_agents = self._apply_cli_disable(self.role_agents)

        self.registry = AgentRegistry()
        if self.backend == "cli":
            # OpenRouter seats: enabled ones + any referenced in the role map.
            enabled = {n for n, on in (self.settings.openrouter_enabled or {}).items() if on}
            referenced = {a for a in self.role_agents.values() if a in config.OPENROUTER_SEATS}
            for seat in sorted(enabled | referenced):
                self._register_openrouter(seat)
            # CLI adapters for every non-OpenRouter agent in the role map,
            # pinned to the model chosen in Settings (else the CLI's default).
            # gemini also gets the key getter so a Settings-stored key (not
            # just the env var) unlocks its SDK path.
            for agent in sorted(set(self.role_agents.values())):
                if agent not in config.OPENROUTER_SEATS:
                    self.registry.register(CliAdapter(
                        agent=agent, model=(self.settings.cli_models or {}).get(agent),
                        role_models=self._role_pins_for(agent),
                        api_key_getter=(lambda: self.secrets.get("gemini"))
                        if agent == "gemini" else None))
        else:
            self.registry.register(MockAdapter())
        self.panel = self._effective_panel()

    def _disabled_cli_seats(self) -> set[str]:
        """Local CLI seats the user turned OFF in Settings (absent ⇒ enabled)."""
        ce = self.settings.cli_enabled or {}
        return {s for s in ("claude", "codex", "gemini") if ce.get(s, True) is False}

    def _openrouter_fallbacks(self) -> list[str]:
        """Enabled OpenRouter seats (with a resolvable slug), in a stable order —
        the pool a disabled CLI seat's roles fall back to."""
        enabled = self.settings.openrouter_enabled or {}
        return [n for n in config.OPENROUTER_SEATS
                if enabled.get(n) and self._openrouter_slug(n)]

    def _apply_cli_disable(self, base: dict) -> dict:
        """Reassign every role held by a disabled CLI seat to an enabled
        OpenRouter seat (round-robin, so the lead and talents don't all collapse
        onto one model). No-op unless there is at least one OpenRouter seat to
        fall back to — a disable with nothing to replace it is ignored so a role
        (especially the lead) is never left with no seat."""
        disabled = self._disabled_cli_seats()
        if not disabled:
            return base
        pool = self._openrouter_fallbacks()
        if not pool:
            return base
        out = dict(base)
        i = 0
        for role, agent in base.items():
            if agent in disabled:
                out[role] = pool[i % len(pool)]
                i += 1
        return out

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
            disabled = self._disabled_cli_seats()
            seats = [s for s in seats if shutil.which(s) and s not in disabled]
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
            role_models=self._role_pins_for(seat),
        ))

    def _role_pins_for(self, agent: str) -> dict[str, str]:
        """The per-role model pins that apply to THIS seat: a pin follows its
        role only while the role is mapped to the seat, so a model id can
        never leak to a different vendor's CLI (pinning code_generator to
        opus must not pass '--model opus' to gemini after a remap)."""
        out: dict[str, str] = {}
        for role_name, model in (self.settings.role_models or {}).items():
            if not (model or "").strip():
                continue
            try:
                role = Role(role_name)
            except ValueError:
                continue  # a stale pin for a role that no longer exists
            if self.role_agents.get(role) == agent:
                out[role_name] = model.strip()
        return out

    # role_agents/budgets are the COMPLETE intended set (the dashboard sends all
    # non-default picks each save), so replace them wholesale — merging would
    # make stale entries linger and break "reset to backend default". Nested
    # composer/ui are partial-friendly and still merge.
    _REPLACE_KEYS = {"role_agents", "budgets", "openrouter_enabled", "openrouter_models",
                     "cli_models", "cli_enabled", "role_models"}

    # API keys the app knows how to use. "openrouter" unlocks the OpenRouter
    # seats; "gemini" is OPTIONAL and upgrades the gemini seat (SDK path),
    # Google's own model list in the Settings dropdown, image vision, and
    # web_search grounding. Nothing else needs a key — the CLIs auth
    # themselves and the model dropdown's public catalog is key-free.
    KNOWN_API_KEYS = ("openrouter", "gemini")

    def api_key_status(self, name: str) -> dict:
        """Masked status of a stored/env API key — never returns the full key."""
        if name not in self.KNOWN_API_KEYS:
            raise KeyError(f"unknown API key {name!r}")
        return {
            "name": name,
            "present": self.secrets.has(name),
            "source": self.secrets.source(name),  # 'env' | 'stored' | None
            "masked": SecretStore.mask(self.secrets.get(name)),
        }

    def reveal_api_key(self, name: str) -> dict:
        """The FULL stored/env key, for the dashboard's explicit eye-reveal.
        The dashboard binds to localhost and a stored key already lives in
        plaintext in data/secrets.json owned by the same user — this adds
        convenience, not exposure. Status calls stay masked; the full value
        ships only on this explicit request and is never embedded in the
        rendered settings page."""
        if name not in self.KNOWN_API_KEYS:
            raise KeyError(f"unknown API key {name!r}")
        return {
            "name": name,
            "present": self.secrets.has(name),
            "value": self.secrets.get(name) or "",
            "source": self.secrets.source(name),
        }

    def set_api_key(self, name: str, value: str) -> dict:
        if name not in self.KNOWN_API_KEYS:
            raise KeyError(f"unknown API key {name!r}")
        self.secrets.set(name, value or "")
        self._model_catalog_cache = None  # a new key may unlock a better catalog
        self._or_catalog_cache = None
        return self.api_key_status(name)

    def clear_api_key(self, name: str) -> dict:
        if name not in self.KNOWN_API_KEYS:
            raise KeyError(f"unknown API key {name!r}")
        self.secrets.clear(name)
        self._model_catalog_cache = None
        self._or_catalog_cache = None
        return self.api_key_status(name)

    # back-compat wrappers (older callers/tests)
    def set_openrouter_key(self, value: str) -> dict:
        return self.set_api_key("openrouter", value)

    def clear_openrouter_key(self) -> dict:
        return self.clear_api_key("openrouter")

    def openrouter_key_status(self) -> dict:
        return self.api_key_status("openrouter")

    def update_settings(self, patch: dict) -> Settings:
        """Apply a partial settings patch, persist it, and re-derive the
        backend/role mapping/registry. Some changes (backend, role mapping)
        affect new sessions; in-flight sessions keep their own backend."""
        merged = self.settings.model_dump()
        old_role_agents = dict(self.role_agents or {})
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
        # A role remapped to a DIFFERENT seat without a fresh role_models set
        # must drop its model pin — a claude model id riding along to gemini
        # would be passed as that CLI's --model and kill the seat. The
        # dashboard sends both keys together (its UI clears the pin on seat
        # change); this guards API callers patching role_agents alone.
        if "role_agents" in (patch or {}) and "role_models" not in (patch or {}) \
                and self.settings.role_models:
            kept = {}
            for role_name, model in self.settings.role_models.items():
                try:
                    role = Role(role_name)
                except ValueError:
                    continue
                if self.role_agents.get(role) == old_role_agents.get(role):
                    kept[role_name] = model
            if kept != self.settings.role_models:
                self.settings.role_models = kept
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
        session.cli_timeouts = dict(self.settings.cli_timeouts or {})
        active = self.workspaces.active()
        session.workspace_root = active.root if active else None
        # Established folder is PER TASK: interpret a path the user referenced in
        # the prompt (a file → its parent). None ⇒ the greenfield gate may ask.
        session.established_root = extract_established_root(text or "")
        # An explicit "save it in <X>" destination (distinct from a read source
        # the task also names) — promote delivers HERE, so "read from A, save to
        # B" lands in B and never overwrites A.
        session.delivery_root = extract_delivery_target(text or "")
        for uid in attachments or []:
            rec = self.uploads.get(uid)
            if rec:
                session.attachments.append({"id": rec["id"], "name": rec["name"], "kind": rec["kind"]})
        self.store.save_session(session)
        # Sweep old scratch sandboxes so they don't pile up forever. Background so
        # it never delays starting the run; the new session is already active and
        # therefore protected.
        self._pool.submit(self._gc_sandboxes)
        return session

    def _gc_sandboxes(self, keep: Optional[int] = None) -> dict:
        """Delete old per-session sandbox scratch folders so they don't accumulate
        without bound. KEEPS: every sandbox belonging to a still-active/paused
        session (its files may still be needed to resume or promote), plus the
        `keep` most-recently-touched of the rest (so recent runs stay openable in
        the dashboard). Never touches the shared 'cli-neutral' folder or anything
        that isn't a session sandbox. Best-effort — never raises."""
        import shutil

        keep = config.SANDBOX_KEEP if keep is None else keep
        root = config.SANDBOX_ROOT
        removed = 0
        try:
            if not root.is_dir():
                return {"removed": 0}
            active = {s.get("session_id") for s in self.store.list_sessions(limit=500)
                      if s.get("status") not in ("done", "cancelled")}
            dirs = [d for d in root.iterdir() if d.is_dir() and d.name.startswith("s_")]
            dirs.sort(key=lambda d: _dir_mtime(d), reverse=True)
            kept = 0
            for d in dirs:
                if d.name in active:
                    continue  # in use — never GC (and doesn't count toward `keep`)
                kept += 1
                if kept <= keep:
                    continue
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        except OSError:
            pass
        if removed:
            self.store.log_event("-", "sandboxes_gc", {"removed": removed, "kept": keep})
        return {"removed": removed}

    def enhance_prompt(self, text: str) -> dict:
        """The Enhance button: amplify a raw prompt with the strong CODIFIER model
        (the summarizer seat, else the lead) — prompt amplification benefits from
        the stronger model. Saves a copy of the original + enhanced under
        data/enhancements/ so nothing is lost, and returns the enhanced text (the
        caller keeps the original for undo). No session is created — one call."""
        raw = (text or "").strip()
        if not raw:
            raise ValueError("nothing to enhance")
        agent, role = self.role_agents.get(Role.summarizer), Role.summarizer
        if not agent or agent not in self.registry.names():
            agent, role = self.role_agents.get(Role.lead), Role.lead
        if not agent or agent not in self.registry.names():
            raise ValueError("no model is available to enhance with")
        result = self.registry.call(agent, role,
                                    f"{AMPLIFY_PROMPT}\n\nRAW PROMPT TO AMPLIFY:\n{raw}",
                                    timeout_s=180)
        enhanced = _strip_fence(result.content or "")
        if not enhanced:
            raise RuntimeError("the lead model returned nothing")
        saved = ""
        try:
            d = Path(self._data_dir) / "enhancements"
            d.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
            p = d / f"enh_{stamp}.json"
            p.write_text(json.dumps({"ts": stamp, "agent": agent, "model": result.model,
                                     "original": raw, "enhanced": enhanced}, indent=2),
                         encoding="utf-8")
            saved = str(p)
        except OSError:
            pass  # saving a copy is best-effort; the enhancement still returns
        return {"enhanced": enhanced, "original": raw, "agent": agent,
                "model": result.model, "saved": saved}

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
                self.registry.register(CliAdapter(
                    agent=agent, model=(self.settings.cli_models or {}).get(agent),
                    role_models=self._role_pins_for(agent),
                    api_key_getter=(lambda: self.secrets.get("gemini"))
                    if agent == "gemini" else None))

    # ---- CLI seats (settings panel) ------------------------------------------

    # vendor prefix in the public catalog → our CLI seat name
    _CATALOG_VENDORS = {"anthropic": "claude", "openai": "codex", "google": "gemini"}
    # non-reasoning model families that don't belong in a council-seat dropdown
    _CATALOG_EXCLUDE = ("embed", "whisper", "tts", "dall-e", "audio", "image",
                        "moderation", "realtime", "transcribe", "veo", "imagen",
                        "aqa", "robotics", "live-translate", "computer-use")

    def cli_model_catalog(self, refresh: bool = False) -> dict[str, list[str]]:
        """Dropdown choices per local CLI seat, fetched LIVE so a model released
        yesterday appears without a code change: OpenRouter's public no-key
        /models catalog grouped by vendor (newest first), plus the gemini SDK's
        own list when a key is present (authoritative for the SDK path the
        gemini seat actually uses). Cached MODEL_CATALOG_TTL seconds; any
        failure or WEB off falls back to the static list, so Settings never
        breaks offline."""
        import time as _time

        now = _time.monotonic()
        if (not refresh and self._model_catalog_cache
                and now - self._model_catalog_cache[0] < config.MODEL_CATALOG_TTL):
            return self._model_catalog_cache[1]
        catalog = {k: list(v) for k, v in config.CLI_MODEL_CATALOG.items()}
        if config.WEB_ENABLED:
            fetched = self._fetch_public_catalog()
            for seat, models in fetched.items():
                if models:
                    # keep the tier aliases (opus/sonnet/haiku) on top — the CLI
                    # resolves them to its current best, so they never go stale
                    aliases = [m for m in catalog.get(seat, []) if "-" not in m]
                    catalog[seat] = aliases + [m for m in models if m not in aliases]
            sdk = self._gemini_sdk_models()
            if sdk:
                catalog["gemini"] = sdk
        # The claude CLI's ids use DASHES, but OpenRouter's public catalog lists
        # Anthropic models with DOTS (claude-opus-4.8) — offering those verbatim
        # made the claude seat fail. Normalize claude ids to the CLI form, curated
        # known-good ids first, deduped. (codex/gemini ids legitimately use dots,
        # so this is claude-only.)
        if "claude" in catalog:
            norm, seen = [], set()
            for m in config.CLI_MODEL_CATALOG["claude"] + catalog["claude"]:
                cli = m.replace(".", "-") if m.startswith("claude-") else m
                if cli not in seen:
                    seen.add(cli)
                    norm.append(cli)
            catalog["claude"] = norm
        self._model_catalog_cache = (now, catalog)
        return catalog

    def _fetch_catalog_raw(self) -> list:
        """The raw OpenRouter /models list (best-effort: [] on any failure).
        Shared by the CLI-seat catalog and the OpenRouter vendor catalog."""
        import httpx

        try:
            resp = httpx.get(config.MODEL_CATALOG_URL, timeout=config.MODEL_CATALOG_TIMEOUT)
            if resp.status_code != 200:
                return []
            return (resp.json() or {}).get("data") or []
        except Exception:  # noqa: BLE001 — offline/misbehaving catalog ⇒ fallback
            return []

    def openrouter_vendor_catalog(self, refresh: bool = False) -> dict:
        """Per OpenRouter SEAT, that vendor's live models with capability flags —
        {seat: [{id, name, vision, reasoning, tools, ctx}], …}, newest first.
        Powers the model dropdown for each generic vendor seat. Cached; {} of
        empty lists offline (the seat still runs on its default/custom slug)."""
        import time as _time

        now = _time.monotonic()
        if (not refresh and self._or_catalog_cache
                and now - self._or_catalog_cache[0] < config.MODEL_CATALOG_TTL):
            return self._or_catalog_cache[1]
        out: dict[str, list] = {seat: [] for seat in config.OPENROUTER_SEATS}
        if config.WEB_ENABLED:
            v2s = {spec.get("vendor"): seat for seat, spec in config.OPENROUTER_SEATS.items()}
            per: dict[str, dict[str, dict]] = {}
            for m in self._fetch_catalog_raw():
                if not isinstance(m, dict):
                    continue
                mid = str(m.get("id") or "")
                seat = v2s.get(mid.split("/", 1)[0])
                base = mid.split(":")[0]  # collapse :free/:extended routing variants
                if not seat or not base or any(x in base.lower() for x in self._CATALOG_EXCLUDE):
                    continue
                arch = m.get("architecture") or {}
                mods = arch.get("input_modalities") or []
                if isinstance(mods, str):
                    mods = mods.replace("+", ",").split(",")
                sup = [str(x).lower() for x in (m.get("supported_parameters") or [])]
                per.setdefault(seat, {}).setdefault(base, {
                    "id": base,
                    "name": str(m.get("name") or base).split(":")[0].strip(),
                    "vision": any("image" in str(x).lower() for x in mods),
                    "reasoning": ("reasoning" in sup or "include_reasoning" in sup),
                    "tools": ("tools" in sup),
                    "ctx": int(m.get("context_length") or 0),
                    "_created": float(m.get("created") or 0),
                })
            for seat, d in per.items():
                ms = sorted(d.values(), key=lambda x: (x["_created"], x["ctx"]), reverse=True)
                out[seat] = [{k: v for k, v in mm.items() if not k.startswith("_")} for mm in ms[:24]]
        self._or_catalog_cache = (now, out)
        return out

    def _fetch_public_catalog(self) -> dict[str, list[str]]:
        """Vendor → model ids from the public catalog, newest release first.
        ':free'/':extended' routing variants collapse to the base id (that is
        what the vendor CLIs accept). Best-effort: {} on any failure."""
        data = self._fetch_catalog_raw()
        if not data:
            return {}
        per: dict[str, list[tuple[float, str]]] = {}
        for m in data:
            if not isinstance(m, dict):
                continue
            vendor, _, tail = str(m.get("id") or "").partition("/")
            seat = self._CATALOG_VENDORS.get(vendor)
            tail = tail.split(":")[0]
            if not seat or not tail or any(x in tail.lower() for x in self._CATALOG_EXCLUDE):
                continue
            per.setdefault(seat, []).append((float(m.get("created") or 0), tail))
        out: dict[str, list[str]] = {}
        for seat, items in per.items():
            seen: set[str] = set()
            ordered: list[str] = []
            for _, tail in sorted(items, key=lambda t: t[0], reverse=True):
                if tail not in seen:
                    seen.add(tail)
                    ordered.append(tail)
            out[seat] = ordered[:12]
        return out

    def _gemini_sdk_models(self) -> list[str]:
        """Google's own model list via the google-genai SDK — exactly what the
        gemini seat can run, since its calls go through that SDK when a key is
        present (env var OR stored in Settings → API keys). Best-effort: []
        without a key or on any failure."""
        key = self.secrets.get("gemini")
        if not key:
            return []
        try:
            from google import genai

            client = genai.Client(api_key=key)
            names = []
            for m in client.models.list():
                tail = str(getattr(m, "name", "")).split("/")[-1]
                if tail.startswith("gemini") and not any(
                        x in tail.lower() for x in self._CATALOG_EXCLUDE):
                    names.append(tail)
            return sorted(set(names), reverse=True)[:12]
        except Exception:  # noqa: BLE001 — SDK/network trouble ⇒ public catalog wins
            return []

    def seats(self, refresh: bool = False) -> dict:
        """All seats the council can use, with availability — used to populate the
        role→agent dropdowns. CLI seats are available when on PATH; OpenRouter
        seats when enabled AND an API key is present."""
        import shutil

        catalog = self.cli_model_catalog(refresh=refresh)
        ce = self.settings.cli_enabled or {}
        cli = [
            {"name": a, "available": shutil.which(a) is not None, "kind": "cli", "label": a,
             "enabled": ce.get(a, True),
             "model": (self.settings.cli_models or {}).get(a) or None,
             "models": catalog.get(a, [])}
            for a in ("claude", "codex", "gemini")
        ]
        key_present = self.secrets.has("openrouter")
        enabled = self.settings.openrouter_enabled or {}
        or_catalog = self.openrouter_vendor_catalog(refresh=refresh)
        openrouter = [
            {"name": name, "kind": "openrouter", "label": spec["label"],
             "vendor": spec.get("vendor"),
             "model_slug": self._openrouter_slug(name),
             "default_slug": spec["model_slug"],
             "models": or_catalog.get(name, []),
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

    def continue_session(self, session_id: str, text: str, background: bool = True,
                         attachments: Optional[list[str]] = None) -> Session:
        """Continue the conversation: the human responds to the council's
        conclusion and the council deliberates AGAIN with the full thread as
        context — no starting over. Re-opens a settled (done) session.
        Responses are multi-modal like the original task: document/PDF text is
        folded into the turn, and image attachments join session.attachments so
        vision-capable agents see them on every subsequent call."""
        if not (text or "").strip() and not attachments:
            raise ValueError("response text or an attachment required")
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
        turn_text = ((text or "").strip() or "(see attached)") \
            + attachment_context(self.uploads, attachments or [])
        session.turns.append({"role": "user", "text": turn_text})
        for uid in attachments or []:
            rec = self.uploads.get(uid)
            if rec:
                session.attachments.append(
                    {"id": rec["id"], "name": rec["name"], "kind": rec["kind"]})
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
    # Live (running) states: a session here needs an active worker thread to
    # advance. After a process restart there is none, so these become orphans.
    _LIVE = {SessionStatus.received, SessionStatus.classified,
             SessionStatus.deliberating, SessionStatus.composing}

    def _reconcile_orphans(self) -> None:
        """Finalize sessions left in a live state by a process that has since
        died. Called once at startup, where there can be no surviving worker, so
        every live-state session is unambiguously orphaned. Marks each cancelled
        (thread preserved) rather than deleting, and never blocks startup."""
        try:
            metas = self.store.list_sessions()
        except Exception:  # noqa: BLE001 — a bad record must not stop the server
            return
        live = {s.value for s in self._LIVE}
        for meta in metas:
            if meta.get("status") not in live:
                continue
            sid = meta.get("session_id")
            session = self.manager.load(sid) if sid else None
            if session is None:
                continue
            session.stop_reason = "interrupted by a server restart"
            cancellation.clear(sid)
            try:
                self.manager.transition(session, SessionStatus.cancelled)
            except ValueError:
                session.status = SessionStatus.cancelled
                self.store.save_session(session)
            self.store.log_event(sid, "session_cancelled", {"from": "restart_reconcile"})

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
