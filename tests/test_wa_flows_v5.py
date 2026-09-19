"""Routing tests for v5 (yta/wa_flows/v5.py) — carried over from v3
unmodified (confirm/decline step, bounded slot-filling cap, concierge-voice
message building, the referral loop) as a regression baseline, PLUS new
tests for v5's own addition: a plain-text message with no open session can
now start a submission on its own, not just a URL or a photo. v0/v1/v2/v3's
own test files are untouched, and re-run here unmodified as part of the
full suite to prove none of them were touched by this work.

No network, no real LLM — whatsapp.find_url/download_media/send_buttons,
pipeline.extract, extract_llm.extract_clarification, yta.web._resolve,
and yta.leads.db.record_lead are all faked/monkeypatched.
"""
import time

import pytest

from yta.wa_flows import v5


@pytest.fixture(autouse=True)
def _clean_sessions():
    v5._WA_SESSIONS.clear()
    v5._PENDING_REFERRALS.clear()
    yield
    v5._WA_SESSIONS.clear()
    v5._PENDING_REFERRALS.clear()


@pytest.fixture
def sent(monkeypatch):
    messages = []
    monkeypatch.setattr(v5, "wa_send", lambda frm, text: messages.append(("text", text)))

    def _fake_send_buttons(to, body, buttons):
        messages.append(("buttons", body, buttons))

    monkeypatch.setattr("yta.whatsapp.send_buttons", _fake_send_buttons)
    return messages


class _Packet:
    status = "ok"
    missing_mandatory = []

    def __init__(self, missing=(), hotel_name="Test Hotel", room_name="Deluxe Room",
                description=None):
        self._still_missing = list(missing)
        self.hotel = type("H", (), {"name": hotel_name})()

        class _Stay:
            check_in = "2026-09-21"
            check_out = "2026-09-22"
            occupancy = [{"adults": 2, "children": 0, "child_ages": []}]
        self.stay = _Stay()

        self.requested_offer = type("O", (), {
            "room_name": room_name, "description": description,
            "meal_plan": None, "refundable": None})()

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


def _open_awaiting(missing=("ota_benchmark.final_payable",), age_sec=0, attempts=0, intent=None):
    v5._WA_SESSIONS["cust"] = {
        "state": "awaiting_field", "packet": _Packet(missing), "missing": list(missing),
        "intent": intent, "unproductive_attempts": attempts, "last_activity": time.time() - age_sec,
    }


def _open_presented(age_sec=0, resolution=None, savings_line=None, confirm_line=None):
    v5._WA_SESSIONS["cust"] = {
        "state": "presented", "packet": _Packet(), "resolution": resolution or {},
        "savings_line": savings_line, "confirm_line": confirm_line,
        "last_activity": time.time() - age_sec,
    }


def _open_choosing_option(options, age_sec=0, attempts=0, resolution=None):
    v5._WA_SESSIONS["cust"] = {
        "state": "choosing_option", "packet": _Packet(), "resolution": resolution or {},
        "options": options, "unproductive_attempts": attempts,
        "last_activity": time.time() - age_sec,
    }


# -- onboarding ------------------------------------------------------

def test_onboarding_is_now_a_choice_between_deal_and_search(sent):
    v5.handle_batch("cust", [{"type": "text", "text": "hi"}])
    kind, body, buttons = sent[0]
    assert kind == "buttons"
    assert body == v5._ONBOARDING_CHOICE_TEXT
    assert [bid for bid, _ in buttons] == ["have_deal", "search_hotel"]


def test_have_deal_tap_names_what_to_send(sent):
    v5.handle_batch("cust", [{"type": "button_reply", "button_id": "have_deal", "text": "I have a deal"}])
    text = next(m[1] for m in sent if m[0] == "text")
    assert "link" in text.lower() and "room" in text.lower() and "price" in text.lower()


def test_search_hotel_tap_never_asks_for_a_room_or_price(sent):
    v5.handle_batch("cust", [{"type": "button_reply", "button_id": "search_hotel", "text": "Search a hotel"}])
    text = next(m[1] for m in sent if m[0] == "text").lower()
    assert "price" not in text          # no OTA price to compare against on this path
    assert "no need to pick a room" in text
    assert "hotel" in text and "dates" in text


def test_onboarding_never_mentions_the_sourcing_mechanism(sent):
    # The bot's own edge (wholesale rates) is deliberately never named to
    # a customer -- the pitch stays outcome-focused ("a better rate"),
    # not mechanism-focused.
    v5.handle_batch("cust", [{"type": "text", "text": "hi"}])
    text = next(m[1] for m in sent if m[0] == "buttons")
    assert "wholesale" not in text.lower()


