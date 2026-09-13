"""WhatsApp Cloud API (Meta) — the "3rd flow": a customer sends a hotel
booking link or a screenshot on WhatsApp, the bot replies with the same
TripJack price comparison the panel/extension show. Same pipeline
(`yta.extract` + the resolve/pricing step in `yta.web`), just a different
front door — no OTA-specific logic here, same as the other two flows.

Zero extra dependencies beyond `requests` (already used by
`yta.tripjack.client`). Credentials come from `.env`:
  WHATSAPP_ACCESS_TOKEN     — Bearer token (temporary 24h token while
                              testing; swap for a permanent System User
                              token before running unattended)
  WHATSAPP_PHONE_NUMBER_ID  — the sending number's Phone Number ID
  WHATSAPP_VERIFY_TOKEN     — a string WE choose, pasted into Meta's
                              webhook config; proves a GET verification
                              request actually came from Meta's setup UI
  WHATSAPP_API_VERSION      — defaults to v22.0
"""
from __future__ import annotations

import os
import re
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

_GRAPH = "https://graph.facebook.com"
_URL_RE = re.compile(r"https?://\S+")


def _cfg():
    return {
        "token": os.environ.get("WHATSAPP_ACCESS_TOKEN", ""),
        "phone_number_id": os.environ.get("WHATSAPP_PHONE_NUMBER_ID", ""),
        "verify_token": os.environ.get("WHATSAPP_VERIFY_TOKEN", ""),
        "api_version": os.environ.get("WHATSAPP_API_VERSION", "v22.0"),
    }


def configured() -> bool:
    c = _cfg()
    return bool(c["token"] and c["phone_number_id"])


# -- inbound: the webhook verification handshake -------------------------

def verify_challenge(query: dict) -> str | None:
    """Meta's webhook setup screen sends a GET with hub.mode=subscribe,
    hub.verify_token, hub.challenge — echo the challenge back ONLY if the
    token matches ours, else the caller should respond 403."""
    cfg = _cfg()
    mode = (query.get("hub.mode") or [""])[0]
    token = (query.get("hub.verify_token") or [""])[0]
    challenge = (query.get("hub.challenge") or [""])[0]
    if mode == "subscribe" and cfg["verify_token"] and token == cfg["verify_token"]:
        return challenge
    return None


# -- inbound: parsing a delivered webhook payload -------------------------

def parse_inbound(payload: dict) -> list[dict]:
    """Meta's webhook POST body -> a flat list of
    {from, type, text, media_id, mime_type} — one per message. `type` is
    "text" or "image"/"document" (media messages, where `media_id` needs
    `download_media()`); anything else (status updates, reactions, etc.)
    is skipped."""
    out = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            for m in value.get("messages") or []:
                mtype = m.get("type")
                item = {"from": m.get("from"), "type": mtype,
                        "text": None, "media_id": None, "mime_type": None}
                if mtype == "text":
                    item["text"] = (m.get("text") or {}).get("body", "")
                elif mtype in ("image", "document"):
                    media = m.get(mtype) or {}
                    item["media_id"] = media.get("id")
                    item["mime_type"] = media.get("mime_type")
                    item["text"] = media.get("caption")  # a link can ride along as a caption
                else:
                    continue                              # status update / reaction / etc.
                out.append(item)
    return out


def find_url(text: str | None) -> str | None:
    """Pull the first http(s) link out of a message body/caption, if any —
    same "generic, not OTA-specific" rule as everywhere else in this
    project: any URL is fair game, nothing keyed to a particular site."""
    if not text:
        return None
    m = _URL_RE.search(text)
    return m.group(0) if m else None


# -- outbound / media download --------------------------------------------

def download_media(media_id: str) -> tuple[bytes, str] | None:
    """Two-step Graph API fetch: resolve the media id to a short-lived URL,
    then download it (both calls need the same bearer token)."""
    import requests
    cfg = _cfg()
    headers = {"Authorization": f"Bearer {cfg['token']}"}
    r = requests.get(f"{_GRAPH}/{cfg['api_version']}/{media_id}", headers=headers, timeout=20)
    r.raise_for_status()
    meta = r.json()
    url, mime = meta.get("url"), meta.get("mime_type", "application/octet-stream")
    if not url:
        return None
    r2 = requests.get(url, headers=headers, timeout=30)
    r2.raise_for_status()
    return r2.content, mime


def send_text(to: str, body: str) -> dict:
    import requests
    cfg = _cfg()
    r = requests.post(
        f"{_GRAPH}/{cfg['api_version']}/{cfg['phone_number_id']}/messages",
        headers={"Authorization": f"Bearer {cfg['token']}", "Content-Type": "application/json"},
        json={"messaging_product": "whatsapp", "to": to,
              "type": "text", "text": {"body": body, "preview_url": False}},
        timeout=15,
    )
    try:
        out = r.json()
    except ValueError:
        out = {"raw": r.text}
    out["_status_code"] = r.status_code
    return out
