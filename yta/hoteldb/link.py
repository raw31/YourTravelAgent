"""Bridge Phase 1 (BookingIntent) -> Phase 2 (TripJack hotel id).

    from yta import extract
    from yta.hoteldb.link import resolve_packet

    packet = extract(url, page_html=...)
    match = resolve_packet(packet)      # ResolveResult
    if match.band == "high":
        tj_id = match.match.tj_id       # -> TripJack Hotel Detail API
"""
from __future__ import annotations

import re

from yta.hoteldb.resolver import resolve, ResolveResult

# small allow-list so an address fragment ("Bandra West") is never treated
# as a country. Only used as a fallback when the LLM didn't give hotel.country.
_COUNTRY_HINTS = {
    "india", "united states", "usa", "us", "united kingdom", "uk", "england",
    "uae", "united arab emirates", "thailand", "singapore", "malaysia",
    "indonesia", "vietnam", "philippines", "sri lanka", "nepal", "bhutan",
    "maldives", "bangladesh", "pakistan", "china", "japan", "south korea",
    "korea", "australia", "new zealand", "france", "germany", "italy", "spain",
    "portugal", "greece", "turkey", "egypt", "morocco", "south africa",
    "kenya", "tanzania", "mauritius", "seychelles", "canada", "mexico",
    "brazil", "argentina", "chile", "peru", "colombia", "netherlands",
    "belgium", "switzerland", "austria", "sweden", "norway", "denmark",
    "finland", "ireland", "poland", "czech republic", "hungary", "croatia",
    "russia", "ukraine", "saudi arabia", "qatar", "kuwait", "bahrain", "oman",
    "jordan", "israel", "lebanon", "cambodia", "laos", "myanmar", "taiwan",
    "hong kong", "macau",
}


def _country_from_address(addr: str | None) -> str | None:
    """Fallback country guess — only accept a segment that is actually a
    country name, not just any trailing address fragment."""
    if not addr:
        return None
    segs = [s.strip() for s in re.split(r"[,\n]", addr) if s.strip()]
    for seg in reversed(segs[-3:]):
        low = re.sub(r"\s+\d.*$", "", seg).strip().lower()   # drop pincodes
        if low in _COUNTRY_HINTS:
            return seg
    return None


def resolve_packet(packet, *, con=None, limit: int = 5) -> ResolveResult:
    h = packet.hotel
    country = h.country or _country_from_address(h.address)
    return resolve(
        h.name or "",
        city=h.city,
        address=h.address,
        lat=h.lat,
        lng=h.lng,
        country=country,
        con=con,
        limit=limit,
    )
