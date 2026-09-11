"""Structured-field resolver — pull the well-shaped booking fields straight
out of captured JSON (network request/response bodies, embedded SPA state,
JSON-LD) before ever calling the LLM.

Same design as occupancy.py: match a field by the MEANING of its key name
(a regex family), not by OTA brand, and only accept values whose shape is
unambiguous. This matters most for the Chrome extension path — its
xhr_json is real, authenticated, structured API traffic (an app talking
JSON to its own backend almost always uses clean keys like `checkIn`,
`totalPrice`, `currency`, `hotelName`), so a meaningful slice of what used
to need an LLM round-trip can be read directly, and the LLM call can be
skipped entirely when nothing mandatory is left to find. The Playwright
render() path produces the same xhr_json shape, so it benefits too.

Deliberately conservative: every value shape accepted here is unambiguous
(ISO dates, epoch millis, a real ISO-4217 code, a positive number). Messy
locale-formatted dates, code-like room names, and free-text room
descriptions are left to the LLM — guessing there would be worse than not
trying.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

# -- key-name families (meaning, not brand) -------------------------

_CHECKIN_KEYS = re.compile(
    r"(?i)^(check[\s_-]?in(date)?|arrival[\s_-]?date|start[\s_-]?date|"
    r"from[\s_-]?date|stay[\s_-]?from|date[\s_-]?from)$")
_CHECKOUT_KEYS = re.compile(
    r"(?i)^(check[\s_-]?out(date)?|departure[\s_-]?date|end[\s_-]?date|"
    r"to[\s_-]?date|stay[\s_-]?to|date[\s_-]?to)$")
_CURRENCY_KEYS = re.compile(r"(?i)^(currency|curr|ccy|currencycode)$")
_PRICE_KEYS = re.compile(
    r"(?i)^(total[\s_-]?(price|amount|payable|fare|cost)|grand[\s_-]?total|"
    r"final[\s_-]?(price|payable|amount)|amount[\s_-]?payable|"
    r"payable[\s_-]?amount|total[\s_-]?charge)$")
_HOTEL_NAME_KEYS = re.compile(
    r"(?i)^(hotel[\s_-]?name|property[\s_-]?name|accommodation[\s_-]?name)$")
_ROOM_NAME_KEYS = re.compile(
    r"(?i)^(room[\s_-]?name|room[\s_-]?type[\s_-]?name|selected[\s_-]?room)$")
_VIEW_KEYS = re.compile(r"(?i)^(view|room[\s_-]?view)$")
_BED_KEYS = re.compile(r"(?i)^(bed[\s_-]?type|bed[\s_-]?description)$")

_ISO4217 = frozenset("""
    INR USD EUR GBP AED SAR QAR OMR KWD BHD SGD THB MYR IDR PHP VND
    KRW JPY CNY HKD AUD CAD NZD CHF ZAR TRY EGP LKR NPR BDT MMK KHR
    LAK MVR SEK NOK DKK PLN CZK HUF RON BGN HRK RUB BRL MXN ARS CLP
""".split())

_ISO_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[T ].*)?$")
_SLUG_LIKE = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")   # "grand-hyatt-2" etc
_ID_LIKE = re.compile(r"^[0-9a-f]{8,}$|^\d+$", re.I)         # hex/uuid/plain numeric


@dataclass
class StructuredField:
    path: str
    value: object
    confidence: float
    pointer: str


def _walk(node, path, out: list):
    if isinstance(node, dict):
        for k, v in node.items():
            kp = f"{path}.{k}" if path else str(k)
            if isinstance(v, (dict, list)):
                _walk(v, kp, out)
            else:
                out.append((k, v, kp))
    elif isinstance(node, list):
        for i, v in enumerate(node[:80]):
            _walk(v, f"{path}[{i}]", out)


def _leaves(blobs: list, wrapped: bool) -> list:
    """`wrapped=True` for xhr_json entries ({"url","kind","body"}); `False`
    for json_ld entries, which are the raw JSON-LD object itself."""
    out: list = []
    for b in blobs or []:
        body = b.get("body") if (wrapped and isinstance(b, dict)) else b
        _walk(body, "", out)
    return out


def _to_iso_date(v) -> str | None:
    if isinstance(v, str):
        m = _ISO_DATE.match(v.strip())
        if m:
            return m.group(1)
        return None
    if isinstance(v, (int, float)) and v > 10**11:            # epoch millis
        try:
            return datetime.fromtimestamp(v / 1000, tz=timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(v, (int, float)) and 10**8 < v <= 10**10:   # epoch seconds
        try:
            return datetime.fromtimestamp(v, tz=timezone.utc).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _looks_like_a_name(v) -> bool:
    if not isinstance(v, str):
        return False
    s = v.strip()
    if not (2 <= len(s) <= 120):
        return False
    if _ID_LIKE.match(s):
        return False
    if _SLUG_LIKE.match(s) and " " not in s:      # all-lowercase-hyphenated -> a slug
        return False
    return True


def extract_structured(xhr_json: list, json_ld: list | None = None) -> dict:
    """-> {dotted_path: StructuredField}. Only the fields whose value shape
    is unambiguous get returned; everything else is left for the LLM."""
    found: dict[str, StructuredField] = {}
    leaves = _leaves(xhr_json, wrapped=True) + _leaves(json_ld or [], wrapped=False)

    def take(path: str, field: StructuredField, better=lambda a, b: b.confidence > a.confidence):
        cur = found.get(path)
        if cur is None or better(cur, field):
            found[path] = field

    for k, v, kp in leaves:
        if v in (None, "", "null"):
            continue
        short_kp = ".".join(kp.split(".")[-3:])

        if "stay.check_in" not in found and _CHECKIN_KEYS.match(str(k)):
            iso = _to_iso_date(v)
            if iso:
                take("stay.check_in", StructuredField("stay.check_in", iso, 0.9, short_kp))
        if "stay.check_out" not in found and _CHECKOUT_KEYS.match(str(k)):
            iso = _to_iso_date(v)
            if iso:
                take("stay.check_out", StructuredField("stay.check_out", iso, 0.9, short_kp))

        if _CURRENCY_KEYS.match(str(k)) and isinstance(v, str) and v.upper() in _ISO4217:
            take("ota_benchmark.currency",
                 StructuredField("ota_benchmark.currency", v.upper(), 0.9, short_kp))

        if _PRICE_KEYS.match(str(k)) and isinstance(v, (int, float)) and v > 0:
            take("ota_benchmark.final_payable",
                 StructuredField("ota_benchmark.final_payable", float(v), 0.75, short_kp),
                 better=lambda a, b: b.value > a.value)   # prefer the largest "total"-ish figure

        if _HOTEL_NAME_KEYS.match(str(k)) and _looks_like_a_name(v):
            take("hotel.name", StructuredField("hotel.name", v.strip(), 0.75, short_kp))

        if _ROOM_NAME_KEYS.match(str(k)) and _looks_like_a_name(v):
            take("requested_offer.room_name",
                 StructuredField("requested_offer.room_name", v.strip(), 0.6, short_kp))

        if _VIEW_KEYS.match(str(k)) and isinstance(v, str) and 2 <= len(v.strip()) <= 60:
            take("requested_offer.view",
                 StructuredField("requested_offer.view", v.strip(), 0.6, short_kp))

        if _BED_KEYS.match(str(k)) and isinstance(v, str) and 2 <= len(v.strip()) <= 60:
            take("requested_offer.bed_type",
                 StructuredField("requested_offer.bed_type", v.strip(), 0.6, short_kp))

    return found
