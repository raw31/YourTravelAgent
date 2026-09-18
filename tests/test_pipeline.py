"""Routing, schema, occupancy, merge/validate — no network, no LLM."""
import datetime as dt

import pytest

from yta import llm
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


# -- currency normalization -------------------------------------------

def test_normalize_currency_maps_common_symbols_to_iso():
    from yta.schema import normalize_currency
    assert normalize_currency("Rs") == "INR"
    assert normalize_currency("Rs.") == "INR"
    assert normalize_currency("₹") == "INR"
    assert normalize_currency("rupees") == "INR"
    assert normalize_currency("usd") == "USD"
    assert normalize_currency("$") == "USD"
    assert normalize_currency("INR") == "INR"          # already clean -- unchanged
    assert normalize_currency(None) is None


def test_packet_add_normalizes_currency_on_write():
    p = BookingIntent(source=Source(ota="agoda", url="x"))
    p.add("ota_benchmark.currency", "Rs.", "llm", 0.8)
    assert p.ota_benchmark.currency == "INR"


def test_currency_mismatch_from_symbol_no_longer_blocks_comparison():
    # this is the exact bug: a price screenshot showing "Rs. 31,683" used to
    # leave ota_benchmark.currency as "Rs." (or unset), which never matched
    # TripJack's "INR" and silently killed the price-comparison message.
    p = BookingIntent(source=Source(ota="agoda", url="x"))
    p.add("ota_benchmark.currency", "Rs.", "llm", 0.8)
    assert p.ota_benchmark.currency.upper() == "INR".upper()


# -- mandatory-field gate -------------------------------------

def test_mandatory_fail_lists_missing():
    p = BookingIntent(source=Source(ota="agoda", url="x"))
    p.requested_offer.room_name = "Standard Room City View"
    p.ota_benchmark.final_payable = 16862.02
    missing = p.check_mandatory()
    assert p.status == "fail"
    assert set(missing) == {
        "hotel.name", "stay.check_in", "stay.check_out", "stay.rooms",
        "stay.occupancy"}


def test_room_detail_is_not_mandatory():
    # description/bed_type/view are deliberately optional now — confirmed
    # unused for anything essential: TripJack's pricing call needs only
    # hid/dates/occupancy/currency, and room matching's real gate is
    # offer.room_name alone (description/bed_type only feed the OPTIONAL
    # LLM tie-break helpers). A packet with none of the three should still
    # be otherwise-complete/"ok", not blocked waiting on this.
    p = BookingIntent(source=Source(ota="agoda", url="x"))
    p.hotel.name = "X"
    p.stay.check_in, p.stay.check_out = "2026-09-21", "2026-09-22"
    p.stay.set_occupancy([{"adults": 2, "children": 0, "child_ages": []}])
    p.requested_offer.room_name = "Standard Room"
    p.ota_benchmark.final_payable = 5000
    assert p.requested_offer.description is None
    assert p.requested_offer.bed_type is None
    assert p.requested_offer.view is None
    missing = p.check_mandatory()
    assert missing == []
    assert p.status == "ok"


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


def test_room_name_is_not_mandatory():
    # A customer who genuinely never named a room (hotel + dates +
    # occupancy + price only) is a legitimate request shape now, not a
    # failed extraction -- _resolve() forks on this (see test_web_resolve.py)
    # and lists representative options instead of matching one specific
    # room. No room_name at all should still be a complete, "ok" packet.
    p = BookingIntent(source=Source(ota="booking", url="x"))
    p.hotel.name = "Aloha on the Ganges"
    p.stay.check_in, p.stay.check_out = "2026-09-21", "2026-09-22"
    p.stay.set_occupancy([{"adults": 2, "children": 0, "child_ages": []}])
    p.ota_benchmark.final_payable = 18160
    assert p.requested_offer.room_name is None
    assert p.check_mandatory() == []
    assert p.status == "ok"


def test_room_name_absent_and_something_else_missing_still_fails_on_that():
    # room_name must never itself appear in missing_mandatory, in either
    # outcome -- but a packet missing something that's still genuinely
    # mandatory (price, here) should still fail on THAT.
    p = BookingIntent(source=Source(ota="booking", url="x"))
    p.hotel.name = "Aloha on the Ganges"
    p.stay.check_in, p.stay.check_out = "2026-09-21", "2026-09-22"
    p.stay.set_occupancy([{"adults": 2, "children": 0, "child_ages": []}])
    missing = p.check_mandatory()
    assert p.status == "fail"
    assert missing == ["ota_benchmark.final_payable"]


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


def test_structured_data_skips_the_llm_entirely(monkeypatch):
    monkeypatch.setattr("yta.pipeline.extract_llm.extract",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("LLM must not be called — everything was resolvable")))
    page_data = {
        "text": "",
        "html": "",
        "json_ld": [],
        "xhr_json": [{"url": "https://secure.booking.com/api/prebook", "kind": "request", "body": {
            "checkIn": "2026-09-21", "checkOut": "2026-09-22",
            "currency": "INR", "totalPrice": 15390,
            "hotelName": "Aloha on the Ganges",
            "roomName": "Deluxe Room", "view": "River View", "bedType": "King",
        }}],
        "final_url": BOOKING_URL,
    }
    p = extract(BOOKING_URL, render=True, page_data=page_data)
    assert p.status == "ok", p.missing_mandatory
    assert p.hotel.name == "Aloha on the Ganges"
    assert p.stay.check_in == "2026-09-21" and p.stay.check_out == "2026-09-22"
    assert p.ota_benchmark.currency == "INR" and p.ota_benchmark.final_payable == 15390
    assert p.requested_offer.room_name == "Deluxe Room"
    assert any("skipping the LLM call" in l["msg"] for l in p.run_log)


