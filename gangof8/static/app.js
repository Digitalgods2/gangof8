let current = null;
let liveKey = null, liveSince = 0;  // drives the ticking elapsed timer
// Expand/collapse state for rollup sections, keyed by stable id (e.g.
// "sec_contributions"). Tracked here rather than read off the DOM because
// renderDetail() rebuilds #right.innerHTML every 3s, which would otherwise
// snap every collapsible shut. The header click handler toggles this map and
// re-renders, so open/closed state survives each poll deterministically.
let openSections = {};

function toggleSection(key) {
  openSections[key] = !openSections[key];
  const el = document.querySelector(`.collapse[data-sec="${key}"]`);
  if (el) el.classList.toggle("open", openSections[key]);
  if (key === "sec_timeline" && openSections[key] && current) loadTimeline(current);
}

async function loadTimeline(sid) {
  const wrap = document.getElementById("tlBody");
  if (!wrap) return;
  const d = await api("/sessions/" + encodeURIComponent(sid) + "/timeline").catch(() => ({events: []}));
  wrap.innerHTML = (d.events || []).map(e => `
    <div class="tlrow">
      <span class="tlts">${esc((e.ts || "").slice(11, 19))}</span>
      <span class="tlic">${e.icon || "•"}</span>
      <span class="tllabel">${esc(e.label)}</span>
      ${e.detail ? `<span class="tldetail">${esc(e.detail)}</span>` : ""}
    </div>`).join("") || '<div class="sub" style="padding:10px">no events</div>';
}

// Render a collapsible block. `key` is the persistent section id, `summary`
// the informative header text shown even when collapsed, `body` the full HTML
// revealed on expand. `defaultOpen` seeds first-seen state (e.g. while live).
function collapsible(key, summary, body, defaultOpen) {
  if (!(key in openSections)) openSections[key] = !!defaultOpen;
  const open = openSections[key];
  return `
    <div class="card collapse ${open ? "open" : ""}" data-sec="${key}">
      <div class="chd" onclick="toggleSection('${key}')">
        <span class="tw">▶</span>
        <h3>${summary}</h3>
      </div>
      <div class="body">${body}</div>
    </div>`;
}

// Shared escaping, diff rendering, model labels, and API access are loaded
// from dashboard-utils.js before this screen-specific script.
// "deepseek/deepseek-v4-pro" → "deepseek-v4-pro"; full id stays in the tooltip
// Colorize a unified diff (promote preview) line by line.

// ---- top-bar utilities: hard refresh, save-as-file, open-a-file --------------
function hardRefresh() {
  // Reload bypassing cache, like Ctrl/Cmd+F5 (a cache-bust param forces a fresh
  // fetch even when the browser would otherwise serve a stale index.html).
  // Drops the #session deep link too, so the page comes back exactly as if
  // the program was freshly launched — no session open, hero showing.
  const u = new URL(location.href);
  u.searchParams.set("_", Date.now());
  u.hash = "";
  location.replace(u.toString());
}

async function savePage() {
  if (!current) { alert("Open a session first, then save it."); return; }
  let s, tl = [];
  try { s = await api("/sessions/" + encodeURIComponent(current)); } catch (e) { s = null; }
  if (!s || s.detail) { alert("Could not load this session to save."); return; }
  try { tl = (await api("/sessions/" + encodeURIComponent(current) + "/timeline")).events || []; } catch (e) {}
  await saveTextFile(sessionToMarkdown(s, tl), "gangof8-" + current + ".md");
}

// Build the whole session view as Markdown (task, summary, risks, files,
// contributions, timeline) from the session object — cleaner than scraping DOM.
function sessionToMarkdown(s, tl) {
  const L = [], final = s.final || {};
  const push = (...xs) => L.push(...xs);
  push("# Gang of 8 — session " + (s.session_id || ""), "");
  push("## Task  (" + (s.status || "") + (s.stop_reason ? " · " + s.stop_reason : "") + ")", "");
  push((s.task && s.task.text) || "", "");
  if (s.established_root) push("📂 established folder: `" + s.established_root + "`", "");
  if (final.answer) {
    push("## Summary" + (final.confidence ? " — " + final.confidence + " confidence" : ""), "");
    push(final.answer, "");
  }
  if ((final.assumptions || []).length) { push("## Assumptions"); final.assumptions.forEach(x => push("- " + x)); push(""); }
  if ((final.risks_unresolved || []).length) { push("## Risks / unresolved"); final.risks_unresolved.forEach(x => push("- " + x)); push(""); }
  if (final.next_action) push("## Next action", "", final.next_action, "");
  const files = s.files_changed || [];
  if (files.length) {
    const est = (s.established_root || "").toLowerCase();
    push("## Files written");
    files.forEach(f => push("- `" + f + "` — " + (est && f.toLowerCase().startsWith(est) ? "your folder (promoted)" : "sandbox (scratch)")));
    push("");
  }
  const contribs = s.contributions || [];
  if (contribs.length) {
    push("## Contributions (" + contribs.length + ")", "");
    contribs.forEach(c => {
      push("### r" + c.round + " · " + c.role + " · " + c.agent + (c.model ? " · " + c.model : "")
           + "  (" + ((c.content || "").length) + " chars)", "", "```", (c.content || ""), "```", "");
    });
  }
  if ((tl || []).length) {
    push("## Timeline");
    tl.forEach(e => push("- " + (e.ts || "") + "  " + (e.label || e.event || "") + (e.detail ? " — " + e.detail : "")));
    push("");
  }
  push("---", "_Exported from Gang of 8_");
  return L.join("\n");
}

async function saveTextFile(text, filename) {
  // Real Save-As dialog where the File System Access API exists (Chrome/Edge);
  // otherwise fall back to a normal download.
  if (window.showSaveFilePicker) {
    try {
      const handle = await window.showSaveFilePicker({
        suggestedName: filename,
        types: [{ description: "Markdown / text", accept: { "text/markdown": [".md"], "text/plain": [".txt"] } }],
      });
      const w = await handle.createWritable();
      await w.write(text);
      await w.close();
      return;
    } catch (e) { if (e && e.name === "AbortError") return; /* else fall through */ }
  }
  const blob = new Blob([text], { type: "text/markdown;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1500);
}

// Open a written file with the host OS's default app (local app: same machine).
async function openFile(path) {
  if (!current || !path) return;
  try {
    const r = await fetch("/files/open", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: current, path }),
    });
    if (!r.ok) {
      const e = await r.json().catch(() => ({}));
      alert("Could not open the file: " + (e.detail || ("HTTP " + r.status)));
    }
  } catch (e) { alert("Could not open the file: " + e); }
}

// ---- empty-state hero: just the site's headline, no animated terminal ------
function renderEmptyHero() {
  document.getElementById("right").innerHTML = `
    <div class="hero-empty">
      <div class="badge-row">
        <span class="bpill"><span class="dot"></span>Runs entirely on your desk</span>
        <span class="bpill">🔒 One hard approval gate</span>
        <span class="bpill">🧾 Full audit trail</span>
      </div>
      <h1>One question.<br><span class="grad-text">Every AI you have.</span><br>One answer you can trust.</h1>
      <p>Pick a session on the left — or give the council something hard in the box below.</p>
    </div>`;
}

async function loadHealth() {
  const h = await api("/health");
  document.getElementById("backend").textContent = "backend: " + h.backend;
}

async function loadWorkspace() {
  wsCache = await api("/workspaces").catch(() => ({workspaces: [], active: null}));
  const active = (wsCache.workspaces || []).find(w => w.id === wsCache.active);
  const el = document.getElementById("workspace");
  el.textContent = "workspace: " + (active ? active.name : "(sandbox)");
  el.title = active ? active.root
                    : ((wsCache.sandbox_root || "") + "\\<session>  (per-session throwaway folder)");
}

// ---- composer: text + multi-modal attachments ----
let attachments = [];  // [{id, name, kind}]
const KIND_ICON = {text: "📄", pdf: "📕", image: "🖼️"};

function autoGrow(el) {
  el.style.height = "auto";
  el.style.height = Math.min(Math.max(el.scrollHeight, 135), 420) + "px";
}

function composerKey(e) {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submitTask(); }
}

function togglePlus(e) {
  e.stopPropagation();
  document.getElementById("plusMenu").classList.toggle("open");
}
document.addEventListener("click", () =>
  document.getElementById("plusMenu")?.classList.remove("open"));

function pickFile(mode) {
  const inp = document.getElementById("fileInput");
  inp.accept = mode === "image"
    ? "image/*"
    : ".txt,.md,.markdown,.rst,.csv,.json,.yaml,.yml,.toml,.ini,.cfg,.log,.pdf,.py,.js,.ts,.html,.css,.sh,.sql";
  inp.value = "";
  inp.click();
  document.getElementById("plusMenu").classList.remove("open");
}

// One upload pipeline for BOTH input boxes (main composer + respond-to-council).
async function uploadFile(file) {
  const b64 = await new Promise((res, rej) => {
    const fr = new FileReader();
    fr.onload = () => res(String(fr.result).split(",", 2)[1] || "");
    fr.onerror = () => rej(fr.error);
    fr.readAsDataURL(file);
  });
  const r = await fetch("/uploads", {method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify({name: file.name, content_base64: b64})});
  if (!r.ok) { const d = await r.json().catch(()=>({})); throw new Error(d.detail || r.status); }
  return r.json();
}

async function onFilePicked(e) {
  const note = document.getElementById("submitNote");
  for (const file of e.target.files) {
    note.style.color = ""; note.textContent = "uploading " + file.name + "…";
    try {
      const rec = await uploadFile(file);
      attachments.push({id: rec.id, name: rec.name, kind: rec.kind});
      renderAttachments();
      note.textContent = rec.note ? `${rec.name}: ${rec.note}` : `attached ${rec.name}`;
    } catch (err) {
      note.style.color = "var(--bad)"; note.textContent = "upload failed: " + err.message;
    }
  }
}

function renderAttachments() {
  document.getElementById("attachments").innerHTML = attachments.map(a => `
    <span class="chip ${esc(a.kind)}">${KIND_ICON[a.kind] || "📎"} ${esc(a.name)}
      <span class="x" onclick="removeAttachment('${esc(a.id)}')">✕</span></span>`).join("");
}

function removeAttachment(id) {
  attachments = attachments.filter(a => a.id !== id);
  renderAttachments();
}

function clearComposer() {
  const box = document.getElementById("task");
  box.value = ""; autoGrow(box);
  attachments = []; renderAttachments();
  document.getElementById("submitNote").textContent = "";
  resetEnhance();
}

// ---- Enhance: amplify a plain prompt with the lead model (with undo) ---------
let enhanceOriginal = null;  // the pre-enhance text; non-null ⇒ button is in Undo mode

