"""Routing tests for v2 (yta/wa_flows/v2.py) — the confirm/decline step,
the bounded slot-filling cap, the concierge-voice message building, and
the button-driven "start over" / "try another hotel" affordances. v1's
own test file (test_wa_flows.py) is untouched, and re-run here unmodified
as part of the full suite to prove v1 itself wasn't touched by this work.

No network, no real LLM — whatsapp.find_url/download_media/send_buttons,
pipeline.extract, extract_llm.extract_clarification, yta.web._resolve,
and yta.leads.db.record_lead are all faked/monkeypatched.
"""
import time

import pytest

from yta.wa_flows import v2


@pytest.fixture(autouse=True)
def _clean_sessions():
    v2._WA_SESSIONS.clear()
    yield
    v2._WA_SESSIONS.clear()


@pytest.fixture
def sent(monkeypatch):
    messages = []
    monkeypatch.setattr(v2, "wa_send", lambda frm, text: messages.append(("text", text)))

    def _fake_send_buttons(to, body, buttons):
        messages.append(("buttons", body, buttons))

    monkeypatch.setattr("yta.whatsapp.send_buttons", _fake_send_buttons)
    return messages


class _Packet:
    status = "ok"
    missing_mandatory = []

    def __init__(self, missing=()):
        self._still_missing = list(missing)
        self.hotel = type("H", (), {"name": "Test Hotel"})()

        class _Stay:
            check_in = "2026-09-21"
            check_out = "2026-09-22"
            occupancy = [{"adults": 2, "children": 0, "child_ages": []}]
        self.stay = _Stay()

        class _Offer:
            room_name = "Deluxe Room"
            meal_plan = None
            refundable = None
        self.requested_offer = _Offer()

        class _Benchmark:
            final_payable = 31683.0
            currency = "INR"
        self.ota_benchmark = _Benchmark()

    def check_mandatory(self):
        return self._still_missing

    def derive_stay(self):
        pass

    def add(self, path, val, *a, **kw):
        if path in self._still_missing:
            self._still_missing.remove(path)

    def to_dict(self):
        return {"hotel": {"name": self.hotel.name}}


def _open_awaiting(missing=("ota_benchmark.final_payable",), age_sec=0, attempts=0):
    v2._WA_SESSIONS["cust"] = {
        "state": "awaiting_field", "packet": _Packet(missing), "missing": list(missing),
        "unproductive_attempts": attempts, "last_activity": time.time() - age_sec,
    }


def _open_presented(age_sec=0, resolution=None):
    v2._WA_SESSIONS["cust"] = {
        "state": "presented", "packet": _Packet(), "resolution": resolution or {},
        "last_activity": time.time() - age_sec,
    }


# -- onboarding ------------------------------------------------------

def test_onboarding_message_names_what_to_send(sent):
    v2.handle_batch("cust", [{"type": "text", "text": "hi"}])
    assert any("hotel" in m[1].lower() and "price" in m[1].lower()
               for m in sent if m[0] == "text")


def test_onboarding_never_calls_itself_a_checker_or_bot(sent):
    v2.handle_batch("cust", [{"type": "text", "text": "hi"}])
    text = next(m[1] for m in sent if m[0] == "text")
    assert "checker" not in text.lower() and "bot" not in text.lower()


# -- structured recap + natural question ---------------------------------

def test_missing_field_ask_shows_a_structured_recap_and_a_real_question(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: "https://booking.com/x")
    monkeypatch.setattr("yta.pipeline.extract",
                         lambda *a, **kw: _Packet(missing=["ota_benchmark.final_payable"]))
    v2.handle_batch("cust", [{"type": "text", "text": "https://booking.com/x"}])
    body = next(m[1] for m in sent if m[0] == "buttons")
    assert "Test Hotel" in body                              # structured recap present
    assert "Total Price Shown on the Page" not in body        # not the raw internal field label
    assert body.strip().endswith("?")                         # closes with a real question


def test_multiple_missing_fields_read_as_one_sentence_not_bullets(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: "https://booking.com/x")
    monkeypatch.setattr(
        "yta.pipeline.extract",
        lambda *a, **kw: _Packet(missing=["requested_offer.room_name", "ota_benchmark.final_payable"]),
    )
    v2.handle_batch("cust", [{"type": "text", "text": "https://booking.com/x"}])
    body = next(m[1] for m in sent if m[0] == "buttons")
    assert "Missing:" not in body
    assert "the room type" in body and "the total price" in body


