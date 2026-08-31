"""Upload ingest — mime detection + guards (no network)."""
import pytest

from yta import ingest


def test_mime_for():
    assert ingest.mime_for("shot.PNG") == "image/png"
    assert ingest.mime_for("a.jpeg") == "image/jpeg"
    assert ingest.mime_for("doc.pdf") == "application/pdf"
    assert ingest.mime_for("notes.txt") is None


def test_load_paths_rejects_unknown(tmp_path):
    f = tmp_path / "x.txt"
    f.write_text("hi")
    with pytest.raises(ValueError):
        ingest.load_paths([str(f)])


def test_load_paths_reads_bytes(tmp_path):
    f = tmp_path / "p.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n fake")
    media = ingest.load_paths([str(f)])
    assert media == [("image/png", b"\x89PNG\r\n\x1a\n fake")]


def test_load_uploads_from_web():
    media = ingest.load_uploads([
        {"name": "a.pdf", "mime": "application/pdf", "bytes": b"%PDF-1.4"},
        {"name": "b.png", "mime": "image/png", "bytes": b"\x89PNG"},
    ])
    assert [m[0] for m in media] == ["application/pdf", "image/png"]


def test_load_uploads_rejects_non_media():
    with pytest.raises(ValueError):
        ingest.load_uploads([{"name": "x.exe", "mime": "application/octet-stream",
                              "bytes": b"MZ"}])


def test_load_uploads_size_guard():
    with pytest.raises(ValueError):
        ingest.load_uploads([{"name": "big.png", "mime": "image/png",
                              "bytes": b"0" * (21 * 1024 * 1024)}])
