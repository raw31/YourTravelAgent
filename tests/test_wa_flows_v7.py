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


def test_onboarding_button_labels_say_what_the_guest_has_not_what_the_bot_does(sent):
    # Owner 2026-10-07: "Help me find a hotel" reads as a city search (the bot can only
    # price ONE specific hotel) and "I picked a room" did not say what happens next.
    v7.handle_batch("cust", [{"type": "text", "text": "hi"}])
    kind, body, buttons = sent[0]
    assert [(i, t) for i, t in buttons] == [("have_deal", "Compare my price"), ("search_hotel", "Check a hotel")]
    assert all(len(t) <= 20 for _, t in buttons)
    # the body explains BOTH paths in plain words, including "one hotel, not a city"
    assert "*Compare my price*" in body and "another site" in body and "beat the price" in body
    assert "*Check a hotel*" in body and "already know the hotel" in body


def test_the_search_prompt_asks_for_an_exact_hotel_and_says_cities_are_not_searchable(sent):
    v7.handle_batch("cust", [{"type": "button_reply", "button_id": "search_hotel", "text": "x"}])
    body = sent[0][1]
    assert "exact hotel name" in body and "not a whole city" in body


def test_a_city_only_message_is_asked_which_hotel_in_that_city(sent):
    p = _Packet(hotel_name=None, missing=["hotel.name"])
    p.hotel.city = "Delhi"
    v7._send_ask("cust", p, ["hotel.name"], None)
    body = sent[-1][1]
    assert body.startswith("🏨 *Which hotel in Delhi?*") and "can't search all of Delhi yet" in body


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
    assert any("reply with a number" in m[1].lower() for m in sent if m[0] in ("text", "buttons"))


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

def test_missing_occupancy_ask_puts_the_question_first_and_has_no_sample_buttons(sent, monkeypatch):
    # Owner feedback 2026-10-07: the question was buried under a recap, and the
    # preset buttons ("2 adults, 1 room") were wrong for a guest who had just
    # said "2 rooms" -- a button can carry a wrong suggestion. Question first,
    # typed example, only the two escapes as buttons.
    monkeypatch.setattr("yta.pipeline.extract",
                         lambda *a, **kw: _Packet(missing=["stay.occupancy"]))
    v7.handle_batch("cust", [{"type": "text", "text": "https://booking.com/x is what I picked"}])
    kind, body, buttons = next(m for m in sent if m[0] == "buttons")
    assert body.startswith("👥 *How many rooms, and how many guests in each?*")
    assert "e.g." in body and "Price shown" not in body and "Taj" not in body   # question only, no recap
    assert not any(b.startswith("occ_") for b, _ in buttons)               # no sample-value buttons
    assert [b for b, _ in buttons] == ["show_anyway", "start_new_chat", "human_help"]
    assert "assume *1 room, 2 adults*" in body                            # the assumption is stated up front


def test_multi_field_missing_offers_human_help_not_occupancy_presets(sent, monkeypatch):
    monkeypatch.setattr(
        "yta.pipeline.extract",
        lambda *a, **kw: _Packet(missing=["stay.occupancy", "ota_benchmark.final_payable"]))
    v7.handle_batch("cust", [{"type": "text", "text": "https://booking.com/x is what I picked"}])
    kind, body, buttons = next(m for m in sent if m[0] == "buttons")
    ids = [b for b, _ in buttons]
    assert ids == ["show_anyway", "start_new_chat", "human_help"]
    assert "assume *1 room, 2 adults*" in body and "without comparing" in body


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
    assert body.startswith("🛏️ *Which room type is it?*")
    assert "e.g." in body and "deluxe room" in body.lower()


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


def test_split_ask_leads_with_the_question_quotes_the_totals_and_suggests_no_split(sent):
    p = _split_packet()
    v7._continue_with_packet("cust", p, None, None)
    kind, body, buttons = sent[-1]
    assert body.startswith("👥 *How are the 4 adults split across the 2 rooms?*")
    assert "Price shown" not in body and "Taj" not in body                      # question only, no recap
    assert [b for b, _ in buttons] == ["start_new_chat", "human_help"]      # no "2 adults in each"
    s = v7._WA_SESSIONS["cust"]
    assert s["state"] == "awaiting_field" and "2 rooms" in s["clarify_text"]   # context for the typed reply