function resetEnhance() {
  enhanceOriginal = null;
  const btn = document.getElementById("enhanceBtn");
  if (btn) { btn.textContent = "✨ Enhance"; btn.disabled = false; btn.classList.remove("undo"); }
}

async function enhancePrompt() {
  const ta = document.getElementById("task");
  const btn = document.getElementById("enhanceBtn");
  const note = document.getElementById("submitNote");
  // UNDO — restore the original prompt
  if (enhanceOriginal !== null) {
    ta.value = enhanceOriginal; resetEnhance(); autoGrow(ta); ta.focus();
    note.style.color = ""; note.textContent = "reverted to your original prompt";
    return;
  }
  const text = (ta.value || "").trim();
  if (!text) { note.style.color = ""; note.textContent = "type a prompt first, then Enhance"; return; }
  btn.disabled = true; btn.textContent = "✨ Enhancing…";
  note.style.color = ""; note.textContent = "enhancing your prompt with the strong codifier model…";
  try {
    const r = await fetch("/enhance", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || !d.enhanced) throw new Error(d.detail || ("HTTP " + r.status));
    enhanceOriginal = text;                 // remember for undo
    ta.value = d.enhanced; autoGrow(ta); ta.focus();
    btn.textContent = "↶ Undo"; btn.classList.add("undo"); btn.disabled = false;
    note.textContent = "enhanced — review it, edit if you like, or Undo to restore your original";
  } catch (e) {
    resetEnhance();
    note.style.color = "var(--deny, #e66)"; note.textContent = "enhance failed: " + (e.message || e);
  }
}

function bindComposerControls() {
  const enhance = document.getElementById("enhanceBtn");
  if (enhance) {
    enhance.addEventListener("click", event => {
      event.preventDefault();
      void enhancePrompt();
    });
  }
}

// ---- floatable composer: drag it by the terminal bar out of the way of
// on-screen text; position survives reloads; double-click the bar to re-dock ----
const COMPOSER_POS_KEY = "g8ComposerPos";
function _savedComposerPos() {
  try { return JSON.parse(localStorage.getItem(COMPOSER_POS_KEY)); }
  catch { return null; }
}
function _placeComposer(box, bar, x, y) {
  // partial off-screen is allowed (that's how you shove it out of the way),
  // but at least 140px of the drag bar stays reachable, and the bar can never
  // leave the viewport vertically — a lost composer is unrecoverable
  const w = box.offsetWidth, barH = bar.offsetHeight || 34;
  x = Math.max(140 - w, Math.min(x, window.innerWidth - 140));
  y = Math.max(0, Math.min(y, window.innerHeight - barH));
  box.style.left = x + "px"; box.style.top = y + "px";
  box.style.right = "auto"; box.style.bottom = "auto"; box.style.margin = "0";
}
function bindComposerDrag() {
  const box = document.getElementById("composer");
  const bar = box ? box.querySelector(".term-bar") : null;
  if (!bar) return;
  bar.title = "drag to move · double-click to snap back";
  const saved = _savedComposerPos();
  if (saved) _placeComposer(box, bar, saved.x, saved.y);
  let drag = null;  // pointer offset inside the box while dragging
  bar.addEventListener("pointerdown", e => {
    if (e.button !== 0) return;
    const r = box.getBoundingClientRect();
    drag = {dx: e.clientX - r.left, dy: e.clientY - r.top};
    box.classList.add("dragging");
    bar.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  bar.addEventListener("pointermove", e => {
    if (drag) _placeComposer(box, bar, e.clientX - drag.dx, e.clientY - drag.dy);
  });
  const endDrag = () => {
    if (!drag) return;
    drag = null;
    box.classList.remove("dragging");
    const r = box.getBoundingClientRect();
    localStorage.setItem(COMPOSER_POS_KEY, JSON.stringify({x: r.left, y: r.top}));
  };
  bar.addEventListener("pointerup", endDrag);
  bar.addEventListener("pointercancel", endDrag);
  bar.addEventListener("dblclick", () => {
    localStorage.removeItem(COMPOSER_POS_KEY);
    box.style.left = box.style.top = box.style.right = "";
    box.style.bottom = box.style.margin = "";
  });
  window.addEventListener("resize", () => {
    const p = _savedComposerPos();
    if (p) _placeComposer(box, bar, p.x, p.y);
  });
}

// ---- respond-to-council attachments — multi-modal like the main composer ----
let fuAttachments = [];  // [{id, name, kind}]
const fuChips = () => fuAttachments.map(a => `
  <span class="chip ${esc(a.kind)}">${KIND_ICON[a.kind] || "📎"} ${esc(a.name)}
    <span class="x" onclick="removeFuAttachment('${esc(a.id)}')">✕</span></span>`).join("");
function renderFuAttachments() {
  const el = document.getElementById("fuAttachments");
  if (el) el.innerHTML = fuChips();
}
function removeFuAttachment(id) {
  fuAttachments = fuAttachments.filter(a => a.id !== id);
  renderFuAttachments();
}
function pickFuFile() {
  const inp = document.getElementById("fuFileInput");
  if (!inp) return;
  inp.accept = "image/*,.txt,.md,.markdown,.rst,.csv,.json,.yaml,.yml,.toml,.ini,.cfg,.log,.pdf,.py,.js,.ts,.html,.css,.sh,.sql";
  inp.value = "";
  inp.click();
}
async function onFuFilePicked(e) {
  const note = document.getElementById("fuNote");
  for (const file of e.target.files) {
    if (note) { note.style.color = ""; note.textContent = "uploading " + file.name + "…"; }
    try {
      const rec = await uploadFile(file);
      fuAttachments.push({id: rec.id, name: rec.name, kind: rec.kind});
      renderFuAttachments();
      if (note) note.textContent = rec.note ? `${rec.name}: ${rec.note}` : `attached ${rec.name}`;
    } catch (err) {
      if (note) { note.style.color = "var(--bad)"; note.textContent = "upload failed: " + err.message; }
    }
  }
}

async function sendFollowup(sid) {
  const ta = document.getElementById("followupText");
  const text = (ta?.value || "").trim();
  if (!text && !fuAttachments.length) return;
  const r = await fetch(`/sessions/${encodeURIComponent(sid)}/followup`, {method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({text, attachments: fuAttachments.map(a => a.id)})});
  if (r.ok) { if (ta) ta.value = ""; fuAttachments = []; _lastDetailSig = ""; pollLoop(); }
  else { const d = await r.json().catch(() => ({})); alert("could not continue: " + (d.detail || r.status)); }
}
function followupKey(e) {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendFollowup(current); }
}

async function cancelCurrent() {
  if (!current) return;
  if (!confirm("Cancel this run? It stops at the next step — an in-flight agent call finishes first.")) return;
  const note = document.getElementById("submitNote");
  const r = await fetch(`/sessions/${current}/cancel`, {method: "POST"});
  if (r.ok) { const d = await r.json(); note.style.color = ""; note.textContent = d.status === "cancelled" ? "cancelled" : "cancelling…"; pollLoop(); }
  else { note.style.color = "var(--bad)"; note.textContent = "cancel failed"; }
}

async function submitTask() {
  const box = document.getElementById("task");
  const text = box.value.trim();
  const note = document.getElementById("submitNote");
  if (!text && !attachments.length) { note.textContent = "type a task or attach a file"; return; }
  if (!text) { note.textContent = "add a short instruction for the attachment(s)"; return; }
  const r = await api("/tasks", {method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify({text, source:"dashboard", background:true,
                          attachments: attachments.map(a => a.id)})});
  note.style.color = ""; note.textContent = "submitted " + r.session_id;
  current = r.session_id;
  clearComposer();
  pollLoop();  // a new run just started → fast polling
}

// True when the user currently has text selected inside `el` — we must NOT
// clobber its innerHTML while they're selecting/copying.
function _hasSelectionIn(el) {
  const sel = window.getSelection && window.getSelection();
  if (!el || !sel || sel.isCollapsed || sel.rangeCount === 0) return false;
  return el.contains(sel.anchorNode) || el.contains(sel.focusNode);
}
let _lastListSig = "", _lastDetailSig = "";

async function refresh() {
  const sessions = await api("/sessions");
  const el = document.getElementById("sessions");
  // Show "Cancel run" only while the open session is actually running.
  const curS = sessions.find(s => s.session_id === current);
  const cancelBtn = document.getElementById("cancelBtn");
  if (cancelBtn) cancelBtn.style.display = (curS && !TERMINAL_STATES.has(curS.status)) ? "inline-block" : "none";
  // Only rebuild the list when something actually changed AND no selection is
  // active in it — re-rendering identical HTML every poll wiped selections.
  const listSig = JSON.stringify(sessions.map(s =>
    [s.session_id, s.status, s.pending_approvals || 0, s.pending_inputs || 0,
     s.task_text || "", s.session_id === current]));
  if (listSig === _lastListSig || _hasSelectionIn(el)) {
    if (current) await _refreshDetail();
    return sessions;
  }
  _lastListSig = listSig;
  el.innerHTML = sessions.map(s => `
    <div class="session ${s.session_id===current?"active":""}" onclick="select('${s.session_id}')">
      <button class="trash" title="Delete this session" onclick="deleteSession('${s.session_id}', event)">🗑</button>
      <div class="text">${esc(s.task_text) || "(no text)"}</div>
      <div class="meta">
        <span class="pill ${esc(s.status)}">${esc(s.status)}</span>
        ${s.pending_approvals ? `<span class="pill awaiting_approval">${s.pending_approvals} approval</span>` : ""}
        ${s.pending_inputs ? `<span class="pill awaiting_input">${s.pending_inputs} question</span>` : ""}
        <span>${esc(s.session_id)}</span>
      </div>
    </div>`).join("");
  if (current) await _refreshDetail();
  return sessions;
}

// Re-render the open session's detail ONLY when its data changed and the user
// isn't selecting text in it — so a static (done) session never gets clobbered.
// Exception: once the session reaches a terminal state, render through a
// lingering selection one time. Otherwise leftover highlighted text freezes the
// detail pane on its last "deliberating" render, and the elapsed ticker (which
// keys off liveSince, only cleared inside renderDetail) counts up forever.
// One timeline fetch powers the whole live picture: the latest talent pull
// still awaiting its answer (a CONSULT/DELEGATE happens INSIDE the lead's
// call, so the session JSON alone never shows it while it's happening) AND a
// rolling activity feed — what the council is actually doing right now, not
// just a timer. Returns the talent line; fills _liveFeed as a side effect.
let _liveTalent = "";
let _liveFeed = [];  // recent {ts, icon, label, detail} rows for the live card

async function _liveActivity(sid) {
  const d = await api(`/sessions/${encodeURIComponent(sid)}/timeline`);
  const events = d.events || [];
  let open = "";
  for (const e of events) {
    if (e.event === "delegation_granted") open = e.detail || "talent";
    else if (["delegation_resolved", "delegation_failed", "delegation_denied",
              "round_synthesized", "final_composed"].includes(e.event)) open = "";
  }
  _liveFeed = events.filter(e => e.event !== "status_change").slice(-8);
  return open.slice(0, 90);
}

async function _refreshDetail() {
  const detail = await fetch("/sessions/" + current).then(r => r.ok ? r.json() : null);
  const workingNow = detail &&
    ["received", "classified", "deliberating", "composing"].includes(detail.status);
  _liveTalent = workingNow ? await _liveActivity(detail.session_id).catch(() => "") : "";
  if (!workingNow) _liveFeed = [];
  const right = document.getElementById("right");
  const feedKey = _liveFeed.length
    ? _liveFeed[_liveFeed.length - 1].ts + ":" + _liveFeed.length : "";
  const sig = JSON.stringify(detail) + "|talent:" + _liveTalent + "|feed:" + feedKey;
  if (sig === _lastDetailSig) return;
  const terminal = detail && TERMINAL_STATES.has(detail.status);
  if (!terminal && _hasSelectionIn(right)) return;
  _lastDetailSig = sig;
  renderDetail(detail);
}

// Adaptive polling: only "heartbeat" the server while something is actually
// working. Idle (everything done/cancelled/failed) → slow checks; tab hidden →
// no requests at all; resume instantly when the tab is shown again.
const TERMINAL_STATES = new Set(["done", "cancelled", "failed"]);
const POLL_ACTIVE = 3000, POLL_IDLE = 20000, POLL_HIDDEN = 30000;
let _pollTimer = null;
async function pollLoop() {
  if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; }
  let delay = POLL_IDLE;
  if (document.hidden) {
    delay = POLL_HIDDEN;  // tab not visible: skip the fetch entirely
  } else {
    const sessions = await refresh().catch(() => []);
    const working = (sessions || []).some(s => !TERMINAL_STATES.has(s.status));
    delay = working ? POLL_ACTIVE : POLL_IDLE;
  }
  _pollTimer = setTimeout(pollLoop, delay);
}

