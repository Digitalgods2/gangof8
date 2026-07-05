"""Headless runtime smoke test: does a web artifact actually RUN, or does it
throw on load?

Judging or verifying a file by READING it misses runtime failures. A real
model shipped a Centipede that read as complete and correct and won a unanimous
5-judge vote — but died on the first frame because `draw()` touched an
uninitialised grid, so the delivered file showed a black screen and did
nothing. Nothing that fails to run may ever win a vote, be promoted, or be
reported as success. This module executes the file's JavaScript in a stubbed
DOM (via Node) and reports whether it survives load + the first animation
frames — the exact window that crash lived in.

Only DETECTED crashes block. A file we cannot execute (unknown type, no Node)
is reported testable=False and never blocks — we gate on what we can prove, and
report honestly what we cannot.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

_SCRIPT_RE = re.compile(r"<script[^>]*>(.*?)</script>", re.S | re.I)
_WEB_SUFFIXES = (".html", ".htm", ".js", ".mjs")

# A stubbed browser: any canvas/DOM call returns a chainable no-op; the script
# runs at load, then we fire load/DOMContentLoaded and pump the animation-frame
# loop a few times — that is where a first-frame crash surfaces.
_HARNESS = r"""
'use strict';
// Some of these names are predefined and read-only on newer Node (navigator,
// performance) — assign defensively so the harness never dies before the
// script under test even runs.
function _def(name, val){
  try { globalThis[name] = val; }
  catch(e){ try { Object.defineProperty(globalThis, name, { value: val, configurable: true, writable: true }); } catch(e2){} }
}
const _noop = () => {};
const _chain = new Proxy(function(){}, {
  get(t, k){ if (k === Symbol.toPrimitive) return () => 0; return _chain; },
  apply(){ return _chain; }, construct(){ return _chain; }
});
function _el(){
  return { getContext: () => _ctx, addEventListener: _add, removeEventListener: _noop,
    appendChild: _noop, removeChild: _noop, setAttribute: _noop, getAttribute: () => null,
    style: {}, classList: { add:_noop, remove:_noop, toggle:_noop, contains: () => false },
    getBoundingClientRect: () => ({ left:0, top:0, right:800, bottom:600, width:800, height:600 }),
    focus: _noop, blur: _noop, requestPointerLock: _noop, textContent: "", innerHTML: "",
    value: "", width: 800, height: 600, clientWidth: 800, clientHeight: 600, dataset: {},
    onload: null, play: () => Promise.resolve(), pause: _noop };
}
const _ctx = new Proxy({}, { get(t, k){
  if (k === "canvas") return _el();
  if (k === "measureText") return () => ({ width: 0 });
  if (k === "createLinearGradient" || k === "createRadialGradient" || k === "createPattern")
    return () => ({ addColorStop: _noop });
  if (k === "getImageData") return () => ({ data: new Uint8ClampedArray(4) });
  return _noop;
}});
const _listeners = {};
function _add(type, fn){ (_listeners[type] = _listeners[type] || []).push(fn); }
const _audio = () => ({ state:"running", resume:()=>Promise.resolve(), suspend:_noop,
  currentTime:0, sampleRate:44100, destination:{},
  createOscillator:()=>({ frequency:{ setValueAtTime:_noop, linearRampToValueAtTime:_noop, exponentialRampToValueAtTime:_noop, value:0 },
    type:"", detune:{ setValueAtTime:_noop }, connect:()=>_chain, start:_noop, stop:_noop, onended:null }),
  createGain:()=>({ gain:{ setValueAtTime:_noop, linearRampToValueAtTime:_noop, exponentialRampToValueAtTime:_noop, value:0 }, connect:()=>_chain }),
  createBiquadFilter:()=>({ frequency:{ setValueAtTime:_noop, exponentialRampToValueAtTime:_noop, value:0 }, type:"", connect:()=>_chain }),
  createBuffer:()=>({ getChannelData:()=>new Float32Array(256) }),
  createBufferSource:()=>({ buffer:null, connect:()=>_chain, start:_noop, stop:_noop }),
  createDynamicsCompressor:()=>({ connect:()=>_chain }),
  createAnalyser:()=>({ connect:()=>_chain }),
  decodeAudioData:()=>Promise.resolve({}) });
const _store = {};
const _raf = (cb) => { _rafState.cb = cb; return 1; };
const _rafState = { cb: null };
const _audioCtor = function(){ return _audio(); };
_def("localStorage", { getItem:(k)=> (k in _store ? _store[k] : null), setItem:(k,v)=>{_store[k]=String(v);},
  removeItem:(k)=>{delete _store[k];}, clear:()=>{for (const k in _store) delete _store[k];} });