def test_ask_buttons_are_only_escapes_plus_show_anyway_where_it_is_possible(sent):
    for field, anyway in (("hotel.name", False), ("stay.check_in", False), ("stay.occupancy", True),
                          ("requested_offer.room_name", True), ("ota_benchmark.final_payable", True)):
        sent.clear()
        v7._send_ask("cust", _Packet(), [field], None)
        ids = [b for b, _ in sent[-1][2]]
        assert ids == (["show_anyway"] if anyway else []) + ["start_new_chat", "human_help"], field
        assert not any(i.startswith(("occ_", "date_")) for i in ids)       # never a sample answer


def test_show_anyway_is_never_offered_when_the_dates_or_hotel_are_missing(sent):
    v7._send_ask("cust", _Packet(), ["stay.check_in", "stay.occupancy"], None)
    assert "show_anyway" not in [b for b, _ in sent[-1][2]]
    assert "Show my rate anyway" not in sent[-1][1]


def test_a_two_room_guest_is_asked_for_the_distribution_not_given_preset_buttons(sent):
    p = _Packet()
    p.stay.rooms, p.stay.adults, p.stay.children = 2, 5, 0
    v7._send_ask("cust", p, ["stay.occupancy"], None, clarify="Could you specify the number of adults?")
    body = sent[-1][1]
    assert body.startswith("👥 *How are the 5 adults distributed across the 2 rooms?*")
    assert "Room 1: 2 adults, Room 2: 3 adults" in body
    assert not any(b.startswith("occ_") for b, _ in sent[-1][2])


def _tap_anyway(sent, packet, missing, intent=None):
    v7._WA_SESSIONS["cust"] = {"state": "awaiting_field", "packet": packet, "missing": list(missing),
                               "intent": intent, "unproductive_attempts": 0, "last_activity": time.time()}
    v7.handle_batch("cust", [{"type": "button_reply", "button_id": "show_anyway", "text": "Show my rate anyway"}])


def test_show_anyway_on_missing_guests_assumes_2_adults_states_it_and_skips_the_comparison(sent, monkeypatch):
    p = _split_packet()
    p.stay.occupancy = []
    p.stay.rooms = None
    opts = [{"option_id": "o1", "rooms": [{"id": "R1", "name": "Deluxe Room"}], "meal_basis": "Room Only",
             "refundable": True, "currency": "INR", "total_price": 20000.0}]
    monkeypatch.setattr("yta.web._resolve", lambda pk: {
        "detail": {"hotel_name": "H", "options": opts},
        "room_map": {"matched": True, "ratekey_option_ids": ["o1"], "rate_options": [
            {"option_id": "o1", "room_type_id": "R1", "room_name": "Deluxe Room", "currency": "INR",
             "total_price": 20000.0}]}})
    _tap_anyway(sent, p, ["stay.occupancy"])
    kind, body, buttons = next(m for m in reversed(sent) if m[0] == "buttons")
    assert "Assumed:* 1 room, 2 adults" in body
    assert "better rate" not in body.lower() and "you save" not in body.lower()   # not like-for-like
    assert "Pocket Stays price" in body and "confirm_book" in [b for b, _ in buttons]
    assert p.stay.occupancy_source == "assumed_default"


def test_show_anyway_on_a_missing_price_shows_our_rate_without_a_comparison(sent, monkeypatch):
    p = _RecPacket(missing=["ota_benchmark.final_payable"])
    opts = [{"option_id": "o1", "rooms": [{"id": "R1", "name": "Deluxe Room"}], "meal_basis": "Room Only",
             "refundable": True, "currency": "INR", "total_price": 20000.0}]
    monkeypatch.setattr("yta.web._resolve", lambda pk: {
        "detail": {"hotel_name": "H", "options": opts},
        "room_map": {"matched": True, "ratekey_option_ids": ["o1"], "rate_options": [
            {"option_id": "o1", "room_type_id": "R1", "room_name": "Deluxe Room", "currency": "INR",
             "total_price": 20000.0}]}})
    p.ota_benchmark.final_payable = None
    _tap_anyway(sent, p, ["ota_benchmark.final_payable"])
    assert getattr(p, "_skip_price", False) is True
    assert any("Pocket Stays price" in m[1] for m in sent if m[0] == "buttons")


