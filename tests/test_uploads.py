"""Task attachments — multi-modal composer input.

Uploads arrive as base64 JSON, are stored + text-extracted (text/PDF), and
their content is folded into the task the council reads. Images are stored and
referenced by path (text-only agents can't view them). The submit endpoint
accepts upload ids and records them on the session.
"""

import base64

import pytest
from fastapi.testclient import TestClient

from conclave_os.service import ConclaveService
from conclave_os.uploads import UploadError, UploadStore, attachment_context, image_inputs


def _b64(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


# ---- UploadStore -------------------------------------------------------------


def test_save_text_extracts_content(tmp_path):
    store = UploadStore(tmp_path)
    rec = store.save("notes.md", _b64("# Title\nbody text"))
    assert rec["kind"] == "text"
    got = store.get(rec["id"])
    assert got["text"] == "# Title\nbody text"


def test_save_image_stored_not_extracted(tmp_path):
    store = UploadStore(tmp_path)
    rec = store.save("pic.png", _b64("\x89PNG fake bytes"))
    assert rec["kind"] == "image"
    assert rec["chars"] == 0
    assert "vision-capable" in rec["note"]
    assert (tmp_path / "uploads").exists()


def test_save_bad_pdf_is_graceful(tmp_path):
    store = UploadStore(tmp_path)
    rec = store.save("doc.pdf", _b64("not really a pdf"))
    assert rec["kind"] == "pdf"
    assert rec["chars"] == 0  # extraction failed, but no crash
    assert "PDF" in rec["note"]


def test_invalid_base64_and_empty_raise(tmp_path):
    store = UploadStore(tmp_path)
    with pytest.raises(UploadError):
        store.save("x.txt", "!!!not base64!!!")
    with pytest.raises(UploadError):
        store.save("x.txt", _b64(""))


def test_attachment_context_blocks(tmp_path):
    store = UploadStore(tmp_path)
    t = store.save("a.txt", _b64("hello world"))
    img = store.save("p.jpg", _b64("imgbytes"))
    ctx = attachment_context(store, [t["id"], img["id"]])
    assert "Attached text file: a.txt" in ctx
    assert "hello world" in ctx
    assert "Attached image: p.jpg" in ctx
    assert attachment_context(store, []) == ""


def test_image_inputs_resolves_supported_images(tmp_path):
    store = UploadStore(tmp_path)
    png = store.save("shot.png", _b64("pngbytes"))
    svg = store.save("logo.svg", _b64("<svg/>"))
    txt = store.save("a.txt", _b64("hi"))
    atts = [
        {"id": png["id"], "name": "shot.png", "kind": "image"},
        {"id": svg["id"], "name": "logo.svg", "kind": "image"},   # unsupported → skipped
        {"id": txt["id"], "name": "a.txt", "kind": "text"},        # not an image → skipped
    ]
    inputs = image_inputs(tmp_path, atts)
    assert len(inputs) == 1
    assert inputs[0]["media_type"] == "image/png"
    assert inputs[0]["path"].endswith("shot.png")


# ---- service + endpoint ------------------------------------------------------


def test_run_folds_attachment_text_into_task(tmp_path):
    svc = ConclaveService(data_dir=tmp_path)
    rec = svc.save_upload("spec.txt", _b64("REQUIREMENT: be fast"))
    session = svc.run("Summarize the attached spec", source="test", attachments=[rec["id"]])
    assert "REQUIREMENT: be fast" in session.task.text
    assert session.attachments == [{"id": rec["id"], "name": "spec.txt", "kind": "text"}]


def test_upload_and_task_endpoints(tmp_path):
    from conclave_os import main as main_mod

    main_mod.service = ConclaveService(data_dir=tmp_path)
    client = TestClient(main_mod.app)
    up = client.post("/uploads", json={"name": "ctx.md", "content_base64": _b64("context here")})
    assert up.status_code == 200
    uid = up.json()["id"]
    r = client.post("/tasks", json={"text": "use the context", "attachments": [uid]})
    assert r.status_code == 200
    assert r.json()["attachments"][0]["name"] == "ctx.md"


def test_upload_endpoint_rejects_bad_base64(tmp_path):
    from conclave_os import main as main_mod

    main_mod.service = ConclaveService(data_dir=tmp_path)
    client = TestClient(main_mod.app)
    r = client.post("/uploads", json={"name": "x.txt", "content_base64": "@@@"})
    assert r.status_code == 422
