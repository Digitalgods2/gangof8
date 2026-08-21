let current = null;
let liveKey = null, liveSince = 0;  // drives the ticking elapsed timer
let uiPreferences = {poll_interval_ms: 3000, collapse_finished: true};
const detailRefreshGate = createLatestRequestGate();
let pendingSubmission = null;
let submissionBusy = false;
let playbooksCache = [];
let artifactCache = {};
let commandCache = {};
let evaluationCache = {};
// Expand/collapse state for rollup sections, keyed by stable id (e.g.
// "sec_contributions"). Tracked here rather than read off the DOM because
// renderDetail() rebuilds #right.innerHTML every 3s, which would otherwise
// snap every collapsible shut. The header click handler toggles this map and
// re-renders, so open/closed state survives each poll deterministically.
let openSections = {};

function shortDuration(ms) {
  const seconds = Math.max(0, Math.round((Number(ms) || 0) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  return remainder ? `${minutes}m ${remainder}s` : `${minutes}m`;
}

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
let headerSeatSaveBusy = false;

function headerSeatStatus(message, bad = false) {
  const status = document.getElementById("aiSeatToggleStatus");
  if (!status) return;
  status.textContent = message || "";
  status.title = message || "";
  status.classList.toggle("error", !!bad);
}

function renderHeaderSeatToggles(settings, busy = headerSeatSaveBusy) {
  const group = document.getElementById("aiSeatToggles");
  if (!group || !settings) return;
  group.setAttribute("aria-busy", busy ? "true" : "false");
  group.querySelectorAll("input[data-settings-map][data-seat]").forEach(input => {
    const mapName = input.dataset.settingsMap;
    const values = settings[mapName] || {};
    const enabled = mapName === "cli_enabled"
      ? values[input.dataset.seat] !== false
      : values[input.dataset.seat] === true;
    input.checked = enabled;
    input.disabled = !!busy;
    input.closest("label")?.classList.toggle("on", enabled);
  });
}

async function _settingsResponseError(response) {
  let detail = "";
  try {
    const body = await response.json();
    detail = body.detail ? String(body.detail) : JSON.stringify(body);
  } catch (_) {
    detail = await response.text().catch(() => "");
  }
  return detail || `HTTP ${response.status}`;
}

async function _freshSettings() {
  const response = await fetch("/settings", {cache: "no-store"});
  if (!response.ok) throw new Error(await _settingsResponseError(response));
  return response.json();
}

function _syncSettingsPanelSeat(mapName, seat, enabled) {
  const className = mapName === "cli_enabled" ? "cli_enable" : "or_enable";
  const panelInput = document.querySelector(`.${className}[data-seat="${seat}"]`);
  if (panelInput) panelInput.checked = enabled;
}

async function toggleHeaderSeat(input) {
  if (!input || headerSeatSaveBusy) return;
  const desired = input.checked;
  const previous = !desired;
  const mapName = input.dataset.settingsMap;
  const seat = input.dataset.seat;
  const brand = input.closest("label")?.textContent.trim() || seat;
  let latest = null;
  headerSeatSaveBusy = true;
  const optimistic = Object.assign(
    {}, settingsCache || {}, seatSettingsPatch(settingsCache || {}, mapName, seat, desired)
  );
  renderHeaderSeatToggles(optimistic, true);
  headerSeatStatus("saving…");
  try {
    // Fetch immediately before writing: the server replaces settings maps, so
    // the patch must merge this one checkbox into the complete current map.
    latest = await _freshSettings();
    const patch = seatSettingsPatch(latest, mapName, seat, desired);
    const response = await fetch("/settings", {
      method: "PUT",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(patch),
    });
    if (!response.ok) throw new Error(await _settingsResponseError(response));
    settingsCache = await response.json();
    applyUiPreferences(settingsCache.ui);
    const savedMap = settingsCache[mapName] || {};
    const savedEnabled = mapName === "cli_enabled"
      ? savedMap[seat] !== false : savedMap[seat] === true;
    _syncSettingsPanelSeat(mapName, seat, savedEnabled);
    headerSeatStatus("saved ✓");
  } catch (error) {
    if (latest) settingsCache = latest;
    else input.checked = previous;
    headerSeatStatus(`Could not update ${brand}: ${error.message || error}`, true);
  } finally {
    headerSeatSaveBusy = false;
    renderHeaderSeatToggles(settingsCache || latest || {}, false);
  }
}

function bindHeaderSeatToggles() {
  document.querySelectorAll("#aiSeatToggles input[data-settings-map][data-seat]")
    .forEach(input => input.addEventListener("change", () => void toggleHeaderSeat(input)));
}

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
  const profile = document.getElementById("executionProfile");
  if (profile) profile.value = "auto";
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
  const profile = document.getElementById("executionProfile");
  const profileHint = document.getElementById("profileHint");
  const hints = {
    auto: "Gang of 8 will explain its routing choice before starting.",
    focused: "Uses the smallest capable group for a fast, direct result. Cheapest route, and the right default for building one thing.",
    council: "Invites broad critique and synthesis across the council.",
    best_of_n: "MOST EXPENSIVE ROUTE. Every enabled seat writes a COMPLETE solution and all but one are discarded, so cost scales with the number of seats and the run is the slowest of any route. Candidates are compared anonymously and the strongest ships. Worth it when you want competing whole answers to choose between — not when you want one thing built.",
    build_team: "Creates owned packages. Independent packages run in parallel; an atomic deliverable may use one owner plus council review.",
  };
  const updateHint = () => {
    if (profileHint && profile) profileHint.textContent = hints[profile.value] || hints.auto;
  };
  // Council and Best-of-all compare whole answers across seats, which costs a
  // multiple of the other routes and discards all but one result. They stay
  // available, but behind a deliberate opt-in rather than sitting in the list
  // as peers of the route you almost always want.
  const ADV_KEY = "gangof8.advancedRoutes";
  const advBox = document.getElementById("advancedRoutes");
  const advOpts = profile ? [...profile.querySelectorAll("option[data-advanced]")] : [];
  const applyAdvanced = () => {
    const on = !!advBox?.checked;
    advOpts.forEach(o => { o.hidden = !on; o.disabled = !on; });
    if (!on && profile && advOpts.some(o => o.value === profile.value)) {
      profile.value = "auto";   // never leave a hidden route selected
      updateHint();
    }
  };
  if (advBox) {
    try { advBox.checked = localStorage.getItem(ADV_KEY) === "1"; } catch (e) { /* private mode */ }
    advBox.addEventListener("change", () => {
      try { localStorage.setItem(ADV_KEY, advBox.checked ? "1" : "0"); } catch (e) { /* ignore */ }
      applyAdvanced();
    });
  }
  applyAdvanced();
  profile?.addEventListener("change", updateHint);
  updateHint();
}

// ---- floatable composer: drag it by the terminal bar out of the way of
// on-screen text; position survives reloads; double-click the bar to re-dock ----
const COMPOSER_POS_KEY = "g8ComposerPos";
function _savedComposerPos() {
  try {
    const saved = JSON.parse(localStorage.getItem(COMPOSER_POS_KEY));
    return Number.isFinite(saved?.x) && Number.isFinite(saved?.y) ? saved : null;
  }
  catch { return null; }
}
function _composerViewport() {
  const viewport = window.visualViewport;
  return {
    left: viewport?.offsetLeft || 0,
    top: viewport?.offsetTop || 0,
    width: viewport?.width || window.innerWidth,
    height: viewport?.height || window.innerHeight,
  };
}
function _placeComposer(box, bar, x, y) {
  // partial off-screen is allowed (that's how you shove it out of the way),
  // but at least 140px of the drag bar stays reachable, and the bar can never
  // leave the viewport vertically — a lost composer is unrecoverable
  const viewport = _composerViewport();
  const w = box.offsetWidth, barH = bar.offsetHeight || 34;
  const reachable = Math.min(140, w);
  x = Math.max(
    viewport.left + reachable - w,
    Math.min(x, viewport.left + viewport.width - reachable),
  );
  y = Math.max(
    viewport.top,
    Math.min(y, viewport.top + viewport.height - Math.min(barH, viewport.height)),
  );
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
  const reclampSavedPosition = () => {
    const p = _savedComposerPos();
    if (!p) return;
    _placeComposer(box, bar, p.x, p.y);
    const r = box.getBoundingClientRect();
    localStorage.setItem(
      COMPOSER_POS_KEY,
      JSON.stringify({x: r.left, y: r.top}),
    );
  };
  window.addEventListener("resize", reclampSavedPosition);
  window.visualViewport?.addEventListener("resize", reclampSavedPosition);
  window.visualViewport?.addEventListener("scroll", reclampSavedPosition);
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
  const d = await r.json().catch(() => ({}));
  if (r.ok) {
    // Actionable follow-ups run in a clean child session. Follow the returned
    // id so the operator sees the new response while the completed result
    // remains immutable.
    current = d.session_id || current;
    if (ta) ta.value = "";
    fuAttachments = [];
    renderFuAttachments();
    _lastDetailSig = "";
    if (d.acknowledged) {
      const note = document.getElementById("fuNote");
      if (note) {
        note.style.color = "var(--ok)";
        note.textContent = d.message || "Acknowledged; the completed session remains closed.";
      }
      return;
    }
    pollLoop();
  }
  else { alert("could not continue: " + (d.detail || r.status)); }
}
function followupKey(e) {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendFollowup(current); }
}

// What the composer's cancel button acts on this poll cycle: the open running
// session, or (with none open) the live goal itself. Set in refresh().
let _cancelTarget = null;
const _slowCallSnoozes = new Map();

function keepWaitingForCall(callId, checkinSeconds) {
  const seconds = Math.max(30, Number(checkinSeconds) || 300);
  _slowCallSnoozes.set(callId, Date.now() + seconds * 1000);
  _lastDetailSig = "";
  _refreshDetail();
}

async function stopAgentCall(sessionId, callId, agent) {
  if (!confirm(`Stop only ${agent || "this model"}?\n\nOther seats and the run will continue.`)) return;
  const response = await fetch(
    `/sessions/${encodeURIComponent(sessionId)}/calls/${encodeURIComponent(callId)}/stop`,
    {method: "POST"},
  );
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    alert(body.detail || `Could not stop ${agent || "the model"} (${response.status})`);
    return;
  }
  _slowCallSnoozes.delete(callId);
  _lastDetailSig = "";
  pollLoop();
}

function keepWaitingForGoalCall(callId, checkinSeconds) {
  const seconds = Math.max(30, Number(checkinSeconds) || 300);
  _slowCallSnoozes.set(callId, Date.now() + seconds * 1000);
  _lastGoalsSig = "";
  pollLoop();
}

async function stopGoalAgentCall(goalId, callId, agent, ev) {
  if (ev) ev.stopPropagation();
  if (!confirm(`Stop only ${agent || "the planning model"}?\n\nThe goal will pause so you can resume planning later.`)) return;
  const response = await fetch(
    `/goals/${encodeURIComponent(goalId)}/calls/${encodeURIComponent(callId)}/stop`,
    {method: "POST"},
  );
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    alert(body.detail || `Could not stop ${agent || "the planning model"} (${response.status})`);
    return;
  }
  _slowCallSnoozes.delete(callId);
  _lastGoalsSig = "";
  pollLoop();
}

async function cancelCurrent() {
  if (_cancelTarget && _cancelTarget.kind === "goal") {
    return cancelGoal(_cancelTarget.id);
  }
  if (!current) return;
  if (!confirm("Cancel this run? Active model calls will be stopped.")) return;
  detailRefreshGate.invalidate(); _lastDetailSig = "";
  const note = document.getElementById("submitNote");
  const r = await fetch(`/sessions/${current}/cancel`, {method: "POST"});
  if (r.ok) { const d = await r.json(); note.style.color = ""; note.textContent = d.status === "cancelled" ? "cancelled" : "cancelling…"; pollLoop(); }
  else { note.style.color = "var(--bad)"; note.textContent = "cancel failed"; }
}

async function submitTaskLegacy() {
  const box = document.getElementById("task");
  const text = box.value.trim();
  const note = document.getElementById("submitNote");
  if (!text && !attachments.length) { note.textContent = "type a task or attach a file"; return; }
  if (!text) { note.textContent = "add a short instruction for the attachment(s)"; return; }
  // /goal <big objective> — architect assigns owned build packages; outputs
  // accumulate in private staging and cross into the project as one final batch.
  if (/^\/goal\b/i.test(text)) {
    const goalText = text.replace(/^\/goal\b/i, "").trim();
    if (!goalText) {
      note.textContent = "usage: /goal <a big objective — the council gets owned build packages and one final release>";
      return;
    }
    const g = await api("/goals", {method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({text: goalText, background: true})});
    note.style.color = ""; note.textContent = "goal " + g.goal_id + " opened — assigning build packages…";
    _followGoal = g.goal_id; _planShownFor = "";  // main pane tracks this goal
    current = null;
    history.replaceState(null, "", location.pathname + location.search);
    clearComposer();
    pollLoop();  // planning + milestone 1 just started → fast polling
    return;
  }
  const r = await api("/tasks", {method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify({text, source:"dashboard", background:true,
                          attachments: attachments.map(a => a.id)})});
  if (r.goal_id) {
    note.style.color = "";
    note.textContent = "substantial build auto-routed to goal " + r.goal_id
      + " — planning owned packages; independent packages can run in parallel…";
    _followGoal = r.goal_id; _planShownFor = "";
    current = null;
    history.replaceState(null, "", location.pathname + location.search);
    clearComposer();
    pollLoop();
    return;
  }
  note.style.color = ""; note.textContent = "submitted " + r.session_id;
  current = r.session_id;
  clearComposer();
  pollLoop();  // a new run just started → fast polling
}

