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

import base64
import copy
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from .. import cancellation, config
from ..cancellation import SessionCancelled
from ..models import Role
from ..registry import AdapterResult, AgentError


def _err_tail(text: str, limit: int = 300) -> str:
    """The most informative slice of a CLI's error output: the END. CLIs print
    banners and prompt echoes first and the actual error last — codex's banner
    alone exceeds 300 chars, so head-truncation hid every real error behind
    'OpenAI Codex v… workdir: …' (live: a delegation failure whose cause was
    unreadable)."""
    t = (text or "").strip()
    return t if len(t) <= limit else "… " + t[-limit:]


def _neutral_cwd() -> str:
    """CLI subprocesses run from an EMPTY, neutral directory — never the
    server's own repo/cwd. A CLI agent with latent tool instincts (claude
    attempting Read calls, codex scanning its workspace) must not perceive —
    or ungovernedly read — whatever folder the server happens to run in.
    Project content reaches agents only through the governed overview and
    SKILL reads. (Live failure: a claude lead running with cwd=the repo said
    \"I'm running in the actual repo\" and emitted tool-call debris instead of
    a synthesis.)"""
    d = config.SANDBOX_ROOT / "cli-neutral"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


class CliAdapter:
    def __init__(self, agent: str, name: Optional[str] = None, model: Optional[str] = None,
                 api_key_getter=None, role_models: Optional[dict] = None):
        self.agent = agent  # claude | codex | gemini
        self.name = name or agent
        self.model = model
        # role name → model id: optional per-ROLE pins layered over the seat
        # pin (role pin › seat pin › CLI default), so a rarely-called talent
        # (code_generator) can run a heavier model than the seat's default.
        self.role_models = dict(role_models or {})
        # gemini only: resolves the key from env OR the Settings-stored secrets
        # (injected by the service) — an env var must not be the only way in.
        self._api_key_getter = api_key_getter

    def call(self, role: Role, prompt: str, timeout_s: int,
             images: list[dict] | None = None,
             model_override: str | None = None) -> AdapterResult:
        # A per-call override (the lead's production model) wins over the role pin
        # and the seat default. Same vendor as this seat — it becomes --model.
        effective = model_override or self.role_models.get(getattr(role, "value", str(role)))
        if effective and effective != self.model:
            # The runner methods read self.model, and this adapter instance is
            # shared across the panel fan-out threads — so apply the pin/override
            # on a call-local CLONE, never by mutating self.
            clone = copy.copy(self)
            clone.model = effective
            clone.role_models = {}
            return clone.call(role, prompt, timeout_s, images)
        t0 = time.monotonic()
        model = self.model  # the pinned model; branches refine it when they know more
        if self.agent == "claude":
            # claude sees images as content blocks via stream-json (no tools)
            if images:
                content = self._run_claude_vision(prompt, images, timeout_s)
            else:
                content, used = self._run_claude(prompt, timeout_s)
                model = model or used  # the CLI reports what it actually ran
        elif self.agent == "codex":
            content = self._run_codex(prompt, timeout_s, images)  # --image=<path>
        elif self.agent == "gemini":
            # Prefer the google-genai SDK for ALL gemini calls (text AND images)
            # when an API key is present — from the env OR stored via Settings →
            # API keys: a clean inference call that sidesteps the gemini CLI's
            # headless problems — its long prompts overflow the Windows command
            # line ('-p <prompt>' as argv), and plan-mode hangs. No key ⇒ fall
            # back to the CLI (which still has those limitations).
            key = (self._api_key_getter() if self._api_key_getter else None) \
                or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
            if key:
                content = self._run_gemini_sdk(prompt, images or [], key)
                model = self.model or "gemini-2.5-flash"  # the SDK default is explicit
            else:
                content = self._run_gemini(prompt, timeout_s)
        else:
            raise AgentError(f"unknown CLI agent: {self.agent!r}")
        content = content.strip()
        if not content:
            raise AgentError(f"{self.agent} CLI returned empty output")
        return AdapterResult(content=content, model=model,
                             duration_ms=int((time.monotonic() - t0) * 1000))

    def _exec_raw(self, cmd: list[str], prompt: str, timeout_s: int) -> tuple[str, str, int]:
        """Run a CLI command with the prompt on stdin; return (stdout, stderr,
        returncode). Uses Popen (not subprocess.run) and registers the process for
        the current session so a cancel can KILL it mid-flight — a killed call
        surfaces as SessionCancelled. The executable is resolved via PATH
        (shutil.which) so Windows .cmd/.exe shims are found and run directly.
        Returns the exit code rather than raising on it, so callers can recover a
        valid result the CLI printed to stdout even when it exits non-zero."""
        exe = shutil.which(cmd[0])
        if not exe:
            raise AgentError(f"{self.agent} CLI not found on PATH ({cmd[0]!r})")
        sid = cancellation.current_session()
        try:
            proc = subprocess.Popen(
                [exe, *cmd[1:]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                cwd=_neutral_cwd(),
            )
        except (OSError, FileNotFoundError) as e:
            raise AgentError(f"{self.agent} CLI not runnable: {e}") from e
        cancellation.register_proc(sid, proc)
        try:
            out, err = proc.communicate(input=prompt, timeout=max(30, timeout_s))
        except subprocess.TimeoutExpired as e:
            proc.kill()
            try:
                proc.communicate(timeout=5)
            except Exception:  # noqa: BLE001
                pass
            raise AgentError(f"{self.agent} CLI timed out after {timeout_s}s") from e
        finally:
            cancellation.unregister_proc(sid, proc)
        # If a cancel killed the process, report it as cancellation (not an error).
        if sid and cancellation.is_requested(sid):
            raise SessionCancelled()
        return out or "", err or "", proc.returncode

    def _exec(self, cmd: list[str], prompt: str, timeout_s: int) -> str:
        """Run a CLI and return stdout, raising on a non-zero exit. The error
        detail prefers stderr but falls back to stdout — claude/codex print their
        real error as JSON on stdout, so a blank stderr must not hide it."""
        out, err, rc = self._exec_raw(cmd, prompt, timeout_s)
        if rc != 0:
            detail = err.strip() or out.strip()
            raise AgentError(f"{self.agent} CLI exited {rc}: {_err_tail(detail)}")
        return out

    def _run_claude(self, prompt: str, timeout_s: int) -> tuple[str, Optional[str]]:
        """Returns (content, model): the CLI's JSON result names the model that
        actually ran (modelUsage keys), so an unpinned seat is still attributable."""
        cmd = ["claude", "-p", "--output-format", "json", "--tools", ""]
        if self.model:
            cmd += ["--model", self.model]
        out, err, rc = self._exec_raw(cmd, prompt, timeout_s)
        try:
            data = json.loads(out)
        except json.JSONDecodeError as e:
            # No parseable result. If it also exited non-zero, that's the failure —
            # surface stderr, else the raw stdout (the CLI's error text lives there).
            if rc != 0:
                raise AgentError(
                    f"claude CLI exited {rc}: {_err_tail(err.strip() or out.strip())}") from e
            raise AgentError(f"claude CLI returned non-JSON: {out[:200]!r}") from e
        if data.get("is_error"):
            raise AgentError(f"claude CLI error: {data.get('result') or data.get('subtype')}")
        result = data.get("result") or ""
        # A clean result that came back with a NON-ZERO exit code still succeeded —
        # the claude CLI can exit non-zero after emitting a valid result (a
        # post-generation hiccup). Use the result; only fail if there is none.
        if not result and rc != 0:
            raise AgentError(f"claude CLI exited {rc}: {_err_tail(err.strip() or out.strip())}")
        used = None
        usage = data.get("modelUsage")
        if isinstance(usage, dict) and usage:
            used = next(iter(usage))
        return result, used

    def _run_claude_vision(self, prompt: str, images: list[dict], timeout_s: int) -> str:
        """Send the prompt + image content blocks via stream-json so the model
        actually sees the images (verified: reads text, interprets content). No
        tools enabled — the image is in the message, not fetched from disk."""
        content: list[dict] = [{"type": "text", "text": prompt}]
        for img in images:
            try:
                data = base64.b64encode(Path(img["path"]).read_bytes()).decode("ascii")
            except OSError:
                continue  # missing/unreadable image — skip, keep the text
            content.append({
                "type": "image",
                "source": {"type": "base64",
                           "media_type": img.get("media_type", "image/png"), "data": data},
            })
        message = {"type": "user", "message": {"role": "user", "content": content}}
        cmd = ["claude", "-p", "--output-format", "stream-json",
               "--input-format", "stream-json", "--verbose", "--tools", ""]
        if self.model:
            cmd += ["--model", self.model]
        out = self._exec(cmd, json.dumps(message) + "\n", timeout_s)
        result = ""
        for line in out.splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "result":
                if ev.get("is_error"):
                    raise AgentError(f"claude vision error: {ev.get('result') or ev.get('subtype')}")
                result = ev.get("result") or result
        return result

    def _run_gemini(self, prompt: str, timeout_s: int) -> str:
        # -p = non-interactive; plan approval-mode = read-only (no side effects).
        cmd = ["gemini", "-p", prompt, "-o", "text", "--approval-mode", "plan"]
        if self.model:
            cmd += ["-m", self.model]
        return self._exec(cmd, "", timeout_s)

    def _run_gemini_sdk(self, prompt: str, images: list[dict],
                        api_key: Optional[str] = None) -> str:
        """Gemini via the google-genai SDK: inline image Parts + the prompt in a
        single generate_content call. No tools, no file access — a pure
        inference request, governed like every other agent call."""
        try:
            from google import genai
            from google.genai import types
        except ImportError as e:
            raise AgentError(f"google-genai not installed (needed for gemini vision): {e}")
        contents: list = []
        for img in images:
            try:
                data = Path(img["path"]).read_bytes()
            except OSError:
                continue
            contents.append(types.Part.from_bytes(
                data=data, mime_type=img.get("media_type", "image/png")))
        contents.append(prompt)
        try:
            client = genai.Client(api_key=api_key) if api_key else genai.Client()
            resp = client.models.generate_content(
                model=self.model or "gemini-2.5-flash", contents=contents)
        except Exception as e:  # noqa: BLE001 — surface as a normal agent error
            raise AgentError(f"gemini SDK error: {e}")
        return resp.text or ""

    def _run_codex(self, prompt: str, timeout_s: int, images: list[dict] | None = None) -> str:
        # codex exec writes its final message cleanly to --output-last-message.
        # Images attach with --image=<path> (verified: reads text in images).
        fd, outfile = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        try:
            # --skip-git-repo-check: codex refuses to run outside a "trusted"
            # (git) directory, and we deliberately run every CLI from a neutral
            # EMPTY dir (see _neutral_cwd) — codex has no tools enabled here, so
            # the trust check protects nothing and only kills the seat.
            cmd = ["codex", "exec", "--color", "never", "--skip-git-repo-check",
                   "--output-last-message", outfile]
            if self.model:
                cmd += ["-m", self.model]
            for img in images or []:
                if Path(img["path"]).is_file():
                    cmd.append(f"--image={img['path']}")
            cmd.append("-")  # read the prompt from stdin
            self._exec(cmd, prompt, timeout_s)
            return Path(outfile).read_text(encoding="utf-8", errors="replace")
        finally:
            try:
                os.unlink(outfile)
            except OSError:
                pass
