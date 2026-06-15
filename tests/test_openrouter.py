"""OpenRouter council seats: adapter, secret store, key endpoints, and the
enable→register wiring that lets a role be assigned to an OpenRouter model.
"""

import pytest
from fastapi.testclient import TestClient

from conclave_os.adapters import openrouter as orm
from conclave_os.adapters.openrouter import OpenRouterAdapter
from conclave_os.models import Role
from conclave_os.registry import AgentError
from conclave_os.secrets import SecretStore
from conclave_os.service import ConclaveService


class _Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code = status
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


# --- adapter ------------------------------------------------------------------


def test_adapter_posts_and_parses(monkeypatch):
    captured = {}

    def fake_post(url, headers, json, timeout):
        captured.update(url=url, headers=headers, payload=json, timeout=timeout)
        return _Resp(200, {
            "choices": [{"message": {"content": "hi from deepseek"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        })

    monkeypatch.setattr(orm.httpx, "post", fake_post)
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
    monkeypatch.setattr(orm.httpx, "post", lambda url, **k: _Resp(429, None, "rate limited"))
    a = OpenRouterAdapter("kimi", "moonshotai/kimi-k2.6", lambda: "sk-or-key")
    with pytest.raises(AgentError, match="HTTP 429"):
        a.call(Role.critic, "hi", timeout_s=10)


def test_adapter_empty_output_raises(monkeypatch):
    monkeypatch.setattr(orm.httpx, "post",
                        lambda url, **k: _Resp(200, {"choices": [{"message": {"content": "  "}}]}))
    a = OpenRouterAdapter("qwen", "qwen/qwen3.6-plus", lambda: "sk-or-key")
    with pytest.raises(AgentError, match="empty output"):
        a.call(Role.researcher, "hi", timeout_s=10)


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
    from conclave_os import main as main_mod
    main_mod.service = ConclaveService(data_dir=tmp_path)
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
    svc = ConclaveService(data_dir=tmp_path)
    svc.set_openrouter_key("sk-or-key")
    svc.update_settings({"backend": "cli", "openrouter_enabled": {"kimi": True}})
    assert "kimi" in svc.registry.names()
    assert isinstance(svc.registry._adapters["kimi"], OpenRouterAdapter)
    # an assigned-but-not-enabled seat is still registered (referenced in role map)
    svc.update_settings({"role_agents": {"researcher": "glm", "summarizer": "claude"}})
    assert isinstance(svc.registry._adapters["glm"], OpenRouterAdapter)


def test_seat_unavailable_without_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    svc = ConclaveService(data_dir=tmp_path)
    svc.update_settings({"openrouter_enabled": {"deepseek": True}})
    seat = next(s for s in svc.seats()["seats"] if s["name"] == "deepseek")
    assert seat["enabled"] is True and seat["available"] is False  # enabled but no key