async function deleteSession(id, ev) {
  ev.stopPropagation();  // don't also select the row
  if (!confirm("Delete this session permanently? Its transcript and log will be removed.")) return;
  const r = await fetch("/sessions/" + encodeURIComponent(id), {method: "DELETE"});
  if (r.ok) {
    if (current === id) {
      current = null;
      history.replaceState(null, "", location.pathname + location.search);
      renderEmptyHero();
    }
    await refresh();
  }
}

function select(id) {
  current = id; _lastDetailSig = ""; _lastListSig = "";
  // deep link: keep the open session in the URL so a refresh (or a shared
  // link) lands back on it instead of the empty state
  if (location.hash !== "#" + id) history.replaceState(null, "", "#" + id);
  refresh();
}

function renderDetail(s) {
  if (!s) return;
  const right = document.getElementById("right");
  const final = s.final;
  const approvals = (s.approvals||[]).filter(a => a.status === "pending");
  const inputs = (s.input_requests||[]).filter(i => i.status === "pending");

  // The poll timer re-renders this pane every few seconds; snapshot any
  // in-progress answer text + focus/caret + scroll so reading/typing isn't
  // clobbered mid-word.
  const draft = {};
  right.querySelectorAll('textarea[id^="ans_"], #followupText').forEach(t => {
    draft[t.id] = {v: t.value, start: t.selectionStart, end: t.selectionEnd,
                   focused: document.activeElement === t};
  });
  const scrollTop = right.scrollTop;

  const working = ["received","classified","deliberating","composing"].includes(s.status);
  const spokenRoles = new Set((s.contributions||[]).map(c => c.role));
  const members = ((s.council||{}).members||[]);
  const roster = members.filter(m => m.active && m.agent && m.agent !== "system");
  const agentOf = Object.fromEntries(members.map(m => [m.role, m.agent]));
  // exact model per seat, learned from the contributions that carried it
  // exact model per ROLE+AGENT, learned from the contributions that carried it —
  // keyed by role+agent (not agent alone) so a seat that ran two roles (claude as
  // lead AND panelist) shows each role's real model, not whichever call reported
  // last (which mislabelled the sonnet lead as opus after the opus panelist ran).
  const modelKey = (r, a) => (r || "") + "|" + (a || "");
  const contribModel = {};
  (s.contributions||[]).forEach(c => { if (c.model) contribModel[modelKey(c.role, c.agent)] = c.model; });

  // What is it doing right now? Current round's goal + the seat we're waiting
  // on, so a long single agent call doesn't read as frozen.
  let liveGoal = "", waitRole = "", waitAgent = "";
  if (working) {
    const cr = s.current_round ?? 0;
    if (s.status === "composing") {
      waitRole = "summarizer"; waitAgent = agentOf["summarizer"] || ""; liveGoal = "composing answer";
    } else {
      const round = (s.rounds||[]).find(r => r.round === cr);
      liveGoal = (round?.goal || "").split(/[;.]/)[0].trim();
      const doneHere = new Set((s.contributions||[]).filter(c => c.round === cr).map(c => c.role));
      waitRole = (round?.agents||[]).find(role => !doneHere.has(role)) || "";
      waitAgent = waitRole ? (agentOf[waitRole] || "") : "";
    }
  }
  // reset the elapsed clock whenever the live situation actually changes
  const key = working ? `${s.session_id}|${s.status}|${s.current_round}|${s.agent_calls}|${waitRole}` : null;
  if (key !== liveKey) { liveKey = key; liveSince = working ? Date.now() : 0; }

  // ---- client-side rollup stats (no walls of text) ----
  const contribs = s.contributions || [];
  const disagreements = s.disagreements || [];
  const byAgent = {};
  contribs.forEach(c => { byAgent[c.agent] = (byAgent[c.agent] || 0) + 1; });
  const agentCount = Object.keys(byAgent).length;
  const roundCount = (s.rounds || []).length ||
    new Set(contribs.map(c => c.round)).size;
  const calls = s.agent_calls ?? contribs.length;
  const runSummary = s.run_summary || {};
  // talents the lead pulled in = contributions from non-driver roles
  // (panelists contribute every round by design — they are not delegations)
  const DRIVE_ROLES = new Set(["lead", "panelist", "summarizer", "coordinator"]);
  const delegations = contribs.filter(c => c.role && !DRIVE_ROLES.has(c.role)).length;
  // "7 agent calls · 3 agents · 2 delegations"
  const statBits = [];
  if (calls) statBits.push(`<b>${calls}</b> agent call${calls === 1 ? "" : "s"}`);
  const activeMs = runSummary.contribution_duration_ms || 0;
  if (activeMs) statBits.push(`<b>${(activeMs / 1000).toFixed(activeMs < 10000 ? 1 : 0)}s</b> model time`);
  if (agentCount) statBits.push(`<b>${agentCount}</b> agent${agentCount === 1 ? "" : "s"}`);
  if (delegations) statBits.push(`<b>${delegations}</b> delegation${delegations === 1 ? "" : "s"}`);
  if (runSummary.test_fix_attempts) statBits.push(`<b>${runSummary.test_fix_attempts}</b> test repair${runSummary.test_fix_attempts === 1 ? "" : "s"}`);
  // legacy sessions may carry court-era disagreements; show them if present
  if (disagreements.length) statBits.push(`<b>${disagreements.length}</b> disagreement${disagreements.length === 1 ? "" : "s"}`);

  // per-agent breakdown e.g. "gemini ×2, codex ×4"
  const agentBreak = Object.entries(byAgent)
    .map(([a, n]) => `${esc(a)} ×${n}`).join(", ");

  // ruling rollup e.g. "all ruled on evidence" / "3 on evidence, 1 on constraint"
  let rulingSummary = "";
  if (disagreements.length) {
    const byBasis = {};
    disagreements.forEach(d => {
      const b = d.ruling_basis || "unspecified";
      byBasis[b] = (byBasis[b] || 0) + 1;
    });
    const bases = Object.entries(byBasis);
    if (bases.length === 1) rulingSummary = `all ruled on ${esc(bases[0][0])}`;
    else rulingSummary = bases.map(([b, n]) => `${n} on ${esc(b)}`).join(", ");
  }

  const showSummary = !!final || contribs.length || disagreements.length;
  // finished sessions collapse by default; live ones expand so progress shows
  const collapseDefault = working;

  right.innerHTML = `
    <div class="card">
      <h3>Task — <span class="pill ${esc(s.status)}">${esc(s.status)}</span>
          ${s.stop_reason ? `<span class="sub"> · ${esc(s.stop_reason)}</span>` : ""}</h3>
      <div class="mono">${esc(s.task?.text)}</div>
      ${s.established_root ? `<div class="sub" style="margin-top:6px" title="read-only source; files reach it only via an approved promote">📂 established folder: <span class="mono">${esc(s.established_root)}</span></div>` : ""}
      ${(s.attachments||[]).length ? `<div class="atts" style="display:flex;flex-wrap:wrap;gap:6px;margin-top:8px">${
        s.attachments.map(a => `<span class="chip ${esc(a.kind)}">${KIND_ICON[a.kind]||"📎"} ${esc(a.name)}</span>`).join("")
      }</div>` : ""}
    </div>

    ${showSummary ? `
      <div class="card summary">
        <h3>${(s.turns && s.turns.length > 2) ? "Latest conclusion" : "Summary"}</h3>
        ${final ? `
          <div class="row" style="margin-top:0"><span class="conf-${esc(final.confidence)}">${esc(final.confidence)} confidence</span></div>
          <pre>${esc(final.answer)}</pre>` : ``}
        ${statBits.length ? `<div class="stats">${statBits.join("<span>·</span>")}</div>` : ``}
        ${agentBreak ? `<div class="ruling">${agentBreak}</div>` : ``}
        ${rulingSummary ? `<div class="ruling">disagreements — ${rulingSummary}</div>` : ``}
      </div>` : ``}

    ${s.status === "done" ? `
      <div class="card respond">
        <h3>💬 Respond to the council</h3>
        <div class="sub" style="margin-bottom:6px">Agree, push back, or add a new angle — attach documents or images just like the task box; it deliberates again with the whole conversation, no starting over.</div>
        <div id="fuAttachments" style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:6px">${fuChips()}</div>
        <textarea id="followupText" rows="2" placeholder="Your response… (Enter to send, Shift+Enter for a new line)" onkeydown="followupKey(event)"></textarea>
        <div class="row" style="margin-top:6px">
          <button onclick="sendFollowup('${esc(s.session_id)}')">Send response</button>
          <button class="ghost" onclick="pickFuFile()" title="Attach a document (text / PDF) or an image — the council reads documents and vision-capable seats really see images">📎 Attach</button>
          <span class="sub" id="fuNote"></span>
        </div>
        <input type="file" id="fuFileInput" multiple style="display:none" onchange="onFuFilePicked(event)">
      </div>` : ``}

    ${(s.turns && s.turns.length > 2) ? collapsible("sec_conversation",
        `Conversation (${s.turns.length} turns)`,
        s.turns.map(t => `
          <div class="turn ${t.role}">
            <span class="turnwho">${t.role === "user" ? "You" : "Council"}</span>
            <div class="turntext">${esc(t.text)}</div>
          </div>`).join(""), true) : ""}

    ${working ? `
      <div class="card live">
        <div class="livehead">
          <span class="dot"></span>
          <div>
            <div class="what">${esc(s.status)}<span class="ell"></span></div>
            <div class="meta">${liveGoal ? esc(liveGoal) : `round ${s.current_round ?? 0}`}${waitRole ? ` · ${esc(waitRole)} · ${esc(waitAgent)}` : ""}${_liveTalent ? ` · 🤝 ${esc(_liveTalent)}` : ""} · <span id="elapsed">0:00</span></div>
          </div>
        </div>
        ${_liveFeed.length ? `<div class="livefeed">${_liveFeed.map(e => `
          <div class="lfrow">
            <span class="lfts">${esc((e.ts || "").slice(11, 19))}</span>
            <span class="lfic">${e.icon || "•"}</span>
            <span class="lflabel">${esc(e.label)}</span>
            ${e.detail ? `<span class="lfdetail">${esc(e.detail)}</span>` : ""}
          </div>`).join("")}</div>` : ""}
      </div>` : ""}

    ${roster.length ? `
      <div class="card">
        <h3>Council</h3>
        <div class="roster">
          ${roster.map(m => {
            const talent = !DRIVE_ROLES.has(m.role);
            // the model this member's role+agent actually ran; else the one it
            // WILL run (resolved server-side: role pin › seat pin › CLI default).
            const full = contribModel[modelKey(m.role, m.agent)] || m.model || "";
            const title = (talent ? "pulled in by the lead mid-round (CONSULT/DELEGATE) — " : "") + (full ? esc(full) : "model: CLI default");
            return `<span class="seat role-${esc(m.role)}${talent ? " talent" : ""} ${spokenRoles.has(m.role)?"spoke":"on"}"
              title="${title}">${talent ? "🤝 " : ""}${esc(m.role)} · ${esc(m.agent)}${full ? ` · ${esc(shortModel(full))}` : ""}</span>`;
          }).join("")}
        </div>
        ${(() => {
          // Recruitment feed: one plain-language row per talent the lead pulled
          // in — "claude · opus called in as Code Generator" — so a mid-round
          // CONSULT/DELEGATE is a first-class visible event. Answered pulls
          // come from contributions (persist forever); an in-flight pull comes
          // from the event log and shows while the talent is still working.
          const rows = contribs.filter(c => c.role && !DRIVE_ROLES.has(c.role)).map(c => `
            <div class="talentrow">🤝
              <b>${esc(c.agent)}${c.model ? ` · ${esc(shortModel(c.model))}` : ""}</b>
              called in as <b class="seat role-${esc(c.role)} talent spoke">${esc(roleLabel(c.role))}</b>
              <span class="sub">answered in round ${(c.round ?? 0) + 1} — full reply in Contributions</span>
            </div>`);
          if (_liveTalent) rows.push(`
            <div class="talentrow">⏳ <b>${esc(_liveTalent)}</b>
              <span class="sub">— recruited by the lead, answering now…</span></div>`);
          return rows.join("");
        })()}
      </div>` : ""}

    ${(s.council_health && s.council_health.degraded) ? `
      <div class="card health">
        <h3>⚠ Council health — ran degraded</h3>
        ${s.council_health.dropped.map(d => `<div class="hrow"><b>${esc(d.role)}</b> seat (${esc(d.agent)}) dropped — ${esc(d.error)}</div>`).join("")}
        ${s.council_health.substitutions.map(x => `<div class="hrow">summarizer <b>${esc(x.failed)}</b> failed → recomposed with <b>${esc(x.replaced_by)}</b></div>`).join("")}
        ${s.council_health.notes.map(n => `<div class="hrow">${esc(n)}</div>`).join("")}
      </div>` : ""}

    ${approvals.map(a => `
      <div class="card needs">
        <h3>Approval needed — ${esc(a.category)} / ${esc(a.risk)}</h3>
        <div>${esc(a.action)}</div>
        ${a.details ? `<pre class="diff">${diffHtml(a.details)}</pre>` : ""}
        <div class="row">
          <button onclick="resolveApproval('${a.approval_id}', true)">Approve</button>
          <button onclick="resolveApproval('${a.approval_id}', true, true)" title="Approve this and every other ${esc(a.category)} in this session — one decision instead of N identical clicks">Approve all ${esc(a.category)}s</button>
          <button class="deny" onclick="resolveApproval('${a.approval_id}', false)">Deny</button>
        </div>
      </div>`).join("")}

    ${inputs.map(i => {
      const ip = i.purpose === "integration_decision" ? s.integration_proposal : null;
      if (ip) {
        // Decision-first layout: the two NAMED options and their buttons are
        // immediately visible; the merged file is behind a dropdown, never a
        // wall of code between the question and the choice.
        const wAgent = ip.winner_agent || "";
        const wModel = wAgent ? (contribModel[modelKey("panelist", wAgent)] || "") : "";
        const wMeta = [
          ip.winner_score != null ? `score ${ip.winner_score}` : "",
          ip.winner_votes != null ? `${ip.winner_votes}/${ip.judges ?? "?"} first-place votes` : "",
          ip.chair || "",
        ].filter(Boolean).join(" · ");
        const iMeta = [
          `merges: ${(ip.source_candidates || []).join(", ") || "council review"}`,
          // tri-state: null = pre-upgrade session, runtime status unknown — say nothing
          ip.runtime_checked === true ? "runtime-checked ✓"
            : ip.runtime_checked === false ? "⚠ not runtime-checked" : "",
        ].filter(Boolean).join(" · ");
        const peekKey = "integ_" + i.input_id;
        return `
      <div class="card needs">
        <h3>🧬 Council integration decision</h3>
        <div class="sub">The council found complementary strengths worth merging after the blind vote. The voted winner stays the default — pick which file ships.</div>
        <div class="duel">
          <div class="opt winner">
            <div class="opt-h">🏆 Voted winner${wAgent ? ` — ${esc(wAgent)}${wModel ? ` · ${esc(shortModel(wModel))}` : ""}` : ""}</div>
            ${wMeta ? `<div class="opt-meta">${esc(wMeta)}</div>` : ""}
            <div class="opt-why">One model's coherent code, exactly as it won the blind vote and passed the runtime gate — ships byte-for-byte, no merge risk.</div>
            <button class="ghost" onclick="chooseIntegration('${i.input_id}', 'keep winner')">Keep voted winner</button>
          </div>
          <div class="opt integ">
            <div class="opt-h">🧬 Integration — <span class="mono">${esc(ip.filename)}</span></div>
            <div class="opt-meta">${esc(iMeta)}</div>
            <div class="opt-why">${esc(ip.rationale)}</div>
            <button onclick="chooseIntegration('${i.input_id}', 'use integration')">Use integration</button>
          </div>
        </div>
        <details class="codepeek" ${openSections[peekKey] ? "open" : ""}
                 ontoggle="openSections['${peekKey}']=this.open">
          <summary>view the integrated file (${((ip.content || "").length).toLocaleString()} chars)</summary>
          <pre>${esc(ip.content)}</pre>
        </details>
      </div>`;
      }
      return `
      <div class="card needs">
        <h3>${i.purpose === "integration_decision" ? "Council integration decision" : "Agent question - " + esc(i.role) + "@" + esc(i.agent)}</h3>
        <div>${esc(i.question)}</div>
        <textarea id="ans_${i.input_id}" placeholder="Your answer…"></textarea>
        <div class="row">
          <button onclick="answerInput('${i.input_id}')">Answer</button>
          <button class="deny" onclick="declineInput('${i.input_id}')">Decline</button>
        </div>
      </div>`;
    }).join("")}

    ${final && (final.assumptions?.length || final.risks_unresolved?.length || final.next_action) ? `
      <div class="card">
        ${final.assumptions?.length ? `<h3>Assumptions</h3><ul>${final.assumptions.map(x=>`<li>${esc(x)}</li>`).join("")}</ul>` : ""}
        ${final.risks_unresolved?.length ? `<h3>Risks / unresolved</h3><ul>${final.risks_unresolved.map(x=>`<li>${esc(x)}</li>`).join("")}</ul>` : ""}
        ${final.next_action ? `<h3>Next action</h3><div>${esc(final.next_action)}</div>` : ""}
      </div>` : ""}

    ${(s.files_changed||[]).length ? (() => {
        const est = s.established_root || "";
        const inEst = f => est && f.toLowerCase().startsWith(est.toLowerCase());
        const manifest = new Map((runSummary.files || []).map(x => [x.path, x]));
        return `
      <div class="card"><h3>Files written</h3>
        <div class="sub" style="margin-bottom:6px">Scratch files live in the council's sandbox/workspace. Your established folder is only touched by an approved <b>promote</b>. Use the <b>📎</b> button to open any file with your default app.</div>
        <ul class="filelist">${s.files_changed.map(f => {
          const entry = manifest.get(f);
          const digest = entry?.sha256 ? ` <span class="badge" title="SHA-256 integrity hash">sha256 ${esc(entry.sha256.slice(0, 12))}…</span>` : "";
          return `<li class="mono filerow"><button class="filebtn" data-path="${escAttr(f)}" title="Open this file with your default app" onclick="openFile(this.dataset.path)">📎</button><span class="fpath">${esc(f)}</span> <span class="badge" style="font-size:10px">${inEst(f) ? "→ your folder (promoted)" : "sandbox (scratch)"}</span>${digest}</li>`;
        }).join("")}</ul></div>`;
      })() : ""}

    ${disagreements.length ? collapsible("sec_disagreements",
        `Disagreements (${disagreements.length})${rulingSummary ? ` — ${rulingSummary}` : ""}`,
        disagreements.map(d => `
          <div class="contrib">
            <div class="hd"><span class="who">${esc(d.topic)}</span>
              <span class="len">ruled on ${esc(d.ruling_basis)}</span></div>
            <div style="padding:10px">
              <ul>${(d.positions||[]).map(p=>`<li><b>${esc(p.role)}:</b> ${esc(p.claim)}</li>`).join("")}</ul>
              ${d.critic_test ? `<div class="mono">${esc(d.critic_test)}</div>` : ""}
            </div>
          </div>`).join(""),
        collapseDefault) : ""}

    ${contribs.length ? collapsible("sec_contributions",
        `Contributions (${contribs.length})${agentBreak ? ` — ${agentBreak}` : ""}`,
        contribs.map(c => `
          <div class="contrib">
            <div class="hd"><span class="rnd">r${c.round}</span>
              <span class="who" title="${c.model ? esc(c.model) : "model: CLI default"}">${esc(c.role)} · ${esc(c.agent)}${c.model ? ` · ${esc(shortModel(c.model))}` : ""}</span>
              <span class="len">${(c.content||"").length} chars</span></div>
            <pre>${esc(c.content)}</pre>
          </div>`).join(""),
        collapseDefault) : ""}

    ${(final || contribs.length) ? collapsible("sec_timeline", "Timeline",
        '<div id="tlBody"><div class="sub" style="padding:10px">loading…</div></div>', false) : ""}
  `;
  if (openSections["sec_timeline"] && current) loadTimeline(current);

  // Restore answer-in-progress + scroll position that survived this re-render.
  for (const [id, d] of Object.entries(draft)) {
    const t = document.getElementById(id);
    if (!t) continue;
    t.value = d.v;
    if (d.focused) { t.focus(); try { t.setSelectionRange(d.start, d.end); } catch {} }
  }
  right.scrollTop = scrollTop;
  tickElapsed();
}

