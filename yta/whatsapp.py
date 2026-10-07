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
    {from, type, text, media_id, mime_type, button_id} — one per message.
    `type` is "text", "image"/"document" (media messages, where `media_id`
    needs `download_media()`), or "button_reply" (a tap on either a
    send_buttons() message OR a send_list() row — both normalize to the
    same shape, since a caller never needs to know which UI produced the
    tap: `button_id` is the id we chose when sending it (a button's own id,
    or a list row's id), `text` carries the visible title so a caller can
    also match on what the customer would have typed instead); anything
    else (status updates, reactions, etc.) is skipped."""
    out = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            for m in value.get("messages") or []:
                mtype = m.get("type")
                item = {"from": m.get("from"), "type": mtype,
                        "text": None, "media_id": None, "mime_type": None,
                        "button_id": None}
                if mtype == "text":
                    item["text"] = (m.get("text") or {}).get("body", "")
                elif mtype in ("image", "document"):
                    media = m.get(mtype) or {}
                    item["media_id"] = media.get("id")
                    item["mime_type"] = media.get("mime_type")
                    item["text"] = media.get("caption")  # a link can ride along as a caption
                elif mtype == "interactive":
                    interactive = m.get("interactive") or {}
                    itype = interactive.get("type")
                    if itype == "list_reply":
                        # A tap on a send_list() row -- normalized into the
                        # exact same {"type": "button_reply", ...} shape a
                        # button tap produces, so callers (session state
                        # handling in wa_flows/*) need zero new branching
                        # to accept either UI.
                        lr = interactive.get("list_reply") or {}
                        item["type"] = "button_reply"
                        item["button_id"] = lr.get("id")
                        item["text"] = lr.get("title")
                        out.append(item)
                        continue
                    if itype != "button_reply":
                        continue                          # anything else -- not sent yet
                    br = interactive.get("button_reply") or {}
                    item["type"] = "button_reply"
                    item["button_id"] = br.get("id")
                    item["text"] = br.get("title")
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


def send_image(to: str, image_url: str, caption: str | None = None) -> dict:
    """A photo, sent as its own message. Takes a public `link`, not an
    uploaded media id -- exactly the shape a hotel's own cover_image URL
    (yta.hoteldb) already is, so there's no download/re-upload step."""
    import requests
    cfg = _cfg()
    image: dict = {"link": image_url}
    if caption:
        image["caption"] = caption
    r = requests.post(
        f"{_GRAPH}/{cfg['api_version']}/{cfg['phone_number_id']}/messages",
        headers={"Authorization": f"Bearer {cfg['token']}", "Content-Type": "application/json"},
        json={"messaging_product": "whatsapp", "to": to, "type": "image", "image": image},
        timeout=15,
    )
    try:
        out = r.json()
    except ValueError:
        out = {"raw": r.text}
    out["_status_code"] = r.status_code
    return out


def send_buttons(to: str, body: str, buttons: list[tuple[str, str]]) -> dict:
    """Interactive reply buttons — up to 3 tappable choices (Meta's own
    limit; a 4th is silently rejected by the API, so callers should never
    pass more). `buttons` is [(id, title), ...] — `id` comes straight back
    on the customer's tap (parse_inbound()'s "button_id"), `title` is what
    they see. A customer can still just type instead of tapping — this is
    an additional affordance, not a replacement for reading plain text.

    Meta caps a button title at 20 characters -- and rejects the ENTIRE
    message (all buttons, the whole body text) if even one title is over,
    not just that button (confirmed live 2026-09-30: a 23-char title on
    v7's onboarding choice silently killed every reply to "hey" for real
    customers, with nothing logged until wa_send_buttons's status check
    was added). Truncated here, same defensive discipline send_list()
    already uses for ITS length caps, so a copy change can never silently
    kill a whole message again -- callers should still write titles that
    fit without truncation, this is a safety net, not a design license."""
    import requests
    cfg = _cfg()
    r = requests.post(
        f"{_GRAPH}/{cfg['api_version']}/{cfg['phone_number_id']}/messages",
        headers={"Authorization": f"Bearer {cfg['token']}", "Content-Type": "application/json"},
        json={"messaging_product": "whatsapp", "to": to, "type": "interactive",
              "interactive": {
                  "type": "button",
                  "body": {"text": body},
                  "action": {"buttons": [
                      {"type": "reply", "reply": {"id": bid, "title": (title or "")[:20]}}
                      for bid, title in buttons[:3]
                  ]},
              }},
        timeout=15,
    )
    try:
        out = r.json()
    except ValueError:
        out = {"raw": r.text}
    out["_status_code"] = r.status_code
    return out


def send_list(to: str, body: str, button_text: str, sections: list[tuple]) -> dict:
    """Interactive LIST message — up to 10 rows total across up to 10
    sections (Meta's own limits; both are truncated here so a caller never
    has to remember them). `sections` is
    [(section_title, [(row_id, row_title, row_description), ...]), ...] --
    `row_id` comes straight back on the customer's tap
    (parse_inbound()'s "button_id", same as a button reply -- see
    parse_inbound's list_reply handling), `row_title`/`row_description`
    are what they see. Meta caps row_title at 24 chars, row_description at
    72, section_title at 24, and button_text at 20 -- truncated here rather
    than left for the API to silently reject. Recommended over
    send_buttons() for more than 3 choices (WhatsApp's own guidance) --
    a customer can still just type instead of tapping, same as buttons."""
    import requests
    cfg = _cfg()
    rows_total = 0
    api_sections = []
    for title, rows in sections:
        if rows_total >= 10:
            break
        take = rows[: 10 - rows_total]
        rows_total += len(take)
        api_sections.append({
            "title": (title or "")[:24],
            "rows": [
                {"id": rid, "title": (rtitle or "")[:24],
                 **({"description": rdesc[:72]} if rdesc else {})}
                for rid, rtitle, rdesc in take
            ],
        })
        if len(api_sections) >= 10:
            break
    r = requests.post(
        f"{_GRAPH}/{cfg['api_version']}/{cfg['phone_number_id']}/messages",
        headers={"Authorization": f"Bearer {cfg['token']}", "Content-Type": "application/json"},
        json={"messaging_product": "whatsapp", "to": to, "type": "interactive",
              "interactive": {
                  "type": "list",
                  "body": {"text": body},
                  "action": {"button": (button_text or "Choose")[:20], "sections": api_sections},
              }},
        timeout=15,
    )
    try:
        out = r.json()
    except ValueError:
        out = {"raw": r.text}
    out["_status_code"] = r.status_code
    return out