def test_onboarding_never_calls_itself_a_checker_or_bot(sent):
    v5.handle_batch("cust", [{"type": "text", "text": "hi"}])
    text = next(m[1] for m in sent if m[0] == "buttons")
    assert "checker" not in text.lower() and "bot" not in text.lower()


# -- v5's own addition: plain text as a submission, no session ----------

def test_greeting_never_triggers_extraction(sent, monkeypatch):
    calls = []
    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: calls.append(1) or _Packet())
    v5.handle_batch("cust", [{"type": "text", "text": "hi"}])
    assert calls == []                      # the pre-filter rejected it for free
    kind, body, _ = sent[0]
    assert kind == "buttons" and body == v5._ONBOARDING_CHOICE_TEXT


def test_short_non_chitchat_text_also_skips_extraction(sent, monkeypatch):
    # Below the length floor but not literally a greeting -- the length
    # filter, not just the chit-chat wordlist, has to reject this too.
    calls = []
    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: calls.append(1) or _Packet())
    v5.handle_batch("cust", [{"type": "text", "text": "book pls"}])
    assert calls == []
    kind, body, _ = sent[0]
    assert kind == "buttons" and body == v5._ONBOARDING_CHOICE_TEXT


def test_free_text_query_with_everything_present_reaches_the_deal(sent, monkeypatch):
    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: _Packet(missing=[]))
    monkeypatch.setattr("yta.web._resolve", lambda packet: {"room_map": {"matched": False}})
    v5.handle_batch("cust", [{"type": "text",
                    "text": "Taj Palace Delhi, Oct 15-17, 2 adults, saw INR 15000 on MMT"}])
    # same shape a fully-informative photo/link submission produces: a
    # recap first, then the buttons message from _present_deal.
    recap_msgs = [m[1] for m in sent if m[0] == "text" and "Test Hotel" in m[1]]
    assert recap_msgs, "expected a recap message showing what was read"
    assert sent[-1][0] == "buttons"


def test_free_text_query_with_missing_fields_opens_awaiting_field(sent, monkeypatch):
    monkeypatch.setattr("yta.pipeline.extract",
                         lambda *a, **kw: _Packet(missing=["ota_benchmark.final_payable"]))
    v5.handle_batch("cust", [{"type": "text",
                    "text": "Taj Palace Delhi, Oct 15-17, 2 adults, deluxe room"}])
    assert "cust" in v5._WA_SESSIONS
    assert v5._WA_SESSIONS["cust"]["state"] == "awaiting_field"
    body = next(m[1] for m in sent if m[0] == "buttons")
    assert "Test Hotel" in body


def test_free_text_with_no_recognizable_hotel_gets_a_soft_nudge_not_a_loop(sent, monkeypatch):
    # Long enough to pass the pre-filter, but the extractor found nothing
    # usable -- should NOT drag the customer into a full slot-filling
    # interrogation seeded from a stray sentence.
    monkeypatch.setattr("yta.pipeline.extract",
                         lambda *a, **kw: _Packet(missing=["hotel.name"], hotel_name=None))
    v5.handle_batch("cust", [{"type": "text",
                    "text": "just wondering what kind of deals you folks usually find"}])
    assert "cust" not in v5._WA_SESSIONS               # no half-open interrogation
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
    v5.handle_batch("cust", [{"type": "text",
                    "text": "actually it's the Grand Hyatt Mumbai, Oct 20-22, 2 adults, saw INR 20000"}])
    assert sent[-1][0] == "buttons"   # resolved via _handle_awaiting_field -> _present_deal
    assert "cust" not in v5._WA_SESSIONS


# -- structured recap + natural question ---------------------------------

def test_missing_field_ask_shows_a_structured_recap_and_a_real_question(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: "https://booking.com/x")
    monkeypatch.setattr("yta.pipeline.extract",
                         lambda *a, **kw: _Packet(missing=["ota_benchmark.final_payable"]))
    v5.handle_batch("cust", [{"type": "text", "text": "https://booking.com/x"}])
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
    v5.handle_batch("cust", [{"type": "text", "text": "https://booking.com/x"}])
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
    v5.handle_batch("cust", [{"type": "text", "text": "https://booking.com/x"}])
    recap_msgs = [m[1] for m in sent if m[0] == "text" and "Test Hotel" in m[1]]
    assert recap_msgs, "expected a recap message showing what was read"


