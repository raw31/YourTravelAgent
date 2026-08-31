"""Turn uploaded files (PDF / page screenshots) into media parts for the
vision extraction path.

No local PDF or OCR libraries — the vision model (Gemini) reads PDFs and
scanned images directly.
"""
from __future__ import annotations

import os

_IMAGE_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
    ".heic": "image/heic", ".heif": "image/heif",
}
_MAX_FILES = 12
_MAX_BYTES = 20 * 1024 * 1024        # per file


def mime_for(name: str) -> str | None:
    ext = os.path.splitext(name)[1].lower()
    if ext == ".pdf":
        return "application/pdf"
    return _IMAGE_MIME.get(ext)


def load_paths(paths: list) -> list:
    """[path, ...] -> [(mime, bytes), ...]. Raises ValueError on bad input."""
    media = []
    for p in paths[:_MAX_FILES]:
        mime = mime_for(p)
        if not mime:
            raise ValueError(f"Unsupported file type: {p} (use PDF or an image)")
        data = open(p, "rb").read()
        if len(data) > _MAX_BYTES:
            raise ValueError(f"{p} is {len(data)//1024//1024} MB — over the "
                             f"{_MAX_BYTES//1024//1024} MB limit")
        media.append((mime, data))
    if not media:
        raise ValueError("No files given")
    return media


def load_uploads(uploads: list) -> list:
    """[{name, mime?, bytes}, ...] (from the web UI) -> [(mime, bytes), ...]."""
    media = []
    for u in uploads[:_MAX_FILES]:
        data = u["bytes"]
        mime = u.get("mime") or mime_for(u.get("name", ""))
        if not mime or (not mime.startswith("image/") and mime != "application/pdf"):
            raise ValueError(f"Unsupported file: {u.get('name')!r}")
        if len(data) > _MAX_BYTES:
            raise ValueError(f"{u.get('name')} is too large "
                             f"(> {_MAX_BYTES//1024//1024} MB)")
        media.append((mime, data))
    if not media:
        raise ValueError("No files given")
    return media