function tickElapsed() {
  const el = document.getElementById("elapsed");
  if (!el || !liveSince) return;
  const t = Math.floor((Date.now() - liveSince) / 1000);
  el.textContent = Math.floor(t / 60) + ":" + String(t % 60).padStart(2, "0");
}

async function resolveApproval(aid, approved, approve_all = false) {
  await api(`/sessions/${current}/approvals/${aid}`, {method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify({approved, approve_all, background:true})});
  pollLoop();  // work may resume → return to fast polling
}

async function answerInput(iid) {
  const text = document.getElementById("ans_" + iid).value;
  if (!text.trim()) return;
  await api(`/sessions/${current}/inputs/${iid}`, {method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify({answer: text, background:true})});
  pollLoop();  // deliberation resumes → return to fast polling
}

async function chooseIntegration(iid, choice) {
  await api(`/sessions/${current}/inputs/${iid}`, {method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify({answer: choice, background:true})});
  pollLoop();
}

async function declineInput(iid) {
  await api(`/sessions/${current}/inputs/${iid}`, {method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify({decline:true})});
  refresh();
}

// ============================ Settings panel ============================
// Self-contained: its own open/close + render, sharing only esc/api helpers.
// Opening overlays the dashboard; closing returns to it untouched.
const ROLES = ["lead", "researcher", "architect", "critic", "implementer", "summarizer"];
const roleList = s => (s.role_catalog && s.role_catalog.length) ? s.role_catalog : ROLES;
const roleLabel = r => r.split("_").map(w => w ? w[0].toUpperCase() + w.slice(1) : w).join(" ");
let settingsCache = null, seatsCache = [], wsCache = {workspaces: [], active: null};
let orKey = {present: false, masked: null, source: null};
let gemKey = {present: false, masked: null, source: null};