def test_recap_includes_ota_price_when_already_known(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: "https://booking.com/x")
    monkeypatch.setattr("yta.pipeline.extract",
                         lambda *a, **kw: _Packet(missing=["requested_offer.room_name"]))
    v5.handle_batch("cust", [{"type": "text", "text": "https://booking.com/x"}])
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
    v5.handle_batch("cust", [{"type": "text", "text": "31683"}])
    assert sent[-1][0] == "buttons"   # reached _present_deal, not re-treated as a fresh submission


def test_abandoned_session_past_the_hygiene_backstop_is_cleared(sent, monkeypatch):
    # The 24h backstop is memory hygiene for a number that never comes
    # back, not a UX judgment -- still worth confirming it actually clears.
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    _open_awaiting(age_sec=v5._SESSION_MAX_AGE_SEC + 60)
    v5.handle_batch("cust", [{"type": "text", "text": "31683"}])
    assert "cust" not in v5._WA_SESSIONS
    # a bare number with no hotel context left should get the onboarding
    # choice again, not be silently absorbed as an answer to the
    # (now-cleared) question
    kind, body, _ = sent[0]
    assert kind == "buttons" and body == v5._ONBOARDING_CHOICE_TEXT


# -- bounded slot-filling ----------------------------------------------

def test_gives_up_after_max_unproductive_attempts(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.extract_llm.extract_clarification",
                         lambda missing, text, media=None: ({}, None))

    _open_awaiting(attempts=0)
    v5.handle_batch("cust", [{"type": "text", "text": "hi there"}])
    assert "cust" in v5._WA_SESSIONS   # attempt 1 of 2 -- still open
    assert v5._WA_SESSIONS["cust"]["unproductive_attempts"] == 1

    v5.handle_batch("cust", [{"type": "text", "text": "still unrelated"}])
    assert "cust" not in v5._WA_SESSIONS   # attempt 2 -- cap reached, gave up
    assert any("start fresh" in m[1] for m in sent if m[0] == "text")
    kind, body, _ = sent[-1]   # the LAST message -- attempt 1's ask sent buttons too
    assert kind == "buttons" and body == v5._ONBOARDING_CHOICE_TEXT


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
    v5.handle_batch("cust", [{"type": "text", "text": "a"}])
    v5.handle_batch("cust", [{"type": "text", "text": "b"}])
    assert v5._WA_SESSIONS["cust"]["unproductive_attempts"] == 0
    v5.handle_batch("cust", [{"type": "text", "text": "c"}])
    assert "cust" in v5._WA_SESSIONS   # still open -- this was only attempt 1 since the reset
    assert v5._WA_SESSIONS["cust"]["unproductive_attempts"] == 1


# -- presented: confirm / decline ---------------------------------------

def test_confirm_button_records_lead_and_replies_with_reference(sent, monkeypatch):
    recorded = []
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.leads.db.record_lead",
                         lambda phone, status, packet, resolution, referred_by=None: recorded.append((phone, status)) or "BMS-TEST1234")
    _open_presented()
    v5.handle_batch("cust", [{"type": "button_reply", "button_id": "confirm_book", "text": "Yes, book this"}])
    assert recorded == [("cust", "confirmed")]
    assert "cust" not in v5._WA_SESSIONS
    assert any("BMS-TEST1234" in m[1] for m in sent if m[0] == "text")


def test_confirm_message_names_what_was_actually_booked(sent, monkeypatch):
    # The confirm note used to be a bare "I've noted this down" -- it
    # should say what was actually secured and for how much, not just
    # hand back a reference number.
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.leads.db.record_lead",
                         lambda phone, status, packet, resolution, referred_by=None: "BMS-TEST5555")
    _open_presented(confirm_line="I've secured Test Hotel for INR 28,015.64 (INR 3,667 less than what you had).")
    v5.handle_batch("cust", [{"type": "button_reply", "button_id": "confirm_book", "text": "Yes, book this"}])
    confirm_text = next(m[1] for m in sent if m[0] == "text" and "BMS-TEST5555" in m[1])
    assert "I've secured Test Hotel for INR 28,015.64" in confirm_text
    assert "3,667 less than what you had" in confirm_text