// True when the user currently has text selected inside `el` — we must NOT
// clobber its innerHTML while they're selecting/copying.
function _lines(value) {
  if (Array.isArray(value)) return value.map(x => String(x).trim()).filter(Boolean);
  return String(value || "").split(/\r?\n/)
    .map(x => x.replace(/^\s*[-*]\s*/, "").trim()).filter(Boolean);
}

async function workbenchRequest(path, options = {}) {
  const response = await fetch(path, {cache: "no-store", ...options});
  let body = null;
  const type = response.headers.get("content-type") || "";
  if (type.includes("json")) body = await response.json().catch(() => ({}));
  else body = await response.text().catch(() => "");
  if (!response.ok) {
    const detail = body && typeof body === "object" ? body.detail : body;
    throw new Error(detail || `HTTP ${response.status}`);
  }
  return body;
}

function closeContractReview() {
  document.getElementById("contractOverlay")?.classList.remove("open");
  document.getElementById("task")?.focus();
}

const EXECUTION_PROFILE_LABELS = {
  auto: "Auto",
  focused: "Focused",
  council: "Council",
  best_of_n: "Best-of-all",
  build_team: "Planned build",
};
function executionProfileLabel(profile) {
  const value = String(profile || "");
  return EXECUTION_PROFILE_LABELS[value] || value.replaceAll("_", " ");
}

function _recommendedProfile(preview) {
  const route = preview?.routing_decision || preview?.recommendation || {};
  return route.execution_profile || route.recommended_profile || route.profile
    || route.selected_profile || preview?.recommended_profile || "";
}

function useRecommendedProfile(profile) {
  if (!pendingSubmission || !profile) return;
  pendingSubmission.execution_profile = profile;
  const select = document.getElementById("executionProfile");
  if (select) select.value = profile;
  const chosen = document.getElementById("contractChosenProfile");
  if (chosen) chosen.textContent = executionProfileLabel(profile);
  document.querySelectorAll("#contractRoute .route-use").forEach(button => button.remove());
}

function _routeReview(preview, selectedProfile) {
  const route = preview?.routing_decision || preview?.recommendation || {};
  const recommended = _recommendedProfile(preview);
  const chosen = selectedProfile === "auto"
    ? (recommended || route.selected_route || route.route || "auto")
    : selectedProfile;
  const reason = route.reason || route.rationale || route.explanation
    || preview?.rationale || "Matched to the task's scope, risk, and expected deliverables.";
  const alternatives = Array.isArray(route.alternatives) ? route.alternatives : [];
  return `
    <div class="route-review">
      <div class="route-kicker">Recommended execution</div>
      <div class="route-choice"><span id="contractChosenProfile">${esc(executionProfileLabel(chosen))}</span>
        ${recommended && recommended !== selectedProfile && selectedProfile !== "auto"
          ? `<button class="ghost route-use" type="button" onclick="useRecommendedProfile('${escAttr(recommended)}')">Use recommendation</button>` : ""}
      </div>
      <div class="route-reason">${esc(reason)}</div>
      ${alternatives.length ? `<details><summary>Why not the other routes?</summary>
        <div class="route-alternatives">${alternatives.map(item => {
          if (typeof item === "string") return `<div>${esc(item)}</div>`;
          const name = item.execution_profile || item.profile || item.route || item.name || "alternative";
          return `<div><b>${esc(executionProfileLabel(name))}</b>${item.reason ? ` · ${esc(item.reason)}` : ""}</div>`;
        }).join("")}</div></details>` : ""}
    </div>`;
}

function openContractReview(preview) {
  const contract = preview?.outcome_contract || preview?.contract || {};
  if (!pendingSubmission) return;
  pendingSubmission.preview = preview;
  pendingSubmission.contractBase = contract;
  document.getElementById("contractOutcome").value =
    contract.outcome || contract.objective || pendingSubmission.text;
  document.getElementById("contractDeliverables").value =
    _lines(contract.deliverables).join("\n");
  document.getElementById("contractAcceptance").value =
    _lines(contract.acceptance_criteria || contract.acceptance).join("\n");
  document.getElementById("contractConstraints").value =
    _lines(contract.constraints).join("\n");
  document.getElementById("contractExclusions").value =
    _lines(contract.exclusions).join("\n");
  document.getElementById("contractRoute").innerHTML =
    _routeReview(preview, pendingSubmission.execution_profile);
  const note = document.getElementById("contractNote");
  note.style.color = "";
  note.textContent = "This becomes the shared definition of done for every agent.";
  document.getElementById("contractRunBtn").disabled = false;
  document.getElementById("contractOverlay").classList.add("open");
  document.getElementById("contractOutcome").focus();
}

async function dispatchTask(payload) {
  const note = document.getElementById("submitNote");
  try {
    const result = await workbenchRequest("/tasks", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({...payload, background: true}),
    });
    closeContractReview();
    pendingSubmission = null;
    if (result.goal_id) {
      note.style.color = "";
      note.textContent = "planned build " + result.goal_id
        + " opened — assigning owned packages; independent packages can run in parallel…";
      _followGoal = result.goal_id; _planShownFor = "";
      current = null;
      history.replaceState(null, "", location.pathname + location.search);
      clearComposer();
      pollLoop();
      return;
    }
    note.style.color = "";
    note.textContent = payload.execution_profile === "best_of_n"
      ? "best-of-all " + result.session_id
        + " opened — every enabled AI is authoring a complete candidate…"
      : "submitted " + result.session_id;
    current = result.session_id;
    history.replaceState(null, "", "#" + current);
    clearComposer();
    pollLoop();
  } catch (error) {
    note.style.color = "var(--bad)";
    note.textContent = "submission failed: " + (error.message || error);
    const contractNote = document.getElementById("contractNote");
    if (contractNote && document.getElementById("contractOverlay")?.classList.contains("open")) {
      contractNote.style.color = "var(--bad)";
      contractNote.textContent = note.textContent;
    }
  } finally {
    submissionBusy = false;
    const button = document.getElementById("contractRunBtn");
    if (button) button.disabled = false;
  }
}

async function confirmOutcomeContract() {
  if (!pendingSubmission || submissionBusy) return;
  const outcome = document.getElementById("contractOutcome").value.trim();
  if (!outcome) {
    const note = document.getElementById("contractNote");
    note.style.color = "var(--bad)";
    note.textContent = "Give the run a concrete outcome before starting.";
    return;
  }
  submissionBusy = true;
  document.getElementById("contractRunBtn").disabled = true;
  const outcomeContract = {
    ...(pendingSubmission.contractBase || {}),
    outcome,
    deliverables: _lines(document.getElementById("contractDeliverables").value),
    acceptance_criteria: _lines(document.getElementById("contractAcceptance").value),
    constraints: _lines(document.getElementById("contractConstraints").value),
    exclusions: _lines(document.getElementById("contractExclusions").value),
  };
  document.getElementById("contractNote").textContent = "Starting with the reviewed contract…";
  await dispatchTask({...pendingSubmission, preview: undefined, contractBase: undefined,
    outcome_contract: outcomeContract});
}

async function submitTask() {
  if (submissionBusy) return;
  const box = document.getElementById("task");
  let text = box.value.trim();
  const note = document.getElementById("submitNote");
  if (!text && !attachments.length) { note.textContent = "type a task or attach a file"; return; }
  if (!text) { note.textContent = "add a short instruction for the attachment(s)"; return; }
  let executionProfile = document.getElementById("executionProfile")?.value || "auto";
  if (/^\/goal\b/i.test(text)) {
    text = text.replace(/^\/goal\b/i, "").trim();
    if (!text) {
      note.textContent = "usage: /goal <a big objective — the planner creates owned packages and parallelizes independent work>";
      return;
    }
    executionProfile = "build_team";
  }
  const payload = {
    text, source: "dashboard",
    attachments: attachments.map(item => item.id),
    execution_profile: executionProfile,
  };
  pendingSubmission = payload;
  submissionBusy = true;
  note.style.color = "";
  note.textContent = "building an outcome contract and checking the best route…";
  try {
    const preview = await workbenchRequest("/tasks/preview", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
    submissionBusy = false;
    note.textContent = "preflight ready — review the contract before the council starts";
    openContractReview(preview || {});
  } catch (error) {
    // Compatibility fast path: preflight being unavailable must not make the
    // existing task runner unusable.
    note.textContent = "preflight unavailable; starting with your prompt…";
    await dispatchTask(payload);
  }
}

function _hasSelectionIn(el) {
  const sel = window.getSelection && window.getSelection();
  if (!el || !sel || sel.isCollapsed || sel.rangeCount === 0) return false;
  return el.contains(sel.anchorNode) || el.contains(sel.focusNode);
}
let _lastListSig = "", _lastDetailSig = "";

// ---- reusable playbooks + safe cloning --------------------------------------
function _playbookId(playbook) {
  return playbook.playbook_id || playbook.id || "";
}

function _playbookText(playbook) {
  return playbook.description || playbook.task_template || playbook.text || playbook.template?.text
    || playbook.outcome_contract?.outcome || "";
}

function renderPlaybooks() {
  const wrap = document.getElementById("playbooksWrap");
  const target = document.getElementById("playbooks");
  if (!wrap || !target) return;
  wrap.style.display = playbooksCache.length ? "" : "none";
  target.innerHTML = playbooksCache.map(playbook => {
    const id = _playbookId(playbook);
    const profile = playbook.execution_profile || playbook.template?.execution_profile || "auto";
    return `<div class="playbook">
      <div class="playbook-head">
        <b>${esc(playbook.name || "Untitled playbook")}</b>
        <span class="badge">${esc(executionProfileLabel(profile))}</span>
      </div>
      ${_playbookText(playbook) ? `<div class="playbook-text">${esc(_playbookText(playbook))}</div>` : ""}
      <div class="playbook-actions">
        <button type="button" onclick="runPlaybook('${escAttr(id)}')">Run</button>
        <button class="ghost" type="button" onclick="editFromPlaybook('${escAttr(id)}')">Edit first</button>
        <button class="deny" type="button" title="Delete playbook" onclick="deletePlaybook('${escAttr(id)}')">Delete</button>
      </div>
    </div>`;
  }).join("");
}

async function loadPlaybooks(announce = false) {
  try {
    const result = await workbenchRequest("/playbooks");
    playbooksCache = Array.isArray(result) ? result : (result?.playbooks || []);
    renderPlaybooks();
    if (announce) {
      const note = document.getElementById("submitNote");
      note.style.color = "";
      note.textContent = `loaded ${playbooksCache.length} playbook${playbooksCache.length === 1 ? "" : "s"}`;
    }
  } catch (error) {
    // Older servers simply omit this optional area; task submission remains usable.
    playbooksCache = [];
    renderPlaybooks();
    if (announce) alert("Could not load playbooks: " + (error.message || error));
  }
}

function _putTemplateInComposer(template, message = "Template loaded — edit it, then Send.") {
  const source = template?.template || template || {};
  const text = source.task_template || source.text || source.task?.original_text || source.task?.text
    || source.outcome_contract?.outcome || "";
  const box = document.getElementById("task");
  box.value = text;
  autoGrow(box);
  const profile = source.execution_profile || source.profile || "auto";
  const select = document.getElementById("executionProfile");
  if (select && [...select.options].some(option => option.value === profile)) select.value = profile;
  const clonedAttachments = source.attachments || source.attachment_records || [];
  attachments = clonedAttachments.filter(item => item && typeof item === "object" && item.id)
    .map(item => ({id: item.id, name: item.name || "attachment", kind: item.kind || "text"}));
  renderAttachments();
  const note = document.getElementById("submitNote");
  note.style.color = "";
  note.textContent = message;
  box.focus();
}

function editFromPlaybook(id) {
  const playbook = playbooksCache.find(item => _playbookId(item) === id);
  if (playbook) _putTemplateInComposer(playbook);
}

async function runPlaybook(id) {
  const note = document.getElementById("submitNote");
  note.style.color = "";
  note.textContent = "starting playbook…";
  try {
    const result = await workbenchRequest(`/playbooks/${encodeURIComponent(id)}/run`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({background: true}),
    });
    if (result.goal_id) {
      _followGoal = result.goal_id; _planShownFor = ""; current = null;
      history.replaceState(null, "", location.pathname + location.search);
      note.textContent = "playbook started as planned build " + result.goal_id;
    } else if (result.session_id) {
      current = result.session_id;
      history.replaceState(null, "", "#" + current);
      note.textContent = "playbook started " + result.session_id;
    } else {
      _putTemplateInComposer(result, "Playbook loaded — review and Send.");
    }
    pollLoop();
  } catch (error) {
    note.style.color = "var(--bad)";
    note.textContent = "playbook failed: " + (error.message || error);
  }
}

async function deletePlaybook(id) {
  if (!confirm("Delete this playbook? Past runs are not affected.")) return;
  try {
    await workbenchRequest(`/playbooks/${encodeURIComponent(id)}`, {method: "DELETE"});
    await loadPlaybooks();
  } catch (error) {
    alert("Could not delete playbook: " + (error.message || error));
  }
}

async function cloneSession(id) {
  const note = document.getElementById("submitNote");
  try {
    const result = await workbenchRequest(`/sessions/${encodeURIComponent(id)}/clone`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({run: false}),
    });
    _putTemplateInComposer(result, "Session copied into the composer — edit it, then Send.");
  } catch (error) {
    note.style.color = "var(--bad)";
    note.textContent = "could not copy session: " + (error.message || error);
  }
}

async function cloneGoal(id, event) {
  event?.stopPropagation();
  const note = document.getElementById("submitNote");
  try {
    const result = await workbenchRequest(`/goals/${encodeURIComponent(id)}/clone`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({run: false}),
    });
    _putTemplateInComposer(result, "Goal copied into the composer as a build-team draft.");
    const profile = document.getElementById("executionProfile");
    if (profile) profile.value = "build_team";
  } catch (error) {
    note.style.color = "var(--bad)";
    note.textContent = "could not copy goal: " + (error.message || error);
  }
}

