"""OpenRouter council seats: adapter, secret store, key endpoints, and the
enable→register wiring that lets a role be assigned to an OpenRouter model.
"""

import pytest
from fastapi.testclient import TestClient

from gangof8 import cancellation
from gangof8.adapters import openrouter as orm
from gangof8.adapters.openrouter import OpenRouterAdapter
from gangof8.models import Role
from gangof8.registry import AgentError
from gangof8.secrets import SecretStore
from gangof8.service import GangOf8Service


class _Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code = status
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def _client_cls(post_fn):
    """A fake httpx.Client whose .post delegates to post_fn; the adapter now owns
    and closes its client (so a cancel can tear it down mid-flight)."""
    class _FakeClient:
        def __init__(self, timeout=None):
            self.timeout = timeout

        def post(self, url, headers=None, json=None):
            return post_fn(url, headers=headers, json=json, timeout=self.timeout)

        def close(self):
            pass
    return _FakeClient


# --- adapter ------------------------------------------------------------------


def test_adapter_posts_and_parses(monkeypatch):
    captured = {}

    def fake_post(url, headers, json, timeout):
        captured.update(url=url, headers=headers, payload=json, timeout=timeout)
        return _Resp(200, {
            "choices": [{"message": {"content": "hi from deepseek"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        })

    monkeypatch.setattr(orm.httpx, "Client", _client_cls(fake_post))
    a = OpenRouterAdapter("deepseek", "deepseek/deepseek-v4-pro", lambda: "sk-or-key")
    out = a.call(Role.researcher, "hello", timeout_s=30)
    assert out.content == "hi from deepseek"
    assert out.tokens == 8
    assert captured["url"].endswith("/chat/completions")
    assert captured["payload"]["model"] == "deepseek/deepseek-v4-pro"
    assert captured["payload"]["messages"][0]["content"] == "hello"
    assert captured["payload"]["provider"]["data_collection"] == "deny"
    assert captured["headers"]["Authorization"] == "Bearer sk-or-key"


def test_adapter_without_key_errors():
    a = OpenRouterAdapter("deepseek", "x/y", lambda: None)
    with pytest.raises(AgentError, match="no OpenRouter API key"):
        a.call(Role.researcher, "hi", timeout_s=10)


def test_adapter_http_error_raises(monkeypatch):
    monkeypatch.setattr(orm.httpx, "Client",
                        _client_cls(lambda url, **k: _Resp(429, None, "rate limited")))
    a = OpenRouterAdapter("kimi", "moonshotai/kimi-k2.6", lambda: "sk-or-key")
    with pytest.raises(AgentError, match="HTTP 429"):
        a.call(Role.critic, "hi", timeout_s=10)


def test_adapter_empty_output_raises(monkeypatch):
    monkeypatch.setattr(orm.httpx, "Client",
                        _client_cls(lambda url, **k: _Resp(200, {"choices": [{"message": {"content": "  "}}]})))
    a = OpenRouterAdapter("qwen", "qwen/qwen3.6-plus", lambda: "sk-or-key")
    with pytest.raises(AgentError, match="empty output"):
        a.call(Role.researcher, "hi", timeout_s=10)


def test_adapter_call_interrupted_by_cancel(monkeypatch):
    """A cancel mid-request tears down the client and surfaces as SessionCancelled
    instead of blocking until the HTTP timeout."""
    import threading

    from gangof8 import cancellation
    from gangof8.cancellation import SessionCancelled

    sid = "s_or_cancel"
    entered = threading.Event()

    class _BlockingClient:
        def __init__(self, timeout=None):
            self._closed = threading.Event()

        def post(self, url, headers=None, json=None):
            entered.set()
            # block like an in-flight call until close() (the cancel) fires
            if self._closed.wait(5):
                raise orm.httpx.ReadError("connection closed by cancel")
            return _Resp(200, {"choices": [{"message": {"content": "late"}}]})

        def close(self):
            self._closed.set()

    monkeypatch.setattr(orm.httpx, "Client", _BlockingClient)
    a = OpenRouterAdapter("kimi", "x/y", lambda: "sk-or-key")

    result = {}

    def run():
        cancellation.set_current_session(sid)
        try:
            a.call(Role.critic, "hi", timeout_s=30)
        except Exception as e:  # noqa: BLE001 — capture for assertion
            result["exc"] = e
        finally:
            cancellation.set_current_session(None)

    t = threading.Thread(target=run)
    t.start()
    try:
        assert entered.wait(5), "the request should be in flight"
        cancellation.request(sid)          # human hits 'Cancel run'
        t.join(timeout=5)
        assert not t.is_alive(), "cancel must interrupt the in-flight HTTP call"
        assert isinstance(result.get("exc"), SessionCancelled)
    finally:
        cancellation.clear(sid)


def test_productive_stream_can_run_longer_than_stall_window(monkeypatch):
    """Coding has no total wall clock: each real output chunk refreshes liveness."""
    import time

    class _StreamResponse:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_lines(self):
            yield 'data: {"model":"qwen/test","choices":[{"delta":{"content":"hello "}}]}'
            time.sleep(0.6)
            yield 'data: {"choices":[{"delta":{"content":"world"}}]}'
            time.sleep(0.6)
            yield "data: [DONE]"

    class _StreamingClient:
        def __init__(self, timeout=None):
            pass

        def stream(self, *args, **kwargs):
            return _StreamResponse()

        def close(self):
            pass

    monkeypatch.setattr(orm.httpx, "Client", _StreamingClient)
    cancellation.set_call_kind("coding")
    try:
        out = OpenRouterAdapter("qwen", "qwen/test", lambda: "sk-or-key").call(
            Role.code_generator, "write it", timeout_s=1)
    finally:
        cancellation.set_call_kind(None)
    assert out.content == "hello world"
    assert out.duration_ms >= 1100  # total exceeded 1s; no output gap did


def test_routine_stream_uses_total_wall_clock_deadline(monkeypatch):
    import time

    class _Response:
        status_code = 200

        def __enter__(self): return self
        def __exit__(self, *args): return False

        def iter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"one"}}]}'
            time.sleep(0.6)
            yield 'data: {"choices":[{"delta":{"content":"two"}}]}'
            time.sleep(0.6)
            yield "data: [DONE]"

    class _Client:
        def __init__(self, timeout=None): pass
        def stream(self, *args, **kwargs): return _Response()
        def close(self): pass

    monkeypatch.setattr(orm.httpx, "Client", _Client)
    cancellation.set_call_kind("routine")
    try:
        with pytest.raises(AgentError, match="timed out after 1s"):
            OpenRouterAdapter("qwen", "qwen/test", lambda: "sk-or-key").call(
                Role.researcher, "summarize", timeout_s=1)
    finally:
        cancellation.set_call_kind(None)


