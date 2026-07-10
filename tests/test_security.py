from __future__ import annotations

import pytest

from gangof8 import security


def test_localhost_is_accepted_without_remote_opt_in(monkeypatch):
    monkeypatch.delenv("GANGOF8_ALLOW_REMOTE", raising=False)
    security.validate_bind_host("127.0.0.1")
    security.validate_bind_host("::1")
    security.validate_bind_host("localhost")
    assert security.local_request_allowed("127.0.0.1") is True
    assert security.local_request_allowed("testclient") is True


def test_non_local_bind_requires_explicit_opt_in(monkeypatch):
    monkeypatch.delenv("GANGOF8_ALLOW_REMOTE", raising=False)
    with pytest.raises(ValueError, match="GANGOF8_ALLOW_REMOTE"):
        security.validate_bind_host("0.0.0.0")
    assert security.local_request_allowed("192.168.1.10") is False


def test_remote_opt_in_allows_network_requests_but_not_sensitive_actions(monkeypatch):
    monkeypatch.setenv("GANGOF8_ALLOW_REMOTE", "1")
    security.validate_bind_host("0.0.0.0")
    assert security.local_request_allowed("192.168.1.10") is True
    assert security.sensitive_local_action_allowed("192.168.1.10") is False
