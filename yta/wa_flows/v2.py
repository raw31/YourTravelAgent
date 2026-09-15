"""v2 — the full conversation: a concierge, not a form.

Starts from v1's intent routing (cancel handled in any state, unsupported
content gets a real reply, a fresh link mid-clarification is treated as
switching hotels) — v1.py itself is untouched; this is a separate module
with its own session store so v0 and v1 keep working exactly as they do
today regardless of which flow is active.

What v2 adds on top of v1's routing, arrived at over several rounds of
live testing and review:

  1. Bounded slot-filling instead of a wall-clock timeout
     (_MAX_UNPRODUCTIVE_ATTEMPTS). Live testing proved a session timeout
     wrong twice over — a customer taking 6, 15, or 60 minutes to find
     the right screenshot is still answering the SAME question, not
     starting a new one, and a clock can't tell those apart; content
     can. A reply with no URL is always tried against the open question
     first via extract_clarification(), no matter how much time has
     passed. Only replies that genuinely don't answer anything count
     against the 2-attempt cap before the bot gives up.
  2. A PRESENTED state with a real confirm/decline step — v1 just stops
     once a deal is quoted. v2 asks (WhatsApp reply buttons — the Cloud
     API doesn't support free-text input on a button, only predefined
     tap-replies) and records a lead (yta.leads.db.record_lead) either
     way, so a human has a queue to call from.
  3. The conversation itself was redesigned around a premium-concierge
     register, not a bot filling a form:
       - Never calls itself a "checker" or a tool.
       - Facts you'd actually need to verify — dates, room, the numbers
         — stay in a clean labeled block; the concierge voice is the
         sentence that opens it and the question that closes it, never
         a replacement for the block itself (a version that dissolved
         the facts into pure prose was tried and rejected — harder to
         scan even though it read warmer).
       - Every mandatory field gets a real question a person would
         actually ask (_NATURAL_QUESTIONS), not its internal field
         label recited back ("Total Price Shown on the Page").
       - Acknowledgment phrasing rotates across a small set of genuine
         alternatives (_CHECKING_PHRASES / _FETCHING_PHRASES) instead of
         repeating the exact same line to the same customer every time.
       - A button rides on the SAME message as the text it belongs to
         (the deal/ask becomes the button message's own body) rather
         than trailing in as a separate bubble.
  4. Buttons attached at every real choice point: "Start over" on the
     missing-field ask, "Yes, book this" / "Not now" on a genuine offer,
     "Try another hotel" when there's no live rate at all. Crucially,
     confirm/decline buttons only appear when there's actually something
     worth confirming — a matched rate that ISN'T cheaper than what the
     customer already has gets a plain "nothing better to offer" reply
     with no buttons, same as v0/v1's underlying logic; only "matched AND
     (cheaper OR not comparable)" counts as a real offer.

A session is only ever dropped by: CANCEL (explicit or a fresh URL,
treated as an implicit "different hotel"), the unproductive-attempt cap,
a confirm/decline in PRESENTED, or an error. There's still a very long
(_SESSION_MAX_AGE_SEC) backstop, but that's pure memory hygiene for a
number that genuinely never comes back — not a UX gate.

Session dict shape: {"state": "awaiting_field" | "presented", "packet",
"missing"?, "clarify_text"?, "unproductive_attempts"?, "resolution"?,
"last_activity"}.
"""
from __future__ import annotations

import random
import re
import threading
import time

from yta.wa_shared import occ_repr, wa_send

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
    "I'm here to help you find a better rate on your stay. Send me the "
    "hotel's booking link, or a screenshot showing the hotel, your dates, "
    "room, and the price you were quoted, and I'll take it from there."
)

# Rotated rather than fixed so the same customer never sees the exact
# same script twice in a row -- one of the concrete things that made the
# old version read as a bot no matter how the individual words changed.
_CHECKING_PHRASES = [
    "Let me take a look for you.",
    "One moment, I'll check this now.",
    "Leave this with me for a moment.",
]
_FETCHING_PHRASES = [
    "One moment, I'll get you the best rate I can find.",
    "Let me check what I can secure for you.",
    "Give me just a moment to pull the best rate.",
]
_FOUND_OPENERS = [
    "Here's what I have for your stay — just need a bit more to compare it properly.",
    "Almost there — just need a little more to compare this properly.",
    "Just about set — one more thing and I can compare this properly.",
]

