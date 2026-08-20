"""Intent pass — what the task MEANS, decided by a model instead of by regex.

``classifier.py`` opens with "rule-based (Phase 0). LLM-assist can be added
later behind the same classify() signature." This module is that LLM-assist,
and it exists because the rule-based layer cannot read English:

    "research heavily the works of Auguste Escoffier and compile a pdf of his
     most famous and popular recipes ... index the book, make it searchable"

matched the ``CODE_WORDS`` entry ``compile``, was typed as software work, and
delivered a 49KB PDF *generator* that nothing ever ran. Five capable models were
registered and none of them was ever asked what the user wanted. Adding more
keywords does not fix that class of failure — every new phrasing needs a new
rule, and the rules cannot tell "compile a pdf" from "compile the parser".

Two outputs, deliberately separate:

1. A REVISION of the rule-based classification (what is being produced, in what
   format, is it software work). Merged in ``classifier.with_intent`` so the
   derived flags stay consistent.
2. An AMBIGUITY report. When the model is unsure AND can name a concrete
   either/or that changes what gets built, the coordinator asks the user one
   question BEFORE spending the run — instead of guessing, executing the guess
   for thirty minutes, and being wrong at the end.

Fail-open by design: an unparseable or failed intent call leaves the rule-based
classification exactly as it was. This layer can only ever improve the guess or
ask about it; it can never block a run on its own.
"""

from __future__ import annotations

import json
import re
from typing import Optional

from pydantic import BaseModel

from . import config
from .models import Classification, TaskType

# Formats the council cannot type into an ARTIFACT block. Mirrors the
# classifier's alias table so a model naming "docx" and a filename ending
# ".docx" normalize to the same requirement.
_KNOWN_FORMATS = {
    "pdf", "docx", "odt", "rtf", "xlsx", "ods", "pptx", "odp", "epub", "mobi",
    "zip", "tar", "gz", "7z", "rar", "png", "jpg", "gif", "bmp", "tiff",
    "webp", "ico", "mp3", "wav", "flac", "ogg", "m4a", "mp4", "mov", "avi",
    "webm", "mkv",
}
_TASK_TYPES = {t.value for t in TaskType}


class Intent(BaseModel):
    """One model's reading of what the user asked for."""

    deliverable: str = ""          # one line: what the user ends up holding
    task_type: str = ""            # question|research|design|code|content|action
    produces_output: bool = True   # does success require a file on disk
    deliverable_formats: list[str] = []   # non-text formats that MUST be produced
    confidence: float = 1.0        # 0..1, the model's own certainty
    ambiguity: str = ""            # the fork, in one sentence; "" when clear
    options: list[str] = []        # the concrete readings, when ambiguous
    notes: str = ""

    def is_ambiguous(self) -> bool:
        """A fork worth a question: stated, concrete, and genuinely two-sided.

        Both halves are required. A model that says "unsure" without naming the
        readings has given the user nothing to answer, and a model that lists
        options while confident is describing scope, not doubt.
        """
        return bool(
            self.ambiguity.strip()
            and len([o for o in self.options if o.strip()]) >= 2
            and self.confidence < config.INTENT_CLARIFY_CONFIDENCE
        )


