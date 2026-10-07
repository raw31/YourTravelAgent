"""yta.web._resolve() — the fork between the existing single-room
map_rooms() path and the list_cheapest_rooms() path (no requested room
name). No network: yta.hoteldb.db.db_path, yta.hoteldb.link.resolve_packet,
yta.tripjack.client.TripJackClient.from_env, and yta.tripjack.hotel.hotel_options
are all faked.
"""
from pathlib import Path

from yta.schema import BookingIntent, Source
from yta.tripjack.client import TripJackClient
from yta.web import _resolve


def _packet(room_name=None):
    pkt = BookingIntent(source=Source(
        ota="test", url="", page_type="hotel_review",
        extraction_method="test", extracted_at="2026-01-01T00:00:00+00:00"))
    pkt.hotel.name = "Test Hotel"
    pkt.stay.check_in = "2026-09-21"
    pkt.stay.check_out = "2026-09-22"
    pkt.stay.occupancy = [{"adults": 2, "children": 0, "child_ages": []}]
    pkt.requested_offer.room_name = room_name
    pkt.ota_benchmark.final_payable = 18000
    pkt.ota_benchmark.currency = "INR"
    return pkt


def _opt(rtid, name, meal, refundable, price, oid, option_type="SRSM"):
    return {
        "optionId": oid, "optionType": option_type,
        "roomInfo": [{"id": rtid, "name": name, "adults": 2, "children": 0}],
        "mealBasis": meal,
        "pricing": {"totalPrice": price, "currency": "INR"},
        "cancellation": {"isRefundable": refundable},
    }


_MIXED_TYPE_OPTIONS = [
    _opt("R1", "Deluxe Room", "Breakfast", True, 20000, "s1", "SRSM"),
    _opt("R1", "Deluxe Room", "Half Board", True, 22000, "c1", "SRCM"),
    _opt("R2", "Premier Room", "Breakfast", True, 25000, "c2", "CRSM"),
]


class _FakeMatch:
    tj_id = "12345"
    unica_id = "u1"
    hotel_name = "Test Hotel (TripJack)"
    score = 0.95

    def to_dict(self):
        return {"tj_id": self.tj_id, "unica_id": self.unica_id,
                "hotel_name": self.hotel_name, "score": self.score}


class _FakeResolveResult:
    def __init__(self, match=None, band="high"):
        self.match = match
        self.band = band
        self.layers = []
        self.query = {}
        self.candidates = []
        self.notes = []

    def to_dict(self):
        return {"query": self.query, "band": self.band, "layers": self.layers,
                "match": self.match.to_dict() if self.match else None,
                "candidates": [], "notes": self.notes}


class _FakeClient:
    currency = "INR"

    def configured(self):
        return True


class _FakeDetail:
    def __init__(self, options):
        self.options = options
        self.check_in = "2026-09-21"
        self.check_out = "2026-09-22"
        self.rooms_query = [{"adults": 2, "children": 0, "childAges": []}]
        self.currency = "INR"
        self.notes = []

    def to_dict(self):
        return {"options": self.options}


def _wire_common_mocks(monkeypatch, options):
    monkeypatch.setattr("yta.hoteldb.db.db_path", lambda: Path(__file__))  # any file that exists
    monkeypatch.setattr("yta.hoteldb.link.resolve_packet",
                         lambda packet, **kw: _FakeResolveResult(_FakeMatch()))
    monkeypatch.setattr(TripJackClient, "from_env", classmethod(lambda cls: _FakeClient()))
    monkeypatch.setattr("yta.tripjack.hotel.hotel_options",
                         lambda *a, **kw: _FakeDetail(options))


def test_no_room_name_routes_to_list_cheapest_rooms(monkeypatch):
    _wire_common_mocks(monkeypatch, _MIXED_TYPE_OPTIONS)
    d = _resolve(_packet(room_name=None))
    assert "room_map" not in d
    assert "room_options" in d
    groups = d["room_options"]["groups"]
    # 2 distinct rooms, ordered by each room's own cheapest price: R1
    # (s1, c1 -- both real combos) then R2 (c2).
    assert [g["room_type_id"] for g in groups] == ["R1", "R2"]
    assert groups[0]["room_name"] == "Deluxe Room"
    assert [o["option_id"] for o in groups[0]["options"]] == ["s1", "c1"]
    assert [o["option_id"] for o in groups[1]["options"]] == ["c2"]


def test_room_name_present_is_completely_unaffected(monkeypatch):
    _wire_common_mocks(monkeypatch, _MIXED_TYPE_OPTIONS)
    d = _resolve(_packet(room_name="Deluxe Room"))
    assert "room_options" not in d
    assert "room_map" in d
    assert d["room_map"]["matched"] is True
    assert d["room_map"]["room_type_id"] == "R1"
    assert "our_price" in d["room_map"]


def test_prebook_ctx_present_and_identical_in_both_paths(monkeypatch):
    _wire_common_mocks(monkeypatch, _MIXED_TYPE_OPTIONS)
    d_no_room = _resolve(_packet(room_name=None))
    d_with_room = _resolve(_packet(room_name="Deluxe Room"))
    assert d_no_room["prebook_ctx"] == d_with_room["prebook_ctx"]
    assert d_no_room["prebook_ctx"]["tj_id"] == "12345"


