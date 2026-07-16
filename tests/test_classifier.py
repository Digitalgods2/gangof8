"""Classifier: 'build an app' / file-producing tasks must classify as code.

Regression for a real run where "Build a tiny app ... main.py,
requirements.txt" classified as a plain question, so the implementer (the only
role that can propose a write_file/ARTIFACT) never joined the council and the
gangof8 concluded the task was impossible. Build/app verbs and filenames with
known extensions must mark the task as output-producing code.
"""

from gangof8.classifier import classify
from gangof8.models import TaskType


def test_build_an_app_is_code():
    cls = classify("Build a tiny app in a sandbox folder.")
    assert cls.task_type == TaskType.code
    assert cls.produces_output is True
    assert cls.tools_allowed is True


def test_code_tasks_are_led_with_specialists_on_standby():
    """Every task — code included — is driven by the lead; the specialist
    talents are present but inactive until the lead delegates."""
    from gangof8.roles import build_council
    from gangof8.models import Role

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
    from gangof8.roles import build_council
    from gangof8.models import Role

    cls = classify("create an app that shows a calendar selectable by year and month")
    assert cls.task_type == TaskType.code
    assert cls.greenfield is True

    council = build_council(cls, panel=["claude", "codex"])
    active = {m.role for m in council.members if m.active}
    assert Role.lead in active
    assert Role.summarizer in active
    assert Role.researcher not in active
    assert Role.critic not in active
    assert Role.red_team not in active
    assert Role.fact_validator not in active
    # the panel seats are convened as first-class, always-active members
    panelists = [m for m in council.members if m.role == Role.panelist]
    assert [m.agent for m in panelists] == ["claude", "codex"]
    assert all(m.active for m in panelists)


def test_recommendation_question_about_an_app_is_not_code():
    # live-smoke regression: mentioning "FastAPI app" must not turn a pure
    # recommendation question into a file-producing code task (which then
    # fails artifact verification and discards the council's real answer)
    cls = classify(
        "For a local single-user FastAPI app that stores session logs, is SQLite "
        "or JSONL the better default? Give a firm recommendation with your key reasons."
    )
    assert cls.task_type == TaskType.research
    assert cls.produces_output is False


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


def test_story_saved_to_txt_is_content_not_code():
    """Regression: a writing task whose ONLY code signal is the .txt it saves to
    was classified `code`, dropping the prose into the game/runtime judging path
    (a judge then penalised it for 'no on-screen rendering'). It must be content,
    yet still produce the file."""
    cls = classify(
        "You are a children's book author. Write the second story about Benny "
        "the dog and save it as a .txt file named Benny's Big Ride.txt in the "
        "output folder."
    )
    assert cls.task_type == TaskType.content
    assert cls.produces_output is True
    assert cls.tools_allowed is True


def test_write_essay_to_markdown_is_content():
    cls = classify("Draft an essay on tide pools and save it as essay.md")
    assert cls.task_type == TaskType.content


def test_story_brief_building_tension_stays_content():
    """"Build gentle tension" is story direction, not a request to build software."""
    task = (
        "Act as a children's book author. Read Benny's Splash.txt, write a sequel, "
        "build gentle age-appropriate tension, format it exactly like the first book, "
        "and save it as a .txt file in C:\\tmp"
    )
    cls = classify(task)
    assert cls.task_type == TaskType.content
    assert cls.match_source is True


def test_script_over_csv_stays_code():
    """A real code word ('script') keeps a file-producing task as code even
    though it names a data file — the content-override must not swallow it."""
    cls = classify("Write a python script to parse data.csv and print totals.")
    assert cls.task_type == TaskType.code


def test_attached_arcade_repair_is_code_not_action():
    """Regression: action-like method names inside the attached arcade source
    must not override the user's explicit Centipede/Asteroids repair
    request. The real run was routed as a generic action, bypassing the coding
    author and runtime/output gates."""
    task = (
        "using this code, fix the centipede and centipede game, they are both "
        "very poor clones of the originals, additionally add sound when "
        "accelerating the ship in asteroid, return a completed arcade html "
        "with all the improvements."
        "\n\nAttachments provided by the user:\n"
        "--- Attached text file: arcade.html ---\n"
        "<!doctype html><script>\n"
        "function teardown(node) { node.remove(); }\n"
        "function sendScore() { return fetch('/score', {method: 'POST'}); }\n"
        "</script>"
    )

    cls = classify(task)

    assert cls.task_type == TaskType.code
    assert cls.produces_output is True
    assert cls.tools_allowed is True
    assert cls.needs_design is True
    assert cls.risk.value == "none"
    assert "attached code artifact" in cls.rationale


def test_delete_attached_code_file_remains_an_action():
    """Attachment awareness must not turn a real destructive request into a
    code edit merely because the deleted file has an executable extension."""
    task = (
        "Delete the attached obsolete file."
        "\n\nAttachments provided by the user:\n"
        "--- Attached text file: arcade.html ---\n"
        "<!doctype html><script>console.log('old')</script>"
    )

    cls = classify(task)

    assert cls.task_type == TaskType.action
    assert cls.risk.value == "high"


# --- Fix 2: matched-set intent ------------------------------------------------


def test_matched_set_intent_detected():
    """Fix 2: 'match the first story ... so the two books feel like a matched set'
    marks the output as one that must MIRROR a source — structural fidelity is a
    hard requirement, so the judges/finisher weigh it (a plain-prose candidate
    that dropped the source's whole format won this task before)."""
    from gangof8.classifier import wants_matched_output
    task = ("Write story #2 about Benny. Match the first story's style exactly, "
            "so the two books feel like a matched set. Save it as a .txt file.")
    assert wants_matched_output(task) is True
    cls = classify(task)
    assert cls.match_source is True
    assert cls.task_type == TaskType.content   # still a writing task (Fix A)


def test_no_match_intent_on_plain_build():
    """A build with no 'match/matched set/same style' phrasing does not set the
    flag — greenfield judging is unchanged."""
    from gangof8.classifier import wants_matched_output
    assert wants_matched_output("Build a tic-tac-toe game in game.html") is False
    assert classify("Build a tic-tac-toe game in game.html").match_source is False