# -- no wall-clock timeout on a real reply -------------------------------

def test_a_late_reply_is_honored_no_matter_how_late(sent, monkeypatch):
    # Regression: live testing proved BOTH 5 minutes and 15 minutes wrong --
    # a customer taking that long to find the right screenshot kept getting
    # silently bounced into a fresh submission, losing the hotel/dates/
    # occupancy already captured. There is no clock-based cutoff on a
    # legitimate reply anymore -- content (does it answer the question?)
    # gates this, not elapsed time. Two hours here is deliberately absurd,
    # to prove there's no hidden shorter cutoff left over.
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.extract_llm.extract_clarification",
                         lambda missing, text, media=None: ({"ota_benchmark.final_payable": 31683}, None))
    monkeypatch.setattr("yta.web._resolve", lambda packet: {"room_map": {"matched": False}})
    _open_awaiting(age_sec=2 * 60 * 60)
    v2.handle_batch("cust", [{"type": "text", "text": "31683"}])
    assert sent[-1][0] == "buttons"   # reached _present_deal, not re-treated as a fresh submission


def test_abandoned_session_past_the_hygiene_backstop_is_cleared(sent, monkeypatch):
    # The 24h backstop is memory hygiene for a number that never comes
    # back, not a UX judgment -- still worth confirming it actually clears.
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    _open_awaiting(age_sec=v2._SESSION_MAX_AGE_SEC + 60)
    v2.handle_batch("cust", [{"type": "text", "text": "31683"}])
    assert "cust" not in v2._WA_SESSIONS
    # a bare number with no hotel context left should ask for the link/photo,
    # not be silently absorbed as an answer to the (now-cleared) question
    assert any("hotel" in m[1].lower() for m in sent if m[0] == "text")


# -- bounded slot-filling ----------------------------------------------

def test_gives_up_after_max_unproductive_attempts(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.extract_llm.extract_clarification",
                         lambda missing, text, media=None: ({}, None))

    _open_awaiting(attempts=0)
    v2.handle_batch("cust", [{"type": "text", "text": "hi there"}])
    assert "cust" in v2._WA_SESSIONS   # attempt 1 of 2 -- still open
    assert v2._WA_SESSIONS["cust"]["unproductive_attempts"] == 1

    v2.handle_batch("cust", [{"type": "text", "text": "still unrelated"}])
    assert "cust" not in v2._WA_SESSIONS   # attempt 2 -- cap reached, gave up
    assert any("pick up from there" in m[1] for m in sent if m[0] == "text")


def test_progress_resets_the_unproductive_counter(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    calls = iter([
        ({}, None),                                          # attempt 1 (unproductive)
        ({}, "which year did you mean?"),                    # progress -- resets counter
        ({}, None),                                          # attempt 1 again (not 3rd overall)
    ])
    monkeypatch.setattr("yta.extract_llm.extract_clarification",
                         lambda missing, text, media=None: next(calls))
    _open_awaiting()
    v2.handle_batch("cust", [{"type": "text", "text": "a"}])
    v2.handle_batch("cust", [{"type": "text", "text": "b"}])
    assert v2._WA_SESSIONS["cust"]["unproductive_attempts"] == 0
    v2.handle_batch("cust", [{"type": "text", "text": "c"}])
    assert "cust" in v2._WA_SESSIONS   # still open -- this was only attempt 1 since the reset
    assert v2._WA_SESSIONS["cust"]["unproductive_attempts"] == 1


# -- presented: confirm / decline ---------------------------------------

def test_confirm_button_records_lead_and_replies_with_reference(sent, monkeypatch):
    recorded = []
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.leads.db.record_lead",
                         lambda phone, status, packet, resolution: recorded.append((phone, status)) or "BMS-TEST1234")
    _open_presented()
    v2.handle_batch("cust", [{"type": "button_reply", "button_id": "confirm_book", "text": "Yes, book this"}])
    assert recorded == [("cust", "confirmed")]
    assert "cust" not in v2._WA_SESSIONS
    assert any("BMS-TEST1234" in m[1] for m in sent if m[0] == "text")


def test_typed_yes_works_same_as_the_button(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.leads.db.record_lead",
                         lambda phone, status, packet, resolution: "BMS-TEST5678")
    _open_presented()
    v2.handle_batch("cust", [{"type": "text", "text": "yes please"}])
    assert "cust" not in v2._WA_SESSIONS
    assert any("BMS-TEST5678" in m[1] for m in sent if m[0] == "text")