def test_no_options_at_all_sets_neither_key(monkeypatch):
    _wire_common_mocks(monkeypatch, [])
    d = _resolve(_packet(room_name=None))
    assert "room_map" not in d
    assert "room_options" not in d
    d2 = _resolve(_packet(room_name="Deluxe Room"))
    assert "room_map" not in d2
    assert "room_options" not in d2


_AMBIGUOUS_ROOM_OPTIONS = [
    _opt("R5", "Grand Suite", "Breakfast", True, 60000, "o10"),
    _opt("R6", "Grand Suite", "Room Only", False, 55000, "o11"),
]


def test_ambiguous_room_match_falls_back_to_room_options(monkeypatch):
    # Real bug found live: "Luxury room" matched ONE of 9 differently-
    # priced "Luxury..." rooms at a hotel confidently, so the customer
    # got compared against (and told "nothing better than") a single
    # possibly-wrong, possibly-pricier room. Two identically-named
    # buckets is the deterministic way to force map_rooms()'s own
    # `ambiguous` flag (see tests/test_roommap.py for that unit test).
    _wire_common_mocks(monkeypatch, _AMBIGUOUS_ROOM_OPTIONS)
    d = _resolve(_packet(room_name="Grand Suite"))
    assert "room_map" not in d
    assert "room_options" in d
    groups = d["room_options"]["groups"]
    assert {g["room_type_id"] for g in groups} == {"R5", "R6"}


def test_ambiguous_match_reorders_nearest_match_first(monkeypatch):
    # The algorithm's own best guess should lead the list, then the rest
    # in the usual cheapest-first order -- not just whatever order
    # list_cheapest_rooms() would have picked on price alone. Uses the
    # view-stripping ambiguity path (deterministic, no LLM tie-break)
    # with the matched room priced in the MIDDLE, not first or last, to
    # prove this is a real reorder and not a coincidence of price order.
    opts = [
        _opt("R1", "Budget Room", "Room Only", False, 10000, "b1"),
        _opt("R2", "Luxury Room Facade View", "Room Only", False, 15000, "o1"),
        _opt("R3", "LUXURY, COURTYARD VIEW", "Room Only", False, 20000, "o2"),
    ]
    _wire_common_mocks(monkeypatch, opts)
    d = _resolve(_packet(room_name="Luxury room"))
    assert "room_map" not in d
    groups = d["room_options"]["groups"]
    # Price order alone would be Budget(10k), Facade View(15k), Courtyard(20k)
    # -- the matched room (Courtyard, the ambiguous pick) must lead anyway.
    assert groups[0]["room_type_id"] == "R3"
    assert [g["room_type_id"] for g in groups[1:]] == ["R1", "R2"]   # rest stay price-ordered


def test_ambiguous_match_not_in_cheapest_five_is_prepended_anyway(monkeypatch):
    # If the matched room didn't make the cheapest-5 cut on price alone,
    # it must still appear -- never silently dropped just for being
    # expensive.
    opts = [_opt(f"B{i}", f"Budget Room {i}", "Room Only", False, 5000 + i * 100, f"b{i}")
            for i in range(1, 6)]   # 5 cheap rooms, filling the cap
    opts += [
        _opt("R2", "Luxury Room Facade View", "Room Only", False, 30000, "o1"),
        _opt("R3", "LUXURY, COURTYARD VIEW", "Room Only", False, 40000, "o2"),
    ]
    _wire_common_mocks(monkeypatch, opts)
    d = _resolve(_packet(room_name="Luxury room"))
    groups = d["room_options"]["groups"]
    assert len(groups) == 6                      # the usual 5 + the prepended match
    assert groups[0]["room_type_id"] == "R3"      # the ambiguous match, prepended first
    assert [g["room_type_id"] for g in groups[1:]] == ["B1", "B2", "B3", "B4", "B5"]


def test_ambiguous_match_room_options_carry_the_ambiguous_flag(monkeypatch):
    opts = [
        _opt("R1", "Budget Room", "Room Only", False, 10000, "b1"),
        _opt("R2", "Luxury Room Facade View", "Room Only", False, 15000, "o1"),
        _opt("R3", "LUXURY, COURTYARD VIEW", "Room Only", False, 20000, "o2"),
    ]
    _wire_common_mocks(monkeypatch, opts)
    d = _resolve(_packet(room_name="Luxury room"))
    ro = d["room_options"]
    assert ro.get("ambiguous_match") is True
    assert ro.get("nearest_match_room_type_id") == "R3"


def test_no_room_name_path_logs_a_summary_line(monkeypatch):
    _wire_common_mocks(monkeypatch, _MIXED_TYPE_OPTIONS)
    pkt = _packet(room_name=None)
    _resolve(pkt)
    assert any("no requested room name" in l["msg"] and "2 room" in l["msg"]
               for l in pkt.run_log)


def test_room_name_that_matches_nothing_falls_back_to_the_room_list(monkeypatch):
    # Live 2026-10-07: a screenshot's room name mapped to none of the
    # hotel's rooms -> the customer hit a "couldn't find a better rate" dead
    # end even though the hotel had live rates. Now it lists the rooms.
    opts = [
        _opt("R1", "Budget Room", "Room Only", False, 10000, "b1"),
        _opt("R2", "Premier Suite", "Room Only", False, 30000, "p1"),
    ]
    _wire_common_mocks(monkeypatch, opts)
    d = _resolve(_packet(room_name="Imperial Club Ocean Terrace Zebra"))
    assert d["room_options"]["no_match"] is True
    assert "room_map" not in d
    assert len(d["room_options"]["groups"]) == 2
