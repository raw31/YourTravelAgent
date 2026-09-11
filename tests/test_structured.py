"""Structured-field resolver — well-shaped values pulled straight out of
captured JSON, before the LLM ever runs."""
from yta.structured import extract_structured


def _blob(kind, body):
    return {"url": "https://x/api/booking", "kind": kind, "body": body}


def test_resolves_iso_dates_currency_price_hotel_room_from_a_request_payload():
    xhr = [_blob("request", {
        "checkIn": "2026-09-17", "checkOut": "2026-09-18",
        "currency": "INR", "totalPrice": 66714,
        "hotelName": "InterContinental Bangkok Sukhumvit by IHG",
        "roomName": "Premium Room, 1 King Bed",
        "view": "City View", "bedType": "King",
    })]
    found = extract_structured(xhr)
    assert found["stay.check_in"].value == "2026-09-17"
    assert found["stay.check_out"].value == "2026-09-18"
    assert found["ota_benchmark.currency"].value == "INR"
    assert found["ota_benchmark.final_payable"].value == 66714.0
    assert found["hotel.name"].value == "InterContinental Bangkok Sukhumvit by IHG"
    assert found["requested_offer.room_name"].value == "Premium Room, 1 King Bed"
    assert found["requested_offer.view"].value == "City View"
    assert found["requested_offer.bed_type"].value == "King"


def test_epoch_millis_date():
    # 2026-09-17T00:00:00Z
    xhr = [_blob("response", {"checkInDate": 1789603200000})]
    found = extract_structured(xhr)
    assert found["stay.check_in"].value == "2026-09-17"


def test_rejects_ambiguous_and_unknown_shapes():
    xhr = [_blob("response", {
        "checkIn": "17/09/2026",          # ambiguous slash format — not accepted
        "currency": "XXX",                # not a real ISO-4217 code
        "hotelId": "336672",              # numeric id, not a name
        "roomType": "abc-def-123",        # slug-like, not a display name
    })]
    found = extract_structured(xhr)
    assert "stay.check_in" not in found
    assert "ota_benchmark.currency" not in found
    assert "hotel.name" not in found
    assert "requested_offer.room_name" not in found


def test_price_prefers_largest_total_like_figure_over_a_subtotal():
    xhr = [_blob("response", {
        "basePrice": 100,                 # doesn't match _PRICE_KEYS at all
        "subTotal": 200,
        "totalAmount": 66714,
    })]
    found = extract_structured(xhr)
    assert found["ota_benchmark.final_payable"].value == 66714.0


def test_scans_json_ld_too():
    ld = [{"@type": "Hotel", "checkinTime": "2026-09-17", "currency": "USD"}]
    # checkinTime isn't in the key family (deliberately — it's a time-of-day
    # field on schema.org, not the stay date) but currency still resolves
    found = extract_structured([], ld)
    assert found.get("ota_benchmark.currency", None)
    assert found["ota_benchmark.currency"].value == "USD"


def test_empty_input_returns_empty():
    assert extract_structured([], []) == {}
    assert extract_structured(None, None) == {}
