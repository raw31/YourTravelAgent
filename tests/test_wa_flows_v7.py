"""Tests for v7 (yta/wa_flows/v7.py) — v6's mechanics carried over unchanged
(error/retry, not_cheaper, cancel-phrase matching, awaiting_field guards,
the 3-strike ladders) are NOT re-tested exhaustively here; v6's own test
file already covers those in full and v7 started as an exact copy of that
logic. This file covers v7's OWN additions: the rewritten onboarding copy,
buttons on every previously button-less message, the native WhatsApp list
message for room selection (with the typed-numeral fallback kept), the
global "talk to a human" and "help" intents, occupancy quick-reply
presets, and showing a field example on the first ask instead of only the
retry.

No network, no real LLM — whatsapp.find_url/download_media/send_buttons/
send_list, pipeline.extract, extract_llm.extract_clarification,
yta.web._resolve, and yta.leads.db.record_lead are all faked/monkeypatched.
"""
import time

import pytest

from yta.wa_flows import v7


@pytest.fixture(autouse=True)
def _clean_sessions():
    v7._WA_SESSIONS.clear()
    v7._PENDING_REFERRALS.clear()
    v7._PENDING_PATH.clear()
    v7._LAST_BATCH_ITEMS.clear()
    yield
    v7._WA_SESSIONS.clear()
    v7._PENDING_REFERRALS.clear()
    v7._PENDING_PATH.clear()
    v7._LAST_BATCH_ITEMS.clear()


@pytest.fixture
def sent(monkeypatch):
    messages = []
    monkeypatch.setattr(v7, "wa_send", lambda frm, text: messages.append(("text", text)))
    monkeypatch.setattr(v7, "wa_send_image",
                        lambda frm, url, caption=None: messages.append(("image", url, caption)))
    monkeypatch.setattr(v7, "wa_send_list",
                        lambda frm, body, button_text, sections:
                            messages.append(("list", body, button_text, sections)))

    monkeypatch.setattr(v7, "wa_send_buttons",
                        lambda frm, body, buttons: messages.append(("buttons", body, buttons)))
    return messages


class _Packet:
    status = "ok"
    missing_mandatory = []

    def __init__(self, missing=(), hotel_name="Test Hotel", room_name="Deluxe Room",
                description=None, ota="test"):
        self._still_missing = list(missing)
        self.hotel = type("H", (), {"name": hotel_name})()
        self.source = type("Src", (), {"ota": ota})()

        class _Stay:
            check_in = "2099-09-21"
            check_out = "2099-09-22"
            occupancy = [{"adults": 2, "children": 0, "child_ages": []}]
            rooms = 1
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
    v7._WA_SESSIONS["cust"] = {
        "state": "awaiting_field", "packet": _Packet(missing), "missing": list(missing),
        "intent": intent, "unproductive_attempts": attempts, "last_activity": time.time() - age_sec,
    }


def _open_choosing_option(options, age_sec=0, attempts=0, resolution=None):
    v7._WA_SESSIONS["cust"] = {
        "state": "choosing_option", "packet": _Packet(), "resolution": resolution or {},
        "options": options, "unproductive_attempts": attempts,
        "last_activity": time.time() - age_sec,
    }


def _rate(oid, room_name, meal, refundable, price):
    return {"option_id": oid, "room_name": room_name, "meal_basis": meal,
            "refundable": refundable, "currency": "INR", "total_price": price}


def _boom(*a, **kw):
    raise ValueError("boom: internal detail that must never reach a customer")


# -- finding 1/2/5: rewritten onboarding -----------------------------------

def test_onboarding_choice_states_the_mechanism_and_gives_an_example(sent):
    v7.handle_batch("cust", [{"type": "text", "text": "hi"}])
    kind, body, buttons = sent[0]
    assert kind == "buttons"
    assert "10" in body and "%" in body   # concrete credibility number, not vague framing
    assert "link" in body.lower() or "screenshot" in body.lower()
    assert "wholesale" not in body.lower() and "tripjack" not in body.lower()
    assert [bid for bid, _ in buttons] == ["have_deal", "search_hotel"]


def test_onboarding_button_labels_are_customer_facing_not_jargon(sent):
    v7.handle_batch("cust", [{"type": "text", "text": "hi"}])
    kind, body, buttons = sent[0]
    titles = [t for _, t in buttons]
    assert "I have a deal" not in titles and "Search a hotel" not in titles
    assert any("room" in t.lower() for t in titles)
    assert any("find" in t.lower() or "search" in t.lower() for t in titles)


# -- finding 6/19: every message now leaves at least one tap --------------

def test_have_deal_followup_has_an_example_and_a_start_over_button(sent):
    v7.handle_batch("cust", [{"type": "button_reply", "button_id": "have_deal", "text": "..."}])
    kind, body, buttons = sent[0]
    assert kind == "buttons"
    assert "for example" in body.lower() or "e.g." in body.lower() or "link to that room" in body.lower()
    assert [bid for bid, _ in buttons] == ["start_new_chat"]


def test_search_hotel_followup_has_an_example_and_a_start_over_button(sent):
    v7.handle_batch("cust", [{"type": "button_reply", "button_id": "search_hotel", "text": "..."}])
    kind, body, buttons = sent[0]
    assert kind == "buttons"
    assert "for example" in body.lower() or "santacruz" in body.lower()
    assert [bid for bid, _ in buttons] == ["start_new_chat"]


def test_unrecognized_text_nudge_now_has_buttons(sent, monkeypatch):
    monkeypatch.setattr("yta.pipeline.extract",
                         lambda *a, **kw: _Packet(missing=["hotel.name"], hotel_name=None))
    v7.handle_batch("cust", [{"type": "text",
                    "text": "just wondering what kind of deals you folks usually find"}])
    kind, body, buttons = next(m for m in sent if m[0] == "buttons")
    assert "couldn't find hotel details" in body.lower()
    assert "start_new_chat" in [b for b, _ in buttons]


def test_no_live_rate_for_hotel_offers_human_help(sent, monkeypatch):
    monkeypatch.setattr("yta.web._resolve", lambda packet: {"room_map": {"matched": False}})
    v7._present_deal("cust", _Packet())
    kind, body, buttons = next(m for m in sent if m[0] == "buttons")
    ids = [b for b, _ in buttons]
    assert "try_another" in ids and "human_help" in ids