def intent_prompt(task_text: str, rule_based: Classification) -> str:
    """Ask for the reading, not for the work.

    The rule-based guess is shown so the model can CORRECT it — that framing
    catches far more than an open-ended "what is this?", because the common
    failure is a specific wrong reading (a document request typed as code), not
    an absence of any reading.
    """
    return (
        "You are the INTAKE ANALYST for a task coordinator. You do not do the "
        "work and you do not plan it. You decide, in one pass, what the user is "
        "actually asking for.\n\n"
        f"USER'S REQUEST:\n{task_text}\n\n"
        "A keyword matcher already guessed the following. It reads word lists, "
        "not sentences, so it is often wrong in exactly one way: it mistakes a "
        "document/media request for software work when the request happens to "
        "contain a word like 'compile', 'build', 'index' or 'package'.\n"
        f"  guessed task_type      : {rule_based.task_type.value}\n"
        f"  guessed produces_output: {rule_based.produces_output}\n"
        f"  guessed formats        : {rule_based.deliverable_formats or 'none'}\n\n"
        "Answer with ONE JSON object and nothing else — no prose, no code "
        "fences:\n"
        "{\n"
        '  "deliverable": "<one line: the thing the user ends up holding>",\n'
        '  "task_type": "question|research|design|code|content|action",\n'
        '  "produces_output": true|false,\n'
        '  "deliverable_formats": ["pdf"],\n'
        '  "confidence": 0.0-1.0,\n'
        '  "ambiguity": "<one sentence, or empty string if the request is clear>",\n'
        '  "options": ["<reading A>", "<reading B>"],\n'
        '  "notes": "<optional, one line>"\n'
        "}\n\n"
        "Rules:\n"
        "- task_type is 'code' ONLY when the SOURCE CODE is what the user wants "
        "to end up with. A request for a document, book, image, spreadsheet or "
        "archive is NOT code, even when producing it obviously requires a "
        "program to be written along the way.\n"
        "- deliverable_formats lists ONLY non-text formats the finished artifact "
        "must be in (pdf, docx, xlsx, pptx, epub, zip, png, mp3, mp4, ...). "
        "Leave it empty for plain text, source code, markdown or an answer. "
        "Never list a format the user only mentioned as an INPUT they supplied.\n"
        "- confidence is your own certainty about the reading, not about your "
        "ability to do the work.\n"
        "- Set 'ambiguity' and give 2-4 'options' ONLY when the request "
        "genuinely reads two ways AND the two readings produce DIFFERENT "
        "deliverables. Do not report ambiguity for missing detail you can "
        "reasonably assume (styling, length, tone, filename). A confident "
        "reading with unstated details is NOT ambiguous.\n"
    )


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def parse_intent(raw: str) -> Optional[Intent]:
    """Parse the analyst's reply, or None when it did not answer in the format.

    Tolerates a fenced block or surrounding chatter (the widest object in the
    reply wins) but never guesses at missing fields — an unparseable reply must
    leave the rule-based classification untouched rather than half-applied.
    """
    text = (raw or "").strip()
    if not text:
        return None
    match = _JSON_BLOCK.search(text)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    def _str(key: str) -> str:
        value = data.get(key)
        return value.strip() if isinstance(value, str) else ""

    formats: list[str] = []
    raw_formats = data.get("deliverable_formats")
    if isinstance(raw_formats, list):
        for item in raw_formats:
            if not isinstance(item, str):
                continue
            fmt = item.strip().lower().lstrip(".")
            if fmt in _KNOWN_FORMATS and fmt not in formats:
                formats.append(fmt)

    options: list[str] = []
    raw_options = data.get("options")
    if isinstance(raw_options, list):
        options = [o.strip() for o in raw_options if isinstance(o, str) and o.strip()]

    try:
        confidence = float(data.get("confidence", 1.0))
    except (TypeError, ValueError):
        confidence = 1.0
    confidence = min(1.0, max(0.0, confidence))

    task_type = _str("task_type").lower()
    if task_type not in _TASK_TYPES:
        task_type = ""

    produces = data.get("produces_output")

    return Intent(
        deliverable=_str("deliverable")[:400],
        task_type=task_type,
        produces_output=bool(produces) if isinstance(produces, bool) else True,
        deliverable_formats=formats,
        confidence=confidence,
        ambiguity=_str("ambiguity")[:400],
        options=options[:4],
        notes=_str("notes")[:400],
    )


def clarifying_question(intent: Intent) -> str:
    """The one question, phrased so a one-word answer resolves it."""
    options = [o for o in intent.options if o.strip()][:4]
    lines = "\n".join(f"  {i + 1}. {o}" for i, o in enumerate(options))
    return (
        f"Before I spend a run on this — {intent.ambiguity.strip()}\n\n"
        f"{lines}\n\n"
        "Reply with the number, or describe what you want instead."
    )


def selected_option(intent: Intent, answer: str) -> str:
    """Resolve the user's reply to one option. A bare number picks by index;
    anything else is passed through as the user's own wording."""
    reply = (answer or "").strip()
    options = [o for o in intent.options if o.strip()]
    match = re.fullmatch(r"(\d+)\.?", reply)
    if match:
        index = int(match.group(1)) - 1
        if 0 <= index < len(options):
            return options[index]
    return reply