# A real question a person would ask, not the internal field label recited
# back ("Total Price Shown on the Page") -- see extract_llm.FIELD_LABELS
# for the label form this deliberately avoids using here.
_NATURAL_QUESTIONS = {
    "hotel.name": "Which hotel is this?",
    "stay.check_in": "What are your check-in and check-out dates?",
    "stay.check_out": "What are your check-in and check-out dates?",
    "stay.rooms": "How many rooms, and how many guests in each?",
    "stay.occupancy": "How many rooms, and how many guests in each?",
    "requested_offer.room_name": "What room type did you pick?",
    "ota_benchmark.final_payable": "What's the total price shown on the page?",
}
# Short noun phrases for weaving several missing fields into one sentence
# ("I still need the room type and the total price...") rather than a
# bulleted "Missing:" dump.
_NATURAL_NOUNS = {
    "hotel.name": "the hotel",
    "stay.check_in": "your dates",
    "stay.check_out": "your dates",
    "stay.rooms": "how many guests",
    "stay.occupancy": "how many guests",
    "requested_offer.room_name": "the room type",
    "ota_benchmark.final_payable": "the total price",
}


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


def _short_date(iso_str):
    from datetime import date
    try:
        y, m, d = (int(x) for x in iso_str.split("-"))
        return date(y, m, d).strftime("%b %-d")
    except Exception:
        return iso_str


def _clean_room_name(name):
    """Room names come straight from the OTA page or TripJack's own
    catalog, unedited -- "DELUXE ROOM" (shouting caps) and "Deluxe Room."
    (a stray trailing period) both showed up verbatim in live testing.
    Strip the obviously-wrong bits without rewriting a name that already
    has deliberate mixed case."""
    if not name:
        return name
    name = name.strip().rstrip(".").strip()
    if name.isupper():
        name = name.title()
    return name


def _price_rows_block(rows: list) -> str:
    """WhatsApp renders in a proportional font -- hand-padding labels with
    spaces (tried in an earlier version) does NOT line values up, it just
    looks like broken, arbitrary whitespace. Real alignment needs
    WhatsApp's own ```monospace``` block, with padding computed to that
    fixed-width font. `rows` is [(label, value), ...]."""
    if len(rows) == 1:
        label, value = rows[0]
        return f"{label}: {value}"
    labels = [f"{label}:" for label, _ in rows]
    width = max(len(l) for l in labels) + 2
    lines = [labels[i].ljust(width) + rows[i][1] for i in range(len(rows))]
    return "```\n" + "\n".join(lines) + "\n```"


def _recap_block(packet) -> str:
    """The facts a customer would actually want to verify, as a clean
    labeled block -- kept structured on purpose even though the messages
    around it are conversational. See the module docstring: dissolving
    this into prose was tried and rejected as harder to scan."""
    lines = []
    if packet.hotel.name:
        lines.append(f"*{packet.hotel.name}*")
    date_occ = []
    if packet.stay.check_in and packet.stay.check_out:
        date_occ.append(f"{_short_date(packet.stay.check_in)} → {_short_date(packet.stay.check_out)}")
    if packet.stay.occupancy:
        date_occ.append(occ_repr(packet.stay.occupancy))
    if date_occ:
        lines.append(" · ".join(date_occ))
    room_bits = []
    room_name = _clean_room_name(packet.requested_offer.room_name)
    if room_name:
        room_bits.append(room_name)
    if packet.requested_offer.meal_plan:
        room_bits.append(packet.requested_offer.meal_plan)
    if packet.requested_offer.refundable is True:
        room_bits.append("Refundable")
    elif packet.requested_offer.refundable is False:
        room_bits.append("Non-refundable")
    if room_bits:
        lines.append(" · ".join(room_bits))
    if packet.ota_benchmark.final_payable:
        ccy = (packet.ota_benchmark.currency or "").strip()
        lines.append(f"Price shown: {ccy} {packet.ota_benchmark.final_payable:,.0f}".replace("  ", " "))
    return "\n".join(lines)


def _closing_question(missing: list, clarify: str | None = None) -> str:
    if clarify:
        return clarify
    if len(missing) == 1:
        return _NATURAL_QUESTIONS.get(missing[0], "Could you share a bit more detail?")
    nouns = []
    for m in missing:
        n = _NATURAL_NOUNS.get(m)
        if n and n not in nouns:
            nouns.append(n)
    if not nouns:
        return "Could you share a bit more detail, or send a fuller screenshot?"
    if len(nouns) == 1:
        joined = nouns[0]
    elif len(nouns) == 2:
        joined = f"{nouns[0]} and {nouns[1]}"
    else:
        joined = ", ".join(nouns[:-1]) + f", and {nouns[-1]}"
    return f"I still need {joined} to finish comparing — send those, or a fuller screenshot?"