def test_no_live_rates_at_all_offers_human_help(sent):
    resolution = {"detail": {"hotel_name": "Taj Santacruz"}, "room_options": {"groups": []}}
    v7._present_option_choices("cust", _Packet(), resolution)
    kind, body, buttons = next(m for m in sent if m[0] == "buttons")
    ids = [b for b, _ in buttons]
    assert "try_another" in ids and "human_help" in ids


# -- finding 3/13: native list message for room selection ------------------

def test_present_option_choices_sends_a_list_not_numbered_text(sent):
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
    v7._present_option_choices("cust", _Packet(), resolution)
    kind, body, button_text, sections = next(m for m in sent if m[0] == "list")
    assert "Taj Santacruz" in body
    assert not any(m[0] == "text" for m in sent)   # not the old numbered-text bubble
    assert [title for title, _ in sections] == ["Deluxe Villa", "Premier Villa"]
    assert len(sections[0][1]) == 2 and len(sections[1][1]) == 1
    assert sections[0][1][0][0] == "opt-1" and sections[1][1][0][0] == "opt-3"
    assert [o["option_id"] for o in v7._WA_SESSIONS["cust"]["options"]] == ["r1", "r2", "p1"]


def test_present_option_choices_ambiguous_callout_still_in_the_body_text(sent):
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
    v7._present_option_choices("cust", _Packet(), resolution)
    kind, body, _, _ = next(m for m in sent if m[0] == "list")
    # closest-match note names the room the algorithm actually matched...
    assert "closest to what you mentioned: *Luxury, Courtyard View*" in body
    # ...and the "starting from" anchor is always the true cheapest, PAIRED
    # WITH ITS OWN room name, regardless of which room was the closest
    # match (findings from live feedback 2026-09-30 -- the old two-callout
    # format could promise "nearest AND cheapest" but only show one when
    # they were the same room; this is a single, always-accurate,
    # self-consistent price+name line instead).
    assert "💰 We have rooms starting from INR 37,718.04 total for 1 night — *Luxury Room Facade View*" in body
    assert "Tap to explore more rooms" in body


def test_present_option_choices_starting_from_price_when_not_ambiguous(sent):
    groups = [
        {"room_type_id": "R1", "room_name": "Deluxe Room", "total_combos": 1,
         "options": [_rate("o1", "Deluxe Room", "Room Only", False, 20000.0)]},
        {"room_type_id": "R2", "room_name": "Premier Room", "total_combos": 1,
         "options": [_rate("o2", "Premier Room", "Room Only", False, 15000.0)]},
    ]
    resolution = {"detail": {"hotel_name": "Taj Santacruz"}, "room_options": {"groups": groups}}
    v7._present_option_choices("cust", _Packet(), resolution)
    kind, body, _, _ = next(m for m in sent if m[0] == "list")
    assert "💰 We have rooms starting from INR 15,000.00 total for 1 night — *Premier Room*" in body
    assert "closest to what you mentioned" not in body


def test_picking_a_room_via_list_row_tap_leads_to_the_confirm_flow(sent):
    options = [
        {"option_id": "s1", "room_name": "Deluxe Room", "meal_basis": "Breakfast",
         "refundable": True, "currency": "INR", "total_price": 20000.0},
        {"option_id": "c2", "room_name": "Premier Room", "meal_basis": "Half Board",
         "refundable": False, "currency": "INR", "total_price": 25000.0},
    ]
    _open_choosing_option(options, resolution={"detail": {"hotel_name": "Taj Santacruz"}})
    v7.handle_batch("cust", [{"type": "button_reply", "button_id": "opt-2", "text": "INR 25,000"}])
    kind, body, buttons = next(m for m in sent if m[0] == "buttons")
    assert "Premier Room" in body
    assert v7._WA_SESSIONS["cust"]["state"] == "presented"
    rm = v7._WA_SESSIONS["cust"]["resolution"]["room_map"]
    assert rm["matched"] is True and rm["ratekey_option_ids"] == ["c2"]


def test_typed_numeral_still_works_after_the_list_conversion(sent):
    # Finding 13: switching the primary UI to a list must not regress a
    # customer who types a number instead of tapping.
    options = [
        {"option_id": "s1", "room_name": "Deluxe Room", "meal_basis": "Breakfast",
         "refundable": True, "currency": "INR", "total_price": 20000.0},
        {"option_id": "c2", "room_name": "Premier Room", "meal_basis": "Half Board",
         "refundable": False, "currency": "INR", "total_price": 25000.0},
    ]
    _open_choosing_option(options, resolution={"detail": {"hotel_name": "Taj Santacruz"}})
    v7.handle_batch("cust", [{"type": "text", "text": "2"}])
    assert v7._WA_SESSIONS["cust"]["state"] == "presented"
    rm = v7._WA_SESSIONS["cust"]["resolution"]["room_map"]
    assert rm["ratekey_option_ids"] == ["c2"]


def test_invalid_list_row_id_falls_back_to_numeral_parser(sent):
    # A malformed/unknown "opt-" id should never crash -- just fall
    # through to the ordinary invalid-reply handling.
    options = [{"option_id": "s1", "room_name": "Deluxe Room", "meal_basis": "Breakfast",
                "refundable": True, "currency": "INR", "total_price": 20000.0}]
    _open_choosing_option(options)
    v7.handle_batch("cust", [{"type": "button_reply", "button_id": "opt-99", "text": "??"}])
    assert "cust" in v7._WA_SESSIONS
    assert any("reply with a number" in m[1].lower() for m in sent if m[0] == "text")


def test_show_list_again_resends_as_a_list(sent):
    options = [{"option_id": "s1", "room_name": "Deluxe Room", "meal_basis": "Breakfast",
                "refundable": True, "currency": "INR", "total_price": 20000.0}]
    resolution = {"detail": {"hotel_name": "Taj Santacruz"},
                  "room_options": {"groups": [{"room_type_id": "R1", "room_name": "Deluxe Room",
                                                "total_combos": 1, "options": options}]}}
    _open_choosing_option(options, attempts=2, resolution=resolution)
    v7.handle_batch("cust", [{"type": "button_reply", "button_id": "show_list_again", "text": "..."}])
    assert v7._WA_SESSIONS["cust"]["unproductive_attempts"] == 0
    kind, body, _, sections = next(m for m in sent if m[0] == "list")
    assert "Taj Santacruz" in body
    assert sections[0][1][0][0] == "opt-1"


