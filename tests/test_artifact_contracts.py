from pathlib import Path

from gangof8 import artifacts


def test_parse_proposals_preserves_document_order():
    text = """\
ARTIFACT: index.html
<!doctype html><html><body>ok</body></html>
RUNTESTS: pytest -q
PROMOTE: index.html
"""
    actions = artifacts.parse_proposals("s_1", text)
    assert [a.kind for a in actions] == ["write_file", "run_tests", "promote"]
    assert actions[0].filename == "index.html"
    assert actions[1].args["command"] == "pytest -q"


def test_parse_edit_contract():
    text = "\n".join([
        "EDIT: app.py",
        "<" * 7 + " OLD",
        'print("old")',
        "=" * 7,
        'print("new")',
        ">" * 7 + " NEW",
        "",
    ])
    action = artifacts.parse_proposals("s_1", text)[0]
    assert action.kind == "edit_file"
    assert action.args["old"] == 'print("old")'
    assert action.args["new"] == 'print("new")'


def test_parse_frontier_fenced_edit_contract():
    text = """\
===== EDIT: arcade.html =====
OLD:
```js
    this.playerX = 0;
    this.playerBullets = [];
```
NEW:
```js
    this.playerX = 0;
    this.playerY = PLAYER_Y;
    this.playerBullets = [];
```
"""
    action = artifacts.parse_proposals("s_1", text)[0]
    assert action.kind == "edit_file"
    assert action.filename == "arcade.html"
    assert action.args["old"] == "    this.playerX = 0;\n    this.playerBullets = [];"
    assert "this.playerY = PLAYER_Y" in action.args["new"]


def test_parse_frontier_outer_fenced_edit_contract():
    text = """\
```js
===== EDIT: arcade.html =====
OLD:
    this.playerX = WIDTH / 2;
    this.playerBullets = [];
NEW:
    this.playerX = WIDTH / 2;
    this.playerY = PLAYER_Y;
    this.playerBullets = [];
```
"""
    action = artifacts.parse_proposals("s_1", text)[0]
    assert action.kind == "edit_file"
    assert action.filename == "arcade.html"
    assert action.args["old"].endswith("this.playerBullets = [];")
    assert "this.playerY = PLAYER_Y" in action.args["new"]


def test_parse_frontier_outer_fenced_file_edit_contract():
    text = """\
```
FILE: arcade.html
OLD:
    if (hitPlayer) {
      this.lives--;
    }
NEW:
    if (hitPlayer && this.playerFlash <= 0) {
      this.lives--;
    }
```
"""
    action = artifacts.parse_proposals("s_1", text)[0]
    assert action.kind == "edit_file"
    assert action.filename == "arcade.html"
    assert action.args["old"].startswith("    if (hitPlayer)")
    assert "playerFlash <= 0" in action.args["new"]


def test_parse_frontier_numbered_fenced_edit_with_blank_lines():
    """A real frontier release-engineer reply echoed this file's own prompt
    wording ("OLD/NEW EDIT block(s)") back as a literal numbered header —
    'OLD/NEW EDIT 1:', 'OLD/NEW EDIT 2:' — and put a blank line between the
    header/OLD/NEW labels and their fences for readability. The strict
    original pattern required the bare word 'EDIT:' with no blank-line
    padding anywhere, so a genuinely correct multi-edit repair (donkey-kong
    momentum + oil-drum ignition fixes) parsed to zero actions and the whole
    release verification was discarded as "no usable implementation repair"
    even though the fix was right there."""
    text = """\
DEFECT: `game.html` - no stored horizontal velocity.

OLD/NEW EDIT 1: `game.html`

OLD:
```js
p.x += dir * MOVE * dt;
```

NEW:
```js
p.vx = approach(p.vx, dir * MOVE, ACCEL * dt);
p.x += p.vx * dt;
```

OLD/NEW EDIT 2: `game.html`

OLD:
```js
oilLit = Math.random() < 0.28;
```

NEW:
```js
oilLit = true;
```

VERDICT: FAIL
"""
    actions = artifacts.parse_proposals("s_1", text)
    edits = [a for a in actions if a.kind == "edit_file"]
    assert len(edits) == 2
    assert all(a.filename == "game.html" for a in edits)
    assert edits[0].args["old"] == "p.x += dir * MOVE * dt;"
    assert "approach(p.vx" in edits[0].args["new"]
    assert edits[1].args["old"] == "oilLit = Math.random() < 0.28;"
    assert edits[1].args["new"] == "oilLit = true;"


