"""Routing, schema, occupancy, merge/validate — no network, no LLM."""
import datetime as dt

import pytest

from yta.pipeline import extract, _norm_occ, _apply
from yta.profiles import route
from yta.schema import BookingIntent, Source, Stay, RoomOccupancy, validate


BOOKING_URL = ("https://secure.booking.com/book.html?hotel_id=1194299"
               "&checkin=2026-09-21&interval=1&room1=A%2CA&rt_selected_total_price=15390")
AGODA_URL = "https://www.agoda.com/en-in/book/?roomName=One+Bedroom&isEasyCancel=false"
MMT_URL = ("https://www.makemytrip.com/hotels/hotel-review/?checkin=09032026"
           "&checkout=09052026&hotelId=201409261648028150&rsc=1e2e0e")


# -- routing ------------------------------------------------------

@pytest.mark.parametrize("url,name", [
    (BOOKING_URL, "booking"),
    (AGODA_URL, "agoda"),
    (MMT_URL, "makemytrip"),
    ("https://www.trivago.com/hotel/xyz", "trivago"),
    ("https://www.cleartrip.com/hotels/itinerary/abc/info", "cleartrip"),
    ("https://in.hotels.com/x", "hotels"),
])
def test_routing_labels_by_domain(url, name):
    assert route(url).name == name


def test_route_rejects_relative():
    with pytest.raises(ValueError):
        route("book.html?checkin=2026-09-21")


# -- occupancy --------------------------------------------------

def test_occupancy_aggregates_sync():
    s = Stay()
    s.set_occupancy([{"adults": 2, "children": 1, "child_ages": [3]},
                     {"adults": 2, "children": 1, "child_ages": [2]}])
    assert s.rooms == 2 and s.adults == 4 and s.children == 2
    assert s.child_ages == [3, 2]
    assert len(s.occupancy) == 2 and isinstance(s.occupancy[0], RoomOccupancy)


def test_norm_occ_coerces_strings_and_missing_children():
    rooms = _norm_occ([{"adults": "2", "child_ages": [3]}, {"adults": 2, "children": 0}])
    assert rooms == [{"adults": 2, "children": 1, "child_ages": [3]},
                     {"adults": 2, "children": 0, "child_ages": []}]


def test_apply_sets_occupancy_from_llm():
    class _Res:
        provider = "test"
        model = "m"
        fields = {"stay.occupancy": [{"adults": 2, "children": 1, "child_ages": [3]},
                                     {"adults": 2, "children": 1, "child_ages": [2]}]}
        confidence = {}
        contradictions = []
    p = BookingIntent(source=Source(ota="mmt", url="x"))
    _apply(p, _Res())
    assert p.stay.rooms == 2 and p.stay.adults == 4
    assert any(e.field == "stay.occupancy" for e in p.evidence)


def test_apply_plain_fields_and_contradictions():
    class _Res:
        provider = "test"
        model = "m"
        fields = {"hotel.name": "Aloha on the Ganges", "ota_benchmark.final_payable": 18160}
        confidence = {"hotel.name": 0.9}
        contradictions = ["stay.check_in: url said 2026-09-21, page shows 2026-09-22"]
    p = BookingIntent(source=Source(ota="booking", url="x"))
    _apply(p, _Res())
    assert p.hotel.name == "Aloha on the Ganges"
    assert p.ota_benchmark.final_payable == 18160
    assert any("LLM flagged" in w for w in p.warnings)


# -- extract() without LLM key -> URL-only, graceful ------------

def test_extract_no_key_is_graceful(monkeypatch):
    for v in ("GROQ_API_KEY", "GROK_API_KEY", "OPENAI_API_KEY",
              "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "LLM_PROVIDER"):
        monkeypatch.delenv(v, raising=False)
    p = extract(MMT_URL, render=False)
    assert p.source.ota == "makemytrip"
    assert any("No LLM key" in w for w in p.warnings)


# -- validation ------------------------------------------------

def test_validate_flags_today_default_date():
    today = dt.date.today()
    p = BookingIntent(source=Source(ota="x", url="https://x"))
    p.stay.check_in = today.isoformat()
    assert any("today's date" in w for w in validate(p, today=today))


def test_validate_flags_room_child_age_mismatch():
    p = BookingIntent(source=Source(ota="x", url="https://x"))
    p.stay.set_occupancy([{"adults": 2, "children": 2, "child_ages": [5]}])
    w = validate(p, today=dt.date(2026, 1, 1))
    assert any("age(s) given" in x for x in w)


def test_clean_text_defangs_markup():
    from yta.schema import clean_text
    assert clean_text("  <script>Deluxe\n Room</script> ") == "scriptDeluxe Room/script"


# -- mandatory-field gate -------------------------------------

def test_mandatory_fail_lists_missing():
    p = BookingIntent(source=Source(ota="agoda", url="x"))
    p.requested_offer.room_name = "Standard Room City View"
    p.ota_benchmark.final_payable = 16862.02
    missing = p.check_mandatory()
    assert p.status == "fail"
    assert set(missing) == {
        "hotel.name", "stay.check_in", "stay.check_out", "stay.rooms",
        "stay.occupancy", "requested_offer.room_detail"}


def test_room_detail_satisfied_by_bed_type_alone():
    p = BookingIntent(source=Source(ota="agoda", url="x"))
    p.hotel.name = "X"
    p.stay.check_in, p.stay.check_out = "2026-09-21", "2026-09-22"
    p.stay.set_occupancy([{"adults": 2, "children": 0, "child_ages": []}])
    p.requested_offer.room_name = "Standard Room"
    p.requested_offer.bed_type = "1 king bed"          # no prose description
    p.ota_benchmark.final_payable = 5000
    assert "requested_offer.room_detail" not in p.check_mandatory()


def test_mandatory_ok_when_all_present():
    p = BookingIntent(source=Source(ota="booking", url="x"))
    p.hotel.name = "Aloha on the Ganges"
    p.stay.check_in, p.stay.check_out = "2026-09-21", "2026-09-22"
    p.stay.set_occupancy([{"adults": 2, "children": 0, "child_ages": []}])
    p.requested_offer.room_name = "One Bedroom Standard Apartment"
    p.requested_offer.description = "Extra-large double bed, garden view"
    p.ota_benchmark.final_payable = 18160
    assert p.check_mandatory() == []
    assert p.status == "ok"


# -- page_data (Chrome extension capture) --------------------------

def test_page_data_builds_context_and_skips_render(monkeypatch):
    for v in ("GROQ_API_KEY", "GROK_API_KEY", "OPENAI_API_KEY",
              "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "LLM_PROVIDER"):
        monkeypatch.delenv(v, raising=False)

    def _boom(*a, **kw):
        raise AssertionError("render() must not be called when page_data is given")
    monkeypatch.setattr("yta.pipeline.render_page", _boom)

    page_data = {
        "text": "Aloha on the Ganges — Deluxe Villa — 2 adults — INR 41,126",
        "html": "",
        "json_ld": [],
        "xhr_json": [{"url": "https://x/api/prebook", "kind": "request",
                      "body": {"rooms": [{"adults": 2, "children": 0}]}}],
        "final_url": BOOKING_URL,
    }
    p = extract(BOOKING_URL, render=True, page_data=page_data)
    assert p.source.rendered is True                     # extension counts as "rendered"
    assert any("browser extension" in l["msg"] for l in p.run_log)
    assert any("prepared" in l["msg"] and "chars of page content" in l["msg"]
              for l in p.run_log)                         # context was built, not skipped