def test_choosing_option_strike2_offers_human_help_alongside_show_list_again(sent):
    options = [{"option_id": "s1", "room_name": "Deluxe Room", "meal_basis": "Breakfast",
                "refundable": True, "currency": "INR", "total_price": 20000.0}]
    _open_choosing_option(options, attempts=1)
    v7.handle_batch("cust", [{"type": "text", "text": "still not a number"}])
    kind, body, buttons = next(m for m in sent if m[0] == "buttons")
    assert [bid for bid, _ in buttons] == ["show_list_again", "start_new_chat", "human_help"]


# -- finding 4/9: "talk to a human" as a global intent ----------------------

def test_human_help_button_flags_a_lead_and_keeps_the_session(sent, monkeypatch):
    recorded = []
    monkeypatch.setattr(
        "yta.leads.db.record_lead",
        lambda phone, status, packet, resolution, referred_by=None:
            recorded.append(status) or "BMS-HUMAN1")
    _open_awaiting()
    v7.handle_batch("cust", [{"type": "button_reply", "button_id": "human_help", "text": "Talk to a human"}])
    assert recorded == ["needs_human"]
    assert "cust" in v7._WA_SESSIONS   # not a cancel -- nothing already said is lost
    assert v7._WA_SESSIONS["cust"]["state"] == "awaiting_field"
    assert any("personal look" in m[1].lower() for m in sent if m[0] in ("text", "buttons"))
    assert sent[-1][0] == "buttons"   # never a plain-text dead end


def test_human_help_text_phrase_works_from_any_state(sent, monkeypatch):
    recorded = []
    monkeypatch.setattr(
        "yta.leads.db.record_lead",
        lambda phone, status, packet, resolution, referred_by=None:
            recorded.append(status) or "BMS-HUMAN2")
    _open_awaiting()
    v7.handle_batch("cust", [{"type": "text", "text": "can I talk to a human"}])
    assert recorded == ["needs_human"]
    assert "cust" in v7._WA_SESSIONS


def test_human_help_with_no_session_still_flags_the_phone_number(sent, monkeypatch):
    # Found by the e2e QA run: a guest who asks for a human before typing
    # anything was told "I'll take a personal look" but NO lead was recorded,
    # so nobody would ever follow up. A bare-phone lead is still actionable.
    recorded = []
    monkeypatch.setattr("yta.leads.db.record_lead",
                         lambda phone, status, packet, *a, **kw: recorded.append((phone, status)) or "BMS-X")
    v7.handle_batch("cust", [{"type": "text", "text": "talk to a human"}])
    assert recorded == [("cust", "needs_human")]
    kind, body, buttons = sent[-1]
    assert kind == "buttons" and "personal look" in body.lower()
    assert [b for b, _ in buttons] == ["have_deal", "search_hotel"]   # a way to carry on meanwhile


def test_human_help_phrase_does_not_fire_when_message_has_a_url(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: "https://booking.com/x")
    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: _Packet(missing=[]))
    monkeypatch.setattr("yta.web._resolve", lambda packet: {"room_map": {"matched": False}})
    calls = []
    monkeypatch.setattr("yta.leads.db.record_lead", lambda *a, **kw: calls.append(1) or "BMS-X")
    v7.handle_batch("cust", [{"type": "text",
                    "text": "talk to a human, here's my link https://booking.com/x"}])
    assert calls == []   # the URL means this is a real submission, not a human-help request


# -- finding 8: "help" as a global intent -----------------------------------

def test_help_phrase_resends_onboarding_choice_without_wiping_the_session(sent):
    _open_awaiting()
    v7.handle_batch("cust", [{"type": "text", "text": "help"}])
    kind, body, buttons = sent[0]
    assert kind == "buttons" and body == v7._ONBOARDING_CHOICE_TEXT
    assert "cust" in v7._WA_SESSIONS   # unlike cancel, this doesn't reset anything
    assert v7._WA_SESSIONS["cust"]["state"] == "awaiting_field"


def test_help_phrase_does_not_fire_on_a_long_real_query(sent, monkeypatch):
    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: _Packet(missing=[]))
    monkeypatch.setattr("yta.web._resolve", lambda packet: {"room_map": {"matched": False}})
    v7.handle_batch("cust", [{"type": "text",
                    "text": "can you help me check the Taj Santacruz for 2 adults, INR 15000"}])
    # a real query mentioning "help" in passing must still run extraction,
    # not get short-circuited into the capability message
    assert not any(m[0] == "buttons" and m[1] == v7._ONBOARDING_CHOICE_TEXT for m in sent)


# -- finding 11: occupancy quick-reply presets ------------------------------

def test_occupancy_only_missing_offers_quick_reply_presets(sent, monkeypatch):
    monkeypatch.setattr("yta.pipeline.extract",
                         lambda *a, **kw: _Packet(missing=["stay.occupancy"]))
    v7.handle_batch("cust", [{"type": "text", "text": "https://booking.com/x is what I picked"}])
    kind, body, buttons = next(m for m in sent if m[0] == "buttons")
    ids = [b for b, _ in buttons]
    assert "occ_2a1r" in ids and "occ_2a_kids" in ids and "start_new_chat" in ids
    assert "human_help" not in ids   # WhatsApp's 3-button cap -- presets take priority here


def test_multi_field_missing_offers_human_help_not_occupancy_presets(sent, monkeypatch):
    monkeypatch.setattr(
        "yta.pipeline.extract",
        lambda *a, **kw: _Packet(missing=["stay.occupancy", "ota_benchmark.final_payable"]))
    v7.handle_batch("cust", [{"type": "text", "text": "https://booking.com/x is what I picked"}])
    kind, body, buttons = next(m for m in sent if m[0] == "buttons")
    ids = [b for b, _ in buttons]
    assert ids == ["start_new_chat", "human_help"]


def test_occupancy_preset_tap_feeds_extraction_as_plain_text(sent, monkeypatch):
    seen = {}

    def _fake_clarify(missing, text, media=None):
        seen["text"] = text
        return ({"stay.occupancy": [{"adults": 2, "children": 0}]}, None, True)

    monkeypatch.setattr("yta.extract_llm.extract_clarification_ex", _fake_clarify)
    monkeypatch.setattr("yta.web._resolve", lambda packet: {"room_map": {"matched": False}})
    _open_awaiting(missing=("stay.occupancy",))
    v7.handle_batch("cust", [{"type": "button_reply", "button_id": "occ_2a1r", "text": "2 adults, 1 room"}])
    assert seen["text"] == "2 adults, 1 room"
    assert sent[-1][0] == "buttons"   # resolved -- reached _present_deal


