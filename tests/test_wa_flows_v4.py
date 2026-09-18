"""Routing tests for v4 (yta/wa_flows/v4.py) — carried over from v3
unmodified (confirm/decline step, bounded slot-filling cap, concierge-voice
message building, the referral loop) as a regression baseline, PLUS new
tests for v4's own addition: a plain-text message with no open session can
now start a submission on its own, not just a URL or a photo. v0/v1/v2/v3's
own test files are untouched, and re-run here unmodified as part of the
full suite to prove none of them were touched by this work.

No network, no real LLM — whatsapp.find_url/download_media/send_buttons,
pipeline.extract, extract_llm.extract_clarification, yta.web._resolve,
and yta.leads.db.record_lead are all faked/monkeypatched.
"""
import time

import pytest

from yta.wa_flows import v4


@pytest.fixture(autouse=True)
def _clean_sessions():
    v4._WA_SESSIONS.clear()
    v4._PENDING_REFERRALS.clear()
    yield
    v4._WA_SESSIONS.clear()
    v4._PENDING_REFERRALS.clear()


@pytest.fixture
def sent(monkeypatch):
    messages = []
    monkeypatch.setattr(v4, "wa_send", lambda frm, text: messages.append(("text", text)))

    def _fake_send_buttons(to, body, buttons):
        messages.append(("buttons", body, buttons))

    monkeypatch.setattr("yta.whatsapp.send_buttons", _fake_send_buttons)
    return messages


class _Packet:
    status = "ok"
    missing_mandatory = []

    def __init__(self, missing=(), hotel_name="Test Hotel"):
        self._still_missing = list(missing)
        self.hotel = type("H", (), {"name": hotel_name})()

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
    v4._WA_SESSIONS["cust"] = {
        "state": "awaiting_field", "packet": _Packet(missing), "missing": list(missing),
        "unproductive_attempts": attempts, "last_activity": time.time() - age_sec,
    }


def _open_presented(age_sec=0, resolution=None, savings_line=None, confirm_line=None):
    v4._WA_SESSIONS["cust"] = {
        "state": "presented", "packet": _Packet(), "resolution": resolution or {},
        "savings_line": savings_line, "confirm_line": confirm_line,
        "last_activity": time.time() - age_sec,
    }


# -- onboarding ------------------------------------------------------

def test_onboarding_message_names_what_to_send(sent):
    v4.handle_batch("cust", [{"type": "text", "text": "hi"}])
    text = next(m[1] for m in sent if m[0] == "text")
    assert "link" in text.lower() and "room" in text.lower() and "price" in text.lower()


def test_onboarding_never_mentions_the_sourcing_mechanism(sent):
    # The bot's own edge (wholesale rates) is deliberately never named to
    # a customer -- the pitch stays outcome-focused ("a better rate"),
    # not mechanism-focused.
    v4.handle_batch("cust", [{"type": "text", "text": "hi"}])
    text = next(m[1] for m in sent if m[0] == "text")
    assert "wholesale" not in text.lower()


def test_onboarding_never_calls_itself_a_checker_or_bot(sent):
    v4.handle_batch("cust", [{"type": "text", "text": "hi"}])
    text = next(m[1] for m in sent if m[0] == "text")
    assert "checker" not in text.lower() and "bot" not in text.lower()


# -- v4's own addition: plain text as a submission, no session ----------

def test_greeting_never_triggers_extraction(sent, monkeypatch):
    calls = []
    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: calls.append(1) or _Packet())
    v4.handle_batch("cust", [{"type": "text", "text": "hi"}])
    assert calls == []                      # the pre-filter rejected it for free
    text = next(m[1] for m in sent if m[0] == "text")
    assert text == v4._ONBOARDING_TEXT


def test_short_non_chitchat_text_also_skips_extraction(sent, monkeypatch):
    # Below the length floor but not literally a greeting -- the length
    # filter, not just the chit-chat wordlist, has to reject this too.
    calls = []
    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: calls.append(1) or _Packet())
    v4.handle_batch("cust", [{"type": "text", "text": "book pls"}])
    assert calls == []
    assert next(m[1] for m in sent if m[0] == "text") == v4._ONBOARDING_TEXT


