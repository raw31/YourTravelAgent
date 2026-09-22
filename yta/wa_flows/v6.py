"""v6 — v5's conversation, plus (so far) a real error/retry path instead of
wiping the session and leaking a raw exception whenever something breaks
mid-conversation.

Starts as an exact copy of v5.py -- v5.py itself is untouched and keeps
running unaffected; this is a separate module with its own session
store, same as v0/v1/v2/v3/v4/v5 each having their own. This module is
being built up against a external chatbot-flow review (P0/P1/P2/P3
priority list); each addition gets its own numbered item below as it
lands, same convention as every prior version's docstring.

What v6 adds on top:

  12. An unhandled exception no longer wipes the session or shows the
      customer anything about the exception itself (P0 fix — the old
      behavior sent `f"Sorry, something went wrong: {type(e).__name__}:
      {e}"` verbatim to the customer AND dropped whatever they'd already
      told the bot). Now: the exception + full traceback is printed
      server-side only; the session is left exactly as it was; the
      customer gets "Sorry, something went wrong on my side. Your
      details are still saved." with two buttons.
        - "Try again" replays the SAME batch of items that just failed
          (_LAST_BATCH_ITEMS, phone -> items, same independent-of-
          session-lifecycle pattern as _PENDING_PATH/_PENDING_REFERRALS
          -- set right before every dispatch attempt, successful or
          not) -- there's no per-step "what exactly broke" tracking;
          replaying the customer's own last message is what "try again"
          means from their side of the conversation regardless of which
          internal step actually threw.
        - "Start over" behaves exactly like the existing cancel path
          (drops the session, re-offers the onboarding choice).
        - If the RETRY itself also throws, that's treated as a real
          give-up: the session is dropped, a short apology is sent, and
          the onboarding choice is re-offered -- no infinite retry loop.

  13. The confirm message no longer promises a vague "shortly" or lets
      the customer think the booking is already done. It now states a
      concrete follow-up window ("I'll message you here within 30
      minutes to complete the booking") and sends the reference number
      as its own short, easy-to-copy message right after -- confirming
      only means "I've noted this down and I'm on it," never "you're
      booked." (This deliberately does NOT call TripJack's Review/
      prebook API to actually hold the rate -- confirming stays a pure
      lead record, same as v5; only the WORDING changed, so it can't
      accidentally promise more than the system behind it does.)

  14. A matched rate that ISN'T cheaper used to be a dead end -- a plain
      message, no buttons, nothing to do next. It's now a real state
      (`not_cheaper`) with two buttons: "See other rooms" (re-lists the
      hotel's OTHER rooms from the pricing response already sitting in
      `resolution["detail"]["options"]` -- no new supplier call, and the
      room just shown is excluded) and "Try another hotel". Also logs a
      `no_deal` lead (yta.leads.db.record_lead) purely for internal
      analysis -- the OTA price and our best price were already being
      stored on every lead row, so the "gap" is just `ota_price - price`
      at query time; nothing new to store for that. Never shown to the
      customer -- the actual rate is never revealed when it isn't better.

  15. Cancel-phrase matching used to run `_CANCEL_RE.search()` against
      the WHOLE message, so a cancel word appearing anywhere -- "wrong
      hotel", "new hotel" as a literal substring of an unrelated hotel
      name, "nevermind" tacked onto an otherwise-meaningful reply --
      could wipe a session that had nothing wrong with it. Now: a
      message containing a URL or a referral code is never treated as a
      cancel at all (it plainly carries other real content); otherwise
      the (punctuation-stripped, lowercased) message is only a FULL
      cancel when it's a short (<=4 word) message that IS or STARTS WITH
      one of a fixed set of phrases -- "new hotel" was dropped from that
      set entirely (too likely to be a literal hotel name substring),
      "another hotel" was added in its place. A LONGER message that
      merely starts with a cancel phrase but clearly carries more
      content past it (e.g. "nevermind, the second one") no longer resets
      anything outright -- it asks "Want to start over?" with Yes/No
      buttons instead, so an ambiguous reply gets a confirmation, not a
      silent wipe.

  16. `awaiting_field` guards. Guard A: a reply that answers nothing
      about the currently-open question but reads like a full query on
      its own (`_looks_like_a_fresh_query` -- `_looks_like_a_query` PLUS
      at least one digit, since a real hotel query always carries a
      date/price/guest-count number and ordinary unproductive prose
      ("still unrelated") almost never does -- confirmed the hard way, a
      test using exactly that phrase as a deliberately-unproductive reply
      first exposed the looser check letting it dodge every strike) is
      treated as a fresh submission on the message as sent -- no strike
      counted. Guard B: a reply arriving after a genuinely long
      gap (`_STALE_REPLY_SEC`, default 4h -- well above the 1-2h slow
      replies actually observed) that ALSO reads like a fresh query gets
      a "Welcome back — continue with {hotel}, or start a new search?"
      check instead of either assumption; under the threshold, or when
      the message can't be read as a fresh query at all, behavior is
      identical to v5 (still no wall-clock cutoff on an actual answer,
      however late). "Continue" just re-asks the same pending question;
      "New search" wipes the session and runs extraction on the message
      that triggered the check, so nothing they typed is thrown away
      either way.

  17. `awaiting_field` (and `choosing_option`) no longer wipe the session
      on the second unproductive reply -- that was throwing away
      everything the customer had already told the bot over one more
      bad guess than usual. New shape, in both states: 1st unproductive
      reply -> re-ask with a concrete example (`_FIELD_EXAMPLES`) for the
      single most relevant missing field / "Tap a room, or reply with a
      number" for choosing_option. 2nd -> the session is KEPT, and
      alternative ways to answer are offered instead ("Type details" /
      "Send screenshot" / "Start over" for awaiting_field; "Show list
      again" / "Start over" for choosing_option -- the former just
      re-sends the same closing question, the latter re-sends the exact
      same numbered list from the session's already-cached options, no
      new supplier call). Only a 3rd unproductive reply, or explicitly
      tapping Start over, actually gives up.

What v5 already does, carried over unchanged below (see v5.py's own
docstring for the full reasoning on each):

  9-11. The onboarding choice, the two paths' different mandatory-field
     expectations, and the room-options list for a no-room-name booking.
     See v5.py.

What v4/v3/v2 already do, carried over unchanged below (see each
version's own docstring for the full reasoning): free text can start a
submission on its own (v4); the referral loop (v3); bounded slot-filling
instead of a wall-clock timeout, the PRESENTED confirm/decline step, and
the concierge-voice message design (v2).

A session is only ever dropped by: CANCEL (explicit or a fresh URL,
treated as an implicit "different hotel"), the unproductive-attempt cap,
a confirm/decline in PRESENTED, a retry that ALSO fails, or the pure
memory-hygiene age backstop. A first-time, non-retried error deliberately
does NOT drop the session anymore (see item 12).

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
import traceback

from yta.wa_shared import (
    occ_repr, wa_send, wa_send_image,
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
# `not_cheaper` still gives up after 2 (offers 2 buttons already; a 3rd
# "which of these" tier would be one too many choices). `awaiting_field`/
# `choosing_option` now give up at 3 instead (see item 17) -- the 2nd
# attempt there offers alternatives rather than wiping, so it needs one
# more real strike after that before actually giving up.
_MAX_UNPRODUCTIVE_ATTEMPTS = 2

# Guard B's threshold (item 16) -- deliberately well above the 1-2h slow
# replies actually observed live, so it only ever fires for a reply that
# is genuinely likely to be about something else entirely, never a
# customer who just took a while.
_STALE_REPLY_SEC = 4 * 60 * 60

# One short, concrete example per field a customer might be asked to
# fill in -- appended to the FIRST unproductive-reply re-ask (item 17)
# only; the closing question itself already names the field, this just
# shows the shape of a usable answer. No entry -> no example line, never
# a placeholder.
_FIELD_EXAMPLES = {
    "stay.check_in": "12-14 Oct",
    "stay.check_out": "12-14 Oct",
    "stay.rooms": "2 adults in 1 room",
    "stay.occupancy": "2 adults in 1 room",
    "requested_offer.room_name": "Deluxe Room",
    "ota_benchmark.final_payable": "INR 18,000",
    "hotel.name": "Taj Santacruz, Mumbai",
}

# Fixed set of cancel phrases, checked against the WHOLE normalized
# message (see _cancel_match_kind) rather than searched for anywhere in
# it -- "new hotel" deliberately dropped (too likely to be a literal
# substring of an unrelated hotel's own name); "another hotel" added,
# since that's the phrasing a customer wanting a different property
# without literally saying "cancel" actually tends to use.
_CANCEL_PHRASES = (
    "cancel", "start over", "start again", "start this again", "restart",
    "start new", "new chat", "wrong hotel", "different hotel", "another hotel",
    "never mind", "nevermind",
)
_CONFIRM_RE = re.compile(r"\b(yes|confirm|book it|go ahead|book this)\b", re.IGNORECASE)
_DECLINE_RE = re.compile(r"\b(no|not now|skip|later|maybe later)\b", re.IGNORECASE)
_PUNCT_RE = re.compile(r"[^\w\s]")


def _normalize_for_cancel(text: str) -> str:
    return re.sub(r"\s+", " ", _PUNCT_RE.sub(" ", (text or "").lower())).strip()


def _cancel_match_kind(text: str, url, has_referral: bool) -> str | None:
    """None: not a cancel at all. "full": reset immediately. "partial":
    starts with a cancel phrase but clearly carries more content past
    it -- ask for confirmation instead of resetting outright, rather
    than guessing which the customer meant.

    A URL or a referral code (BMS-XXXXXXXX) in the message means it's
    NOT a cancel, full stop -- both carry real content of their own that
    a cancel word happening to also appear in the same message must
    never override."""
    if url or has_referral:
        return None
    norm = _normalize_for_cancel(text)
    if not norm:
        return None
    for phrase in _CANCEL_PHRASES:
        if norm == phrase:
            return "full"
        if norm.startswith(phrase + " "):
            return "full" if len(norm.split()) <= 4 else "partial"
    return None

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

# yta.profiles.route()'s domain labels for MakeMyTrip's two link shapes
# (its own site, and the app.mmyt.co AppsFlyer share-link domain).
# Narrow, deliberate exception to the "zero per-OTA code" design -- this
# isn't a parsing special-case, it's an infrastructure one: MakeMyTrip
# blocks this server's automated access at the network level, confirmed
# live (both Playwright rendering AND a plain HTTP fetch time out/reset
# on its real hotel pages) -- no parsing improvement can fix that.
_RENDER_BLOCKED_OTAS = {"mmyt", "makemytrip"}


def _looks_like_a_query(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < _MIN_QUERY_LEN:
        return False
    if _CHITCHAT_RE.match(t):
        return False
    return True


def _looks_like_a_fresh_query(text: str) -> bool:
    """A STRICTER check than `_looks_like_a_query`, used only by the
    awaiting_field guards (item 16) -- there, misreading ordinary
    unproductive prose ("still unrelated", "hi there") as a pivot to a
    new hotel would let a genuinely stuck conversation dodge every
    strike forever (confirmed by a real test failure while building
    this: "still unrelated" alone passed the looser filter). A real
    hotel query -- v6's own free-text-submission examples included --
    always carries at least one digit (a date, a price, a guest count);
    plain unproductive prose almost never does. `_looks_like_a_query`
    stays the looser gate everywhere else (deciding whether a FIRST
    contact message is worth an extraction call at all, where a false
    positive just costs one wasted LLM call, not a strike that never
    lands)."""
    return _looks_like_a_query(text) and any(c.isdigit() for c in text)

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

# phone -> the items list of the last batch handle_batch attempted to
# dispatch (successful or not). Lets a "Try again" button replay the
# exact same reply after an unhandled error -- there's no tracking of
# WHICH internal step broke, and there doesn't need to be: from the
# customer's side, "try again" just means "have another go at what I
# just sent you," whatever that was.
_LAST_BATCH_ITEMS: dict = {}


def _effective_missing(packet, missing: list, intent: str | None) -> list:
    """`missing` is packet.check_mandatory()'s own list -- schema.py's
    mandatory set already supports both a full single-room deal and a
    no-room submission on its own (the default, `intent=None`, when a
    customer bypasses the buttons entirely). An explicit intent layers the
    ONE additional expectation that path implies, without changing what
    schema.py itself considers mandatory for every other flow.

    On "deal", room_name is now STRICTLY required, unconditionally --
    per explicit instruction, tapping "I have a deal" commits to that
    flow, and a specific room is the whole point of a deal comparison.
    (An earlier version of this only forced the question when a
    description hint existed and fell through to a hotel-based search
    otherwise -- reversed on purpose; "I have a deal" quietly becoming a
    hotel search read as wrong for that path. A customer with no
    specific room in mind should use "Search a hotel" instead -- that
    path, and the no-button-tapped intent=None default, still fall
    through to the room-options list exactly as before.)

    No room name at all, REGARDLESS of intent, also always drops the
    price requirement -- _resolve() already forks into a hotel-based
    search whenever room_name is empty (see yta/web.py), and there's
    nothing to compare a price against on that path. Real transcript that
    exposed the gap: a customer typed "Hey" (no button tapped, intent
    stays None), then hotel+dates with no room -- asked for a price;
    replied "Do hotel search only" (a plain-text intent change this
    codebase doesn't parse) -- STILL asked for a price, with nothing to
    give, and gave up. This is a blanket fallback under whatever the
    intent-specific branches above already decided, not a replacement for
    them -- it only ever REMOVES the price requirement, never adds one."""
    if intent == "search":
        missing = [m for m in missing if m != "ota_benchmark.final_payable"]
    elif intent == "deal":
        # Strict, per explicit instruction -- see the docstring above.
        if not packet.requested_offer.room_name \
                and "requested_offer.room_name" not in missing:
            missing = missing + ["requested_offer.room_name"]
    if not packet.requested_offer.room_name:
        missing = [m for m in missing if m != "ota_benchmark.final_payable"]
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
                print(f"[wa v6] downloaded media: {len(data)} bytes, {mime}", flush=True)
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
        # A real rate, but not actually cheaper -- deal_message() only
        # ever returns matched=True/bookable=False for exactly this
        # (comparable-and-not-cheaper) case, never any other. Used to be
        # a dead end (a plain message, no buttons, no state) -- now a
        # real `not_cheaper` state with somewhere to go next (item 14).
        _present_not_cheaper(frm, packet, resolution)
    else:
        whatsapp.send_buttons(frm, text, [("try_another", "Try another hotel")])
    print(f"[wa v6] batch for {frm} complete (matched={matched} bookable={bookable})", flush=True)


def _present_not_cheaper(frm: str, packet, resolution: dict) -> None:
    from yta import whatsapp
    from yta.leads.db import record_lead

    ota_price = packet.ota_benchmark.final_payable
    ota_ccy = (packet.ota_benchmark.currency or "").strip()
    room_map = (resolution or {}).get("room_map") or {}
    opts = room_map.get("rate_options") or []
    keyed = set(room_map.get("ratekey_option_ids") or [])
    pool = [o for o in opts if o.get("option_id") in keyed] or opts
    best = min(pool, key=lambda o: o.get("total_price", float("inf"))) if pool else {}
    # room_type_id only exists on RateOption's OWN output shape (best's
    # shape here) -- raw supplier-option dicts (resolution["detail"]
    # ["options"], see _handle_not_cheaper) carry room identity nested
    # under "rooms"/"roomInfo" instead, with no matching flat field.
    # room_name is what both shapes actually share.
    excluded_room_name = (best.get("room_name") or "").strip()

    try:
        # Internal analytics only -- ota_price/price are already stored
        # on every lead row, so the "gap" is just ota_price - price at
        # query time; nothing new to compute or store here. A logging
        # failure must never block the customer's reply.
        record_lead(frm, "no_deal", packet, resolution)
    except Exception as e:  # noqa: BLE001
        print(f"[wa v6] no_deal lead logging failed for {frm}: {type(e).__name__}: {e}", flush=True)

    with _WA_SESSIONS_LOCK:
        _WA_SESSIONS[frm] = {"state": "not_cheaper", "packet": packet, "resolution": resolution,
                              "excluded_room_name": excluded_room_name,
                              "unproductive_attempts": 0, "last_activity": time.time()}
    price_str = f"{ota_ccy} {ota_price:,.0f}" if ota_price else "your current price"
    whatsapp.send_buttons(
        frm, f"I checked current prices and couldn't beat {price_str} for this room. "
             f"It looks like a good deal already.",
        [("see_other_rooms", "See other rooms"), ("try_another", "Try another hotel")])


def _handle_not_cheaper(frm: str, session: dict, items: list, button_id) -> None:
    """See item 14. "See other rooms" re-lists this same hotel's OTHER
    rooms straight from the pricing response already cached on the
    session (resolution["detail"]["options"]) -- no new supplier call --
    excluding the room that was just shown. "Try another hotel" wipes
    the session and re-offers the onboarding choice (same as the global
    try_another handler). Anything else repeats the two buttons once,
    then falls back to the onboarding choice, same shape as every other
    bounded-attempt state in this flow."""
    from yta import whatsapp
    from yta.roommap import list_cheapest_rooms

    def _room_name_of(o: dict) -> str:
        rooms = o.get("rooms") or o.get("roomInfo") or []
        return " + ".join(str(r.get("name") or "").strip() for r in rooms).strip()

    if button_id == "see_other_rooms":
        packet = session["packet"]
        resolution = session.get("resolution") or {}
        options = (resolution.get("detail") or {}).get("options") or []
        excluded = session.get("excluded_room_name")
        remaining = [o for o in options if _room_name_of(o) != excluded] if excluded else options
        if not remaining:
            with _WA_SESSIONS_LOCK:
                _WA_SESSIONS.pop(frm, None)
            whatsapp.send_buttons(
                frm, "That's actually the only room I found a live rate for at this hotel "
                     "right now.", [("try_another", "Try another hotel")])
            return
        rgr = list_cheapest_rooms(remaining)
        new_resolution = dict(resolution)
        new_resolution["room_options"] = rgr.to_dict()
        new_resolution.pop("room_map", None)
        _present_option_choices(frm, packet, new_resolution)
        return

    if button_id == "try_another":
        with _WA_SESSIONS_LOCK:
            _WA_SESSIONS.pop(frm, None)
        _send_onboarding_choice(frm)
        return

    attempts = session.get("unproductive_attempts", 0) + 1
    if attempts >= _MAX_UNPRODUCTIVE_ATTEMPTS:
        with _WA_SESSIONS_LOCK:
            _WA_SESSIONS.pop(frm, None)
        _send_onboarding_choice(frm)
        return
    with _WA_SESSIONS_LOCK:
        session["unproductive_attempts"] = attempts
        session["last_activity"] = time.time()
        _WA_SESSIONS[frm] = session
    whatsapp.send_buttons(
        frm, "Just tap one of the options below.",
        [("see_other_rooms", "See other rooms"), ("try_another", "Try another hotel")])


def _present_option_choices(frm: str, packet, resolution: dict) -> None:
    """The no-requested-room path: resolution["room_options"]
    (yta.roommap.list_cheapest_rooms()) names up to 5 DISTINCT rooms
    (the cheapest 5, ranked by each room's own lowest price), each with
    up to 2 of its own meal x refundability variants. Shown as room
    headers with their variants underneath, numbered SEQUENTIALLY across
    the whole list (not restarting per room) so a numeric reply maps to
    exactly one option regardless of which room it's under. WhatsApp's
    reply buttons cap at 3 and this codebase has no list-message support
    (up to 10 tappable rows) -- a numbered text list + numeric reply is
    the practical alternative. Opens a "choosing_option" session holding
    the FLATTENED option list for that reply."""
    from yta import whatsapp

    rz = resolution or {}
    groups = (rz.get("room_options") or {}).get("groups") or []
    flat_options = [opt for g in groups for opt in (g.get("options") or [])]
    hotel_name = (rz.get("detail") or {}).get("hotel_name") \
        or (rz.get("match") or {}).get("hotel_name") \
        or packet.hotel.name or "this hotel"

    if not flat_options:
        whatsapp.send_buttons(
            frm, f"I wasn't able to find any live rates for {hotel_name} for these dates "
                 f"right now. Happy to take a look at another property, if you'd like?",
            [("try_another", "Try another hotel")])
        print(f"[wa v6] batch for {frm} complete (no room_options available)", flush=True)
        return

    lines = [f"🏨 *{hotel_name}*"]
    date_occ = []
    if packet.stay.check_in and packet.stay.check_out:
        date_occ.append(f"{_short_date(packet.stay.check_in)} → {_short_date(packet.stay.check_out)}")
    if packet.stay.occupancy:
        date_occ.append(occ_repr(packet.stay.occupancy))
    if date_occ:
        lines.append("📅 " + " · ".join(date_occ))

    if (rz.get("room_options") or {}).get("ambiguous_match") and groups:
        # A specific room WAS requested, but not confidently enough to
        # quote outright (see yta/web.py::_resolve()) -- name the
        # algorithm's own nearest guess and the hotel's actual cheapest
        # room up front, before the full list, rather than just dropping
        # the customer into a bare list with no explanation.
        def _callout_line(grp):
            opt = min(grp.get("options") or [{}], key=lambda o: o.get("total_price") or float("inf"))
            bits = [opt.get("meal_basis") or "Room Only"]
            if opt.get("refundable") is True:
                bits.append("Refundable")
            elif opt.get("refundable") is False:
                bits.append("Non-refundable")
            ccy = opt.get("currency") or ""
            price = opt.get("total_price") or 0
            name = _clean_room_name(grp.get("room_name")) or "Room"
            return f"{name} — {' · '.join(bits)} — {ccy} {price:,.2f}"

        nearest = groups[0]   # _resolve() already sorts the nearest match first
        cheapest = min(groups, key=lambda g: min(
            (o.get("total_price") or float("inf")) for o in (g.get("options") or [{}])))
        lines += ["", "I couldn't confidently match your room to one exact type — here's "
                       "the nearest match and the cheapest option we have:", ""]
        lines.append(f"🎯 Nearest match: {_callout_line(nearest)}")
        lines.append("")
        if cheapest.get("room_type_id") != nearest.get("room_type_id"):
            lines.append(f"💰 Cheapest available: {_callout_line(cheapest)}")
            lines.append("")
        lines += ["Full list:", ""]
    else:
        lines += ["", "Here's what's available:", ""]

    n = 0
    for g in groups:
        opts = g.get("options") or []
        if not opts:
            continue
        lines.append(f"🛏️ *{_clean_room_name(g.get('room_name')) or 'Room'}*")
        for opt in opts:
            n += 1
            bits = [opt.get("meal_basis") or "Room Only"]
            if opt.get("refundable") is True:
                bits.append("Refundable")
            elif opt.get("refundable") is False:
                bits.append("Non-refundable")
            ccy = opt.get("currency") or ""
            price = opt.get("total_price") or 0
            lines.append(f"{n}. {' · '.join(bits)} — {ccy} {price:,.2f}")
        lines.append("")
    lines.append(f"Reply with a number (1–{n}) to pick one.")

    with _WA_SESSIONS_LOCK:
        _WA_SESSIONS[frm] = {"state": "choosing_option", "packet": packet,
                              "resolution": resolution, "options": flat_options,
                              "unproductive_attempts": 0, "last_activity": time.time()}
    wa_send(frm, "\n".join(lines))
    print(f"[wa v6] batch for {frm} complete ({n} option(s) across {len(groups)} room(s))", flush=True)


def _present_deal(frm: str, packet) -> None:
    from yta.web import _resolve   # lazy, same reason wa_shared.finish_and_reply does this

    wa_send(frm, random.choice(_FETCHING_PHRASES))
    resolution = _resolve(packet) if packet.hotel.name else None
    # A cover photo, sent once the hotel itself is confidently identified
    # (resolution["match"] is only ever set for a high/medium-band match --
    # see yta.hoteldb.resolver.resolve) -- regardless of which of the two
    # paths below actually shows the rates.
    cover_image = ((resolution or {}).get("match") or {}).get("cover_image")
    if cover_image:
        wa_send_image(frm, cover_image)
    if (resolution or {}).get("room_options"):
        _present_option_choices(frm, packet, resolution)
        return
    _send_deal_result(frm, packet, resolution)


def _run_extraction(frm: str, url, media, page_text: str | None = None,
                    intent: str | None = None) -> None:
    from yta import whatsapp
    from yta.pipeline import extract

    wa_send(frm, random.choice(_CHECKING_PHRASES))
    print(f"[wa v6] extracting: url={url!r} has_media={bool(media)} has_text={bool(page_text)} "
          f"intent={intent!r}", flush=True)
    packet = extract(url or "", render=bool(url), media=media, page_text=page_text, log_sink=[])
    print(f"[wa v6] extraction done: hotel={packet.hotel.name!r} status={packet.status}", flush=True)

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
        if url and not packet.hotel.name and (packet.source.ota or "").lower() in _RENDER_BLOCKED_OTAS:
            # A known, narrow exception -- not a parsing special-case, an
            # infrastructure one: MakeMyTrip actively blocks this server's
            # automated access at the network level (verified live: both
            # headless-browser rendering AND a plain HTTP fetch from this
            # server time out/reset on MMT's real hotel pages). No amount
            # of better parsing gets past that, so don't even try the
            # generic "what's missing" ask -- tell the customer plainly
            # and let them send a screenshot instead, which an MMT share
            # generally has everything on anyway.
            whatsapp.send_buttons(
                frm, "I wasn't able to pull the details from that MakeMyTrip link — could "
                     "you send a screenshot of the page instead, or just tell me the hotel "
                     "name, dates, guests, and price?",
                [("start_new_chat", "Start over")])
        else:
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

    # Guard B (item 16): a reply arriving after a genuinely long gap AND
    # reading like a fresh query, not an answer to the pending question,
    # gets a "welcome back" check instead of being silently absorbed as
    # an answer or burning a strike -- the no-timeout design stays (this
    # is a confirmation prompt, never a silent drop), it's just no longer
    # blind to "this might not even be about the same search anymore."
    # Skipped entirely under the threshold, or when it can't possibly be
    # read as a fresh query on its own.
    age = time.time() - session.get("last_activity", 0)
    if age > _STALE_REPLY_SEC and _looks_like_a_fresh_query(text):
        print(f"[wa v6] {frm} awaiting_field reply after {age:.0f}s looks like a fresh query "
              f"-- asking before assuming either way", flush=True)
        with _WA_SESSIONS_LOCK:
            session["pending_new_text"] = text
            session["pending_new_media"] = media
            session["last_activity"] = time.time()
            _WA_SESSIONS[frm] = session
        hotel_name = session["packet"].hotel.name or "this search"
        whatsapp.send_buttons(
            frm, f"Welcome back. Should I continue with {hotel_name}, or start a new search?",
            [("welcome_continue", "Continue"), ("welcome_new_search", "New search")])
        return

    accumulated = "\n".join(t for t in (session.get("clarify_text"), text) if t)

    fields, clarify = extract_clarification(session["missing"], accumulated, media=media)
    print(f"[wa v6] clarification filled: {list(fields.keys())}; note={clarify!r}", flush=True)

    if not fields and not clarify:
        # Guard A (item 16): nothing about the CURRENT question was
        # answered, but the reply reads like a full query on its own --
        # more likely a pivot to a different hotel that a customer typed
        # instead of tapping "Start over" than a genuinely unproductive
        # reply. Treated as a fresh submission on the message as-sent
        # (not `accumulated`, which may carry unrelated earlier context)
        # -- no strike counted.
        if _looks_like_a_fresh_query(text):
            print(f"[wa v6] {frm} awaiting_field reply answers nothing but reads like a new "
                  f"query -- treating as a fresh submission, no strike", flush=True)
            intent = session.get("intent")
            with _WA_SESSIONS_LOCK:
                _WA_SESSIONS.pop(frm, None)
            _run_extraction(frm, None, media, page_text=text, intent=intent)
            return

        attempts = session.get("unproductive_attempts", 0) + 1
        example = _FIELD_EXAMPLES.get(session["missing"][0]) if session["missing"] else None
        if attempts == 1:
            # Strike 1 (item 17): re-ask with a concrete example instead
            # of the bare question -- still open, no alternative-input
            # offer yet.
            question = _closing_question(session["missing"])
            if example:
                question = f"{question} For example, {example}."
            with _WA_SESSIONS_LOCK:
                session["last_activity"] = time.time()
                session["unproductive_attempts"] = attempts
                _WA_SESSIONS[frm] = session
            whatsapp.send_buttons(frm, question, [("start_new_chat", "Start over")])
            return
        if attempts == 2:
            # Strike 2 (item 17): NOT a wipe -- offer alternative ways to
            # answer and keep the session open. Only a THIRD unproductive
            # reply (or tapping Start over) actually gives up.
            with _WA_SESSIONS_LOCK:
                session["last_activity"] = time.time()
                session["unproductive_attempts"] = attempts
                _WA_SESSIONS[frm] = session
            whatsapp.send_buttons(
                frm, "Still missing that one — how would you like to share it?",
                [("await_type_details", "Type details"), ("await_send_screenshot", "Send screenshot"),
                 ("start_new_chat", "Start over")])
            return
        print(f"[wa v6] {frm} gave up after {attempts} unproductive replies", flush=True)
        with _WA_SESSIONS_LOCK:
            _WA_SESSIONS.pop(frm, None)
        wa_send(frm, "I wasn't able to pull together everything I need for this one "
                     "just yet — no worries, let's start fresh.")
        _PENDING_PATH.pop(frm, None)
        _send_onboarding_choice(frm)
        return

    packet = session["packet"]
    intent = session.get("intent")
    for path, val in fields.items():
        packet.add(path, val, LLM, 0.7, "whatsapp clarification")
    packet.derive_stay()
    still_missing = _effective_missing(packet, packet.check_mandatory(), intent)
    print(f"[wa v6] still missing after clarification: {still_missing}", flush=True)
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


def _handle_choosing_option(frm: str, session: dict, items: list, button_id=None) -> None:
    """A numeric reply to _present_option_choices()'s list. Repackages the
    picked RateOption as a synthetic single-option room_map -- the exact
    shape _deal_message()/_send_deal_result() already expect from the
    single-room path -- so confirm/decline and lead recording need zero
    changes to handle a picked option. Give-up shape mirrors
    _handle_awaiting_field's (item 17): 1st bad reply -> nudge, 2nd ->
    keep the session and offer alternatives, 3rd (or Start over) ->
    actually give up."""
    if button_id == "show_list_again":
        # Re-render the exact same list from the ORIGINAL resolution --
        # no new supplier call -- which also naturally resets the strike
        # count, same as any other fresh presentation of a choice.
        _present_option_choices(frm, session["packet"], session.get("resolution") or {})
        return

    text = _text_of(items)
    options = session.get("options") or []
    m = re.search(r"\b([1-9])\b", text)
    idx = int(m.group(1)) - 1 if m else -1

    if not (0 <= idx < len(options)):
        from yta import whatsapp
        attempts = session.get("unproductive_attempts", 0) + 1
        if attempts == 1:
            with _WA_SESSIONS_LOCK:
                session["last_activity"] = time.time()
                session["unproductive_attempts"] = attempts
                _WA_SESSIONS[frm] = session
            wa_send(frm, f"Tap a room, or reply with a number from 1 to {len(options)} to pick one.")
            return
        if attempts == 2:
            with _WA_SESSIONS_LOCK:
                session["last_activity"] = time.time()
                session["unproductive_attempts"] = attempts
                _WA_SESSIONS[frm] = session
            whatsapp.send_buttons(
                frm, "Still didn't catch a valid number — want to see the list again?",
                [("show_list_again", "Show list again"), ("start_new_chat", "Start over")])
            return
        print(f"[wa v6] {frm} gave up picking an option after {attempts} tries", flush=True)
        with _WA_SESSIONS_LOCK:
            _WA_SESSIONS.pop(frm, None)
        wa_send(frm, "No worries — let's start fresh.")
        _PENDING_PATH.pop(frm, None)
        _send_onboarding_choice(frm)
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
    print(f"[wa v6] {frm} picked option {idx + 1} of {len(options)} ({picked.get('option_id')})", flush=True)
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
        # A concrete window, not "shortly" -- and never implies the
        # booking itself is already done (see item 13). The reference is
        # its own short message right after, so it's easy to copy on its
        # own without the sentence around it.
        wa_send(frm, f"Wonderful — {confirm_line} I'll message you here within 30 minutes "
                     f"to complete the booking with you.")
        wa_send(frm, f"Your reference: {ref}")
        ask, shareable = _referral_share_messages(ref)
        wa_send(frm, ask)
        if shareable:
            wa_send(frm, shareable)
        print(f"[wa v6] {frm} confirmed, lead {ref} (referred_by={referred_by})", flush=True)
        return

    if is_decline:
        referred_by = _PENDING_REFERRALS.pop(frm, None)
        ref = record_lead(frm, "declined", session["packet"], session.get("resolution"),
                           referred_by=referred_by)
        with _WA_SESSIONS_LOCK:
            _WA_SESSIONS.pop(frm, None)
        wa_send(frm, "Of course — whenever you're ready with the next one, I'm here to help.")
        print(f"[wa v6] {frm} declined, lead {ref} (referred_by={referred_by})", flush=True)
        return

    wa_send(frm, "Just let me know — tap \"Yes, book this\" to confirm, or \"Not now\" to "
                 "skip. Typing works too, if the buttons aren't showing.")
    with _WA_SESSIONS_LOCK:
        session["last_activity"] = time.time()
        _WA_SESSIONS[frm] = session


def _run_batch_once(frm: str, items: list, session, *, allow_retry: bool) -> None:
    """The actual per-batch dispatch -- everything handle_batch used to do
    inline. `allow_retry=True` is the normal case: an unhandled error here
    leaves the session untouched (nothing the customer already told us is
    thrown away) and offers Try again / Start over instead of showing the
    exception. `allow_retry=False` is used only for replaying a batch via
    the "Try again" button itself -- a SECOND failure on the same retried
    reply is treated as a real give-up (drops the session for real) rather
    than offering to retry forever."""
    from yta import whatsapp

    try:
        _LAST_BATCH_ITEMS[frm] = items
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
            print(f"[wa v6] {frm} mentioned referral {referral_match.group(0).upper()}", flush=True)

        if not text and not url and not has_media and button_id is None:
            if _all_unreadable(items):
                wa_send(frm, "I'm only able to read text or a photo at the moment — a link "
                             "or a screenshot would be perfect, and I'll take it from there.")
            else:
                _send_onboarding_choice(frm)
            return

        # Global intents -- recognized in ANY state, checked before
        # anything state-specific. A URL or referral code in the message
        # means _cancel_match_kind never returns anything at all for it
        # (see item 15) -- only evaluated when there's no button already
        # answering the question.
        cancel_kind = None if button_id is not None else _cancel_match_kind(text, url, bool(referral_match))
        if button_id == "start_new_chat" or cancel_kind == "full":
            if session is not None:
                with _WA_SESSIONS_LOCK:
                    _WA_SESSIONS.pop(frm, None)
                wa_send(frm, "Not a problem at all — let's start fresh.")
            _PENDING_PATH.pop(frm, None)
            _send_onboarding_choice(frm)
            return

        if button_id == "confirm_cancel":
            if session is not None:
                with _WA_SESSIONS_LOCK:
                    _WA_SESSIONS.pop(frm, None)
                wa_send(frm, "Not a problem at all — let's start fresh.")
            _PENDING_PATH.pop(frm, None)
            _send_onboarding_choice(frm)
            return

        if button_id == "cancel_continue":
            wa_send(frm, "Got it — go ahead, I'm listening.")
            return

        if button_id == "welcome_continue":
            # Guard B (item 16) -- resume the SAME pending question;
            # the message that triggered the check is deliberately not
            # replayed as an answer to it (that's what "New search" is
            # for) -- asking again is the whole point of the check.
            if session is not None and session.get("state") == "awaiting_field":
                with _WA_SESSIONS_LOCK:
                    session.pop("pending_new_text", None)
                    session.pop("pending_new_media", None)
                    session["last_activity"] = time.time()
                    _WA_SESSIONS[frm] = session
                whatsapp.send_buttons(frm, _closing_question(session.get("missing", [])),
                                       [("start_new_chat", "Start over")])
            else:
                _send_onboarding_choice(frm)
            return

        if button_id == "welcome_new_search":
            pending_text = session.get("pending_new_text") if session else None
            pending_media = session.get("pending_new_media") if session else None
            pending_intent = session.get("intent") if session else None
            with _WA_SESSIONS_LOCK:
                _WA_SESSIONS.pop(frm, None)
            _PENDING_PATH.pop(frm, None)
            if pending_text or pending_media:
                _run_extraction(frm, None, pending_media, page_text=pending_text, intent=pending_intent)
            else:
                _send_onboarding_choice(frm)
            return

        if button_id in ("await_type_details", "await_send_screenshot"):
            # Item 17's strike-2 alternatives for awaiting_field -- both
            # are pure nudges (re-send the same open question), not a new
            # answer to consume, so neither counts as a strike either
            # way and the session's attempt count is left untouched.
            if session is not None and session.get("state") == "awaiting_field":
                with _WA_SESSIONS_LOCK:
                    session["last_activity"] = time.time()
                    _WA_SESSIONS[frm] = session
                whatsapp.send_buttons(frm, _closing_question(session.get("missing", [])),
                                       [("start_new_chat", "Start over")])
            else:
                _send_onboarding_choice(frm)
            return

        if cancel_kind == "partial":
            # Starts with a cancel phrase but clearly carries more
            # content past it (e.g. "nevermind, the second one") -- an
            # outright reset risks wiping a perfectly good session over
            # a phrase that wasn't actually meant as one. Ask, don't
            # assume.
            whatsapp.send_buttons(frm, "Want to start over?",
                                   [("confirm_cancel", "Yes, start over"), ("cancel_continue", "No, continue")])
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
            if session is not None:
                with _WA_SESSIONS_LOCK:
                    _WA_SESSIONS.pop(frm, None)
            _send_onboarding_choice(frm)
            return

        if url:
            # A real link is treated as an implicit "different hotel,
            # forget the old one", in any state -- safer to trust than
            # guessing the same from a bare photo.
            if session is not None:
                print(f"[wa v6] {frm} sent a new link mid-conversation — dropping the old session", flush=True)
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
            _handle_choosing_option(frm, session, items, button_id)
            return

        if session.get("state") == "not_cheaper":
            _handle_not_cheaper(frm, session, items, button_id)
            return

        _handle_awaiting_field(frm, session, items)
    except Exception as e:  # noqa: BLE001
        print(f"[wa v6] ERROR handling batch: {type(e).__name__}: {e}\n{traceback.format_exc()}",
              flush=True)
        if allow_retry:
            # Session deliberately left exactly as it was -- a broken
            # reply should never cost the customer whatever they'd
            # already told the bot. Never surface the exception itself.
            whatsapp.send_buttons(
                frm, "Sorry, something went wrong on my side. Your details are still saved.",
                [("try_again", "Try again"), ("start_new_chat", "Start over")])
        else:
            with _WA_SESSIONS_LOCK:
                _WA_SESSIONS.pop(frm, None)
            _LAST_BATCH_ITEMS.pop(frm, None)
            _PENDING_PATH.pop(frm, None)
            wa_send(frm, "That didn't work either — let's start fresh.")
            _send_onboarding_choice(frm)


def handle_batch(frm: str, items: list) -> None:
    with _WA_SESSIONS_LOCK:
        session = _WA_SESSIONS.get(frm)

    if session is not None and time.time() - session.get("last_activity", 0) > _SESSION_MAX_AGE_SEC:
        print(f"[wa v6] session for {frm} abandoned (idle > {_SESSION_MAX_AGE_SEC}s) — clearing it "
              f"(memory hygiene, not a reply-relevance judgment)", flush=True)
        with _WA_SESSIONS_LOCK:
            _WA_SESSIONS.pop(frm, None)
        session = None

    if _button_id(items) == "try_again":
        last_items = _LAST_BATCH_ITEMS.get(frm)
        if not last_items:
            # Nothing on file to replay (e.g. process restarted in
            # between) -- fail into the onboarding choice rather than a
            # silent no-op.
            wa_send(frm, "Nothing to retry — let's start fresh.")
            _send_onboarding_choice(frm)
            return
        _run_batch_once(frm, last_items, session, allow_retry=False)
        return

    _run_batch_once(frm, items, session, allow_retry=True)