# -- finding 7: example shown on the FIRST ask, not only the retry ---------

def test_first_ask_for_a_missing_field_includes_an_example(sent, monkeypatch):
    monkeypatch.setattr("yta.pipeline.extract",
                         lambda *a, **kw: _Packet(missing=["requested_offer.room_name"]))
    v7.handle_batch("cust", [{"type": "text", "text": "https://booking.com/x"}])
    body = next(m[1] for m in sent if m[0] == "buttons")
    assert "for example" in body.lower()
    assert "deluxe room" in body.lower()


def test_clarify_question_is_never_followed_by_a_generic_example(sent, monkeypatch):
    # A specific `clarify` question (from extract_clarification's second
    # return value) is already precise and on-point -- appending a generic
    # field example after it would be redundant. The recap/opener still
    # appear (found_and_ask_message's own job), only the "For example, ..."
    # suffix is skipped.
    packet = _Packet(missing=["stay.check_in"])
    msg = v7._ask_message_with_example(packet, ["stay.check_in"], clarify="Which year did you mean?")
    assert msg.endswith("Which year did you mean?")
    assert "for example" not in msg.lower()


# -- finding 12: strike-2 alternatives now say something different --------

def test_strike2_send_screenshot_names_what_a_good_screenshot_looks_like(sent, monkeypatch):
    monkeypatch.setattr("yta.extract_llm.extract_clarification_ex", lambda missing, text, media=None: ({}, None, True))
    _open_awaiting(attempts=1)
    v7.handle_batch("cust", [{"type": "text", "text": "still nothing useful here"}])
    sent.clear()
    v7.handle_batch("cust", [{"type": "button_reply", "button_id": "await_send_screenshot", "text": "..."}])
    body = next(m[1] for m in sent if m[0] == "buttons")
    assert "screenshot" in body.lower() and "confirmation" in body.lower()

    sent.clear()
    _open_awaiting(attempts=1)
    v7.handle_batch("cust", [{"type": "text", "text": "still nothing useful here"}])
    sent.clear()
    v7.handle_batch("cust", [{"type": "button_reply", "button_id": "await_type_details", "text": "..."}])
    body2 = next(m[1] for m in sent if m[0] == "buttons")
    assert "confirmation or checkout page" not in body2.lower()


# -- carried-over v6 mechanics still work (spot checks, not exhaustive) ----

def test_unhandled_error_still_never_leaks_exception_and_keeps_session(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.extract_llm.extract_clarification_ex", _boom)
    _open_awaiting()
    v7.handle_batch("cust", [{"type": "text", "text": "some answer"}])
    assert "cust" in v7._WA_SESSIONS
    kind, body, buttons = next(m for m in sent if m[0] == "buttons")
    assert "ValueError" not in body and "boom" not in body
    assert [bid for bid, _ in buttons] == ["try_again", "start_new_chat"]


def test_short_cancel_phrase_still_resets_in_full(sent):
    _open_awaiting()
    v7.handle_batch("cust", [{"type": "text", "text": "cancel please"}])
    assert "cust" not in v7._WA_SESSIONS
    kind, body, _ = sent[-1]
    assert kind == "buttons" and body == v7._ONBOARDING_CHOICE_TEXT


def test_bookable_offer_shows_percentage_in_headline_and_offers_explore_other_rooms(sent):
    # Live feedback 2026-09-30: the % was buried below the fold; now it's
    # in the headline itself, and a third button lets a customer who
    # likes the hotel but not THIS room look at others without declining.
    monkeypatch_options = [
        {"option_id": "o1", "rooms": [{"id": "R1", "name": "Deluxe Room"}],
         "meal_basis": "Room Only", "refundable": False, "currency": "INR", "total_price": 25000.0},
    ]
    resolution = {
        "detail": {"hotel_name": "Taj Santacruz", "options": monkeypatch_options},
        "room_map": {"matched": True, "ratekey_option_ids": ["o1"], "rate_options": [
            {"option_id": "o1", "room_type_id": "R1", "room_name": "Deluxe Room",
             "currency": "INR", "total_price": 25000.0},
        ]},
    }
    v7._send_deal_result("cust", _Packet(), resolution)
    kind, body, buttons = next(m for m in sent if m[0] == "buttons")
    assert "% better rate" in body   # ota 31,683 -> 25,000 = 21% off
    assert "found you a better rate" not in body.lower()   # old generic headline gone
    assert [bid for bid, _ in buttons] == ["confirm_book", "decline_book", "explore_other_rooms"]
    assert v7._WA_SESSIONS["cust"]["state"] == "presented"


def test_explore_other_rooms_shows_every_room_including_the_matched_one(sent):
    resolution = {
        "detail": {"hotel_name": "Taj Santacruz", "options": [
            {"option_id": "o1", "rooms": [{"id": "R1", "name": "Deluxe Room"}],
             "meal_basis": "Room Only", "refundable": False, "currency": "INR", "total_price": 25000.0},
            {"option_id": "o2", "rooms": [{"id": "R2", "name": "Premier Room"}],
             "meal_basis": "Breakfast", "refundable": True, "currency": "INR", "total_price": 30000.0},
        ]},
        "room_map": {"matched": True, "ratekey_option_ids": ["o1"], "rate_options": [
            {"option_id": "o1", "room_type_id": "R1", "room_name": "Deluxe Room",
             "currency": "INR", "total_price": 25000.0},
        ]},
    }
    v7._WA_SESSIONS["cust"] = {"state": "presented", "packet": _Packet(), "resolution": resolution,
                              "savings_line": None, "confirm_line": None, "last_activity": time.time()}
    v7.handle_batch("cust", [{"type": "button_reply", "button_id": "explore_other_rooms",
                              "text": "Explore other rooms"}])
    kind, body, button_text, sections = next(m for m in sent if m[0] == "list")
    names = [title for title, _ in sections]
    assert "Deluxe Room" in names and "Premier Room" in names   # matched room NOT excluded
    assert v7._WA_SESSIONS["cust"]["state"] == "choosing_option"


def test_explore_other_rooms_with_no_cached_detail_falls_back_gracefully(sent):
    v7._WA_SESSIONS["cust"] = {"state": "presented", "packet": _Packet(), "resolution": {},
                              "savings_line": None, "confirm_line": None, "last_activity": time.time()}
    v7.handle_batch("cust", [{"type": "button_reply", "button_id": "explore_other_rooms",
                              "text": "Explore other rooms"}])
    kind, body, buttons = next(m for m in sent if m[0] == "buttons")
    assert "only option" in body.lower()
    assert [bid for bid, _ in buttons] == ["confirm_book", "decline_book"]


def test_matched_but_not_cheaper_still_opens_the_not_cheaper_state(sent, monkeypatch):
    monkeypatch.setattr("yta.leads.db.record_lead", lambda *a, **kw: "BMS-NODEAL1")
    monkeypatch.setattr(
        "yta.web._resolve",
        lambda packet: {"room_map": {"matched": True, "ratekey_option_ids": ["o1"], "rate_options": [
            {"option_id": "o1", "room_type_id": "R1", "room_name": "Deluxe Room",
             "currency": "INR", "total_price": 40000.0},
        ]}},
    )
    v7._present_deal("cust", _Packet())
    kind, body, buttons = next(m for m in sent if m[0] == "buttons")
    assert "couldn't beat" in body.lower()
    assert v7._WA_SESSIONS["cust"]["state"] == "not_cheaper"


# -- live 2026-10-07 fixes: slow notice, past dates, expired buttons --------

class _RecPacket(_Packet):
    """_Packet whose add() really stores stay dates (the base stub doesn't)."""
    def add(self, path, val, *a, **kw):
        if path == "stay.check_in":
            self.stay.check_in = val
        elif path == "stay.check_out":
            self.stay.check_out = val
        super().add(path, val, *a, **kw)


def _year_packet(ci="2024-12-17", co="2024-12-18"):
    p = _RecPacket()
    p.stay.check_in, p.stay.check_out = ci, co
    return p


def test_past_dates_are_never_searched_the_guest_is_asked_for_the_year(sent, monkeypatch):
    # Live 2026-10-07: a year-less screenshot was read as 2024 -> TripJack
    # rejected it -> "no better rate". Policy: never guess a year, never
    # search a past date -- ask.
    monkeypatch.setattr("yta.whatsapp.find_url", lambda t: "https://x.com/h" if "http" in (t or "") else None)
    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: _year_packet())
    resolved = []
    monkeypatch.setattr("yta.web._resolve", lambda p: resolved.append(1) or {})
    v7.handle_batch("cust", [{"type": "text", "text": "https://x.com/h"}])
    assert resolved == []
    kind, body, buttons = next(m for m in sent if m[0] == "buttons")
    assert "which year" in body.lower() and "17 Dec" in body
    ids = [b for b, _ in buttons]
    assert ids[:2] == ["year_0", "year_1"]
    assert v7._WA_SESSIONS["cust"]["state"] == "awaiting_year"
    from datetime import date
    for (ci, co) in v7._WA_SESSIONS["cust"]["year_options"].values():
        assert ci >= date.today() and co > ci