def test_free_text_query_with_everything_present_reaches_the_deal(sent, monkeypatch):
    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: _Packet(missing=[]))
    monkeypatch.setattr("yta.web._resolve", lambda packet: {"room_map": {"matched": False}})
    v4.handle_batch("cust", [{"type": "text",
                    "text": "Taj Palace Delhi, Oct 15-17, 2 adults, saw INR 15000 on MMT"}])
    # same shape a fully-informative photo/link submission produces: a
    # recap first, then the buttons message from _present_deal.
    recap_msgs = [m[1] for m in sent if m[0] == "text" and "Test Hotel" in m[1]]
    assert recap_msgs, "expected a recap message showing what was read"
    assert sent[-1][0] == "buttons"


def test_free_text_query_with_missing_fields_opens_awaiting_field(sent, monkeypatch):
    monkeypatch.setattr("yta.pipeline.extract",
                         lambda *a, **kw: _Packet(missing=["ota_benchmark.final_payable"]))
    v4.handle_batch("cust", [{"type": "text",
                    "text": "Taj Palace Delhi, Oct 15-17, 2 adults, deluxe room"}])
    assert "cust" in v4._WA_SESSIONS
    assert v4._WA_SESSIONS["cust"]["state"] == "awaiting_field"
    body = next(m[1] for m in sent if m[0] == "buttons")
    assert "Test Hotel" in body


def test_free_text_with_no_recognizable_hotel_gets_a_soft_nudge_not_a_loop(sent, monkeypatch):
    # Long enough to pass the pre-filter, but the extractor found nothing
    # usable -- should NOT drag the customer into a full slot-filling
    # interrogation seeded from a stray sentence.
    monkeypatch.setattr("yta.pipeline.extract",
                         lambda *a, **kw: _Packet(missing=["hotel.name"], hotel_name=None))
    v4.handle_batch("cust", [{"type": "text",
                    "text": "just wondering what kind of deals you folks usually find"}])
    assert "cust" not in v4._WA_SESSIONS               # no half-open interrogation
    assert not any(m[0] == "buttons" for m in sent)     # no missing-field ask fired
    assert any("couldn't find hotel details" in m[1].lower() for m in sent if m[0] == "text")


def test_free_text_mid_session_is_still_handled_as_an_answer_not_a_new_query(sent, monkeypatch):
    # The new text-as-submission path only applies when session is None --
    # a long message while awaiting_field is open must still be tried
    # against the OPEN QUESTION first, never treated as a fresh submission.
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.extract_llm.extract_clarification",
                         lambda missing, text, media=None: ({"ota_benchmark.final_payable": 31683}, None))
    monkeypatch.setattr("yta.web._resolve", lambda packet: {"room_map": {"matched": False}})
    _open_awaiting()
    v4.handle_batch("cust", [{"type": "text",
                    "text": "actually it's the Grand Hyatt Mumbai, Oct 20-22, 2 adults, saw INR 20000"}])
    assert sent[-1][0] == "buttons"   # resolved via _handle_awaiting_field -> _present_deal
    assert "cust" not in v4._WA_SESSIONS


# -- structured recap + natural question ---------------------------------

def test_missing_field_ask_shows_a_structured_recap_and_a_real_question(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: "https://booking.com/x")
    monkeypatch.setattr("yta.pipeline.extract",
                         lambda *a, **kw: _Packet(missing=["ota_benchmark.final_payable"]))
    v4.handle_batch("cust", [{"type": "text", "text": "https://booking.com/x"}])
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
    v4.handle_batch("cust", [{"type": "text", "text": "https://booking.com/x"}])
    body = next(m[1] for m in sent if m[0] == "buttons")
    assert "Missing:" not in body
    assert "the room type" in body and "the total price" in body


def test_recap_shown_even_when_nothing_is_missing_on_first_submission(sent, monkeypatch):
    # A screenshot that already has everything shouldn't skip straight to
    # the price quote -- the customer should still see what was actually
    # read, same transparency as the ask-for-more path gets.
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: "https://booking.com/x")
    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: _Packet(missing=[]))
    monkeypatch.setattr("yta.web._resolve", lambda packet: {"room_map": {"matched": False}})
    v4.handle_batch("cust", [{"type": "text", "text": "https://booking.com/x"}])
    recap_msgs = [m[1] for m in sent if m[0] == "text" and "Test Hotel" in m[1]]
    assert recap_msgs, "expected a recap message showing what was read"


