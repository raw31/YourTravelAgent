"""Booking Intent + OTA Offer packet — the single structured output of the
extraction layer (strategy doc §8 / Appendix A, schema_version 1.0).

Design rules carried from the strategy doc:
  - Every field must preserve provenance. "Observed" facts and "inferred"
    values must never be conflated — see the `evidence` list. Adapters set a
    field AND record how they know it via `packet.add(...)`.
  - Adapters never invent plausible defaults; unknown stays `None`.
  - All OTA text is untrusted input — `clean_text()` before storing strings.
"""
from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta
from typing import Optional

SCHEMA_VERSION = "1.0"


def _is_int(v) -> bool:
    try:
        int(v)
        return True
    except (TypeError, ValueError):
        return False

# evidence source kinds
URL = "url"            # read straight from a URL query param
DOM = "dom"            # parsed from fetched page HTML
NETWORK = "network"    # captured from an XHR/fetch JSON response
INFERRED = "inferred"  # computed/derived from other observed fields
LLM = "llm"            # produced by the generic LLM fallback


@dataclass
class Evidence:
    field: str                       # dotted path, e.g. "stay.check_in"
    value: object
    source: str                      # one of URL / DOM / NETWORK / INFERRED / LLM
    confidence: float                # 0.0–1.0
    pointer: Optional[str] = None    # url param name, CSS selector, or JSON path


@dataclass
class Source:
    ota: str
    url: str
    page_type: Optional[str] = None          # hotel_review | checkout | search | unknown
    extraction_method: str = ""              # url_params | url+render+llm:<provider>:<model> | ...
    extracted_at: Optional[str] = None       # ISO 8601 with offset
    rendered: bool = False                   # did the browser render step run
    # OTA-internal ids — best-effort only. The matching engine (§9) keys on
    # NAMES + coordinates, not these; nothing fails if they stay null.
    source_hotel_id: Optional[str] = None
    source_room_id: Optional[str] = None
    source_rate_id: Optional[str] = None


@dataclass
class Hotel:
    name: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    star_rating: Optional[float] = None       # 1-5 if stated on the page
    category: Optional[str] = None            # free-text class if stated


@dataclass
class RoomOccupancy:
    adults: Optional[int] = None
    children: int = 0
    child_ages: list = field(default_factory=list)   # years, one per child when known

    def to_dict(self):
        return asdict(self)


@dataclass
class Stay:
    check_in: Optional[str] = None            # YYYY-MM-DD
    check_out: Optional[str] = None           # YYYY-MM-DD
    nights: Optional[int] = None
    # occupancy is the source of truth for a multi-room booking.
    # rooms / adults / children / child_ages are aggregates kept in sync
    # (and are all that's available when the source doesn't split by room).
    occupancy: list = field(default_factory=list)     # list[RoomOccupancy]
    rooms: Optional[int] = None
    adults: Optional[int] = None
    children: Optional[int] = None
    child_ages: list = field(default_factory=list)

    def set_occupancy(self, rooms_list) -> None:
        """rooms_list: list of RoomOccupancy | dict. Sets per-room occupancy
        and recomputes the aggregates."""
        norm = []
        for r in rooms_list or []:
            if isinstance(r, RoomOccupancy):
                norm.append(r)
            elif isinstance(r, dict):
                ages = [int(a) for a in (r.get("child_ages") or []) if _is_int(a)]
                ch = r.get("children")
                ch = int(ch) if _is_int(ch) else (len(ages) if ages else 0)
                ad = r.get("adults")
                norm.append(RoomOccupancy(
                    adults=int(ad) if _is_int(ad) else None,
                    children=ch, child_ages=ages))
        self.occupancy = norm
        if norm:
            self.rooms = len(norm)
            if all(r.adults is not None for r in norm):
                self.adults = sum(r.adults for r in norm)
            self.children = sum(r.children for r in norm)
            self.child_ages = [a for r in norm for a in r.child_ages]


@dataclass
class Offer:
    room_name: Optional[str] = None
    description: Optional[str] = None
    bed_type: Optional[str] = None
    view: Optional[str] = None
    meal_plan: Optional[str] = None           # "Breakfast included" | "Room only" | ...
    cancellation: Optional[str] = None        # "Non-refundable" | "Free cancellation until ..."
    refundable: Optional[bool] = None
    payment_terms: Optional[str] = None       # "Prepaid" | "Pay at hotel" | ...
    amenities: list = field(default_factory=list)