def _found_and_ask_message(packet, missing: list, clarify: str | None = None) -> str:
    question = _closing_question(missing, clarify)
    recap = _recap_block(packet)
    if not recap:
        return f"I wasn't able to pick up much from that screenshot — {question}"
    return f"{random.choice(_FOUND_OPENERS)}\n\n{recap}\n\n{question}"


def _deal_recap_block(packet, best: dict) -> str:
    """Same shape as _recap_block(), but for the actual offer being
    quoted -- room/meal/refundable come from the matched TripJack option
    when available (it can word these slightly differently than what the
    OTA page showed), falling back to the customer's original request
    only where TripJack didn't return its own value. Dates/occupancy
    don't change between what was asked and what's being offered, so
    those still come straight from the packet."""
    lines = []
    if packet.hotel.name:
        lines.append(f"*{packet.hotel.name}*")
    date_occ = []
    if packet.stay.check_in and packet.stay.check_out:
        date_occ.append(f"{_short_date(packet.stay.check_in)} → {_short_date(packet.stay.check_out)}")
    if packet.stay.occupancy:
        date_occ.append(occ_repr(packet.stay.occupancy))
    if date_occ:
        lines.append(" · ".join(date_occ))
    room_bits = []
    room_name = _clean_room_name(best.get("room_name") or packet.requested_offer.room_name)
    if room_name:
        room_bits.append(room_name)
    meal = best.get("meal_basis") or packet.requested_offer.meal_plan
    if meal:
        room_bits.append(meal)
    refundable = best.get("refundable")
    if refundable is None:
        refundable = packet.requested_offer.refundable
    if refundable is True:
        room_bits.append("Refundable")
    elif refundable is False:
        room_bits.append("Non-refundable")
    if room_bits:
        lines.append(" · ".join(room_bits))
    return "\n".join(lines)


def _deal_message(packet, resolution) -> tuple:
    """Returns (text, matched, bookable). `matched`: a room/rate was
    actually found at all. `bookable`: there's a genuine offer worth
    confirming -- matched AND (not directly comparable to the OTA price,
    or it's actually cheaper). A matched rate that ISN'T cheaper gets a
    plain "nothing better to offer" reply with no confirm/decline buttons
    -- there's nothing to confirm -- mirroring wa_shared.whatsapp_reply()'s
    own gate (v2 builds its own message text/structure here rather than
    reusing that function, but keeps the same underlying business logic)."""
    import os

    rz = resolution or {}
    room_map = rz.get("room_map") or {}
    best = None
    if room_map.get("matched"):
        opts = room_map.get("rate_options") or []
        keyed = set(room_map.get("ratekey_option_ids") or [])
        pool = [o for o in opts if o.get("option_id") in keyed] or opts
        if pool:
            best = min(pool, key=lambda o: o.get("total_price", float("inf")))

    if not best:
        name = packet.hotel.name or "this hotel"
        return (f"I wasn't able to find a better live rate for {name} at the moment. "
                f"Happy to take a look at another property, if you'd like?"), False, False

    ota_price = packet.ota_benchmark.final_payable
    ota_ccy = packet.ota_benchmark.currency
    pct = float(os.environ.get("WHATSAPP_MARKUP_PCT", "0") or 0)
    flat = float(os.environ.get("WHATSAPP_MARKUP_FLAT", "0") or 0)
    ccy = best.get("currency", "") or ""
    sell = round(best.get("total_price", 0) * (1 + pct / 100) + flat, 2)
    comparable = bool(ota_price and ota_ccy and ota_ccy.upper() == ccy.upper())

    if comparable and (ota_price - sell) < 0:
        return ("I checked, but the price you already have looks like the best "
                 "deal for this stay — nothing better to offer right now."), True, False

    recap = _deal_recap_block(packet, best)

    lines = ["*Good news — I found you a better rate.*", "", recap, ""]
    if comparable:
        diff = ota_price - sell
        dpct = (diff / ota_price * 100) if ota_price else 0
        lines.append(_price_rows_block([
            ("Your price", f"{ota_ccy} {ota_price:,.0f}"),
            ("BookMyStay price", f"{ccy} {sell:,.2f}"),
            ("You save", f"{ccy} {diff:,.0f} ({dpct:.0f}%)"),
        ]))
    else:
        lines[0] = "*Good news — I found you a rate.*"
        lines.append(_price_rows_block([("BookMyStay price", f"{ccy} {sell:,.2f}")]))
    lines.append("")
    lines.append("Shall I go ahead and secure this for you?")
    return "\n".join(lines), True, True


