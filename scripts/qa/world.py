"""End-to-end QA world for the WhatsApp bot.

Runs the REAL stack, only the outside world is faked:

  real   webhook HTTP handler (yta.web.Handler) -> debounce -> wa_flows.v7
         real extraction (Groq/Gemini LLM), real hotel DB resolve
         (data/hotels.db), real room mapping, real lead recording (temp DB)
  fake   Meta Graph API (outbound messages + media) -- every payload is
         validated against WhatsApp's real limits, a violation is answered
         with a 400 exactly as Meta would, and recorded
  fake   TripJack pricing -- replays a REAL captured response (Taj
         Santacruz, 161 options with the genuine noisy room names) and
         rejects past dates / bad bodies like the real API does

NOTHING reaches a real phone number: WHATSAPP_ACCESS_TOKEN is overridden
before any import and every graph.facebook.com call is intercepted.
"""
from __future__ import annotations

import contextlib
import copy
import http.server
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
FIXTURE = Path(__file__).with_name("taj_santacruz_pricing.json")

# --- neutralise anything that could reach a real number, BEFORE importing yta
os.environ["WHATSAPP_ACCESS_TOKEN"] = "qa-fake-token"
os.environ["WHATSAPP_PHONE_NUMBER_ID"] = "000000000000000"
os.environ["WHATSAPP_VERIFY_TOKEN"] = "qa"
os.environ["YTA_WA_FLOW"] = "v7"
_TMP = tempfile.mkdtemp(prefix="yta_qa_")
os.environ["YTA_LEADS_DB"] = str(Path(_TMP) / "leads.db")
os.environ["YTA_LLM_PARK_FILE"] = str(Path(_TMP) / "llm_parking.json")   # QA never touches real parking state

# .env is loaded by yta.llm; it must not overwrite our fakes
_ENV_FILE = ROOT / ".env"
if _ENV_FILE.exists():
    for line in _ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            if not k.startswith("WHATSAPP_"):
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

import requests  # noqa: E402

BANNED_WORDS = ["bookmystay", "book my stay", "pocket travels", "pockettravels", "tripjack",
                "yourtravelagent", "traceback", "nan inr", "none inr", "inr none", "undefined"]


# ---------------------------------------------------------------- messages
@dataclass
class Msg:
    to: str
    kind: str                       # text | image | buttons | list
    text: str = ""
    buttons: list = field(default_factory=list)   # [(id, title)]
    rows: list = field(default_factory=list)      # [(id, title, desc)]
    sections: list = field(default_factory=list)  # [section titles]
    list_button: str = ""
    t: float = 0.0

    def ids(self):
        return [b[0] for b in self.buttons] + [r[0] for r in self.rows]

    def render(self) -> str:
        out = [f"  🤖 [{self.kind}] {self.text}".rstrip()]
        for b in self.buttons:
            out.append(f"       ( {b[1]} )   <{b[0]}>")
        if self.kind == "list":
            out.append(f"       ≡ list button: {self.list_button!r}")
            out.append(f"         sections: {self.sections}")
            for r in self.rows:
                out.append(f"         - {r[1]} | {r[2]}   <{r[0]}>")
        return "\n".join(out)


class Violation(Exception):
    pass


