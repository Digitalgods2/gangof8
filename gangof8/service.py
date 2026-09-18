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
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import (
    assembly,
    browser_acceptance,
    cancellation,
    classifier,
    config,
    executor,
    goals,
    intake,
    intent,
    reporting,
    rounds,
    recovery,
    smoke,
    validation,
)
from .artifacts import parse_proposals
from .adapters.cli import CliAdapter, agy_models, cli_available
from .adapters.mock import MockAdapter
from .adapters.openrouter import OpenRouterAdapter
from .secrets import SecretStore
from .roles import resolve_frontier_authors, separate_authoring_from_lead
from .composer import fallback_final
from .checkpoints import CheckpointStore
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
from .models import (
    ApprovalPolicy,
    BuildRecipe,
    Budgets,
    Complexity,
    CouncilMember,
    FinalAnswer,
    Goal,
    GoalMilestone,
    InputRequest,
    MaterializationMode,
    MaterializationPlan,
    ProposedAction,
    RecoveryState,
    ReviewStatus,
    Risk,
    Role,
    Session,
    SessionStatus,
    TaskType,
    utcnow,
)
from .paths import extract_delivery_target, extract_established_root, prior_deliverable_files
from .registry import AgentCallStopped, AgentError
from .registry import AgentRegistry
from .runtime_diagnostics import collect_runtime_diagnostics
from .seat_health import UNAVAILABLE_STATES, SeatHealth, classify_failure
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
from .seat_profiles import PRODUCTION_SEATS, SeatProfileStore
from .uploads import UploadStore, attachment_context
from .workbench import (
    OutcomeContract,
    Playbook,
    RunEvaluation,
    SteeringCommand,
    WorkbenchStore,
    artifact_manifest as build_artifact_manifest,
    execution_text,
    infer_outcome_contract,
    resolve_artifact,
)
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