async function saveSessionAsPlaybook(id) {
  const session = _sessionsCache.find(item => item.session_id === id);
  const defaultName = (session?.task?.original_text || session?.task?.text
    || session?.task_text || "Reusable run")
    .replace(/\s+/g, " ").slice(0, 52);
  const name = prompt("Name this playbook:", defaultName);
  if (!name?.trim()) return;
  try {
    await workbenchRequest("/playbooks", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name: name.trim(), session_id: id}),
    });
    await loadPlaybooks();
    const note = document.getElementById("submitNote");
    note.style.color = "";
    note.textContent = `saved “${name.trim()}” as a playbook`;
  } catch (error) {
    alert("Could not save playbook: " + (error.message || error));
  }
}

// ---- goals rail: long-horizon objectives above the session list -------------
const GOAL_LIVE = new Set(["planning", "running", "draining", "awaiting_release"]);
const GOAL_FOLLOWUP_LIVE = new Set(["revising", "following_up"]);
function goalIsLive(g) {
  return !!g && (
    GOAL_LIVE.has(g.status)
    || GOAL_FOLLOWUP_LIVE.has(g.display_status)
  );
}
let _goalsCache = [], _lastGoalsSig = "";
let _sessionsCache = [];

// ---- goal following: the MAIN PANE tracks the active goal ------------------
// While a goal is live the right pane shouldn't sit on the empty hero: show a
// planning state until milestone 1's session exists, then open each milestone's
// session as the goal advances. Selecting an unrelated session stops the
// steering; it resumes if you come back to one of the goal's sessions.
let _followGoal = null, _planShownFor = "";

function _goalSessions(g) {
  return goalSessionIds(g, _sessionsCache);
}

function renderPlanningHero(g) {
  if (_planShownFor === g.goal_id) return;  // don't re-clobber every poll
  _planShownFor = g.goal_id;
  document.getElementById("right").innerHTML = `
    <div class="hero-empty">
      <div class="badge-row">
        <span class="bpill"><span class="dot dot-brand"></span>goal ${esc(g.goal_id)}</span>
        <span class="bpill">🧭 assigning build packages…</span>
      </div>
      <h1>Planning the build and<br><span class="grad-text">assigning owned packages.</span></h1>
      <p>The architect is mapping dependencies and interfaces. Ready packages
         start in parallel and their work accumulates in private staging.</p>
    </div>`;
}

function _followActiveGoal() {
  if (!_followGoal) {
    // adopt a live goal when nothing is open, or when the open session is
    // already one of its milestones (e.g. after a page reload mid-goal)
    const live = _goalsCache.find(g => goalIsLive(g) &&
      (!current || _goalSessions(g).includes(current)));
    if (live) _followGoal = live.goal_id;
  }
  if (!_followGoal) return;
  const fg = _goalsCache.find(g => g.goal_id === _followGoal);
  if (!fg || (current && !_goalSessions(fg).includes(current))) {
    _followGoal = null;  // goal gone, or the user moved elsewhere
    return;
  }
  const ms = fg.milestones || [];
  const cur = ms[fg.current_index] || ms[ms.length - 1] || null;
  const latest = fg.actionable_session_id || fg.release_session_id
    || (cur && cur.session_id) || _goalSessions(fg).slice(-1)[0] || null;
  if (latest && latest !== current) {
    detailRefreshGate.invalidate();
    current = latest; _lastDetailSig = ""; _lastListSig = ""; _planShownFor = "";
    if (location.hash !== "#" + latest) history.replaceState(null, "", "#" + latest);
  } else if (!latest && fg.status === "planning" && !current) {
    renderPlanningHero(fg);
  }
  if (!goalIsLive(fg)) _followGoal = null;  // settled — stop steering
}

// One human vocabulary for EVERY status surface (goal pills, session
// pills, release states). The dashboard previously spoke four dialects at
// once — goal status, release_status, session status, now-line — and an
// operator watching an approval could not tell which one was THE status.
// CSS classes stay keyed by the raw state; only the words are unified.
const STATUS_WORDS = {
  received: "queued", classified: "starting", deliberating: "working",
  composing: "finishing", awaiting_approval: "needs your approval",
  awaiting_input: "needs your answer", awaiting_release: "needs your approval",
  awaiting_target: "needs a folder", done: "done", failed: "failed",
  cancelled: "cancelled", running: "working", planning: "planning",
  paused: "paused", draining: "winding down", completed: "done",
  revising: "revising", following_up: "reviewing follow-up",
  not_started: "not released yet", failed_verification: "verification failed",
  released: "released", denied: "release denied",
};
function humanStatus(st) {
  return STATUS_WORDS[st] || String(st || "").replaceAll("_", " ");
}

function goalCard(g) {
  const ms = g.milestones || [];
  const done = ms.filter(m => m.status === "done").length;
  const running = g.active_packages ?? ms.filter(m => m.status === "running").length;
  const owners = new Set(ms.map(m => m.owner).filter(Boolean)).size;
  const contributors = g.artifact_contributor_count ?? g.contributor_count ?? 0;
  const expectedContributors = g.expected_artifact_contributor_count
    ?? g.expected_contributor_count ?? 0;
  const displayStatus = g.display_status || g.status;
  const icons = {done: "✓", running: "▶", pending: "○", failed: "×",
    awaiting_approval: "!", awaiting_input: "?", cancelled: "×", draining: "◌"};
  const rows = ms.map(m => {
    const hard = (m.depends_on || []).filter(i => ms[i] && ms[i].status !== "done");
    const contracts = m.contract_depends_on || [];
    const edge = hard.length
      ? ` · waiting for verified P${hard.map(i => i + 1).join(", P")}`
      : contracts.length ? ` · contract-linked to P${contracts.map(i => i + 1).join(", P")}` : "";
    const rowStatus = m.pending_approvals ? "awaiting_approval"
      : m.pending_inputs ? "awaiting_input" : m.status;
    const attempts = m.attempt_count > 1 ? ` · ${m.attempt_count} attempts` : "";
    const outputAuthors = Object.values(m.output_authors || {});
    const fanout = new Set(outputAuthors).size > 1
      ? ` · ${new Set(outputAuthors).size} exact-output authors` : "";
    const callAttempts = m.agent_call_attempts
      ? ` · ${m.agent_call_attempts} model attempt${m.agent_call_attempts === 1 ? "" : "s"}` : "";
    const working = (m.active_agent_calls || []).length
      ? ` · ${m.active_agent_calls.map(c => {
          const chars = c.progress_chars || 0;
          return `${c.agent} ${chars ? `streaming ${chars.toLocaleString()} chars` : "waiting for output"}`;
        }).join(", ")}` : "";
    const title = `${m.title}${m.owner ? ` — accountable owner ${m.owner}` : ""}${edge}${attempts}${fanout}${callAttempts}${working}`;
    return `
      <div class="gms ${esc(rowStatus)}" title="${esc(title)}"
           ${m.session_id ? `onclick="select('${esc(m.session_id)}')"` : ""}>
        <span class="gicon">${icons[rowStatus] || "○"}</span>
        <span class="gtitle">${esc(m.title)}${m.owner ? ` <small>— ${esc(m.owner)}${esc(edge)}${esc(attempts)}${esc(fanout)}${esc(callAttempts)}${esc(working)}</small>` : ""}</span>
      </div>`;
  }).join("");
  const live = GOAL_LIVE.has(g.status);
  const followupLive = GOAL_FOLLOWUP_LIVE.has(displayStatus);
  let btns =
    (g.status === "paused" ? `<button class="gbtn" onclick="resumeGoal('${esc(g.goal_id)}', event)">Resume</button>` : "") +
    (live || g.status === "paused" ? `<button class="gbtn ghost" onclick="cancelGoal('${esc(g.goal_id)}', event)">Cancel</button>` : "") +
    (followupLive && g.actionable_session_id
      ? `<button class="gbtn ghost" onclick="cancelSession('${esc(g.actionable_session_id)}', event)">Cancel repair</button>`
      : "") +
    (!live && !followupLive && g.status !== "paused" ? `<button class="trash" title="Remove this goal (its sessions stay)" onclick="deleteGoal('${esc(g.goal_id)}', event)">🗑</button>` : "") +
    `<button class="gbtn ghost" title="Full story: timeline + postmortem" onclick="toggleGoalStory('${esc(g.goal_id)}', event)">📜</button>`;
  btns += `<button class="gbtn ghost" title="Copy this goal into the composer" onclick="cloneGoal('${esc(g.goal_id)}', event)">Copy</button>`;
  const aggregate = g.delivery_mode === "final_batch"
    ? `<div class="sub">${g.participation_mode === "full_council" ? "Full Council" : g.participation_mode === "adaptive" ? "Adaptive council" : "Focused build"} · ${contributors}/${expectedContributors || "?"} artifact contributors` +
      ` · ${owners} planned owner${owners === 1 ? "" : "s"} · ${running} active` +
      `${g.active_agent_calls ? ` · ${g.active_agent_calls} model call${g.active_agent_calls === 1 ? "" : "s"}` : ""}` +
      `${g.pending_approvals ? ` · ${g.pending_approvals} approval blocked` : ""}` +
      `${g.pending_inputs ? ` · ${g.pending_inputs} question blocked` : ""}` +
      ` · shared staging · ${esc(humanStatus(g.release_status || "not_started"))}</div>` : "";
  const artifactContributors = new Set(g.artifact_contributors || []);
  const assignmentStatus = new Map();
  ms.flatMap(m => m.collaboration_assignments || []).forEach(a => {
    assignmentStatus.set(a.seat, a.status);
  });
  const resourceRoster = (g.resource_roster || []).map(seat => {
    const status = artifactContributors.has(seat)
      ? "contributed" : (assignmentStatus.get(seat) || "pending");
    return `<span class="resource-chip ${esc(status)}" title="${esc(status.replaceAll("_", " "))}">${esc(seat)} <small>${esc(status === "contributed" ? "✓" : status)}</small></span>`;
  }).join("");
  const resources = resourceRoster
    ? `<div class="resource-roster" aria-label="Collaboration resources">${resourceRoster}</div>` : "";
  const nowMs = Date.now();
  const slowPlanningCalls = (g.planning_agent_calls || []).filter(call => {
    const started = Date.parse(call.started_at || "");
    const checkin = Number(call.operator_checkin_s || 0);
    const snoozedUntil = _slowCallSnoozes.get(call.call_id) || 0;
    return call.operator_stoppable && checkin > 0 && Number.isFinite(started) &&
      nowMs - started >= checkin * 1000 && nowMs >= snoozedUntil;
  });
  const planningCheckin = slowPlanningCalls.length ? `
    <div class="slow-call-checkin" role="status" aria-live="polite">
      <div class="slow-call-title">Long-running planning check-in</div>
      ${slowPlanningCalls.map(call => {
        const elapsedMinutes = Math.max(
          1, Math.floor((nowMs - Date.parse(call.started_at)) / 60000)
        );
        const chars = Number(call.progress_chars || 0);
        const checkin = Number(call.operator_checkin_s || 300);
        return `<div class="slow-call-row">
          <div>
            <b>${esc(call.agent || "model")}</b> has been planning for ${elapsedMinutes} minute${elapsedMinutes === 1 ? "" : "s"}.
            ${chars ? `It is still producing output (${chars.toLocaleString()} characters so far).` : "It has not produced output yet."}
            There is no automatic wall-clock cutoff.
          </div>
          <div class="slow-call-actions">
            <button class="ghost mini" type="button"
                    onclick="keepWaitingForGoalCall('${escAttr(call.call_id)}', ${checkin})">Keep waiting</button>
            <button class="deny mini" type="button"
                    onclick="stopGoalAgentCall('${escAttr(g.goal_id)}', '${escAttr(call.call_id)}', '${escAttr(call.agent || "model")}', event)">Stop planner</button>
            <button class="deny mini" type="button"
                    onclick="cancelGoal('${escAttr(g.goal_id)}', event)">Cancel goal</button>
          </div>
        </div>`;
      }).join("")}
    </div>` : "";
  const spentSeats = Object.entries(g.model_calls_by_seat || {})
    .sort((a, b) => b[1] - a[1]).map(([seat, n]) => `${seat} ${n}`).join(", ");
  const cost = g.model_calls_used
    ? `<span class="gprog" title="model calls spent${spentSeats ? `: ${esc(spentSeats)}` : ""}">` +
      `${g.model_calls_used}${g.call_budget ? `/${g.call_budget}` : ""} calls</span>`
    : "";
  return `
    <div class="goal ${esc(g.status)}">
      <div class="ghead">
        <span class="pill g-${esc(displayStatus)}">${esc(humanStatus(displayStatus))}</span>
        <span class="gprog">${done}/${ms.length || "…"}</span>
        ${cost}
        ${btns}
      </div>
      <div class="text" title="click to expand/collapse"
           onclick="this.classList.toggle('open')">${esc(g.text)}</div>
      ${g.now ? `<div class="gnow">▸ ${esc(g.now)}</div>` : ""}
      ${g.last_error ? `<div class="gerr">${esc(g.last_error)}</div>` : ""}
      ${aggregate}
      ${resources}
      ${planningCheckin}
      ${rows}
      <div class="gstory" id="gstory-${esc(g.goal_id)}" style="display:none"></div>
    </div>`;
}

