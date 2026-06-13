"""SwitchboardAdapter — Conclave AI Switchboard backend.

Each call() submits a single-agent task to the Switchboard (FastAPI service,
default 127.0.0.1:8787) in `resolve` mode with the target agent as primary and
no consultants, polls until terminal, and returns the final answer text. All
Switchboard permissions are denied — Conclave OS governance is the only path
to side effects.

When the agent asks the user a question (Switchboard `awaiting_user_input`),
call() raises AgentInputRequired carrying the question and the Switchboard
task id; resume() answers via POST /api/tasks/{id}/answer and polls the same
task to completion. `waiting_for_user` (action approval) is still cancelled —
actions are governed on the Conclave OS side, never approved remotely.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from ..models import Role
from ..registry import AdapterResult, AgentError, AgentInputRequired

PROTOCOL_VERSION = "1.2"

PERMISSION_KEYS = [
    "can_read_files", "can_write_files", "can_run_commands",
    "can_access_network", "can_install_packages", "can_apply_patches",
    "can_read_env_files", "can_read_secrets",
]

_TERMINAL = {"completed", "failed", "cancelled"}
_PAUSED = {"waiting_for_user"}  # awaiting_user_input is handled as AgentInputRequired


class SwitchboardAdapter:
    def __init__(
        self,
        agent: str,
        base_url: str = "http://127.0.0.1:8787",
        name: Optional[str] = None,
        poll_interval: float = 2.0,
        context_extra: Optional[dict] = None,
    ):
        self.agent = agent  # switchboard agent id: codex | gemini | claude-code | fake
        self.name = name or agent  # name in the Conclave OS registry
        self.base_url = base_url.rstrip("/")
        self.poll_interval = poll_interval
        self.context_extra = context_extra or {}

    def _http(self, method: str, path: str, body: Optional[dict] = None, timeout: int = 15) -> Any:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:500]
            raise AgentError(f"switchboard HTTP {e.code} on {path}: {detail}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise AgentError(f"switchboard unreachable ({self.base_url}{path}): {e}") from e

    def _cancel(self, task_id: str) -> None:
        try:
            self._http("POST", f"/api/tasks/{task_id}/cancel")
        except AgentError:
            pass  # best effort — the deadline/error is already being reported

    @staticmethod
    def _answer_from(data: dict) -> str:
        final = data.get("final_result") or {}
        answer = (final.get("final_answer") or "").strip()
        if answer:
            return answer
        for m in reversed(data.get("messages") or []):
            if m.get("direction") == "from_agent" and (m.get("content") or "").strip():
                return m["content"].strip()
        return ""

    @staticmethod
    def _output_tokens(data: dict) -> int:
        return sum(r.get("output_tokens") or 0 for r in data.get("agent_runs") or [])

    def call(self, role: Role, prompt: str, timeout_s: int) -> AdapterResult:
        per_call = max(10, min(timeout_s, 3600))
        deadline_s = per_call * 2 + 60  # max_rounds=2 plus queue/orchestration overhead
        body = {
            "protocol_version": PROTOCOL_VERSION,
            "source": "api",
            "source_agent": "conclave-os",
            "mode": "resolve",
            "task_type": "general_consultation",
            "user_request": prompt,
            "primary_agent": self.agent,
            "consultants": [],
            "context": {"extra": {"conclave_os_role": role.value, **self.context_extra}},
            "permissions": {k: False for k in PERMISSION_KEYS},
            "limits": {
                "max_rounds": 2,
                "timeout_seconds": per_call,
                "max_seconds": min(deadline_s, 86400),
            },
        }
        created = self._http("POST", "/api/tasks", body)
        return self._await(created["task_id"], deadline_s)

    def resume(self, resume_token: str, answer: str, timeout_s: int) -> AdapterResult:
        """Answer a paused Switchboard task and poll it to completion."""
        self._http("POST", f"/api/tasks/{resume_token}/answer", {"answer": answer})
        return self._await(resume_token, max(10, min(timeout_s, 3600)) * 2 + 60)

    def cancel_resume(self, resume_token: str) -> None:
        """Cancel a paused Switchboard task (the human declined to answer)."""
        self._cancel(resume_token)

    @staticmethod
    def _question_from(data: dict) -> str:
        for m in reversed(data.get("messages") or []):
            if m.get("message_type") == "user_input_request" and (m.get("content") or "").strip():
                return m["content"].strip()
        return "The agent requested additional input from the user."

    def _await(self, task_id: str, deadline_s: float) -> AdapterResult:
        t0 = time.monotonic()
        while time.monotonic() - t0 < deadline_s:
            data = self._http("GET", f"/api/tasks/{task_id}")
            status = data["task"]["status"]
            if status == "completed":
                content = self._answer_from(data)
                if not content:
                    raise AgentError(f"switchboard task {task_id} completed with empty answer")
                return AdapterResult(
                    content=content,
                    tokens=self._output_tokens(data),
                    duration_ms=int((time.monotonic() - t0) * 1000),
                )
            if status in _TERMINAL:  # failed | cancelled
                raise AgentError(
                    f"switchboard task {task_id} {status}: {data['task'].get('error_message')}"
                )
            if status == "awaiting_user_input":
                raise AgentInputRequired(self._question_from(data), resume_token=task_id)
            if status in _PAUSED:  # waiting_for_user (action approval) — never answer remotely
                self._cancel(task_id)
                raise AgentError(
                    f"switchboard task {task_id} paused for remote action approval — "
                    "cancelled; actions are governed by Conclave OS, not the Switchboard"
                )
            time.sleep(self.poll_interval)

        self._cancel(task_id)
        raise AgentError(f"switchboard task {task_id} exceeded {deadline_s}s deadline")
