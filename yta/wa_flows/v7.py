"""v7 — v6's conversation, plus a conversation-DESIGN pass (not a bug-fix
pass, unlike every prior version): v6 went live, real customers gave
diffuse "the flow isn't self-understood" feedback with no single
transcript to chase, so this version was built against a read of
established conversational-UX research (Nielsen Norman Group's chatbot
guidelines, Meta's own WhatsApp Business Platform docs on interactive
messages, general conversational-commerce practice) plus a full cold
re-read of v6 against it. See the plan this was built from for the full
13-point finding list; each fix below cites which finding(s) it answers.

Starts as an exact copy of v6.py -- v6.py itself is untouched and stays
live and unaffected until this version is validated and YTA_WA_FLOW is
flipped; this is a separate module with its own session store, same as
v0-v6 each having their own.

What v7 adds on top:

  18. (findings 1, 2, 5) The opening message now states the value in one
      line with a concrete, credible number (the same "10-15% less" line
      proven back in v3/v4 -- reused rather than reinvented after a live
      copy pass showed an invented "partner pricing" framing read as
      vague/off), gives a concrete example of what to send, and never
      names TripJack -- all in the same first message, instead of an
      abstract branching question with no context. The two button labels
      changed from internal jargon to what the customer is actually
      choosing:
      "I have a deal" -> "I picked a room", "Search a hotel" ->
      "Help me find a hotel". Same two `intent` values (`deal`/`search`)
      downstream -- copy only, `_effective_missing()` is untouched.

  19. (finding 6) `_ONBOARDING_TEXT` and `_SEARCH_TEXT` (sent right after
      the opening choice) and the "I couldn't find hotel details in
      that" nudge in `_run_extraction` were the only messages in the
      whole v6 flow with zero tap options -- a confused customer there
      had nothing to tap. All three now carry a "Start over" button, same
      as every other message in the flow.

  20. (finding 10) The two "nothing live" dead ends -- no live rate for a
      cleanly-matched room, and no live rates at all in the room-options
      path -- now also offer "Talk to a human" alongside "Try another
      hotel" (see item 22), not just a single way out.

  21. (findings 3, 13) Room selection is now a native WhatsApp interactive
      LIST message (`whatsapp.send_list`, via the new
      `wa_shared.room_list_sections()` helper) instead of a numbered text
      bubble -- Meta's own guidance recommends a tap-through list over
      free text for more than 3 choices. `parse_inbound()` normalizes a
      list-row tap into the exact same shape a button tap already
      produces, so `_handle_choosing_option()` needed almost no new
      branching -- and its original numeral parser (`re.search(r"\\b([1-9])
      \\b", text)`) is left wired in unchanged as a fallback: a customer
      who types "2" instead of tapping still works.

  22. (findings 4, 9) "Talk to a human" is now a global intent, not just a
      button: `_human_help_match()` mirrors `_cancel_match_kind()`'s
      exact/prefix phrase design (a message carrying a URL or referral
      code is never hijacked by it), checked in `_run_batch_once`
      alongside cancel/referral detection in every state. Either the
      button or the phrase calls `record_lead(..., "needs_human", ...)`
      -- the same queue a confirmed lead already lands in, just a new
      status -- and the session is left OPEN (this isn't a cancel).

  23. (finding 8) "help" is now a second global intent
      (`_help_match()`, same phrase-matching shape as item 22) --
      re-sends the (now much clearer, per item 18) opening message
      without wiping the session or changing state, so a customer who
      types "what is this" or "I don't understand" gets a tailored answer
      instead of falling through to a failed extraction attempt.

  24. (finding 11) Two occupancy quick-reply buttons
      (`wa_shared.OCCUPANCY_QUICK_REPLIES`) are now offered alongside the
      ordinary `stay.occupancy`/`stay.rooms` question -- free text still
      answers anything unusual exactly as before; the presets just
      button-ify the majority real-world case.

  25. (finding 7) The concrete example (`_FIELD_EXAMPLES`) that used to
      only appear on the 1st unproductive RETRY now also appears on the
      very FIRST ask for a missing field -- shown before the customer
      needs it, not as a consolation after a wrong guess. (Finding 12,
      the strike-2 "Type details"/"Send screenshot" copy, is folded in
      here too -- the screenshot option now names what a good screenshot
      looks like instead of repeating the identical sentence.)

What v6 already does, carried over unchanged below (see v6.py's own
docstring for the full reasoning on each):

  12-17. The error/retry safety net, the rewritten confirm wording, the
     `not_cheaper` state, refined cancel-phrase matching, the
     awaiting_field guards, and the gentler 3-strike retry ladders. See
     v6.py.

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
    occ_field, occ_repr, wa_send, wa_send_image, wa_send_list, wa_send_buttons, room_list_sections,
    CHECKING_PHRASES as _CHECKING_PHRASES,
    FETCHING_PHRASES as _FETCHING_PHRASES,
    FOUND_OPENERS as _FOUND_OPENERS,
    NATURAL_QUESTIONS as _NATURAL_QUESTIONS,
    NATURAL_NOUNS as _NATURAL_NOUNS,
    OCCUPANCY_QUICK_REPLIES as _OCCUPANCY_QUICK_REPLIES,
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


def _short_phrase_match(text: str, url, has_referral: bool, phrases: tuple,
                        *, max_words: int = 6) -> bool:
    """Shared shape for the two new global intents (finding 4/9's "talk to
    a human" and finding 8's "help") -- same URL/referral exemption as
    _cancel_match_kind (a message carrying either always has real content
    of its own). Unlike cancel's startswith-only check, this searches for
    the phrase ANYWHERE in the message ("can I talk to a human" doesn't
    start with the phrase, but plainly means it) -- safe here specifically
    because it's gated on a short message (at most `max_words` words), so
    a long real query that happens to mention "help" in passing never
    matches; only a genuinely short, on-topic message does."""
    if url or has_referral:
        return False
    norm = _normalize_for_cancel(text)
    if not norm or len(norm.split()) > max_words:
        return False
    padded = f" {norm} "
    for phrase in phrases:
        if f" {phrase} " in padded:
            return True
    return False


def _human_help_match(text: str, url, has_referral: bool) -> bool:
    return _short_phrase_match(text, url, has_referral, _HUMAN_HELP_PHRASES)


def _help_match(text: str, url, has_referral: bool) -> bool:
    return _short_phrase_match(text, url, has_referral, _HELP_PHRASES)

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

# Rewritten for v7 (finding 1, 2, 5): states the mechanism ("partner
# pricing", never TripJack by name) and gives a concrete example in the
# SAME first message, instead of an abstract branching question with no
# context -- NN/G's finding is that a customer decides within ~5 seconds
# whether a tool will work for them, and that call is made from concrete
# examples, not framing alone. Button labels changed from internal jargon
# ("I have a deal") to what the customer is actually choosing -- same two
# `intent` values (deal/search) downstream, this is copy only.
# The two starting paths, named for what the guest HAS, not for what the bot does
# (owner feedback 2026-10-07: "Help me find a hotel" reads as a city search -- the
# bot can only price a SPECIFIC hotel -- and "I picked a room" did not say what
# happens next). Button titles are capped at 20 chars by WhatsApp.
_BTN_DEAL = "Compare my price"      # button id "have_deal"
_BTN_SEARCH = "Check a hotel"       # button id "search_hotel"

_ONBOARDING_CHOICE_TEXT = (
    "Hi! 👋 I check hotel rates and often find the same room for 10–15% less.\n\n"
    "How would you like to start?\n\n"
    f"• *{_BTN_DEAL}* — you found a room on another site. Send me its link or a "
    "screenshot and I'll check if I can beat the price.\n"
    f"• *{_BTN_SEARCH}* — you know the hotel. Tell me its name, dates and guests "
    "and I'll show you live room rates.\n\n"
    "You can also just send a link or screenshot any time."
)

# Follow-up once "Compare my price" is tapped -- a specific room
# really is expected on this path (see _effective_missing). Opens with a
# concrete example (finding 1) before the requirements list, instead of
# only the list.
_ONBOARDING_TEXT = (
    "Great — send me the link to that room (a Booking.com, MakeMyTrip, or "
    "similar page works), or a screenshot of it. For example, I can read "
    "a page or screenshot showing:\n"
    "• Hotel name\n"
    "• Dates\n"
    "• Guest count\n"
    "• Room type\n"
    "• Total price\n\n"
    "A couple of screenshots work just as well if it doesn't fit in one. "
    "If I find a better deal, I'll show you the savings — no obligation "
    "to book through me."
)

# Follow-up once "Check a hotel" is tapped -- deliberately never
# asks for a room or a price: there's no OTA deal to compare against on
# this path (see _effective_missing), just a live look at what's
# available. Gives a concrete example (finding 1) instead of only naming
# the fields.
def _search_example_year() -> int:
    from datetime import date
    t = date.today()
    return t.year + (1 if t.month >= 11 else 0)


_SEARCH_TEXT = (
    "Great — send me:\n"
    "• The *exact hotel name* (I can check one hotel at a time, not a whole city)\n"
    "• Your dates\n"
    "• Number of guests\n\n"
    f"For example: \"Taj Santacruz, Mumbai, 12-14 Dec {_search_example_year()}, 2 adults\"\n\n"
    "I'll show you the live room options to choose from — no need to "
    "pick a room first."
)

# Fixed set of phrases recognized as a global "talk to a human" request
# (finding 4, 9) -- matched the SAME way as _CANCEL_PHRASES via
# _human_help_match() below, so a message carrying a URL or referral code
# is never hijacked by a phrase that happens to appear in it.
_HUMAN_HELP_PHRASES = (
    "talk to a human", "talk to someone", "speak to a person",
    "speak to someone", "real person", "human help", "human please",
    "agent", "customer service", "customer support",
)

# Fixed set of phrases recognized as a global "what is this / how does
# this work" request (finding 8) -- same matching shape as
# _HUMAN_HELP_PHRASES / _CANCEL_PHRASES.
_HELP_PHRASES = (
    "help", "what can you do", "what is this", "how does this work",
    "how do you work", "what do you do", "confused", "i don't understand",
    "i dont understand", "not sure how this works",
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
    if not packet.requested_offer.room_name or getattr(packet, "_skip_price", False):
        missing = [m for m in missing if m != "ota_benchmark.final_payable"]
    return missing


def _send_onboarding_choice(frm: str) -> None:
    # Button ids unchanged ("have_deal"/"search_hotel") -- every downstream
    # intent/routing decision keys off the id, never the label, so
    # relabeling for clarity (finding 2) is pure copy, zero logic change.
    wa_send_buttons(frm, _ONBOARDING_CHOICE_TEXT,
                           [("have_deal", _BTN_DEAL), ("search_hotel", _BTN_SEARCH)])


def _handle_human_help(frm: str, session: dict | None) -> None:
    """Finding 4/9's escape hatch -- reached from a button OR the global
    `_human_help_match` text phrase, from any state. Flags a lead for the
    same personal follow-up a CONFIRMED deal already gets
    (yta.leads.db.record_lead's "needs_human" status, v7+) when there's a
    packet to attach; a session-less contact (nothing typed yet at all)
    has nothing to flag, so it's just acknowledged and left to continue
    normally. Never pops the session -- this isn't a cancel, and dropping
    whatever the customer already said while they're asking for MORE help
    would be exactly backwards."""
    from yta.leads.db import record_lead
    packet = session.get("packet") if session is not None else None
    if packet is None:
        # Nothing typed yet -- still flag the PHONE NUMBER, otherwise the
        # promise below ("I'll take a personal look") would be empty.
        from datetime import datetime, timezone
        from yta.schema import BookingIntent, Source
        packet = BookingIntent(source=Source(ota="whatsapp", url="", page_type="chat",
                                             extraction_method="none",
                                             extracted_at=datetime.now(timezone.utc).isoformat()))
    try:
        record_lead(frm, "needs_human", packet, (session or {}).get("resolution"))
    except Exception as e:  # noqa: BLE001 -- a logging failure must never block the reply
        print(f"[wa v7] needs_human lead logging failed for {frm}: {type(e).__name__}: {e}",
              flush=True)
    if session is not None and session.get("packet") is not None:
        wa_send_buttons(frm, "Got it — I'll take a personal look and message you here shortly. "
                             "Feel free to keep going in the meantime.",
                        [("start_new_chat", "Start over")])
    else:
        wa_send_buttons(frm, "Got it — I'll take a personal look and message you here shortly. "
                             "In the meantime, you can share a hotel link, a screenshot, or just "
                             "the hotel name and dates, and I'll pass it straight along.",
                        [("have_deal", _BTN_DEAL), ("search_hotel", _BTN_SEARCH)])


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
                print(f"[wa v7] downloaded media: {len(data)} bytes, {mime}", flush=True)
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
    shareable = f"I just found a better hotel rate through Pocket Stays — check yours here: {link}"
    return ask, shareable


_PCT_RE = re.compile(r"\((\d+)%\)")


def _with_percent_headline(text: str, savings_line: str | None) -> str:
    """Live feedback 2026-09-30: the percentage saved was buried in the
    price-comparison block below the fold -- customers skimming just see
    a generic "found you a better rate" headline. Pulls the number
    straight out of savings_line (already computed by wa_shared's
    deal_message(), format "...saved X (NN%) on this one.") and puts it
    in the headline itself. Local to v7 only -- wa_shared.deal_message()
    is shared by v2-v6 and stays untouched; a savings_line-less bookable
    result (not directly comparable to any OTA price) leaves the generic
    headline as-is, since there's no percentage to show."""
    if not savings_line:
        return text
    m = _PCT_RE.search(savings_line)
    if not m:
        return text
    return text.replace(
        "*Good news — I found you a better rate.*",
        f"*Good news — we found you a {m.group(1)}% better rate!*",
        1,
    )


def _send_deal_result(frm: str, packet, resolution: dict | None) -> None:
    """Builds and sends the final deal message from a resolution that
    names exactly ONE candidate rate (resolution["room_map"], a single-
    entry rate_options list) -- shared by the single-room-request path
    (_present_deal) and the pick-one-of-N path (_handle_choosing_option,
    once a customer picks from the option list), so both end up in the
    exact same confirm/decline + lead-recording flow."""
    text, matched, bookable, savings_line, confirm_line = _deal_message(
        packet, resolution,
        compare=(resolution or {}).get("compare_ok", True) and not getattr(packet, "_assumed_guests", False))

    if bookable:
        text = _with_percent_headline(text, savings_line)
        text = re.sub(r"(Pocket Stays price: \*[^*]+\*)",
                      lambda m: f"{m.group(1)} total for {_nights_label(packet)}", text, count=1)
        # The comparison variant has no "Pocket Stays price" line -- label its
        # bare money-bag header the same way so the guest always knows the
        # figure is the TOTAL for the whole stay, not per night.
        note = _assumption_line(packet)
        if note:
            ask = "Shall I go ahead and secure this for you?"
            text = text.replace(ask, f"{note}\n\n{ask}", 1) if ask in text else f"{text}\n\n{note}"
        if "Pocket Stays price" not in text:
            text = text.replace("\n💰\n", f"\n💰 Total for {_nights_label(packet)}\n", 1)
        with _WA_SESSIONS_LOCK:
            _WA_SESSIONS[frm] = {"state": "presented", "packet": packet,
                                  "resolution": resolution, "savings_line": savings_line,
                                  "confirm_line": confirm_line, "last_activity": time.time()}
        # Finding (2026-09-30 live feedback): "Explore other rooms" -- a
        # third option alongside confirm/decline, so a customer who likes
        # the HOTEL but not this specific room isn't forced to decline
        # and start over. See _handle_presented's "explore_other_rooms".
        wa_send_buttons(frm, text, [("confirm_book", "Yes, book this"),
                                    ("decline_book", "Not now"),
                                    ("explore_other_rooms", "Explore other rooms")])
    elif matched:
        # A real rate, but not actually cheaper -- deal_message() only
        # ever returns matched=True/bookable=False for exactly this
        # (comparable-and-not-cheaper) case, never any other. Used to be
        # a dead end (a plain message, no buttons, no state) -- now a
        # real `not_cheaper` state with somewhere to go next (item 14).
        _present_not_cheaper(frm, packet, resolution)
    else:
        # Finding 10: a genuine pre-rate dead end -- now also offers the
        # human-help escape, not just one way out.
        wa_send_buttons(frm, text, [("try_another", "Try another hotel"),
                                          ("human_help", "Talk to a human")])
    print(f"[wa v7] batch for {frm} complete (matched={matched} bookable={bookable})", flush=True)


def _present_not_cheaper(frm: str, packet, resolution: dict) -> None:
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
        print(f"[wa v7] no_deal lead logging failed for {frm}: {type(e).__name__}: {e}", flush=True)

    with _WA_SESSIONS_LOCK:
        _WA_SESSIONS[frm] = {"state": "not_cheaper", "packet": packet, "resolution": resolution,
                              "excluded_room_name": excluded_room_name,
                              "unproductive_attempts": 0, "last_activity": time.time()}
    price_str = f"{ota_ccy} {ota_price:,.0f}" if ota_price else "your current price"
    wa_send_buttons(
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
            wa_send_buttons(
                frm, "That's actually the only room I found a live rate for at this hotel "
                     "right now.", [("try_another", "Try another hotel")])
            return
        rgr = list_cheapest_rooms(remaining)
        new_resolution = dict(resolution)
        new_resolution["reference_room_type_id"] = (resolution.get("reference_room_type_id")
                                                    or (resolution.get("room_map") or {}).get("room_type_id"))
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
    wa_send_buttons(
        frm, "Just tap one of the options below.",
        [("see_other_rooms", "See other rooms"), ("try_another", "Try another hotel")])


def _cap_rows(groups: list, limit: int, keep: str | None = None) -> list:
    """Trim `groups` so their options total at most `limit` rows, dropping from
    the END (the priciest rooms) -- the hotel's cheapest rate is never what gets
    cut. `keep` (the guest's nearest-match room) is retained even if it sits
    beyond the limit, at the cost of the priciest other rooms. Keeps the
    session's option list 1:1 with the rows the guest can actually see and tap
    (an ambiguous list can be 6 rooms x 2 = 12 rows)."""
    def _opts(g):
        return g.get("options") or []
    chosen, used = [], 0
    keeper = next((g for g in groups if keep and g.get("room_type_id") == keep), None)
    reserve = len(_opts(keeper)[:limit]) if keeper is not None else 0
    for g in groups:
        if g is keeper:
            continue
        take = _opts(g)[: max(limit - used - reserve, 0)] if keeper is not None else _opts(g)[: limit - used]
        if not take:
            continue
        used += len(take)
        chosen.append((g, take))
    out = []
    for g in groups:                                    # restore the original (cheapest-first) order
        if g is keeper:
            out.append({**g, "options": _opts(g)[:limit]})
        else:
            hit = next((t for gg, t in chosen if gg is g), None)
            if hit:
                out.append({**g, "options": hit})
    return out


def _group_price(g: dict) -> float:
    return min((o.get("total_price") or float("inf") for o in (g.get("options") or [])), default=float("inf"))


def _present_option_choices(frm: str, packet, resolution: dict) -> None:
    """The no-requested-room path: resolution["room_options"]
    (yta.roommap.list_cheapest_rooms()) names up to 5 DISTINCT rooms
    (the cheapest 5, ranked by each room's own lowest price), each with
    up to 2 of its own meal x refundability variants. Finding 3/13
    (v7+): shown as a native WhatsApp interactive LIST message -- one
    section per room, one row per variant -- instead of a numbered text
    bubble; Meta's own guidance recommends a tap-through list over free
    text for more than 3 choices. Row ids are "opt-N", N matching the
    FLATTENED option order 1:1, so `_handle_choosing_option`'s original
    numeral parser still works unchanged as a fallback for a customer who
    types "2" instead of tapping. Opens a "choosing_option" session
    holding the FLATTENED option list for that reply, same as before."""
    rz = resolution or {}
    groups = (rz.get("room_options") or {}).get("groups") or []
    nearest_id = (rz.get("room_options") or {}).get("nearest_match_room_type_id")
    # Owner policy: the hotel-level CHEAPEST room is always at the top of any room
    # list (explore / see other rooms / no match / ambiguous). The nearest match is
    # still kept in the list, and called out in the text, just not placed first.
    groups = sorted(groups, key=_group_price)
    groups = _cap_rows(groups, 10, keep=nearest_id)   # WhatsApp shows at most 10 list rows
    flat_options = [opt for g in groups for opt in (g.get("options") or [])]
    hotel_name = (rz.get("detail") or {}).get("hotel_name") \
        or (rz.get("match") or {}).get("hotel_name") \
        or packet.hotel.name or "this hotel"

    if not flat_options:
        # Finding 10: a genuine pre-rate dead end -- now also offers the
        # human-help escape.
        wa_send_buttons(
            frm, f"I wasn't able to find any live rates for {hotel_name} for these dates "
                 f"right now. Happy to take a look at another property, if you'd like?",
            [("try_another", "Try another hotel"), ("human_help", "Talk to a human")])
        print(f"[wa v7] batch for {frm} complete (no room_options available)", flush=True)
        return

    lines = [f"🏨 *{hotel_name}*"]
    date_occ = []
    if packet.stay.check_in and packet.stay.check_out:
        date_occ.append(f"{_short_date(packet.stay.check_in)} → {_short_date(packet.stay.check_out)}")
    if packet.stay.occupancy:
        date_occ.append(occ_repr(packet.stay.occupancy))
    if date_occ:
        lines.append("📅 " + " · ".join(date_occ))

    # Finding (2026-09-30 live feedback): the old two-callout "nearest
    # match AND cheapest option" framing promised two things but often
    # collapsed to showing just one (when the nearest match WAS the
    # cheapest room) -- confusing on screen, and neither case gave a
    # clean price anchor. Replaced with a single, always-accurate
    # "starting from" price -- the one number that's true regardless of
    # match confidence -- plus a plain note of the closest match when a
    # specific room WAS requested but not confidently enough to quote
    # outright (see yta/web.py::_resolve()).
    all_opts = [o for g in groups for o in (g.get("options") or [])]
    ambiguous = (rz.get("room_options") or {}).get("ambiguous_match") and groups
    if ambiguous:
        nearest = next((g for g in groups if nearest_id and g.get("room_type_id") == nearest_id), groups[0])
        nearest_name = _clean_room_name(nearest.get("room_name")) or "a room"
        lines += ["", f"I couldn't confidently match your room to one exact type — "
                       f"closest to what you mentioned: *{nearest_name}*.", ""]
    else:
        lines.append("")
    if all_opts:
        # Names the room the price actually belongs to (the cheapest
        # option's own room), not the nearest-match room -- the two can
        # differ, and pairing the number with the wrong name would be
        # worse than not naming one at all.
        cheapest_opt = min(all_opts, key=lambda o: o.get("total_price") or float("inf"))
        ccy = cheapest_opt.get("currency") or ""
        price = cheapest_opt.get("total_price") or 0
        cheapest_name = _clean_room_name(cheapest_opt.get("room_name")) or "a room"
        lines.append(f"💰 We have rooms starting from {ccy} {price:,.2f} total for "
                     f"{_nights_label(packet)} — *{cheapest_name}*")
        lines.append("")
    note = _assumption_line(packet)
    if note:
        lines += [note, ""]
    lines.append("Tap to explore more rooms")

    sections = room_list_sections(groups, clean_name=_clean_room_name)
    n = sum(len(rows) for _, rows in sections)

    with _WA_SESSIONS_LOCK:
        _WA_SESSIONS[frm] = {"state": "choosing_option", "packet": packet,
                              "resolution": resolution, "options": flat_options,
                              "unproductive_attempts": 0, "last_activity": time.time()}
    wa_send_list(frm, "\n".join(lines), "Choose a room", sections)
    print(f"[wa v7] batch for {frm} complete ({n} option(s) across {len(groups)} room(s))", flush=True)


def _present_deal(frm: str, packet, ack: bool = True) -> None:
    from yta.web import _resolve   # lazy, same reason wa_shared.finish_and_reply does this

    md = _year_missing_md(packet)
    if md and len(_year_candidates(md)) == 2:   # never search a past/unknown-year date
        _ask_year(frm, packet, md, None)
        return
    if ack:
        wa_send(frm, random.choice(_FETCHING_PHRASES))
    resolution = _resolve(packet) if packet.hotel.name else None
    _rz = resolution or {}
    _det = _rz.get("detail") or {}
    print(f"[wa v7] resolve: hotel={packet.hotel.name!r} band={_rz.get('band')} "
          f"available={_rz.get('available')} detail_error={_rz.get('detail_error')!r} "
          f"note={_rz.get('note')!r} n_options={len(_det.get('options') or [])} "
          f"pricing_notes={(_det.get('notes') or [])[:1]} "
          f"dates={packet.stay.check_in}->{packet.stay.check_out} "
          f"occ={occ_repr(packet.stay.occupancy)} "
          f"room_asked={packet.requested_offer.room_name!r}", flush=True)
    rz = resolution or {}
    if rz.get("band") == "medium" and (rz.get("match") or {}).get("hotel_name"):
        # A fuzzy hotel match is a guess -- confirm before quoting anything.
        with _WA_SESSIONS_LOCK:
            _WA_SESSIONS[frm] = {"state": "confirming_hotel", "packet": packet,
                                  "resolution": resolution, "unproductive_attempts": 0,
                                  "last_activity": time.time()}
        wa_send_buttons(frm, f"Just to be sure — is the hotel *{rz['match']['hotel_name']}*?",
                        [("hotel_yes", "Yes, that's it"), ("hotel_no", "No, a different one")])
        return
    _present_resolution(frm, packet, resolution)


def _no_rate_reply(frm: str, packet, rz: dict) -> bool:
    """Say WHY there is no rate, truthfully -- "I wasn't able to find a better
    live rate" used to cover a supplier outage, a hotel we don't know, and a
    sold-out stay alike, each of which needs a different next step. Returns
    True when it sent a reply."""
    name = packet.hotel.name or "this hotel"
    if rz.get("detail_error"):
        print(f"[wa v7] {frm}: supplier error -> {rz.get('detail_error')!r}", flush=True)
        wa_send_buttons(frm, "I'm having trouble reaching the live rates right now — that's on my "
                             "side, not yours. Tap *Try again* in a moment.",
                        [("try_again", "Try again"), ("human_help", "Talk to a human")])
        return True
    if not rz.get("match"):
        wa_send_buttons(frm, f"I couldn't find *{name}* in my hotel list. Could you check the "
                             f"spelling, or send the booking link or a screenshot of it?",
                        [("try_another", "Try another hotel"), ("human_help", "Talk to a human")])
        return True
    if not ((rz.get("detail") or {}).get("options")):
        wa_send_buttons(frm, f"I don't see any live rates for *{name}* on those dates right now. "
                             f"Want to try different dates, or another hotel?",
                        [("try_another", "Try another hotel"), ("human_help", "Talk to a human")])
        return True
    return False


def _present_resolution(frm: str, packet, resolution) -> None:
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
    if not ((resolution or {}).get("room_map") or {}).get("matched") \
            and _no_rate_reply(frm, packet, resolution or {}):
        return
    _send_deal_result(frm, packet, resolution)


def _handle_confirming_hotel(frm: str, session: dict, items: list, button_id) -> None:
    text = _text_of(items)
    yes = button_id == "hotel_yes" or (button_id is None and _CONFIRM_RE.search(text))
    no = button_id == "hotel_no" or (button_id is None and _DECLINE_RE.search(text))
    packet = session["packet"]
    if yes:
        with _WA_SESSIONS_LOCK:
            _WA_SESSIONS.pop(frm, None)
        _present_resolution(frm, packet, session.get("resolution"))
        return
    if no:
        packet.hotel.name = None
        with _WA_SESSIONS_LOCK:
            _WA_SESSIONS[frm] = {"state": "awaiting_field", "packet": packet,
                                  "missing": ["hotel.name"], "intent": None,
                                  "unproductive_attempts": 0, "last_activity": time.time()}
        wa_send_buttons(frm, "Sorry about that — which hotel is it? Send the exact name as "
                             "shown on the booking page.",
                        [("start_new_chat", "Start over"), ("human_help", "Talk to a human")])
        return
    attempts = session.get("unproductive_attempts", 0) + 1
    if attempts >= 3:
        with _WA_SESSIONS_LOCK:
            _WA_SESSIONS.pop(frm, None)
        _send_onboarding_choice(frm)
        return
    session["unproductive_attempts"] = attempts
    session["last_activity"] = time.time()
    name = ((session.get("resolution") or {}).get("match") or {}).get("hotel_name") or "this hotel"
    wa_send_buttons(frm, f"Is the hotel *{name}*? Tap one below.",
                    [("hotel_yes", "Yes, that's it"), ("hotel_no", "No, a different one")])


_SLOW_NOTICE_SEC = 12
_LLM_TROUBLE_TEXT = ("I'm having trouble reading that right now — it's on my side, not yours. "
                    "Tap *Try again* in a moment and I'll pick it up from here.")
_ORPHAN_BUTTON_IDS = {"confirm_book", "decline_book", "explore_other_rooms",
                      "show_list_again", "see_other_rooms", "year_0", "year_1", "hotel_yes", "hotel_no", "show_anyway"}


def _start_slow_notice(frm: str):
    """Reading a screenshot/link can take 10-60s with nothing sent in the
    meantime, which reads as a dead bot. If extraction is still running
    after _SLOW_NOTICE_SEC, say so once."""
    done = threading.Event()

    def _fire():
        if not done.is_set():
            wa_send(frm, "Still reading this one — it can take up to a minute. "
                         "Hang tight 🙏")

    t = threading.Timer(_SLOW_NOTICE_SEC, _fire)
    t.daemon = True
    t.start()
    return done, t


def _year_missing_md(packet):
    """((m, d), (m, d)) when the stay dates have no trustworthy year, else
    None. Policy (explicit, from the owner): NEVER guess a year and NEVER
    search a past date -- ask the guest. Triggers when the source showed
    month-day only (see pipeline._apply), or when the dates came out in the
    past (a guessed/old year)."""
    from datetime import date
    ym = getattr(packet, "_year_missing", None) or {}
    if ym.get("check_in") and ym.get("check_out"):
        return ym["check_in"], ym["check_out"]
    try:
        ci = date.fromisoformat(packet.stay.check_in)
        co = date.fromisoformat(packet.stay.check_out)
    except (TypeError, ValueError):
        return None
    if ci < date.today():
        return (ci.month, ci.day), (co.month, co.day)
    return None


def _year_candidates(md):
    """Two (check_in, check_out) options: the next occurrence of those
    dates (current year if still ahead, else next year), then the year
    after -- never a past date."""
    from datetime import date
    (m1, d1), (m2, d2) = md
    today = date.today()
    out = []
    y = today.year
    for _ in range(4):
        try:
            ci = date(y, m1, d1)
            co = date(y if (m2, d2) > (m1, d1) else y + 1, m2, d2)
        except ValueError:
            y += 1
            continue
        if ci >= today and co > ci:
            out.append((ci, co))
            if len(out) == 2:
                break
        y += 1
    return out


def _ask_year(frm: str, packet, md, intent) -> None:
    cands = _year_candidates(md)
    (m1, d1), (m2, d2) = md
    from datetime import date
    shown = f"{date(2000, m1, d1):%-d %b} – {date(2000, m2, d2):%-d %b}"
    opts = {f"year_{i}": c for i, c in enumerate(cands)}
    with _WA_SESSIONS_LOCK:
        _WA_SESSIONS[frm] = {"state": "awaiting_year", "packet": packet, "intent": intent,
                              "year_options": opts, "unproductive_attempts": 0,
                              "last_activity": time.time()}
    buttons = [(bid, f"{ci:%-d %b}–{co:%-d %b} {ci.year}") for bid, (ci, co) in opts.items()]
    buttons.append(("start_new_chat", "Start over"))
    wa_send_buttons(frm, f"📅 *Which year is {shown}?*\nTap one below, or just type the year.",
                    buttons[:3])


def _handle_awaiting_year(frm: str, session: dict, items: list, button_id) -> None:
    opts = session.get("year_options") or {}
    pick = opts.get(button_id)
    if pick is None:
        m = re.search(r"\b(20\d{2})\b", _text_of(items))
        if m:
            pick = next((c for c in opts.values() if c[0].year == int(m.group(1))), None)
    if pick is None:
        attempts = session.get("unproductive_attempts", 0) + 1
        if attempts >= 3:
            with _WA_SESSIONS_LOCK:
                _WA_SESSIONS.pop(frm, None)
            _send_onboarding_choice(frm)
            return
        session["unproductive_attempts"] = attempts
        session["last_activity"] = time.time()
        buttons = [(bid, f"{ci:%-d %b}–{co:%-d %b} {ci.year}") for bid, (ci, co) in opts.items()]
        buttons.append(("start_new_chat", "Start over"))
        wa_send_buttons(frm, "Which year is this stay for? Tap one below, or type the year "
                             "(e.g. 2026).", buttons[:3])
        return
    from yta.schema import LLM
    packet = session["packet"]
    ci, co = pick
    packet.add("stay.check_in", ci.isoformat(), LLM, 1.0, "guest picked the year")
    packet.add("stay.check_out", co.isoformat(), LLM, 1.0, "guest picked the year")
    packet.derive_stay()
    packet._year_missing = {}
    with _WA_SESSIONS_LOCK:
        _WA_SESSIONS.pop(frm, None)
    _continue_with_packet(frm, packet, None, session.get("intent"))


def _nights(packet) -> int | None:
    from datetime import date
    try:
        n = (date.fromisoformat(packet.stay.check_out)
             - date.fromisoformat(packet.stay.check_in)).days
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _nights_label(packet) -> str:
    n = _nights(packet)
    return f"{n} night{'s' if n != 1 else ''}" if n else "your stay"


# --------------------------------------------------------------------------
# Guided asks (owner feedback 2026-10-07): when something is missing, the
# QUESTION comes first (the old message buried it under a recap), and the
# guest gets tappable sample answers plus an "Other -- type it" row instead of
# only "Start over / Talk to a human", which read as a dead end. A tap is the
# guest's own explicit choice, never an assumption. Typing always still works.
# --------------------------------------------------------------------------
_ASK_ORDER = ("hotel.name", "stay.check_in", "stay.check_out", "stay.rooms", "stay.occupancy",
              "requested_offer.room_name", "ota_benchmark.final_payable")
_ASK_KIND = {"hotel.name": "hotel", "stay.check_in": "dates", "stay.check_out": "dates",
             "stay.rooms": "guests", "stay.occupancy": "guests",
             "requested_offer.room_name": "room", "ota_benchmark.final_payable": "price"}
_ASK_TEXT = {
    "hotel": ("🏨 *Which hotel is this?*",
              "Type the exact hotel name, e.g. *Taj Santacruz, Mumbai* — I can check one hotel at a "
              "time, not a whole city. Or send a screenshot."),
    "dates": ("📅 *What are your check-in and check-out dates?*",
              "Just type them, e.g. *17 Dec – 18 Dec 2026* — or send a fuller screenshot."),
    "guests": ("👥 *How many rooms, and how many guests in each?*",
               "Just type it, e.g. *1 room: 2 adults* or *2 rooms: 2 adults + 3 adults* "
               "(add the kids' ages, if any)."),
    "room": ("🛏️ *Which room type is it?*",
             "Type the room name as shown on the page, e.g. *Deluxe Room*."),
    "price": ("💳 *What total price did you see?*",
              "Type the total, e.g. *INR 25,000*."),
}


def _first_missing_kind(missing: list):
    for f in _ASK_ORDER:
        if f in missing:
            return _ASK_KIND[f]
    return None


_SKIPPABLE_KINDS = ("guests", "room", "price")


def _missing_kinds(missing: list) -> list:
    out = []
    for f in _ASK_ORDER:
        k = _ASK_KIND[f]
        if f in missing and k not in out:
            out.append(k)
    return out


def _assumption_sentence(kinds: list, has_price: bool = True) -> str:
    """What "Show my rate anyway" will do, spelled out BEFORE the guest taps.
    `has_price`: the guest gave an OTA price to compare with (the "I picked a
    room" path); on the "find me a hotel" path there is none, so no comparison
    is mentioned."""
    parts = []
    if "guests" in kinds:
        parts.append("assume *1 room, 2 adults*")
    if "room" in kinds:
        parts.append("show *all available rooms*")
    if "price" in kinds:
        parts.append("show our rate *without comparing* it to your price")
    if "guests" in kinds and "price" not in kinds and has_price:
        parts.append("skip the price comparison, since the guests may differ")
    return "I'll " + ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1] if parts else ""


def _can_show_anyway(missing: list, packet=None) -> bool:
    kinds = _missing_kinds(missing)
    if "guests" in kinds and packet is not None:
        rooms = getattr(packet.stay, "rooms", None)
        if isinstance(rooms, int) and rooms > 1:
            return False      # they already said several rooms -- "1 room, 2 adults" would contradict them
    return bool(kinds) and all(k in _SKIPPABLE_KINDS for k in kinds)


def _ask_body(packet, missing: list, clarify: str | None = None) -> str:
    """Every open question in ONE message, question(s) first, no recap of what
    was already read (owner feedback 2026-10-07: "bas question puch, stay
    information mat copy kar follow-up me har bar", and ask everything at once)."""
    kinds = _missing_kinds(missing)
    parts = []
    for kind in kinds:
        question, hint = _ASK_TEXT[kind]
        rooms = getattr(packet.stay, "rooms", None)
        if kind == "guests" and isinstance(rooms, int) and rooms > 1:
            # The guest already said how many rooms ("2 room 5 log"), so the one
            # thing missing is the DISTRIBUTION -- ask exactly that.
            adults = getattr(packet.stay, "adults", None)
            kids = getattr(packet.stay, "children", None) or 0
            who = f"{adults + kids} guests" if (adults and kids) else (f"{adults} adults" if adults else "guests")
            question = f"👥 *How are the {who} distributed across the {rooms} rooms?*"
            hint = ("Just type it, e.g. *Room 1: 2 adults, Room 2: 3 adults* "
                    "(add the kids' ages, if any).")
        elif clarify and len(clarify) <= 140 and len(kinds) == 1:
            # The model's own follow-up refines the ONE open question ("how many
            # children, and how old?"). With several open it tends to rattle them
            # all off in one long sentence -- the per-field questions are cleaner.
            question = f"❓ *{clarify.strip().rstrip('?')}?*"
        city = (getattr(packet.hotel, "city", None) or "").strip()
        if kind == "hotel" and city:
            # They named a place ("Delhi", "hotels in Goa"), not a hotel.
            question = f"🏨 *Which hotel in {city}?*"
            hint = ("Send the exact hotel name, e.g. *Taj Santacruz, Mumbai* — I can check one hotel at "
                    "a time, I can't search all of " + city + " yet.")
        parts.append(f"{question}\n{hint}")
    body = "\n\n".join(parts)
    if _can_show_anyway(missing, packet):
        has_price = bool(getattr(packet.ota_benchmark, "final_payable", None))
        body += f"\n\nOr tap *Show my rate anyway* — {_assumption_sentence(kinds, has_price)}."
    return body[:1000]


def _send_ask(frm: str, packet, missing: list, intent, clarify: str | None = None) -> None:
    """The one place every "something is missing" question is sent from: ALL the
    open questions in one message, question first, a typed example under each,
    nothing else. Buttons are only the escapes (+ "show anyway" where it is
    possible) -- no sample answers (a button can carry a wrong suggestion, e.g.
    "1 room" to a 2-room guest). `clarify` (the model's own follow-up) is only
    used when it refines the single open question, as a short sentence."""
    kind = _first_missing_kind(missing)
    if kind is None:
        wa_send_buttons(frm, clarify or "Could you tell me a bit more?",
                        [("start_new_chat", "Start over"), ("human_help", "Talk to a human")])
        return
    buttons = [("start_new_chat", "Start over"), ("human_help", "Talk to a human")]
    if _can_show_anyway(missing, packet):
        buttons.insert(0, ("show_anyway", "Show my rate anyway"))
    body = _ask_body(packet, missing, clarify)
    wa_send_buttons(frm, body, buttons)


def _handle_show_anyway(frm: str, session: dict) -> None:
    """"Show my rate anyway": proceed without the skippable gaps. Every
    assumption is recorded on the packet (evidence + _assumptions) and printed
    in the rate message, and a rate built on assumed guests is never compared
    with the guest's own price (not like-for-like)."""
    from yta.schema import LLM
    packet, intent = session["packet"], session.get("intent")
    kinds = _missing_kinds(session.get("missing", []))
    assumptions = []
    if "guests" in kinds:
        packet.add("stay.occupancy", [{"adults": 2, "children": 0, "child_ages": []}],
                   LLM, 0.5, "ASSUMPTION: guest tapped Show my rate anyway without giving guests")
        packet.add("stay.rooms", 1, LLM, 0.5, "ASSUMPTION: guest tapped Show my rate anyway")
        packet.stay.occupancy_source, packet.stay.occupancy_confidence = "assumed_default", 0.5
        packet._assumed_guests = True
        assumptions.append("1 room, 2 adults")
    if "room" in kinds:
        intent = "search"
    if "price" in kinds:
        packet._skip_price = True
    packet._assumptions = assumptions
    packet._split_hint = None
    print(f"[wa v7] {frm} tapped Show my rate anyway: kinds={kinds} assumptions={assumptions}", flush=True)
    packet.derive_stay()
    with _WA_SESSIONS_LOCK:
        _WA_SESSIONS.pop(frm, None)
    _continue_with_packet(frm, packet, None, intent)


def _assumption_line(packet) -> str:
    a = getattr(packet, "_assumptions", None)
    return (f"ℹ️ *Assumed:* {', '.join(a)}. Different guests? Send *start over* and give me "
            f"the exact guests.") if a else ""


def _pull_month_day_dates(fields: dict):
    """The clarification extractor returns a year-less date as MM-DD (policy:
    never guess a year). Splits those out so the guest is asked the year with
    buttons instead of getting a text question with nothing to tap."""
    ym = {}
    for key, name in (("stay.check_in", "check_in"), ("stay.check_out", "check_out")):
        v = fields.get(key)
        m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", v.strip()) if isinstance(v, str) else None
        if m:
            ym[name] = (int(m.group(1)), int(m.group(2)))
    if len(ym) == 2:
        fields = {k: v for k, v in fields.items() if k not in ("stay.check_in", "stay.check_out")}
        return fields, ym
    return fields, {}


def _ask_message_with_example(packet, missing: list, clarify: str | None = None) -> str:
    """Wraps wa_shared.found_and_ask_message() to also show a concrete
    example for the single most relevant missing field on the FIRST ask
    (finding 7) -- v6 only showed an example on the 1st unproductive
    RETRY (see _handle_awaiting_field's strike-1 below); this shows it
    before the customer needs it, not as a consolation after a wrong
    guess. Skipped when a specific `clarify` question is already driving
    the message -- that's already a precise, on-point ask; an example
    alongside it would be redundant."""
    msg = _found_and_ask_message(packet, missing, clarify)
    if not clarify and missing:
        example = _FIELD_EXAMPLES.get(missing[0])
        if example:
            msg = f"{msg} For example, {example}."
    return msg


def _ask_missing_buttons(missing: list) -> list:
    """Only the two escapes. The old occupancy quick-reply presets are gone:
    "2 adults, 1 room" offered to a guest who had just said "2 rooms" is a
    wrong suggestion, and a tap can't be taken back."""
    return [("start_new_chat", "Start over"), ("human_help", "Talk to a human")]


def _run_extraction(frm: str, url, media, page_text: str | None = None,
                    intent: str | None = None) -> None:
    from yta.pipeline import extract

    wa_send(frm, random.choice(_CHECKING_PHRASES))
    print(f"[wa v7] extracting: url={url!r} has_media={bool(media)} has_text={bool(page_text)} "
          f"intent={intent!r}", flush=True)
    _slow_done, _slow_timer = _start_slow_notice(frm)
    try:
        packet = extract(url or "", render=bool(url), media=media, page_text=page_text, log_sink=[])
    finally:
        _slow_done.set()
        _slow_timer.cancel()
    print(f"[wa v7] extraction done: hotel={packet.hotel.name!r} status={packet.status}", flush=True)
    if getattr(packet, "_llm_failed", False):
        # Every LLM provider failed (outage / rate limit): nothing was read, so
        # do NOT go on to ask the guest for "missing" fields or say the message
        # had no hotel details -- apologise and offer a retry of the same input.
        print(f"[wa v7] {frm}: every LLM provider failed -- asking the guest to retry", flush=True)
        wa_send_buttons(frm, _LLM_TROUBLE_TEXT,
                        [("try_again", "Try again"), ("start_new_chat", "Start over")])
        return

    # A free-text submission has no unambiguous "this is definitely a
    # booking" signal the way a URL or an upload does -- the pre-filter in
    # _looks_like_a_query only rejects the cheap/obvious non-queries before
    # ever getting here. If NOTHING recognizable came through at all (no
    # hotel name), don't drag the customer into a full slot-filling
    # interrogation seeded from a stray sentence that happened to be long
    # enough to pass the filter -- a soft nudge instead.
    if page_text and not url and not media and not packet.hotel.name:
        # Finding 6: was a bare wa_send with nothing to tap.
        wa_send_buttons(
            frm, "I couldn't find hotel details in that — send the link, a screenshot, "
                 "or the hotel name with your dates and price and I'll take it from there.",
            [("start_new_chat", "Start over"), ("human_help", "Talk to a human")])
        return

    md = _year_missing_md(packet)
    if md and len(_year_candidates(md)) == 2:
        _ask_year(frm, packet, md, intent)
        return
    _continue_with_packet(frm, packet, url, intent)


def _drop_guessed_occupancy(packet) -> None:
    """Owner policy: never guess hotel/dates/room/occupancy -- ask. A multi-
    room booking that only showed totals ("4 adults, 2 rooms") gets an
    even split assumed by the resolver (source "even_split", confidence
    < 0.7); that assumption must not be priced as fact. Cleared here so the
    normal missing-field flow asks for the per-room breakdown."""
    st = packet.stay
    if getattr(st, "occupancy_source", None) == "even_split" \
            and (getattr(st, "occupancy_confidence", None) or 1.0) < 0.7:
        print(f"[wa v7] dropping guessed (even-split) occupancy {occ_repr(st.occupancy)} "
              f"-- will ask the guest", flush=True)
        # What the guest DID state (totals + room count) -- only the
        # per-room split was a guess. Kept so the follow-up can quote it
        # back instead of asking the guest to repeat themselves.
        occ = st.occupancy or []
        adults = sum(occ_field(o, "adults", 0) or 0 for o in occ)
        kids = sum(occ_field(o, "children", 0) or 0 for o in occ)
        rooms = getattr(st, "rooms", None) or len(occ)
        packet._split_hint = {"adults": adults, "children": kids, "rooms": rooms}
        st.rooms = rooms
        st.occupancy = []
        st.occupancy_source = None
        st.occupancy_confidence = None


def _ask_room_split(frm: str, packet, intent, hint: dict) -> None:
    """Owner policy: never guess the per-room guest split. The guest gave
    only totals ("4 adults, 2 rooms"), so quote that back and ask how the
    guests are divided. The even split is offered as an explicit TAP -- a
    choice the guest makes, not an assumption -- and the stated totals are
    seeded as context so a typed "2 adults in each room" resolves
    correctly instead of re-asking how many rooms."""
    a, c, n = hint["adults"], hint["children"], hint["rooms"]
    who = f"{a} adult{'s' if a != 1 else ''}" + (f" + {c} child{'ren' if c != 1 else ''}" if c else "")
    seed = f"The booking is {who} across {n} rooms in total."
    with _WA_SESSIONS_LOCK:
        _WA_SESSIONS[frm] = {"state": "awaiting_field", "packet": packet, "missing": ["stay.occupancy"],
                              "intent": intent, "clarify_text": seed, "split_hint": hint,
                              "unproductive_attempts": 0, "last_activity": time.time()}
    wa_send_buttons(
        frm,
        f"👥 *How are the {who} split across the {n} rooms?*\n"
        f"For example, \"2 adults in each room\", or \"3 adults in room 1 and 1 adult in room 2\".",
        [("start_new_chat", "Start over"), ("human_help", "Talk to a human")])


def _continue_with_packet(frm: str, packet, url, intent) -> None:
    _drop_guessed_occupancy(packet)
    # Always recompute: packet.missing_mandatory is the snapshot from BEFORE
    # anything the guest has since supplied (the year, a dropped guessed
    # occupancy) and would re-ask for fields that are now filled.
    missing = _effective_missing(packet, packet.check_mandatory(), intent)
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
            wa_send_buttons(
                frm, "I wasn't able to pull the details from that MakeMyTrip link — could "
                     "you send a screenshot of the page instead, or just tell me the hotel "
                     "name, dates, guests, and price?",
                [("start_new_chat", "Start over"), ("human_help", "Talk to a human")])
        else:
            hint = getattr(packet, "_split_hint", None)
            if hint and missing == ["stay.occupancy"]:
                _ask_room_split(frm, packet, intent, hint)
                return
            _send_ask(frm, packet, missing, intent)
        return
    # Everything needed came in on the first submission -- still show what
    # was actually read before quoting a price, same as the ask-for-more
    # path does, so the customer can catch a bad extraction either way.
    wa_send(frm, f"Got it all — here's what I have. Checking the best rate now…\n\n{_recap_block(packet)}")
    _present_deal(frm, packet, ack=False)


def _handle_awaiting_field(frm: str, session: dict, items: list) -> None:
    from yta.extract_llm import extract_clarification_ex
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
        print(f"[wa v7] {frm} awaiting_field reply after {age:.0f}s looks like a fresh query "
              f"-- asking before assuming either way", flush=True)
        with _WA_SESSIONS_LOCK:
            session["pending_new_text"] = text
            session["pending_new_media"] = media
            session["last_activity"] = time.time()
            _WA_SESSIONS[frm] = session
        hotel_name = session["packet"].hotel.name or "this search"
        wa_send_buttons(
            frm, f"Welcome back. Should I continue with {hotel_name}, or start a new search?",
            [("welcome_continue", "Continue"), ("welcome_new_search", "New search")])
        return

    accumulated = "\n".join(t for t in (session.get("clarify_text"), text) if t)

    # Occupancy answers usually carry the room count too ("2 room 5 log"), so let
    # the extractor fill stay.rooms alongside stay.occupancy.
    wanted = list(session["missing"])
    if "stay.occupancy" in wanted and "stay.rooms" not in wanted:
        wanted.append("stay.rooms")
    fields, clarify, llm_ok = extract_clarification_ex(wanted, accumulated, media=media)
    fields, year_md = _pull_month_day_dates(fields)
    print(f"[wa v7] clarification filled: {list(fields.keys())}; note={clarify!r}; llm_ok={llm_ok}",
          flush=True)
    if not llm_ok:
        # Every provider failed -- that is OUR outage, not a wrong answer from
        # the guest: no strike, session untouched, honest message.
        with _WA_SESSIONS_LOCK:
            session["last_activity"] = time.time()
            _WA_SESSIONS[frm] = session
        wa_send_buttons(frm, _LLM_TROUBLE_TEXT,
                        [("try_again", "Try again"), ("start_new_chat", "Start over")])
        return

    if not fields and not clarify and not year_md:
        # Guard A (item 16): nothing about the CURRENT question was
        # answered, but the reply reads like a full query on its own --
        # more likely a pivot to a different hotel that a customer typed
        # instead of tapping "Start over" than a genuinely unproductive
        # reply. Treated as a fresh submission on the message as-sent
        # (not `accumulated`, which may carry unrelated earlier context)
        # -- no strike counted.
        if _looks_like_a_fresh_query(text):
            print(f"[wa v7] {frm} awaiting_field reply answers nothing but reads like a new "
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
            with _WA_SESSIONS_LOCK:
                session["last_activity"] = time.time()
                session["unproductive_attempts"] = attempts
                _WA_SESSIONS[frm] = session
            _send_ask(frm, session["packet"], session["missing"], session.get("intent"))
            return
        if attempts == 2:
            # Strike 2 (item 17): NOT a wipe -- offer alternative ways to
            # answer and keep the session open. Only a THIRD unproductive
            # reply (or tapping Start over) actually gives up.
            with _WA_SESSIONS_LOCK:
                session["last_activity"] = time.time()
                session["unproductive_attempts"] = attempts
                _WA_SESSIONS[frm] = session
            wa_send_buttons(
                frm, "Still missing that one — how would you like to share it?",
                [("await_type_details", "Type details"), ("await_send_screenshot", "Send screenshot"),
                 ("start_new_chat", "Start over")])
            return
        print(f"[wa v7] {frm} gave up after {attempts} unproductive replies", flush=True)
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
    if year_md:
        # "22 dec se 24 dec": the day/month are known, the YEAR is not -- ask it
        # with tappable full dates (the rest is asked after the guest picks).
        packet._year_missing = {"check_in": year_md["check_in"], "check_out": year_md["check_out"]}
        md = _year_missing_md(packet)
        if md and len(_year_candidates(md)) == 2:
            _ask_year(frm, packet, md, intent)
            return
    still_missing = _effective_missing(packet, packet.check_mandatory(), intent)
    print(f"[wa v7] still missing after clarification: {still_missing}", flush=True)
    if still_missing:
        with _WA_SESSIONS_LOCK:
            _WA_SESSIONS[frm] = {"state": "awaiting_field", "packet": packet, "missing": still_missing,
                                  "intent": intent, "clarify_text": accumulated,
                                  "unproductive_attempts": 0, "last_activity": time.time()}
        _send_ask(frm, packet, still_missing, intent, clarify)
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

    options = session.get("options") or []
    idx = -1
    if button_id and button_id.startswith("opt-"):
        # A tap on a send_list() row (finding 3/13) -- the row id IS the
        # flattened 1-based index _present_option_choices() assigned it.
        try:
            idx = int(button_id.split("-", 1)[1]) - 1
        except ValueError:
            idx = -1
    if not (0 <= idx < len(options)):
        # No valid list-row id -- fall back to the original numeral
        # parser (finding 13), so a customer who types "2" instead of
        # tapping still works exactly as it did before the list UI.
        text = _text_of(items)
        m = re.search(r"\b([1-9])\b", text)
        idx = int(m.group(1)) - 1 if m else -1

    if not (0 <= idx < len(options)):
        attempts = session.get("unproductive_attempts", 0) + 1
        if attempts == 1:
            with _WA_SESSIONS_LOCK:
                session["last_activity"] = time.time()
                session["unproductive_attempts"] = attempts
                _WA_SESSIONS[frm] = session
            wa_send_buttons(
                frm, f"Tap a room from the list, or reply with a number from 1 to {len(options)}.",
                [("show_list_again", "Show list again"), ("start_new_chat", "Start over")])
            return
        if attempts == 2:
            with _WA_SESSIONS_LOCK:
                session["last_activity"] = time.time()
                session["unproductive_attempts"] = attempts
                _WA_SESSIONS[frm] = session
            wa_send_buttons(
                frm, "Still didn't catch a valid number — want to see the list again?",
                [("show_list_again", "Show list again"), ("start_new_chat", "Start over"),
                 ("human_help", "Talk to a human")])
            return
        print(f"[wa v7] {frm} gave up picking an option after {attempts} tries", flush=True)
        with _WA_SESSIONS_LOCK:
            _WA_SESSIONS.pop(frm, None)
        wa_send(frm, "No worries — let's start fresh.")
        _PENDING_PATH.pop(frm, None)
        _send_onboarding_choice(frm)
        return

    picked = options[idx]
    packet = session["packet"]
    resolution = dict(session.get("resolution") or {})
    # Owner policy: the OTA price is for ONE specific room. A "% better rate" /
    # "couldn't beat" line is only honest when the guest picks THAT room (the
    # matched one, or the nearest match of an ambiguous list); any other room
    # is shown as a plain rate with no comparison.
    ref = resolution.get("reference_room_type_id") \
        or (resolution.get("room_options") or {}).get("nearest_match_room_type_id")
    resolution["reference_room_type_id"] = ref
    resolution["compare_ok"] = bool(ref) and picked.get("room_type_id") == ref
    resolution.pop("room_options", None)
    resolution["room_map"] = {
        "matched": True, "rate_options": [picked],
        "ratekey_option_ids": [picked.get("option_id")],
    }
    with _WA_SESSIONS_LOCK:
        _WA_SESSIONS.pop(frm, None)
    print(f"[wa v7] {frm} picked option {idx + 1} of {len(options)} ({picked.get('option_id')})", flush=True)
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
        print(f"[wa v7] {frm} confirmed, lead {ref} (referred_by={referred_by})", flush=True)
        return

    if is_decline:
        referred_by = _PENDING_REFERRALS.pop(frm, None)
        ref = record_lead(frm, "declined", session["packet"], session.get("resolution"),
                           referred_by=referred_by)
        with _WA_SESSIONS_LOCK:
            _WA_SESSIONS.pop(frm, None)
        wa_send(frm, "Of course — whenever you're ready with the next one, I'm here to help.")
        print(f"[wa v7] {frm} declined, lead {ref} (referred_by={referred_by})", flush=True)
        return

    if button_id == "explore_other_rooms":
        # Live feedback 2026-09-30: a customer who likes the HOTEL but not
        # this specific room shouldn't have to decline and start over --
        # shows every room with a live rate, the one just presented
        # INCLUDED (unlike _handle_not_cheaper's "see_other_rooms", which
        # deliberately excludes the room already ruled out as not
        # cheaper -- here nothing has been ruled out, so nothing is
        # excluded). No new supplier call -- reuses the same pricing
        # response already cached on the session.
        packet = session["packet"]
        resolution = session.get("resolution") or {}
        options = (resolution.get("detail") or {}).get("options") or []
        if not options:
            wa_send_buttons(
                frm, "That's actually the only option I have a live rate for at this "
                     "hotel right now.",
                [("confirm_book", "Yes, book this"), ("decline_book", "Not now")])
            return
        from yta.roommap import list_cheapest_rooms
        rgr = list_cheapest_rooms(options)
        new_resolution = dict(resolution)
        new_resolution["reference_room_type_id"] = (resolution.get("reference_room_type_id")
                                                    or (resolution.get("room_map") or {}).get("room_type_id"))
        new_resolution["room_options"] = rgr.to_dict()
        new_resolution.pop("room_map", None)
        _present_option_choices(frm, packet, new_resolution)
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
            print(f"[wa v7] {frm} mentioned referral {referral_match.group(0).upper()}", flush=True)

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

        # Two more global intents (findings 4/9 and 8) -- checked here,
        # alongside cancel, so they interrupt ANY state, independent of
        # whatever buttons happen to be on the current message. Neither
        # wipes the session: "help" just re-explains and lets the
        # customer continue wherever they were; "talk to a human" flags
        # the lead and leaves the conversation exactly as it was, since
        # tapping/typing it isn't a request to start over.
        if button_id == "human_help" or (button_id is None
                and _human_help_match(text, url, bool(referral_match))):
            _handle_human_help(frm, session)
            return

        if button_id is None and _help_match(text, url, bool(referral_match)):
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
                wa_send_buttons(frm, _closing_question(session.get("missing", [])),
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

        if (button_id == "show_anyway" and session is not None
                and session.get("state") == "awaiting_field"):
            _handle_show_anyway(frm, session)
            return

        if button_id in ("await_type_details", "await_send_screenshot"):
            # Item 17's strike-2 alternatives for awaiting_field -- both
            # are pure nudges (re-send the same open question), not a new
            # answer to consume, so neither counts as a strike either
            # way and the session's attempt count is left untouched.
            # Finding 12: the two used to send the IDENTICAL question --
            # a tap that visibly does nothing different from its label
            # erodes trust. "Send screenshot" now names what a good one
            # looks like; "Type details" just repeats the open question,
            # since that's literally what typing details means.
            if session is not None and session.get("state") == "awaiting_field":
                with _WA_SESSIONS_LOCK:
                    session["last_activity"] = time.time()
                    _WA_SESSIONS[frm] = session
                question = _closing_question(session.get("missing", []))
                if button_id == "await_send_screenshot":
                    question += " A screenshot of the confirmation or checkout page works great."
                wa_send_buttons(frm, question, [("start_new_chat", "Start over")])
            else:
                _send_onboarding_choice(frm)
            return

        if cancel_kind == "partial":
            # Starts with a cancel phrase but clearly carries more
            # content past it (e.g. "nevermind, the second one") -- an
            # outright reset risks wiping a perfectly good session over
            # a phrase that wasn't actually meant as one. Ask, don't
            # assume.
            wa_send_buttons(frm, "Want to start over?",
                                   [("confirm_cancel", "Yes, start over"), ("cancel_continue", "No, continue")])
            return

        if button_id == "have_deal":
            _PENDING_PATH[frm] = "deal"
            # Finding 6: was a bare wa_send with nothing to tap -- now
            # carries "Start over" like every other message in the flow.
            wa_send_buttons(frm, _ONBOARDING_TEXT, [("start_new_chat", "Start over")])
            return

        if button_id == "search_hotel":
            _PENDING_PATH[frm] = "search"
            wa_send_buttons(frm, _SEARCH_TEXT, [("start_new_chat", "Start over")])
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
                print(f"[wa v7] {frm} sent a new link mid-conversation — dropping the old session", flush=True)
                with _WA_SESSIONS_LOCK:
                    _WA_SESSIONS.pop(frm, None)
            media = _download_media(items) if has_media else None
            _run_extraction(frm, url, media, intent=_PENDING_PATH.pop(frm, None))
            return

        if session is None and (button_id in _ORPHAN_BUTTON_IDS
                                or (button_id or "").startswith("opt-")):
            # A tap on a button from a conversation we no longer hold (a
            # restart/deploy wipes sessions) used to fall through to the
            # generic onboarding greeting -- confusing right after "Yes,
            # book this".
            wa_send_buttons(
                frm, "That offer has expired — I've refreshed in the meantime. Send me the "
                     "link or screenshot again and I'll recheck it right away.",
                [("have_deal", _BTN_DEAL), ("search_hotel", _BTN_SEARCH)])
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

        if session.get("state") == "confirming_hotel":
            _handle_confirming_hotel(frm, session, items, button_id)
            return

        if session.get("state") == "awaiting_year":
            _handle_awaiting_year(frm, session, items, button_id)
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
        print(f"[wa v7] ERROR handling batch: {type(e).__name__}: {e}\n{traceback.format_exc()}",
              flush=True)
        if allow_retry:
            # Session deliberately left exactly as it was -- a broken
            # reply should never cost the customer whatever they'd
            # already told the bot. Never surface the exception itself.
            wa_send_buttons(
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
        print(f"[wa v7] session for {frm} abandoned (idle > {_SESSION_MAX_AGE_SEC}s) — clearing it "
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