def test_clean_artifact_body_keeps_single_html_document():
    raw = """Here is the file:
<!doctype html>
<html><body><script>const x = "</html>";</script></body></html>

Notes that must not land in the file.
"""
    assert artifacts.clean_artifact_body(raw, "index.html").endswith("</html>")
    assert "Notes that must not land" not in artifacts.clean_artifact_body(raw, "index.html")


def test_clean_artifact_body_strips_single_whole_file_fence():
    assert artifacts.clean_artifact_body("```python\nprint(1)\n```", "app.py") == "print(1)"


def test_clean_artifact_body_strips_a_leaked_leading_artifact_header():
    body = artifacts.clean_artifact_body("ARTIFACT: sequel.txt\nOnce upon a time.\n", "sequel.txt")
    assert body == "Once upon a time."


def test_docs_and_static_do_not_contain_common_mojibake():
    bad = ("â", "ð", "Ã", "Â", "\ufffd")
    paths = [Path("README.md"), Path("DESIGN.md"), Path("ARCHITECTURE.md")]
    paths += list(Path("gangof8/static").glob("*.html"))
    paths += list(Path("gangof8/static").glob("*.css"))
    paths += list(Path("gangof8/static").glob("*.js"))
    offenders = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        if any(mark in text for mark in bad):
            offenders.append(str(path))
    assert offenders == []


def test_python_comment_does_not_truncate_the_file():
    """A '# ' line is a COMMENT in Python, never a Markdown heading.

    Regression: the trailing-prose fallback cut .py artifacts at the first
    comment following any line ending in ')', ';', '}' or ']'. A live 54,000
    character generator was stored as 514 bytes, still parsed as valid Python,
    passed verification, and was promoted over the good delivered copy.
    """
    source = (
        "import sys\n\n"
        "def main():\n"
        '    print("hello")\n'
        "    sys.exit(0)\n\n"
        "# Configuration constants\n"
        "WIDTH = 100\n"
        "HEIGHT = 200\n\n"
        'if __name__ == "__main__":\n'
        "    main()\n"
    )
    body = artifacts.clean_artifact_body(source, "app.py")
    assert body == source.strip()
    assert "WIDTH = 100" in body
    assert body.rstrip().endswith("main()")


def test_python_artifact_keeps_every_byte_through_parse_proposals():
    source = (
        "import os\n\n"
        "def build():\n"
        "    os.makedirs('out', exist_ok=True)\n\n"
        "# Registries used by the second pass\n"
        "REGISTRY = {}\n"
    )
    text = f"ARTIFACT: gen.py\n{source}END_ARTIFACT\nPROMOTE: gen.py\n"
    write = artifacts.parse_proposals("s_1", text)[0]
    assert write.kind == "write_file"
    assert "REGISTRY = {}" in write.content


def test_markdown_release_notes_are_still_trimmed_from_python():
    """The fallback must keep working for its actual purpose."""
    source = "import os\n\ndef go():\n    os.getcwd()\n"
    body = artifacts.clean_artifact_body(
        source + "\n**Summary of changes**\nAdded a go() helper.\n", "app.py")
    assert "Summary of changes" not in body
    assert body.endswith("os.getcwd()")


def test_markdown_heading_still_trims_javascript():
    source = "const a = 1;\n\nfunction go() {\n  console.log(a);\n}\n"
    body = artifacts.clean_artifact_body(
        source + "\n### Implementation notes\nIt logs.\n", "app.js")
    assert "Implementation notes" not in body
    assert body.endswith("}")