def test_month_day_only_source_asks_for_the_year(sent, monkeypatch):
    p = _RecPacket()
    p.stay.check_in = p.stay.check_out = None
    p._year_missing = {"check_in": (12, 17), "check_out": (12, 18)}
    monkeypatch.setattr("yta.whatsapp.find_url", lambda t: "https://x.com/h" if "http" in (t or "") else None)
    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: p)
    v7.handle_batch("cust", [{"type": "text", "text": "https://x.com/h"}])
    assert v7._WA_SESSIONS["cust"]["state"] == "awaiting_year"


def test_tapping_a_year_sets_the_dates_and_continues_to_the_rates(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda t: "https://x.com/h" if "http" in (t or "") else None)
    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: _year_packet())
    seen = {}
    monkeypatch.setattr("yta.web._resolve",
                        lambda p: seen.update(ci=p.stay.check_in) or {"room_map": {"matched": False}})
    v7.handle_batch("cust", [{"type": "text", "text": "https://x.com/h"}])
    want = v7._WA_SESSIONS["cust"]["year_options"]["year_1"][0].isoformat()
    v7.handle_batch("cust", [{"type": "button_reply", "button_id": "year_1", "text": "x"}])
    assert seen["ci"] == want
    assert "cust" not in v7._WA_SESSIONS


def test_typing_a_year_also_works(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda t: "https://x.com/h" if "http" in (t or "") else None)
    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: _year_packet())
    seen = {}
    monkeypatch.setattr("yta.web._resolve",
                        lambda p: seen.update(ci=p.stay.check_in) or {"room_map": {"matched": False}})
    v7.handle_batch("cust", [{"type": "text", "text": "https://x.com/h"}])
    y = v7._WA_SESSIONS["cust"]["year_options"]["year_0"][0].year
    v7.handle_batch("cust", [{"type": "text", "text": f"it's {y}"}])
    assert seen["ci"].startswith(str(y))


def test_future_dates_with_a_year_are_not_questioned(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda t: "https://x.com/h" if "http" in (t or "") else None)
    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: _Packet(missing=[]))
    monkeypatch.setattr("yta.web._resolve", lambda p: {"room_map": {"matched": False}})
    v7.handle_batch("cust", [{"type": "text", "text": "https://x.com/h"}])
    assert not any("which year" in m[1].lower() for m in sent if m[0] == "buttons")


def test_pipeline_keeps_month_day_dates_out_of_the_packet():
    from yta.pipeline import _apply
    from yta.schema import BookingIntent, Source
    pkt = BookingIntent(source=Source(ota="t", url="", page_type="u",
                                      extraction_method="d", extracted_at=""))
    res = type("R", (), {"fields": {"stay.check_in": "12-17", "stay.check_out": "12-18"},
                         "confidence": {}, "provider": "x", "contradictions": []})()
    _apply(pkt, res)
    assert pkt.stay.check_in is None and pkt.stay.check_out is None
    assert pkt._year_missing == {"check_in": (12, 17), "check_out": (12, 18)}


def test_slow_extraction_sends_a_still_reading_notice(sent, monkeypatch):
    import time as _t
    monkeypatch.setattr(v7, "_SLOW_NOTICE_SEC", 0.05)
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: "https://booking.com/x")

    def _slow(*a, **kw):
        _t.sleep(0.4)
        return _Packet(missing=[])

    monkeypatch.setattr("yta.pipeline.extract", _slow)
    monkeypatch.setattr("yta.web._resolve", lambda packet: {"room_map": {"matched": False}})
    v7.handle_batch("cust", [{"type": "text", "text": "https://booking.com/x"}])
    assert any("Still reading" in m[1] for m in sent if m[0] == "text")


