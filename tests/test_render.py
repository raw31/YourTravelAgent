"""render.py helpers — _json_digest, llm_context (no browser)."""
from yta.render import RenderResult, _json_digest


# a realistic slice of an OTA itinerary SPA payload: the per-room guest split
# and child ages live deep in the tree, behind a "Details" click on the page.
SPA_BODY = {
    "pageData": {"ravenTracking": {"mergeData": {
        "h_hotel_name": "InterContinental Bangkok",
        "h_room_count": "2", "h_adults_count": "4", "h_child_count": "3"}}},
    "slotsData": [
        {"slotData": {"type": "COUPON_LIST", "data": {
            "couponCode": "CTLOGIN", "discountType": "COUPON",
            "supercoinBalance": 5000}}},
        {"slotData": {"type": "CANCELLATION_POLICY", "data": {
            "heading": "Cancellation policy",
            "subHeading": "This special discounted rate is non-refundable."}}},
    ],
    "subPages": {"guestInfo": {"slotsData": [{"slotData": {
        "type": "ITINERARY_GUEST_INFO", "data": {"roomGuests": [
            {"roomName": "Room 1", "adultString": "2 adults",
             "childrenString": "2 Children", "childrenAgesString": "3 yrs, 2 yrs"},
            {"roomName": "Room 2", "adultString": "2 adults",
             "childrenString": "1 Child", "childrenAgesString": "2 yrs"},
        ]}}}]}},
}


def test_json_digest_surfaces_deep_per_room_split():
    d = _json_digest([{"url": "x", "body": SPA_BODY}], 4000)
    assert "roomGuests[0].adultString = 2 adults" in d
    assert "childrenAgesString = 3 yrs, 2 yrs" in d
    assert "roomGuests[1].childrenString = 1 Child" in d
    # noise is dropped
    assert "coupon" not in d.lower() and "supercoin" not in d.lower()
    # cancellation policy kept
    assert "non-refundable" in d.lower()


def test_json_digest_priority_orders_room_split_first():
    d = _json_digest([{"url": "x", "body": SPA_BODY}], 4000)
    lines = d.splitlines()
    gi = next(i for i, l in enumerate(lines) if "roomGuests[0]" in l)
    canc = next(i for i, l in enumerate(lines) if "non-refundable" in l.lower())
    assert gi < canc            # high-value fields float up


def test_json_digest_caps_length():
    big = {"rooms": [{"roomName": f"Room {i}", "adultString": "2 adults"}
                     for i in range(500)]}
    assert len(_json_digest([{"url": "x", "body": big}], 800)) <= 800


def test_llm_context_includes_api_digest():
    rr = RenderResult(url="u", final_url="u", text="x " * 300,
                      xhr_json=[{"url": "x", "body": SPA_BODY}])
    ctx = rr.llm_context()
    assert "CAPTURED API DATA" in ctx
    assert "roomGuests[0].adultString = 2 adults" in ctx


def test_llm_context_no_xhr_is_fine():
    rr = RenderResult(url="u", final_url="u", text="hello world " * 50)
    ctx = rr.llm_context()
    assert "CAPTURED API DATA" not in ctx
    assert "hello world" in ctx