def test_confirm_message_falls_back_gracefully_with_no_confirm_line(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.leads.db.record_lead",
                         lambda phone, status, packet, resolution, referred_by=None: "BMS-TEST6666")
    _open_presented(confirm_line=None)
    v5.handle_batch("cust", [{"type": "button_reply", "button_id": "confirm_book", "text": "Yes, book this"}])
    confirm_text = next(m[1] for m in sent if m[0] == "text" and "BMS-TEST6666" in m[1])
    assert "I've noted this down" in confirm_text


def test_typed_yes_works_same_as_the_button(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.leads.db.record_lead",
                         lambda phone, status, packet, resolution, referred_by=None: "BMS-TEST5678")
    _open_presented()
    v5.handle_batch("cust", [{"type": "text", "text": "yes please"}])
    assert "cust" not in v5._WA_SESSIONS
    assert any("BMS-TEST5678" in m[1] for m in sent if m[0] == "text")


def test_decline_button_records_lead_and_does_not_block_next_hotel(sent, monkeypatch):
    recorded = []
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.leads.db.record_lead",
                         lambda phone, status, packet, resolution, referred_by=None: recorded.append(status) or "BMS-DECL0000")
    _open_presented()
    v5.handle_batch("cust", [{"type": "button_reply", "button_id": "decline_book", "text": "Not now"}])
    assert recorded == ["declined"]
    assert "cust" not in v5._WA_SESSIONS


def test_unrecognized_reply_in_presented_state_reprompts_and_stays_open(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    _open_presented()
    v5.handle_batch("cust", [{"type": "text", "text": "what's the cancellation policy?"}])
    assert "cust" in v5._WA_SESSIONS
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
    v5._present_deal("cust", _Packet())
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
    v5._present_deal("cust", _Packet())
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
    v5.handle_batch("cust", [{"type": "text", "text": "https://booking.com/x"}])
    ask_body = next(m[1] for m in sent if m[0] == "buttons")
    assert "🏨" in ask_body and "📅" in ask_body and "🛏️" in ask_body

    sent.clear()
    monkeypatch.setattr(
        "yta.web._resolve",
        lambda packet: {"room_map": {"matched": True, "ratekey_option_ids": ["o1"], "rate_options": [
            {"option_id": "o1", "room_name": "Deluxe Room", "currency": "INR", "total_price": 28015.64},
        ]}},
    )
    v5._present_deal("cust", _Packet())
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
    v5._present_deal("cust", _Packet())
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
    assert "cust" in v5._WA_SESSIONS and v5._WA_SESSIONS["cust"]["state"] == "presented"


def test_deal_card_uses_tripjacks_own_hotel_name_not_the_customers_typed_one(sent, monkeypatch):
    # Regression, live: a customer typed "Taj sanracruz" and the deal card
    # echoed that misspelling back verbatim instead of showing TripJack's
    # own record ("Taj Santacruz") from the Detail/Pricing response it had
    # just called. Once a live rate exists, TripJack's own name is what's
    # actually being booked -- that's what should show, not the customer's
    # typo. `detail.hotel_name` (the live pricing response) wins even over
    # `Test Hotel` from the packet itself.
    monkeypatch.setattr(
        "yta.web._resolve",
        lambda packet: {
            "detail": {"hotel_name": "Taj Santacruz"},
            "room_map": {"matched": True, "ratekey_option_ids": ["o1"], "rate_options": [
                {"option_id": "o1", "room_name": "Deluxe Room", "meal_basis": "Breakfast",
                 "refundable": True, "currency": "INR", "total_price": 28015.64},
            ]},
        },
    )
    v5._present_deal("cust", _Packet())
    body = next(m[1] for m in sent if m[0] == "buttons")
    assert "Taj Santacruz" in body
    assert "Test Hotel" not in body


def test_deal_card_falls_back_to_hoteldb_match_name_when_no_live_detail(sent, monkeypatch):
    # `match.hotel_name` (the earlier hotel-id resolution step, against
    # TripJack's own catalog) is the second-best TripJack source, used only
    # when the live pricing response didn't carry its own hotelName.
    monkeypatch.setattr(
        "yta.web._resolve",
        lambda packet: {
            "match": {"hotel_name": "Taj Santacruz (Catalog)"},
            "room_map": {"matched": True, "ratekey_option_ids": ["o1"], "rate_options": [
                {"option_id": "o1", "room_name": "Deluxe Room", "currency": "INR", "total_price": 28015.64},
            ]},
        },
    )
    v5._present_deal("cust", _Packet())
    body = next(m[1] for m in sent if m[0] == "buttons")
    assert "Taj Santacruz (Catalog)" in body
    assert "Test Hotel" not in body


def test_deal_card_room_name_never_falls_back_to_the_customers_requested_room(sent, monkeypatch):
    # room_name/meal/refundable in the deal card must come strictly from
    # the matched TripJack option -- never backfilled from what the
    # customer originally asked for, even if TripJack's own field for one
    # of them happens to be empty.
    monkeypatch.setattr(
        "yta.web._resolve",
        lambda packet: {"room_map": {"matched": True, "ratekey_option_ids": ["o1"], "rate_options": [
            {"option_id": "o1", "room_name": "", "meal_basis": "", "refundable": None,
             "currency": "INR", "total_price": 28015.64},
        ]}},
    )
    v5._present_deal("cust", _Packet())
    body = next(m[1] for m in sent if m[0] == "buttons")
    assert "Deluxe Room" not in body   # _Packet()'s requested_offer.room_name -- must not leak in
    assert "🛏️" not in body            # nothing TripJack-sourced to show -- the line is omitted, not backfilled


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
    v5._present_deal("cust", _Packet())
    assert not any(m[0] == "buttons" for m in sent)
    assert any("nothing better to offer" in m[1] for m in sent if m[0] == "text")
    assert "cust" not in v5._WA_SESSIONS


def test_no_live_rate_offers_try_another_button(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: "https://booking.com/x")
    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: _Packet())
    monkeypatch.setattr("yta.web._resolve", lambda packet: {"available": False, "room_map": {"matched": False}})

    v5.handle_batch("cust", [{"type": "text", "text": "https://booking.com/x"}])
    assert "cust" not in v5._WA_SESSIONS
    assert any(m[0] == "buttons" and any(bid == "try_another" for bid, _ in m[2]) for m in sent)


