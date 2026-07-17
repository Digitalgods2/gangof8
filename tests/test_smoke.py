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

STANDARD_DOM_GLOBALS = "<!doctype html><html><body><script>\n" \
                       "visualViewport.addEventListener('resize',()=>{});\n" \
                       "const frag=document.createDocumentFragment();\n" \
                       "frag.querySelectorAll('[data-game]');\n" \
                       "document.dispatchEvent({type:'arcadeportal:state'});\n" \
                       "document.body.removeAttribute('hidden');\n" \
                       "document.body.hasAttribute('hidden');\n" \
                       "document.body.toggleAttribute('hidden');\n" \
                       "const button=document.createElement('button');\n" \
                       "button.innerHTML='<span class=game-name></span>';\n" \
                       "button.querySelector('.game-name').textContent='Frogger';\n" \
                       "window.ArcadePortal={register(){}};\n" \
                       "if(window.ArcadePortal) ArcadePortal.register('demo',class{});\n" \
                       "</script></body></html>"

KEY_HANDLER_CRASH = "<!doctype html><html><body><canvas></canvas><script>\n" \
                    "addEventListener('keydown', e => { if(e.code === 'Space') missingSound.fire(); });\n" \
                    "requestAnimationFrame(function tick(){ requestAnimationFrame(tick); });\n" \
                    "</script></body></html>"

POINTER_HANDLER_CRASH = "<!doctype html><html><body><canvas></canvas><script>\n" \
                        "addEventListener('pointerdown', () => missingPointer.fire());\n" \
                        "</script></body></html>"

TIMER_CRASH = "<!doctype html><html><body><script>\n" \
              "setInterval(() => missingTimer.tick(), 10);\n" \
              "</script></body></html>"

SAFE_CLOSEST = "<!doctype html><html><body><button>Sound</button><script>\n" \
               "addEventListener('click', e => { const button=e.target.closest('button'); });\n" \
               "</script></body></html>"


def test_bare_browser_globals_do_not_falsely_crash():
    ran, testable, detail, _dyn = smoke.smoke_source(BARE_GLOBALS, ".html")
    assert ran is True and testable is True, detail  # not "addEventListener is not defined"


def test_layout_observers_and_css_style_api_do_not_falsely_crash():
    ran, testable, detail, _dyn = smoke.smoke_source(BROWSER_LAYOUT_APIS, ".html")
    assert ran is True and testable is True, detail


def test_standard_dom_and_window_assigned_globals_do_not_falsely_crash():
    ran, testable, detail, _dyn = smoke.smoke_source(STANDARD_DOM_GLOBALS, ".html")
    assert ran is True and testable is True, detail


@pytest.mark.parametrize("source", [KEY_HANDLER_CRASH, POINTER_HANDLER_CRASH])
def test_input_handler_exception_blocks_smoke(source):
    ran, testable, detail, _dyn = smoke.smoke_source(source, ".html")
    assert ran is False and testable is True
    assert "missing" in detail.lower() or "defined" in detail.lower()


def test_timer_exception_blocks_smoke():
    ran, testable, detail, _dyn = smoke.smoke_source(TIMER_CRASH, ".html")
    assert ran is False and testable is True
    assert "missing" in detail.lower() or "defined" in detail.lower()


def test_realistic_closest_stub_does_not_falsely_reject_delegated_clicks():
    ran, testable, detail, _dyn = smoke.smoke_source(SAFE_CLOSEST, ".html")
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


def test_smoke_source_with_line_reports_the_throwing_line():
    """The error line matters when a caller (service._assembly_runtime_failure_target)
    needs to map a crash back to which concatenated file actually threw, as
    opposed to whichever file's inclusion merely triggered execution."""
    src = "function ok(){ return 1; }\nfunction boom(){\n  var x;\n  x.trim();\n}\nboom();"
    ran, testable, detail, _dyn, line = smoke.smoke_source_with_line(src, ".js")
    assert ran is False and testable is True, detail
    assert src.splitlines()[line - 1].strip() == "x.trim();"


def test_smoke_source_with_line_is_zero_on_clean_run():
    ran, testable, detail, _dyn, line = smoke.smoke_source_with_line("var x = 1 + 1;", ".js")
    assert ran is True and testable is True, detail
    assert line == 0


def test_smoke_source_backward_compatible_four_tuple_unaffected():
    """smoke_source's public signature/behavior must stay untouched — dozens
    of existing call sites unpack exactly 4 values."""
    ran, testable, detail, dyn = smoke.smoke_source("var a; a.b.c = 1;", ".js")
    assert ran is False and testable is True and dyn is None
