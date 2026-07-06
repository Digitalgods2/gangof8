"""The lead's two hats: a fast COORDINATION model on the serial critical path,
and a stronger PRODUCTION model (session.lead_work_model) for when the lead
itself authors/repairs/fixes code — so a smart-but-slow model does the heavy
lifting without stalling (or timing out) the round loop.

These cover the plumbing (per-call model override) and the adapter precedence
(override > role pin > seat default). The end-to-end split (chair decision on the
coordination model, chair production on the work model) lives in test_best_of_n.
"""

import pytest

from conclave_os.adapters.cli import CliAdapter
from conclave_os.models import Role
from conclave_os.registry import AdapterResult, AgentRegistry


class _Recorder:
    name = "rec"

    def __init__(self):
        self.seen = []

    def call(self, role, prompt, timeout_s, images=None, model_override=None):
        self.seen.append(model_override)
        return AdapterResult(content="ok", model=model_override or "seat-default")


class _OldDouble:
    """A registry double from before the override existed — no model_override kwarg."""
    name = "old"

    def call(self, role, prompt, timeout_s, images=None):
        return AdapterResult(content="ok")


def test_registry_forwards_model_override():
    reg = AgentRegistry()
    rec = _Recorder()
    reg.register(rec)
    out = reg.call("rec", Role.lead, "hi", 10, model_override="claude-opus-4-8")
    assert rec.seen[-1] == "claude-opus-4-8"
    assert out.model == "claude-opus-4-8"


def test_registry_omits_override_when_unset():
    # Passed only when set, so pre-existing adapter doubles keep working.
    reg = AgentRegistry()
    reg.register(_OldDouble())
    assert reg.call("old", Role.lead, "hi", 10).content == "ok"  # no override → no kwarg → fine


def test_cli_override_beats_role_pin(monkeypatch):
    # lead pinned to sonnet; a production call overrides to opus — override wins.
    monkeypatch.setattr(CliAdapter, "_run_claude", lambda self, prompt, ts: (self.model, self.model))
    a = CliAdapter("claude", model="claude-sonnet-5", role_models={"lead": "claude-sonnet-5"})
    assert a.call(Role.lead, "hi", 10, model_override="claude-opus-4-8").content == "claude-opus-4-8"
    # no override → the role pin (sonnet) stands
    assert a.call(Role.lead, "hi", 10).content == "claude-sonnet-5"
