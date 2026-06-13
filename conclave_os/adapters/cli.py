"""Direct local-CLI agent adapter — Conclave OS runs the agent CLIs itself.

Each call invokes the local CLI in plain non-interactive generation mode and
returns its raw text output. That is what lets the implementer emit real file
bodies instead of descriptions — Conclave OS is fully self-contained.

Tools are disabled / read-only so the agent cannot perform side effects itself;
every write stays governed by Conclave OS (executor + approval kernel). Print
mode is one-shot, so there is no awaiting_user_input/resume path here.

Supported agents: claude (fully exercised), codex, gemini.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from ..models import Role
from ..registry import AdapterResult, AgentError


class CliAdapter:
    def __init__(self, agent: str, name: Optional[str] = None, model: Optional[str] = None):
        self.agent = agent  # claude | codex | gemini
        self.name = name or agent
        self.model = model

    def call(self, role: Role, prompt: str, timeout_s: int) -> AdapterResult:
        t0 = time.monotonic()
        runner = {
            "claude": self._run_claude,
            "codex": self._run_codex,
            "gemini": self._run_gemini,
        }.get(self.agent)
        if runner is None:
            raise AgentError(f"unknown CLI agent: {self.agent!r}")
        content = runner(prompt, timeout_s).strip()
        if not content:
            raise AgentError(f"{self.agent} CLI returned empty output")
        return AdapterResult(content=content, duration_ms=int((time.monotonic() - t0) * 1000))

    def _exec(self, cmd: list[str], prompt: str, timeout_s: int) -> str:
        """Run a CLI command with the prompt on stdin; return stdout."""
        try:
            proc = subprocess.run(
                cmd, input=prompt, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=max(30, timeout_s),
            )
        except subprocess.TimeoutExpired as e:
            raise AgentError(f"{self.agent} CLI timed out after {timeout_s}s") from e
        except (OSError, FileNotFoundError) as e:
            raise AgentError(f"{self.agent} CLI not runnable: {e}") from e
        if proc.returncode != 0:
            raise AgentError(
                f"{self.agent} CLI exited {proc.returncode}: {(proc.stderr or '').strip()[:300]}"
            )
        return proc.stdout or ""

    def _run_claude(self, prompt: str, timeout_s: int) -> str:
        cmd = ["claude", "-p", "--output-format", "json", "--tools", ""]
        if self.model:
            cmd += ["--model", self.model]
        out = self._exec(cmd, prompt, timeout_s)
        try:
            data = json.loads(out)
        except json.JSONDecodeError as e:
            raise AgentError(f"claude CLI returned non-JSON: {out[:200]!r}") from e
        if data.get("is_error"):
            raise AgentError(f"claude CLI error: {data.get('result') or data.get('subtype')}")
        return data.get("result") or ""

    def _run_gemini(self, prompt: str, timeout_s: int) -> str:
        # -p = non-interactive; plan approval-mode = read-only (no side effects).
        cmd = ["gemini", "-p", prompt, "-o", "text", "--approval-mode", "plan"]
        if self.model:
            cmd += ["-m", self.model]
        return self._exec(cmd, "", timeout_s)

    def _run_codex(self, prompt: str, timeout_s: int) -> str:
        # codex exec writes its final message cleanly to --output-last-message.
        fd, outfile = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        try:
            cmd = ["codex", "exec", "--color", "never", "--output-last-message", outfile]
            if self.model:
                cmd += ["-m", self.model]
            cmd.append("-")  # read the prompt from stdin
            self._exec(cmd, prompt, timeout_s)
            return Path(outfile).read_text(encoding="utf-8", errors="replace")
        finally:
            try:
                os.unlink(outfile)
            except OSError:
                pass
