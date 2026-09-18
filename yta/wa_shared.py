"""Shared helpers for every WhatsApp conversation flow (yta/wa_flows/*).

Nothing here decides WHEN to send what — that's each flow's job. This
module only holds the pieces every flow needs regardless of its own
routing logic: the one place a message actually goes out over WhatsApp,
and the formatting for the two messages every flow eventually sends
once it has a packet (the "here's what's missing" ask and the final
deal). Moved out of yta/web.py unchanged so v0 and v1 (and any flow
after that) share one implementation instead of drifting apart.

The functions below CHECKING_PHRASES/recap_block/deal_message/etc. are a
SEPARATE, newer design -- the "concierge voice + structured fact block"
terminal message v2 built independently of whatsapp_reply() above (see
deal_message()'s own docstring). v2, v3, and v4 each carried their own
identical copy of this forward as flows were forked, which is exactly
the drift risk this module exists to prevent -- moved here 2026-09-18 so
v3/v4 (and any flow after them) share one implementation instead of
three private copies going out of sync the next time this design
changes. v2.py's own copy is untouched (v2 stays frozen, same as v0/v1).
"""
from __future__ import annotations

import os
import random


def wa_send(frm: str, text: str) -> dict:
    """Every outbound WhatsApp send goes through here — logs the actual
    result. A bare whatsapp.send_text() call can fail silently (expired
    token, rate limit, bad recipient) and the code carries on as if the
    customer got the message when they got nothing at all — this is the
    one and only place that matters, so fix it once here rather than
    re-checking the result at every call site."""
    from yta import whatsapp
    result = whatsapp.send_text(frm, text)
    if result.get("_status_code") != 200:
        print(f"[wa] SEND FAILED to {frm}: status={result.get('_status_code')} "
              f"error={result.get('error')}", flush=True)
    return result


def occ_field(r, name, default=None):
    """`stay.occupancy` holds real RoomOccupancy objects on a live packet,
    but plain dicts once something's gone through .to_dict()/JSON — accept
    either shape rather than assuming one."""
    if r is None:
        return default
    if isinstance(r, dict):
        return r.get(name, default)
    return getattr(r, name, default)


def occ_repr(occ) -> str:
    if not occ:
        return "—"
    parts = []
    for r in occ:
        a = occ_field(r, "adults", "?")
        c = occ_field(r, "children", 0) or 0
        bit = f"{a} adult{'s' if a != 1 else ''}"
        if c:
            bit += f" + {c} child{'ren' if c != 1 else ''}"
        parts.append(bit)
    return "; ".join(parts)


def extracted_lines(packet) -> list:
    """The hotel/dates/occupancy/room/price facts as extracted so far —
    shared by the "here's what I found" message and the final reply.
    Emoji + text label together — the emoji alone ("🏨 Taj Exotica...")
    takes a beat to parse as "this is the hotel"; the label makes it
    explicit while the emoji keeps each line quick to scan."""
    p = packet
    lines = [
        f"🏨 Hotel Name : {p.hotel.name or '—'}",
        f"📅 Dates : {p.stay.check_in or '?'} → {p.stay.check_out or '?'}",
        f"👥 Room Occupancy : {occ_repr(p.stay.occupancy)}",
    ]
    if p.requested_offer.room_name:
        lines.append(f"🛏️ Room Type : {p.requested_offer.room_name}")
    if p.requested_offer.meal_plan:
        lines.append(f"🍽️ Meal Type : {p.requested_offer.meal_plan}")
    if p.requested_offer.refundable is True:
        lines.append("↩️ Refundability : Refundable")
    elif p.requested_offer.refundable is False:
        lines.append("↩️ Refundability : Non-refundable")
    if p.ota_benchmark.final_payable:
        lines.append(f"💳 Price Shown : {p.ota_benchmark.currency or ''} {p.ota_benchmark.final_payable}")
    return lines


def ask_for_missing(frm: str, missing: list, clarify: str | None = None) -> None:
    from yta.extract_llm import FIELD_LABELS
    labels = [FIELD_LABELS[p] for p in missing if p in FIELD_LABELS]
    if not labels:
        return
    # `clarify` is a specific follow-up (e.g. "which year — you said 24
    # Sept to 25 Sept?") from extract_clarification() when a reply was
    # partial rather than absent — that's already a direct, on-point
    # question, so send it alone rather than also re-listing the field(s)
    # it's about. Otherwise, ALWAYS one field per line — comma-joining
    # into a sentence reads fine for 1-2 missing fields and turns into an
    # unreadable run-on the moment 4-5 are missing at once.
    if clarify:
        text = f"{clarify} (or send a new link/photo to start over)"
    else:
        text = ("Missing:\n" + "\n".join(labels)
                + "\n\nReply with these — any format works, or send a new link/photo to start over.")
    wa_send(frm, text)


