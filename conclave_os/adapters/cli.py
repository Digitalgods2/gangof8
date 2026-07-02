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
    def __init__(self, agent: str, name: Optional[str] = None, model: Optional[str] = None):
        self.agent = agent  # claude | codex | gemini
        self.name = name or agent
        self.model = model

    def call(self, role: Role, prompt: str, timeout_s: int,
             images: list[dict] | None = None) -> AdapterResult:
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
            # when an API key is present: a clean inference call that sidesteps the
            # gemini CLI's headless problems — its long prompts overflow the
            # Windows command line ('-p <prompt>' as argv), and plan-mode hangs.
            # No key ⇒ fall back to the CLI (which still has those limitations).
            if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
                content = self._run_gemini_sdk(prompt, images or [])
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

    def _exec(self, cmd: list[str], prompt: str, timeout_s: int) -> str:
        """Run a CLI command with the prompt on stdin; return stdout. Uses Popen
        (not subprocess.run) and registers the process for the current session so
        a cancel can KILL it mid-flight — a killed call surfaces as SessionCancelled
        instead of a generic error. The executable is resolved via PATH
        (shutil.which) so Windows .cmd/.exe shims are found and run directly."""
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
        if proc.returncode != 0:
            raise AgentError(
                f"{self.agent} CLI exited {proc.returncode}: {(err or '').strip()[:300]}"
            )
        return out or ""

    def _run_claude(self, prompt: str, timeout_s: int) -> tuple[str, Optional[str]]:
        """Returns (content, model): the CLI's JSON result names the model that
        actually ran (modelUsage keys), so an unpinned seat is still attributable."""
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
        used = None
        usage = data.get("modelUsage")
        if isinstance(usage, dict) and usage:
            used = next(iter(usage))
        return data.get("result") or "", used

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

    def _run_gemini_sdk(self, prompt: str, images: list[dict]) -> str:
        """Vision for the gemini role via the google-genai SDK: inline image
        Parts + the prompt in a single generate_content call. No tools, no file
        access — a pure inference request, governed like every other agent call."""
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
            client = genai.Client()
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