@dataclass
class Benchmark:
    subtotal: Optional[float] = None
    taxes: Optional[float] = None
    fees: Optional[float] = None
    discount: Optional[float] = None
    final_payable: Optional[float] = None     # total the guest pays, all-in
    currency: Optional[str] = None            # INR / USD / ...
    payment_method: Optional[str] = None


DEFAULT_MATCHING_POLICY = {
    "hotel": "exact",
    "room": "equivalent_allowed",
    "meal": "same_or_better",
    "cancellation": "same_or_better",
    "max_customer_price": None,
}


# Fields that MUST be present for a usable request. A null in any of these
# means the extraction FAILED — the caller should paste the page / upload a
# screenshot rather than proceed to matching.
MANDATORY_FIELDS = (
    ("hotel.name",                   lambda p: p.hotel.name),
    ("stay.check_in",                lambda p: p.stay.check_in),
    ("stay.check_out",               lambda p: p.stay.check_out),
    ("stay.rooms",                   lambda p: p.stay.rooms),
    ("stay.occupancy",               lambda p: bool(p.stay.occupancy)),
    ("requested_offer.room_name",    lambda p: p.requested_offer.room_name),
    # "room detail" — a prose description OR the bed/view fields that describe
    # the same thing. Many OTAs never show a separate description blurb.
    ("requested_offer.room_detail",  lambda p: bool(
        p.requested_offer.description or p.requested_offer.bed_type
        or p.requested_offer.view)),
    ("ota_benchmark.final_payable",  lambda p: p.ota_benchmark.final_payable),
)


@dataclass
class BookingIntent:
    source: Source
    schema_version: str = SCHEMA_VERSION
    hotel: Hotel = field(default_factory=Hotel)
    stay: Stay = field(default_factory=Stay)
    requested_offer: Offer = field(default_factory=Offer)
    ota_benchmark: Benchmark = field(default_factory=Benchmark)
    evidence: list = field(default_factory=list)
    matching_policy: dict = field(default_factory=lambda: dict(DEFAULT_MATCHING_POLICY))
    warnings: list = field(default_factory=list)
    status: str = "incomplete"           # "ok" | "fail"
    missing_mandatory: list = field(default_factory=list)
    run_log: list = field(default_factory=list)   # [{ms, msg}] — step trace for the UI
    _t0: float = field(default_factory=time.monotonic, repr=False, compare=False)

    def log(self, msg: str) -> None:
        self.run_log.append(
            {"ms": round((time.monotonic() - self._t0) * 1000), "msg": str(msg)})

    def check_mandatory(self) -> list:
        """Recompute status from the mandatory-field list. Returns the
        missing ones."""
        self.missing_mandatory = [
            name for name, present in MANDATORY_FIELDS if not present(self)
        ]
        self.status = "fail" if self.missing_mandatory else "ok"
        return self.missing_mandatory

    # -- write API for adapters -----------------------------------------

    def add(self, path: str, value, source: str, confidence: float,
            pointer: Optional[str] = None) -> None:
        """Set a dotted field AND record its provenance. `value` of None is
        ignored (keeps the packet honest about what was actually found)."""
        if value is None:
            return
        if isinstance(value, str):
            value = clean_text(value)
            if not value:
                return
        obj_path, _, attr = path.rpartition(".")
        target = self
        for part in obj_path.split("."):
            target = getattr(target, part)
        setattr(target, attr, value)
        self.evidence.append(
            Evidence(path, value, source, round(float(confidence), 2), pointer)
        )

    def note(self, path: str, value, source: str, confidence: float,
             pointer: Optional[str] = None) -> None:
        """Record provenance for a field that was already set (e.g. derived
        in bulk) without re-setting it."""
        if value is None:
            return
        self.evidence.append(
            Evidence(path, value, source, round(float(confidence), 2), pointer)
        )

    def derive_stay(self) -> None:
        """Fill nights <-> check_out from whichever two of the three are known."""
        ci = parse_iso(self.stay.check_in)
        co = parse_iso(self.stay.check_out)
        if ci and co and self.stay.nights is None:
            self.stay.nights = (co - ci).days
            self.note("stay.nights", self.stay.nights, INFERRED, 1.0,
                      "check_out - check_in")
        elif ci and self.stay.nights and not co:
            self.stay.check_out = (ci + timedelta(days=self.stay.nights)).isoformat()
            self.note("stay.check_out", self.stay.check_out, INFERRED, 1.0,
                      "check_in + nights")

    def min_confidence(self) -> Optional[float]:
        vals = [e.confidence for e in self.evidence]
        return min(vals) if vals else None

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("_t0", None)
        return d


