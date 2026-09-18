"""Direct local-CLI agent adapter — Gang of 8 runs the agent CLIs itself.

Each call invokes the local CLI in plain non-interactive generation mode and
returns its raw text output. That is what lets the implementer emit real file
bodies instead of descriptions — Gang of 8 is fully self-contained.

Reviewer calls are read-only. Codex author calls may edit only a fresh,
hash-sealed disposable package directory; changed text files are converted back
into the normal ARTIFACT protocol and still pass Gang of 8's path, contract,
validation, executor, and approval gates. Print mode is one-shot, so there is
no awaiting_user_input/resume path here.

Supported agents: claude (fully exercised), codex, gemini. The gemini seat runs
on the Antigravity CLI (``agy``) first, the successor Google moved personal
accounts to when it retired the gemini CLI on 2026-06-18, and falls back to the
google-genai API key.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import re
import shlex
import shutil
import shutil as _shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from .. import cancellation, config
from ..cancellation import SessionCancelled
from ..models import Role
from ..registry import AdapterResult, AgentCallStopped, AgentError


_OPAQUE_SUFFIXES = {
    ".7z", ".avi", ".docx", ".gif", ".gz", ".ico", ".jpeg", ".jpg",
    ".mov", ".mp3", ".mp4", ".pdf", ".png", ".pptx", ".tar", ".webp",
    ".xlsx", ".zip",
}


def _workspace_snapshot(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    try:
        paths = list(root.rglob("*"))
    except OSError:
        return snapshot
    for path in paths:
        if not path.is_file():
            continue
        try:
            name = path.relative_to(root).as_posix()
            snapshot[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return snapshot


def _workspace_change_envelopes(root: Path, before: dict[str, str]) -> tuple[str, list[str]]:
    """Import native CLI edits through the same governed ARTIFACT protocol."""
    after = _workspace_snapshot(root)
    changed = [name for name, digest in after.items()
               if before.get(name) != digest and name != "_gangof8_manifest.json"]
    blocks: list[str] = []
    accepted: list[str] = []
    for name in changed:
        path = root / name
        if path.suffix.lower() in _OPAQUE_SUFFIXES:
            continue
        try:
            raw = path.read_bytes()
            if len(raw) > 4_000_000 or b"\x00" in raw:
                continue
            body = raw.decode("utf-8")
        except (OSError, UnicodeError):
            continue
        blocks.append(f"ARTIFACT: {name}\n{body}\nEND_ARTIFACT")
        accepted.append(name)
    return "\n\n".join(blocks), accepted


def _err_tail(text: str, limit: int = 300) -> str:
    """The most informative slice of a CLI's error output: the END. CLIs print
    banners and prompt echoes first and the actual error last — codex's banner
    alone exceeds 300 chars, so head-truncation hid every real error behind
    'OpenAI Codex v… workdir: …' (live: a delegation failure whose cause was
    unreadable)."""
    t = (text or "").strip()
    return t if len(t) <= limit else "… " + t[-limit:]


# The cwd of the CLI call running on THIS thread. The adapter instance is
# shared across the panel fan-out, so the per-call directory cannot live on
# self — see CliAdapter.call.
_CALL_DIR = threading.local()


def _neutral_root() -> Path:
    d = config.SANDBOX_ROOT / "cli-neutral"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _neutral_cwd() -> str:
    """Fallback cwd for CLI invocations made outside a governed agent call
    (auth_status). Governed calls get their own directory — see _call_dir."""
    return str(_neutral_root())


# Call directories currently in use. The scratch sweep consults this so it can
# never delete the working directory of a call that is still running — a call
# has no fixed duration (timeout_s=0 means operator-supervised), so an age
# threshold alone would eventually be wrong.
_LIVE_DIRS: set[str] = set()
_LIVE_LOCK = threading.Lock()


def live_call_dirs() -> set[str]:
    """Snapshot of the call directories in use right now (read by the sweep)."""
    with _LIVE_LOCK:
        return set(_LIVE_DIRS)


def _call_dir() -> Path:
    """A FRESH, EMPTY directory for one CLI call.

    CLI subprocesses must never run from the server's own repo/cwd: an agent
    with latent tool instincts (claude attempting Read calls, codex scanning
    its workspace) must not perceive — or ungovernedly read — whatever folder
    the server happens to run in. (Live failure: a claude lead running with
    cwd=the repo said "I'm running in the actual repo" and emitted tool-call
    debris instead of a synthesis.)

    It must also never be SHARED. Every call used to run from one persistent
    'cli-neutral' directory, and a CLI seat can write into its cwd despite the
    no-side-effect flags below — so that directory silently accumulated 36
    files across unrelated sessions (another task's index.html, a whole
    manuscript/ and output/ tree, browser-tool logs). A later session's seat
    could read an earlier one's leftovers, and the directory the docstring
    called 'empty' had not been empty for weeks. Any seat can be the lead and
    any seat can leave debris, so the isolation is per CALL, not per vendor.
    """
    return Path(tempfile.mkdtemp(prefix="call-", dir=str(_neutral_root())))


# Scratch subdirectory of a call directory that holds the seat's TEMP/TMP.
# Contained on purpose, and not reported as an ungoverned write: a tool writing
# its own temp files is normal, and only DELIBERATE output is a governance
# signal worth surfacing to the human.
_TMP_SUBDIR = "_tmp"


def _contained_env(call_dir: Optional[str]) -> Optional[dict]:
    """Point the seat's TEMP/TMP inside its own call directory.

    A CLI seat that cannot write to its cwd will still happily write to the
    user's %TEMP%, which is shared with every other program on the machine and
    survives the run. Redirecting it keeps that spill inside the directory the
    coordinator already owns and garbage-collects.

    HOME/USERPROFILE are deliberately left ALONE: every seat reads its own
    credentials from under the real home directory, so redirecting it would
    break authentication for whichever seats are enabled."""
    if not call_dir:
        return None
    env = os.environ.copy()
    tmp = Path(call_dir) / _TMP_SUBDIR
    try:
        tmp.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    env["TEMP"] = env["TMP"] = env["TMPDIR"] = str(tmp)
    return env


def _quarantine_call_dir(d: Path, sid: Optional[str]) -> list[str]:
    """Empty call directory → removed. Anything the seat wrote → MOVED into
    the owning session's sandbox under '_ungoverned/', never deleted.

    These files are, by definition, side effects that did not pass through the
    executor or the approval kernel. Applies to every local CLI seat equally —
    each one is a subprocess with the user's privileges, whatever its own
    sandbox flag claims. They are not deleted because a binary deliverable (a
    PDF) has no governed path today and destroying it would lose real work —
    but they are quarantined out of the shared root, bound to the session that
    produced them, and reported so they cannot be silently presented as
    delivered output. Returns the relative paths written."""
    _shutil.rmtree(d / _TMP_SUBDIR, ignore_errors=True)  # contained scratch, not output
    try:
        written = sorted(str(f.relative_to(d)) for f in d.rglob("*") if f.is_file())
    except OSError:
        return []
    if not written:
        _shutil.rmtree(d, ignore_errors=True)
        return []
    dest_root = (config.SANDBOX_ROOT / sid / "_ungoverned") if sid         else (_neutral_root() / "_ungoverned")
    try:
        dest_root.mkdir(parents=True, exist_ok=True)
        _shutil.move(str(d), str(dest_root / d.name))
    except OSError:
        pass  # leave it where it is rather than lose the bytes
    return written


_AGY_DENY_REASON = "Gang of 8 seats have no native tools; answer from the prompt only."
_AGY_MODELS: Optional[list[str]] = None
_AGY_LOCK = threading.Lock()
_AGY_EFFORT_SUFFIX = re.compile(r"-(?:high|medium|low)$")


def gemini_cli() -> Optional[str]:
    """The gemini seat's local CLI: Antigravity when installed, else the
    retired gemini CLI (still served for Code Assist Standard/Enterprise)."""
    for name in ("agy", "gemini"):
        if shutil.which(name):
            return name
    return None


def cli_available(agent: str) -> bool:
    """Is this seat's local CLI installed? The gemini seat's binary is no
    longer named after the seat."""
    if agent == "gemini":
        return gemini_cli() is not None
    return shutil.which(agent) is not None


def agy_models(timeout_s: int = 30) -> list[str]:
    """Gemini model ids the Antigravity CLI accepts for ``--model`` (cached).

    Listing is not an inference call. Its ids differ from the API's (effort
    suffixes such as ``-high``), and an unknown id is refused outright, so a
    pin is passed only when it is on this list. Claude/GPT models that
    Antigravity also offers are left out: this is the gemini seat."""
    global _AGY_MODELS
    with _AGY_LOCK:
        if _AGY_MODELS is not None:
            return list(_AGY_MODELS)
    exe = shutil.which("agy")
    if not exe:
        return []
    try:
        proc = subprocess.run(
            [exe, "models"], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout_s, cwd=_neutral_cwd())
    except (OSError, subprocess.TimeoutExpired):
        return []
    models = [line.split("\t", 1)[0].strip() for line in proc.stdout.splitlines()
              if "\t" in line and line.startswith("gemini")]
    if models:
        with _AGY_LOCK:
            _AGY_MODELS = models
    return models


def _agy_guard_dir() -> Path:
    """A workspace folder whose ``.agents/hooks.json`` denies EVERY tool.

    Antigravity has no switch to turn its tools off, and headless it will
    list folders and search the web without asking (verified live). Its
    PreToolUse hook can hard-deny a tool before it runs; the folder is added
    to each call's workspace so the hook loads. Rewritten when it drifts, as
    the sandbox root is swept."""
    agents = config.SANDBOX_ROOT / "agy-guard" / ".agents"
    agents.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"decision": "deny", "reason": _AGY_DENY_REASON})
    if os.name == "nt":
        script = agents / "deny.cmd"
        body = f"@echo {payload}\r\n"
        command = str(script)
    else:
        script = agents / "deny.sh"
        body = f"#!/bin/sh\ncat >/dev/null\nprintf '%s\\n' '{payload}'\n"
        command = f"sh {shlex.quote(str(script))}"
    hooks = json.dumps({"gangof8-no-tools": {"PreToolUse": [{
        "matcher": "*",
        "hooks": [{"type": "command", "command": command, "timeout": 10}],
    }]}})
    for path, text in ((script, body), (agents / "hooks.json", hooks)):
        try:
            current = path.read_text(encoding="utf-8")
        except OSError:
            current = None
        if current != text:
            path.write_text(text, encoding="utf-8", newline="")
    return agents.parent


class CliAdapter:
    # A heavy local subprocess: counts against the machine-wide CLI concurrency
    # bound (loop._agent_call). HTTP-backed adapters set this False and share a
    # larger bound of their own.
    local_process = True
    # The registry may pass an explicit working directory (a disposable review
    # copy of release files) so an agentic CLI inspecting "its workspace" sees
    # the real bytes under review instead of the empty neutral sandbox — a
    # codex release verifier FAILed a passing game with "frogger.html is
    # absent from the workspace" for exactly that reason.
    supports_cwd = True
    _cwd_override: Optional[str] = None

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

    def auth_status(self, timeout_s: int = 15) -> tuple[Optional[bool], str]:
        """Check local CLI authentication without spending an inference call.

        ``None`` means the CLI has no stable non-generative status command, so
        the caller should leave that seat available and let normal execution
        report any later failure.
        """
        try:
            if self.agent == "claude":
                out, err, rc = self._exec_raw(
                    ["claude", "auth", "status", "--json"], "", timeout_s)
                if rc != 0:
                    return False, _err_tail(err.strip() or out.strip())
                try:
                    logged_in = bool(json.loads(out).get("loggedIn"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    return False, "claude auth status returned invalid JSON"
                return logged_in, "logged in" if logged_in else "not logged in"
            if self.agent == "codex":
                out, err, rc = self._exec_raw(["codex", "login", "status"], "", timeout_s)
                if rc != 0:
                    return False, _err_tail(err.strip() or out.strip())
                return True, "logged in"
        except AgentError as e:
            return False, str(e)
        return None, "no non-generative authentication status command"

    def call(self, role: Role, prompt: str, timeout_s: int,
             images: list[dict] | None = None,
             cwd: Optional[str] = None) -> AdapterResult:
        if cwd and self._cwd_override != cwd:
            # call-local clone, same pattern as the role-model pin below: the
            # adapter instance is shared across fan-out threads.
            clone = copy.copy(self)
            clone._cwd_override = cwd
            return clone.call(role, prompt, timeout_s, images)
        pinned = self.role_models.get(getattr(role, "value", str(role)))
        if pinned and pinned != self.model:
            # The runner methods read self.model, and this adapter instance is
            # shared across the panel fan-out threads — so apply the role pin
            # on a call-local CLONE, never by mutating self.
            clone = copy.copy(self)
            clone.model = pinned
            clone.role_models = {}
            return clone.call(role, prompt, timeout_s, images)
        # An explicit cwd is a caller-owned disposable copy. Author roles may
        # edit only that copy; changed text is converted back into ARTIFACT
        # envelopes so the normal contract filter and executor govern import.
        if self._cwd_override:
            root = Path(self._cwd_override)
            before = _workspace_snapshot(root)
            result = self._dispatch(role, prompt, timeout_s, images)
            envelopes, _changed = _workspace_change_envelopes(root, before)
            if envelopes:
                result.content = (
                    result.content.rstrip()
                    + "\n\nNATIVE WORKSPACE CHANGES CAPTURED BY COORDINATOR:\n"
                    + envelopes
                )
            return result
        sid = cancellation.current_session()
        d = _call_dir()
        previous = getattr(_CALL_DIR, "path", None)
        _CALL_DIR.path = str(d)
        with _LIVE_LOCK:
            _LIVE_DIRS.add(str(d))
        try:
            result = self._dispatch(role, prompt, timeout_s, images)
        finally:
            _CALL_DIR.path = previous
            with _LIVE_LOCK:
                _LIVE_DIRS.discard(str(d))
            written = _quarantine_call_dir(d, sid)
        result.ungoverned_writes = written
        return result

    def _dispatch(self, role: Role, prompt: str, timeout_s: int,
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
            content = self._run_codex(role, prompt, timeout_s, images)  # --image=<path>
        elif self.agent == "gemini":
            content, model = self._run_gemini_routes(prompt, images, timeout_s)
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
        call_id = cancellation.current_call()
        isolated = getattr(_CALL_DIR, "path", None)
        try:
            proc = subprocess.Popen(
                [exe, *cmd[1:]], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                cwd=(isolated or self._cwd_override or _neutral_cwd()),
                env=_contained_env(isolated),
            )
        except (OSError, FileNotFoundError) as e:
            raise AgentError(f"{self.agent} CLI not runnable: {e}") from e
        cancellation.register_proc(sid, proc)
        try:
            # A non-positive timeout intentionally means no coordinator deadline.
            # The process remains cancellation-registered and can still be killed
            # instantly from the API/dashboard.
            deadline = None if timeout_s <= 0 else max(30, timeout_s)
            out, err = proc.communicate(input=prompt, timeout=deadline)
        except subprocess.TimeoutExpired as e:
            cancellation.kill_tree(proc)
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
        if sid and call_id and cancellation.is_call_requested(sid, call_id):
            raise AgentCallStopped(f"{self.agent} CLI stopped by operator")
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

    def _claude_model_id(self) -> Optional[str]:
        """The claude CLI expects DASH-form model ids (claude-opus-4-8), but the
        Settings dropdown is fed from OpenRouter's public catalog, which lists
        Anthropic models with DOTS (claude-opus-4.8) — the CLI rejects those
        ('model may not exist'), dropping the seat every round. Claude ids never
        contain a dot, so normalizing dots→dashes corrects a stale/dotted pin."""
        return self.model.replace(".", "-") if self.model else self.model

    def _run_claude(self, prompt: str, timeout_s: int) -> tuple[str, Optional[str]]:
        """Returns (content, model): the CLI's JSON result names the model that
        actually ran (modelUsage keys), so an unpinned seat is still attributable."""
        cmd = ["claude", "-p", "--output-format", "json", "--tools", ""]
        if self.model:
            cmd += ["--model", self._claude_model_id()]
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
            cmd += ["--model", self._claude_model_id()]
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

    def _run_gemini_routes(self, prompt: str, images: list[dict] | None,
                           timeout_s: int) -> tuple[str, Optional[str]]:
        """Subscription first, paid API key second, retired CLI last.

        The Antigravity CLI runs on the user's Google subscription; the
        google-genai SDK bills the API key per call, so it is the fallback
        (and the route for images, which it sends inline). The model label
        names the route so a paid call is visible wherever the model is."""
        key = (self._api_key_getter() if self._api_key_getter else None) \
            or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        agy_failure = ""
        if shutil.which("agy") and not (images and key):
            try:
                return self._run_agy(prompt, timeout_s)
            except (SessionCancelled, AgentCallStopped):
                raise
            except AgentError as e:
                if not key:
                    raise
                agy_failure = str(e)
        if key:
            sdk_model = self._sdk_model()
            content = self._run_gemini_sdk(prompt, images or [], key, sdk_model)
            label = "API key fallback" if agy_failure else "API key"
            return content, f"{sdk_model} ({label})"
        # Only the old gemini CLI is left (Code Assist Standard/Enterprise).
        # It overflows the Windows command line on long prompts.
        return self._run_gemini(prompt, timeout_s), self.model

    def _sdk_model(self) -> str:
        """The pin in the API's form: Antigravity ids carry an effort suffix
        (gemini-3.1-pro-high) that the API does not know."""
        if self.model:
            return _AGY_EFFORT_SUFFIX.sub("", self.model)
        return "gemini-2.5-flash"

    def _run_agy(self, prompt: str, timeout_s: int) -> tuple[str, Optional[str]]:
        """One headless Antigravity turn, prompt on stdin, every tool denied.

        ``-p <prompt>`` would put the prompt on the command line, which
        overflows on Windows, so it goes as one stream-json message on stdin
        (``-p=`` with an empty value is what print mode needs for that). A
        denied tool step ends in state ERROR; one that reaches DONE means the
        guard did not load, so the reply is discarded rather than trusted."""
        pinned = self.model if self.model and self.model in agy_models() else None
        deadline = f"{timeout_s}s" if timeout_s > 0 else "24h"
        cmd = ["agy", "--add-dir", str(_agy_guard_dir()),
               "--input-format", "stream-json", "--output-format", "stream-json",
               "--print-timeout", deadline]
        if pinned:
            cmd += ["--model", pinned]
        cmd.append("-p=")
        message = json.dumps({"event": "user", "message": {"content": prompt}})
        out, err, rc = self._exec_raw(cmd, message + "\n", timeout_s)
        result: dict = {}
        ran: set[str] = set()
        for line in out.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event") == "result":
                result = event.get("result") or {}
            step = event.get("step_update") or {}
            if step.get("step_type") == "tool" and step.get("state") == "DONE":
                ran.add(str(step.get("tool_name") or "unknown"))
        if ran:
            raise AgentError(
                f"agy ran native tools ({', '.join(sorted(ran))}) despite the "
                "deny hook; its reply was discarded")
        if not result:
            raise AgentError(f"agy CLI exited {rc}: {_err_tail(err.strip() or out.strip())}")
        if result.get("status") != "SUCCESS":
            raise AgentError(f"agy CLI error: {_err_tail(str(result.get('error') or result))}")
        label = f"{pinned} (Antigravity)" if pinned else "Antigravity default"
        return str(result.get("response") or ""), label

    def _run_gemini(self, prompt: str, timeout_s: int) -> str:
        # -p = non-interactive; plan approval-mode = read-only (no side effects).
        cmd = ["gemini", "-p", prompt, "-o", "text", "--approval-mode", "plan"]
        if self.model:
            cmd += ["-m", self.model]
        return self._exec(cmd, "", timeout_s)

    def _run_gemini_sdk(self, prompt: str, images: list[dict],
                        api_key: Optional[str] = None,
                        model: Optional[str] = None) -> str:
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
        sid = cancellation.current_session()
        call_id = cancellation.current_call()
        client = genai.Client(api_key=api_key) if api_key else genai.Client()

        def _abort() -> None:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

        cancellation.register_canceler(sid, _abort)
        try:
            resp = client.models.generate_content(
                model=model or self._sdk_model(), contents=contents)
        except Exception as e:  # noqa: BLE001 — surface as a normal agent error
            if sid and cancellation.is_requested(sid):
                raise SessionCancelled() from e
            if sid and call_id and cancellation.is_call_requested(sid, call_id):
                raise AgentCallStopped("gemini SDK call stopped by operator") from e
            raise AgentError(f"gemini SDK error: {e}")
        finally:
            cancellation.unregister_canceler(sid, _abort)
            _abort()
        if sid and cancellation.is_requested(sid):
            raise SessionCancelled()
        if sid and call_id and cancellation.is_call_requested(sid, call_id):
            raise AgentCallStopped("gemini SDK call stopped by operator")
        return resp.text or ""

    def _run_codex(self, role: Role, prompt: str, timeout_s: int,
                   images: list[dict] | None = None) -> str:
        # codex exec writes its final message cleanly to --output-last-message.
        # Images attach with --image=<path> (verified: reads text in images).
        fd, outfile = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        try:
            # --skip-git-repo-check: codex refuses to run outside a "trusted"
            # (git) directory, and we deliberately run every CLI from a neutral
            # EMPTY dir (see _neutral_cwd) — codex has no tools enabled here, so
            # the trust check protects nothing and only kills the seat.
            writable = bool(
                self._cwd_override
                and role in {Role.code_generator, Role.implementer}
            )
            cmd = ["codex", "exec", "--color", "never", "--skip-git-repo-check",
                   "--sandbox", "workspace-write" if writable else "read-only",
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
