"""Agent Registry — maps agent names to adapters behind one call interface."""

from __future__ import annotations

import time
from typing import Optional, Protocol

from pydantic import BaseModel

from .models import Role


class AgentError(Exception):
    """An agent backend failed, timed out, or returned unusable output.
    The loop degrades to a partial answer instead of crashing the session."""


class AgentCallStopped(AgentError):
    """The operator stopped one call; the seat itself is still healthy."""


class AgentInputRequired(Exception):
    """An agent paused mid-call to ask the human a question. The loop turns
    this into an InputRequest and pauses the session; answering resumes the
    same underlying call via resume_token."""

    def __init__(self, question: str, resume_token: str):
        super().__init__(question)
        self.question = question
        self.resume_token = resume_token
        self.role = None        # enriched by the loop at the call site
        self.agent_name = None


class AdapterResult(BaseModel):
    content: str
    tokens: int = 0
    duration_ms: int = 0
    # The exact model that produced this reply (slug/id), when the adapter
    # knows it — every take should be attributable to a model, not just a
    # vendor seat. None ⇒ the backend's own default, unknown to us.
    model: Optional[str] = None
    # Files a local CLI seat wrote straight into its own working directory,
    # i.e. side effects that never passed the executor or the approval kernel.
    # Every CLI seat is a subprocess running with the user's own privileges, so
    # this is a property of the seat CLASS, not of any one vendor: each CLI is
    # asked to stay read-only, but that is the tool's own promise and not all
    # of them can enforce it on every platform. This reports what actually
    # happened instead of trusting the flag. API-backed seats never set it.
    ungoverned_writes: list[str] = []


class Adapter(Protocol):
    name: str

    def call(self, role: Role, prompt: str, timeout_s: int,
             images: list[dict] | None = None) -> AdapterResult: ...


class AgentRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, Adapter] = {}
        # Optional SeatHealth sink (injected by the service): every call's
        # outcome updates per-seat health so scheduling can route around
        # seats that cannot answer instead of burning attempts against them.
        self.health = None

    def register(self, adapter: Adapter) -> None:
        self._adapters[adapter.name] = adapter

    def names(self) -> list[str]:
        return sorted(self._adapters)

    def get(self, agent: str) -> Optional[Adapter]:
        """Return a registered adapter for optional capability checks."""
        return self._adapters.get(agent)

    def call(self, agent: str, role: Role, prompt: str, timeout_s: int = 0,
             images: list[dict] | None = None,
             cwd: str | None = None) -> AdapterResult:
        if agent not in self._adapters:
            raise KeyError(f"no adapter registered for agent '{agent}'")
        t0 = time.perf_counter()
        adapter = self._adapters[agent]
        # Only pass optional kwargs when present/supported, so adapters that
        # don't take them (simple/text-only doubles) keep working unchanged.
        kwargs: dict = {}
        if images:
            kwargs["images"] = images
        if cwd and getattr(adapter, "supports_cwd", False):
            kwargs["cwd"] = cwd
        try:
            result = adapter.call(role, prompt, timeout_s, **kwargs)
        except AgentCallStopped:
            raise
        except Exception as e:
            if self.health is not None:
                self.health.record_failure(agent, str(e))
            raise
        if self.health is not None:
            self.health.record_success(agent)
        return self._normalize(result, t0)

    def resume(self, agent: str, resume_token: str, answer: str, timeout_s: int = 0) -> AdapterResult:
        """Continue a paused call with the human's answer."""
        if agent not in self._adapters:
            raise KeyError(f"no adapter registered for agent '{agent}'")
        fn = getattr(self._adapters[agent], "resume", None)
        if fn is None:
            raise AgentError(f"agent '{agent}' cannot resume paused calls")
        t0 = time.perf_counter()
        return self._normalize(fn(resume_token, answer, timeout_s), t0)

    def cancel(self, agent: str, resume_token: str) -> None:
        """Best-effort cancellation of a paused call (input declined)."""
        fn = getattr(self._adapters.get(agent), "cancel_resume", None)
        if fn is not None:
            try:
                fn(resume_token)
            except AgentError:
                pass

    @staticmethod
    def _normalize(result: AdapterResult, t0: float) -> AdapterResult:
        if not result.duration_ms:
            result.duration_ms = int((time.perf_counter() - t0) * 1000)
        if not result.tokens:
            result.tokens = max(1, len(result.content) // 4)
        return result
