"""Classifier: 'build an app' / file-producing tasks must classify as code.

Regression for a real run where "Build a tiny app ... main.py,
requirements.txt" classified as a plain question, so the implementer (the only
role that can propose a write_file/ARTIFACT) never joined the council and the
conclave concluded the task was impossible. Build/app verbs and filenames with
known extensions must mark the task as output-producing code.
"""

from conclave_os.classifier import classify
from conclave_os.models import TaskType


def test_build_an_app_is_code():
    cls = classify("Build a tiny app in a sandbox folder.")
    assert cls.task_type == TaskType.code
    assert cls.produces_output is True
    assert cls.tools_allowed is True


def test_code_tasks_are_led_with_specialists_on_standby():
    """Every task — code included — is driven by the lead; the specialist
    talents are present but inactive until the lead delegates."""
    from conclave_os.roles import build_council
    from conclave_os.models import Role

    cls = classify("Edit main.py to add a logging line")
    assert cls.task_type == TaskType.code
    assert cls.needs_facts is True
    council = build_council(cls)
    assert council.is_active(Role.lead)
    assert council.is_active(Role.summarizer)
    # specialists stand by (reachable via delegation), not auto-convened
    assert not council.is_active(Role.researcher)
    assert not council.is_active(Role.critic)


def test_greenfield_build_is_led_directly():
    from conclave_os.config import budgets_for
    from conclave_os.roles import build_council, plan_rounds
    from conclave_os.models import Complexity, Role

    cls = classify("create an app that shows a calendar selectable by year and month")
    assert cls.task_type == TaskType.code
    assert cls.greenfield is True

    council = build_council(cls)
    active = {m.role for m in council.members if m.active}
    assert Role.lead in active
    assert Role.summarizer in active
    assert Role.researcher not in active
    assert Role.critic not in active
    assert Role.red_team not in active
    assert Role.fact_validator not in active

    plan = plan_rounds(cls, council, budgets_for(Complexity.standard))
    assert len(plan) == 1
    assert plan[0].agents == [Role.lead]


def test_fastapi_app_with_filenames_is_code():
    cls = classify(
        "Create a simple FastAPI hello-world app with main.py, README.md, "
        "requirements.txt, and one test file."
    )
    assert cls.task_type == TaskType.code
    assert cls.produces_output is True


def test_bare_filename_signals_code():
    cls = classify("Add a logging line to main.py")
    assert cls.task_type == TaskType.code
    assert "filename artifact" in cls.rationale


def test_web_build_requests_are_code():
    """A 'calendar webpage' / 'landing page website' is a file-producing build,
    not a plain question — otherwise the lead is never asked to emit a file."""
    for t in ("make me a calendar webpage",
              "build a landing page website",
              "create a single-page html calendar"):
        c = classify(t)
        assert c.task_type == TaskType.code, t
        assert c.produces_output is True, t


def test_plain_question_stays_question():
    cls = classify("What is the capital of France?")
    assert cls.task_type == TaskType.question
    assert cls.produces_output is False


def test_compare_recommend_question_unchanged():
    # the existing mock-loop fixture task must stay a question
    cls = classify(
        "Compare SQLite vs. plain JSON files for storing session logs in a "
        "local service, and recommend one."
    )
    assert cls.task_type == TaskType.question
    assert cls.tools_allowed is False


def test_write_content_still_content():
    cls = classify("Write a short blog article about home baking.")
    assert cls.task_type == TaskType.content
    assert cls.produces_output is True
