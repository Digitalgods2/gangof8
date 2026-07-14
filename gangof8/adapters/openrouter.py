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

import time
from typing import Callable, Optional

import httpx

from .. import cancellation
from ..cancellation import SessionCancelled
from ..models import Role
from ..registry import AdapterResult, AgentError

_APP_TITLE = "Gang of 8"
_REFERER = "https://github.com/Digitalgods2/gangof8"


class OpenRouterAdapter:
    # A plain HTTP request, not a local subprocess — bounded by the (larger)
    # API-seat concurrency limit, so it never queues behind heavy CLI seats.
    local_process = False

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
        client = httpx.Client(timeout=max(30, timeout_s))

        def _abort() -> None:
            try:
                client.close()
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass

        cancellation.register_canceler(sid, _abort)
        try:
            resp = client.post(f"{self.endpoint}/chat/completions",
                               headers=headers, json=payload)
        except httpx.TimeoutException as e:
            raise AgentError(f"{self.name} (OpenRouter) timed out after {timeout_s}s") from e
        except httpx.HTTPError as e:
            # A cancel that closed the client surfaces here as a transport error;
            # report it as cancellation, not a generic failure.
            if sid and cancellation.is_requested(sid):
                raise SessionCancelled() from e
            raise AgentError(f"{self.name} (OpenRouter) request failed: {e}") from e
        finally:
            cancellation.unregister_canceler(sid, _abort)
            client.close()
        if sid and cancellation.is_requested(sid):
            raise SessionCancelled()
        if resp.status_code != 200:
            detail = (resp.text or "").strip()[:300]
            raise AgentError(f"{self.name} (OpenRouter) HTTP {resp.status_code}: {detail}")
        try:
            body = resp.json()
        except ValueError as e:
            raise AgentError(f"{self.name} (OpenRouter) returned non-JSON: {resp.text[:200]!r}") from e
        if body.get("error"):
            err = body["error"]
            msg = err.get("message") if isinstance(err, dict) else err
            raise AgentError(f"{self.name} (OpenRouter) error: {msg}")
        choices = body.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise AgentError(f"{self.name} (OpenRouter) returned no choices")
        content = ((choices[0].get("message") or {}).get("content") or "").strip()
        if not content:
            raise AgentError(f"{self.name} (OpenRouter) returned empty output")
        usage = body.get("usage") or {}
        tokens = (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)
        return AdapterResult(content=content, tokens=tokens,
                             duration_ms=int((time.monotonic() - t0) * 1000),
                             model=body.get("model") or slug)
