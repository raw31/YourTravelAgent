"""Routing tests for v6 (yta/wa_flows/v6.py) — carried over from v5 unmodified
(the onboarding choice, bounded slot-filling cap, concierge-voice message
building, the referral loop, room-options listing) as a regression baseline,
PLUS new tests for v6's own addition: an unhandled error no longer wipes the
session or shows the customer the raw exception, and offers a real
Try again / Start over choice instead. v0-v5's own test files are untouched,
and re-run here unmodified as part of the full suite to prove none of them
were touched by this work.

No network, no real LLM — whatsapp.find_url/download_media/send_buttons,
pipeline.extract, extract_llm.extract_clarification, yta.web._resolve,
and yta.leads.db.record_lead are all faked/monkeypatched.
"""
import time

import pytest

from yta.wa_flows import v6


@pytest.fixture(autouse=True)
def _clean_sessions():
    v6._WA_SESSIONS.clear()
    v6._PENDING_REFERRALS.clear()
    v6._PENDING_PATH.clear()
    v6._LAST_BATCH_ITEMS.clear()
    yield
    v6._WA_SESSIONS.clear()
    v6._PENDING_REFERRALS.clear()
    v6._PENDING_PATH.clear()
    v6._LAST_BATCH_ITEMS.clear()


@pytest.fixture
def sent(monkeypatch):
    messages = []
    monkeypatch.setattr(v6, "wa_send", lambda frm, text: messages.append(("text", text)))
    monkeypatch.setattr(v6, "wa_send_image",
                        lambda frm, url, caption=None: messages.append(("image", url, caption)))

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
    v6._WA_SESSIONS["cust"] = {
        "state": "awaiting_field", "packet": _Packet(missing), "missing": list(missing),
        "intent": intent, "unproductive_attempts": attempts, "last_activity": time.time() - age_sec,
    }


def _open_presented(age_sec=0, resolution=None, savings_line=None, confirm_line=None):
    v6._WA_SESSIONS["cust"] = {
        "state": "presented", "packet": _Packet(), "resolution": resolution or {},
        "savings_line": savings_line, "confirm_line": confirm_line,
        "last_activity": time.time() - age_sec,
    }


def _open_choosing_option(options, age_sec=0, attempts=0, resolution=None):
    v6._WA_SESSIONS["cust"] = {
        "state": "choosing_option", "packet": _Packet(), "resolution": resolution or {},
        "options": options, "unproductive_attempts": attempts,
        "last_activity": time.time() - age_sec,
    }


# -- onboarding ------------------------------------------------------

def test_onboarding_is_now_a_choice_between_deal_and_search(sent):
    v6.handle_batch("cust", [{"type": "text", "text": "hi"}])
    kind, body, buttons = sent[0]
    assert kind == "buttons"
    assert body == v6._ONBOARDING_CHOICE_TEXT
    assert [bid for bid, _ in buttons] == ["have_deal", "search_hotel"]


def test_have_deal_tap_names_what_to_send(sent):
    v6.handle_batch("cust", [{"type": "button_reply", "button_id": "have_deal", "text": "I have a deal"}])
    text = next(m[1] for m in sent if m[0] == "text")
    assert "link" in text.lower() and "room" in text.lower() and "price" in text.lower()


def test_search_hotel_tap_never_asks_for_a_room_or_price(sent):
    v6.handle_batch("cust", [{"type": "button_reply", "button_id": "search_hotel", "text": "Search a hotel"}])
    text = next(m[1] for m in sent if m[0] == "text").lower()
    assert "price" not in text          # no OTA price to compare against on this path
    assert "no need to pick a room" in text
    assert "hotel" in text and "dates" in text


def test_onboarding_never_mentions_the_sourcing_mechanism(sent):
    # The bot's own edge (wholesale rates) is deliberately never named to
    # a customer -- the pitch stays outcome-focused ("a better rate"),
    # not mechanism-focused.
    v6.handle_batch("cust", [{"type": "text", "text": "hi"}])
    text = next(m[1] for m in sent if m[0] == "buttons")
    assert "wholesale" not in text.lower()


def test_onboarding_never_calls_itself_a_checker_or_bot(sent):
    v6.handle_batch("cust", [{"type": "text", "text": "hi"}])
    text = next(m[1] for m in sent if m[0] == "buttons")
    assert "checker" not in text.lower() and "bot" not in text.lower()


# -- carried over from v4/v5 unchanged: plain text as a submission, no session --

def test_greeting_never_triggers_extraction(sent, monkeypatch):
    calls = []
    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: calls.append(1) or _Packet())
    v6.handle_batch("cust", [{"type": "text", "text": "hi"}])
    assert calls == []                      # the pre-filter rejected it for free
    kind, body, _ = sent[0]
    assert kind == "buttons" and body == v6._ONBOARDING_CHOICE_TEXT


def test_short_non_chitchat_text_also_skips_extraction(sent, monkeypatch):
    # Below the length floor but not literally a greeting -- the length
    # filter, not just the chit-chat wordlist, has to reject this too.
    calls = []
    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: calls.append(1) or _Packet())
    v6.handle_batch("cust", [{"type": "text", "text": "book pls"}])
    assert calls == []
    kind, body, _ = sent[0]
    assert kind == "buttons" and body == v6._ONBOARDING_CHOICE_TEXT


def test_free_text_query_with_everything_present_reaches_the_deal(sent, monkeypatch):
    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: _Packet(missing=[]))
    monkeypatch.setattr("yta.web._resolve", lambda packet: {"room_map": {"matched": False}})
    v6.handle_batch("cust", [{"type": "text",
                    "text": "Taj Palace Delhi, Oct 15-17, 2 adults, saw INR 15000 on MMT"}])
    # same shape a fully-informative photo/link submission produces: a
    # recap first, then the buttons message from _present_deal.
    recap_msgs = [m[1] for m in sent if m[0] == "text" and "Test Hotel" in m[1]]
    assert recap_msgs, "expected a recap message showing what was read"
    assert sent[-1][0] == "buttons"


def test_free_text_query_with_missing_fields_opens_awaiting_field(sent, monkeypatch):
    monkeypatch.setattr("yta.pipeline.extract",
                         lambda *a, **kw: _Packet(missing=["ota_benchmark.final_payable"]))
    v6.handle_batch("cust", [{"type": "text",
                    "text": "Taj Palace Delhi, Oct 15-17, 2 adults, deluxe room"}])
    assert "cust" in v6._WA_SESSIONS
    assert v6._WA_SESSIONS["cust"]["state"] == "awaiting_field"
    body = next(m[1] for m in sent if m[0] == "buttons")
    assert "Test Hotel" in body


def test_free_text_with_no_recognizable_hotel_gets_a_soft_nudge_not_a_loop(sent, monkeypatch):
    # Long enough to pass the pre-filter, but the extractor found nothing
    # usable -- should NOT drag the customer into a full slot-filling
    # interrogation seeded from a stray sentence.
    monkeypatch.setattr("yta.pipeline.extract",
                         lambda *a, **kw: _Packet(missing=["hotel.name"], hotel_name=None))
    v6.handle_batch("cust", [{"type": "text",
                    "text": "just wondering what kind of deals you folks usually find"}])
    assert "cust" not in v6._WA_SESSIONS               # no half-open interrogation
    assert not any(m[0] == "buttons" for m in sent)     # no missing-field ask fired
    assert any("couldn't find hotel details" in m[1].lower() for m in sent if m[0] == "text")


