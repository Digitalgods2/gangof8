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

// Export the pure helper for the Node regression test. Browsers load this file
// as a normal script and never enter this branch.
if (typeof module !== "undefined" && module.exports) {
  module.exports = {createLatestRequestGate};
}