def _check_graph_payload(p: dict) -> list:
    """WhatsApp Cloud API limits. Returns a list of violation strings."""
    v = []
    t = p.get("type")
    if t == "text":
        b = (p.get("text") or {}).get("body", "")
        if not b.strip():
            v.append("text: empty body")
        if len(b) > 4096:
            v.append(f"text: body {len(b)} > 4096")
    elif t == "image":
        if not (p.get("image") or {}).get("link"):
            v.append("image: no link")
    elif t == "interactive":
        i = p.get("interactive") or {}
        body = (i.get("body") or {}).get("text", "")
        if not body.strip():
            v.append("interactive: empty body")
        if len(body) > 1024:
            v.append(f"interactive: body {len(body)} > 1024")
        if i.get("type") == "button":
            bs = (i.get("action") or {}).get("buttons") or []
            if not 1 <= len(bs) <= 3:
                v.append(f"buttons: {len(bs)} buttons (need 1-3)")
            ids = [b["reply"]["id"] for b in bs]
            if len(set(ids)) != len(ids):
                v.append("buttons: duplicate ids")
            for b in bs:
                ti = b["reply"]["title"]
                if not ti.strip():
                    v.append("buttons: empty title")
                if len(ti) > 20:
                    v.append(f"buttons: title {ti!r} {len(ti)} > 20")
                if len(b["reply"]["id"]) > 256:
                    v.append("buttons: id too long")
        elif i.get("type") == "list":
            a = i.get("action") or {}
            if len((a.get("button") or "")) > 20 or not a.get("button"):
                v.append(f"list: button text {a.get('button')!r} invalid")
            secs = a.get("sections") or []
            if not 1 <= len(secs) <= 10:
                v.append(f"list: {len(secs)} sections")
            total, ids = 0, []
            for s in secs:
                if len(secs) > 1 and not s.get("title"):
                    v.append("list: section without title")
                if len(s.get("title") or "") > 24:
                    v.append(f"list: section title {s.get('title')!r} > 24")
                for r in s.get("rows") or []:
                    total += 1
                    ids.append(r["id"])
                    if not r["title"].strip():
                        v.append("list: empty row title")
                    if len(r["title"]) > 24:
                        v.append(f"list: row title {r['title']!r} > 24")
                    if len(r.get("description") or "") > 72:
                        v.append("list: row description > 72")
            if total > 10:
                v.append(f"list: {total} rows > 10")
            if total == 0:
                v.append("list: no rows")
            if len(set(ids)) != len(ids):
                v.append("list: duplicate row ids")
    else:
        v.append(f"unknown message type {t}")
    return v