def test_show_anyway_on_a_missing_room_switches_to_listing_all_rooms(sent, monkeypatch):
    seen = {}
    monkeypatch.setattr(v7, "_continue_with_packet", lambda frm, pk, url, intent: seen.update(intent=intent))
    _tap_anyway(sent, _Packet(missing=["requested_offer.room_name"]), ["requested_offer.room_name"], intent="deal")
    assert seen["intent"] == "search"


def test_a_stale_show_anyway_tap_after_a_restart_gets_the_expired_reply(sent):
    v7.handle_batch("cust", [{"type": "button_reply", "button_id": "show_anyway", "text": "x"}])
    assert "expired" in sent[-1][1].lower()


def test_a_year_less_date_typed_in_a_follow_up_gets_year_buttons_not_a_text_question(sent, monkeypatch):
    # Live 2026-10-07: "22 dec se 24 dec" got a text-only "which year?" with no
    # buttons -- the year flow only ran on the first extraction.
    monkeypatch.setattr("yta.extract_llm.extract_clarification_ex",
                        lambda *a, **kw: ({"stay.check_in": "12-22", "stay.check_out": "12-24"}, None, True))
    _open_awaiting(missing=("stay.check_in", "stay.check_out"))
    v7.handle_batch("cust", [{"type": "text", "text": "22 dec se 24 dec"}])
    kind, body, buttons = sent[-1]
    assert body.startswith("📅 *Which year is 22 Dec – 24 Dec?*")
    assert [b for b, _ in buttons][:2] == ["year_0", "year_1"]
    assert v7._WA_SESSIONS["cust"]["state"] == "awaiting_year"


def test_month_day_dates_are_split_out_only_when_both_are_year_less():
    f, ym = v7._pull_month_day_dates({"stay.check_in": "12-22", "stay.check_out": "12-24", "x": 1})
    assert ym == {"check_in": (12, 22), "check_out": (12, 24)} and f == {"x": 1}
    f, ym = v7._pull_month_day_dates({"stay.check_in": "2027-12-22", "stay.check_out": "2027-12-24"})
    assert ym == {} and "stay.check_in" in f
    f, ym = v7._pull_month_day_dates({"stay.check_in": "12-22"})
    assert ym == {}



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


def test_show_anyway_is_not_offered_for_guests_when_the_guest_already_said_several_rooms(sent):
    # "1 room, 2 adults" would contradict a guest who just said "2 rooms".
    p = _Packet()
    p.stay.rooms = 2
    v7._send_ask("cust", p, ["stay.occupancy"], None)
    assert "show_anyway" not in [b for b, _ in sent[-1][2]]
    assert "assume" not in sent[-1][1]


def test_the_assumption_line_only_promises_what_the_bot_can_actually_do():
    p = _Packet()
    p._assumptions = ["1 room, 2 adults"]
    line = v7._assumption_line(p)
    assert "start over" in line.lower() and "tell me if" not in line.lower()


def test_everything_missing_is_asked_in_one_message_question_first_without_a_recap(sent):
    # Owner: ask it all at once (not one by one), and never copy the stay info
    # back in a follow-up. Live 2026-10-07 the model's own wall of text buried
    # the question at the end under a recap.
    long_q = ("Could you please let me know your check-in and check-out dates, how many rooms "
              "you need, and the number of adults and children (with their ages) for each room?")
    v7._send_ask("cust", _Packet(hotel_name="Taj Santacruz"),
                 ["stay.check_in", "stay.check_out", "stay.rooms", "stay.occupancy"], None, long_q)
    body = sent[-1][1]
    assert body.startswith("📅 *What are your check-in and check-out dates?*")
    assert "👥 *How many rooms, and how many guests in each?*" in body      # asked in the SAME message
    assert body.index("📅") < body.index("👥")
    assert "Could you please" not in body and "Taj Santacruz" not in body and "Price shown" not in body


def test_a_follow_up_never_repeats_the_stay_details(sent):
    p = _Packet(hotel_name="Taj Santacruz")
    p.ota_benchmark.final_payable = 64015.0
    v7._send_ask("cust", p, ["stay.occupancy", "requested_offer.room_name"], None)
    body = sent[-1][1]
    assert "64,015" not in body and "Taj Santacruz" not in body and "So far" not in body


