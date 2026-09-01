"""Room + rate-plan mapping (yta.roommap)."""
from yta.schema import Offer
from yta.roommap import (
    map_rooms, meal_to_tj, split_name_and_view, views_match,
    RoomNormalizationService,
)


# -- meal_to_tj -----------------------------------------------------------

def test_meal_to_tj_basic():
    assert meal_to_tj("Breakfast included") == "Breakfast"
    assert meal_to_tj("Bed & Breakfast") == "Breakfast"
    assert meal_to_tj("Room only, no meals") == "Room Only"
    assert meal_to_tj("Half board") == "Half Board"
    assert meal_to_tj("breakfast and dinner") == "Half Board"
    assert meal_to_tj("Full board") == "Full Board"
    assert meal_to_tj("All-inclusive (food/beverages)") == "All Inclusive"
    assert meal_to_tj("HB") == "Half Board"
    assert meal_to_tj("RO") == "Room Only"
    assert meal_to_tj("") is None
    assert meal_to_tj(None) is None
    assert meal_to_tj("mystery plan") is None


# -- view split ---------------------------------------------------------

def test_split_name_and_view():
    assert split_name_and_view("Deluxe Room - Valley View") == ("Deluxe Room", "Valley View")
    assert split_name_and_view("Premier Villa") == ("Premier Villa", None)
    assert views_match("Valley View", "valley-view") is True
    assert views_match("Valley View", "Mountain View") is False
    assert views_match("Valley View", None) is False


def test_normalizer_order_and_synonyms():
    svc = RoomNormalizationService()
    a = svc.analyze_similarity("Deluxe Twin Room", "Twin Deluxe")
    assert a["final_similarity"] > 0.9          # word order discarded
    b = svc.analyze_similarity("Dlx King Room", "Deluxe King")
    assert b["category_std"] == "deluxe" and b["bed_std"] == "king"


# -- map_rooms fixture -------------------------------------------------

def _opt(rtid, name, meal, refundable, price, oid):
    return {
        "optionId": oid, "optionType": "SRSM",
        "roomInfo": [{"id": rtid, "name": name, "adults": 2, "children": 0}],
        "mealBasis": meal,
        "pricing": {"totalPrice": price, "currency": "INR"},
        "cancellation": {"isRefundable": refundable},
    }


OPTIONS = [
    _opt("R1", "Deluxe Villa", "Breakfast", True, 40000, "o1"),
    _opt("R1", "Deluxe Villa", "Half Board", True, 49000, "o2"),
    _opt("R1", "Deluxe Villa, Hi-Tea", "Half Board", True, 47000, "o3"),
    _opt("R1", "Deluxe Villa", "Room Only", True, 38000, "o4"),
    _opt("R2", "DELUXE, VILLA, KING BED", "Breakfast", True, 41000, "o5"),
    _opt("R2", "Deluxe Villa, 1 King Bed", "Room Only", False, 39000, "o6"),
    _opt("R3", "Premier Villa", "Breakfast", True, 50000, "o7"),
    _opt("R3", "Premier Villa", "Room Only", True, 46000, "o8"),
    _opt("R4", "Executive Villa", "Breakfast", True, 53000, "o9"),
]


def test_map_rooms_returns_every_option_of_the_matched_room():
    off = Offer(room_name="Deluxe Villa", meal_plan="Half Board", refundable=True)
    r = map_rooms(OPTIONS, off, use_llm=False)
    assert r.matched and r.band == "strong" and r.room_type_id == "R1"
    # ALL four R1 options come back, nothing dropped by meal / cancellation
    assert {ro.option_id for ro in r.rate_options} == {"o1", "o2", "o3", "o4"}
    # rate-plan matches (Half-Board-or-better + refundable) are o2, o3
    assert set(r.ratekey_option_ids) == {"o2", "o3"}
    # rate-plan matches sort first, then by price
    assert [ro.option_id for ro in r.rate_options][:2] == ["o3", "o2"]
    o3 = next(ro for ro in r.rate_options if ro.option_id == "o3")
    assert "ratekey-match" in o3.tags and "meal:exact" in o3.tags
    assert "perk:hi-tea" in o3.tags
    o1 = next(ro for ro in r.rate_options if ro.option_id == "o1")
    assert "meal:lower" in o1.tags and "ratekey-match" not in o1.tags
    # cheapest across the whole room is o4 (38000)
    o4 = next(ro for ro in r.rate_options if ro.option_id == "o4")
    assert "cheapest" in o4.tags