def _prune_empty_dirs(root: Path, *, protect: Optional[set] = None,
                      min_age_s: float = 0.0) -> int:
    """Remove empty directories under `root`, deepest first. Returns the count.

    Deleting files leaves their folders behind, so scratch space slowly fills
    with hollow trees that no size-based or age-based sweep notices: they hold
    no bytes and their mtime keeps changing as children are removed. Walking
    bottom-up matters — emptying a leaf is what makes its parent empty, and a
    single top-down pass would miss every parent it had already visited.

    `root` itself is never removed (a live writer expects it to exist), and
    nothing in `protect` is touched.

    `min_age_s` is not a retention policy — it is a safety guard. An empty
    directory is ambiguous: it may be debris, or it may be a folder created
    seconds ago by a writer that has not saved its first file yet. Deleting
    the second kind pulls the ground out from under a running call, so a
    freshly-touched directory is always left alone. Best-effort — never
    raises."""
    import os
    import time as _time

    protect = protect or set()
    cutoff = _time.time() - min_age_s if min_age_s else None
    # Removing a child updates the parent's mtime, which would make the parent
    # look freshly-touched and protect it until the next sweep — so a deep
    # hollow tree would peel only one layer per run. Our own deletions are not
    # evidence that someone is writing here, so they never confer protection.
    emptied_by_us: set = set()
    removed = 0
    try:
        if not root.is_dir():
            return 0
        for dirpath, _dirnames, _files in os.walk(root, topdown=False):
            d = Path(dirpath)
            if d == root or str(d) in protect or d.name in protect:
                continue
            try:
                if (cutoff is not None and _dir_mtime(d) > cutoff
                        and d not in emptied_by_us):
                    continue  # too fresh — a writer may be about to use it
                if next(d.iterdir(), None) is None:
                    d.rmdir()
                    removed += 1
                    emptied_by_us.add(d.parent)
            except OSError:
                continue  # vanished, in use, or not actually empty — leave it
    except OSError:
        pass
    return removed


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
        self.seat_profiles = SeatProfileStore(self._data_dir)
        self.seat_profiles.ensure_many(PRODUCTION_SEATS)
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
        self.store.seat_profiles = self.seat_profiles
        self.manager = SessionManager(self.store)
        self.governance = Governance(self.store)
        self.workspaces = WorkspaceStore(self._data_dir)
        self.uploads = UploadStore(self._data_dir)
        self.secrets = SecretStore(self._data_dir)
        self.goals = goals.GoalStore(self._data_dir)
        self.checkpoints = CheckpointStore(self._data_dir)
        self.store.goal_store = self.goals
        self.store.checkpoints = self.checkpoints
        self.workbench = WorkbenchStore(self._data_dir)
        # The execution loop receives the LogStore rather than this service.
        # Attach the durable workbench so every agent-call checkpoint can see
        # operator steering without mutating a worker's in-memory Session from
        # the API thread.
        self.store.workbench = self.workbench
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
            # Sweep at startup too, not only on submit. Cleanup used to be a
            # side effect of starting new work, so an idle instance never
            # reclaimed anything: the longer it sat, the longer debris stayed.
            # Guarded exactly like the reconcilers, so a throwaway Service or a
            # second launcher can never sweep a live owner's data.
            self._pool.submit(self._gc_sandboxes)
            self._pool.submit(self._gc_cli_scratch)
            self._pool.submit(self._gc_goal_workspaces)

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
            # Each service owns its effective mapping. Returning the config
            # dictionary by reference let a test, embedder, or live remap on
            # one service silently change every service created afterward.
            self.role_agents = dict(config.ROLE_AGENTS_BY_BACKEND[self.backend])

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
        self.registry.seat_profiles = self.seat_profiles
        # Shared per-seat health: fed by every registry call outcome and
        # consulted by scheduling so hard-unavailable seats (quota, auth,
        # offline) are routed around instead of retried into the ground.
        self.seat_health = SeatHealth()
        self.registry.health = self.seat_health
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
            cli_resources = set(self.role_agents.values())
            if not self._explicit_role_agents:
                # Local seats are resources in their own right. Register an
                # enabled CLI even when no named specialist role maps to it so
                # full-council collaboration cannot silently lose that model.
                cli_resources.update(
                    config.PANEL_SEATS_BY_BACKEND.get("cli", [])
                )
            for agent in sorted(cli_resources):
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

    def _frontier_seats(self) -> list[str]:
        """Frontier-class seats for the CURRENTLY enabled roster.

        Reads through ``resolve_frontier_authors`` rather than
        ``config.FRONTIER_AUTHOR_SEATS`` directly so that disabling claude and
        codex re-points the privileged author/verifier role at the models that
        remain, instead of leaving it unfilled and silently skipping the work
        it guards.
        """
        return resolve_frontier_authors(self._enabled_role_fallbacks())

    def _apply_seat_disables(self, base: dict) -> dict:
        """Move roles from disabled seats onto the enabled roster round-robin,
        then keep the inherited authoring roles off the lead's own seat.

        If every seat is disabled there is intentionally no invented fallback:
        disabled adapters remain unregistered and task submission fails clearly
        until the user enables at least one model.

        The round-robin alone divides roles EVENLY but not USEFULLY: it can put
        the lead and the authoring talent on the same seat, and a lead that
        delegates to itself is one model doing the whole run. See
        ``roles.separate_authoring_from_lead``.
        """
        disabled = {agent for agent in set(base.values()) if not self._seat_enabled(agent)}
        if not disabled:
            return dict(base)
        pool = self._enabled_role_fallbacks()
        if not pool:
            return dict(base)
        out = dict(base)
        inherited = set()
        i = 0
        for role, agent in base.items():
            if agent in disabled:
                out[role] = pool[i % len(pool)]
                inherited.add(role)
                i += 1
        return separate_authoring_from_lead(out, pool, inherited)

    def _effective_panel(self) -> list[str]:
        """The seats that contribute every round. Explicit ctor arg › settings
        roster › backend default. Degrades gracefully: no OpenRouter key ⇒
        CLI-only; a seat with no registered adapter is dropped.

        The backend default is governed by config.PANEL_MODE
        (ARCHITECTURE-REVIEW.md, Phase 1): "duo" convenes a lead author plus
        one independent frontier reviewer; "council" convenes every
        configured seat plus enabled OpenRouter seats. An explicit
        settings.panel_seats roster is the user's choice and always wins.

        Turning a frontier CLI off HANDS ITS SEAT OVER to an enabled OpenRouter
        model — it does not shrink the council. Role assignment already
        inherits this way (``_apply_seat_disables``); without the same backfill
        here the two disagreed: disabling claude and codex left a role map
        spanning five models but a panel of one, so every round ran solo on the
        last frontier seat standing while four enabled, registered, paid-for
        OpenRouter seats sat idle. Disabling every CLI must leave a working
        council, not an empty one.
        """
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
            seats = [s for s in seats if cli_available(s) and s not in disabled]
            if config.PANEL_MODE == "council" and self.secrets.has("openrouter"):
                seats += sorted(
                    n for n, on in (self.settings.openrouter_enabled or {}).items()
                    if on and self._openrouter_slug(n)
                )
            elif self.secrets.has("openrouter"):
                # DUO_PANEL_SIZE is the single dial for how many seats a duo
                # convenes and therefore for what it costs. Backfill fills the
                # gap up to it and the trim below caps at the same number, so
                # the two can never disagree: a full frontier roster is
                # untouched at the default, and raising the dial adds OpenRouter
                # seats rather than silently doing nothing.
                for seat in self._openrouter_fallbacks():
                    if len(seats) >= config.DUO_PANEL_SIZE:
                        break
                    if seat not in seats:
                        seats.append(seat)
        if config.PANEL_MODE != "council":
            seats = seats[:config.DUO_PANEL_SIZE]
        return [s for s in seats if s in self.registry.names()]

    def _default_build_roster(self) -> list[str]:
        """Frontier seats author goals by default (ARCHITECTURE-REVIEW.md P2).

        Measured across two build goals, the budget OpenRouter seats
        introduced most seam defects while the frontier seats spent their
        calls detecting, attributing, and repairing them — capability
        converted into supervision burden. Budget seats join a goal only via
        GANGOF8_GOAL_FULL_ROSTER=1 or an explicit per-goal roster.
        """
        if config.GOAL_FULL_ROSTER:
            roster = list(self.panel)
        else:
            frontier = [
                seat for seat in self.panel
                if seat not in config.OPENROUTER_SEATS
            ]
            roster = frontier or list(self.panel)

        # The Settings role map is an ownership preference, not merely the
        # model used if a planner happens to delegate to CODE GENERATOR. Put
        # that enabled seat first so the architect sees the same priority the
        # deterministic normalizer enforces below. Keep every other seat in
        # its existing order for additional packages and failover.
        preferred_coder = self.role_agents.get(Role.code_generator)
        if preferred_coder in roster:
            roster = [preferred_coder, *(
                seat for seat in roster if seat != preferred_coder
            )]
        return roster

    def _effective_resource_roster(self) -> list[str]:
        """Every enabled, registered model available to collaborate on goals.

        This is intentionally independent of both ``panel_seats`` and role
        mappings. An enabled resource such as DeepSeek must not disappear just
        because no specialist role currently points at it, and duo mode must
        not silently turn a seven-model installation into a two-model build.
        The roster is frozen onto each goal so settings changes cannot alter a
        run halfway through it.
        """
        preferred_coder = self.role_agents.get(Role.code_generator)
        candidates = [
            preferred_coder,
            *config.PANEL_SEATS_BY_BACKEND.get(self.backend, []),
            *config.OPENROUTER_SEATS,
            *self.panel,
            *self.role_agents.values(),
        ]
        registered = set(self.registry.names())
        return list(dict.fromkeys(
            seat for seat in candidates
            if seat and seat in registered and self._seat_enabled(seat)
        ))

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
                     "cli_models", "cli_timeouts", "cli_enabled", "role_models"}

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

    _EXECUTION_PROFILES = {
        "auto", "focused", "council", "best_of_n", "build_team",
    }
    _ROUTING_POLICY_VERSION = "outcome-router.v2"
    _TERMINAL_ACKNOWLEDGEMENTS = {
        "accept",
        "accepted",
        "acknowledged",
        "agree",
        "all good",
        "approve",
        "approved",
        "done",
        "good",
        "great",
        "i accept",
        "i approve",
        "looks good",
        "looks great",
        "ok",
        "okay",
        "perfect",
        "sounds good",
        "thank you",
        "thanks",
        "that works",
        "works for me",
        "yes",
        "yes thank you",
        "yes thanks",
    }
    _PENDING_ACKNOWLEDGEMENTS = {
        "accept", "agree", "approve", "continue", "go ahead", "ok", "okay",
        "proceed", "resume", "yes",
    }

    def _resolve_pending_acknowledgement(
        self, text: str, *, background: bool, attachments: Optional[list[str]],
    ) -> Optional[tuple[str, Session | Goal]]:
        """Route a bare acknowledgment to one unambiguous pending state."""
        if attachments:
            return None
        normalized = re.sub(r"[\s.!?,;:]+", " ", (text or "").casefold()).strip()
        if normalized not in self._PENDING_ACKNOWLEDGEMENTS:
            return None
        approvals = self.pending_approvals()
        inputs = [item for item in self.pending_inputs()
                  if item.get("purpose") in {"continue_rounds", "integration_decision"}]
        if len(approvals) + len(inputs) == 1:
            if approvals:
                pending = approvals[0]
                session = self.approve(
                    pending["session_id"], pending["approval_id"], True,
                    by="chat_acknowledgement", background=background,
                )
                self.store.log_event(
                    session.session_id, "pending_state_acknowledged",
                    {"kind": "approval", "text": normalized},
                )
                return "session", session
            pending = inputs[0]
            session = self.answer(
                pending["session_id"], pending["input_id"], "yes",
                by="chat_acknowledgement", background=background,
            )
            self.store.log_event(
                session.session_id, "pending_state_acknowledged",
                {"kind": pending.get("purpose"), "text": normalized},
            )
            return "session", session
        if normalized in {"continue", "proceed", "resume", "go ahead"}:
            paused = [goal for goal in self.goals.list() if goal.status == "paused"]
            if len(paused) == 1:
                self.resume_goal(paused[0].goal_id, background=background)
                goal = self.goals.get(paused[0].goal_id) or paused[0]
                self._sys_log("pending_state_acknowledged",
                    {"goal_id": goal.goal_id, "kind": "resume",
                     "text": normalized},
                )
                return "goal", goal
        waiting = len(approvals) + len(inputs)
        if not waiting:
            # "Does not identify exactly one" read as if the user had picked the
            # wrong one, when there was nothing to acknowledge at all.
            raise ValueError(
                f"nothing is waiting for your approval or answer, so "
                f"\"{normalized}\" had nothing to act on; send a concrete "
                "instruction to start new work"
            )
        raise ValueError(
            f"{waiting} items are waiting for your approval or answer, so "
            f"\"{normalized}\" is ambiguous; open the one you mean or add a "
            "concrete instruction"
        )

    def _sys_log(self, event: str, payload: Optional[dict] = None) -> None:
        """Log an event that belongs to no single session.

        These used to be written under the literal session id "-", which
        produced a `data/sessions/-.jsonl` holding goal lifecycle events next to
        sandbox garbage collection — attached to a session that does not exist,
        and unreachable from the goal they describe. Goal events now land in
        that goal's own log; the rest go to one service log.
        """
        goal_id = (payload or {}).get("goal_id")
        self.store.log_event(
            f"goal-{goal_id}" if goal_id else "_service", event, payload or {})

    def _execution_profile(self, value: Optional[str]) -> str:
        """Resolve a requested profile, falling back to the configured default.

        An unspecified profile used to mean "auto" unconditionally, and auto
        scores `focused` above `council` for any request under 60 words — so a
        multi-model installation ran single-model on nearly everything without
        anyone choosing that. ``settings.default_execution_profile`` makes it a
        decision. An explicit per-task profile still wins.
        """
        cls = type(self)
        requested = (value or "").strip()
        if not requested or requested.lower() == "auto":
            requested = getattr(
                self.settings, "default_execution_profile", "auto") or "auto"
        profile = requested.strip().lower().replace("-", "_")
        aliases = {
            "full_council": "council",
            "parallel_candidates": "best_of_n",
            "best_of_all": "best_of_n",
            "tournament": "best_of_n",
            "team": "build_team",
            "build": "build_team",
        }
        profile = aliases.get(profile, profile)
        if profile not in cls._EXECUTION_PROFILES:
            raise ValueError(
                "execution_profile must be auto, focused, council, best_of_n, "
                "or build_team"
            )
        return profile

    @classmethod
    def is_terminal_acknowledgement(
        cls,
        text: str,
        *,
        attachments: Optional[list[str]] = None,
        artifact_id: Optional[str] = None,
    ) -> bool:
        """True when a response only accepts an already completed result.

        This is intentionally exact and conservative. A phrase such as
        ``accept, but fix the date`` must remain an actionable follow-up, while
        punctuation and harmless whitespace around ``accept`` should not
        convene another council.
        """
        if attachments or artifact_id:
            return False
        normalized = re.sub(
            r"[\s.!?,;:]+",
            " ",
            (text or "").strip().casefold(),
        ).strip()
        return normalized in cls._TERMINAL_ACKNOWLEDGEMENTS

    def _outcome_contract(
        self,
        text: str,
        supplied: Optional[dict] = None,
        *,
        has_attachments: bool = False,
    ) -> OutcomeContract:
        """Merge an edited contract over deterministic intake inference.

        The preview is editable, so callers may send either the complete object
        they received or a small patch. Unknown keys are rejected by the
        Pydantic contract model instead of silently becoming prompt content.
        """
        inferred = infer_outcome_contract(
            text,
            role_agents=self.role_agents,
            has_attachments=has_attachments,
        ).model_dump()
        if supplied:
            unknown = sorted(set(supplied) - set(OutcomeContract.model_fields))
            if unknown:
                raise ValueError(
                    "unknown outcome contract field(s): " + ", ".join(unknown)
                )
            inferred.update(supplied)
        contract = OutcomeContract.model_validate(inferred)
        if not contract.outcome.strip():
            raise ValueError("outcome contract requires a concrete outcome")
        return contract

    def _evaluation_routing_evidence(self, task_type: str) -> dict[str, dict]:
        """Summarize only statistically useful, explicit user evaluations."""
        grouped: dict[str, list[RunEvaluation]] = {}
        try:
            evaluations = self.workbench.list_evaluations(limit=200)
        except Exception:  # best-effort recommendation; intake must still work
            return {}
        for evaluation in evaluations:
            metadata = evaluation.metadata or {}
            if metadata.get("task_type") != task_type:
                continue
            strategy = str(metadata.get("strategy") or "").strip()
            if strategy not in {
                "focused", "council", "best_of_n", "build_team",
            }:
                continue
            grouped.setdefault(strategy, []).append(evaluation)
        evidence: dict[str, dict] = {}
        for strategy, rows in grouped.items():
            if len(rows) < 3:
                continue
            positive = 0
            ratings: list[int] = []
            for row in rows:
                verdict = (row.verdict or "").strip().lower()
                row_positive = verdict in {
                    "success", "satisfied", "accepted", "useful"
                }
                if row.rating is not None:
                    ratings.append(int(row.rating))
                    if int(row.rating) >= 4 and verdict not in {
                        "failed", "rejected", "unsatisfied"
                    }:
                        row_positive = True
                if row_positive:
                    positive += 1
            success_rate = positive / len(rows)
            average_rating = (
                round(sum(ratings) / len(ratings), 2) if ratings else None
            )
            evidence[strategy] = {
                "sample_size": len(rows),
                "success_rate": round(success_rate, 3),
                "average_rating": average_rating,
                "score_adjustment": round((success_rate - 0.5) * 20, 2),
            }
        return evidence

    def _routing_decision(
        self,
        text: str,
        contract: OutcomeContract,
        execution_profile: str,
        *,
        has_attachments: bool,
        require_eligible: bool = True,
    ) -> dict:
        profile = self._execution_profile(execution_profile)
        classification = classifier.classify(text, self.role_agents)
        substantial_build = goals.should_auto_route(
            text, has_attachments=has_attachments
        )
        candidates = {
            "focused": {
                "route": "focused",
                "eligible": True,
                "score": 55.0,
                "rationale": [
                    "one lead with specialists pulled in only when needed"
                ],
            },
            "council": {
                "route": "council",
                "eligible": True,
                "score": 48.0,
                "rationale": [
                    "independent model perspectives and explicit synthesis"
                ],
            },
            "best_of_n": {
                "route": "best_of_n",
                "eligible": bool(classification.produces_output),
                "score": 32.0,
                "rationale": [
                    "every enabled model attempts one complete candidate; "
                    "runnable candidates are compared blindly"
                ],
            },
            "build_team": {
                "route": "build_team",
                "eligible": bool(
                    classification.produces_output and not has_attachments
                ),
                "score": 20.0,
                "rationale": [
                    "owned parallel packages with one verified final release"
                ],
            },
        }
        if classification.complexity.value == "trivial":
            candidates["focused"]["score"] += 30
            candidates["focused"]["rationale"].append(
                "the request is compact enough for a lean path"
            )
        elif classification.complexity.value == "complex":
            candidates["council"]["score"] += 24
            candidates["council"]["rationale"].append(
                "complexity benefits from independent challenge"
            )
        else:
            candidates["focused"]["score"] += 8
            candidates["council"]["score"] += 8
        if classification.risk.value in {"medium", "high"}:
            candidates["council"]["score"] += 12
            candidates["council"]["rationale"].append(
                "risk benefits from adversarial review"
            )
        if substantial_build:
            candidates["build_team"]["score"] += 78
            candidates["build_team"]["rationale"].append(
                "the brief spans a substantial multi-surface build"
            )
        if has_attachments:
            candidates["build_team"]["rationale"].append(
                "attached source stays in a single contract-aware session"
            )

        evidence = self._evaluation_routing_evidence(
            classification.task_type.value
        )
        for route, historical in evidence.items():
            candidates[route]["score"] += historical["score_adjustment"]
            candidates[route]["rationale"].append(
                f"{historical['sample_size']} comparable rated runs informed "
                "the recommendation"
            )

        if profile != "auto":
            if not candidates[profile]["eligible"] and require_eligible:
                raise ValueError(
                    f"execution profile '{profile}' is not available for this task"
                )
            selected = profile
            if candidates[profile]["eligible"]:
                reason = "selected explicitly by the user"
            else:
                # The caller owns this route by construction (a /goal build is a
                # build team whatever the classifier makes of the brief). Keep
                # the route but record that scoring would not have offered it.
                candidates[profile]["rationale"].append(
                    "route fixed by the caller; scoring did not rank it eligible"
                )
                reason = "fixed by the caller despite scoring it ineligible"
        else:
            selected = max(
                (
                    item for item in candidates.values()
                    if item["eligible"]
                ),
                key=lambda item: (item["score"], item["route"]),
            )["route"]
            reason = "; ".join(candidates[selected]["rationale"])
        ordered_candidates = sorted(
            candidates.values(), key=lambda item: item["score"], reverse=True
        )
        return {
            "policy_version": self._ROUTING_POLICY_VERSION,
            "requested_profile": profile,
            "selected_route": selected,
            "recommended_profile": selected,
            "reason": reason,
            "task_type": classification.task_type.value,
            "complexity": classification.complexity.value,
            "risk": classification.risk.value,
            "historical_evidence": evidence,
            "candidates": ordered_candidates,
            "alternatives": [
                {
                    "route": item["route"],
                    "eligible": item["eligible"],
                    "score": round(item["score"], 2),
                    "reason": "; ".join(item["rationale"]),
                }
                for item in ordered_candidates
                if item["route"] != selected
            ],
        }

    def preview_task(
        self,
        text: str,
        *,
        source: str = "api",
        attachments: Optional[list[str]] = None,
        outcome_contract: Optional[dict] = None,
        execution_profile: str = "auto",
        approval_policy: ApprovalPolicy | str = ApprovalPolicy.manual,
    ) -> dict:
        """Return the editable contract and explainable route without model calls."""
        raw = (text or "").strip()
        if not raw and not attachments:
            raise ValueError("task text or an attachment is required")
        raw = raw or "(see attached)"
        attachment_ids = attachments or []
        full_text = raw + attachment_context(self.uploads, attachment_ids)
        contract = self._outcome_contract(
            full_text,
            outcome_contract,
            has_attachments=bool(attachment_ids),
        )
        profile = self._execution_profile(execution_profile)
        routing = self._routing_decision(
            full_text,
            contract,
            profile,
            has_attachments=bool(attachment_ids),
        )
        contract = contract.model_copy(
            update={
                "execution_profile": profile,
                "execution_mode": routing["selected_route"],
                "auto_routed": (
                    profile == "auto"
                    and routing["selected_route"] == "build_team"
                ),
            }
        )
        return {
            "source": source,
            "outcome_contract": contract.model_dump(),
            "execution_profile": profile,
            "approval_policy": ApprovalPolicy(approval_policy).value,
            "recommended_profile": routing["selected_route"],
            "routing_decision": routing,
            "classification": {
                "task_type": routing["task_type"],
                "complexity": routing["complexity"],
                "risk": routing["risk"],
            },
        }

    def start_task(
        self,
        text: str,
        *,
        source: str = "api",
        background: bool = False,
        attachments: Optional[list[str]] = None,
        outcome_contract: Optional[dict] = None,
        execution_profile: str = "auto",
        playbook_id: Optional[str] = None,
        parent_session_id: Optional[str] = None,
        approval_policy: ApprovalPolicy | str = ApprovalPolicy.manual,
    ) -> tuple[str, Session | Goal]:
        """Central intake router used by the API, clones, and playbooks."""
        resolved = self._resolve_pending_acknowledgement(
            text, background=background, attachments=attachments)
        if resolved is not None:
            return resolved
        preview = self.preview_task(
            text,
            source=source,
            attachments=attachments,
            outcome_contract=outcome_contract,
            execution_profile=execution_profile,
            approval_policy=approval_policy,
        )
        route = preview["routing_decision"]["selected_route"]
        contract = preview["outcome_contract"]
        if route == "build_team":
            goal = self.create_goal(
                text,
                background=background,
                outcome_contract=contract,
                execution_profile=execution_profile,
                playbook_id=playbook_id,
                routing_decision=preview["routing_decision"],
                approval_policy=approval_policy,
            )
            return "goal", goal
        runner = self.submit_background if background else self.run
        session = runner(
            text,
            source=source,
            attachments=attachments,
            outcome_contract=contract,
            execution_profile=execution_profile,
            routing_decision=preview["routing_decision"],
            playbook_id=playbook_id,
            parent_session_id=parent_session_id,
            approval_policy=approval_policy,
        )
        return "session", session

    def _open(self, text: str, source: str, budgets: Optional[Budgets],
              attachments: Optional[list[str]] = None,
              outcome_contract: Optional[dict] = None,
              execution_profile: str = "auto",
              routing_decision: Optional[dict] = None,
              playbook_id: Optional[str] = None,
              parent_session_id: Optional[str] = None,
              approval_policy: ApprovalPolicy | str = ApprovalPolicy.manual) -> Session:
        """Create a session, stamping the backend, the active workspace root
        (so file skills operate in that project; None ⇒ per-session sandbox),
        and folding any attachment text into the task the council reads."""
        if self.backend == "cli" and not self.registry.names():
            raise ValueError(
                "no AI models are enabled; enable at least one model before starting a task")
        raw_text = (text or "").strip()
        if not raw_text and attachments:
            raw_text = "(see attached)"
        full_text = raw_text + attachment_context(self.uploads, attachments or [])
        contract = self._outcome_contract(
            full_text,
            outcome_contract,
            has_attachments=bool(attachments),
        )
        profile = self._execution_profile(execution_profile)
        routing = routing_decision or self._routing_decision(
            full_text,
            contract,
            profile,
            has_attachments=bool(attachments),
        )
        selected_route = routing.get("selected_route") or "focused"
        contract_budget = None
        if budgets is None and contract.budgets:
            contract_budget = Budgets.model_validate(contract.budgets)
        session = intake.receive(
            full_text, source, self.manager, budgets or contract_budget
        )
        session.task.original_text = raw_text
        session.outcome_contract = contract.model_copy(
            update={
                "execution_profile": profile,
                "execution_mode": selected_route,
            }
        ).model_dump()
        session.execution_profile = profile
        session.approval_policy = ApprovalPolicy(approval_policy)
        session.routing_decision = dict(routing)
        session.playbook_id = playbook_id
        session.parent_session_id = parent_session_id
        session.backend = self.backend
        # Focused is lead-driven; council preserves the configured discussion
        # panel. Best-of-all is different: its product promise is one independent
        # candidate attempt from EVERY enabled model, even when the ordinary
        # panel is intentionally configured as a two-seat duo.
        if selected_route == "focused":
            session.panel = []
        elif selected_route == "best_of_n":
            session.panel = self._effective_resource_roster()
        else:
            session.panel = list(self.panel)
        # Frontier-class membership follows the ENABLED roster, not a fixed list
        # of vendor names: switching claude and codex off hands the role to the
        # models that are actually running instead of leaving it unfilled.
        session.frontier_author_seats = resolve_frontier_authors(
            self._enabled_role_fallbacks())
        # A blind tournament has no privileged author quorum: every enabled seat
        # gets the same candidate contract and runnable files advance on merit.
        # The panel filter is load-bearing: this list is the AUTHOR QUORUM (a
        # seat that must produce a candidate), not the inspector list, and a
        # seat that is not participating cannot be required to author.
        session.required_frontier_authors = (
            [] if selected_route == "best_of_n" else [
                seat for seat in session.frontier_author_seats
                if seat in session.panel
            ]
        )
        session.cli_timeouts = dict(self.settings.cli_timeouts or {})
        # Best-of-all has one explicit promise: select the strongest validated
        # candidate. Do not turn that winner into a separate merged alternative.
        session.integration_review_enabled = bool(
            self.settings.integration_review_enabled
            and selected_route != "best_of_n"
        )
        active = self.workspaces.active()
        session.workspace_root = active.root if active else None
        # Established folder is PER TASK: interpret a path the user referenced in
        # the prompt (a file → its parent). None ⇒ the greenfield gate may ask.
        session.established_root = (
            contract.established_root or extract_established_root(raw_text)
        )
        # An explicit "save it in <X>" destination (distinct from a read source
        # the task also names) — promote delivers HERE, so "read from A, save to
        # B" lands in B and never overwrites A.
        session.delivery_root = (
            contract.delivery_root or extract_delivery_target(raw_text)
        )
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
        self.store.log_event(
            session.session_id,
            "outcome_contract_frozen",
            {
                "version": contract.version,
                "outcome": contract.outcome[:300],
                "profile": profile,
                "route": selected_route,
                "playbook_id": playbook_id,
            },
        )
        self.store.log_event(
            session.session_id, "routing_decided", session.routing_decision
        )
        self.store.save_session(session)
        # Sweep old scratch sandboxes so they don't pile up forever. Background so
        # it never delays starting the run; the new session is already active and
        # therefore protected.
        self._pool.submit(self._gc_sandboxes)
        self._pool.submit(self._gc_cli_scratch)
        self._pool.submit(self._gc_goal_workspaces)
        return session

    def _gc_sandboxes(self, keep: Optional[int] = None) -> dict:
        """Delete old per-session sandbox scratch folders so they don't accumulate
        without bound. KEEPS: every sandbox belonging to a still-active/paused
        session (its files may still be needed to resume or promote), plus the
        `keep` most-recently-touched of the rest (so recent runs stay openable in
        the dashboard). Only session sandboxes; the CLI scratch root is swept by
        _gc_cli_scratch. Best-effort — never raises.

        A session's quarantined ungoverned writes live inside its sandbox, so
        they are retired with it and need no separate bound."""
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
                    # Retained for inspection — but the tree inside it still
                    # collects empty folders that no size or age rule notices.
                    # The session is finished, so nothing is writing here.
                    removed += _prune_empty_dirs(d)
                    continue
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        except OSError:
            pass
        if removed:
            self._sys_log("sandboxes_gc", {"removed": removed, "kept": keep})
        return {"removed": removed}

    def _gc_goal_workspaces(self, grace_hours: Optional[float] = None) -> dict:
        """Delete per-goal staging directories whose goal no longer exists.

        Every other scratch space here has a bound — sandboxes keep the newest
        SANDBOX_KEEP, CLI scratch expires by age. Goal staging had none, and
        clearing history removes the goal ROW while leaving its directory on
        disk, so orphans accumulated permanently (21 dirs / 4.5 MB observed).

        A directory is removed only when no goal row owns it. Live goals are
        never touched whatever their age or size, and a grace window protects
        a goal whose row is being written right now, so this can never delete
        staging out from under a starting run. Best-effort — never raises.

        `grace_hours=0` skips that wait, for the one caller that has already
        deleted every goal row on purpose: after "clear history" there is no
        row left to settle, and honouring the window there would strand the
        staging of the run the user just cleared."""
        import shutil
        import time as _time

        root = self._data_dir / "goal-workspaces"
        removed = 0
        try:
            if not root.is_dir():
                return {"removed": 0}
            live = {g.goal_id for g in self.goals.list()}
            grace = (config.GOAL_WORKSPACE_GRACE_HOURS
                     if grace_hours is None else grace_hours)
            cutoff = _time.time() - grace * 3600
            for entry in root.iterdir():
                if not entry.is_dir() or entry.name in live:
                    continue
                if grace and _dir_mtime(entry) > cutoff:
                    continue  # too fresh to be sure its goal row is settled
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
            # A live goal keeps its directory, but the tree under it still
            # collects empty folders as packages are staged and promoted.
            # Its own dir and its `stage` root are protected — a running
            # package expects both to exist.
            for goal_id in live:
                goal_dir = root / goal_id
                if goal_dir.is_dir():
                    removed += _prune_empty_dirs(
                        goal_dir, protect={"stage"}, min_age_s=grace * 3600)
        except OSError:
            pass
        if removed:
            self._sys_log("goal_workspaces_gc", {"removed": removed})
        return {"removed": removed}

    def _gc_cli_scratch(self) -> dict:
        """Sweep SANDBOX_ROOT/cli-neutral, the root every local CLI seat runs in.

        Three things collect there and none of them used to be cleaned:

        - `call-*` — one per agent call. Normally removed the moment the call
          ends; a killed server or a hard crash strands them. Swept once they
          are older than CLI_SCRATCH_MAX_AGE_HOURS and no live call owns them.
        - `_ungoverned` — writes that could not be attributed to a session.
          Bounded to the newest UNGOVERNED_ORPHAN_KEEP.
        - anything else — legacy debris from when every call shared this one
          directory, plus whatever a seat drops straight into the root.

        Never deletes a directory a running call still owns, whatever its age:
        a call has no fixed duration, so an age threshold alone would eventually
        delete live scratch out from under a working seat. Best-effort."""
        import shutil
        import time as _time

        from .adapters.cli import live_call_dirs

        root = config.SANDBOX_ROOT / "cli-neutral"
        removed = 0
        try:
            if not root.is_dir():
                return {"removed": 0}
            live = live_call_dirs()
            cutoff = _time.time() - config.CLI_SCRATCH_MAX_AGE_HOURS * 3600
            keep_orphans = config.UNGOVERNED_ORPHAN_KEEP
            for entry in root.iterdir():
                if str(entry) in live:
                    continue
                if entry.name == "_ungoverned":
                    if not entry.is_dir():
                        continue
                    kids = sorted((k for k in entry.iterdir()),
                                  key=_dir_mtime, reverse=True)
                    for stale in kids[keep_orphans:]:
                        shutil.rmtree(stale, ignore_errors=True) if stale.is_dir()                             else stale.unlink(missing_ok=True)
                        removed += 1
                    continue
                if config.CLI_SCRATCH_MAX_AGE_HOURS and _dir_mtime(entry) > cutoff:
                    continue  # recent — a call may be about to use it
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
                removed += 1
            # No empty-directory sweep belongs here. Every entry under this
            # root is already governed: stale ones are deleted above whether
            # or not they hold files, recent ones must survive because a call
            # may be about to write into them, and `_ungoverned` children are
            # bounded by UNGOVERNED_ORPHAN_KEEP rather than by emptiness.
            # A blanket prune would override all three.
        except OSError:
            pass
        if removed:
            self._sys_log("cli_scratch_gc", {"removed": removed})
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
                                    timeout_s=0)
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
            attachments: Optional[list[str]] = None,
            outcome_contract: Optional[dict] = None,
            execution_profile: str = "auto",
            routing_decision: Optional[dict] = None,
            playbook_id: Optional[str] = None,
            parent_session_id: Optional[str] = None,
            approval_policy: ApprovalPolicy | str = ApprovalPolicy.manual) -> Session:
        session = self._open(
            text, source, budgets, attachments,
            outcome_contract=outcome_contract,
            execution_profile=execution_profile,
            routing_decision=routing_decision,
            playbook_id=playbook_id,
            parent_session_id=parent_session_id,
            approval_policy=approval_policy,
        )
        return self._run_owned(session, self._run_full, background=False)

    def submit_background(self, text: str, source: str = "api",
                          budgets: Optional[Budgets] = None,
                          attachments: Optional[list[str]] = None,
                          outcome_contract: Optional[dict] = None,
                          execution_profile: str = "auto",
                          routing_decision: Optional[dict] = None,
                          playbook_id: Optional[str] = None,
                          parent_session_id: Optional[str] = None,
                          approval_policy: ApprovalPolicy | str = ApprovalPolicy.manual) -> Session:
        """Create the session and run it on a worker thread; the caller polls
        GET /sessions/{id} for progress."""
        session = self._open(
            text, source, budgets, attachments,
            outcome_contract=outcome_contract,
            execution_profile=execution_profile,
            routing_decision=routing_decision,
            playbook_id=playbook_id,
            parent_session_id=parent_session_id,
            approval_policy=approval_policy,
        )
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
        root = Path(ws.root).resolve()
        app_root = Path(__file__).resolve().parent.parent
        data_root = self._data_dir.resolve()
        home_root = Path.home().resolve()
        anchor = Path(root.anchor).resolve()
        if (
            root in {anchor, home_root, app_root, data_root}
            or root in app_root.parents
            or root in data_root.parents
            or (root / ".git").exists()
        ):
            raise WorkspaceError(
                "refusing to empty a filesystem, home, application, data, or Git root"
            )
        removed = 0
        if root.is_dir():
            for child in root.iterdir():
                if child.is_symlink():
                    child.unlink()
                elif child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink()
                removed += 1
        self._sys_log("workspace_emptied", {"id": ws.id, "removed": removed})
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
            detail = f"{type(e).__name__}: {e}"
            session.stop_reason = f"orchestrator internal error: {detail}"
            session.quality_gate = {
                "verdict": "FAIL",
                "stage": "orchestrator_internal_error",
                "detail": detail,
            }
            try:
                recovery.record_failure(
                    session,
                    stage="orchestrator",
                    category="internal_error",
                    summary=detail,
                    evidence={"traceback": traceback.format_exc(limit=12)[-6000:]},
                    responsible_owner="coordinator",
                    recoverable=False,
                )
            except Exception as ledger_error:  # noqa: BLE001 - preserve root cause
                session.unresolved.append(
                    f"failure ledger also failed: {type(ledger_error).__name__}: "
                    f"{ledger_error}"
                )
            self.store.log_event(
                session.session_id,
                "internal_error",
                {
                    "detail": detail,
                    "traceback": traceback.format_exc(limit=12)[-6000:],
                    "failure_layer": "orchestrator",
                },
            )
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
            # The gemini seat runs on the Antigravity CLI first, and it
            # refuses ids it does not list, so its own ids lead the dropdown.
            # The API fallback strips their effort suffix (gemini-3.1-pro-high).
            agy = agy_models()
            if agy:
                catalog["gemini"] = agy + [
                    m for m in catalog.get("gemini", []) if m not in agy]
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
        catalog = self.cli_model_catalog(refresh=refresh)
        ce = self.settings.cli_enabled or {}
        cli = [
            {"name": a, "available": cli_available(a), "kind": "cli", "label": a,
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
            best_of_all_roster=self._effective_resource_roster(),
            role_agents=self.role_agents,
            frontier_seats=self._frontier_seats(),
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

    @staticmethod
    def _record_dict(record) -> dict:
        return record.model_dump() if hasattr(record, "model_dump") else dict(record)

    @classmethod
    def _steering_dict(cls, command) -> dict:
        data = cls._record_dict(command)
        if command.kind in {"constraint", "focus"}:
            data["payload"] = {"text": command.directive}
        elif command.kind == "increase_budget":
            try:
                data["payload"] = json.loads(command.directive or "{}")
            except (json.JSONDecodeError, TypeError):
                data["payload"] = {"agent_calls": command.amount}
        else:
            data["payload"] = {}
        return data

    def clone_session(
        self, session_id: str, *, run: bool = False, background: bool = True
    ) -> dict:
        """Clone intent, never mutable run state, approvals, or artifact leases."""
        session = self.manager.load(session_id)
        if session is None:
            raise KeyError(session_id)
        text = (
            session.task.original_text
            or session.task.text.split(
                "\n\nAttachments provided by the user:", 1
            )[0].strip()
        )
        attachment_records = [
            dict(item)
            for item in session.attachments
            if item.get("id") and self.uploads.get(str(item.get("id")))
        ]
        attachment_ids = [str(item["id"]) for item in attachment_records]
        template = {
            "text": text,
            "execution_profile": session.execution_profile,
            "outcome_contract": session.outcome_contract,
            "attachments": attachment_records,
            "source_session_id": session_id,
        }
        if not run:
            return {"template": template}
        kind, item = self.start_task(
            text,
            source="clone",
            background=background,
            attachments=attachment_ids,
            outcome_contract=session.outcome_contract,
            execution_profile=session.execution_profile,
            playbook_id=session.playbook_id,
            parent_session_id=session_id,
        )
        if kind == "goal":
            payload = self.get_goal(item.goal_id) or item.model_dump()
        else:
            payload = {
                "session_id": item.session_id,
                "status": item.status.value,
                "outcome_contract": item.outcome_contract,
                "execution_profile": item.execution_profile,
                "routing_decision": item.routing_decision,
            }
        payload["kind"] = kind
        return payload

    def artifact_manifest(self, session_id: str) -> dict:
        session = self.manager.load(session_id)
        if session is None:
            raise KeyError(session_id)
        return build_artifact_manifest(session)

    def _artifact_item(self, session: Session, artifact_id: str) -> tuple[dict, Path]:
        manifest = build_artifact_manifest(session)
        item = next(
            (
                candidate
                for candidate in manifest.get("artifacts", [])
                if candidate.get("artifact_id") == artifact_id
            ),
            None,
        )
        if item is None:
            raise KeyError(artifact_id)
        try:
            path = resolve_artifact(session, artifact_id)
        except (FileNotFoundError, PermissionError, ValueError):
            raise KeyError(artifact_id) from None
        if not path.is_file():
            raise KeyError(artifact_id)
        return item, path

    def preview_artifact(self, session_id: str, artifact_id: str) -> dict:
        """Return bounded inert text/HTML, or a safe raster image path."""
        session = self.manager.load(session_id)
        if session is None:
            raise KeyError(session_id)
        item, path = self._artifact_item(session, artifact_id)
        media_type = str(item.get("media_type") or "application/octet-stream")
        kind = str(item.get("kind") or "binary")
        if media_type in {
            "image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"
        }:
            return {
                "kind": "image",
                "media_type": media_type,
                "file_path": str(path),
            }
        if media_type == "image/svg+xml":
            # SVG is active XML rather than a passive raster image. Show its
            # source as inert text; never hand it to an unrestricted document
            # context as though it were PNG/JPEG.
            kind = "code"
        if kind not in {"text", "html", "markdown", "code", "data", "table"}:
            return {
                "kind": "binary",
                "media_type": media_type,
                "content": "",
                "truncated": False,
                "message": "This artifact is available for download but has no inline preview.",
            }
        max_bytes = 250_000
        with path.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
        clipped = raw[:max_bytes]
        return {
            "kind": kind,
            "media_type": media_type,
            "content": clipped.decode("utf-8", errors="replace"),
            "truncated": len(raw) > len(clipped),
        }

    def download_artifact(self, session_id: str, artifact_id: str) -> dict:
        session = self.manager.load(session_id)
        if session is None:
            raise KeyError(session_id)
        item, path = self._artifact_item(session, artifact_id)
        return {
            "name": item.get("name") or path.name,
            "media_type": item.get("media_type") or "application/octet-stream",
            "file_path": str(path),
        }

    def list_steering_commands(self, session_id: str) -> list[dict]:
        if self.manager.load(session_id) is None:
            raise KeyError(session_id)
        return [
            self._steering_dict(command)
            for command in self.workbench.list_steering(
                session_id=session_id, include_inactive=True
            )
        ]

    def add_steering_command(
        self, session_id: str, kind: str, payload: Optional[dict] = None
    ) -> dict:
        session = self.manager.load(session_id)
        if session is None:
            raise KeyError(session_id)
        if session.status in {
            SessionStatus.done, SessionStatus.failed, SessionStatus.cancelled
        }:
            raise ValueError("steering is only available while a session is active")
        normalized = (kind or "").strip().lower()
        values = payload or {}
        if normalized in {"constraint", "focus"}:
            directive = str(values.get("text") or "").strip()
            if not directive:
                raise ValueError(f"{normalized} requires non-empty text")
            if len(directive) > 2000:
                raise ValueError("steering text is limited to 2000 characters")
            command = SteeringCommand(
                session_id=session_id,
                kind=normalized,
                directive=directive,
                durable=True,
                status="active",
            )
        elif normalized == "finish_now":
            command = SteeringCommand(
                session_id=session_id,
                kind=normalized,
                durable=False,
                status="pending",
            )
        elif normalized == "increase_budget":
            increments = {
                "agent_calls": max(0, min(int(values.get("agent_calls") or 0), 200)),
                "rounds": max(0, min(int(values.get("rounds") or 0), 20)),
                "duration_seconds": max(
                    0, min(int(values.get("duration_seconds") or 0), 14_400)
                ),
            }
            if not any(increments.values()):
                raise ValueError("increase_budget requires a positive increment")
            command = SteeringCommand(
                session_id=session_id,
                kind=normalized,
                directive=json.dumps(increments, sort_keys=True),
                amount=increments["agent_calls"],
                durable=False,
                status="pending",
            )
        else:
            raise ValueError(
                "kind must be constraint, focus, increase_budget, or finish_now"
            )
        saved = self.workbench.add_steering(command)
        self.store.log_event(
            session_id,
            "steering_added",
            {
                "command_id": saved.command_id,
                "kind": saved.kind,
                "durable": saved.durable,
            },
        )
        return self._steering_dict(saved)

    def revoke_steering_command(self, session_id: str, command_id: str) -> dict:
        command = self.workbench.get_steering(command_id)
        if command is None or command.session_id != session_id:
            raise KeyError(command_id)
        revoked = self.workbench.revoke_steering(command_id)
        if revoked is None:
            raise KeyError(command_id)
        if revoked.status == "applied":
            raise ValueError("command already applied and can no longer be revoked")
        if command.status != "revoked":
            self.store.log_event(
                session_id,
                "steering_revoked",
                {"command_id": command_id, "kind": command.kind},
            )
        return self._steering_dict(revoked)

    def evaluate_session(
        self,
        session_id: str,
        verdict: str,
        *,
        rating: Optional[int] = None,
        notes: str = "",
    ) -> dict:
        session = self.manager.load(session_id)
        if session is None:
            raise KeyError(session_id)
        if session.status not in {
            SessionStatus.done, SessionStatus.failed, SessionStatus.cancelled
        }:
            raise ValueError("evaluate the run after it reaches a terminal outcome")
        normalized = (verdict or "").strip().lower().replace(" ", "_")
        normalized = {
            "partial": "partly_satisfied",
            "failure": "failed",
        }.get(normalized, normalized)
        allowed = {
            "success", "satisfied", "accepted", "useful",
            "mixed", "partly_satisfied", "needs_work",
            "failed", "rejected", "not_satisfied", "unsatisfied",
        }
        if normalized not in allowed:
            raise ValueError(
                "verdict must describe a satisfied, mixed, or unsuccessful result"
            )
        if rating is not None and not 1 <= int(rating) <= 5:
            raise ValueError("rating must be between 1 and 5")
        clean_notes = (notes or "").strip()[:4000]
        existing = self.workbench.get_evaluation(session_id)
        try:
            start = datetime.fromisoformat(session.created_at)
            end = datetime.fromisoformat(session.updated_at)
            elapsed = max(0.0, (end - start).total_seconds())
        except (TypeError, ValueError):
            elapsed = session.agent_attempt_duration_ms / 1000
        manifest = build_artifact_manifest(session)
        promoted = any(
            action.kind in {"promote", "promote_batch"}
            and action.status == "executed"
            for action in session.proposed_actions
        )
        negative = normalized in {
            "failed", "rejected", "not_satisfied", "unsatisfied", "needs_work"
        }
        values = {
            "session_id": session_id,
            "verdict": normalized,
            "rating": int(rating) if rating is not None else None,
            "promoted": promoted,
            "rejection_reason": clean_notes if negative else "",
            "notes": clean_notes,
            "elapsed_seconds": elapsed,
            "model_calls": session.agent_call_attempts,
            "agent_calls": session.agent_calls,
            "artifact_ids": [
                item.get("artifact_id")
                for item in manifest.get("artifacts", [])
                if item.get("artifact_id")
            ],
            "metadata": {
                "task_type": (
                    session.classification.task_type.value
                    if session.classification else
                    session.outcome_contract.get("task_type", "")
                ),
                "complexity": (
                    session.classification.complexity.value
                    if session.classification else
                    session.outcome_contract.get("complexity", "")
                ),
                "strategy": (
                    session.routing_decision.get("selected_route")
                    or session.execution_profile
                ),
                "profile": session.execution_profile,
                "outcome": session.outcome,
                "playbook_id": session.playbook_id,
            },
        }
        if existing is not None:
            values.update({
                "evaluation_id": existing.evaluation_id,
                "created_at": existing.created_at,
            })
        evaluation = RunEvaluation(**values)
        saved = self.workbench.upsert_evaluation(evaluation)
        self.store.log_event(
            session_id,
            "run_evaluated",
            {"verdict": normalized, "rating": rating},
        )
        return self._record_dict(saved)

    def session_evaluation(self, session_id: str) -> Optional[dict]:
        if self.manager.load(session_id) is None:
            raise KeyError(session_id)
        evaluation = self.workbench.get_evaluation(session_id)
        return self._record_dict(evaluation) if evaluation is not None else None

    def _playbook_contract(self, contract: Optional[dict], task_template: str) -> dict:
        cleaned = self._outcome_contract(task_template, contract).model_dump()
        # A reusable procedure cannot silently retain machine-specific project
        # locations from the run that inspired it.
        cleaned["established_root"] = None
        cleaned["delivery_root"] = None
        return cleaned

    def save_playbook(
        self,
        *,
        name: str,
        description: str = "",
        task_template: str = "",
        outcome_contract: Optional[dict] = None,
        execution_profile: str = "auto",
        playbook_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> dict:
        existing = self.workbench.get_playbook(playbook_id) if playbook_id else None
        if playbook_id and existing is None:
            raise KeyError(playbook_id)
        if session_id:
            session = self.manager.load(session_id)
            if session is None:
                raise KeyError(session_id)
            task_template = (
                session.task.original_text
                or session.task.text.split(
                    "\n\nAttachments provided by the user:", 1
                )[0].strip()
            )
            outcome_contract = session.outcome_contract
            execution_profile = session.execution_profile
        clean_name = (name or "").strip()
        clean_template = (task_template or "").split(
            "\n\nAttachments provided by the user:", 1
        )[0].strip()
        if not clean_name:
            raise ValueError("playbook name is required")
        if not clean_template:
            raise ValueError("playbook task template is required")
        profile = self._execution_profile(execution_profile)
        values = {
            "name": clean_name[:120],
            "description": (description or "").strip()[:1000],
            "task_template": clean_template[:20_000],
            "outcome_contract": self._playbook_contract(
                outcome_contract, clean_template
            ),
            "execution_profile": profile,
        }
        if existing is not None:
            values.update({
                "playbook_id": existing.playbook_id,
                "created_at": existing.created_at,
            })
        playbook = Playbook(**values)
        saved = (
            self.workbench.upsert_playbook(playbook)
            if existing is not None
            else self.workbench.create_playbook(playbook)
        )
        return self._record_dict(saved)

    def list_playbooks(self) -> list[dict]:
        return [
            self._record_dict(playbook)
            for playbook in self.workbench.list_playbooks()
        ]

    def delete_playbook(self, playbook_id: str) -> bool:
        return bool(self.workbench.delete_playbook(playbook_id))

    def run_playbook(
        self,
        playbook_id: str,
        *,
        text: Optional[str] = None,
        background: bool = True,
    ) -> dict:
        playbook = self.workbench.get_playbook(playbook_id)
        if playbook is None:
            raise KeyError(playbook_id)
        task_text = (text or "").strip() or playbook.task_template
        kind, item = self.start_task(
            task_text,
            source="playbook",
            background=background,
            outcome_contract=playbook.outcome_contract,
            execution_profile=playbook.execution_profile,
            playbook_id=playbook_id,
        )
        if kind == "goal":
            payload = self.get_goal(item.goal_id) or item.model_dump()
        else:
            payload = {
                "session_id": item.session_id,
                "status": item.status.value,
                "outcome_contract": item.outcome_contract,
                "execution_profile": item.execution_profile,
                "routing_decision": item.routing_decision,
            }
        payload["kind"] = kind
        return payload

    def delete_session(self, session_id: str) -> bool:
        """Delete a session only after revoking/cancelling any live worker."""
        session = self.manager.load(session_id)
        if session is not None and session.status not in {
                SessionStatus.done, SessionStatus.failed, SessionStatus.cancelled}:
            cancellation.request(session_id)
            self.store.revoke_worker_lease(session_id)
        self.workbench.revoke_session_steering(session_id)
        for command in self.workbench.list_steering(
                session_id=session_id, include_inactive=True):
            self.workbench.delete_steering(command.command_id)
        self.workbench.delete_evaluation(session_id)
        return self.store.delete_session(session_id)

    def delete_all_history(self) -> dict[str, int]:
        """Cancel active work, then remove all goal and session history."""
        goal_records = self.goals.list()
        for goal in goal_records:
            self.goals.cancel(goal.goal_id)

        session_records = self.store.list_sessions(limit=None)
        terminal = {
            SessionStatus.done, SessionStatus.failed, SessionStatus.cancelled,
        }
        for record in session_records:
            session_id = record["session_id"]
            # A row that will not deserialize must not block the delete. This
            # operation is precisely what a user reaches for when stored data
            # has gone bad, so a single unreadable session cannot be allowed to
            # fail the whole sweep — fall back to the status column, which is
            # all this loop actually needs to decide whether to cancel.
            try:
                session = self.manager.load(session_id)
                status = session.status if session is not None else None
            except Exception:  # noqa: BLE001 — unreadable is still deletable
                session = None
                status = record.get("status")
                self._sys_log("history_clear_unreadable_session",
                              {"session_id": session_id, "status": str(status)})
            if status is not None and status not in terminal:
                cancellation.request(session_id)
                self.store.revoke_worker_lease(session_id)
            self.workbench.revoke_session_steering(session_id)
            for command in self.workbench.list_steering(
                    session_id=session_id, include_inactive=True):
                self.workbench.delete_steering(command.command_id)
            self.workbench.delete_evaluation(session_id)

        goals_deleted = self.goals.remove_all()
        sessions_deleted = self.store.delete_all_sessions()
        # Deleting rows and goal rows frees nothing the user can see: the DB
        # keeps the pages on its freelist and the staging directories are not
        # DB rows at all. Reclaim both, so "clear history" actually clears.
        workspaces_deleted = self._gc_goal_workspaces(grace_hours=0).get("removed", 0)
        reclaimed = self.store.vacuum().get("reclaimed_bytes", 0)
        self._sys_log("history_cleared", {
            "sessions_deleted": sessions_deleted,
            "goals_deleted": goals_deleted,
            "workspaces_deleted": workspaces_deleted,
            "reclaimed_bytes": reclaimed,
        })
        return {
            "sessions_deleted": sessions_deleted,
            "goals_deleted": goals_deleted,
            "workspaces_deleted": workspaces_deleted,
            "reclaimed_bytes": reclaimed,
        }

    _CORRECTIVE_FOLLOWUP_RE = re.compile(
        r"\b(?:fix|fixed|change|update|modify|adjust|correct|repair|patch|"
        r"debug|broken|bug|wrong|mismatch|inconsistent|except|"
        r"does(?:n't| not)|did(?:n't| not)|is(?:n't| not)|are(?:n't| not)|"
        r"was(?:n't| not)|were(?:n't| not)|can(?:not|'t| not)|"
        r"won(?:'t| not)|fails?|issue|problem|add|remove|replace|prefer|"
        r"instead|should|supposed to|blurr(?:y|ed|iness)|too fast|too slow|"
        r"not agree)\b",
        re.IGNORECASE,
    )

    @staticmethod
    def _followup_artifact_records(session: Session) -> list[dict]:
        """Exact recorded artifacts eligible to anchor a corrective follow-up."""
        manifest = build_artifact_manifest(session)
        records = [
            item for item in manifest.get("artifacts", [])
            if item.get("state") in {"verified", "delivered"}
            and item.get("relative_path")
        ]
        records.sort(
            key=lambda item: (
                0 if item.get("delivered") else 1,
                str(item.get("relative_path") or "").casefold(),
            )
        )
        # A final-batch release can record both staging and delivered copies of
        # the same logical path. Bind repairs to the delivered copy (sorted
        # first) and count that path once when deciding whether selection is
        # ambiguous.
        unique: dict[str, dict] = {}
        for item in records:
            key = str(item.get("relative_path") or "").casefold()
            unique.setdefault(key, item)
        return list(unique.values())

    def _followup_revision_records(
        self,
        session: Session,
        directive: str,
        artifact_id: Optional[str],
        latest_contract: OutcomeContract,
        current_contract: OutcomeContract,
    ) -> tuple[bool, list[dict]]:
        """Recognize a correction and bind it to exact recorded artifact bytes.

        The latest sentence is not classified in a vacuum: defect/change
        language on a completed output is a revision even when it contains no
        words such as "code" or "HTML". Ambiguous multi-artifact repairs fail
        closed instead of expanding into a whole-folder council run.
        """
        records = self._followup_artifact_records(session)
        has_output_context = bool(
            records
            or session.required_files
            or session.release_verified_hashes
            or session.verified_output_hashes
        )
        prior_output_type = current_contract.task_type in {
            TaskType.code.value,
            TaskType.content.value,
            TaskType.design.value,
        }
        explicit_revision = (
            latest_contract.task_type in {
                TaskType.code.value,
                TaskType.content.value,
                TaskType.design.value,
            }
            or bool(self._CORRECTIVE_FOLLOWUP_RE.search(directive or ""))
        )
        corrective = bool(
            has_output_context and (prior_output_type or session.goal_release)
            and explicit_revision
        )
        if not corrective:
            return False, []

        if artifact_id:
            try:
                selected_path = resolve_artifact(session, artifact_id)
            except KeyError as exc:
                raise ValueError(
                    "the selected artifact is no longer available on this session"
                ) from exc
            selected = [
                item for item in records
                if Path(str(item.get("path") or "")).resolve()
                == selected_path.resolve()
            ]
            if not selected:
                raise ValueError(
                    "the selected artifact is not a verified or delivered output"
                )
            return True, selected

        lower = (directive or "").casefold()
        path_mentions = [
            item for item in records
            if "/" in str(item.get("relative_path") or "")
            and str(item.get("relative_path") or "").casefold() in lower
        ]
        if len(path_mentions) == 1:
            return True, path_mentions
        if len(path_mentions) > 1:
            names = ", ".join(
                str(item.get("relative_path")) for item in path_mentions
            )
            raise ValueError(
                "this correction names multiple artifacts; select one exact "
                f"artifact for a surgical repair: {names}"
            )
        name_mentions = [
            item for item in records
            if str(item.get("name") or "").casefold() in lower
        ]
        if len(name_mentions) == 1:
            return True, name_mentions
        if len(name_mentions) > 1:
            names = ", ".join(
                str(item.get("relative_path")) for item in name_mentions
            )
            raise ValueError(
                "that filename matches multiple artifacts; select the exact "
                f"artifact to repair: {names}"
            )
        if len(records) == 1:
            return True, records
        # Released apps commonly include one runnable artifact plus supporting
        # repository notes.  A defect report about "the app" should bind to
        # that sole implementation file instead of forcing the operator to
        # spell out index.html merely because README.md was also delivered.
        implementation = [
            item for item in records
            if str(item.get("kind") or "") in {"html", "code"}
        ]
        auxiliary_names = {
            "readme", "license", "licence", "changelog", "contributing",
            "authors", "notice",
        }
        auxiliary = [
            item for item in records
            if str(item.get("kind") or "") in {"markdown", "text"}
            and Path(str(item.get("name") or "")).stem.casefold()
            in auxiliary_names
        ]
        if (
            len(implementation) == 1
            and len(implementation) + len(auxiliary) == len(records)
        ):
            return True, implementation
        if not records:
            raise ValueError(
                "this looks like a correction, but the completed run has no "
                "recorded verified artifact to revise"
            )
        names = ", ".join(str(item.get("relative_path")) for item in records[:8])
        raise ValueError(
            "this correction could apply to multiple artifacts; name the exact "
            f"file in your response or select its artifact first: {names}"
        )

    def _followup_revision_context(
        self,
        session: Session,
        directive: str,
        artifact_id: Optional[str],
        latest_contract: OutcomeContract,
        current_contract: OutcomeContract,
    ) -> tuple[Session, bool, list[dict]]:
        """Find the nearest durable output in the conversation/goal lineage.

        A failed or answer-only follow-up has no artifacts of its own.  The
        next user correction must still revise the released bytes it was
        discussing, while remaining a child of the latest conversational turn.
        """
        candidates: list[Session] = []
        seen: set[str] = set()
        cursor: Optional[Session] = session
        while cursor is not None and cursor.session_id not in seen:
            candidates.append(cursor)
            seen.add(cursor.session_id)
            cursor = (
                self.manager.load(cursor.parent_session_id)
                if cursor.parent_session_id else None
            )

        goal = self.goals.get(session.goal_id) if session.goal_id else None
        if (
            goal
            and goal.release_session_id
            and goal.release_session_id not in seen
        ):
            released = self.manager.load(goal.release_session_id)
            if released is not None:
                candidates.append(released)

        output_context_seen = False
        for candidate in candidates:
            records = self._followup_artifact_records(candidate)
            output_context_seen = output_context_seen or bool(
                records
                or candidate.required_files
                or candidate.release_verified_hashes
                or candidate.verified_output_hashes
            )
            # Do not let an unsuccessful revision with only required-file
            # metadata shadow the actual verified release farther up-chain.
            if not records:
                continue
            if artifact_id and not any(
                item.get("artifact_id") == artifact_id for item in records
            ):
                continue
            try:
                source_contract = OutcomeContract.model_validate(
                    candidate.outcome_contract
                )
            except (TypeError, ValueError):
                source_contract = current_contract
            corrective, selected = self._followup_revision_records(
                candidate,
                directive,
                artifact_id,
                latest_contract,
                source_contract,
            )
            if corrective:
                return candidate, True, selected

        if artifact_id:
            raise ValueError(
                "the selected artifact is no longer available in this "
                "conversation"
            )
        if (
            output_context_seen
            and bool(self._CORRECTIVE_FOLLOWUP_RE.search(directive or ""))
        ):
            raise ValueError(
                "this looks like a correction, but the conversation has no "
                "recorded verified artifact to revise"
            )
        return session, False, []

    @staticmethod
    def _merged_followup_contract(
        current: OutcomeContract,
        latest: OutcomeContract,
        *,
        corrective: bool,
        targets: list[str],
    ) -> OutcomeContract:
        def merged_items(left: list[str], right: list[str]) -> list[str]:
            return list(dict.fromkeys(
                item for item in [*left, *right] if str(item).strip()
            ))

        amended_outcome = current.outcome
        if latest.outcome and latest.outcome != amended_outcome:
            amended_outcome = (
                f"{amended_outcome}\nLatest user amendment: {latest.outcome}"
            ).strip()
        deliverables = merged_items(current.deliverables, latest.deliverables)
        acceptance = merged_items(
            current.acceptance_criteria, latest.acceptance_criteria
        )
        if corrective:
            deliverables = list(dict.fromkeys([
                *targets,
                *deliverables,
            ]))
            acceptance = list(dict.fromkeys([
                f"Apply the requested correction to {name} and preserve all "
                "unrelated behavior."
                for name in targets
            ] + acceptance))
        return current.model_copy(update={
            "outcome": amended_outcome,
            "deliverables": deliverables,
            "acceptance_criteria": acceptance,
            "constraints": merged_items(current.constraints, latest.constraints),
            "exclusions": merged_items(current.exclusions, latest.exclusions),
            "established_root": (
                latest.established_root or current.established_root
            ),
            "delivery_root": latest.delivery_root or current.delivery_root,
            # A terse defect report must not downgrade a code/output contract
            # into an answer-only question.
            "task_type": (
                TaskType.code.value
                if corrective
                else latest.task_type
            ),
            "complexity": (
                Complexity.standard.value
                if corrective
                else latest.complexity
            ),
            "risk": current.risk if corrective else latest.risk,
            "execution_mode": "focused" if corrective else current.execution_mode,
            "execution_profile": (
                "focused" if corrective else current.execution_profile
            ),
            "budgets": (
                config.budgets_for(Complexity.standard)
                if corrective else current.budgets
            ),
            "has_attachments": (
                current.has_attachments or latest.has_attachments
            ),
            "rationale": (
                f"{current.rationale}; amended by a user follow-up"
                + (
                    "; exact-artifact corrective revision"
                    if corrective else ""
                )
            ).strip("; "),
        })

    def _continue_as_child(
        self,
        parent: Session,
        directive_turn: str,
        turn_text: str,
        attachments: list[str],
        contract: OutcomeContract,
        revision_records: list[dict],
        background: bool,
        revision_source: Optional[Session] = None,
    ) -> Session:
        """Open a clean child so released evidence remains immutable."""
        corrective = bool(revision_records)
        routing = {
            "policy_version": "followup-router.v1",
            "requested_profile": "focused",
            "selected_route": "focused",
            "recommended_profile": "focused",
            "reason": (
                "exact-artifact corrective follow-up"
                if corrective else
                "post-release conversational follow-up"
            ),
            "task_type": contract.task_type,
            "complexity": contract.complexity,
            "risk": contract.risk,
            "candidates": [],
            "alternatives": [],
        }
        child = self._open(
            directive_turn or "(see attached)",
            "followup",
            None,
            attachments=attachments,
            outcome_contract=contract.model_dump(),
            execution_profile="focused",
            routing_decision=routing,
            playbook_id=parent.playbook_id,
            parent_session_id=parent.session_id,
            approval_policy=parent.approval_policy,
        )
        history = list(parent.turns)
        if not history:
            history.append({"role": "user", "text": parent.task.text})
            if parent.final:
                history.append(
                    {"role": "council", "text": parent.final.answer}
                )
        child.turns = [*history, {"role": "user", "text": turn_text}]
        child.goal_id = parent.goal_id
        child.goal_epoch = parent.goal_epoch
        child.goal_milestone = None
        child.goal_release = False
        child.goal_background = False
        child.collaboration_mode = "tournament"
        child.delivery_mode = "immediate"
        child.work_package_id = ""
        child.work_package_owner = ""
        artifact_context = (
            revision_source if corrective and revision_source else parent
        )
        child.workspace_root = artifact_context.workspace_root
        child.established_root = artifact_context.established_root
        child.delivery_root = artifact_context.delivery_root
        child.established_asked = artifact_context.established_asked
        child.acceptance_commands = list(artifact_context.acceptance_commands)
        if corrective:
            targets: list[str] = []
            base_hashes: dict[str, str] = {}
            source_spaces: dict[str, str] = {}
            for record in revision_records:
                name = str(record.get("relative_path") or "").replace("\\", "/")
                if not name or name in targets:
                    continue
                targets.append(name)
                base_hashes[name] = str(
                    record.get("sha256") or record.get("hash") or ""
                )
                source_spaces[name] = str(record.get("space") or "")
            child.required_files = targets
            child.revision_targets = targets
            child.revision_base_hashes = base_hashes
            child.revision_source_spaces = source_spaces
            child.runtime_dependencies = []
            child.dependency_hashes = {}
            child.assembly_mode = ""
            child.assembly_template = ""
            child.assembly_result = {}
        self.store.log_event(
            parent.session_id,
            "followup_child_created",
            {
                "child_session_id": child.session_id,
                "corrective": corrective,
                "targets": list(child.revision_targets),
            },
        )
        self.store.log_event(
            child.session_id,
            "conversation_continued",
            {
                "parent_session_id": parent.session_id,
                "turn": len(child.turns),
                "corrective": corrective,
                "targets": list(child.revision_targets),
            },
        )
        self.store.save_session(child)
        return self._run_owned(
            child, self._run_full, background=background
        )

    def continue_session(self, session_id: str, text: str, background: bool = True,
                         attachments: Optional[list[str]] = None,
                         artifact_id: Optional[str] = None) -> Session:
        """Continue the conversation: the human responds to the council's
        conclusion and the council deliberates AGAIN with the full thread as
        context without starting over. Actionable responses run in a focused
        child so the settled session remains immutable.
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
        if self.is_terminal_acknowledgement(
            text,
            attachments=attachments,
            artifact_id=artifact_id,
        ):
            # A completed result has nothing left to approve. Record the human
            # signal for the audit trail, but do not mutate the conversation,
            # reopen the contract, or inherit the prior execution strategy.
            self.store.log_event(
                session_id,
                "result_acknowledged",
                {"intent": "accept_completed_result"},
            )
            return session
        self._ensure_adapters(session)
        # seed turn-one history for sessions created before the conversation feature
        if not session.turns:
            session.turns.append({"role": "user", "text": session.task.text})
            if session.final:
                session.turns.append({"role": "council", "text": session.final.answer})
        directive_turn = (text or "").strip() or "(see attached)"
        turn_text = directive_turn + attachment_context(
            self.uploads, attachments or []
        )
        # A follow-up is an explicit human amendment to the definition of done.
        # Preserve the original contract for auditability while folding the new
        # outcome, checks, and boundaries into what every remaining seat sees.
        latest_contract = self._outcome_contract(
            directive_turn, has_attachments=bool(attachments)
        )
        try:
            current_contract = OutcomeContract.model_validate(
                session.outcome_contract
            )
        except (TypeError, ValueError):
            current_contract = self._outcome_contract(session.task.text)

        revision_source, corrective, revision_records = (
            self._followup_revision_context(
                session,
                directive_turn,
                artifact_id,
                latest_contract,
                current_contract,
            )
        )
        targets = [
            str(item.get("relative_path") or "").replace("\\", "/")
            for item in revision_records
            if item.get("relative_path")
        ]
        amended_contract = self._merged_followup_contract(
            current_contract,
            latest_contract,
            corrective=corrective,
            targets=targets,
        )
        # A completed result is immutable audit evidence. Every actionable
        # response continues in a focused child: cancelling or failing that
        # follow-up can never erase the result it was responding to, and a
        # question cannot accidentally inherit a prior Best-of-all fan-out.
        return self._continue_as_child(
            session,
            directive_turn,
            turn_text,
            list(attachments or []),
            amended_contract,
            revision_records,
            background,
            revision_source=revision_source,
        )

    # ---- Goals (/goal): long-horizon objectives, milestone by milestone -------

    def create_goal(
        self, text: str, background: bool = False,
        participation_mode: Optional[str] = None,
        outcome_contract: Optional[dict] = None,
        execution_profile: str = "build_team",
        playbook_id: Optional[str] = None,
        parent_goal_id: Optional[str] = None,
        routing_decision: Optional[dict] = None,
        approval_policy: ApprovalPolicy | str = ApprovalPolicy.manual,
    ) -> Goal:
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
        profile = self._execution_profile(execution_profile)
        contract = self._outcome_contract(raw, outcome_contract)
        routing = routing_decision or self._routing_decision(
            raw, contract, "build_team", has_attachments=False,
            require_eligible=False,
        )
        contract = contract.model_copy(
            update={
                "execution_profile": profile,
                "execution_mode": "build_team",
                "auto_routed": profile == "auto",
            }
        )
        established = contract.established_root or extract_established_root(raw)
        delivery = contract.delivery_root or extract_delivery_target(raw)
        active = self.workspaces.active()
        if not established and active:
            established = active.root
        mode = participation_mode or self.settings.participation_mode
        if mode not in {"focused", "adaptive", "full_council"}:
            raise ValueError(
                "participation_mode must be focused, adaptive, or full_council"
            )
        goal = Goal(
            text=raw,
            outcome_contract=contract.model_dump(),
            execution_profile=profile,
            approval_policy=ApprovalPolicy(approval_policy),
            routing_decision=routing,
            playbook_id=playbook_id,
            parent_goal_id=parent_goal_id,
            collaboration_mode="build_team",
            delivery_mode="final_batch",
            background=background,
            build_roster=self._default_build_roster(),
            resource_roster=self._effective_resource_roster(),
            participation_mode=mode,
            established_root=established,
            delivery_root=delivery,
        )
        goal.staging_root = str((self._data_dir / "goal-workspaces" / goal.goal_id / "stage").resolve())
        self.goals.save(goal)
        self._sys_log("goal_created",
                             {"goal_id": goal.goal_id, "chars": len(raw),
                              "profile": profile, "route": "build_team",
                              "playbook_id": playbook_id})
        if background:
            self._pool.submit(self._plan_and_start_safely, goal.goal_id)
            return goal
        return self._plan_and_start(goal.goal_id)

    def clone_goal(
        self, goal_id: str, *, run: bool = False, background: bool = True
    ) -> dict:
        """Copy a goal's durable intent into a fresh, independently planned run."""
        goal = self.goals.get(goal_id)
        if goal is None:
            raise KeyError(goal_id)
        template = {
            "text": goal.text,
            "execution_profile": goal.execution_profile,
            "outcome_contract": goal.outcome_contract,
            "participation_mode": goal.participation_mode,
            "source_goal_id": goal_id,
        }
        if not run:
            return {"template": template}
        cloned = self.create_goal(
            goal.text,
            background=background,
            participation_mode=goal.participation_mode,
            outcome_contract=goal.outcome_contract,
            execution_profile=goal.execution_profile,
            playbook_id=goal.playbook_id,
            parent_goal_id=goal_id,
            routing_decision=goal.routing_decision,
        )
        payload = self.get_goal(cloned.goal_id) or cloned.model_dump()
        payload["kind"] = "goal"
        return payload

    def _plan_and_start_safely(self, goal_id: str) -> None:
        """Worker guard — a goal must never die silently in a thread."""
        try:
            self._plan_and_start(goal_id)
        except Exception as e:  # noqa: BLE001 — last-resort containment
            self._sys_log("goal_error", {"goal_id": goal_id, "detail": str(e)})
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

    def _obvious_single_artifact_plan(
        self, goal: Goal, goal_contract_text: str,
    ) -> tuple[list[GoalMilestone], str]:
        """Skip the planning model when intake already proves one file exists.

        Only an explicit, safe relative filename from the deterministic outcome
        contract qualifies. Descriptions such as "a polished report" are not
        guessed into paths, and multiple named files still require a dependency
        plan. One named artifact has no package graph to discover: its complete
        authorship and materialization belong to one accountable owner.
        """
        raw = list((goal.outcome_contract or {}).get("deliverables") or [])
        if len(raw) != 1:
            return [], ""
        name = str(raw[0] or "").strip().strip("`\"'").replace("\\", "/")
        supported = {
            ".7z", ".c", ".cc", ".cpp", ".cs", ".css", ".csv",
            ".docx", ".gif", ".go", ".h", ".hpp", ".html", ".htm",
            ".java", ".jpeg", ".jpg", ".js", ".json", ".jsx", ".md",
            ".mjs", ".mp3", ".mp4", ".pdf", ".php", ".png", ".pptx",
            ".py", ".rb", ".rs", ".rst", ".svelte", ".svg", ".swift",
            ".tar", ".ts", ".tsv", ".tsx", ".txt", ".vue", ".webp",
            ".xlsx", ".xml", ".yaml", ".yml", ".zip",
        }
        # Intake intentionally uses a human-readable fallback when the user
        # names a format but not a filename ("create a PDF" becomes "The
        # requested polished content artifact").  Treating that description as
        # if no artifact were known sent an obvious one-file job to an LLM
        # planner, which then split 100 recipes into four research packages and
        # an integrator.  The format is already deterministic; give it one safe
        # canonical path and skip the planning call altogether.
        if Path(name).suffix.lower() not in supported:
            inferred = classifier.classify(goal.text, self.role_agents)
            formats = list(dict.fromkeys(
                f".{str(fmt).strip().lower().lstrip('.')}"
                for fmt in inferred.deliverable_formats
                if f".{str(fmt).strip().lower().lstrip('.')}" in supported
            ))
            if inferred.produces_output and len(formats) == 1:
                name = f"deliverable{formats[0]}"
        if (not name or name.startswith(("/", "//"))
                or re.match(r"^[A-Za-z]:", name)
                or any(part in {"", ".", ".."} for part in name.split("/"))):
            return [], ""
        if Path(name).suffix.lower() not in supported:
            return [], ""
        roster = list(dict.fromkeys(goal.build_roster or self.panel))
        preferred = self.role_agents.get(Role.code_generator)
        owner = preferred if preferred in roster else ""
        if not owner:
            owner = next(
                (seat for seat in self._frontier_seats() if seat in roster),
                roster[0] if roster else "",
            )
        package = GoalMilestone(
            index=0,
            title=f"Create {Path(name).name}",
            task_text=goal_contract_text,
            package_id="wp_1",
            owner=owner,
            contract_declared=True,
            requires_delivery=True,
            required_files=[name],
            release_files=[name],
            release_declared=True,
        )
        normalized, errors = self._normalize_work_packages(
            [package], goal_contract_text, roster=roster,
        )
        if errors:
            return [], ""
        return normalized, (
            "deterministic one-artifact plan: intake identifies one safe "
            "release file, so no planning model call was necessary"
        )

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
        self._sys_log("goal_stage_seeded",
                             {"goal_id": goal.goal_id, "files": copied, "root": str(stage)})

    # Extensions that carry CONTENT rather than behaviour. A research package
    # may only produce these: the moment a package emits code or markup it is
    # authoring part of the artifact, and the single-author rule applies again.
    _RESEARCH_SUFFIXES = (".json", ".md", ".markdown", ".yaml", ".yml",
                          ".csv", ".tsv", ".txt", ".rst")
    _DERIVED_SUFFIXES = {
        ".7z", ".docx", ".gif", ".gz", ".ico", ".jpeg", ".jpg",
        ".mp3", ".mp4", ".pdf", ".png", ".pptx", ".tar", ".webp",
        ".xlsx", ".zip",
    }

    @classmethod
    def _materialization_plan(
        cls, package: GoalMilestone, outcome_contract: Optional[dict] = None,
    ) -> MaterializationPlan:
        """Turn a planner file list into an executable output contract."""
        required = [name.replace("\\", "/") for name in package.required_files]
        dependencies = [name.replace("\\", "/") for name in package.dependencies]
        released = [name.replace("\\", "/") for name in package.release_files]
        assertions = list((outcome_contract or {}).get("acceptance_criteria") or [])
        derived = [name for name in required
                   if Path(name).suffix.lower() in cls._DERIVED_SUFFIXES]
        if derived:
            safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", package.package_id or "output")
            producing_files = [name for name in required if name not in derived]
            runner = next(
                (name for name in producing_files
                 if (Path(name).suffix.lower() in {".py", ".js", ".mjs"}
                     and " " not in name)),
                "",
            )
            if not runner:
                runner = f"_gangof8/build_{safe_id}.py"
                producing_files.append(runner)
            command = (
                f"node {runner}" if Path(runner).suffix.lower() in {".js", ".mjs"}
                else f"python {runner}"
            )
            validators = list(dict.fromkeys(
                f"format:{Path(name).suffix.lower().lstrip('.')}" for name in derived
            ))
            return MaterializationPlan(
                mode=MaterializationMode.build,
                authoritative_inputs=dependencies,
                producing_files=producing_files,
                release_files=released,
                validator_ids=validators,
                contract_assertions=assertions,
                build=BuildRecipe(
                    command=command,
                    inputs=[*dependencies, *producing_files],
                    outputs=derived,
                    validator_ids=validators,
                ),
            )
        mode = MaterializationMode.text
        if package.assembly_mode:
            mode = MaterializationMode.transform
        elif set(required) & set(dependencies):
            mode = MaterializationMode.revision
        return MaterializationPlan(
            mode=mode,
            authoritative_inputs=dependencies,
            producing_files=(required if mode != MaterializationMode.transform else []),
            release_files=released,
            contract_assertions=assertions,
        )

    @staticmethod
    def _is_research_package(package, release_paths: set) -> bool:
        """True when a package only GATHERS content for another package.

        The single-artifact cap exists because splitting authorship of one file
        across owners produced seam defects on every boundary. Gathering the
        content that file will contain is a different activity: two seats
        researching different recipes share no interface, so there is nothing
        for them to disagree about.

        Deliberately structural. A package qualifies only if every output is a
        data file and none of them reaches the user -- so "the CSS half of
        index.html", the case the rule was written for, can never qualify.
        """
        outs = [str(n).replace("\\", "/") for n in (package.required_files or [])]
        if not outs:
            return False
        if any(o in release_paths for o in outs):
            return False
        if package.release_files:
            return False
        return all(
            o.lower().endswith(GangOf8Service._RESEARCH_SUFFIXES) for o in outs)


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
        frontier = [seat for seat in self._frontier_seats() if seat in seats]
        targets = sorted(
            code_indices,
            key=lambda i: (not bool(milestones[i].release_files), i),
        )

        # A configured CODE GENERATOR must actually own the primary source
        # package. Previously the setting only controlled on-demand specialist
        # calls; the architect's OWNER survived normalization, so a one-file
        # build could be assigned to Claude even when Codex was the selected
        # coder. Prefer the release/integration package, and swap the displaced
        # owner onto the coder's former package when possible to preserve the
        # planner's team coverage. If that seat is absent from the goal roster,
        # retain the existing frontier fallback behavior.
        preferred_coder = self.role_agents.get(Role.code_generator)
        if targets and preferred_coder in seats:
            primary_i = targets[0]
            if milestones[primary_i].owner != preferred_coder:
                displaced = milestones[primary_i].owner
                old_i = next(
                    (i for i, package in enumerate(milestones)
                     if i != primary_i and package.owner == preferred_coder),
                    None,
                )
                milestones[primary_i].owner = preferred_coder
                if old_i is not None and displaced:
                    milestones[old_i].owner = displaced

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

        # Right-sizing (ARCHITECTURE-REVIEW.md, Phase 1): the deliverable's
        # structure caps the team; the roster never shapes the plan. The old
        # rule here did the opposite — it REJECTED plans that failed to give
        # "every enabled AI a model-authored package", which is how one HTML
        # file became 9 staged files across 8 owners, and every defect in two
        # days of forensics lived on a seam between those owners. A seat left
        # unassigned costs nothing; an unnecessary package costs seams.
        release_paths = {
            name.replace("\\", "/")
            for package in milestones for name in package.release_files
        }
        # Only FILE-AUTHORING packages create seams on the artifact; a prose
        # step with no outputs (a decision, a policy recommendation) is a
        # legitimate separate package even in a single-artifact goal.
        # GANGOF8_GOAL_FULL_ROSTER=1 is an explicit operator choice of team
        # mode and bypasses the cap.
        # A RESEARCH package gathers content the author embeds; it authors no
        # part of the artifact, so it is not a seam on it. Recognised
        # structurally rather than by a label the planner could simply assert:
        # data-only outputs, and nothing it produces is released.
        file_packages = [
            package for package in milestones
            if package.required_files
            and not self._is_research_package(package, release_paths)
        ]
        research_packages = [
            package for package in milestones
            if self._is_research_package(package, release_paths)
        ]
        if (not config.GOAL_FULL_ROSTER
                and len(release_paths) == 1 and len(file_packages) > 1):
            errors.append(
                "right-sizing violation: the deliverable is one artifact "
                f"({next(iter(release_paths))}) but the plan splits its "
                f"authorship across {len(file_packages)} file-producing "
                "packages. A single-artifact deliverable is EXACTLY ONE "
                "file-authoring package whose owner writes the complete file "
                "— no template package, no staged fragments, no assembly step"
            )
        if (not config.GOAL_FULL_ROSTER
                and len(release_paths) == 1 and len(research_packages) > 1):
            errors.append(
                "right-sizing violation: a single-artifact build may have at "
                "most one independently checkpointed research package, but "
                f"the plan created {len(research_packages)}. Do not divide a "
                "document into arbitrary content ranges merely to occupy more "
                "models; keep research with the accountable author unless one "
                "reusable source corpus genuinely needs its own package"
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
        if current.status == "running" and self._pause_goal_over_budget(current):
            self.goals.save(current)
            return
        self._route_around_unavailable_owners(current)
        ready = [i for i in range(len(current.milestones)) if self._package_ready(current, i)]
        if ready:
            self._sys_log("goal_package_wave_started",
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

    def _goal_agent_call(
        self,
        goal: Goal,
        token: str,
        agent: str,
        role: Role,
        prompt: str,
        timeout_s: int = 0,
    ):
        """Run and persist a supervised planning call before a Session exists."""
        reservation = self.goals.reserve_model_call(
            goal.goal_id,
            session_id="",
            agent=agent,
            phase="planning",
        )
        if reservation is None:
            raise AgentError(
                "goal model-call budget exhausted before planning dispatch"
            )
        refreshed = self.goals.get(goal.goal_id)
        if refreshed is not None:
            goal.model_calls_used = refreshed.model_calls_used
            goal.model_calls_by_seat = dict(refreshed.model_calls_by_seat)
            goal.model_call_reservations = list(
                refreshed.model_call_reservations
            )
        adapter = self.registry.get(agent)
        streaming = bool(getattr(adapter, "streams_progress", False))
        hard_timeout = (
            config.OPENROUTER_HARD_TIMEOUT if streaming else
            (int(timeout_s) if int(timeout_s or 0) > 0
             else config.BUFFERED_CALL_HARD_TIMEOUT)
        )
        call_id = f"goalcall_{goal.goal_id}_{time.monotonic_ns()}"
        started_at = utcnow()
        activity = {
            "call_id": call_id,
            "agent": agent,
            "role": role.value,
            "state": "running",
            "started_at": started_at,
            "last_progress_at": started_at,
            "progress_chars": 0,
            "progress_detail": "planning request dispatched",
            "timeout_s": hard_timeout,
            "timeout_policy": (
                "hard_deadline" if hard_timeout > 0 else "operator_supervised"
            ),
            "stall_timeout_s": (
                config.OPENROUTER_OUTPUT_STALL_TIMEOUT if streaming else None
            ),
            "operator_checkin_s": (
                config.MODEL_OPERATOR_CHECKIN_SECONDS
                if hard_timeout == 0 else None
            ),
            "operator_stoppable": True,
        }
        goal.active_agent_calls = [activity]
        if not self.goals.save_owned(goal, token):
            raise SessionCancelled()
        self._sys_log("goal_agent_call_started",
            {"goal_id": goal.goal_id, **activity},
        )
        last_save = [0.0]
        last_chars = [0]

        def _record_progress(chars: int, detail: str, tail: str = "") -> None:
            now = time.monotonic()
            if chars <= last_chars[0] and detail == "output":
                return
            last_chars[0] = max(last_chars[0], int(chars))
            current = next(
                (
                    item for item in goal.active_agent_calls
                    if item.get("call_id") == call_id
                ),
                None,
            )
            if current is None:
                return
            current["last_progress_at"] = utcnow()
            current["progress_chars"] = last_chars[0]
            current["progress_detail"] = str(detail or "output")[:80]
            if tail:
                current["tail"] = str(tail)[-400:]
            if now - last_save[0] >= 2.0:
                last_save[0] = now
                self.goals.save_owned(goal, token)

        cancellation.set_current_session(goal.goal_id)
        cancellation.set_current_call(call_id)
        cancellation.set_call_kind("planning")
        cancellation.register_progress(goal.goal_id, call_id, _record_progress)
        try:
            result = self.registry.call(
                agent, role, prompt, timeout_s=hard_timeout
            )
            if (
                cancellation.is_requested(goal.goal_id)
                or not self.goals.lease_is_current(goal.goal_id, token)
            ):
                raise SessionCancelled()
            self._sys_log("goal_agent_call_finished",
                {
                    "goal_id": goal.goal_id,
                    "call_id": call_id,
                    "agent": agent,
                    "role": role.value,
                    "duration_ms": result.duration_ms,
                },
            )
            return result
        except AgentCallStopped:
            self._sys_log("goal_agent_call_stopped",
                {
                    "goal_id": goal.goal_id,
                    "call_id": call_id,
                    "agent": agent,
                    "role": role.value,
                },
            )
            raise
        finally:
            cancellation.unregister_progress(goal.goal_id, call_id)
            cancellation.clear_call(goal.goal_id, call_id)
            cancellation.set_current_call(None)
            cancellation.set_call_kind(None)
            cancellation.set_current_session(None)
            goal.active_agent_calls = []
            self.goals.save_owned(goal, token)

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
            goal_contract_text = execution_text(
                goal.text, goal.outcome_contract
            )
            agent, role = self._goal_planner()
            milestones: list[GoalMilestone] = []
            rationale = ""
            validation_errors: list[str] = []
            call_error = ""
            repair_count = 0
            milestones, rationale = self._obvious_single_artifact_plan(
                goal, goal_contract_text,
            )
            if milestones:
                goal.planned_by = "coordinator:deterministic-single-artifact"
                self._sys_log(
                    "goal_plan_derived",
                    {
                        "goal_id": goal.goal_id,
                        "reason": "one explicit or deterministically inferred artifact",
                        "release_files": list(milestones[0].release_files),
                    },
                )
            elif agent:
                prompt = goals.plan_prompt(
                    goal_contract_text, goal.build_roster or self.panel,
                    # Code authorship stays frontier-only; content gathering may
                    # use every enabled seat, because data has no seams.
                    research_seats=self.panel,
                )
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
                        result = self._goal_agent_call(
                            goal,
                            token,
                            agent,
                            role,
                            prompt,
                            timeout_s=config.GOAL_PLAN_TIMEOUT,
                        )
                    except AgentCallStopped:
                        goal.status = "paused"
                        goal.last_error = (
                            f"{agent} planning was stopped by the operator; "
                            "resume when you want to retry planning"
                        )
                        goal.plan_rationale = goal.last_error
                        self.goals.save_owned(goal, token)
                        return self.goals.get(goal.goal_id) or goal
                    except SessionCancelled:
                        return self.goals.get(goal.goal_id) or goal
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
                        if goals.requires_delivery_contract(goal_contract_text):
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
                                candidate, goal_contract_text,
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
                            self._sys_log("goal_plan_repaired",
                                {"goal_id": goal.goal_id, "attempts": repair_count},
                            )
                        break

                    validation_errors = candidate_errors
                    if attempt >= config.GOAL_PLAN_REPAIR_ATTEMPTS:
                        break
                    repair_count += 1
                    self._sys_log("goal_plan_repair_requested",
                        {
                            "goal_id": goal.goal_id,
                            "attempt": repair_count,
                            "errors": validation_errors,
                        },
                    )
                    prompt = goals.plan_repair_prompt(
                        goal_contract_text,
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
                if goals.requires_delivery_contract(goal_contract_text):
                    goal.status = "paused"
                    goal.last_error = "planner did not produce a delivery contract"
                    goal.plan_rationale = call_error or rationale or goal.last_error
                    self.goals.save_owned(goal, token)
                    return self.goals.get(goal.goal_id) or goal
                milestones = [GoalMilestone(
                    index=0, title=goal.text[:80], task_text=goal_contract_text,
                    contract_declared=True, requires_delivery=False,
                )]
                rationale = (rationale or "plan was not parseable") + " - analysis-only milestone"
            for package in milestones:
                package.materialization_plan = self._materialization_plan(
                    package, goal.outcome_contract)
                if package.materialization_plan.mode == MaterializationMode.build:
                    for producer in package.materialization_plan.producing_files:
                        if producer not in package.required_files:
                            package.required_files.append(producer)
            goal.milestones = milestones
            self._seed_goal_stage(goal)
            goal.plan_rationale = rationale
            goal.current_index = 0
            goal.status = "running"
            goal.epoch += 1
            if not self.goals.save_owned(goal, token):
                return self.goals.get(goal.goal_id) or goal
            start_epoch = goal.epoch
            self._sys_log("goal_planned",
                                 {"goal_id": goal.goal_id, "milestones": len(milestones),
                                  "planned_by": goal.planned_by, "epoch": goal.epoch})
        finally:
            cancellation.clear(goal.goal_id)
            self.goals.release_worker_lease(goal.goal_id, token)
        current = self.goals.get(goal.goal_id)
        if current and current.status == "running" and current.epoch == start_epoch:
            self._start_ready_packages(current, background=current.background)
        return self.goals.get(goal.goal_id) or goal

    @staticmethod
    def _session_seat_outage(session: Session) -> str:
        """A human-readable seat-outage explanation for a failed session, or
        "" when the failure was not a seat outage. Scans the session's
        unresolved notes for dropped seats whose error classifies as
        hard-unavailable (quota/auth/offline)."""
        from .seat_health import UNAVAILABLE_STATES, classify_failure
        for note in (session.unresolved or []):
            match = re.search(
                r"seat '([\w.\-]+)' dropped[^:]*:\s*(.+)", str(note))
            if not match:
                continue
            seat, error_text = match.group(1), match.group(2)
            state = classify_failure(error_text)
            if state in UNAVAILABLE_STATES:
                return (
                    f"seat {seat} is unavailable ({state.replace('_', ' ')}): "
                    f"{error_text.strip()[:160]}"
                )
        return ""

    @staticmethod
    def _session_orchestrator_failure(session: Session) -> Optional[ProposedAction]:
        return next(
            (
                action for action in reversed(session.proposed_actions)
                if action.status == "failed"
                and action.failure_layer == "orchestrator"
            ),
            None,
        )

    @staticmethod
    def _milestone_input_hashes(
        goal: Goal, milestone: GoalMilestone,
    ) -> dict[str, str]:
        """Seal every accepted byte the package actually consumes."""
        wanted = {
            name.replace("\\", "/")
            for name in [
                *milestone.dependencies,
                *((milestone.materialization_plan.authoritative_inputs)
                  if milestone.materialization_plan else []),
            ]
            if name
        }
        hashes: dict[str, str] = {}
        for package in goal.milestones:
            if package.status != "done":
                continue
            for name, digest in package.accepted_hashes.items():
                normalized = name.replace("\\", "/")
                if not wanted or normalized in wanted:
                    hashes[normalized] = digest
        return hashes

    @staticmethod
    def _merge_goal_research_provenance(
        goal: Goal, milestone: GoalMilestone, session: Session,
    ) -> None:
        existing = {
            (str(item.get("session_id") or ""), str(item.get("recorded_at") or ""))
            for item in goal.research_provenance
        }
        for raw in session.research_provenance:
            item = dict(raw)
            item["session_id"] = session.session_id
            item["package_id"] = milestone.package_id
            key = (session.session_id, str(item.get("recorded_at") or ""))
            if key not in existing:
                goal.research_provenance.append(item)
                existing.add(key)
        modes = {str(item.get("mode") or "") for item in goal.research_provenance}
        if "retrieved" in modes:
            goal.research_mode = "retrieved"
        elif "capability_gap" in modes or "recall_only" in modes:
            goal.research_mode = "capability_gap"

    def _recover_failed_milestone(
        self, goal: Goal, milestone: GoalMilestone, session: Session,
        category: str, detail: str,
    ) -> bool:
        """Apply bounded recovery; approval policy governs actions, not diagnosis."""
        prior_repair = next(
            (attempt for attempt in reversed(goal.repair_history)
             if attempt.status == "started" and attempt.owner == milestone.owner),
            None,
        )
        if prior_repair is not None:
            recovery.finish_repair(goal, prior_repair, verified=False)
        orchestrator_action = self._session_orchestrator_failure(session)
        if orchestrator_action is not None:
            exact = (
                f"{orchestrator_action.kind}: "
                f"{orchestrator_action.error or detail}"
            )
            failure = recovery.record_failure(
                goal,
                stage="orchestrator",
                category="capability_contract",
                summary=exact,
                evidence={
                    "package_id": milestone.package_id,
                    "milestone": milestone.index,
                    "session_id": session.session_id,
                    "action_id": orchestrator_action.action_id,
                    "diagnostic": exact,
                },
                responsible_owner="coordinator",
                recoverable=False,
                producer_paths=(
                    list(milestone.materialization_plan.producing_files)
                    if milestone.materialization_plan else []
                ),
            )
            recovery.mark_exhausted(goal, failure)
            milestone.status = "failed"
            goal.status = "failed"
            goal.last_error = (
                "manual intervention required: orchestrator capability contract "
                f"failed; {exact}"
            )[:300]
            self._sys_log(
                "goal_orchestrator_failure",
                {"goal_id": goal.goal_id, "package": milestone.package_id,
                 "session_id": session.session_id,
                 "action_id": orchestrator_action.action_id,
                 "fault_signature": failure.fault_signature,
                 "error": orchestrator_action.error},
            )
            return False
        unresolved_failures = [
            item for item in session.failure_records
            if item.resolution_state != "resolved"
        ]
        # Missing-output verification is commonly the final symptom of an
        # earlier build/test/dependency failure.  Recovery must diagnose and
        # fingerprint the causal execution failure, not repeatedly retry the
        # most recent secondary symptom.
        causal_categories = {
            "build_command_failed",
            "test_command_failed",
            "dependency_install_failed",
            "invalid_producer",
            "dependency_closure",
        }
        source_failure = next(
            (
                item for item in reversed(unresolved_failures)
                if item.category in causal_categories
            ),
            unresolved_failures[-1] if unresolved_failures else None,
        )
        self._preserve_failed_producer(goal, milestone, session, source_failure)
        failure_stage = source_failure.stage if source_failure else "milestone"
        failure_category = source_failure.category if source_failure else category
        failure_summary = (
            source_failure.summary if source_failure else category
        )
        failure = recovery.record_failure(
            goal,
            stage=failure_stage,
            category=failure_category,
            summary=f"{milestone.package_id or milestone.index}: {failure_summary}",
            evidence={"package_id": milestone.package_id,
                      "milestone": milestone.index,
                      "diagnostic": detail[:1000],
                      "session_failure_id": (
                          source_failure.failure_id if source_failure else ""
                      )},
            responsible_owner=milestone.owner,
            recoverable=(source_failure.recoverable if source_failure else True),
            validator_id=(source_failure.validator_id if source_failure else ""),
            producer_paths=(
                list(source_failure.repair_scope)
                if source_failure else
                list(milestone.materialization_plan.producing_files)
                if milestone.materialization_plan else []
            ),
            expected_hash=(source_failure.expected_hash if source_failure else ""),
            command_result=(source_failure.command_result if source_failure else {}),
        )
        # Preserve the rich changing diagnostic without letting incidental
        # wording create an unbounded series of distinct fault signatures.
        failure.evidence["diagnostic"] = detail[:1000]
        if not failure.recoverable:
            recovery.mark_exhausted(goal, failure)
            milestone.status = "failed"
            goal.status = "failed"
            goal.last_error = (
                f"manual intervention required: {failure.stage} / "
                f"{failure.category}; {detail}"
            )[:300]
            self._sys_log(
                "goal_nonrecoverable_failure",
                {"goal_id": goal.goal_id, "package": milestone.package_id,
                 "stage": failure.stage, "category": failure.category,
                 "fault_signature": failure.fault_signature,
                 "reason": detail[:500]},
            )
            return False
        frontier = [
            seat for seat in self._frontier_seats()
            if seat in self.panel and not self.seat_health.is_unavailable(seat)
        ]
        decision = recovery.choose_goal_recovery(
            goal, failure, milestone.owner, frontier)
        if decision.action == "exhausted":
            recovery.mark_exhausted(goal, failure)
            milestone.status = "failed"
            goal.status = "failed"
            goal.last_error = (
                f"manual intervention required: {milestone.package_id or milestone.title} "
                f"exhausted bounded recovery for {category}; {detail}"
            )[:300]
            self._sys_log("goal_recovery_exhausted",
                {"goal_id": goal.goal_id, "package": milestone.package_id,
                 "fault_signature": failure.fault_signature,
                 "attempt": decision.attempt},
            )
            return False
        attempt = recovery.begin_repair(
            goal, failure,
            repair_owner=decision.owner,
            strategy=decision.action,
            input_hashes=self._milestone_input_hashes(goal, milestone),
        )
        if attempt is None:
            recovery.mark_exhausted(goal, failure)
            milestone.status = "failed"
            goal.status = "failed"
            goal.last_error = "manual intervention required: unchanged retry rejected"
            return False
        previous_owner = milestone.owner
        milestone.owner = decision.owner
        milestone.status = "pending"
        milestone.resume_session_id = milestone.session_id or session.session_id
        milestone.session_id = None
        # The owner's RETRY CORRECTION must carry the causal execution record,
        # not the downstream symptom. A live retry was told only "no .pdf file
        # was produced" while the recorded cause was its own generator raising
        # at build time, so it re-authored blind. Keep the traceback's tail:
        # that is where the exception message lives.
        retry_detail = f"{category}: {detail}"
        # Up to two distinct causal records, newest first: a repair's own
        # rejected command must not hide the producer crash it failed to fix.
        causal_records = []
        seen_causes: set[str] = set()
        for item in reversed(unresolved_failures):
            key = (item.summary or "")[-300:]
            if item.category in causal_categories and key not in seen_causes:
                seen_causes.add(key)
                causal_records.append(item)
            if len(causal_records) == 2:
                break
        if causal_records:
            parts = []
            for item in causal_records:
                causal = item.summary or ""
                if len(causal) > 700:
                    causal = causal[:150] + "\n...\n" + causal[-550:]
                parts.append(f"{item.category}: {causal}")
            retry_detail = (
                "\n\nEarlier: ".join(parts)
                + f"\nResulting symptom: {category}: {detail[:200]}"
            )
        if milestone.candidate_checkpoint_id:
            retry_detail += (
                "\nThe failed producer from that attempt is in your working set "
                "as an UNVERIFIED repair baseline: fix the cause in it and emit "
                "the complete corrected file rather than starting over."
            )
        milestone.acceptance_detail = (
            f"{retry_detail}\nFault signature: {failure.fault_signature}. "
            "Change the producing source or relevant implementation; do not "
            "repeat unchanged bytes."
        )[:1600]
        # Never demote or erase the last verified candidate merely because a
        # repair branch is starting. The branch becomes authoritative only
        # after it passes objective verification and seals a new checkpoint.
        milestone.repair_context = {
            "failure_id": failure.failure_id,
            "fault_signature": failure.fault_signature,
            "category": failure.category,
            "detail": retry_detail[:1600],
            "base_checkpoint_id": milestone.active_verified_checkpoint_id,
            "target_paths": list(failure.repair_scope),
            "repair_owner": decision.owner,
        }
        milestone.phase = "repairing"
        goal.phase = "repairing"
        goal.status = "running"
        goal.current_index = milestone.index
        goal.release_status = "not_started"
        goal.release_session_id = None
        goal.last_error = (
            f"automatic recovery {decision.attempt}: {decision.action} "
            f"for {milestone.package_id or milestone.title}"
        )
        self._record_work_item(
            goal,
            milestone.package_id,
            "repairing",
            "ready",
            checkpoint_id=milestone.active_verified_checkpoint_id,
        )
        self._sys_log("goal_recovery_scheduled",
            {"goal_id": goal.goal_id, "package": milestone.package_id,
             "from_owner": previous_owner, "to_owner": decision.owner,
             "strategy": decision.action, "attempt": decision.attempt,
             "fault_signature": failure.fault_signature},
        )
        return True

    def _restore_failed_producer(
        self, session: Session, milestone: GoalMilestone,
    ) -> None:
        """Hand a repair attempt the exact failed producer bytes to fix.

        _preserve_failed_producer seals them, but nothing put them back: live
        retries got an empty working set and re-authored an ~80KB generator
        from scratch, discarding a producer that needed a one-line repair. The
        bytes land in the session sandbox, which the package working set
        already copies as the (unverified) repair baseline.
        """
        if not milestone.candidate_checkpoint_id:
            return
        planned = set(
            milestone.materialization_plan.producing_files
            if milestone.materialization_plan else []
        )
        try:
            record = self.checkpoints.get(milestone.candidate_checkpoint_id)
            names = [
                name for name in (record or {}).get("manifest", {})
                if not planned or name in planned
            ]
            if not names:
                return
            restored = self.checkpoints.materialize(
                milestone.candidate_checkpoint_id,
                executor.artifacts_dir(self.store.data_dir, session.session_id),
                names=names,
            )
        except (KeyError, OSError, ValueError) as exc:
            self.store.log_event(
                session.session_id, "failed_producer_restore_failed",
                {"checkpoint_id": milestone.candidate_checkpoint_id,
                 "reason": str(exc)[:300]},
            )
            return
        self.store.log_event(
            session.session_id, "failed_producer_restored",
            {"checkpoint_id": milestone.candidate_checkpoint_id,
             "files": sorted(restored)},
        )

    def _preserve_failed_producer(
        self,
        goal: Goal,
        milestone: GoalMilestone,
        session: Session,
        source_failure,
    ) -> None:
        """Checkpoint failed source bytes for the next bounded repair attempt."""
        if not goal.staging_root:
            return
        planned = list(
            milestone.materialization_plan.producing_files
            if milestone.materialization_plan else []
        )
        scoped = list(source_failure.repair_scope) if source_failure else []
        producer_paths = {
            name.replace("\\", "/") for name in [*scoped, *planned] if name
        }
        if not producer_paths:
            return
        latest = {}
        for action in session.proposed_actions:
            name = str(action.filename or action.args.get("filename") or "").replace(
                "\\", "/"
            )
            if (
                name in producer_paths
                and action.kind in {"write_file", "edit_file"}
                and action.status == "executed"
                and (action.content or action.args.get("content"))
            ):
                latest[name] = action
        if not latest:
            return
        candidate_paths: dict[str, Path] = {}
        for name, action in latest.items():
            path = Path(action.result_path) if action.result_path else None
            if path is not None and path.is_file():
                candidate_paths[name] = path
        if not candidate_paths:
            return
        try:
            record = self.checkpoints.seal_paths(
                goal_id=goal.goal_id,
                package_id=milestone.package_id,
                session_id=session.session_id,
                paths=candidate_paths,
                parent_id=milestone.active_verified_checkpoint_id,
                state="failed_candidate",
                evidence={
                    "causal_failure_id": (
                        source_failure.failure_id if source_failure else ""
                    )
                },
            )
        except (OSError, ValueError):
            return
        milestone.candidate_checkpoint_id = record["checkpoint_id"]
        milestone.recovery_source_checkpoint = record
        self._sys_log(
            "goal_failed_producer_preserved",
            {
                "goal_id": goal.goal_id,
                "package": milestone.package_id,
                "session_id": session.session_id,
                "checkpoint_id": record["checkpoint_id"],
                "producer_paths": list(record["manifest"]),
            },
        )

    def _route_around_unavailable_owners(self, goal: Goal) -> None:
        """Reassign pending packages away from seats that cannot answer.

        A seat in a hard-unavailable state (quota exhausted, auth expired,
        CLI offline) fails every attempt by definition; scheduling against
        it burns budget and surfaces as a fake-fatal session error. When a
        healthy frontier seat exists, transfer ownership BEFORE the session
        opens and say so; when none exists, the package start will fail
        honestly and the goal error names the seat, not the symptom.
        """
        changed = False
        for package in goal.milestones:
            if package.status != "pending" or not package.owner:
                continue
            if not self.seat_health.is_unavailable(package.owner):
                continue
            replacement = next(
                (seat for seat in self._frontier_seats()
                 if seat in self.panel and seat != package.owner
                 and not self.seat_health.is_unavailable(seat)),
                None,
            )
            if replacement is None:
                continue
            reason = self.seat_health.state(package.owner)
            self._sys_log("package_owner_rerouted_seat_unavailable",
                {"goal_id": goal.goal_id, "package": package.index + 1,
                 "from_owner": package.owner, "to_owner": replacement,
                 "seat_state": reason,
                 "detail": self.seat_health.reason(package.owner)[:200]},
            )
            package.owner = replacement
            changed = True
        if changed:
            self.goals.save(goal)

    @staticmethod
    def _record_work_item(
        goal: Goal,
        package_id: str,
        phase: str,
        status: str,
        *,
        session_id: str = "",
        checkpoint_id: str = "",
    ) -> dict:
        """Upsert one idempotent durable phase cursor."""
        identity = hashlib.sha256(
            f"{goal.goal_id}|{package_id}|{phase}|{checkpoint_id}".encode("utf-8")
        ).hexdigest()[:24]
        key = f"wi_{identity}"
        item = next(
            (entry for entry in goal.work_items
             if entry.get("work_item_id") == key),
            None,
        )
        if item is None:
            item = {
                "work_item_id": key,
                "package_id": package_id,
                "phase": phase,
                "checkpoint_id": checkpoint_id,
                "created_at": utcnow(),
            }
            goal.work_items.append(item)
        item.update({
            "status": status,
            "session_id": session_id,
            "updated_at": utcnow(),
        })
        goal.phase = phase
        return item

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
        if current.milestones[index].materialization_plan is None:
            current.milestones[index].materialization_plan = self._materialization_plan(
                current.milestones[index], current.outcome_contract)
            if (current.milestones[index].materialization_plan.mode
                    == MaterializationMode.build):
                for producer in current.milestones[index].materialization_plan.producing_files:
                    if producer not in current.milestones[index].required_files:
                        current.milestones[index].required_files.append(producer)
            self.goals.save(current)
        session = self._open(
            goals.compose_milestone_task(current, index),
            "goal",
            None,
            None,
            outcome_contract=current.outcome_contract,
            execution_profile=current.execution_profile,
            routing_decision=current.routing_decision,
            playbook_id=current.playbook_id,
            approval_policy=current.approval_policy,
        )
        bound = self.goals.bind_milestone(
            current.goal_id, index, current.epoch, session.session_id)
        if bound is None:
            self.store.delete_session(session.session_id)
            return None
        milestone = bound.milestones[index]
        self._record_work_item(
            bound,
            milestone.package_id,
            "repairing" if milestone.repair_context else "baseline_ready",
            "running",
            session_id=session.session_id,
            checkpoint_id=milestone.active_verified_checkpoint_id,
        )
        self.goals.save(bound)
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
        session.materialization_plan = milestone.materialization_plan
        session.repair_mode = bool(milestone.repair_context)
        session.base_checkpoint_id = milestone.active_verified_checkpoint_id
        session.candidate_checkpoint_id = milestone.candidate_checkpoint_id
        session.reused_participation_reports = list(
            milestone.participation_reports
        ) if session.repair_mode else []
        session.phase = "repairing" if session.repair_mode else "baseline_ready"
        self._restore_failed_producer(session, milestone)
        if milestone.resume_session_id and not session.repair_mode:
            previous = self.manager.load(milestone.resume_session_id)
            if previous is not None and previous.work_package_id == milestone.package_id:
                session.candidate_fallbacks = list(
                    previous.candidate_fallbacks
                )
                session.candidate_fallback_groups = [
                    list(group) for group in previous.candidate_fallback_groups
                ]
                standby_names = {
                    name
                    for group in session.candidate_fallback_groups
                    for name in group
                } | set(session.candidate_fallbacks)
                resumable_kinds = {
                    "write_file", "edit_file", "install_deps",
                    "build_artifact", "run_tests",
                }
                session.proposed_actions = []
                for prior_action in previous.proposed_actions:
                    if prior_action.kind not in resumable_kinds:
                        continue
                    if (prior_action.role == Role.panelist
                            and prior_action.filename not in standby_names):
                        continue
                    restored = prior_action.model_copy(deep=True)
                    restored.session_id = session.session_id
                    restored.status = "proposed"
                    restored.approval_id = None
                    restored.result_path = None
                    restored.error = None
                    session.proposed_actions.append(restored)
                session.contributions = [
                    item.model_copy(deep=True) for item in previous.contributions
                ]
                session.collaboration_assignments = [
                    item.model_copy(deep=True)
                    for item in previous.collaboration_assignments
                ]
                for assignment in session.collaboration_assignments:
                    if assignment.status in {"running", "requesting_context"}:
                        assignment.status = "pending"
                session.collaboration_baseline = dict(
                    previous.collaboration_baseline
                )
                session.collaboration_integrated_files = list(
                    previous.collaboration_integrated_files
                )
                session.collaboration_integration_status = (
                    previous.collaboration_integration_status
                )
                session.package_output_authors = dict(
                    previous.package_output_authors
                )
                session.package_output_history = dict(
                    previous.package_output_history
                )
                session.phase = previous.phase or "baseline_ready"
                self.store.log_event(
                    session.session_id,
                    "package_phase_resumed",
                    {
                        "from_session_id": previous.session_id,
                        "phase": session.phase,
                        "reused_actions": len(session.proposed_actions),
                        "reused_contributions": sum(
                            1 for item in session.collaboration_assignments
                            if item.status == "contributed"
                        ),
                    },
                )
        session.resource_roster = list(bound.resource_roster or session.panel)
        if bound.research_mode == "retrieved":
            reusable = [
                dict(item) for item in bound.research_provenance
                if item.get("mode") == "retrieved" and item.get("evidence")
            ]
            if reusable:
                session.research_mode = "retrieved"
                session.research_provenance = reusable
        session.participation_mode = bound.participation_mode
        session.package_helpers = [
            seat for seat in session.resource_roster
            if seat and seat != milestone.owner
        ]
        session.assembly_mode, session.assembly_template = self._assembly_contract(milestone)
        session.frontier_author_seats = self._frontier_seats()
        session.required_frontier_authors = (
            [milestone.owner]
            if (not session.assembly_mode
                and milestone.owner in session.frontier_author_seats)
            else []
        )
        if bound.delivery_mode == "final_batch":
            session.workspace_root = bound.staging_root
            session.established_root = bound.established_root
            session.delivery_root = bound.delivery_root
            if (session.participation_mode != "focused"
                    and session.resource_roster):
                # The owner controls final bytes, but every enabled resource is
                # part of the Planned team from package start. Keeping only the
                # owner here made the live council announce a gang of one even
                # though peers were scheduled to challenge the baseline later.
                session.panel = list(dict.fromkeys(session.resource_roster))
            elif milestone.owner:
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
            list(milestone.dependencies)
            + list(
                milestone.materialization_plan.authoritative_inputs
                if milestone.materialization_plan else []
            )
            + ready_contract_files
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
        if session.materialization_plan is not None:
            session.materialization_plan.input_hashes = dict(session.dependency_hashes)
            if session.materialization_plan.build is not None:
                session.materialization_plan.build.inputs = list(dict.fromkeys([
                    *session.materialization_plan.build.inputs,
                    *session.materialization_plan.authoritative_inputs,
                    *session.materialization_plan.producing_files,
                ]))
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
            if (action.role != Role.panelist and action.kind == "build_artifact"
                    and action.status == "executed"):
                declared = [item.strip().replace("\\", "/") for item in
                            str(action.args.get("produces") or "").split(",")
                            if item.strip()]
                try:
                    produced = [Path(item) for item in json.loads(
                        action.args.get("produced_paths") or "[]")]
                except (json.JSONDecodeError, TypeError):
                    produced = []
                for output_name, output_path in zip(declared, produced):
                    latest[output_name] = output_path
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
            build_action = next((
                action for action in reversed(session.proposed_actions)
                if action.kind == "build_artifact" and action.status == "executed"
                and name in {part.strip().replace("\\", "/") for part in
                             str(action.args.get("produces") or "").split(",")}
            ), None)
            if build_action is not None:
                record = {
                    "sha256": hashes.get(name, ""),
                    "session_id": session.session_id,
                    "method": "deterministic_build",
                    "agent": session.work_package_owner or None,
                    "build_action_id": build_action.action_id,
                    "command": build_action.args.get("command", ""),
                    "producer_files": list(
                        session.materialization_plan.producing_files
                        if session.materialization_plan else []
                    ),
                    "input_hashes": dict(
                        session.materialization_plan.input_hashes
                        if session.materialization_plan else {}
                    ),
                }
            elif deterministic:
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

    def _authorize_goal_release(
        self, session: Session, *, linked_goal: Optional[Goal] = None,
    ) -> Session:
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
        if approval.status == "approved":
            action.status = "approved"
            self.store.save_session(session)
            self.store.log_event(
                session.session_id, "final_batch_approval_auto_resolved",
                {"approval_id": approval.approval_id,
                 "policy": session.approval_policy.value},
            )
            return self._finish_goal_release(
                session, True, linked_goal=linked_goal,
            )
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
        expected_hashes = {
            name.replace("\\", "/"): package.accepted_hashes.get(
                name.replace("\\", "/"), "")
            for package in release_packages for name in package.release_files
        }
        format_failures: list[str] = []
        format_evidence: list[dict] = []
        assertions = list(
            (goal.outcome_contract or {}).get("acceptance_criteria") or []
        )
        for name in session.required_files:
            try:
                path = executor.resolve_in_workspace(stage, name)
                raw = path.read_bytes()
            except (OSError, executor.ExecutionError) as exc:
                format_failures.append(f"{name}: release file is unavailable ({exc})")
                continue
            digest = hashlib.sha256(raw).hexdigest()
            if expected_hashes.get(name) != digest:
                format_failures.append(
                    f"{name}: staged bytes do not match the package's accepted hash"
                )
                continue
            result = validation.validate_artifact(path, assertions)
            format_evidence.append({"file": name, **result.model_dump()})
            format_failures.extend(
                f"{name}: {failure}" for failure in result.failures
            )
        self.store.log_event(
            session.session_id, "release_format_validated",
            {"verdict": "FAIL" if format_failures else "PASS",
             "files": format_evidence, "failures": format_failures},
        )
        if format_failures:
            session.quality_gate = {
                "verdict": "FAIL", "stage": "release_format_validation",
                "files": format_evidence, "remaining_defects": format_failures,
            }
            session.unresolved.extend(format_failures)
            session.stop_reason = "strict final-artifact validation failed"
            session.outcome = "failed_verification"
            self.manager.transition(session, SessionStatus.composing)
            session.final = FinalAnswer(
                answer="The final artifact failed deterministic format or hash validation and was not released.",
                confidence="low", risks_unresolved=list(session.unresolved),
                next_action="Repair the producing source identified by the validation evidence.",
            )
            self.manager.transition(session, SessionStatus.failed)
            self.store.save_session(session)
            return False
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

        if not browser_failures:
            objective_paths: dict[str, Path] = {}
            objective_hashes: dict[str, str] = {}
            for name in session.required_files:
                path = executor.resolve_in_workspace(stage, name)
                objective_paths[name] = path
                objective_hashes[name] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
            objective_checkpoint = self.checkpoints.seal_paths(
                goal_id=goal.goal_id,
                package_id="__release__",
                session_id=session.session_id,
                paths=objective_paths,
                expected_hashes=objective_hashes,
                parent_id=goal.active_verified_checkpoint_id,
                state="objective_validated",
                evidence={
                    "format_validation": format_evidence,
                    "deterministic_preflight": deterministic_preflight,
                    "browser_acceptance": browser_evidence,
                },
            )
            session.base_checkpoint_id = objective_checkpoint["checkpoint_id"]
            goal.active_verified_checkpoint_id = objective_checkpoint[
                "checkpoint_id"
            ]
            goal.phase = "objective_validated"
            session.phase = "objective_validated"
            self._record_work_item(
                goal, "__release__", "objective_validated", "completed",
                session_id=session.session_id,
                checkpoint_id=objective_checkpoint["checkpoint_id"],
            )

        def seal_verified_hashes() -> dict[str, str]:
            sealed: dict[str, str] = {}
            checkpoint_paths: dict[str, Path] = {}
            for name in session.required_files:
                path = executor.resolve_in_workspace(stage, name)
                if not path.is_file():
                    raise OSError(f"verified release file disappeared: {name}")
                sealed[name] = hashlib.sha256(path.read_bytes()).hexdigest()
                checkpoint_paths[name] = path
            checkpoint = self.checkpoints.seal_paths(
                goal_id=goal.goal_id,
                package_id="__release__",
                session_id=session.session_id,
                paths=checkpoint_paths,
                expected_hashes=sealed,
                parent_id=goal.active_verified_checkpoint_id,
                state="release_verified",
                evidence={
                    "criteria": [
                        item.model_dump() for item in session.criteria
                    ],
                    "quality_gate": dict(session.quality_gate),
                },
            )
            session.release_verified_hashes = sealed
            session.candidate_checkpoint_id = checkpoint["checkpoint_id"]
            goal.active_verified_checkpoint_id = checkpoint["checkpoint_id"]
            goal.phase = "release_ready"
            session.phase = "release_ready"
            self._record_work_item(
                goal,
                "__release__",
                "release_ready",
                "completed",
                session_id=session.session_id,
                checkpoint_id=checkpoint["checkpoint_id"],
            )
            session.quality_gate["verified_hashes"] = dict(sealed)
            session.quality_gate["checkpoint_id"] = checkpoint["checkpoint_id"]
            return sealed

        enabled_frontier = [
            seat for seat in self._frontier_seats()
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
        verifier_pool = [
            seat for seat in enabled_frontier if seat not in release_owners
        ]
        healthy_pool = [
            seat for seat in verifier_pool
            if not self.seat_health.is_unavailable(seat)
        ]
        # Independent fallback: any other enabled seat that did not author the
        # release. Frontier seats stay first; the fallback only matters when
        # they cannot answer (a live goal failed while its only independent
        # frontier seat sat behind a session limit and Gemini was idle).
        # Drawn from every registered seat, like the deliverable review: the
        # duo panel is [claude, codex], so a panel-only pool left a healthy
        # Gemini out and the release paused anyway.
        registered = self.registry.names()
        fallback_pool = [
            seat for seat in dict.fromkeys(
                [*self.panel, *sorted(registered)])
            if seat in registered
            and seat not in release_owners
            and seat not in verifier_pool
            and seat not in ("system", "mock")
            and not self.seat_health.is_unavailable(seat)
        ]
        # Prefer seats that can actually answer; if health has marked every
        # candidate unavailable, keep the original pool so the transport
        # retry/UNAVAILABLE path reports honestly rather than aborting here.
        verifier_pool = (healthy_pool + fallback_pool) or verifier_pool
        verifier_name = verifier_pool[0] if verifier_pool else None
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

        def read_files() -> list[tuple[str, str]]:
            loaded: list[tuple[str, str]] = []
            for name in session.required_files:
                path = executor.resolve_in_workspace(stage, name)
                suffix = path.suffix.lower()
                if suffix == ".pdf":
                    try:
                        from pypdf import PdfReader
                        reader = PdfReader(str(path), strict=True)
                        body = "\n\n".join(
                            f"--- PAGE {index + 1} ---\n{page.extract_text() or ''}"
                            for index, page in enumerate(reader.pages)
                        )
                        metadata = dict(reader.metadata or {})
                        body = (
                            "[STRICTLY PARSED PDF TEXT FOR SEMANTIC REVIEW]\n"
                            f"Metadata: {metadata}\nPages: {len(reader.pages)}\n\n{body}"
                        )
                    except Exception as exc:
                        body = f"[PDF text extraction failed after format validation: {exc}]"
                elif suffix in self._DERIVED_SUFFIXES:
                    body = (
                        f"[BINARY ARTIFACT: {path.stat().st_size} bytes. The exact "
                        "file is available in the verifier working directory and "
                        "passed deterministic format validation.]"
                    )
                else:
                    body = path.read_text(encoding="utf-8", errors="replace")
                loaded.append((name, body))
            return loaded

        review_root = Path(config.SANDBOX_ROOT) / f"release-review-{session.session_id}"

        def write_review_copy() -> str:
            """A disposable directory holding the exact release bytes, used as
            the verifier CLI's working directory. Agentic CLIs inspect "their
            workspace" with tools and trust it over inline prompt text — codex
            FAILed a passing game with "frogger.html is absent from the
            workspace" because it ran from the empty neutral sandbox. A copy
            (not the staging dir itself) so a verifier's shell experiments can
            never mutate accepted staged bytes."""
            import shutil as _shutil
            if review_root.exists():
                _shutil.rmtree(review_root, ignore_errors=True)
            review_root.mkdir(parents=True, exist_ok=True)
            for name in session.required_files:
                source = executor.resolve_in_workspace(stage, name)
                target = review_root / name.replace("\\", "/")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
            return str(review_root)

        files = read_files()
        total_edits = 0
        for attempt in range(max(1, config.FRONTIER_VERIFY_ATTEMPTS)):
            if attempt:
                browser_evidence, browser_failures = run_browser_acceptance()
            if not browser_failures:
                # The current real-browser run is the authority on
                # browser-detected defects: register entries recorded by
                # earlier runs describe a state that no longer exists, and
                # feeding them to the verifier as open defects sends it
                # hunting for problems that are already fixed. The bare
                # no-usable-repair note is meta-history, not a defect.
                goal.release_defects = [
                    entry for entry in goal.release_defects
                    if "browser acceptance failed" not in str(entry)
                    and str(entry) != (
                        "verifier rejected the batch without a usable "
                        "implementation repair"
                    )
                ]
            release_defect_register = list(dict.fromkeys(
                [*goal.release_defects, *browser_failures]
            ))
            prompt = rounds.frontier_release_prompt(
                session, files, defect_register=release_defect_register,
                repair_attempt=attempt) + (
                "\n\nWORKSPACE NOTE: your working directory contains the exact "
                "release file bytes under review — identical to the inline "
                "copies above. Inspect them with your tools freely. Never "
                "report a file as missing without listing the directory first."
            )
            review_cwd = write_review_copy()
            # A verifier CLI that exits, times out, or reports capacity did NOT
            # judge the batch — a real release recorded "verifier rejected
            # release" over a codex "model is at capacity" outage, for a game
            # that had just PASSED real-browser acceptance. Rotate through the
            # eligible independent seats with a short backoff, and when none
            # can run, report the verifier as UNAVAILABLE (retryable via
            # resume), never as a rejection.
            answer = None
            transport_error = ""
            # A hard-unavailable seat (quota, auth, missing CLI) is dropped for
            # the rest of this release instead of being retried: a live goal
            # spent all three attempts on a seat behind a session limit.
            dead_seats: set[str] = set()
            attempts = max(
                config.RELEASE_VERIFIER_TRANSPORT_RETRIES + 1, len(verifier_pool))
            for transport_attempt in range(attempts):
                live_seats = [s for s in verifier_pool if s not in dead_seats]
                if not live_seats:
                    break
                seat = live_seats[transport_attempt % len(live_seats)]
                if seat not in enabled_frontier:
                    self.store.log_event(
                        session.session_id, "release_verifier_fallback",
                        {"agent": seat, "reason": "no independent frontier seat could answer"},
                    )
                member = CouncilMember(role=Role.panelist, agent=seat, active=True)
                try:
                    answer = _agent_call(
                        session, self.registry, self.store, member, prompt,
                        timeout_s=config.FRONTIER_VERIFY_TIMEOUT,
                        cwd=review_cwd,
                    )
                    verifier_name = seat
                    break
                except Exception as e:
                    # Do not absorb explicit cancellation; the goal
                    # cancellation path owns that state transition.
                    if isinstance(e, SessionCancelled):
                        raise
                    transport_error = str(e)
                    self.store.log_event(
                        session.session_id, "release_verifier_transport_failed",
                        {"agent": seat, "attempt": transport_attempt + 1,
                         "detail": transport_error[:300]},
                    )
                    if classify_failure(transport_error) in UNAVAILABLE_STATES:
                        dead_seats.add(seat)
                        continue  # another seat may answer now; no backoff
                    if transport_attempt < attempts - 1:
                        time.sleep(config.RELEASE_VERIFIER_TRANSPORT_BACKOFF)
            if answer is None:
                session.quality_gate = {
                    "verifier": verifier_name, "verdict": "UNAVAILABLE",
                    "detail": (
                        "release verifier did not run (transport failure): "
                        f"{transport_error}"
                    )[:400],
                    "browser_acceptance": browser_evidence,
                }
                session.unresolved.append(
                    "release verification could not run — the verifier seat was "
                    "unavailable; the batch was NOT judged. Resume the goal to "
                    "retry verification."
                )
                session.stop_reason = "frontier release verifier unavailable"
                session.outcome = "failed_verification"
                self.manager.transition(session, SessionStatus.composing)
                session.final = FinalAnswer(
                    answer=(
                        "The assembled batch passed deterministic and browser "
                        "checks but its independent verification could not run."
                    ),
                    confidence="low", risks_unresolved=list(session.unresolved),
                    next_action="Resume the goal to retry the release verifier.",
                )
                self.manager.transition(session, SessionStatus.failed)
                self.store.save_session(session)
                return False
            criteria = rounds.canonical_acceptance_criteria(session)
            report = rounds.parse_frontier_review(
                answer.content,
                criteria,
                checkpoint_id=(goal.active_verified_checkpoint_id
                               or session.base_checkpoint_id),
                reviewer=verifier_name,
            )
            session.review_attempts.append(report)
            goal.review_attempts.append(report)
            verdict, checks, defects = rounds.parse_frontier_verdict(answer.content)
            if report.status == ReviewStatus.protocol_invalid:
                # No CHECK lines, no DEFECT lines, no VERDICT at all — the
                # turn produced real output (this is not a transport
                # failure) but never performed an actual review. A real
                # goal saw a verifier CLI propose exploratory tool calls
                # ("Tool: bash" ... list the directory) and stop there
                # instead of reading the results and continuing; treating
                # that as a genuine rejection burned a fault-streak attempt
                # on the package owner and overwrote real defects an
                # earlier round had already found with a meaningless
                # "missing acceptance checks: ALL" placeholder. Retry with
                # a fresh call before ever attributing this to the code.
                self.store.log_event(
                    session.session_id, "release_verifier_protocol_invalid",
                    {"agent": verifier_name, "attempt": attempt + 1,
                     "response_chars": len(answer.content or ""),
                     "detail": report.protocol_detail},
                )
                if attempt + 1 < config.FRONTIER_VERIFY_ATTEMPTS:
                    continue
                session.quality_gate = {
                    "verifier": verifier_name, "verdict": "UNAVAILABLE",
                    "detail": (
                        "release verifier did not complete a review (incomplete "
                        "turn, no checks or defects emitted)"
                    ),
                    "review_status": report.status.value,
                    "checkpoint_id": report.checkpoint_id,
                    "browser_acceptance": browser_evidence,
                }
                session.unresolved.append(
                    "release verification could not complete — the verifier's "
                    "turn ended without reviewing the release; the batch was NOT "
                    "judged. Resume the goal to retry verification."
                )
                session.stop_reason = "frontier release verifier unavailable"
                session.outcome = "failed_verification"
                self.manager.transition(session, SessionStatus.composing)
                session.final = FinalAnswer(
                    answer=(
                        "The assembled batch passed deterministic and browser "
                        "checks but its independent verification did not complete."
                    ),
                    confidence="low", risks_unresolved=list(session.unresolved),
                    next_action="Resume the goal to retry the release verifier.",
                )
                self.manager.transition(session, SessionStatus.failed)
                self.store.save_session(session)
                return False
            checks = [
                {
                    "id": item.criterion_id,
                    "status": item.status.upper(),
                    "detail": item.detail,
                }
                for item in report.criteria
            ]
            defects = [
                str(item.get("description") or "")
                for item in report.defects if item.get("description")
            ]
            # Advisory findings stay visible in remaining_defects but are never
            # typed as release-blocking or routed to the producer as repairs.
            advisory = {
                str(item.get("description") or "")
                for item in report.defects if not item.get("blocks_release", True)
            }
            verdict = (
                "PASS" if report.status in {
                    ReviewStatus.passed, ReviewStatus.nonblocking
                } else "FAIL"
            )
            # Reviewers diagnose immutable checkpoint bytes; they never become
            # an untracked second author. Any attempted rewrite is evidence of
            # a protocol/ownership violation and routes back to the producer.
            proposed_repairs = [action for action in parse_proposals(
                session.session_id, answer.content, Role.implementer)
                if action.kind in ("edit_file", "write_file")
                and action.filename in session.required_files
            ]
            if proposed_repairs:
                verdict = "FAIL"
                defects.append(
                    "release reviewer attempted to rewrite owner-controlled bytes; "
                    "route the diagnosed issue to the accountable producer"
                )
            if browser_failures:
                verdict = "FAIL"
                defects = list(dict.fromkeys(browser_failures + defects))
            missing_checks: list[str] = []
            default_targets = list(dict.fromkeys(
                path
                for package in release_packages
                for path in (
                    package.materialization_plan.producing_files
                    if package.materialization_plan else package.required_files
                )
            )) if len(release_packages) == 1 else []
            blocking_defects = [
                {
                    "criterion_id": str(check.get("id") or ""),
                    "description": str(check.get("detail") or ""),
                    "severity": "error",
                    "blocks_release": True,
                    "observed_checkpoint_id": report.checkpoint_id,
                    "target_producer_paths": default_targets,
                }
                for check in checks
                if str(check.get("status") or "").upper() == "FAIL"
            ]
            blocking_defects.extend({
                "criterion_id": "",
                "description": defect,
                "severity": "error",
                "blocks_release": True,
                "observed_checkpoint_id": report.checkpoint_id,
                "target_producer_paths": default_targets,
            } for defect in defects if defect not in advisory)
            session.quality_gate = {
                "verifier": verifier_name,
                "verdict": verdict,
                "review_status": report.status.value,
                "checkpoint_id": report.checkpoint_id,
                "checks": checks,
                "remaining_defects": defects,
                "blocking_defects": blocking_defects,
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
            # A valid semantic FAIL ends this reviewer pass immediately. The
            # typed defect below opens a producer-owned repair branch; only a
            # protocol-invalid turn is retried against the unchanged checkpoint.
            break

        detail = session.quality_gate.get("detail") or (
            "; ".join(session.quality_gate.get("remaining_defects") or [])
            or "semantic acceptance did not pass"
        )
        retry_defects = list(session.quality_gate.get("remaining_defects") or [])
        # A verifier that FAILs specific acceptance checks without a usable
        # repair leaves its whole critique inside `checks`; losing it here
        # made the next verification start from scratch and stranded the
        # goal with no path back to the responsible package.
        for check in (session.quality_gate.get("checks") or []):
            if (str(check.get("status") or "").upper() == "FAIL"
                    and check.get("detail")):
                entry = f"acceptance {check.get('id') or 'check'}: {check['detail']}"
                if entry not in retry_defects:
                    retry_defects.append(entry)
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
            next_action="Repair the accountable producer, then rerun objective and semantic verification.",
        )
        self.manager.transition(session, SessionStatus.failed)
        self.store.save_session(session)
        return False

    @staticmethod
    def _goal_call_budget(goal: Goal) -> int:
        """Effective model-call budget: per-goal override, else the config
        default. <= 0 disables the cap."""
        return goal.model_calls_budget or config.GOAL_MAX_MODEL_CALLS

    @staticmethod
    def _goal_cost_report(goal: Goal) -> str:
        by_seat = ", ".join(
            f"{seat} {count}" for seat, count in sorted(
                goal.model_calls_by_seat.items(), key=lambda kv: (-kv[1], kv[0]))
        ) or "no completed calls"
        return f"{goal.model_calls_used} model calls ({by_seat})"

    def _successful_session_calls(self, session: Session) -> dict[str, int]:
        """Return exact successful calls, with a legacy-session fallback.

        Current sessions increment ``successful_agent_calls`` at the same
        transaction boundary as ``agent_call_finished``. Older saved sessions
        predate that field, so recover the authoritative events from their log.
        Only fixtures/very old sessions without either signal fall back to
        contributions; those are capped at attempted calls so deterministic
        coordinator summaries cannot inflate the total.
        """
        explicit = {
            str(seat): int(count)
            for seat, count in (session.successful_agent_calls or {}).items()
            if int(count) > 0
        }
        if explicit:
            return explicit

        from_events: dict[str, int] = {}
        path = self.store.session_log_path(session.session_id)
        if path.exists():
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    record = json.loads(line)
                    if record.get("event") != "agent_call_finished":
                        continue
                    seat = str((record.get("payload") or {}).get("agent") or "")
                    if seat:
                        from_events[seat] = from_events.get(seat, 0) + 1
            except (OSError, json.JSONDecodeError):
                from_events = {}
        if from_events:
            return from_events

        remaining = max(0, int(session.agent_call_attempts))
        legacy: dict[str, int] = {}
        for contribution in session.contributions:
            if remaining <= 0:
                break
            seat = str(contribution.agent or "")
            if not seat or seat == "system":
                continue
            legacy[seat] = legacy.get(seat, 0) + 1
            remaining -= 1
        return legacy

    def _count_goal_session(self, goal: Goal, session: Session) -> None:
        """Fold a session's spend into the goal ledger exactly once.

        agent_call_attempts includes timeouts and transport failures — those
        cost real time and often real money, so they count; contributions
        attribute the completed calls per seat for the report.
        """
        if session.session_id in goal.counted_session_ids:
            return
        goal.counted_session_ids.append(session.session_id)
        attempts = int(session.agent_call_attempts)
        reserved = [
            item for item in goal.model_call_reservations
            if item.get("session_id") == session.session_id
        ]
        unreserved_attempts = max(0, attempts - len(reserved))
        goal.model_calls_used += unreserved_attempts
        successful = self._successful_session_calls(session)
        # New calls were attributed to their dispatched seat at reservation
        # time. Only legacy/unreserved attempts need the old terminal fold.
        if not reserved:
            for seat, count in successful.items():
                goal.model_calls_by_seat[seat] = (
                    goal.model_calls_by_seat.get(seat, 0) + int(count))
        unattributed = (
            max(0, attempts - sum(int(n) for n in successful.values()))
            if not reserved else unreserved_attempts
        )
        if unattributed:
            goal.model_calls_by_seat["unattributed_attempts"] = (
                goal.model_calls_by_seat.get("unattributed_attempts", 0)
                + unattributed
            )

    def _pause_goal_over_budget(self, goal: Goal) -> bool:
        """Phase 3 cap: a goal at/over budget pauses with a cost report
        instead of spending further; mutates the goal only — callers own
        persistence. Resume grants another default block."""
        budget = self._goal_call_budget(goal)
        if budget <= 0 or goal.model_calls_used < budget:
            return False
        if goal.approval_policy == ApprovalPolicy.god_mode:
            goal.status = "failed"
            goal.recovery_state = RecoveryState.manual_intervention_required
            goal.last_error = (
                f"goal call budget exhausted after {self._goal_cost_report(goal)} "
                f"against {budget}; God mode will not wait for an approval or "
                "silently buy more attempts"
            )[:300]
            self._sys_log("goal_budget_exhausted",
                {"goal_id": goal.goal_id, "used": goal.model_calls_used,
                 "budget": budget, "policy": "god_mode"},
            )
            return True
        goal.status = "paused"
        goal.last_error = (
            f"goal call budget reached: {self._goal_cost_report(goal)} against "
            f"a budget of {budget}; pausing for cost review — resume grants "
            f"another {config.GOAL_MAX_MODEL_CALLS} calls"
        )[:300]
        self._sys_log("goal_budget_reached",
            {"goal_id": goal.goal_id, "used": goal.model_calls_used,
             "budget": budget, "by_seat": dict(goal.model_calls_by_seat)},
        )
        return True

    def _grant_budget_extension(self, goal_id: str) -> None:
        """An explicit human resume of a budget-paused goal IS the cost
        review: grant one more default block and clear the pause reason."""
        goal = self.goals.claim_worker_lease(goal_id, {"paused"})
        if goal is None:
            return
        token = goal.worker_lease
        try:
            goal.model_calls_budget = (
                self._goal_call_budget(goal) + config.GOAL_MAX_MODEL_CALLS)
            goal.last_error = ""
            self.goals.save_owned(goal, token)
            self._sys_log("goal_budget_extended",
                {"goal_id": goal_id, "budget": goal.model_calls_budget},
            )
        finally:
            self.goals.release_worker_lease(goal_id, token)

    def _mark_stale_assembly_packages(self, goal: Goal) -> bool:
        """Reopen any done assembly package whose accepted output was built
        from input bytes other packages have since rebuilt.

        Deterministic assembly records the exact source/template hashes it
        expanded (``assembly_result``). If a provider package was rebuilt
        afterwards, the accepted HTML is a fossil of superseded inputs —
        releasing it re-verifies (and re-fails on) defects the inputs no
        longer have. One real goal recovered a stale assembly this way and
        was headed into a frontier release of an HTML expanded from the old
        stylesheet. Mutates the goal only; callers own persistence and
        scheduling. Returns True when a package was reopened.
        """
        current_accepted: dict[str, str] = {}
        for package in goal.milestones:
            if package.status == "done":
                current_accepted.update(package.accepted_hashes)
        reopened = False
        for package in goal.milestones:
            if package.status != "done" or not package.session_id:
                continue
            if self._assembly_contract(package)[0] != assembly.HTML_INLINE:
                continue
            session = self.manager.load(package.session_id)
            if session is None:
                continue
            recorded = dict(
                (session.assembly_result or {}).get("source_hashes") or {})
            template_name = (session.assembly_template or "").replace("\\", "/")
            template_hash = str(
                (session.assembly_result or {}).get("template_hash") or "")
            if template_name and template_hash:
                recorded.setdefault(template_name, template_hash)
            stale = sorted(
                name for name, digest in recorded.items()
                if current_accepted.get(name) not in (None, digest)
            )
            if not stale:
                continue
            package.status = "pending"
            package.session_id = None
            goal.current_index = package.index
            goal.release_status = "not_started"
            goal.release_session_id = None
            goal.last_error = (
                "accepted assembly output is stale — inputs were rebuilt "
                "after acceptance (" + ", ".join(stale[:6]) + "); reassembling"
            )
            self._sys_log("assembly_reopened_stale_inputs",
                {"goal_id": goal.goal_id, "package": package.index + 1,
                 "stale": stale[:12]},
            )
            reopened = True
        return reopened

    def _reusable_goal_release(
        self, goal: Goal, files: list[str],
    ) -> Optional[Session]:
        """Return a previously verified release turn whose bytes still match.

        A server/process fault can happen after semantic verification but before
        the final promotion transaction.  Repeating the frontier review spends
        money without adding evidence.  Reuse is therefore allowed only when
        every sealed release hash still matches the current staging bytes.
        """
        candidates: list[Session] = []
        for meta in self.store.list_sessions(limit=None):
            if meta.get("goal_id") != goal.goal_id:
                continue
            session_id = str(meta.get("session_id") or "")
            session = self.manager.load(session_id) if session_id else None
            if session is None or not session.goal_release:
                continue
            if session.status not in {
                SessionStatus.deliberating, SessionStatus.awaiting_approval,
            }:
                continue
            if session.outcome != "succeeded":
                continue
            if str((session.quality_gate or {}).get("verdict") or "").upper() != "PASS":
                continue
            hashes = dict(session.release_verified_hashes or {})
            if any(not hashes.get(name) for name in files):
                continue
            stage = Path(goal.staging_root)
            valid = True
            for name in files:
                try:
                    path = executor.resolve_in_workspace(stage, name)
                    valid = bool(
                        path.is_file()
                        and hashlib.sha256(path.read_bytes()).hexdigest()
                        == hashes[name]
                    )
                except (OSError, executor.ExecutionError):
                    valid = False
                if not valid:
                    break
            if valid:
                candidates.append(session)
        return max(candidates, key=lambda item: item.updated_at) if candidates else None

    def _resume_reusable_goal_release(
        self, goal: Goal, session: Session,
    ) -> None:
        """Resume the deterministic promotion step without another model call."""
        goal.release_session_id = session.session_id
        goal.status = "awaiting_release"
        goal.release_status = "awaiting_approval"
        goal.phase = "semantic_review_passed"
        self.goals.save(goal)

        pending = next(
            (approval for approval in session.approvals
             if approval.status == "pending"),
            None,
        )
        if pending is not None and goal.approval_policy != ApprovalPolicy.god_mode:
            if session.status == SessionStatus.deliberating:
                self.manager.transition(session, SessionStatus.awaiting_approval)
            self.store.save_session(session)
            return

        # A promotion authorization captures destination baselines. Re-create
        # it after an interrupted transaction so a file changed while the app
        # was down is detected against a fresh, explicit audit record. God mode
        # resolves the new in-contract action immediately; manual mode asks once.
        if session.status == SessionStatus.awaiting_approval:
            self.manager.transition(session, SessionStatus.deliberating)
        for approval in session.approvals:
            if approval.status == "pending":
                approval.status = "denied"
                approval.resolved_at = utcnow()
                approval.resolved_by = "release_reconciliation_superseded"
                self.store.log_event(
                    session.session_id,
                    "approval_superseded",
                    {"approval_id": approval.approval_id,
                     "reason": "fresh destination baseline required"},
                )
        session.proposed_actions = [
            action for action in session.proposed_actions
            if action.kind != "promote_batch"
        ]
        self.store.save_session(session)
        self.store.log_event(
            session.session_id,
            "verified_release_resumed",
            {"goal_id": goal.goal_id, "model_calls_added": 0},
        )
        self._authorize_goal_release(session, linked_goal=goal)

    def _prepare_goal_release(self, goal: Goal) -> None:
        """Create the final review session after every package has staged cleanly."""
        if self._mark_stale_assembly_packages(goal):
            # A pending assembly package now exists; the caller's normal
            # scheduling path rebuilds it from current inputs before any
            # release session is opened or paid for.
            return
        if self._pause_goal_over_budget(goal):
            # Release verification spends frontier calls; a goal at budget
            # pauses for cost review before opening the session.
            return
        files = self._goal_release_files(goal)
        goal.release_files = files
        if not files:
            if goals.requires_delivery_contract(goal.text):
                goal.status = "paused"
                goal.release_status = "failed"
                goal.last_error = "final release has no verified output files"
                self._sys_log("goal_release_blocked",
                    {"goal_id": goal.goal_id, "reason": goal.last_error},
                )
                return
            goal.status = "completed"
            goal.release_status = "released"
            self._sys_log("goal_completed", {"goal_id": goal.goal_id})
            return
        reusable = self._reusable_goal_release(goal, files)
        if reusable is not None:
            self._resume_reusable_goal_release(goal, reusable)
            return
        session = self._open(
            f"[FINAL BATCH RELEASE] {goal.text}\nReview and release all staged package outputs together.",
            "goal-release", None, None,
            outcome_contract=goal.outcome_contract,
            execution_profile=goal.execution_profile,
            routing_decision=goal.routing_decision,
            playbook_id=goal.playbook_id,
            approval_policy=goal.approval_policy,
        )
        session.goal_id = goal.goal_id
        session.goal_epoch = goal.epoch
        session.goal_release = True
        # Semantic verification must evaluate the original brief, not the short
        # coordinator wrapper used to create this release session.
        session.task.text = goal.text
        session.task.original_text = goal.text
        if goal.criteria:
            session.criteria = list(goal.criteria)
        else:
            goal.criteria = rounds.canonical_acceptance_criteria(session)
        session.base_checkpoint_id = goal.active_verified_checkpoint_id
        session.phase = "objective_validated"
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
        goal.status = "awaiting_release"
        goal.release_status = "verifying"
        goal.phase = "semantic_review"
        # The release session link is a prerequisite of God mode's synchronous
        # promotion. Persist it before verification/authorization so the final
        # transaction can never reload a stale goal and report an incomplete
        # release state after all artifact gates passed.
        self.goals.save(goal)
        verified = self._verify_goal_release(goal, session)
        self._count_goal_session(goal, session)
        if not verified:
            gate = dict(session.quality_gate or {})
            blocking_defects = list(gate.get("blocking_defects") or [])
            release_packages = [
                package for package in goal.milestones if package.release_files
            ]
            diagnostic = session.stop_reason or "frontier final-batch verification failed"
            # The owner must see WHAT the reviewer found, not only that it
            # failed: a live repair was sent "verification failed" and rewrote
            # a verified 80KB generator blind.
            findings = [
                (f"{item.get('criterion_id')}: " if item.get("criterion_id") else "")
                + str(item.get("description") or "").strip()
                for item in blocking_defects
                if str(item.get("description") or "").strip()
            ]
            if findings:
                diagnostic = (
                    "release reviewer found: " + " | ".join(dict.fromkeys(findings))
                )[:1200]
            target_package = next(
                (
                    package for package in release_packages
                    if set(
                        name.replace("\\", "/")
                        for item in blocking_defects
                        for name in (
                            item.get("target_producer_paths") or []
                        )
                    ) & set(
                        name.replace("\\", "/") for name in [
                            *package.required_files,
                            *package.release_files,
                            *(
                                package.materialization_plan.producing_files
                                if package.materialization_plan else []
                            ),
                        ]
                    )
                ),
                release_packages[0] if len(release_packages) == 1 else None,
            )
            auto_recover = bool(
                target_package is not None
                and gate.get("verdict") == "FAIL"
                and (
                    (
                        blocking_defects
                        and any(
                            item.get("target_producer_paths")
                            for item in blocking_defects
                        )
                    )
                    or gate.get("stage") in {
                    "release_format_validation", "deterministic_assembly_release"
                    }
                )
                and self._recover_failed_milestone(
                    goal, target_package, session,
                    "release_verification_rejected", diagnostic,
                )
            )
            if auto_recover:
                goal.release_status = "not_started"
                goal.release_session_id = None
                return
            failure = recovery.record_failure(
                goal,
                stage="release_verification",
                category="verification_failed",
                summary=session.stop_reason or "frontier final-batch verification failed",
                evidence={"session_id": session.session_id,
                          "quality_gate": gate},
                responsible_owner=str((session.quality_gate or {}).get("verifier") or ""),
            )
            # A reviewer that could not RUN did not judge the batch: that is a
            # capacity outage, retryable once a seat recovers, never an
            # exhausted recovery. Only a real rejection fails a God-mode goal.
            verifier_unavailable = gate.get("verdict") == "UNAVAILABLE"
            goal.status = (
                "failed"
                if goal.approval_policy == ApprovalPolicy.god_mode
                and not verifier_unavailable
                else "paused"
            )
            if goal.status == "failed":
                recovery.mark_exhausted(goal, failure)
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
            goal.phase = "semantic_review_passed"
            self.goals.save(goal)
            self._authorize_goal_release(session, linked_goal=goal)
            # God mode can authorize and complete the release synchronously.
            # Reflect that durable result in the leased object the caller is
            # about to save, instead of overwriting it with awaiting_release.
            if goal.approval_policy == ApprovalPolicy.god_mode:
                persisted = self.goals.get(goal.goal_id)
                if persisted is not None:
                    goal.status = persisted.status
                    goal.release_status = persisted.release_status
                    goal.last_error = persisted.last_error
                    goal.recovery_state = persisted.recovery_state
                    goal.phase = persisted.phase
                    goal.work_items = list(persisted.work_items)

    @staticmethod
    def _assembly_runtime_interface_hint(
        loaded_sources: list[tuple[str, str]],
        all_sources: Optional[list[tuple[str, str]]] = None,
    ) -> str:
        """Describe browser-global paths earlier scripts require at runtime.

        Runtime errors such as ``cannot set ... fire`` reveal only the final
        property and caused expensive whole-package retries to add one field
        while dropping another. Recover the complete cross-file surface from
        the accepted consumers, including aliases such as
        ``var portalInput = window.NS.input``. Works for ANY shared window
        namespace the sources establish — an earlier version was hardcoded
        to one past project's ``ArcadePortal.input`` and silently did
        nothing for every other build.
        """
        namespaces: set[str] = set()
        # Roots may be DECLARED by a file that loads after the consumers being
        # scanned (that is often the fault itself), so detect them across the
        # whole declared order, not only the already-loaded prefix.
        for _name, text in (all_sources or loaded_sources):
            namespaces.update(
                m.group(1) for m in _ASSEMBLY_NS_ROOT_RE.finditer(text))
            namespaces.update(
                m.group(1) for m in _ASSEMBLY_WINDOW_ROOT_RE.finditer(text))
        if not namespaces:
            return ""
        requirements: list[tuple[str, list[str]]] = []
        for source_name, text in loaded_sources:
            paths: set[str] = set()
            for ns in sorted(namespaces):
                chains = [
                    m.group(1) for m in re.finditer(
                        rf"(?:window\.)?{re.escape(ns)}"
                        r"((?:\.[A-Za-z_$][\w$]*)+)",
                        text,
                    )
                ]
                alias_decl = re.compile(
                    rf"\b(?:var|let|const)\s+([A-Za-z_$][\w$]*)\s*=\s*"
                    rf"(?:window\.)?{re.escape(ns)}"
                    r"((?:\.[A-Za-z_$][\w$]*)+)\b"
                )
                for alias, base in alias_decl.findall(text):
                    for m in re.finditer(
                            rf"\b{re.escape(alias)}((?:\.[A-Za-z_$][\w$]*)+)",
                            text):
                        chains.append(base + m.group(1))
                for chain in chains:
                    pieces = [piece for piece in chain.split(".") if piece]
                    for size in range(1, len(pieces) + 1):
                        paths.add(f"{ns}." + ".".join(pieces[:size]))
            if paths:
                ordered = sorted(paths, key=lambda path: (path.count("."), path))
                requirements.append((source_name, ordered[:14]))

        if not requirements:
            return ""
        clauses = [
            f"{name} requires window paths [{', '.join(paths)}]"
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
        # Both class and id hooks count, exactly like the browser gate: a page
        # fully styled through #id selectors is a styled page. Keep this in
        # lockstep with browser_acceptance._style_contract_errors.
        used: set[str] = set()
        for quoted in re.finditer(r"class\s*=\s*\"([^\"]*)\"", html):
            used.update("." + name for name in quoted.group(1).split())
        for quoted in re.finditer(r"class\s*=\s*'([^']*)'", html):
            used.update("." + name for name in quoted.group(1).split())
        for quoted in re.finditer(r"id\s*=\s*\"([^\"]+)\"", html):
            used.add("#" + quoted.group(1).strip())
        for quoted in re.finditer(r"id\s*=\s*'([^']+)'", html):
            used.add("#" + quoted.group(1).strip())
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
            token.group(0)
            for token in re.finditer(r"[.#][_a-zA-Z][_a-zA-Z0-9-]*", css_text)
        }
        matched = used & covered
        if len(matched) / len(used) >= 0.35:
            return "", ""
        missing = sorted(used - covered)
        target = css_names[0]
        detail = (
            f"{target} addresses only {len(matched)} of the {len(used)} "
            f"class/id hooks that the template {template} actually uses. The "
            "template markup and the stylesheet are ONE contract: keep the "
            "template's existing class and id names and write real rules for "
            "them — do NOT invent a different naming scheme. Hooks needing "
            "rules: " + ", ".join(missing[:24])
        )
        return target, detail

    def _semantic_release_target(
        self, release_session: Session, assembly_session: Session,
    ) -> tuple[str, str]:
        """Map a frontier verifier's FAILed acceptance checks to the staged
        file whose code they criticize.

        A verifier that fails the release on gameplay/content grounds (e.g.
        "``LANES`` places river rows below the median") without a usable
        inline repair used to strand the goal: the critique had no path back
        to the package that owns the criticized code, so resume could only
        re-run an identical verification. The check details quote the
        identifiers they judged in backticks; the file that DECLARES such an
        identifier (const/let/var/function/class) is deterministic to find,
        and its owner package is the right recipient of the critique. Files
        named outright in a detail count too. Returns ``(path, detail)`` with
        the matched critiques verbatim, or ``("", "")`` when nothing maps
        unambiguously.
        """
        checks = (release_session.quality_gate or {}).get("checks") or []
        failed = [
            check for check in checks
            if str(check.get("status") or "").upper() == "FAIL"
            and check.get("detail")
        ]
        if not failed or not assembly_session.workspace_root:
            return "", ""
        sources = list(
            (assembly_session.assembly_result or {}).get("sources") or [])
        template = (assembly_session.assembly_template or "").replace("\\", "/")
        if template:
            sources.append(template)
        loaded: list[tuple[str, str]] = []
        for name in sources:
            normalized = str(name).replace("\\", "/")
            try:
                text = executor.resolve_in_workspace(
                    Path(assembly_session.workspace_root), normalized,
                ).read_text(encoding="utf-8", errors="replace")
            except (OSError, executor.ExecutionError):
                continue
            loaded.append((normalized, text))
        if not loaded:
            return "", ""
        votes: dict[str, list[str]] = {}
        for check in failed:
            detail_text = str(check["detail"])
            entry = f"{check.get('id') or 'check'}: {detail_text}"
            matched_files: set[str] = set()
            for name, _text in loaded:
                if name in detail_text or Path(name).name in detail_text:
                    matched_files.add(name)
            for ident in set(re.findall(r"`([A-Za-z_$][\w$]{2,})`", detail_text)):
                definers = [
                    name for name, text in loaded
                    if re.search(
                        rf"\b(?:const|let|var|function|class)\s+{re.escape(ident)}\b",
                        text)
                ]
                if len(definers) == 1:
                    matched_files.add(definers[0])
            for name in matched_files:
                votes.setdefault(name, []).append(entry)
        if not votes:
            return "", ""
        blamed = max(votes, key=lambda name: len(votes[name]))
        return blamed, " | ".join(dict.fromkeys(votes[blamed]))

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
                    legacy = self._assembly_runtime_interface_hint(
                        loaded_sources, ordered)
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
        # Escalation ladder (ARCHITECTURE-REVIEW.md Phase 2): after the owner
        # has had its constrained retry, the package transfers to the
        # strongest frontier seat rather than going back to the same seat
        # with the same brief — one real fault cycled owner -> rebuild ->
        # same fault three times before the breaker finally paused it.
        escalated_from = ""
        if streak >= config.ASSEMBLY_FAULT_ESCALATE_AT:
            replacement = next(
                (seat for seat in self._frontier_seats()
                 if seat in self.panel and seat != provider.owner
                 and not self.seat_health.is_unavailable(seat)),
                None,
            )
            if replacement:
                escalated_from = provider.owner
                provider.owner = replacement
                self.store.log_event(
                    session.session_id, "assembly_fault_escalated",
                    {
                        "goal_id": goal.goal_id,
                        "provider_package": provider.index + 1,
                        "from_owner": escalated_from,
                        "to_owner": replacement,
                        "streak": streak,
                        "input": invalid_input,
                    },
                )
        provider.status = "failed"
        # Open a repair branch without destroying the accepted manifest. The
        # replacement may inspect these bytes, but cannot supersede them until
        # its own candidate passes the objective gates and is sealed.
        provider.repair_context = {
            "reason": "deterministic_assembly_failure",
            "fault_scope": scope,
            "fault_path": invalid_input,
            "base_checkpoint_id": provider.active_verified_checkpoint_id,
            "failed_session_id": session.session_id,
        }
        guidance = ""
        if "@import" in detail and "DELETE the @import" not in detail:
            # Sessions saved before the assembler's message became
            # prescriptive carry only the symptom; a real owner re-added its
            # Google-Fonts @import three times in a row on that text alone.
            guidance = (
                " Fix: the release is ONE self-contained HTML file with zero "
                "network fetches — DELETE the @import line entirely and use "
                "widely installed font stacks instead, e.g. font-family: "
                "'Courier New', monospace."
            )
        takeover = ""
        if escalated_from:
            takeover = (
                f" OWNERSHIP TAKEOVER: {escalated_from} failed to resolve this "
                f"same fault {streak - 1} time(s); you ({provider.owner}) are "
                "taking the package over. Author your own complete replacement "
                "from the contract — do not imitate the failed approach."
            )
        # 600, not 300: this exact text becomes the owner's RETRY CORRECTION
        # prompt, and load-order faults need room to state the constraint the
        # rebuild must satisfy — truncating it re-creates the blind rebuild
        # that reproduced the same defect.
        provider.acceptance_detail = (
            f"invalidated by deterministic assembly {scope}: {detail}{guidance}{takeover}"
        )[:700]
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
                # Fold this terminal session's spend into the goal ledger
                # before any branch saves; idempotent per session.
                self._count_goal_session(goal, session)
                self._merge_goal_research_provenance(
                    goal, milestone, session
                )
                if session.status == SessionStatus.done:
                    accepted, files, detail = self._goal_acceptance(session, milestone)
                    milestone.acceptance_detail = detail
                    if not accepted:
                        milestone.status = "failed"
                        auto_recover = self._recover_failed_milestone(
                            goal, milestone, session,
                            "acceptance_rejected", detail,
                        )
                        sibling_running = any(
                            item.status == "running" and item.index != idx
                            for item in goal.milestones
                        )
                        if not auto_recover and goal.status != "failed":
                            goal.status = "draining" if sibling_running else "paused"
                            goal.last_error = detail[:300]
                        self.goals.save_owned(goal, token)
                        self.store.log_event(session.session_id, "goal_milestone_rejected",
                                             {"goal_id": goal.goal_id, "milestone": idx + 1,
                                              "reason": detail,
                                              "auto_recovery": auto_recover})
                        if auto_recover:
                            self._pool.submit(
                                self._start_ready_packages,
                                goal.model_copy(deep=True), background,
                            )
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
                    checkpoint_paths: dict[str, Path] = {}
                    if goal.delivery_mode == "final_batch":
                        for name in milestone.accepted_hashes:
                            try:
                                checkpoint_paths[name] = executor.resolve_in_workspace(
                                    Path(goal.staging_root), name
                                )
                            except executor.ExecutionError:
                                continue
                    else:
                        checkpoint_paths = {
                            name: Path(path) for name, path in zip(
                                milestone.required_files, files
                            ) if Path(path).is_file()
                        }
                    if checkpoint_paths:
                        checkpoint = self.checkpoints.seal_paths(
                            goal_id=goal.goal_id,
                            package_id=milestone.package_id,
                            session_id=session.session_id,
                            paths=checkpoint_paths,
                            expected_hashes=milestone.accepted_hashes,
                            parent_id=milestone.active_verified_checkpoint_id,
                            state="verified",
                            evidence={
                                "phase": "objective_validated",
                                "repair_mode": session.repair_mode,
                            },
                        )
                        milestone.active_verified_checkpoint_id = checkpoint[
                            "checkpoint_id"
                        ]
                        milestone.candidate_checkpoint_id = ""
                        session.candidate_checkpoint_id = checkpoint["checkpoint_id"]
                        goal.active_verified_checkpoint_id = checkpoint[
                            "checkpoint_id"
                        ]
                    if session.materialization_plan is not None:
                        milestone.materialization_plan = session.materialization_plan
                    milestone.last_good_checkpoint = dict(
                        session.last_good_checkpoint or {}
                    )
                    accepted_goal_hashes: dict[str, str] = {}
                    for completed_package in goal.milestones:
                        if completed_package.status == "done":
                            accepted_goal_hashes.update(
                                completed_package.accepted_hashes
                            )
                    recovery.seal_checkpoint(
                        goal,
                        checkpoint_id=milestone.active_verified_checkpoint_id,
                        package_id=milestone.package_id,
                        session_id=session.session_id,
                        artifact_hashes=accepted_goal_hashes,
                    )
                    active_repair = next(
                        (attempt for attempt in reversed(goal.repair_history)
                         if attempt.status == "started"
                         and attempt.owner == milestone.owner),
                        None,
                    )
                    if active_repair is not None:
                        recovery.finish_repair(
                            goal, active_repair, verified=True,
                            changed_files=milestone.accepted_files,
                            after_hashes=milestone.accepted_hashes,
                            verification_evidence={
                                "acceptance_detail": milestone.acceptance_detail,
                            },
                            result_checkpoint_id=(
                                milestone.active_verified_checkpoint_id
                            ),
                        )
                    if session.collaboration_assignments:
                        milestone.participation_reports = [
                            item.model_dump()
                            for item in session.collaboration_assignments
                        ]
                        baseline_identity = hashlib.sha256(
                            json.dumps(
                                session.collaboration_baseline,
                                sort_keys=True,
                            ).encode("utf-8")
                        ).hexdigest()[:24]
                        milestone.participation_checkpoint_id = (
                            "baseline_" + baseline_identity
                        )
                    milestone.repair_context = {}
                    milestone.resume_session_id = ""
                    milestone.phase = "objective_validated"
                    goal.phase = "objective_validated"
                    self._record_work_item(
                        goal,
                        milestone.package_id,
                        "objective_validated",
                        "completed",
                        session_id=session.session_id,
                        checkpoint_id=milestone.active_verified_checkpoint_id,
                    )
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
                            self._sys_log("goal_drained",
                                {"goal_id": goal.goal_id, "reason": goal.last_error},
                            )
                        self.goals.save_owned(goal, token)
                        return
                    if not remaining:
                        if goal.delivery_mode == "final_batch" and goal.status == "running":
                            self._prepare_goal_release(goal)
                        elif goal.delivery_mode != "final_batch":
                            goal.status = "completed"
                            self._sys_log("goal_completed", {"goal_id": goal.goal_id})
                        if self.goals.save_owned(goal, token):
                            # Release prep may instead have reopened a stale
                            # assembly package; that rebuild must be scheduled,
                            # not left waiting for a human resume.
                            schedule_ready = goal.status == "running" and any(
                                m.status == "pending" for m in goal.milestones
                            )
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
                        self._sys_log((
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
                        if goal.approval_policy == ApprovalPolicy.god_mode:
                            goal.status = "failed"
                            goal.recovery_state = RecoveryState.manual_intervention_required
                            goal.last_error = (
                                "manual intervention required: " + goal.last_error
                            )[:300]
                        self.goals.save_owned(goal, token)
                        self._sys_log("goal_paused",
                            {"goal_id": goal.goal_id, "reason": goal.last_error},
                        )
                    else:
                        failure_detail = (
                            self._session_seat_outage(session)
                            or session.stop_reason
                            or f"milestone {idx + 1} failed"
                        )
                        auto_recover = self._recover_failed_milestone(
                            goal, milestone, session,
                            "execution_failed", failure_detail,
                        )
                        sibling_running = any(
                            item.status == "running" and item.index != idx
                            for item in goal.milestones
                        )
                        if not auto_recover and goal.status != "failed":
                            goal.status = "draining" if sibling_running else "paused"
                        # Name the SEAT truth when that is the real story: a
                        # quota-capped claude once surfaced as "no file was
                        # delivered" and read as a fatal app error.
                            goal.last_error = failure_detail[:300]
                        self.goals.save_owned(goal, token)
                        if auto_recover:
                            self._pool.submit(
                                self._start_ready_packages,
                                goal.model_copy(deep=True), background,
                            )
                        else:
                            self._sys_log(
                                "goal_draining" if sibling_running else
                                "goal_failed" if goal.status == "failed" else "goal_paused",
                                {"goal_id": goal.goal_id, "reason": goal.last_error},
                            )
                elif session.status == SessionStatus.cancelled:
                    # A restart reconciles an in-flight package as cancelled.
                    # Keep what that attempt learned: its failed producer and
                    # its latest failure. Without this a resume re-authored
                    # from scratch against a stale, symptom-only correction.
                    pending_failure = next(
                        (item for item in reversed(session.failure_records)
                         if item.resolution_state != "resolved"),
                        None,
                    )
                    if pending_failure is not None:
                        self._preserve_failed_producer(
                            goal, milestone, session, pending_failure)
                        summary = pending_failure.summary or ""
                        if len(summary) > 1100:
                            summary = summary[:250] + "\n...\n" + summary[-850:]
                        note = (
                            "\nThe failed producer from that attempt is in your "
                            "working set as an UNVERIFIED repair baseline: fix the "
                            "cause in it and emit the complete corrected file "
                            "rather than starting over."
                            if milestone.candidate_checkpoint_id else ""
                        )
                        milestone.acceptance_detail = (
                            f"{pending_failure.category}: {summary}{note}"
                        )[:1600]
                        if milestone.repair_context:
                            milestone.repair_context["detail"] = (
                                milestone.acceptance_detail)
                    milestone.status = "pending"
                    restart_interrupted = (
                        session.stop_reason == "interrupted by a server restart"
                    )
                    if (restart_interrupted
                            and goal.approval_policy == ApprovalPolicy.god_mode
                            and goal.status == "running"):
                        # Nobody cancelled this: the server restarted under a
                        # God-mode run, which is advance consent to keep going.
                        # Parking it for a manual resume stalled live runs.
                        goal.last_error = (
                            f"milestone {idx + 1} interrupted by a server restart; "
                            "resumed automatically"
                        )
                        if self.goals.save_owned(goal, token):
                            schedule_ready = True
                        self._sys_log("goal_restart_resumed",
                            {"goal_id": goal.goal_id, "milestone": idx + 1},
                        )
                    else:
                        sibling_running = any(
                            item.status == "running" and item.index != idx
                            for item in goal.milestones
                        )
                        goal.status = "draining" if sibling_running else "paused"
                        goal.last_error = f"milestone {idx + 1} was cancelled"
                        self.goals.save_owned(goal, token)
                        self._sys_log(
                            "goal_draining" if sibling_running else "goal_paused",
                            {"goal_id": goal.goal_id, "reason": goal.last_error},
                        )
            finally:
                self.goals.release_worker_lease(goal.goal_id, token)
            if schedule_ready:
                current = self.goals.get(goal.goal_id)
                if current and current.status == "running" and current.epoch == session.goal_epoch:
                    self._start_ready_packages(current, background=background)
        except Exception as e:  # noqa: BLE001
            self._sys_log(
                "goal_advance_error",
                {"goal_id": session.goal_id, "detail": str(e)},
            )
            self._recover_goal_advance_error(session, e, background)

    def _recover_goal_advance_error(
        self, session: Session, error: Exception, background: bool,
    ) -> None:
        """Bound a coordinator-state failure and guarantee a truthful outcome.

        This path does not spend another model call. One deterministic replay is
        allowed because the artifact/session bytes are unchanged; a repeated
        identical coordinator failure becomes an explicit paused/failed state
        instead of an immortal ``running`` goal with no worker.
        """
        if not session.goal_id:
            return
        goal = self.goals.claim_worker_lease(
            session.goal_id,
            {"running", "draining", "paused", "awaiting_release"},
        )
        if goal is None:
            return
        token = goal.worker_lease
        retry = False
        try:
            detail = " ".join(str(error).split())[:1000]
            failure = recovery.record_failure(
                goal,
                stage="coordinator",
                category="goal_advance_error",
                summary=detail or error.__class__.__name__,
                evidence={
                    "session_id": session.session_id,
                    "package_index": session.goal_milestone,
                    "exception_type": error.__class__.__name__,
                },
                responsible_owner="coordinator",
                recoverable=True,
            )
            key = "goal_advance:" + failure.fault_signature
            attempt = int(goal.recovery_attempts.get(key, 0)) + 1
            goal.recovery_attempts[key] = attempt
            terminal_session = session.status in self._TERMINAL
            retry = attempt == 1 and terminal_session
            if retry:
                goal.status = "running"
                goal.recovery_state = RecoveryState.repairing
                goal.last_error = (
                    "automatic coordinator recovery: replaying the terminal "
                    f"goal transition after {detail}"
                )[:300]
                self._record_work_item(
                    goal,
                    "__coordinator__",
                    "reconciling_goal_state",
                    "ready",
                    session_id=session.session_id,
                )
            else:
                milestone = goal.current
                if milestone is not None and milestone.status == "running":
                    milestone.status = "failed"
                    milestone.phase = "recovery_exhausted"
                goal.status = (
                    "failed"
                    if goal.approval_policy == ApprovalPolicy.god_mode
                    else "paused"
                )
                goal.recovery_state = RecoveryState.manual_intervention_required
                goal.phase = "recovery_exhausted"
                goal.last_error = (
                    "coordinator recovery stopped after the same goal-state "
                    f"transition failed {attempt} times: {detail}"
                )[:300]
                self._record_work_item(
                    goal,
                    "__coordinator__",
                    "reconciling_goal_state",
                    "failed",
                    session_id=session.session_id,
                )
            self.goals.save_owned(goal, token)
            self._sys_log(
                "goal_advance_recovery_scheduled" if retry
                else "goal_advance_recovery_exhausted",
                {
                    "goal_id": goal.goal_id,
                    "session_id": session.session_id,
                    "attempt": attempt,
                    "fault_signature": failure.fault_signature,
                    "detail": detail,
                },
            )
        finally:
            self.goals.release_worker_lease(goal.goal_id, token)
        if retry:
            self._pool.submit(
                self._maybe_advance_goal,
                session.model_copy(deep=True),
                background,
            )

    @staticmethod
    def _goal_now_line(goal: Goal, related: list[dict]) -> str:
        """Plain-language current activity for a goal card."""
        if goal.status == "planning":
            active = next(iter(goal.active_agent_calls or []), None)
            if active:
                agent = active.get("agent") or "model"
                chars = int(active.get("progress_chars") or 0)
                return (
                    f"{agent} planning the build"
                    + (f" ({chars:,} characters streamed)" if chars else "")
                )
            return "planning the build"
        if goal.status == "completed":
            followups = [
                item for item in related
                if item.get("parent_session_id")
            ]
            live = [
                item for item in followups
                if item.get("status") not in ("done", "failed", "cancelled")
            ]
            if live:
                current = max(
                    live, key=lambda item: item.get("updated_at") or ""
                )
                targets = current.get("revision_targets") or []
                calls = current.get("active_agent_calls") or []
                actor = next(
                    (
                        call.get("agent")
                        for call in calls
                        if call.get("agent")
                    ),
                    "",
                )
                if targets:
                    files = ", ".join(targets[:2])
                    prefix = f"{actor} revising" if actor else "revising"
                    return f"{prefix} {files}"
                return (
                    f"{actor} reviewing your follow-up"
                    if actor else "reviewing your follow-up"
                )
            revisions = [
                item for item in followups
                if item.get("revision_targets")
            ]
            if revisions:
                latest = max(
                    revisions, key=lambda item: item.get("updated_at") or ""
                )
                if (
                    latest.get("status") == "done"
                    and latest.get("outcome") == "succeeded"
                ):
                    return "released - latest correction applied"
                if latest.get("status") in ("failed", "cancelled") or (
                    latest.get("status") == "done"
                    and latest.get("outcome") != "succeeded"
                ):
                    return "released - latest correction needs attention"
            return "released"
        if goal.status == "cancelled":
            return "cancelled"
        if goal.status == "paused":
            reason = (goal.last_error or "").split(";")[0].strip()
            return f"paused — {reason}" if reason else "paused"
        if goal.release_status == "released" and goal.status != "completed":
            return "delivering the approved release"
        if goal.release_status == "awaiting_approval":
            return "waiting for your release approval"
        if goal.release_status == "awaiting_target":
            return "waiting for a delivery folder"
        running = [m for m in goal.milestones if m.status == "running"]
        if running:
            parts = []
            for package in running[:2]:
                meta = next(
                    (item for item in related
                     if item.get("session_id") == package.session_id), {})
                calls = meta.get("active_agent_calls") or []
                streaming = next(
                    (c for c in calls if c.get("progress_chars")), None)
                doing = f"{package.owner or 'seat'} authoring " + (
                    ", ".join(package.required_files[:2]) or package.title)
                if streaming:
                    doing += f" ({streaming['progress_chars']:,} chars streamed)"
                parts.append(doing)
            return "; ".join(parts)
        if goal.status == "running" and all(
                m.status == "done" for m in goal.milestones) and goal.milestones:
            return "verifying the final release"
        return goal.status.replace("_", " ")
    def _goal_views(self, items: list[Goal]) -> list[dict]:
        """Attach aggregate/actionable state without mutating durable goals."""
        sessions = self.store.list_sessions(limit=None)
        by_id = {item["session_id"]: item for item in sessions}
        views: list[dict] = []
        for goal in items:
            data = goal.model_dump()
            related = [item for item in sessions if item.get("goal_id") == goal.goal_id]
            live_related = [
                item for item in related
                if item.get("status") not in ("done", "failed", "cancelled")
            ]
            live_followups = [
                item for item in live_related if item.get("parent_session_id")
            ]
            live_revision = next(
                (
                    item for item in live_followups
                    if item.get("revision_targets")
                ),
                None,
            )
            # The released Goal ledger stays completed and immutable, while a
            # post-release child can still be live and actionable in the view.
            terminal_goal = (
                goal.status in ("cancelled", "failed")
                or (goal.status == "completed" and not live_followups)
            )
            approvals = (0 if terminal_goal else
                         sum(item.get("pending_approvals", 0) for item in live_related))
            inputs = (0 if terminal_goal else
                      sum(item.get("pending_inputs", 0) for item in live_related))
            planning_calls = [] if terminal_goal else list(goal.active_agent_calls or [])
            active_calls = (
                0 if terminal_goal else
                len(planning_calls)
                + sum(
                    len(item.get("active_agent_calls") or [])
                    for item in live_related
                )
            )
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
                    "resource_roster": current.get("resource_roster", []),
                    "participation_mode": current.get(
                        "participation_mode", goal.participation_mode
                    ),
                    "collaboration_assignments": current.get(
                        "collaboration_assignments", []
                    ) or list(package.participation_reports),
                    "collaboration_integrated_files": current.get(
                        "collaboration_integrated_files", []
                    ),
                    "collaboration_integration_status": (
                        "reused_for_targeted_repair"
                        if (package.participation_reports
                            and package.repair_context
                            and not current.get("collaboration_assignments"))
                        else current.get(
                            "collaboration_integration_status", "not_started"
                        )
                    ),
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
                            "collaboration_assignments": item.get(
                                "collaboration_assignments") or [],
                            "collaboration_integrated_files": item.get(
                                "collaboration_integrated_files") or [],
                            "collaboration_integration_status": item.get(
                                "collaboration_integration_status") or "not_started",
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
            artifact_contributors: set[str] = set()
            for item in related:
                artifact_contributors.update(
                    (item.get("package_output_authors") or {}).values()
                )
                artifact_contributors.update(
                    assignment.get("seat")
                    for assignment in (item.get("collaboration_assignments") or [])
                    if assignment.get("status") == "contributed"
                    and assignment.get("seat")
                )
            expected_roster = list(
                goal.resource_roster
                if goal.participation_mode != "focused" and goal.resource_roster
                else goal.build_roster or []
            )
            has_artifact_work = any(
                package.required_files for package in goal.milestones
            )
            effective_artifact_contributors = (
                artifact_contributors if has_artifact_work else contributing_agents
            )
            data["contributing_agents"] = sorted(contributing_agents)
            data["contributor_count"] = len(contributing_agents)
            data["expected_contributor_count"] = len(expected_roster)
            data["artifact_contributors"] = sorted(
                effective_artifact_contributors
            )
            data["artifact_contributor_count"] = len(
                effective_artifact_contributors
            )
            data["expected_artifact_contributor_count"] = len(expected_roster)
            # Phase 3 economics: effective budget so the UI can render
            # "used/budget" without knowing the server's config default.
            data["call_budget"] = self._goal_call_budget(goal)
            # NEXT-LEVEL R5: one plain sentence answering "what is this goal
            # doing right now" — internal states are coordinator jargon.
            data["now"] = self._goal_now_line(goal, related)
            if not goal.model_calls_used and related:
                # The durable ledger began with Phase 3; goals that predate it
                # still show their true spend from their sessions
                # (display-only — the stored goal is untouched).
                data["model_calls_used"] = sum(
                    item.get("agent_call_attempts") or 0 for item in related)
            data["participation_complete"] = (
                set(expected_roster).issubset(
                    effective_artifact_contributors
                    if goal.participation_mode != "focused"
                    else contributing_agents
                )
                if expected_roster else None
            )
            if goal.status == "completed" and live_followups:
                display_status = (
                    "revising" if live_revision else "following_up"
                )
            elif goal.status in ("cancelled", "completed", "failed"):
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
                if actionable is None:
                    actionable = next(
                        (
                            item for item in live_followups
                            if item.get("status") not in
                            ("done", "failed", "cancelled")
                        ),
                        None,
                    )
                if actionable is None:
                    current = goal.current
                    actionable = (by_id.get(current.session_id)
                                  if current and current.session_id else None)
                if actionable is None and goal.release_session_id:
                    actionable = by_id.get(goal.release_session_id)
                if actionable is None:
                    actionable = next((item for item in related
                                       if item.get("status") not in
                                       ("done", "failed", "cancelled")), None)
            revision_sessions = [
                {
                    "session_id": item.get("session_id"),
                    "parent_session_id": item.get("parent_session_id"),
                    "status": item.get("status"),
                    "outcome": item.get("outcome"),
                    "targets": item.get("revision_targets") or [],
                    "created_at": item.get("created_at"),
                    "updated_at": item.get("updated_at"),
                }
                for item in related
                if item.get("parent_session_id")
                and item.get("revision_targets")
            ]
            revision_sessions.sort(
                key=lambda item: item.get("created_at") or ""
            )
            data.update({
                "display_status": display_status,
                "active_packages": (0 if terminal_goal else
                                    sum(1 for package in goal.milestones
                                        if package.status == "running")),
                "pending_approvals": approvals,
                "pending_inputs": inputs,
                "active_agent_calls": active_calls,
                "planning_agent_calls": planning_calls,
                "agent_call_attempts": sum(
                    item.get("agent_call_attempts", 0) for item in related
                ),
                "agent_attempt_duration_ms": sum(
                    item.get("agent_attempt_duration_ms", 0) for item in related
                ),
                "actionable_session_id": actionable.get("session_id") if actionable else None,
                "revision_sessions": revision_sessions,
                "latest_revision_session_id": (
                    revision_sessions[-1]["session_id"]
                    if revision_sessions else None
                ),
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
        existing = self.goals.get(goal_id)
        if existing is None:
            raise KeyError(f"goal {goal_id} not found")
        if existing.active_agent_calls:
            # Planning has no milestone Session yet, so use the goal id as its
            # cancellation scope and kill the registered API/CLI call directly.
            cancellation.request(goal_id)
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
            cancelled_release_ids: set[str] = set()
            if goal.release_session_id:
                try:
                    self.cancel_session(goal.release_session_id)
                    cancelled_release_ids.add(goal.release_session_id)
                except (KeyError, ValueError):
                    pass
            # A coordinator fault from older versions could create the release
            # turn before persisting release_session_id. Cancellation still
            # owns every live release turn linked by goal_id; do not leave an
            # orphan looking active beside a cancelled parent.
            for meta in self.store.list_sessions(limit=None):
                release_id = str(meta.get("session_id") or "")
                if (
                    not release_id
                    or release_id in cancelled_release_ids
                    or meta.get("goal_id") != goal_id
                    or not meta.get("goal_release")
                    or meta.get("status") in {"done", "failed", "cancelled"}
                ):
                    continue
                try:
                    self.cancel_session(release_id)
                    cancelled_release_ids.add(release_id)
                except (KeyError, ValueError):
                    pass
            self._sys_log("goal_cancelled", {"goal_id": goal_id, "epoch": goal.epoch})
            return goal.model_dump()

    def stop_goal_agent_call(self, goal_id: str, call_id: str) -> dict:
        """Stop the current planning model without cancelling the goal."""
        goal = self.goals.get(goal_id)
        if goal is None:
            raise KeyError(f"goal {goal_id} not found")
        activity = next(
            (
                item for item in goal.active_agent_calls
                if item.get("call_id") == call_id
            ),
            None,
        )
        if activity is None:
            raise KeyError(f"active planning call {call_id} not found")
        cancellation.request_call(goal_id, call_id)
        self._sys_log("goal_agent_call_stop_requested",
            {
                "goal_id": goal_id,
                "call_id": call_id,
                "agent": activity.get("agent"),
                "role": activity.get("role"),
                "progress_chars": activity.get("progress_chars", 0),
            },
        )
        return {
            "goal_id": goal_id,
            "call_id": call_id,
            "agent": activity.get("agent"),
            "status": "stop_requested",
        }

    def _grant_streak_review_retry(self, goal_id: str) -> None:
        """An explicit human resume of a breaker-paused goal IS the human
        review the breaker paused for. Without this, such a goal was a dead
        end: resume would re-run the doomed assembly once and re-pause on the
        same cap. Grant exactly one more attribution cycle by dropping every
        capped streak to one below the limit — the next identical fault
        re-pauses immediately, so an unattended loop stays impossible."""
        goal = self.goals.claim_worker_lease(goal_id, {"paused"})
        if goal is None:
            return
        token = goal.worker_lease
        try:
            limit = config.ASSEMBLY_FAULT_STREAK_LIMIT
            reduced = {
                key: min(value, max(limit - 1, 0))
                for key, value in goal.assembly_fault_streak.items()
            }
            if reduced != goal.assembly_fault_streak:
                goal.assembly_fault_streak = reduced
                self.goals.save_owned(goal, token)
                self._sys_log("assembly_fault_streak_review_retry",
                    {"goal_id": goal_id, "streak": dict(reduced)},
                )
        finally:
            self.goals.release_worker_lease(goal_id, token)

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
                self._sys_log(event, scheduled)

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
            if goal.release_status != "failed_verification":
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
                # No deterministic HTML_INLINE assembly step exists — the
                # default "one authoring package" shape for a single-file
                # deliverable. There is no separate assembly package to
                # blame; attribute the critique to the package that directly
                # authored the criticized release file(s) instead.
                return self._reopen_direct_release_owner(goal, token)
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
            if not fault_path and goal.release_session_id:
                release_session = self.manager.load(goal.release_session_id)
                if release_session is not None:
                    semantic_path, semantic_detail = self._semantic_release_target(
                        release_session, session)
                    if semantic_path:
                        fault_path = semantic_path
                        fault_detail = (
                            "the release verifier failed acceptance checks that "
                            f"criticize {semantic_path} — correct exactly what the "
                            f"critique states: {semantic_detail}"
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
                self._sys_log("release_regression_rebuild_scheduled", scheduled)

    def _reopen_direct_release_owner(self, goal: Goal, token: str) -> str:
        """Attribute a failed release verification straight to the package
        that authored the criticized file(s), for the common case where
        there is no deterministic HTML_INLINE assembly step to blame — a
        single-file build's one authoring package IS the release.

        Without this, a goal with no assembly package could only retry via
        the frontier verifier's own blind self-repair loop (it did not write
        the code, has no design context, and gets a couple of attempts per
        session), so a real defect just came back unchanged on every resume.
        Routing it back to the actual owner mirrors the same streak/escalation
        protection ``_invalidate_assembly_input_provider`` uses, so a genuinely
        stuck repair still pauses for a human instead of looping forever.

        Returns "reopened", "breaker", or "" (ambiguous ownership across
        multiple contributing packages — fall back to the frontier loop).
        """
        if not goal.release_files or not goal.release_defects:
            return ""
        owners: dict[str, GoalMilestone] = {}
        for package in goal.milestones:
            for name in package.release_files:
                owners[name.replace("\\", "/")] = package
        candidates = {
            owners[name].index for name in goal.release_files if name in owners
        }
        if len(candidates) != 1:
            # Either an unowned release file (shouldn't happen) or several
            # packages each contributed distinct release files — a generic
            # critique cannot be pinned on one of them without guessing.
            return ""
        provider = goal.milestones[next(iter(candidates))]
        defects = list(goal.release_defects)
        streak_key = f"{provider.index}:release_owner"
        streak = goal.assembly_fault_streak.get(streak_key, 0) + 1
        goal.assembly_fault_streak[streak_key] = streak
        if streak > config.ASSEMBLY_FAULT_STREAK_LIMIT:
            goal.status = "paused"
            goal.last_error = (
                f"release verification has rejected package {provider.index + 1}'s "
                f"output {streak - 1} times in a row without resolving it; pausing "
                "for human review instead of rebuilding it again"
            )[:300]
            self._sys_log("release_owner_fault_loop_detected",
                {"goal_id": goal.goal_id, "provider_package": provider.index + 1,
                 "streak": streak},
            )
            self.goals.save_owned(goal, token)
            return "breaker"
        escalated_from = ""
        if streak >= config.ASSEMBLY_FAULT_ESCALATE_AT:
            replacement = next(
                (seat for seat in self._frontier_seats()
                 if seat in self.panel and seat != provider.owner
                 and not self.seat_health.is_unavailable(seat)),
                None,
            )
            if replacement:
                escalated_from = provider.owner
                provider.owner = replacement
                self._sys_log("release_owner_fault_escalated",
                    {"goal_id": goal.goal_id, "provider_package": provider.index + 1,
                     "from_owner": escalated_from, "to_owner": replacement,
                     "streak": streak},
                )
        provider.status = "pending"
        provider.resume_session_id = provider.session_id or ""
        provider.session_id = None
        provider.acceptance_detail = (
            "The independent release verifier rejected this package's output on "
            "final review. Fix every item below; do not change files you do not "
            "own:\n- " + "\n- ".join(defects)
        )[:4000]
        provider.repair_context = {
            "category": "semantic_release_defect",
            "detail": "\n".join(defects)[:3000],
            "base_checkpoint_id": provider.active_verified_checkpoint_id,
            "target_paths": list(
                provider.materialization_plan.producing_files
                if provider.materialization_plan else provider.required_files
            ),
            "repair_owner": provider.owner,
        }
        provider.phase = "repairing"
        goal.phase = "repairing"
        goal.current_index = provider.index
        goal.release_status = "not_started"
        goal.release_session_id = None
        goal.release_defects = []
        takeover = f" (escalated from {escalated_from} to {provider.owner})" if escalated_from else ""
        goal.last_error = (
            f"release verification failed; sending package {provider.index + 1} "
            f"back to its owner{takeover} for repair"
        )[:300]
        if not self.goals.save_owned(goal, token):
            return ""
        self._sys_log("release_regression_owner_reopened",
            {"goal_id": goal.goal_id, "provider_package": provider.index + 1,
             "owner": provider.owner, "defects": len(defects)},
        )
        return "reopened"

    def _recover_verified_goal_packages(
        self, goal_id: str, allowed_statuses: Optional[set[str]] = None,
    ) -> list[str]:
        """Adopt completed package attempts that lost their goal commit.

        Session verification and goal staging are separate durable transactions.
        A pause/restart in the old implementation could land between them.  On
        resume, recover exact owner/package outputs from any completed successful
        attempt before spending another model call.
        """
        goal = self.goals.claim_worker_lease(
            goal_id, allowed_statuses or {"paused"}
        )
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
            current_accepted: dict[str, str] = {}
            for done_package in goal.milestones:
                if done_package.status == "done":
                    current_accepted.update(done_package.accepted_hashes)
            for package in goal.milestones:
                if package.status == "done":
                    continue
                if any(
                    goal.milestones[dependency].status != "done"
                    for dependency in package.depends_on
                    if 0 <= dependency < len(goal.milestones)
                ):
                    # An attempt that completed against dependency bytes now
                    # being rebuilt would resurrect a stale output — a real
                    # goal adopted an assembled HTML expanded from a
                    # superseded stylesheet exactly this way.
                    continue
                match = None
                for candidate in candidates.get(package.package_id, []):
                    if (candidate.work_package_owner != package.owner
                            or not set(package.required_files).issubset(
                                set(candidate.required_files))):
                        continue
                    if any(
                        current_accepted.get(name) not in (None, digest)
                        for name, digest in candidate.dependency_hashes.items()
                    ):
                        # The attempt consumed input bytes that differ from the
                        # currently accepted ones; its output is not equivalent
                        # to what a fresh run would produce.
                        continue
                    sealed = candidate.verified_output_hashes
                    latest = {
                        action.filename.replace("\\", "/"): Path(action.result_path)
                        for action in candidate.proposed_actions
                        if (action.role != Role.panelist
                            and action.kind in ("write_file", "edit_file")
                            and action.status == "executed" and action.result_path)
                    }
                    for action in candidate.proposed_actions:
                        if (action.role == Role.panelist
                                or action.kind != "build_artifact"
                                or action.status != "executed"):
                            continue
                        declared = [
                            item.strip().replace("\\", "/")
                            for item in str(
                                action.args.get("produces") or ""
                            ).split(",") if item.strip()
                        ]
                        try:
                            produced = [
                                Path(item) for item in json.loads(
                                    action.args.get("produced_paths") or "[]"
                                )
                            ]
                        except (json.JSONDecodeError, TypeError):
                            produced = []
                        for name, path in zip(declared, produced):
                            latest[name] = path
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
                checkpoint_paths = {
                    name: executor.resolve_in_workspace(
                        Path(goal.staging_root), name
                    )
                    for name in package.accepted_hashes
                }
                checkpoint = self.checkpoints.seal_paths(
                    goal_id=goal.goal_id,
                    package_id=package.package_id,
                    session_id=match.session_id,
                    paths=checkpoint_paths,
                    expected_hashes=package.accepted_hashes,
                    parent_id=package.active_verified_checkpoint_id,
                    state="recovered_verified",
                    evidence={"migration": "verified_session_recovery"},
                )
                package.active_verified_checkpoint_id = checkpoint[
                    "checkpoint_id"
                ]
                package.invalidated_session_ids = [
                    item for item in package.invalidated_session_ids
                    if item != match.session_id
                ]
                package.phase = "objective_validated"
                goal.active_verified_checkpoint_id = checkpoint["checkpoint_id"]
                goal.phase = "objective_validated"
                if match.collaboration_assignments:
                    package.participation_reports = [
                        item.model_dump()
                        for item in match.collaboration_assignments
                    ]
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
            self._sys_log("goal_replanning", {"goal_id": goal_id})
            if background:
                self._pool.submit(self._plan_and_start_safely, goal_id)
                return self.get_goal(goal_id) or replanning.model_dump()
            self._plan_and_start(goal_id)
            return self.get_goal(goal_id) or replanning.model_dump()
        if "pausing for human review" in (goal.last_error or ""):
            self._grant_streak_review_retry(goal_id)
        if "goal call budget reached" in (goal.last_error or ""):
            self._grant_budget_extension(goal_id)
            goal = self.goals.get(goal_id) or goal
        self._reopen_paused_assembly_provider(goal_id)
        recovered = self._recover_verified_goal_packages(goal_id)
        goal = self.goals.get(goal_id) or goal
        if recovered:
            self._sys_log("goal_packages_recovered",
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
                self._sys_log("goal_resumed",
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
            if goal.status == "running" and any(
                    m.status == "pending" for m in goal.milestones):
                # Release prep reopened a stale assembly package instead of
                # opening a release session; start its rebuild now.
                self._start_ready_packages(goal, background=background)
            return self.get_goal(goal_id) or goal.model_dump()
        if goal.current is None:  # defensive: nothing left to run
            goal.status = "completed"
            self.goals.save(goal)
            return goal.model_dump()
        goal = self.goals.resume(goal_id)
        if goal is None:
            raise ValueError("goal changed while attempting to resume")
        self._sys_log("goal_resumed", {"goal_id": goal_id, "epoch": goal.epoch})
        self._start_ready_packages(goal, background=background)
        return self.get_goal(goal_id) or goal.model_dump()

    def recover_goal(
        self, goal_id: str, strategy: str, background: bool = True,
    ) -> dict:
        """Apply an explicit operator recovery command to persisted state."""
        strategy = (strategy or "").strip().lower()
        if strategy not in {"retry_verifier", "repair_owner", "frontier_takeover"}:
            raise ValueError(
                "strategy must be retry_verifier, repair_owner, or frontier_takeover"
            )
        if strategy == "retry_verifier":
            self._recover_verified_goal_packages(
                goal_id, {"paused", "failed"}
            )
        goal = self.goals.claim_worker_lease(goal_id, {"paused", "failed"})
        if goal is None:
            if self.goals.get(goal_id) is None:
                raise KeyError(f"goal {goal_id} not found")
            raise ValueError("goal is not in a recoverable terminal state")
        token = goal.worker_lease
        prepare_release = False
        schedule = False
        try:
            if strategy == "retry_verifier":
                if not goal.milestones or not (
                        goal.active_verified_checkpoint_id
                        or all(
                            package.status == "done"
                            for package in goal.milestones
                        )):
                    raise ValueError("release verification is not the current failure")
                if goal.active_verified_checkpoint_id and goal.staging_root:
                    self.checkpoints.materialize(
                        goal.active_verified_checkpoint_id,
                        Path(goal.staging_root),
                        names=goal.release_files or None,
                    )
                goal.status = "running"
                goal.release_session_id = None
                goal.release_status = "not_started"
                goal.last_error = "operator requested a fresh independent verifier"
                prepare_release = True
            else:
                latest_failure = next(iter(reversed(goal.failure_records)), None)
                if (latest_failure is not None
                        and latest_failure.stage == "orchestrator"):
                    raise ValueError(
                        "this is an orchestrator capability-contract failure; "
                        "another artifact author cannot repair it"
                    )
                target = next(
                    (package for package in goal.milestones
                     if package.status == "failed"),
                    None,
                )
                if target is None and goal.release_status not in {
                        "not_started", "released"}:
                    release_packages = [
                        package for package in goal.milestones if package.release_files
                    ]
                    target = release_packages[0] if len(release_packages) == 1 else None
                if target is None:
                    target = goal.current
                if target is None:
                    raise ValueError("no package can be attributed for recovery")
                prior_owner = target.owner
                if strategy == "frontier_takeover":
                    replacement = next(
                        (seat for seat in self._frontier_seats()
                         if seat in self.panel and seat != target.owner
                         and not self.seat_health.is_unavailable(seat)),
                        None,
                    )
                    if replacement is None:
                        raise ValueError("no healthy independent frontier seat is available")
                    target.owner = replacement
                target.status = "pending"
                target.resume_session_id = target.session_id or ""
                target.session_id = None
                target.acceptance_detail = (
                    f"Operator selected {strategy}; preserve the last-good inputs, "
                    "change the producing source, and rerun every gate."
                )
                target.repair_context = {
                    "category": "operator_recovery",
                    "detail": target.acceptance_detail,
                    "base_checkpoint_id": target.active_verified_checkpoint_id,
                    "target_paths": list(
                        target.materialization_plan.producing_files
                        if target.materialization_plan else target.required_files
                    ),
                    "repair_owner": target.owner,
                }
                target.phase = "repairing"
                goal.phase = "repairing"
                goal.status = "running"
                goal.current_index = target.index
                goal.release_session_id = None
                goal.release_status = "not_started"
                goal.last_error = f"operator selected {strategy} for {target.package_id}"
                failure = next(iter(reversed(goal.failure_records)), None)
                if failure is None:
                    failure = recovery.record_failure(
                        goal, stage="operator", category="manual_recovery",
                        summary=f"operator recovery for {target.package_id}",
                        evidence={"package_id": target.package_id},
                        responsible_owner=prior_owner,
                    )
                recovery.begin_repair(
                    goal, failure, repair_owner=target.owner,
                    strategy=f"operator_{strategy}",
                    input_hashes=self._milestone_input_hashes(goal, target),
                )
                schedule = True
            goal.recovery_state = RecoveryState.repairing
            if not self.goals.save_owned(goal, token):
                raise ValueError("goal changed while applying recovery")
        finally:
            self.goals.release_worker_lease(goal_id, token)
        current = self.goals.get(goal_id) or goal
        self._sys_log("operator_recovery_selected",
            {"goal_id": goal_id, "strategy": strategy},
        )
        if prepare_release:
            self._prepare_goal_release(current)
            self.goals.save(current)
            if current.status == "running" and any(
                    m.status == "pending" for m in current.milestones):
                # The re-run release review routed a defect back to its
                # producer. Like resume_goal, start that repair now; leaving
                # it unscheduled parked a live goal with no worker.
                self._start_ready_packages(current, background=background)
        elif schedule:
            self._start_ready_packages(current, background=background)
        return self.get_goal(goal_id) or current.model_dump()

    def delete_goal(self, goal_id: str) -> bool:
        """Remove the goal record. Its milestone sessions remain in the store."""
        return self.goals.remove(goal_id)

    def _reconcile_goal_orphans(self) -> None:
        """Resume durable work after restart or park it with a truthful reason.

        Terminal package sessions can advance without another author call, and
        dependency-ready packages can be scheduled directly. Only work with no
        resumable transition is parked for operator attention.
        """
        try:
            for goal in self.goals.list():
                if goal.status == "cancelled":
                    # Older cancellation logic made the parent terminal but
                    # left package rows as "running", which kept the dashboard
                    # visually alive forever. Normalize those records once.
                    changed = False
                    if goal.last_error != "cancelled by user":
                        goal.last_error = "cancelled by user"
                        changed = True
                    for package in goal.milestones:
                        if package.status == "running":
                            package.status = "cancelled"
                            changed = True
                    if changed:
                        self.goals.save(goal)
                    continue
                if goal.status not in (
                    "planning", "running", "draining", "awaiting_release",
                ):
                    continue
                if (
                    goal.milestones
                    and all(package.status == "done" for package in goal.milestones)
                ):
                    # The crash may have happened after the last package was
                    # accepted but before (or during) final promotion. Re-enter
                    # release preparation directly; it reuses a hash-matching
                    # passed review and otherwise creates only the missing
                    # review step.
                    self._sys_log(
                        "goal_release_transition_resumed",
                        {"goal_id": goal.goal_id,
                         "release_session_id": goal.release_session_id},
                    )
                    self._pool.submit(self._prepare_goal_release, goal)
                    continue
                # A terminal package turn has already spent its model calls and
                # contains durable output/failure evidence. Resume the goal
                # transition itself instead of throwing that work away and
                # forcing another author attempt after every restart.
                terminal = next(
                    (
                        session
                        for package in goal.milestones
                        if package.status == "running" and package.session_id
                        if (session := self.manager.load(package.session_id)) is not None
                        and session.status in self._TERMINAL
                    ),
                    None,
                )
                if terminal is not None:
                    self._sys_log(
                        "goal_terminal_transition_resumed",
                        {"goal_id": goal.goal_id,
                         "session_id": terminal.session_id},
                    )
                    self._pool.submit(self._maybe_advance_goal, terminal, True)
                    continue
                if any(
                    self._package_ready(goal, index)
                    for index in range(len(goal.milestones))
                ):
                    self._sys_log(
                        "goal_ready_work_resumed", {"goal_id": goal.goal_id},
                    )
                    self._pool.submit(self._start_ready_packages, goal, True)
                    continue
                parked = self.goals.park_active(goal.goal_id, "interrupted by a server restart")
                if parked is not None:
                    self._sys_log("goal_paused",
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

    def goal_timeline(self, goal_id: str) -> dict:
        """One ordered story for a whole goal, plus a derived postmortem.

        Events are logged per-session (goal-scoped ones in that goal's own log,
        or the legacy shared "-" log for goals created before that split, both
        keyed by a goal_id payload), so a goal's narrative was previously scattered
        across files and only recoverable by hand. This merges every related
        log, and summarizes what a human wants after the fact: duration,
        spend per seat, packages/owners/attempts, and how attempts were
        actually lost (completed vs seat outage vs interrupted) so model
        cost and infrastructure cost are separated honestly.
        """
        import json as _json

        goal = self.goals.get(goal_id)
        if goal is None:
            raise KeyError(f"goal {goal_id} not found")

        def read_log(session_id: str) -> list[dict]:
            path = self.store.session_log_path(session_id)
            out: list[dict] = []
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:
                        out.append(_json.loads(line))
                    except _json.JSONDecodeError:
                        continue
            return out

        related = [
            meta for meta in self.store.list_sessions(limit=None)
            if meta.get("goal_id") == goal_id
        ]
        events: list[dict] = []
        for meta in related:
            for record in read_log(meta["session_id"]):
                record["session_id"] = meta["session_id"]
                events.append(record)
        # Goal-scoped events now live in this goal's own log. Goals created
        # before that still have theirs in the shared "-" log, so read both and
        # keep filtering on goal_id — a legacy timeline must not go blank.
        for source in (f"goal-{goal_id}", "-"):
            for record in read_log(source):
                if (record.get("payload") or {}).get("goal_id") == goal_id:
                    record["session_id"] = source
                    events.append(record)
        events.sort(key=lambda record: record.get("ts") or "")
        events = events[-400:]

        attempts = 0
        completed = 0
        outage_attempts = 0
        interrupted = 0
        session_verification_signatures: set[str] = set()
        from .seat_health import UNAVAILABLE_STATES, classify_failure
        for meta in related:
            session = self.manager.load(meta["session_id"])
            if session is None:
                continue
            attempts += session.agent_call_attempts
            successful = sum(self._successful_session_calls(session).values())
            completed += successful
            if session.status == SessionStatus.cancelled:
                interrupted += max(
                    0, session.agent_call_attempts - successful)
            for failure in session.failure_records:
                if ("verification" in failure.stage
                        or "validation" in failure.category):
                    session_verification_signatures.add(
                        failure.fault_signature or failure.failure_id
                    )
            for note in (session.unresolved or []):
                if ("dropped" in str(note)
                        and classify_failure(str(note)) in UNAVAILABLE_STATES):
                    outage_attempts += 1
        failed_other = max(
            0, attempts - completed - interrupted - outage_attempts)
        summary = {
            "status": goal.status,
            "release_status": goal.release_status,
            "created_at": goal.created_at,
            "updated_at": goal.updated_at,
            "calls_used": goal.model_calls_used,
            "call_budget": self._goal_call_budget(goal),
            "calls_by_seat": dict(goal.model_calls_by_seat),
            "attempts": {
                "total": attempts,
                "completed": completed,
                "seat_outage": outage_attempts,
                "interrupted": interrupted,
                "other_failures": failed_other,
            },
            "economics": {
                "useful_completed_calls": completed,
                "repair_attempts": len(goal.repair_history),
                "verification_failures": len({
                    failure.fault_signature or failure.failure_id
                    for failure in goal.failure_records
                    if ("verification" in failure.stage
                        or "validation" in failure.category)
                } | session_verification_signatures),
                "transport_or_seat_failures": outage_attempts,
                "orchestration_calls": max(0, goal.model_calls_used - attempts),
            },
            "recovery_state": goal.recovery_state,
            "failure_records": [item.model_dump() for item in goal.failure_records],
            "repair_history": [item.model_dump() for item in goal.repair_history],
            "last_good_checkpoint": dict(goal.last_good_checkpoint),
            "packages": [
                {
                    "package": package.index + 1,
                    "title": package.title,
                    "owner": package.owner,
                    "status": package.status,
                    "invalidated_attempts": len(package.invalidated_session_ids),
                }
                for package in goal.milestones
            ],
        }
        return {
            "goal_id": goal_id,
            "summary": summary,
            "events": reporting.format_timeline(events),
        }

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
            if session.goal_release and session.goal_id:
                goal = self.goals.get(session.goal_id)
                files = self._goal_release_files(goal) if goal is not None else []
                reusable = (
                    self._reusable_goal_release(goal, files)
                    if (
                        goal is not None
                        and goal.status in {
                            "planning", "running", "draining", "awaiting_release",
                        }
                        and files
                    )
                    else None
                )
                if reusable is not None and reusable.session_id == session.session_id:
                    # The paid reviewer phase already passed. Revoke the dead
                    # process lease but preserve this deterministic promotion
                    # cursor for the goal reconciler instead of cancelling it.
                    session.active_agent_calls = []
                    self.store.revoke_worker_lease(sid)
                    session.worker_lease = ""
                    self.store.save_session(session)
                    self.store.log_event(
                        sid,
                        "verified_release_parked_for_resume",
                        {"goal_id": session.goal_id},
                    )
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

    def stop_agent_call(self, session_id: str, call_id: str) -> dict:
        """Stop one supervised model call while sibling seats keep working."""
        session = self.manager.load(session_id)
        if session is None:
            raise KeyError(f"session {session_id} not found")
        activity = next(
            (
                item for item in session.active_agent_calls
                if item.get("call_id") == call_id
            ),
            None,
        )
        if activity is None:
            raise KeyError(f"active call {call_id} not found")
        if not activity.get("operator_stoppable"):
            raise ValueError("this call does not support individual stopping")
        cancellation.request_call(session_id, call_id)
        self.store.log_event(
            session_id,
            "agent_call_stop_requested",
            {
                "call_id": call_id,
                "agent": activity.get("agent"),
                "role": activity.get("role"),
                "progress_chars": activity.get("progress_chars", 0),
            },
        )
        return {
            "session_id": session_id,
            "call_id": call_id,
            "agent": activity.get("agent"),
            "status": "stop_requested",
        }

    def list(self) -> list[dict]:
        return self.store.list_sessions()

    def _finish_goal_release(
        self,
        session: Session,
        approved: bool,
        *,
        linked_goal: Optional[Goal] = None,
    ) -> Session:
        """Resolve the special one-action release session without another LLM run."""
        action = next((a for a in session.proposed_actions if a.kind == "promote_batch"), None)
        goal = linked_goal or (
            self.goals.get(session.goal_id) if session.goal_id else None
        )
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
            goal.phase = "released"
            session.phase = "released"
            self._record_work_item(
                goal, "__release__", "released", "completed",
                session_id=session.session_id,
                checkpoint_id=goal.active_verified_checkpoint_id,
            )
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
            goal.status = (
                "failed" if goal.approval_policy == ApprovalPolicy.god_mode else "paused"
            )
            goal.release_status = "failed"
            goal.last_error = session.stop_reason[:300]
            failure = recovery.record_failure(
                goal, stage="release", category="promotion_failed",
                summary=goal.last_error,
                evidence={"action_id": action.action_id, "error": str(e)},
            )
            if goal.approval_policy == ApprovalPolicy.god_mode:
                recovery.mark_exhausted(goal, failure)
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

        # Intent clarification, asked BEFORE any work: the request read two ways
        # and the coordinator stopped rather than guessing. Fold the answer into
        # the task text the classifier and every prompt read, then start the run
        # from the top — the intent pass re-reads it with the fork settled and
        # `intent_clarified` guarantees the question is never asked twice.
        if req.agent == "system" and req.purpose == "clarify_intent":
            parsed = intent.Intent(**(session.intent or {}))
            chosen = intent.selected_option(parsed, req.answer or "")
            session.intent_clarification = chosen
            session.intent_reviewed = False  # re-read the task, fork resolved
            session.task.text = (
                f"{session.task.text}\n\n"
                f"[Clarified by the user] {parsed.ambiguity.strip()} "
                f"→ {chosen}"
            ).strip()
            session.turns.append({
                "role": "user",
                "text": f"Clarification: {chosen}",
            })
            self.store.log_event(session.session_id, "intent_clarified", {
                "answer": req.answer, "resolved_to": chosen})
            self.store.save_session(session)
            return run_session(
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
            if session.goal_id:
                reservation = self.goals.reserve_model_call(
                    session.goal_id,
                    session_id=session.session_id,
                    agent=req.agent,
                    phase=session.phase or "resume_after_input",
                )
                if reservation is None:
                    raise AgentError(
                        "goal model-call budget exhausted before provider resume"
                    )
                session.goal_model_call_reservation_ids.append(
                    reservation["reservation_id"]
                )
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