def test_free_text_mid_session_is_still_handled_as_an_answer_not_a_new_query(sent, monkeypatch):
    # The text-as-submission path only applies when session is None -- a
    # long message while awaiting_field is open must still be tried
    # against the OPEN QUESTION first, never treated as a fresh submission.
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.extract_llm.extract_clarification",
                         lambda missing, text, media=None: ({"ota_benchmark.final_payable": 31683}, None))
    monkeypatch.setattr("yta.web._resolve", lambda packet: {"room_map": {"matched": False}})
    _open_awaiting()
    v6.handle_batch("cust", [{"type": "text",
                    "text": "actually it's the Grand Hyatt Mumbai, Oct 20-22, 2 adults, saw INR 20000"}])
    assert sent[-1][0] == "buttons"   # resolved via _handle_awaiting_field -> _present_deal
    assert "cust" not in v6._WA_SESSIONS


# -- v6's own addition: Guard B, the staleness "welcome back" check ------

def test_stale_reply_that_looks_like_a_fresh_query_gets_a_welcome_back_check(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    _open_awaiting(age_sec=v6._STALE_REPLY_SEC + 60)
    v6.handle_batch("cust", [{"type": "text",
                    "text": "actually it's the Grand Hyatt Mumbai, Oct 20-22, 2 adults"}])
    assert "cust" in v6._WA_SESSIONS   # NOT wiped, NOT silently treated as an answer either
    assert v6._WA_SESSIONS["cust"]["state"] == "awaiting_field"
    kind, body, buttons = next(m for m in sent if m[0] == "buttons")
    assert body.startswith("Welcome back")
    assert "Test Hotel" in body   # names the hotel the OLD session was about
    assert [bid for bid, _ in buttons] == ["welcome_continue", "welcome_new_search"]


def test_stale_but_answer_shaped_reply_is_still_honored_exactly_like_v5(sent, monkeypatch):
    # A bare number has no digit-based "fresh query" shape -- Guard B
    # must not fire just because time passed; content still gates this,
    # not the clock.
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.extract_llm.extract_clarification",
                         lambda missing, text, media=None: ({"ota_benchmark.final_payable": 31683}, None))
    monkeypatch.setattr("yta.web._resolve", lambda packet: {"room_map": {"matched": False}})
    _open_awaiting(age_sec=v6._STALE_REPLY_SEC + 60)
    v6.handle_batch("cust", [{"type": "text", "text": "31683"}])
    assert sent[-1][0] == "buttons"   # reached _present_deal, not a welcome-back check
    assert "cust" not in v6._WA_SESSIONS


def test_welcome_back_continue_restores_the_pending_question(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    _open_awaiting(age_sec=v6._STALE_REPLY_SEC + 60)
    v6.handle_batch("cust", [{"type": "text",
                    "text": "actually it's the Grand Hyatt Mumbai, Oct 20-22, 2 adults"}])
    sent.clear()
    v6.handle_batch("cust", [{"type": "button_reply", "button_id": "welcome_continue", "text": "Continue"}])
    assert v6._WA_SESSIONS["cust"]["state"] == "awaiting_field"
    assert "pending_new_text" not in v6._WA_SESSIONS["cust"]
    kind, body, _ = sent[-1]
    assert kind == "buttons" and body.strip().endswith("?")


def test_welcome_back_new_search_wipes_and_runs_extraction_on_the_stale_message(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.pipeline.extract",
                         lambda *a, **kw: _Packet(missing=[], hotel_name="Grand Hyatt Mumbai"))
    monkeypatch.setattr("yta.web._resolve", lambda packet: {"room_map": {"matched": False}})
    _open_awaiting(age_sec=v6._STALE_REPLY_SEC + 60)
    v6.handle_batch("cust", [{"type": "text",
                    "text": "actually it's the Grand Hyatt Mumbai, Oct 20-22, 2 adults"}])
    sent.clear()
    v6.handle_batch("cust", [{"type": "button_reply", "button_id": "welcome_new_search", "text": "New search"}])
    assert sent[-1][0] == "buttons"   # reached _present_deal via a fresh extraction
    assert "cust" not in v6._WA_SESSIONS


# -- structured recap + natural question ---------------------------------

def test_missing_field_ask_shows_a_structured_recap_and_a_real_question(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: "https://booking.com/x")
    monkeypatch.setattr("yta.pipeline.extract",
                         lambda *a, **kw: _Packet(missing=["ota_benchmark.final_payable"]))
    v6.handle_batch("cust", [{"type": "text", "text": "https://booking.com/x"}])
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
    v6.handle_batch("cust", [{"type": "text", "text": "https://booking.com/x"}])
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
    v6.handle_batch("cust", [{"type": "text", "text": "https://booking.com/x"}])
    recap_msgs = [m[1] for m in sent if m[0] == "text" and "Test Hotel" in m[1]]
    assert recap_msgs, "expected a recap message showing what was read"


def test_recap_includes_ota_price_when_already_known(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: "https://booking.com/x")
    monkeypatch.setattr("yta.pipeline.extract",
                         lambda *a, **kw: _Packet(missing=["requested_offer.room_name"]))
    v6.handle_batch("cust", [{"type": "text", "text": "https://booking.com/x"}])
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
    v6.handle_batch("cust", [{"type": "text", "text": "31683"}])
    assert sent[-1][0] == "buttons"   # reached _present_deal, not re-treated as a fresh submission


def test_abandoned_session_past_the_hygiene_backstop_is_cleared(sent, monkeypatch):
    # The 24h backstop is memory hygiene for a number that never comes
    # back, not a UX judgment -- still worth confirming it actually clears.
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    _open_awaiting(age_sec=v6._SESSION_MAX_AGE_SEC + 60)
    v6.handle_batch("cust", [{"type": "text", "text": "31683"}])
    assert "cust" not in v6._WA_SESSIONS
    # a bare number with no hotel context left should get the onboarding
    # choice again, not be silently absorbed as an answer to the
    # (now-cleared) question
    kind, body, _ = sent[0]
    assert kind == "buttons" and body == v6._ONBOARDING_CHOICE_TEXT


# -- bounded slot-filling ----------------------------------------------

def test_gives_up_only_after_three_unproductive_attempts(sent, monkeypatch):
    # v6 item 17: the OLD 2-strike wipe is gone -- strike 2 offers
    # alternatives and keeps the session; only strike 3 actually gives up.
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.extract_llm.extract_clarification",
                         lambda missing, text, media=None: ({}, None))

    _open_awaiting(attempts=0)
    v6.handle_batch("cust", [{"type": "text", "text": "hi there"}])
    assert "cust" in v6._WA_SESSIONS   # attempt 1 of 3 -- still open, re-asked with an example
    assert v6._WA_SESSIONS["cust"]["unproductive_attempts"] == 1
    body1 = next(m[1] for m in sent if m[0] == "buttons")
    assert "for example" in body1.lower()

    sent.clear()
    v6.handle_batch("cust", [{"type": "text", "text": "nope still no clue lol"}])
    assert "cust" in v6._WA_SESSIONS   # attempt 2 -- NOT wiped, offers alternatives
    assert v6._WA_SESSIONS["cust"]["unproductive_attempts"] == 2
    kind, body2, buttons2 = next(m for m in sent if m[0] == "buttons")
    assert [bid for bid, _ in buttons2] == ["await_type_details", "await_send_screenshot", "start_new_chat"]

    sent.clear()
    v6.handle_batch("cust", [{"type": "text", "text": "sorry no idea what you mean by that"}])
    assert "cust" not in v6._WA_SESSIONS   # attempt 3 -- cap reached, gave up for real
    assert any("start fresh" in m[1] for m in sent if m[0] == "text")
    kind, body, _ = sent[-1]
    assert kind == "buttons" and body == v6._ONBOARDING_CHOICE_TEXT


def test_awaiting_field_alternative_buttons_re_nudge_without_costing_a_strike(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.extract_llm.extract_clarification",
                         lambda missing, text, media=None: ({}, None))
    _open_awaiting(attempts=2)   # already at strike 2 -- alternatives were just offered
    v6.handle_batch("cust", [{"type": "button_reply", "button_id": "await_type_details", "text": "Type details"}])
    assert "cust" in v6._WA_SESSIONS
    assert v6._WA_SESSIONS["cust"]["unproductive_attempts"] == 2   # unchanged -- not a strike
    kind, body, _ = sent[-1]
    assert kind == "buttons" and "?" in body   # the same open question, re-sent


