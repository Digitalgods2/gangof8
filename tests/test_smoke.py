"""Headless runtime smoke test — the gate that a blind judge vote can't provide.

A file is executed in a stubbed DOM; a throw on load is a failure. This is what
would have caught the Centipede that read as complete, won a unanimous 5-judge
vote, and showed a black screen because draw() touched an uninitialised array.
"""

import shutil

import pytest

from conclave_os import smoke

pytestmark = pytest.mark.skipif(shutil.which("node") is None,
                                reason="node not on PATH — runtime smoke test unavailable")

CLEAN = "<!doctype html><html><body><canvas id='c'></canvas><script>\n" \
        "const cv=document.getElementById('c'); const x=cv.getContext('2d');\n" \
        "let g=[]; for(let i=0;i<5;i++){g[i]=[]; for(let j=0;j<5;j++) g[i][j]=0;}\n" \
        "function draw(){ for(let i=0;i<5;i++) for(let j=0;j<5;j++){ if(g[i][j]) x.fillRect(i,j,1,1);} }\n" \
        "function loop(){ draw(); requestAnimationFrame(loop); } requestAnimationFrame(loop);\n" \
        "</script></body></html>"

# kimi's exact failure: draw() runs on frame 1 but the grid is empty until start
CRASH = "<!doctype html><html><body><script>\n" \
        "let grid=[];\n" \
        "function draw(){ for(let c=0;c<30;c++){ for(let r=0;r<30;r++){ let m=grid[c][r]; } } }\n" \
        "function loop(){ draw(); requestAnimationFrame(loop); } requestAnimationFrame(loop);\n" \
        "</script></body></html>"


def test_clean_web_file_runs():
    ran, testable, detail = smoke.smoke_source(CLEAN, ".html")
    assert ran is True and testable is True, detail


def test_load_crash_is_caught():
    ran, testable, detail = smoke.smoke_source(CRASH, ".html")
    assert ran is False and testable is True
    assert "read" in detail.lower() or "undefined" in detail.lower()


def test_non_web_file_is_not_blocked():
    ran, testable, _ = smoke.smoke_source("print('hi')", ".py")
    assert ran is True and testable is False  # can't test → never blocks


def test_html_without_script_is_not_blocked():
    ran, testable, _ = smoke.smoke_source("<html><body>static page</body></html>", ".html")
    assert ran is True and testable is False


def test_bare_js_file_runs():
    ran, testable, detail = smoke.smoke_source("var x = 1 + 1;", ".js")
    assert ran is True and testable is True, detail


def test_bare_js_crash_caught():
    ran, testable, detail = smoke.smoke_source("var a; a.b.c = 1;", ".js")
    assert ran is False and testable is True