def whatsapp_reply(packet, resolution: dict | None) -> str:
    # Deliberately does NOT repeat hotel/dates/occupancy/room/OTA-price —
    # the "📋 Here's what I found" message already showed all of that a
    # moment ago. Repeating it here just made the actual quote harder to
    # read. This message carries only what's NEW: the TripJack quote.
    ota_price = packet.ota_benchmark.final_payable
    ota_ccy = packet.ota_benchmark.currency

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
        # Never surface rz['note']/rz['detail_error'] raw here — those are
        # internal wholesale-supplier diagnostics (can literally say things
        # like "TripJack DB not built") and must never reach a customer.
        # The real reason is still logged server-side for us to act on.
        name = packet.hotel.name or "this hotel"
        reason = (rz.get("note") if rz.get("available") is False
                  else rz.get("detail_error") if rz.get("detail_error")
                  else "no confident room/price match" if (rz.get("available") and rz.get("match"))
                  else "not resolved")
        print(f"[wa] no live rate for {name!r}: {reason}", flush=True)
        return f"⚠️ Couldn't get a live rate for {name} right now — I'll take a manual look and follow up."

    # Same generic markup concept as the extension (yta_markup_pct/flat in
    # its admin page) — no shared per-user config between the two flows
    # yet, so this is its own env-based knob for now.
    pct = float(os.environ.get("WHATSAPP_MARKUP_PCT", "0") or 0)
    flat = float(os.environ.get("WHATSAPP_MARKUP_FLAT", "0") or 0)
    ccy = best.get("currency", "") or ""
    sell = round(best.get("total_price", 0) * (1 + pct / 100) + flat, 2)

    comparable = bool(ota_price and ota_ccy and ota_ccy.upper() == ccy.upper())
    if comparable:
        diff = ota_price - sell
        dpct = (diff / ota_price * 100) if ota_price else 0
        cheaper = diff >= 0
        if not cheaper:
            # Never show a price that's worse than what the customer
            # already has on the OTA page — no upside in surfacing that
            # number, and it undercuts the whole pitch. Say we checked,
            # not what we found.
            return ("👍 We checked — the price you already have looks like "
                     "the best deal for this stay. Nothing better to offer "
                     "right now.")

    lines = []
    # WhatsApp renders *single asterisks* as bold. When we actually know
    # we're cheaper, lead with the win and lay out BookMyStay's own deal
    # in full — same labeled shape as the "Your deal" summary, but sourced
    # from what TripJack actually matched/returned (hotel name, room, meal
    # can each differ in wording from what the OTA page showed) — so the
    # customer sees exactly what they'd be booking, not just a condensed
    # price line.
    if comparable:
        tj_hotel_name = (rz.get("match") or {}).get("hotel_name") or packet.hotel.name or "—"
        lines.append("✅ Found you a better rate!")
        lines.append("")
        lines.append("BookMyStay's deal:")
        lines.append("----")
        lines.append(f"🏨 Hotel Name : {tj_hotel_name}")
        lines.append(f"📅 Dates : {packet.stay.check_in or '?'} → {packet.stay.check_out or '?'}")
        lines.append(f"👥 Room Occupancy : {occ_repr(packet.stay.occupancy)}")
        lines.append(f"🛏️ Room Type : {best.get('room_name') or '—'}")
        if best.get("meal_basis"):
            lines.append(f"🍽️ Meal Type : {best['meal_basis']}")
        if best.get("refundable") is True:
            lines.append("↩️ Refundability : Refundable")
        elif best.get("refundable") is False:
            lines.append("↩️ Refundability : Non-refundable")
        if ota_price:
            lines.append(f"💳 Price Shown In Your Deal : {ota_ccy or ''} {ota_price}")
        lines.append("")
        lines.append(f"*💰 BookMyStay price: {ccy} {sell}*")
        lines.append(f"*📉 {ccy} {abs(diff):.0f} ({abs(dpct):.1f}%) cheaper than your deal*")
    else:
        lines.append(f"💰 BookMyStay price: {ccy} {sell}")
        # Meal plan / refundability come with the matched TripJack option
        # itself — different rooms/rates at the same hotel can differ on
        # both, so this is what's ACTUALLY being quoted, not assumed from
        # the OTA. Bolded specifically (not the room name) since these are
        # the two terms of the deal the customer needs to confirm.
        meta_bits = []
        if best.get("room_name") and best["room_name"] != packet.requested_offer.room_name:
            meta_bits.append(best["room_name"])
        if best.get("meal_basis"):
            meta_bits.append(f"*{best['meal_basis']}*")
        if best.get("refundable") is True:
            meta_bits.append("*refundable*")
        elif best.get("refundable") is False:
            meta_bits.append("*non-refundable*")
        if meta_bits:
            lines.append("🛏️ " + " · ".join(meta_bits))

    lines.append("\nReply to this message to book — we'll confirm and take it from there.")
    return "\n".join(lines)


