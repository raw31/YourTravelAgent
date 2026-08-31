"""hotel_options() — the TripJack Hotel **Detail / Pricing** call for one
resolved hotel id, normalised.

    POST https://apitest-hms.tripjack.com/hms/v3/hotel/pricing

Just this endpoint. Given tj_id + check_in/check_out + per-room occupancy,
returns the hotel's live bookable options (rooms, rates, meal plans,
cancellation). No Listing/Review/Book here.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta

from yta.tripjack.client import TripJackClient

IST = timezone(timedelta(hours=5, minutes=30))   # TripJack cancellation dates are IST


# -- normalised output -------------------------------------------

@dataclass
class SupplierOption:
    option_id: str
    option_type: str                 # SRSM / SRCM / CRSM / CRCM
    rooms: list                      # [{id, name, adults, children}]
    meal_basis: str                  # "Room Only" / "Breakfast" / ...
    inclusions: list
    refundable: bool
    free_cancel_until: str | None     # ISO; latest window whose penalty is 0
    cancellation_penalties: list      # [{from, to, amount}]
    total_price: float
    base_price: float
    taxes: float
    mgmt_fee: float                   # mf
    mgmt_fee_tax: float               # mft
    currency: str
    strikethrough: float | None
    commercial_type: str             # NET / COMMISSIONABLE / EXTRANET
    commission: float
    pan_required: bool
    passport_required: bool
    gst_type: str                    # NA / PASSTHROUGH / RESELLER
    booking_notes: str | None

    def to_dict(self):
        return asdict(self)


@dataclass
class SupplierDetail:
    tj_id: str
    hotel_name: str | None
    review_hash: str | None
    correlation_id: str
    check_in: str
    check_out: str
    rooms_query: list
    currency: str
    options: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    raw: dict | None = None

    def to_dict(self):
        d = asdict(self)
        d["options"] = [o.to_dict() if isinstance(o, SupplierOption) else o
                        for o in self.options]
        d.pop("raw", None)
        return d


# -- request helpers --------------------------------------------

def rooms_payload(occupancy) -> list:
    """occupancy: list[RoomOccupancy | dict] -> TripJack `rooms` array.
    A room with no adults defaults to 2 (TripJack requires adults >= 1)."""
    out = []
    for r in occupancy or []:
        adults = getattr(r, "adults", None) if not isinstance(r, dict) else r.get("adults")
        children = getattr(r, "children", 0) if not isinstance(r, dict) else r.get("children", 0)
        ages = getattr(r, "child_ages", None) if not isinstance(r, dict) else r.get("child_ages")
        room = {"adults": int(adults) if adults else 2}
        children = int(children or 0)
        if children:
            room["children"] = children
            room["childAge"] = [int(a) for a in (ages or [])][:children] or [10] * children
        out.append(room)
    return out or [{"adults": 2}]


def _free_cancel_until(penalties: list) -> str | None:
    """Latest window boundary before any charge kicks in."""
    best = None
    for p in penalties or []:
        try:
            amt = float(p.get("amount", 0))
        except (TypeError, ValueError):
            amt = 0.0
        if amt == 0.0 and p.get("to"):
            if best is None or p["to"] > best:
                best = p["to"]
    return best


def _norm_option(o: dict) -> SupplierOption:
    pr = o.get("pricing", {}) or {}
    cm = o.get("commercial", {}) or {}
    cp = o.get("compliance", {}) or {}
    cn = o.get("cancellation", {}) or {}
    pens = cn.get("penalties", []) or []
    return SupplierOption(
        option_id=o.get("optionId", ""),
        option_type=o.get("optionType", ""),
        rooms=o.get("roomInfo", []) or [],
        meal_basis=o.get("mealBasis", ""),
        inclusions=o.get("inclusions", []) or [],
        refundable=bool(cn.get("isRefundable")),
        free_cancel_until=_free_cancel_until(pens),
        cancellation_penalties=pens,
        total_price=_f(pr.get("totalPrice")),
        base_price=_f(pr.get("basePrice")),
        taxes=_f(pr.get("taxes")),
        mgmt_fee=_f(pr.get("mf")),
        mgmt_fee_tax=_f(pr.get("mft")),
        currency=pr.get("currency", ""),
        strikethrough=_f(pr.get("strikethrough")) if pr.get("strikethrough") else None,
        commercial_type=cm.get("type", ""),
        commission=_f(cm.get("commission")),
        pan_required=bool(cp.get("panRequired")),
        passport_required=bool(cp.get("passportRequired")),
        gst_type=cp.get("gstType", ""),
        booking_notes=o.get("bookingNotes"),
    )


def _f(v):
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return 0.0


# -- request building (no network) ----------------------------

PRICING_URL = "https://apitest-hms.tripjack.com/hms/v3/hotel/pricing"


def pricing_request(tj_id, check_in: str, check_out: str, occupancy, *,
                    currency: str = "INR", nationality: str = "106",
                    correlation_id: str | None = None) -> dict:
    """Build the exact request for POST /hms/v3/hotel/pricing — no client,
    no network. Returns {method, url, headers, body}."""
    return {
        "method": "POST",
        "url": PRICING_URL,
        "headers": {"apikey": "<TRIPJACK_API_KEY>",
                    "Content-Type": "application/json",
                    "Accept": "application/json"},
        "body": TripJackClient.pricing_body(
            hid=str(tj_id), check_in=check_in, check_out=check_out,
            rooms=rooms_payload(occupancy), currency=currency,
            nationality=nationality,
            correlation_id=correlation_id or uuid.uuid4().hex),
    }


def pricing_request_from_packet(packet, tj_id, *, correlation_id=None) -> dict:
    """Build the Pricing request straight from a Phase-1 BookingIntent packet
    + the Phase-2 resolved tj_id."""
    s = packet.stay
    if not (s.check_in and s.check_out):
        raise ValueError("packet has no stay dates — Pricing needs checkIn/checkOut")
    occ = s.occupancy or [{"adults": s.adults or 2, "children": s.children or 0,
                           "child_ages": s.child_ages or []}]
    return pricing_request(
        tj_id, s.check_in, s.check_out, occ,
        currency=packet.ota_benchmark.currency or "INR",
        correlation_id=correlation_id)


# -- entry point (build + send + normalise) ------------------

def hotel_options(tj_id, check_in: str, check_out: str, occupancy, *,
                  currency: str = "INR", nationality: str | None = None,
                  correlation_id: str | None = None,
                  client: TripJackClient | None = None) -> SupplierDetail:
    """Call POST /hms/v3/hotel/pricing for one hotel and normalise the result."""
    client = client or TripJackClient.from_env()
    corr = correlation_id or uuid.uuid4().hex
    rooms = rooms_payload(occupancy)
    nat = nationality or client.nationality
    result = SupplierDetail(tj_id=str(tj_id), hotel_name=None, review_hash=None,
                            correlation_id=corr, check_in=check_in,
                            check_out=check_out, rooms_query=rooms, currency=currency)

    resp = client.pricing(hid=str(tj_id), check_in=check_in, check_out=check_out,
                          rooms=rooms, currency=currency, nationality=nat,
                          correlation_id=corr)
    result.raw = resp
    result.hotel_name = resp.get("hotelName")
    result.review_hash = resp.get("reviewHash")
    result.options = [_norm_option(o) for o in resp.get("options", []) or []]
    if not result.options:
        result.notes.append("no bookable options returned for this stay")
    return result