def test_recap_includes_ota_price_when_already_known(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: "https://booking.com/x")
    monkeypatch.setattr("yta.pipeline.extract",
                         lambda *a, **kw: _Packet(missing=["requested_offer.room_name"]))
    v4.handle_batch("cust", [{"type": "text", "text": "https://booking.com/x"}])
    body = next(m[1] for m in sent if m[0] == "buttons")
    assert "31,683" in body   # the OTA price the packet already had is shown in the recap


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
    v4.handle_batch("cust", [{"type": "text", "text": "31683"}])
    assert sent[-1][0] == "buttons"   # reached _present_deal, not re-treated as a fresh submission


def test_abandoned_session_past_the_hygiene_backstop_is_cleared(sent, monkeypatch):
    # The 24h backstop is memory hygiene for a number that never comes
    # back, not a UX judgment -- still worth confirming it actually clears.
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    _open_awaiting(age_sec=v4._SESSION_MAX_AGE_SEC + 60)
    v4.handle_batch("cust", [{"type": "text", "text": "31683"}])
    assert "cust" not in v4._WA_SESSIONS
    # a bare number with no hotel context left should ask for the link/photo,
    # not be silently absorbed as an answer to the (now-cleared) question
    assert any("send me the link" in m[1].lower() for m in sent if m[0] == "text")


# -- bounded slot-filling ----------------------------------------------

def test_gives_up_after_max_unproductive_attempts(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.extract_llm.extract_clarification",
                         lambda missing, text, media=None: ({}, None))

    _open_awaiting(attempts=0)
    v4.handle_batch("cust", [{"type": "text", "text": "hi there"}])
    assert "cust" in v4._WA_SESSIONS   # attempt 1 of 2 -- still open
    assert v4._WA_SESSIONS["cust"]["unproductive_attempts"] == 1

    v4.handle_batch("cust", [{"type": "text", "text": "still unrelated"}])
    assert "cust" not in v4._WA_SESSIONS   # attempt 2 -- cap reached, gave up
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
    v4.handle_batch("cust", [{"type": "text", "text": "a"}])
    v4.handle_batch("cust", [{"type": "text", "text": "b"}])
    assert v4._WA_SESSIONS["cust"]["unproductive_attempts"] == 0
    v4.handle_batch("cust", [{"type": "text", "text": "c"}])
    assert "cust" in v4._WA_SESSIONS   # still open -- this was only attempt 1 since the reset
    assert v4._WA_SESSIONS["cust"]["unproductive_attempts"] == 1


# -- presented: confirm / decline ---------------------------------------

def test_confirm_button_records_lead_and_replies_with_reference(sent, monkeypatch):
    recorded = []
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.leads.db.record_lead",
                         lambda phone, status, packet, resolution, referred_by=None: recorded.append((phone, status)) or "BMS-TEST1234")
    _open_presented()
    v4.handle_batch("cust", [{"type": "button_reply", "button_id": "confirm_book", "text": "Yes, book this"}])
    assert recorded == [("cust", "confirmed")]
    assert "cust" not in v4._WA_SESSIONS
    assert any("BMS-TEST1234" in m[1] for m in sent if m[0] == "text")


def test_confirm_message_names_what_was_actually_booked(sent, monkeypatch):
    # The confirm note used to be a bare "I've noted this down" -- it
    # should say what was actually secured and for how much, not just
    # hand back a reference number.
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.leads.db.record_lead",
                         lambda phone, status, packet, resolution, referred_by=None: "BMS-TEST5555")
    _open_presented(confirm_line="I've secured Test Hotel for INR 28,015.64 (INR 3,667 less than what you had).")
    v4.handle_batch("cust", [{"type": "button_reply", "button_id": "confirm_book", "text": "Yes, book this"}])
    confirm_text = next(m[1] for m in sent if m[0] == "text" and "BMS-TEST5555" in m[1])
    assert "I've secured Test Hotel for INR 28,015.64" in confirm_text
    assert "3,667 less than what you had" in confirm_text


def test_confirm_message_falls_back_gracefully_with_no_confirm_line(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.leads.db.record_lead",
                         lambda phone, status, packet, resolution, referred_by=None: "BMS-TEST6666")
    _open_presented(confirm_line=None)
    v4.handle_batch("cust", [{"type": "button_reply", "button_id": "confirm_book", "text": "Yes, book this"}])
    confirm_text = next(m[1] for m in sent if m[0] == "text" and "BMS-TEST6666" in m[1])
    assert "I've noted this down" in confirm_text


