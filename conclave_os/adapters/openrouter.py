"""OpenRouter adapter — one OpenRouter-hosted model exposed as a council seat.

OpenRouter is an OpenAI-compatible gateway to many providers (DeepSeek, GLM,
Qwen, Kimi, …). Each seat is registered under a friendly name (e.g. "deepseek")
and can be assigned to any role, alongside the local CLI agents.

Pure inference (no tools), like every Conclave OS agent — the council reasons,
the coordinator governs side effects. Privacy: `provider.data_collection: deny`
by default so OpenRouter won't route through providers that retain/train on the
prompt. The API key is resolved lazily via the injected getter (env or the
local secrets store).
"""

from __future__ import annotations

import time
from typing import Callable, Optional

import httpx

from ..models import Role
from ..registry import AdapterResult, AgentError

_APP_TITLE = "Conclave OS"
_REFERER = "https://github.com/Digitalgods2/conclave-os"


class OpenRouterAdapter:
    def __init__(
        self,
        name: str,
        model_slug: str,
        api_key_getter: Callable[[], Optional[str]],
        endpoint: str = "https://openrouter.ai/api/v1",
        data_collection: str = "deny",
    ) -> None:
        if not name or not model_slug:
            raise ValueError("OpenRouterAdapter requires name and model_slug")
        self.name = name
        self.model_slug = model_slug
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
        payload = {
            "model": self.model_slug,
            "messages": [{"role": "user", "content": prompt}],
            "provider": {"data_collection": self.data_collection},
        }
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "X-Title": _APP_TITLE,
            "HTTP-Referer": _REFERER,
        }
        t0 = time.monotonic()
        try:
            resp = httpx.post(f"{self.endpoint}/chat/completions",
                              headers=headers, json=payload, timeout=max(30, timeout_s))
        except httpx.TimeoutException as e:
            raise AgentError(f"{self.name} (OpenRouter) timed out after {timeout_s}s") from e
        except httpx.HTTPError as e:
            raise AgentError(f"{self.name} (OpenRouter) request failed: {e}") from e
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
                             duration_ms=int((time.monotonic() - t0) * 1000))
