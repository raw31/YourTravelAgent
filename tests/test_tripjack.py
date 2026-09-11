"""TripJack Detail/Pricing client — offline (fixture / stub client)."""
import json
from pathlib import Path

import pytest

from yta.tripjack.client import TripJackClient, TripJackError
from yta.tripjack.hotel import (hotel_options, rooms_payload, pricing_request,
                                pricing_request_from_packet, review_option,
                                review_request, review_from_detail)

FIX = json.loads((Path(__file__).parent / "fixtures" / "tj_pricing_sample.json").read_text())
REVIEW_FIX = json.loads((Path(__file__).parent / "fixtures" / "tj_review_sample.json").read_text())


class _StubClient(TripJackClient):
    """Records the pricing()/review() request, returns the fixture."""
    def __init__(self, resp=None, err=None, review_resp=None):
        super().__init__(api_key="stub", env="test")
        self._resp, self._err, self.last_body = resp or FIX, err, None
        self._review_resp = review_resp or REVIEW_FIX
        self.last_review = None

    def pricing(self, **kw):
        self.last_body = kw
        if self._err:
            raise self._err
        return self._resp

    def review(self, **kw):
        self.last_review = kw
        if self._err:
            raise self._err
        return self._review_resp


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
    assert req["url"] == "https://hms-search.tripjack.com/hms/v3/hotel/pricing"
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


def test_pricing_request_from_packet_ignores_the_otas_currency(monkeypatch):
    # An OTA can display in any currency (a UAE hotel in AED, a US site in
    # USD, ...). Sending that straight to TripJack gets rejected with
    # "[6533] Currency <X> is not supported for this account" the moment it
    # doesn't match the account's provisioned currency — so the request
    # must use the account's fixed currency, never packet.ota_benchmark.
    from yta.schema import BookingIntent, Source
    monkeypatch.delenv("TRIPJACK_CURRENCY", raising=False)
    p = BookingIntent(source=Source(ota="agoda", url="x"))
    p.stay.check_in, p.stay.check_out = "2026-09-02", "2026-09-03"
    p.stay.set_occupancy([{"adults": 2, "children": 0, "child_ages": []}])
    p.ota_benchmark.currency = "AED"          # the OTA page showed AED
    b = pricing_request_from_packet(p, "1")["body"]
    assert b["currency"] == "INR"             # default account currency, not AED

    monkeypatch.setenv("TRIPJACK_CURRENCY", "SGD")
    b = pricing_request_from_packet(p, "1")["body"]
    assert b["currency"] == "SGD"             # honours an explicit account override

    b = pricing_request_from_packet(p, "1", currency="USD")["body"]
    assert b["currency"] == "USD"             # explicit currency= wins over the env var


def test_client_currency_defaults_and_env_override(monkeypatch):
    monkeypatch.delenv("TRIPJACK_CURRENCY", raising=False)
    assert TripJackClient.from_env().currency == "INR"
    monkeypatch.setenv("TRIPJACK_CURRENCY", "AED")
    assert TripJackClient.from_env().currency == "AED"
    assert TripJackClient(currency="").currency == "INR"     # never blank


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


# -- review / prebook -------------------------------------

def test_review_request_body():
    req = review_request("100000297299", "opt-123", "hash-abc",
                         correlation_id="corr-1")
    assert req["method"] == "POST"
    assert req["url"].endswith("/hms/v3/hotel/review")
    assert req["body"] == {"correlationId": "corr-1", "optionId": "opt-123",
                           "reviewHash": "hash-abc", "hid": "100000297299"}


def test_review_option_normalises_and_flags_price_move():
    c = _StubClient()
    rv = review_option("100000297299", "opt-x", "hash-y", correlation_id="corr-9",
                       expected_price=40689.44, client=c)
    assert c.last_review == {"correlation_id": "corr-9", "option_id": "opt-x",
                             "review_hash": "hash-y", "hid": "100000297299"}
    assert rv.booking_id == "TJ2092185633018"
    assert rv.onhold_allowed is True
    assert rv.deadline == "2026-11-08T09:59:59"
    assert rv.option.total_price == 41200.0
    assert rv.option.meal_basis == "Breakfast"
    assert rv.price_changed is True
    assert rv.price_delta == round(41200.0 - 40689.44, 2)
    assert any("price moved" in n for n in rv.notes)


def test_review_option_price_held_when_matching():
    rv = review_option("1", "o", "h", expected_price=41200.0, client=_StubClient())
    assert rv.price_changed is False and rv.price_delta == 0.0
    assert rv.notes == []


def test_review_from_detail_uses_detail_context():
    det = hotel_options("10000000012345", "2026-09-02", "2026-09-03",
                        [{"adults": 2}], client=_StubClient())
    oid = det.options[0].option_id
    c = _StubClient()
    rv = review_from_detail(det, oid, client=c)
    assert c.last_review["review_hash"] == det.review_hash
    assert c.last_review["correlation_id"] == det.correlation_id
    assert c.last_review["option_id"] == oid
    assert rv.booking_id == "TJ2092185633018"


def test_review_error_propagates():
    c = _StubClient(err=TripJackError("OPTION_SOLD_OUT", "gone", 200))
    with pytest.raises(TripJackError) as ei:
        review_option("1", "o", "h", client=c)
    assert ei.value.code == "OPTION_SOLD_OUT"


def test_find_option_matches_by_signature_not_id():
    from yta.tripjack.hotel import find_option, _option_rtid
    det = hotel_options("10000000012345", "2026-09-02", "2026-09-03",
                        [{"adults": 2}], client=_StubClient())
    rt = _option_rtid(det.options[0])           # the fixture's room signature
    # fixture has two options for that room: Breakfast/refundable and
    # Room Only/non-refundable
    o = find_option(det, rt, "Breakfast", True)
    assert o is not None and o.meal_basis == "Breakfast" and o.refundable
    o2 = find_option(det, rt, "Room Only", False)
    assert o2 is not None and not o2.refundable
    # relaxation still returns something for the room when meal doesn't match
    assert find_option(det, rt, "Nonexistent Meal", True) is not None
    assert find_option(det, "99999", "Breakfast", True) is None


# -- client config -----------------------------------------

def test_client_unconfigured(monkeypatch):
    monkeypatch.delenv("TRIPJACK_API_KEY", raising=False)
    assert TripJackClient(api_key="").configured() is False


def test_session_dead_flag():
    assert TripJackError("SEARCH_SESSION_EXPIRED", "x").session_dead is True
    assert TripJackError("INVALID_HOTEL_ID", "x").session_dead is False
