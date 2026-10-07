"""yta/whatsapp.py -- the additive interactive-message pieces: send_buttons
+ parse_inbound()'s button_reply support (v2), and send_list +
parse_inbound()'s list_reply support (v7). No network -- requests.post is
mocked."""
import pytest

from yta import whatsapp


class _FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {"messaging_product": "whatsapp"}

    def json(self):
        return self._body


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123456")


def test_send_buttons_posts_interactive_button_payload(monkeypatch):
    captured = {}

    def _fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse(200)

    monkeypatch.setattr("requests.post", _fake_post)
    out = whatsapp.send_buttons("919999999999", "Pick one",
                                 [("a", "Option A"), ("b", "Option B")])
    assert out["_status_code"] == 200
    body = captured["json"]
    assert body["type"] == "interactive"
    assert body["interactive"]["type"] == "button"
    assert body["interactive"]["body"]["text"] == "Pick one"
    ids = [b["reply"]["id"] for b in body["interactive"]["action"]["buttons"]]
    titles = [b["reply"]["title"] for b in body["interactive"]["action"]["buttons"]]
    assert ids == ["a", "b"]
    assert titles == ["Option A", "Option B"]


def test_send_image_posts_link_and_caption(monkeypatch):
    captured = {}
    monkeypatch.setattr("requests.post", lambda *a, **kw: (captured.update(json=kw["json"]), _FakeResponse(200))[1])
    out = whatsapp.send_image("919999999999", "https://example.com/photo.jpg", caption="Nice hotel")
    assert out["_status_code"] == 200
    body = captured["json"]
    assert body["type"] == "image"
    assert body["image"] == {"link": "https://example.com/photo.jpg", "caption": "Nice hotel"}


def test_send_image_without_caption_omits_the_field(monkeypatch):
    captured = {}
    monkeypatch.setattr("requests.post", lambda *a, **kw: (captured.update(json=kw["json"]), _FakeResponse(200))[1])
    whatsapp.send_image("919999999999", "https://example.com/photo.jpg")
    assert captured["json"]["image"] == {"link": "https://example.com/photo.jpg"}


def test_send_buttons_never_sends_more_than_three(monkeypatch):
    captured = {}
    monkeypatch.setattr("requests.post", lambda *a, **kw: (captured.update(json=kw["json"]), _FakeResponse(200))[1])
    whatsapp.send_buttons("919999999999", "Pick one",
                           [("a", "A"), ("b", "B"), ("c", "C"), ("d", "D")])
    assert len(captured["json"]["interactive"]["action"]["buttons"]) == 3


def test_send_buttons_truncates_titles_over_twenty_chars(monkeypatch):
    # Regression: Meta rejects the WHOLE message (every button, the body
    # text, everything) if even one title exceeds 20 chars -- confirmed
    # live 2026-09-30, a 23-char onboarding button title silently killed
    # every reply to "hey" for real customers. Truncating here is the
    # safety net so a copy change can never do that again.
    captured = {}
    monkeypatch.setattr("requests.post", lambda *a, **kw: (captured.update(json=kw["json"]), _FakeResponse(200))[1])
    whatsapp.send_buttons("919999999999", "Pick one",
                           [("a", "I already picked a room")])
    title = captured["json"]["interactive"]["action"]["buttons"][0]["reply"]["title"]
    assert len(title) <= 20
    assert title == "I already picked a r"


def test_parse_inbound_extracts_button_reply():
    payload = {"entry": [{"changes": [{"value": {"messages": [{
        "from": "919999999999",
        "type": "interactive",
        "interactive": {"type": "button_reply", "button_reply": {"id": "confirm_book", "title": "Yes, book this"}},
    }]}}]}]}
    items = whatsapp.parse_inbound(payload)
    assert len(items) == 1
    item = items[0]
    assert item["type"] == "button_reply"
    assert item["button_id"] == "confirm_book"
    assert item["text"] == "Yes, book this"