def test_silent_stream_is_closed_by_model_progress_watchdog(monkeypatch):
    import threading

    closed = threading.Event()

    class _StalledResponse:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def iter_lines(self):
            if closed.wait(5):
                raise orm.httpx.ReadError("closed by watchdog")
            return
            yield  # pragma: no cover - make this a generator

    class _StalledClient:
        def __init__(self, timeout=None):
            pass

        def stream(self, *args, **kwargs):
            return _StalledResponse()

        def close(self):
            closed.set()

    monkeypatch.setattr(orm.httpx, "Client", _StalledClient)
    adapter = OpenRouterAdapter("qwen", "qwen/test", lambda: "sk-or-key")
    cancellation.set_call_kind("coding")
    try:
        with pytest.raises(AgentError, match="stalled: no model output for 1s"):
            adapter.call(Role.code_generator, "write it", timeout_s=1)
    finally:
        cancellation.set_call_kind(None)
    assert closed.is_set()


# --- secret store -------------------------------------------------------------


def test_secret_store_roundtrip(tmp_path):
    s = SecretStore(tmp_path)
    assert s.get("openrouter") is None and not s.has("openrouter")
    s.set("openrouter", " sk-or-abcd1234 ")  # trimmed
    assert s.get("openrouter") == "sk-or-abcd1234"
    assert s.source("openrouter") == "stored"
    masked = SecretStore.mask(s.get("openrouter"))
    assert masked.startswith("sk-o") and masked.endswith("1234") and "abcd" not in masked
    s.clear("openrouter")
    assert not s.has("openrouter")


