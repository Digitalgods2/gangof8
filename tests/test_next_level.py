"""NEXT-LEVEL.md implementation: seat health, live feed, goal timeline,
now-line. The through-line: the machine already decides well; these make it
able to explain itself and route around dead seats instead of burning
attempts against them."""

import threading

from gangof8.logstore import LogStore
from gangof8.models import Goal, GoalMilestone, Role, Session, SessionStatus, Task
from gangof8.registry import AgentRegistry
from gangof8.seat_health import SeatHealth, classify_failure
from gangof8.service import GangOf8Service


def test_failure_classification_covers_the_observed_outages():
    # the real claude outage
    assert classify_failure(
        "claude CLI error: You've hit your monthly spend limit · raise it at "
        "claude.ai/settings/usage") == "quota_exhausted"
    # the real codex outage
    assert classify_failure(
        "codex CLI exited 1: ERROR: Selected model is at capacity") == "capacity"
    assert classify_failure("gemini CLI not found on PATH ('gemini')") == "offline"
    assert classify_failure("401 unauthorized") == "auth_expired"
    assert classify_failure("call timed out after 600s") == "timeout"
    assert classify_failure("something exploded") == "degraded"


def test_seat_health_state_machine_and_hard_unavailability():
    health = SeatHealth()
    assert health.state("claude") == "healthy"
    assert not health.is_unavailable("claude")

    health.record_failure("claude", "You've hit your monthly spend limit")
    assert health.state("claude") == "quota_exhausted"
    assert health.is_unavailable("claude")

    # capacity is transient: degraded but still schedulable
    health.record_failure("codex", "Selected model is at capacity")
    assert health.state("codex") == "capacity"
    assert not health.is_unavailable("codex")

    # a success clears the state
    health.record_success("claude")
    assert health.state("claude") == "healthy"
    assert not health.is_unavailable("claude")
    snap = health.snapshot()
    assert snap["claude"]["state"] == "healthy"
    assert snap["codex"]["failures"] == 1


def test_registry_feeds_seat_health_on_every_outcome():
    registry = AgentRegistry()
    registry.health = SeatHealth()

    class _DeadSeat:
        name = "claude"

        def call(self, role, prompt, timeout_s, images=None):
            raise RuntimeError("You've hit your monthly spend limit")

    class _LiveSeat:
        name = "codex"

        def call(self, role, prompt, timeout_s, images=None):
            from gangof8.registry import AdapterResult
            return AdapterResult(content="ok", duration_ms=1)

    registry.register(_DeadSeat())
    registry.register(_LiveSeat())
    try:
        registry.call("claude", Role.panelist, "hi", 5)
    except RuntimeError:
        pass
    assert registry.health.state("claude") == "quota_exhausted"
    registry.call("codex", Role.panelist, "hi", 5)
    assert registry.health.state("codex") == "healthy"


def test_pending_packages_route_around_a_quota_capped_owner(tmp_path):
    """A hard-unavailable owner fails every attempt by definition; scheduling
    must transfer the package to a healthy frontier seat BEFORE opening a
    session — the codex takeover of the Asteroids build happened by helper
    luck; this makes it deliberate policy."""
    service = GangOf8Service(data_dir=tmp_path / "data")
    service.panel = ["claude", "codex"]
    service.seat_health.record_failure(
        "claude", "You've hit your monthly spend limit")
    goal = Goal(
        text="build", status="running", collaboration_mode="build_team",
        delivery_mode="final_batch",
        milestones=[GoalMilestone(
            index=0, package_id="wp_1", owner="claude", title="game",
            task_text="author", status="pending", contract_declared=True,
            requires_delivery=True, required_files=["game.html"],
            release_files=["game.html"],
        )],
    )
    service.goals.save(goal)

    service._route_around_unavailable_owners(goal)

    assert goal.milestones[0].owner == "codex"
    persisted = service.goals.get(goal.goal_id)
    assert persisted.milestones[0].owner == "codex"


def test_goal_error_names_the_seat_outage_not_the_symptom():
    session = Session(
        session_id="s_outage",
        unresolved=[
            "panel seat 'claude' dropped this round: claude CLI error: "
            "You've hit your monthly spend limit · raise it at claude.ai"
        ],
        task=Task(task_id="t", session_id="s_outage", text="build"),
    )
    text = GangOf8Service._session_seat_outage(session)
    assert "claude" in text
    assert "quota exhausted" in text
    assert "spend limit" in text
    # a normal failure is not misreported as an outage
    session.unresolved = ["panel seat 'claude' dropped this round: model wrote prose"]
    assert GangOf8Service._session_seat_outage(session) == ""


