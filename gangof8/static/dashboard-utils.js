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
const api = (path, opts) => fetch(path, opts).then(response => response.json());