def test_typed_yes_works_same_as_the_button(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.leads.db.record_lead",
                         lambda phone, status, packet, resolution, referred_by=None: "BMS-TEST5678")
    _open_presented()
    v4.handle_batch("cust", [{"type": "text", "text": "yes please"}])
    assert "cust" not in v4._WA_SESSIONS
    assert any("BMS-TEST5678" in m[1] for m in sent if m[0] == "text")


def test_decline_button_records_lead_and_does_not_block_next_hotel(sent, monkeypatch):
    recorded = []
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.leads.db.record_lead",
                         lambda phone, status, packet, resolution, referred_by=None: recorded.append(status) or "BMS-DECL0000")
    _open_presented()
    v4.handle_batch("cust", [{"type": "button_reply", "button_id": "decline_book", "text": "Not now"}])
    assert recorded == ["declined"]
    assert "cust" not in v4._WA_SESSIONS


def test_unrecognized_reply_in_presented_state_reprompts_and_stays_open(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    _open_presented()
    v4.handle_batch("cust", [{"type": "text", "text": "what's the cancellation policy?"}])
    assert "cust" in v4._WA_SESSIONS
    assert any("tap" in m[1].lower() for m in sent if m[0] == "text")


# -- the deal reveal: matched-and-cheaper / matched-not-cheaper / no rate --

def test_price_shown_as_short_wrap_safe_was_now_lines(sent, monkeypatch):
    # Regression, round 2: hand-padded spaces don't align in WhatsApp's
    # proportional font (round 1's bug); a ```monospace``` fence DOES
    # align, but the padding needed for "BookMyStay price:" made lines
    # wider than a phone screen, so WhatsApp wrapped mid-value ("INR" on
    # one line, the number on the next) -- confirmed in live testing with
    # a 6-figure Atlantis, The Palm price. No columns at all this time:
    # short "was -> now" lines can't wrap badly because nothing needs to
    # line up.
    monkeypatch.setattr(
        "yta.web._resolve",
        lambda packet: {"room_map": {"matched": True, "ratekey_option_ids": ["o1"], "rate_options": [
            {"option_id": "o1", "room_name": "Deluxe Room", "currency": "INR", "total_price": 28015.64},
        ]}},
    )
    v4._present_deal("cust", _Packet())
    body = next(m[1] for m in sent if m[0] == "buttons")
    assert "```" not in body
    assert "  " not in body                       # no multi-space padding anywhere
    assert all(len(line) < 45 for line in body.split("\n"))   # every line short enough not to wrap
    assert "~INR 31,683~" in body and "*INR 28,015.64*" in body


def test_shouty_and_stray_punctuation_room_names_get_cleaned_up(sent, monkeypatch):
    # Regression: "DELUXE ROOM" (all-caps from TripJack) and "Deluxe Room."
    # (a stray trailing period from the OTA extraction) both showed up
    # verbatim in live testing.
    monkeypatch.setattr(
        "yta.web._resolve",
        lambda packet: {"room_map": {"matched": True, "ratekey_option_ids": ["o1"], "rate_options": [
            {"option_id": "o1", "room_name": "DELUXE ROOM", "currency": "INR", "total_price": 28015.64},
        ]}},
    )
    v4._present_deal("cust", _Packet())
    body = next(m[1] for m in sent if m[0] == "buttons")
    assert "DELUXE ROOM" not in body
    assert "Deluxe Room" in body


def test_category_icons_are_consistent_between_recap_and_deal_card(sent, monkeypatch):
    # One icon per category of fact (hotel/dates/room), the same set and
    # placement in both the "here's what I read" recap and the deal card
    # -- picked after reviewing five styles side by side. Never one icon
    # per individual field (the version that got called "tacky").
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: "https://booking.com/x")
    monkeypatch.setattr("yta.pipeline.extract",
                         lambda *a, **kw: _Packet(missing=["ota_benchmark.final_payable"]))
    v4.handle_batch("cust", [{"type": "text", "text": "https://booking.com/x"}])
    ask_body = next(m[1] for m in sent if m[0] == "buttons")
    assert "🏨" in ask_body and "📅" in ask_body and "🛏️" in ask_body

    sent.clear()
    monkeypatch.setattr(
        "yta.web._resolve",
        lambda packet: {"room_map": {"matched": True, "ratekey_option_ids": ["o1"], "rate_options": [
            {"option_id": "o1", "room_name": "Deluxe Room", "currency": "INR", "total_price": 28015.64},
        ]}},
    )
    v4._present_deal("cust", _Packet())
    deal_body = next(m[1] for m in sent if m[0] == "buttons")
    assert "🏨" in deal_body and "📅" in deal_body and "🛏️" in deal_body and "💰" in deal_body