def test_try_another_button_sends_onboarding_choice(sent):
    v5.handle_batch("cust", [{"type": "button_reply", "button_id": "try_another", "text": "Try another hotel"}])
    kind, body, _ = sent[0]
    assert kind == "buttons" and body == v5._ONBOARDING_CHOICE_TEXT


# -- global cancel / start-over -------------------------------------------

def test_start_new_chat_button_clears_any_open_session(sent):
    _open_awaiting()
    v5.handle_batch("cust", [{"type": "button_reply", "button_id": "start_new_chat", "text": "Start over"}])
    assert "cust" not in v5._WA_SESSIONS
    assert any("start fresh" in m[1].lower() for m in sent if m[0] == "text")


def test_start_over_re_offers_the_onboarding_choice(sent):
    # Regression: starting over used to leave the customer with a bare
    # acknowledgment and no next step -- the deal/search choice needs to
    # come back so the two-path flow actually restarts.
    _open_awaiting()
    v5.handle_batch("cust", [{"type": "button_reply", "button_id": "start_new_chat", "text": "Start over"}])
    kind, body, buttons = next(m for m in sent if m[0] == "buttons")
    assert body == v5._ONBOARDING_CHOICE_TEXT
    assert [bid for bid, _ in buttons] == ["have_deal", "search_hotel"]


def test_cancel_with_no_open_session_still_offers_the_choice(sent):
    v5.handle_batch("cust", [{"type": "text", "text": "never mind"}])
    kind, body, buttons = next(m for m in sent if m[0] == "buttons")
    assert body == v5._ONBOARDING_CHOICE_TEXT


def test_start_over_clears_any_pending_path(sent):
    v5._PENDING_PATH["cust"] = "search"
    _open_awaiting()
    v5.handle_batch("cust", [{"type": "button_reply", "button_id": "start_new_chat", "text": "Start over"}])
    assert "cust" not in v5._PENDING_PATH


@pytest.mark.parametrize("phrase", ["start again", "Start this again", "restart", "RESTART please"])
def test_natural_restart_phrases_are_recognized_as_cancel(sent, phrase):
    # Regression, live: a customer typed "Start again" / "Start this
    # again" mid-conversation expecting a reset -- neither matched the
    # old cancel regex, so both got silently absorbed as failed answers
    # to whatever question was open, burning through the unproductive-
    # attempt cap before the bot gave up on its own.
    _open_awaiting()
    v5.handle_batch("cust", [{"type": "text", "text": phrase}])
    assert "cust" not in v5._WA_SESSIONS
    kind, body, _ = sent[-1]
    assert kind == "buttons" and body == v5._ONBOARDING_CHOICE_TEXT


