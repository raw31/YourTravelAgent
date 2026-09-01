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
    meal_filter: str | None            # TJ mealBasis we filtered on
    refundable_filter: bool | None
    view_flag: str | None
    llm_used: bool
    notes: list = field(default_factory=list)

    def to_dict(self):
        d = asdict(self)
        d["rate_options"] = [r.to_dict() if isinstance(r, RateOption) else r
                             for r in self.rate_options]
        d["ranked_buckets"] = [b.to_dict() if isinstance(b, RoomBucket) else b
                               for b in self.ranked_buckets]
        return d


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

    # 5. meal + refundable filter — honour the matching policy
    sel = list(buckets_rows[chosen.room_type_id])
    tj_meal = meal_to_tj(offer.meal_plan)
    meal_pol = pol["meal"]
    if tj_meal:
        want = meal_rank(tj_meal)
        if meal_pol == "same_or_better":
            keep = [r for r in sel if meal_rank(r["meal_basis"]) >= want]
        else:
            keep = [r for r in sel if r["meal_basis"] == tj_meal]
        if keep:
            sel = keep
        else:
            notes.append(f"no {tj_meal!r}"
                         f"{'-or-better' if meal_pol == 'same_or_better' else ''} "
                         f"option for this room — showing all meal plans")
            tj_meal = None
    _log(f"  meal filter [{meal_pol}]: {offer.meal_plan!r} -> {tj_meal!r}  ({len(sel)} left)")

    ref_filter = offer.refundable
    canc_pol = pol["cancellation"]
    if offer.refundable is not None:
        if offer.refundable is False and canc_pol == "same_or_better":
            # a free-cancellation rate is an upgrade over a non-refundable
            # request — keep everything, tag the upgrades below
            keep = sel
        else:
            keep = [r for r in sel if r["refundable"] == offer.refundable]
        if keep:
            sel = keep
        else:
            notes.append(f"no {'refundable' if offer.refundable else 'non-refundable'} "
                         f"option for this room/meal — showing all")
            ref_filter = None
    _log(f"  cancellation filter [{canc_pol}]: {offer.refundable} -> {len(sel)} option(s)")

    # 6. tag + return all
    want_meal = meal_rank(meal_to_tj(offer.meal_plan)) if offer.meal_plan else -1
    cheapest = min((r["total_price"] for r in sel), default=None)
    rate_options = []
    for r in sorted(sel, key=lambda r: r["total_price"]):
        tags = ["refundable" if r["refundable"] else "non-refundable"]
        if cheapest is not None and r["total_price"] == cheapest:
            tags.append("cheapest")
        if benchmark_price and benchmark_price > 0 \
                and abs(r["total_price"] - benchmark_price) / benchmark_price <= 0.02:
            tags.append("matches-benchmark")
        if want_meal >= 0 and meal_rank(r["meal_basis"]) > want_meal:
            tags.append("meal:better")
        if offer.refundable is False and r["refundable"]:
            tags.append("cancel:better")
        tags += [f"perk:{p}" for p in _perks(r["room_name"])]
        rate_options.append(RateOption(
            option_id=r["option_id"], room_type_id=r["room_type_id"],
            room_name=r["room_name"], meal_basis=r["meal_basis"],
            refundable=r["refundable"], total_price=r["total_price"],
            currency=r["currency"], tags=tags))

    return RoomMapResult(
        matched=True, room_type_id=chosen.room_type_id, band=band,
        score=chosen.score, rate_options=rate_options, ranked_buckets=scored,
        meal_filter=tj_meal, refundable_filter=ref_filter, view_flag=view_flag,
        llm_used=llm_used, notes=notes)


def _dump_buckets(buckets_rows, svc) -> list:
    out = []
    for rtid, brows in buckets_rows.items():
        variants = sorted({r["room_name"] for r in brows})
        out.append(RoomBucket(rtid, variants, variants[0], None, 0.0, "none",
                              "", len(brows)))
    return out