def finish_and_reply(frm: str, packet) -> None:
    # The TripJack resolve+pricing call below is the one genuinely slow
    # step left with nothing sent back in between — ack it so the wait
    # doesn't read as the bot having gone silent.
    from yta.web import _resolve   # lazy: web.py imports the flows, so this
                                    # avoids a top-level import cycle.
    wa_send(frm, "Fetching the discounted rates for you.")
    resolution = _resolve(packet) if packet.hotel.name else None
    reply = whatsapp_reply(packet, resolution)
    wa_send(frm, reply)
    print(f"[wa] batch for {frm} complete", flush=True)


# -- v2/v3/v4-style deal-reveal design (see module docstring above) -----

# Rotated rather than fixed so the same customer never sees the exact
# same script twice in a row -- one of the concrete things that made the
# old version read as a bot no matter how the individual words changed.
CHECKING_PHRASES = [
    "Let me take a look for you.",
    "One moment, I'll check this now.",
    "Leave this with me for a moment.",
]
FETCHING_PHRASES = [
    "One moment, I'll get you the best rate I can find.",
    "Let me check what I can secure for you.",
    "Give me just a moment to pull the best rate.",
]
FOUND_OPENERS = [
    "Here's what I have for your stay — just need a bit more to compare it properly.",
    "Almost there — just need a little more to compare this properly.",
    "Just about set — one more thing and I can compare this properly.",
]