async function toggleGoalStory(goalId, ev) {
  if (ev) ev.stopPropagation();
  const el = document.getElementById("gstory-" + goalId);
  if (!el) return;
  if (el.style.display !== "none") { el.style.display = "none"; return; }
  el.style.display = "";
  el.innerHTML = `<div class="hint">loading the story…</div>`;
  const d = await api("/goals/" + encodeURIComponent(goalId) + "/timeline").catch(() => null);
  if (!d) { el.innerHTML = `<div class="hint">could not load the timeline</div>`; return; }
  const s = d.summary || {};
  const seats = Object.entries(s.calls_by_seat || {}).map(([k, v]) => `${k} ${v}`).join(", ");
  const at = s.attempts || {};
  const attemptBits = [
    at.completed ? `${at.completed} completed` : "",
    at.seat_outage ? `${at.seat_outage} lost to seat outages` : "",
    at.interrupted ? `${at.interrupted} interrupted` : "",
    at.other_failures ? `${at.other_failures} failed` : "",
  ].filter(Boolean).join(" · ");
  const pkgs = (s.packages || []).map(p =>
    `P${p.package} ${esc(p.title)} — ${esc(p.owner)} (${esc(p.status)}${p.invalidated_attempts ? `, ${p.invalidated_attempts} invalidated attempt${p.invalidated_attempts === 1 ? "" : "s"}` : ""})`
  ).join("<br>");
  const rows = (d.events || []).slice(-120).map(e =>
    `<div class="tlrow"><span class="tlts">${esc((e.ts || "").slice(11, 19))}</span> ${esc(e.icon || "•")} <b>${esc(e.label || e.event)}</b>${e.detail ? ` — ${esc(e.detail)}` : ""}</div>`
  ).join("");
  el.innerHTML = `
    <div class="gsummary">
      <b>${esc(s.status || "")}${s.release_status ? " · " + esc(s.release_status) : ""}</b>
      · ${s.calls_used ?? 0}/${s.call_budget ?? "∞"} calls${seats ? ` (${esc(seats)})` : ""}
      ${attemptBits ? `<br>attempts: ${esc(attemptBits)}` : ""}
      ${pkgs ? `<br>${pkgs}` : ""}
    </div>
    <div class="gtimeline">${rows || '<div class="hint">no events recorded</div>'}</div>`;
}

async function refreshGoals(sessions = null) {
  if (sessions) _sessionsCache = sessions;
  _goalsCache = await api("/goals").catch(() => []) || [];
  const wrap = document.getElementById("goalsWrap");
  const el = document.getElementById("goals");
  if (!wrap || !el) return;
  wrap.style.display = _goalsCache.length ? "" : "none";
  _followActiveGoal();  // steer the main pane before the detail render runs
  const nowMs = Date.now();
  const checkinState = _goalsCache.map(goal =>
    (goal.planning_agent_calls || []).some(call => {
      const started = Date.parse(call.started_at || "");
      const checkin = Number(call.operator_checkin_s || 0);
      const snoozedUntil = _slowCallSnoozes.get(call.call_id) || 0;
      return call.operator_stoppable && checkin > 0 && Number.isFinite(started) &&
        nowMs - started >= checkin * 1000 && nowMs >= snoozedUntil;
    }) ? `${goal.goal_id}:due` : `${goal.goal_id}:waiting`
  ).join("|");
  const sig = JSON.stringify(_goalsCache) + "|checkins:" + checkinState;
  if (sig === _lastGoalsSig || _hasSelectionIn(el)) return;
  _lastGoalsSig = sig;
  el.innerHTML = _goalsCache.map(goalCard).join("");
}

async function resumeGoal(id, ev) {
  ev.stopPropagation();
  const response = await fetch(`/goals/${encodeURIComponent(id)}/resume`, {method: "POST"});
  const resumed = await response.json().catch(() => ({}));
  if (!response.ok) {
    alert("could not resume: " + (resumed.detail || response.status));
    return;
  }
  _followGoal = id;
  _planShownFor = "";
  detailRefreshGate.invalidate();
  _lastDetailSig = "";
  _lastGoalsSig = "";
  // The resume endpoint returns the fresh actionable package session. Select
  // it immediately instead of leaving the failed attempt in the main pane.
  if (resumed.actionable_session_id) {
    current = resumed.actionable_session_id;
    history.replaceState(null, "", "#" + current);
  }
  pollLoop();
}

async function cancelGoal(id, ev) {
  if (ev) ev.stopPropagation();  // no event when called from the composer button
  if (!confirm("Cancel this goal? Every running package will be cancelled too.")) return;
  detailRefreshGate.invalidate(); _lastDetailSig = "";
  await fetch(`/goals/${encodeURIComponent(id)}/cancel`, {method: "POST"});
  _lastGoalsSig = ""; pollLoop();
}

async function cancelSession(id, ev) {
  if (ev) ev.stopPropagation();
  if (!confirm("Cancel this repair run? Active model calls will be stopped.")) return;
  detailRefreshGate.invalidate();
  _lastDetailSig = "";
  await fetch(`/sessions/${encodeURIComponent(id)}/cancel`, {method: "POST"});
  _lastGoalsSig = "";
  pollLoop();
}

async function deleteGoal(id, ev) {
  ev.stopPropagation();
  if (!confirm("Remove this goal? Its milestone sessions stay in the session list.")) return;
  await fetch(`/goals/${encodeURIComponent(id)}`, {method: "DELETE"});
  _lastGoalsSig = ""; pollLoop();
}

function deleteAllHistory() {
  const sessionCount = _sessionsCache.length;
  const goalCount = _goalsCache.length;
  if (!sessionCount && !goalCount) {
    alert("There is no session history to delete.");
    return;
  }
  const summary = document.getElementById("deleteHistorySummary");
  if (summary) {
    summary.textContent =
      `Are you sure you want to delete ${sessionCount} session${sessionCount === 1 ? "" : "s"}` +
      `${goalCount ? ` and ${goalCount} goal${goalCount === 1 ? "" : "s"}` : ""}?`;
  }
  document.getElementById("deleteHistoryOverlay")?.classList.add("open");
}

function closeDeleteHistoryConfirm() {
  document.getElementById("deleteHistoryOverlay")?.classList.remove("open");
}

async function confirmDeleteAllHistory() {
  closeDeleteHistoryConfirm();

  const button = document.getElementById("deleteAllHistory");
  if (button) { button.disabled = true; button.textContent = "Deleting…"; }
  try {
    const response = await fetch("/history", {
      method: "DELETE",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({confirmation: "DELETE ALL"}),
    });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || `HTTP ${response.status}`);

    detailRefreshGate.invalidate();
    current = null; _followGoal = null; _planShownFor = "";
    _sessionsCache = []; _goalsCache = [];
    _lastListSig = ""; _lastDetailSig = ""; _lastGoalsSig = "";
    history.replaceState(null, "", location.pathname + location.search);
    const feed = document.getElementById("feed");
    if (feed) feed.innerHTML = "";
    renderEmptyHero();
    await refresh();
    alert(
      `Deleted ${result.sessions_deleted || 0} session${result.sessions_deleted === 1 ? "" : "s"}` +
      ` and ${result.goals_deleted || 0} goal${result.goals_deleted === 1 ? "" : "s"}.`
    );
  } catch (error) {
    alert("Could not delete the history: " + (error.message || error));
  } finally {
    if (button) { button.textContent = "Del All"; }
  }
}

