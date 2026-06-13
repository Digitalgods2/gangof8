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