def test_a_short_llm_follow_up_for_the_single_open_question_is_still_used(sent):
    v7._send_ask("cust", _Packet(), ["stay.occupancy"], None, "How many children, and how old are they?")
    assert sent[-1][1].startswith("❓ *How many children, and how old are they?*")


def test_an_ambiguous_room_list_never_exceeds_the_ten_rows_whatsapp_can_show(sent):
    # Found by the e2e QA run: nearest match + the 5 cheapest rooms x 2 = 12 rows,
    # and send_list silently dropped the last two while the session still held 12.
    def grp(i):
        return {"room_type_id": f"R{i}", "room_name": f"Room {i}",
                "options": [{"option_id": f"o{i}a", "total_price": 100 + i, "currency": "INR",
                             "meal_basis": "Room Only", "refundable": True, "room_type_id": f"R{i}"},
                            {"option_id": f"o{i}b", "total_price": 200 + i, "currency": "INR",
                             "meal_basis": "Breakfast", "refundable": True, "room_type_id": f"R{i}"}]}
    res = {"detail": {"hotel_name": "H", "options": []},
           "room_options": {"groups": [grp(i) for i in range(6)], "ambiguous_match": True,
                            "nearest_match_room_type_id": "R0"}}
    v7._present_option_choices("cust", _Packet(), res)
    kind, body, button_text, sections = next(m for m in sent if m[0] == "list")
    assert sum(len(rows) for _, rows in sections) == 10
    assert len(v7._WA_SESSIONS["cust"]["options"]) == 10          # session 1:1 with what is shown
    assert sections[0][0] == "Room 0"                               # cheapest first (here also the nearest)


def _grp(i, price, rid=None):
    rid = rid or f"R{i}"
    return {"room_type_id": rid, "room_name": f"Room {i}",
            "options": [{"option_id": f"o{i}a", "total_price": price, "currency": "INR",
                         "meal_basis": "Room Only", "refundable": True, "room_type_id": rid},
                        {"option_id": f"o{i}b", "total_price": price + 3000, "currency": "INR",
                         "meal_basis": "Breakfast", "refundable": True, "room_type_id": rid}]}


def test_the_hotel_level_cheapest_room_is_first_even_when_the_nearest_match_is_pricier(sent):
    # Owner: "overall hotel-level cheapest sabse uper rahega -- explore rooms, jahan bhi
    # room list dikha rahe ho". The nearest match used to be forced to the top.
    groups = [_grp(0, 50000), _grp(1, 20000), _grp(2, 30000)]              # nearest = R0 (priciest)
    res = {"detail": {"hotel_name": "H", "options": []},
           "room_options": {"groups": groups, "ambiguous_match": True, "nearest_match_room_type_id": "R0"}}
    v7._present_option_choices("cust", _Packet(), res)
    kind, body, button_text, sections = next(m for m in sent if m[0] == "list")
    assert [t for t, _ in sections] == ["Room 1", "Room 2", "Room 0"]       # cheapest -> priciest
    assert "closest to what you mentioned: *Room 0*" in body                # the nearest match is still called out
    assert v7._WA_SESSIONS["cust"]["options"][0]["total_price"] == 20000


def test_the_nearest_match_stays_in_the_list_even_when_it_would_fall_off_the_ten_rows(sent):
    groups = [_grp(i, 10000 + i * 1000) for i in range(6)] + [_grp(9, 99000)]   # nearest R9 is the priciest
    res = {"detail": {"hotel_name": "H", "options": []},
           "room_options": {"groups": groups, "ambiguous_match": True, "nearest_match_room_type_id": "R9"}}
    v7._present_option_choices("cust", _Packet(), res)
    kind, body, button_text, sections = next(m for m in sent if m[0] == "list")
    titles = [t for t, _ in sections]
    assert "Room 9" in titles and titles[0] == "Room 0"                      # kept, cheapest still first
    assert sum(len(r) for _, r in sections) == 10 and "Room 5" not in titles  # the priciest OTHER room made way


def test_every_room_list_is_sorted_cheapest_first(sent):
    res = {"detail": {"hotel_name": "H", "options": []},
           "room_options": {"groups": [_grp(0, 40000), _grp(1, 15000), _grp(2, 25000)], "no_match": True}}
    v7._present_option_choices("cust", _Packet(), res)
    kind, body, button_text, sections = next(m for m in sent if m[0] == "list")
    firsts = [rows[0][1] for _, rows in sections]
    assert firsts == ["INR 15,000", "INR 25,000", "INR 40,000"]
    assert "starting from INR 15,000" in body


