"""Headless runtime smoke test — the gate that a blind judge vote can't provide.

A file is executed in a stubbed DOM; a throw on load is a failure. This is what
would have caught the Centipede that read as complete, won a unanimous 5-judge
vote, and showed a black screen because draw() touched an uninitialised array.
"""

import shutil

import pytest

from gangof8 import smoke

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

# a live game: the rectangle MOVES every frame → motion detected
DYNAMIC = "<!doctype html><html><body><canvas id='c'></canvas><script>\n" \
          "const x=document.getElementById('c').getContext('2d'); let t=0;\n" \
          "function loop(){ x.clearRect(0,0,300,300); x.fillRect(t%280,10,20,20); t+=5; requestAnimationFrame(loop); }\n" \
          "requestAnimationFrame(loop);\n</script></body></html>"

# a frozen game: it renders, but draws the SAME frame forever despite input
STATIC = "<!doctype html><html><body><canvas id='c'></canvas><script>\n" \
         "const x=document.getElementById('c').getContext('2d');\n" \
         "function loop(){ x.fillRect(10,10,20,20); requestAnimationFrame(loop); }\n" \
         "requestAnimationFrame(loop);\n</script></body></html>"


# addEventListener / getComputedStyle are valid BARE in a browser (they resolve
# to window.*); the harness must provide them or it falsely rejects good games —
# live, two of the best Space Invaders candidates were killed by exactly this.
BARE_GLOBALS = "<!doctype html><html><body><canvas id='c'></canvas><script>\n" \
               "addEventListener('keydown', function(e){});\n" \
               "const cs = getComputedStyle(document.body);\n" \
               "const x = document.getElementById('c').getContext('2d');\n" \
               "let t=0; function loop(){ x.fillRect(t%100,0,5,5); t++; requestAnimationFrame(loop); }\n" \
               "requestAnimationFrame(loop);\n</script></body></html>"

BROWSER_LAYOUT_APIS = "<!doctype html><html><body><canvas id='c'></canvas><script>\n" \
                      "const cv=document.getElementById('c');\n" \
                      "cv.style.setProperty('--scale','1');\n" \
                      "const ro=new ResizeObserver(()=>{}); ro.observe(cv); ro.disconnect();\n" \
                      "const io=new IntersectionObserver(()=>{}); io.observe(cv); io.disconnect();\n" \
                      "const mo=new MutationObserver(()=>{}); mo.observe(cv,{}); mo.disconnect();\n" \
                      "requestAnimationFrame(()=>cv.getContext('2d').fillRect(0,0,1,1));\n" \
                      "</script></body></html>"


def test_bare_browser_globals_do_not_falsely_crash():
    ran, testable, detail, _dyn = smoke.smoke_source(BARE_GLOBALS, ".html")
    assert ran is True and testable is True, detail  # not "addEventListener is not defined"


def test_layout_observers_and_css_style_api_do_not_falsely_crash():
    ran, testable, detail, _dyn = smoke.smoke_source(BROWSER_LAYOUT_APIS, ".html")
    assert ran is True and testable is True, detail


def test_clean_web_file_runs():
    ran, testable, detail, _dyn = smoke.smoke_source(CLEAN, ".html")
    assert ran is True and testable is True, detail


def test_load_crash_is_caught():
    ran, testable, detail, _dyn = smoke.smoke_source(CRASH, ".html")
    assert ran is False and testable is True
    assert "read" in detail.lower() or "undefined" in detail.lower()


def test_dynamic_game_is_detected():
    ran, testable, detail, dynamic = smoke.smoke_source(DYNAMIC, ".html")
    assert ran is True and testable is True and dynamic is True, detail


def test_static_frozen_screen_is_flagged():
    ran, testable, detail, dynamic = smoke.smoke_source(STATIC, ".html")
    assert ran is True and testable is True and dynamic is False, detail


def test_non_web_file_is_not_blocked():
    ran, testable, _, _dyn = smoke.smoke_source("print('hi')", ".py")
    assert ran is True and testable is False  # can't test → never blocks


def test_html_without_script_is_not_blocked():
    ran, testable, _, _dyn = smoke.smoke_source("<html><body>static page</body></html>", ".html")
    assert ran is True and testable is False


def test_bare_js_file_runs():
    ran, testable, detail, _dyn = smoke.smoke_source("var x = 1 + 1;", ".js")
    assert ran is True and testable is True, detail


def test_js_module_smoke_uses_declared_load_order_prelude():
    """A game module may legally depend on a prior core.js global."""
    module = "class Frogger extends Game {}\nwindow.Frogger = Frogger;"
    ran_alone, _testable, _detail, _dyn = smoke.smoke_source(module, ".js")
    assert ran_alone is False
    ran, testable, detail, _dyn = smoke.smoke_source(
        module, ".js", prelude="class Game {}\nwindow.Game = Game;")
    assert ran is True and testable is True, detail


def test_bare_js_crash_caught():
    ran, testable, detail, _dyn = smoke.smoke_source("var a; a.b.c = 1;", ".js")
    assert ran is False and testable is True