def test_unproductive_reply_with_no_digits_is_never_mistaken_for_a_fresh_query(sent, monkeypatch):
    # Regression: an early version of Guard A (item 16) used the LOOSER
    # _looks_like_a_query check -- "still unrelated" (16 chars, not
    # literal chitchat) passed it and got silently re-routed into a real
    # extraction attempt instead of counting as a strike, letting an
    # actually-stuck conversation dodge the give-up cap forever. A real
    # hotel query always carries a digit somewhere (a date/price/guest
    # count); ordinary unproductive prose almost never does.
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.extract_llm.extract_clarification",
                         lambda missing, text, media=None: ({}, None))
    extract_calls = []
    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: extract_calls.append(1) or _Packet())
    _open_awaiting(attempts=0)
    v6.handle_batch("cust", [{"type": "text", "text": "still unrelated to anything"}])
    assert extract_calls == []                          # never ran a fresh extraction
    assert "cust" in v6._WA_SESSIONS
    assert v6._WA_SESSIONS["cust"]["unproductive_attempts"] == 1   # counted as a real strike


def test_a_reply_with_a_digit_that_looks_like_a_new_hotel_query_is_not_a_strike(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.extract_llm.extract_clarification",
                         lambda missing, text, media=None: ({}, None))
    monkeypatch.setattr("yta.pipeline.extract",
                         lambda *a, **kw: _Packet(missing=[], hotel_name="Grand Hyatt Mumbai"))
    monkeypatch.setattr("yta.web._resolve", lambda packet: {"room_map": {"matched": False}})
    _open_awaiting(attempts=1)
    v6.handle_batch("cust", [{"type": "text",
                    "text": "actually it's the Grand Hyatt Mumbai, Oct 20-22, 2 adults"}])
    # routed to a fresh extraction (reached _present_deal), not counted
    # as attempt 2 of the OLD session
    assert sent[-1][0] == "buttons"
    assert "Grand Hyatt Mumbai" in sent[0][1] or any("Grand Hyatt Mumbai" in (m[1] if m[0] == "text" else "")
                                                       for m in sent)


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
    v6.handle_batch("cust", [{"type": "text", "text": "a"}])
    v6.handle_batch("cust", [{"type": "text", "text": "b"}])
    assert v6._WA_SESSIONS["cust"]["unproductive_attempts"] == 0
    v6.handle_batch("cust", [{"type": "text", "text": "c"}])
    assert "cust" in v6._WA_SESSIONS   # still open -- this was only attempt 1 since the reset
    assert v6._WA_SESSIONS["cust"]["unproductive_attempts"] == 1


# -- presented: confirm / decline ---------------------------------------

def test_confirm_button_records_lead_and_replies_with_reference(sent, monkeypatch):
    recorded = []
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.leads.db.record_lead",
                         lambda phone, status, packet, resolution, referred_by=None: recorded.append((phone, status)) or "BMS-TEST1234")
    _open_presented()
    v6.handle_batch("cust", [{"type": "button_reply", "button_id": "confirm_book", "text": "Yes, book this"}])
    assert recorded == [("cust", "confirmed")]
    assert "cust" not in v6._WA_SESSIONS
    assert any("BMS-TEST1234" in m[1] for m in sent if m[0] == "text")


def test_confirm_message_names_what_was_actually_booked(sent, monkeypatch):
    # The confirm note used to be a bare "I've noted this down" -- it
    # should say what was actually secured and for how much, not just
    # hand back a reference number. v6: the reference is now its own
    # separate, easy-to-copy message right after (item 13).
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.leads.db.record_lead",
                         lambda phone, status, packet, resolution, referred_by=None: "BMS-TEST5555")
    _open_presented(confirm_line="I've secured Test Hotel for INR 28,015.64 (INR 3,667 less than what you had).")
    v6.handle_batch("cust", [{"type": "button_reply", "button_id": "confirm_book", "text": "Yes, book this"}])
    texts = [m[1] for m in sent if m[0] == "text"]
    confirm_text = next(t for t in texts if "I've secured" in t)
    assert "I've secured Test Hotel for INR 28,015.64" in confirm_text
    assert "3,667 less than what you had" in confirm_text
    assert "within 30 minutes" in confirm_text   # a concrete SLA, not "shortly"
    assert any(t == "Your reference: BMS-TEST5555" for t in texts)


def test_confirm_message_falls_back_gracefully_with_no_confirm_line(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.leads.db.record_lead",
                         lambda phone, status, packet, resolution, referred_by=None: "BMS-TEST6666")
    _open_presented(confirm_line=None)
    v6.handle_batch("cust", [{"type": "button_reply", "button_id": "confirm_book", "text": "Yes, book this"}])
    texts = [m[1] for m in sent if m[0] == "text"]
    confirm_text = next(t for t in texts if "I've noted this down" in t)
    assert "I've noted this down" in confirm_text
    assert any(t == "Your reference: BMS-TEST6666" for t in texts)


def test_confirm_message_never_implies_the_booking_is_already_done(sent, monkeypatch):
    # P0 fix: the old wording ("I'll personally follow up shortly to
    # finalize everything") reads as vague, and testing showed customers
    # assumed they'd already booked. The new copy must never claim the
    # booking itself is complete.
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.leads.db.record_lead",
                         lambda phone, status, packet, resolution, referred_by=None: "BMS-TEST4321")
    _open_presented(confirm_line=None)
    v6.handle_batch("cust", [{"type": "button_reply", "button_id": "confirm_book", "text": "Yes, book this"}])
    confirm_text = next(m[1] for m in sent if m[0] == "text" and "noted this down" in m[1])
    for phrase in ("booked", "you're all set", "all done", "confirmed and complete"):
        assert phrase not in confirm_text.lower()


def test_typed_yes_works_same_as_the_button(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.leads.db.record_lead",
                         lambda phone, status, packet, resolution, referred_by=None: "BMS-TEST5678")
    _open_presented()
    v6.handle_batch("cust", [{"type": "text", "text": "yes please"}])
    assert "cust" not in v6._WA_SESSIONS
    assert any("BMS-TEST5678" in m[1] for m in sent if m[0] == "text")


def test_decline_button_records_lead_and_does_not_block_next_hotel(sent, monkeypatch):
    recorded = []
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.leads.db.record_lead",
                         lambda phone, status, packet, resolution, referred_by=None: recorded.append(status) or "BMS-DECL0000")
    _open_presented()
    v6.handle_batch("cust", [{"type": "button_reply", "button_id": "decline_book", "text": "Not now"}])
    assert recorded == ["declined"]
    assert "cust" not in v6._WA_SESSIONS