def test_a_wrong_reply_to_the_room_list_gets_a_nudge_with_something_to_tap(sent):
    # Found by the e2e QA run: the first nudge was a bare text line -- a dead end.
    _open_choosing_option([_rate("o1", "Deluxe Room", "Room Only", True, 20000.0)])
    v7.handle_batch("cust", [{"type": "text", "text": "hmm"}])
    kind, body, buttons = sent[-1]
    assert kind == "buttons" and "reply with a number from 1 to 1" in body
    assert [b for b, _ in buttons] == ["show_list_again", "start_new_chat"]


# -- live 2026-10-08: check_in "12-17" reached TripJack (HTTP 400) -> no rate ------------

def test_one_year_less_date_takes_the_year_of_the_stated_one():
    # "17 Dec to 18 Dec 2026": the model returned check_in "12-17", check_out "2026-12-18".
    f, ym = v7._pull_month_day_dates({"stay.check_in": "12-17", "stay.check_out": "2026-12-18"})
    assert ym == {} and f == {"stay.check_in": "2026-12-17", "stay.check_out": "2026-12-18"}
    f, ym = v7._pull_month_day_dates({"stay.check_in": "2026-12-17", "stay.check_out": "12-18"})
    assert f == {"stay.check_in": "2026-12-17", "stay.check_out": "2026-12-18"}


def test_a_stay_over_new_year_gets_the_right_year_on_each_end():
    f, _ = v7._pull_month_day_dates({"stay.check_in": "12-30", "stay.check_out": "2027-01-02"})
    assert f["stay.check_in"] == "2026-12-30"                 # not 2027, that would be after check-out
    f, _ = v7._pull_month_day_dates({"stay.check_in": "2026-12-30", "stay.check_out": "01-02"})
    assert f["stay.check_out"] == "2027-01-02"


def test_a_malformed_date_is_dropped_and_never_reaches_the_packet():
    f, ym = v7._pull_month_day_dates({"stay.check_in": "tomorrow", "stay.check_out": "2026-12-18", "x": 1})
    assert "stay.check_in" not in f and f["stay.check_out"] == "2026-12-18" and f["x"] == 1
    f, _ = v7._pull_month_day_dates({"stay.check_in": "02-30", "stay.check_out": "2026-12-18"})
    assert "stay.check_in" not in f                            # Feb 30 is not a date


def test_mixed_dates_typed_in_a_reply_go_on_to_the_search_with_real_dates(sent, monkeypatch):
    seen = {}
    monkeypatch.setattr("yta.extract_llm.extract_clarification_ex",
                        lambda *a, **kw: ({"stay.check_in": "12-17", "stay.check_out": "2026-12-18"}, None, True))
    monkeypatch.setattr("yta.web._resolve",
                        lambda pk: seen.update(ci=pk.stay.check_in, co=pk.stay.check_out) or {"room_map": {"matched": False}})
    p = _RecPacket(missing=["stay.check_in", "stay.check_out"])
    p.stay.check_in = p.stay.check_out = None            # a packet that really stores what is added
    v7._WA_SESSIONS["cust"] = {"state": "awaiting_field", "packet": p,
                               "missing": ["stay.check_in", "stay.check_out"], "intent": None,
                               "unproductive_attempts": 0, "last_activity": time.time()}
    v7.handle_batch("cust", [{"type": "text", "text": "17 dec to 18 dec 2026"}])
    assert seen == {"ci": "2026-12-17", "co": "2026-12-18"}


def test_the_search_is_never_called_with_a_malformed_date(sent, monkeypatch):
    called = []
    monkeypatch.setattr("yta.web._resolve", lambda pk: called.append(1) or {})
    p = _RecPacket()
    p.stay.check_in, p.stay.check_out = "12-17", "2026-12-18"
    v7._present_deal("cust", p)
    assert called == []
    assert v7._WA_SESSIONS["cust"]["state"] == "awaiting_field"
    assert sent[-1][1].startswith("📅 *What are your check-in and check-out dates?*")
