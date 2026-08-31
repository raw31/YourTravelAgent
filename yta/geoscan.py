"""Coordinates from a rendered page — only where the site *explicitly declares
the hotel's location* through a web standard, and only when that declaration
is unambiguous (appears exactly once).

No per-site markup knowledge, no heuristics about "which of the many lat/lngs
on the page is probably the hotel". If the site doesn't publish its location
in a standard place, we return None and resolution falls back to
name + city + country.

Sources, in order:
  1. schema.org `geo` on a single Hotel / Lodging / Resort / Place node
     (JSON-LD or microdata) — the W3C way to state a place's coordinates
  2. `og:latitude` + `og:longitude` / `place:location:*` <meta> (Open Graph)
  3. `geo.position` / `ICBM` <meta> (the pre-OG geotag convention)
  4. exactly one `data-lat*` + `data-lng*` attribute pair (map-widget convention)
"""
from __future__ import annotations

import re

_NUM = r"-?\d{1,3}(?:\.\d+)?"


def _valid(lat, lng):
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180) or (lat == 0 and lng == 0):
        return None
    return round(lat, 6), round(lng, 6)


def _order(a, b):
    if abs(a) <= 90 < abs(b):
        return a, b
    if abs(b) <= 90 < abs(a):
        return b, a
    return a, b


# -- 1. schema.org geo -----------------------------------------

_HOTEL_TYPES = ("hotel", "lodging", "lodgingbusiness", "resort", "place",
                "hostel", "motel", "bedandbreakfast", "campground")


def _from_jsonld(blocks):
    hits = []
    for blk in blocks or []:
        for node in (blk if isinstance(blk, list) else [blk]):
            if not isinstance(node, dict):
                continue
            t = str(node.get("@type", "")).lower().replace(" ", "")
            if not any(k == t for k in _HOTEL_TYPES):
                continue
            geo = node.get("geo") or {}
            if isinstance(geo, list):
                geo = geo[0] if geo else {}
            if isinstance(geo, dict):
                v = _valid(geo.get("latitude"), geo.get("longitude"))
                if v:
                    hits.append(v)
    # only if the whole page declares exactly one hotel location
    uniq = list({h for h in hits})
    return uniq[0] if len(uniq) == 1 else None


# schema.org microdata: <meta itemprop="latitude" content="..."> inside a geo scope
_MICRO_LAT = re.compile(
    r'itemprop=["\']latitude["\'][^>]*?content=["\']([^"\']+)', re.I)
_MICRO_LNG = re.compile(
    r'itemprop=["\']longitude["\'][^>]*?content=["\']([^"\']+)', re.I)


# -- 2/3. geo meta tags ---------------------------------------

_META_LAT = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:|place:location:)?latitude["\']'
    r'[^>]*?content=["\']([^"\']+)', re.I)
_META_LNG = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:|place:location:)?longitude["\']'
    r'[^>]*?content=["\']([^"\']+)', re.I)
_META_GEOPOS = re.compile(
    r'<meta[^>]+name=["\'](?:geo\.position|icbm)["\'][^>]*?content=["\']'
    rf'({_NUM})\s*[;,]\s*({_NUM})', re.I)


# -- 4. data-* attribute pair (only if unique) ---------------

_DATA_LAT = re.compile(rf'data-lat(?:itude)?=["\']({_NUM})["\']', re.I)
_DATA_LNG = re.compile(rf'data-(?:lng|lon|long|longitude)=["\']({_NUM})["\']', re.I)


def find_latlng(html: str = "", json_ld=None, xhr_json=None):
    """(lat, lng, source) or None. `xhr_json` is accepted for signature
    compatibility but not used — captured API bodies are full of nearby-hotel
    and POI coordinates with no reliable way to tell which is the hotel."""
    html = html or ""

    v = _from_jsonld(json_ld)
    if v:
        return (*v, "schema.org geo (JSON-LD)")

    ml, mg = _MICRO_LAT.findall(html), _MICRO_LNG.findall(html)
    if len(set(ml)) == 1 and len(set(mg)) == 1:
        v = _valid(ml[0], mg[0])
        if v:
            return (*v, "schema.org geo (microdata)")

    ma, mo = _META_LAT.search(html), _META_LNG.search(html)
    if ma and mo:
        v = _valid(ma.group(1), mo.group(1))
        if v:
            return (*v, "og:latitude / place:location meta")

    m = _META_GEOPOS.search(html)
    if m:
        v = _valid(*_order(float(m.group(1)), float(m.group(2))))
        if v:
            return (*v, "geo.position meta")

    lats, lngs = _DATA_LAT.findall(html), _DATA_LNG.findall(html)
    if len(set(lats)) == 1 and len(set(lngs)) == 1:
        v = _valid(*_order(float(lats[0]), float(lngs[0])))
        if v:
            return (*v, "data-lat attribute")

    return None
