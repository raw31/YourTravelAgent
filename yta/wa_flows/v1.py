"""v1 — intent-routed WhatsApp flow.

Same underlying pipeline as v0 (extract(), extract_clarification(),
check_mandatory(), the TripJack resolve) — none of that changes here.
What changes is HOW an incoming message gets routed. v0 has exactly one
branch ("is there an open session?") and reads every message through
whichever question is currently pending; anything that doesn't fit gets
silently dropped and the same question repeats forever. That's the root
cause behind three separate bugs found in v0 (a photo reply during
clarification, a stale session answered days later, "cancel"/"start
over" not being recognized) — one pattern wearing three costumes.

v1 works out what an incoming message actually IS first — an answer, a
cancel, a new hotel, something we can't read at all, or genuinely
unclear — against one of two named states, and every (state, intent)
pair has a defined reply instead of a repeat:

  state NEW            no open question
  state AWAITING_FIELD  bot is waiting on specific missing field(s)

  intent                NEW            AWAITING_FIELD
  ------------------------------------------------------------------
  link/photo, no url    (n/a)          try extract_clarification()
                                        first (the field being asked
                                        about might be answered by a
                                        photo) — see _handle_reply
  link present           run extract()  drop the old session, run
                                        extract() fresh (a URL is a
                                        strong, low-risk signal of a
                                        genuinely different hotel —
                                        much safer than guessing that
                                        from a bare photo)
  "cancel"/"start over"  (n/a)          drop session, ask for a link
  unsupported content    "send a link/  same, but the pending
  (voice/sticker/etc.)   screenshot"    question stays open
  nothing usable         same as above  re-ask the SPECIFIC missing
                                        field, offer "cancel"

A session with no activity for over _SESSION_TIMEOUT_SEC is treated as
expired: it's dropped and the incoming message is re-evaluated as if
state were NEW, so a stale "just the answer" reply (no hotel context
left to attach it to) gets a real re-ask instead of being silently
absorbed into a conversation the customer may have abandoned.
"""
from __future__ import annotations

import re
import threading
import time

from yta.wa_shared import ask_for_missing, extracted_lines, finish_and_reply, wa_send

_WA_SESSIONS: dict = {}
_WA_SESSIONS_LOCK = threading.Lock()
_SESSION_TIMEOUT_SEC = 5 * 60

_CANCEL_RE = re.compile(
    r"\b(cancel|start over|wrong hotel|different hotel|new hotel|never ?mind)\b",
    re.IGNORECASE,
)

_READABLE_TYPES = ("text", "image", "document")


def _text_of(items: list) -> str:
    return " ".join((m.get("text") or "") for m in items).strip()


def _has_media(items: list) -> bool:
    return any(m.get("type") in ("image", "document") and m.get("media_id") for m in items)


def _all_unreadable(items: list) -> bool:
    # Every item is a type we genuinely can't read (voice note, sticker,
    # location, contact card...) — no text, no downloadable image/document.
    return bool(items) and all(m.get("type") not in _READABLE_TYPES for m in items)


def _find_url(items: list):
    from yta import whatsapp
    for m in items:
        url = whatsapp.find_url(m.get("text"))
        if url:
            return url
    return None


def _download_media(items: list):
    from yta import whatsapp
    from yta.ingest import load_uploads
    media_items = []
    for m in items:
        if m.get("type") in ("image", "document") and m.get("media_id"):
            dl = whatsapp.download_media(m["media_id"])
            if dl:
                data, mime = dl
                media_items.append({"name": "whatsapp-media", "mime": mime, "bytes": data})
                print(f"[wa v1] downloaded media: {len(data)} bytes, {mime}", flush=True)
    return load_uploads(media_items) if media_items else None


def _run_extraction(frm: str, url, media) -> None:
    from yta.pipeline import extract
    wa_send(frm, "Checking your deal")
    print(f"[wa v1] extracting: url={url!r} has_media={bool(media)}", flush=True)
    packet = extract(url or "", render=bool(url), media=media, log_sink=[])
    print(f"[wa v1] extraction done: hotel={packet.hotel.name!r} status={packet.status}", flush=True)
    wa_send(frm, "\n".join(["Your deal:", "----"] + extracted_lines(packet)))
    missing = packet.missing_mandatory or packet.check_mandatory()
    if missing:
        with _WA_SESSIONS_LOCK:
            _WA_SESSIONS[frm] = {"packet": packet, "missing": missing, "last_activity": time.time()}
        ask_for_missing(frm, missing)
        return
    finish_and_reply(frm, packet)


