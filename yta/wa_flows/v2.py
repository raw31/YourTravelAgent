"""v2 — the full conversation: onboarding, bounded slot-filling, a real
confirm/decline step, and a lead recorded for a human to call.

Starts from v1's intent routing (cancel handled in any state, unsupported
content gets a real reply, a fresh link mid-clarification is treated as
switching hotels) — v1.py itself is untouched; this is a separate module
with its own session store so v0 and v1 keep working exactly as they do
today regardless of which flow is active.

What v2 adds on top:

  1. Onboarding copy — the "I don't understand" fallback actually teaches
     the customer what to send (hotel name/dates/room/price visible in
     the screenshot), instead of a bare one-liner.
  2. A hard cap on unproductive clarification rounds (_MAX_UNPRODUCTIVE_
     ATTEMPTS) instead of a wall-clock session timeout. v1 (and v2's own
     first cut) expired a session after N minutes of silence — live
     testing proved that wrong twice over: a customer who takes 6, 15, or
     24 minutes to go find the right screenshot is still answering the
     SAME question, not starting a new conversation, and a clock can't
     tell those apart. Content can: an incoming reply with no URL is
     always tried against the open question first via
     extract_clarification(), no matter how much time has passed: a
     price screenshot from an hour ago still answers "what's the price."
     Only replies that genuinely don't answer anything (checked by
     content, not by a timer) count against the 2-attempt cap before the
     bot gives up — see the standard "bounded fallback, then handoff"
     pattern this follows.
  3. A PRESENTED state — once a deal is actually quoted, v1 just stops.
     v2 asks the customer to confirm (WhatsApp reply buttons, since the
     Cloud API doesn't support free-text input on a button — only
     predefined tap-replies) and, on confirmation, records a lead
     (yta.leads.db.record_lead) so a human can call and close the booking.
  4. Buttons at every choice point, not just confirm/decline — a
     "Start new chat" button rides along with the missing-field ask and
     the "no live rate" reply, so the customer always has a visible way
     back to the start instead of needing to know to type "cancel".

A session is only ever dropped by: CANCEL (explicit or a fresh URL,
treated as an implicit "different hotel"), the unproductive-attempt cap,
a confirm/decline in PRESENTED, or an error. There's still a very long
(_SESSION_MAX_AGE_SEC) backstop, but that's pure memory hygiene for a
number that genuinely never comes back — not a UX gate, and long enough
that no real reply should ever hit it.

Every path below ends at one of: COMPLETED handled via CONFIRMED/DECLINED,
CANCELLED, GAVE_UP, or ERROR — all of which clear _WA_SESSIONS[frm].
Session dict shape: {"state": "awaiting_field" | "presented", "packet",
"missing"?, "clarify_text"?, "unproductive_attempts"?, "resolution"?,
"last_activity"}.
"""
from __future__ import annotations

import re
import threading
import time

from yta.wa_shared import ask_for_missing, extracted_lines, wa_send, whatsapp_reply

_WA_SESSIONS: dict = {}
_WA_SESSIONS_LOCK = threading.Lock()
# Both 5 and 15 minutes turned out wrong in live testing -- a customer
# asked for a price screenshot needs however long it takes to switch
# apps, find the checkout page, and come back, and no fixed number ever
# covers that reliably. A wall-clock cutoff was the wrong tool for "is
# this reply still relevant" -- content answers that question directly
# (does it fill the field we asked about?), so that's what gates the
# conversation now (_MAX_UNPRODUCTIVE_ATTEMPTS below). This stays only as
# a memory-hygiene backstop for a number that genuinely never replies
# again, not as a UX timeout -- no real reply should ever hit it.
_SESSION_MAX_AGE_SEC = 24 * 60 * 60
_MAX_UNPRODUCTIVE_ATTEMPTS = 2   # "after two fallback attempts, suggest human assistance"

_CANCEL_RE = re.compile(
    r"\b(cancel|start over|start new|new chat|wrong hotel|different hotel|new hotel|never ?mind)\b",
    re.IGNORECASE,
)
_CONFIRM_RE = re.compile(r"\b(yes|confirm|book it|go ahead|book this)\b", re.IGNORECASE)
_DECLINE_RE = re.compile(r"\b(no|not now|skip|later|maybe later)\b", re.IGNORECASE)

_READABLE_TYPES = ("text", "image", "document", "button_reply")

_ONBOARDING_TEXT = (
    "Send me the hotel's booking link, or a screenshot that clearly shows "
    "the hotel name, your dates, room type, and the total price — and "
    "I'll check if there's a better rate."
)


def _text_of(items: list) -> str:
    return " ".join((m.get("text") or "") for m in items).strip()