def _present_deal(frm: str, packet) -> None:
    from yta import whatsapp
    from yta.web import _resolve   # lazy, same reason wa_shared.finish_and_reply does this

    wa_send(frm, random.choice(_FETCHING_PHRASES))
    resolution = _resolve(packet) if packet.hotel.name else None
    text, matched, bookable = _deal_message(packet, resolution)

    if bookable:
        with _WA_SESSIONS_LOCK:
            _WA_SESSIONS[frm] = {"state": "presented", "packet": packet,
                                  "resolution": resolution, "last_activity": time.time()}
        whatsapp.send_buttons(frm, text, [("confirm_book", "Yes, book this"), ("decline_book", "Not now")])
    elif matched:
        wa_send(frm, text)   # a real rate, but not actually cheaper -- nothing to confirm
    else:
        whatsapp.send_buttons(frm, text, [("try_another", "Try another hotel")])
    print(f"[wa v2] batch for {frm} complete (matched={matched} bookable={bookable})", flush=True)


def _run_extraction(frm: str, url, media) -> None:
    from yta import whatsapp
    from yta.pipeline import extract

    wa_send(frm, random.choice(_CHECKING_PHRASES))
    print(f"[wa v2] extracting: url={url!r} has_media={bool(media)}", flush=True)
    packet = extract(url or "", render=bool(url), media=media, log_sink=[])
    print(f"[wa v2] extraction done: hotel={packet.hotel.name!r} status={packet.status}", flush=True)

    missing = packet.missing_mandatory or packet.check_mandatory()
    if missing:
        with _WA_SESSIONS_LOCK:
            _WA_SESSIONS[frm] = {"state": "awaiting_field", "packet": packet, "missing": missing,
                                  "unproductive_attempts": 0, "last_activity": time.time()}
        whatsapp.send_buttons(frm, _found_and_ask_message(packet, missing),
                               [("start_new_chat", "Start over")])
        return
    # Everything needed came in on the first submission -- still show what
    # was actually read before quoting a price, same as the ask-for-more
    # path does, so the customer can catch a bad extraction either way.
    wa_send(frm, f"Got it all — here's what I have:\n\n{_recap_block(packet)}")
    _present_deal(frm, packet)


def _handle_awaiting_field(frm: str, session: dict, items: list) -> None:
    from yta import whatsapp
    from yta.extract_llm import extract_clarification
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
            wa_send(frm, "I wasn't able to pull together everything I need for this one "
                         "just yet — whenever it's convenient, send a fresh link or photo "
                         "and we'll pick up from there.")
            return
        question = _closing_question(session["missing"])
        with _WA_SESSIONS_LOCK:
            session["last_activity"] = time.time()
            session["unproductive_attempts"] = attempts
            _WA_SESSIONS[frm] = session
        whatsapp.send_buttons(frm, question, [("start_new_chat", "Start over")])
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
        whatsapp.send_buttons(frm, _found_and_ask_message(packet, still_missing, clarify),
                               [("start_new_chat", "Start over")])
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
        wa_send(frm, f"Wonderful — I've noted this down. Your reference is *{ref}*, "
                     f"and I'll personally follow up shortly to finalize everything with you.")
        print(f"[wa v2] {frm} confirmed, lead {ref}", flush=True)
        return

    if is_decline:
        ref = record_lead(frm, "declined", session["packet"], session.get("resolution"))
        with _WA_SESSIONS_LOCK:
            _WA_SESSIONS.pop(frm, None)
        wa_send(frm, "Of course — whenever you're ready with the next one, I'm here to help.")
        print(f"[wa v2] {frm} declined, lead {ref}", flush=True)
        return

    wa_send(frm, "Just let me know — tap \"Yes, book this\" to confirm, or \"Not now\" to "
                 "skip. Typing works too, if the buttons aren't showing.")
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
                wa_send(frm, "I'm only able to read text or a photo at the moment — a link "
                             "or a screenshot would be perfect, and I'll take it from there.")
            else:
                wa_send(frm, _ONBOARDING_TEXT)
            return

        # Global intents -- recognized in ANY state, checked before
        # anything state-specific.
        if button_id == "start_new_chat" or (button_id is None and _CANCEL_RE.search(text)):
            if session is not None:
                with _WA_SESSIONS_LOCK:
                    _WA_SESSIONS.pop(frm, None)
                wa_send(frm, "Not a problem at all — send over the next one whenever you're ready.")
            else:
                wa_send(frm, "There's nothing to cancel just yet — send me a hotel's link "
                             "or screenshot whenever you're ready.")
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