// Tooltip text — purpose of each setting, shown on hover of the ⓘ marker.
const TIPS = {
  backend: "Which agent backend runs sessions. 'mock' is offline and free for testing; 'cli' runs the local claude/codex/gemini CLIs directly in generation mode, so the implementer emits real file contents — fully self-contained.",
  roles: "The 'lead' is the organizer: it breaks each task into assignments and hands them to the talents below, who do the actual work (coding, research, writing, verification). Map each role to a seat (claude, codex, gemini, or an OpenRouter seat) and optionally pin a MODEL for that role — the pin beats the seat's default only when this role runs (role pin › seat pin › CLI default). '(backend default)' uses the preset. The agent CLIs manage their own auth.",
  lead: "The organizer and integrator: decomposes the task, assigns the substantive work to the talents below, integrates their results, and decides when it's done — it authors only trivial glue itself.",
  knowledge_retriever: "Talent that gathers cited evidence and marks unsupported claims as assumptions.",
  researcher: "Agent that interprets gathered evidence for the user's research/development goal.",
  architect: "Agent that proposes design / structure when a task needs design work.",
  api_integrator: "Agent that maps external API contracts and integration failure modes.",
  code_generator: "Agent that proposes modular implementation units and integration points.",
  critic: "Agent that challenges the strongest claims and surfaces disagreements.",
  red_team: "Agent that tries to break the proposal with adversarial cases and wrong assumptions.",
  fact_validator: "Agent that independently confirms, refutes, or marks claims as unverifiable.",
  implementer: "Talent that drafts complete written deliverables (docs, reports, prose) when the lead assigns them.",
  summarizer: "Agent that composes the final, reconciled answer.",
  risk: "Tasks classified above this risk level pause for your approval before any deliberation runs.",
  prose: "If an agent returns unlabeled prose at least this many characters long, it's accepted as the answer (at medium confidence) instead of being retried.",
  reserved: "Agent calls held back from deliberation so the composer (and its one retry) can always run.",
  critic_tests: "Maximum disagreements the critic actively tests per round; the rest are ruled on constraints to save budget.",
  poll: "How often the dashboard refreshes session state, in milliseconds.",
  collapse: "Collapse the contributions / disagreements sections by default once a session has finished.",
  workspace: "The active workspace is the real project directory the council reads and (with approval) writes into. With no workspace active, files go to a throwaway per-session sandbox. New sessions capture whichever workspace is active when they start.",
  cli_models: "The local CLI seats (claude, codex, gemini) and the exact model each one runs. Leave '(CLI default)' to inherit that CLI's own configured model, or pin one from the live catalog. Unchecking a seat drops it from the council and its roles fall back to an enabled OpenRouter model.",
  gemini_key: "Optional. With a key the gemini seat runs through Google's SDK (faster and more reliable on Windows than the headless CLI), the gemini dropdown switches to Google's own model list, image vision works, and web_search gains Google Search grounding. Stored locally in data/secrets.json (gitignored) — never sent anywhere but Google.",
  openrouter_key: "Needed only for the pay-per-token OpenRouter seats below. Stored locally in data/secrets.json (gitignored); an OPENROUTER_API_KEY environment variable always wins over the stored value.",
  openrouter_models: "Extra panel seats that run through OpenRouter, one per VENDOR (DeepSeek, z.ai, Alibaba/Qwen, Moonshot/Kimi). Each offers that vendor's live models in a dropdown — 👁 vision, 🧠 reasoning, 🔧 tools/coding, newest first — or pick 'custom slug…' to paste an exact id. Enable a seat and it joins every panel round and becomes selectable in Role mapping. Billed per token by OpenRouter.",
  governance: "Safety and composer tunables: the risk gate that pauses risky tasks for your approval, plus how the final answer is composed and how much budget the critic gets.",
  ui_prefs: "Dashboard-only preferences — they change what you see, never how the council deliberates.",
};
const tip = k => TIPS[k] ? `<i class="tip" title="${esc(TIPS[k])}">i</i>` : "";
const lbl = (text, k) => `<span class="lbl">${esc(text)}${tip(k)}</span>`;

// Every input/select in the settings gets its own color from this cycling
// palette (restarting per section), so adjacent rows are easy to tell apart:
// a colored border plus a matching bar on the input's left edge.
const FIELD_COLORS = ["#58a6ff", "#3fb950", "#e3b341", "#f0883e", "#db61a2",
                      "#bc8cff", "#2dd4bf", "#f85149", "#79c0ff", "#7ee787",
                      "#ffa657", "#d2a8ff"];
function colorizeSettingsFields() {
  document.querySelectorAll("#settingsBody .sset").forEach(sset => {
    sset.querySelectorAll("input[type=text],input[type=number],input[type=password],select")
        .forEach((el, i) => {
      const c = FIELD_COLORS[i % FIELD_COLORS.length];
      el.style.borderColor = c;
      el.style.boxShadow = `inset 3px 0 0 ${c}`;
    });
  });
}

// Reveal/occlude a stored-key input. The status API only ever ships a masked
// hint, so the box is EMPTY until you type — revealing an empty box would show
// nothing. On reveal with nothing typed, fetch the full stored key on demand;
// on hide, clear it again unless you edited it (so the field returns to the
// "key set — type to replace" hint and the same key isn't accidentally re-saved).
const PW_PROVIDERS = {gem_key: "gemini", or_key: "openrouter"};
async function togglePw(id, btn) {
  const el = document.getElementById(id);
  if (!el) return;
  const show = el.type === "password";
  if (show && !el.value && PW_PROVIDERS[id]) {
    try {
      const r = await api(`/settings/api-keys/${PW_PROVIDERS[id]}/reveal`);
      if (r && r.value) { el.value = r.value; el.dataset.revealed = r.value; }
    } catch (e) { /* no stored key — nothing to reveal */ }
  }
  el.type = show ? "text" : "password";
  btn.textContent = show ? "🙈" : "👁";
  btn.title = show ? "Hide key" : "Show key";
  if (!show && el.dataset.revealed !== undefined) {
    if (el.value === el.dataset.revealed) el.value = "";
    delete el.dataset.revealed;
  }
}

async function openSettings() {
  document.getElementById("settingsOverlay").classList.add("open");
  document.getElementById("savedNote").textContent = "";
  document.getElementById("settingsBody").innerHTML = '<div class="empty">Loading…</div>';
  const [settings, seatsResp, wsResp, keyResp, gemResp] = await Promise.all([
    api("/settings"),
    api("/settings/seats").catch(() => ({seats: []})),
    api("/workspaces").catch(() => ({workspaces: [], active: null})),
    api("/settings/api-keys/openrouter").catch(() => ({present: false})),
    api("/settings/api-keys/gemini").catch(() => ({present: false})),
  ]);
  settingsCache = settings;
  seatsCache = seatsResp.seats || [];
  wsCache = wsResp;
  orKey = keyResp || {present: false};
  gemKey = gemResp || {present: false};
  renderSettings(settings, seatsCache);
}

function closeSettings() {
  document.getElementById("settingsOverlay").classList.remove("open");
}

// Model options for a ROLE's model pin: the catalog of whichever seat the role
// is mapped to. "(seat default)" = no pin (the seat's own pinned model or CLI
// default runs). Rebuilt live when the role's seat dropdown changes.
function roleModelOptions(seatName, selected) {
  const seat = seatsCache.find(x => x.name === seatName);
  let models = [];
  if (seat && seat.kind === "openrouter") {
    models = [...new Set([seat.model_slug, seat.default_slug].filter(Boolean))];
  } else if (seat) {
    models = seat.models || [];
  }
  const opts = ['<option value="">(seat default)</option>'].concat(
    models.map(m => `<option value="${escAttr(m)}" ${m === selected ? "selected" : ""}>${esc(m)}</option>`));
  if (selected && !models.includes(selected)) {
    opts.push(`<option value="${escAttr(selected)}" selected>${esc(selected)} (custom)</option>`);
  }
  opts.push('<option value="__custom__">custom…</option>');
  return opts.join("");
}