def test_fast_extraction_sends_no_still_reading_notice(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: "https://booking.com/x")
    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: _Packet(missing=[]))
    monkeypatch.setattr("yta.web._resolve", lambda packet: {"room_map": {"matched": False}})
    v7.handle_batch("cust", [{"type": "text", "text": "https://booking.com/x"}])
    import time as _t; _t.sleep(0.1)
    assert not any("Still reading" in m[1] for m in sent if m[0] == "text")


def test_tap_on_an_old_offer_button_with_no_session_says_it_expired(sent):
    for bid in ("confirm_book", "explore_other_rooms", "opt-3"):
        sent.clear()
        v7.handle_batch("cust", [{"type": "button_reply", "button_id": bid, "text": "x"}])
        kind, body, buttons = sent[0]
        assert "expired" in body.lower()
        assert [b for b, _ in buttons] == ["have_deal", "search_hotel"]


# -- owner policy: never guess hotel / dates / room / occupancy -- ask -------

def test_even_split_occupancy_is_dropped_and_the_guest_is_asked(sent, monkeypatch):
    # "4 adults, 2 rooms" with no per-room breakdown -> resolver assumed an even
    # split (source even_split, conf 0.5). That is a guess: ask, don't price.
    class _P(_RecPacket):
        def check_mandatory(self):
            return [] if self.stay.occupancy else ["stay.occupancy"]
    p = _P()
    p.stay.occupancy = [{"adults": 2, "children": 0, "child_ages": []}] * 2
    p.stay.occupancy_source = "even_split"
    p.stay.occupancy_confidence = 0.5
    monkeypatch.setattr("yta.whatsapp.find_url", lambda t: "https://x.com/h" if "http" in (t or "") else None)
    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: p)
    resolved = []
    monkeypatch.setattr("yta.web._resolve", lambda pk: resolved.append(1) or {})
    v7.handle_batch("cust", [{"type": "text", "text": "https://x.com/h"}])
    assert resolved == []
    assert v7._WA_SESSIONS["cust"]["state"] == "awaiting_field"
    assert "stay.occupancy" in v7._WA_SESSIONS["cust"]["missing"]


def test_confident_occupancy_is_kept(sent, monkeypatch):
    p = _RecPacket()
    p.stay.occupancy_source = "url:room1"
    p.stay.occupancy_confidence = 0.95
    monkeypatch.setattr("yta.whatsapp.find_url", lambda t: "https://x.com/h" if "http" in (t or "") else None)
    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: p)
    monkeypatch.setattr("yta.web._resolve", lambda pk: {"room_map": {"matched": False}})
    v7.handle_batch("cust", [{"type": "text", "text": "https://x.com/h"}])
    assert p.stay.occupancy


def _fuzzy_resolution():
    return {"band": "medium", "match": {"hotel_name": "Hotel Grand Palace"},
            "room_map": {"matched": False}}


def test_medium_hotel_match_is_confirmed_before_any_rate_is_shown(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda t: "https://x.com/h" if "http" in (t or "") else None)
    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: _Packet(missing=[]))
    monkeypatch.setattr("yta.web._resolve", lambda pk: _fuzzy_resolution())
    v7.handle_batch("cust", [{"type": "text", "text": "https://x.com/h"}])
    kind, body, buttons = [m for m in sent if m[0] == "buttons"][-1]
    assert "Hotel Grand Palace" in body
    assert [b for b, _ in buttons] == ["hotel_yes", "hotel_no"]
    assert v7._WA_SESSIONS["cust"]["state"] == "confirming_hotel"


def test_confirming_the_hotel_continues_to_the_rates(sent, monkeypatch):
    shown = []
    monkeypatch.setattr(v7, "_present_resolution", lambda frm, pk, rz: shown.append(rz))
    v7._WA_SESSIONS["cust"] = {"state": "confirming_hotel", "packet": _Packet(),
                               "resolution": _fuzzy_resolution(), "unproductive_attempts": 0,
                               "last_activity": time.time()}
    v7.handle_batch("cust", [{"type": "button_reply", "button_id": "hotel_yes", "text": "Yes"}])
    assert shown and "cust" not in v7._WA_SESSIONS


def test_rejecting_the_hotel_asks_for_the_name_and_shows_no_rate(sent, monkeypatch):
    shown = []
    monkeypatch.setattr(v7, "_present_resolution", lambda frm, pk, rz: shown.append(rz))
    v7._WA_SESSIONS["cust"] = {"state": "confirming_hotel", "packet": _Packet(),
                               "resolution": _fuzzy_resolution(), "unproductive_attempts": 0,
                               "last_activity": time.time()}
    v7.handle_batch("cust", [{"type": "button_reply", "button_id": "hotel_no", "text": "No"}])
    assert shown == []
    assert v7._WA_SESSIONS["cust"]["state"] == "awaiting_field"
    assert v7._WA_SESSIONS["cust"]["missing"] == ["hotel.name"]


def test_high_band_hotel_match_is_not_questioned(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda t: "https://x.com/h" if "http" in (t or "") else None)
    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: _Packet(missing=[]))
    monkeypatch.setattr("yta.web._resolve",
                        lambda pk: {"band": "high", "match": {"hotel_name": "Test Hotel"},
                                    "room_map": {"matched": False}})
    v7.handle_batch("cust", [{"type": "text", "text": "https://x.com/h"}])
    assert not any("Just to be sure" in m[1] for m in sent if m[0] == "buttons")


def test_tapping_a_year_does_not_re_ask_for_dates_from_a_stale_missing_list(sent, monkeypatch):
    # Live 2026-10-07: after tapping "17 Dec-18 Dec 2026" the bot replied "I
    # still need your dates" -- packet.missing_mandatory was the pre-year
    # snapshot and still listed check_in/check_out.
    class _Stale(_RecPacket):
        missing_mandatory = ["stay.check_in", "stay.check_out"]

        def check_mandatory(self):
            return [f for f, v in (("stay.check_in", self.stay.check_in),
                                   ("stay.check_out", self.stay.check_out)) if not v]
    p = _Stale()
    p.stay.check_in = p.stay.check_out = None
    p._year_missing = {"check_in": (12, 17), "check_out": (12, 18)}
    monkeypatch.setattr("yta.whatsapp.find_url", lambda t: "https://x.com/h" if "http" in (t or "") else None)
    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: p)
    resolved = []
    monkeypatch.setattr("yta.web._resolve", lambda pk: resolved.append(pk.stay.check_in) or {"room_map": {"matched": False}})
    v7.handle_batch("cust", [{"type": "text", "text": "https://x.com/h"}])
    v7.handle_batch("cust", [{"type": "button_reply", "button_id": "year_0", "text": "x"}])
    assert resolved, "should go on to fetch rates"
    assert not any("still need" in m[1].lower() or "your dates" in m[1].lower()
                   for m in sent if m[0] == "buttons")