def test_missing_field_ask_carries_a_start_new_chat_button(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: "https://booking.com/x")
    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: _Packet(missing=["ota_benchmark.final_payable"]))
    v5.handle_batch("cust", [{"type": "text", "text": "https://booking.com/x"}])
    assert any(m[0] == "buttons" and any(bid == "start_new_chat" for bid, _ in m[2]) for m in sent)


# -- the referral loop: new in v5, not in v2 ------------------------------

def test_confirming_asks_for_a_referral_and_a_deep_link(sent, monkeypatch):
    monkeypatch.setenv("WHATSAPP_DISPLAY_NUMBER", "919999999999")
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.leads.db.record_lead",
                         lambda phone, status, packet, resolution, referred_by=None: "BMS-TEST1234")
    _open_presented(confirm_line="I've secured Test Hotel for INR 28,015.64 (INR 3,667 less than what you had).")
    v5.handle_batch("cust", [{"type": "button_reply", "button_id": "confirm_book", "text": "Yes, book this"}])
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
    v5.handle_batch("cust", [{"type": "button_reply", "button_id": "confirm_book", "text": "Yes, book this"}])
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
    v5.handle_batch("cust", [{"type": "button_reply", "button_id": "confirm_book", "text": "Yes, book this"}])
    texts = [m[1] for m in sent if m[0] == "text"]
    shareable = next(t for t in texts if "wa.me/" in t)
    assert "Forward" not in shareable and "Tap and hold" not in shareable
    assert shareable.startswith("I just found a better hotel rate")


def test_decline_does_not_ask_for_a_referral(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.leads.db.record_lead",
                         lambda phone, status, packet, resolution, referred_by=None: "BMS-TEST0002")
    _open_presented(savings_line="You just saved INR 3,667 (12%) on this one.")
    v5.handle_batch("cust", [{"type": "button_reply", "button_id": "decline_book", "text": "Not now"}])
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
    v5.handle_batch("cust", [{"type": "text", "text": "Hi, I was referred by BMS-7F3A9C2E"}])
    assert v5._PENDING_REFERRALS.get("cust") == "BMS-7F3A9C2E"

    _open_presented()
    v5.handle_batch("cust", [{"type": "button_reply", "button_id": "confirm_book", "text": "Yes, book this"}])
    assert recorded == ["BMS-7F3A9C2E"]
    assert "cust" not in v5._PENDING_REFERRALS   # consumed, not left lying around


def test_referral_mention_alongside_a_link_is_captured_immediately(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: "https://booking.com/x")
    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: _Packet(missing=["ota_benchmark.final_payable"]))
    v5.handle_batch("cust", [{"type": "text",
                               "text": "referred by BMS-AAAA1111 -- https://booking.com/x"}])
    assert v5._PENDING_REFERRALS.get("cust") == "BMS-AAAA1111"


def test_no_referral_mention_means_no_attribution(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.leads.db.record_lead",
                         lambda phone, status, packet, resolution, referred_by=None: recorded.append(referred_by) or "BMS-X")
    recorded = []
    _open_presented()
    v5.handle_batch("cust", [{"type": "button_reply", "button_id": "confirm_book", "text": "Yes, book this"}])
    assert recorded == [None]


# -- v5's own addition: the deal/search choice and its downstream effect -

def test_effective_missing_default_is_unchanged_from_schema():
    assert v5._effective_missing(_Packet(), [], None) == []
    assert v5._effective_missing(_Packet(), ["ota_benchmark.final_payable"], None) \
        == ["ota_benchmark.final_payable"]


def test_effective_missing_deal_intent_adds_room_name_back_when_a_hint_exists():
    # A description exists but didn't resolve to a clean room_name --
    # worth asking, since there's clearly SOME room in mind.
    p = _Packet(room_name=None, description="a lovely room with a view")
    assert v5._effective_missing(p, [], "deal") == ["requested_offer.room_name"]
    # already present in the list -- not duplicated
    assert v5._effective_missing(p, ["requested_offer.room_name"], "deal") \
        == ["requested_offer.room_name"]
    # room already given -- nothing added
    assert v5._effective_missing(_Packet(room_name="Deluxe Room"), [], "deal") == []


def test_effective_missing_deal_intent_does_not_force_room_with_no_hint_at_all():
    # Neither room_name NOR description at all -- the customer plainly
    # doesn't have a specific room in mind despite tapping "I have a
    # deal". Falls through instead of stalling on a field they don't
    # have -- same as bypassing the buttons entirely (intent=None).
    p = _Packet(room_name=None, description=None)
    assert v5._effective_missing(p, [], "deal") == []


