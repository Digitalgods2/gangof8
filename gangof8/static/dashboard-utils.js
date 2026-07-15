// Shared dashboard primitives. Kept independent of screen-specific state so
// the session, settings, and workspace views can evolve independently.
const esc = s => (s ?? "").toString()
  .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
const escAttr = s => esc(s).replaceAll('"', "&quot;").replaceAll("'", "&#39;");
const shortModel = m => (m || "").split("/").pop();
const diffHtml = s => esc(s).split("\n").map(line => {
  const kind = line.startsWith("+") && !line.startsWith("+++") ? "add"
    : line.startsWith("-") && !line.startsWith("---") ? "del"
      : line.startsWith("@@") ? "hunk"
        : line.startsWith("+++") || line.startsWith("---") ? "meta" : "";
  return kind ? `<span class="d-${kind}">${line}</span>` : line;
}).join("\n");
const api = (path, opts = {}) => {
  const request = {...opts};
  const method = String(request.method || "GET").toUpperCase();
  // Session/goal state is live application data, never a cacheable document.
  if (method === "GET" && request.cache === undefined) request.cache = "no-store";
  return fetch(path, request).then(response => response.json());
};

// Async refreshes can finish out of order. A cancelled session must never be
// repainted by an older request that captured its former live state.
function createLatestRequestGate() {
  let generation = 0;
  return {
    begin() { generation += 1; return generation; },
    invalidate() { generation += 1; },
    isCurrent(token) { return token === generation; },
  };
}

// A package's durable milestone points only at its newest attempt. Keep older
// attempts associated with the goal so a resumed run can steer away from a
// historical failure screen and into the replacement session.
function goalSessionIds(goal, sessions = []) {
  const ids = new Set();
  for (const milestone of goal?.milestones || []) {
    if (milestone.session_id) ids.add(milestone.session_id);
    for (const attempt of milestone.attempts || []) {
      if (attempt.session_id) ids.add(attempt.session_id);
    }
  }
  if (goal?.release_session_id) ids.add(goal.release_session_id);
  for (const session of sessions || []) {
    if (session.goal_id === goal?.goal_id && session.session_id) ids.add(session.session_id);
  }
  return [...ids];
}

// Session JSON can remain unchanged while its parent package moves from failed
// attempt N to running attempt N+1. Include parent state in the detail render
// signature so that transition is never left visually stale.
function goalRenderSignature(goal) {
  if (!goal) return "";
  return JSON.stringify({
    status: goal.status,
    display_status: goal.display_status,
    current_index: goal.current_index,
    actionable_session_id: goal.actionable_session_id,
    active_packages: goal.active_packages,
    active_agent_calls: goal.active_agent_calls,
    milestones: (goal.milestones || []).map(m => ({
      package_id: m.package_id,
      status: m.status,
      session_id: m.session_id,
      attempt_count: m.attempt_count,
      session_status: m.session_status,
      active_agent_calls: m.active_agent_calls || [],
    })),
  });
}

function packageAttemptState(session, goal, sessions = []) {
  if (!session?.work_package_id || !goal) return null;
  const milestone = (goal.milestones || []).find(
    item => item.package_id === session.work_package_id
  );
  if (!milestone) return null;
  let attempts = (milestone.attempts || []).map(item => ({...item}));
  if (!attempts.length) {
    attempts = (sessions || [])
      .filter(item => item.goal_id === goal.goal_id &&
        item.work_package_id === session.work_package_id)
      .sort((a, b) => String(a.created_at || "").localeCompare(String(b.created_at || "")))
      .map((item, index) => ({...item, number: index + 1}));
  }
  if (!attempts.some(item => item.session_id === session.session_id)) {
    attempts.push({
      number: attempts.length + 1,
      session_id: session.session_id,
      status: session.status,
      created_at: session.created_at,
    });
  }
  const selected = attempts.find(item => item.session_id === session.session_id) || null;
  const current = attempts.find(item => item.session_id === milestone.session_id) || null;
  return {
    milestone,
    attempts,
    total: attempts.length,
    selected,
    current,
    selectedNumber: selected?.number || 0,
    currentNumber: current?.number || 0,
    isHistorical: !!milestone.session_id && milestone.session_id !== session.session_id,
  };
}

// Export the pure helper for the Node regression test. Browsers load this file
// as a normal script and never enter this branch.
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    createLatestRequestGate, goalSessionIds, goalRenderSignature, packageAttemptState,
  };
}