def test_comparison_deal_message_labels_the_price_as_a_total(sent):
    # Found by the e2e QA run: only the non-comparable variant said "total for
    # N night(s)"; the ~OTA~ -> *ours* comparison had a bare money-bag header.
    res = _matched_resolution() if "_matched_resolution" in globals() else {
        "detail": {"hotel_name": "Taj", "options": []},
        "room_map": {"matched": True, "ratekey_option_ids": ["o1"], "rate_options": [
            {"option_id": "o1", "room_type_id": "R1", "room_name": "Deluxe Room",
             "currency": "INR", "total_price": 25000.0}]}}
    v7._send_deal_result("cust", _Packet(), res)
    kind, body, buttons = next(m for m in sent if m[0] == "buttons")
    assert "Total for 1 night" in body


# -- found by the e2e QA run (scripts/qa/run.py) --------------------------

def test_room_list_headers_are_cut_at_a_word_and_the_full_name_is_kept():
    from yta.wa_shared import room_list_sections
    groups = [{"room_name": "Luxury Room City View Twin Bed",
               "options": [{"meal_basis": "Room Only", "refundable": True,
                            "currency": "INR", "total_price": 23790.2}]},
              {"room_name": "Deluxe Room",
               "options": [{"meal_basis": "Breakfast", "refundable": False,
                            "currency": "INR", "total_price": 30000.0}]}]
    (t1, r1), (t2, r2) = room_list_sections(groups)
    assert len(t1) <= 24 and t1.endswith("…") and not t1.endswith("Tw…")
    assert "Luxury Room City View Twin Bed" in r1[0][2] and len(r1[0][2]) <= 72
    assert t2 == "Deluxe Room" and r2[0][2] == "Breakfast · Non-refundable"   # short names untouched


def test_every_occupancy_preset_fits_a_whatsapp_button():
    from yta.wa_shared import OCCUPANCY_QUICK_REPLIES
    assert all(len(title) <= 20 for _, title in OCCUPANCY_QUICK_REPLIES)


def _split_packet():
    class _P(_RecPacket):
        def check_mandatory(self):
            return [] if self.stay.occupancy else ["stay.occupancy"]

        def add(self, path, val, *a, **kw):          # the real packet stores it
            if path == "stay.occupancy":
                self.stay.occupancy = val
            super().add(path, val, *a, **kw)
    p = _P()
    p.stay.occupancy = [{"adults": 2, "children": 0, "child_ages": []}] * 2
    p.stay.rooms = 2
    p.stay.occupancy_source, p.stay.occupancy_confidence = "even_split", 0.5
    return p


def test_split_ask_quotes_the_guests_own_totals_and_offers_an_explicit_even_split(sent):
    p = _split_packet()
    v7._continue_with_packet("cust", p, None, None)
    kind, body, buttons = sent[-1]
    assert "4 adults" in body and "2 rooms" in body
    assert [b for b, _ in buttons][0] == "split_even" and buttons[0][1] == "2 adults in each"
    s = v7._WA_SESSIONS["cust"]
    assert s["state"] == "awaiting_field" and "2 rooms" in s["clarify_text"]   # context for the typed reply


def test_tapping_the_even_split_sets_the_occupancy_and_goes_on_to_the_rates(sent, monkeypatch):
    resolved = []
    monkeypatch.setattr("yta.web._resolve", lambda pk: resolved.append(pk.stay.occupancy) or {"room_map": {"matched": False}})
    p = _split_packet()
    v7._continue_with_packet("cust", p, None, None)
    v7.handle_batch("cust", [{"type": "button_reply", "button_id": "split_even", "text": "2 adults in each"}])
    assert resolved and [o["adults"] for o in resolved[0]] == [2, 2]
    assert p.stay.occupancy_source == "guest_confirmed"


def test_no_even_split_shortcut_when_children_are_in_the_totals(sent):
    p = _split_packet()
    p.stay.occupancy = [{"adults": 2, "children": 1, "child_ages": [5]}] * 2
    v7._continue_with_packet("cust", p, None, None)
    assert "split_even" not in [b for b, _ in sent[-1][2]]


def test_stale_even_split_tap_after_a_restart_gets_the_expired_reply(sent):
    v7.handle_batch("cust", [{"type": "button_reply", "button_id": "split_even", "text": "2 adults in each"}])
    kind, body, buttons = sent[-1]
    assert "expired" in body.lower()


# -- LLM outage is OUR problem, never counted against the guest -------------

def test_llm_outage_during_extraction_apologises_with_try_again_not_a_field_ask(sent, monkeypatch):
    p = _Packet(missing=["hotel.name"])
    p._llm_failed = True
    monkeypatch.setattr("yta.whatsapp.find_url", lambda t: None)
    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: p)
    v7.handle_batch("cust", [{"type": "text", "text": "Taj Santacruz Mumbai 17 Dec 2027 2 adults 25000"}])
    kind, body, buttons = sent[-1]
    assert "trouble reading" in body.lower()
    assert [b for b, _ in buttons] == ["try_again", "start_new_chat"]
    assert "cust" not in v7._WA_SESSIONS                 # nothing half-asked


def test_llm_outage_while_awaiting_a_field_costs_no_strike(sent, monkeypatch):
    monkeypatch.setattr("yta.extract_llm.extract_clarification_ex", lambda *a, **kw: ({}, None, False))
    _open_awaiting(attempts=1)
    v7.handle_batch("cust", [{"type": "text", "text": "INR 31,000"}])
    assert v7._WA_SESSIONS["cust"]["unproductive_attempts"] == 1   # unchanged
    assert v7._WA_SESSIONS["cust"]["state"] == "awaiting_field"
    assert "trouble reading" in sent[-1][1].lower()


# -- found by the e2e QA run: honest "no rate" reasons, merged ack, occupancy text --

