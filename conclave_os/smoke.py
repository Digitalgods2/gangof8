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
let _frameOps = [];  // canvas draw calls recorded for the CURRENT frame
function _drawSig(name, args){
  let s = name;
  for (let i = 0; i < args.length; i++){
    const a = args[i];
    s += (typeof a === "number" ? (a | 0) : (typeof a === "string" ? a.slice(0, 10) : "x")) + ",";
  }
  _frameOps.push(s);
}
// Canvas ops that put pixels on screen — recorded so we can tell whether the
// picture CHANGES frame to frame (a live game) or is frozen (a static screen).
const _DRAW = { fillRect:1, strokeRect:1, fillText:1, strokeText:1, drawImage:1, arc:1, arcTo:1,
  ellipse:1, rect:1, lineTo:1, quadraticCurveTo:1, bezierCurveTo:1, fill:1, stroke:1, putImageData:1, roundRect:1 };
const _ctx = new Proxy({}, { get(t, k){
  if (k === "canvas") return _el();
  if (k === "measureText") return () => ({ width: 0, actualBoundingBoxAscent: 0, actualBoundingBoxDescent: 0 });
  if (k === "createLinearGradient" || k === "createRadialGradient" || k === "createConicGradient")
    return () => ({ addColorStop: _noop });
  if (k === "createPattern") return () => ({ setTransform: _noop });
  if (k === "getImageData") return () => ({ data: new Uint8ClampedArray(4), width: 1, height: 1 });
  if (k === "createImageData") return () => ({ data: new Uint8ClampedArray(4), width: 1, height: 1 });
  if (k === "getLineDash") return () => [];
  if (typeof k === "string" && _DRAW[k]) return function(){ _drawSig(k, arguments); return _chain; };
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
// Capture timer callbacks so timer-driven game loops (setTimeout/setInterval) can
// be advanced by the pump, not silently dropped.
const _timers = [];
_def("setTimeout", (fn) => { if (typeof fn === "function") _timers.push(fn); return 0; });
_def("clearTimeout", _noop);
_def("setInterval", (fn) => { if (typeof fn === "function") _timers.push(fn); return 0; });
_def("clearInterval", _noop);
// An ADVANCING clock so delta-time games (dt = now - last) actually move.
let _now = 0;
_def("performance", { now: () => _now });
_def("matchMedia", () => ({ matches:false, addEventListener:_noop, addListener:_noop, removeListener:_noop }));
_def("navigator", { userAgent:"node", maxTouchPoints:0, vibrate:_noop, language:"en", platform:"node" });
_def("devicePixelRatio", 1);
_def("AudioContext", _audioCtor);
_def("webkitAudioContext", _audioCtor);
_def("Image", function(){ return _el(); });
_def("FontFace", function(){ return { load: () => Promise.resolve() }; });
_def("screen", { width:800, height:600, orientation:{ lock:_noop } });
// Browser globals scripts legitimately call BARE (they resolve to window.* in a
// browser). Missing these here FALSELY rejected valid games as crashers — e.g.
// "addEventListener is not defined" / "getComputedStyle is not defined" killed
// two of the best Space Invaders candidates and let a worse one win.
_def("addEventListener", _add);
_def("removeEventListener", _noop);
_def("dispatchEvent", () => true);
_def("getComputedStyle", () => ({ getPropertyValue: () => "", width:"800px", height:"600px" }));
_def("getSelection", () => ({ removeAllRanges:_noop, addRange:_noop, empty:_noop, toString:()=>"", rangeCount:0 }));
_def("scrollTo", _noop); _def("scrollBy", _noop); _def("scroll", _noop);
_def("alert", _noop); _def("confirm", () => true); _def("prompt", () => null);
_def("focus", _noop); _def("blur", _noop); _def("print", _noop); _def("stop", _noop);
_def("open", () => null); _def("close", _noop); _def("postMessage", _noop);
_def("innerWidth", 800); _def("innerHeight", 600);
_def("outerWidth", 800); _def("outerHeight", 600);
_def("pageXOffset", 0); _def("pageYOffset", 0); _def("scrollX", 0); _def("scrollY", 0);
_def("Path2D", function(){ return { moveTo:_noop, lineTo:_noop, arc:_noop, arcTo:_noop, rect:_noop,
  roundRect:_noop, closePath:_noop, bezierCurveTo:_noop, quadraticCurveTo:_noop, ellipse:_noop, addPath:_noop }; });
_def("ImageData", function(w, h){ const W=(w|0)||1, H=(h|0)||1; return { data:new Uint8ClampedArray(W*H*4), width:W, height:H }; });
_def("Audio", function(){ return _el(); });
_def("OffscreenCanvas", function(){ return _el(); });
_def("DOMRect", function(){ return { x:0, y:0, width:0, height:0, top:0, left:0, right:0, bottom:0 }; });
_def("document", { getElementById: _el, querySelector: _el, querySelectorAll: () => [],
  createElement: _el, createElementNS: _el, getElementsByTagName: () => [ _el() ],
  getElementsByClassName: () => [], addEventListener:_add, removeEventListener:_noop,
  body: _el(), documentElement: _el(), head: _el(), readyState:"complete",
  fonts: { add:_noop, ready: Promise.resolve(), load: () => Promise.resolve() }, hidden:false });
_def("window", new Proxy({
  innerWidth:800, innerHeight:600, devicePixelRatio:1, addEventListener:_add, removeEventListener:_noop,
  requestAnimationFrame:_raf, cancelAnimationFrame:_noop,
  setTimeout:(f)=>globalThis.setTimeout(f), clearTimeout:_noop,
  setInterval:(f)=>globalThis.setInterval(f), clearInterval:_noop,
  AudioContext:_audioCtor, webkitAudioContext:_audioCtor,
  localStorage: globalThis.localStorage, matchMedia: globalThis.matchMedia, navigator: globalThis.navigator,
  performance: globalThis.performance, location:{ href:"", reload:_noop }, onload:null,
  getComputedStyle: () => ({ getPropertyValue: () => "" }), scrollTo:_noop, alert:_noop, focus:_noop,
  document: globalThis.document
}, { get(t,k){ return k in t ? t[k] : (globalThis[k] !== undefined ? globalThis[k] : _noop); }, set(t,k,v){ t[k]=v; return true; } }));

let _sigs = [];  // one draw-signature per pumped frame (for motion detection)
function _ev(extra){
  const b = { preventDefault:_noop, stopPropagation:_noop, stopImmediatePropagation:_noop,
    bubbles:true, cancelable:true, code:"", key:"", keyCode:0, which:0, button:0, buttons:0,
    clientX:200, clientY:300, pageX:200, pageY:300, offsetX:200, offsetY:300,
    touches:[], changedTouches:[], targetTouches:[], deltaY:0 };
  if (extra) for (const k in extra) b[k] = extra[k];
  b.target = global.document.body; b.currentTarget = global.document.body;
  return b;
}
function _fire(map, type){ (map[type]||[]).slice().forEach(fn => { try { fn(_ev()); } catch(e){} }); }
function _fireType(type, ev){
  (_listeners[type]||[]).slice().forEach(fn => { try { fn(ev); } catch(e){} });
  const on = "on" + type;
  [global.window, global.document].forEach(o => { try { if (o && typeof o[on] === "function") o[on](ev); } catch(e){} });
}
function _pressKey(kind, key, code){ _fireType(kind, _ev({ key:key, code:code })); }
// Drive the game the way a player would — press keys, click, tap — so we can see
// whether it responds. Broad coverage so most games get "started" and moving.
function _simInput(){
  const keys = [[" ","Space"],["Enter","Enter"],["ArrowLeft","ArrowLeft"],["ArrowRight","ArrowRight"],
    ["ArrowUp","ArrowUp"],["ArrowDown","ArrowDown"],["a","KeyA"],["d","KeyD"],["w","KeyW"],["s","KeyS"],["p","KeyP"]];
  keys.forEach(k => _pressKey("keydown", k[0], k[1]));
  ["mousedown","mouseup","click","pointerdown","pointerup"].forEach(t => _fireType(t, _ev({})));
  const touch = { clientX:200, clientY:300, identifier:0, pageX:200, pageY:300 };
  _fireType("touchstart", _ev({ touches:[touch], changedTouches:[touch], targetTouches:[touch] }));
  _fireType("touchend", _ev({ touches:[], changedTouches:[touch], targetTouches:[] }));
}
function _step(){
  _frameOps = [];
  if (_rafState.cb){ const cb = _rafState.cb; _rafState.cb = null; cb(_now); }  // may throw
  const batch = _timers.splice(0);
  for (let i = 0; i < batch.length && i < 60; i++){ try { batch[i](); } catch(e){} }
  _now += 16;
  _sigs.push(_frameOps.join("|"));
}

try {
__SCRIPT__
  _fire(_listeners, "DOMContentLoaded");
  _fire(_listeners, "load");
  if (typeof global.window.onload === "function") { try { global.window.onload(); } catch(e){} }
  // Phase A — load + first frames must not throw (a crasher fails HERE).
  for (let i = 0; i < 12; i++) _step();
  console.log("SMOKE_OK");
  // Phase B — gameplay probe: drive input and watch for on-screen motion. LENIENT:
  // a throw here does NOT fail the file, it only limits what we can assess. A game
  // that renders a CHANGING picture is "dynamic"; one that draws the same frame
  // forever despite input is "static/frozen".
  let dynamic = "na";
  try {
    _sigs = [];
    _simInput();
    for (let f = 0; f < 90; f++){
      if (f === 15 || f === 45 || f === 70) _simInput();
      if (f === 30){ [" ","ArrowLeft","ArrowRight","a","d"].forEach(k => _pressKey("keyup", k, k)); }
      _step();
    }
    const drew = _sigs.filter(s => s.length > 0);
    if (drew.length >= 3) dynamic = (new Set(drew).size >= 2) ? "true" : "false";
  } catch (e) { /* probe error — keep whatever we assessed */ }
  console.log("SMOKE_DYNAMIC:" + dynamic);
} catch (e) {
  console.log("SMOKE_THREW:" + (e && e.message ? e.message : String(e)));
}
"""


def is_web_file(path) -> bool:
    return Path(path).suffix.lower() in _WEB_SUFFIXES


def smoke_source(text: str, suffix: str = ".html", timeout_s: int = 25) -> tuple[bool, bool, str, Optional[bool]]:
    """Run web source and report (ran, testable, detail, dynamic).

    - ran: False only on a DETECTED runtime failure (throws on load, hangs).
    - testable: False when we couldn't execute it (no Node / not web / no
      script) — those never block.
    - dynamic: True  → rendered a CHANGING picture under simulated input (live);
               False → drew, but the same frame forever despite input (frozen);
               None  → couldn't assess motion (not enough drawing, or a throw
                       during the probe). None never counts against a file."""
    suffix = (suffix or "").lower()
    if suffix not in _WEB_SUFFIXES:
        return True, False, "not a runnable web file", None
    node = shutil.which("node")
    if not node:
        return True, False, "node not on PATH — runtime test skipped", None
    if suffix in (".js", ".mjs"):
        src = text
    else:
        scripts = _SCRIPT_RE.findall(text or "")
        if not scripts:
            return True, False, "no <script> to execute", None
        src = "\n".join(scripts)
    harness = _HARNESS.replace("__SCRIPT__", src)
    fd, tmp = tempfile.mkstemp(suffix=".js", prefix="conclave_smoke_")
    os.close(fd)
    try:
        Path(tmp).write_text(harness, encoding="utf-8")
        try:
            r = subprocess.run([node, tmp], capture_output=True, text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return False, True, "runtime hung on load (possible infinite loop before first frame)", None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    out = (r.stdout or "") + (r.stderr or "")
    if "SMOKE_OK" in out:
        dm = re.search(r"SMOKE_DYNAMIC:(true|false|na)", out)
        dynamic = {"true": True, "false": False, "na": None}.get(dm.group(1) if dm else "na")
        if dynamic is False:
            return True, True, "loads, but renders a static/frozen screen (no motion under input)", False
        return True, True, "ran clean" + (" and shows motion under input" if dynamic else ""), dynamic
    m = re.search(r"SMOKE_THREW:(.*)", out)
    if m:
        return False, True, "threw on load — " + m.group(1).strip()[:200], None
    return False, True, "did not run — " + (out.strip()[:200] or "no output"), None


def smoke_test(path, timeout_s: int = 25) -> tuple[bool, bool, str, Optional[bool]]:
    """(ran, testable, detail, dynamic) for a file on disk. See smoke_source."""
    p = Path(path)
    if not is_web_file(p):
        return True, False, "not a runnable web file", None
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return False, True, f"unreadable: {e}", None
    return smoke_source(text, p.suffix, timeout_s)
