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


def test_no_room_name_path_logs_a_summary_line(monkeypatch):
    _wire_common_mocks(monkeypatch, _MIXED_TYPE_OPTIONS)
    pkt = _packet(room_name=None)
    _resolve(pkt)
    assert any("no requested room name" in l["msg"] and "2 room" in l["msg"]
               for l in pkt.run_log)