def test_parse_inbound_normalizes_list_reply_like_a_button_reply():
    payload = {"entry": [{"changes": [{"value": {"messages": [{
        "from": "919999999999",
        "type": "interactive",
        "interactive": {"type": "list_reply", "list_reply": {"id": "opt-2", "title": "INR 25,000"}},
    }]}}]}]}
    items = whatsapp.parse_inbound(payload)
    assert len(items) == 1
    item = items[0]
    assert item["type"] == "button_reply"          # same shape a button tap produces
    assert item["button_id"] == "opt-2"
    assert item["text"] == "INR 25,000"


def test_parse_inbound_skips_other_interactive_types():
    payload = {"entry": [{"changes": [{"value": {"messages": [{
        "from": "919999999999",
        "type": "interactive",
        "interactive": {"type": "nfm_reply", "nfm_reply": {}},
    }]}}]}]}
    assert whatsapp.parse_inbound(payload) == []


def test_send_list_posts_interactive_list_payload(monkeypatch):
    captured = {}
    monkeypatch.setattr("requests.post", lambda *a, **kw: (captured.update(json=kw["json"]), _FakeResponse(200))[1])
    sections = [
        ("Deluxe Villa", [("opt-1", "INR 20,000", "Room Only"), ("opt-2", "INR 21,000", "Breakfast")]),
        ("Premier Villa", [("opt-3", "INR 25,000", "Room Only · Refundable")]),
    ]
    out = whatsapp.send_list("919999999999", "Pick a room", "Choose a room", sections)
    assert out["_status_code"] == 200
    body = captured["json"]
    assert body["type"] == "interactive"
    assert body["interactive"]["type"] == "list"
    assert body["interactive"]["body"]["text"] == "Pick a room"
    assert body["interactive"]["action"]["button"] == "Choose a room"
    api_sections = body["interactive"]["action"]["sections"]
    assert [s["title"] for s in api_sections] == ["Deluxe Villa", "Premier Villa"]
    assert len(api_sections[0]["rows"]) == 2 and len(api_sections[1]["rows"]) == 1
    assert api_sections[0]["rows"][0] == {"id": "opt-1", "title": "INR 20,000", "description": "Room Only"}


def test_send_list_caps_at_ten_rows_total(monkeypatch):
    captured = {}
    monkeypatch.setattr("requests.post", lambda *a, **kw: (captured.update(json=kw["json"]), _FakeResponse(200))[1])
    sections = [(f"Room {i}", [(f"opt-{i}", f"INR {i}00", "")]) for i in range(15)]
    whatsapp.send_list("919999999999", "Pick a room", "Choose", sections)
    total_rows = sum(len(s["rows"]) for s in captured["json"]["interactive"]["action"]["sections"])
    assert total_rows == 10


def test_send_list_truncates_long_labels(monkeypatch):
    captured = {}
    monkeypatch.setattr("requests.post", lambda *a, **kw: (captured.update(json=kw["json"]), _FakeResponse(200))[1])
    long_title = "A" * 40
    whatsapp.send_list("919999999999", "Pick", "X" * 30, [("S" * 40, [("id1", long_title, None)])])
    body = captured["json"]["interactive"]
    assert len(body["action"]["button"]) <= 20
    assert len(body["action"]["sections"][0]["title"]) <= 24
    assert len(body["action"]["sections"][0]["rows"][0]["title"]) <= 24
    assert "description" not in body["action"]["sections"][0]["rows"][0]   # falsy description omitted


def test_parse_inbound_still_handles_plain_text_and_media():
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"from": "1", "type": "text", "text": {"body": "hi"}},
        {"from": "1", "type": "image", "image": {"id": "m1", "mime_type": "image/jpeg"}},
    ]}}]}]}
    items = whatsapp.parse_inbound(payload)
    assert items[0]["type"] == "text" and items[0]["text"] == "hi"
    assert items[1]["type"] == "image" and items[1]["media_id"] == "m1"
    assert items[0]["button_id"] is None