def test_unrecognized_reply_in_presented_state_reprompts_and_stays_open(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    _open_presented()
    v6.handle_batch("cust", [{"type": "text", "text": "what's the cancellation policy?"}])
    assert "cust" in v6._WA_SESSIONS
    assert any("tap" in m[1].lower() for m in sent if m[0] == "text")


# -- the deal reveal: matched-and-cheaper / matched-not-cheaper / no rate --

def test_price_shown_as_short_wrap_safe_was_now_lines(sent, monkeypatch):
    monkeypatch.setattr(
        "yta.web._resolve",
        lambda packet: {"room_map": {"matched": True, "ratekey_option_ids": ["o1"], "rate_options": [
            {"option_id": "o1", "room_name": "Deluxe Room", "currency": "INR", "total_price": 28015.64},
        ]}},
    )
    v6._present_deal("cust", _Packet())
    body = next(m[1] for m in sent if m[0] == "buttons")
    assert "```" not in body
    assert "  " not in body                       # no multi-space padding anywhere
    assert all(len(line) < 45 for line in body.split("\n"))   # every line short enough not to wrap
    assert "~INR 31,683~" in body and "*INR 28,015.64*" in body


def test_shouty_and_stray_punctuation_room_names_get_cleaned_up(sent, monkeypatch):
    monkeypatch.setattr(
        "yta.web._resolve",
        lambda packet: {"room_map": {"matched": True, "ratekey_option_ids": ["o1"], "rate_options": [
            {"option_id": "o1", "room_name": "DELUXE ROOM", "currency": "INR", "total_price": 28015.64},
        ]}},
    )
    v6._present_deal("cust", _Packet())
    body = next(m[1] for m in sent if m[0] == "buttons")
    assert "DELUXE ROOM" not in body
    assert "Deluxe Room" in body


def test_category_icons_are_consistent_between_recap_and_deal_card(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: "https://booking.com/x")
    monkeypatch.setattr("yta.pipeline.extract",
                         lambda *a, **kw: _Packet(missing=["ota_benchmark.final_payable"]))
    v6.handle_batch("cust", [{"type": "text", "text": "https://booking.com/x"}])
    ask_body = next(m[1] for m in sent if m[0] == "buttons")
    assert "🏨" in ask_body and "📅" in ask_body and "🛏️" in ask_body

    sent.clear()
    monkeypatch.setattr(
        "yta.web._resolve",
        lambda packet: {"room_map": {"matched": True, "ratekey_option_ids": ["o1"], "rate_options": [
            {"option_id": "o1", "room_name": "Deluxe Room", "currency": "INR", "total_price": 28015.64},
        ]}},
    )
    v6._present_deal("cust", _Packet())
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
    v6._present_deal("cust", _Packet())
    body = next(m[1] for m in sent if m[0] == "buttons")
    assert "31,683" in body and "28,015.64" in body and "3,667" in body
    assert "Test Hotel" in body
    assert "Sep 21" in body and "Sep 22" in body
    assert "2 adult" in body
    assert "Deluxe Room" in body and "Breakfast" in body and "Refundable" in body
    ids = [bid for bid, _ in next(m[2] for m in sent if m[0] == "buttons")]
    assert ids == ["confirm_book", "decline_book"]
    assert "cust" in v6._WA_SESSIONS and v6._WA_SESSIONS["cust"]["state"] == "presented"


def test_deal_card_uses_tripjacks_own_hotel_name_not_the_customers_typed_one(sent, monkeypatch):
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
    v6._present_deal("cust", _Packet())
    body = next(m[1] for m in sent if m[0] == "buttons")
    assert "Taj Santacruz" in body
    assert "Test Hotel" not in body


def test_deal_card_falls_back_to_hoteldb_match_name_when_no_live_detail(sent, monkeypatch):
    monkeypatch.setattr(
        "yta.web._resolve",
        lambda packet: {
            "match": {"hotel_name": "Taj Santacruz (Catalog)"},
            "room_map": {"matched": True, "ratekey_option_ids": ["o1"], "rate_options": [
                {"option_id": "o1", "room_name": "Deluxe Room", "currency": "INR", "total_price": 28015.64},
            ]},
        },
    )
    v6._present_deal("cust", _Packet())
    body = next(m[1] for m in sent if m[0] == "buttons")
    assert "Taj Santacruz (Catalog)" in body
    assert "Test Hotel" not in body


def test_deal_card_room_name_never_falls_back_to_the_customers_requested_room(sent, monkeypatch):
    monkeypatch.setattr(
        "yta.web._resolve",
        lambda packet: {"room_map": {"matched": True, "ratekey_option_ids": ["o1"], "rate_options": [
            {"option_id": "o1", "room_name": "", "meal_basis": "", "refundable": None,
             "currency": "INR", "total_price": 28015.64},
        ]}},
    )
    v6._present_deal("cust", _Packet())
    body = next(m[1] for m in sent if m[0] == "buttons")
    assert "Deluxe Room" not in body
    assert "🛏️" not in body


def test_matched_but_not_cheaper_opens_a_real_state_with_buttons(sent, monkeypatch):
    # v6 item 14: this used to be a dead end (a plain message, no
    # buttons, no state). It's now `not_cheaper`, with somewhere to go.
    monkeypatch.setattr("yta.leads.db.record_lead", lambda *a, **kw: "BMS-NODEAL1")
    monkeypatch.setattr(
        "yta.web._resolve",
        lambda packet: {"room_map": {"matched": True, "ratekey_option_ids": ["o1"], "rate_options": [
            {"option_id": "o1", "room_type_id": "R1", "room_name": "Deluxe Room",
             "currency": "INR", "total_price": 40000.0},
        ]}},
    )
    v6._present_deal("cust", _Packet())
    kind, body, buttons = next(m for m in sent if m[0] == "buttons")
    assert "couldn't beat" in body.lower()
    assert "40,000" not in body and "40000" not in body   # our own price is never revealed here
    assert [bid for bid, _ in buttons] == ["see_other_rooms", "try_another"]
    assert v6._WA_SESSIONS["cust"]["state"] == "not_cheaper"


def test_not_cheaper_logs_a_no_deal_lead_for_internal_analysis_only(sent, monkeypatch):
    recorded = []
    monkeypatch.setattr(
        "yta.leads.db.record_lead",
        lambda phone, status, packet, resolution, referred_by=None: recorded.append(status) or "BMS-NODEAL2",
    )
    monkeypatch.setattr(
        "yta.web._resolve",
        lambda packet: {"room_map": {"matched": True, "ratekey_option_ids": ["o1"], "rate_options": [
            {"option_id": "o1", "room_type_id": "R1", "room_name": "Deluxe Room",
             "currency": "INR", "total_price": 40000.0},
        ]}},
    )
    v6._present_deal("cust", _Packet())
    assert recorded == ["no_deal"]
    assert not any("no_deal" in (m[1] if m[0] == "text" else str(m)) for m in sent)  # never shown to the customer


def test_not_cheaper_see_other_rooms_uses_cached_rates_excludes_matched_room(sent, monkeypatch):
    monkeypatch.setattr("yta.leads.db.record_lead", lambda *a, **kw: "BMS-NODEAL3")
    # detail.options is SupplierOption.to_dict()'s shape -- room identity
    # lives in a nested "rooms" list ({id, name}), not flat room_type_id/
    # room_name keys (that's list_cheapest_rooms()'s own OUTPUT shape,
    # not its input -- see the real _rows() bug this caught, fixed in
    # yta/roommap/match.py).
    resolution = {
        "detail": {"hotel_name": "Taj Santacruz", "options": [
            {"option_id": "o1", "rooms": [{"id": "R1", "name": "Deluxe Room"}],
             "meal_basis": "Room Only", "refundable": False, "currency": "INR", "total_price": 40000.0},
            {"option_id": "o2", "rooms": [{"id": "R2", "name": "Premier Room"}],
             "meal_basis": "Breakfast", "refundable": True, "currency": "INR", "total_price": 38000.0},
        ]},
        "room_map": {"matched": True, "ratekey_option_ids": ["o1"], "rate_options": [
            {"option_id": "o1", "room_type_id": "R1", "room_name": "Deluxe Room",
             "currency": "INR", "total_price": 40000.0},
        ]},
    }
    monkeypatch.setattr("yta.web._resolve", lambda packet: resolution)
    v6._present_deal("cust", _Packet())
    assert v6._WA_SESSIONS["cust"]["state"] == "not_cheaper"

    v6.handle_batch("cust", [{"type": "button_reply", "button_id": "see_other_rooms", "text": "See other rooms"}])
    text = next(m[1] for m in sent if m[0] == "text" and "Taj Santacruz" in m[1])
    assert "Premier Room" in text
    assert "Deluxe Room" not in text   # the room just shown as not-cheaper is excluded
    assert v6._WA_SESSIONS["cust"]["state"] == "choosing_option"


def test_not_cheaper_see_other_rooms_with_nothing_left_offers_try_another(sent, monkeypatch):
    monkeypatch.setattr("yta.leads.db.record_lead", lambda *a, **kw: "BMS-NODEAL4")
    resolution = {
        "detail": {"hotel_name": "Taj Santacruz", "options": [
            {"option_id": "o1", "rooms": [{"id": "R1", "name": "Deluxe Room"}],
             "currency": "INR", "total_price": 40000.0},
        ]},
        "room_map": {"matched": True, "ratekey_option_ids": ["o1"], "rate_options": [
            {"option_id": "o1", "room_type_id": "R1", "room_name": "Deluxe Room",
             "currency": "INR", "total_price": 40000.0},
        ]},
    }
    monkeypatch.setattr("yta.web._resolve", lambda packet: resolution)
    v6._present_deal("cust", _Packet())
    sent.clear()
    v6.handle_batch("cust", [{"type": "button_reply", "button_id": "see_other_rooms", "text": "See other rooms"}])
    kind, body, buttons = next(m for m in sent if m[0] == "buttons")
    assert [bid for bid, _ in buttons] == ["try_another"]
    assert "cust" not in v6._WA_SESSIONS


def test_not_cheaper_try_another_wipes_session_and_reoffers_choice(sent, monkeypatch):
    monkeypatch.setattr("yta.leads.db.record_lead", lambda *a, **kw: "BMS-NODEAL5")
    monkeypatch.setattr(
        "yta.web._resolve",
        lambda packet: {"room_map": {"matched": True, "ratekey_option_ids": ["o1"], "rate_options": [
            {"option_id": "o1", "room_type_id": "R1", "room_name": "Deluxe Room",
             "currency": "INR", "total_price": 40000.0},
        ]}},
    )
    v6._present_deal("cust", _Packet())
    sent.clear()
    v6.handle_batch("cust", [{"type": "button_reply", "button_id": "try_another", "text": "Try another hotel"}])
    assert "cust" not in v6._WA_SESSIONS
    kind, body, _ = sent[0]
    assert kind == "buttons" and body == v6._ONBOARDING_CHOICE_TEXT


def test_not_cheaper_unrecognized_reply_reprompts_then_gives_up(sent, monkeypatch):
    monkeypatch.setattr("yta.leads.db.record_lead", lambda *a, **kw: "BMS-NODEAL6")
    monkeypatch.setattr(
        "yta.web._resolve",
        lambda packet: {"room_map": {"matched": True, "ratekey_option_ids": ["o1"], "rate_options": [
            {"option_id": "o1", "room_type_id": "R1", "room_name": "Deluxe Room",
             "currency": "INR", "total_price": 40000.0},
        ]}},
    )
    v6._present_deal("cust", _Packet())
    sent.clear()
    v6.handle_batch("cust", [{"type": "text", "text": "what does that mean"}])
    assert v6._WA_SESSIONS["cust"]["state"] == "not_cheaper"   # attempt 1 -- still open

    v6.handle_batch("cust", [{"type": "text", "text": "still confused"}])
    assert "cust" not in v6._WA_SESSIONS   # attempt 2 -- gave up
    kind, body, _ = sent[-1]   # the LAST message -- attempt 1's reprompt sent buttons too
    assert body == v6._ONBOARDING_CHOICE_TEXT


def test_cover_image_sent_once_hotel_is_confidently_identified(sent, monkeypatch):
    monkeypatch.setattr(
        "yta.web._resolve",
        lambda packet: {
            "match": {"hotel_name": "Taj Santacruz", "cover_image": "https://example.com/taj.jpg"},
            "room_map": {"matched": True, "ratekey_option_ids": ["o1"], "rate_options": [
                {"option_id": "o1", "room_name": "Deluxe Room", "currency": "INR", "total_price": 28015.64},
            ]},
        },
    )
    v6._present_deal("cust", _Packet())
    image_msgs = [m for m in sent if m[0] == "image"]
    assert image_msgs == [("image", "https://example.com/taj.jpg", None)]
    assert sent.index(image_msgs[0]) < len(sent) - 1   # sent before the rate message
    assert sent[-1][0] == "buttons"


def test_no_cover_image_sent_when_hotel_is_not_confidently_matched(sent, monkeypatch):
    monkeypatch.setattr("yta.web._resolve", lambda packet: {"room_map": {"matched": False}})
    v6._present_deal("cust", _Packet())
    assert not any(m[0] == "image" for m in sent)


def test_no_live_rate_offers_try_another_button(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: "https://booking.com/x")
    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: _Packet())
    monkeypatch.setattr("yta.web._resolve", lambda packet: {"available": False, "room_map": {"matched": False}})

    v6.handle_batch("cust", [{"type": "text", "text": "https://booking.com/x"}])
    assert "cust" not in v6._WA_SESSIONS
    assert any(m[0] == "buttons" and any(bid == "try_another" for bid, _ in m[2]) for m in sent)