def test_logstore_feed_publishes_events_with_cursors(tmp_path):
    store = LogStore(tmp_path)
    base = store.feed_cursor
    store.log_event("s_1", "goal_created", {"goal_id": "g_x"})
    store.log_event("s_1", "goal_milestone_started", {"milestone": 1})

    fresh = store.feed_since(base)
    assert [e["event"] for e in fresh] == ["goal_created", "goal_milestone_started"]
    assert fresh[0]["session_id"] == "s_1"
    assert fresh[1]["seq"] > fresh[0]["seq"]
    # cursor semantics: nothing new after the last seq
    assert store.feed_since(fresh[-1]["seq"]) == []
    # feed_wait returns immediately when data is already fresh
    assert store.feed_wait(base, timeout_s=0.1)


def test_logstore_feed_wait_wakes_on_new_events(tmp_path):
    store = LogStore(tmp_path)
    base = store.feed_cursor
    got: list = []

    def waiter():
        got.extend(store.feed_wait(base, timeout_s=5.0))

    t = threading.Thread(target=waiter)
    t.start()
    store.log_event("s_2", "goal_completed", {"goal_id": "g_y"})
    t.join(timeout=5)
    assert [e["event"] for e in got] == ["goal_completed"]


def test_goal_timeline_merges_sessions_and_summarizes_attempts(tmp_path):
    from gangof8.models import Contribution
    service = GangOf8Service(data_dir=tmp_path / "data")
    goal = Goal(
        text="build", status="completed", release_status="released",
        collaboration_mode="build_team", delivery_mode="final_batch",
        model_calls_used=6, model_calls_by_seat={"claude": 4, "codex": 2},
        milestones=[GoalMilestone(
            index=0, package_id="wp_1", owner="codex", title="game",
            task_text="author", status="done", session_id="s_tl_b",
            contract_declared=True, requires_delivery=True,
            required_files=["game.html"], release_files=["game.html"],
            invalidated_session_ids=["s_tl_a"],
        )],
    )
    service.goals.save(goal)
    service.store.save_session(Session(
        session_id="s_tl_a", status=SessionStatus.failed, goal_id=goal.goal_id,
        agent_call_attempts=3,
        contributions=[Contribution(round=0, role=Role.panelist, agent="claude", content="x")],
        unresolved=["panel seat 'claude' dropped this round: monthly spend limit"],
        task=Task(task_id="t1", session_id="s_tl_a", text="build"),
    ))
    service.store.save_session(Session(
        session_id="s_tl_b", status=SessionStatus.done, goal_id=goal.goal_id,
        agent_call_attempts=2,
        contributions=[
            Contribution(round=0, role=Role.panelist, agent="codex", content="y"),
            Contribution(round=0, role=Role.panelist, agent="codex", content="z"),
        ],
        task=Task(task_id="t2", session_id="s_tl_b", text="build"),
    ))
    service.store.log_event("s_tl_a", "goal_milestone_started", {"milestone": 1})
    service.store.log_event("s_tl_b", "goal_milestone_done", {"milestone": 1})
    service.store.log_event("-", "goal_completed", {"goal_id": goal.goal_id})

    out = service.goal_timeline(goal.goal_id)

    events = [e["event"] for e in out["events"]]
    assert "goal_milestone_started" in events
    assert "goal_milestone_done" in events
    assert "goal_completed" in events
    summary = out["summary"]
    assert summary["calls_by_seat"] == {"claude": 4, "codex": 2}
    assert summary["attempts"]["total"] == 5
    assert summary["attempts"]["completed"] == 3
    assert summary["attempts"]["seat_outage"] == 1
    assert summary["packages"][0]["invalidated_attempts"] == 1


def test_goal_now_line_speaks_plainly():
    goal = Goal(text="build", status="running", collaboration_mode="build_team",
                delivery_mode="final_batch",
                milestones=[GoalMilestone(
                    index=0, package_id="wp_1", owner="claude", title="game",
                    task_text="author", status="running", session_id="s_now",
                    contract_declared=True, requires_delivery=True,
                    required_files=["game.html"],
                )])
    related = [{
        "session_id": "s_now",
        "active_agent_calls": [{"agent": "claude", "progress_chars": 12345}],
    }]
    line = GangOf8Service._goal_now_line(goal, related)
    assert "claude authoring game.html" in line
    assert "12,345 chars" in line

    goal.status = "paused"
    goal.last_error = "goal call budget reached: 40 calls; pausing for cost review"
    assert GangOf8Service._goal_now_line(goal, []).startswith(
        "paused — goal call budget reached")

    goal.status = "running"
    goal.last_error = ""
    goal.milestones[0].status = "done"
    assert GangOf8Service._goal_now_line(goal, []) == "verifying the final release"