def _handle_reply(frm: str, session: dict, items: list) -> None:
    """State AWAITING_FIELD, message has no URL and isn't a cancel — try it
    as an answer (text and/or photo) before giving up on it."""
    from yta.extract_llm import FIELD_LABELS, extract_clarification
    from yta.schema import LLM

    text = _text_of(items)
    media = _download_media(items) if _has_media(items) else None
    accumulated = "\n".join(t for t in (session.get("clarify_text"), text) if t)

    fields, clarify = extract_clarification(session["missing"], accumulated, media=media)
    print(f"[wa v1] clarification filled: {list(fields.keys())}; note={clarify!r}", flush=True)

    if not fields and not clarify:
        # Didn't answer what we asked — off-topic question, or a photo/text
        # about something else entirely. Rather than silently repeat the
        # exact same ask (v0's bug), name the field again and offer a way
        # out, and keep the session open in case the NEXT message answers it.
        label = FIELD_LABELS.get(session["missing"][0], "a few more details")
        wa_send(frm, f"I still need {label} to finish checking your deal — "
                     f"or send \"cancel\" to check a different hotel instead.")
        with _WA_SESSIONS_LOCK:
            session["last_activity"] = time.time()
            _WA_SESSIONS[frm] = session
        return

    packet = session["packet"]
    for path, val in fields.items():
        packet.add(path, val, LLM, 0.7, "whatsapp clarification")
    packet.derive_stay()
    still_missing = packet.check_mandatory()
    print(f"[wa v1] still missing after clarification: {still_missing}", flush=True)
    if still_missing:
        with _WA_SESSIONS_LOCK:
            _WA_SESSIONS[frm] = {"packet": packet, "missing": still_missing,
                                  "clarify_text": accumulated, "last_activity": time.time()}
        ask_for_missing(frm, still_missing, clarify)
        return

    with _WA_SESSIONS_LOCK:
        _WA_SESSIONS.pop(frm, None)
    finish_and_reply(frm, packet)


def handle_batch(frm: str, items: list) -> None:
    with _WA_SESSIONS_LOCK:
        session = _WA_SESSIONS.get(frm)

    if session is not None and time.time() - session.get("last_activity", 0) > _SESSION_TIMEOUT_SEC:
        print(f"[wa v1] session for {frm} expired (idle > {_SESSION_TIMEOUT_SEC}s) — starting fresh", flush=True)
        with _WA_SESSIONS_LOCK:
            _WA_SESSIONS.pop(frm, None)
        session = None

    try:
        text = _text_of(items)
        url = _find_url(items)
        has_media = _has_media(items)

        if not text and not url and not has_media:
            if _all_unreadable(items):
                wa_send(frm, "I can only read text or a hotel link/screenshot right now — "
                             "send one of those and I'll take it from there.")
            else:
                wa_send(frm, "Send me a hotel booking link or a screenshot "
                             "of one and I'll check the best price for it.")
            return

        if _CANCEL_RE.search(text):
            if session is not None:
                with _WA_SESSIONS_LOCK:
                    _WA_SESSIONS.pop(frm, None)
                wa_send(frm, "No problem — send me the hotel's link or a screenshot whenever you're ready.")
            else:
                wa_send(frm, "Nothing to cancel yet — send me a hotel link or a screenshot "
                             "and I'll check the best price for it.")
            return

        if url:
            # A real link mid-clarification is treated as an implicit
            # "different hotel, forget the old one" — safer to trust than
            # guessing the same from a bare photo (see module docstring).
            if session is not None:
                print(f"[wa v1] {frm} sent a new link mid-clarification — dropping the old session", flush=True)
                with _WA_SESSIONS_LOCK:
                    _WA_SESSIONS.pop(frm, None)
            media = _download_media(items) if has_media else None
            _run_extraction(frm, url, media)
            return

        if session is None:
            if has_media:
                _run_extraction(frm, None, _download_media(items))
                return
            wa_send(frm, "Send me a hotel booking link or a screenshot "
                         "of one and I'll check the best price for it.")
            return

        _handle_reply(frm, session, items)
    except Exception as e:  # noqa: BLE001
        print(f"[wa v1] ERROR handling batch: {type(e).__name__}: {e}", flush=True)
        with _WA_SESSIONS_LOCK:
            _WA_SESSIONS.pop(frm, None)
        wa_send(frm, f"Sorry, something went wrong: {type(e).__name__}: {e}")