def test_try_another_button_sends_onboarding_choice(sent):
    v6.handle_batch("cust", [{"type": "button_reply", "button_id": "try_another", "text": "Try another hotel"}])
    kind, body, _ = sent[0]
    assert kind == "buttons" and body == v6._ONBOARDING_CHOICE_TEXT


# -- global cancel / start-over -------------------------------------------

def test_start_new_chat_button_clears_any_open_session(sent):
    _open_awaiting()
    v6.handle_batch("cust", [{"type": "button_reply", "button_id": "start_new_chat", "text": "Start over"}])
    assert "cust" not in v6._WA_SESSIONS
    assert any("start fresh" in m[1].lower() for m in sent if m[0] == "text")


def test_start_over_re_offers_the_onboarding_choice(sent):
    _open_awaiting()
    v6.handle_batch("cust", [{"type": "button_reply", "button_id": "start_new_chat", "text": "Start over"}])
    kind, body, buttons = next(m for m in sent if m[0] == "buttons")
    assert body == v6._ONBOARDING_CHOICE_TEXT
    assert [bid for bid, _ in buttons] == ["have_deal", "search_hotel"]


def test_cancel_with_no_open_session_still_offers_the_choice(sent):
    v6.handle_batch("cust", [{"type": "text", "text": "never mind"}])
    kind, body, buttons = next(m for m in sent if m[0] == "buttons")
    assert body == v6._ONBOARDING_CHOICE_TEXT


def test_start_over_clears_any_pending_path(sent):
    v6._PENDING_PATH["cust"] = "search"
    _open_awaiting()
    v6.handle_batch("cust", [{"type": "button_reply", "button_id": "start_new_chat", "text": "Start over"}])
    assert "cust" not in v6._PENDING_PATH


@pytest.mark.parametrize("phrase", ["start again", "Start this again", "restart", "RESTART please"])
def test_natural_restart_phrases_are_recognized_as_cancel(sent, phrase):
    _open_awaiting()
    v6.handle_batch("cust", [{"type": "text", "text": phrase}])
    assert "cust" not in v6._WA_SESSIONS
    kind, body, _ = sent[-1]
    assert kind == "buttons" and body == v6._ONBOARDING_CHOICE_TEXT


def test_missing_field_ask_carries_a_start_new_chat_button(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: "https://booking.com/x")
    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: _Packet(missing=["ota_benchmark.final_payable"]))
    v6.handle_batch("cust", [{"type": "text", "text": "https://booking.com/x"}])
    assert any(m[0] == "buttons" and any(bid == "start_new_chat" for bid, _ in m[2]) for m in sent)


# -- the referral loop: carried over from v3/v5 unchanged -----------------

def test_confirming_asks_for_a_referral_and_a_deep_link(sent, monkeypatch):
    monkeypatch.setenv("WHATSAPP_DISPLAY_NUMBER", "919999999999")
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.leads.db.record_lead",
                         lambda phone, status, packet, resolution, referred_by=None: "BMS-TEST1234")
    _open_presented(confirm_line="I've secured Test Hotel for INR 28,015.64 (INR 3,667 less than what you had).")
    v6.handle_batch("cust", [{"type": "button_reply", "button_id": "confirm_book", "text": "Yes, book this"}])
    texts = [m[1] for m in sent if m[0] == "text"]
    assert any("BMS-TEST1234" in t for t in texts)
    assert any("wa.me/" in t for t in texts)


