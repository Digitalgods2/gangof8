"""Service wiring — one object that owns the store, manager, registry, and
governance, used by both the FastAPI app and the CLI.

Backends:
  mock — deterministic offline adapter (default; tests, Phase 0)
  cli  — Gang of 8 runs the local claude/codex/gemini CLIs itself, in plain
         generation mode → real file content; fully self-contained
"""

from __future__ import annotations

import json
import hashlib
import re
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import (
    assembly,
    browser_acceptance,
    cancellation,
    config,
    executor,
    goals,
    intake,
    reporting,
    rounds,
    smoke,
)
from .artifacts import parse_proposals
from .adapters.cli import CliAdapter
from .adapters.mock import MockAdapter
from .adapters.openrouter import OpenRouterAdapter
from .secrets import SecretStore
from .composer import fallback_final
from .governance import Governance
from .logstore import LogStore
from .loop import (
    SessionCancelled,
    _agent_call,
    resume_deliberation,
    resume_session,
    resume_with_input,
    run_session,
)
from .models import (Budgets, CouncilMember, FinalAnswer, Goal, GoalMilestone, InputRequest,
                     ProposedAction, Risk, Role, Session, SessionStatus, utcnow)
from .paths import extract_delivery_target, extract_established_root, prior_deliverable_files
from .registry import AgentError
from .registry import AgentRegistry
from .runtime_diagnostics import collect_runtime_diagnostics
from .sessions import SessionManager
from .settings import (
    Settings,
    SettingsProfile,
    apply_settings_profile,
    budgets_overrides,
    load_default_settings_profile,
    load_settings,
    make_settings_profile,
    save_settings,
)
from .uploads import UploadStore, attachment_context
from .workspaces import WorkspaceError, WorkspaceStore


# Shared browser-global namespace detection for assembled multi-file bundles.
# Roots are established either as `<x>.NS = <x>.NS || {...}` (any receiver
# alias — window, globalThis, or an IIFE's `global` parameter) or literally
# `window.NS = {...}`; modules then attach exports as `NS.Member = ...` or
# `window.NS.Member = ...`.
_ASSEMBLY_NS_ROOT_RE = re.compile(
    r"\b[\w$]+\.([A-Za-z_$][\w$]*)\s*=\s*[\w$]+\.\1\s*\|\|\s*\{"
)
_ASSEMBLY_WINDOW_ROOT_RE = re.compile(
    r"\bwindow\.([A-Za-z_$][\w$]*)\s*=\s*\{"
)


def _assembly_member_assign_re(namespace: str) -> str:
    """Pattern matching an export attachment `NS.Member =` (not `==`/`===`),
    with an optional single receiver prefix such as `window.` or `global.`."""
    return rf"\b(?:[\w$]+\.)?{re.escape(namespace)}\.([A-Za-z_$][\w$]*)\s*=(?!=)"


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


