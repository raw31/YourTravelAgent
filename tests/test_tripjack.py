"""TripJack Detail/Pricing client — offline (fixture / stub client)."""
import json
from pathlib import Path

import pytest

from yta.tripjack.client import TripJackClient, TripJackError
from yta.tripjack.hotel import (hotel_options, rooms_payload, pricing_request,
                                pricing_request_from_packet)

FIX = json.loads((Path(__file__).parent / "fixtures" / "tj_pricing_sample.json").read_text())


class _StubClient(TripJackClient):
    """Records the pricing() request, returns the fixture."""
    def __init__(self, resp=None, err=None):
        super().__init__(api_key="stub", env="test")
        self._resp, self._err, self.last_body = resp or FIX, err, None

    def pricing(self, **kw):
        self.last_body = kw
        if self._err:
            raise self._err
        return self._resp


# -- rooms payload ---------------------------------------------

def test_rooms_payload_from_dicts():
    rooms = rooms_payload([
        {"adults": 2, "children": 1, "child_ages": [3]},
        {"adults": 2, "children": 1, "child_ages": [2]},
    ])
    assert rooms == [
        {"adults": 2, "children": 1, "childAge": [3]},
        {"adults": 2, "children": 1, "childAge": [2]},
    ]


def test_rooms_payload_defaults_adults_and_ages():
    assert rooms_payload([{"children": 1}]) == [
        {"adults": 2, "children": 1, "childAge": [10]}]
    assert rooms_payload([]) == [{"adults": 2}]


# -- hotel_options normalisation -----------------------------

def test_pricing_request_body():
    req = pricing_request("10000000012345", "2026-09-02", "2026-09-03",
                          [{"adults": 2, "children": 1, "child_ages": [3]},
                           {"adults": 2, "children": 1, "child_ages": [2]}],
                          currency="INR")
    assert req["method"] == "POST"
    assert req["url"] == "https://apitest-hms.tripjack.com/hms/v3/hotel/pricing"
    assert req["headers"]["apikey"] == "<TRIPJACK_API_KEY>"
    b = req["body"]
    assert b["hid"] == "10000000012345"
    assert b["checkIn"] == "2026-09-02" and b["checkOut"] == "2026-09-03"
    assert b["currency"] == "INR" and b["nationality"] == "106"
    assert b["rooms"] == [
        {"adults": 2, "children": 1, "childAge": [3]},
        {"adults": 2, "children": 1, "childAge": [2]},
    ]
    assert b["correlationId"]


def test_pricing_request_from_packet():
    from yta.schema import BookingIntent, Source
    p = BookingIntent(source=Source(ota="mmt", url="x"))
    p.stay.check_in, p.stay.check_out = "2026-09-02", "2026-09-03"
    p.stay.set_occupancy([{"adults": 2, "children": 1, "child_ages": [3]},
                          {"adults": 2, "children": 1, "child_ages": [2]}])
    p.ota_benchmark.currency = "INR"
    b = pricing_request_from_packet(p, "100001137288")["body"]
    assert b["hid"] == "100001137288"
    assert b["checkIn"] == "2026-09-02"
    assert b["rooms"] == [
        {"adults": 2, "children": 1, "childAge": [3]},
        {"adults": 2, "children": 1, "childAge": [2]},
    ]


def test_pricing_request_from_packet_needs_dates():
    from yta.schema import BookingIntent, Source
    p = BookingIntent(source=Source(ota="agoda", url="x"))
    with pytest.raises(ValueError):
        pricing_request_from_packet(p, "1")


def test_hotel_options_sends_expected_kwargs():
    c = _StubClient()
    hotel_options("10000000012345", "2026-09-02", "2026-09-03",
                  [{"adults": 2, "children": 1, "child_ages": [3]}],
                  currency="INR", client=c)
    assert c.last_body["hid"] == "10000000012345"
    assert c.last_body["check_in"] == "2026-09-02"
    assert c.last_body["rooms"] == [{"adults": 2, "children": 1, "childAge": [3]}]
    assert c.last_body["nationality"] == "106"


def test_hotel_options_normalises_options():
    d = hotel_options("10000000012345", "2026-09-21", "2026-09-22",
                      [{"adults": 2}], client=_StubClient())
    assert d.hotel_name == "Pride Plaza Hotel Aerocity New Delhi"
    assert d.review_hash == "abc123def456"
    assert len(d.options) == 2

    bf, ro = d.options
    assert bf.meal_basis == "Breakfast"
    assert bf.total_price == 27806.62
    assert bf.base_price == 25000.0 and bf.taxes == 2500.0
    assert bf.mgmt_fee == 250.62 and bf.mgmt_fee_tax == 56.0
    assert bf.commercial_type == "COMMISSIONABLE" and bf.commission == 1800.0
    assert bf.pan_required is True
    assert bf.refundable is True
    assert bf.free_cancel_until == "2026-09-18T23:59:59"
    assert bf.strikethrough == 31000.0

    assert ro.meal_basis == "Room Only"
    assert ro.refundable is False
    assert ro.free_cancel_until is None
    assert ro.commercial_type == "NET"


def test_hotel_options_empty_options_note():
    empty = {"hotelName": "X", "reviewHash": "h", "options": [], "status": {"success": True}}
    d = hotel_options("1", "2026-09-21", "2026-09-22", [{"adults": 2}],
                      client=_StubClient(resp=empty))
    assert d.options == []
    assert any("no bookable options" in n for n in d.notes)


def test_error_propagates():
    c = _StubClient(err=TripJackError("INVALID_HOTEL_ID", "nope", 400))
    with pytest.raises(TripJackError) as ei:
        hotel_options("1", "2026-09-21", "2026-09-22", [{"adults": 2}], client=c)
    assert ei.value.code == "INVALID_HOTEL_ID"


# -- client config -----------------------------------------

def test_client_unconfigured(monkeypatch):
    monkeypatch.delenv("TRIPJACK_API_KEY", raising=False)
    assert TripJackClient(api_key="").configured() is False


def test_session_dead_flag():
    assert TripJackError("SEARCH_SESSION_EXPIRED", "x").session_dead is True
    assert TripJackError("INVALID_HOTEL_ID", "x").session_dead is False
