"""Occupancy — the booking field that fails most, resolved generically.

Occupancy always appears as one of three semantic SHAPES, on any OTA:

  per-room   "Room 1: 2 adults, 1 child age 5"  ·  room1=A,A,7  ·  roomGuests[]
  aggregate  "2 rooms, 4 adults, 2 children"     ·  adults=4&children=2&rooms=2
  encoded    2e1e3e2e1e2e  ·  o:2;p:0  ·  rm1=a2:c5  ·  guests=2a1c

`signals_from_url()` recognises those shapes in a URL's params + tokens with
regex families keyed on MEANING, not on OTA brand. `resolve()` reconciles
every signal (URL + whatever the LLM read off the page) into one per-room
list with a confidence and a provenance, doing an even-split when only an
aggregate is known.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urlparse

# ── param-name families (grow slowly; the DECODERS below stay generic) ──
_ADULT_KEYS = {"adults", "adult", "group_adults", "numadults", "numberofadults",
               "noofadults", "adultcount", "adt", "nad", "a", "occadults", "pax_adults"}
_CHILD_KEYS = {"children", "child", "kids", "group_children", "numchildren",
               "numberofchildren", "noofchildren", "childcount", "chd", "nch",
               "occchildren", "pax_children"}
_ROOM_KEYS = {"rooms", "room", "no_rooms", "noofrooms", "numrooms", "roomcount",
              "crn", "nor", "rmcount", "roomnum", "roomqty"}
_AGE_KEYS = {"age", "ages", "childage", "childages", "childrenage", "childrenages",
             "childagelist", "agesofchildren", "childage1", "childage2", "childage3",
             "childage4", "childrenagelist"}
# flat "e / - / _" occupancy streams
_STREAM_KEYS = {"roomstayqualifier", "roomstay", "paxconfig", "occupancies",
                "occupancy", "roomsconfig", "guestconfig", "roomconfig"}
# packed guest strings ("2a1c", "2A 1C", "2-1")
_PACKED_KEYS = {"guests", "pax", "party", "travellers", "travelers", "occ",
                "guestinfo", "paxinfo"}

_MAX_ROOMS = 16


@dataclass
class Room:
    adults: int
    children: int = 0
    child_ages: list = field(default_factory=list)

    def dict(self) -> dict:
        return {"adults": int(self.adults or 0),
                "children": int(self.children or 0),
                "child_ages": [int(a) for a in self.child_ages]}


@dataclass
class OccSignal:
    origin: str                       # "url:room1" / "url:stream" / "url:aggregate" / "llm" / ...
    rooms: int | None = None
    adults: int | None = None         # total across rooms
    children: int | None = None       # total
    child_ages: list = field(default_factory=list)
    per_room: list | None = None      # list[Room] when the split is known
    confidence: float = 0.6

    def total_adults(self) -> int | None:
        if self.per_room is not None:
            return sum(r.adults for r in self.per_room)
        return self.adults

    def total_children(self) -> int | None:
        if self.per_room is not None:
            return sum(r.children for r in self.per_room)
        return self.children


def _ints(s: str) -> list:
    return [int(x) for x in re.findall(r"\d+", s or "")]


def _first_int(low: dict, keys) -> int | None:
    for k in keys:
        if k in low and low[k] and low[k][0].strip().lstrip("-").isdigit():
            return int(low[k][0])
    return None


def _collect_ages(low: dict) -> list:
    ages: list = []
    for k in sorted(low):
        if k in _AGE_KEYS or re.fullmatch(r"(child(ren)?)?age\d*", k):
            for v in low[k]:
                ages += [n for n in _ints(v) if 0 <= n <= 17]
    return ages


# ── stream decoders ───────────────────────────────────────────────────

def _parse_stream(v: str) -> "OccSignal | None":
    """A flat occupancy stream. Two layouts:
      grouped  '2-1-5_2-0'   rooms split by _ | ; , then adults-children-ages
      walk     '2e1e3e2e1e2e' one separator, walk: adults, children, <ages>, …
    """
    v = unquote(v or "").strip().lower()
    if not re.search(r"\d", v):
        return None

    for grp in ("_", "|", ";"):
        if grp in v:
            rooms = []
            for chunk in v.split(grp):
                nums = _ints(chunk)
                if not nums:
                    continue
                a = nums[0]
                c = nums[1] if len(nums) > 1 else 0
                rooms.append(Room(a, c, nums[2:2 + c]))
            if rooms:
                return OccSignal("url:stream", rooms=len(rooms), per_room=rooms,
                                 confidence=0.9)

    nums = _ints(v)
    rooms, i = [], 0
    while i < len(nums):
        a = nums[i]; i += 1
        c = nums[i] if i < len(nums) else 0; i += 1
        ages = nums[i:i + c]; i += c
        if a == 0 and c == 0:
            break
        rooms.append(Room(a, c, ages))
        if len(rooms) >= _MAX_ROOMS:
            break
    if rooms and sum(r.adults for r in rooms):
        return OccSignal("url:stream", rooms=len(rooms), per_room=rooms,
                         confidence=0.82)
    return None


def _parse_rsc(v: str) -> "OccSignal | None":
    """`rsc` (MMT) is the AGGREGATE: rooms e adults e children e <ages>."""
    nums = _ints(unquote(v or ""))
    if len(nums) < 2:
        return None
    rm, ad = nums[0], nums[1]
    ch = nums[2] if len(nums) > 2 else 0
    ages = nums[3:3 + ch]
    return OccSignal("url:rsc", rooms=rm, adults=ad, children=ch,
                     child_ages=ages, confidence=0.8)


def _parse_packed(v: str) -> "OccSignal | None":
    """'2a1c', '2 adults 1 child', '2-1', 'a2c1'."""
    s = unquote(v or "").strip().lower()
    m = re.fullmatch(r"(\d+)\s*a(?:d(?:ult)?s?)?(?:[ ,;/+-]*(\d+)\s*c(?:h(?:ild(?:ren)?)?)?)?", s)
    if m:
        return OccSignal("url:packed", adults=int(m.group(1)),
                         children=int(m.group(2) or 0), confidence=0.75)
    m = re.fullmatch(r"a(\d+)c(\d+)", s)
    if m:
        return OccSignal("url:packed", adults=int(m.group(1)),
                         children=int(m.group(2)), confidence=0.75)
    m = re.fullmatch(r"(\d+)[-_](\d+)", s)
    if m:
        return OccSignal("url:packed", adults=int(m.group(1)),
                         children=int(m.group(2)), confidence=0.6)
    return None


# ── URL scanner ───────────────────────────────────────────────────────

def signals_from_url(url: str) -> list:
    if not url:
        return []
    q = parse_qs(urlparse(url).query, keep_blank_values=False)
    low = {k.lower(): [unquote(x) for x in vs] for k, vs in q.items() if vs}
    out: list = []

    # A. per-room param lists — roomN=A,A,7  (Booking) / rmN=a2:c5:c3 (Expedia)
    for prefix, decode in (("room", _decode_room_letters), ("rm", _decode_rm_codes)):
        rooms = []
        for i in range(1, _MAX_ROOMS + 1):
            v = low.get(f"{prefix}{i}", [None])[0]
            r = decode(v) if v else None
            if r is not None:
                rooms.append(r)
        if rooms:
            out.append(OccSignal(f"url:{prefix}N", rooms=len(rooms),
                                 per_room=rooms, confidence=0.95))

    # B. flat streams — roomStayQualifier etc.
    for key in _STREAM_KEYS:
        for v in low.get(key, []):
            sig = _parse_stream(v)
            if sig:
                out.append(sig)
    for v in low.get("rsc", []):
        sig = _parse_rsc(v)
        if sig:
            out.append(sig)

    # C. token blob — any value that is `k:v;k:v;…`  (Agoda roomToken o:2;p:0)
    token_room = None
    for vs in low.values():
        v = vs[0]
        if v and re.search(r"[a-z]{1,6}:[^;:]+;[a-z]{1,6}:", v, re.I):
            kv = {}
            for part in v.split(";"):
                if ":" in part:
                    k, _, val = part.partition(":")
                    kv[k.strip().lower()] = val.strip()
            a = _kv_int(kv, ("o", "occ", "adults", "adult", "adt", "a"))
            c = _kv_int(kv, ("p", "ch", "children", "child", "chd"))
            if a:
                token_room = Room(a, c or 0)
                break

    # D. aggregate — adults= / children= / rooms= / nrN=
    ad = _first_int(low, _ADULT_KEYS)
    ch = _first_int(low, _CHILD_KEYS)
    rm = _first_int(low, _ROOM_KEYS)
    ages = _collect_ages(low)
    nr_total = sum(int(vs[0]) for k, vs in low.items()
                   if re.fullmatch(r"n?r\d+", k) and vs and vs[0].isdigit())
    rm = rm or (nr_total or None)

    if token_room and rm:
        out.append(OccSignal("url:token+rooms", rooms=rm,
                             per_room=[Room(token_room.adults, token_room.children)
                                       for _ in range(min(rm, _MAX_ROOMS))],
                             confidence=0.9))
    elif ad or (rm and rm > 0):
        out.append(OccSignal("url:aggregate", rooms=rm, adults=ad, children=ch,
                             child_ages=ages, confidence=0.8))

    # E. packed guest strings
    for key in _PACKED_KEYS:
        for v in low.get(key, []):
            sig = _parse_packed(v)
            if sig:
                out.append(sig)

    return out


def _decode_room_letters(v: str) -> "Room | None":
    """Booking: 'A' per adult, a trailing bare number per child's age.
    'A,A,7' -> 2 adults + 1 child aged 7."""
    if not re.fullmatch(r"[Aa](%s[Aa])*(%s\d{1,2})*" % ("[,|]", "[,|]"), v or ""):
        return None
    toks = re.split(r"[,|]", v)
    adults = sum(1 for t in toks if t.lower() == "a")
    ages = [int(t) for t in toks if t.isdigit()]
    return Room(adults, len(ages), ages)


def _decode_rm_codes(v: str) -> "Room | None":
    """Expedia: 'a2:c5:c3' -> 2 adults + 2 children aged 5 and 3."""
    if not v or "a" not in v.lower():
        return None
    m = re.search(r"a(\d+)", v, re.I)
    if not m:
        return None
    ages = [int(x) for x in re.findall(r"c(\d+)", v, re.I)]
    return Room(int(m.group(1)), len(ages), ages)


def _kv_int(kv: dict, keys) -> int | None:
    for k in keys:
        if k in kv and kv[k].lstrip("-").isdigit():
            return int(kv[k])
    return None


# ── reconciler ────────────────────────────────────────────────────────

def even_split(rooms: int, adults: int, children: int = 0,
               ages: list | None = None, default_age: int = 12) -> list:
    """Distribute totals across `rooms` as evenly as possible; the remainder
    goes to the first rooms. Deterministic."""
    rooms = max(1, min(rooms, _MAX_ROOMS))
    ba, ea = divmod(max(adults, rooms), rooms)          # >= 1 adult / room
    bc, ec = divmod(max(children, 0), rooms)
    ages = list(ages or [])
    ages += [default_age] * max(0, children - len(ages))
    out, ai = [], 0
    for i in range(rooms):
        a = ba + (1 if i < ea else 0)
        c = bc + (1 if i < ec else 0)
        out.append({"adults": a, "children": c, "child_ages": ages[ai:ai + c]})
        ai += c
    return out


def _occ_repr(occ: list) -> str:
    return " ; ".join(
        f"R{i+1} {r['adults']}A" + (f"{r['children']}C" if r['children'] else "")
        + (f"({','.join(map(str, r['child_ages']))})" if r.get('child_ages') else "")
        for i, r in enumerate(occ))


def resolve(signals: list, *, llm_rooms=None, llm_adults=None, llm_children=None,
            llm_occupancy=None, default_child_age: int = 12):
    """Reconcile all occupancy signals into one per-room list.

    Returns (occupancy: list[dict], confidence: float, source: str, notes: list).
    `occupancy == []` means it genuinely could not be determined -> the caller
    should FAIL the request.
    """
    signals = list(signals)
    notes: list = []

    if llm_occupancy:
        rooms = []
        for r in llm_occupancy:
            r = r if isinstance(r, dict) else {}
            ages = [int(a) for a in (r.get("child_ages") or []) if str(a).lstrip("-").isdigit()]
            ch = r.get("children")
            ch = int(ch) if str(ch).lstrip("-").isdigit() else len(ages)
            ad = r.get("adults")
            rooms.append(Room(int(ad) if str(ad).lstrip("-").isdigit() else 1, ch, ages))
        signals.append(OccSignal("llm:per_room", rooms=len(rooms), per_room=rooms,
                                 confidence=0.85))
    elif llm_adults or llm_rooms:
        signals.append(OccSignal("llm:aggregate", rooms=llm_rooms, adults=llm_adults,
                                 children=llm_children, confidence=0.8))

    if not signals:
        return [], 0.0, "none", ["no occupancy signal in the URL or the page"]

    # 1. a per-room signal wins outright
    per_room_sigs = [s for s in signals if s.per_room]
    if per_room_sigs:
        best = max(per_room_sigs, key=lambda s: s.confidence)
        occ = [r.dict() for r in best.per_room]
        # cross-check the total against any other signal
        others = [s for s in signals if s is not best
                  and s.total_adults() is not None]
        mine_a = sum(r["adults"] for r in occ)
        for s in others:
            if s.total_adults() and s.total_adults() != mine_a:
                notes.append(f"occupancy: {best.origin} gives {mine_a} adult(s), "
                             f"{s.origin} gives {s.total_adults()} — used {best.origin}")
        return occ, best.confidence, best.origin, notes

    # 2. aggregates only -> even split
    rm = _pick(signals, "rooms") or 1
    ad = _pick(signals, "adults")
    ch = _pick(signals, "children") or 0
    ages = next((s.child_ages for s in signals if s.child_ages), [])
    if ad:
        occ = even_split(rm, ad, ch, ages, default_child_age)
        conf = 0.5 if (rm > 1) else 0.72        # single room -> no real ambiguity
        if rm > 1:
            notes.append(
                f"per-room guest split not shown anywhere — assumed an even "
                f"distribution: {_occ_repr(occ)}. TripJack pricing depends on "
                f"this; confirm before booking.")
        if ch and not ages:
            notes.append(f"child age(s) not shown — assumed {default_child_age}")
        return occ, conf, "even_split", notes

    if rm and not ad:
        return [], 0.0, "rooms_only", [
            f"{rm} room(s) found but no adult count — cannot build occupancy"]
    return [], 0.0, "none", ["occupancy could not be determined "
                             "(need rooms + adults, or a per-room split)"]


def _pick(signals: list, attr: str) -> int | None:
    """Highest-confidence non-null value for a total across all signals."""
    vals = [(s.confidence, getattr(s, attr) if attr != "adults" else s.total_adults()
             if s.total_adults() is not None else s.adults)
            for s in signals]
    vals = [(c, v) for c, v in vals if v]
    return max(vals)[1] if vals else None
