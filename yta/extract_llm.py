"""LLM extraction layer — one path for every OTA.

Input: the booking URL (+ its query params) and, when available, the page
content (rendered text + JSON-LD + captured API responses, or pasted
text/HTML, or uploaded screenshots / PDF).

Output: a dict of packet fields, human-readable NAMES prioritised, plus a
per-room `occupancy` array. The model parses the OTA URL params itself
(date formats, occupancy encodings) and cross-checks against the page.

Provider/model come from `yta.llm` (Groq for text, Gemini for images/PDF).
"""
from __future__ import annotations

import json
import re

from yta import llm

SYSTEM = """You read a hotel booking / review page from an Online Travel Agency
— it may be given as rendered page text, or as screenshot image(s) or a PDF
of the page — and return a single JSON object describing the exact stay the
user is about to book.

MANDATORY FIELDS — the request is a FAILURE unless every one of these is
found. Search BOTH the URL parameters and the page hard for each:
  1. hotel.name                    — the real hotel display name
  2. stay.check_in, stay.check_out — the exact stay dates
  3. stay.rooms                    — number of rooms
  4. stay.occupancy                — the per-room adults/children/ages breakdown
  5. requested_offer.room_name     — the selected room type
  6. room detail — ANY of requested_offer.description / bed_type / view
     (whatever the page shows about the room; don't force a prose blurb)
  7. ota_benchmark.final_payable   — the all-in total price
Only use null when the content genuinely does not contain the value — never
invent one — but treat a null in these as "extraction failed", not "fine".

Return ONLY this JSON shape:

{
  "hotel": {
    "name": string|null,            // the hotel's real display name, e.g. "Aloha on the Ganges"
    "address": string|null,         // full street / area address as shown
    "city": string|null,            // just the city
    "country": string|null,         // just the country (e.g. "India") — infer from the address/domain if not labelled
    "star_rating": number|null,     // 1-5 if shown
    "lat": number|null,             // latitude, from a URL param or the page/map
    "lng": number|null              // longitude
  },
  "stay": {
    "check_in": "YYYY-MM-DD"|null,
    "check_out": "YYYY-MM-DD"|null,
    "rooms": integer|null,               // total number of rooms
    "adults": integer|null,              // total adults across all rooms
    "children": integer|null,            // total children across all rooms
    "occupancy": [                       // ONE entry per room, in the page's order
      {"adults": integer, "children": integer, "child_ages": [integer]}
    ] | null                            // null if the page never splits guests by room
  },
  "requested_offer": {
    "room_name": string|null,       // the selected room type, e.g. "One Bedroom Standard Apartment (Garden Facing)"
    "description": string|null,     // short room description if present
    "bed_type": string|null,
    "view": string|null,
    "meal_plan": string|null,       // e.g. "Breakfast included", "Room only", "Half board"
    "cancellation": string|null,    // the policy TEXT as shown, e.g. "Free cancellation until 5 Sep 2026", "Non-refundable"
    "refundable": true|false|null,   // derive from the cancellation text: any "free cancellation" / "fully refundable" => true; "non-refundable" / "no refund" / "cannot be cancelled" => false; unclear => null
    "payment_terms": string|null    // e.g. "Pay now", "Pay at the property", "Book now, pay later"
  },
  "ota_benchmark": {
    "subtotal": number|null,
    "taxes": number|null,
    "fees": number|null,
    "discount": number|null,
    "final_payable": number|null,   // the ALL-IN total the guest pays
    "currency": string|null         // ISO code: INR, USD, EUR, ...
  },
  "field_confidence": { "<dotted.path>": 0.0-1.0 },   // for every non-null field above
  "contradicts_url": [ "<dotted.path>: url said X, page shows Y", ... ]
}

You are also given the BOOKING URL and its query parameters. OTA URLs encode
a lot of the booking directly — use them, and cross-check against the page
when the page is readable.

The page content may include a "CAPTURED API DATA" block — booking-relevant
fields pulled from the JSON the OTA's own page fetched. OTAs keep the
per-room guest split, child ages, full price break-up and cancellation rules
behind a "Details" / "Guest information" / "Show more" click; that data
still shows up here. Read it. It is authoritative — prefer it over anything
you'd have to infer. Per-room guest strings look like
`roomGuests[0].adultString = "2 adults"`, `childrenString = "1 Child"`,
`childrenAgesString = "3 yrs, 2 yrs"` (parse "3 yrs, 2 yrs" -> child_ages
[3, 2]) — map each room to one `occupancy` entry, in order.

READING OTA URL PARAMETERS
- Dates: `checkin` / `checkout` may be ISO (2026-09-21) or 8 digits.
  8-digit MakeMyTrip dates are MMDDYYYY (09032026 -> 2026-09-03). Booking.com
  uses ISO. If a page is also given and shows dates, the page wins.
- Occupancy — report what you can see; a downstream resolver decodes the
  URL encodings, so your job is to read the PAGE and set stay.rooms /
  stay.adults / stay.children and, when the page/dialog/[req] payload
  actually splits guests by room, the per-room `occupancy` array.
  Occupancy encodings you may still recognise directly: a per-room letter
  list ("A,A,7" = 2 adults + 1 child aged 7), a flat number stream repeated
  per room (adults, children, <one age each>), a `k:v` token blob (`o`/`occ`
  = adults, `p`/`ch` = children), or plain `adults=`/`children=`/`rooms=`
  params. If only a total is shown ("2 rooms, 4 adults, 2 children") set the
  aggregates and leave `occupancy` [].
- Agoda `/book/`: `roomName` = room name; `isBreakfastIncluded=false` ->
  "Room only"; `isEasyCancel=false` -> "Non-refundable"; `roomToken` `sai:`
  -> final_payable, `rcy:` -> currency; `h:<id>` is the Agoda hotel id (NOT
  the name). These URLs carry NO dates and NO hotel name — those need the
  page.
- Price: Booking.com `rt_selected_total_price`; Agoda `roomToken` `sai:`.
  These are numbers only.
- `_uCurrency` / `roomToken` `rcy:` give the currency.
- Coordinates: look hard for them in the URL and the page. Param names vary —
  `lat`/`latitude` + `lng`/`lon`/`long`/`longitude`, or a single `ll` / `geo` /
  `latlng` / `location` / `center` param holding "lat,lng", or an `@lat,lng` in
  a maps link. Latitude is -90..90, longitude -180..180. Put them in
  hotel.lat / hotel.lng.

RULES
- Use ONLY facts visible in the provided content or encoded in the URL.
  Never guess, never fill in typical/default values. Unknown => null.
- Names must be the real human-readable names shown on the page. Do NOT
  output internal IDs, slugs, or codes as names.
- hotel.address must be the COMPLETE address exactly as printed — every
  part: building/street, area, city, state/region, country and postcode.
  Do NOT keep only the first line or the neighbourhood. Join multi-line
  addresses with ", ". Then also fill hotel.city and hotel.country from it.
- Money: numbers only (no currency symbols, no thousands separators).
  final_payable is the final all-in amount, not a per-night or pre-tax figure.
- Dates as YYYY-MM-DD. If the page shows only a weekday/day-month, combine
  with the year in context; if you cannot be sure of the year, use null.
- occupancy: for a multi-room booking give the per-room split exactly as
  shown ("Room 1: 2 adults", "Room 2: 2 adults, 1 child age 3"), including
  from a collapsed "Guest information" panel or the CAPTURED API DATA block.
  child_ages lists one age per child when stated ("3 yrs, 2 yrs" -> [3, 2]),
  else []. Only when NO per-room split exists anywhere (page, dialog, API,
  URL) — just an aggregate like "2 rooms, 4 adults, 2 children" — leave
  `occupancy` as [] and fill stay.rooms / stay.adults / stay.children; do
  NOT split it yourself.
- If the page shows a different value than the URL_HINTS below, still
  report what the PAGE shows and add an entry to "contradicts_url".
- Output ONLY the JSON object. No markdown, no commentary."""

