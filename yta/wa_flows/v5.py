"""v5 — v4's conversation, plus an upfront choice between "I already have
a deal" and "search a hotel for me", and a way to present several rate
options (not just one) when the customer picks the latter.

Starts as an exact copy of v4.py -- v4.py itself is untouched and keeps
running unaffected; this is a separate module with its own session
store, same as v1/v2/v3/v4 each having their own.

What v5 adds on top:

  9. Onboarding is now a choice, not one prompt. handle_batch's every
     "show onboarding" moment (first contact, an unreadable message,
     "try another hotel", falling through with no session) sends
     _ONBOARDING_CHOICE_TEXT with two buttons -- "I have a deal" / "Search
     a hotel" -- instead of unconditionally asking for a full single-room
     deal. Tapping one sends the matching follow-up prompt
     (_ONBOARDING_TEXT for "I have a deal", _SEARCH_TEXT for "Search a
     hotel") and remembers the choice (_PENDING_PATH, same
     independent-of-session-lifecycle pattern as _PENDING_REFERRALS) until
     the next real submission consumes it.
  10. The two paths ask different things of `check_mandatory()`'s output
     (_effective_missing): "I have a deal" adds `requested_offer.room_name`
     BACK into what's asked for even though yta/schema.py no longer makes
     it globally mandatory -- someone who says they have a specific room
     picked out should still be asked for it. "Search a hotel" REMOVES
     `ota_benchmark.final_payable` from what's asked for -- there's no OTA
     price to compare against when the whole point is finding one, so
     requiring it would stall the conversation on a field that doesn't
     apply. A customer who ignores the buttons entirely (pastes a link out
     of habit) gets neither override -- exactly today's v4 behavior,
     schema.py's own mandatory set is already enough for both a full
     single-room deal and a no-room submission on its own.
  11. When _resolve() comes back with `room_options` (no room was ever
     requested) instead of `room_map`, _present_deal hands off to
     _present_option_choices -- a numbered text list of up to 4 rate
     options (TripJack's own hotel name/room name, same as the single-
     room deal card), one per available optionType, with a new
     "choosing_option" session state. A numeric reply maps to the picked
     RateOption, which is repackaged into a synthetic single-option
     `room_map` and handed to the SAME `_send_deal_result()` the single-
     room path already uses -- so confirm/decline, lead recording, and
     the referral ask are 100% reused, unmodified, for a picked option.
     WhatsApp's reply-button cap (3) is why this is a numbered TEXT list
     with a text reply, not native tappable rows -- this codebase has no
     WhatsApp list-message support (a real "list" interactive message,
     up to 10 rows) built yet; flagged as a nicer follow-up, not required
     for this to work.

What v4 already does, carried over unchanged below (see v4.py's own
docstring for the full reasoning on each):

  8. Plain text, with no open session, can now start a submission on its
     own -- previously ONLY a URL or a photo could (handle_batch's
     `session is None` branch sent free text straight to _ONBOARDING_TEXT,
     unconditionally, no matter what it said). extract() already accepts
     a `page_text` param and runs the identical LLM extraction against it
     that it runs on a rendered page or an upload -- this is a routing
     change in v4.py, not new extraction machinery.
       - A cheap pre-filter (_looks_like_a_query: a minimum length plus a
         small chit-chat regex) runs BEFORE any LLM call, so "hi"/"thanks"/
         "ok" keep costing nothing and getting onboarding, exactly as
         before -- the filter is not the thing deciding whether this is a
         real query, just what's cheap enough to reject for free.
       - Anything that passes the filter goes through _run_extraction with
         `page_text=text` in place of a URL. If a hotel name comes through
         at all, it's treated as a real (if partial) submission and lands
         in `awaiting_field` exactly like an under-informative photo would
         today. If NOTHING recognizable came through -- the filter let
         through a long-but-irrelevant message -- a soft nudge is sent
         instead of dragging the customer into a full slot-filling
         interrogation seeded from nothing.
       - Deliberately NOT changed: `awaiting_field` and `presented` state
         handling. Free text there already means something specific
         (answer the open question; confirm/decline) and isn't given a
         second interpretation in this pass -- a customer wanting to pivot
         mid-conversation still uses the existing _CANCEL_RE phrases
         ("new hotel", "start over"), same as in v3. A fresh URL still
         always wins and resets any open session (unchanged); a strong
         free-text match does NOT get that same override power yet --
         auto-detecting a full pivot from unstructured text was judged the
         riskiest part of this and is left for a later pass if it turns
         out people actually try it.

What v3 already does, carried over unchanged below (see v3.py's own
docstring for the full reasoning on each):

  5. A referral ask after a CONFIRMED booking only (never on decline) --
     the GTM plan for personal-network reach concluded referrals are the
     only real growth lever until there are proof points, and the right
     moment to ask is right after a good outcome, referencing the actual
     money just saved.
  6. The ask doesn't require the new person to type or remember a code --
     that's real friction for exactly the person you want to have zero
     friction. Instead the referrer gets one forward-ready message with a
     wa.me deep link baked in (https://wa.me/<number>?text=...): tapping
     it opens a chat with this bot with a referral mention already
     typed in, so the friend just hits send. Forwarding that message to a
     DM or posting it to a WhatsApp Status are both native WhatsApp
     actions on any received message -- nothing new to build for that
     part, just a message worth forwarding.
  7. Basic attribution: a referral code is just a prior booking_ref
     (BMS-XXXXXXXX). Any incoming message mentioning one --
     at any point, not just the message that starts a submission -- gets
     it stashed per phone number (_PENDING_REFERRALS) until that
     conversation eventually confirms or declines a deal, at which point
     it's attached to the new lead row (yta.leads.db.record_lead's
     referred_by). yta/leads/schema.sql and db.py are shared with v2 --
     the referred_by column and the record_lead() parameter are additive,
     so v2's own confirm/decline calls are unaffected by their presence.

What v2 already does, carried over unchanged below (see v2.py's own
docstring for the full reasoning on each):

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

Session dict shape: {"state": "awaiting_field" | "presented" |
"choosing_option", "packet", "missing"?, "intent"? ("deal"|"search",
awaiting_field only), "clarify_text"?, "unproductive_attempts"?,
"resolution"?, "options"? (choosing_option only), "last_activity"}.
"""
from __future__ import annotations