def test_savings_figure_is_not_repeated_in_the_referral_ask(sent, monkeypatch):
    monkeypatch.setenv("WHATSAPP_DISPLAY_NUMBER", "919999999999")
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.leads.db.record_lead",
                         lambda phone, status, packet, resolution, referred_by=None: "BMS-TEST7777")
    _open_presented(confirm_line="I've secured Test Hotel for INR 28,015.64 (INR 3,667 less than what you had).")
    v6.handle_batch("cust", [{"type": "button_reply", "button_id": "confirm_book", "text": "Yes, book this"}])
    texts = [m[1] for m in sent if m[0] == "text"]
    confirm_text = next(t for t in texts if "I've secured" in t)
    ask_text = next(t for t in texts if "trip coming up" in t.lower())
    assert "3,667" in confirm_text
    assert "3,667" not in ask_text
    assert any("wa.me/" in t for t in texts)


def test_the_forwardable_message_carries_nothing_but_the_shareable_text(sent, monkeypatch):
    monkeypatch.setenv("WHATSAPP_DISPLAY_NUMBER", "919999999999")
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.leads.db.record_lead",
                         lambda phone, status, packet, resolution, referred_by=None: "BMS-TEST9999")
    _open_presented(savings_line="You just saved INR 3,667 (12%) on this one.")
    v6.handle_batch("cust", [{"type": "button_reply", "button_id": "confirm_book", "text": "Yes, book this"}])
    texts = [m[1] for m in sent if m[0] == "text"]
    shareable = next(t for t in texts if "wa.me/" in t)
    assert "Forward" not in shareable and "Tap and hold" not in shareable
    assert shareable.startswith("I just found a better hotel rate")


def test_decline_does_not_ask_for_a_referral(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.leads.db.record_lead",
                         lambda phone, status, packet, resolution, referred_by=None: "BMS-TEST0002")
    _open_presented(savings_line="You just saved INR 3,667 (12%) on this one.")
    v6.handle_batch("cust", [{"type": "button_reply", "button_id": "decline_book", "text": "Not now"}])
    texts = [m[1] for m in sent if m[0] == "text"]
    assert not any("trip coming up" in t.lower() for t in texts)


def test_a_referral_mention_is_captured_and_attached_at_confirm(sent, monkeypatch):
    recorded = []
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr(
        "yta.leads.db.record_lead",
        lambda phone, status, packet, resolution, referred_by=None: recorded.append(referred_by) or "BMS-NEW00001",
    )
    v6.handle_batch("cust", [{"type": "text", "text": "Hi, I was referred by BMS-7F3A9C2E"}])
    assert v6._PENDING_REFERRALS.get("cust") == "BMS-7F3A9C2E"

    _open_presented()
    v6.handle_batch("cust", [{"type": "button_reply", "button_id": "confirm_book", "text": "Yes, book this"}])
    assert recorded == ["BMS-7F3A9C2E"]
    assert "cust" not in v6._PENDING_REFERRALS


def test_referral_mention_alongside_a_link_is_captured_immediately(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: "https://booking.com/x")
    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: _Packet(missing=["ota_benchmark.final_payable"]))
    v6.handle_batch("cust", [{"type": "text",
                               "text": "referred by BMS-AAAA1111 -- https://booking.com/x"}])
    assert v6._PENDING_REFERRALS.get("cust") == "BMS-AAAA1111"