def test_cheaper_rate_shows_structured_savings_and_confirm_buttons(sent, monkeypatch):
    monkeypatch.setattr(
        "yta.web._resolve",
        lambda packet: {"room_map": {"matched": True, "ratekey_option_ids": ["o1"], "rate_options": [
            {"option_id": "o1", "room_name": "Deluxe Room", "meal_basis": "Breakfast",
             "refundable": True, "currency": "INR", "total_price": 28015.64},
        ]}},
    )
    v4._present_deal("cust", _Packet())
    body = next(m[1] for m in sent if m[0] == "buttons")
    assert "31,683" in body and "28,015.64" in body and "3,667" in body
    # The deal card is a full, self-contained recap -- not just the price
    # delta -- since a customer might forward just this message on its own.
    assert "Test Hotel" in body
    assert "Sep 21" in body and "Sep 22" in body   # dates
    assert "2 adult" in body                        # occupancy
    assert "Deluxe Room" in body and "Breakfast" in body and "Refundable" in body
    ids = [bid for bid, _ in next(m[2] for m in sent if m[0] == "buttons")]
    assert ids == ["confirm_book", "decline_book"]
    assert "cust" in v4._WA_SESSIONS and v4._WA_SESSIONS["cust"]["state"] == "presented"


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
    v4._present_deal("cust", _Packet())
    assert not any(m[0] == "buttons" for m in sent)
    assert any("nothing better to offer" in m[1] for m in sent if m[0] == "text")
    assert "cust" not in v4._WA_SESSIONS


def test_no_live_rate_offers_try_another_button(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: "https://booking.com/x")
    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: _Packet())
    monkeypatch.setattr("yta.web._resolve", lambda packet: {"available": False, "room_map": {"matched": False}})

    v4.handle_batch("cust", [{"type": "text", "text": "https://booking.com/x"}])
    assert "cust" not in v4._WA_SESSIONS
    assert any(m[0] == "buttons" and any(bid == "try_another" for bid, _ in m[2]) for m in sent)


def test_try_another_button_sends_onboarding_prompt(sent):
    v4.handle_batch("cust", [{"type": "button_reply", "button_id": "try_another", "text": "Try another hotel"}])
    assert any("hotel" in m[1].lower() for m in sent if m[0] == "text")


# -- global cancel / start-over -------------------------------------------

def test_start_new_chat_button_clears_any_open_session(sent):
    _open_awaiting()
    v4.handle_batch("cust", [{"type": "button_reply", "button_id": "start_new_chat", "text": "Start over"}])
    assert "cust" not in v4._WA_SESSIONS
    assert any("next one" in m[1].lower() for m in sent if m[0] == "text")


def test_missing_field_ask_carries_a_start_new_chat_button(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: "https://booking.com/x")
    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: _Packet(missing=["ota_benchmark.final_payable"]))
    v4.handle_batch("cust", [{"type": "text", "text": "https://booking.com/x"}])
    assert any(m[0] == "buttons" and any(bid == "start_new_chat" for bid, _ in m[2]) for m in sent)


# -- the referral loop: new in v4, not in v2 ------------------------------

def test_confirming_asks_for_a_referral_and_a_deep_link(sent, monkeypatch):
    monkeypatch.setenv("WHATSAPP_DISPLAY_NUMBER", "919999999999")
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.leads.db.record_lead",
                         lambda phone, status, packet, resolution, referred_by=None: "BMS-TEST1234")
    _open_presented(confirm_line="I've secured Test Hotel for INR 28,015.64 (INR 3,667 less than what you had).")
    v4.handle_batch("cust", [{"type": "button_reply", "button_id": "confirm_book", "text": "Yes, book this"}])
    texts = [m[1] for m in sent if m[0] == "text"]
    assert any("BMS-TEST1234" in t for t in texts)                       # the confirm note
    assert any("wa.me/" in t for t in texts)                             # a real forwardable deep link


