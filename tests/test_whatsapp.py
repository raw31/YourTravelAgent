"""yta/whatsapp.py -- the additive interactive-message pieces built for
v2 (send_buttons, and parse_inbound()'s button_reply support). No network
-- requests.post is mocked."""
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


def test_send_buttons_never_sends_more_than_three(monkeypatch):
    captured = {}
    monkeypatch.setattr("requests.post", lambda *a, **kw: (captured.update(json=kw["json"]), _FakeResponse(200))[1])
    whatsapp.send_buttons("919999999999", "Pick one",
                           [("a", "A"), ("b", "B"), ("c", "C"), ("d", "D")])
    assert len(captured["json"]["interactive"]["action"]["buttons"]) == 3


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


def test_parse_inbound_skips_non_button_interactive():
    payload = {"entry": [{"changes": [{"value": {"messages": [{
        "from": "919999999999",
        "type": "interactive",
        "interactive": {"type": "list_reply", "list_reply": {"id": "x", "title": "y"}},
    }]}}]}]}
    assert whatsapp.parse_inbound(payload) == []


def test_parse_inbound_still_handles_plain_text_and_media():
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"from": "1", "type": "text", "text": {"body": "hi"}},
        {"from": "1", "type": "image", "image": {"id": "m1", "mime_type": "image/jpeg"}},
    ]}}]}]}
    items = whatsapp.parse_inbound(payload)
    assert items[0]["type"] == "text" and items[0]["text"] == "hi"
    assert items[1]["type"] == "image" and items[1]["media_id"] == "m1"
    assert items[0]["button_id"] is None