import random
import re
import threading
import time

from yta.wa_shared import (
    occ_repr, wa_send,
    CHECKING_PHRASES as _CHECKING_PHRASES,
    FETCHING_PHRASES as _FETCHING_PHRASES,
    FOUND_OPENERS as _FOUND_OPENERS,
    NATURAL_QUESTIONS as _NATURAL_QUESTIONS,
    NATURAL_NOUNS as _NATURAL_NOUNS,
    short_date as _short_date,
    clean_room_name as _clean_room_name,
    price_comparison_lines as _price_comparison_lines,
    price_line as _price_line,
    recap_block as _recap_block,
    closing_question as _closing_question,
    found_and_ask_message as _found_and_ask_message,
    deal_recap_block as _deal_recap_block,
    deal_message as _deal_message,
)

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

# Gates whether a plain-text message with no open session is even worth an
# extraction call, cheaply, before touching the LLM at all -- a greeting or
# an ack should cost nothing and still just get onboarding, exactly as
# every plain-text message did before v4. This is NOT what decides whether
# something IS a genuine booking query -- that's still the extractor's job
# (see _run_extraction's hotel-name check) -- it's only what's cheap enough
# to reject for free without ever calling it.
_MIN_QUERY_LEN = 15
_CHITCHAT_RE = re.compile(
    r"^\s*(hi+|hello+|hey+|yo|thanks?( you)?|thank you|ok(ay)?|k|bye|"
    r"good\s?(morning|evening|afternoon)|sure|cool|great|nice)\s*[!.?]*\s*$",
    re.IGNORECASE,
)


