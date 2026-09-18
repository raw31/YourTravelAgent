"""Room + rate-plan mapping: OTA `requested_offer`  ->  TripJack pricing options.

    ratekey = room_type  x  meal_plan  x  isRefundable

Pipeline (mirrors the production MMT `find_room` + `build_rate_plan_options`):

  1. group TJ options into buckets by roomInfo[].id  (TJ pre-clusters name noise)
  2. split view off the OTA room name; score it against every bucket
     (max over the bucket's name variants) with RoomNormalizationService
  3. eligibility gate (>= MIN_BASE_SCORE); pick max
       - top-2 within LLM_TIEBREAK_DELTA  -> LLM tie-breaker
       - nothing clears the gate but best is in the 'good' band -> LLM confirm
  4. view check — flag-and-ignore when TJ carries no view info
  5. filter the chosen bucket's options by meal plan, then by isRefundable
  6. tag every surviving option (cheapest / matches-benchmark / perk:* /
     refundable) and return them ALL

No network. The verified-mapping cache is a separate follow-up.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict

from yta.roommap.normalize import (
    CONFIG, RoomMatchConfig, RoomNormalizationService,
    split_name_and_view, views_match,
)
from yta.roommap.meal import meal_to_tj, meal_rank

# the default matching policy (mirrors yta.schema.DEFAULT_MATCHING_POLICY)
_DEFAULT_POLICY = {"meal": "same_or_better", "cancellation": "same_or_better"}

# most-specific bed keyword wins — OTAs write "1 extra-large double bed (King)"
# where the real bed is King and "double" is just a size descriptor
_BED_PRIORITY = ("king", "queen", "twin", "double", "single", "sofabed",
                 "bunkbed", "rollaway")


def _bed_kw(text: str | None) -> str:
    if not text:
        return ""
    toks = set(re.findall(r"[a-z]+", text.lower()))
    return next((b for b in _BED_PRIORITY if b in toks), "")


_PERKS = [
    (re.compile(r"hi[\s-]?tea", re.I), "hi-tea"),
    (re.compile(r"\bspa\b", re.I), "spa"),
    (re.compile(r"laundry", re.I), "laundry"),
    (re.compile(r"discount.*(food|f&b|beverage)", re.I), "discount-fnb"),
    (re.compile(r"car service", re.I), "car-service"),
]


# -- output types ----------------------------------------------------------

@dataclass
class RateOption:
    option_id: str
    room_type_id: str
    room_name: str
    meal_basis: str
    refundable: bool
    total_price: float
    currency: str
    option_type: str = ""        # SRSM/SRCM/CRSM/CRCM; "" where unknown/unset
    tags: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


@dataclass
class RoomBucket:
    room_type_id: str
    name_variants: list
    canonical: str            # the variant that scored best
    view: str | None          # view split from `canonical`, if any
    score: float
    band: str
    breakdown: str
    n_options: int

    def to_dict(self):
        return asdict(self)


@dataclass
class RoomMapResult:
    matched: bool
    room_type_id: str | None
    band: str                          # strong / good / weak / none
    score: float | None
    rate_options: list                 # list[RateOption] — all surviving, tagged
    ranked_buckets: list               # list[RoomBucket] — every bucket, best first
    meal_filter: str | None            # requested meal, as a TJ mealBasis value
    refundable_filter: bool | None     # requested refundable flag
    view_flag: str | None
    llm_used: bool
    notes: list = field(default_factory=list)
    ratekey_option_ids: list = field(default_factory=list)  # options that match the rate plan

    def to_dict(self):
        d = asdict(self)
        d["rate_options"] = [r.to_dict() if isinstance(r, RateOption) else r
                             for r in self.rate_options]
        d["ranked_buckets"] = [b.to_dict() if isinstance(b, RoomBucket) else b
                               for b in self.ranked_buckets]
        return d

    def best_option(self):
        """The option to prebook: cheapest rate-plan match, else cheapest
        option of the matched room. None when nothing matched."""
        if not self.rate_options:
            return None
        pool = [o for o in self.rate_options
                if o.option_id in (self.ratekey_option_ids or [])] or self.rate_options
        return min(pool, key=lambda o: o.total_price)


# -- option-row adapter (accepts raw TJ dicts or SupplierOption) ----------

def _rows(options) -> list[dict]:
    out = []
    for o in options or []:
        if hasattr(o, "option_id"):                       # SupplierOption
            rooms = o.rooms or []
            row = {
                "option_id": o.option_id, "meal_basis": o.meal_basis or "",
                "refundable": bool(o.refundable),
                "total_price": float(o.total_price or 0),
                "currency": o.currency or "",
                "option_type": o.option_type or "",
            }
        else:                                            # raw pricing dict
            rooms = o.get("roomInfo") or []
            pr = o.get("pricing") or {}
            row = {
                "option_id": o.get("optionId", ""),
                "meal_basis": o.get("mealBasis", "") or "",
                "refundable": bool((o.get("cancellation") or {}).get("isRefundable")),
                "total_price": float(pr.get("totalPrice") or 0),
                "currency": pr.get("currency", "") or "",
                "option_type": o.get("optionType", "") or "",
            }
        ids = sorted(str(r.get("id")) for r in rooms if r.get("id"))
        row["room_type_id"] = "+".join(ids) or "?"
        row["room_name"] = " + ".join(str(r.get("name") or "").strip()
                                      for r in rooms) or "(unnamed)"
        out.append(row)
    return out


def _perks(name: str) -> list[str]:
    low = name or ""
    return [tag for rx, tag in _PERKS if rx.search(low)]


def _json_obj(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    return json.loads(m.group(0)) if m else {}


# -- LLM tie-breakers (best-effort; any failure -> fall back to score) ----

def _llm_tiebreak(offer, buckets) -> "RoomBucket | None":
    from yta import llm
    listing = "\n".join(f"{i}: {b.canonical}" for i, b in enumerate(buckets))
    system = ("You map a booked hotel room to the matching supplier room. "
              "Reply with compact JSON only.")
    user = (f"Guest booked:\n  room_name: {offer.room_name!r}\n"
            f"  bed_type: {offer.bed_type!r}\n"
            f"  description: {(offer.description or '')[:400]!r}\n\n"
            f"Supplier rooms:\n{listing}\n\n"
            'Return {"pick": <index>} for the one that is the same room, '
            'or {"pick": null} if none clearly is.')
    try:
        txt, *_ = llm.complete(system, user, max_tokens=120)
        i = _json_obj(txt).get("pick")
        if isinstance(i, int) and 0 <= i < len(buckets):
            return buckets[i]
    except Exception:
        pass
    return None


def _llm_confirm(offer, bucket) -> bool:
    from yta import llm
    system = ("You verify whether two hotel room descriptions refer to the "
              "same physical room. Reply with compact JSON only.")
    user = (f"Booked room: {offer.room_name!r} (bed {offer.bed_type!r}). "
            f"Supplier room: {bucket.canonical!r}.\n"
            'Return {"same": true} or {"same": false}.')
    try:
        txt, *_ = llm.complete(system, user, max_tokens=60)
        return bool(_json_obj(txt).get("same"))
    except Exception:
        return False


# -- entry point ---------------------------------------------------------

def map_rooms(options, offer, *, benchmark_price: float | None = None,
              policy: dict | None = None,
              config: RoomMatchConfig | None = None, use_llm: bool = True,
              log=None) -> RoomMapResult:
    """`options`   : TJ pricing options (raw dicts or SupplierOption list)
    `offer`      : yta.schema.Offer  (requested_offer from the packet)
    `benchmark_price` : ota_benchmark.final_payable, for the matches-benchmark tag
    `policy`     : matching_policy dict — `meal` / `cancellation` each
                   "exact" or "same_or_better" (default: same_or_better)
    """
    cfg = config or CONFIG
    pol = {**_DEFAULT_POLICY, **(policy or {})}
    svc = RoomNormalizationService(cfg)
    notes: list[str] = []

    def _log(m):
        if log:
            log(m)

    rows = _rows(options)
    if not rows:
        return RoomMapResult(False, None, "none", None, [], [], None, None, None,
                             False, ["TripJack returned no options to map against"])

    # 1. buckets by room_type_id
    buckets_rows: dict[str, list[dict]] = {}
    for r in rows:
        buckets_rows.setdefault(r["room_type_id"], []).append(r)
    _log(f"{len(rows)} options -> {len(buckets_rows)} room-type bucket(s)")

    # 2. score
    if not offer or not offer.room_name:
        return RoomMapResult(False, None, "none", None, [],
                             _dump_buckets(buckets_rows, svc), None, None, None,
                             False, ["no OTA room_name to match — cannot map"])

    q_base, q_view = split_name_and_view(offer.room_name)
    if not q_view and getattr(offer, "view", None):
        q_view = offer.view
    q_aug = q_base
    bed_kw = _bed_kw(getattr(offer, "bed_type", None))
    if bed_kw and bed_kw not in q_base.lower():
        q_aug = f"{q_base} {bed_kw}"

    scored: list[RoomBucket] = []
    for rtid, brows in buckets_rows.items():
        variants = sorted({r["room_name"] for r in brows})
        best_s, best_var, best_expl = -1.0, variants[0], ""
        for v in variants:
            vb, _ = split_name_and_view(v)
            a = svc.analyze_similarity(q_aug, vb)
            if a["final_similarity"] > best_s:
                best_s, best_var, best_expl = a["final_similarity"], v, a["explanation"]
        _, bv = split_name_and_view(best_var)
        scored.append(RoomBucket(
            room_type_id=rtid, name_variants=variants, canonical=best_var,
            view=bv, score=round(best_s, 4), band=svc.recommend(best_s),
            breakdown=best_expl, n_options=len(brows)))

    scored.sort(key=lambda b: b.score, reverse=True)
    for b in scored[:5]:
        _log(f"  {b.score:.3f} [{b.band}] {b.canonical!r}  ({b.n_options} opt)")

    # 3. gate + LLM
    llm_used = False
    eligible = [b for b in scored if b.score >= cfg.MIN_BASE_SCORE]
    chosen: RoomBucket | None = None
    if eligible:
        top = eligible[0]
        close = [b for b in eligible if top.score - b.score <= cfg.LLM_TIEBREAK_DELTA]
        if len(close) > 1 and use_llm:
            _log(f"  {len(close)} buckets within {cfg.LLM_TIEBREAK_DELTA} — LLM tie-break")
            pick = _llm_tiebreak(offer, close)
            llm_used = True
            chosen = pick or top
            if pick:
                notes.append(f"LLM tie-break picked {pick.canonical!r}")
            else:
                notes.append("LLM tie-break inconclusive — took the top score")
        else:
            chosen = top
        band = svc.recommend(chosen.score)
    elif scored and scored[0].score >= cfg.THRESHOLD_GOOD and use_llm:
        _log(f"  best {scored[0].score:.3f} below gate but in 'good' band — LLM confirm")
        llm_used = True
        if _llm_confirm(offer, scored[0]):
            chosen = scored[0]
            band = "good"
            notes.append(f"LLM confirmed {scored[0].canonical!r} despite score "
                         f"{scored[0].score:.2f} < {cfg.MIN_BASE_SCORE}")
        else:
            band = "none"
            notes.append(f"closest room {scored[0].canonical!r} scored "
                         f"{scored[0].score:.2f} (need {cfg.MIN_BASE_SCORE}); "
                         f"LLM did not confirm a match")
    else:
        band = scored[0].band if scored else "none"
        if scored:
            notes.append(f"closest room {scored[0].canonical!r} scored "
                         f"{scored[0].score:.2f} (need {cfg.MIN_BASE_SCORE})")

    if not chosen:
        return RoomMapResult(False, None, band, scored[0].score if scored else None,
                             [], scored, None, None, None, llm_used, notes)

    _log(f"matched room_type_id {chosen.room_type_id} — {chosen.canonical!r} "
         f"(score {chosen.score:.3f}, {band})")

    # 4. view check — flag & ignore
    view_flag = None
    if q_view:
        if not chosen.view:
            view_flag = (f"OTA requested {q_view!r} view, but TripJack room names "
                         f"carry no view info — view check skipped")
        elif not views_match(q_view, chosen.view):
            view_flag = (f"OTA requested {q_view!r} view; matched TripJack room "
                         f"reads {chosen.view!r} — kept anyway (TJ view text is "
                         f"unreliable)")
        if view_flag:
            notes.append(view_flag)
            _log(f"  view: {view_flag}")

    # 5. keep EVERY option of the matched room_type_id — meal / cancellation
    #    are annotated, never used to drop a row.
    sel = list(buckets_rows[chosen.room_type_id])
    req_meal = meal_to_tj(offer.meal_plan)
    req_rank = meal_rank(req_meal) if req_meal else -1
    req_ref = offer.refundable
    meal_pol, canc_pol = pol["meal"], pol["cancellation"]

    def _ratekey_ok(r) -> bool:
        """Does this option satisfy the requested rate plan under the policy?"""
        if req_meal:
            rr = meal_rank(r["meal_basis"])
            if meal_pol == "same_or_better":
                if rr < req_rank:
                    return False
            elif meal_to_tj(r["meal_basis"]) != req_meal:
                return False
        if req_ref is not None:
            if req_ref and not r["refundable"]:
                return False                       # they wanted free cancellation
            if not req_ref and r["refundable"] is False:
                pass
            if not req_ref and r["refundable"] and canc_pol == "exact":
                return False
        return True

    cheapest = min((r["total_price"] for r in sel), default=None)
    rate_options, ratekey_ids = [], []
    for r in sel:
        tags = ["refundable" if r["refundable"] else "non-refundable"]
        rr = meal_rank(r["meal_basis"])
        if req_meal:
            if meal_to_tj(r["meal_basis"]) == req_meal:
                tags.append("meal:exact")
            elif rr > req_rank:
                tags.append("meal:better")
            elif rr >= 0:
                tags.append("meal:lower")
        if req_ref is not None and r["refundable"] == req_ref:
            tags.append("cancel:exact")
        elif req_ref is False and r["refundable"]:
            tags.append("cancel:better")       # free cancellation upgrade
        ok = _ratekey_ok(r)
        if ok:
            tags.append("ratekey-match")
            ratekey_ids.append(r["option_id"])
        if cheapest is not None and r["total_price"] == cheapest:
            tags.append("cheapest")
        if benchmark_price and benchmark_price > 0 \
                and abs(r["total_price"] - benchmark_price) / benchmark_price <= 0.02:
            tags.append("matches-benchmark")
        tags += [f"perk:{p}" for p in _perks(r["room_name"])]
        rate_options.append((ok, RateOption(
            option_id=r["option_id"], room_type_id=r["room_type_id"],
            room_name=r["room_name"], meal_basis=r["meal_basis"],
            refundable=r["refundable"], total_price=r["total_price"],
            currency=r["currency"], option_type=r.get("option_type", ""), tags=tags)))

    # rate-plan matches first, then by price
    rate_options.sort(key=lambda t: (not t[0], t[1].total_price))
    rate_options = [ro for _, ro in rate_options]

    notes.append(f"{len(sel)} option(s) for room_type_id {chosen.room_type_id}"
                 + (f" — {len(ratekey_ids)} match the requested rate plan "
                    f"(meal {req_meal}"
                    + (f", {'refundable' if req_ref else 'non-refundable'}" if req_ref is not None else "")
                    + ")" if req_meal or req_ref is not None else ""))
    _log(f"  {len(sel)} option(s) for the matched room; {len(ratekey_ids)} match the rate plan")

    return RoomMapResult(
        matched=True, room_type_id=chosen.room_type_id, band=band,
        score=chosen.score, rate_options=rate_options, ranked_buckets=scored,
        meal_filter=req_meal, refundable_filter=req_ref, view_flag=view_flag,
        llm_used=llm_used, notes=notes, ratekey_option_ids=ratekey_ids)


# -- no-requested-room path: list up to 4 representative options ---------

# TripJack's real optionType codes, in the fixed display/selection order.
OPTION_TYPES = ("SRSM", "SRCM", "CRSM", "CRCM")


@dataclass
class RateOptionsByType:
    """Result of list_by_option_type() — the no-requested-room path.
    `options` holds 0-4 RateOption, at most one per OPTION_TYPES entry,
    ordered SRSM, SRCM, CRSM, CRCM (skipping any type TripJack returned none
    of — this is 'up to 4', never padded, never fabricated)."""
    options: list                    # list[RateOption]
    types_found: list                # e.g. ["SRSM", "CRCM"]
    types_missing: list              # e.g. ["SRCM", "CRSM"]
    notes: list = field(default_factory=list)

    def to_dict(self):
        d = asdict(self)
        d["options"] = [o.to_dict() if isinstance(o, RateOption) else o
                        for o in self.options]
        return d


def list_by_option_type(options) -> RateOptionsByType:
    """Entry point for the no-requested-room-name case. `options`: TJ pricing
    options (raw dicts or SupplierOption list — same input shape map_rooms()
    accepts). Groups by optionType and returns the cheapest option within
    each of the (up to) 4 real TripJack optionType codes. No matching, no
    scoring, no LLM — there is nothing to match against."""
    rows = _rows(options)
    if not rows:
        return RateOptionsByType([], [], list(OPTION_TYPES),
                                 ["TripJack returned no options to list"])

    by_type: dict = {}
    unknown = 0
    for r in rows:
        t = r.get("option_type") or ""
        if t not in OPTION_TYPES:
            unknown += 1
            continue
        by_type.setdefault(t, []).append(r)

    out, found, missing = [], [], []
    for t in OPTION_TYPES:
        rows_t = by_type.get(t)
        if not rows_t:
            missing.append(t)
            continue
        found.append(t)
        # deterministic tie-break: cheapest, then lowest option_id string
        cheapest = min(rows_t, key=lambda r: (r["total_price"], r["option_id"]))
        out.append(RateOption(
            option_id=cheapest["option_id"], room_type_id=cheapest["room_type_id"],
            room_name=cheapest["room_name"], meal_basis=cheapest["meal_basis"],
            refundable=cheapest["refundable"], total_price=cheapest["total_price"],
            currency=cheapest["currency"], option_type=t, tags=["cheapest-in-type"]))

    notes = [f"{len(rows)} option(s) -> {len(found)}/{len(OPTION_TYPES)} "
             f"optionType(s) represented"]
    if missing:
        notes.append(f"TripJack returned no {', '.join(missing)} option "
                     f"for this hotel/stay/occupancy")
    if unknown:
        notes.append(f"{unknown} option(s) had an unrecognised/blank "
                     f"optionType — excluded")
    return RateOptionsByType(out, found, missing, notes)


def _dump_buckets(buckets_rows, svc) -> list:
    out = []
    for rtid, brows in buckets_rows.items():
        variants = sorted({r["room_name"] for r in brows})
        out.append(RoomBucket(rtid, variants, variants[0], None, 0.0, "none",
                              "", len(brows)))
    return out