async function refresh() {
  const sessions = await api("/sessions");
  _sessionsCache = sessions;
  await refreshGoals(sessions);
  const deleteAllButton = document.getElementById("deleteAllHistory");
  if (deleteAllButton) {
    deleteAllButton.disabled = sessions.length === 0 && _goalsCache.length === 0;
  }
  const el = document.getElementById("sessions");
  // Cancel button: a running open session cancels that run; with none open, a
  // LIVE goal (planning, or between milestones) offers cancelling the goal —
  // otherwise the /goal planning window had no cancel anywhere in the composer.
  const curS = sessions.find(s => s.session_id === current);
  const cancelBtn = document.getElementById("cancelBtn");
  if (cancelBtn) {
    const liveGoal = _goalsCache.find(g => GOAL_LIVE.has(g.status));
    const currentGoal = curS && curS.goal_id
      ? _goalsCache.find(g => g.goal_id === curS.goal_id && GOAL_LIVE.has(g.status)) : null;
    if (currentGoal) {
      _cancelTarget = {kind: "goal", id: currentGoal.goal_id};
      cancelBtn.textContent = "Cancel goal";
      cancelBtn.style.display = "inline-block";
    } else if (curS && !TERMINAL_STATES.has(curS.status)) {
      _cancelTarget = {kind: "run"};
      cancelBtn.textContent = "Cancel run";
      cancelBtn.style.display = "inline-block";
    } else if (liveGoal) {
      _cancelTarget = {kind: "goal", id: liveGoal.goal_id};
      cancelBtn.textContent = "Cancel goal";
      cancelBtn.style.display = "inline-block";
    } else {
      _cancelTarget = null;
      cancelBtn.style.display = "none";
    }
  }
  // Only rebuild the list when something actually changed AND no selection is
  // active in it — re-rendering identical HTML every poll wiped selections.
  const visibleGoalIds = new Set(_goalsCache.map(g => g.goal_id));
  // Hide package attempts only while their owning goal card exists. If a goal
  // is removed, its retained sessions become directly accessible again.
  const visibleSessions = sessions.filter(s => !s.goal_id || !visibleGoalIds.has(s.goal_id));
  const listSig = JSON.stringify(visibleSessions.map(s =>
    [s.session_id, s.status, s.pending_approvals || 0, s.pending_inputs || 0,
     s.task_text || "", s.session_id === current]));
  if (listSig === _lastListSig || _hasSelectionIn(el)) {
    if (current) await _refreshDetail();
    return sessions;
  }
  _lastListSig = listSig;
  el.innerHTML = visibleSessions.map(s => `
    <div class="session ${s.session_id===current?"active":""}" onclick="select('${s.session_id}')">
      <button class="trash" title="Delete this session" onclick="deleteSession('${s.session_id}', event)">🗑</button>
      <div class="text">${esc(s.task_text) || "(no text)"}</div>
      <div class="meta">
        <span class="pill ${esc(s.status)}">${esc(humanStatus(s.status))}</span>
        ${s.pending_approvals ? `<span class="pill awaiting_approval">${s.pending_approvals} approval</span>` : ""}
        ${s.pending_inputs ? `<span class="pill awaiting_input">${s.pending_inputs} question</span>` : ""}
        <span>${esc(s.session_id)}</span>
      </div>
    </div>`).join("") || (_goalsCache.length
      ? '<div class="sub" style="padding:8px 4px">Package attempts are grouped under Goals above.</div>'
      : "");
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
// just a timer. Both values stay local until the refresh-order gate accepts
// this request, so obsolete work cannot mutate the visible live card.
let _liveTalent = "";
let _liveFeed = [];  // recent {ts, icon, label, detail} rows for the live card
let _liveEvents = []; // complete live timeline, used for the plain-language status brief

async function _liveActivity(sid) {
  const d = await api(`/sessions/${encodeURIComponent(sid)}/timeline`);
  const events = d.events || [];
  let open = "";
  for (const e of events) {
    if (e.event === "delegation_granted") open = e.detail || "talent";
    else if (["delegation_resolved", "delegation_failed", "delegation_denied",
              "round_synthesized", "final_composed"].includes(e.event)) open = "";
  }
  return {
    talent: open.slice(0, 90),
    feed: events.filter(e => e.event !== "status_change").slice(-8),
    events,
  };
}

async function _refreshDetail() {
  const sid = current;
  if (!sid) return;
  const requestToken = detailRefreshGate.begin();
  const detail = await fetch("/sessions/" + encodeURIComponent(sid), {cache: "no-store"})
    .then(r => r.ok ? r.json() : null);
  if (!detailRefreshGate.isCurrent(requestToken) || current !== sid) return;
  const workingNow = detail &&
    ["received", "classified", "deliberating", "composing"].includes(detail.status);
  let activity = {talent: "", feed: [], events: []};
  if (detail) {
    activity = await _liveActivity(detail.session_id).catch(() => activity);
    if (!detailRefreshGate.isCurrent(requestToken) || current !== sid) return;
  }
  _liveTalent = activity.talent;
  _liveFeed = activity.feed;
  _liveEvents = activity.events;
  const right = document.getElementById("right");
  const feedKey = _liveFeed.length
    ? _liveFeed[_liveFeed.length - 1].ts + ":" + _liveFeed.length : "";
  const parentGoal = detail?.goal_id
    ? _goalsCache.find(goal => goal.goal_id === detail.goal_id) : null;
  const nowMs = Date.now();
  const callCheckinDue = (detail.active_agent_calls || []).some(call => {
    const started = Date.parse(call.started_at || "");
    const checkin = Number(call.operator_checkin_s || 0);
    const snoozedUntil = _slowCallSnoozes.get(call.call_id) || 0;
    return call.operator_stoppable && checkin > 0 && Number.isFinite(started) &&
      nowMs - started >= checkin * 1000 && nowMs >= snoozedUntil;
  });
  const sig = JSON.stringify(detail) + "|goal:" + goalRenderSignature(parentGoal)
    + "|talent:" + _liveTalent + "|feed:" + feedKey
    + "|checkin:" + callCheckinDue;
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
// ---- live activity feed (SSE) + seat health badges ----------------------
const FEED_MAX_ROWS = 60;
let _feedSource = null;
function startFeed() {
  if (_feedSource) return;
  const el = document.getElementById("feed");
  const dot = document.getElementById("feedDot");
  if (!el) return;
  try { _feedSource = new EventSource("/events/stream"); } catch (e) { return; }
  _feedSource.onopen = () => { if (dot) dot.classList.add("on"); };
  _feedSource.onerror = () => {
    if (dot) dot.classList.remove("on");
    // EventSource retries automatically; nothing else to do.
  };
  _feedSource.onmessage = (m) => {
    let e = null;
    try { e = JSON.parse(m.data); } catch (err) { return; }
    const row = document.createElement("div");
    row.className = "feedrow";
    const ts = (e.ts || "").slice(11, 19);
    const target = e.session_id && e.session_id !== "-" ? e.session_id : "";
    row.innerHTML = `<span class="tlts">${esc(ts)}</span> ${esc(e.icon || "•")} ` +
      `<b>${esc(e.label || e.event || "")}</b>` +
      `${e.detail ? ` — ${esc(e.detail)}` : ""}`;
    if (target) {
      row.classList.add("clickable");
      row.title = "open session " + target;
      row.onclick = () => select(target);
    }
    el.prepend(row);
    while (el.childElementCount > FEED_MAX_ROWS) el.removeChild(el.lastChild);
  };
}

async function refreshSeatHealth() {
  const el = document.getElementById("seatHealth");
  if (!el) return;
  const d = await api("/seats").catch(() => null);
  if (!d) return;
  const badge = (seat) => {
    const info = (d.seats || {})[seat] || {};
    const state = info.state || "healthy";
    const cls = state === "healthy" ? "ok"
      : (state === "degraded" || state === "capacity" || state === "timeout") ? "warn" : "bad";
    const label = state === "healthy" ? "" : ` ${state.replaceAll("_", " ")}`;
    const reason = info.reason ? ` — ${info.reason}` : "";
    return `<span class="seatb ${cls}" title="${esc(seat)}: ${esc(state)}${esc(reason)}">${esc(seat)}${esc(label)}</span>`;
  };
  el.innerHTML = (d.panel || []).map(badge).join("");
}

const POLL_ACTIVE_DEFAULT = 3000, POLL_IDLE = 20000, POLL_HIDDEN = 30000;
let _pollTimer = null, _pollGeneration = 0;
function activePollInterval() {
  const n = Number(uiPreferences.poll_interval_ms);
  return Number.isFinite(n) ? Math.max(500, Math.min(60000, n)) : POLL_ACTIVE_DEFAULT;
}
async function pollLoop() {
  const pollGeneration = ++_pollGeneration;
  if (_pollTimer) { clearTimeout(_pollTimer); _pollTimer = null; }
  let delay = POLL_IDLE;
  if (document.hidden) {
    delay = POLL_HIDDEN;  // tab not visible: skip the fetch entirely
  } else {
    const sessions = await refresh().catch(() => []);
    refreshSeatHealth().catch(() => {});
    // A submit/cancel/resolve may start a newer poll while this one awaits the
    // API. Only that newest poll is allowed to own the next timer.
    if (pollGeneration !== _pollGeneration) return;
    const working = (sessions || []).some(s => !TERMINAL_STATES.has(s.status))
      || _goalsCache.some(goalIsLive);
    delay = working ? activePollInterval() : POLL_IDLE;
  }
  _pollTimer = setTimeout(pollLoop, delay);
}

async function deleteSession(id, ev) {
  ev.stopPropagation();  // don't also select the row
  if (!confirm("Delete this session permanently? Its transcript and log will be removed.")) return;
  const r = await fetch("/sessions/" + encodeURIComponent(id), {method: "DELETE"});
  if (r.ok) {
    if (current === id) {
      detailRefreshGate.invalidate();
      current = null;
      history.replaceState(null, "", location.pathname + location.search);
      renderEmptyHero();
    }
    await refresh();
  }
}

function select(id) {
  detailRefreshGate.invalidate();
  current = id; _lastDetailSig = ""; _lastListSig = "";
  // deep link: keep the open session in the URL so a refresh (or a shared
  // link) lands back on it instead of the empty state
  if (location.hash !== "#" + id) history.replaceState(null, "", "#" + id);
  refresh();
}

function _detailList(label, values) {
  const lines = _lines(values);
  return lines.length ? `<div class="contract-detail">
    <b>${esc(label)}</b><ul>${lines.map(item => `<li>${esc(item)}</li>`).join("")}</ul>
  </div>` : "";
}

function outcomeContractCard(s) {
  const contract = s.outcome_contract || {};
  const route = s.routing_decision || {};
  const profile = s.execution_profile || route.execution_profile || route.selected_profile || "";
  const outcome = contract.outcome || contract.objective || "";
  if (!outcome && !profile && !Object.keys(route).length) return "";
  const routeName = route.selected_route || route.route || route.recommended_profile
    || route.execution_profile || profile;
  const reason = route.reason || route.rationale || route.explanation || "";
  return `<div class="card outcome-card">
    <div class="card-heading-row">
      <h3>Outcome contract</h3>
      ${profile ? `<span class="pill profile-pill">${esc(executionProfileLabel(profile))}</span>` : ""}
    </div>
    ${outcome ? `<div class="contract-outcome">${esc(outcome)}</div>` : ""}
    <div class="contract-detail-grid">
      ${_detailList("Deliverables", contract.deliverables)}
      ${_detailList("Acceptance criteria", contract.acceptance_criteria || contract.acceptance)}
      ${_detailList("Constraints", contract.constraints)}
      ${_detailList("Exclusions", contract.exclusions)}
    </div>
    ${routeName || reason ? `<div class="routing-line">
      <span>Route</span> <b>${esc(executionProfileLabel(routeName || "auto"))}</b>
      ${reason ? `<span>· ${esc(reason)}</span>` : ""}
    </div>` : ""}
  </div>`;
}

function _artifactData(sessionId) {
  const entry = artifactCache[sessionId];
  return entry?.data || null;
}

function artifactWorkbenchCard(s) {
  const data = _artifactData(s.session_id);
  const artifacts = data?.artifacts || [];
  const counts = data?.counts || {};
  const count = artifacts.length || counts.total || 0;
  return `<div class="card artifact-workbench">
    <div class="card-heading-row">
      <h3>Artifact workbench${count ? ` · ${count}` : ""}</h3>
      <button class="ghost mini" type="button" onclick="loadArtifacts('${escAttr(s.session_id)}', true)">Refresh</button>
    </div>
    <div class="sub workbench-intro">Inspect outputs here before opening or promoting them. HTML stays isolated in a sandbox.</div>
    <div id="artifactList">${data ? renderArtifactItems(s.session_id, artifacts) : '<div class="workbench-loading">Loading artifacts…</div>'}</div>
    <div id="artifactPreview" class="artifact-preview" aria-live="polite"></div>
  </div>`;
}

function renderArtifactItems(sessionId, artifacts) {
  if (!artifacts.length) return '<div class="workbench-empty">No artifacts yet. This area updates while the run works.</div>';
  return `<div class="artifact-grid">${artifacts.map(item => {
    const aid = item.artifact_id || item.id || "";
    const kind = item.kind || "file";
    const state = item.state || "scratch";
    const size = Number(item.size || 0);
    const sizeLabel = size >= 1048576 ? `${(size / 1048576).toFixed(1)} MB`
      : size >= 1024 ? `${Math.round(size / 1024)} KB` : (size ? `${size} B` : "");
    const digest = item.hash || item.sha256 || "";
    const downloadUrl = _safeSameOriginUrl(item.download_url);
    return `<article class="artifact-item">
      <div class="artifact-icon">${kind === "image" ? "IMG" : kind === "html" ? "WEB" : kind === "text" ? "TXT" : "FILE"}</div>
      <div class="artifact-copy">
        <b title="${escAttr(item.relative_path || item.name || "")}">${esc(item.name || item.relative_path || "artifact")}</b>
        <div class="artifact-meta"><span class="artifact-state ${escAttr(state)}">${esc(state)}</span>
          <span>${esc(kind)}</span>${sizeLabel ? `<span>${esc(sizeLabel)}</span>` : ""}
          ${digest ? `<span title="${escAttr(digest)}">${esc(digest.slice(0, 10))}…</span>` : ""}</div>
      </div>
      <div class="artifact-actions">
        <button class="ghost mini" type="button"
          data-session="${escAttr(sessionId)}" data-artifact="${escAttr(aid)}"
          onclick="previewArtifact(this)">Preview</button>
        ${downloadUrl ? `<a class="button-link mini" href="${escAttr(downloadUrl)}" download>Download</a>` : ""}
      </div>
    </article>`;
  }).join("")}</div>`;
}

async function loadArtifacts(sessionId, force = false) {
  const cached = artifactCache[sessionId];
  if (cached?.loading) return;
  if (!force && cached?.loadedAt && Date.now() - cached.loadedAt < 3500) return;
  artifactCache[sessionId] = {...cached, loading: true};
  try {
    const result = await workbenchRequest(`/sessions/${encodeURIComponent(sessionId)}/artifacts`);
    artifactCache[sessionId] = {data: result || {artifacts: []}, loadedAt: Date.now(), loading: false};
    if (current !== sessionId) return;
    const target = document.getElementById("artifactList");
    if (target) target.innerHTML = renderArtifactItems(sessionId, result?.artifacts || []);
  } catch (error) {
    artifactCache[sessionId] = {...cached, loadedAt: Date.now(), loading: false, error: String(error)};
    if (current === sessionId) {
      const target = document.getElementById("artifactList");
      if (target) target.innerHTML = `<div class="workbench-empty">Artifacts are not available for this run yet.</div>`;
    }
  }
}

function _safeSameOriginUrl(value) {
  if (!value) return "";
  try {
    const url = new URL(value, location.origin);
    return url.origin === location.origin ? url.href : "";
  } catch (_) { return ""; }
}

async function previewArtifact(button) {
  const sessionId = button.dataset.session;
  const artifactId = button.dataset.artifact;
  const data = _artifactData(sessionId);
  const item = (data?.artifacts || []).find(candidate =>
    String(candidate.artifact_id || candidate.id) === artifactId);
  const viewer = document.getElementById("artifactPreview");
  if (!viewer || !item) return;
  viewer.replaceChildren();
  viewer.classList.add("open");
  const heading = document.createElement("div");
  heading.className = "artifact-preview-head";
  heading.textContent = `Preview · ${item.name || item.relative_path || "artifact"}`;
  viewer.appendChild(heading);
  const safeUrl = _safeSameOriginUrl(item.preview_url);
  if (!safeUrl) {
    const note = document.createElement("div");
    note.className = "workbench-empty";
    note.textContent = "This artifact does not have a safe preview URL.";
    viewer.appendChild(note);
    return;
  }
  const rasterTypes = new Set(["image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"]);
  if (rasterTypes.has(String(item.media_type || "").toLowerCase())) {
    const image = document.createElement("img");
    image.className = "artifact-image";
    image.alt = item.name || "Generated artifact";
    image.src = safeUrl;
    viewer.appendChild(image);
    return;
  }
  try {
    const response = await fetch(safeUrl, {cache: "no-store"});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const type = response.headers.get("content-type") || "";
    const payload = type.includes("json")
      ? await response.json() : {kind: item.kind, content: await response.text(), media_type: type};
    const content = String(payload.content ?? payload.text ?? "");
    const kind = payload.kind || item.kind;
    const mediaType = payload.media_type || item.media_type || "";
    if (kind === "html" || mediaType.includes("html")) {
      const frame = document.createElement("iframe");
      frame.className = "artifact-frame";
      frame.setAttribute("sandbox", "allow-scripts");
      frame.setAttribute("referrerpolicy", "no-referrer");
      const csp = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data: blob:; style-src 'unsafe-inline'; script-src 'unsafe-inline'">`;
      frame.srcdoc = csp + content;
      frame.title = `Sandboxed preview of ${item.name || "artifact"}`;
      viewer.appendChild(frame);
    } else if (content) {
      const pre = document.createElement("pre");
      pre.className = "artifact-text";
      pre.textContent = content;
      viewer.appendChild(pre);
    } else {
      const note = document.createElement("div");
      note.className = "workbench-empty";
      note.textContent = payload.message || "This artifact is available for download but has no inline preview.";
      viewer.appendChild(note);
    }
    if (payload.truncated) {
      const warning = document.createElement("div");
      warning.className = "sub";
      warning.textContent = "Preview truncated — download the artifact to inspect the full file.";
      viewer.appendChild(warning);
    }
  } catch (error) {
    const note = document.createElement("div");
    note.className = "workbench-empty";
    note.textContent = "Preview failed: " + (error.message || error);
    viewer.appendChild(note);
  }
}

function steeringCard(s, working) {
  const commands = commandCache[s.session_id]?.data || s.steering_commands || [];
  return `<div class="card steering-card">
    <div class="card-heading-row"><h3>Steer this run</h3>
      <span class="pill ${working ? "deliberating" : "done"}">${working ? "live controls" : "command history"}</span>
    </div>
    ${working ? `<div class="steering-grid">
      <div class="steering-box">
        <label for="steerConstraint">Add a constraint</label>
        <textarea id="steerConstraint" data-draft rows="2" placeholder="A boundary every remaining agent must respect…"></textarea>
        <button class="ghost mini" type="button" onclick="sendSteering('constraint')">Apply constraint</button>
      </div>
      <div class="steering-box">
        <label for="steerFocus">Shift the focus</label>
        <textarea id="steerFocus" data-draft rows="2" placeholder="What should the remaining work prioritize?"></textarea>
        <button class="ghost mini" type="button" onclick="sendSteering('focus')">Refocus</button>
      </div>
    </div>
    <div class="budget-controls">
      <span>Increase budget</span>
      <label>calls <input id="budgetCalls" data-draft type="number" min="0" value="2"></label>
      <label>rounds <input id="budgetRounds" data-draft type="number" min="0" value="1"></label>
      <label>seconds <input id="budgetSeconds" data-draft type="number" min="0" step="30" value="120"></label>
      <button class="ghost mini" type="button" onclick="sendSteering('increase_budget')">Add</button>
      <button class="deny mini" type="button" onclick="sendSteering('finish_now')">Finish after current step</button>
    </div>` : `<div class="sub">This run is settled; its steering history remains part of the audit trail.</div>`}
    <div id="commandList" class="command-list">${renderCommands(commands, working)}</div>
    <div id="steeringNote" class="sub" aria-live="polite"></div>
  </div>`;
}

function renderCommands(commands, canRevoke) {
  if (!commands.length) return '<div class="workbench-empty">No steering commands.</div>';
  return commands.map(command => {
    const payload = command.payload || {};
    const detail = payload.text || Object.entries(payload)
      .map(([key, value]) => `${key.replaceAll("_", " ")} +${value}`).join(" · ");
    const id = command.command_id || command.id || "";
    return `<div class="command-row">
      <span class="command-kind">${esc(String(command.kind || "command").replaceAll("_", " "))}</span>
      <span>${esc(detail)}</span>
      <span class="artifact-state ${escAttr(command.status || "pending")}">${esc(command.status || "pending")}</span>
      ${canRevoke && !["applied", "revoked", "completed"].includes(command.status)
        ? `<button class="ghost mini" type="button" data-command="${escAttr(id)}" onclick="revokeSteering(this)">Revoke</button>` : ""}
    </div>`;
  }).join("");
}

async function loadCommands(sessionId, force = false) {
  const cached = commandCache[sessionId];
  if (cached?.loading) return;
  if (!force && cached?.loadedAt && Date.now() - cached.loadedAt < 3500) return;
  commandCache[sessionId] = {...cached, loading: true};
  try {
    const result = await workbenchRequest(`/sessions/${encodeURIComponent(sessionId)}/commands`);
    const commands = Array.isArray(result) ? result : (result?.commands || []);
    commandCache[sessionId] = {data: commands, loadedAt: Date.now(), loading: false};
    if (current === sessionId) {
      const target = document.getElementById("commandList");
      const session = _sessionsCache.find(item => item.session_id === sessionId);
      const working = session ? !TERMINAL_STATES.has(session.status) : true;
      if (target) target.innerHTML = renderCommands(commands, working);
    }
  } catch (_) {
    commandCache[sessionId] = {...cached, loadedAt: Date.now(), loading: false};
  }
}

async function sendSteering(kind) {
  if (!current) return;
  let payload = {};
  if (kind === "constraint" || kind === "focus") {
    const input = document.getElementById(kind === "constraint" ? "steerConstraint" : "steerFocus");
    const text = input?.value.trim() || "";
    if (!text) { input?.focus(); return; }
    payload = {text};
  } else if (kind === "increase_budget") {
    payload = {
      agent_calls: Math.max(0, Number(document.getElementById("budgetCalls")?.value || 0)),
      rounds: Math.max(0, Number(document.getElementById("budgetRounds")?.value || 0)),
      duration_seconds: Math.max(0, Number(document.getElementById("budgetSeconds")?.value || 0)),
    };
    if (!payload.agent_calls && !payload.rounds && !payload.duration_seconds) return;
  } else if (kind === "finish_now" &&
             !confirm("Finish after the current model step? The council will compose from work already completed.")) {
    return;
  }
  const note = document.getElementById("steeringNote");
  if (note) note.textContent = "Sending command…";
  try {
    await workbenchRequest(`/sessions/${encodeURIComponent(current)}/commands`, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({kind, payload}),
    });
    const field = kind === "constraint" ? "steerConstraint" : kind === "focus" ? "steerFocus" : "";
    if (field) document.getElementById(field).value = "";
    if (note) note.textContent = "Command recorded. It takes effect at the next safe checkpoint.";
    await loadCommands(current, true);
    pollLoop();
  } catch (error) {
    if (note) {
      note.style.color = "var(--bad)";
      note.textContent = "Command failed: " + (error.message || error);
    }
  }
}

async function revokeSteering(button) {
  if (!current || !button.dataset.command) return;
  try {
    await workbenchRequest(`/sessions/${encodeURIComponent(current)}/commands/${encodeURIComponent(button.dataset.command)}`,
      {method: "DELETE"});
    await loadCommands(current, true);
  } catch (error) {
    const note = document.getElementById("steeringNote");
    if (note) note.textContent = "Could not revoke: " + (error.message || error);
  }
}

function evaluationCard(s) {
  if (!TERMINAL_STATES.has(s.status)) return "";
  const saved = evaluationCache[s.session_id] || s.evaluation || {};
  const verdict = saved.verdict || "";
  const rating = Number(saved.rating || 0);
  const option = (value, label) => `<option value="${value}" ${verdict === value ? "selected" : ""}>${label}</option>`;
  return `<div class="card evaluation-card">
    <div class="card-heading-row"><h3>Evaluate this outcome</h3>
      ${saved.updated_at || saved.created_at ? '<span class="pill done">saved</span>' : ""}
    </div>
    <div class="evaluation-grid">
      <label>Verdict<select id="evalVerdict" data-draft>
        ${option("", "Choose…")}${option("success", "Successful")}${option("partial", "Partially successful")}${option("failure", "Missed the outcome")}
      </select></label>
      <label>Rating<input id="evalRating" data-draft type="number" min="1" max="5" value="${rating || ""}" placeholder="1–5"></label>
    </div>
    <label class="evaluation-notes">Notes
      <textarea id="evalNotes" data-draft rows="3" placeholder="What worked, what missed, and what should the next run do differently?">${esc(saved.notes || "")}</textarea>
    </label>
    <div class="row"><button type="button" onclick="saveEvaluation('${escAttr(s.session_id)}')">Save evaluation</button>
      <span id="evaluationNote" class="sub">Your feedback improves future route recommendations.</span></div>
  </div>`;
}

async function saveEvaluation(sessionId) {
  const verdict = document.getElementById("evalVerdict")?.value || "";
  const rating = Number(document.getElementById("evalRating")?.value || 0);
  const notes = document.getElementById("evalNotes")?.value.trim() || "";
  const status = document.getElementById("evaluationNote");
  if (!verdict || rating < 1 || rating > 5) {
    if (status) {
      status.style.color = "var(--bad)";
      status.textContent = "Choose a verdict and a rating from 1 to 5.";
    }
    return;
  }
  try {
    const result = await workbenchRequest(`/sessions/${encodeURIComponent(sessionId)}/evaluation`, {
      method: "PUT", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({verdict, rating, notes}),
    });
    evaluationCache[sessionId] = result?.evaluation || result || {verdict, rating, notes};
    if (status) {
      status.style.color = "var(--ok)";
      status.textContent = "Evaluation saved — future auto-routing can learn from it.";
    }
  } catch (error) {
    if (status) {
      status.style.color = "var(--bad)";
      status.textContent = "Could not save: " + (error.message || error);
    }
  }
}

function statusAgentName(name) {
  const value = String(name || "model");
  const known = {codex: "Codex", claude: "Claude", gemini: "Gemini"};
  return known[value.toLowerCase()] || value.replace(/(^|[-_ ])([a-z])/g,
    (_m, left, letter) => left + letter.toUpperCase());
}

function runStatusBrief(s, goal, events) {
  const activeCalls = s.active_agent_calls || [];
  const activeCall = activeCalls[0] || null;
  const gate = s.quality_gate || s.run_summary?.quality_gate || {};
  const timeline = events || [];
  const hasEvent = name => timeline.some(item => item.event === name);
  const verification = !!(
    s.goal_release || gate.verifier ||
    hasEvent("browser_release_verified") ||
    hasEvent("frontier_final_batch_verdict")
  );
  const working = ["received", "classified", "deliberating", "composing"]
    .includes(s.status);
  let headline = "This run is waiting for an update.";
  if (working && verification) {
    headline = "The current run is still actively verifying\u2014not stuck or finished.";
  } else if (working) {
    headline = "The current run is still actively working\u2014not stuck or finished.";
  } else if (s.status === "awaiting_approval") {
    headline = "The run is paused for your approval; no model work is currently blocked invisibly.";
  } else if (s.status === "awaiting_input") {
    headline = "The run is paused for an answer from you.";
  } else if (s.status === "done") {
    headline = s.outcome === "succeeded"
      ? "This run finished successfully."
      : "This run finished, but its recorded outcome was not successful.";
  } else if (s.status === "failed") {
    headline = "This run stopped after failing verification; it was not delivered as a success.";
  } else if (s.status === "cancelled") {
    headline = "This run was cancelled and is no longer doing work.";
  }

  const bullets = [];
  const completedPackages = (goal?.milestones || []).filter(item =>
    item.status === "done" && item.owner
  );
  for (const item of completedPackages.slice(-2)) {
    const outputs = Object.keys(item.output_authors || {});
    const result = outputs.length
      ? outputs.join(", ")
      : (item.title || `package ${item.package_id || ""}`).trim();
    bullets.push(`${statusAgentName(item.owner)} completed ${result} successfully.`);
  }

  if (goal?.release_status === "failed_verification" &&
      goal.release_session_id && goal.release_session_id !== s.session_id) {
    bullets.push("An earlier final-release attempt failed verification; this is the active retry.");
  }

  const missingChecks = gate.missing_checks || [];
  const gateVerdict = String(gate.verdict || "").toUpperCase();
  const repairApplied = hasEvent("frontier_final_batch_repair_applied");
  if (gateVerdict === "FAIL") {
    const verifier = statusAgentName(gate.verifier || "verifier");
    const attempt = Number(gate.attempt || 1);
    if (missingChecks.length) {
      bullets.push(
        `${verifier}'s verification attempt ${attempt} omitted results for ` +
        `${missingChecks.length} acceptance check${missingChecks.length === 1 ? "" : "s"} ` +
        `(${missingChecks.join(", ")}).`
      );
    } else {
      bullets.push(`${verifier}'s verification attempt ${attempt} did not pass.`);
    }
  } else if (gateVerdict === "PASS" && repairApplied) {
    bullets.push("The first verification attempt did not pass and triggered a repair cycle.");
  }

  if (repairApplied) {
    bullets.push("The app automatically applied the verifier's proposed repairs before retrying.");
  }

  if (gateVerdict === "PASS") {
    const verifier = statusAgentName(gate.verifier || "verifier");
    const attempt = Number(gate.attempt || 1);
    bullets.push(
      `${verifier} completed verification attempt ${attempt} successfully.`
    );
  }

  if (activeCall) {
    const attempt = Number(activeCall.attempt || 1);
    const action = verification ? "verification" : (activeCall.role || "work");
    bullets.push(
      `${statusAgentName(activeCall.agent)} is now performing ${action} attempt ${attempt}.`
    );
  } else if (working) {
    bullets.push("No model call is active at this instant; the coordinator is processing the latest result.");
  }

  const browserChecks = gate.browser_acceptance || [];
  const passedBrowser = browserChecks.find(check => check.passed);
  if (passedBrowser) {
    const browser = String(passedBrowser.browser || "the configured browser")
      .replace(/^msedge$/i, "Microsoft Edge");
    bullets.push(`Browser testing already passed in ${browser}.`);
  } else if (hasEvent("browser_release_verified")) {
    bullets.push("The real-browser release check already passed.");
  }

  const approvals = (s.approvals || []).filter(item => item.status === "pending");
  bullets.push(approvals.length
    ? `${approvals.length} approval${approvals.length === 1 ? " is" : "s are"} pending.`
    : "No approval is pending.");

  const promoted = (s.proposed_actions || []).some(action =>
    ["promote", "promote_batch"].includes(action.kind) && action.status === "executed"
  );
  const established = String(s.established_root || "").toLowerCase();
  const delivered = promoted || (s.files_changed || []).some(path =>
    established && String(path).toLowerCase().startsWith(established)
  );
  bullets.push(delivered
    ? "Verified output has been delivered to the established folder."
    : "Nothing has been delivered yet.");

  if (activeCall) {
    if (Number(activeCall.timeout_s) === 0) {
      bullets.push("This call has no hard timeout; it is operator-supervised.");
    } else if (activeCall.timeout_s) {
      bullets.push(`This call has a ${activeCall.timeout_s}-second hard timeout.`);
    }
  }
  return {headline, bullets, live: working || s.status === "awaiting_approval"};
}

function toggleRunStatus() {
  const card = document.getElementById("runStatusCard");
  if (!card) return;
  const key = card.dataset.statusKey;
  const expanded = !card.classList.contains("expanded");
  card.classList.toggle("expanded", expanded);
  if (key) openSections[key] = expanded;
  const button = card.querySelector("[data-status-toggle]");
  if (button) button.textContent = expanded ? "Hide details" : "Show details";
}

function openRunStatus() {
  const card = document.getElementById("runStatusCard");
  if (!card) return;
  if (!card.classList.contains("expanded")) toggleRunStatus();
  card.scrollIntoView({behavior: "smooth", block: "start"});
}

function renderDetail(s) {
  if (!s) return;
  const right = document.getElementById("right");
  const final = s.final;
  const approvals = (s.approvals||[]).filter(a => a.status === "pending");
  const inputs = (s.input_requests||[]).filter(i => i.status === "pending");
  const verificationFailed = s.outcome === "failed_verification";
  const packageMode = s.collaboration_mode === "build_team" && !!s.work_package_owner;
  const candidateMode = s.execution_profile === "best_of_n"
    || s.routing_decision?.selected_route === "best_of_n";
  const sessionGoal = s.goal_id
    ? _goalsCache.find(g => g.goal_id === s.goal_id) : null;
  const parentGoal = packageMode ? sessionGoal : null;
  const currentPackage = parentGoal
    ? (parentGoal.milestones || []).find(m => m.package_id === s.work_package_id)
    : null;
  const attemptState = packageMode
    ? packageAttemptState(s, parentGoal, _sessionsCache) : null;
  const retryRunning = !!(attemptState?.isHistorical && currentPackage &&
    !TERMINAL_STATES.has(currentPackage.session_status || currentPackage.status));
  const taskStatusLabel = retryRunning
    ? `historical ${verificationFailed ? "failed verification" : humanStatus(s.status)}`
    : (verificationFailed ? "failed verification" : humanStatus(s.status));
  const attemptMeta = attemptState?.total
    ? `attempt ${attemptState.selectedNumber || "?"} of ${attemptState.total}` : "";

  // The poll timer re-renders this pane every few seconds; snapshot any
  // in-progress answer text + focus/caret + scroll so reading/typing isn't
  // clobbered mid-word.
  const draft = {};
  right.querySelectorAll('textarea[id^="ans_"], #followupText, [data-draft]').forEach(t => {
    let start = null, end = null;
    try { start = t.selectionStart; end = t.selectionEnd; } catch (_) {}
    draft[t.id] = {v: t.value, start, end, focused: document.activeElement === t};
  });
  const scrollTop = right.scrollTop;

  const working = ["received","classified","deliberating","composing"].includes(s.status);
  const statusBrief = runStatusBrief(s, sessionGoal, _liveEvents);
  const runStatusKey = `run_status_${s.session_id}`;
  if (!Object.prototype.hasOwnProperty.call(openSections, runStatusKey)) {
    openSections[runStatusKey] = working || s.status === "awaiting_approval";
  }
  const runStatusExpanded = !!openSections[runStatusKey];
  const spokenRoles = new Set((s.contributions||[]).map(c => c.role));
  const respondedAgents = new Set([
    ...Object.keys(s.successful_agent_calls || {}),
    ...(s.contributions || []).map(c => c.agent).filter(Boolean),
  ]);
  const acceptedCandidateAgents = new Set(
    (s.proposed_actions || [])
      .filter(a => a.kind === "write_file" && a.role === "panelist" &&
        a.status === "executed" && String(a.content || "").trim())
      .map(a => a.args?.package_author ||
        (String(a.filename || "").includes("__")
          ? String(a.filename).split("__", 1)[0] : ""))
      .filter(Boolean)
  );
  const workingCandidateAgents = new Set(
    (s.active_agent_calls || []).map(call => call.agent).filter(Boolean)
  );
  const members = ((s.council||{}).members||[]);
  const roster = members.filter(m => m.active && m.agent && m.agent !== "system" &&
    (candidateMode ? m.role === "panelist" : (!packageMode || m.role === "panelist")));
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
  if (packageMode) {
    liveGoal = `package ${s.work_package_id || ""}${currentPackage ? ` · ${currentPackage.title}` : ""}`;
    waitRole = "owner";
    waitAgent = s.work_package_owner;
  }
  const activeCalls = s.active_agent_calls || [];
  const activeCall = activeCalls[0] || null;
  const nowMs = Date.now();
  const slowCalls = activeCalls.filter(call => {
    const started = Date.parse(call.started_at || "");
    const checkin = Number(call.operator_checkin_s || 0);
    const snoozedUntil = _slowCallSnoozes.get(call.call_id) || 0;
    return call.operator_stoppable && checkin > 0 && Number.isFinite(started) &&
      nowMs - started >= checkin * 1000 && nowMs >= snoozedUntil;
  });
  const slowCallPrompt = slowCalls.length ? `
    <div class="slow-call-checkin" role="status" aria-live="polite">
      <div class="slow-call-title">Long-running model check-in</div>
      ${slowCalls.map(call => {
        const elapsedMinutes = Math.max(
          1, Math.floor((nowMs - Date.parse(call.started_at)) / 60000)
        );
        const chars = Number(call.progress_chars || 0);
        const checkin = Number(call.operator_checkin_s || 300);
        return `<div class="slow-call-row">
          <div>
            <b>${esc(call.agent || "model")}</b> has been working for ${elapsedMinutes} minute${elapsedMinutes === 1 ? "" : "s"}.
            ${chars ? `It is still producing output (${chars.toLocaleString()} characters so far).` : "It has not produced output yet."}
            There is no automatic wall-clock cutoff.
          </div>
          <div class="slow-call-actions">
            <button class="ghost mini" type="button"
                    onclick="keepWaitingForCall('${escAttr(call.call_id)}', ${checkin})">Keep waiting</button>
            <button class="deny mini" type="button"
                    onclick="stopAgentCall('${escAttr(s.session_id)}', '${escAttr(call.call_id)}', '${escAttr(call.agent || "model")}')">Stop this seat</button>
            <button class="deny mini" type="button"
                    onclick="cancelCurrent()">Cancel run</button>
          </div>
        </div>`;
      }).join("")}
    </div>` : "";
  if (activeCall) {
    waitRole = activeCall.role || waitRole;
    waitAgent = activeCall.agent || waitAgent;
    const deadline = activeCall.timeout_s === 0
      ? "no hard deadline"
      : `${activeCall.timeout_s || "?"}s hard deadline`;
    const stall = activeCall.stall_timeout_s
      ? ` · ${activeCall.stall_timeout_s}s no-output limit` : "";
    const chars = activeCall.progress_chars || 0;
    const progress = chars
      ? `streamed ${chars.toLocaleString()} chars`
      : "waiting for first model output";
    const workers = packageMode && activeCalls.length > 1
      ? `${activeCalls.length} package authors working`
      : `${packageMode ? "package author" : "model"} working`;
    liveGoal = `${workers} · ${progress} · ${deadline}${stall}`;
  }
  // reset the elapsed clock whenever the live situation actually changes
  const key = working ? `${s.session_id}|${s.status}|${s.current_round}|${s.agent_calls}|${activeCall?.call_id || waitRole}` : null;
  if (key !== liveKey) {
    liveKey = key;
    liveSince = working
      ? (activeCall?.started_at ? Date.parse(activeCall.started_at) : Date.now()) : 0;
  }

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
  const callAttempts = s.agent_call_attempts ?? runSummary.agent_call_attempts ?? calls;
  // talents the lead pulled in = contributions from non-driver roles
  // (panelists contribute every round by design — they are not delegations)
  const DRIVE_ROLES = new Set(["lead", "panelist", "summarizer", "coordinator"]);
  const delegations = contribs.filter(c => c.role && !DRIVE_ROLES.has(c.role)).length;
  // "7 agent calls · 3 agents · 2 delegations"
  const statBits = [];
  if (calls) statBits.push(`<b>${calls}</b> agent call${calls === 1 ? "" : "s"}`);
  if (callAttempts > calls) {
    statBits.push(`<b>${callAttempts}</b> total attempt${callAttempts === 1 ? "" : "s"}`);
  }
  const packageElapsedMs = runSummary.package_elapsed_ms || 0;
  if (packageElapsedMs) {
    statBits.push(`<b>${shortDuration(packageElapsedMs)}</b> package elapsed`);
  }
  const completedModelMs = runSummary.contribution_duration_ms || 0;
  const attemptMs = runSummary.agent_attempt_duration_ms || completedModelMs;
  if (attemptMs) {
    statBits.push(`<span title="Sum of elapsed time across every model attempt, including failed and overlapping calls"><b>${shortDuration(attemptMs)}</b> aggregate attempt time</span>`);
  }
  if (candidateMode) {
    const responseCount = Object.keys(
      runSummary.successful_agent_calls || s.successful_agent_calls || {}
    ).length;
    if (responseCount) {
      statBits.push(`<b>${responseCount}</b> model${responseCount === 1 ? "" : "s"} responded`);
    }
  } else if (agentCount) {
    statBits.push(`<b>${agentCount}</b> successful agent${agentCount === 1 ? "" : "s"}`);
  }
  if (delegations) statBits.push(`<b>${delegations}</b> delegation${delegations === 1 ? "" : "s"}`);
  if (runSummary.test_fix_attempts) statBits.push(`<b>${runSummary.test_fix_attempts}</b> test repair${runSummary.test_fix_attempts === 1 ? "" : "s"}`);
  const candidateMetrics = runSummary.candidate_metrics || s.candidate_metrics || {};
  if (candidateMetrics.authored !== undefined) {
    const expected = candidateMetrics.expected
      ? ` of <b>${candidateMetrics.expected}</b> expected` : "";
    const missing = (candidateMetrics.missing_authors || []).length
      ? ` · missing ${esc(candidateMetrics.missing_authors.join(", "))}` : "";
    const runtime = candidateMetrics.runtime_evaluated === false
      ? "runtime not evaluated"
      : `<b>${candidateMetrics.runnable ?? 0}</b> runnable`;
    statBits.push(
      `<b>${candidateMetrics.authored}</b> authored${expected} / `
      + `${runtime}${missing}`
    );
  }
  const qualityGate = runSummary.quality_gate || s.quality_gate || {};
  if (qualityGate.verdict) {
    const gateStage = String(qualityGate.stage || "");
    const gateLabel = gateStage.startsWith("deterministic_assembly")
      ? "deterministic assembly gate"
      : gateStage === "candidate_comparison"
        ? "candidate comparison gate"
        : "release gate";
    statBits.push(`${gateLabel} <b>${esc(String(qualityGate.verdict).toLowerCase())}</b>${qualityGate.verifier ? ` by ${esc(qualityGate.verifier)}` : ""}`);
  }
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
  const collapseDefault = working || uiPreferences.collapse_finished === false;

  right.innerHTML = `
    <div class="card">
      <h3>Task — <span class="pill ${esc(s.status)}">${esc(taskStatusLabel)}</span>
          ${attemptMeta ? `<span class="sub"> · ${esc(attemptMeta)}${attemptState.isHistorical ? " · historical" : " · current"}</span>` : ""}
          ${s.stop_reason ? `<span class="sub"> · ${esc(s.stop_reason)}</span>` : ""}</h3>
      ${retryRunning ? `<div class="attempt-notice live-attempt">
        <span><b>Retry attempt ${esc(attemptState.currentNumber || attemptState.total)} is still running.</b>
        This failed result is historical, not the goal's current state.</span>
        <button onclick="select('${esc(currentPackage.session_id)}')">View active retry</button>
      </div>` : ""}
      ${packageMode ? `<div class="sub brief-note">Package briefs are captured when an attempt starts. Any status words inside older briefs are historical; the live build-team state is shown below.</div>` : ""}
      <div class="mono">${esc(s.task?.text)}</div>
      ${s.established_root ? `<div class="sub" style="margin-top:6px" title="read-only source; files reach it only via an approved promote">📂 established folder: <span class="mono">${esc(s.established_root)}</span></div>` : ""}
      ${(s.attachments||[]).length ? `<div class="atts" style="display:flex;flex-wrap:wrap;gap:6px;margin-top:8px">${
        s.attachments.map(a => `<span class="chip ${esc(a.kind)}">${KIND_ICON[a.kind]||"📎"} ${esc(a.name)}</span>`).join("")
      }</div>` : ""}
    </div>

    <div class="detail-quick-actions">
      <button class="ghost mini" type="button" onclick="openRunStatus()">Run status</button>
      <button class="ghost mini" type="button" onclick="cloneSession('${escAttr(s.session_id)}')">Copy & edit</button>
      <button class="ghost mini" type="button" onclick="saveSessionAsPlaybook('${escAttr(s.session_id)}')">Save as playbook</button>
    </div>
    <div id="runStatusCard"
         class="card run-status ${runStatusExpanded ? "expanded" : ""} ${statusBrief.live ? "is-live" : ""}"
         data-status-key="${escAttr(runStatusKey)}">
      <div class="run-status-head">
        <h3>${statusBrief.live ? '<span class="run-status-pulse" aria-hidden="true"></span>' : ""}Run status</h3>
        <button class="ghost mini" type="button" data-status-toggle onclick="toggleRunStatus()">${runStatusExpanded ? "Hide details" : "Show details"}</button>
      </div>
      <div class="run-status-headline" role="status" aria-live="polite">${esc(statusBrief.headline)}</div>
      <div class="run-status-details">
        <ul>${statusBrief.bullets.map(item => `<li>${esc(item)}</li>`).join("")}</ul>
        <div class="sub">${statusBrief.live
          ? "Updated automatically while this session is active."
          : "Final recorded status for this session."}</div>
      </div>
    </div>
    ${outcomeContractCard(s)}

    ${showSummary ? `
      <div class="card summary">
        <h3>${retryRunning ? "Historical attempt failed — retry still running" : (verificationFailed ? "Verification failed — not delivered" : ((s.turns && s.turns.length > 2) ? "Latest conclusion" : "Summary"))}</h3>
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
            <div class="what">${packageMode ? "Building package" : esc(s.status)}<span class="ell"></span></div>
            <div class="meta">${liveGoal ? esc(liveGoal) : `round ${s.current_round ?? 0}`}${waitRole ? ` · ${esc(waitRole)} · ${esc(waitAgent)}` : ""}${_liveTalent ? ` · 🤝 ${esc(_liveTalent)}` : ""} · <span id="elapsed">0:00</span></div>
            ${activeCall && activeCall.tail ? `<div class="tail" title="what the model is writing right now"><code>${esc(activeCall.tail)}</code></div>` : ""}
          </div>
        </div>
        ${slowCallPrompt}
        ${_liveFeed.length ? `<div class="livefeed">${_liveFeed.map(e => `
          <div class="lfrow">
            <span class="lfts">${esc((e.ts || "").slice(11, 19))}</span>
            <span class="lfic">${e.icon || "•"}</span>
            <span class="lflabel">${esc(e.label)}</span>
            ${e.detail ? `<span class="lfdetail">${esc(e.detail)}</span>` : ""}
          </div>`).join("")}</div>` : ""}
      </div>` : ""}

    ${steeringCard(s, working)}

    ${(roster.length || (parentGoal && (parentGoal.milestones || []).length)) ? `
      <div class="card">
        <h3>${candidateMode ? "Best-of-all candidates" : packageMode ? "Planned build" : "Council"}${
          candidateMode
            ? ` <span class="sub">· every enabled AI attempts the complete artifact; runnable candidates are judged anonymously</span>`
            : packageMode
              ? ` <span class="sub">· this package has one accountable owner; peers review and can propose edits</span>`
              : ""
        }</h3>
        <div class="roster">
          ${packageMode && parentGoal ? (parentGoal.milestones || []).map((m, i) => {
            const active = m.package_id === s.work_package_id;
            const hard = (m.depends_on || []).filter(d => parentGoal.milestones[d] && parentGoal.milestones[d].status !== "done");
            const title = hard.length
              ? `blocked on verified package ${hard.map(d => d + 1).join(", ")}`
              : m.status === "done" ? "completed"
              : m.status === "running" && (m.contract_depends_on || []).length
                ? "running from declared interfaces; no artifact wait"
                : `${m.status}; no upstream blocker`;
            return `<span class="seat role-panelist ${active ? "spoke" : "on"}" title="${esc(title)}">P${i + 1} · ${esc(m.owner || "unassigned")} · ${esc(m.status)}</span>`;
          }).join("") : roster.map(m => {
            const talent = !DRIVE_ROLES.has(m.role);
            // the model this member's role+agent actually ran; else the one it
            // WILL run (resolved server-side: role pin › seat pin › CLI default).
            const full = contribModel[modelKey(m.role, m.agent)] || m.model || "";
            if (candidateMode) {
              const accepted = acceptedCandidateAgents.has(m.agent);
              const responding = workingCandidateAgents.has(m.agent);
              const responded = respondedAgents.has(m.agent);
              const missing = !working && !accepted;
              const state = accepted
                ? "candidate accepted"
                : responding
                  ? "building candidate"
                  : missing
                    ? (responded ? "response received; no candidate accepted" : "no response")
                    : responded
                      ? "response received; checking candidate"
                      : "awaiting candidate";
              const stateClass = accepted ? "spoke"
                : responding ? "working"
                  : missing ? "missing"
                    : responded ? "responded" : "on";
              return `<span class="seat role-panelist ${stateClass}"
                title="${esc(state)}${full ? ` · ${esc(full)}` : ""}">${esc(m.agent)}${full ? ` · ${esc(shortModel(full))}` : ""} · ${esc(state)}</span>`;
            }
            const title = (talent ? "pulled in by the lead mid-round (CONSULT/DELEGATE) — " : "") + (full ? esc(full) : "model: CLI default");
            return `<span class="seat role-${esc(m.role)}${talent ? " talent" : ""} ${spokenRoles.has(m.role)?"spoke":"on"}"
              title="${title}">${talent ? "🤝 " : ""}${esc(m.role)} · ${esc(m.agent)}${full ? ` · ${esc(shortModel(full))}` : ""}</span>`;
          }).join("")}
        </div>
        ${!packageMode ? (() => {
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
        })() : `<div class="sub" style="margin-top:8px">The highlighted owner writes this package. Contract-linked owners can work simultaneously; only hard artifact dependencies wait.</div>`}
      </div>` : ""}

    ${(s.council_health && s.council_health.degraded) ? `
      <div class="card health">
        <h3>⚠ Council health — ran degraded</h3>
        ${s.council_health.dropped.map(d => `<div class="hrow"><b>${esc(d.role)}</b> seat (${esc(d.agent)}) dropped — ${esc(d.error)}</div>`).join("")}
        ${s.council_health.substitutions.map(x => `<div class="hrow">summarizer <b>${esc(x.failed)}</b> failed → recomposed with <b>${esc(x.replaced_by)}</b></div>`).join("")}
        ${s.council_health.notes.map(n => `<div class="hrow">${esc(n)}</div>`).join("")}
      </div>` : ""}

    ${approvals.map(a => {
      const isBatch = String(a.action || "").includes("APPROVE FINAL BATCH");
      return `
        <div class="card needs">
          <h3>${isBatch ? "Final batch ready" : "Approval needed — " + esc(a.category) + " / " + esc(a.risk)}</h3>
          <div>${esc(a.action)}</div>
          ${a.details ? `<pre class="diff">${diffHtml(a.details)}</pre>` : ""}
          <div class="row">
            <button onclick="resolveApproval('${a.approval_id}', true)">${isBatch ? "Approve final batch" : "Approve"}</button>
            ${isBatch ? "" : `<button onclick="resolveApproval('${a.approval_id}', true, true)" title="Approve this and every other ${esc(a.category)} in this session — one decision instead of N identical clicks">Approve all ${esc(a.category)}s</button>`}
            <button class="deny" onclick="resolveApproval('${a.approval_id}', false)">Deny</button>
          </div>
        </div>`;
    }).join("")}

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

    ${artifactWorkbenchCard(s)}
    ${evaluationCard(s)}

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
  loadArtifacts(s.session_id);
  loadCommands(s.session_id);
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
  default_profile: "How a task is run when you do not pick a profile for it. 'Auto' lets the router choose — and for any request under ~60 words it scores Focused highest, which runs ONE model (the lead) with no panel. Pick Council or Best-of-N here if you want several models on ordinary tasks.",
  panel_roster: "The exact seats that contribute every round. Leave empty to derive it automatically (installed CLI agents, plus OpenRouter seats backfilling for any you disabled). Ticking seats here overrides that entirely — your list wins over panel size and duo/council mode.",
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
  cli_models: "The local CLI seats (claude, codex, gemini) and the exact model each one runs. Leave '(CLI default)' to inherit that CLI's own configured model, or pin one from the live catalog. Unchecking a seat drops it from the council and redistributes its roles across every remaining enabled seat.",
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
  applyUiPreferences(settings.ui);
  renderHeaderSeatToggles(settings);
  seatsCache = seatsResp.seats || [];
  wsCache = wsResp;
  orKey = keyResp || {present: false};
  gemKey = gemResp || {present: false};
  renderSettings(settings, seatsCache);
}

function closeSettings() {
  document.getElementById("settingsOverlay").classList.remove("open");
}

function applyUiPreferences(ui) {
  uiPreferences = Object.assign(
    {poll_interval_ms: 3000, collapse_finished: true}, ui || {}
  );
}

async function loadUiPreferences() {
  try {
    const settings = await api("/settings");
    settingsCache = settings;
    applyUiPreferences(settings.ui);
    renderHeaderSeatToggles(settings);
  } catch (error) {
    headerSeatStatus(`Could not load AI seats: ${error.message || error}`, true);
    // The dashboard remains usable with the built-in visual defaults.
  }
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
    <div class="sset s-profile">
      <label>Portable settings profile</label>
      <div class="field profile-actions">
        <button class="ghost" onclick="exportSettingsProfile()">Export saved profile</button>
        <button class="ghost" onclick="chooseSettingsProfile()">Import profile…</button>
        <button onclick="loadDefaultSettingsProfile()">Load packaged defaults</button>
        <input id="settingsProfileFile" type="file" accept="application/json,.json"
               style="display:none" onchange="importSettingsProfile(event)">
      </div>
      <div class="sub">Moves local CLI and OpenRouter seat/model selections, role and model mapping, governance/composer controls, and UI preferences between installations. API keys, workspaces, sandbox/delivery folders, sessions, and other machine state are never included.</div>
    </div>

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
        return `
        <div class="field" style="align-items:center;flex-wrap:wrap">
          <label style="margin:0;flex:0 0 92px;display:flex;align-items:center;gap:6px"
                 title="Uncheck to drop this local CLI seat and redistribute its roles across the remaining enabled models.">
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
        </div>`;}).join("")}
      <div class="field" style="justify-content:flex-end">
        <button class="ghost" onclick="refreshModelCatalog(this)">↻ refresh model list</button>
      </div>
      <div class="sub">Fetched live from the public model catalog — <b>no API key needed</b> (newest first; a model released yesterday shows up here). A Gemini key below upgrades the gemini list to Google's own authoritative catalog. "custom…" takes any id; default = whatever that CLI is configured to use. Every contribution shows the model that actually produced it.</div>
      <div class="sub"><b>Human-supervised calls:</b> no model is stopped merely because an estimated duration elapsed. After five minutes the app asks whether to keep waiting, stop that model, or cancel the run. Keep waiting does not restart the call.</div>
      <div class="sub">Unchecking a seat removes its adapter and redistributes its roles across all remaining enabled local and OpenRouter models. Enable at least one model; OpenRouter seats also require the API key below to run.</div>
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

    <div class="sset s-panelroster">
      <label>Panel roster ${tip("panel_roster")}</label>
      <div class="field" style="flex-wrap:wrap;gap:10px">
        ${(seats || []).map(x => `
          <label style="display:flex;gap:6px;align-items:center;cursor:pointer;margin:0">
            <input type="checkbox" class="panel_seat" data-seat="${esc(x.name)}"
                   ${(s.panel_seats || []).includes(x.name) ? "checked" : ""}>
            ${esc(x.label || x.name)}
          </label>`).join("")}
      </div>
      <div class="sub">Leave every box unticked to derive the panel automatically. Tick any and that exact list becomes the panel, overriding duo/council mode and the panel-size cap. Disabled seats are still skipped.</div>
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
      <div class="field">${lbl("Default run profile", "default_profile")}
        <select id="set_default_profile">
          <option value="auto" ${(s.default_execution_profile || "auto") === "auto" ? "selected" : ""}>Auto — router decides (usually one model)</option>
          <option value="focused" ${s.default_execution_profile === "focused" ? "selected" : ""}>Focused — one lead, no panel</option>
          <option value="council" ${s.default_execution_profile === "council" ? "selected" : ""}>Council — the panel deliberates every task</option>
          <option value="best_of_n" ${s.default_execution_profile === "best_of_n" ? "selected" : ""}>Best-of-N — every enabled model submits a candidate</option>
        </select></div>
      <div class="sub">Applies to tasks submitted without an explicit profile. Auto scores Focused above Council for anything under ~60 words, and Focused convenes no panel — so on a multi-model install Auto means one model does nearly everything.</div>
      <div class="field">${lbl("Build participation", "participation")}
        <select id="set_participation_mode">
          <option value="focused" ${s.participation_mode === "focused" ? "selected" : ""}>Focused — owner + verifier</option>
          <option value="adaptive" ${s.participation_mode === "adaptive" ? "selected" : ""}>Adaptive — full council on substantial builds</option>
          <option value="full_council" ${s.participation_mode === "full_council" ? "selected" : ""}>Full Council — every enabled resource</option>
        </select></div>
      <div class="sub">Full Council keeps one accountable file owner while every enabled resource reviews the actual baseline and can submit concrete code edits. Enabled resources such as DeepSeek participate even when no named specialist role maps to them.</div>
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

function _profileNote(message, bad = false) {
  const note = document.getElementById("savedNote");
  if (!note) return;
  note.style.color = bad ? "var(--bad)" : "";
  note.textContent = message;
}

async function _refreshSettingsAfterProfile(message) {
  const [settings, seatsResp] = await Promise.all([
    api("/settings"),
    api("/settings/seats").catch(() => ({seats: []})),
  ]);
  settingsCache = settings;
  seatsCache = seatsResp.seats || [];
  applyUiPreferences(settings.ui);
  renderHeaderSeatToggles(settings);
  openSections = {};
  renderSettings(settingsCache, seatsCache);
  _profileNote(message);
  loadHealth();
  pollLoop();
}

async function exportSettingsProfile() {
  try {
    const profile = await api("/settings/profile");
    const blob = new Blob([JSON.stringify(profile, null, 2) + "\n"], {type: "application/json"});
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "gangof8-settings-profile.json";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    _profileNote("portable profile exported ✓");
  } catch (e) {
    _profileNote(`profile export failed: ${e.message || e}`, true);
  }
}

function chooseSettingsProfile() {
  document.getElementById("settingsProfileFile")?.click();
}

async function importSettingsProfile(event) {
  const input = event?.target;
  const file = input?.files?.[0];
  if (!file) return;
  try {
    // Windows PowerShell 5.1 may write UTF-8 JSON with a BOM.  Treat it as the
    // same portable file instead of rejecting an otherwise valid export.
    const profile = JSON.parse((await file.text()).replace(/^\uFEFF/, ""));
    const r = await fetch("/settings/profile", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(profile),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      throw new Error(d.detail || `HTTP ${r.status}`);
    }
    await _refreshSettingsAfterProfile("profile loaded; keys and folders unchanged ✓");
  } catch (e) {
    _profileNote(`profile import failed: ${e.message || e}`, true);
  } finally {
    if (input) input.value = "";
  }
}

async function loadDefaultSettingsProfile() {
  if (!confirm("Load the packaged Gang of 8 defaults?\n\nThis replaces model, role, governance/composer, and UI preferences. API keys, workspaces, and sandbox folders stay unchanged.")) return;
  try {
    const r = await fetch("/settings/profile/default", {method: "POST"});
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      throw new Error(d.detail || `HTTP ${r.status}`);
    }
    await _refreshSettingsAfterProfile("packaged defaults loaded; keys and folders unchanged ✓");
  } catch (e) {
    _profileNote(`could not load packaged defaults: ${e.message || e}`, true);
  }
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
  const panel_seats = [];
  document.querySelectorAll(".panel_seat").forEach(c => { if (c.checked) panel_seats.push(c.dataset.seat); });
  const cli_enabled = {};
  document.querySelectorAll(".cli_enable").forEach(c => { cli_enabled[c.dataset.seat] = c.checked; });
  const patch = {
    backend: document.getElementById("set_backend").value,
    role_agents,
    role_models,
    openrouter_enabled,
    openrouter_models,
    cli_models,
    cli_enabled,
    cli_timeouts: {},
    integration_review_enabled: document.getElementById("set_integration_review").checked,
    participation_mode: document.getElementById("set_participation_mode").value,
    default_execution_profile: document.getElementById("set_default_profile").value,
    panel_seats,
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
    applyUiPreferences(settingsCache.ui);
    renderHeaderSeatToggles(settingsCache);
    openSections = {};
    pollLoop();
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
loadPlaybooks();
bindHeaderSeatToggles();
bindComposerControls();
bindComposerDrag();
// deep link: /#<session_id> re-opens that session; otherwise the hero greets
if (location.hash.length > 1) select(decodeURIComponent(location.hash.slice(1)));
else renderEmptyHero();
loadUiPreferences().finally(() => pollLoop());
setInterval(tickElapsed, 1000);
startFeed();
// when a task is submitted or an action resolved, jump back to fast polling
document.addEventListener("visibilitychange", () => { if (!document.hidden) pollLoop(); });
