"""Name / geo normalization shared by the loader and the resolver."""
from __future__ import annotations

import json
import math
import re

try:
    from unidecode import unidecode
except ImportError:                       # pragma: no cover
    def unidecode(s):  # type: ignore
        return s

# generic words that carry little identifying signal
_GENERIC = {
    "hotel", "hotels", "resort", "resorts", "inn", "suites", "suite", "the",
    "and", "&", "by", "a", "an", "of", "at", "on", "in", "near", "de", "la",
    "el", "les", "los", "das", "der", "die", "das",
    "guest", "house", "guesthouse", "lodge", "motel", "hostel", "apartment",
    "apartments", "apart", "aparthotel", "residency", "residence", "villa",
    "villas", "collection", "hometel", "rooms", "room", "stay", "stays",
    "boutique",
}
# real country names (+ common aliases), normalised. Used to tell an actual
# country apart from an address fragment like "Bandra West".
_COUNTRY_NAMES = {
    "india", "bharat", "united states", "united states of america", "usa", "us",
    "united kingdom", "uk", "great britain", "england", "scotland", "wales",
    "northern ireland", "ireland", "canada", "australia", "new zealand",
    "united arab emirates", "uae", "saudi arabia", "qatar", "kuwait",
    "bahrain", "oman", "jordan", "israel", "lebanon", "egypt", "morocco",
    "tunisia", "turkey", "greece", "cyprus", "malta", "italy", "france",
    "spain", "portugal", "germany", "netherlands", "belgium", "luxembourg",
    "switzerland", "austria", "liechtenstein", "monaco", "andorra",
    "sweden", "norway", "denmark", "finland", "iceland", "estonia", "latvia",
    "lithuania", "poland", "czech republic", "czechia", "slovakia", "hungary",
    "romania", "bulgaria", "croatia", "slovenia", "serbia", "montenegro",
    "bosnia and herzegovina", "north macedonia", "albania", "kosovo",
    "russia", "russian federation", "ukraine", "belarus", "moldova", "georgia",
    "armenia", "azerbaijan", "kazakhstan", "uzbekistan", "turkmenistan",
    "kyrgyzstan", "tajikistan", "china", "hong kong", "macau", "taiwan",
    "japan", "south korea", "korea", "north korea", "mongolia", "thailand",
    "vietnam", "cambodia", "laos", "myanmar", "burma", "malaysia", "singapore",
    "indonesia", "philippines", "brunei", "timor leste", "east timor",
    "sri lanka", "nepal", "bhutan", "bangladesh", "pakistan", "afghanistan",
    "maldives", "mauritius", "seychelles", "madagascar", "south africa",
    "namibia", "botswana", "zimbabwe", "zambia", "mozambique", "kenya",
    "tanzania", "uganda", "rwanda", "ethiopia", "ghana", "nigeria", "senegal",
    "ivory coast", "cote d ivoire", "cameroon", "gabon", "angola",
    "united states minor outlying islands", "mexico", "guatemala", "belize",
    "honduras", "el salvador", "nicaragua", "costa rica", "panama", "cuba",
    "jamaica", "haiti", "dominican republic", "bahamas", "barbados",
    "trinidad and tobago", "puerto rico", "aruba", "curacao", "colombia",
    "venezuela", "ecuador", "peru", "bolivia", "brazil", "paraguay",
    "uruguay", "argentina", "chile", "fiji", "papua new guinea", "samoa",
    "tonga", "vanuatu", "new caledonia", "french polynesia", "guam",
}


def is_country(s) -> bool:
    if not s:
        return False
    n = norm_name(s)
    return n in _COUNTRY_NAMES or n.replace(" ", "") in {
        c.replace(" ", "") for c in _COUNTRY_NAMES}


# common brand connectors — everything after " by " / " - " is often a chain tag
_BRAND_SPLIT = re.compile(r"\s+(?:by|-|–|—|\||,)\s+", re.I)
_PUNCT = re.compile(r"[^\w\s]", re.U)
_WS = re.compile(r"\s+")


def norm_name(s: str | None) -> str:
    if not s:
        return ""
    s = unidecode(str(s)).lower()
    s = _PUNCT.sub(" ", s)
    return _WS.sub(" ", s).strip()


def core_name(s: str | None) -> str:
    """Normalized name with the brand suffix and generic tokens removed —
    what's left is usually the distinctive part ("aloha on the ganges")."""
    if not s:
        return ""
    head = _BRAND_SPLIT.split(str(s), maxsplit=1)[0]
    toks = [t for t in norm_name(head).split() if t not in _GENERIC]
    return " ".join(toks) or norm_name(head)


def norm_region(s: str | None) -> str:
    return norm_name(s)


def parse_location(raw) -> tuple[float | None, float | None]:
    """'{"lat": -33.8, "lon": -59.6}' -> (-33.8, -59.6)."""
    if raw is None:
        return None, None
    if isinstance(raw, dict):
        d = raw
    else:
        try:
            d = json.loads(raw)
        except (ValueError, TypeError):
            return None, None
    lat = d.get("lat") if isinstance(d, dict) else None
    lon = d.get("lon", d.get("lng")) if isinstance(d, dict) else None
    try:
        lat = float(lat) if lat is not None else None
        lon = float(lon) if lon is not None else None
    except (TypeError, ValueError):
        return None, None
    if lat is not None and (not -90 <= lat <= 90):
        lat = None
    if lon is not None and (not -180 <= lon <= 180):
        lon = None
    return lat, lon


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in metres."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def deg_box(lat: float, lon: float, metres: float) -> tuple[float, float, float, float]:
    """Bounding box (lat_min, lat_max, lon_min, lon_max) around a point."""
    dlat = metres / 111_320.0
    dlon = metres / (111_320.0 * max(math.cos(math.radians(lat)), 0.01))
    return lat - dlat, lat + dlat, lon - dlon, lon + dlon
