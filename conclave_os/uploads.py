"""Task attachments — multi-modal input for the dashboard composer.

Files arrive as base64 JSON (no multipart dependency), are saved under
DATA_DIR/uploads/, and their TEXT is extracted so the council can actually read
them:
  - text-ish files: decoded directly
  - PDF: text extracted via pypdf (best effort; scanned PDFs yield a note)
  - image: stored and referenced by path only — the text-only CLI agents can't
    see pixels, so we surface the path and say so (honest, not silently broken)

`attachment_context()` turns a list of upload ids into a context block appended
to the task text. Each upload's record (incl. extracted text, capped) is stored
as a sidecar JSON so a submit can fetch it by id.
"""

from __future__ import annotations

import base64
import binascii
import json
from pathlib import Path
from typing import Optional

from .models import short_id, utcnow

_TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".rst", ".csv", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".log", ".py", ".js", ".ts", ".tsx", ".jsx",
    ".go", ".rs", ".java", ".rb", ".c", ".cpp", ".h", ".html", ".css", ".sh", ".sql",
}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
_MAX_TEXT_CHARS = 20_000  # per-attachment cap injected into the prompt


class UploadError(Exception):
    pass


def _kind(name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext == ".pdf":
        return "pdf"
    if ext in _IMAGE_EXTS:
        return "image"
    return "text"  # default: try to read as text


class UploadStore:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir) / "uploads"

    def save(self, name: str, content_b64: str) -> dict:
        """Decode + store a base64 upload, extract its text, return a record
        (without the full text body)."""
        try:
            raw = base64.b64decode(content_b64 or "", validate=True)
        except (binascii.Error, ValueError) as e:
            raise UploadError(f"invalid base64 content: {e}")
        if not raw:
            raise UploadError("empty upload")
        self.dir.mkdir(parents=True, exist_ok=True)
        safe = Path(name or "upload").name or "upload"
        uid = f"up_{short_id()}"
        blob = self.dir / f"{uid}__{safe}"
        blob.write_bytes(raw)
        kind = _kind(safe)
        text, note = self._extract(blob, kind, raw)
        record = {
            "id": uid, "name": safe, "kind": kind, "path": str(blob),
            "chars": len(text), "note": note, "created_at": utcnow(),
        }
        (self.dir / f"{uid}.json").write_text(
            json.dumps({**record, "text": text[:_MAX_TEXT_CHARS]}, ensure_ascii=False),
            encoding="utf-8",
        )
        return record

    def get(self, upload_id: str) -> Optional[dict]:
        p = self.dir / f"{upload_id}.json"
        if not p.is_file():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _extract(blob: Path, kind: str, raw: bytes) -> tuple[str, str]:
        if kind == "text":
            return raw.decode("utf-8", errors="replace"), ""
        if kind == "pdf":
            try:
                import pypdf

                reader = pypdf.PdfReader(str(blob))
                txt = "\n".join((page.extract_text() or "") for page in reader.pages)
                note = f"{len(reader.pages)} page(s)" if txt.strip() else \
                    "no extractable text (scanned/image PDF?)"
                return txt, note
            except Exception as e:  # noqa: BLE001 — never fail the upload
                return "", f"PDF text extraction failed: {e}"
        if kind == "image":
            return "", "image stored; text-only agents cannot view it (path provided)"
        return "", ""


def attachment_context(store: UploadStore, upload_ids: list[str]) -> str:
    """Build the context block appended to a task's text for its attachments."""
    blocks: list[str] = []
    for uid in upload_ids or []:
        rec = store.get(uid)
        if not rec:
            continue
        if rec["kind"] == "image":
            blocks.append(
                f"[Attached image: {rec['name']} — saved at {rec['path']}; "
                "not visually interpreted by text-only agents]"
            )
        elif (rec.get("text") or "").strip():
            blocks.append(f"--- Attached {rec['kind']} file: {rec['name']} ---\n{rec['text']}")
        else:
            blocks.append(f"[Attached {rec['kind']}: {rec['name']} — {rec.get('note') or 'no text extracted'}]")
    if not blocks:
        return ""
    return "\n\nAttachments provided by the user:\n" + "\n\n".join(blocks)
