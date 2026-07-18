"""OpenRouter adapter — one OpenRouter-hosted model exposed as a council seat.

OpenRouter is an OpenAI-compatible gateway to many providers (DeepSeek, GLM,
Qwen, Kimi, …). Each seat is registered under a friendly name (e.g. "deepseek")
and can be assigned to any role, alongside the local CLI agents.

Pure inference (no tools), like every Gang of 8 agent — the council reasons,
the coordinator governs side effects. Privacy: `provider.data_collection: deny`
by default so OpenRouter won't route through providers that retain/train on the
prompt. The API key is resolved lazily via the injected getter (env or the
local secrets store).
"""

from __future__ import annotations

import json
import threading
import time
from typing import Callable, Optional

import httpx

from .. import cancellation, config
from ..cancellation import SessionCancelled
from ..models import Role
from ..registry import AdapterResult, AgentError

_APP_TITLE = "Gang of 8"
_REFERER = "https://github.com/Digitalgods2/gangof8"


class OpenRouterAdapter:
    # A plain HTTP request, not a local subprocess — bounded by the (larger)
    # API-seat concurrency limit, so it never queues behind heavy CLI seats.
    local_process = False
    streams_progress = True

    def __init__(
        self,
        name: str,
        model_slug: str,
        api_key_getter: Callable[[], Optional[str]],
        endpoint: str = "https://openrouter.ai/api/v1",
        data_collection: str = "deny",
        role_models: Optional[dict] = None,
    ) -> None:
        if not name or not model_slug:
            raise ValueError("OpenRouterAdapter requires name and model_slug")
        self.name = name
        self.model_slug = model_slug
        # role name → slug: optional per-ROLE pins layered over the seat slug
        # (role pin › seat slug), mirroring the CLI adapter's role_models.
        self.role_models = dict(role_models or {})
        self._key_getter = api_key_getter
        self.endpoint = endpoint.rstrip("/")
        self.data_collection = data_collection if data_collection in ("deny", "allow") else "deny"

    def call(self, role: Role, prompt: str, timeout_s: int,
             images: Optional[list[dict]] = None) -> AdapterResult:
        key = self._key_getter()
        if not key:
            raise AgentError(
                f"{self.name}: no OpenRouter API key — set OPENROUTER_API_KEY or "
                "add it in Settings → API Keys")
        slug = self.role_models.get(getattr(role, "value", str(role))) or self.model_slug
        payload = {
            "model": slug,
            "messages": [{"role": "user", "content": prompt}],
            "provider": {"data_collection": self.data_collection},
            # Streaming is correctness infrastructure, not cosmetic output. It
            # lets the coordinator distinguish a long productive coding call
            # from an open socket that has stopped producing model tokens.
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "X-Title": _APP_TITLE,
            "HTTP-Referer": _REFERER,
        }
        # Own the client so a cancel can close it mid-flight. An OpenRouter call
        # is a plain HTTP request with no subprocess to kill, so without this a
        # cancelled run's abandoned seat would keep blocking until the timeout.
        # We register a closer that `cancellation.request` invokes to tear down
        # the connection; the torn read then surfaces as SessionCancelled.
        sid = cancellation.current_session()
        if sid and cancellation.is_requested(sid):
            raise SessionCancelled()
        t0 = time.monotonic()
        # httpx's scalar timeout is per socket operation. A provider can keep a
        # request alive forever with transport-level trickles, which is exactly
        # how the live Qwen call ran for 16 minutes under a nominal 360-second
        # limit. Reads are therefore unbounded here and the watchdog below owns
        # an opted-in hard deadline. Streaming supplies truthful progress and
        # the no-output watchdog still detects a genuinely stalled provider.
        transport_timeout = httpx.Timeout(
            None,
            connect=30.0,
            write=30.0,
            pool=30.0,
        )
        client = httpx.Client(timeout=transport_timeout)
        finished = threading.Event()
        stalled = threading.Event()
        deadline_reason = ["timed out"]
        progress_lock = threading.Lock()
        last_progress = [time.monotonic()]
        output_chars = [0]

        def _abort() -> None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass

        tail_buf = [""]

        def _model_progress(chars: int, tail_delta: str = "") -> None:
            with progress_lock:
                last_progress[0] = time.monotonic()
                output_chars[0] = max(output_chars[0], chars)
                if tail_delta:
                    tail_buf[0] = (tail_buf[0] + tail_delta)[-400:]
            cancellation.report_progress(
                output_chars[0], "model output streaming", tail=tail_buf[0])

        def _watch_deadline() -> None:
            deadline_s = max(1.0, float(timeout_s)) if timeout_s > 0 else None
            stall_s = float(config.OPENROUTER_OUTPUT_STALL_TIMEOUT)
            while not finished.wait(0.5):
                with progress_lock:
                    quiet_for = time.monotonic() - last_progress[0]
                elapsed = time.monotonic() - t0
                if quiet_for >= stall_s:
                    deadline_reason[0] = "stalled"
                    stalled.set()
                    _abort()
                    return
                if deadline_s is not None and elapsed >= deadline_s:
                    deadline_reason[0] = "timed out"
                    stalled.set()
                    _abort()
                    return

        def _deadline_error() -> str:
            if deadline_reason[0] == "stalled":
                return (
                    f"{self.name} (OpenRouter) stalled: no model output for "
                    f"{config.OPENROUTER_OUTPUT_STALL_TIMEOUT}s"
                )
            return f"{self.name} (OpenRouter) timed out after {timeout_s}s"

        watchdog = threading.Thread(
            target=_watch_deadline,
            name=f"gangof8-{self.name}-deadline-watch",
            daemon=True,
        )
        cancellation.register_canceler(sid, _abort)
        watchdog.start()
        try:
            content_parts: list[str] = []
            usage: dict = {}
            response_model = slug
            status_code = 200
            error_text = ""

            # Old tests and third-party client doubles may expose only .post().
            # Keep that compatibility path, while real httpx clients use SSE.
            if callable(getattr(client, "stream", None)):
                with client.stream(
                    "POST", f"{self.endpoint}/chat/completions",
                    headers=headers, json=payload,
                ) as resp:
                    status_code = resp.status_code
                    if status_code != 200:
                        try:
                            error_text = resp.read().decode("utf-8", errors="replace")
                        except Exception:  # noqa: BLE001 - error reporting only
                            error_text = getattr(resp, "text", "") or ""
                    else:
                        for raw_line in resp.iter_lines():
                            if sid and cancellation.is_requested(sid):
                                raise SessionCancelled()
                            # The watchdog is required to interrupt a blocked
                            # socket, but thread scheduling must not decide
                            # whether a routine total deadline is enforced. A
                            # final SSE line arriving after the deadline is
                            # still late even if the watcher has not run yet.
                            if (timeout_s > 0
                                    and time.monotonic() - t0 >= max(1.0, float(timeout_s))):
                                deadline_reason[0] = "timed out"
                                stalled.set()
                                raise AgentError(_deadline_error())
                            line = (raw_line.decode("utf-8", errors="replace")
                                    if isinstance(raw_line, bytes) else str(raw_line or ""))
                            if not line.startswith("data:"):
                                continue  # comments/keep-alives are not model progress
                            data = line[5:].strip()
                            if not data or data == "[DONE]":
                                continue
                            try:
                                chunk = json.loads(data)
                            except ValueError:
                                continue
                            if chunk.get("error"):
                                err = chunk["error"]
                                msg = err.get("message") if isinstance(err, dict) else err
                                raise AgentError(f"{self.name} (OpenRouter) error: {msg}")
                            response_model = chunk.get("model") or response_model
                            if isinstance(chunk.get("usage"), dict):
                                usage = chunk["usage"]
                            choices = chunk.get("choices") or []
                            if choices and isinstance(choices[0], dict):
                                delta_obj = choices[0].get("delta") or {}
                                # Reasoning models can spend minutes streaming
                                # private reasoning before the first answer token.
                                # It proves model progress but is deliberately not
                                # copied into the delivered response.
                                reasoning = (delta_obj.get("reasoning")
                                             or delta_obj.get("reasoning_details"))
                                if isinstance(reasoning, str) and reasoning:
                                    _model_progress(output_chars[0] + len(reasoning))
                                elif isinstance(reasoning, list) and reasoning:
                                    _model_progress(
                                        output_chars[0] + len(json.dumps(reasoning)))
                                delta = delta_obj.get("content")
                                if isinstance(delta, str) and delta:
                                    content_parts.append(delta)
                                    _model_progress(
                                        output_chars[0] + len(delta), delta)
            else:
                fallback_payload = dict(payload)
                fallback_payload.pop("stream", None)
                fallback_payload.pop("stream_options", None)
                resp = client.post(
                    f"{self.endpoint}/chat/completions",
                    headers=headers, json=fallback_payload,
                )
                status_code = resp.status_code
                error_text = getattr(resp, "text", "") or ""
                if status_code == 200:
                    try:
                        body = resp.json()
                    except ValueError as e:
                        raise AgentError(
                            f"{self.name} (OpenRouter) returned non-JSON: {error_text[:200]!r}"
                        ) from e
                    if body.get("error"):
                        err = body["error"]
                        msg = err.get("message") if isinstance(err, dict) else err
                        raise AgentError(f"{self.name} (OpenRouter) error: {msg}")
                    choices = body.get("choices") or []
                    if choices and isinstance(choices[0], dict):
                        value = ((choices[0].get("message") or {}).get("content") or "")
                        if value:
                            content_parts.append(value)
                            _model_progress(len(value))
                    usage = body.get("usage") or {}
                    response_model = body.get("model") or response_model
        except httpx.TimeoutException as e:
            raise AgentError(
                f"{self.name} (OpenRouter) transport timed out after {timeout_s}s"
            ) from e
        except httpx.HTTPError as e:
            # A cancel that closed the client surfaces here as a transport error;
            # report it as cancellation, not a generic failure.
            if sid and cancellation.is_requested(sid):
                raise SessionCancelled() from e
            if stalled.is_set():
                raise AgentError(_deadline_error()) from e
            raise AgentError(f"{self.name} (OpenRouter) request failed: {e}") from e
        except (AgentError, SessionCancelled):
            raise
        except Exception as e:  # a cross-thread client.close may surface as RuntimeError
            if sid and cancellation.is_requested(sid):
                raise SessionCancelled() from e
            if stalled.is_set():
                raise AgentError(_deadline_error()) from e
            raise AgentError(f"{self.name} (OpenRouter) request failed: {e}") from e
        finally:
            finished.set()
            cancellation.unregister_canceler(sid, _abort)
            client.close()
            watchdog.join(timeout=0.2)
        if sid and cancellation.is_requested(sid):
            raise SessionCancelled()
        if stalled.is_set():
            raise AgentError(_deadline_error())
        if status_code != 200:
            detail = (error_text or "").strip()[:300]
            raise AgentError(f"{self.name} (OpenRouter) HTTP {status_code}: {detail}")
        content = "".join(content_parts).strip()
        if not content:
            raise AgentError(f"{self.name} (OpenRouter) returned empty output")
        tokens = (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)
        return AdapterResult(content=content, tokens=tokens,
                             duration_ms=int((time.monotonic() - t0) * 1000),
                             model=response_model)
