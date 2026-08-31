"""Deterministic facts pulled straight from the URL — currently just
coordinates, which are the strongest signal for TripJack hotel resolution
and are cheap and unambiguous when present.

Everything else (dates, occupancy, price) still goes through the LLM.
"""
from __future__ import annotations

import re
from urllib.parse import unquote, urlparse, parse_qs

# separate lat / lng params (any of these names, case-insensitive)
_LAT_KEYS = ("lat", "latitude", "latt", "y", "hlat", "hotellat", "geolat")
_LNG_KEYS = ("lng", "lon", "long", "longitude", "lngt", "x", "hlng", "hlon",
             "hotellng", "hotellon", "geolng", "geolon")

# single params holding "lat,lng" (or ; | space separated)
_PAIR_KEYS = ("latlng", "latlong", "ll", "geo", "coord", "coords", "coordinate",
              "coordinates", "center", "centre", "location", "loc", "sll",
              "point", "position", "pos", "map", "gps")

_NUM = r"[-+]?\d{1,3}(?:\.\d+)?"
_PAIR_RE = re.compile(rf"({_NUM})\s*[,;| ]\s*({_NUM})")
# google-maps style ".../@30.1308,78.3213,15z"
_AT_RE = re.compile(rf"@({_NUM}),({_NUM})")


def _valid(lat, lng):
    return (lat is not None and lng is not None
            and -90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0
            and not (lat == 0.0 and lng == 0.0))


def _order(a: float, b: float):
    """Return (lat, lng). If exactly one value is outside +-90 it must be the
    longitude; otherwise assume the conventional lat,lng order."""
    a_is_lat = abs(a) <= 90
    b_is_lat = abs(b) <= 90
    if a_is_lat and not b_is_lat:
        return a, b
    if b_is_lat and not a_is_lat:
        return b, a
    return a, b            # both plausible as latitude -> trust the given order


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def latlng_from_url(url: str):
    """-> (lat, lng) or (None, None). Tries, in order: separate lat/lng
    params, a combined 'lat,lng' param, then an @lat,lng in the path."""
    if not url:
        return None, None
    parsed = urlparse(url)
    q = parse_qs(parsed.query, keep_blank_values=False)
    low = {k.lower(): vs[0] for k, vs in q.items() if vs}

    # 1. separate params
    lat = next((_f(low[k]) for k in _LAT_KEYS if k in low), None)
    lng = next((_f(low[k]) for k in _LNG_KEYS if k in low), None)
    if _valid(lat, lng):
        return _order(lat, lng)

    # 2. combined "lat,lng" param
    for k in _PAIR_KEYS:
        if k in low:
            m = _PAIR_RE.search(unquote(low[k]))
            if m:
                a, b = _f(m.group(1)), _f(m.group(2))
                if a is not None and b is not None:
                    la, ln = _order(a, b)
                    if _valid(la, ln):
                        return la, ln

    # 3. google-maps @lat,lng in the path or the raw url
    m = _AT_RE.search(unquote(url))
    if m:
        a, b = _f(m.group(1)), _f(m.group(2))
        la, ln = _order(a, b) if (a is not None and b is not None) else (None, None)
        if _valid(la, ln):
            return la, ln

    return None, None