def test_occ_repr_describes_multi_room_bookings_instead_of_repeating_the_line():
    from yta.wa_shared import occ_repr
    two = {"adults": 2, "children": 0, "child_ages": []}
    assert occ_repr([two]) == "2 adults"
    assert occ_repr([two, two]) == "2 rooms · 2 adults each"
    assert occ_repr([{"adults": 3, "children": 0}, {"adults": 1, "children": 0}]) == "2 rooms · 3 adults + 1 adult"


def test_no_rate_because_the_supplier_errored_says_so_and_offers_try_again(sent):
    v7._present_resolution("cust", _Packet(), {"match": {"hotel_name": "H"}, "detail_error": "supplier unavailable"})
    kind, body, buttons = sent[-1]
    assert "trouble reaching the live rates" in body and "better live rate" not in body
    assert [b for b, _ in buttons] == ["try_again", "human_help"]


def test_no_rate_because_the_hotel_is_unknown_asks_to_check_the_name(sent):
    v7._present_resolution("cust", _Packet(hotel_name="Zzyzx Inn"), {"band": "none", "match": None})
    kind, body, buttons = sent[-1]
    assert "couldn't find *Zzyzx Inn*" in body
    assert "try_another" in [b for b, _ in buttons]


def test_no_rate_because_the_stay_is_sold_out_names_the_dates_problem(sent):
    v7._present_resolution("cust", _Packet(), {"match": {"hotel_name": "H"}, "detail": {"options": []}})
    kind, body, buttons = sent[-1]
    assert "don't see any live rates" in body and "different dates" in body


def test_one_ack_not_two_between_the_recap_and_the_rates(sent, monkeypatch):
    monkeypatch.setattr("yta.web._resolve", lambda pk: {"room_map": {"matched": False}})
    v7._continue_with_packet("cust", _Packet(missing=[]), None, None)
    texts = [m[1] for m in sent if m[0] == "text"]
    assert len(texts) == 1 and "Checking the best rate now" in texts[0]


# -- owner policy: never claim a saving against a DIFFERENT room's OTA price --

def _list_resolution(reference=None, nearest=None):
    ro = {"groups": [], "no_match": True}
    if nearest:
        ro["nearest_match_room_type_id"] = nearest
    r = {"detail": {"hotel_name": "Taj", "options": []}, "room_options": ro}
    if reference:
        r["reference_room_type_id"] = reference
    return r


def _pick(sent, resolution, picked):
    v7._WA_SESSIONS["cust"] = {"state": "choosing_option", "packet": _Packet(), "resolution": resolution,
                               "options": [picked], "unproductive_attempts": 0, "last_activity": time.time()}
    v7.handle_batch("cust", [{"type": "button_reply", "button_id": "opt-1", "text": "x"}])
    return next(m for m in reversed(sent) if m[0] == "buttons")


def test_picking_a_room_other_than_the_guests_own_shows_a_plain_rate_without_a_saving_claim(sent):
    picked = {"option_id": "o9", "room_type_id": "R9", "room_name": "Standard Room",
              "currency": "INR", "total_price": 18000.0, "meal_basis": "Room Only", "refundable": True}
    kind, body, buttons = _pick(sent, _list_resolution(reference="R1"), picked)
    assert "better rate" not in body.lower() and "you save" not in body.lower()
    assert "couldn't beat" not in body.lower()
    assert "Pocket Stays price" in body and "18,000" in body
    assert "confirm_book" in [b for b, _ in buttons]


def test_picking_a_room_other_than_the_guests_own_never_dead_ends_as_not_cheaper(sent):
    # OTA price 31,683 is for another room; this one costs MORE -- still just a rate.
    picked = {"option_id": "o9", "room_type_id": "R9", "room_name": "Suite", "currency": "INR",
              "total_price": 60000.0, "meal_basis": "Room Only", "refundable": True}
    kind, body, buttons = _pick(sent, _list_resolution(reference="R1"), picked)
    assert "couldn't beat" not in body.lower()
    assert v7._WA_SESSIONS["cust"]["state"] == "presented"


def test_picking_the_guests_own_room_from_the_list_still_compares(sent):
    picked = {"option_id": "o1", "room_type_id": "R1", "room_name": "Deluxe Room", "currency": "INR",
              "total_price": 25000.0, "meal_basis": "Room Only", "refundable": True}
    kind, body, buttons = _pick(sent, _list_resolution(reference="R1"), picked)
    assert "% better rate" in body


def test_picking_the_nearest_match_of_an_ambiguous_list_compares(sent):
    picked = {"option_id": "o1", "room_type_id": "RN", "room_name": "Deluxe Room", "currency": "INR",
              "total_price": 25000.0, "meal_basis": "Room Only", "refundable": True}
    kind, body, buttons = _pick(sent, _list_resolution(nearest="RN"), picked)
    assert "% better rate" in body


def test_no_reference_room_at_all_means_no_comparison(sent):
    picked = {"option_id": "o1", "room_type_id": "R1", "room_name": "Deluxe Room", "currency": "INR",
              "total_price": 25000.0, "meal_basis": "Room Only", "refundable": True}
    kind, body, buttons = _pick(sent, _list_resolution(), picked)
    assert "better rate" not in body.lower()


def test_the_reference_room_survives_explore_then_pick_the_original(sent):
    def _opt_d(oid, rid, name, price):
        return {"option_id": oid, "rooms": [{"id": rid, "name": name}], "meal_basis": "Room Only",
                "refundable": False, "currency": "INR", "total_price": price}
    res = {"detail": {"hotel_name": "Taj", "options": [
               _opt_d("o1", "R1", "Luxury Room", 25000.0), _opt_d("o2", "R2", "Standard Room", 18000.0)]},
           "room_map": {"matched": True, "room_type_id": "R1", "ratekey_option_ids": ["o1"], "rate_options": [
               {"option_id": "o1", "room_type_id": "R1", "room_name": "Luxury Room", "currency": "INR",
                "total_price": 25000.0}]}}
    v7._WA_SESSIONS["cust"] = {"state": "presented", "packet": _Packet(), "resolution": res,
                               "savings_line": None, "confirm_line": None, "last_activity": time.time()}
    v7.handle_batch("cust", [{"type": "button_reply", "button_id": "explore_other_rooms", "text": "x"}])
    assert v7._WA_SESSIONS["cust"]["resolution"]["reference_room_type_id"] == "R1"