def _looks_like_a_query(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < _MIN_QUERY_LEN:
        return False
    if _CHITCHAT_RE.match(t):
        return False
    return True

# A referral code is just a prior booking reference. Checked against
# EVERY incoming message (not only the one starting a submission) since
# someone tapping a shared wa.me link sends the mention as its own first
# message, often before they've attached a hotel link/photo at all.
_REFERRAL_RE = re.compile(r"\bBMS-[0-9A-F]{8}\b", re.IGNORECASE)
# phone -> referral code, held until that conversation confirms/declines
# a deal (or is cleared alongside the session on cancel/give-up/error) --
# deliberately NOT tied to the session dict's own lifecycle, since the
# code can arrive before any real submission does.
_PENDING_REFERRALS: dict = {}

_READABLE_TYPES = ("text", "image", "document", "button_reply")

_ONBOARDING_CHOICE_TEXT = (
    "Hi! I'm here to find you a better rate before you book.\n\n"
    "Do you already have a specific room picked out, or would you like "
    "me to search a hotel for you?"
)

# Follow-up once "I have a deal" is tapped -- a specific room really is
# expected on this path (see _effective_missing).
_ONBOARDING_TEXT = (
    "Once you've finalized the room and hotel, send me the link or a "
    "screenshot showing:\n"
    "• Hotel name\n"
    "• Dates\n"
    "• Guest count\n"
    "• Room type\n"
    "• Total price\n\n"
    "A couple of screenshots work just as well if it doesn't fit in one. "
    "If I find a better deal, I'll show you the savings — no obligation "
    "to book through me."
)

# Follow-up once "Search a hotel" is tapped -- deliberately never asks for
# a room or a price: there's no OTA deal to compare against on this path
# (see _effective_missing), just a live look at what's available.
_SEARCH_TEXT = (
    "Great — send me the hotel name, your dates, and how many guests, "
    "and I'll show you a few live room options to choose from. No need "
    "to pick a room first."
)

# phone -> "deal" | "search", set when the matching onboarding button is
# tapped, consumed by the NEXT real submission (see _run_extraction) --
# same independent-of-session-lifecycle reasoning as _PENDING_REFERRALS,
# since the choice is made before any session exists.
_PENDING_PATH: dict = {}


def _effective_missing(packet, missing: list, intent: str | None) -> list:
    """`missing` is packet.check_mandatory()'s own list -- schema.py's
    mandatory set already supports both a full single-room deal and a
    no-room submission on its own (the default, `intent=None`, when a
    customer bypasses the buttons entirely). An explicit intent layers the
    ONE additional expectation that path implies, without changing what
    schema.py itself considers mandatory for every other flow."""
    if intent == "search":
        return [m for m in missing if m != "ota_benchmark.final_payable"]
    if intent == "deal":
        if not packet.requested_offer.room_name and "requested_offer.room_name" not in missing:
            return missing + ["requested_offer.room_name"]
        return missing
    return missing


def _send_onboarding_choice(frm: str) -> None:
    from yta import whatsapp
    whatsapp.send_buttons(frm, _ONBOARDING_CHOICE_TEXT,
                           [("have_deal", "I have a deal"), ("search_hotel", "Search a hotel")])


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
                print(f"[wa v5] downloaded media: {len(data)} bytes, {mime}", flush=True)
    return load_uploads(media_items) if media_items else None


def _referral_share_messages(booking_ref: str) -> tuple:
    """Returns (ask_text, shareable_text) as two SEPARATE messages, not
    one -- WhatsApp's forward action grabs the whole message bubble, so
    bundling instructions in with the shareable text would forward the
    instructions too. The second message is nothing BUT the shareable
    line, so "tap and hold, then Forward" on it forwards something clean.

    There's no way for a business-sent message to trigger WhatsApp's own
    Forward/Share-to-Status picker directly -- that's a long-press
    gesture inside WhatsApp itself, not something the Cloud API exposes
    to any app, ours included -- so the instruction has to be spelled out
    rather than assumed. The wa.me deep link pre-fills the referral
    mention for whoever taps it, so the new person never has to type or
    remember a code themselves -- they just hit send on a message that
    already says who referred them.

    Deliberately does NOT restate the savings figure -- the confirm
    message sent right before this already says "I've secured X for Y
    (Z less than what you had)" (see _deal_message()'s confirm_line), so
    repeating "you just saved Z" here read like the bot forgot what it
    just said one message earlier."""
    import os
    from urllib.parse import quote
    number = os.environ.get("WHATSAPP_DISPLAY_NUMBER", "").strip()
    if not number:
        # No number configured to build a deep link from -- fall back to
        # asking them to mention the code themselves, as one message.
        ask = (f"If a friend's got a trip coming up, I'd love to help "
               f"them too. Send them my number and have them mention your "
               f"reference *{booking_ref}* — I'll take good care of them too.")
        return ask, None

    prefill = quote(f"Hi, I was referred by {booking_ref}")
    link = f"https://wa.me/{number}?text={prefill}"
    ask = ("If a friend's got a trip coming up, I'd love to help "
           "them too. Tap and hold the next message, then choose "
           "*Forward* or *Share to Status*.")
    shareable = f"I just found a better hotel rate through BookMyStay — check yours here: {link}"
    return ask, shareable


def _send_deal_result(frm: str, packet, resolution: dict | None) -> None:
    """Builds and sends the final deal message from a resolution that
    names exactly ONE candidate rate (resolution["room_map"], a single-
    entry rate_options list) -- shared by the single-room-request path
    (_present_deal) and the pick-one-of-N path (_handle_choosing_option,
    once a customer picks from the option list), so both end up in the
    exact same confirm/decline + lead-recording flow."""
    from yta import whatsapp
    text, matched, bookable, savings_line, confirm_line = _deal_message(packet, resolution)

    if bookable:
        with _WA_SESSIONS_LOCK:
            _WA_SESSIONS[frm] = {"state": "presented", "packet": packet,
                                  "resolution": resolution, "savings_line": savings_line,
                                  "confirm_line": confirm_line, "last_activity": time.time()}
        whatsapp.send_buttons(frm, text, [("confirm_book", "Yes, book this"), ("decline_book", "Not now")])
    elif matched:
        wa_send(frm, text)   # a real rate, but not actually cheaper -- nothing to confirm
    else:
        whatsapp.send_buttons(frm, text, [("try_another", "Try another hotel")])
    print(f"[wa v5] batch for {frm} complete (matched={matched} bookable={bookable})", flush=True)


def _present_option_choices(frm: str, packet, resolution: dict) -> None:
    """The no-requested-room path: resolution["room_options"] names up to
    4 representative rate options (yta.roommap.list_by_option_type()), one
    per TripJack optionType -- nothing was matched/ranked against a
    specific request, so there's no single "best" to lead with. Shows them
    as a numbered text list (WhatsApp's reply buttons cap at 3, and this
    codebase has no list-message support to show up to 4 as tappable rows)
    and opens a "choosing_option" session for the numeric reply."""
    from yta import whatsapp

    rz = resolution or {}
    options = ((rz.get("room_options") or {}).get("options")) or []
    hotel_name = (rz.get("detail") or {}).get("hotel_name") \
        or (rz.get("match") or {}).get("hotel_name") \
        or packet.hotel.name or "this hotel"

    if not options:
        whatsapp.send_buttons(
            frm, f"I wasn't able to find any live rates for {hotel_name} for these dates "
                 f"right now. Happy to take a look at another property, if you'd like?",
            [("try_another", "Try another hotel")])
        print(f"[wa v5] batch for {frm} complete (no room_options available)", flush=True)
        return

    lines = [f"🏨 *{hotel_name}*"]
    date_occ = []
    if packet.stay.check_in and packet.stay.check_out:
        date_occ.append(f"{_short_date(packet.stay.check_in)} → {_short_date(packet.stay.check_out)}")
    if packet.stay.occupancy:
        date_occ.append(occ_repr(packet.stay.occupancy))
    if date_occ:
        lines.append("📅 " + " · ".join(date_occ))
    lines += ["", "Here's what's available:", ""]

    numerals = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"]
    for i, opt in enumerate(options):
        bits = [_clean_room_name(opt.get("room_name")) or "Room"]
        if opt.get("meal_basis"):
            bits.append(opt["meal_basis"])
        if opt.get("refundable") is True:
            bits.append("Refundable")
        elif opt.get("refundable") is False:
            bits.append("Non-refundable")
        ccy = opt.get("currency") or ""
        price = opt.get("total_price") or 0
        lines.append(f"{numerals[i]} {' · '.join(bits)} — {ccy} {price:,.2f}")
    lines += ["", f"Reply with a number (1–{len(options)}) to pick one."]

    with _WA_SESSIONS_LOCK:
        _WA_SESSIONS[frm] = {"state": "choosing_option", "packet": packet,
                              "resolution": resolution, "options": options,
                              "unproductive_attempts": 0, "last_activity": time.time()}
    wa_send(frm, "\n".join(lines))
    print(f"[wa v5] batch for {frm} complete ({len(options)} option(s) offered)", flush=True)


def _present_deal(frm: str, packet) -> None:
    from yta.web import _resolve   # lazy, same reason wa_shared.finish_and_reply does this

    wa_send(frm, random.choice(_FETCHING_PHRASES))
    resolution = _resolve(packet) if packet.hotel.name else None
    if (resolution or {}).get("room_options"):
        _present_option_choices(frm, packet, resolution)
        return
    _send_deal_result(frm, packet, resolution)


def _run_extraction(frm: str, url, media, page_text: str | None = None,
                    intent: str | None = None) -> None:
    from yta import whatsapp
    from yta.pipeline import extract

    wa_send(frm, random.choice(_CHECKING_PHRASES))
    print(f"[wa v5] extracting: url={url!r} has_media={bool(media)} has_text={bool(page_text)} "
          f"intent={intent!r}", flush=True)
    packet = extract(url or "", render=bool(url), media=media, page_text=page_text, log_sink=[])
    print(f"[wa v5] extraction done: hotel={packet.hotel.name!r} status={packet.status}", flush=True)

    # A free-text submission has no unambiguous "this is definitely a
    # booking" signal the way a URL or an upload does -- the pre-filter in
    # _looks_like_a_query only rejects the cheap/obvious non-queries before
    # ever getting here. If NOTHING recognizable came through at all (no
    # hotel name), don't drag the customer into a full slot-filling
    # interrogation seeded from a stray sentence that happened to be long
    # enough to pass the filter -- a soft nudge instead.
    if page_text and not url and not media and not packet.hotel.name:
        wa_send(frm, "I couldn't find hotel details in that — send the link, a screenshot, "
                     "or the hotel name with your dates and price and I'll take it from there.")
        return

    missing = _effective_missing(packet, packet.missing_mandatory or packet.check_mandatory(), intent)
    if missing:
        with _WA_SESSIONS_LOCK:
            _WA_SESSIONS[frm] = {"state": "awaiting_field", "packet": packet, "missing": missing,
                                  "intent": intent, "unproductive_attempts": 0,
                                  "last_activity": time.time()}
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
    print(f"[wa v5] clarification filled: {list(fields.keys())}; note={clarify!r}", flush=True)

    if not fields and not clarify:
        attempts = session.get("unproductive_attempts", 0) + 1
        if attempts >= _MAX_UNPRODUCTIVE_ATTEMPTS:
            print(f"[wa v5] {frm} gave up after {attempts} unproductive replies", flush=True)
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
    intent = session.get("intent")
    for path, val in fields.items():
        packet.add(path, val, LLM, 0.7, "whatsapp clarification")
    packet.derive_stay()
    still_missing = _effective_missing(packet, packet.check_mandatory(), intent)
    print(f"[wa v5] still missing after clarification: {still_missing}", flush=True)
    if still_missing:
        with _WA_SESSIONS_LOCK:
            _WA_SESSIONS[frm] = {"state": "awaiting_field", "packet": packet, "missing": still_missing,
                                  "intent": intent, "clarify_text": accumulated,
                                  "unproductive_attempts": 0, "last_activity": time.time()}
        whatsapp.send_buttons(frm, _found_and_ask_message(packet, still_missing, clarify),
                               [("start_new_chat", "Start over")])
        return

    with _WA_SESSIONS_LOCK:
        _WA_SESSIONS.pop(frm, None)
    _present_deal(frm, packet)


def _handle_choosing_option(frm: str, session: dict, items: list) -> None:
    """A numeric reply to _present_option_choices()'s list. Repackages the
    picked RateOption as a synthetic single-option room_map -- the exact
    shape _deal_message()/_send_deal_result() already expect from the
    single-room path -- so confirm/decline and lead recording need zero
    changes to handle a picked option."""
    text = _text_of(items)
    options = session.get("options") or []
    m = re.search(r"\b([1-9])\b", text)
    idx = int(m.group(1)) - 1 if m else -1

    if not (0 <= idx < len(options)):
        attempts = session.get("unproductive_attempts", 0) + 1
        if attempts >= _MAX_UNPRODUCTIVE_ATTEMPTS:
            print(f"[wa v5] {frm} gave up picking an option after {attempts} tries", flush=True)
            with _WA_SESSIONS_LOCK:
                _WA_SESSIONS.pop(frm, None)
            wa_send(frm, "No worries — whenever you're ready, send a fresh hotel link or "
                         "the details and we'll start again.")
            return
        with _WA_SESSIONS_LOCK:
            session["last_activity"] = time.time()
            session["unproductive_attempts"] = attempts
            _WA_SESSIONS[frm] = session
        wa_send(frm, f"Just reply with a number from 1 to {len(options)} to pick one.")
        return

    picked = options[idx]
    packet = session["packet"]
    resolution = dict(session.get("resolution") or {})
    resolution.pop("room_options", None)
    resolution["room_map"] = {
        "matched": True, "rate_options": [picked],
        "ratekey_option_ids": [picked.get("option_id")],
    }
    with _WA_SESSIONS_LOCK:
        _WA_SESSIONS.pop(frm, None)
    print(f"[wa v5] {frm} picked option {idx + 1} of {len(options)} ({picked.get('option_id')})", flush=True)
    _send_deal_result(frm, packet, resolution)


def _handle_presented(frm: str, session: dict, items: list, button_id) -> None:
    from yta.leads.db import record_lead

    text = _text_of(items)
    is_confirm = button_id == "confirm_book" or (button_id is None and _CONFIRM_RE.search(text))
    is_decline = button_id == "decline_book" or (button_id is None and _DECLINE_RE.search(text))

    if is_confirm:
        referred_by = _PENDING_REFERRALS.pop(frm, None)
        ref = record_lead(frm, "confirmed", session["packet"], session.get("resolution"),
                           referred_by=referred_by)
        with _WA_SESSIONS_LOCK:
            _WA_SESSIONS.pop(frm, None)
        confirm_line = session.get("confirm_line") or "I've noted this down."
        wa_send(frm, f"Wonderful — {confirm_line} Your reference is *{ref}*, "
                     f"and I'll personally follow up shortly to finalize everything with you.")
        ask, shareable = _referral_share_messages(ref)
        wa_send(frm, ask)
        if shareable:
            wa_send(frm, shareable)
        print(f"[wa v5] {frm} confirmed, lead {ref} (referred_by={referred_by})", flush=True)
        return

    if is_decline:
        referred_by = _PENDING_REFERRALS.pop(frm, None)
        ref = record_lead(frm, "declined", session["packet"], session.get("resolution"),
                           referred_by=referred_by)
        with _WA_SESSIONS_LOCK:
            _WA_SESSIONS.pop(frm, None)
        wa_send(frm, "Of course — whenever you're ready with the next one, I'm here to help.")
        print(f"[wa v5] {frm} declined, lead {ref} (referred_by={referred_by})", flush=True)
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
        print(f"[wa v5] session for {frm} abandoned (idle > {_SESSION_MAX_AGE_SEC}s) — clearing it "
              f"(memory hygiene, not a reply-relevance judgment)", flush=True)
        with _WA_SESSIONS_LOCK:
            _WA_SESSIONS.pop(frm, None)
        session = None

    try:
        text = _text_of(items)
        url = _find_url(items)
        has_media = _has_media(items)
        button_id = _button_id(items)

        # Checked before anything else and regardless of what else is in
        # this batch -- a referral mention can arrive on its own (someone
        # tapping the shared wa.me link, before they've attached a hotel
        # link/photo) or alongside one. Held per phone number until this
        # conversation eventually confirms or declines (see
        # _handle_presented), independent of the session/state lifecycle.
        referral_match = _REFERRAL_RE.search(text)
        if referral_match:
            _PENDING_REFERRALS[frm] = referral_match.group(0).upper()
            print(f"[wa v5] {frm} mentioned referral {referral_match.group(0).upper()}", flush=True)

        if not text and not url and not has_media and button_id is None:
            if _all_unreadable(items):
                wa_send(frm, "I'm only able to read text or a photo at the moment — a link "
                             "or a screenshot would be perfect, and I'll take it from there.")
            else:
                _send_onboarding_choice(frm)
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

        if button_id == "have_deal":
            _PENDING_PATH[frm] = "deal"
            wa_send(frm, _ONBOARDING_TEXT)
            return

        if button_id == "search_hotel":
            _PENDING_PATH[frm] = "search"
            wa_send(frm, _SEARCH_TEXT)
            return

        if button_id == "try_another":
            _send_onboarding_choice(frm)
            return

        if url:
            # A real link is treated as an implicit "different hotel,
            # forget the old one", in any state -- safer to trust than
            # guessing the same from a bare photo.
            if session is not None:
                print(f"[wa v5] {frm} sent a new link mid-conversation — dropping the old session", flush=True)
                with _WA_SESSIONS_LOCK:
                    _WA_SESSIONS.pop(frm, None)
            media = _download_media(items) if has_media else None
            _run_extraction(frm, url, media, intent=_PENDING_PATH.pop(frm, None))
            return

        if session is None:
            if has_media:
                _run_extraction(frm, None, _download_media(items), intent=_PENDING_PATH.pop(frm, None))
                return
            if _looks_like_a_query(text):
                _run_extraction(frm, None, None, page_text=text, intent=_PENDING_PATH.pop(frm, None))
                return
            _send_onboarding_choice(frm)
            return

        if session.get("state") == "presented":
            _handle_presented(frm, session, items, button_id)
            return

        if session.get("state") == "choosing_option":
            _handle_choosing_option(frm, session, items)
            return

        _handle_awaiting_field(frm, session, items)
    except Exception as e:  # noqa: BLE001
        print(f"[wa v5] ERROR handling batch: {type(e).__name__}: {e}", flush=True)
        with _WA_SESSIONS_LOCK:
            _WA_SESSIONS.pop(frm, None)
        wa_send(frm, f"Sorry, something went wrong: {type(e).__name__}: {e}")