def test_effective_missing_search_intent_drops_price():
    assert v5._effective_missing(_Packet(), ["ota_benchmark.final_payable"], "search") == []
    assert v5._effective_missing(
        _Packet(), ["ota_benchmark.final_payable", "stay.check_in"], "search") == ["stay.check_in"]


def test_have_deal_path_still_asks_for_a_room_when_a_hint_exists(sent, monkeypatch):
    # schema.py no longer makes room_name mandatory, so a real extraction
    # with no room would normally report missing=[] -- but on the "I have
    # a deal" path, with SOME room detail to go on, it should still be
    # asked for.
    v5._PENDING_PATH["cust"] = "deal"
    monkeypatch.setattr("yta.pipeline.extract",
                         lambda *a, **kw: _Packet(missing=[], room_name=None,
                                                  description="a lovely room with a view"))
    v5.handle_batch("cust", [{"type": "text", "text": "https://booking.com/x", }])
    body = next(m[1] for m in sent if m[0] == "buttons")
    assert "room" in body.lower()
    assert v5._WA_SESSIONS["cust"]["state"] == "awaiting_field"
    assert v5._WA_SESSIONS["cust"]["missing"] == ["requested_offer.room_name"]


def test_have_deal_path_falls_through_to_options_with_no_room_hint_at_all(sent, monkeypatch):
    # Neither room_name nor description at all, despite tapping "I have a
    # deal" -- rather than nagging for a room that was never coming, it
    # proceeds exactly like the search path would.
    v5._PENDING_PATH["cust"] = "deal"
    monkeypatch.setattr("yta.pipeline.extract",
                         lambda *a, **kw: _Packet(missing=[], room_name=None, description=None))
    monkeypatch.setattr(
        "yta.web._resolve",
        lambda packet: {"room_options": {"groups": [
            {"room_type_id": "R1", "room_name": "Deluxe Room", "total_combos": 1, "options": [
                {"option_id": "s1", "room_name": "Deluxe Room", "meal_basis": "Breakfast",
                 "refundable": True, "currency": "INR", "total_price": 20000.0},
            ]},
        ]}},
    )
    v5.handle_batch("cust", [{"type": "text", "text": "https://booking.com/x", }])
    assert v5._WA_SESSIONS["cust"]["state"] == "choosing_option"
    assert not any(m[0] == "buttons" and "start_new_chat" in [bid for bid, _ in m[2]]
                   for m in sent)   # never entered awaiting_field asking for a room


def test_search_hotel_path_proceeds_without_a_price(sent, monkeypatch):
    v5._PENDING_PATH["cust"] = "search"
    monkeypatch.setattr("yta.pipeline.extract",
                         lambda *a, **kw: _Packet(missing=["ota_benchmark.final_payable"], room_name=None))
    monkeypatch.setattr(
        "yta.web._resolve",
        lambda packet: {"room_options": {"groups": [
            {"room_type_id": "R1", "room_name": "Deluxe Room", "total_combos": 1, "options": [
                {"option_id": "s1", "room_name": "Deluxe Room", "meal_basis": "Breakfast",
                 "refundable": True, "currency": "INR", "total_price": 20000.0},
            ]},
        ]}},
    )
    v5.handle_batch("cust", [{"type": "text", "text": "Taj Santacruz, Sep 21-22, 2 adults"}])
    # never got stuck asking for the price -- went straight to presenting options
    assert v5._WA_SESSIONS["cust"]["state"] == "choosing_option"
    assert any("here's what's available" in m[1].lower() for m in sent if m[0] == "text")


def _rate(oid, room_name, meal, refundable, price):
    return {"option_id": oid, "room_name": room_name, "meal_basis": meal,
            "refundable": refundable, "currency": "INR", "total_price": price}


