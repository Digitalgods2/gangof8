"""Real-browser acceptance for interactive HTML artifacts.

The Node smoke harness is useful for early crash detection, but it cannot
faithfully exercise browser event delivery, DOM hit testing, or linked scripts.
This optional Playwright gate runs only for HTML that appears interactive.  An
interactive artifact fails closed when no supported browser can be launched;
plain, static HTML remains nonblocking.
"""

from __future__ import annotations

import re
import json
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable


_EXECUTABLE_SCRIPT_TYPES = {
    "",
    "application/ecmascript",
    "application/javascript",
    "module",
    "text/ecmascript",
    "text/javascript",
}
_INTERACTIVE_TAGS = {
    "button",
    "canvas",
    "details",
    "form",
    "input",
    "select",
    "textarea",
}
_INTERACTIVE_ROLES = {
    "button",
    "checkbox",
    "combobox",
    "listbox",
    "menuitem",
    "option",
    "radio",
    "slider",
    "spinbutton",
    "switch",
    "tab",
    "textbox",
}
_EVENT_ATTRIBUTE = re.compile(r"^on[a-z]+$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class BrowserAcceptanceResult:
    """Outcome of the browser gate.

    ``passed`` is the only field an integration gate needs to branch on.
    ``interactive`` and ``testable`` make skips and infrastructure failures
    explicit in reports.
    """

    passed: bool
    interactive: bool
    testable: bool
    detail: str
    errors: tuple[str, ...] = ()
    browser: str = ""


class _InteractionDetector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.interactive = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        values = {str(name).lower(): (value or "") for name, value in attrs}
        if tag == "script":
            script_type = values.get("type", "").strip().lower().split(";", 1)[0]
            if script_type in _EXECUTABLE_SCRIPT_TYPES:
                self.interactive = True
                return
        if tag in _INTERACTIVE_TAGS:
            self.interactive = True
            return
        if "contenteditable" in values and values["contenteditable"].lower() != "false":
            self.interactive = True
            return
        if values.get("role", "").lower() in _INTERACTIVE_ROLES:
            self.interactive = True
            return
        if any(_EVENT_ATTRIBUTE.match(name) for name in values):
            self.interactive = True


def is_interactive_html(text: str) -> bool:
    """Return whether HTML contains executable or user-interactive features."""
    detector = _InteractionDetector()
    try:
        detector.feed(text or "")
        detector.close()
    except (ValueError, TypeError):
        # Malformed HTML with obvious script/event syntax still deserves the
        # fail-closed browser path instead of being mistaken for static prose.
        low = (text or "").lower()
        return "<script" in low or bool(re.search(r"\son[a-z]+\s*=", low))
    return detector.interactive


def _sync_playwright_factory() -> Callable[[], Any] | None:
    try:
        from playwright.sync_api import sync_playwright
    except (ImportError, ModuleNotFoundError):
        return None
    return sync_playwright


def _member_text(value: Any, name: str) -> str:
    member = getattr(value, name, "")
    try:
        member = member() if callable(member) else member
    except Exception:
        member = ""
    return str(member or "")


def _short_error(kind: str, detail: str) -> str:
    one_line = " ".join(str(detail or "unknown failure").split())
    return f"{kind}: {one_line[:300]}"


def _launch_browser(playwright: Any, timeout_ms: int) -> tuple[Any | None, str, list[str]]:
    chromium = getattr(playwright, "chromium", None)
    if chromium is None:
        return None, "", ["Playwright Chromium launcher is unavailable"]

    failures: list[str] = []
    candidates = (
        ("chromium", {}),
        ("msedge", {"channel": "msedge"}),
        ("chrome", {"channel": "chrome"}),
    )
    for name, extra in candidates:
        try:
            browser = chromium.launch(headless=True, timeout=timeout_ms, **extra)
            return browser, name, failures
        except Exception as exc:
            failures.append(_short_error(name, str(exc)))
    return None, "", failures


def _attach_error_capture(page: Any, errors: list[str], attached: set[int]) -> None:
    marker = id(page)
    if marker in attached:
        return
    attached.add(marker)

    def on_page_error(exc: Any) -> None:
        errors.append(_short_error("page error", str(exc)))

    def on_console(message: Any) -> None:
        if _member_text(message, "type").lower() == "error":
            errors.append(_short_error("console error", _member_text(message, "text")))

    def on_request_failed(request: Any) -> None:
        url = _member_text(request, "url")
        failure = _member_text(request, "failure")
        errors.append(_short_error("request failed", f"{url} {failure}".strip()))

    page.on("pageerror", on_page_error)
    page.on("console", on_console)
    page.on("requestfailed", on_request_failed)
    page.on("dialog", lambda dialog: dialog.dismiss())


def _block_external(route: Any, request: Any) -> None:
    url = _member_text(request, "url").lower()
    if url.startswith(("http://", "https://")):
        route.abort("blockedbyclient")
    else:
        route.continue_()


def _press_keys(page: Any) -> None:
    keys = (
        "Enter",
        "Space",
        "ArrowLeft",
        "ArrowRight",
        "ArrowUp",
        "ArrowDown",
        "a",
        "d",
        "w",
        "s",
        "p",
        "m",
        "Escape",
    )
    for key in keys:
        page.keyboard.down(key)
        page.wait_for_timeout(20)
        page.keyboard.up(key)


def _press_gameplay_keys(page: Any) -> None:
    for key in ("Space", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "a", "d", "w", "s"):
        page.keyboard.down(key)
        page.wait_for_timeout(20)
        page.keyboard.up(key)


def _click_visible_controls(page: Any, limit: int = 12) -> None:
    controls = page.locator(
        "button:visible, input[type=button]:visible, input[type=submit]:visible, "
        "input[type=checkbox]:visible, input[type=radio]:visible, [role=button]:visible"
    )
    for index in range(min(controls.count(), limit)):
        control = controls.nth(index)
        try:
            if control.is_visible():
                control.click(timeout=750, no_wait_after=True)
                page.wait_for_timeout(40)
        except Exception:
            # Controls can disappear after an earlier click. Event-handler
            # failures are captured by pageerror/console; a stale locator is a
            # driver detail, not an application defect.
            continue


def _exercise_pointer(page: Any) -> None:
    x, y = 400.0, 300.0
    try:
        canvas = page.locator("canvas:visible").first
        box = canvas.bounding_box() if canvas.is_visible() else None
        if box:
            x = float(box["x"]) + float(box["width"]) / 2
            y = float(box["y"]) + float(box["height"]) / 2
    except Exception:
        pass
    page.mouse.move(x, y)
    page.mouse.down()
    page.wait_for_timeout(25)
    page.mouse.up()
    page.mouse.click(x, y)


def _style_contract_errors(page: Any) -> list[str]:
    """Detect a wholesale DOM/CSS contract miss without policing hook classes."""
    if not hasattr(page, "evaluate"):
        return []
    try:
        coverage = page.evaluate(
            r"""() => {
              const used = new Set();
              document.querySelectorAll('[class]').forEach(el =>
                el.classList.forEach(name => used.add(name)));
              const covered = new Set();
              function visit(rules) {
                for (const rule of Array.from(rules || [])) {
                  if (rule.cssRules) visit(rule.cssRules);
                  if (!rule.selectorText) continue;
                  const matches = rule.selectorText.match(/\.[_a-zA-Z][_a-zA-Z0-9-]*/g) || [];
                  matches.forEach(token => covered.add(token.slice(1)));
                }
              }
              for (const sheet of Array.from(document.styleSheets)) {
                try { visit(sheet.cssRules); } catch (_) {}
              }
              return { used: Array.from(used), covered: Array.from(covered) };
            }"""
        )
    except Exception as exc:
        return [_short_error("style contract", str(exc))]
    if not isinstance(coverage, dict):
        return []
    used = set(coverage.get("used") or [])
    covered = set(coverage.get("covered") or [])
    if len(used) < 8:
        return []
    ratio = len(used & covered) / len(used)
    if ratio >= 0.35:
        return []
    missing = sorted(used - covered)[:12]
    return [
        "style contract: only "
        f"{len(used & covered)}/{len(used)} DOM classes match any stylesheet rule; "
        "unmatched examples: " + ", ".join(missing)
    ]


def _exercise_arcade_portal(
    page: Any, url: str, errors: list[str], timeout_ms: int, sustain_ms: int,
) -> bool:
    """Run behavior assertions for the reusable data-game arcade contract."""
    if not hasattr(page, "evaluate"):
        return False
    try:
        profile = page.evaluate(
            "() => !!window.__arcadePortal && document.querySelectorAll('[data-game]').length > 1"
        )
    except Exception:
        return False
    if not profile:
        return False

    try:
        game_ids = page.locator("[data-game]").evaluate_all(
            "els => [...new Set(els.map(el => el.dataset.game).filter(Boolean))]"
        )
    except Exception as exc:
        errors.append(_short_error("arcade profile", str(exc)))
        return True

    # Global sound is a user-visible toggle: its accessible state or label must
    # change when activated. A click that leaves every signal unchanged is inert.
    sound = page.locator("#sound-toggle")
    try:
        if sound.count() and sound.first.is_visible():
            before = sound.first.evaluate(
                "el => [el.textContent.trim(), el.getAttribute('aria-pressed'), el.className]"
            )
            sound.first.click(timeout=750, no_wait_after=True)
            page.wait_for_timeout(80)
            after = sound.first.evaluate(
                "el => [el.textContent.trim(), el.getAttribute('aria-pressed'), el.className]"
            )
            if before == after:
                errors.append("arcade sound contract: sound toggle is inert")
    except Exception as exc:
        errors.append(_short_error("arcade sound contract", str(exc)))

    for game_id in game_ids:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(150)
            selector = f"[data-game={json.dumps(str(game_id))}]"
            page.locator(selector).first.click(timeout=1_000, no_wait_after=True)
            page.wait_for_timeout(180)
            started = page.evaluate(
                "() => !!(window.__arcadePortal && window.__arcadePortal._currentGame)"
            )
            if not started:
                errors.append(f"arcade game contract: {game_id} did not start")
                continue

            timer_before = page.evaluate(
                "() => { const g=window.__arcadePortal._currentGame; "
                "return typeof g.timer === 'number' ? g.timer : null; }"
            )
            _press_gameplay_keys(page)
            _exercise_pointer(page)
            page.wait_for_timeout(max(400, min(900, int(sustain_ms))))
            timer_after = page.evaluate(
                "() => { const g=window.__arcadePortal._currentGame; "
                "return typeof g.timer === 'number' ? g.timer : null; }"
            )
            elapsed = max(400, min(900, int(sustain_ms)))
            if (isinstance(timer_before, (int, float))
                    and isinstance(timer_after, (int, float))
                    and timer_before > 1_000
                    and timer_before - timer_after < elapsed * 0.1):
                errors.append(
                    f"arcade timing contract: {game_id} millisecond-scale timer "
                    "is decrementing in seconds"
                )

            pointer = page.evaluate(
                "() => { const g=window.__arcadePortal._currentGame; const p=g.input&&g.input.pointer; "
                "return p ? {x:p.x,y:p.y,rawX:p.rawX,rawY:p.rawY,reticle:g.reticle||null} : null; }"
            )
            if (isinstance(pointer, dict) and pointer.get("rawX")
                    and pointer.get("rawY")
                    and pointer.get("x") == 0 and pointer.get("y") == 0):
                errors.append(
                    f"arcade pointer contract: {game_id} receives raw pointer input "
                    "but leaves logical coordinates at (0,0)"
                )

            pause_contract = page.locator("#overlay-pause, [data-action=pause]").count() > 0
            if pause_contract:
                state_before = page.evaluate("() => window.__arcadePortal.state")
                page.keyboard.press("p")
                page.wait_for_timeout(100)
                state_after = page.evaluate("() => window.__arcadePortal.state")
                overlay = page.locator("#overlay-pause")
                overlay_visible = bool(overlay.count() and overlay.first.is_visible())
                if state_after == state_before and not overlay_visible:
                    errors.append(f"arcade pause contract: {game_id} does not pause on P")
                page.keyboard.press("p")
        except Exception as exc:
            errors.append(_short_error(f"arcade game {game_id}", str(exc)))
    return True


def browser_acceptance(
    path: str | Path,
    *,
    timeout_s: float = 20,
    sustain_ms: int = 1_500,
) -> BrowserAcceptanceResult:
    """Exercise an HTML file in an isolated real browser.

    External HTTP(S) requests are blocked. Local linked assets, ``data:`` URLs,
    and ``blob:`` URLs remain available, so a staged self-contained web project
    is tested as delivered.
    """
    artifact = Path(path)
    if artifact.suffix.lower() not in {".html", ".htm"}:
        return BrowserAcceptanceResult(True, False, False, "not an HTML artifact")
    try:
        source = artifact.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        detail = _short_error("unreadable HTML", str(exc))
        return BrowserAcceptanceResult(False, True, False, detail, (detail,))
    if not is_interactive_html(source):
        return BrowserAcceptanceResult(True, False, False, "static HTML; browser gate skipped")

    factory = _sync_playwright_factory()
    if factory is None:
        detail = "interactive HTML requires Playwright, but Playwright is unavailable"
        return BrowserAcceptanceResult(False, True, False, detail, (detail,))

    timeout_ms = max(1_000, int(float(timeout_s) * 1_000))
    browser = None
    context = None
    errors: list[str] = []
    browser_name = ""
    try:
        with factory() as playwright:
            browser, browser_name, launch_failures = _launch_browser(playwright, timeout_ms)
            if browser is None:
                detail = "no Chromium, Microsoft Edge, or Google Chrome browser could be launched"
                return BrowserAcceptanceResult(
                    False, True, False, detail, tuple(launch_failures), ""
                )

            context = browser.new_context(service_workers="block")
            context.set_default_timeout(min(timeout_ms, 3_000))
            context.route("**/*", _block_external)
            attached: set[int] = set()
            if hasattr(context, "on"):
                context.on("page", lambda opened: _attach_error_capture(opened, errors, attached))
            page = context.new_page()
            _attach_error_capture(page, errors, attached)
            page.goto(
                artifact.resolve().as_uri(),
                wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
            page.wait_for_timeout(250)
            used_profile = _exercise_arcade_portal(
                page, artifact.resolve().as_uri(), errors, timeout_ms, sustain_ms
            )
            if not used_profile:
                _press_keys(page)
                _click_visible_controls(page)
                _exercise_pointer(page)
                _press_keys(page)
                page.wait_for_timeout(max(250, int(sustain_ms)))
            errors.extend(_style_contract_errors(page))
    except Exception as exc:
        errors.append(_short_error("browser driver", str(exc)))
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass

    if errors:
        detail = f"browser acceptance failed with {len(errors)} error(s): {errors[0]}"
        return BrowserAcceptanceResult(
            False, True, True, detail, tuple(errors), browser_name
        )
    return BrowserAcceptanceResult(
        True,
        True,
        True,
        f"passed real-browser input and sustained-runtime checks ({browser_name})",
        (),
        browser_name,
    )


__all__ = ["BrowserAcceptanceResult", "browser_acceptance", "is_interactive_html"]