_FIELDS = [
    "hotel.name", "hotel.address", "hotel.city", "hotel.country",
    "hotel.star_rating", "hotel.lat", "hotel.lng",
    "stay.check_in", "stay.check_out", "stay.rooms", "stay.adults", "stay.children",
    "stay.occupancy",
    "requested_offer.room_name", "requested_offer.description",
    "requested_offer.bed_type", "requested_offer.view",
    "requested_offer.meal_plan", "requested_offer.cancellation",
    "requested_offer.refundable", "requested_offer.payment_terms",
    "ota_benchmark.subtotal", "ota_benchmark.taxes", "ota_benchmark.fees",
    "ota_benchmark.discount", "ota_benchmark.final_payable", "ota_benchmark.currency",
]


def _dig(d: dict, path: str):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


# Human-friendly prompts for the mandatory fields — used only by
# extract_clarification() below, when a customer (WhatsApp flow) needs to
# be asked for whatever the link/screenshot itself didn't contain.
FIELD_QUESTIONS = {
    "hotel.name": "the hotel's name",
    "stay.check_in": "the check-in date",
    "stay.check_out": "the check-out date",
    "stay.rooms": "the number of rooms",
    "stay.occupancy": "adults/children (and ages) per room",
    "requested_offer.room_name": "the room type you booked",
    "ota_benchmark.final_payable": "the total price shown on the page",
}