def _button_id(items: list):
    for m in items:
        if m.get("type") == "button_reply" and m.get("button_id"):
            return m["button_id"]
    return None


def _has_media(items: list) -> bool:
    return any(m.get("type") in ("image", "document") and m.get("media_id") for m in items)


def _all_unreadable(items: list) -> bool:
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
                print(f"[wa v2] downloaded media: {len(data)} bytes, {mime}", flush=True)
    return load_uploads(media_items) if media_items else None


def _send_start_new_chat_button(frm: str, body: str) -> None:
    from yta import whatsapp
    whatsapp.send_buttons(frm, body, [("start_new_chat", "Start new chat")])


def _present_deal(frm: str, packet) -> None:
    """Replaces wa_shared.finish_and_reply() for v2 only, so the choice of
    follow-up buttons can depend on whether a live rate was actually
    found — wa_shared.py itself is untouched (whatsapp_reply() is reused
    as-is; it's pure formatting)."""
    from yta import whatsapp
    from yta.web import _resolve   # lazy, same reason wa_shared.finish_and_reply does this

    wa_send(frm, "Fetching the discounted rates for you.")
    resolution = _resolve(packet) if packet.hotel.name else None
    reply = whatsapp_reply(packet, resolution)
    wa_send(frm, reply)

    matched = bool((resolution or {}).get("room_map", {}).get("matched"))
    if matched:
        with _WA_SESSIONS_LOCK:
            _WA_SESSIONS[frm] = {"state": "presented", "packet": packet,
                                  "resolution": resolution, "last_activity": time.time()}
        whatsapp.send_buttons(frm, "Want to go ahead with this one?",
                               [("confirm_book", "Yes, book this"), ("decline_book", "Not now")])
    else:
        whatsapp.send_buttons(frm, "No live rate on this one right now.",
                               [("try_another", "Try another hotel")])
    print(f"[wa v2] batch for {frm} complete (matched={matched})", flush=True)


def _run_extraction(frm: str, url, media) -> None:
    from yta.pipeline import extract
    wa_send(frm, "Checking your deal")
    print(f"[wa v2] extracting: url={url!r} has_media={bool(media)}", flush=True)
    packet = extract(url or "", render=bool(url), media=media, log_sink=[])
    print(f"[wa v2] extraction done: hotel={packet.hotel.name!r} status={packet.status}", flush=True)
    wa_send(frm, "\n".join(["Your deal:", "----"] + extracted_lines(packet)))
    missing = packet.missing_mandatory or packet.check_mandatory()
    if missing:
        with _WA_SESSIONS_LOCK:
            _WA_SESSIONS[frm] = {"state": "awaiting_field", "packet": packet, "missing": missing,
                                  "unproductive_attempts": 0, "last_activity": time.time()}
        ask_for_missing(frm, missing)
        _send_start_new_chat_button(frm, "Or, if you'd rather start over:")
        return
    _present_deal(frm, packet)


def _handle_awaiting_field(frm: str, session: dict, items: list) -> None:
    from yta.extract_llm import FIELD_LABELS, extract_clarification
    from yta.schema import LLM

    text = _text_of(items)
    media = _download_media(items) if _has_media(items) else None
    accumulated = "\n".join(t for t in (session.get("clarify_text"), text) if t)

    fields, clarify = extract_clarification(session["missing"], accumulated, media=media)
    print(f"[wa v2] clarification filled: {list(fields.keys())}; note={clarify!r}", flush=True)

    if not fields and not clarify:
        attempts = session.get("unproductive_attempts", 0) + 1
        if attempts >= _MAX_UNPRODUCTIVE_ATTEMPTS:
            print(f"[wa v2] {frm} gave up after {attempts} unproductive replies", flush=True)
            with _WA_SESSIONS_LOCK:
                _WA_SESSIONS.pop(frm, None)
            wa_send(frm, "I wasn't able to get the full details for this one after a "
                         "couple of tries — send a fresh link or screenshot whenever "
                         "you're ready, no rush.")
            return
        label = FIELD_LABELS.get(session["missing"][0], "a few more details")
        wa_send(frm, f"I still need {label} to finish checking your deal — "
                     f"or send \"cancel\" to check a different hotel instead.")
        with _WA_SESSIONS_LOCK:
            session["last_activity"] = time.time()
            session["unproductive_attempts"] = attempts
            _WA_SESSIONS[frm] = session
        return

    packet = session["packet"]
    for path, val in fields.items():
        packet.add(path, val, LLM, 0.7, "whatsapp clarification")
    packet.derive_stay()
    still_missing = packet.check_mandatory()
    print(f"[wa v2] still missing after clarification: {still_missing}", flush=True)
    if still_missing:
        with _WA_SESSIONS_LOCK:
            _WA_SESSIONS[frm] = {"state": "awaiting_field", "packet": packet, "missing": still_missing,
                                  "clarify_text": accumulated, "unproductive_attempts": 0,
                                  "last_activity": time.time()}
        ask_for_missing(frm, still_missing, clarify)
        return

    with _WA_SESSIONS_LOCK:
        _WA_SESSIONS.pop(frm, None)
    _present_deal(frm, packet)


