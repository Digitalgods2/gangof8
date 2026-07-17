from __future__ import annotations

from dataclasses import dataclass

import gangof8.browser_acceptance as ba


class FakeManager:
    def __init__(self, playwright):
        self.playwright = playwright

    def __enter__(self):
        return self.playwright

    def __exit__(self, exc_type, exc, traceback):
        return False


class FakeKeyboard:
    def __init__(self):
        self.events = []

    def down(self, key):
        self.events.append(("down", key))

    def up(self, key):
        self.events.append(("up", key))


class FakeMouse:
    def __init__(self):
        self.events = []

    def move(self, x, y):
        self.events.append(("move", x, y))

    def down(self):
        self.events.append(("down",))

    def up(self):
        self.events.append(("up",))

    def click(self, x, y):
        self.events.append(("click", x, y))


class FakeControl:
    def __init__(self):
        self.clicks = 0

    def is_visible(self):
        return True

    def click(self, **kwargs):
        self.clicks += 1


class FakeLocator:
    def __init__(self, controls=None, canvas=False):
        self.controls = controls or []
        self.canvas = canvas

    @property
    def first(self):
        return self

    def count(self):
        return len(self.controls)

    def nth(self, index):
        return self.controls[index]

    def is_visible(self):
        return self.canvas

    def bounding_box(self):
        return {"x": 10, "y": 20, "width": 200, "height": 100}


@dataclass
class FakeConsoleMessage:
    type: str
    text: str


@dataclass
class FakeRequest:
    url: str
    failure: str = ""


class FakePage:
    def __init__(self, emit_errors=False):
        self.handlers = {}
        self.keyboard = FakeKeyboard()
        self.mouse = FakeMouse()
        self.controls = [FakeControl(), FakeControl()]
        self.waits = []
        self.goto_args = None
        self.emit_errors = emit_errors

    def on(self, event, callback):
        self.handlers[event] = callback

    def goto(self, url, **kwargs):
        self.goto_args = (url, kwargs)
        if self.emit_errors:
            self.handlers["pageerror"](RuntimeError("boom from frame"))
            self.handlers["console"](FakeConsoleMessage("error", "bad control"))
            self.handlers["requestfailed"](
                FakeRequest("https://tracker.invalid/a.js", "blockedbyclient")
            )

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)

    def locator(self, selector):
        if selector == "canvas:visible":
            return FakeLocator(canvas=True)
        return FakeLocator(controls=self.controls)


class FakeRoute:
    def __init__(self):
        self.action = ""
        self.code = ""

    def abort(self, code):
        self.action = "abort"
        self.code = code

    def continue_(self):
        self.action = "continue"


class FakeContext:
    def __init__(self, page):
        self.page = page
        self.default_timeout = None
        self.route_callback = None
        self.page_callback = None
        self.closed = False

    def set_default_timeout(self, timeout):
        self.default_timeout = timeout

    def route(self, pattern, callback):
        assert pattern == "**/*"
        self.route_callback = callback

    def on(self, event, callback):
        assert event == "page"
        self.page_callback = callback

    def new_page(self):
        if self.page_callback:
            self.page_callback(self.page)
        return self.page

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, context):
        self.context = context
        self.context_options = None
        self.closed = False

    def new_context(self, **kwargs):
        self.context_options = kwargs
        return self.context

    def close(self):
        self.closed = True


class FakeLauncher:
    def __init__(self, browser=None, fail_names=()):
        self.browser = browser
        self.fail_names = set(fail_names)
        self.calls = []

    def launch(self, **kwargs):
        name = kwargs.get("channel", "chromium")
        self.calls.append(kwargs)
        if name in self.fail_names:
            raise RuntimeError(f"{name} missing")
        return self.browser


class FakePlaywright:
    def __init__(self, launcher):
        self.chromium = launcher


def _install_fake(monkeypatch, *, emit_errors=False, fail_names=()):
    page = FakePage(emit_errors=emit_errors)
    context = FakeContext(page)
    browser = FakeBrowser(context)
    launcher = FakeLauncher(browser, fail_names=fail_names)
    playwright = FakePlaywright(launcher)
    monkeypatch.setattr(ba, "_sync_playwright_factory", lambda: lambda: FakeManager(playwright))
    return page, context, browser, launcher


def test_interaction_detection_ignores_data_scripts_and_finds_real_controls():
    assert not ba.is_interactive_html("<article>Hello</article>")
    assert not ba.is_interactive_html('<script type="application/ld+json">{}</script>')
    assert ba.is_interactive_html("<canvas></canvas>")
    assert ba.is_interactive_html('<div role="button">Play</div>')
    assert ba.is_interactive_html("<div contenteditable>Type here</div>")
    assert ba.is_interactive_html('<section onclick="start()"></section>')
    assert ba.is_interactive_html("<script type=module src=game.js></script>")