def test_llm_cannot_overwrite_a_protected_structured_field(monkeypatch):
    # even if an LLM pass DID run (e.g. one field still missing), it must not
    # clobber a value the structured resolver already set with high confidence
    calls = []

    class _FakeRes:
        fields = {"hotel.name": "WRONG NAME FROM LLM",
                  "requested_offer.description": "a lovely room"}
        confidence = {}
        provider, model = "fake", "fake"
        contradictions = []

    def _fake_extract(*a, **kw):
        calls.append(1)
        return _FakeRes()

    monkeypatch.setattr("yta.pipeline.extract_llm.extract", _fake_extract)
    page_data = {
        "text": "", "html": "", "json_ld": [],
        "xhr_json": [{"url": "x", "kind": "request", "body": {
            "hotelName": "Aloha on the Ganges",
        }}],
        "final_url": BOOKING_URL,
    }
    p = extract(BOOKING_URL, render=True, page_data=page_data)
    assert len(calls) >= 1                       # LLM did run (room_name/dates still missing)
    assert p.hotel.name == "Aloha on the Ganges"  # but did NOT overwrite the protected field


def test_generation_failed_falls_back_to_next_provider(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setenv("GOOGLE_API_KEY", "AIza_test")

    class _GoodRes:
        provider, model = "gemini", "gemini-flash-latest"
        fields = {"hotel.name": "Aloha on the Ganges",
                  "requested_offer.room_name": "Deluxe Room",
                  "requested_offer.description": "Garden view"}
        confidence = {}
        contradictions = []

    calls = []

    def _fake_extract(context, url="", media=None, provider=None):
        calls.append(provider)
        if provider == "groq":
            raise llm.GenerationFailed("Failed to validate JSON.")
        return _GoodRes()

    monkeypatch.setattr("yta.pipeline.extract_llm.extract", _fake_extract)
    p = extract(BOOKING_URL, render=False)
    assert calls == ["groq", "gemini"]                    # fell through, didn't crash
    assert p.hotel.name == "Aloha on the Ganges"
    assert any("bad generation" in w for w in p.warnings)


# -- currency default for a typed-out/spoken submission -------------------

def test_missing_currency_on_free_text_defaults_to_account_currency(monkeypatch):
    # Live bug: a Hinglish free-text message ("...18000 me...") captured
    # the price but not a currency (no symbol was ever typed), silently
    # losing the whole OTA-vs-TripJack savings comparison downstream.
    monkeypatch.setenv("TRIPJACK_CURRENCY", "INR")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")

    class _FakeRes:
        fields = {"hotel.name": "Taj Santacruz",
                  "stay.check_in": "2026-09-21", "stay.check_out": "2026-09-22",
                  "stay.rooms": 1, "stay.occupancy": [{"adults": 2, "children": 0}],
                  "requested_offer.room_name": "Luxury Room",
                  "ota_benchmark.final_payable": 18000}      # no currency field at all
        confidence = {}
        provider, model = "fake", "fake"
        contradictions = []

    monkeypatch.setattr("yta.pipeline.extract_llm.extract", lambda *a, **kw: _FakeRes())
    p = extract("", render=False, page_text="taj santacruz 18000 me")
    assert p.ota_benchmark.final_payable == 18000
    assert p.ota_benchmark.currency == "INR"
    assert any("defaulting to INR" in l["msg"] for l in p.run_log)


def test_missing_currency_default_respects_a_non_inr_account_currency(monkeypatch):
    monkeypatch.setenv("TRIPJACK_CURRENCY", "AED")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")

    class _FakeRes:
        fields = {"hotel.name": "Some Hotel", "ota_benchmark.final_payable": 500}
        confidence = {}
        provider, model = "fake", "fake"
        contradictions = []

    monkeypatch.setattr("yta.pipeline.extract_llm.extract", lambda *a, **kw: _FakeRes())
    p = extract("", render=False, page_text="some hotel 500 please")
    assert p.ota_benchmark.currency == "AED"


def test_currency_default_never_applies_to_a_url_or_media_submission(monkeypatch):
    # The default is specifically for a typed-out/spoken submission with
    # nothing else to go on -- a real OTA page or screenshot reliably shows
    # its own currency, so a genuine miss there should stay unresolved
    # rather than being silently papered over with a guess.
    monkeypatch.setenv("TRIPJACK_CURRENCY", "INR")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")

    class _FakeRes:
        fields = {"hotel.name": "Aloha on the Ganges",
                  "requested_offer.room_name": "Deluxe Room",
                  "ota_benchmark.final_payable": 15390}     # no currency, same as above
        confidence = {}
        provider, model = "fake", "fake"
        contradictions = []

    monkeypatch.setattr("yta.pipeline.extract_llm.extract", lambda *a, **kw: _FakeRes())
    p = extract(BOOKING_URL, render=False)   # a real URL this time, no page_text
    assert p.ota_benchmark.final_payable == 15390
    assert p.ota_benchmark.currency is None
    assert not any("defaulting to" in l["msg"] for l in p.run_log)


def test_currency_default_does_not_override_a_real_extracted_currency(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")

    class _FakeRes:
        fields = {"hotel.name": "Some Hotel", "ota_benchmark.final_payable": 500,
                  "ota_benchmark.currency": "AED"}
        confidence = {}
        provider, model = "fake", "fake"
        contradictions = []

    monkeypatch.setattr("yta.pipeline.extract_llm.extract", lambda *a, **kw: _FakeRes())
    p = extract("", render=False, page_text="dubai hotel 500 aed please")
    assert p.ota_benchmark.currency == "AED"   # the real extracted value, not the INR default
