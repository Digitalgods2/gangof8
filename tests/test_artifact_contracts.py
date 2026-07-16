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