def test_map_rooms_bed_type_disambiguates():
    off = Offer(room_name="Deluxe Villa", bed_type="King", meal_plan="Room Only",
                refundable=False)
    r = map_rooms(OPTIONS, off, use_llm=False,
                  policy={"meal": "exact", "cancellation": "exact"})
    assert r.room_type_id == "R2"                 # King bed -> R2, not plain R1
    assert {ro.option_id for ro in r.rate_options} == {"o5", "o6"}
    assert r.ratekey_option_ids == ["o6"]         # Room Only + non-refundable


def test_map_rooms_cancellation_same_or_better():
    off = Offer(room_name="Deluxe Villa", bed_type="King", meal_plan="Room Only",
                refundable=False)
    r = map_rooms(OPTIONS, off, use_llm=False)          # default same_or_better
    assert r.room_type_id == "R2"
    o5 = next(ro for ro in r.rate_options if ro.option_id == "o5")
    assert "cancel:better" in o5.tags
    # a free-cancellation rate satisfies a non-refundable request under the policy
    assert set(r.ratekey_option_ids) == {"o5", "o6"}


def test_map_rooms_no_match_returns_ranked_buckets():
    off = Offer(room_name="Overwater Bungalow", meal_plan="Breakfast", refundable=True)
    r = map_rooms(OPTIONS, off, use_llm=False)
    assert not r.matched and r.room_type_id is None
    assert not r.rate_options
    assert r.ranked_buckets and r.ranked_buckets[0].score < 0.85


def test_map_rooms_no_ratekey_match_still_lists_all_options():
    off = Offer(room_name="Executive Villa", meal_plan="All Inclusive", refundable=True)
    r = map_rooms(OPTIONS, off, use_llm=False)
    assert r.matched and r.room_type_id == "R4"
    assert [ro.option_id for ro in r.rate_options] == ["o9"]   # the only R4 option
    assert r.ratekey_option_ids == []                          # o9 is Breakfast, below AI
    assert "meal:lower" in r.rate_options[0].tags


def test_map_rooms_view_flagged_and_ignored():
    off = Offer(room_name="Deluxe Villa - Ganges View", meal_plan="Room Only",
                refundable=True)
    r = map_rooms(OPTIONS, off, use_llm=False)
    assert r.matched and r.room_type_id == "R1"
    assert r.view_flag and "no view info" in r.view_flag


def test_map_rooms_benchmark_tag():
    off = Offer(room_name="Premier Villa", meal_plan="Room Only", refundable=True)
    r = map_rooms(OPTIONS, off, benchmark_price=46200, use_llm=False)
    o8 = next(ro for ro in r.rate_options if ro.option_id == "o8")
    assert "matches-benchmark" in o8.tags
    assert {ro.option_id for ro in r.rate_options} == {"o7", "o8"}


def test_map_rooms_meal_same_or_better_tags():
    off = Offer(room_name="Premier Villa", meal_plan="Room Only", refundable=True)
    r = map_rooms(OPTIONS, off, use_llm=False)              # default same_or_better
    assert {ro.option_id for ro in r.rate_options} == {"o7", "o8"}
    assert set(r.ratekey_option_ids) == {"o7", "o8"}        # Room-Only-or-better
    o7 = next(ro for ro in r.rate_options if ro.option_id == "o7")
    assert "meal:better" in o7.tags
    # exact policy -> only the Room Only rate is a ratekey match
    r2 = map_rooms(OPTIONS, off, use_llm=False, policy={"meal": "exact"})
    assert r2.ratekey_option_ids == ["o8"]
    assert {ro.option_id for ro in r2.rate_options} == {"o7", "o8"}   # still all listed


def test_map_rooms_accepts_supplier_options():
    from yta.tripjack.hotel import _norm_option
    sopts = [_norm_option(o) for o in OPTIONS]
    off = Offer(room_name="Deluxe Villa", meal_plan="Breakfast", refundable=True)
    r = map_rooms(sopts, off, use_llm=False, policy={"meal": "exact"})
    assert r.matched and r.room_type_id == "R1"
    assert {ro.option_id for ro in r.rate_options} == {"o1", "o2", "o3", "o4"}
    assert r.ratekey_option_ids == ["o1"]
