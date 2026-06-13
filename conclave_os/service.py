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

from . import config, intake
from .adapters.cli import CliAdapter
from .adapters.mock import MockAdapter
from .composer import fallback_final
from .governance import Governance
from .logstore import LogStore
from .loop import resume_session, resume_with_input, run_session
from .models import Budgets, Risk, Role, Session, SessionStatus, utcnow
from .registry import AgentError
from .registry import AgentRegistry
from .sessions import SessionManager
from .settings import Settings, budgets_overrides, load_settings, save_settings


class ConclaveService:
    def __init__(
        self,
        data_dir: Optional[Path] = None,
        backend: Optional[str] = None,
        role_agents: Optional[dict[Role, str]] = None,
    ):
        self._data_dir = Path(data_dir) if data_dir else config.DATA_DIR
        # Persisted settings layer over config/env. With no settings.json this
        # returns the pure config/env defaults, so behaviour is unchanged.
        self.settings = load_settings(self._data_dir)
        self._explicit_role_agents = role_agents  # explicit arg always wins

        self.store = LogStore(self._data_dir)
        self.manager = SessionManager(self.store)
        self.governance = Governance(self.store)
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
            self.role_agents = {
                Role(role): agent for role, agent in self.settings.role_agents.items()
            }
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
        config.BUDGETS_BY_COMPLEXITY = budgets_overrides(self.settings)

        self.registry = AgentRegistry()
        if self.backend == "cli":
            for agent in sorted(set(self.role_agents.values())):
                self.registry.register(CliAdapter(agent=agent))
        else:
            self.registry.register(MockAdapter())

    def update_settings(self, patch: dict) -> Settings:
        """Apply a partial settings patch, persist it, and re-derive the
        backend/role mapping/registry. Some changes (backend, role mapping)
        affect new sessions; in-flight sessions keep their own backend."""
        merged = self.settings.model_dump()
        for key, value in (patch or {}).items():
            if key not in merged:
                continue
            if isinstance(merged[key], dict) and isinstance(value, dict):
                merged[key].update(value)
            else:
                merged[key] = value
        self.settings = Settings.model_validate(merged)
        save_settings(self.settings, self._data_dir)
        self._apply_settings()
        return self.settings

    def run(self, text: str, source: str = "cli", budgets: Optional[Budgets] = None) -> Session:
        session = intake.receive(text, source, self.manager, budgets)
        session.backend = self.backend
        return run_session(
            session, self.manager, self.registry, self.governance, self.store,
            role_agents=self.role_agents,
        )

    def submit_background(self, text: str, source: str = "api",
                          budgets: Optional[Budgets] = None) -> Session:
        """Create the session and run it on a worker thread; the caller polls
        GET /sessions/{id} for progress."""
        session = intake.receive(text, source, self.manager, budgets)
        session.backend = self.backend
        self.store.save_session(session)
        self._pool.submit(self._safely, session, self._run_full)
        return session

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
            self.registry.register(CliAdapter(agent=agent))

    # ---- CLI seats (settings panel) ------------------------------------------

    def seats(self) -> dict:
        """The local CLI agents Conclave OS can drive, with availability from
        PATH — used to populate the role→agent dropdowns in settings."""
        import shutil

        agents = ["claude", "codex", "gemini"]
        return {"seats": [
            {"name": a, "available": shutil.which(a) is not None, "kind": "cli"}
            for a in agents
        ]}

    def get(self, session_id: str) -> Optional[dict]:
        return self.store.load_session(session_id)

    def list(self) -> list[dict]:
        return self.store.list_sessions()

    def approve(self, session_id: str, approval_id: str, approved: bool,
                by: str = "user", background: bool = False) -> Session:
        """Resolve an approval. Approving the last pending approval on a paused
        session resumes it. Denying a session gate cancels the session; denying
        an action approval (action_ref set) skips just that action — the
        session resumes and completes without the artifact."""
        session = self.manager.load(session_id)
        if session is None:
            raise KeyError(f"session {session_id} not found")
        self._ensure_adapters(session)
        approval = self.governance.resolve(session, approval_id, approved, by=by)
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

    def _answer_continue(self, session: Session, req) -> Session:
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