_def("requestAnimationFrame", _raf);
_def("cancelAnimationFrame", _noop);
_def("setTimeout", () => 0); _def("clearTimeout", _noop);
_def("setInterval", () => 0); _def("clearInterval", _noop);
_def("performance", { now: () => 0 });
_def("matchMedia", () => ({ matches:false, addEventListener:_noop, addListener:_noop, removeListener:_noop }));
_def("navigator", { userAgent:"node", maxTouchPoints:0, vibrate:_noop, language:"en", platform:"node" });
_def("devicePixelRatio", 1);
_def("AudioContext", _audioCtor);
_def("webkitAudioContext", _audioCtor);
_def("Image", function(){ return _el(); });
_def("FontFace", function(){ return { load: () => Promise.resolve() }; });
_def("screen", { width:800, height:600, orientation:{ lock:_noop } });
_def("document", { getElementById: _el, querySelector: _el, querySelectorAll: () => [],
  createElement: _el, createElementNS: _el, getElementsByTagName: () => [ _el() ],
  getElementsByClassName: () => [], addEventListener:_add, removeEventListener:_noop,
  body: _el(), documentElement: _el(), head: _el(), readyState:"complete",
  fonts: { add:_noop, ready: Promise.resolve(), load: () => Promise.resolve() }, hidden:false });
_def("window", new Proxy({
  innerWidth:800, innerHeight:600, devicePixelRatio:1, addEventListener:_add, removeEventListener:_noop,
  requestAnimationFrame:_raf, cancelAnimationFrame:_noop,
  setTimeout: () => 0, clearTimeout:_noop, setInterval: () => 0, clearInterval:_noop,
  AudioContext:_audioCtor, webkitAudioContext:_audioCtor,
  localStorage: globalThis.localStorage, matchMedia: globalThis.matchMedia, navigator: globalThis.navigator,
  performance: globalThis.performance, location:{ href:"", reload:_noop }, onload:null,
  getComputedStyle: () => ({ getPropertyValue: () => "" }), scrollTo:_noop, alert:_noop, focus:_noop,
  document: globalThis.document
}, { get(t,k){ return k in t ? t[k] : (globalThis[k] !== undefined ? globalThis[k] : _noop); }, set(t,k,v){ t[k]=v; return true; } }));

function _fire(map, type){ (map[type]||[]).slice().forEach(fn => { try { fn({ preventDefault:_noop, code:"", key:"", touches:[], changedTouches:[], clientX:0, clientY:0 }); } catch(e){} }); }

try {
__SCRIPT__
  _fire(_listeners, "DOMContentLoaded");
  _fire(_listeners, "load");
  if (typeof global.window.onload === "function") { try { global.window.onload(); } catch(e){} }
  let ts = 0;
  for (let i = 0; i < 12 && _rafState.cb; i++){ const cb = _rafState.cb; _rafState.cb = null; ts += 16; cb(ts); }
  console.log("SMOKE_OK");
} catch (e) {
  console.log("SMOKE_THREW:" + (e && e.message ? e.message : String(e)));
}
"""


def is_web_file(path) -> bool:
    return Path(path).suffix.lower() in _WEB_SUFFIXES


def smoke_source(text: str, suffix: str = ".html", timeout_s: int = 25) -> tuple[bool, bool, str]:
    """Run web source and report (ok, testable, detail). testable is False when
    we could not execute it (no Node / not web / no script) — those never
    block. ok is False only on a DETECTED runtime failure."""
    suffix = (suffix or "").lower()
    if suffix not in _WEB_SUFFIXES:
        return True, False, "not a runnable web file"
    node = shutil.which("node")
    if not node:
        return True, False, "node not on PATH — runtime test skipped"
    if suffix in (".js", ".mjs"):
        src = text
    else:
        scripts = _SCRIPT_RE.findall(text or "")
        if not scripts:
            return True, False, "no <script> to execute"
        src = "\n".join(scripts)
    harness = _HARNESS.replace("__SCRIPT__", src)
    fd, tmp = tempfile.mkstemp(suffix=".js", prefix="conclave_smoke_")
    os.close(fd)
    try:
        Path(tmp).write_text(harness, encoding="utf-8")
        try:
            r = subprocess.run([node, tmp], capture_output=True, text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return False, True, "runtime hung on load (possible infinite loop before first frame)"
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    out = (r.stdout or "") + (r.stderr or "")
    if "SMOKE_OK" in out:
        return True, True, "ran clean through load + first frames"
    m = re.search(r"SMOKE_THREW:(.*)", out)
    if m:
        return False, True, "threw on load — " + m.group(1).strip()[:200]
    return False, True, "did not run — " + (out.strip()[:200] or "no output")


def smoke_test(path, timeout_s: int = 25) -> tuple[bool, bool, str]:
    """(ok, testable, detail) for a file on disk. See smoke_source."""
    p = Path(path)
    if not is_web_file(p):
        return True, False, "not a runnable web file"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return False, True, f"unreadable: {e}"
    return smoke_source(text, p.suffix, timeout_s)