// The role's seat changed → repopulate its model-pin dropdown with the NEW
// seat's catalog and drop the old pin (a claude model id must not ride along
// to gemini).
function roleSeatChanged(r) {
  const seatSel = document.getElementById(`role_${r}`);
  const modelSel = document.getElementById(`role_model_${r}`);
  if (!seatSel || !modelSel) return;
  modelSel.innerHTML = roleModelOptions(seatSel.value || modelSel.dataset.resolved || "", "");
  const custom = document.getElementById(`role_model_custom_${r}`);
  if (custom) { custom.style.display = "none"; custom.value = ""; }
}

// ---- OpenRouter vendor model dropdown (generic seat → pick a model) ---------
function orModelBadges(m) {
  return (m.vision ? " 👁" : "") + (m.reasoning ? " 🧠" : "") + (m.tools ? " 🔧" : "");
}
// A vendor's live models (with capability badges) plus "custom slug…". "custom"
// is selected when the current slug isn't one of the live models — a manual slug,
// or the catalog was unreachable (offline / no key).
function orModelOptions(models, current, defaultSlug) {
  models = models || [];
  const known = models.some(m => m.id === current);
  const opts = models.map(m =>
    `<option value="${escAttr(m.id)}" ${m.id === current ? "selected" : ""}>${esc(m.id)}${orModelBadges(m)}${m.ctx ? "  ·  " + Math.round(m.ctx / 1000) + "k" : ""}</option>`);
  opts.push(`<option value="__custom__" ${known ? "" : "selected"}>custom slug…</option>`);
  return opts.join("");
}
// vendor dropdown changed → show the custom-slug box only when "custom…" is picked
function orModelChanged(seat) {
  const sel = document.getElementById("or_model_" + seat);
  const inp = document.getElementById("or_custom_" + seat);
  if (!sel || !inp) return;
  const custom = sel.value === "__custom__";
  inp.style.display = custom ? "" : "none";
  if (custom) inp.focus();
}

function seatOptions(selected) {
  // CLI seats always selectable; OpenRouter seats only once enabled.
  const usable = seatsCache.filter(s => s.kind !== "openrouter" || s.enabled);
  const names = usable.map(s => s.name).filter(Boolean);
  const labelFor = n => {
    const s = seatsCache.find(x => x.name === n);
    return s && s.kind === "openrouter" ? `${n} · OpenRouter` : n;
  };
  const opts = ['<option value="">(backend default)</option>'].concat(
    names.map(n => `<option value="${esc(n)}" ${n === selected ? "selected" : ""}>${esc(labelFor(n))}</option>`)
  );
  // include a current value that isn't in the seat list, so it isn't lost
  if (selected && !names.includes(selected)) {
    opts.push(`<option value="${esc(selected)}" selected>${esc(selected)} (current)</option>`);
  }
  return opts.join("");
}

function renderSettings(s, seats) {
  const c = s.composer || {};
  const ui = s.ui || {};
  const ra = s.role_agents || {};
  const resolved = s.resolved_role_agents || {};
  const roles = roleList(s);
  const roleCatalogWarn = s.role_catalog ? "" :
    '<div class="sub" style="color:var(--bad);margin-bottom:8px">Backend role catalog is missing. Restart Gang of 8 to load the new specialist roles.</div>';
  const seatErr = (seatsCache.length === 0)
    ? '<div class="sub" style="margin-top:4px">no CLI agents found on PATH</div>' : "";

  const wsRows = (wsCache.workspaces || []).map(w => {
    const on = w.id === wsCache.active;
    return `<div class="wsrow ${on ? "active" : ""}">
      <span class="wsname">${esc(w.name)}</span>
      ${on ? '<span class="badge">active ✓</span>'
           : `<button class="ghost" onclick="activateWorkspace('${esc(w.id)}')">Activate</button>`}
      ${on ? '<button class="ghost" onclick="emptyWorkspace()" title="Delete the contents (start fresh)">Empty</button>' : ""}
      <button class="deny" onclick="deleteWorkspace('${esc(w.id)}')">✕</button>
      <span class="wsroot" title="${esc(w.root)}">${esc(w.root)}</span>
    </div>`;
  }).join("");

  document.getElementById("settingsBody").innerHTML = `
    <div class="sset s-ws">
      <label>Workspace ${tip("workspace")}</label>
      ${wsRows || '<div class="sub">no workspaces — add one below</div>'}
      <div class="wsrow ${wsCache.active ? "" : "active"}">
        <span class="wsname">(sandbox)</span>
        ${wsCache.active ? '<button class="ghost" onclick="activateWorkspace(null)">Use this</button>'
                         : '<span class="badge">active ✓</span>'}
        <span class="wsroot" title="per-session throwaway folder">${esc(wsCache.sandbox_root || "")}\\&lt;session&gt;<span class="sub" style="display:block;margin-top:2px">per-session throwaway folder</span></span>
      </div>
      <div class="field" style="margin-top:8px">${lbl("Add workspace", "workspace")}
        <input type="text" id="ws_name" placeholder="name" style="flex:0 0 170px">
        <input type="text" id="ws_root" placeholder="path (or Browse…)">
        <button class="ghost" onclick="openFs()" title="Browse folders">Browse…</button>
        <button onclick="addWorkspace()">Add</button>
      </div>
    </div>

    <div class="sset s-backend">
      <label>Backend ${tip("backend")}</label>
      <div class="field">${lbl("Backend", "backend")}
        <select id="set_backend">
          <option value="mock" ${s.backend === "mock" ? "selected" : ""}>mock (offline)</option>
          <option value="cli" ${s.backend === "cli" ? "selected" : ""}>cli (local CLIs, real files)</option>
        </select></div>
    </div>

    <div class="sset s-models">
      <label>Local CLI models ${tip("cli_models")}</label>
      ${(seats || []).filter(x => x.kind === "cli").map(x => {
        const catalog = x.models || [];
        const isCustom = x.model && !catalog.includes(x.model);
        const _to = (s.cli_timeouts || {})[x.name] || (s.cli_timeout_defaults || {})[x.name] || "";
        const _tod = (s.cli_timeout_defaults || {})[x.name] || "—";
        return `
        <div class="field" style="align-items:center;flex-wrap:wrap">
          <label style="margin:0;flex:0 0 92px;display:flex;align-items:center;gap:6px"
                 title="Uncheck to drop this local CLI seat and run its roles on an OpenRouter model instead.">
            <input type="checkbox" class="cli_enable" data-seat="${esc(x.name)}" ${x.enabled === false ? "" : "checked"}>
            <b>${esc(x.name)}</b></label>
          <select class="cli_model_sel" data-seat="${esc(x.name)}" style="flex:1;min-width:240px"
                  title="Pin the exact model this seat runs (passed as --model / -m / SDK model)."
                  onchange="this.nextElementSibling.style.display = this.value === '__custom__' ? '' : 'none'">
            <option value="">(CLI default${x.name === "gemini" ? " · SDK: gemini-2.5-flash" : ""})</option>
            ${catalog.map(m => `<option value="${escAttr(m)}" ${x.model === m ? "selected" : ""}>${esc(m)}</option>`).join("")}
            ${isCustom ? `<option value="${escAttr(x.model)}" selected>${esc(x.model)} (custom)</option>` : ""}
            <option value="__custom__">custom…</option>
          </select><input type="text" class="cli_model_custom mono" data-seat="${esc(x.name)}"
                 spellcheck="false" placeholder="exact model id" style="flex:1;min-width:220px;display:none">
          <label class="sub" style="flex:0 0 auto;display:flex;align-items:center;gap:5px;margin:0"
                 title="Call timeout for this seat, in seconds (built-in default ${_tod}s). Raise it so a thorough seat isn't dropped mid-work; heavy authoring keeps a built-in minimum.">⏱
            <input type="number" class="cli_timeout" data-seat="${esc(x.name)}" min="30" max="3600" step="10"
                   value="${escAttr(String(_to))}" placeholder="${escAttr(String(_tod))}" style="width:74px">s</label>
        </div>`;}).join("")}
      <div class="field" style="justify-content:flex-end">
        <button class="ghost" onclick="refreshModelCatalog(this)">↻ refresh model list</button>
      </div>
      <div class="sub">Fetched live from the public model catalog — <b>no API key needed</b> (newest first; a model released yesterday shows up here). A Gemini key below upgrades the gemini list to Google's own authoritative catalog. "custom…" takes any id; default = whatever that CLI is configured to use. Every contribution shows the model that actually produced it.</div>
      <div class="sub"><b>⏱ timeout</b> is each seat's per-call budget in seconds (built-in defaults: claude 240 · codex 300 · gemini 150). Raise it so a thorough seat (e.g. claude on opus) isn't dropped mid-work; lower it to fail faster on a stall. Heavy authoring (lead / panel / codifier) keeps its own built-in minimum, so a small value here can't starve it.</div>
      <div class="sub">Uncheck a seat to run <b>OpenRouter-only</b>: its roles fall back to an enabled OpenRouter model (below), and it leaves the panel. Needs an OpenRouter key and at least one enabled OpenRouter seat — otherwise the seat stays on so the council always has a lead.</div>
    </div>

    <div class="sset s-gemkey">
      <label>Gemini API key (optional) ${tip("gemini_key")}</label>
      <div class="field">
        <span class="pw">
          <input type="password" id="gem_key" autocomplete="off" placeholder="${gemKey.present ? "key set (" + esc(gemKey.masked || "••••") + ") — type to replace" : "AIza… paste a Google AI Studio key (optional)"}">
          <button type="button" class="eye" title="Show key" onclick="togglePw('gem_key', this)">👁</button>
        </span>
        <button onclick="saveGeminiKey()">Save key</button>
        ${gemKey.present ? '<button class="ghost" onclick="clearGeminiKey()">Clear</button>' : ""}
      </div>
      <div class="sub">${gemKey.present
        ? (gemKey.source === "env" ? "Using GEMINI_API_KEY/GOOGLE_API_KEY from your environment." : "Stored locally in data/secrets.json (gitignored).")
        : "NOT required — without it the gemini seat runs via its CLI and the model list uses the public catalog. With it: gemini runs through Google's SDK (faster, more reliable on Windows), the dropdown shows Google's own model list, image vision works, and web_search gains Google Search grounding. Free at aistudio.google.com."}</div>
    </div>

    <div class="sset s-orkey">
      <label>OpenRouter API key ${tip("openrouter_key")}</label>
      <div class="field">
        <span class="pw">
          <input type="password" id="or_key" autocomplete="off" placeholder="${orKey.present ? "key set (" + esc(orKey.masked || "••••") + ") — type to replace" : "sk-or-… paste your OpenRouter key"}">
          <button type="button" class="eye" title="Show key" onclick="togglePw('or_key', this)">👁</button>
        </span>
        <button onclick="saveOpenrouterKey()">Save key</button>
        ${orKey.present ? '<button class="ghost" onclick="clearOpenrouterKey()">Clear</button>' : ""}
      </div>
      <div class="sub">${orKey.present
        ? (orKey.source === "env" ? "Using OPENROUTER_API_KEY from your environment." : "Stored locally in data/secrets.json (gitignored).")
        : "Needed for the OpenRouter models below — get one at openrouter.ai/keys."}</div>
    </div>

    <div class="sset s-ormodels">
      <label>OpenRouter models (pay-per-token) ${tip("openrouter_models")}</label>
      ${(seats || []).filter(s => s.kind === "openrouter").map(s => {
        const known = (s.models || []).some(m => m.id === s.model_slug);
        return `
        <div class="field" style="align-items:center;flex-wrap:wrap;gap:8px">
          <label style="display:flex;gap:8px;align-items:center;cursor:pointer;margin:0;flex:0 0 auto" title="OpenRouter vendor '${esc(s.vendor || s.name)}'">
            <input type="checkbox" class="or_enable" data-seat="${esc(s.name)}" ${s.enabled ? "checked" : ""}>
            <b style="min-width:74px;display:inline-block">${esc(s.label || s.name)}</b>
          </label>
          <select id="or_model_${esc(s.name)}" class="or_model" data-seat="${esc(s.name)}"
                  title="Pick a ${esc(s.label || s.name)} model — 👁 vision · 🧠 reasoning · 🔧 tools/coding — or 'custom slug…' to paste an exact id"
                  onchange="orModelChanged('${esc(s.name)}')" style="flex:1;min-width:300px">
            ${orModelOptions(s.models, s.model_slug, s.default_slug)}</select>
          <input type="text" id="or_custom_${esc(s.name)}" class="or_custom mono" data-seat="${esc(s.name)}" spellcheck="false"
                 value="${escAttr(known ? "" : (s.model_slug || ""))}" placeholder="${escAttr((s.vendor || "provider") + "/model-id")}"
                 style="flex:1;min-width:260px;display:${known ? "none" : ""}">
          <span class="badge" style="color:${s.available ? "var(--accent)" : "var(--dim)"}">${s.available ? "ready ✓" : (s.enabled ? "needs key" : "off")}</span>
        </div>`; }).join("") || '<div class="sub">none configured</div>'}
      <div class="sub">Each vendor offers its live models (👁 vision · 🧠 reasoning · 🔧 tools/coding, newest first) — pick one, or choose <b>custom slug…</b> to paste an exact id from openrouter.ai/models. Enabled seats join every panel round and are selectable in Role mapping. Billed by OpenRouter; data_collection = "deny".</div>
    </div>

    <div class="sset s-roles">
      <label>Role mapping → agent · model ${tip("roles")}</label>
      ${roleCatalogWarn}
      ${roles.map(r => {
        const rm = s.role_models || {};
        const mappedSeat = ra[r] || resolved[r] || "";
        return `
        <div class="field">${lbl(roleLabel(r), r)}
          <select id="role_${r}" style="flex:1" onchange="roleSeatChanged('${r}')">${seatOptions(ra[r] || "")}</select>
          <select id="role_model_${r}" data-resolved="${escAttr(resolved[r] || "")}" style="flex:1.2"
                  title="Model pin for THIS ROLE only — when ${escAttr(roleLabel(r))} runs on its seat, this model is used instead of the seat's default (role pin › seat pin › CLI default). Talents run rarely, so a heavyweight pin here costs a handful of calls, not the whole run."
                  onchange="const c=document.getElementById('role_model_custom_${r}'); if(c) c.style.display = this.value === '__custom__' ? '' : 'none'">
            ${roleModelOptions(mappedSeat, rm[r] || "")}</select>
          <input type="text" id="role_model_custom_${r}" class="mono" spellcheck="false"
                 placeholder="exact model id" style="flex:1;display:none">
          <span class="sub" style="flex:0 0 auto">now: ${esc(resolved[r] || "default")}</span></div>`;
      }).join("")}
      ${seatErr}
      <div class="sub">Each role can pin its own model on top of its seat — e.g. keep the claude seat on sonnet for panel/synthesis but give <b>code generator</b> opus for the few calls it makes when the lead pulls it in. "(seat default)" = the seat's Local-CLI-models pin, else the CLI's own default.</div>
      <div class="sub">The lead stays fast (it only orchestrates — kicking off, feeding the panel, pulling in talents). Set the <b>Summarizer</b> to a strong model: it's the "codifier" that examines and finishes the panel's work — selecting/reviewing/fixing the best-of-N winner on a build, and composing the answer otherwise.</div>
    </div>

    <div class="sset s-gov">
      <label>Governance / composer ${tip("governance")}</label>
      <div class="field">${lbl("Risk boundary", "risk")}
        <select id="set_risk">
          ${["none","low","medium","high"].map(rk =>
            `<option value="${rk}" ${s.risk_boundary === rk ? "selected" : ""}>${rk}</option>`).join("")}
        </select></div>
      <div class="field">${lbl("Prose min chars", "prose")}
        <input type="number" id="set_prose" value="${esc(c.prose_min_chars)}"></div>
      <div class="field">${lbl("Reserved calls", "reserved")}
        <input type="number" id="set_reserved" value="${esc(c.reserved_calls)}"></div>
      <div class="field">${lbl("Max critic tests", "critic_tests")}
        <input type="number" id="set_critic" value="${esc(c.max_critic_tests)}"></div>
      <div class="field">${lbl("Council integration review", "integration_review")}
        <input type="checkbox" id="set_integration_review" ${s.integration_review_enabled ? "checked" : ""}></div>
      <div class="sub">After a best-of-N build vote, the codifier may offer a separately validated merge when candidates have complementary strengths. The voted winner remains the default, and the human chooses whether to use the integration.</div>
    </div>

    <div class="sset s-ui">
      <label>UI preferences ${tip("ui_prefs")}</label>
      <div class="field">${lbl("Poll interval (ms)", "poll")}
        <input type="number" id="set_poll" value="${esc(ui.poll_interval_ms)}"></div>
      <div class="field">${lbl("Collapse finished", "collapse")}
        <input type="checkbox" id="set_collapse" ${ui.collapse_finished ? "checked" : ""}></div>
    </div>`;
  colorizeSettingsFields();
}

