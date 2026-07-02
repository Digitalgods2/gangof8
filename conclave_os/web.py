"""Web access for the council — the coordinator reaches the internet on the
agents' behalf (they stay pure inference). Two primitives:

- web_search(query): Google Search grounding via the gemini SDK — a synthesized,
  cited answer. Reuses GEMINI_API_KEY (no extra key).
- web_fetch(url): fetch a public http(s) URL and return its readable text.

SSRF-guarded: web_fetch refuses localhost / private / link-local / reserved
addresses so the council can never be steered into your internal services.
"""

from __future__ import annotations

import html as _html
import ipaddress
import os
import re
import socket
import urllib.request
from urllib.parse import urlparse

from . import config


class WebError(Exception):
    pass


def _guard_url(url: str) -> str:
    p = urlparse((url or "").strip())
    if p.scheme not in ("http", "https"):
        raise WebError("only http/https URLs are allowed")
    host = (p.hostname or "").lower()
    if not host:
        raise WebError("invalid URL (no host)")
    if host in ("localhost", "localhost.localdomain") or host.endswith(".local"):
        raise WebError("refusing to fetch a local address")
    try:
        addrs = {ai[4][0] for ai in socket.getaddrinfo(host, None)}
    except socket.gaierror:
        addrs = set()  # unresolvable: urlopen will fail naturally
    for a in addrs:
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved \
                or ip.is_multicast or ip.is_unspecified:
            raise WebError("refusing to fetch a private/internal address")
    return url


_SCRIPT = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")


def _html_to_text(raw: str) -> str:
    raw = _SCRIPT.sub(" ", raw)
    text = _html.unescape(_TAG.sub(" ", raw))
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def web_fetch(url: str) -> str:
    """Fetch a public http(s) URL and return its readable text (bounded)."""
    _guard_url(url)
    req = urllib.request.Request(
        url, headers={"User-Agent": "ConclaveOS/1.0 (council web_fetch)"})
    try:
        with urllib.request.urlopen(req, timeout=config.WEB_FETCH_TIMEOUT) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            raw = resp.read(config.WEB_FETCH_MAX_BYTES)
    except WebError:
        raise
    except Exception as e:  # noqa: BLE001 — surface as a clean skill error
        raise WebError(f"fetch failed: {e}") from e
    body = raw.decode("utf-8", errors="replace")
    if "html" in ctype or body.lstrip()[:1] == "<":
        body = _html_to_text(body)
    return (body[: config.WEB_FETCH_MAX_CHARS] or "(empty page)") + (
        "\n\n[truncated]" if len(body) > config.WEB_FETCH_MAX_CHARS else "")


def web_search(query: str) -> str:
    """Answer a query with live web grounding (Gemini + Google Search), returning
    a synthesized answer plus its source URLs. The key resolves from the env OR
    the Settings-stored secrets — an env var must not be the only way in."""
    from .secrets import SecretStore

    api_key = SecretStore(config.DATA_DIR).get("gemini")  # env override wins inside
    if not api_key:
        raise WebError("web_search needs a Gemini API key — set GEMINI_API_KEY or "
                       "add it in Settings → API keys (Google Search grounding)")
    try:
        from google import genai
        from google.genai import types
    except ImportError as e:
        raise WebError(f"google-genai not installed: {e}") from e
    try:
        client = genai.Client(api_key=api_key)
        cfg = types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())])
        resp = client.models.generate_content(
            model=config.WEB_SEARCH_MODEL, contents=query, config=cfg)
    except Exception as e:  # noqa: BLE001
        raise WebError(f"web search failed: {e}") from e

    answer = (resp.text or "").strip()
    sources: list[str] = []
    try:
        gm = resp.candidates[0].grounding_metadata
        for ch in (getattr(gm, "grounding_chunks", None) or []):
            web = getattr(ch, "web", None)
            uri = getattr(web, "uri", None)
            if uri:
                title = getattr(web, "title", None) or uri
                sources.append(f"- {title}: {uri}")
    except Exception:  # noqa: BLE001 — citations are best-effort
        pass
    out = answer or "(no answer)"
    if sources:
        out += "\n\nSources:\n" + "\n".join(sources[:8])
    return out[: config.WEB_SEARCH_MAX_CHARS]