def test_present_option_choices_shows_each_room_name_once_with_its_own_variants(sent):
    groups = [
        {"room_type_id": "R1", "room_name": "Deluxe Villa", "total_combos": 2, "options": [
            _rate("r1", "Deluxe Villa", "Room Only", False, 20000.0),
            _rate("r2", "Deluxe Villa", "Breakfast", False, 21000.0),
        ]},
        {"room_type_id": "R2", "room_name": "Premier Villa", "total_combos": 1, "options": [
            _rate("p1", "Premier Villa", "Room Only", True, 25000.0),
        ]},
    ]
    resolution = {"detail": {"hotel_name": "Taj Santacruz"}, "room_options": {"groups": groups}}
    v5._present_option_choices("cust", _Packet(), resolution)
    text = next(m[1] for m in sent if m[0] == "text")
    assert "Taj Santacruz" in text
    # each room name appears exactly once, even though R1 has 2 lines under it
    assert text.count("Deluxe Villa") == 1
    assert text.count("Premier Villa") == 1
    # numbering is sequential ACROSS rooms, not restarting per room
    assert "1. Room Only" in text and "2. Breakfast" in text and "3. Room Only" in text
    assert "Reply with a number (1–3)" in text
    assert "20,000.00" in text and "21,000.00" in text and "25,000.00" in text
    # session stores the FLATTENED list -- index 2 (picking "3") is p1
    assert [o["option_id"] for o in v5._WA_SESSIONS["cust"]["options"]] == ["r1", "r2", "p1"]


def test_present_option_choices_with_no_options_offers_try_another(sent):
    resolution = {"detail": {"hotel_name": "Taj Santacruz"}, "room_options": {"groups": []}}
    v5._present_option_choices("cust", _Packet(), resolution)
    kind, body, buttons = next(m for m in sent if m[0] == "buttons")
    assert "Taj Santacruz" in body
    assert [bid for bid, _ in buttons] == ["try_another"]
    assert "cust" not in v5._WA_SESSIONS


def test_picking_an_option_by_number_leads_to_the_same_confirm_flow(sent):
    options = [
        {"option_id": "s1", "room_name": "Deluxe Room", "meal_basis": "Breakfast",
         "refundable": True, "currency": "INR", "total_price": 20000.0},
        {"option_id": "c2", "room_name": "Premier Room", "meal_basis": "Half Board",
         "refundable": False, "currency": "INR", "total_price": 25000.0},
    ]
    _open_choosing_option(options, resolution={"detail": {"hotel_name": "Taj Santacruz"}})
    v5.handle_batch("cust", [{"type": "text", "text": "2"}])
    body = next(m[1] for m in sent if m[0] == "buttons")
    assert "Premier Room" in body
    assert "Taj Santacruz" in body           # TripJack hotel name carried through
    assert v5._WA_SESSIONS["cust"]["state"] == "presented"
    rm = v5._WA_SESSIONS["cust"]["resolution"]["room_map"]
    assert rm["matched"] is True and rm["ratekey_option_ids"] == ["c2"]


def test_picking_an_invalid_number_reprompts_then_gives_up(sent):
    options = [{"option_id": "s1", "room_name": "Deluxe Room", "meal_basis": "Breakfast",
                "refundable": True, "currency": "INR", "total_price": 20000.0}]
    _open_choosing_option(options)
    v5.handle_batch("cust", [{"type": "text", "text": "banana"}])
    assert "cust" in v5._WA_SESSIONS   # attempt 1 of 2 -- still open
    assert any("reply with a number" in m[1].lower() for m in sent if m[0] == "text")

    v5.handle_batch("cust", [{"type": "text", "text": "still not a number"}])
    assert "cust" not in v5._WA_SESSIONS   # attempt 2 -- gave up
    assert any("start fresh" in m[1].lower() for m in sent if m[0] == "text")
    kind, body, _ = next(m for m in sent if m[0] == "buttons")
    assert body == v5._ONBOARDING_CHOICE_TEXT   # re-offers the choice, not silence


def test_confirming_a_picked_option_records_the_lead_normally(sent, monkeypatch):
    recorded = []
    monkeypatch.setattr("yta.leads.db.record_lead",
                         lambda phone, status, packet, resolution, referred_by=None:
                             recorded.append((status, resolution["room_map"]["rate_options"][0]["option_id"]))
                             or "BMS-PICKED1")
    options = [{"option_id": "c2", "room_name": "Premier Room", "meal_basis": "Half Board",
                "refundable": False, "currency": "INR", "total_price": 25000.0}]
    _open_choosing_option(options, resolution={"detail": {"hotel_name": "Taj Santacruz"}})
    v5.handle_batch("cust", [{"type": "text", "text": "1"}])
    assert v5._WA_SESSIONS["cust"]["state"] == "presented"

    v5.handle_batch("cust", [{"type": "button_reply", "button_id": "confirm_book", "text": "Yes, book this"}])
    assert recorded == [("confirmed", "c2")]
    assert "cust" not in v5._WA_SESSIONS