function _num(id, fallback) {
  const v = parseInt(document.getElementById(id).value, 10);
  return Number.isFinite(v) ? v : fallback;
}

// One-click preset: enable every OpenRouter seat, turn off the local CLI seats,
// and save. Needs an OpenRouter key — without one the backend keeps the CLIs on
// (a role must always have a seat), so we stop and say so rather than no-op.
async function applyOpenRouterOnly() {
  const note = document.getElementById("savedNote");
  if (!orKey.present) {
    note.style.color = "var(--warn)";
    note.textContent = "Add an OpenRouter API key first — without it the local CLIs stay on so the council keeps a lead.";
    return;
  }
  if (!confirm("Run OpenRouter-only?\n\nThis enables all OpenRouter models and turns OFF the local CLI seats (claude, codex, gemini). Every role moves to an OpenRouter model. You can re-check the CLI seats anytime to undo.")) return;
  document.querySelectorAll(".or_enable").forEach(c => { c.checked = true; });
  document.querySelectorAll(".cli_enable").forEach(c => { c.checked = false; });
  await saveSettings();
}

// The inverse preset: local CLIs back on, all OpenRouter seats off, then save.
async function applyLocalCliOnly() {
  if (!confirm("Use local CLIs only?\n\nThis turns the local CLI seats (claude, codex, gemini) back ON and disables all OpenRouter models. The council runs entirely on the local CLIs.")) return;
  document.querySelectorAll(".cli_enable").forEach(c => { c.checked = true; });
  document.querySelectorAll(".or_enable").forEach(c => { c.checked = false; });
  await saveSettings();
}

async function saveSettings() {
  const note = document.getElementById("savedNote");
  note.style.color = "";
  note.textContent = "saving...";
  const role_agents = {};
  const role_models = {};
  roleList(settingsCache || {}).forEach(r => {
    const v = document.getElementById("role_" + r).value;
    if (v) role_agents[r] = v;  // empty ⇒ backend default
    const msel = document.getElementById("role_model_" + r);
    if (msel) {
      let m = msel.value;
      if (m === "__custom__") {
        const custom = document.getElementById("role_model_custom_" + r);
        m = custom ? custom.value.trim() : "";
      }
      if (m) role_models[r] = m;  // empty ⇒ the seat's own default
    }
  });
  const openrouter_enabled = {};
  document.querySelectorAll(".or_enable").forEach(c => { openrouter_enabled[c.dataset.seat] = c.checked; });
  const openrouter_models = {};
  document.querySelectorAll(".or_model").forEach(sel => {
    let v = sel.value;
    if (v === "__custom__") {
      const c = document.getElementById("or_custom_" + sel.dataset.seat);
      v = c ? c.value.trim() : "";
    }
    if (v) openrouter_models[sel.dataset.seat] = v;
  });
  const cli_models = {};
  document.querySelectorAll(".cli_model_sel").forEach(sel => {
    let v = sel.value;
    if (v === "__custom__") {
      const custom = sel.parentElement.querySelector(".cli_model_custom");
      v = custom ? custom.value.trim() : "";
    }
    if (v) cli_models[sel.dataset.seat] = v;
  });
  const cli_enabled = {};
  document.querySelectorAll(".cli_enable").forEach(c => { cli_enabled[c.dataset.seat] = c.checked; });
  const cli_timeouts = {};
  document.querySelectorAll(".cli_timeout").forEach(i => {
    const v = parseInt(i.value, 10);
    if (Number.isFinite(v) && v > 0) cli_timeouts[i.dataset.seat] = v;
  });
  const patch = {
    backend: document.getElementById("set_backend").value,
    role_agents,
    role_models,
    openrouter_enabled,
    openrouter_models,
    cli_models,
    cli_enabled,
    cli_timeouts,
    integration_review_enabled: document.getElementById("set_integration_review").checked,
    risk_boundary: document.getElementById("set_risk").value,
    composer: {
      prose_min_chars: _num("set_prose", 200),
      reserved_calls: _num("set_reserved", 2),
      max_critic_tests: _num("set_critic", 3),
    },
    ui: {
      poll_interval_ms: _num("set_poll", 3000),
      collapse_finished: document.getElementById("set_collapse").checked,
    },
  };
  let r;
  try {
    r = await fetch("/settings", {method: "PUT",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify(patch)});
  } catch (e) {
    note.style.color = "var(--bad)";
    note.textContent = `save failed: ${e.message || e}`;
    return;
  }
  if (r.ok) {
    note.textContent = "saved ✓";
    settingsCache = await r.json();
    loadHealth();  // header backend label may have changed
    // also commit a workspace folder typed but not yet added — Save should
    // persist it too, not silently drop it (idempotent on the backend).
    const pendingRoot = (document.getElementById("ws_root")?.value || "").trim();
    if (pendingRoot) await addWorkspace();
    // refresh seats so newly-enabled OpenRouter models appear in role dropdowns
    seatsCache = (await api("/settings/seats").catch(() => ({seats: []}))).seats || seatsCache;
    renderSettings(settingsCache, seatsCache);
    note.textContent = "saved ✓";
  } else {
    let detail = "";
    try {
      const body = await r.json();
      detail = body.detail ? String(body.detail) : JSON.stringify(body);
    } catch (_) {
      detail = await r.text().catch(() => "");
    }
    note.style.color = "var(--bad)";
    note.textContent = `save failed (${r.status})${detail ? ": " + detail.slice(0, 180) : ""}`;
  }
}