def test_decline_button_records_lead_and_does_not_block_next_hotel(sent, monkeypatch):
    recorded = []
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.leads.db.record_lead",
                         lambda phone, status, packet, resolution: recorded.append(status) or "BMS-DECL0000")
    _open_presented()
    v2.handle_batch("cust", [{"type": "button_reply", "button_id": "decline_book", "text": "Not now"}])
    assert recorded == ["declined"]
    assert "cust" not in v2._WA_SESSIONS


def test_unrecognized_reply_in_presented_state_reprompts_and_stays_open(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    _open_presented()
    v2.handle_batch("cust", [{"type": "text", "text": "what's the cancellation policy?"}])
    assert "cust" in v2._WA_SESSIONS
    assert any("tap" in m[1].lower() for m in sent if m[0] == "text")


# -- the deal reveal: matched-and-cheaper / matched-not-cheaper / no rate --

def test_cheaper_rate_shows_structured_savings_and_confirm_buttons(sent, monkeypatch):
    monkeypatch.setattr(
        "yta.web._resolve",
        lambda packet: {"room_map": {"matched": True, "ratekey_option_ids": ["o1"], "rate_options": [
            {"option_id": "o1", "room_name": "Deluxe Room", "meal_basis": "Breakfast",
             "refundable": True, "currency": "INR", "total_price": 28015.64},
        ]}},
    )
    v2._present_deal("cust", _Packet())
    body = next(m[1] for m in sent if m[0] == "buttons")
    assert "31,683" in body and "28,015.64" in body and "3,667" in body
    ids = [bid for bid, _ in next(m[2] for m in sent if m[0] == "buttons")]
    assert ids == ["confirm_book", "decline_book"]
    assert "cust" in v2._WA_SESSIONS and v2._WA_SESSIONS["cust"]["state"] == "presented"


def test_matched_but_not_cheaper_gets_no_confirm_buttons(sent, monkeypatch):
    # A real bug in the first cut of this: a matched rate that ISN'T
    # actually cheaper still showed "Yes, book this" -- there's nothing to
    # confirm in that case, it should just say so with no buttons at all.
    monkeypatch.setattr(
        "yta.web._resolve",
        lambda packet: {"room_map": {"matched": True, "ratekey_option_ids": ["o1"], "rate_options": [
            {"option_id": "o1", "room_name": "Deluxe Room", "currency": "INR", "total_price": 40000.0},
        ]}},
    )
    v2._present_deal("cust", _Packet())
    assert not any(m[0] == "buttons" for m in sent)
    assert any("nothing better to offer" in m[1] for m in sent if m[0] == "text")
    assert "cust" not in v2._WA_SESSIONS


def test_no_live_rate_offers_try_another_button(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: "https://booking.com/x")
    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: _Packet())
    monkeypatch.setattr("yta.web._resolve", lambda packet: {"available": False, "room_map": {"matched": False}})

    v2.handle_batch("cust", [{"type": "text", "text": "https://booking.com/x"}])
    assert "cust" not in v2._WA_SESSIONS
    assert any(m[0] == "buttons" and any(bid == "try_another" for bid, _ in m[2]) for m in sent)


def test_try_another_button_sends_onboarding_prompt(sent):
    v2.handle_batch("cust", [{"type": "button_reply", "button_id": "try_another", "text": "Try another hotel"}])
    assert any("hotel" in m[1].lower() for m in sent if m[0] == "text")


# -- global cancel / start-over -------------------------------------------

def test_start_new_chat_button_clears_any_open_session(sent):
    _open_awaiting()
    v2.handle_batch("cust", [{"type": "button_reply", "button_id": "start_new_chat", "text": "Start over"}])
    assert "cust" not in v2._WA_SESSIONS
    assert any("next one" in m[1].lower() for m in sent if m[0] == "text")


def test_missing_field_ask_carries_a_start_new_chat_button(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: "https://booking.com/x")
    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: _Packet(missing=["ota_benchmark.final_payable"]))
    v2.handle_batch("cust", [{"type": "text", "text": "https://booking.com/x"}])
    assert any(m[0] == "buttons" and any(bid == "start_new_chat" for bid, _ in m[2]) for m in sent)