# Short, labeled form the WhatsApp flow shows the customer when asking for
# whatever's missing — ALWAYS rendered one per line (never comma-joined
# into a sentence: that reads fine for 1-2 fields and turns into an
# unreadable run-on the moment 4-5 are missing at once). Emoji + Title
# Case rather than ALL CAPS, which reads as shouting once several are
# stacked; occupancy keeps its example since it's the one field people
# naturally phrase inconsistently.
FIELD_LABELS = {
    "hotel.name": "🏨 Hotel Name",
    "stay.check_in": "📅 Check-in Date",
    "stay.check_out": "📅 Check-out Date",
    "stay.rooms": "🚪 Number of Rooms",
    "stay.occupancy": '👥 Occupancy (e.g. "1 Room 2 Adults, 1 Room 1 Adult 1 Child")',
    "requested_offer.room_name": "🛏️ Room Name",
    "ota_benchmark.final_payable": "💳 Total Price Shown on the Page",
}


def extract_clarification(missing_paths: list, reply_text: str,
                           media: list | None = None,
                           provider: str | None = None) -> tuple:
    """A small, targeted follow-up call — NOT the full page extraction.
    Given exactly the mandatory fields still missing and a customer's
    reply so far (text and/or a photo — see the caller for why the TEXT
    should be the FULL accumulated conversation, not just the latest
    message), fill in whichever fields the reply actually answers. Used
    by the WhatsApp flow when a link/screenshot didn't have everything
    mandatory. `media` matters: a customer asked for a missing price
    will often just reply with ANOTHER screenshot showing it, not type
    it out — without vision here that reply was silently dropped and the
    bot just asked the same question again forever.

    Returns (fields: dict, clarify: str | None). `fields` never guesses —
    a field stays absent rather than invented. `clarify` is a short,
    SPECIFIC follow-up question the model raises only when a reply was
    partial/ambiguous for one of these fields (a date with no year, an
    occupancy with no children count, etc.) — lets the caller ask exactly
    what's missing instead of repeating the whole field list verbatim,
    which reads as the bot having ignored what was already said."""
    wanted = [p for p in missing_paths if p in FIELD_QUESTIONS]
    if not wanted or not ((reply_text or "").strip() or media):
        return {}, None
    system = (
        "A hotel-booking assistant is missing a few details and asked the "
        "customer for them. Their reply may be plain text, a photo (e.g. "
        "a screenshot of a price or booking page), or both. Extract ONLY "
        "the fields listed below from whatever they gave you. Use null "
        "for anything not actually answered — never guess or invent a "
        "value (e.g. a date with no year given stays null, don't assume "
        "a year). Reply with JSON only.\n\n"
        "Fields:\n" + "\n".join(f"  {p} — {FIELD_QUESTIONS[p]}" for p in wanted) +
        "\n\nReturn a FLAT JSON object. Each field above must be a top-level "
        "key using its EXACT dotted string as written, e.g. the literal key "
        f"{json.dumps(wanted[0])} — do NOT nest by splitting on the dot "
        "(that means NOT {\"" + wanted[0].split(".")[0] + "\": {\"" +
        wanted[0].split(".", 1)[1] + "\": ...}}).\n\n"
        "Shape notes: dates as YYYY-MM-DD; stay.occupancy as an array of "
        "{adults, children, child_ages} objects, one per room; stay.rooms as "
        "a plain integer; ota_benchmark.final_payable as a plain number, no "
        "currency symbol or thousands separators.\n\n"
        "Also include a \"clarify\" key: a short, specific one-sentence "
        "question, referencing what they already said, ONLY if the reply "
        "gave PARTIAL or ambiguous info for one of these fields that you "
        "could not fully resolve (e.g. they said \"24 Sept to 25 Sept\" "
        "with no year -> ask which year; they said \"me and my wife\" for "
        "occupancy with an unclear room count -> ask that). Use null for "
        "\"clarify\" if the reply either fully answered a field or didn't "
        "address it at all — don't invent a question otherwise."
    )
    user = f"Customer's reply so far (may span more than one message): {reply_text!r}"
    if media:
        user += "\n\nA photo from the customer is attached below — read it too."
    try:
        # gpt-oss-120b (Groq's default text model) spends part of its token
        # budget on internal reasoning before the actual JSON — a tight
        # budget here truncates before valid JSON is produced and Groq's
        # json_object mode rejects it outright. 700 is comfortably above
        # what this small a task needs even with that overhead. `media`
        # forces the vision provider chain the same way the main
        # extraction does (yta.llm.complete picks it automatically).
        raw, _, _ = llm.complete(system, user, max_tokens=700, media=media, provider=provider)
    except Exception:
        return {}, None
    raw = re.sub(r"^```(?:json)?|```$", "", (raw or "").strip(), flags=re.MULTILINE).strip()
    try:
        data = json.loads(raw)
    except Exception:
        return {}, None
    if not isinstance(data, dict):
        return {}, None
    # The prompt asks for flat dotted keys, but vision models sometimes nest
    # by the dot anyway (e.g. {"ota_benchmark": {"final_payable": 31683}}
    # instead of {"ota_benchmark.final_payable": 31683}) despite being told
    # not to — flatten defensively so a field isn't silently lost to that.
    def _get(d: dict, path: str):
        if path in d and d[path] is not None:
            return d[path]
        cur = d
        for part in path.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return None
            cur = cur[part]
        return cur
    fields = {p: v for p in wanted if (v := _get(data, p)) is not None}
    clarify = data.get("clarify")
    clarify = clarify.strip() if isinstance(clarify, str) and clarify.strip() else None
    return fields, clarify