# ------------------------------------------------------------------- world
class World:
    def __init__(self, *, batch_window=0.4, tj_latency=0.0):
        self.outbox: dict[str, list[Msg]] = {}
        self.violations: list[str] = []
        self.copy_warnings: list[str] = []      # titles over limit BEFORE the safety-net truncation
        self.tj_calls: list[dict] = []
        self.media: dict[str, tuple] = {}
        self.log = io.StringIO()
        self._lock = threading.Lock()
        self._inflight = 0
        self.tj_latency = tj_latency
        self.tj_mode = "ok"                     # ok | error | empty
        self.tj_price_scale = 1.0
        self._today = date.today()
        self._fixture = json.loads(FIXTURE.read_text())
        self._seq = 0
        self._install(batch_window)

    # -- install patches -----------------------------------------------
    def _install(self, batch_window):
        self._real_post, self._real_get = requests.post, requests.get
        world = self

        class _Resp:
            def __init__(self, status, data):
                self.status_code, self._d = status, data
                self.text = json.dumps(data)
                self.headers = {}

            def json(self):
                return self._d

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise requests.HTTPError(f"{self.status_code}")

            content = b""

        def fake_post(url, *a, **kw):
            if "graph.facebook.com" in url:
                return world._graph(url, kw.get("json") or {})
            if "tripjack.com" in url:
                return _Resp(*world._tripjack(url, kw.get("json") or {}))
            return world._real_post(url, *a, **kw)

        def fake_get(url, *a, **kw):
            if "graph.facebook.com" in url:
                m = re.search(r"/(\w+)$", url)
                mid = m.group(1) if m else ""
                if mid in world.media:
                    return _Resp(200, {"url": f"https://lookaside.fbsbx.com/{mid}",
                                       "mime_type": world.media[mid][1]})
                return _Resp(404, {"error": {"message": "unknown media"}})
            if "lookaside.fbsbx.com" in url:
                mid = url.rsplit("/", 1)[-1]
                r = _Resp(200, {})
                r.content = world.media[mid][0]
                return r
            if "tripjack.com" in url:
                return _Resp(200, {})
            return world._real_get(url, *a, **kw)

        requests.post, requests.get = fake_post, fake_get
        self._Resp = _Resp

        from yta import web, whatsapp, wa_flows
        self.web, self.whatsapp, self.wa_flows = web, whatsapp, wa_flows
        web._WA_BATCH_WINDOW_SEC = batch_window

        # record titles BEFORE the send layer's truncation safety net, so a
        # copy change that silently relies on truncation is still caught
        orig_buttons, orig_list = whatsapp.send_buttons, whatsapp.send_list

        def spy_buttons(to, body, buttons):
            for bid, title in buttons[:3]:
                if len(title or "") > 20:
                    world.copy_warnings.append(f"button title over 20 chars (truncated): {title!r}")
            if len(buttons) > 3:
                world.copy_warnings.append(f"{len(buttons)} buttons passed, only 3 sent: "
                                           f"{[b[0] for b in buttons]}")
            return orig_buttons(to, body, buttons)

        def spy_list(to, body, button_text, sections):
            n = sum(len(r) for _, r in sections)
            if n > 10:
                world.copy_warnings.append(f"list had {n} rows, only 10 sent")
            if len(button_text or "") > 20:
                world.copy_warnings.append(f"list button over 20: {button_text!r}")
            for title, rows in sections:
                if len(title or "") > 24:
                    world.copy_warnings.append(f"list section title over 24 (truncated): {title!r}")
            return orig_list(to, body, button_text, sections)

        whatsapp.send_buttons, whatsapp.send_list = spy_buttons, spy_list

        # track in-flight batches for quiescence
        orig_hb = wa_flows.handle_batch

        def hb(frm, items):
            with world._lock:
                world._inflight += 1
            try:
                return orig_hb(frm, items)
            finally:
                with world._lock:
                    world._inflight -= 1
        wa_flows.handle_batch = hb
        self._orig_hb = orig_hb

        # real HTTP server with the real webhook handler
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        requests.post, requests.get = self._real_post, self._real_get
        self.wa_flows.handle_batch = self._orig_hb
        self.httpd.shutdown()

    # -- fake Meta Graph -------------------------------------------------
    def _graph(self, url, payload):
        to = payload.get("to", "?")
        viol = _check_graph_payload(payload)
        if viol:
            for x in viol:
                self.violations.append(f"to={to}: {x}")
            return self._Resp(400, {"error": {"message": "; ".join(viol), "code": 100}})
        t = payload.get("type")
        m = Msg(to=to, kind="text", t=time.time())
        if t == "text":
            m.text = payload["text"]["body"]
        elif t == "image":
            m.kind, m.text = "image", payload["image"]["link"]
        else:
            i = payload["interactive"]
            m.text = i["body"]["text"]
            if i["type"] == "button":
                m.kind = "buttons"
                m.buttons = [(b["reply"]["id"], b["reply"]["title"]) for b in i["action"]["buttons"]]
            else:
                m.kind = "list"
                m.list_button = i["action"]["button"]
                for s in i["action"]["sections"]:
                    m.sections.append(s["title"])
                    for r in s["rows"]:
                        m.rows.append((r["id"], r["title"], r.get("description", "")))
        with self._lock:
            self.outbox.setdefault(to, []).append(m)
        self._seq += 1
        return self._Resp(200, {"messages": [{"id": f"wamid.QA{self._seq}"}]})

    # -- fake TripJack -----------------------------------------------------
    def _tripjack(self, url, body):
        self.tj_calls.append(body)
        if self.tj_latency:
            time.sleep(self.tj_latency)
        if "/pricing" not in url:
            return 200, {"status": {"success": True, "httpStatus": 200}}
        if self.tj_mode == "error":
            return 200, {"status": {"success": False, "httpStatus": 500},
                         "errors": [{"errCode": "9999", "message": "supplier unavailable"}]}
        # emulate the real API's validation (a past date was a real 400 in prod)
        try:
            ci = date.fromisoformat(body.get("checkIn") or "")
        except ValueError:
            ci = None
        if ci is None:
            return 400, {"status": {"success": False, "httpStatus": 400},
                         "errors": [{"errCode": "2001", "message": "invalid or missing check-in date"}]}
        if ci < self._today:
            return 400, {"status": {"success": False, "httpStatus": 400},
                         "errors": [{"errCode": "2001", "message": "check-in date is in the past"}]}
        if not body.get("rooms"):
            return 400, {"status": {"success": False, "httpStatus": 400},
                         "errors": [{"errCode": "2002", "message": "roomInfo required"}]}
        d = copy.deepcopy(self._fixture)
        n_rooms = len(body.get("rooms") or [])
        if n_rooms > 1:
            # TripJack returns a combo per option for multi-room searches: roomInfo
            # repeated once per room, price covering ALL rooms.
            for o in d["options"]:
                o["roomInfo"] = [dict(r) for r in o["roomInfo"]] * n_rooms
                o["pricing"]["totalPrice"] = round(o["pricing"]["totalPrice"] * n_rooms, 2)
        if self.tj_mode == "empty":
            d["options"] = []
        if self.tj_price_scale != 1.0:
            for o in d["options"]:
                o["pricing"]["totalPrice"] = round(o["pricing"]["totalPrice"] * self.tj_price_scale, 2)
        return 200, d

    # -- inbound -------------------------------------------------------------
    def _post(self, frm, message):
        payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
            "messaging_product": "whatsapp", "contacts": [{"wa_id": frm}], "messages": [message]}}]}]}
        r = self._real_post(f"http://127.0.0.1:{self.port}/webhook/whatsapp", json=payload, timeout=10)
        assert r.status_code == 200

    def _mid(self):
        self._seq += 1
        return f"wamid.IN{self._seq}"

    def say(self, frm, text):
        self._post(frm, {"from": frm, "id": self._mid(), "type": "text", "text": {"body": text}})

    def tap(self, frm, bid, title="x"):
        """Tap a button OR a list row, whichever the bot last offered with that id."""
        last = self.last_interactive(frm)
        is_row = last is not None and any(r[0] == bid for r in last.rows)
        if is_row:
            ti = next(r[1] for r in last.rows if r[0] == bid)
            inter = {"type": "list_reply", "list_reply": {"id": bid, "title": ti}}
        else:
            ti = next((b[1] for b in (last.buttons if last else []) if b[0] == bid), title)
            inter = {"type": "button_reply", "button_reply": {"id": bid, "title": ti}}
        self._post(frm, {"from": frm, "id": self._mid(), "type": "interactive", "interactive": inter})

    def send_image(self, frm, data: bytes, mime="image/png", caption=None):
        mid = f"media{len(self.media) + 1}"
        self.media[mid] = (data, mime)
        img = {"id": mid, "mime_type": mime}
        if caption:
            img["caption"] = caption
        self._post(frm, {"from": frm, "id": self._mid(), "type": "image", "image": img})

    # -- waiting ---------------------------------------------------------------
    def settle(self, timeout=150, quiet=0.8):
        """Block until the debounce buffer is empty, no batch is in flight, and
        no new outbound message has appeared for `quiet` seconds."""
        end = time.time() + timeout
        last_change, last_n = time.time(), -1
        while time.time() < end:
            with self.web._WA_PENDING_LOCK:
                pending = bool(self.web._WA_PENDING)
            n = sum(len(v) for v in self.outbox.values())
            if n != last_n:
                last_n, last_change = n, time.time()
            if not pending and self._inflight == 0 and time.time() - last_change >= quiet:
                return True
            time.sleep(0.1)
        return False

    # -- views -------------------------------------------------------------------
    def msgs(self, frm):
        return list(self.outbox.get(frm, []))

    def last_interactive(self, frm):
        for m in reversed(self.outbox.get(frm, [])):
            if m.kind in ("buttons", "list"):
                return m
        return None

    def state(self, frm):
        from yta.wa_flows import v7
        return (v7._WA_SESSIONS.get(frm) or {}).get("state")

    def reset(self, frm=None):
        from yta.wa_flows import v7
        v7._WA_SESSIONS.clear()
        for n in ("_PENDING_REFERRALS", "_PENDING_PATH", "_LAST_BATCH_ITEMS"):
            getattr(v7, n, {}).clear()
        self.outbox.clear()
        self.violations.clear()
        self.copy_warnings.clear()
        self.tj_calls.clear()
        self.tj_mode, self.tj_price_scale = "ok", 1.0