def test_secret_env_overrides_stored(tmp_path, monkeypatch):
    s = SecretStore(tmp_path)
    s.set("openrouter", "stored-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
    assert s.get("openrouter") == "env-key"
    assert s.source("openrouter") == "env"


# --- key endpoints ------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    from gangof8 import main as main_mod
    main_mod.service = GangOf8Service(data_dir=tmp_path)
    return TestClient(main_mod.app)


def test_key_endpoints_never_leak_the_key(client):
    assert client.get("/settings/api-keys/openrouter").json()["present"] is False
    r = client.put("/settings/api-keys/openrouter", json={"value": "sk-or-supersecret-9999"}).json()
    assert r["present"] is True
    assert "supersecret" not in str(r)           # only a mask is returned
    assert r["masked"].startswith("sk-o") and r["masked"].endswith("9999")
    client.delete("/settings/api-keys/openrouter")
    assert client.get("/settings/api-keys/openrouter").json()["present"] is False


# --- enable → register --------------------------------------------------------


def test_enabling_seat_registers_openrouter_adapter(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    svc = GangOf8Service(data_dir=tmp_path)
    svc.set_openrouter_key("sk-or-key")
    svc.update_settings({"backend": "cli", "openrouter_enabled": {"kimi": True}})
    assert "kimi" in svc.registry.names()
    assert isinstance(svc.registry._adapters["kimi"], OpenRouterAdapter)
    # an assigned-but-not-enabled seat is still registered (referenced in role map)
    svc.update_settings({"role_agents": {"researcher": "glm", "summarizer": "claude"}})
    assert isinstance(svc.registry._adapters["glm"], OpenRouterAdapter)


def test_model_slug_override_is_used(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    svc = GangOf8Service(data_dir=tmp_path)
    svc.set_openrouter_key("sk-or-key")
    svc.update_settings({
        "backend": "cli",
        "openrouter_enabled": {"deepseek": True},
        "openrouter_models": {"deepseek": "deepseek/deepseek-v5-ultra"},
    })
    assert svc.registry._adapters["deepseek"].model_slug == "deepseek/deepseek-v5-ultra"
    seat = next(s for s in svc.seats()["seats"] if s["name"] == "deepseek")
    assert seat["model_slug"] == "deepseek/deepseek-v5-ultra"
    assert seat["default_slug"] == "deepseek/deepseek-v4-pro"
    # clearing the override falls back to the built-in default
    svc.update_settings({"openrouter_models": {}})
    assert svc._openrouter_slug("deepseek") == "deepseek/deepseek-v4-pro"


def test_seat_unavailable_without_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    svc = GangOf8Service(data_dir=tmp_path)
    svc.update_settings({"openrouter_enabled": {"deepseek": True}})
    seat = next(s for s in svc.seats()["seats"] if s["name"] == "deepseek")
    assert seat["enabled"] is True and seat["available"] is False  # enabled but no key


# --- disabling local CLI seats → OpenRouter-only fallback ---------------------

_CLI = {"claude", "codex", "gemini"}


def test_disabling_cli_seat_falls_back_to_openrouter(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    svc = GangOf8Service(data_dir=tmp_path)
    svc.set_openrouter_key("sk-or-key")
    svc.update_settings({"backend": "cli",
                         "openrouter_enabled": {"kimi": True, "qwen": True},
                         "cli_enabled": {"claude": False}})
    # every role that was on claude is remapped onto an enabled OpenRouter seat
    assert "claude" not in svc.role_agents.values()
    assert svc.role_agents[Role.lead] in ("kimi", "qwen")
    # claude is no longer registered; the fallback seat is; and it left the panel
    assert "claude" not in svc.registry.names()
    assert "kimi" in svc.registry.names()
    assert "claude" not in svc.panel


def test_disabling_all_clis_runs_openrouter_only(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    svc = GangOf8Service(data_dir=tmp_path)
    svc.set_openrouter_key("sk-or-key")
    svc.update_settings({"backend": "cli",
                         "openrouter_enabled": {"deepseek": True, "glm": True},
                         "cli_enabled": {"claude": False, "codex": False, "gemini": False}})
    assert not (_CLI & set(svc.role_agents.values())), "no CLI seat left in the role map"
    assert not (_CLI & set(svc.registry.names())), "no CLI adapter registered"
    assert set(svc.role_agents.values()) <= {"deepseek", "glm"}


def test_disabling_cli_without_openrouter_is_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    svc = GangOf8Service(data_dir=tmp_path)
    # nothing to fall back to → the disable is ignored so the lead still exists
    svc.update_settings({"backend": "cli", "cli_enabled": {"claude": False}})
    assert svc.role_agents[Role.lead] == "claude"


def test_put_settings_endpoint_persists_cli_enabled(client):
    """The HTTP PUT path (not just update_settings) must carry cli_enabled/
    cli_models through — the SettingsPatch model was dropping them, so the
    dashboard's OpenRouter-only preset saved but the CLI seats stayed on."""
    client.put("/settings/api-keys/openrouter", json={"value": "sk-or-key"})
    r = client.put("/settings", json={
        "backend": "cli",
        "openrouter_enabled": {"kimi": True},
        "cli_models": {"claude": "opus"},
        "cli_enabled": {"claude": False},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["cli_enabled"]["claude"] is False      # persisted, not dropped
    assert body["cli_models"]["claude"] == "opus"
    cli = {s["name"]: s for s in client.get("/settings/seats").json()["seats"]
           if s["kind"] == "cli"}
    assert cli["claude"]["enabled"] is False            # seats endpoint reflects it


def test_seats_reports_cli_enabled_flag(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    svc = GangOf8Service(data_dir=tmp_path)
    svc.update_settings({"backend": "cli", "cli_enabled": {"codex": False}})
    cli = {s["name"]: s for s in svc.seats()["seats"] if s["kind"] == "cli"}
    assert cli["codex"]["enabled"] is False
    assert cli["claude"]["enabled"] is True and cli["gemini"]["enabled"] is True