class LLMExtractionResult:
    def __init__(self, fields: dict, confidence: dict, contradictions: list,
                 provider: str, model: str, raw: str = ""):
        self.fields = fields                # {dotted_path: value}
        self.confidence = confidence        # {dotted_path: float}
        self.contradictions = contradictions
        self.provider = provider
        self.model = model
        self.raw = raw


def extract(context: str | None = None, url: str = "",
            media: list | None = None,
            provider: str | None = None) -> LLMExtractionResult:
    from urllib.parse import urlparse, parse_qs

    url_block = ""
    if url:
        q = parse_qs(urlparse(url).query, keep_blank_values=True)
        params = "\n".join(f"  {k} = {v[0]!r}" for k, v in q.items() if v and v[0])
        url_block = (f"=== BOOKING URL ===\n{url}\n\nquery parameters:\n"
                     f"{params or '  (none)'}\n\n")

    if media:
        user = (url_block + "=== PAGE CONTENT ===\nThe page is attached as "
                "image(s) / a PDF below. Read every visible field.")
        if context:
            user += f"\n\nAdditional text context:\n{context}"
    else:
        user = url_block + f"=== PAGE CONTENT ===\n{context or '(none — rely on the URL)'}"

    raw, provider, model = llm.complete(SYSTEM, user, max_tokens=1800,
                                        media=media, provider=provider)

    raw = re.sub(r"^```(?:json)?|```$", "", (raw or "").strip(), flags=re.MULTILINE).strip()
    data = json.loads(raw)   # let a bad parse raise — caller handles

    conf_in = data.get("field_confidence", {}) or {}
    fields, confidence = {}, {}
    for path in _FIELDS:
        val = _dig(data, path)
        if val is None or (isinstance(val, str) and not val.strip()):
            continue
        fields[path] = val
        c = conf_in.get(path)
        confidence[path] = float(c) if isinstance(c, (int, float)) else 0.6

    return LLMExtractionResult(
        fields=fields, confidence=confidence,
        contradictions=list(data.get("contradicts_url", []) or []),
        provider=provider, model=model, raw=raw,
    )