def _handle_presented(frm: str, session: dict, items: list, button_id) -> None:
    from yta.leads.db import record_lead

    text = _text_of(items)
    is_confirm = button_id == "confirm_book" or (button_id is None and _CONFIRM_RE.search(text))
    is_decline = button_id == "decline_book" or (button_id is None and _DECLINE_RE.search(text))

    if is_confirm:
        ref = record_lead(frm, "confirmed", session["packet"], session.get("resolution"))
        with _WA_SESSIONS_LOCK:
            _WA_SESSIONS.pop(frm, None)
        wa_send(frm, f"Booked! Your reference is *{ref}* — our team will call you "
                     f"shortly to confirm and complete the booking.")
        print(f"[wa v2] {frm} confirmed, lead {ref}", flush=True)
        return

    if is_decline:
        ref = record_lead(frm, "declined", session["packet"], session.get("resolution"))
        with _WA_SESSIONS_LOCK:
            _WA_SESSIONS.pop(frm, None)
        wa_send(frm, "No problem — send me another hotel's link or a screenshot "
                     "whenever you're ready.")
        print(f"[wa v2] {frm} declined, lead {ref}", flush=True)
        return

    wa_send(frm, "Tap \"Yes, book this\" to confirm or \"Not now\" to skip — "
                 "or just type it if the buttons aren't showing.")
    with _WA_SESSIONS_LOCK:
        session["last_activity"] = time.time()
        _WA_SESSIONS[frm] = session


def handle_batch(frm: str, items: list) -> None:
    with _WA_SESSIONS_LOCK:
        session = _WA_SESSIONS.get(frm)

    if session is not None and time.time() - session.get("last_activity", 0) > _SESSION_MAX_AGE_SEC:
        print(f"[wa v2] session for {frm} abandoned (idle > {_SESSION_MAX_AGE_SEC}s) — clearing it "
              f"(memory hygiene, not a reply-relevance judgment)", flush=True)
        with _WA_SESSIONS_LOCK:
            _WA_SESSIONS.pop(frm, None)
        session = None

    try:
        text = _text_of(items)
        url = _find_url(items)
        has_media = _has_media(items)
        button_id = _button_id(items)

        if not text and not url and not has_media and button_id is None:
            if _all_unreadable(items):
                wa_send(frm, "I can only read text or a hotel link/screenshot right now — "
                             "send one of those and I'll take it from there.")
            else:
                wa_send(frm, _ONBOARDING_TEXT)
            return

        # Global intents -- recognized in ANY state, checked before
        # anything state-specific.
        if button_id == "start_new_chat" or (button_id is None and _CANCEL_RE.search(text)):
            if session is not None:
                with _WA_SESSIONS_LOCK:
                    _WA_SESSIONS.pop(frm, None)
                wa_send(frm, "No problem — send me the hotel's link or a screenshot whenever you're ready.")
            else:
                wa_send(frm, "Nothing to cancel yet — send me a hotel link or a screenshot "
                             "and I'll check the best price for it.")
            return

        if button_id == "try_another":
            wa_send(frm, _ONBOARDING_TEXT)
            return

        if url:
            # A real link is treated as an implicit "different hotel,
            # forget the old one", in any state -- safer to trust than
            # guessing the same from a bare photo.
            if session is not None:
                print(f"[wa v2] {frm} sent a new link mid-conversation — dropping the old session", flush=True)
                with _WA_SESSIONS_LOCK:
                    _WA_SESSIONS.pop(frm, None)
            media = _download_media(items) if has_media else None
            _run_extraction(frm, url, media)
            return

        if session is None:
            if has_media:
                _run_extraction(frm, None, _download_media(items))
                return
            wa_send(frm, _ONBOARDING_TEXT)
            return

        if session.get("state") == "presented":
            _handle_presented(frm, session, items, button_id)
            return

        _handle_awaiting_field(frm, session, items)
    except Exception as e:  # noqa: BLE001
        print(f"[wa v2] ERROR handling batch: {type(e).__name__}: {e}", flush=True)
        with _WA_SESSIONS_LOCK:
            _WA_SESSIONS.pop(frm, None)
        wa_send(frm, f"Sorry, something went wrong: {type(e).__name__}: {e}")