def test_static_html_is_nonblocking_without_loading_browser(tmp_path, monkeypatch):
    path = tmp_path / "readme.html"
    path.write_text("<!doctype html><h1>Release notes</h1>", encoding="utf-8")
    monkeypatch.setattr(
        ba,
        "_sync_playwright_factory",
        lambda: (_ for _ in ()).throw(AssertionError("browser should not be inspected")),
    )

    result = ba.browser_acceptance(path)

    assert result.passed
    assert not result.interactive
    assert not result.testable
    assert "static HTML" in result.detail


def test_interactive_html_fails_closed_when_playwright_is_unavailable(tmp_path, monkeypatch):
    path = tmp_path / "app.html"
    path.write_text("<button>Run</button>", encoding="utf-8")
    monkeypatch.setattr(ba, "_sync_playwright_factory", lambda: None)

    result = ba.browser_acceptance(path)

    assert not result.passed
    assert result.interactive
    assert not result.testable
    assert "requires Playwright" in result.detail


def test_browser_gate_uses_isolated_context_fallback_and_real_input(tmp_path, monkeypatch):
    path = tmp_path / "game.html"
    path.write_text("<canvas></canvas><button>Start</button><script></script>", encoding="utf-8")
    page, context, browser, launcher = _install_fake(
        monkeypatch, fail_names={"chromium"}
    )

    result = ba.browser_acceptance(path, timeout_s=4, sustain_ms=777)

    assert result.passed
    assert result.testable
    assert result.browser == "msedge"
    assert browser.context_options == {"service_workers": "block"}
    assert [call.get("channel", "chromium") for call in launcher.calls] == [
        "chromium",
        "msedge",
    ]
    assert page.goto_args[0].startswith("file:")
    assert page.goto_args[1]["wait_until"] == "domcontentloaded"
    downs = [key for action, key in page.keyboard.events if action == "down"]
    ups = [key for action, key in page.keyboard.events if action == "up"]
    assert downs == ups
    assert "Space" in downs and "p" in downs and "ArrowUp" in downs
    assert all(control.clicks == 1 for control in page.controls)
    assert ("click", 110.0, 70.0) in page.mouse.events
    assert 777 in page.waits
    assert context.closed and browser.closed

    external = FakeRoute()
    context.route_callback(external, FakeRequest("https://example.test/game.js"))
    assert (external.action, external.code) == ("abort", "blockedbyclient")
    local = FakeRoute()
    context.route_callback(local, FakeRequest(path.resolve().as_uri()))
    assert local.action == "continue"


def test_browser_gate_captures_runtime_console_and_request_failures(tmp_path, monkeypatch):
    path = tmp_path / "game.html"
    path.write_text("<button>Start</button><script></script>", encoding="utf-8")
    _install_fake(monkeypatch, emit_errors=True)

    result = ba.browser_acceptance(path)

    assert not result.passed
    assert result.testable
    assert len(result.errors) == 3
    assert any("page error: boom from frame" in issue for issue in result.errors)
    assert any("console error: bad control" in issue for issue in result.errors)
    assert any("request failed: https://tracker.invalid" in issue for issue in result.errors)


def test_browser_gate_reports_all_browser_launch_failures(tmp_path, monkeypatch):
    path = tmp_path / "app.html"
    path.write_text("<script>window.app = true</script>", encoding="utf-8")
    _, _, _, launcher = _install_fake(
        monkeypatch, fail_names={"chromium", "msedge", "chrome"}
    )

    result = ba.browser_acceptance(path)

    assert not result.passed
    assert not result.testable
    assert len(result.errors) == 3
    assert [call.get("channel", "chromium") for call in launcher.calls] == [
        "chromium",
        "msedge",
        "chrome",
    ]


def test_style_contract_flags_wholesale_dom_css_selector_mismatch():
    class Page:
        def evaluate(self, _script):
            return {
                "used": [f".game-{index}" for index in range(10)],
                "covered": [".game-0", ".game-1"],
            }

    errors = ba._style_contract_errors(Page())

    assert errors
    assert "2/10 DOM class/id hooks" in errors[0]


def test_style_contract_accepts_page_styled_through_id_selectors():
    """Coverage counts id hooks alongside classes: a stylesheet that addresses
    every element via #id selectors is a real stylesheet. A classes-only
    version of this check rejected exactly such a page twice, even though it
    rendered fully styled — the check polices wholesale misses, not which
    selector idiom the author prefers."""
    class Page:
        def evaluate(self, _script):
            return {
                # markup carries decorative classes the sheet never names...
                "used": [f".decor-{index}" for index in range(10)]
                        + [f"#panel-{index}" for index in range(10)],
                # ...but styles every element through its id
                "covered": [f"#panel-{index}" for index in range(10)],
            }

    assert ba._style_contract_errors(Page()) == []
