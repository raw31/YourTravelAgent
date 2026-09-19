"""render.py helpers — _json_digest, llm_context, _resolve_deferred_deeplink
(no browser)."""
from yta.render import RenderResult, _json_digest, _resolve_deferred_deeplink


# -- _resolve_deferred_deeplink -------------------------------------------

class _FakeResp:
    def __init__(self, url):
        self.url = url


class _FakeFinalResp(_FakeResp):
    def __init__(self, url, history):
        super().__init__(url)
        self.history = history


def test_resolve_deferred_deeplink_extracts_the_real_url_from_a_redirect_hop(monkeypatch):
    # Real live case: an AppsFlyer OneLink share link (app.mmyt.co/...)
    # redirects through an intermediate hop carrying the actual
    # destination as `deep_link_value`, before a LATER hop tries to open
    # a custom app URI that breaks Playwright entirely.
    real = "https://www.makemytrip.com/hotels/hotel-details?hotelId=123&checkin=09232026"
    from urllib.parse import quote
    hop = _FakeResp(f"https://onelink.example/x?deep_link_value={quote(real, safe='')}&other=1")
    final = _FakeFinalResp("https://apps.apple.com/in/app/makemytrip/id530488359", [hop])
    monkeypatch.setattr("requests.get", lambda *a, **kw: final)
    assert _resolve_deferred_deeplink("https://app.mmyt.co/Xm2V/in2ce1bl") == real


def test_resolve_deferred_deeplink_returns_unchanged_with_no_such_param(monkeypatch):
    final = _FakeFinalResp("https://www.booking.com/hotel/in/some-hotel.html", [])
    monkeypatch.setattr("requests.get", lambda *a, **kw: final)
    url = "https://www.booking.com/hotel/in/some-hotel.html"
    assert _resolve_deferred_deeplink(url) == url


def test_resolve_deferred_deeplink_returns_unchanged_on_network_failure(monkeypatch):
    def _boom(*a, **kw):
        raise ConnectionError("no network")
    monkeypatch.setattr("requests.get", _boom)
    url = "https://app.mmyt.co/Xm2V/in2ce1bl"
    assert _resolve_deferred_deeplink(url) == url


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


def test_llm_context_ignores_a_browser_error_page_as_final_url():
    # Real gap found live: a failed navigation still leaves final_url set
    # to Playwright's own page.url (e.g. "chrome-error://chromewebdata/"
    # after a net::ERR_HTTP2_PROTOCOL_ERROR) -- that used to win over
    # `url` in the "PAGE URL:" line fed to the LLM, discarding a
    # deep-link-resolved URL's real query params (hotelId, dates,
    # occupancy) for a useless browser-internal scheme carrying nothing.
    rr = RenderResult(url="https://www.makemytrip.com/hotels/hotel-details?hotelId=123",
                      final_url="chrome-error://chromewebdata/", text="")
    ctx = rr.llm_context()
    assert "PAGE URL: https://www.makemytrip.com/hotels/hotel-details?hotelId=123" in ctx
    assert "chrome-error" not in ctx


def test_llm_context_uses_a_real_final_url_when_navigation_succeeded():
    rr = RenderResult(url="https://short.link/x",
                      final_url="https://www.makemytrip.com/hotels/hotel-details?hotelId=123",
                      text="hello")
    ctx = rr.llm_context()
    assert "PAGE URL: https://www.makemytrip.com/hotels/hotel-details?hotelId=123" in ctx


def test_json_digest_request_payload_tagged_and_first():
    xhr = [
        {"url": "https://ota/api/detail", "kind": "response",
         "body": {"hotelName": "InterContinental", "pricing": {"totalPrice": 66714}}},
        {"url": "https://ota/api/prebook", "kind": "request",
         "body": {"checkIn": "2026-09-17", "hotelId": 336672,
                  "rooms": [{"adults": 2, "children": 1, "childrenAges": [5]},
                            {"adults": 2, "children": 0}]}},
    ]
    d = _json_digest(xhr, 4000)
    lines = d.splitlines()
    assert lines[0].startswith("[req]")                       # request payload floats up
    assert "[req] rooms[0].adults = 2" in d
    assert "[req] rooms[0].childrenAges = [5]" in d           # scalar list emitted
    assert "[resp] hotelName = InterContinental" in d
    # the request occupancy comes before the response detail
    assert d.index("rooms[0].adults") < d.index("hotelName")


def test_json_digest_three_channel_ordering():
    xhr = [
        {"url": "r", "kind": "response", "body": {"pricing": {"totalPrice": 30780}}},
        {"url": "e", "kind": "embedded",
         "body": {"booking": {"checkIn": "2026-09-24",
                              "rooms": [{"adults": 2, "children": 1}]}}},
        {"url": "q", "kind": "request",
         "body": {"roomOccupancies": [{"numAdults": 2, "numChildren": 1}]}},
    ]
    d = _json_digest(xhr, 3000)
    order = [ln.split()[0] for ln in d.splitlines()]
    assert order.index("[req]") < order.index("[embed]") < order.index("[resp]")
    assert "[embed] booking.checkIn = 2026-09-24" in d
    assert "[req] roomOccupancies[0].numAdults = 2" in d


def test_json_digest_drops_enum_values_not_real_words():
    xhr = [{"url": "x", "kind": "response", "body": {
        "cancellationCharges": "MERGE_LOCAL_COUPON_DATA",   # enum -> drop
        "roomName": "Deluxe King",                          # real -> keep
        "childAge": 7}}]
    d = _json_digest(xhr, 2000)
    assert "roomName = Deluxe King" in d
    assert "childAge = 7" in d
    assert "MERGE_LOCAL_COUPON_DATA" not in d