def test_savings_figure_is_not_repeated_in_the_referral_ask(sent, monkeypatch):
    # Regression: the confirm message (message 1) already states the
    # savings via confirm_line -- the referral ask (message 2) used to
    # restate "you just saved X" immediately after, reading like the bot
    # forgot what it had just said one message earlier.
    monkeypatch.setenv("WHATSAPP_DISPLAY_NUMBER", "919999999999")
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.leads.db.record_lead",
                         lambda phone, status, packet, resolution, referred_by=None: "BMS-TEST7777")
    _open_presented(confirm_line="I've secured Test Hotel for INR 28,015.64 (INR 3,667 less than what you had).")
    v4.handle_batch("cust", [{"type": "button_reply", "button_id": "confirm_book", "text": "Yes, book this"}])
    texts = [m[1] for m in sent if m[0] == "text"]
    confirm_text = next(t for t in texts if "BMS-TEST7777" in t)
    ask_text = next(t for t in texts if "trip coming up" in t.lower())
    assert "3,667" in confirm_text          # the figure lives in the confirm message...
    assert "3,667" not in ask_text          # ...and isn't repeated in the ask right after it
    assert any("wa.me/" in t for t in texts)                             # a real forwardable deep link


def test_the_forwardable_message_carries_nothing_but_the_shareable_text(sent, monkeypatch):
    # WhatsApp's forward action grabs the whole message bubble -- if the
    # "tap and hold, then Forward" instruction and the shareable text
    # were bundled in one message, forwarding it would carry the
    # instructions along too. They must be two separate sends.
    monkeypatch.setenv("WHATSAPP_DISPLAY_NUMBER", "919999999999")
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.leads.db.record_lead",
                         lambda phone, status, packet, resolution, referred_by=None: "BMS-TEST9999")
    _open_presented(savings_line="You just saved INR 3,667 (12%) on this one.")
    v4.handle_batch("cust", [{"type": "button_reply", "button_id": "confirm_book", "text": "Yes, book this"}])
    texts = [m[1] for m in sent if m[0] == "text"]
    shareable = next(t for t in texts if "wa.me/" in t)
    assert "Forward" not in shareable and "Tap and hold" not in shareable
    assert shareable.startswith("I just found a better hotel rate")


def test_decline_does_not_ask_for_a_referral(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.leads.db.record_lead",
                         lambda phone, status, packet, resolution, referred_by=None: "BMS-TEST0002")
    _open_presented(savings_line="You just saved INR 3,667 (12%) on this one.")
    v4.handle_batch("cust", [{"type": "button_reply", "button_id": "decline_book", "text": "Not now"}])
    texts = [m[1] for m in sent if m[0] == "text"]
    assert not any("trip coming up" in t.lower() for t in texts)


def test_a_referral_mention_is_captured_and_attached_at_confirm(sent, monkeypatch):
    # Someone tapping the shared wa.me link sends the referral mention as
    # its own first message, often before attaching a hotel link/photo --
    # the code must be held from that message all the way to confirm.
    recorded = []
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr(
        "yta.leads.db.record_lead",
        lambda phone, status, packet, resolution, referred_by=None: recorded.append(referred_by) or "BMS-NEW00001",
    )
    v4.handle_batch("cust", [{"type": "text", "text": "Hi, I was referred by BMS-7F3A9C2E"}])
    assert v4._PENDING_REFERRALS.get("cust") == "BMS-7F3A9C2E"

    _open_presented()
    v4.handle_batch("cust", [{"type": "button_reply", "button_id": "confirm_book", "text": "Yes, book this"}])
    assert recorded == ["BMS-7F3A9C2E"]
    assert "cust" not in v4._PENDING_REFERRALS   # consumed, not left lying around


def test_referral_mention_alongside_a_link_is_captured_immediately(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: "https://booking.com/x")
    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: _Packet(missing=["ota_benchmark.final_payable"]))
    v4.handle_batch("cust", [{"type": "text",
                               "text": "referred by BMS-AAAA1111 -- https://booking.com/x"}])
    assert v4._PENDING_REFERRALS.get("cust") == "BMS-AAAA1111"


def test_no_referral_mention_means_no_attribution(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.leads.db.record_lead",
                         lambda phone, status, packet, resolution, referred_by=None: recorded.append(referred_by) or "BMS-X")
    recorded = []
    _open_presented()
    v4.handle_batch("cust", [{"type": "button_reply", "button_id": "confirm_book", "text": "Yes, book this"}])
    assert recorded == [None]
