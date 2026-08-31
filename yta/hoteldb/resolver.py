"""Hotel-identity resolution (strategy doc §9) — multi-layer cascade.

Given what Phase 1 scraped from the OTA (name + optional city / region /
country / address / coordinates), find the exact TripJack hotel id.

    from yta.hoteldb import resolve
    r = resolve("Aloha on the Ganges by Leisure Hotels",
                city="Rishikesh", lat=30.13083, lng=78.32835, country="India")
    r.band        # "high" | "medium" | "low" | "none"
    r.match       # best Candidate (None if band == "none")
    r.candidates  # ranked alternatives, each with a full score breakdown

Cascade:
  L0   retrieve pool   coords -> lat/lon box ;  else -> FTS on distinctive tokens
  L1   country         narrow. A *recognised* country with 0 matching rows ->
                       "none" (wrong hotel entirely). A junk string -> ignored.
  L4a  coordinates     hard gate — with coords, drop any candidate > 25 km from
                       the pin; if that empties the pool -> "none".
  L2   region / city   narrow (region_name OR city token inside hotel_full_name)
  L3   address         score boost only (too noisy to filter on)
  then score survivors -> band
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict

from yta.hoteldb.db import connect
from yta.hoteldb.normalize import (_GENERIC, core_name, deg_box, haversine_m,
                                   is_country, norm_name, norm_region)

try:
    from rapidfuzz import fuzz
except ImportError:                        # pragma: no cover
    from difflib import SequenceMatcher

    class fuzz:  # type: ignore
        @staticmethod
        def token_set_ratio(a, b):
            return SequenceMatcher(None, str(a), str(b)).ratio() * 100
        token_sort_ratio = token_set_ratio
        ratio = token_set_ratio


GEO_FULL_M = 150.0
GEO_ZERO_M = 3000.0
GEO_BOX_M = 2000.0
FAR_PIN_M = 3000.0        # chosen hotel this far from the pin -> demote + note
MAX_PIN_M = 25000.0      # with coords, a candidate beyond this is discarded outright

BAND_HIGH = 0.84        # auto-pick
BAND_MED = 0.75         # usable but confirm;  below this -> "not found"

_TOK = re.compile(r"[a-z0-9]+")

# short forms / exonyms -> the canonical name TripJack uses.
_COUNTRY_CANON = {
    "usa": "united states", "us": "united states", "u s a": "united states",
    "u s": "united states", "united states of america": "united states",
    "america": "united states", "the states": "united states",
    "uk": "united kingdom", "u k": "united kingdom", "britain": "united kingdom",
    "great britain": "united kingdom", "england": "united kingdom",
    "scotland": "united kingdom", "wales": "united kingdom",
    "uae": "united arab emirates", "u a e": "united arab emirates",
    "emirates": "united arab emirates", "the emirates": "united arab emirates",
    "korea": "south korea", "republic of korea": "south korea",
    "s korea": "south korea", "dprk": "north korea", "bharat": "india",
    "republic of india": "india",
    "czechia": "czech republic", "holland": "netherlands", "the netherlands":
    "netherlands", "burma": "myanmar", "east timor": "timor leste",
    "ivory coast": "cote d ivoire", "cape verde": "cabo verde",
    "russian federation": "russia", "vietnam": "viet nam", "viet nam": "vietnam",
    "hong kong sar": "hong kong", "macao": "macau",
}
def _canon_country(s: str) -> set[str]:
    n = norm_name(s)
    n = _COUNTRY_CANON.get(n, n)
    n = _COUNTRY_CANON.get(n, n)          # resolve one alias->alias hop
    return {t for t in _TOK.findall(n) if t not in ("of", "the", "republic")}


def _country_match(query_c: set[str], row_country: str) -> bool:
    """True iff the two names denote the same country. The smaller normalised
    token set must be a subset of the larger — so 'united arab emirates' ==
    'uae' == 'emirates', 'united states' == 'united states of america', but
    'south korea' != 'north korea' and 'united states' != 'united kingdom'."""
    if not query_c:
        return True
    rc = _canon_country(row_country or "")
    if not rc:
        return False
    small, big = (query_c, rc) if len(query_c) <= len(rc) else (rc, query_c)
    return small.issubset(big)


# -- name scoring --------------------------------------------------

def _tokens(s: str, drop: set[str]) -> list[str]:
    return [t for t in _TOK.findall(s) if t not in _GENERIC and t not in drop
            and len(t) > 1]


def _tmatch(a: str, b: str) -> bool:
    return a == b or fuzz.ratio(a, b) >= 84


def _name_score(q_core, q_norm, c_core, c_norm, place_tokens: set[str]) -> float:
    """Compare distinctive name tokens, with place names (city/region) removed
    from BOTH sides so "Comfort Inn Flagstaff" isn't scored on "flagstaff".

    token_set_ratio rewards subset matches ("aloha ganges" vs "ganges" ~= 0.9),
    so we also weigh in token order (sort_ratio) and, crucially, *coverage* —
    how many of the query's distinctive tokens actually appear in the
    candidate. One shared common word ("ganges") can't make a strong match.
    """
    qt = _tokens(q_core or q_norm, place_tokens) or _tokens(q_norm, set())
    ct = _tokens(c_core or c_norm, place_tokens) or _tokens(c_norm, set())
    if not qt or not ct:
        return 0.0
    qs, cs = " ".join(qt), " ".join(ct)
    set_r = fuzz.token_set_ratio(qs, cs) / 100.0
    sort_r = fuzz.token_sort_ratio(qs, cs) / 100.0

    matched = sum(1 for q in qt if any(_tmatch(q, c) for c in ct))
    coverage = matched / len(qt)
    score = 0.50 * set_r + 0.22 * sort_r + 0.28 * coverage

    # the brand token (first distinctive word — hotel names lead with it)
    # must be present, else this is a filler-word coincidence
    anchor = qt[0]
    if not any(_tmatch(anchor, c) for c in ct):
        return min(score, 0.30)
    # a single shared token when the query has ≥2 distinctive ones
    if len(qt) >= 2 and matched < 2:
        score = min(score, 0.60)
    return score


def _city_score(city_norm: str, row) -> float | None:
    if not city_norm:
        return None
    hay = f"{row['region_norm'] or ''} {norm_name(row['hotel_full_name'])}".split()
    if any(city_norm == h for h in hay):
        return 1.0
    return fuzz.token_set_ratio(city_norm, " ".join(hay)) / 100.0


def _addr_score(addr_norm: str, row) -> float | None:
    if not addr_norm:
        return None
    a = {t for t in _TOK.findall(addr_norm) if t not in _GENERIC and len(t) > 2}
    b = {t for t in _TOK.findall(
        f"{row['region_norm'] or ''} {norm_name(row['hotel_full_name'])}"
    ) if len(t) > 2}
    if not a or not b:
        return None
    return len(a & b) / len(a)


def _geo_score(dist_m):
    if dist_m is None:
        return None
    if dist_m <= GEO_FULL_M:
        return 1.0
    if dist_m >= GEO_ZERO_M:
        return 0.0
    return 1.0 - (dist_m - GEO_FULL_M) / (GEO_ZERO_M - GEO_FULL_M)


# -- data classes --------------------------------------------------

@dataclass
class Candidate:
    tj_id: int
    unica_id: str | None
    hotel_name: str
    hotel_full_name: str | None
    region_name: str | None
    country_name: str | None
    rating: float | None
    lat: float | None
    lon: float | None
    distance_m: float | None
    name_score: float
    geo_score: float | None
    city_score: float | None
    addr_score: float | None
    score: float

    def to_dict(self):
        return asdict(self)


@dataclass
class ResolveResult:
    query: dict
    band: str
    match: Candidate | None
    candidates: list
    notes: list = field(default_factory=list)
    layers: list = field(default_factory=list)

    def to_dict(self):
        return {
            "query": self.query, "band": self.band, "layers": self.layers,
            "match": self.match.to_dict() if self.match else None,
            "candidates": [c.to_dict() for c in self.candidates],
            "notes": self.notes,
        }


# -- retrieval ----------------------------------------------------

def _distinctive(name: str, place_tokens: set[str]) -> list[str]:
    toks = [t for t in _TOK.findall(norm_name(name))
            if t not in _GENERIC and t not in place_tokens and len(t) > 1]
    return toks or [t for t in _TOK.findall(norm_name(name)) if len(t) > 1][:2]


def _fts_pool(con, name, place_tokens, cap=250):
    toks = _distinctive(name, place_tokens)
    if not toks:
        return []
    q_and = " AND ".join(f'"{t}"' for t in toks[:8])
    q_or = " OR ".join(f'"{t}"' for t in toks[:8])
    sql = ("SELECT h.*, bm25(tj_hotels_fts) AS bm FROM tj_hotels_fts f "
           "JOIN tj_hotels h ON h.tj_id = f.rowid "
           "WHERE tj_hotels_fts MATCH ? ORDER BY bm LIMIT ?")
    for expr in (q_and, q_or):
        try:
            rows = con.execute(sql, [expr, cap]).fetchall()
        except Exception:
            rows = []
        if rows:
            return rows
    return []


def _geo_pool(con, lat, lng, box_m):
    lo_lat, hi_lat, lo_lon, hi_lon = deg_box(lat, lng, box_m)
    return con.execute(
        "SELECT * FROM tj_hotels WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
        (lo_lat, hi_lat, lo_lon, hi_lon)).fetchall()


def _band(score):
    if score >= BAND_HIGH:
        return "high"
    if score >= BAND_MED:
        return "medium"
    return "none"          # < 0.75 -> not a usable match


def resolve(name: str, *, city: str | None = None, region: str | None = None,
            address: str | None = None, country: str | None = None,
            lat: float | None = None, lng: float | None = None,
            con=None, limit: int = 5) -> ResolveResult:
    own = con is None
    con = con or connect()
    q = {"name": name, "city": city, "region": region, "address": address,
         "country": country, "lat": lat, "lng": lng}

    q_norm = norm_name(name)
    q_core = core_name(name) or q_norm
    city_norm = norm_region(city or region or "")
    country_c = _canon_country(country) if country else set()
    addr_norm = norm_name(address) if address else ""
    place_tokens = {t for t in _TOK.findall(f"{city_norm} {norm_region(region or '')}")
                    if len(t) > 1}
    # distinctive query tokens once the city name is stripped — "Amari Bangkok"
    # in Bangkok reduces to just {"amari"}, which can't pin one property
    q_distinct = _tokens(q_core, place_tokens) or _tokens(q_norm, place_tokens)
    thin_name = len(q_distinct) <= 1
    layers, notes = [], []

    try:
        # L0 — retrieval pool
        pool = {}
        if lat is not None and lng is not None:
            for r in _geo_pool(con, lat, lng, GEO_BOX_M):
                pool[r["tj_id"]] = r
            layers.append(f"L0 geo ±{GEO_BOX_M:.0f}m: {len(pool)}")
            if len(pool) < 5:
                for r in _geo_pool(con, lat, lng, GEO_BOX_M * 4):
                    pool.setdefault(r["tj_id"], r)
                layers.append(f"L0 geo ±{GEO_BOX_M*4:.0f}m: {len(pool)}")
        n0 = len(pool)
        for r in _fts_pool(con, name, place_tokens):
            pool.setdefault(r["tj_id"], r)
        layers.append(f"L0 name/FTS: +{len(pool)-n0} (pool {len(pool)})")
        if not pool:
            return ResolveResult(q, "none", None, [], ["no candidates"], layers)
        rows = list(pool.values())

        # L1 — country. Whenever a country was extracted it is a HARD filter:
        # any candidate whose tj_country_name is a different country is dropped.
        country_wrong = False
        if country_c:
            keep = [r for r in rows if _country_match(country_c, r["country_name"])]
            if keep:
                if len(keep) < len(rows):
                    layers.append(f"L1 country={country!r}: {len(keep)} kept "
                                  f"(dropped {len(rows) - len(keep)} in other countries)")
                else:
                    layers.append(f"L1 country={country!r}: all {len(rows)} match")
                rows = keep
            elif is_country(country):
                country_wrong = True
                layers.append(f"L1 country={country!r}: NONE of the {len(rows)} "
                              f"candidates are in {country} — no match")
            else:
                layers.append(f"L1 country={country!r}: unrecognised country name "
                              f"and 0 matches — not filtering")

        # L4a — hard coordinate gate: drop anything > MAX_PIN_M from the pin
        if lat is not None and lng is not None:
            before = len(rows)
            rows = [r for r in rows if r["lat"] is None
                    or haversine_m(lat, lng, r["lat"], r["lon"]) <= MAX_PIN_M]
            if len(rows) < before:
                layers.append(f"L4a within {MAX_PIN_M/1000:.0f} km of the pin: "
                              f"{len(rows)} (dropped {before - len(rows)})")
            if not rows:
                return ResolveResult(
                    q, "none", None, [],
                    [f"no TripJack hotel within {MAX_PIN_M/1000:.0f} km of the "
                     f"scraped coordinates"], layers)

        # L2 — region / city (skip if it would empty the pool)
        if city_norm:
            keep = [r for r in rows if any(
                city_norm == h for h in
                f"{r['region_norm'] or ''} {norm_name(r['hotel_full_name'])}".split())]
            if keep and len(keep) < len(rows):
                rows = keep
                layers.append(f"L2 city={city_norm!r}: {len(rows)}")
            else:
                layers.append(f"L2 city={city_norm!r}: no narrowing ({len(rows)})")

        # score
        cands = []
        for r in rows:
            dist = (haversine_m(lat, lng, r["lat"], r["lon"])
                    if lat is not None and lng is not None and r["lat"] is not None
                    else None)
            gs = _geo_score(dist)
            ns = _name_score(q_core, q_norm, r["name_core"] or "",
                             r["name_norm"] or "", place_tokens)
            cs = _city_score(city_norm, r)
            as_ = _addr_score(addr_norm, r)

            name_trust = 0.74 * ns + 0.16 * (cs if cs is not None else ns) \
                + 0.10 * (as_ if as_ is not None else ns)
            if gs is not None:
                # every surviving candidate is within MAX_PIN_M of the pin now
                geo_trust = 0.46 * gs + 0.40 * ns + 0.09 * (cs or 0) + 0.05 * (as_ or 0)
                if ns < 0.4:                       # right spot, wrong name
                    geo_trust = min(geo_trust, 0.52)
                s = max(geo_trust, name_trust - 0.03)
            else:
                s = name_trust

            cands.append(Candidate(
                tj_id=r["tj_id"], unica_id=r["unica_id"], hotel_name=r["hotel_name"],
                hotel_full_name=r["hotel_full_name"], region_name=r["region_name"],
                country_name=r["country_name"], rating=r["rating"],
                lat=r["lat"], lon=r["lon"],
                distance_m=round(dist, 1) if dist is not None else None,
                name_score=round(ns, 3),
                geo_score=round(gs, 3) if gs is not None else None,
                city_score=round(cs, 3) if cs is not None else None,
                addr_score=round(as_, 3) if as_ is not None else None,
                score=round(s, 3)))

        cands.sort(key=lambda c: c.score, reverse=True)
        top = cands[:limit]
        best = top[0]
        band = _band(best.score)

        if country_wrong:
            notes.append(f"every candidate is in a country other than "
                         f"{country} — discarded")
            return ResolveResult(q, "none", None, top, notes, layers)

        if len(top) > 1 and best.score - top[1].score < 0.04 and top[1].name_score > 0.5:
            notes.append(f"ambiguous: #{top[1].tj_id} ({top[1].score}) ≈ "
                         f"#{best.tj_id} ({best.score}) — confirm")
            if band == "high":
                band = "medium"
        if band != "none" and best.distance_m is not None and best.distance_m > FAR_PIN_M:
            notes.append(f"chosen hotel is {best.distance_m/1000:.1f} km from the "
                         f"scraped coordinates — the OTA pin may be stale; confirm")
            if band == "high":
                band = "medium"
        if band == "high" and best.name_score < 0.6:
            notes.append("weak name match — confirm")
            band = "medium"
        # a name that reduces to one word (usually a chain) can't be "high"
        # without a coordinate pinning the exact property
        if band == "high" and thin_name and best.geo_score is None:
            notes.append(f"name reduces to one distinctive word "
                         f"({q_distinct or ['?']}) — can't pick one property "
                         f"of the chain without coordinates; confirm")
            band = "medium"

        return ResolveResult(q, band, best if band != "none" else None,
                             top, notes, layers)
    finally:
        if own:
            con.close()