def test_no_referral_mention_means_no_attribution(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    recorded = []
    monkeypatch.setattr("yta.leads.db.record_lead",
                         lambda phone, status, packet, resolution, referred_by=None: recorded.append(referred_by) or "BMS-X")
    _open_presented()
    v6.handle_batch("cust", [{"type": "button_reply", "button_id": "confirm_book", "text": "Yes, book this"}])
    assert recorded == [None]


# -- carried over from v5 unchanged: the deal/search choice and its effect -

def test_effective_missing_default_is_unchanged_from_schema():
    assert v6._effective_missing(_Packet(), [], None) == []
    assert v6._effective_missing(_Packet(), ["ota_benchmark.final_payable"], None) \
        == ["ota_benchmark.final_payable"]


def test_effective_missing_deal_intent_adds_room_name_back_when_a_hint_exists():
    p = _Packet(room_name=None, description="a lovely room with a view")
    assert v6._effective_missing(p, [], "deal") == ["requested_offer.room_name"]
    assert v6._effective_missing(p, ["requested_offer.room_name"], "deal") \
        == ["requested_offer.room_name"]
    assert v6._effective_missing(_Packet(room_name="Deluxe Room"), [], "deal") == []


def test_effective_missing_deal_intent_does_not_force_room_with_no_hint_at_all():
    p = _Packet(room_name=None, description=None)
    assert v6._effective_missing(p, [], "deal") == []


def test_effective_missing_search_intent_drops_price():
    assert v6._effective_missing(_Packet(), ["ota_benchmark.final_payable"], "search") == []
    assert v6._effective_missing(
        _Packet(), ["ota_benchmark.final_payable", "stay.check_in"], "search") == ["stay.check_in"]


def test_have_deal_path_still_asks_for_a_room_when_a_hint_exists(sent, monkeypatch):
    v6._PENDING_PATH["cust"] = "deal"
    monkeypatch.setattr("yta.pipeline.extract",
                         lambda *a, **kw: _Packet(missing=[], room_name=None,
                                                  description="a lovely room with a view"))
    v6.handle_batch("cust", [{"type": "text", "text": "https://booking.com/x", }])
    body = next(m[1] for m in sent if m[0] == "buttons")
    assert "room" in body.lower()
    assert v6._WA_SESSIONS["cust"]["state"] == "awaiting_field"
    assert v6._WA_SESSIONS["cust"]["missing"] == ["requested_offer.room_name"]


def test_have_deal_path_falls_through_to_options_with_no_room_hint_at_all(sent, monkeypatch):
    v6._PENDING_PATH["cust"] = "deal"
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
    v6.handle_batch("cust", [{"type": "text", "text": "https://booking.com/x", }])
    assert v6._WA_SESSIONS["cust"]["state"] == "choosing_option"
    assert not any(m[0] == "buttons" and "start_new_chat" in [bid for bid, _ in m[2]]
                   for m in sent)


def test_search_hotel_path_proceeds_without_a_price(sent, monkeypatch):
    v6._PENDING_PATH["cust"] = "search"
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
    v6.handle_batch("cust", [{"type": "text", "text": "Taj Santacruz, Sep 21-22, 2 adults"}])
    assert v6._WA_SESSIONS["cust"]["state"] == "choosing_option"
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
    v6._present_option_choices("cust", _Packet(), resolution)
    text = next(m[1] for m in sent if m[0] == "text")
    assert "Taj Santacruz" in text
    assert text.count("Deluxe Villa") == 1
    assert text.count("Premier Villa") == 1
    assert "1. Room Only" in text and "2. Breakfast" in text and "3. Room Only" in text
    assert "Reply with a number (1–3)" in text
    assert "20,000.00" in text and "21,000.00" in text and "25,000.00" in text
    assert [o["option_id"] for o in v6._WA_SESSIONS["cust"]["options"]] == ["r1", "r2", "p1"]


def test_present_option_choices_shows_nearest_match_and_cheapest_callouts_when_ambiguous(sent):
    groups = [
        {"room_type_id": "R2", "room_name": "LUXURY, COURTYARD VIEW", "total_combos": 1, "options": [
            _rate("o2", "LUXURY, COURTYARD VIEW", "Room Only", False, 38450.13),
        ]},
        {"room_type_id": "R1", "room_name": "Luxury Room Facade View", "total_combos": 1, "options": [
            _rate("o1", "Luxury Room Facade View", "Room Only", False, 37718.04),
        ]},
    ]
    resolution = {"detail": {"hotel_name": "Taj Santacruz"},
                  "room_options": {"groups": groups, "ambiguous_match": True,
                                   "nearest_match_room_type_id": "R2"}}
    v6._present_option_choices("cust", _Packet(), resolution)
    text = next(m[1] for m in sent if m[0] == "text")
    assert "🎯 Nearest match: Luxury, Courtyard View" in text and "38,450.13" in text
    assert "💰 Cheapest available: Luxury Room Facade View" in text and "37,718.04" in text
    assert "Full list:" in text
    assert "Here's what's available:" not in text


def test_present_option_choices_no_duplicate_callout_when_nearest_is_cheapest(sent):
    groups = [
        {"room_type_id": "R1", "room_name": "Deluxe Room", "total_combos": 1, "options": [
            _rate("o1", "Deluxe Room", "Room Only", False, 20000.0),
        ]},
        {"room_type_id": "R2", "room_name": "Premier Room", "total_combos": 1, "options": [
            _rate("o2", "Premier Room", "Room Only", False, 25000.0),
        ]},
    ]
    resolution = {"detail": {"hotel_name": "Taj Santacruz"},
                  "room_options": {"groups": groups, "ambiguous_match": True,
                                   "nearest_match_room_type_id": "R1"}}
    v6._present_option_choices("cust", _Packet(), resolution)
    text = next(m[1] for m in sent if m[0] == "text")
    assert text.count("Nearest match") == 1
    assert "Cheapest available" not in text


def test_present_option_choices_no_callouts_when_not_ambiguous(sent):
    groups = [{"room_type_id": "R1", "room_name": "Deluxe Room", "total_combos": 1,
               "options": [_rate("o1", "Deluxe Room", "Room Only", False, 20000.0)]}]
    resolution = {"detail": {"hotel_name": "Taj Santacruz"}, "room_options": {"groups": groups}}
    v6._present_option_choices("cust", _Packet(), resolution)
    text = next(m[1] for m in sent if m[0] == "text")
    assert "Here's what's available:" in text
    assert "Nearest match" not in text and "Cheapest available" not in text


def test_present_option_choices_with_no_options_offers_try_another(sent):
    resolution = {"detail": {"hotel_name": "Taj Santacruz"}, "room_options": {"groups": []}}
    v6._present_option_choices("cust", _Packet(), resolution)
    kind, body, buttons = next(m for m in sent if m[0] == "buttons")
    assert "Taj Santacruz" in body
    assert [bid for bid, _ in buttons] == ["try_another"]
    assert "cust" not in v6._WA_SESSIONS


def test_picking_an_option_by_number_leads_to_the_same_confirm_flow(sent):
    options = [
        {"option_id": "s1", "room_name": "Deluxe Room", "meal_basis": "Breakfast",
         "refundable": True, "currency": "INR", "total_price": 20000.0},
        {"option_id": "c2", "room_name": "Premier Room", "meal_basis": "Half Board",
         "refundable": False, "currency": "INR", "total_price": 25000.0},
    ]
    _open_choosing_option(options, resolution={"detail": {"hotel_name": "Taj Santacruz"}})
    v6.handle_batch("cust", [{"type": "text", "text": "2"}])
    body = next(m[1] for m in sent if m[0] == "buttons")
    assert "Premier Room" in body
    assert "Taj Santacruz" in body
    assert v6._WA_SESSIONS["cust"]["state"] == "presented"
    rm = v6._WA_SESSIONS["cust"]["resolution"]["room_map"]
    assert rm["matched"] is True and rm["ratekey_option_ids"] == ["c2"]


def test_picking_an_invalid_number_reprompts_offers_alternatives_then_gives_up(sent):
    # v6 item 17: strike 2 offers "Show list again" and keeps the
    # session; only strike 3 actually gives up.
    options = [{"option_id": "s1", "room_name": "Deluxe Room", "meal_basis": "Breakfast",
                "refundable": True, "currency": "INR", "total_price": 20000.0}]
    _open_choosing_option(options)
    v6.handle_batch("cust", [{"type": "text", "text": "banana"}])
    assert "cust" in v6._WA_SESSIONS   # attempt 1 of 3 -- still open
    assert any("reply with a number" in m[1].lower() for m in sent if m[0] == "text")

    sent.clear()
    v6.handle_batch("cust", [{"type": "text", "text": "still not a number"}])
    assert "cust" in v6._WA_SESSIONS   # attempt 2 -- NOT wiped, offers alternatives
    kind, body, buttons = next(m for m in sent if m[0] == "buttons")
    assert [bid for bid, _ in buttons] == ["show_list_again", "start_new_chat"]

    sent.clear()
    v6.handle_batch("cust", [{"type": "text", "text": "nope"}])
    assert "cust" not in v6._WA_SESSIONS   # attempt 3 -- gave up for real
    assert any("start fresh" in m[1].lower() for m in sent if m[0] == "text")
    kind, body, _ = next(m for m in sent if m[0] == "buttons")
    assert body == v6._ONBOARDING_CHOICE_TEXT


def test_show_list_again_resends_the_same_list_and_resets_attempts(sent):
    options = [{"option_id": "s1", "room_name": "Deluxe Room", "meal_basis": "Breakfast",
                "refundable": True, "currency": "INR", "total_price": 20000.0}]
    resolution = {"detail": {"hotel_name": "Taj Santacruz"},
                  "room_options": {"groups": [{"room_type_id": "R1", "room_name": "Deluxe Room",
                                                "total_combos": 1, "options": options}]}}
    _open_choosing_option(options, attempts=2, resolution=resolution)
    v6.handle_batch("cust", [{"type": "button_reply", "button_id": "show_list_again", "text": "Show list again"}])
    assert v6._WA_SESSIONS["cust"]["state"] == "choosing_option"
    assert v6._WA_SESSIONS["cust"]["unproductive_attempts"] == 0   # fresh presentation resets it
    text = next(m[1] for m in sent if m[0] == "text" and "Taj Santacruz" in m[1])
    assert "Reply with a number" in text


def test_confirming_a_picked_option_records_the_lead_normally(sent, monkeypatch):
    recorded = []
    monkeypatch.setattr("yta.leads.db.record_lead",
                         lambda phone, status, packet, resolution, referred_by=None:
                             recorded.append((status, resolution["room_map"]["rate_options"][0]["option_id"]))
                             or "BMS-PICKED1")
    options = [{"option_id": "c2", "room_name": "Premier Room", "meal_basis": "Half Board",
                "refundable": False, "currency": "INR", "total_price": 25000.0}]
    _open_choosing_option(options, resolution={"detail": {"hotel_name": "Taj Santacruz"}})
    v6.handle_batch("cust", [{"type": "text", "text": "1"}])
    assert v6._WA_SESSIONS["cust"]["state"] == "presented"

    v6.handle_batch("cust", [{"type": "button_reply", "button_id": "confirm_book", "text": "Yes, book this"}])
    assert recorded == [("confirmed", "c2")]
    assert "cust" not in v6._WA_SESSIONS


# -- v6's own addition: cancel-phrase matching no longer wipes a good -----
# -- session over an ambiguous or coincidental match (item 15) ------------

def test_short_cancel_phrase_resets_in_full(sent):
    _open_awaiting()
    v6.handle_batch("cust", [{"type": "text", "text": "cancel please"}])
    assert "cust" not in v6._WA_SESSIONS
    kind, body, _ = sent[-1]
    assert kind == "buttons" and body == v6._ONBOARDING_CHOICE_TEXT


def test_partial_cancel_phrase_asks_before_resetting(sent):
    # Regression target: a longer reply that only STARTS with a cancel
    # word used to wipe the session outright. Now it asks first.
    _open_choosing_option([{"option_id": "s1", "room_name": "Deluxe Room", "meal_basis": "Breakfast",
                             "refundable": True, "currency": "INR", "total_price": 20000.0},
                            {"option_id": "c2", "room_name": "Premier Room", "meal_basis": "Half Board",
                             "refundable": False, "currency": "INR", "total_price": 25000.0}])
    v6.handle_batch("cust", [{"type": "text", "text": "nevermind, the second one please"}])
    assert "cust" in v6._WA_SESSIONS   # NOT wiped
    assert v6._WA_SESSIONS["cust"]["state"] == "choosing_option"
    kind, body, buttons = next(m for m in sent if m[0] == "buttons")
    assert body == "Want to start over?"
    assert [bid for bid, _ in buttons] == ["confirm_cancel", "cancel_continue"]


def test_confirm_cancel_button_resets_for_real(sent):
    _open_awaiting()
    v6.handle_batch("cust", [{"type": "text", "text": "nevermind, the second one please"}])
    v6.handle_batch("cust", [{"type": "button_reply", "button_id": "confirm_cancel", "text": "Yes, start over"}])
    assert "cust" not in v6._WA_SESSIONS
    kind, body, _ = sent[-1]
    assert kind == "buttons" and body == v6._ONBOARDING_CHOICE_TEXT


def test_cancel_continue_button_leaves_the_session_untouched(sent):
    _open_awaiting()
    v6.handle_batch("cust", [{"type": "text", "text": "nevermind, the second one please"}])
    v6.handle_batch("cust", [{"type": "button_reply", "button_id": "cancel_continue", "text": "No, continue"}])
    assert "cust" in v6._WA_SESSIONS
    assert v6._WA_SESSIONS["cust"]["state"] == "awaiting_field"


def test_cancel_phrase_with_a_url_is_never_treated_as_cancel(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: "https://booking.com/x")
    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: _Packet(missing=["ota_benchmark.final_payable"]))
    _open_awaiting()
    v6.handle_batch("cust", [{"type": "text", "text": "actually never mind, try this one instead https://booking.com/x"}])
    # dropped the OLD session as an implicit "different hotel" (the
    # existing URL rule), NOT via the cancel path -- and started a fresh
    # extraction rather than just wiping and re-offering the choice.
    assert not any(m[0] == "buttons" and m[1] == v6._ONBOARDING_CHOICE_TEXT for m in sent)
    assert "cust" in v6._WA_SESSIONS
    assert v6._WA_SESSIONS["cust"]["state"] == "awaiting_field"


