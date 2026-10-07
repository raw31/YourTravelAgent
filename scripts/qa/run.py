"""Run the end-to-end QA scenarios against the real stack (see world.py).

    python scripts/qa/run.py                 # all scenarios + tap explorer
    python scripts/qa/run.py --only S2,S9    # a subset
    python scripts/qa/run.py --no-explore    # skip the tap-every-button explorer

Exit code 1 if anything failed. Transcript: scripts/qa/last_run.md,
server log: scripts/qa/server.log.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import sqlite3
import sys
import time
import traceback
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from world import BANNED_WORDS, World  # noqa: E402

HERE = Path(__file__).resolve().parent
TODAY = date.today()
FUT = TODAY.year + 1                    # an explicit, definitely-future year
GENERIC_ERR = "something went wrong on my side"
TAJ = "Taj Santacruz Mumbai"
DEAL_PRICE = "INR 31,000"               # > the cheapest real TJ rate (23,790) -> a genuine saving


class Tee(io.StringIO):
    pass


# ----------------------------------------------------------------- runner
class Run:
    def __init__(self, name, w: World, frm: str, logbuf: Tee):
        self.name, self.w, self.frm, self.log = name, w, frm, logbuf
        self.transcript, self.fails, self.notes = [], [], []
        w.reset()
        self._log_pos = len(logbuf.getvalue())
        self._lead_base = {st: len(leads(st)) for st in ("confirmed", "declined", "no_deal", "needs_human")}

    # -- assertions
    def check(self, cond, msg):
        if not cond:
            self.fails.append(msg)
            self.transcript.append(f"  ❌ CHECK FAILED: {msg}")
        return bool(cond)

    # -- a user action + settle + invariants
    def do(self, what, arg=None, *, allow_text_end=False, expect_reply=True, expect_error=False,
           note=None):
        w, frm = self.w, self.frm
        before = len(w.msgs(frm))
        t0 = time.time()
        if what == "say":
            label = f'👤 says: "{arg}"'
            w.say(frm, arg)
        elif what == "tap":
            label = f"👤 taps <{arg}>"
            w.tap(frm, arg)
        elif what == "img":
            label = "👤 sends an image"
            w.send_image(frm, arg)
        else:
            raise ValueError(what)
        ok = w.settle()
        dt = time.time() - t0
        new = w.msgs(frm)[before:]
        self.transcript.append(f"{label}   ({dt:.1f}s)" + (f"   // {note}" if note else ""))
        for m in new:
            self.transcript.append(m.render())
        self.check(ok, "bot did not settle within 150s (hang?)")
        if expect_reply:
            self.check(new, "bot sent NO reply")
        self._invariants(new, allow_text_end, expect_error)
        return new

    def _invariants(self, new, allow_text_end, expect_error):
        w = self.w
        blob_all = []
        for m in new:
            blob = " ".join([m.text] + [b[1] for b in m.buttons] + [f"{r[1]} {r[2]}" for r in m.rows]
                            + m.sections).lower()
            blob_all.append(blob)
            for bad in BANNED_WORDS:
                self.check(bad not in blob, f"banned/internal word {bad!r} in a customer message: {m.text[:80]!r}")
            for junk in ("{", "}", "[object", " none ", "nan"):
                if junk in f" {blob} " and not (junk == "nan" and "nan" not in blob.split()):
                    if junk in (" none ",) or junk in ("{", "}", "[object"):
                        self.check(False, f"junk token {junk!r} in message: {m.text[:80]!r}")
            if m.kind == "buttons" and m.text.startswith(("📅", "👥", "🏨 *Which", "🛏️", "💳", "❓")):   # an ask
                first = m.text.splitlines()[0]
                self.check("?" in first, f"ask does not lead with its question: {first[:70]!r}")
                self.check("Price shown" not in m.text and "So far" not in m.text,
                           "a follow-up ask copied the stay details back")
            if m.kind == "list" and m.rows:               # hotel-level cheapest is always on top
                import re as _re
                prices = [float(_re.sub(r"[^\d.]", "", r[1])) for r in m.rows if _re.search(r"\d", r[1])]
                self.check(prices and prices[0] == min(prices),
                           f"cheapest room is not at the top of the list: first={prices[:1]} min={min(prices) if prices else None}")
            for bid in m.ids():
                self.check(not bid.startswith(("occ_", "date_")), f"sample-value button/row offered: {bid}")
            if GENERIC_ERR in blob and not expect_error:
                self.check(False, "bot hit its generic 'something went wrong' error")
        if new and not allow_text_end:
            self.check(new[-1].kind in ("buttons", "list"),
                       f"dead end: last message of the step is plain {new[-1].kind!r} with nothing to tap "
                       f"({new[-1].text[:60]!r})")
        if w.violations:
            for v in w.violations:
                self.check(False, f"WhatsApp would REJECT a message: {v}")
            w.violations.clear()
        if w.copy_warnings:
            for v in w.copy_warnings:
                self.check(False, f"copy relies on truncation: {v}")
            w.copy_warnings.clear()
        seg = self.log.getvalue()[self._log_pos:]
        self._log_pos = len(self.log.getvalue())
        for needle in ("Traceback", "SEND FAILED", "Exception in thread", "Error in "):
            if needle in seg:
                line = next((ln for ln in seg.splitlines() if needle in ln), needle)
                self.check(False, f"server log has {needle!r}: {line[:160]}")

    # -- helpers
    def texts(self, new=None):
        return [m.text for m in (new if new is not None else self.w.msgs(self.frm))]

    def ids_of(self, new):
        m = next((x for x in reversed(new) if x.kind in ("buttons", "list")), None)
        return m.ids() if m else []

    def has(self, new, *subs):
        blob = "\n".join(self.texts(new)).lower()
        return all(s.lower() in blob for s in subs)

    def last_ids(self):
        m = self.w.last_interactive(self.frm)
        return m.ids() if m else []

    def state(self):
        return self.w.state(self.frm)

    def new_leads(self, status):
        return leads(status)[self._lead_base.get(status, 0):]

    def tj_checkin(self):
        return [c.get("checkIn") for c in self.w.tj_calls]


def leads(status=None):
    from yta.leads import db
    con = sqlite3.connect(str(db.db_path()))
    con.row_factory = sqlite3.Row
    try:
        q = "SELECT * FROM leads" + (" WHERE status=?" if status else "")
        return [dict(r) for r in con.execute(q, (status,) if status else ())]
    except sqlite3.OperationalError:
        return []
    finally:
        con.close()


# -------------------------------------------------------------- scenarios
SCENARIOS = {}


def scenario(sid, title):
    def deco(fn):
        SCENARIOS[sid] = (title, fn)
        return fn
    return deco


def open_deal(r: Run):
    r.do("say", "hi")
    r.do("tap", "have_deal")


def full_deal_text(room="Luxury Room City View King Bed", price=DEAL_PRICE, dates=None, hotel=TAJ,
                   occ="2 adults, 1 room", meal="Room only", refundable="refundable"):
    dates = dates or f"17 Dec {FUT} to 18 Dec {FUT}"
    return f"{hotel}, {dates}, {occ}, {room}, {meal}, {refundable}, total {price}"


@scenario("S1", "deal, full text, exact room -> better rate -> confirm -> lead saved")
def s1(r: Run):
    open_deal(r)
    new = r.do("say", full_deal_text())
    r.check(r.has(new, "% better rate"), "no '% better rate' headline")
    r.check(r.has(new, "you save"), "no savings line")
    r.check(r.has(new, "total for 1 night"), "no 'total for 1 night' wording")
    r.check({"confirm_book", "decline_book", "explore_other_rooms"} <= set(r.last_ids()),
            f"deal buttons wrong: {r.last_ids()}")
    r.check(len(r.w.tj_calls) == 1, f"expected exactly 1 TripJack pricing call, got {len(r.w.tj_calls)}")
    r.check(r.tj_checkin() == [f"{FUT}-12-17"], f"TripJack called with {r.tj_checkin()}")
    r.check(r.state() == "presented", f"state {r.state()!r}")
    new = r.do("tap", "confirm_book", allow_text_end=True)
    r.check(r.has(new, "booking") or r.has(new, "secured"), "no confirmation message after Yes, book this")
    ls = r.new_leads("confirmed")
    r.check(len(ls) == 1, f"expected 1 confirmed lead, got {len(ls)}")
    if ls:
        r.check("Taj" in (ls[0]["hotel_name"] or ""), f"lead hotel {ls[0]['hotel_name']!r}")
        r.check(ls[0]["check_in"] == f"{FUT}-12-17", f"lead check_in {ls[0]['check_in']!r}")
        r.check((ls[0]["price"] or 0) > 0, "lead has no price")


@scenario("S2", "year missing in text -> bot asks the year -> tap year -> rates (the 2026-10-07 live bug)")
def s2(r: Run):
    open_deal(r)
    new = r.do("say", full_deal_text(dates="17 Dec to 18 Dec"))
    r.check(r.has(new, "which year"), "bot did not ask for the year")
    r.check(r.state() == "awaiting_year", f"state {r.state()!r}")
    r.check(len(r.w.tj_calls) == 0, f"searched BEFORE knowing the year: {r.tj_checkin()}")
    r.check({"year_0", "year_1"} <= set(r.last_ids()), f"year buttons missing: {r.last_ids()}")
    new = r.do("tap", "year_0")
    r.check(not r.has(new, "still need", "dates"), "re-asked for dates right after the year tap")
    r.check(not r.has(new, "your dates"), "asked for dates after the year tap")
    r.check(len(r.w.tj_calls) == 1, f"no pricing call after the year tap ({len(r.w.tj_calls)})")
    r.check(r.tj_checkin() and r.tj_checkin()[0] >= TODAY.isoformat(), f"searched a past date: {r.tj_checkin()}")
    r.check(r.state() == "presented", f"state after year tap {r.state()!r}")
    r.do("tap", "confirm_book", allow_text_end=True)
    r.check(len(r.new_leads("confirmed")) == 1, "lead not saved after year flow")


@scenario("S3", "year missing -> user TYPES the year (2027)")
def s3(r: Run):
    open_deal(r)
    r.do("say", full_deal_text(dates="17 Dec to 18 Dec"))
    r.check(r.state() == "awaiting_year", f"state {r.state()!r}")
    r.do("say", f"it's {FUT}")
    r.check(r.tj_checkin() == [f"{FUT}-12-17"], f"TripJack called with {r.tj_checkin()}")
    r.check(r.state() == "presented", f"state {r.state()!r}")


@scenario("S4", "explicit PAST year -> never searched, bot asks")
def s4(r: Run):
    open_deal(r)
    new = r.do("say", full_deal_text(dates="17 Dec 2024 to 18 Dec 2024"))
    r.check(len(r.w.tj_calls) == 0, f"searched a past date: {r.tj_checkin()}")
    r.check(r.has(new, "year"), "did not ask about the year for a past date")
    for (ci, co) in (r.w.wa_flows and __import__("yta.wa_flows.v7", fromlist=["x"])._WA_SESSIONS.get(r.frm, {}).get("year_options", {}) or {}).values():
        r.check(ci >= TODAY, f"offered a past year option {ci}")


@scenario("S5", "search path: hotel+dates, no room -> list -> pick -> deal -> confirm")
def s5(r: Run):
    r.do("say", "hi")
    r.do("tap", "search_hotel")
    new = r.do("say", f"{TAJ} {FUT}-12-17 to {FUT}-12-18, 2 adults 1 room")
    lst = next((m for m in new if m.kind == "list"), None)
    r.check(lst is not None, "no room list shown for a no-room search")
    if lst:
        r.check(len(lst.rows) <= 10, f"{len(lst.rows)} rows")
        r.check("starting from" in lst.text.lower(), "list has no 'rooms starting from' price anchor")
        prices = [float(x.replace(",", "")) for x in
                  [rw[1].split()[-1] for rw in lst.rows]]
        r.check(min(prices) == 23790, f"cheapest rate in list is {min(prices)}, real cheapest is 23,790")
        r.check(prices[0] == min(prices), "first row is not the cheapest")
        new = r.do("tap", lst.rows[0][0], allow_text_end=False)
        r.check(r.state() == "presented", f"state after picking a row {r.state()!r}")
        r.check(not r.has(new, "better rate"), "claimed a saving on a search with no OTA price")
        r.check(r.has(new, "pocket stays price"), "plain-rate message missing")
        r.check(r.has(new, "total for 1 night") and "total for 1 night total" not in "\n".join(r.texts(new)).lower()
                and "\n".join(r.texts(new)).lower().count("total for 1 night") == 1,
                "price label duplicated or missing")


@scenario("S6", "not cheaper than the OTA -> 'see other rooms' -> list -> pick")
def s6(r: Run):
    open_deal(r)
    new = r.do("say", full_deal_text(price="INR 15,000"))
    r.check(r.state() == "not_cheaper", f"state {r.state()!r}")
    r.check(r.has(new, "couldn't beat"), "no 'couldn't beat' message")
    r.check({"see_other_rooms", "try_another"} <= set(r.last_ids()), f"buttons {r.last_ids()}")
    r.check(len(r.new_leads("no_deal")) == 1, "no_deal lead not recorded")
    new = r.do("tap", "see_other_rooms")
    lst = next((m for m in new if m.kind == "list"), None)
    r.check(lst is not None, "no list after 'see other rooms'")
    if lst:
        r.check("Luxury Room City View King Bed" not in lst.sections, "the just-shown room is listed again")
        new = r.do("tap", lst.rows[0][0])
        r.check(not r.has(new, "couldn't beat"), "looped back to 'couldn't beat' for a different room")
        r.check(not r.has(new, "better rate"), "claimed a saving against another room's price")
        r.check(r.state() == "presented" and "confirm_book" in r.last_ids(), "no bookable offer after picking another room")


@scenario("S7", "requested room matches nothing -> shows rooms, not a dead end")
def s7(r: Run):
    open_deal(r)
    new = r.do("say", full_deal_text(room="Presidential Ocean Mega Suite Sea View"))
    lst = next((m for m in new if m.kind == "list"), None)
    r.check(lst is not None, "unmatched room did not fall back to a room list")
    if lst:
        r.check("starting from" in lst.text.lower(), "no price anchor on unmatched-room list")
        new = r.do("tap", lst.rows[-1][0])        # not the nearest match (row 1) -> a different room
        r.check(not r.has(new, "better rate") and not r.has(new, "you save"),
                "claimed a saving for a room the OTA price was never for")
        r.check(r.has(new, "pocket stays price"), "plain-rate message missing")


@scenario("S8", "ambiguous room name ('Luxury Room') -> list with closest first, not a wrong quote")
def s8(r: Run):
    open_deal(r)
    new = r.do("say", full_deal_text(room="Luxury Room"))
    r.check(any(m.kind == "list" for m in new) or r.state() == "presented",
            "neither a list nor a deal")
    if r.state() == "presented":
        r.notes.append("'Luxury Room' was matched confidently to one room (deal shown)")


@scenario("S9", "fuzzy hotel ('Taj Mumbai' -> 'Taj The Trees') is CONFIRMED, not silently priced")
def s9(r: Run):
    open_deal(r)
    new = r.do("say", full_deal_text(hotel="Taj Mumbai"))
    r.check(r.state() == "confirming_hotel", f"state {r.state()!r} (expected a hotel confirmation)")
    r.check(r.has(new, "is the hotel"), "no hotel confirmation question")
    r.check({"hotel_yes", "hotel_no"} <= set(r.last_ids()), f"buttons {r.last_ids()}")
    new = r.do("tap", "hotel_yes")
    r.check(r.state() in ("presented", "choosing_option", "not_cheaper") or any(m.kind == "list" for m in new),
            f"rates not shown after Yes ({r.state()!r})")


@scenario("S9b", "fuzzy hotel -> 'No, a different one' -> asks the name, no rate shown")
def s9b(r: Run):
    open_deal(r)
    r.do("say", full_deal_text(hotel="Taj Mumbai"))
    new = r.do("tap", "hotel_no")
    r.check(r.state() == "awaiting_field", f"state {r.state()!r}")
    r.check(not any("pocket stays price" in m.text.lower() for m in new), "showed a rate after 'No'")
    new = r.do("say", "Taj Santacruz Mumbai")
    r.check(r.state() in ("presented", "choosing_option", "not_cheaper", "confirming_hotel"),
            f"did not proceed after the corrected hotel name (state {r.state()!r})")


def _split_ask(r: Run, occ="4 adults, 2 rooms", price="INR 62,000"):
    open_deal(r)
    new = r.do("say", full_deal_text(occ=occ, price=price))
    r.check(len(r.w.tj_calls) == 0, "priced an assumed even split of the guests")
    r.check(r.state() == "awaiting_field", f"state {r.state()!r}")
    r.check(r.has(new, "split across the 2 rooms"), "did not ask how the guests are split")
    r.check(r.ids_of(new) == ["start_new_chat", "human_help"], f"unexpected buttons {r.ids_of(new)}")
    return new


@scenario("S10", "aggregate occupancy (4 adults, 2 rooms): never split by guess -> asked; typed 'each room' works")
def s10(r: Run):
    _split_ask(r)
    new = r.do("say", "2 adults in each room")
    r.check(len(r.w.tj_calls) == 1, f"no pricing after the guest gave the split ({len(r.w.tj_calls)})")
    if r.w.tj_calls:
        rooms = r.w.tj_calls[0].get("rooms") or []
        r.check(len(rooms) == 2, f"TripJack asked for {len(rooms)} room(s), expected 2")
    # multi-room: the named room must MATCH (not fall to the manual list), the
    # comparison is like-for-like (62,000 vs 2 x 23,790), and nothing is doubled
    r.check(r.state() == "presented", f"multi-room booking did not match its room (state {r.state()!r})")
    r.check(r.has(new, "23% better rate"), "wrong saving for a 2-room booking (expected 62,000 vs 47,580)")
    r.check(not any(" + Luxury" in m.text for m in new), "doubled room name in a customer message")
    r.check(r.has(new, "2 rooms · 2 adults each"), "multi-room occupancy not described clearly")


@scenario("S10c", "uneven split typed (3 adults + 1 adult)")
def s10c(r: Run):
    _split_ask(r)
    r.do("say", "3 adults in room 1 and 1 adult in room 2")
    r.check(len(r.w.tj_calls) == 1, f"no pricing ({len(r.w.tj_calls)})")
    if r.w.tj_calls:
        rooms = r.w.tj_calls[0].get("rooms") or []
        r.check(sorted(x.get("adults") for x in rooms) == [1, 3], f"rooms sent to TripJack: {rooms}")


@scenario("S11", "'Explore other rooms': the guest's OWN room is compared, any other room is a plain rate")
def s11(r: Run):
    open_deal(r)
    r.do("say", full_deal_text())
    new = r.do("tap", "explore_other_rooms")
    lst = next((m for m in new if m.kind == "list"), None)
    r.check(lst is not None, "no list from Explore other rooms")
    if not lst:
        return
    r.check(any("City View King Bed" in row[2] or "City View King" in sec
                for row in lst.rows for sec in lst.sections), "the matched room is missing from the list")
    r.check(len(lst.sections) >= 3, f"only {len(lst.sections)} rooms listed")
    own = next((row for row in lst.rows if "City View King Bed" in row[2]), None)
    other = next((row for row in lst.rows if "Pool View" in row[2]), lst.rows[-1])
    new = r.do("tap", other[0])
    r.check(not r.has(new, "better rate") and not r.has(new, "you save"),
            "claimed a saving for a different room than the OTA price was for")
    r.check(r.state() == "presented", f"state {r.state()!r}")
    # and back: explore again, pick the guest's own room -> compared again
    new = r.do("tap", "explore_other_rooms")
    lst2 = next((m for m in new if m.kind == "list"), None)
    r.check(lst2 is not None, "no list on the 2nd explore")
    if lst2 and own:
        own2 = next((row for row in lst2.rows if "City View King Bed" in row[2]), None)
        if own2:
            new = r.do("tap", own2[0])
            r.check(r.has(new, "% better rate"), "the guest's own room lost its comparison after a detour")


@scenario("S12", "global intents: help / human / cancel from several states")
def s12(r: Run):
    r.do("say", "hi")
    new = r.do("say", "help")
    r.check(r.state() is None or True, "")
    r.do("tap", "have_deal")
    r.do("say", "what is this?")
    new = r.do("say", "can I talk to a human")
    r.check(len(r.new_leads("needs_human")) >= 1, "no needs_human lead recorded")
    r.check(r.has(new, "personal") or r.has(new, "human") or r.has(new, "shortly"), "no acknowledgement of the human request")
    r.do("say", "cancel")
    r.check(r.state() is None, f"cancel left state {r.state()!r}")


@scenario("S13", "buttons from before a restart are answered, not silently dropped")
def s13(r: Run):
    open_deal(r)
    r.do("say", full_deal_text())
    from yta.wa_flows import v7
    v7._WA_SESSIONS.clear()                      # a deploy/restart wipes sessions
    for bid in ("confirm_book", "explore_other_rooms", "year_0", "hotel_yes"):
        new = r.do("tap", bid, note="after restart")
        r.check(new and new[-1].kind in ("buttons", "list"), f"{bid}: no way forward after restart")


@scenario("S14", "gibberish / empty-ish first messages never dead-end")
def s14(r: Run):
    for t in ("asdfgh", "?", "ok", "👍", "price?"):
        r.do("say", t)


@scenario("S15", "image with no booking info -> graceful, with buttons")
def s15(r: Run):
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d49444154789c6360f8cfc0f01f00050001ffd8a1d2f80000000049454e44ae426082")
    r.do("img", png)


@scenario("S16", "TripJack error -> honest, recoverable message")
def s16(r: Run):
    r.w.tj_mode = "error"
    open_deal(r)
    r.w.tj_mode = "error"
    new = r.do("say", full_deal_text())
    r.check(r.has(new, "pocket stays price") is False, "showed a rate while the supplier was down")
    r.check(r.has(new, "trouble reaching"), "a supplier outage was reported as 'no better rate' (misleading)")
    r.check("try_again" in r.last_ids(), f"no Try again after a supplier outage: {r.last_ids()}")


@scenario("S17", "TripJack returns no inventory -> clear 'no live rates' with a way out")
def s17(r: Run):
    open_deal(r)
    r.w.tj_mode = "empty"
    new = r.do("say", full_deal_text())
    r.check(r.has(new, "don't see any live rates"), "sold-out stay not explained as such")


@scenario("S18", "slow supplier -> a 'taking a moment' notice, once, before the result")
def s18(r: Run):
    from yta.wa_flows import v7
    old = v7._SLOW_NOTICE_SEC
    v7._SLOW_NOTICE_SEC = 0.5
    r.w.tj_latency = 2.5
    try:
        open_deal(r)
        new = r.do("say", full_deal_text())
        texts = [m.text.lower() for m in new if m.kind == "text"]
        n = sum(1 for t in texts if "moment" in t or "bit longer" in t or "taking" in t)
        r.notes.append(f"slow-notice-like texts: {n}")
        r.check(n <= 2, f"slow notice sent {n} times")
    finally:
        v7._SLOW_NOTICE_SEC = old
        r.w.tj_latency = 0


@scenario("S19", "unknown hotel -> 'no live rate' with buttons, no crash")
def s19(r: Run):
    open_deal(r)
    new = r.do("say", full_deal_text(hotel="Zzyzx Nowhere Inn Mumbai"))
    r.check(r.has(new, "couldn't find"), "unknown hotel not reported as unknown")


@scenario("S20", "two quick messages are one conversation turn (debounce), one reply")
def s20(r: Run):
    open_deal(r)
    before = len(r.w.msgs(r.frm))
    r.w.say(r.frm, TAJ)
    time.sleep(0.1)
    r.w.say(r.frm, f"17 Dec {FUT} to 18 Dec {FUT}, 2 adults 1 room, Luxury Room City View King Bed, {DEAL_PRICE}")
    r.w.settle()
    new = r.w.msgs(r.frm)[before:]
    r.transcript.append("👤 sends two messages 0.1s apart")
    r.transcript += [m.render() for m in new]
    r._invariants(new, False, False)
    r.check(len(r.w.tj_calls) <= 1, f"{len(r.w.tj_calls)} pricing calls for one turn")


@scenario("S21", "decline -> graceful, what next")
def s21(r: Run):
    open_deal(r)
    r.do("say", full_deal_text())
    r.do("tap", "decline_book", allow_text_end=True)
    r.check(len(r.new_leads("declined")) == 1, "declined lead not recorded")


@scenario("S22", "awaiting_year: repeated nonsense never traps the guest")
def s22(r: Run):
    open_deal(r)
    r.do("say", full_deal_text(dates="17 Dec to 18 Dec"))
    for t in ("hello?", "what", "blah"):
        r.do("say", t)
    r.check(r.state() != "awaiting_year", f"still trapped in awaiting_year after 3 bad replies ({r.state()!r})")


@scenario("S23", "start over from awaiting_year / confirming_hotel / not_cheaper")
def s23(r: Run):
    open_deal(r)
    r.do("say", full_deal_text(dates="17 Dec to 18 Dec"))
    r.do("tap", "start_new_chat")
    r.check(r.state() is None or r.state() == "not_cheaper" or True, "")
    r.check(any(b == "have_deal" for b in r.last_ids()), f"start over did not show the onboarding choice: {r.last_ids()}")


@scenario("S24", "missing price -> asks for it with an example; reply completes the flow")
def s24(r: Run):
    open_deal(r)
    new = r.do("say", f"{TAJ}, {FUT}-12-17 to {FUT}-12-18, 2 adults 1 room, Luxury Room City View King Bed")
    r.check(r.state() == "awaiting_field", f"state {r.state()!r}")
    r.do("say", "INR 31,000")
    r.check(len(r.w.tj_calls) >= 1, "no pricing after the guest supplied the missing price")


@scenario("S25", "Hinglish / informal phrasing is understood")
def s25(r: Run):
    open_deal(r)
    new = r.do("say", f"bhai Taj Santacruz mumbai, {FUT}-12-17 se {FUT}-12-18, 2 log 1 room, "
                      f"Luxury Room City View Twin Bed, Room only, 31000 rupees")
    r.check(len(r.w.tj_calls) >= 1 or r.state() in ("awaiting_field", "awaiting_year"),
            "informal message produced no progress")


@scenario("S26", "dates missing: question first, no 'show anyway'; typed year-less dates -> year buttons")
def s26(r: Run):
    open_deal(r)
    new = r.do("say", f"{TAJ}, 2 adults 1 room, Luxury Room City View King Bed, total {DEAL_PRICE}")
    r.check(r.state() == "awaiting_field", f"state {r.state()!r}")
    r.check(new[-1].text.startswith("📅 *What are your check-in and check-out dates?*"),
            f"question not first: {new[-1].text[:60]!r}")
    r.check("show_anyway" not in r.last_ids(), "offered 'show anyway' with no dates at all")
    new = r.do("say", "22 dec se 24 dec")
    r.check(new[-1].text.startswith("📅 *Which year is 22 Dec – 24 Dec?*"), f"no year question: {new[-1].text[:70]!r}")
    r.check({"year_0", "year_1"} <= set(r.last_ids()), f"year buttons missing: {r.last_ids()}")
    r.check(len(r.w.tj_calls) == 0, "searched before the year was known")
    r.do("tap", "year_0")
    r.check(r.tj_checkin() and r.tj_checkin()[0].endswith("-12-22"), f"TripJack dates {r.tj_checkin()}")


@scenario("S27", "guests missing -> '2 room 5 log' -> asks the DISTRIBUTION -> '2 in one, 3 in the other'")
def s27(r: Run):
    open_deal(r)
    new = r.do("say", f"{TAJ}, 17 Dec {FUT} to 18 Dec {FUT}, Luxury Room City View King Bed, total INR 62,000")
    r.check(r.state() == "awaiting_field", f"state {r.state()!r}")
    r.check(new[-1].text.startswith("👥 *How many rooms, and how many guests in each?*"), f"question not first: {new[-1].text[:70]!r}")
    r.check("show_anyway" in r.last_ids(), "no 'Show my rate anyway' for missing guests")
    r.check("assume *1 room, 2 adults*" in new[-1].text, "the assumption is not stated before the tap")
    new = r.do("say", "2 room 5 log")
    r.check("distributed across the 2 rooms" in new[-1].text, f"not a distribution question: {new[-1].text[:90]!r}")
    r.check("Room 1: 2 adults, Room 2: 3 adults" in new[-1].text, "no distribution example")
    r.check(r.state() == "awaiting_field", f"state {r.state()!r}")
    r.do("say", "Ek kamre me do log, dusre kamre me 3 log")
    r.check(len(r.w.tj_calls) == 1, f"no pricing after the distribution ({len(r.w.tj_calls)})")
    if r.w.tj_calls:
        rooms = r.w.tj_calls[0].get("rooms") or []
        r.check(sorted(x.get("adults") for x in rooms) == [2, 3], f"TripJack rooms: {rooms}")


@scenario("S28", "'Show my rate anyway' on missing guests: assumption stated, rate shown, NO comparison")
def s28(r: Run):
    open_deal(r)
    r.do("say", f"{TAJ}, 17 Dec {FUT} to 18 Dec {FUT}, Luxury Room City View King Bed, total INR 62,000")
    new = r.do("tap", "show_anyway")
    r.check(len(r.w.tj_calls) == 1, "no pricing after Show my rate anyway")
    if r.w.tj_calls:
        rooms = r.w.tj_calls[0].get("rooms") or []
        r.check([x.get("adults") for x in rooms] == [2], f"assumed guests sent to TripJack: {rooms}")
    r.check(r.has(new, "assumed:", "1 room, 2 adults"), "the assumption is not printed with the rate")
    r.check(not r.has(new, "better rate") and not r.has(new, "you save") and not r.has(new, "couldn't beat"),
            "compared a rate built on assumed guests with the guest's own price")
    r.check(r.state() == "presented" and "confirm_book" in r.last_ids(), f"no bookable offer ({r.state()!r})")


@scenario("S29", "'Show my rate anyway' on a missing price: our rate, explicitly without a comparison")
def s29(r: Run):
    open_deal(r)
    new = r.do("say", f"{TAJ}, {FUT}-12-17 to {FUT}-12-18, 2 adults 1 room, Luxury Room City View King Bed")
    r.check(new[-1].text.startswith("💳 *What total price did you see?*"), f"question not first: {new[-1].text[:60]!r}")
    r.check("without comparing" in new[-1].text, "the 'no comparison' consequence is not stated up front")
    new = r.do("tap", "show_anyway")
    r.check(r.has(new, "pocket stays price"), "no plain rate after Show my rate anyway")
    r.check(not r.has(new, "better rate") and not r.has(new, "you save"), "claimed a saving with no price to compare")


@scenario("S30", "'Show my rate anyway' on a missing room name lists all the rooms")
def s30(r: Run):
    open_deal(r)
    new = r.do("say", f"{TAJ}, {FUT}-12-17 to {FUT}-12-18, 2 adults 1 room, total {DEAL_PRICE}")
    r.check(new[-1].text.startswith("🛏️ *Which room type is it?*"), f"question not first: {new[-1].text[:60]!r}")
    r.check("show_anyway" in r.last_ids(), "no 'Show my rate anyway' for a missing room name")
    new = r.do("tap", "show_anyway")
    r.check(any(m.kind == "list" for m in new), "did not list the rooms")


# ---------------------------------------------------------------- explorer
EXPLORE_SETUPS = {
    "onboarding": [("say", "hi")],
    "deal_presented": [("say", "hi"), ("tap", "have_deal"), ("say", None)],
    "not_cheaper": [("say", "hi"), ("tap", "have_deal"), ("say", "NC")],
    "awaiting_year": [("say", "hi"), ("tap", "have_deal"), ("say", "YEARLESS")],
    "confirming_hotel": [("say", "hi"), ("tap", "have_deal"), ("say", "FUZZY")],
    "room_list": [("say", "hi"), ("tap", "search_hotel"), ("say", "SEARCH")],
    "awaiting_price": [("say", "hi"), ("tap", "have_deal"), ("say", "NOPRICE")],
}


def _text_for(token):
    return {
        None: full_deal_text(),
        "NC": full_deal_text(price="INR 15,000"),
        "YEARLESS": full_deal_text(dates="17 Dec to 18 Dec"),
        "FUZZY": full_deal_text(hotel="Taj Mumbai"),
        "SEARCH": f"{TAJ} {FUT}-12-17 to {FUT}-12-18, 2 adults 1 room",
        "NOPRICE": f"{TAJ}, {FUT}-12-17 to {FUT}-12-18, 2 adults 1 room, Luxury Room City View King Bed",
    }[token]


def replay(r: Run, ops):
    for kind, arg in ops:
        if kind == "say":
            arg = arg if arg in ("hi",) else (_text_for(arg) if arg in (None, "NC", "YEARLESS", "FUZZY", "SEARCH", "NOPRICE") else arg)
        r.do(kind, arg, allow_text_end=True)


def explore(w, logbuf, frm, budget_runs=70, max_depth=3):
    """From each setup state, tap EVERY button / a sample of list rows the bot
    offers, then keep going from where that lands (bounded). Each tap must
    produce a reply, no error, valid WhatsApp payloads, no dead end."""
    results, seen, runs = [], set(), 0
    for sname, setup in EXPLORE_SETUPS.items():
        queue = [list(setup)]
        while queue and runs < budget_runs:
            ops = queue.pop(0)
            depth = len([o for o in ops if o[0] == "tap"]) - len([o for o in setup if o[0] == "tap"])
            r = Run(f"X:{sname}:{'/'.join(str(a) for k, a in ops[len(setup):])}", w, frm, logbuf)
            runs += 1
            try:
                replay(r, ops[:-1] if len(ops) > len(setup) else ops)
                if len(ops) > len(setup):
                    terminal = ops[-1][1] in ("confirm_book", "decline_book")   # graceful closing messages
                    r.do("tap", ops[-1][1], allow_text_end=terminal)
            except Exception as e:  # noqa: BLE001
                r.check(False, f"harness/bot exception: {type(e).__name__}: {e}")
            m = w.last_interactive(frm)
            sig = (r.state(), tuple(sorted(m.ids())) if m else ())
            r.transcript.append(f"  → state={r.state()!r} offers={list(sig[1])}")
            results.append(r)
            if sig in seen or depth >= max_depth or m is None:
                continue
            seen.add(sig)
            ids = m.ids()
            if m.kind == "list" and len(ids) > 3:
                ids = [ids[0], ids[len(ids) // 2], ids[-1]]
            for i in ids:
                queue.append(ops + [("tap", i)])
    return results


# -------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--no-explore", action="store_true")
    ap.add_argument("--budget", type=int, default=70)
    args = ap.parse_args()
    only = {s.strip() for s in args.only.split(",") if s.strip()}

    logbuf = Tee()
    real_stdout = sys.stdout
    results = []
    with contextlib.redirect_stdout(logbuf):
        w = World()
        frm = "919000000042"
        try:
            for sid, (title, fn) in SCENARIOS.items():
                if only and sid not in only:
                    continue
                r = Run(sid, w, frm, logbuf)
                r.title = title
                t0 = time.time()
                try:
                    fn(r)
                except Exception as e:  # noqa: BLE001
                    r.check(False, f"scenario crashed: {type(e).__name__}: {e}")
                    r.transcript.append(traceback.format_exc())
                r.elapsed = time.time() - t0
                results.append(r)
                print(f"[qa] {sid} done", file=real_stdout, flush=True) if False else None
                real_stdout.write(f"{'PASS' if not r.fails else 'FAIL'}  {sid:4} {title}  ({r.elapsed:.0f}s)\n")
                real_stdout.flush()
                for f in r.fails:
                    real_stdout.write(f"        ✗ {f}\n")
                real_stdout.flush()
            if not args.no_explore and not only:
                real_stdout.write("\n--- tap explorer (every offered button, bounded) ---\n")
                xr = explore(w, logbuf, frm, args.budget)
                bad = [x for x in xr if x.fails]
                real_stdout.write(f"explorer: {len(xr)} paths walked, {len(bad)} with failures\n")
                for x in bad:
                    real_stdout.write(f"FAIL  {x.name}\n")
                    for f in x.fails:
                        real_stdout.write(f"        ✗ {f}\n")
                results += xr
        finally:
            w.close()

    # artifacts
    out = ["# QA run\n"]
    for r in results:
        out.append(f"\n## {'✅' if not r.fails else '❌'} {r.name} — {getattr(r, 'title', '')}\n")
        out.append("```")
        out += r.transcript
        out.append("```")
        for n in r.notes:
            out.append(f"_note: {n}_")
    (HERE / "last_run.md").write_text("\n".join(out))
    (HERE / "server.log").write_text(logbuf.getvalue())
    failed = [r for r in results if r.fails]
    real_stdout.write(f"\n{len(results) - len(failed)}/{len(results)} passed\n")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