class GangOf8Service:
    def __init__(
        self,
        data_dir: Optional[Path] = None,
        backend: Optional[str] = None,
        role_agents: Optional[dict[Role, str]] = None,
        panel: Optional[list[str]] = None,
    ):
        self._data_dir = Path(data_dir) if data_dir else config.DATA_DIR
        # A normal application start (no injected data_dir) uses the bundled,
        # versioned non-secret profile when settings.json does not exist. Tests
        # and embedders with an explicit data directory retain config defaults
        # unless they explicitly load/apply a profile.
        self.settings = load_settings(
            self._data_dir, use_packaged_default=data_dir is None
        )
        self._explicit_backend = backend
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
        self.goals = goals.GoalStore(self._data_dir)
        # background workers for service mode — sessions on real backends take
        # minutes, so the dashboard submits and polls instead of blocking
        # Goal packages are intentionally independent work units.  Keep enough
        # workers for the full seven-seat roster plus planning/release overhead;
        # per-provider semaphores still enforce backend-safe call concurrency.
        self._pool = ThreadPoolExecutor(max_workers=10, thread_name_prefix="gangof8")
        self._model_catalog_cache: Optional[tuple[float, dict]] = None
        self._or_catalog_cache: Optional[tuple[float, dict]] = None
        self._apply_settings(backend=backend)
        # Crash recovery: a previous process may have died mid-run (e.g. a
        # restart), leaving sessions stuck in a live state with no worker to
        # advance or cancel them. Finalize those now so they can't linger as
        # un-cancellable "deliberating" ghosts.
        #
        # This must NOT run if another process already owns this data dir and
        # is actively serving it — e.g. a second launch (double-clicked
        # launcher, a stray `cli.py <subcommand>`) that hasn't yet failed to
        # bind the port. Without this guard, constructing a throwaway Service
        # object is enough to park a goal/session an already-running server is
        # actively working on, even though nothing actually crashed. Only a
        # real standalone start (no injected data_dir) probes the ambient
        # port — tests and embedders use isolated data dirs and must stay
        # deterministic regardless of what else happens to be running on the
        # host.
        if data_dir is not None or not self._another_instance_is_live():
            self._reconcile_orphans()
            self._reconcile_goal_orphans()

    def _another_instance_is_live(self) -> bool:
        """True if something is already listening on the dashboard port —
        a plain TCP probe, so it works the same on Windows/macOS/Linux with
        no OS-specific process APIs. The real server always binds before any
        client request can reach it, so a successful connect here means a
        live owner already exists for this data dir."""
        import os
        import socket

        port = int(os.environ.get("GANGOF8_PORT", "8790"))
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except OSError:
            return False

    def _apply_settings(self, backend: Optional[str] = None) -> None:
        """(Re)derive backend, role mapping and registry from
        the explicit args + current self.settings. Precedence for backend:
        explicit arg › settings.json › env/config default."""
        self.backend = backend or self._explicit_backend or self.settings.backend
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

        # Keep the user's declared/default mapping separate from the effective
        # mapping after disabled-seat inheritance. Per-role model pins belong
        # to the provider they were configured for; they must not follow an
        # inherited role onto a different vendor's adapter.
        self.configured_role_agents = dict(self.role_agents)

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

        # Roles owned by a disabled seat inherit across every remaining enabled
        # seat. This makes a one-model configuration real instead of quietly
        # retaining an adapter for a seat the user turned off. Explicit
        # constructor maps are trusted (tests/embedders own registration).
        if self.backend == "cli" and not self._explicit_role_agents:
            self.role_agents = self._apply_seat_disables(self.role_agents)

        self.registry = AgentRegistry()
        if self.backend == "cli":
            # A disabled OpenRouter seat is never registered merely because a
            # stale/custom role mapping references it.
            enabled = {n for n, on in (self.settings.openrouter_enabled or {}).items() if on}
            for seat in sorted(enabled):
                self._register_openrouter(seat)
            # CLI adapters for every non-OpenRouter agent in the role map,
            # pinned to the model chosen in Settings (else the CLI's default).
            # gemini also gets the key getter so a Settings-stored key (not
            # just the env var) unlocks its SDK path.
            for agent in sorted(set(self.role_agents.values())):
                if (agent not in config.OPENROUTER_SEATS
                        and (self._explicit_role_agents or self._seat_enabled(agent))):
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

    def _seat_enabled(self, seat: str) -> bool:
        """Whether a known user-toggleable seat is enabled.

        Unknown/custom adapter names stay enabled for embedder compatibility.
        Availability/authentication is separate: this enforces the user's
        switch, not whether a CLI happens to be on PATH.
        """
        if seat in ("claude", "codex", "gemini"):
            return bool((self.settings.cli_enabled or {}).get(seat, True))
        if seat in config.OPENROUTER_SEATS:
            return bool((self.settings.openrouter_enabled or {}).get(seat, False))
        return True

    def _openrouter_fallbacks(self) -> list[str]:
        """Enabled OpenRouter seats (with a resolvable slug), in a stable order —
        the pool a disabled CLI seat's roles fall back to."""
        enabled = self.settings.openrouter_enabled or {}
        return [n for n in config.OPENROUTER_SEATS
                if enabled.get(n) and self._openrouter_slug(n)]

    def _enabled_role_fallbacks(self) -> list[str]:
        """All enabled seats in stable council order for role inheritance."""
        local = [seat for seat in ("claude", "codex", "gemini")
                 if self._seat_enabled(seat)]
        return local + self._openrouter_fallbacks()

    def _apply_seat_disables(self, base: dict) -> dict:
        """Move roles from disabled seats onto the enabled roster round-robin.

        If every seat is disabled there is intentionally no invented fallback:
        disabled adapters remain unregistered and task submission fails clearly
        until the user enables at least one model.
        """
        disabled = {agent for agent in set(base.values()) if not self._seat_enabled(agent)}
        if not disabled:
            return dict(base)
        pool = self._enabled_role_fallbacks()
        if not pool:
            return dict(base)
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
            disabled_cli = self._disabled_cli_seats() if self.backend == "cli" else set()
            return [
                seat for seat in self.settings.panel_seats
                if (seat in self.registry.names()
                    and seat not in disabled_cli
                    and (seat not in config.OPENROUTER_SEATS
                         or bool((self.settings.openrouter_enabled or {}).get(seat))))
            ]
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
            if (self.role_agents.get(role) == agent
                    and self.configured_role_agents.get(role) == agent):
                out[role_name] = model.strip()
        return out

    def resolved_model(self, role: str, agent: str) -> Optional[str]:
        """The model a (role, agent) pair actually runs, by the SAME precedence
        the adapters use — role pin › seat pin › the seat's own default (None ⇒
        the CLI/vendor default). The council roster is labelled with this so a
        seat that fills two roles shows each role's real model: the claude LEAD
        runs sonnet via its role pin while the claude PANELIST runs the opus seat
        pin, and a per-agent label can't show both (it showed whichever call
        reported last, mislabelling the other)."""
        if not agent:
            return None
        pin = self._role_pins_for(agent).get(role)
        if pin:
            return pin
        if agent in config.OPENROUTER_SEATS:
            return self._openrouter_slug(agent)
        return (self.settings.cli_models or {}).get(agent) or None

    def annotate_council_models(self, data: Optional[dict]) -> Optional[dict]:
        """Enrich a serialized session's council members with the model each will
        run (resolved_model). Mutates + returns the dict. Deliberately kept OUT of
        stored session state — it's a live view of the CURRENT settings, recomputed
        per request, so re-pinning a model relabels the roster without a rerun."""
        members = ((data or {}).get("council") or {}).get("members") or []
        for m in members:
            if isinstance(m, dict) and m.get("agent") and not m.get("model"):
                m["model"] = self.resolved_model(m.get("role") or "", m["agent"])
        return data

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
        old_role_agents = dict(self.configured_role_agents or {})
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
                if self.configured_role_agents.get(role) == old_role_agents.get(role):
                    kept[role_name] = model
            if kept != self.settings.role_models:
                self.settings.role_models = kept
                save_settings(self.settings, self._data_dir)
                self._apply_settings()
        return self.settings

    def settings_profile(self) -> SettingsProfile:
        """Export the current portable settings; secrets and paths are absent."""
        return make_settings_profile(self.settings)

    def import_settings_profile(self, profile: SettingsProfile) -> Settings:
        """Atomically replace portable settings from a validated profile.

        Registry derivation is attempted before persistence.  A bad backend,
        role, risk value, or other runtime-incompatible selection therefore
        leaves both memory and settings.json on the previous known-good state.
        """
        previous = self.settings
        candidate = apply_settings_profile(previous, profile)
        self.settings = candidate
        try:
            self._apply_settings()
            save_settings(self.settings, self._data_dir)
        except Exception:
            self.settings = previous
            self._apply_settings()
            raise
        return self.settings

    def load_default_settings_profile(self) -> Settings:
        """Apply the profile shipped with this installation."""
        return self.import_settings_profile(load_default_settings_profile())

    def _open(self, text: str, source: str, budgets: Optional[Budgets],
              attachments: Optional[list[str]] = None) -> Session:
        """Create a session, stamping the backend, the active workspace root
        (so file skills operate in that project; None ⇒ per-session sandbox),
        and folding any attachment text into the task the council reads."""
        if self.backend == "cli" and not self.registry.names():
            raise ValueError(
                "no AI models are enabled; enable at least one model before starting a task")
        full_text = (text or "") + attachment_context(self.uploads, attachments or [])
        session = intake.receive(full_text, source, self.manager, budgets)
        session.backend = self.backend
        session.panel = list(self.panel)
        session.required_frontier_authors = [
            seat for seat in config.FRONTIER_AUTHOR_SEATS if seat in session.panel
        ]
        session.cli_timeouts = dict(self.settings.cli_timeouts or {})
        session.integration_review_enabled = self.settings.integration_review_enabled
        active = self.workspaces.active()
        session.workspace_root = active.root if active else None
        # Established folder is PER TASK: interpret a path the user referenced in
        # the prompt (a file → its parent). None ⇒ the greenfield gate may ask.
        session.established_root = extract_established_root(text or "")
        # An explicit "save it in <X>" destination (distinct from a read source
        # the task also names) — promote delivers HERE, so "read from A, save to
        # B" lands in B and never overwrites A.
        session.delivery_root = extract_delivery_target(text or "")
        self._preflight_panel(session)
        # If the SOURCE folder already holds a file matching this task's deliverable
        # by title, it is a prior/existing version (not an authorized input) — seats
        # can read it and a shipped copy would go unnoticed. Surface it up front.
        for name in prior_deliverable_files(session.established_root, session.task.text or ""):
            session.unresolved.append(
                f"source folder already contains '{name}', matching this task's "
                "deliverable by title — a PRIOR/existing version, not an authorized "
                "input; verify the shipped file is freshly authored, not a copy")
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
        return self._run_owned(session, self._run_full, background=False)

    def submit_background(self, text: str, source: str = "api",
                          budgets: Optional[Budgets] = None,
                          attachments: Optional[list[str]] = None) -> Session:
        """Create the session and run it on a worker thread; the caller polls
        GET /sessions/{id} for progress."""
        session = self._open(text, source, budgets, attachments)
        self._run_owned(session, self._run_full, background=True)
        return session

    def _preflight_panel(self, session: Session) -> None:
        """Remove locally unauthenticated CLI seats before panel fan-out.

        Only adapters exposing a non-generative auth status command are checked;
        custom test adapters, Gemini CLI, and remote seats remain available under
        their existing runtime handling.
        """
        healthy: list[str] = []
        for seat in session.panel:
            status = getattr(self.registry.get(seat), "auth_status", None)
            if not callable(status):
                healthy.append(seat)
                continue
            available, detail = status()
            if available is False:
                note = f"panel seat '{seat}' unavailable before run: {detail}"
                session.unresolved.append(note)
                self.store.log_event(session.session_id, "panel_seat_preflight_failed",
                                     {"agent": seat, "error": detail[:300]})
                continue
            healthy.append(seat)
        session.panel = healthy

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
$d.Description = 'Select a workspace folder for Gang of 8'
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

    def _claim_worker(self, session: Session) -> Session:
        """Atomically claim a session and reload it carrying the lease token."""
        token = self.store.claim_worker_lease(session.session_id)
        if not token:
            raise ValueError(f"session {session.session_id} is already owned or terminal")
        owned = self.manager.load(session.session_id)
        if owned is None:
            raise KeyError(f"session {session.session_id} disappeared while claiming it")
        owned.worker_lease = token
        return owned

    def _lease_current(self, session: Session) -> bool:
        return self.store.lease_is_current(session.session_id, session.worker_lease)

    def _run_owned(self, session: Session, fn, background: bool, *args) -> Session:
        """Run a session only while it owns the persisted worker lease."""
        worker = self._claim_worker(session)
        if background:
            self._pool.submit(self._safely, worker, fn, *args)
            return worker
        return self._safely(worker, fn, *args)

    def _safely(self, session: Session, fn, *args) -> Session:
        """Background guard: a session must never die silently in a thread."""
        try:
            if not self._lease_current(session):
                return session
            return fn(session, *args)
        except SessionCancelled:
            if not self._lease_current(session):
                return session
            cancellation.clear(session.session_id)
            session.stop_reason = "cancelled by user"
            session.outcome = "cancelled"
            try:
                self.manager.transition(session, SessionStatus.cancelled)
            except ValueError:
                session.status = SessionStatus.cancelled
                self.store.save_session(session)
            self.store.log_event(session.session_id, "session_cancelled", {})
            return session
        except Exception as e:  # noqa: BLE001 — last-resort containment
            if not self._lease_current(session):
                return session
            self.store.log_event(session.session_id, "internal_error", {"detail": str(e)})
            session.outcome = "failed"
            try:
                self.manager.transition(session, SessionStatus.failed)
            except ValueError:
                session.status = SessionStatus.failed
                self.store.save_session(session)
            return session
        finally:
            # goal bookkeeping sees every outcome (done/failed/cancelled/paused);
            # it never raises, so it can't clobber the return value above
            if self._lease_current(session):
                self._maybe_advance_goal(session, background=session.goal_background)
                self.store.release_worker_lease(session.session_id, session.worker_lease)

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
            if not self._seat_enabled(agent):
                # Never resurrect a disabled adapter while loading an older
                # session. The missing seat remains a visible run failure.
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

    def diagnostics(self) -> dict:
        """Redaction-safe runtime diagnostics for setup/debugging."""
        return collect_runtime_diagnostics(
            data_dir=self._data_dir,
            backend=self.backend,
            settings=self.settings,
            active_workspace=self.workspaces.active(),
            workspace_count=len(self.workspaces.list()),
            panel=self.panel,
            role_agents=self.role_agents,
            api_key_status=self.api_key_status,
            api_key_names=self.KNOWN_API_KEYS,
        )

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
        """Delete a session only after revoking/cancelling any live worker."""
        session = self.manager.load(session_id)
        if session is not None and session.status not in {
                SessionStatus.done, SessionStatus.failed, SessionStatus.cancelled}:
            cancellation.request(session_id)
            self.store.revoke_worker_lease(session_id)
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
        session.successful_agent_calls = {}
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
        return self._run_owned(session, self._run_full, background=background)

    # ---- Goals (/goal): long-horizon objectives, milestone by milestone -------

    def create_goal(self, text: str, background: bool = False) -> Goal:
        """Open a goal: the architect decomposes it into milestone-sized
        deliverables, repairing a rejected contract when necessary, then the
        ready package wave runs as normal sessions. With
        background=True the planning + first milestone run on a worker and the
        caller polls GET /goals/{id}."""
        raw = (text or "").strip()
        if raw.lower().startswith("/goal"):
            raw = raw[5:].strip()
        if not raw:
            raise ValueError("goal text is empty")
        established = extract_established_root(raw)
        delivery = extract_delivery_target(raw)
        active = self.workspaces.active()
        if not established and active:
            established = active.root
        goal = Goal(
            text=raw,
            collaboration_mode="build_team",
            delivery_mode="final_batch",
            background=background,
            build_roster=list(self.panel),
            established_root=established,
            delivery_root=delivery,
        )
        goal.staging_root = str((self._data_dir / "goal-workspaces" / goal.goal_id / "stage").resolve())
        self.goals.save(goal)
        self.store.log_event("-", "goal_created",
                             {"goal_id": goal.goal_id, "chars": len(raw)})
        if background:
            self._pool.submit(self._plan_and_start_safely, goal.goal_id)
            return goal
        return self._plan_and_start(goal.goal_id)

    def _plan_and_start_safely(self, goal_id: str) -> None:
        """Worker guard — a goal must never die silently in a thread."""
        try:
            self._plan_and_start(goal_id)
        except Exception as e:  # noqa: BLE001 — last-resort containment
            self.store.log_event("-", "goal_error", {"goal_id": goal_id, "detail": str(e)})
            goal = self.goals.claim_worker_lease(goal_id, {"planning"})
            if goal is not None:
                goal.status = "failed"
                goal.last_error = str(e)[:300]
                self.goals.save_owned(goal, goal.worker_lease)
                self.goals.release_worker_lease(goal.goal_id, goal.worker_lease)

    def _goal_planner(self) -> tuple[Optional[str], Role]:
        """The seat that authors the plan: architect › summarizer › lead —
        first one that maps to a registered adapter."""
        for role in (Role.architect, Role.summarizer, Role.lead):
            agent = self.role_agents.get(role)
            if agent and agent in self.registry.names():
                return agent, role
        return None, Role.architect

    _GOAL_STAGE_SKIP = {
        ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env",
        "__pycache__", ".mypy_cache", ".pytest_cache", "dist", "build",
        ".idea", ".vscode", ".next", "target", "vendor",
    }

    def _seed_goal_stage(self, goal: Goal) -> None:
        """Create the private overlay used by every package in this goal.

        Existing source is copied once, excluding generated/vendor trees that
        dominate startup time.  The user's project remains read-only until the
        final batch action is approved.
        """
        stage = Path(goal.staging_root).resolve()
        stage.mkdir(parents=True, exist_ok=True)
        if any(stage.iterdir()) or not goal.established_root:
            return
        source = Path(goal.established_root).resolve()
        if not source.is_dir():
            return
        if stage == source or source in stage.parents:
            raise ValueError("goal staging workspace must be outside the established project")
        copied = 0
        for path in source.rglob("*"):
            try:
                rel = path.relative_to(source)
            except ValueError:
                continue
            if any(part in self._GOAL_STAGE_SKIP for part in rel.parts):
                continue
            if path.is_symlink() or not path.is_file():
                continue
            target = stage / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(path, target)
                copied += 1
            except OSError:
                continue
        self.store.log_event("-", "goal_stage_seeded",
                             {"goal_id": goal.goal_id, "files": copied, "root": str(stage)})

    def _normalize_work_packages(
        self, milestones: list[GoalMilestone], goal_text: str = "",
        roster: Optional[list[str]] = None,
    ) -> tuple[list[GoalMilestone], list[str]]:
        """Assign owners and normalize hard versus contract dependency edges.

        ``depends_on`` is deliberately conservative: it blocks scheduling until
        verified bytes exist. ``contract_depends_on`` gives an owner the upstream
        interface immediately and never blocks its start.
        """
        seats = list(dict.fromkeys(s for s in (roster if roster is not None else self.panel) if s))
        errors: list[str] = []
        planner_named_owners = any(m.owner for m in milestones)
        for i, package in enumerate(milestones):
            package.index = i
            package.package_id = package.package_id or f"wp_{i + 1}"
            if not package.owner or (seats and package.owner not in seats):
                package.owner = seats[i % len(seats)] if seats else ""
            package.depends_on = list(dict.fromkeys(
                d for d in package.depends_on if 0 <= d < len(milestones) and d != i
            ))
            package.contract_depends_on = list(dict.fromkeys(
                d for d in package.contract_depends_on
                if (0 <= d < len(milestones) and d != i and d not in package.depends_on)
            ))
            # Older/mock planners did not know AFTER. Preserve their historical
            # sequential meaning while new package plans opt into parallelism.
            if not planner_named_owners and i > 0 and not package.depends_on:
                package.depends_on = [i - 1]

            package.assembly_mode, package.assembly_template = self._assembly_contract(package)

        # Frontier seats are valuable because they implement, not because they
        # can return later as judges. Repair a weak planner assignment by moving
        # each enabled frontier seat onto a source-producing package. Swap its
        # prior non-code owner assignment when possible so roster coverage stays
        # broad; prefer the release/integration package for the first frontier.
        code_suffixes = {
            ".html", ".htm", ".css", ".js", ".mjs", ".cjs", ".ts", ".tsx",
            ".jsx", ".py", ".go", ".rs", ".java", ".c", ".cc", ".cpp",
            ".h", ".hpp", ".cs", ".rb", ".php", ".vue", ".svelte", ".swift",
        }
        runtime_interface_suffixes = {
            ".html", ".htm", ".css", ".js", ".mjs", ".cjs", ".ts", ".tsx",
            ".jsx", ".vue", ".svelte",
        }

        # Prose-only parallel contracts are unsafe for coupled runtime files.
        # The provider may choose different method names, clock units, DOM hooks,
        # or coordinate semantics while still satisfying the same vague prose.
        # Promote those edges to hard artifact dependencies and put the actual
        # accepted files in the consumer's runtime context. CONTRACTS remains a
        # non-blocking option for genuinely descriptive/non-runtime work.
        for consumer in milestones:
            consumer_is_runtime = any(
                Path(name).suffix.lower() in runtime_interface_suffixes
                for name in consumer.required_files
            )
            if not consumer_is_runtime:
                continue
            promoted: list[int] = []
            for dependency_index in consumer.contract_depends_on:
                provider = milestones[dependency_index]
                provider_is_runtime = any(
                    Path(name).suffix.lower() in runtime_interface_suffixes
                    for name in provider.required_files
                )
                if not provider_is_runtime:
                    continue
                if dependency_index not in consumer.depends_on:
                    consumer.depends_on.append(dependency_index)
                for output in provider.required_files:
                    if output not in consumer.dependencies:
                        consumer.dependencies.append(output)
                promoted.append(dependency_index)
            if promoted:
                consumer.contract_depends_on = [
                    dependency_index
                    for dependency_index in consumer.contract_depends_on
                    if dependency_index not in promoted
                ]

        code_indices = [
            i for i, package in enumerate(milestones)
            if (not package.assembly_mode
                and any(Path(name).suffix.lower() in code_suffixes
                        for name in package.required_files))
        ]
        frontier = [seat for seat in config.FRONTIER_AUTHOR_SEATS if seat in seats]
        targets = sorted(
            code_indices,
            key=lambda i: (not bool(milestones[i].release_files), i),
        )
        for seat in frontier:
            if any(milestones[i].owner == seat for i in code_indices):
                continue
            target_i = next(
                (i for i in targets
                 if milestones[i].owner not in frontier),
                None,
            )
            if target_i is None:
                continue
            displaced = milestones[target_i].owner
            old_i = next(
                (i for i, package in enumerate(milestones)
                 if package.owner == seat and i not in code_indices),
                None,
            )
            milestones[target_i].owner = seat
            if old_i is not None and displaced:
                milestones[old_i].owner = displaced

        # Repair duplicate planner assignments deterministically so every
        # enabled AI owns one model-authored package before anyone gets a
        # second. Zero-call deterministic assembly does not count as an AI
        # contribution.
        if (len(seats) > 1 and len(milestones) >= len(seats)
                and goals.requires_delivery_contract(goal_text)):
            authored_indices = [
                index for index, package in enumerate(milestones)
                if not package.assembly_mode
            ]
            if len(authored_indices) < len(seats):
                errors.append(
                    "build plan has fewer model-authored packages than enabled AIs "
                    f"({len(authored_indices)} packages for {len(seats)} seats); "
                    "deterministic assembly does not count as participation"
                )
            counts = {
                seat: sum(milestones[index].owner == seat for index in authored_indices)
                for seat in seats
            }
            missing_owners = [seat for seat in seats if counts.get(seat, 0) == 0]
            for missing_owner in missing_owners:
                candidate_index = next(
                    (
                        index for index in reversed(authored_indices)
                        if counts.get(milestones[index].owner, 0) > 1
                    ),
                    None,
                )
                if candidate_index is None:
                    continue
                displaced = milestones[candidate_index].owner
                milestones[candidate_index].owner = missing_owner
                counts[missing_owner] = counts.get(missing_owner, 0) + 1
                counts[displaced] = counts.get(displaced, 0) - 1
            missing_owners = [seat for seat in seats if counts.get(seat, 0) == 0]
            if missing_owners:
                errors.append(
                    "build plan cannot assign every enabled AI a model-authored package: "
                    + ", ".join(missing_owners)
                )

        providers: dict[str, int] = {}
        for i, package in enumerate(milestones):
            for needed in package.dependencies:
                if needed in providers and providers[needed] not in package.depends_on:
                    package.depends_on.append(providers[needed])
            for output in package.required_files:
                if output in providers:
                    previous = providers[output]
                    if previous not in package.depends_on:
                        package.depends_on.append(previous)
                providers[output] = i

        # A physical file requirement always wins over a contract-only hint.
        # Keeping the same edge in both lists would make the API/UI claim it is
        # non-blocking even though the scheduler correctly waits for the file.
        for package in milestones:
            package.contract_depends_on = [
                d for d in package.contract_depends_on if d not in package.depends_on
            ]
            invalid_release = [name for name in package.release_files
                               if name not in package.required_files]
            if invalid_release:
                errors.append(
                    f"{package.package_id} RELEASE is not owned by OUTPUTS: "
                    + ", ".join(invalid_release)
                )
            if package.assembly_mode:
                if package.assembly_mode != assembly.HTML_INLINE:
                    errors.append(
                        f"{package.package_id} has unsupported ASSEMBLY mode: "
                        f"{package.assembly_mode}"
                    )
                    continue
                html_outputs = [
                    name for name in package.required_files
                    if Path(name).suffix.lower() in {".html", ".htm"}
                ]
                if len(package.required_files) != 1 or len(html_outputs) != 1:
                    errors.append(
                        f"{package.package_id} HTML_INLINE assembly must own exactly one HTML output"
                    )
                if not package.dependencies:
                    errors.append(
                        f"{package.package_id} HTML_INLINE assembly declares no staged sources"
                    )
                if (package.assembly_template != assembly.OWNER_TEMPLATE
                        and package.assembly_template not in package.dependencies):
                    errors.append(
                        f"{package.package_id} TEMPLATE must be OWNER or one of REQUIRES"
                    )
                inline_sources = [
                    name for name in package.dependencies
                    if name != package.assembly_template
                ]
                unsupported = [
                    name for name in inline_sources
                    if Path(name).suffix.lower() not in {".css", ".js"}
                ]
                if unsupported:
                    errors.append(
                        f"{package.package_id} HTML_INLINE has unsupported source files: "
                        + ", ".join(unsupported)
                    )
            elif package.assembly_template:
                errors.append(
                    f"{package.package_id} declares TEMPLATE without an ASSEMBLY mode"
                )

        # Concatenation cannot be the only integration stage for a broad runtime
        # graph. Require a real, non-assembly QA/integration owner after multiple
        # producers; that owner receives their accepted bytes via the hard edges
        # normalized above. This catches the old graph where seven incompatible
        # implementations flowed straight into a zero-call HTML assembler.
        integration_markers = ("integration", "integrate", "acceptance", "qa", "quality", "verify")
        for release in milestones:
            if self._assembly_contract(release)[0] != assembly.HTML_INLINE:
                continue
            runtime_upstream = [
                dependency_index for dependency_index in release.depends_on
                if any(
                    Path(name).suffix.lower() in runtime_interface_suffixes
                    for name in milestones[dependency_index].required_files
                )
            ]
            if len(runtime_upstream) < 2:
                continue
            integrators = []
            for dependency_index in runtime_upstream:
                candidate = milestones[dependency_index]
                label = f"{candidate.title} {candidate.task_text}".lower()
                if (not candidate.assembly_mode
                        and len(candidate.depends_on) >= 2
                        and any(marker in label for marker in integration_markers)):
                    integrators.append(candidate)
            if not integrators:
                errors.append(
                    f"{release.package_id} assembly follows multiple runtime producers "
                    "without a hard-after non-assembly integration/QA package"
                )

        # Backward-compatible deterministic inference for a planner that predates
        # RELEASE.  Only sink-package outputs are candidates; for an explicitly
        # single-file HTML goal, prefer its one final HTML artifact and keep build
        # scripts/source modules private.  New planner prompts declare this
        # explicitly, so inference is a safety net rather than the normal path.
        if milestones and not any(p.release_declared for p in milestones):
            consumed = {d for package in milestones for d in package.depends_on}
            sinks = [p for p in milestones if p.index not in consumed]
            sink = sinks[-1] if sinks else milestones[-1]
            candidates = list(sink.required_files)
            text = (goal_text or "").lower()
            single_file = any(phrase in text for phrase in (
                "single-file", "single file", "one file", "one html file",
            ))
            html = [name for name in candidates
                    if Path(name).suffix.lower() in (".html", ".htm")]
            sink.release_files = html if single_file and len(html) == 1 else candidates
            sink.release_declared = True

        if (goal_text and goals.requires_delivery_contract(goal_text)
                and any(p.release_declared for p in milestones)
                and not any(p.release_files for p in milestones)):
            errors.append("build plan declares no final RELEASE files")

        visiting: set[int] = set()
        visited: set[int] = set()

        def visit(i: int) -> None:
            if i in visiting:
                errors.append("work-package dependency graph contains a cycle")
                return
            if i in visited:
                return
            visiting.add(i)
            for dependency in milestones[i].depends_on:
                visit(dependency)
            visiting.remove(i)
            visited.add(i)

        for i in range(len(milestones)):
            visit(i)
        return milestones, list(dict.fromkeys(errors))

    @staticmethod
    def _assembly_contract(package: GoalMilestone) -> tuple[str, str]:
        """Normalize explicit assembly metadata and backfill pre-contract plans."""
        mode = assembly.normalize_mode(package.assembly_mode)
        template = assembly.normalize_template(package.assembly_template)
        if not mode and assembly.infer_html_inline(
                package.required_files, package.dependencies,
                package.release_files, package.task_text):
            mode = assembly.HTML_INLINE
            template = assembly.OWNER_TEMPLATE
        if mode == assembly.HTML_INLINE and not template:
            template = assembly.OWNER_TEMPLATE
        return mode, template

    @staticmethod
    def _package_ready(goal: Goal, index: int) -> bool:
        package = goal.milestones[index]
        return (package.status == "pending"
                and all(goal.milestones[d].status == "done" for d in package.depends_on))

    def _start_ready_packages(self, goal: Goal, background: bool) -> None:
        """Schedule every hard-dependency-ready package; binding is duplicate-safe.

        Contract-only edges are intentionally absent from ``_package_ready``:
        their declared interface is enough for parallel authoring.
        """
        current = self.goals.get(goal.goal_id) or goal
        ready = [i for i in range(len(current.milestones)) if self._package_ready(current, i)]
        if ready:
            self.store.log_event(
                "-", "goal_package_wave_started",
                {
                    "goal_id": current.goal_id,
                    "packages": [current.milestones[i].package_id for i in ready],
                    "owners": [current.milestones[i].owner for i in ready],
                },
            )
        for index in ready:
            latest = self.goals.get(goal.goal_id)
            if latest is None or latest.status != "running":
                return
            self._start_milestone(latest, index, background=background)

    # ---- Goal v2: transactional planner/advance ownership -------------------

    def _plan_and_start(self, goal_id: str) -> Goal:
        """Plan only while owning the goal, then bind milestone 1 atomically."""
        goal = self.goals.claim_worker_lease(goal_id, {"planning"})
        if goal is None:
            existing = self.goals.get(goal_id)
            if existing is not None:
                return existing
            raise KeyError(f"goal {goal_id} not found")
        token = goal.worker_lease
        start_epoch: Optional[int] = None
        try:
            agent, role = self._goal_planner()
            milestones: list[GoalMilestone] = []
            rationale = ""
            validation_errors: list[str] = []
            call_error = ""
            repair_count = 0
            if agent:
                prompt = goals.plan_prompt(goal.text, goal.build_roster or self.panel)
                rejected_plan = ""
                for attempt in range(config.GOAL_PLAN_REPAIR_ATTEMPTS + 1):
                    # A model call can outlive a concurrent Cancel. Do not spend
                    # another repair call or save stale state after ownership was
                    # revoked while the adapter was blocked.
                    persisted = self.goals.get(goal.goal_id)
                    if (persisted is None or persisted.status != "planning"
                            or persisted.worker_lease != token
                            or persisted.epoch != goal.epoch):
                        return persisted or goal
                    try:
                        result = self.registry.call(
                            agent, role, prompt, timeout_s=config.GOAL_PLAN_TIMEOUT)
                    except Exception as e:  # noqa: BLE001
                        label = "plan repair call" if repair_count else "planning call"
                        call_error = f"{label} failed ({str(e)[:200]})"
                        break

                    persisted = self.goals.get(goal.goal_id)
                    if (persisted is None or persisted.status != "planning"
                            or persisted.worker_lease != token
                            or persisted.epoch != goal.epoch):
                        return persisted or goal

                    goal.planned_by = agent
                    rejected_plan = result.content or ""
                    candidate = goals.parse_milestones(rejected_plan)
                    candidate_errors: list[str] = []
                    if not candidate:
                        if goals.requires_delivery_contract(goal.text):
                            candidate_errors.append(
                                "planner did not produce a delivery contract")
                    else:
                        invalid = [
                            package for package in candidate
                            if (not package.contract_declared or package.contract_error
                                or (package.requires_delivery
                                    and not package.required_files))
                        ]
                        for package in invalid:
                            reasons: list[str] = []
                            if not package.contract_declared:
                                reasons.append("missing OUTPUTS declaration")
                            if package.contract_error:
                                reasons.append(package.contract_error)
                            if package.requires_delivery and not package.required_files:
                                reasons.append("delivery contract has no output files")
                            identity = package.package_id or package.title
                            candidate_errors.append(
                                f"{identity} has an incomplete delivery contract: "
                                + "; ".join(reasons)
                            )
                        if not candidate_errors:
                            candidate, candidate_errors = self._normalize_work_packages(
                                candidate, goal.text,
                                roster=goal.build_roster or self.panel,
                            )

                    if not candidate_errors:
                        milestones = candidate
                        if repair_count:
                            rationale = (
                                "planner contract repaired automatically after "
                                f"{repair_count} rejected attempt"
                                f"{'s' if repair_count != 1 else ''}"
                            )
                            self.store.log_event(
                                "-", "goal_plan_repaired",
                                {"goal_id": goal.goal_id, "attempts": repair_count},
                            )
                        break

                    validation_errors = candidate_errors
                    if attempt >= config.GOAL_PLAN_REPAIR_ATTEMPTS:
                        break
                    repair_count += 1
                    self.store.log_event(
                        "-", "goal_plan_repair_requested",
                        {
                            "goal_id": goal.goal_id,
                            "attempt": repair_count,
                            "errors": validation_errors,
                        },
                    )
                    prompt = goals.plan_repair_prompt(
                        goal.text,
                        goal.build_roster or self.panel,
                        rejected_plan,
                        validation_errors,
                        repair_count,
                    )
            if not milestones and validation_errors:
                goal.status = "paused"
                detail = "; ".join(validation_errors)
                if call_error:
                    detail = f"{call_error}; prior plan errors: {detail}"
                goal.last_error = detail[:300]
                goal.plan_rationale = detail
                self.goals.save_owned(goal, token)
                return self.goals.get(goal.goal_id) or goal
            if not milestones:
                if goals.requires_delivery_contract(goal.text):
                    goal.status = "paused"
                    goal.last_error = "planner did not produce a delivery contract"
                    goal.plan_rationale = call_error or rationale or goal.last_error
                    self.goals.save_owned(goal, token)
                    return self.goals.get(goal.goal_id) or goal
                milestones = [GoalMilestone(
                    index=0, title=goal.text[:80], task_text=goal.text,
                    contract_declared=True, requires_delivery=False,
                )]
                rationale = (rationale or "plan was not parseable") + " - analysis-only milestone"
            goal.milestones = milestones
            self._seed_goal_stage(goal)
            goal.plan_rationale = rationale
            goal.current_index = 0
            goal.status = "running"
            goal.epoch += 1
            if not self.goals.save_owned(goal, token):
                return self.goals.get(goal.goal_id) or goal
            start_epoch = goal.epoch
            self.store.log_event("-", "goal_planned",
                                 {"goal_id": goal.goal_id, "milestones": len(milestones),
                                  "planned_by": goal.planned_by, "epoch": goal.epoch})
        finally:
            self.goals.release_worker_lease(goal.goal_id, token)
        current = self.goals.get(goal.goal_id)
        if current and current.status == "running" and current.epoch == start_epoch:
            self._start_ready_packages(current, background=current.background)
        return self.goals.get(goal.goal_id) or goal

    def _start_milestone(self, goal: Goal, index: int, background: bool) -> Optional[Session]:
        """Create a session, then atomically bind it to one live goal epoch."""
        current = self.goals.get(goal.goal_id)
        if (current is None or current.status != "running" or current.epoch != goal.epoch
                or not (0 <= index < len(current.milestones))):
            return None
        if (current.delivery_mode == "final_batch" and not self._package_ready(current, index)):
            return None
        if (current.delivery_mode != "final_batch"
                and (current.current_index != index or current.current is None)):
            return None
        session = self._open(goals.compose_milestone_task(current, index), "goal", None, None)
        bound = self.goals.bind_milestone(
            current.goal_id, index, current.epoch, session.session_id)
        if bound is None:
            self.store.delete_session(session.session_id)
            return None
        milestone = bound.milestones[index]
        prior_hashes: dict[str, str] = {}
        for prior in bound.milestones:
            if prior.status == "done":
                prior_hashes.update(prior.accepted_hashes)
        session.goal_id = bound.goal_id
        session.goal_milestone = index
        session.goal_epoch = bound.epoch
        session.goal_background = bound.background
        session.collaboration_mode = bound.collaboration_mode
        session.delivery_mode = bound.delivery_mode
        session.work_package_id = milestone.package_id
        session.work_package_owner = milestone.owner
        session.package_helpers = [
            seat for seat in session.panel
            if seat and seat != milestone.owner
        ]
        session.assembly_mode, session.assembly_template = self._assembly_contract(milestone)
        session.required_frontier_authors = (
            [milestone.owner]
            if (not session.assembly_mode and milestone.owner in config.FRONTIER_AUTHOR_SEATS)
            else []
        )
        if bound.delivery_mode == "final_batch":
            session.workspace_root = bound.staging_root
            session.established_root = bound.established_root
            session.delivery_root = bound.delivery_root
            if milestone.owner:
                session.panel = [milestone.owner]
        session.required_files = list(milestone.required_files)
        ready_contract_files: list[str] = []
        pending_contract_files: list[str] = []
        for dependency_index in milestone.contract_depends_on:
            dependency = bound.milestones[dependency_index]
            target = (ready_contract_files if dependency.status == "done"
                      else pending_contract_files)
            target.extend(
                name for name in dependency.required_files
                if Path(name).suffix.lower() in (".js", ".mjs")
            )
        session.runtime_dependencies = list(dict.fromkeys(
            list(milestone.dependencies) + ready_contract_files
        ))
        session.deferred_runtime_dependencies = list(dict.fromkeys(pending_contract_files))
        # A same-path dependency is an in-place revision target, not an
        # immutable predecessor.  The old hash gate compared the edited file to
        # its own pre-edit hash and made every correct revision fail forever.
        mutable = {name.replace("\\", "/") for name in session.required_files}
        session.revision_targets = [
            name.replace("\\", "/") for name in session.runtime_dependencies
            if name.replace("\\", "/") in mutable
        ]
        session.dependency_hashes = {
            name: prior_hashes[name] for name in session.runtime_dependencies
            if name in prior_hashes and name.replace("\\", "/") not in mutable
        }
        session.revision_base_hashes = {
            name: prior_hashes[name] for name in session.revision_targets if name in prior_hashes
        }
        session.acceptance_commands = list(milestone.acceptance_commands)
        self.store.save_session(session)
        self.store.log_event(session.session_id, "goal_milestone_started",
                             {"goal_id": bound.goal_id, "milestone": index + 1,
                              "of": len(bound.milestones), "title": milestone.title,
                              "epoch": bound.epoch})
        return self._run_owned(session, self._run_full, background=background)

    @staticmethod
    def _goal_delivery_manifest(
        session: Session, required: list[str]
    ) -> tuple[list[str], list[str], dict[str, str]]:
        """Exact-path, hash-bearing manifest of artifacts actually promoted."""
        promoted = {
            a.filename.replace("\\", "/"): a.result_path
            for a in session.proposed_actions
            if a.kind == "promote" and a.status == "executed" and a.result_path
        }
        missing = [name for name in required if name not in promoted]
        accepted = [promoted[name] for name in required if name in promoted]
        if not required:
            accepted = list(dict.fromkeys(promoted.values()))
        hashes: dict[str, str] = {}
        for name, path in promoted.items():
            try:
                hashes[name] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            except OSError:
                if name in required:
                    missing.append(name)
        return accepted, list(dict.fromkeys(missing)), hashes

    @staticmethod
    def _goal_stage_manifest(
        session: Session, required: list[str], staging_root: str,
    ) -> tuple[list[str], list[str], dict[str, str]]:
        """Copy only verified package outputs into the shared goal overlay."""
        stage = Path(staging_root)
        stage.mkdir(parents=True, exist_ok=True)
        latest: dict[str, Path] = {}
        for action in session.proposed_actions:
            name = action.filename.replace("\\", "/")
            if (action.role != Role.panelist and action.kind in ("write_file", "edit_file")
                    and action.status == "executed" and action.result_path):
                latest[name] = Path(action.result_path)
        missing: list[str] = []
        accepted: list[str] = []
        hashes: dict[str, str] = {}
        for name in required:
            source = latest.get(name.replace("\\", "/"))
            if source is None or not source.is_file():
                missing.append(name)
                continue
            try:
                target = executor.resolve_in_workspace(stage, name)
                target.parent.mkdir(parents=True, exist_ok=True)
                if source.resolve() != target.resolve():
                    shutil.copy2(source, target)
                digest = hashlib.sha256(target.read_bytes()).hexdigest()
            except (OSError, executor.ExecutionError):
                missing.append(name)
                continue
            accepted.append(str(target))
            hashes[name.replace("\\", "/")] = digest
        return accepted, list(dict.fromkeys(missing)), hashes

    @staticmethod
    def _accepted_output_provenance(
        session: Session, required: list[str], hashes: dict[str, str],
    ) -> dict[str, dict]:
        """Bind accepted hashes to the real author or deterministic transform."""
        records: dict[str, dict] = {}
        assembly_result = dict(session.assembly_result or {})
        deterministic = bool(assembly_result)
        model_calls = int(assembly_result.get("model_calls") or 0)
        for raw_name in required:
            name = raw_name.replace("\\", "/")
            history = session.package_output_history.get(name) or []
            authored = next(
                (entry for entry in reversed(history)
                 if entry.get("status") == "completed" and entry.get("agent")),
                None,
            )
            if deterministic:
                record = {
                    "sha256": hashes.get(name, ""),
                    "session_id": session.session_id,
                    "method": (
                        "model_template+deterministic_assembly"
                        if model_calls else "deterministic_assembly"
                    ),
                    "agent": authored.get("agent") if model_calls and authored else None,
                    "template_hash": assembly_result.get("template_hash", ""),
                    "source_hashes": dict(assembly_result.get("source_hashes") or {}),
                }
            else:
                record = {
                    "sha256": hashes.get(name, ""),
                    "session_id": session.session_id,
                    "method": "model_authored",
                    "agent": authored.get("agent") if authored else None,
                }
            records[name] = record
        return records

    def _goal_acceptance(self, session: Session, milestone: GoalMilestone) -> tuple[bool, list[str], str]:
        if session.outcome != "succeeded":
            detail = session.stop_reason or session.outcome or "session did not succeed"
            return False, [], f"milestone execution did not succeed: {detail}"
        if not milestone.contract_declared:
            return False, [], "planner omitted the required OUTPUTS contract"
        if milestone.contract_error:
            return False, [], "invalid delivery contract: " + milestone.contract_error
        if milestone.requires_delivery and not milestone.required_files:
            return False, [], "delivery contract declares no required files"
        goal = self.goals.get(session.goal_id) if session.goal_id else None
        if goal and goal.delivery_mode == "final_batch":
            accepted, missing, _ = self._goal_stage_manifest(
                session, milestone.required_files, goal.staging_root)
        else:
            accepted, missing, _ = self._goal_delivery_manifest(session, milestone.required_files)
        if missing:
            return False, accepted, "required delivery missing: " + ", ".join(missing)
        return True, accepted, "acceptance passed"

    @staticmethod
    def _goal_release_files(goal: Goal) -> list[str]:
        """Return only the explicit user-facing manifest, never all staging.

        Package ``required_files`` are internal build inputs.  Treating every one
        as a deliverable leaked source trees and test harnesses into goals that
        requested one final artifact.
        """
        files: list[str] = []
        for package in goal.milestones:
            for name in package.release_files:
                normalized = name.replace("\\", "/")
                if normalized not in files:
                    files.append(normalized)
        return files

    @staticmethod
    def _release_baselines(files: list[str], destination: str) -> dict[str, Optional[str]]:
        root = Path(destination)
        out: dict[str, Optional[str]] = {}
        for name in files:
            try:
                path = executor.resolve_in_workspace(root, name)
                out[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            except (OSError, executor.ExecutionError):
                out[name] = None
        return out

    def _authorize_goal_release(self, session: Session) -> Session:
        """Create exactly one approval-bearing action for the complete manifest."""
        destination = session.delivery_root or session.established_root
        if not destination:
            raise ValueError("final delivery folder has not been selected")
        files = list(session.required_files)
        baselines = self._release_baselines(files, destination)
        stage = Path(session.workspace_root or "")
        source_hashes = dict(session.release_verified_hashes)
        missing_seals = [name for name in files if not source_hashes.get(name)]
        if missing_seals:
            raise ValueError(
                "cannot authorize release without final verified hashes: "
                + ", ".join(missing_seals)
            )
        for name in files:
            try:
                path = executor.resolve_in_workspace(stage, name)
                if not path.is_file():
                    raise OSError("staged file is missing")
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
                if actual != source_hashes[name]:
                    raise OSError("staged bytes changed after final verification")
            except (OSError, executor.ExecutionError) as exc:
                raise ValueError(
                    f"cannot authorize an unhashable staged release file {name}: {exc}"
                ) from exc
        action = ProposedAction(
            session_id=session.session_id,
            kind="promote_batch",
            role=Role.implementer,
            filename=f"final batch ({len(files)} files)",
            args={
                "files": json.dumps(files),
                "baselines": json.dumps(baselines),
                "source_hashes": json.dumps(source_hashes),
            },
        )
        session.proposed_actions = [action]
        approval = self.governance.authorize_action(session, action)
        if approval is None:
            raise RuntimeError("final batch unexpectedly bypassed its approval gate")
        action.approval_id = approval.approval_id
        action.status = "awaiting_approval"
        if session.status == SessionStatus.awaiting_input:
            self.manager.transition(session, SessionStatus.deliberating)
        self.manager.transition(session, SessionStatus.awaiting_approval)
        session.stop_reason = "one final batch approval needed"
        self.store.save_session(session)
        return session

    def _verify_goal_release(self, goal: Goal, session: Session) -> bool:
        """Verify an assembled batch deterministically or by an independent frontier."""
        session.release_verified_hashes = {}
        release_packages = [package for package in goal.milestones if package.release_files]
        stage = Path(goal.staging_root)
        deterministic_release = bool(release_packages) and all(
            self._assembly_contract(package)[0] == assembly.HTML_INLINE
            for package in release_packages
        )
        deterministic_preflight: dict = {}
        if deterministic_release:
            failures: list[str] = []
            verified_hashes: dict[str, str] = {}
            expected = {
                name: package.accepted_hashes.get(name, "")
                for package in release_packages for name in package.release_files
            }
            for name in session.required_files:
                try:
                    path = executor.resolve_in_workspace(stage, name)
                    raw = path.read_bytes()
                except (OSError, executor.ExecutionError) as exc:
                    failures.append(f"{name}: unavailable during deterministic release verification ({exc})")
                    continue
                digest = hashlib.sha256(raw).hexdigest()
                verified_hashes[name] = digest
                if not expected.get(name) or digest != expected[name]:
                    failures.append(f"{name}: staged bytes no longer match the accepted assembly output")
                    continue
                content = raw.decode("utf-8", errors="replace")
                ran, testable, detail, _dynamic = smoke.smoke_source(
                    content, Path(name).suffix or ".txt"
                )
                if testable and not ran:
                    failures.append(f"{name}: {detail}")
            deterministic_preflight = {
                "verdict": "FAIL" if failures else "PASS",
                "stage": "deterministic_assembly_release",
                "verifier": "coordinator",
                "files": list(session.required_files),
                "hashes": verified_hashes,
                "remaining_defects": failures,
            }
            session.quality_gate = dict(deterministic_preflight)
            self.store.log_event(
                session.session_id, "deterministic_release_verified",
                {"verdict": session.quality_gate["verdict"],
                 "files": list(session.required_files), "failures": failures},
            )
            if failures:
                session.unresolved.extend(failures)
                session.stop_reason = "deterministic final-batch verification failed"
                session.outcome = "failed_verification"
                self.manager.transition(session, SessionStatus.composing)
                session.final = FinalAnswer(
                    answer="The deterministic assembly changed or failed its runtime check and was not released.",
                    confidence="low", risks_unresolved=list(session.unresolved),
                    next_action="Restore the accepted staged inputs and resume the goal.",
                )
                self.manager.transition(session, SessionStatus.failed)
                self.store.save_session(session)
                return False
            # Integrity is necessary, but it is not semantic acceptance. Continue
            # into the independent checklist below; deterministic concatenation
            # must never bypass product-quality review again.

        def run_browser_acceptance() -> tuple[list[dict], list[str]]:
            evidence: list[dict] = []
            failures: list[str] = []
            for name in session.required_files:
                if Path(name).suffix.lower() not in {".html", ".htm"}:
                    continue
                try:
                    path = executor.resolve_in_workspace(stage, name)
                    result = browser_acceptance.browser_acceptance(path)
                except (OSError, executor.ExecutionError) as exc:
                    failures.append(f"{name}: browser acceptance could not start ({exc})")
                    continue
                record = {
                    "file": name,
                    "passed": result.passed,
                    "interactive": result.interactive,
                    "testable": result.testable,
                    "detail": result.detail,
                    "browser": result.browser,
                    "errors": list(result.errors),
                }
                evidence.append(record)
                if result.interactive and not result.passed:
                    failures.append(f"{name}: {result.detail}")
            return evidence, failures

        browser_evidence, browser_failures = run_browser_acceptance()
        self.store.log_event(
            session.session_id,
            "browser_release_verified",
            {
                "verdict": "FAIL" if browser_failures else "PASS",
                "files": len(browser_evidence),
                "failures": browser_failures,
            },
        )

        def seal_verified_hashes() -> dict[str, str]:
            sealed: dict[str, str] = {}
            for name in session.required_files:
                path = executor.resolve_in_workspace(stage, name)
                if not path.is_file():
                    raise OSError(f"verified release file disappeared: {name}")
                sealed[name] = hashlib.sha256(path.read_bytes()).hexdigest()
            session.release_verified_hashes = sealed
            session.quality_gate["verified_hashes"] = dict(sealed)
            return sealed

        enabled_frontier = [
            seat for seat in config.FRONTIER_AUTHOR_SEATS
            if seat in self.panel and seat in self.registry.names()
        ]
        if not enabled_frontier:
            if not deterministic_release and not browser_failures:
                session.quality_gate = {
                    "verdict": "SKIPPED", "detail": "no frontier seat is enabled",
                    "browser_acceptance": browser_evidence,
                }
                try:
                    seal_verified_hashes()
                except (OSError, executor.ExecutionError) as exc:
                    browser_failures = [f"could not seal verified release bytes: {exc}"]
                else:
                    self.store.save_session(session)
                    return True
            detail = (
                "; ".join(browser_failures)
                if browser_failures else
                "deterministic assembly passed integrity checks, but no independent "
                "frontier seat is enabled for semantic acceptance"
            )
            session.quality_gate = {
                "verdict": "FAIL", "detail": detail,
                "deterministic_preflight": deterministic_preflight,
                "browser_acceptance": browser_evidence,
            }
            session.unresolved.append(detail)
            session.stop_reason = "semantic final-batch verification unavailable"
            session.outcome = "failed_verification"
            self.manager.transition(session, SessionStatus.composing)
            session.final = FinalAnswer(
                answer="The assembled batch passed integrity checks but was not released without semantic review.",
                confidence="low", risks_unresolved=list(session.unresolved),
                next_action="Enable an independent frontier seat and resume the goal.",
            )
            self.manager.transition(session, SessionStatus.failed)
            self.store.save_session(session)
            return False
        release_owners = {
            package.owner for package in goal.milestones if package.release_files
        }
        verifier_name = next(
            (seat for seat in enabled_frontier if seat not in release_owners),
            None,
        )
        if verifier_name is None:
            detail = (
                "no independent frontier release engineer is available after "
                f"excluding release owner(s): {', '.join(sorted(release_owners)) or 'none'}"
            )
            session.quality_gate = {"verdict": "FAIL", "detail": detail}
            session.unresolved.append(detail)
            session.stop_reason = "frontier final-batch verification failed"
            session.outcome = "failed_verification"
            self.manager.transition(session, SessionStatus.composing)
            session.final = FinalAnswer(
                answer="The assembled final batch was not released because independent frontier verification was unavailable.",
                confidence="low", risks_unresolved=list(session.unresolved),
                next_action="Restore a second frontier seat and resume the goal.",
            )
            self.manager.transition(session, SessionStatus.failed)
            self.store.save_session(session)
            return False

        member = CouncilMember(role=Role.panelist, agent=verifier_name, active=True)
        def read_files() -> list[tuple[str, str]]:
            loaded: list[tuple[str, str]] = []
            for name in session.required_files:
                path = executor.resolve_in_workspace(stage, name)
                loaded.append((name, path.read_text(encoding="utf-8", errors="replace")))
            return loaded

        files = read_files()
        total_edits = 0
        for attempt in range(max(1, config.FRONTIER_VERIFY_ATTEMPTS)):
            if attempt:
                browser_evidence, browser_failures = run_browser_acceptance()
            release_defect_register = list(dict.fromkeys(
                [*goal.release_defects, *browser_failures]
            ))
            prompt = rounds.frontier_release_prompt(
                session, files, defect_register=release_defect_register,
                repair_attempt=attempt)
            try:
                answer = _agent_call(
                    session, self.registry, self.store, member, prompt,
                    timeout_s=config.FRONTIER_VERIFY_TIMEOUT,
                )
            except Exception as e:  # normalized below into a release failure
                # Do not absorb explicit cancellation; the goal cancellation path
                # owns that state transition.
                if isinstance(e, SessionCancelled):
                    raise
                session.quality_gate = {
                    "verifier": verifier_name, "verdict": "FAIL", "detail": str(e),
                }
                break
            verdict, checks, defects = rounds.parse_frontier_verdict(answer.content)
            edits = [action for action in parse_proposals(
                session.session_id, answer.content, Role.implementer)
                if action.kind == "edit_file"
                and action.filename in session.required_files
            ]
            # A verifier cannot truthfully PASS the current bytes while also
            # supplying edits they require. Apply those edits first, then demand
            # the normal clean-room confirmation pass.
            if verdict == "PASS" and edits:
                verdict = "FAIL"
                defects.append(
                    "frontier verifier supplied implementation repairs that must "
                    "be applied and confirmed before PASS"
                )
            if browser_failures:
                verdict = "FAIL"
                defects = list(dict.fromkeys(browser_failures + defects))
            expected = {
                f"R{i}" for i in range(
                    1, len(rounds.acceptance_requirements(session.task.text)) + 1)
            }
            checked = {item.get("id") for item in checks}
            missing_checks = sorted(expected - checked)
            if missing_checks:
                verdict = "FAIL"
                defects.append("missing acceptance checks: " + ", ".join(missing_checks))
            session.quality_gate = {
                "verifier": verifier_name,
                "verdict": verdict,
                "checks": checks,
                "remaining_defects": defects,
                "missing_checks": missing_checks,
                "attempt": attempt + 1,
                "repairs_applied": total_edits,
                "browser_acceptance": browser_evidence,
            }
            if deterministic_preflight:
                session.quality_gate["deterministic_preflight"] = deterministic_preflight
            self.store.log_event(
                session.session_id, "frontier_final_batch_verdict",
                {"agent": verifier_name, "verdict": verdict,
                 "checks": len(checks), "defects": len(defects),
                 "attempt": attempt + 1},
            )
            if verdict == "PASS":
                if deterministic_release and total_edits:
                    # A frontier repair changes the assembled bytes. Persist the
                    # new accepted hash explicitly so release provenance remains
                    # truthful instead of silently relying on the pre-repair hash.
                    for package in release_packages:
                        for name in package.release_files:
                            try:
                                path = executor.resolve_in_workspace(stage, name)
                                package.accepted_hashes[name] = hashlib.sha256(
                                    path.read_bytes()
                                ).hexdigest()
                                package.output_provenance[name] = {
                                    "sha256": package.accepted_hashes[name],
                                    "session_id": session.session_id,
                                    "method": "frontier_release_repair",
                                    "agent": verifier_name,
                                    "source_hashes": dict(
                                        deterministic_preflight.get("hashes") or {}
                                    ),
                                }
                            except (OSError, executor.ExecutionError):
                                pass
                try:
                    seal_verified_hashes()
                except (OSError, executor.ExecutionError) as exc:
                    session.quality_gate["verdict"] = "FAIL"
                    session.quality_gate["detail"] = (
                        f"could not seal verified release bytes: {exc}"
                    )
                    break
                session.outcome = "succeeded"
                goal.release_defects = []
                # The release is the only proof an assembly fault is truly
                # resolved; the streak intentionally persists across provider
                # rebuilds (see _maybe_advance_goal) and resets only here.
                goal.assembly_fault_streak = {}
                self.store.save_session(session)
                return True
            if attempt + 1 >= config.FRONTIER_VERIFY_ATTEMPTS:
                break
            applied = 0
            for action in edits:
                action.args["target"] = "workspace"
                try:
                    path = executor.execute(session, action, self._data_dir)
                except executor.ExecutionError as e:
                    action.status = "failed"
                    action.error = str(e)
                else:
                    action.status = "executed"
                    action.result_path = path
                    applied += 1
                session.proposed_actions.append(action)
            if not applied:
                session.quality_gate["detail"] = (
                    "verifier rejected the batch without a usable implementation repair"
                )
                break
            files = read_files()
            runtime_failures = []
            for name, content in files:
                ran, testable, detail, _dynamic = smoke.smoke_source(
                    content, Path(name).suffix or ".txt")
                if testable and not ran:
                    runtime_failures.append(f"{name}: {detail}")
            if runtime_failures:
                session.quality_gate["detail"] = "frontier repair failed runtime: " + "; ".join(runtime_failures)
                break
            total_edits += applied
            self.store.log_event(
                session.session_id, "frontier_final_batch_repair_applied",
                {"agent": verifier_name, "edits": applied},
            )

        detail = session.quality_gate.get("detail") or (
            "; ".join(session.quality_gate.get("remaining_defects") or [])
            or "semantic acceptance did not pass"
        )
        retry_defects = list(session.quality_gate.get("remaining_defects") or [])
        if detail and detail not in retry_defects:
            retry_defects.append(detail)
        goal.release_defects = list(dict.fromkeys(
            [*goal.release_defects, *retry_defects]
        ))
        session.unresolved.append(f"frontier final-batch verifier rejected release: {detail}")
        session.stop_reason = "frontier final-batch verification failed"
        session.outcome = "failed_verification"
        self.manager.transition(session, SessionStatus.composing)
        session.final = FinalAnswer(
            answer="The assembled final batch failed independent frontier verification and was not offered for approval.",
            confidence="low", risks_unresolved=list(session.unresolved),
            next_action="Resume the goal so the frontier release engineer can repair and re-check it.",
        )
        self.manager.transition(session, SessionStatus.failed)
        self.store.save_session(session)
        return False

    def _prepare_goal_release(self, goal: Goal) -> None:
        """Create the final review session after every package has staged cleanly."""
        files = self._goal_release_files(goal)
        goal.release_files = files
        if not files:
            if goals.requires_delivery_contract(goal.text):
                goal.status = "paused"
                goal.release_status = "failed"
                goal.last_error = "final release has no verified output files"
                self.store.log_event(
                    "-", "goal_release_blocked",
                    {"goal_id": goal.goal_id, "reason": goal.last_error},
                )
                return
            goal.status = "completed"
            goal.release_status = "released"
            self.store.log_event("-", "goal_completed", {"goal_id": goal.goal_id})
            return
        session = self._open(
            f"[FINAL BATCH RELEASE] {goal.text}\nReview and release all staged package outputs together.",
            "goal-release", None, None)
        session.goal_id = goal.goal_id
        session.goal_epoch = goal.epoch
        session.goal_release = True
        # Semantic verification must evaluate the original brief, not the short
        # coordinator wrapper used to create this release session.
        session.task.text = goal.text
        session.collaboration_mode = "build_team"
        session.delivery_mode = "final_batch"
        session.workspace_root = goal.staging_root
        session.established_root = goal.established_root
        session.delivery_root = goal.delivery_root
        session.required_files = files
        session.panel = []
        self.store.save_session(session)
        self.manager.transition(session, SessionStatus.classified)
        self.manager.transition(session, SessionStatus.deliberating)
        goal.release_session_id = session.session_id
        if not self._verify_goal_release(goal, session):
            goal.status = "paused"
            goal.release_status = "failed_verification"
            goal.last_error = session.stop_reason or "frontier final-batch verification failed"
            return
        goal.status = "awaiting_release"
        if not (session.delivery_root or session.established_root):
            request = InputRequest(
                session_id=session.session_id, agent="system", role=Role.coordinator,
                round=0, purpose="promote_target", resume_token="",
                question=(
                    f"The complete goal batch is staged ({len(files)} files). Where should "
                    "the final batch go? Reply with one folder path. You will then see one "
                    "aggregate diff and approve the whole release once."
                ),
            )
            session.input_requests.append(request)
            session.stop_reason = "final batch needs a delivery target"
            goal.release_status = "awaiting_target"
            self.store.log_event(session.session_id, "input_requested", request.model_dump())
            self.manager.transition(session, SessionStatus.awaiting_input)
        else:
            goal.release_status = "awaiting_approval"
            self._authorize_goal_release(session)

    @staticmethod
    def _assembly_runtime_interface_hint(
        loaded_sources: list[tuple[str, str]],
    ) -> str:
        """Describe browser-global paths earlier scripts require at runtime.

        Runtime errors such as ``cannot set ... fire`` reveal only the final
        property and caused expensive whole-package retries to add one field
        while dropping another.  Recover the complete cross-file surface from
        the accepted consumers, including aliases such as
        ``var portalInput = window.ArcadePortal.input``.
        """
        requirements: list[tuple[str, list[str]]] = []
        direct = re.compile(
            r"(?:window\.)?ArcadePortal\.input"
            r"((?:\.[A-Za-z_$][\w$]*)+)"
        )
        alias_decl = re.compile(
            r"\b(?:var|let|const)\s+([A-Za-z_$][\w$]*)\s*=\s*"
            r"(?:window\.)?ArcadePortal\.input\b"
        )

        for source_name, text in loaded_sources:
            paths: set[str] = set()
            matches = list(direct.finditer(text))
            aliases = set(alias_decl.findall(text))
            for alias in aliases:
                matches.extend(re.finditer(
                    rf"\b{re.escape(alias)}((?:\.[A-Za-z_$][\w$]*)+)",
                    text,
                ))
            for match in matches:
                pieces = [piece for piece in match.group(1).split(".") if piece]
                for size in range(1, len(pieces) + 1):
                    paths.add(".".join(pieces[:size]))
            if paths:
                ordered = sorted(paths, key=lambda path: (path.count("."), path))
                requirements.append((source_name, ordered[:14]))

        if not requirements:
            return ""
        clauses = [
            f"{name} requires ArcadePortal.input paths [{', '.join(paths)}]"
            for name, paths in requirements[:3]
        ]
        return "cross-file interface mismatch: " + "; ".join(clauses)

    @staticmethod
    def _assembly_export_probe(ordered: list[tuple[str, str]]) -> str:
        """Fail-loud probe appended to a combined bundle before smoking it.

        Any shared-namespace export a file's own source assigns
        (``window.NS.Member = ...`` / ``NS.Member = ...`` where some file
        establishes ``window.NS``/``x.NS = x.NS || {}``) must actually EXIST
        once the page has loaded. A module-pattern file whose defensive guard
        bails at load — because a module it reads is loaded later in the
        template order — neither throws nor logs: it just silently never
        attaches its export, and the defect only surfaces as the entry
        point's "missing modules" complaint, far from the culprit. The probe
        turns that silence into a deterministic, attributable throw:
        ``MISSING_EXPORT NS.Member (declared in file)``. It runs two timer
        hops after the load event, so exports legitimately attached inside
        DOMContentLoaded/load handlers are not false positives. Textual
        detection can be fooled by an assignment mentioned only in a comment,
        so this probe is used for attribution of already-failing bundles, and
        the fault streak caps any misfire.
        """
        namespaces: set[str] = set()
        for _name, text in ordered:
            namespaces.update(
                m.group(1) for m in _ASSEMBLY_NS_ROOT_RE.finditer(text))
            namespaces.update(
                m.group(1) for m in _ASSEMBLY_WINDOW_ROOT_RE.finditer(text))
        if not namespaces:
            return ""
        checks: list[str] = []
        seen: set[tuple[str, str]] = set()
        for name, text in ordered:
            for ns in sorted(namespaces):
                for m in re.finditer(_assembly_member_assign_re(ns), text):
                    member = m.group(1)
                    if (ns, member) in seen:
                        continue
                    seen.add((ns, member))
                    checks.append(
                        f"if (!(window.{ns} && window.{ns}.{member} !== undefined)) "
                        f"throw new Error(\"MISSING_EXPORT {ns}.{member} "
                        f"(declared in {name})\");"
                    )
        if not checks:
            return ""
        return (
            "\n;window.addEventListener('load', function(){ "
            "setTimeout(function(){ setTimeout(function(){\n"
            + "\n".join(checks)
            + "\n}, 0); }, 0); });\n"
        )

    @staticmethod
    def _late_namespace_reads(
        blamed: str, ordered: list[tuple[str, str]],
    ) -> list[str]:
        """Shared-namespace members the blamed file reads whose attaching file
        loads LATER in the declared order — the deterministic signature of a
        load-order hazard, e.g. ``["Frogger.World (attached by world.js)"]``."""
        namespaces: set[str] = set()
        for _name, text in ordered:
            namespaces.update(
                m.group(1) for m in _ASSEMBLY_NS_ROOT_RE.finditer(text))
            namespaces.update(
                m.group(1) for m in _ASSEMBLY_WINDOW_ROOT_RE.finditer(text))
        order = [name for name, _text in ordered]
        blamed_position = order.index(blamed) if blamed in order else -1
        if blamed_position < 0:
            return []
        blamed_text = ordered[blamed_position][1]
        late: list[str] = []
        for ns in sorted(namespaces):
            own = set(re.findall(_assembly_member_assign_re(ns), blamed_text))
            reads = set(re.findall(
                rf"\b(?:[\w$]+\.)?{re.escape(ns)}\.([A-Za-z_$][\w$]*)\b",
                blamed_text,
            )) - own
            for read in sorted(reads):
                provider_position = next(
                    (position for position, (_name, text) in enumerate(ordered)
                     if re.search(
                         rf"\b(?:[\w$]+\.)?{re.escape(ns)}\.{re.escape(read)}"
                         rf"\s*=(?!=)",
                         text)),
                    -1,
                )
                if provider_position > blamed_position:
                    late.append(
                        f"{ns}.{read} (attached by {order[provider_position]})")
        return late

    @classmethod
    def _describe_missing_export(
        cls, ns: str, member: str, declared_in: str,
        ordered: list[tuple[str, str]],
    ) -> tuple[str, str]:
        """Actionable blame for a module that silently failed to attach its
        export: name the missing attachment and, when the source shows it, the
        exact cross-module read whose provider loads later. This text lands
        verbatim in the owner's RETRY CORRECTION prompt, so it must state the
        constraint the rebuild has to satisfy — a bare symptom ("Renderer is
        missing") historically made the owner wrap its module in a defensive
        guard that silenced the crash and reproduced this very defect."""
        late = cls._late_namespace_reads(declared_in, ordered)
        detail = (
            f"{declared_in} never attached window.{ns}.{member}: its top-level "
            "code bailed out before the assignment ran"
        )
        if late:
            detail += (
                ". Root cause: it reads " + ", ".join(late) + " — loaded AFTER "
                "it in the template script order, so a script-load-time read "
                "sees undefined. Attach exports unconditionally at load time; "
                "look up other modules lazily inside functions when called"
            )
        return declared_in, detail

    @staticmethod
    def _style_contract_regression(session: Session) -> tuple[str, str]:
        """Statically reproduce the release browser gate's style-contract check.

        The browser gate fails a release whose rendered DOM classes are mostly
        unmatched by any stylesheet rule (coverage < 0.35 with >= 8 classes in
        use). That comparison needs no browser: class tokens in the staged
        template's markup vs class tokens in the staged stylesheets' selectors.
        Reproducing it here lets a failed release reopen the stylesheet's
        owner package with the exact unmatched class list, instead of praying
        the frontier verifier rewrites a whole stylesheet inline — which a
        real goal watched it decline to do, twice.

        Returns ``(css_path, detail)`` when the staged inputs reproduce the
        defect, else ``("", "")``.
        """
        if not session.workspace_root:
            return "", ""
        sources = list((session.assembly_result or {}).get("sources") or [])
        css_names = [
            str(name).replace("\\", "/") for name in sources
            if Path(str(name)).suffix.lower() == ".css"
        ]
        template = (session.assembly_template or "").replace("\\", "/")
        if not css_names or not template:
            return "", ""
        root = Path(session.workspace_root)
        try:
            html = executor.resolve_in_workspace(root, template).read_text(
                encoding="utf-8", errors="replace")
        except (OSError, executor.ExecutionError):
            return "", ""
        used: set[str] = set()
        for quoted in re.finditer(r"class\s*=\s*\"([^\"]*)\"", html):
            used.update(quoted.group(1).split())
        for quoted in re.finditer(r"class\s*=\s*'([^']*)'", html):
            used.update(quoted.group(1).split())
        if len(used) < 8:
            return "", ""
        css_text = ""
        for name in css_names:
            try:
                css_text += "\n" + executor.resolve_in_workspace(
                    root, name).read_text(encoding="utf-8", errors="replace")
            except (OSError, executor.ExecutionError):
                continue
        covered = {
            token.group(0)[1:]
            for token in re.finditer(r"\.[_a-zA-Z][_a-zA-Z0-9-]*", css_text)
        }
        matched = used & covered
        if len(matched) / len(used) >= 0.35:
            return "", ""
        missing = sorted(used - covered)
        target = css_names[0]
        detail = (
            f"{target} styles only {len(matched)} of the {len(used)} CSS "
            f"classes that the template {template} actually uses. The template "
            "markup and the stylesheet are ONE contract: keep the template's "
            "existing class names and write real rules for them — do NOT "
            "invent a different class scheme. Classes needing rules: "
            + ", ".join(missing[:24])
        )
        return target, detail

    def _assembly_runtime_failure_target(self, session: Session) -> tuple[str, str]:
        """Locate the accepted script that makes an assembled runtime fail.

        Deterministic assembly preserves a declared load order. Replaying those
        accepted JavaScript sources cumulatively identifies the point at which
        the bundle starts failing, without sending the expanded HTML back
        through a model context window. But the file whose ADDITION first
        flips the bundle from clean to failing is not necessarily the file
        whose CODE threw — a bug can sit dormant in an earlier dependency
        (only defined, never invoked) until a later entry-point file's
        lifecycle wiring finally calls it. So once a failing prefix is found,
        the captured stack trace's source line is mapped back through the
        known per-file line ranges of THAT prefix to find which file's own
        code was actually executing when it threw; that file is blamed
        instead of the merely-triggering one whenever the mapping succeeds.

        Each prefix also carries an export probe (see
        ``_assembly_export_probe``): a module that silently fails to attach
        its declared export — the no-throw shape a stack trace can never
        attribute — is caught at exactly the prefix that reproduces its real
        load environment, and blamed with the load-order constraint spelled
        out for the rebuild prompt.
        """
        if not session.workspace_root:
            return "", ""
        sources = list((session.assembly_result or {}).get("sources") or [])
        ordered: list[tuple[str, str]] = []
        for name in sources:
            normalized = str(name).replace("\\", "/")
            if Path(normalized).suffix.lower() not in {".js", ".mjs"}:
                continue
            try:
                path = executor.resolve_in_workspace(
                    Path(session.workspace_root), normalized,
                )
                text = path.read_text(encoding="utf-8")
            except (OSError, executor.ExecutionError, UnicodeError):
                continue
            ordered.append((normalized, text))
        combined = ""
        loaded_sources: list[tuple[str, str]] = []
        # (start_line, end_line, name) 1-based inclusive ranges within `combined`
        # for every file folded in so far, so a stack line can be mapped back.
        ranges: list[tuple[int, int, str]] = []
        for position, (normalized, text) in enumerate(ordered):
            # 1-based line number of the first character = newlines before it + 1.
            # Holds regardless of whether the preceding text ends in "\n".
            separator = "\n\n" if combined else ""
            prefix = combined + separator
            start = prefix.count("\n") + 1
            combined = prefix + text
            end = start + text.count("\n") - (1 if text.endswith("\n") else 0)
            ranges.append((start, end, normalized))
            probe = self._assembly_export_probe(ordered[:position + 1])
            ran, testable, detail, _dynamic, error_line = smoke.smoke_source_with_line(
                combined + probe, ".js")
            if testable and not ran:
                export = re.search(
                    r"MISSING_EXPORT ([\w$]+)\.([\w$]+) \(declared in (.+?)\)",
                    detail,
                )
                if export:
                    return self._describe_missing_export(
                        export.group(1), export.group(2), export.group(3), ordered)
                blamed = normalized
                for range_start, range_end, range_name in ranges:
                    if range_start <= error_line <= range_end:
                        blamed = range_name
                        break
                hints: list[str] = []
                if "undefined" in detail.lower():
                    legacy = self._assembly_runtime_interface_hint(loaded_sources)
                    if legacy:
                        hints.append(legacy)
                    # A THROWN "undefined" crash needs the same load-order
                    # constraint as a silent bail: telling the owner only the
                    # symptom made one rebuild oscillate between the two
                    # shapes — guard added (silent bail), guard removed
                    # (throw) — without ever deferring the read.
                    late = self._late_namespace_reads(blamed, ordered)
                    if late:
                        hints.append(
                            f"load-order hazard: {blamed} reads "
                            + ", ".join(late)
                            + " — loaded AFTER it in the template script "
                            "order, so a script-load-time read sees undefined; "
                            "look up other modules lazily inside functions "
                            "when called, not at the top of the file"
                        )
                hint = "; ".join(hints)
                return blamed, f"{hint}; {detail}" if hint else detail
            loaded_sources.append((normalized, text))
        return "", ""

    def _assembly_failure_target(self, session: Session) -> tuple[str, str]:
        """Return the accepted input responsible for an assembly failure.

        New sessions persist structured blame from ``AssemblyError``.  The
        marker fallback keeps already-paused goals created by older versions
        recoverable without guessing from arbitrary model text.
        """
        gate = session.quality_gate or {}
        if gate.get("stage") != "deterministic_assembly":
            runtime_failure = next(
                (
                    issue for issue in reversed(session.unresolved)
                    if "artifact verification failed" in issue.lower()
                    and "does not run" in issue.lower()
                ),
                "",
            )
            if session.assembly_mode == assembly.HTML_INLINE and runtime_failure:
                runtime_path, runtime_detail = self._assembly_runtime_failure_target(session)
                if runtime_path:
                    session.quality_gate = {
                        "verdict": "FAIL",
                        "stage": "deterministic_assembly",
                        "detail": (
                            "assembled runtime became invalid at accepted dependency "
                            f"{runtime_path}: {runtime_detail}"
                        ),
                        "fault_scope": "dependency",
                        "fault_path": runtime_path,
                    }
                    self.store.save_session(session)
                    return "dependency", runtime_path
            return "", ""
        scope = str(gate.get("fault_scope") or "").lower()
        path = str(gate.get("fault_path") or "").replace("\\", "/")
        detail = str(gate.get("detail") or "").lower()
        if not scope:
            integrity_markers = (
                "assembly dependency is missing",
                "assembly dependency has no accepted hash",
                "assembly dependency changed after acceptance",
            )
            dependency_markers = (
                "assembly dependency is not utf-8 text",
                "cannot inline ",
                "inline stylesheet contains @import",
            )
            if any(marker in detail for marker in integrity_markers):
                scope = "integrity"
            elif any(marker in detail for marker in dependency_markers):
                scope = "dependency"
            elif any(marker in detail for marker in (
                "template", "directive", "external script",
                "stylesheet reference", "complete document",
            )):
                scope = "template"
        if scope == "template":
            path = session.assembly_template.replace("\\", "/")
        elif scope == "dependency" and not path:
            path = next(
                (name.replace("\\", "/") for name in session.runtime_dependencies
                 if name.replace("\\", "/").lower() in detail),
                "",
            )
        return scope, path

    def _invalidate_assembly_input_provider(
        self, goal: Goal, session: Session, assembly_index: int,
    ) -> Optional[GoalMilestone]:
        """Attribute deterministic assembly failure to its accepted input owner.

        The final assembly package cannot repair an accepted template or source
        file it does not own. Blacklist only the invalid upstream attempt so a
        retry preserves every healthy sibling package.
        """
        if (session.assembly_mode != assembly.HTML_INLINE
                or not session.assembly_template):
            return None
        scope, invalid_input = self._assembly_failure_target(session)
        if (scope == "template"
                and session.assembly_template == assembly.OWNER_TEMPLATE):
            return None
        if scope not in {"template", "dependency"} or not invalid_input:
            return None
        # Keep original case: this text is repeated to the rebuilding model,
        # and identifiers like window.Frogger.Renderer must survive verbatim.
        detail = str((session.quality_gate or {}).get("detail") or "")
        provider = next(
            (package for package in goal.milestones
             if package.index != assembly_index
             and invalid_input in {
                 name.replace("\\", "/") for name in package.required_files
             }),
            None,
        )
        if provider is None:
            return None
        streak_key = f"{provider.index}:{scope}:{invalid_input}"
        streak = goal.assembly_fault_streak.get(streak_key, 0) + 1
        goal.assembly_fault_streak[streak_key] = streak
        if streak > config.ASSEMBLY_FAULT_STREAK_LIMIT:
            goal.status = "paused"
            goal.last_error = (
                f"assembly attribution has blamed package {provider.index + 1} "
                f"for the same {scope} fault {streak - 1} times in a row without "
                f"resolving it ({invalid_input}); pausing for human review instead "
                "of rebuilding it again"
            )[:300]
            self.store.log_event(
                session.session_id,
                "assembly_fault_loop_detected",
                {
                    "goal_id": goal.goal_id,
                    "provider_package": provider.index + 1,
                    "fault_scope": scope,
                    "input": invalid_input,
                    "streak": streak,
                },
            )
            return None
        if provider.session_id and provider.session_id not in provider.invalidated_session_ids:
            provider.invalidated_session_ids.append(provider.session_id)
        provider.status = "failed"
        provider.files = []
        provider.accepted_files = []
        provider.accepted_hashes = {}
        # 600, not 300: this exact text becomes the owner's RETRY CORRECTION
        # prompt, and load-order faults need room to state the constraint the
        # rebuild must satisfy — truncating it re-creates the blind rebuild
        # that reproduced the same defect.
        provider.acceptance_detail = (
            f"invalidated by deterministic assembly {scope}: {detail}"
        )[:600]
        event = (
            "assembly_template_provider_invalidated"
            if scope == "template" else
            "assembly_dependency_provider_invalidated"
        )
        self.store.log_event(
            session.session_id,
            event,
            {
                "goal_id": goal.goal_id,
                "assembly_package": assembly_index + 1,
                "provider_package": provider.index + 1,
                "provider_session_id": provider.session_id,
                "fault_scope": scope,
                "input": invalid_input,
                "reason": detail[:300],
            },
        )
        return provider

    def _maybe_advance_goal(self, session: Session, background: bool = False) -> None:
        """Advance only from the goal epoch that started this session."""
        if not session.goal_id:
            return
        try:
            goal = None
            # Parallel packages may finish in the same instant.  Wait briefly
            # for the other completion transaction instead of dropping this
            # package's terminal event on a busy goal lease.
            for _ in range(100):
                goal = self.goals.claim_worker_lease(
                    session.goal_id, {"running", "draining", "paused"})
                if goal is not None:
                    break
                current = self.goals.get(session.goal_id)
                if current is None or current.status not in ("running", "draining", "paused"):
                    return
                time.sleep(0.02)
            if goal is None:
                return
            token = goal.worker_lease
            schedule_ready = False
            try:
                idx = session.goal_milestone
                milestone = goal.milestones[idx] if (
                    idx is not None and 0 <= idx < len(goal.milestones)) else None
                if (milestone is None or milestone.session_id != session.session_id
                        or session.goal_epoch != goal.epoch):
                    return
                if session.status == SessionStatus.done:
                    accepted, files, detail = self._goal_acceptance(session, milestone)
                    milestone.acceptance_detail = detail
                    if not accepted:
                        milestone.status = "failed"
                        sibling_running = any(
                            item.status == "running" and item.index != idx
                            for item in goal.milestones
                        )
                        goal.status = "draining" if sibling_running else "paused"
                        goal.last_error = detail[:300]
                        self.goals.save_owned(goal, token)
                        self.store.log_event(session.session_id, "goal_milestone_rejected",
                                             {"goal_id": goal.goal_id, "milestone": idx + 1,
                                              "reason": detail})
                        return
                    if goal.delivery_mode == "final_batch":
                        _, _, hashes = self._goal_stage_manifest(
                            session, milestone.required_files, goal.staging_root)
                    else:
                        _, _, hashes = self._goal_delivery_manifest(
                            session, milestone.required_files)
                    milestone.status = "done"
                    # Deliberately NOT clearing assembly_fault_streak here: a
                    # provider finishing a rebuild only means an attempt
                    # completed, not that the bundle-level fault is fixed. A
                    # real goal cycled release-fail -> blame renderer.js ->
                    # rebuild "done" (streak wiped right here) -> same fault
                    # again, so the loop breaker could never trip. The streak
                    # now survives until the release actually verifies.
                    milestone.files = list(files)
                    milestone.accepted_files = list(files)
                    milestone.accepted_hashes = {
                        name: hashes[name] for name in milestone.required_files if name in hashes
                    }
                    milestone.output_provenance = self._accepted_output_provenance(
                        session, milestone.required_files, milestone.accepted_hashes
                    )
                    milestone.summary = (session.final.answer if session.final else "")[
                        : config.GOAL_SUMMARY_MAX_CHARS]
                    self.store.log_event(session.session_id, "goal_milestone_done",
                                         {"goal_id": goal.goal_id, "milestone": idx + 1,
                                          "of": len(goal.milestones)})
                    remaining = [m.index for m in goal.milestones if m.status != "done"]
                    if goal.status == "draining":
                        if not any(m.status == "running" for m in goal.milestones):
                            goal.status = "paused"
                            failed = [m.index for m in goal.milestones if m.status == "failed"]
                            goal.current_index = min(failed or remaining or [len(goal.milestones)])
                            self.store.log_event(
                                "-", "goal_drained",
                                {"goal_id": goal.goal_id, "reason": goal.last_error},
                            )
                        self.goals.save_owned(goal, token)
                        return
                    if not remaining:
                        if goal.delivery_mode == "final_batch" and goal.status == "running":
                            self._prepare_goal_release(goal)
                        elif goal.delivery_mode != "final_batch":
                            goal.status = "completed"
                            self.store.log_event("-", "goal_completed", {"goal_id": goal.goal_id})
                        self.goals.save_owned(goal, token)
                    else:
                        goal.current_index = min(remaining)
                        if self.goals.save_owned(goal, token):
                            schedule_ready = goal.status == "running"
                elif session.status == SessionStatus.failed:
                    milestone.status = "failed"
                    goal_status_before_attribution = goal.status
                    invalidated_provider = self._invalidate_assembly_input_provider(
                        goal, session, idx)
                    fault_loop_detected = (
                        invalidated_provider is None
                        and goal.status == "paused"
                        and goal_status_before_attribution != "paused"
                    )
                    if invalidated_provider is not None:
                        # This is an ownership-attribution correction, not a
                        # reason to make the human press Resume twice. Rebuild
                        # the exact upstream provider, then let hard-dependency
                        # scheduling rerun assembly automatically.
                        invalidated_provider.status = "pending"
                        invalidated_provider.session_id = None
                        milestone.status = "pending"
                        milestone.session_id = None
                        goal.status = "running"
                        goal.current_index = invalidated_provider.index
                        fault_scope, fault_path = self._assembly_failure_target(session)
                        goal.last_error = (
                            f"rebuilding invalid assembly {fault_scope} {fault_path} "
                            f"from package {invalidated_provider.index + 1}"
                        )
                        if self.goals.save_owned(goal, token):
                            schedule_ready = True
                        self.store.log_event(
                            "-", (
                                "assembly_template_rebuild_scheduled"
                                if fault_scope == "template" else
                                "assembly_dependency_rebuild_scheduled"
                            ),
                            {
                                "goal_id": goal.goal_id,
                                "provider_package": invalidated_provider.index + 1,
                                "assembly_package": idx + 1,
                                "fault_scope": fault_scope,
                                "input": fault_path,
                            },
                        )
                    elif fault_loop_detected:
                        # goal.status/last_error were already set by the streak
                        # breaker inside _invalidate_assembly_input_provider —
                        # preserve that diagnostic instead of the generic one.
                        self.goals.save_owned(goal, token)
                        self.store.log_event(
                            "-", "goal_paused",
                            {"goal_id": goal.goal_id, "reason": goal.last_error},
                        )
                    else:
                        sibling_running = any(
                            item.status == "running" and item.index != idx
                            for item in goal.milestones
                        )
                        goal.status = "draining" if sibling_running else "paused"
                        goal.last_error = (
                            session.stop_reason or f"milestone {idx + 1} failed"
                        )[:300]
                        self.goals.save_owned(goal, token)
                        self.store.log_event(
                            "-", "goal_draining" if sibling_running else "goal_paused",
                            {"goal_id": goal.goal_id, "reason": goal.last_error},
                        )
                elif session.status == SessionStatus.cancelled:
                    milestone.status = "pending"
                    sibling_running = any(
                        item.status == "running" and item.index != idx
                        for item in goal.milestones
                    )
                    goal.status = "draining" if sibling_running else "paused"
                    goal.last_error = f"milestone {idx + 1} was cancelled"
                    self.goals.save_owned(goal, token)
                    self.store.log_event(
                        "-", "goal_draining" if sibling_running else "goal_paused",
                        {"goal_id": goal.goal_id, "reason": goal.last_error},
                    )
            finally:
                self.goals.release_worker_lease(goal.goal_id, token)
            if schedule_ready:
                current = self.goals.get(goal.goal_id)
                if current and current.status == "running" and current.epoch == session.goal_epoch:
                    self._start_ready_packages(current, background=background)
        except Exception as e:  # noqa: BLE001
            self.store.log_event("-", "goal_advance_error",
                                 {"goal_id": session.goal_id, "detail": str(e)})

    def _goal_views(self, items: list[Goal]) -> list[dict]:
        """Attach aggregate/actionable state without mutating durable goals."""
        sessions = self.store.list_sessions(limit=None)
        by_id = {item["session_id"]: item for item in sessions}
        views: list[dict] = []
        for goal in items:
            data = goal.model_dump()
            related = [item for item in sessions if item.get("goal_id") == goal.goal_id]
            terminal_goal = goal.status in ("cancelled", "completed", "failed")
            live_related = [
                item for item in related
                if item.get("status") not in ("done", "failed", "cancelled")
            ]
            approvals = (0 if terminal_goal else
                         sum(item.get("pending_approvals", 0) for item in live_related))
            inputs = (0 if terminal_goal else
                      sum(item.get("pending_inputs", 0) for item in live_related))
            active_calls = (0 if terminal_goal else
                            sum(len(item.get("active_agent_calls") or []) for item in live_related))
            package_views: list[dict] = []
            for package in goal.milestones:
                package_data = package.model_dump()
                attempts = [item for item in related
                            if item.get("work_package_id") == package.package_id]
                attempts = sorted(attempts, key=lambda item: item.get("created_at") or "")
                current = by_id.get(package.session_id or "", {})
                effective_status = package.status
                if goal.status == "cancelled" and package.status == "running":
                    # Repair the API view of pre-upgrade cancelled goals whose
                    # durable package row was left looking live.
                    effective_status = "cancelled"
                package_data.update({
                    "status": effective_status,
                    "attempt_count": len(attempts),
                    "session_status": current.get("status"),
                    "pending_approvals": current.get("pending_approvals", 0),
                    "pending_inputs": current.get("pending_inputs", 0),
                    "active_agent_calls": current.get("active_agent_calls", []),
                    "agent_calls": current.get("agent_calls", 0),
                    "agent_call_attempts": current.get("agent_call_attempts", 0),
                    "agent_attempt_duration_ms": current.get(
                        "agent_attempt_duration_ms", 0
                    ),
                    "output_authors": current.get("package_output_authors", {}),
                    "output_attempts": current.get("package_output_attempts", {}),
                    "output_history": current.get("package_output_history", {}),
                    "author_failures": current.get("package_call_failures", {}),
                    "authoring_started_at": current.get("package_started_at"),
                    "authoring_deadline_at": current.get("package_deadline_at"),
                    "attempts": [
                        {
                            "number": number,
                            "session_id": item.get("session_id"),
                            "status": item.get("status"),
                            "created_at": item.get("created_at"),
                            "updated_at": item.get("updated_at"),
                            "active_agent_calls": item.get("active_agent_calls") or [],
                            "agent_calls": item.get("agent_calls", 0),
                            "agent_call_attempts": item.get("agent_call_attempts", 0),
                            "agent_attempt_duration_ms": item.get(
                                "agent_attempt_duration_ms", 0
                            ),
                            "output_authors": item.get("package_output_authors") or {},
                            "output_attempts": item.get("package_output_attempts") or {},
                            "output_history": item.get("package_output_history") or {},
                            "author_failures": item.get("package_call_failures") or {},
                            "authoring_started_at": item.get("package_started_at"),
                            "authoring_deadline_at": item.get("package_deadline_at"),
                            "is_current": item.get("session_id") == package.session_id,
                        }
                        for number, item in enumerate(attempts, start=1)
                    ],
                })
                package_views.append(package_data)
            data["milestones"] = package_views
            contributing_agents = set()
            if goal.planned_by:
                contributing_agents.add(goal.planned_by)
            for item in related:
                contributing_agents.update(
                    (item.get("successful_agent_calls") or {}).keys()
                )
            expected_roster = list(goal.build_roster or [])
            data["contributing_agents"] = sorted(contributing_agents)
            data["contributor_count"] = len(contributing_agents)
            data["expected_contributor_count"] = len(expected_roster)
            data["participation_complete"] = (
                set(expected_roster).issubset(contributing_agents)
                if expected_roster else None
            )
            if goal.status in ("cancelled", "completed", "failed"):
                display_status = goal.status
            elif approvals:
                display_status = "awaiting_approval"
            elif inputs:
                display_status = "awaiting_input"
            else:
                display_status = goal.status
            actionable = None
            if not terminal_goal:
                actionable = next(
                    (item for item in related if item.get("pending_approvals")), None)
                actionable = actionable or next(
                    (item for item in related if item.get("pending_inputs")), None)
                if actionable is None and goal.release_session_id:
                    actionable = by_id.get(goal.release_session_id)
                if actionable is None:
                    current = goal.current
                    actionable = (by_id.get(current.session_id)
                                  if current and current.session_id else None)
                if actionable is None:
                    actionable = next((item for item in related
                                       if item.get("status") not in
                                       ("done", "failed", "cancelled")), None)
            data.update({
                "display_status": display_status,
                "active_packages": (0 if terminal_goal else
                                    sum(1 for package in goal.milestones
                                        if package.status == "running")),
                "pending_approvals": approvals,
                "pending_inputs": inputs,
                "active_agent_calls": active_calls,
                "agent_call_attempts": sum(
                    item.get("agent_call_attempts", 0) for item in related
                ),
                "agent_attempt_duration_ms": sum(
                    item.get("agent_attempt_duration_ms", 0) for item in related
                ),
                "actionable_session_id": actionable.get("session_id") if actionable else None,
            })
            views.append(data)
        return views

    def list_goals(self) -> list[dict]:
        return self._goal_views(list(reversed(self.goals.list())))

    def get_goal(self, goal_id: str) -> Optional[dict]:
        goal = self.goals.get(goal_id)
        return self._goal_views([goal])[0] if goal else None

    def cancel_goal(self, goal_id: str) -> dict:
        """Cancel a goal and its running milestone session. Cancelled is
        terminal — use resume on a PAUSED goal to retry a milestone."""
        goal = self.goals.cancel(goal_id)
        if goal is None:
            raise KeyError(f"goal {goal_id} not found")
        if goal.status in ("completed", "cancelled", "failed"):
            # A just-cancelled goal belongs here too; its epoch/lease have
            # already been invalidated atomically by GoalStore.cancel().
            ms = goal.current
            if ms and ms.session_id:
                try:
                    self.cancel_session(ms.session_id)
                except KeyError:
                    pass
            for package in goal.milestones:
                if package.session_id and (ms is None or package.session_id != ms.session_id):
                    try:
                        self.cancel_session(package.session_id)
                    except (KeyError, ValueError):
                        pass
            if goal.release_session_id:
                try:
                    self.cancel_session(goal.release_session_id)
                except (KeyError, ValueError):
                    pass
            self.store.log_event("-", "goal_cancelled", {"goal_id": goal_id, "epoch": goal.epoch})
            return goal.model_dump()

    def _reopen_paused_assembly_provider(self, goal_id: str) -> bool:
        """Repair blame for an assembly failure recorded before resume.

        This also upgrades paused goals written before structured assembly
        faults existed. The rejected assembler remains retryable, while only
        the accepted package that owns the bad input is invalidated.
        """
        goal = self.goals.claim_worker_lease(goal_id, {"paused"})
        if goal is None:
            return False
        token = goal.worker_lease
        scheduled: Optional[dict] = None
        try:
            assembly_package = goal.current
            if (assembly_package is None or assembly_package.status != "failed"
                    or not assembly_package.session_id):
                return False
            session = self.manager.load(assembly_package.session_id)
            if session is None:
                return False
            status_before_attribution = goal.status
            provider = self._invalidate_assembly_input_provider(
                goal, session, assembly_package.index)
            if provider is None:
                if goal.status == "paused" and status_before_attribution != "paused":
                    # The streak breaker fired and set a diagnostic last_error;
                    # persist it, or the next /resume just recomputes streak=1
                    # and the cap never actually bites.
                    self.goals.save_owned(goal, token)
                return False
            fault_scope, fault_path = self._assembly_failure_target(session)
            provider.status = "pending"
            provider.session_id = None
            assembly_package.status = "pending"
            assembly_package.session_id = None
            goal.current_index = provider.index
            goal.last_error = (
                f"rebuilding invalid assembly {fault_scope} {fault_path} "
                f"from package {provider.index + 1}"
            )
            if not self.goals.save_owned(goal, token):
                return False
            scheduled = {
                "goal_id": goal.goal_id,
                "provider_package": provider.index + 1,
                "assembly_package": assembly_package.index + 1,
                "fault_scope": fault_scope,
                "input": fault_path,
            }
            return True
        finally:
            self.goals.release_worker_lease(goal_id, token)
            if scheduled:
                event = (
                    "assembly_template_rebuild_scheduled"
                    if scheduled["fault_scope"] == "template" else
                    "assembly_dependency_rebuild_scheduled"
                )
                self.store.log_event("-", event, scheduled)

    def _reopen_release_regression_package(self, goal_id: str) -> str:
        """Turn a failed final release verification back into a package rebuild.

        Browser acceptance rejecting the assembled release means an ACCEPTED
        package's staged output is defective — but the release loop can only
        re-verify the same staged bytes, so resuming used to re-run another
        identical, expensive frontier verification that could never pass (a
        real goal burned two full release sessions on the exact same console
        error). Instead, reproduce the failure deterministically from the
        staged assembly inputs; when that pins a culprit file, reopen its
        owner package (and the assembly package) exactly like an
        assembly-time fault, with the same streak accounting.

        Returns "reopened" (packages rescheduled), "breaker" (the fault
        streak cap fired — stay paused for a human), or "" (could not
        reproduce or attribute — fall back to the release retry loop).
        """
        goal = self.goals.claim_worker_lease(goal_id, {"paused"})
        if goal is None:
            return ""
        token = goal.worker_lease
        scheduled: Optional[dict] = None
        try:
            if goal.release_status != "failed_verification" or not goal.release_defects:
                return ""
            if not (goal.milestones
                    and all(m.status == "done" for m in goal.milestones)):
                return ""
            assembly_package = next(
                (package for package in reversed(goal.milestones)
                 if package.session_id
                 and self._assembly_contract(package)[0] == assembly.HTML_INLINE),
                None,
            )
            if assembly_package is None:
                return ""
            session = self.manager.load(assembly_package.session_id)
            if session is None or session.assembly_mode != assembly.HTML_INLINE:
                return ""
            fault_path = ""
            fault_detail = ""
            runtime_path, runtime_detail = self._assembly_runtime_failure_target(session)
            if runtime_path:
                fault_path = runtime_path
                fault_detail = (
                    "release verification failed and the staged assembled runtime "
                    f"reproduces it at accepted dependency {runtime_path}: "
                    f"{runtime_detail}"
                )
            elif any("style contract" in str(d) for d in goal.release_defects):
                style_path, style_detail = self._style_contract_regression(session)
                if style_path:
                    fault_path = style_path
                    fault_detail = (
                        "release verification failed on the style contract and "
                        f"the staged inputs reproduce it: {style_detail}"
                    )
            if not fault_path:
                return ""
            session.quality_gate = {
                "verdict": "FAIL",
                "stage": "deterministic_assembly",
                "detail": fault_detail,
                "fault_scope": "dependency",
                "fault_path": fault_path,
            }
            self.store.save_session(session)
            provider = self._invalidate_assembly_input_provider(
                goal, session, assembly_package.index)
            if provider is None:
                if "pausing for human review" in (goal.last_error or ""):
                    # The streak breaker fired inside the invalidation call;
                    # persist its diagnostic so repeated resumes cannot
                    # silently reset the cap and grind the same rebuild.
                    self.goals.save_owned(goal, token)
                    return "breaker"
                return ""
            provider.status = "pending"
            provider.session_id = None
            assembly_package.status = "pending"
            assembly_package.session_id = None
            goal.current_index = provider.index
            goal.release_status = "not_started"
            goal.release_session_id = None
            goal.last_error = (
                f"release verification failed; rebuilding {fault_path} "
                f"from package {provider.index + 1}"
            )
            if not self.goals.save_owned(goal, token):
                return ""
            scheduled = {
                "goal_id": goal.goal_id,
                "provider_package": provider.index + 1,
                "assembly_package": assembly_package.index + 1,
                "fault_scope": "dependency",
                "input": fault_path,
            }
            return "reopened"
        finally:
            self.goals.release_worker_lease(goal_id, token)
            if scheduled:
                self.store.log_event(
                    "-", "release_regression_rebuild_scheduled", scheduled)

    def _recover_verified_goal_packages(self, goal_id: str) -> list[str]:
        """Adopt completed package attempts that lost their goal commit.

        Session verification and goal staging are separate durable transactions.
        A pause/restart in the old implementation could land between them.  On
        resume, recover exact owner/package outputs from any completed successful
        attempt before spending another model call.
        """
        goal = self.goals.claim_worker_lease(goal_id, {"paused"})
        if goal is None:
            return []
        token = goal.worker_lease
        recovered: list[str] = []
        superseded: list[str] = []
        try:
            candidates: dict[str, list[Session]] = {}
            for meta in self.store.list_sessions(limit=None):
                session = self.manager.load(meta.get("session_id", ""))
                if (session is None or session.goal_id != goal_id
                        or session.status != SessionStatus.done
                        or session.outcome != "succeeded"
                        or not session.work_package_id):
                    continue
                candidates.setdefault(session.work_package_id, []).append(session)
            for package in goal.milestones:
                if package.status == "done":
                    continue
                match = None
                for candidate in candidates.get(package.package_id, []):
                    if (candidate.work_package_owner != package.owner
                            or candidate.session_id in package.invalidated_session_ids
                            or not set(package.required_files).issubset(
                                set(candidate.required_files))):
                        continue
                    sealed = candidate.verified_output_hashes
                    latest = {
                        action.filename.replace("\\", "/"): Path(action.result_path)
                        for action in candidate.proposed_actions
                        if (action.role != Role.panelist
                            and action.kind in ("write_file", "edit_file")
                            and action.status == "executed" and action.result_path)
                    }
                    try:
                        intact = all(
                            sealed.get(name)
                            and name in latest
                            and hashlib.sha256(latest[name].read_bytes()).hexdigest()
                            == sealed[name]
                            for name in package.required_files
                        )
                    except OSError:
                        intact = False
                    if intact:
                        match = candidate
                        break
                if match is None:
                    continue
                accepted, missing, hashes = self._goal_stage_manifest(
                    match, package.required_files, goal.staging_root)
                if missing:
                    continue
                if package.session_id and package.session_id != match.session_id:
                    superseded.append(package.session_id)
                package.status = "done"
                package.session_id = match.session_id
                package.files = list(accepted)
                package.accepted_files = list(accepted)
                package.accepted_hashes = {
                    name: hashes[name] for name in package.required_files if name in hashes
                }
                package.output_provenance = self._accepted_output_provenance(
                    match, package.required_files, package.accepted_hashes
                )
                package.acceptance_detail = "recovered verified output from completed attempt"
                package.summary = (match.final.answer if match.final else "")[
                    : config.GOAL_SUMMARY_MAX_CHARS]
                recovered.append(package.package_id)
                self.store.log_event(
                    match.session_id, "goal_milestone_recovered",
                    {"goal_id": goal_id, "milestone": package.index + 1},
                )
            remaining = [m.index for m in goal.milestones if m.status != "done"]
            goal.current_index = min(remaining) if remaining else len(goal.milestones)
            self.goals.save_owned(goal, token)
        finally:
            self.goals.release_worker_lease(goal_id, token)
        for session_id in superseded:
            try:
                self.cancel_session(session_id)
            except (KeyError, ValueError):
                pass
        return recovered

    def resume_goal(self, goal_id: str, background: bool = True) -> dict:
        """Retry a paused goal's current milestone with a FRESH session (the
        prior attempt failed or was cancelled)."""
        goal = self.goals.get(goal_id)
        if goal is None:
            raise KeyError(f"goal {goal_id} not found")
        if goal.status != "paused":
            raise ValueError(f"cannot resume a goal in status '{goal.status}'")
        if not goal.milestones:
            replanning = self.goals.replan(goal_id)
            if replanning is None:
                raise ValueError("goal changed while attempting to restart planning")
            self.store.log_event("-", "goal_replanning", {"goal_id": goal_id})
            if background:
                self._pool.submit(self._plan_and_start_safely, goal_id)
                return self.get_goal(goal_id) or replanning.model_dump()
            self._plan_and_start(goal_id)
            return self.get_goal(goal_id) or replanning.model_dump()
        self._reopen_paused_assembly_provider(goal_id)
        recovered = self._recover_verified_goal_packages(goal_id)
        goal = self.goals.get(goal_id) or goal
        if recovered:
            self.store.log_event(
                "-", "goal_packages_recovered",
                {"goal_id": goal_id, "packages": recovered},
            )
        if (goal.milestones
                and all(m.status == "done" for m in goal.milestones)
                and goal.delivery_mode == "final_batch"):
            # A release that failed verification cannot pass by re-verifying
            # the same staged bytes. Reproduce the failure deterministically
            # and reopen the culprit package first; only fall back to the
            # frontier release-repair loop when nothing can be attributed.
            regression = self._reopen_release_regression_package(goal_id)
            if regression == "breaker":
                return self.get_goal(goal_id) or goal.model_dump()
            if regression == "reopened":
                goal = self.goals.resume(goal_id)
                if goal is None:
                    raise ValueError("goal changed while attempting to resume")
                self.store.log_event(
                    "-", "goal_resumed",
                    {"goal_id": goal_id, "epoch": goal.epoch})
                self._start_ready_packages(goal, background=background)
                return self.get_goal(goal_id) or goal.model_dump()
            retry_defects = list(goal.release_defects)
            previous_release = (
                self.manager.load(goal.release_session_id)
                if goal.release_session_id else None
            )
            if previous_release is not None:
                retry_defects.extend(
                    previous_release.quality_gate.get("remaining_defects") or []
                )
                for contribution in previous_release.contributions:
                    for action in parse_proposals(
                        previous_release.session_id, contribution.content,
                        Role.implementer,
                    ):
                        if (action.kind != "edit_file"
                                or action.filename not in goal.release_files):
                            continue
                        old = str(action.args.get("old") or "")[:500]
                        new = str(action.args.get("new") or "")[:500]
                        retry_defects.append(
                            f"pending prior frontier repair for {action.filename}: "
                            f"replace exact OLD [{old}] with NEW [{new}]"
                        )
            goal = self.goals.resume(goal_id)
            if goal is None:
                raise ValueError("goal changed while attempting to resume final release")
            goal.release_defects = list(dict.fromkeys(retry_defects))
            goal.release_session_id = None
            goal.release_status = "not_started"
            goal.last_error = ""
            self._prepare_goal_release(goal)
            self.goals.save(goal)
            return self.get_goal(goal_id) or goal.model_dump()
        if goal.current is None:  # defensive: nothing left to run
            goal.status = "completed"
            self.goals.save(goal)
            return goal.model_dump()
        goal = self.goals.resume(goal_id)
        if goal is None:
            raise ValueError("goal changed while attempting to resume")
        self.store.log_event("-", "goal_resumed", {"goal_id": goal_id, "epoch": goal.epoch})
        self._start_ready_packages(goal, background=background)
        return self.get_goal(goal_id) or goal.model_dump()

    def delete_goal(self, goal_id: str) -> bool:
        """Remove the goal record. Its milestone sessions remain in the store."""
        return self.goals.remove(goal_id)

    def _reconcile_goal_orphans(self) -> None:
        """After a restart there is no worker driving any goal: running
        milestone sessions were just cancelled by _reconcile_orphans, so park
        planning/running goals as paused — resume retries the current milestone."""
        try:
            for goal in self.goals.list():
                if goal.status == "cancelled":
                    # Older cancellation logic made the parent terminal but
                    # left package rows as "running", which kept the dashboard
                    # visually alive forever. Normalize those records once.
                    changed = False
                    for package in goal.milestones:
                        if package.status == "running":
                            package.status = "cancelled"
                            changed = True
                    if changed:
                        self.goals.save(goal)
                    continue
                if goal.status not in ("planning", "running", "draining"):
                    continue
                parked = self.goals.park_active(goal.goal_id, "interrupted by a server restart")
                if parked is not None:
                    self.store.log_event("-", "goal_paused",
                                         {"goal_id": parked.goal_id, "reason": parked.last_error})
        except Exception:  # noqa: BLE001 — a bad record must not stop the server
            pass

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
            # Startup recovery is correctness work, not a dashboard page: scan
            # every persisted row so a live session older than the UI's first
            # 100 entries cannot remain an orphan forever.
            metas = self.store.list_sessions(limit=None)
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
            session.outcome = "cancelled"
            session.active_agent_calls = []
            # In a hot-reload scenario the old Python thread may still be alive.
            # Signal it and revoke its token before recording the terminal state.
            cancellation.request(sid)
            self.store.revoke_worker_lease(sid)
            session.worker_lease = ""
            try:
                self.manager.transition(session, SessionStatus.cancelled)
            except ValueError:
                session.status = SessionStatus.cancelled
                self.store.save_session(session)
            self.store.log_event(sid, "session_cancelled", {"from": "restart_reconcile"})

    def cancel_session(self, session_id: str) -> dict:
        """Cancel immediately and revoke the worker's write authority.

        The adapter abort signal tears down HTTP/CLI work; revoking the lease
        prevents a late worker from resurrecting the session. Persisting the
        terminal snapshot here keeps the UI truthful instead of showing a
        cancelled goal whose selected session still says Deliberating.
        """
        session = self.manager.load(session_id)
        if session is None:
            raise KeyError(f"session {session_id} not found")
        if session.status in self._TERMINAL:
            return {"session_id": session_id, "status": session.status.value, "note": "already finished"}
        previous = session.status.value
        # Signal first so registered clients/processes are torn down while their
        # cancellation callback is still present.
        cancellation.request(session_id)
        self.store.log_event(session_id, "cancel_requested", {})
        self.store.revoke_worker_lease(session_id)
        for approval in session.approvals:
            if approval.status == "pending":
                approval.status = "denied"
        for request in session.input_requests:
            if request.status == "pending":
                request.status = "declined"
        session.worker_lease = ""
        session.active_agent_calls = []
        session.stop_reason = "cancelled by user"
        session.outcome = "cancelled"
        try:
            self.manager.transition(session, SessionStatus.cancelled)
        except ValueError:
            session.status = SessionStatus.cancelled
            self.store.save_session(session)
        self.store.log_event(session_id, "session_cancelled", {"from": previous})
        self._maybe_advance_goal(session, background=True)
        return {"session_id": session_id, "status": "cancelled"}

    def list(self) -> list[dict]:
        return self.store.list_sessions()

    def _finish_goal_release(self, session: Session, approved: bool) -> Session:
        """Resolve the special one-action release session without another LLM run."""
        action = next((a for a in session.proposed_actions if a.kind == "promote_batch"), None)
        goal = self.goals.get(session.goal_id) if session.goal_id else None
        if action is None or goal is None or goal.release_session_id != session.session_id:
            raise ValueError("final-batch release state is incomplete")
        if not approved:
            action.status = "denied"
            action.error = "final batch approval denied; staged files retained"
            session.stop_reason = action.error
            session.outcome = "cancelled"
            self.manager.transition(session, SessionStatus.cancelled)
            goal.status = "paused"
            goal.release_status = "denied"
            goal.last_error = action.error
            self.goals.save(goal)
            return session
        action.status = "approved"
        self.manager.transition(session, SessionStatus.composing)
        try:
            destination = executor.execute(session, action, self.store.data_dir)
            action.status = "executed"
            action.result_path = destination
            files = json.loads(action.args.get("files", "[]"))
            root = Path(destination)
            session.files_changed.extend(str(root / name) for name in files)
            session.tools_called.append("promote_batch")
            session.final = FinalAnswer(
                answer=(f"Released the complete verified goal batch in one transaction: "
                        f"{len(files)} files → {destination}."),
                confidence="high", assumptions=[], risks_unresolved=[], next_action=None)
            session.outcome = "succeeded"
            session.stop_reason = "final batch released"
            self.manager.transition(session, SessionStatus.done)
            self.store.log_event(session.session_id, "final_batch_released",
                                 {"goal_id": goal.goal_id, "files": files,
                                  "destination": destination})
            goal.status = "completed"
            goal.release_status = "released"
            goal.last_error = ""
            self.goals.save(goal)
        except Exception as e:  # noqa: BLE001
            action.status = "failed"
            action.error = str(e)
            session.outcome = "failed"
            session.stop_reason = f"final batch release failed: {e}"
            session.final = FinalAnswer(
                answer="The final batch was not released. The transaction failed and rollback was attempted.",
                confidence="low", assumptions=[], risks_unresolved=[str(e)],
                next_action="Review the conflict, then resume the goal to generate a fresh final diff.")
            self.manager.transition(session, SessionStatus.failed)
            goal.status = "paused"
            goal.release_status = "failed"
            goal.last_error = session.stop_reason[:300]
            self.goals.save(goal)
        self.store.save_session(session)
        return session

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
        # The awaiting-approval status can become visible a few milliseconds
        # before the background worker reaches its finally block and releases
        # the lease. An approval in that window used to be logged but rejected
        # as a stale write, then resume reloaded the still-pending snapshot.
        # A paused worker has returned from deliberation and will perform no more
        # session work, so hand off its token atomically and reload authority.
        if session.status == SessionStatus.awaiting_approval and session.worker_lease:
            self.store.release_worker_lease(session_id, session.worker_lease)
            session = self.manager.load(session_id) or session
        self._ensure_adapters(session)
        approval = self.governance.resolve(session, approval_id, approved, by=by,
                                           approve_all=approve_all)
        if session.goal_release:
            return self._finish_goal_release(session, approved)
        if session.status != SessionStatus.awaiting_approval:
            return session  # nothing to resume — approval was informational
        if not approved and approval.action_ref is None:
            session.stop_reason = "approval denied"
            self.manager.transition(session, SessionStatus.cancelled)
            return session
        if session.has_pending_approval:
            return session  # other gates still open; stay paused
        if background:
            return self._run_owned(session, self._resume_full, background=True)
        session = self._run_owned(session, self._resume_full, background=False)
        # synchronous resume runs in the caller's (request) thread — chain any
        # follow-on goal milestone on a worker so the response isn't held hostage
        self._maybe_advance_goal(session, background=True)
        return session

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
            return self._run_owned(session, self._answer_continue, True, req)
        session = self._run_owned(session, self._answer_continue, False, req)
        self._maybe_advance_goal(session, background=True)
        return session

    # answers that keep the build in the council's own spaces (no delivery target)
    _WORKSPACE_ANSWERS = {"workspace", "sandbox", "none", "skip", "no", "keep", "here"}
    # answers that end the rotation and compose from the work done so far
    _STOP_ANSWERS = {"no", "n", "stop", "finish", "done", "compose", "wrap up", "enough"}
    _USE_INTEGRATION_ANSWERS = {"use integration", "use", "integrate", "merge", "yes", "y"}

    def _answer_continue(self, session: Session, req) -> Session:
        # A best-of-N vote can surface a separately validated integration when
        # the codifier found concrete complementary strengths. This is a human
        # product decision, not a governance approval: either choice keeps the
        # existing delivery gate intact.
        if req.agent == "system" and req.purpose == "integration_decision":
            proposal = session.integration_proposal
            use_integration = (req.answer or "").strip().lower() in self._USE_INTEGRATION_ANSWERS
            if proposal is None:
                session.unresolved.append("integration decision was requested without a proposal")
            elif use_integration:
                write = next(
                    (a for a in reversed(session.proposed_actions)
                     if a.kind == "write_file" and a.role == Role.implementer
                     and a.filename == proposal.filename and a.status == "proposed"),
                    None,
                )
                if write is None:
                    session.unresolved.append("chosen integration could not replace the voted winner")
                    proposal.status = "kept_winner"
                else:
                    write.content = proposal.content
                    write.args["content"] = proposal.content
                    proposal.status = "adopted"
            else:
                proposal.status = "kept_winner"
            session.stop_reason = None
            self.store.log_event(
                session.session_id, "integration_decided",
                {"decision": proposal.status if proposal else "unavailable"},
            )
            self.store.save_session(session)
            return resume_deliberation(
                session, self.manager, self.registry, self.governance,
                self.store, role_agents=self.role_agents,
            )

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
                    if a.kind in ("promote", "promote_batch") and a.status == "proposed":
                        a.status = "denied"
                        a.error = "user kept the files in the council workspace"
            else:
                picked = extract_established_root(ans)
                if picked is None and ("/" in ans or "\\" in ans):
                    picked = str(Path(ans).expanduser().resolve())
                session.established_root = picked
            if session.goal_release:
                goal = self.goals.get(session.goal_id) if session.goal_id else None
                if goal is None:
                    raise ValueError("goal release no longer has a goal")
                if ans.lower() in self._WORKSPACE_ANSWERS:
                    self.manager.transition(session, SessionStatus.composing)
                    session.outcome = "succeeded"
                    session.stop_reason = "final batch retained in staging by user"
                    session.final = FinalAnswer(
                        answer=f"The complete batch remains in goal staging: {session.workspace_root}",
                        confidence="high")
                    self.manager.transition(session, SessionStatus.done)
                    goal.status = "completed"
                    goal.release_status = "released"
                    self.goals.save(goal)
                    return session
                if not session.established_root:
                    raise ValueError("a valid final delivery folder is required")
                goal.established_root = session.established_root
                goal.release_status = "awaiting_approval"
                self.goals.save(goal)
                self.store.save_session(session)
                return self._authorize_goal_release(session)
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
        resume_started = time.monotonic()
        try:
            result = self.registry.resume(req.agent, req.resume_token, req.answer)
        except AgentError as e:
            elapsed_ms = int((time.monotonic() - resume_started) * 1000)
            session.agent_call_attempts += 1
            session.agent_attempt_duration_ms += elapsed_ms
            session.unresolved.append(f"resume after user input failed: {e}")
            self.store.log_event(
                session.session_id,
                "agent_call_failed",
                {
                    "agent": req.agent,
                    "role": req.role.value,
                    "attempt": session.agent_call_attempts,
                    "duration_ms": elapsed_ms,
                    "error": str(e)[:300],
                    "resumed_after_input": True,
                },
            )
            self.manager.transition(session, SessionStatus.composing)
            session.final = fallback_final(session, "agent resume failed")
            session.outcome = "failed"
            self.manager.transition(session, SessionStatus.done)
            self.store.save_session(session)
            return session
        return resume_with_input(
            session, self.manager, self.registry, self.governance, self.store,
            self.role_agents, req, result,
            attempt_duration_ms=int((time.monotonic() - resume_started) * 1000),
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
        session.outcome = "cancelled"
        self.manager.transition(session, SessionStatus.cancelled)
        if session.goal_release and session.goal_id:
            goal = self.goals.get(session.goal_id)
            if goal and goal.release_session_id == session.session_id:
                goal.status = "paused"
                goal.release_status = "denied"
                goal.last_error = "final delivery target was declined; staged files retained"
                self.goals.save(goal)
        return session