# A real question a person would ask, not the internal field label recited
# back ("Total Price Shown on the Page") -- see extract_llm.FIELD_LABELS
# for the label form this deliberately avoids using here.
NATURAL_QUESTIONS = {
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
NATURAL_NOUNS = {
    "hotel.name": "the hotel",
    "stay.check_in": "your dates",
    "stay.check_out": "your dates",
    "stay.rooms": "how many guests",
    "stay.occupancy": "how many guests",
    "requested_offer.room_name": "the room type",
    "ota_benchmark.final_payable": "the total price",
}


def short_date(iso_str):
    from datetime import date
    try:
        y, m, d = (int(x) for x in iso_str.split("-"))
        return date(y, m, d).strftime("%b %-d")
    except Exception:
        return iso_str


def clean_room_name(name):
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


def price_comparison_lines(ota_ccy, ota_price, ccy, sell, diff, dpct) -> str:
    """Two failed approaches taught the same lesson: don't try to align
    price labels into columns at all. Hand-padded spaces don't align in
    WhatsApp's proportional font; a ```monospace``` block DOES align, but
    the padding needed to line up "BookMyStay price:" makes the line
    wider than a phone screen, so WhatsApp wraps it mid-value ("INR" on
    one line, the number on the next) -- worse than the original
    misalignment. A "was -> now" line has nothing to align and is short
    enough to never wrap."""
    return (f"~{ota_ccy} {ota_price:,.0f}~ → *{ccy} {sell:,.2f}*\n"
            f"You save *{ccy} {diff:,.0f}* ({dpct:.0f}%)")


def price_line(ccy, sell) -> str:
    return f"BookMyStay price: *{ccy} {sell:,.2f}*"


def recap_block(packet) -> str:
    """The facts a customer would actually want to verify, as a clean
    labeled block -- kept structured on purpose even though the messages
    around it are conversational. See the module docstring: dissolving
    this into prose was tried and rejected as harder to scan.

    One icon per CATEGORY of fact (hotel / dates+guests / room / price),
    never one per individual field -- the middle ground picked after
    trying both zero icons and one on every line. Keep this exact set
    (🏨 📅 🛏️ 💰) and placement in sync with deal_recap_block() and
    deal_message() below -- same visual language everywhere a
    structured fact block appears."""
    lines = []
    if packet.hotel.name:
        lines.append(f"🏨 *{packet.hotel.name}*")
    date_occ = []
    if packet.stay.check_in and packet.stay.check_out:
        date_occ.append(f"{short_date(packet.stay.check_in)} → {short_date(packet.stay.check_out)}")
    if packet.stay.occupancy:
        date_occ.append(occ_repr(packet.stay.occupancy))
    if date_occ:
        lines.append("📅 " + " · ".join(date_occ))
    room_bits = []
    room_name = clean_room_name(packet.requested_offer.room_name)
    if room_name:
        room_bits.append(room_name)
    if packet.requested_offer.meal_plan:
        room_bits.append(packet.requested_offer.meal_plan)
    if packet.requested_offer.refundable is True:
        room_bits.append("Refundable")
    elif packet.requested_offer.refundable is False:
        room_bits.append("Non-refundable")
    if room_bits:
        lines.append("🛏️ " + " · ".join(room_bits))
    if packet.ota_benchmark.final_payable:
        ccy = (packet.ota_benchmark.currency or "").strip()
        lines.append(f"💰 Price shown: {ccy} {packet.ota_benchmark.final_payable:,.0f}".replace("  ", " "))
    return "\n".join(lines)


def closing_question(missing: list, clarify: str | None = None) -> str:
    if clarify:
        return clarify
    if len(missing) == 1:
        return NATURAL_QUESTIONS.get(missing[0], "Could you share a bit more detail?")
    nouns = []
    for m in missing:
        n = NATURAL_NOUNS.get(m)
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


def found_and_ask_message(packet, missing: list, clarify: str | None = None) -> str:
    question = closing_question(missing, clarify)
    recap = recap_block(packet)
    if not recap:
        return f"I wasn't able to pick up much from that screenshot — {question}"
    return f"{random.choice(FOUND_OPENERS)}\n\n{recap}\n\n{question}"


def deal_recap_block(packet, best: dict) -> str:
    """Same shape as recap_block(), but for the actual offer being
    quoted -- room/meal/refundable come from the matched TripJack option
    when available (it can word these slightly differently than what the
    OTA page showed), falling back to the customer's original request
    only where TripJack didn't return its own value. Dates/occupancy
    don't change between what was asked and what's being offered, so
    those still come straight from the packet."""
    lines = []
    if packet.hotel.name:
        lines.append(f"🏨 *{packet.hotel.name}*")
    date_occ = []
    if packet.stay.check_in and packet.stay.check_out:
        date_occ.append(f"{short_date(packet.stay.check_in)} → {short_date(packet.stay.check_out)}")
    if packet.stay.occupancy:
        date_occ.append(occ_repr(packet.stay.occupancy))
    if date_occ:
        lines.append("📅 " + " · ".join(date_occ))
    room_bits = []
    room_name = clean_room_name(best.get("room_name") or packet.requested_offer.room_name)
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
        lines.append("🛏️ " + " · ".join(room_bits))
    return "\n".join(lines)


def deal_message(packet, resolution) -> tuple:
    """Returns (text, matched, bookable, savings_line, confirm_line).
    `matched`: a room/rate was actually found at all. `bookable`: there's
    a genuine offer worth confirming -- matched AND (not directly
    comparable to the OTA price, or it's actually cheaper). A matched
    rate that ISN'T cheaper gets a plain "nothing better to offer" reply
    with no confirm/decline buttons -- there's nothing to confirm --
    mirroring whatsapp_reply()'s own gate above. `savings_line` is a
    short standalone sentence naming the actual amount saved, for the
    referral ask after a confirm (v3/v4 only). `confirm_line` continues
    directly after "Wonderful — " in the confirm message ("I've secured X
    for Y (Z less than what you had)."), so the confirmation itself names
    what was actually booked instead of a bare "I've noted this down."
    Both are only set when there's a real offer; None otherwise
    (bookable-but-not-comparable only sets confirm_line, not
    savings_line -- nothing to compare against; no rate at all sets
    neither)."""
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
                f"Happy to take a look at another property, if you'd like?"), False, False, None, None

    ota_price = packet.ota_benchmark.final_payable
    ota_ccy = packet.ota_benchmark.currency
    pct = float(os.environ.get("WHATSAPP_MARKUP_PCT", "0") or 0)
    flat = float(os.environ.get("WHATSAPP_MARKUP_FLAT", "0") or 0)
    ccy = best.get("currency", "") or ""
    sell = round(best.get("total_price", 0) * (1 + pct / 100) + flat, 2)
    comparable = bool(ota_price and ota_ccy and ota_ccy.upper() == ccy.upper())
    hotel_name = packet.hotel.name or "this hotel"

    if comparable and (ota_price - sell) < 0:
        return ("I checked, but the price you already have looks like the best "
                 "deal for this stay — nothing better to offer right now."), True, False, None, None

    recap = deal_recap_block(packet, best)
    savings_line = None

    lines = ["*Good news — I found you a better rate.*", "", recap, "", "💰"]
    if comparable:
        diff = ota_price - sell
        dpct = (diff / ota_price * 100) if ota_price else 0
        lines.append(price_comparison_lines(ota_ccy, ota_price, ccy, sell, diff, dpct))
        savings_line = f"You just saved {ccy} {diff:,.0f} ({dpct:.0f}%) on this one."
        confirm_line = (f"I've secured {hotel_name} for {ccy} {sell:,.2f} "
                         f"({ccy} {diff:,.0f} less than what you had).")
    else:
        lines[0] = "*Good news — I found you a rate.*"
        lines.append(price_line(ccy, sell))
        confirm_line = f"I've secured {hotel_name} for {ccy} {sell:,.2f}."
    lines.append("")
    lines.append("Shall I go ahead and secure this for you?")
    return "\n".join(lines), True, True, savings_line, confirm_line
