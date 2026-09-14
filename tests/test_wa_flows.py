"""Routing tests for the pluggable WhatsApp flows (yta/wa_flows/*).

No network, no real LLM — whatsapp.find_url/download_media,
pipeline.extract and extract_llm.extract_clarification are all faked.
These specifically exercise what's NEW in v1 over v0: cancel handling,
session expiry, unsupported content, and an implicit hotel switch
mid-clarification — the gaps v0 doesn't cover at all.
"""
import time

import pytest

from yta import wa_flows
from yta.wa_flows import v0, v1


@pytest.fixture(autouse=True)
def _clean_sessions():
    v0._WA_SESSIONS.clear()
    v1._WA_SESSIONS.clear()
    yield
    v0._WA_SESSIONS.clear()
    v1._WA_SESSIONS.clear()


@pytest.fixture
def sent(monkeypatch):
    messages = []
    monkeypatch.setattr(v1, "wa_send", lambda frm, text: messages.append(text))
    monkeypatch.setattr(v1, "ask_for_missing",
                         lambda frm, missing, clarify=None: messages.append(clarify or f"Missing: {missing}"))
    monkeypatch.setattr(v1, "finish_and_reply", lambda frm, packet: messages.append("FINISHED"))
    return messages


def _open_session(missing=("ota_benchmark.final_payable",), age_sec=0):
    class _Packet:
        def __init__(self):
            self._still_missing = list(missing)

        def check_mandatory(self):
            return self._still_missing

        def derive_stay(self):
            pass

        def add(self, path, val, *a, **kw):
            if path in self._still_missing:
                self._still_missing.remove(path)

    v1._WA_SESSIONS["cust"] = {
        "packet": _Packet(), "missing": list(missing),
        "last_activity": time.time() - age_sec,
    }


# -- registry ----------------------------------------------------------

def test_registry_dispatches_to_the_configured_flow(monkeypatch):
    calls = []
    monkeypatch.setattr(wa_flows, "FLOWS", {"fake": lambda frm, items: calls.append((frm, items))})
    monkeypatch.setenv("YTA_WA_FLOW", "fake")
    wa_flows.handle_batch("cust", [{"type": "text", "text": "hi"}])
    assert calls == [("cust", [{"type": "text", "text": "hi"}])]


def test_registry_falls_back_to_default_on_unknown_flow(monkeypatch):
    monkeypatch.setenv("YTA_WA_FLOW", "does-not-exist")
    assert wa_flows.active_flow_name() == "does-not-exist"
    # handle_batch itself must not raise -- falls back to v0's real handler
    monkeypatch.setattr(v0, "handle_batch", lambda frm, items: None)
    wa_flows.handle_batch("cust", [])  # no exception


# -- v1: cancel ----------------------------------------------------------

def test_v1_cancel_mid_clarification_resets_session(sent):
    _open_session()
    v1.handle_batch("cust", [{"type": "text", "text": "wait, wrong hotel, cancel this"}])
    assert "cust" not in v1._WA_SESSIONS
    assert any("send me the hotel" in m.lower() for m in sent)


def test_v1_cancel_with_no_open_session_is_a_no_op_reply(sent):
    v1.handle_batch("cust", [{"type": "text", "text": "cancel"}])
    assert "cust" not in v1._WA_SESSIONS
    assert any("nothing to cancel" in m.lower() for m in sent)


# -- v1: session expiry ---------------------------------------------------

def test_v1_stale_session_is_dropped_and_bare_reply_is_not_absorbed(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    _open_session(age_sec=v1._SESSION_TIMEOUT_SEC + 30)
    v1.handle_batch("cust", [{"type": "text", "text": "31683"}])
    assert "cust" not in v1._WA_SESSIONS
    # a bare number with no hotel context left should ask for the link/photo,
    # NOT be treated as answering the (now-expired) question
    assert any("send me a hotel" in m.lower() for m in sent)


def test_v1_fresh_session_reply_is_still_honored(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.extract_llm.extract_clarification",
                         lambda missing, text, media=None: ({"ota_benchmark.final_payable": 31683}, None))
    _open_session(age_sec=10)   # well within the timeout
    v1.handle_batch("cust", [{"type": "text", "text": "31683"}])
    assert sent == ["FINISHED"]


# -- v1: unsupported content ----------------------------------------------

def test_v1_unsupported_content_keeps_session_open(sent):
    _open_session()
    v1.handle_batch("cust", [{"type": "audio", "media_id": "m1"}])
    assert "cust" in v1._WA_SESSIONS   # NOT dropped -- unlike v0, which silently stalls here
    assert any("only read text" in m.lower() for m in sent)


def test_v1_unsupported_content_with_no_session(sent):
    v1.handle_batch("cust", [{"type": "sticker"}])
    assert any("only read text" in m.lower() for m in sent)


# -- v1: implicit hotel switch --------------------------------------------

def test_v1_new_link_mid_clarification_drops_old_session_and_restarts(sent, monkeypatch):
    _open_session()
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: "https://booking.com/x")

    class _NewPacket:
        hotel = type("H", (), {"name": "New Hotel"})()
        status = "ok"
        missing_mandatory = []

        def check_mandatory(self):
            return []

    monkeypatch.setattr("yta.pipeline.extract", lambda *a, **kw: _NewPacket())
    monkeypatch.setattr(v1, "extracted_lines", lambda packet: ["New Hotel"])

    v1.handle_batch("cust", [{"type": "text", "text": "https://booking.com/x"}])
    assert sent[-1] == "FINISHED"
    assert "cust" not in v1._WA_SESSIONS


# -- v1: off-topic / unclear reply keeps the specific field named ---------

def test_v1_unrelated_reply_names_the_specific_missing_field(sent, monkeypatch):
    monkeypatch.setattr("yta.whatsapp.find_url", lambda text: None)
    monkeypatch.setattr("yta.extract_llm.extract_clarification",
                         lambda missing, text, media=None: ({}, None))
    _open_session(missing=("ota_benchmark.final_payable",))
    v1.handle_batch("cust", [{"type": "text", "text": "is breakfast included?"}])
    assert "cust" in v1._WA_SESSIONS   # kept open, not dropped
    assert any("cancel" in m.lower() for m in sent)