# -- text hygiene (strategy doc §13: all OTA text is untrusted) ----------

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS_RE = re.compile(r"\s+")


def clean_text(value, max_len: int = 400) -> str:
    """Normalize and defang a string pulled from an OTA URL/DOM before it
    enters the packet. Strips control chars, collapses whitespace, removes
    angle brackets so it can't be re-injected as markup downstream."""
    if value is None:
        return ""
    s = unicodedata.normalize("NFKC", str(value))
    s = _CONTROL_RE.sub("", s)
    s = s.replace("<", "").replace(">", "")
    s = _WS_RE.sub(" ", s).strip()
    return s[:max_len]


# -- dates -------------------------------------------------------------

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def now_iso() -> str:
    """Local timestamp with UTC offset, e.g. 2026-08-31T17:00:00+05:30."""
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def parse_iso(value: Optional[str]) -> Optional[date]:
    if not value or not _ISO_RE.match(str(value)):
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        return None


# -- validation (feeds the Phase-1 manual review/debug panel) -----------

def validate(packet: BookingIntent, today: Optional[date] = None) -> list:
    """Sanity-check a packet. Appends to `packet.warnings` and returns the
    full list. Never raises — the matching engine (§9) decides how strict."""
    today = today or date.today()
    w = packet.warnings
    s, p = packet.stay, packet.ota_benchmark

    ci = parse_iso(s.check_in)
    co = parse_iso(s.check_out)

    for name, raw in (("check_in", s.check_in), ("check_out", s.check_out)):
        if raw and not _ISO_RE.match(str(raw)):
            w.append(f"stay.{name} not in YYYY-MM-DD format: {raw!r}")

    if ci and ci == today:
        w.append("stay.check_in equals today's date — likely an OTA default, "
                 "not a real user selection. Treat stay dates as unreliable.")
    if ci and ci < today:
        w.append(f"stay.check_in {s.check_in} is in the past")
    if ci and co and co <= ci:
        w.append(f"stay.check_out {s.check_out} is not after check_in {s.check_in}")
    if s.nights is not None and not (1 <= s.nights <= 30):
        w.append(f"Suspicious nights count: {s.nights}")
    if s.adults is not None and not (1 <= s.adults <= 20):
        w.append(f"Suspicious adults count: {s.adults}")
    if s.rooms is not None and not (1 <= s.rooms <= 10):
        w.append(f"Suspicious rooms count: {s.rooms}")
    if s.children is not None and not (0 <= s.children <= 10):
        w.append(f"Suspicious children count: {s.children}")

    for i, r in enumerate(s.occupancy, 1):
        if r.adults is not None and not (1 <= r.adults <= 10):
            w.append(f"Room {i}: suspicious adults count: {r.adults}")
        if r.children and r.child_ages and len(r.child_ages) != r.children:
            w.append(f"Room {i}: {r.children} children but "
                     f"{len(r.child_ages)} age(s) given")
        for age in r.child_ages:
            if not (0 <= age <= 17):
                w.append(f"Room {i}: suspicious child age: {age}")
    if s.occupancy and s.rooms and len(s.occupancy) != s.rooms:
        w.append(f"rooms={s.rooms} but occupancy has {len(s.occupancy)} room(s)")

    if p.final_payable is not None:
        if not isinstance(p.final_payable, (int, float)) or p.final_payable <= 0:
            w.append(f"Suspicious price value: {p.final_payable!r}")
        elif p.final_payable < 100:
            w.append(f"Price {p.final_payable} looks too low for a stay total")
        if not p.currency:
            w.append("Price present but currency is unknown")

    for e in packet.evidence:
        if isinstance(e.confidence, (int, float)) and e.confidence < 0.5:
            w.append(f"Low confidence on {e.field}: {e.confidence} "
                     f"(source={e.source})")

    return w