def test_cancel_phrase_with_a_referral_code_is_never_treated_as_cancel(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.extract_llm.extract_clarification",
                         lambda missing, text, media=None: ({"ota_benchmark.final_payable": 31683}, None))
    monkeypatch.setattr("yta.web._resolve", lambda packet: {"room_map": {"matched": False}})
    _open_awaiting()
    v6.handle_batch("cust", [{"type": "text",
                    "text": "cancel that, but by the way I was referred by BMS-7F3A9C2E"}])
    assert v6._PENDING_REFERRALS.get("cust") == "BMS-7F3A9C2E"
    assert sent[-1][0] == "buttons"   # went to _handle_awaiting_field -> _present_deal, not a reset


def test_bare_new_hotel_is_no_longer_a_cancel_trigger(sent, monkeypatch):
    # "new hotel" was dropped from the phrase list entirely -- too likely
    # to be a literal substring of an unrelated hotel's own name.
    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: _Packet(missing=[]))
    monkeypatch.setattr("yta.web._resolve", lambda packet: {"room_map": {"matched": False}})
    v6.handle_batch("cust", [{"type": "text",
                    "text": "The New Hotel Mumbai, Oct 12-14, 2 adults, saw INR 12000"}])
    assert sent[-1][0] == "buttons"   # reached _present_deal via free-text extraction, not cancel


@pytest.mark.parametrize("phrase", ["cancel", "start over", "never mind", "nevermind",
                                     "another hotel", "different hotel", "wrong hotel"])
def test_short_standalone_cancel_phrases_still_reset(sent, phrase):
    _open_awaiting()
    v6.handle_batch("cust", [{"type": "text", "text": phrase}])
    assert "cust" not in v6._WA_SESSIONS


# -- v6's own addition: error handling never leaks the exception, never ---
# -- throws away the customer's work, and offers a real retry -------------

def _boom(*a, **kw):
    raise ValueError("boom -- some internal detail nobody should ever see")


def test_unhandled_error_never_leaks_exception_text_and_keeps_session(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.extract_llm.extract_clarification", _boom)
    _open_awaiting()
    v6.handle_batch("cust", [{"type": "text", "text": "some answer"}])

    assert "cust" in v6._WA_SESSIONS   # nothing the customer told us is lost
    kind, body, buttons = next(m for m in sent if m[0] == "buttons")
    assert "ValueError" not in body and "boom" not in body and "internal detail" not in body
    assert "something went wrong" in body.lower()
    assert [bid for bid, _ in buttons] == ["try_again", "start_new_chat"]


def test_try_again_replays_the_last_batch_and_can_succeed(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    calls = {"n": 0}

    def _flaky(missing, text, media=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return ({"ota_benchmark.final_payable": 31683}, None)

    monkeypatch.setattr("yta.extract_llm.extract_clarification", _flaky)
    monkeypatch.setattr("yta.web._resolve", lambda packet: {"room_map": {"matched": False}})
    _open_awaiting()
    v6.handle_batch("cust", [{"type": "text", "text": "31683"}])
    assert calls["n"] == 1
    assert "cust" in v6._WA_SESSIONS   # first attempt failed -- session kept
    kind, body, buttons = next(m for m in sent if m[0] == "buttons")
    assert [bid for bid, _ in buttons] == ["try_again", "start_new_chat"]

    sent.clear()
    v6.handle_batch("cust", [{"type": "button_reply", "button_id": "try_again", "text": "Try again"}])
    assert calls["n"] == 2   # replayed the exact same reply, not a new one
    assert sent[-1][0] == "buttons"   # this time it reached _present_deal
    assert "cust" not in v6._WA_SESSIONS


def test_try_again_that_fails_again_gives_up_for_real(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.extract_llm.extract_clarification", _boom)
    _open_awaiting()
    v6.handle_batch("cust", [{"type": "text", "text": "some answer"}])
    assert "cust" in v6._WA_SESSIONS   # first failure -- kept, offered a retry

    sent.clear()
    v6.handle_batch("cust", [{"type": "button_reply", "button_id": "try_again", "text": "Try again"}])
    assert "cust" not in v6._WA_SESSIONS   # second failure -- real give-up
    assert "cust" not in v6._LAST_BATCH_ITEMS
    assert any("didn't work" in m[1].lower() or "start fresh" in m[1].lower()
               for m in sent if m[0] == "text")
    kind, body, _ = next(m for m in sent if m[0] == "buttons")
    assert body == v6._ONBOARDING_CHOICE_TEXT


def test_try_again_with_nothing_stored_falls_back_to_onboarding(sent):
    v6.handle_batch("cust", [{"type": "button_reply", "button_id": "try_again", "text": "Try again"}])
    assert any("nothing to retry" in m[1].lower() for m in sent if m[0] == "text")
    kind, body, _ = next(m for m in sent if m[0] == "buttons")
    assert body == v6._ONBOARDING_CHOICE_TEXT


def test_successful_batch_does_not_offer_a_retry(sent):
    # Sanity check: the try_again/start_over pair is an ERROR-path
    # affordance only -- a perfectly normal onboarding message must not
    # accidentally carry it.
    v6.handle_batch("cust", [{"type": "text", "text": "hi"}])
    kind, body, buttons = sent[0]
    assert [bid for bid, _ in buttons] != ["try_again", "start_new_chat"]