async function _refreshSeatsKeepChecks() {
  // preserve unsaved enable-checkbox toggles AND model picks across a re-render
  const checked = {}, slugs = {};
  document.querySelectorAll(".or_enable").forEach(c => { checked[c.dataset.seat] = c.checked; });
  document.querySelectorAll(".or_model").forEach(sel => {
    let v = sel.value;
    if (v === "__custom__") { const c = document.getElementById("or_custom_" + sel.dataset.seat); v = c ? c.value : ""; }
    slugs[sel.dataset.seat] = v;
  });
  seatsCache = (await api("/settings/seats").catch(() => ({seats: []}))).seats || seatsCache;
  seatsCache.forEach(s => {
    if (s.name in checked) s.enabled = checked[s.name];
    if (s.name in slugs && (slugs[s.name] || "").trim()) s.model_slug = slugs[s.name];
  });
  renderSettings(settingsCache, seatsCache);
}

async function saveOpenrouterKey() {
  const note = document.getElementById("savedNote");
  const v = (document.getElementById("or_key")?.value || "").trim();
  if (!v) { note.textContent = "paste an OpenRouter key first"; return; }
  const r = await fetch("/settings/api-keys/openrouter", {method: "PUT",
    headers: {"Content-Type": "application/json"}, body: JSON.stringify({value: v})});
  if (r.ok) { orKey = await r.json(); note.textContent = "OpenRouter key saved ✓"; await _refreshSeatsKeepChecks(); }
  else { note.style.color = "var(--bad)"; note.textContent = "could not save key"; }
}

async function clearOpenrouterKey() {
  const note = document.getElementById("savedNote");
  if (!confirm("Remove the stored OpenRouter key?")) return;
  const r = await fetch("/settings/api-keys/openrouter", {method: "DELETE"});
  if (r.ok) { orKey = await r.json(); note.textContent = "OpenRouter key cleared"; await _refreshSeatsKeepChecks(); }
}

async function saveGeminiKey() {
  const note = document.getElementById("savedNote");
  const v = (document.getElementById("gem_key")?.value || "").trim();
  if (!v) { note.textContent = "paste a Gemini key first"; return; }
  const r = await fetch("/settings/api-keys/gemini", {method: "PUT",
    headers: {"Content-Type": "application/json"}, body: JSON.stringify({value: v})});
  if (r.ok) {
    gemKey = await r.json(); note.textContent = "Gemini key saved ✓";
    await refreshModelCatalog(null);  // the dropdown can now use Google's own list
  } else { note.style.color = "var(--bad)"; note.textContent = "could not save key"; }
}

async function clearGeminiKey() {
  const note = document.getElementById("savedNote");
  if (!confirm("Remove the stored Gemini key?")) return;
  const r = await fetch("/settings/api-keys/gemini", {method: "DELETE"});
  if (r.ok) { gemKey = await r.json(); note.textContent = "Gemini key cleared"; await refreshModelCatalog(null); }
}

function _renderWsAfterChange(msg) {
  loadWorkspace();                            // update the header indicator
  renderSettings(settingsCache, seatsCache);  // re-render the panel from wsCache
  if (msg) document.getElementById("savedNote").textContent = msg;
}

async function activateWorkspace(id) {
  const r = await fetch("/workspaces/active", {method: "PUT",
    headers: {"Content-Type": "application/json"}, body: JSON.stringify({id})});
  if (r.ok) { wsCache = await r.json(); _renderWsAfterChange("workspace updated ✓"); }
}

// ---- in-page folder browser ----
let fsCurrent = "";  // "" = drive roots

async function refreshModelCatalog(btn) {
  if (btn) btn.textContent = "↻ fetching…";
  try {
    seatsCache = (await api("/settings/seats?refresh=1")).seats || seatsCache;
    renderSettings(settingsCache, seatsCache);
  } catch (e) {
    if (btn) btn.textContent = "↻ fetch failed — retry";
  }
}
const baseName = p => (p || "").replace(/[\\/]+$/, "").split(/[\\/]/).filter(Boolean).pop() || p;
const isDrive = p => /^[a-z]:\\?$/i.test(p);

async function openFs() {
  document.getElementById("fsOverlay").classList.add("open");
  await fsLoadNav();
  const cur = document.getElementById("ws_root").value.trim();
  // open at the current path, else Desktop, else Home, else drives
  const start = cur || (fsShortcuts.find(s => s.label === "Desktop") || fsShortcuts.find(s => s.label === "Home") || {}).path || "";
  fsNavigate(start);
}
function closeFs() { document.getElementById("fsOverlay").classList.remove("open"); }

let fsShortcuts = [];
async function fsLoadNav() {
  if (!fsShortcuts.length)
    fsShortcuts = (await api("/fs/shortcuts").catch(() => ({shortcuts: []}))).shortcuts || [];
  const nav = document.getElementById("fsNav");
  nav.innerHTML = '<div class="fsnav sec">Quick access</div>' + fsShortcuts.map(s =>
    `<div class="fsnav" data-path="${escAttr(s.path)}"><span>${s.icon}</span> ${esc(s.label)}</div>`).join("");
  nav.onclick = e => { const n = e.target.closest(".fsnav[data-path]"); if (n) fsNavigate(n.dataset.path); };
}

async function fsNavigate(path) {
  const list = document.getElementById("fsList");
  list.innerHTML = '<div class="sub" style="padding:12px">loading…</div>';
  const d = await api("/fs/list?path=" + encodeURIComponent(path || ""));
  fsCurrent = d.path || "";
  document.getElementById("fsInput").value = fsCurrent;
  document.getElementById("fsSel").textContent = fsCurrent ? "Selected: " + fsCurrent : "Select a folder";
  fsCrumbs(fsCurrent);
  let rows = "";
  if (d.parent !== null && d.parent !== undefined)
    rows += `<div class="fsrow" data-path="${escAttr(d.parent)}"><span class="ic">⬆</span> ..</div>`;
  rows += (d.dirs || []).map(p =>
    `<div class="fsrow" data-path="${escAttr(p)}"><span class="ic">${isDrive(p) ? "💾" : "📁"}</span> ${esc(isDrive(p) ? p : baseName(p))}</div>`
  ).join("");
  list.innerHTML = rows || `<div class="sub" style="padding:12px">${esc(d.error || "(no sub-folders)")}</div>`;
  list.onclick = e => { const r = e.target.closest(".fsrow"); if (r) fsNavigate(r.dataset.path); };
}

function fsCrumbs(path) {
  const el = document.getElementById("fsCrumbs");
  let html = `<span class="cr" data-p="">Drives</span>`;
  let acc = "";
  (path || "").split(/[\\/]+/).filter(Boolean).forEach((seg, i) => {
    acc = i === 0 ? seg + "\\" : acc.replace(/\\?$/, "\\") + seg;
    html += `<span class="sep">›</span><span class="cr" data-p="${escAttr(acc)}">${esc(seg)}</span>`;
  });
  el.innerHTML = html;
  el.querySelectorAll(".cr").forEach(c => c.onclick = () => fsNavigate(c.dataset.p));
}

async function fsNewFolder() {
  if (!fsCurrent) { document.getElementById("fsSel").textContent = "open a folder first"; return; }
  const name = prompt("New folder name in:\n" + fsCurrent);
  if (!name) return;
  const d = await fetch("/fs/mkdir", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({path: fsCurrent, name})}).then(r => r.json());
  if (d.error) document.getElementById("fsSel").textContent = "error: " + d.error;
  else fsNavigate(fsCurrent + "\\" + name);  // jump into the new folder
}

function useFsFolder() {
  if (!fsCurrent) { document.getElementById("fsSel").textContent = "open a folder first"; return; }
  document.getElementById("ws_root").value = fsCurrent;
  if (!document.getElementById("ws_name").value.trim())
    document.getElementById("ws_name").value = baseName(fsCurrent);
  closeFs();
  addWorkspace();  // choosing a folder saves it immediately (no separate step)
}

async function addWorkspace() {
  const root = document.getElementById("ws_root").value.trim();
  const name = document.getElementById("ws_name").value.trim() || baseName(root);
  const note = document.getElementById("savedNote");
  if (!root) { note.style.color = ""; note.textContent = "pick a folder first"; return; }
  const r = await fetch("/workspaces", {method: "POST",
    headers: {"Content-Type": "application/json"}, body: JSON.stringify({name, root})});
  if (!r.ok) {
    const d = await r.json().catch(() => ({})); note.style.color = "var(--bad)";
    note.textContent = "add failed: " + (d.detail || r.status); return;
  }
  // persist AND activate immediately so the folder is the permanent workspace
  const created = await r.json();
  const ar = await fetch("/workspaces/active", {method: "PUT",
    headers: {"Content-Type": "application/json"}, body: JSON.stringify({id: created.id})});
  wsCache = ar.ok ? await ar.json() : await api("/workspaces");
  document.getElementById("ws_name").value = "";
  document.getElementById("ws_root").value = "";
  note.style.color = "";
  _renderWsAfterChange("workspace saved ✓");
}

async function deleteWorkspace(id) {
  const r = await fetch("/workspaces/" + encodeURIComponent(id), {method: "DELETE"});
  if (r.ok) { wsCache = await r.json(); _renderWsAfterChange("workspace removed"); }
}

async function emptyWorkspace() {
  if (!confirm("Delete ALL contents of the active workspace? This cannot be undone. (Your established folder is not touched.)")) return;
  const r = await fetch("/workspaces/empty", {method: "POST"});
  if (r.ok) { const d = await r.json(); _renderWsAfterChange(`workspace emptied (${d.removed} item${d.removed===1?"":"s"})`); }
  else { _renderWsAfterChange("could not empty workspace"); }
}

loadHealth();
loadWorkspace();
bindComposerControls();
bindComposerDrag();
// deep link: /#<session_id> re-opens that session; otherwise the hero greets
if (location.hash.length > 1) select(decodeURIComponent(location.hash.slice(1)));
else renderEmptyHero();
pollLoop();
setInterval(tickElapsed, 1000);
// when a task is submitted or an action resolved, jump back to fast polling
document.addEventListener("visibilitychange", () => { if (!document.hidden) pollLoop(); });
