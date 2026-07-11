"""Council-health summary (#5) and the run timeline (#3)."""

from gangof8.models import SessionStatus
from gangof8.reporting import council_health, format_timeline
from gangof8.service import GangOf8Service


# --- council health -----------------------------------------------------------


def test_council_health_parses_degradation():
    h = council_health([
        "researcher seat (codex) dropped: codex CLI timed out after 120s",
        "summarizer 'gemini' failed (timeout); recomposed with 'codex'",
        "stopped refining after 6 rounds without critic acceptance",
        "an unrelated note about something",
    ])
    assert h["degraded"] is True
    assert h["dropped"][0] == {
        "role": "researcher", "agent": "codex", "error": "codex CLI timed out after 120s"}
    assert h["substitutions"][0] == {"failed": "gemini", "replaced_by": "codex"}
    assert any("stopped refining" in n for n in h["notes"])
    assert all("unrelated" not in n for n in h["notes"])  # noise excluded


def test_council_health_clean_run():
    h = council_health([])
    assert h["degraded"] is False
    assert h["dropped"] == [] and h["substitutions"] == [] and h["notes"] == []


# --- timeline -----------------------------------------------------------------


def test_format_timeline_labels_and_details():
    rows = format_timeline([
        {"ts": "2026-06-15T10:00:00Z", "event": "task_received", "payload": {}},
        {"ts": "2026-06-15T10:00:01Z", "event": "classified",
         "payload": {"task_type": "question", "complexity": "standard", "risk": "none"}},
        {"ts": "2026-06-15T10:00:02Z", "event": "seat_dropped",
         "payload": {"role": "researcher", "agent": "codex", "error": "timed out"}},
        {"ts": "2026-06-15T10:00:03Z", "event": "final_composed", "payload": {}},
    ])
    assert rows[0]["label"] == "Task received"
    assert "question" in rows[1]["detail"]
    assert rows[2]["icon"] == "⚠️" and "codex" in rows[2]["detail"]
    assert rows[3]["label"] == "Final answer composed"


def test_unknown_event_gets_a_friendly_label():
    rows = format_timeline([{"ts": "", "event": "some_new_event", "payload": {}}])
    assert rows[0]["label"] == "Some New Event"


def test_timeline_makes_integration_choice_explicit():
    rows = format_timeline([
        {"ts": "", "event": "integration_decided", "payload": {"decision": "adopted"}},
        {"ts": "", "event": "integration_decided", "payload": {"decision": "kept_winner"}},
    ])
    assert rows[0]["label"] == "Integration decision"
    assert rows[0]["detail"] == "used the integrated candidate"
    assert rows[1]["detail"] == "kept the voted winner"


# --- end to end via the service ----------------------------------------------


def test_timeline_endpoint_data_from_a_real_run(tmp_path):
    svc = GangOf8Service(data_dir=tmp_path)
    session = svc.run("What is SQLite?", source="test")
    assert session.status == SessionStatus.done
    tl = svc.timeline(session.session_id)
    events = {e["event"] for e in tl["events"]}
    assert "task_received" in events
    assert "classified" in events
    assert "final_composed" in events
    # every row carries an icon + label
    assert all(e.get("icon") and e.get("label") for e in tl["events"])
