"""Phase-1 debug panel — a tiny local web UI over `yta.extract`.

    python -m yta.web            # then open http://127.0.0.1:8765

Paste an OTA booking URL, get the Booking Intent packet, the per-field
evidence trail, and validation warnings. Zero extra dependencies (stdlib
http.server). Also serves the WhatsApp "3rd flow" webhook (yta/whatsapp.py)
— so while the debug panel itself is meant for local/internal use, this
process as a whole is a real service when deployed (Railway, etc.), not
localhost-only anymore.

HOST/PORT come from the environment so the same code runs unchanged
locally (defaults: 0.0.0.0:8765) and on a PaaS like Railway, which injects
its own PORT and expects the app to bind 0.0.0.0 (not 127.0.0.1— that
only accepts connections FROM the container itself, which is why an app
that only ever bound loopback would look "up" in logs but be completely
unreachable from the platform's router).
"""
from __future__ import annotations

import base64
import json
import os
import threading
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from yta.pipeline import extract
from yta.profiles import route

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", 8765))

# in-memory job registry: job_id -> {log: [...], done: bool, result / error}
_JOBS: dict = {}
_JOBS_LOCK = threading.Lock()


def _run_job(job_id: str, req: dict) -> None:
    job = _JOBS[job_id]
    try:
        url = (req.get("url") or "").strip()
        do_render = bool(req.get("render", True))
        paste = (req.get("paste") or "").strip() or None
        uploads = req.get("files") or []
        # page_data: the Chrome extension's capture — {text, html, json_ld,
        # xhr_json, final_url} from the live authenticated tab. Same shape
        # render() produces; pipeline.extract() gives it priority. Reuses
        # this exact job runner / endpoint — no separate path for the
        # extension, the panel UI and the extension both post here.
        page_data = req.get("page_data") or None

        media = None
        if uploads:
            from yta.ingest import load_uploads
            media = load_uploads([
                {"name": u.get("name", ""), "mime": u.get("mime"),
                 "bytes": base64.b64decode(u["b64"])}
                for u in uploads if u.get("b64")
            ])

        if not media and not page_data and (not urlparse(url).scheme or not urlparse(url).netloc):
            job["error"] = "Enter an absolute http(s) URL, or attach a PDF / screenshot"
            return

        is_html = bool(paste) and paste.lstrip()[:1] == "<"
        packet = extract(
            url, render=do_render, media=media, log_sink=job["log"],
            page_html=paste if (is_html and not media and not page_data) else None,
            page_text=paste if (paste and not is_html and not media and not page_data) else None,
            page_data=page_data,
        )

        resolution = None
        if req.get("resolve", True):
            if not packet.hotel.name:
                packet.log("TripJack lookup skipped — no hotel name extracted")
            else:
                resolution = _resolve(packet)

        job["result"] = {"adapter": route(url).name if url else "generic",
                         "packet": packet.to_dict(), "resolution": resolution}
    except Exception as e:  # noqa: BLE001
        job["error"] = f"{type(e).__name__}: {e}"
        job["trace"] = traceback.format_exc()
    finally:
        job["done"] = True


# -- WhatsApp "3rd flow" --------------------------------------------------
# A customer sends a link or a screenshot on WhatsApp; the bot replies with
# the same TripJack comparison the panel/extension show. Same
# extract()+_resolve() pipeline as everywhere else — this only adds a
# webhook front door and a text-formatted reply.

def _wa_send(frm: str, text: str) -> dict:
    """Every outbound WhatsApp send goes through here — logs the actual
    result. A bare whatsapp.send_text() call can fail silently (expired
    token, rate limit, bad recipient) and the code carries on as if the
    customer got the message when they got nothing at all — this is the
    one and only place that matters, so fix it once here rather than
    re-checking the result at every call site."""
    from yta import whatsapp
    result = whatsapp.send_text(frm, text)
    if result.get("_status_code") != 200:
        print(f"[wa] SEND FAILED to {frm}: status={result.get('_status_code')} "
              f"error={result.get('error')}", flush=True)
    return result


# In-memory: phone number -> {"packet": BookingIntent, "missing": [paths]}.
# A customer is "mid-conversation" whenever they're in here — their NEXT
# message is treated as an answer to the missing-fields question, not as a
# fresh link/photo. Same pattern/lock style as _JOBS above.
_WA_SESSIONS: dict = {}
_WA_SESSIONS_LOCK = threading.Lock()


def _occ_field(r, name, default=None):
    """`stay.occupancy` holds real RoomOccupancy objects on a live packet,
    but plain dicts once something's gone through .to_dict()/JSON — accept
    either shape rather than assuming one."""
    if r is None:
        return default
    if isinstance(r, dict):
        return r.get(name, default)
    return getattr(r, name, default)


def _occ_repr(occ) -> str:
    if not occ:
        return "—"
    parts = []
    for r in occ:
        a = _occ_field(r, "adults", "?")
        c = _occ_field(r, "children", 0) or 0
        parts.append(f"{a}A" + (f"+{c}C" if c else ""))
    return ", ".join(parts)


def _extracted_lines(packet) -> list:
    """The hotel/dates/occupancy/room/price facts as extracted so far —
    shared by the "here's what I found" message and the final reply.
    Emoji + text label together — the emoji alone ("🏨 Taj Exotica...")
    takes a beat to parse as "this is the hotel"; the label makes it
    explicit while the emoji keeps each line quick to scan."""
    p = packet
    lines = [
        f"🏨 Hotel Name : {p.hotel.name or '—'}",
        f"📅 Dates : {p.stay.check_in or '?'} → {p.stay.check_out or '?'}",
        f"👥 Room Occupancy : {_occ_repr(p.stay.occupancy)}",
    ]
    if p.requested_offer.room_name:
        lines.append(f"🛏️ Room Type : {p.requested_offer.room_name}")
    if p.requested_offer.meal_plan:
        lines.append(f"🍽️ Meal Type : {p.requested_offer.meal_plan}")
    if p.requested_offer.refundable is True:
        lines.append("↩️ Refundability : Refundable")
    elif p.requested_offer.refundable is False:
        lines.append("↩️ Refundability : Non-refundable")
    if p.ota_benchmark.final_payable:
        lines.append(f"💳 Price Shown : {p.ota_benchmark.currency or ''} {p.ota_benchmark.final_payable}")
    return lines


def _ask_for_missing(frm: str, missing: list, clarify: str | None = None) -> None:
    from yta.extract_llm import FIELD_LABELS
    labels = [FIELD_LABELS[p] for p in missing if p in FIELD_LABELS]
    if not labels:
        return
    # `clarify` is a specific follow-up (e.g. "which year — you said 24
    # Sept to 25 Sept?") from extract_clarification() when a reply was
    # partial rather than absent — that's already a direct, on-point
    # question, so send it alone rather than also re-listing the field(s)
    # it's about. Otherwise, ALWAYS one field per line — comma-joining
    # into a sentence reads fine for 1-2 missing fields and turns into an
    # unreadable run-on the moment 4-5 are missing at once.
    if clarify:
        text = f"{clarify} (or send a new link/photo to start over)"
    else:
        text = ("Missing:\n" + "\n".join(labels)
                + "\n\nReply with these — any format works, or send a new link/photo to start over.")
    _wa_send(frm, text)


def _whatsapp_reply(packet, resolution: dict | None) -> str:
    # Deliberately does NOT repeat hotel/dates/occupancy/room/OTA-price —
    # the "📋 Here's what I found" message already showed all of that a
    # moment ago. Repeating it here just made the actual quote harder to
    # read. This message carries only what's NEW: the TripJack quote.
    ota_price = packet.ota_benchmark.final_payable
    ota_ccy = packet.ota_benchmark.currency

    rz = resolution or {}
    room_map = rz.get("room_map") or {}
    best = None
    if room_map.get("matched"):
        opts = room_map.get("rate_options") or []
        keyed = set(room_map.get("ratekey_option_ids") or [])
        pool = [o for o in opts if o.get("option_id") in keyed] or opts
        if pool:
            best = min(pool, key=lambda o: o.get("total_price", float("inf")))

    if not best:
        # Never surface rz['note']/rz['detail_error'] raw here — those are
        # internal wholesale-supplier diagnostics (can literally say things
        # like "TripJack DB not built") and must never reach a customer.
        # The real reason is still logged server-side for us to act on.
        name = packet.hotel.name or "this hotel"
        reason = (rz.get("note") if rz.get("available") is False
                  else rz.get("detail_error") if rz.get("detail_error")
                  else "no confident room/price match" if (rz.get("available") and rz.get("match"))
                  else "not resolved")
        print(f"[wa] no live rate for {name!r}: {reason}", flush=True)
        return f"⚠️ Couldn't get a live rate for {name} right now — I'll take a manual look and follow up."

    # Same generic markup concept as the extension (yta_markup_pct/flat in
    # its admin page) — no shared per-user config between the two flows
    # yet, so this is its own env-based knob for now.
    pct = float(os.environ.get("WHATSAPP_MARKUP_PCT", "0") or 0)
    flat = float(os.environ.get("WHATSAPP_MARKUP_FLAT", "0") or 0)
    ccy = best.get("currency", "") or ""
    sell = round(best.get("total_price", 0) * (1 + pct / 100) + flat, 2)

    comparable = bool(ota_price and ota_ccy and ota_ccy.upper() == ccy.upper())
    if comparable:
        diff = ota_price - sell
        dpct = (diff / ota_price * 100) if ota_price else 0
        cheaper = diff >= 0
        if not cheaper:
            # Never show a price that's worse than what the customer
            # already has on the OTA page — no upside in surfacing that
            # number, and it undercuts the whole pitch. Say we checked,
            # not what we found.
            return ("👍 We checked — the price you already have looks like "
                     "the best deal for this stay. Nothing better to offer "
                     "right now.")

    lines = []
    # WhatsApp renders *single asterisks* as bold. When we actually know
    # we're cheaper, lead with the win and lay out BookMyStay's own deal
    # in full — same labeled shape as the "Your deal" summary, but sourced
    # from what TripJack actually matched/returned (hotel name, room, meal
    # can each differ in wording from what the OTA page showed) — so the
    # customer sees exactly what they'd be booking, not just a condensed
    # price line.
    if comparable:
        tj_hotel_name = (rz.get("match") or {}).get("hotel_name") or packet.hotel.name or "—"
        lines.append("✅ Found you a better rate!")
        lines.append("")
        lines.append("BookMyStay's deal:")
        lines.append("----")
        lines.append(f"🏨 Hotel Name : {tj_hotel_name}")
        lines.append(f"📅 Dates : {packet.stay.check_in or '?'} → {packet.stay.check_out or '?'}")
        lines.append(f"👥 Room Occupancy : {_occ_repr(packet.stay.occupancy)}")
        lines.append(f"🛏️ Room Type : {best.get('room_name') or '—'}")
        if best.get("meal_basis"):
            lines.append(f"🍽️ Meal Type : {best['meal_basis']}")
        if best.get("refundable") is True:
            lines.append("↩️ Refundability : Refundable")
        elif best.get("refundable") is False:
            lines.append("↩️ Refundability : Non-refundable")
        if ota_price:
            lines.append(f"💳 Price Shown In Your Deal : {ota_ccy or ''} {ota_price}")
        lines.append("")
        lines.append(f"*💰 BookMyStay price: {ccy} {sell}*")
        lines.append(f"*📉 {ccy} {abs(diff):.0f} ({abs(dpct):.1f}%) cheaper than your deal*")
    else:
        lines.append(f"💰 BookMyStay price: {ccy} {sell}")
        # Meal plan / refundability come with the matched TripJack option
        # itself — different rooms/rates at the same hotel can differ on
        # both, so this is what's ACTUALLY being quoted, not assumed from
        # the OTA. Bolded specifically (not the room name) since these are
        # the two terms of the deal the customer needs to confirm.
        meta_bits = []
        if best.get("room_name") and best["room_name"] != packet.requested_offer.room_name:
            meta_bits.append(best["room_name"])
        if best.get("meal_basis"):
            meta_bits.append(f"*{best['meal_basis']}*")
        if best.get("refundable") is True:
            meta_bits.append("*refundable*")
        elif best.get("refundable") is False:
            meta_bits.append("*non-refundable*")
        if meta_bits:
            lines.append("🛏️ " + " · ".join(meta_bits))

    lines.append("\nReply to this message to book — we'll confirm and take it from there.")
    return "\n".join(lines)


def _finish_and_reply(frm: str, packet) -> None:
    # The TripJack resolve+pricing call below is the one genuinely slow
    # step left with nothing sent back in between — ack it so the wait
    # doesn't read as the bot having gone silent.
    _wa_send(frm, "Fetching the discounted rates for you.")
    resolution = _resolve(packet) if packet.hotel.name else None
    reply = _whatsapp_reply(packet, resolution)
    _wa_send(frm, reply)
    print(f"[wa] batch for {frm} complete", flush=True)


# Sending several photos "together" in WhatsApp does NOT arrive as one
# webhook event — each image is its OWN message, delivered as a separate
# POST, typically a fraction of a second to a couple seconds apart. Handle
# each the instant it lands and you get N independent (usually incomplete)
# extractions instead of one combined one. So inbound messages are
# buffered per sender for a short debounce window; new messages from the
# same sender reset the window, and everything collected gets processed
# together as ONE extraction call once it goes quiet.
_WA_PENDING: dict = {}          # from -> {"items": [msg, ...], "timer": Timer}
_WA_PENDING_LOCK = threading.Lock()
_WA_BATCH_WINDOW_SEC = 3.0


def _enqueue_whatsapp_message(msg: dict) -> None:
    frm = msg.get("from")
    if not frm:
        return
    with _WA_PENDING_LOCK:
        entry = _WA_PENDING.setdefault(frm, {"items": [], "timer": None})
        entry["items"].append(msg)
        count = len(entry["items"])
        if entry["timer"] is not None:
            entry["timer"].cancel()
        t = threading.Timer(_WA_BATCH_WINDOW_SEC, _process_batch, args=(frm,))
        t.daemon = True
        entry["timer"] = t
        t.start()
    print(f"[wa] buffered message from {frm} (batch now has {count} item(s), window reset)", flush=True)


def _process_batch(frm: str) -> None:
    with _WA_PENDING_LOCK:
        entry = _WA_PENDING.pop(frm, None)
    if not entry or not entry["items"]:
        return
    items = entry["items"]
    print(f"[wa] processing batch for {frm}: {len(items)} message(s)", flush=True)

    from yta import whatsapp
    from yta.extract_llm import extract_clarification
    from yta.schema import LLM

    with _WA_SESSIONS_LOCK:
        session = _WA_SESSIONS.get(frm)

    try:
        if session is not None:
            # Mid-conversation: these message(s) are the customer answering
            # our "I couldn't find X" question, not a new link/photo(s).
            # Accumulate across EVERY clarification turn so far, not just
            # this one — if we ask "which year?" and they reply just
            # "2026", that reply alone has no day/month; only combined
            # with "24 Sept to 25 Sept" from the earlier turn can it
            # resolve to a real date.
            new_text = " ".join((m.get("text") or "") for m in items).strip()
            accumulated = "\n".join(t for t in (session.get("clarify_text"), new_text) if t)
            print(f"[wa] treating batch as clarification for {frm}: {new_text[:160]!r} "
                  f"(accumulated: {accumulated[:200]!r})", flush=True)
            packet = session["packet"]
            fields, clarify = extract_clarification(session["missing"], accumulated)
            print(f"[wa] clarification filled: {list(fields.keys())}; note={clarify!r}", flush=True)
            for path, val in fields.items():
                if path == "requested_offer.room_detail":
                    # Virtual/computed field (see MANDATORY_FIELDS in
                    # schema.py) — NOT a real attribute on Offer, it's a
                    # check over description/bed_type/view. Writing it
                    # directly creates a phantom attribute check_mandatory()
                    # never looks at, so this field could never actually
                    # resolve via clarification — confirmed live: a
                    # customer answered "Deluxe" -> "Deluxe room" ->
                    # "Deluxe room with breakfast" and the bot kept asking
                    # for the same thing forever. Route it to the generic
                    # free-text field instead, which the check does look at.
                    path = "requested_offer.description"
                packet.add(path, val, LLM, 0.7, "whatsapp clarification")
            packet.derive_stay()
            still_missing = packet.check_mandatory()
            print(f"[wa] still missing after clarification: {still_missing}", flush=True)
            if still_missing:
                with _WA_SESSIONS_LOCK:
                    _WA_SESSIONS[frm] = {"packet": packet, "missing": still_missing,
                                          "clarify_text": accumulated}
                _ask_for_missing(frm, still_missing, clarify)
                return
            with _WA_SESSIONS_LOCK:
                _WA_SESSIONS.pop(frm, None)
            _finish_and_reply(frm, packet)
            return

        # Fresh submission: a link, one or more photos, or both — combine
        # every item in the batch into a SINGLE extract() call so the model
        # sees all of it at once (e.g. hotel name in photo 1, price in
        # photo 3), same as pasting multiple screenshots in the panel.
        url = None
        for m in items:
            url = whatsapp.find_url(m.get("text"))
            if url:
                break
        media_items = []
        for m in items:
            if m.get("type") in ("image", "document") and m.get("media_id"):
                dl = whatsapp.download_media(m["media_id"])
                if dl:
                    data, mime = dl
                    media_items.append({"name": "whatsapp-media", "mime": mime, "bytes": data})
                    print(f"[wa] downloaded media: {len(data)} bytes, {mime}", flush=True)
        media = None
        if media_items:
            from yta.ingest import load_uploads
            media = load_uploads(media_items)

        if not url and not media:
            print("[wa] no link or media found — sending the how-to-use reply", flush=True)
            _wa_send(frm, "Send me a hotel booking link or a screenshot "
                                     "of one and I'll check the best price for it.")
            return

        _wa_send(frm, "Checking your deal")   # ack before the (vision) extraction runs

        print(f"[wa] extracting: url={url!r} media_count={len(media_items)}", flush=True)
        packet = extract(url or "", render=bool(url), media=media, log_sink=[])
        print(f"[wa] extraction done: hotel={packet.hotel.name!r} status={packet.status}", flush=True)

        _wa_send(frm, "\n".join(["Your deal:", "----"] + _extracted_lines(packet)))

        missing = packet.missing_mandatory or packet.check_mandatory()
        if missing:
            with _WA_SESSIONS_LOCK:
                _WA_SESSIONS[frm] = {"packet": packet, "missing": missing}
            _ask_for_missing(frm, missing)
            return

        _finish_and_reply(frm, packet)
    except Exception as e:  # noqa: BLE001
        print(f"[wa] ERROR handling batch: {type(e).__name__}: {e}", flush=True)
        with _WA_SESSIONS_LOCK:
            _WA_SESSIONS.pop(frm, None)
        _wa_send(frm, f"Sorry, something went wrong: {type(e).__name__}: {e}")


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>YourTravelAgent — extraction debug panel</title>
<style>
  :root {
    --bg:#0f1115; --panel:#181b22; --panel2:#1f232c; --line:#2b303b;
    --fg:#e6e8ec; --muted:#9aa3b2; --accent:#5b9dff;
    --warn:#f0b34e; --bad:#ef6b6b; --mono:ui-monospace,SFMono-Regular,Menlo,monospace;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
    font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  header { padding:18px 24px; border-bottom:1px solid var(--line); }
  header h1 { margin:0; font-size:15px; font-weight:600; letter-spacing:.02em; }
  header p { margin:4px 0 0; color:var(--muted); font-size:12px; }
  main { max-width:1000px; margin:0 auto; padding:24px; }
  form { display:flex; flex-direction:column; gap:10px; }
  textarea { width:100%; min-height:90px; resize:vertical; padding:12px;
    background:var(--panel); color:var(--fg); border:1px solid var(--line);
    border-radius:8px; font:12px/1.5 var(--mono); }
  .row { display:flex; gap:14px; align-items:center; flex-wrap:wrap; }
  button { background:var(--accent); color:#04102a; border:0; border-radius:8px;
    padding:9px 18px; font-weight:600; cursor:pointer; }
  button:disabled { opacity:.5; cursor:default; }
  button.pb-btn { padding:4px 12px; font-size:12px; font-weight:600; }
  label.chk { color:var(--muted); display:flex; gap:6px; align-items:center; cursor:pointer; }
  .adapter { color:var(--muted); font-size:12px; }
  .adapter b { color:var(--accent); }
  section { margin-top:22px; background:var(--panel); border:1px solid var(--line);
    border-radius:10px; overflow:hidden; }
  section > h2 { margin:0; padding:10px 16px; font-size:12px; font-weight:600;
    text-transform:uppercase; letter-spacing:.06em; color:var(--muted);
    background:var(--panel2); border-bottom:1px solid var(--line); }
  .body { padding:14px 16px; }
  .grid { display:grid; grid-template-columns:140px 1fr; gap:6px 16px; }
  .grid div:nth-child(odd) { color:var(--muted); }
  .null { color:#5b6270; font-style:italic; }
  table { width:100%; border-collapse:collapse; font-size:12.5px; }
  th,td { text-align:left; padding:6px 10px; border-bottom:1px solid var(--line); }
  th { color:var(--muted); font-weight:500; }
  td.mono, .grid .mono { font-family:var(--mono); font-size:12px; }
  .src { font-size:11px; padding:1px 7px; border-radius:20px; border:1px solid var(--line); }
  .src-url { color:#7fd7a6; border-color:#2f5c44; }
  .src-inferred { color:#f0b34e; border-color:#5c4a2f; }
  .src-network { color:#5b9dff; border-color:#2f4a6b; }
  .src-llm { color:#c58bff; border-color:#4a2f6b; }
  .band { font-size:11px; padding:2px 9px; border-radius:20px; margin-left:8px;
    text-transform:uppercase; letter-spacing:.04em; vertical-align:middle; }
  .band-high { background:#1f4a34; color:#7fd7a6; }
  .band-medium { background:#4d3f1c; color:#f0b34e; }
  .band-low { background:#4a2f2f; color:#ef9b9b; }
  .band-none { background:#333; color:#9aa3b2; }
  .status { display:flex; align-items:center; gap:10px; padding:12px 16px;
    border-radius:10px; margin-top:18px; font-weight:600; }
  .status-ok { background:#16301f; color:#7fd7a6; border:1px solid #2f5c44; }
  .status-fail { background:#3a1d1d; color:#ef9b9b; border:1px solid #6b2f2f; }
  .status .missing { font-weight:400; color:var(--fg); font-size:12.5px; }
  .conf { font-family:var(--mono); }
  .warn-list { margin:0; padding-left:18px; }
  .warn-list li { color:var(--warn); margin:3px 0; }
  pre.raw { margin:0; padding:14px 16px; overflow:auto; font:12px/1.5 var(--mono);
    color:var(--fg); background:#12151b; max-height:420px; }
  .err { color:var(--bad); font-family:var(--mono); white-space:pre-wrap; }
  details summary { cursor:pointer; color:var(--muted); padding:10px 16px;
    background:var(--panel2); }
  .muted { color:var(--muted); }
  .rooms { margin-top:4px; font-size:12.5px; }
  .rooms > div { padding:1px 0; }
  table.log { width:100%; border-collapse:collapse; font-size:12.5px; }
  table.log td { padding:4px 12px; border-bottom:1px solid var(--line);
    vertical-align:top; }
  table.log td:first-child { white-space:pre; width:80px; text-align:right; }
  .spin { display:inline-block; color:var(--accent); animation:blink 1s steps(2) infinite; }
  @keyframes blink { 50% { opacity:0.2; } }
</style>
</head>
<body>
<header>
  <h1>YourTravelAgent — extraction debug panel</h1>
  <p>Paste an OTA booking URL (MakeMyTrip / Booking.com / Agoda / other). Phase-1 URL extraction.</p>
</header>
<main>
  <form id="f">
    <textarea id="url" placeholder="https://secure.booking.com/book.html?..." autofocus></textarea>
    <details id="pastebox">
      <summary class="muted">or paste the page's text / HTML (for expired checkout links — copy from the live tab)</summary>
      <textarea id="paste" placeholder="Select-all + copy on the open booking page, paste here. Leave URL above filled in too."></textarea>
    </details>
    <div class="row">
      <label class="chk">📎 upload PDF / screenshots of the page
        <input type="file" id="files" accept="image/*,.pdf,application/pdf" multiple></label>
      <span class="muted" id="filenames"></span>
    </div>
    <div class="row">
      <button id="go" type="submit">Extract</button>
      <label class="chk"><input type="checkbox" id="render" checked> render page + LLM parse (~20-40s)</label>
      <span class="adapter" id="adapter"></span>
    </div>
  </form>
  <div id="out"></div>
</main>
<script>
const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const val = v => (v === null || v === undefined || v === '' || (Array.isArray(v) && !v.length))
  ? '<span class="null">null</span>' : esc(Array.isArray(v) ? v.join(', ') : v);

const readB64 = f => new Promise((res, rej) => {
  const r = new FileReader();
  r.onload = () => res({name: f.name, mime: f.type, b64: String(r.result).split(',')[1]});
  r.onerror = rej;
  r.readAsDataURL(f);
});

$('#files').addEventListener('change', () => {
  const fs = [...$('#files').files];
  $('#filenames').textContent = fs.length ? fs.map(f => f.name).join(', ') : '';
});

function log_section(entries, running) {
  const spin = running ? ' <span class="spin">▍</span>' : '';
  return `<section><details${running ? ' open' : ''}><summary>Logs — ${entries.length} step(s)${spin}</summary>
    <table class="log">${entries.map(l => `<tr>
      <td class="mono muted">${String(l.ms).padStart(6)} ms</td>
      <td>${esc(l.msg)}</td></tr>`).join('')}</table></details></section>`;
}

$('#f').addEventListener('submit', async e => {
  e.preventDefault();
  const url = $('#url').value.trim();
  const paste = $('#paste').value.trim();
  const fileList = [...$('#files').files];
  if (!url && !fileList.length) { render_error('Enter a URL or attach a PDF / screenshot.'); return; }
  $('#go').disabled = true;
  $('#go').textContent = 'Working…';
  $('#adapter').textContent = ''; $('#out').innerHTML = log_section([], true);

  let jobId;
  try {
    const files = await Promise.all(fileList.map(readB64));
    const r = await fetch('/api/extract', {
      method:'POST', headers:{'content-type':'application/json'},
      body: JSON.stringify({url, render: $('#render').checked, paste, files})
    });
    const j = await r.json();
    if (!r.ok || j.error) { render_error(j.error || ('HTTP '+r.status)); $('#go').disabled=false; $('#go').textContent='Extract'; return; }
    jobId = j.job_id;
  } catch (err) { render_error(err.message); $('#go').disabled=false; $('#go').textContent='Extract'; return; }

  // poll the running job — show the log live, then the full result
  const poll = setInterval(async () => {
    let s;
    try { s = await (await fetch('/api/job?id=' + jobId)).json(); }
    catch (err) { return; }
    if (!s.done) {
      $('#out').innerHTML = log_section(s.log || [], true);
      return;
    }
    clearInterval(poll);
    $('#go').disabled = false; $('#go').textContent = 'Extract';
    if (s.error) { render_error(s.error, s.trace); return; }
    window.__jobId = jobId;
    render(s.result);
  }, 400);
});

// delegated: "Prebook" button in the room-map table → POST /api/review
document.addEventListener('click', async e => {
  const btn = e.target.closest('.pb-btn');
  if (!btn) return;
  const box = document.getElementById('prebook-out');
  btn.disabled = true;
  if (box) box.innerHTML = '<span class="spin">▍</span> calling POST /hms/v3/hotel/review …';
  try {
    const r = await fetch('/api/review', {
      method: 'POST', headers: {'content-type': 'application/json'},
      body: JSON.stringify({job_id: window.__jobId, option_id: btn.getAttribute('data-oid')})
    });
    const j = await r.json();
    if (box) box.innerHTML = render_prebook_result(j);
  } catch (err) {
    if (box) box.innerHTML = `<div class="body err">${esc(err.message)}</div>`;
  }
  btn.disabled = false;
});

function render_prebook_result(j) {
  if (!j.ok) return `<div class="status status-fail">✗ Review ${esc(j.status||'failed')} — ${esc(j.error||'')}</div>`;
  const cur = esc(j.currency || '');
  let h = `<div class="status status-ok">✓ Review ${esc(j.status)} — no Book / Hold call made</div>
    <div class="grid" style="margin-top:8px">
      <div>TJ bookingId</div><div class="mono" style="font-size:15px"><b>${val(j.tj_booking_id)}</b></div>
      <div>Selected</div><div>${esc(j.selected.room)} <span class="muted">· ${esc(j.selected.meal)} · ${j.selected.refundable ? 'refundable' : 'non-refundable'}</span></div>
      <div>Confirmed price</div><div class="mono">${cur} ${val(j.confirmed_price)}
        ${j.price_changed ? `<span style="color:#c66">(moved ${j.price_delta>=0?'+':''}${j.price_delta} vs pricing ${cur} ${j.selected.price})</span>`
          : '<span class="muted">(held — same as pricing)</span>'}</div>
      <div>Refundable</div><div>${j.refundable ? 'yes' : 'no'}${j.free_cancel_until ? ' <span class="muted">· free until '+esc(j.free_cancel_until)+'</span>' : ''}</div>
      <div>On-hold allowed</div><div>${j.onhold_allowed ? 'yes' : 'no'}${j.deadline ? ' <span class="muted">· deadline '+esc(j.deadline)+'</span>' : ''}</div>
    </div>`;
  if (j.notes && j.notes.length)
    h += `<ul class="warn-list" style="margin-top:8px">${j.notes.map(n=>`<li>${esc(n)}</li>`).join('')}</ul>`;
  if (j.booking_notes)
    h += `<details><summary>booking notes</summary><pre class="raw">${esc(j.booking_notes)}</pre></details>`;
  if (j.review_request)
    h += `<details><summary>Review request — POST ${esc(j.review_request.url)}</summary><pre class="raw">${esc(JSON.stringify(j.review_request.body, null, 2))}</pre></details>`;
  if (j.raw)
    h += `<details><summary>raw review response</summary><pre class="raw">${esc(JSON.stringify(j.raw, null, 2))}</pre></details>`;
  return h;
}

function render_error(msg, trace) {
  $('#out').innerHTML = `<section><h2>Error</h2><div class="body err">${esc(msg)}${trace ? '\\n\\n'+esc(trace) : ''}</div></section>`;
}

function render(d) {
  const p = d.packet;
  $('#adapter').innerHTML = `adapter: <b>${esc(d.adapter)}</b> &nbsp;·&nbsp; method: ${esc(p.source.extraction_method)}`;
  const s = p.stay, h = p.hotel, o = p.requested_offer, b = p.ota_benchmark;
  let occ;
  if (s.occupancy && s.occupancy.length) {
    occ = `${s.occupancy.length} room(s), ${s.adults||'?'} adult(s)`
      + (s.children ? `, ${s.children} child` : '')
      + '<div class="rooms">' + s.occupancy.map((r,i) => {
          const kids = r.children ? ` + ${r.children} child`
            + (r.child_ages && r.child_ages.length ? ` (age ${r.child_ages.join(', ')})` : '') : '';
          return `<div><span class="muted">Room ${i+1}</span> — ${r.adults||'?'} adult${kids}</div>`;
        }).join('') + '</div>';
  } else {
    occ = [s.rooms && s.rooms+' room(s)', s.adults && s.adults+' adult(s)',
      s.children ? s.children+' child' : null].filter(Boolean).join(', ');
  }

  const st = p.status === 'ok'
    ? `<div class="status status-ok">✓ OK — all mandatory fields present</div>`
    : `<div class="status status-fail">✗ FAIL
        <span class="missing">missing: ${(p.missing_mandatory||[]).map(esc).join(', ')}</span></div>`;

  let html = st + `
  <section><h2>Summary</h2><div class="body"><div class="grid">
    <div>Hotel</div><div>${val(h.name)} ${h.city ? '<span class="muted">· '+esc(h.city)+'</span>':''}</div>
    <div>Stay</div><div>${val(s.check_in)} → ${val(s.check_out)} ${s.nights ? '<span class="muted">('+s.nights+'n)</span>':''}</div>
    <div>Occupancy</div><div>${occ || '<span class="null">null</span>'}</div>
    <div>Room</div><div>${val(o.room_name)}</div>
    <div>Meal / cancel</div><div>${val(o.meal_plan)} <span class="muted">/</span> ${val(o.cancellation)}</div>
    <div>Price</div><div>${val(b.final_payable)} ${val(b.currency)}</div>
    <div>Source IDs</div><div class="mono">hotel=${val(p.source.source_hotel_id)} room=${val(p.source.source_room_id)}</div>
  </div></div></section>`;

  html += resolution_hotel(d.resolution);
  html += resolution_roommap(d.resolution);
  html += resolution_prebook(d.resolution);

  if (p.warnings.length) html += `<section><h2>Warnings — ${p.warnings.length}</h2>
    <div class="body"><ul class="warn-list">${p.warnings.map(w=>`<li>${esc(w)}</li>`).join('')}</ul></div></section>`;

  // ── everything below is collapsed by default ──
  html += resolution_live_options(d.resolution);

  html += `<section><details><summary>Evidence — ${p.evidence.length} field(s)</summary>
    <table><tr><th>Field</th><th>Value</th><th>Source</th><th>Conf.</th><th>Pointer</th></tr>
    ${p.evidence.map(e => `<tr>
      <td class="mono">${esc(e.field)}</td>
      <td>${val(e.value)}</td>
      <td><span class="src src-${esc(e.source)}">${esc(e.source)}</span></td>
      <td class="conf">${e.confidence}</td>
      <td class="mono muted">${e.pointer ? esc(e.pointer) : ''}</td></tr>`).join('')}
    </table></details></section>`;

  if (p.run_log && p.run_log.length) html += log_section(p.run_log, false);

  html += `<section><details><summary>Raw packet JSON</summary>
    <pre class="raw">${esc(JSON.stringify(p, null, 2))}</pre></details></section>`;

  $('#out').innerHTML = html;
}

// ── 2. TripJack Mapped Hotel details ──────────────────────────────
function resolution_hotel(rz) {
  if (!rz) return '';
  if (!rz.available)
    return `<section><h2>TripJack Mapped Hotel</h2><div class="body muted">${esc(rz.note || 'unavailable')}</div></section>`;

  const m = rz.match, found = !!m;
  let h = `<section><h2>TripJack Mapped Hotel details
      <span class="band ${found ? 'band-' + rz.band : 'band-none'}">${found ? esc(rz.band) : 'not found'}</span>
      <span class="muted" style="font-weight:400"> ${rz.ms} ms</span></h2><div class="body">`;

  if (found) {
    h += `<div class="grid">
      <div>tj_id</div><div class="mono" style="font-size:14px">${esc(m.tj_id)}</div>
      <div>unica_id</div><div class="mono">${val(m.unica_id)}</div>
      <div>Hotel name</div><div>${esc(m.hotel_name)}</div>
      <div>Locality</div><div>${val(m.region_name)} <span class="muted">· ${val(m.country_name)}</span> ${m.rating ? '· '+m.rating+'★' : ''}</div>
      <div>Score</div><div class="mono">${m.score}
        <span class="muted">(name ${m.name_score}${m.geo_score!=null ? ' · geo '+m.geo_score : ''}${m.city_score!=null ? ' · city '+m.city_score : ''}${m.distance_m!=null ? ' · '+m.distance_m+' m' : ''})</span></div>
    </div>`;
  } else {
    const best = (rz.candidates || [])[0];
    h += `<div class="muted">No confident match`
      + (best ? ` — best candidate scored <span class="mono">${best.score}</span> (need ≥ 0.75).` : '.') + `</div>`;
  }

  if (rz.notes && rz.notes.length)
    h += `<ul class="warn-list" style="margin-top:10px">${rz.notes.map(n=>`<li>${esc(n)}</li>`).join('')}</ul>`;

  const cand = found ? (rz.candidates || []).slice(1) : (rz.candidates || []);
  if (cand.length) {
    const tbl = `<table><tr><th>tj_id</th><th>unica_id</th><th>name</th><th>locality · country</th><th>score</th><th>name</th><th>geo</th><th>dist</th></tr>
      ${cand.map(c=>`<tr>
        <td class="mono">${esc(c.tj_id)}</td>
        <td class="mono muted">${val(c.unica_id)}</td>
        <td>${esc(c.hotel_name)}</td>
        <td class="muted">${val(c.region_name)} · ${val(c.country_name)}</td>
        <td class="mono">${c.score}</td>
        <td class="mono muted">${c.name_score}</td>
        <td class="mono muted">${c.geo_score!=null ? c.geo_score : ''}</td>
        <td class="mono muted">${c.distance_m!=null ? Math.round(c.distance_m)+'m' : ''}</td></tr>`).join('')}
      </table>`;
    h += found
      ? `<details><summary>${cand.length} other candidate(s)</summary>${tbl}</details>`
      : `<div style="margin-top:10px"><div class="muted" style="margin-bottom:4px">discarded candidates (${cand.length}):</div>${tbl}</div>`;
  }
  if (rz.detail_request) {
    const dr = rz.detail_request;
    h += `<details><summary>TripJack Detail request — POST ${esc(dr.url)}</summary>
      <pre class="raw">${esc(JSON.stringify(dr.body, null, 2))}</pre></details>`;
  } else if (rz.detail_request_error) {
    h += `<div class="muted" style="margin-top:8px">Detail request not buildable: ${esc(rz.detail_request_error)}</div>`;
  }
  h += `<details><summary>cascade trace</summary><pre class="raw">${esc((rz.layers||[]).join('\\n'))}</pre></details>`;
  return h + `</div></section>`;
}

// ── 3. Room → rate-plan mapping ──────────────────────────────────
function resolution_roommap(rz) {
  if (!rz || !rz.available) return '';
  if (!rz.room_map) {
    if (rz.detail_error)
      return `<section><h2>Room → rate-plan mapping</h2><div class="body muted">TripJack pricing call failed: ${esc(rz.detail_error)}</div></section>`;
    return '';
  }
  const rm = rz.room_map;
  let h = `<section><h2>Room → rate-plan mapping
      <span class="band ${rm.matched ? 'band-'+(rm.band==='strong'?'high':'medium') : 'band-none'}">${rm.matched ? esc(rm.band) : 'no match'}</span>
      ${rm.llm_used ? '<span class="muted" style="font-weight:400">· LLM tie-break</span>' : ''}</h2><div class="body">`;

  if (rm.matched)
    h += `<div class="muted" style="margin-bottom:6px">room_type_id <span class="mono">${esc(rm.room_type_id)}</span> · score <span class="mono">${rm.score}</span>
      ${rm.meal_filter ? '· meal <span class="mono">'+esc(rm.meal_filter)+'</span>' : ''}
      ${rm.refundable_filter!=null ? '· '+(rm.refundable_filter?'refundable':'non-refundable') : ''}</div>`;
  if (rm.view_flag)
    h += `<div class="muted" style="margin-bottom:6px">⚑ ${esc(rm.view_flag)}</div>`;
  const rmNotes = (rm.notes||[]).filter(n => n !== rm.view_flag);
  if (rmNotes.length)
    h += `<ul class="warn-list">${rmNotes.map(n=>`<li>${esc(n)}</li>`).join('')}</ul>`;

  // TripJack results for the matched room_type_id — on top
  if ((rm.rate_options||[]).length) {
    const rk = new Set(rm.ratekey_option_ids||[]);
    h += `<div class="muted" style="margin:10px 0 4px">TripJack — all ${rm.rate_options.length} option(s) for this room type${rk.size ? ' · '+rk.size+' match the requested rate plan (highlighted)' : ''}. Pick one → <b>Prebook</b>.</div>`;
    h += `<table><tr><th></th><th>optionId</th><th>room</th><th>meal</th><th>refund</th><th>total</th><th>tags</th></tr>
      ${rm.rate_options.map(o=>`<tr${rk.has(o.option_id) ? ' style="background:rgba(74,170,110,.16)"' : ''}>
        <td><button class="pb-btn" data-oid="${esc(o.option_id)}">Prebook</button></td>
        <td class="mono">${esc((o.option_id||'').slice(0,8))}</td>
        <td>${esc(o.room_name)}</td>
        <td>${esc(o.meal_basis)}</td>
        <td>${o.refundable ? 'yes' : 'no'}</td>
        <td class="mono">${esc(o.currency)} ${o.total_price}</td>
        <td class="muted">${(o.tags||[]).map(esc).join(', ')}</td></tr>`).join('')}
      </table>`;
  }
  // our price (OTA benchmark) — underneath
  if (rm.our_price) {
    const p = rm.our_price, cur = esc(p.currency||'');
    const rows = [['Room', esc(p.room_name||'—')],
      ['Meal / cancel', esc([p.meal_plan, p.cancellation].filter(Boolean).join(' · ')||'—')]];
    if (p.subtotal!=null) rows.push(['Subtotal', cur+' '+p.subtotal]);
    if (p.taxes!=null) rows.push(['Taxes', cur+' '+p.taxes]);
    if (p.discount!=null) rows.push(['Discount', cur+' '+p.discount]);
    rows.push(['Final payable', '<b>'+cur+' '+p.final_payable+'</b>']);
    let delta = '';
    const rkOpts = (rm.rate_options||[]).filter(o=>(rm.ratekey_option_ids||[]).includes(o.option_id));
    if (rkOpts.length && p.final_payable) {
      const best = Math.min(...rkOpts.map(o=>o.total_price));
      const diff = best - p.final_payable, pct = diff/p.final_payable*100;
      delta = `<div class="muted" style="margin-top:6px">best matching TripJack rate <span class="mono">${cur} ${best}</span> —
        <span class="mono" style="color:${diff<=0?'#4a4':'#c66'}">${diff<=0?'':'+'}${cur} ${Math.round(diff)} (${pct>=0?'+':''}${pct.toFixed(1)}%)</span> vs our price</div>`;
    }
    h += `<div class="muted" style="margin:12px 0 4px">Our price (OTA benchmark)</div>
      <table>${rows.map(kv=>`<tr><td>${kv[0]}</td><td class="mono">${kv[1]}</td></tr>`).join('')}</table>${delta}`;
  }
  h += `<details><summary>all ${(rm.ranked_buckets||[]).length} room-type buckets (ranked)</summary>
    <table><tr><th>room_type_id</th><th>best-matched name</th><th>score</th><th>band</th><th>#opt</th></tr>
    ${(rm.ranked_buckets||[]).map(b=>`<tr>
      <td class="mono">${esc(b.room_type_id)}</td><td>${esc(b.canonical)}</td>
      <td class="mono">${b.score}</td><td class="muted">${esc(b.band)}</td>
      <td class="mono">${b.n_options}</td></tr>`).join('')}
    </table></details>`;
  return h + `</div></section>`;
}

// ── 4. Prebook (Review) — user-selected option, revalidate, NO Book ──
function resolution_prebook(rz) {
  if (!rz || !rz.available || !rz.room_map || !rz.room_map.matched) return '';
  return `<section><h2>Prebook (Review)</h2><div class="body">
    <div class="muted">Pick an option in the table above and hit <b>Prebook</b> — this fires
      <span class="mono">POST /hms/v3/hotel/review</span> for that exact option and returns the
      TripJack bookingId. No Book / Hold call is made.</div>
    <div id="prebook-out" style="margin-top:12px"></div>
  </div></section>`;
}

// ── collapsible: the full TripJack pricing option list ───────────
function resolution_live_options(rz) {
  if (!rz || !rz.available || !rz.detail) return '';
  const dt = rz.detail, opts = dt.options || [];
  let h = `<section><details><summary>TripJack live options · ${esc(dt.hotel_name||'')} · ${opts.length} option(s)</summary>`;
  if (dt.notes && dt.notes.length)
    h += `<ul class="warn-list">${dt.notes.map(n=>`<li>${esc(n)}</li>`).join('')}</ul>`;
  if (opts.length) {
    h += `<table><tr><th>room</th><th>meal</th><th>refundable</th><th>free-cancel until</th><th>total</th><th>type</th></tr>
      ${opts.map(o=>`<tr>
        <td>${esc((o.rooms||[]).map(r=>r.name).join(' + ')||'—')}</td>
        <td>${esc(o.meal_basis||'—')}</td>
        <td>${o.refundable ? 'yes' : 'no'}</td>
        <td class="muted">${val(o.free_cancel_until)}</td>
        <td class="mono">${esc(o.currency||'')} ${o.total_price}</td>
        <td class="mono muted">${esc(o.option_type||'')}</td></tr>`).join('')}
      </table>`;
  }
  h += `<details><summary>raw pricing response</summary><pre class="raw">${esc(JSON.stringify(dt, null, 2))}</pre></details>`;
  return h + `</details></section>`;
}
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # CORS: lets the Chrome extension (chrome-extension://… origin) call
        # this same localhost API the panel UI already uses. No cookies /
        # credentials are involved, so a wildcard origin is fine here.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if self.path.startswith("/api/job"):
            jid = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
            job = _JOBS.get(jid)
            if not job:
                self._send(404, b'{"error":"unknown job"}')
                return
            out = {"done": job["done"], "log": list(job["log"])}
            if job["done"]:
                out["result"] = job.get("result")
                out["error"] = job.get("error")
                out["trace"] = job.get("trace")
            self._send(200, json.dumps(out, default=str).encode("utf-8"))
            return
        if self.path.startswith("/webhook/whatsapp"):
            # Meta's webhook-config screen verifies ownership with a GET
            # carrying hub.mode/hub.verify_token/hub.challenge — echo the
            # challenge back only if the token matches ours.
            from yta import whatsapp
            challenge = whatsapp.verify_challenge(parse_qs(urlparse(self.path).query))
            if challenge is not None:
                self._send(200, challenge.encode("utf-8"), "text/plain; charset=utf-8")
            else:
                self._send(403, b"forbidden", "text/plain; charset=utf-8")
            return
        self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        if self.path == "/webhook/whatsapp":
            # Inbound WhatsApp message delivery. Meta expects a fast 2xx
            # ack (it retries on timeout/5xx) — acknowledge immediately,
            # then do the actual extract+resolve+reply per message in a
            # background thread, same pattern as /api/extract's job runner.
            try:
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
            except Exception:  # noqa: BLE001
                payload = {}
            self._send(200, b'{"status":"ok"}')
            from yta import whatsapp
            msgs = whatsapp.parse_inbound(payload)
            print(f"[wa] webhook POST received, {len(msgs)} message(s) parsed", flush=True)
            for msg in msgs:
                _enqueue_whatsapp_message(msg)   # debounced — see _WA_PENDING above
            return
        if self.path not in ("/api/extract", "/api/review"):
            self._send(404, b'{"error":"not found"}')
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:  # noqa: BLE001
            self._send(400, json.dumps({"error": f"bad request: {e}"}).encode())
            return

        if self.path == "/api/review":
            out = _do_review(req.get("job_id", ""), req.get("option_id", ""))
            self._send(200 if out.get("ok") else 400,
                       json.dumps(out, default=str).encode())
            return

        job_id = uuid.uuid4().hex[:12]
        with _JOBS_LOCK:
            _JOBS[job_id] = {"log": [], "done": False}
            for old in [k for k in _JOBS if _JOBS[k]["done"]][:-15]:
                _JOBS.pop(old, None)          # keep the last ~15 finished jobs
        threading.Thread(target=_run_job, args=(job_id, req), daemon=True).start()
        self._send(202, json.dumps({"job_id": job_id}).encode())

    def log_message(self, fmt, *args):  # quieter console
        return


def _do_review(job_id: str, option_id: str) -> dict:
    """User picked an option in the panel → hit POST /hms/v3/hotel/review for
    it and return {ok, status, booking_id, ...}. Option ids expire, so this
    re-runs pricing and re-matches the selection by its stable signature
    (room_type_id + meal + refundable + nearest price). NO Book call."""
    job = _JOBS.get(job_id)
    if not job or not job.get("done") or not job.get("result"):
        return {"ok": False, "error": "unknown or unfinished job"}
    rz = (job["result"] or {}).get("resolution") or {}
    rm, ctx = rz.get("room_map") or {}, rz.get("prebook_ctx") or {}
    if not ctx:
        return {"ok": False, "error": "no prebook context (no confident match / pricing)"}
    sel = next((o for o in rm.get("rate_options", []) if o["option_id"] == option_id), None)
    if not sel:
        return {"ok": False, "error": f"option {option_id} not in this result"}

    try:
        from yta.tripjack.client import TripJackClient, TripJackError
        from yta.tripjack.hotel import (hotel_options, find_option,
                                        review_from_detail, review_request)
        client = TripJackClient.from_env()
        if not client.configured():
            return {"ok": False, "error": "TRIPJACK_API_KEY not set"}

        det = hotel_options(ctx["tj_id"], ctx["check_in"], ctx["check_out"],
                            ctx["rooms_query"], currency=ctx["currency"], client=client)
        chosen = find_option(det, sel["room_type_id"], sel["meal_basis"],
                             bool(sel["refundable"]), near_price=sel["total_price"])
        if not chosen:
            return {"ok": False,
                    "error": "selected option is no longer available in a fresh "
                             "pricing call — re-run the extraction"}
        rv = review_from_detail(det, chosen.option_id, client=client)
        return {
            "ok": True,
            "status": "success" if rv.booking_id else "no bookingId",
            "tj_booking_id": rv.booking_id,
            "hotel_name": rv.hotel_name,
            "selected": {"room": sel["room_name"], "meal": sel["meal_basis"],
                         "refundable": sel["refundable"], "price": sel["total_price"]},
            "confirmed_price": rv.option.total_price if rv.option else None,
            "currency": rv.option.currency if rv.option else ctx["currency"],
            "price_changed": rv.price_changed, "price_delta": rv.price_delta,
            "onhold_allowed": rv.onhold_allowed, "deadline": rv.deadline,
            "refundable": rv.option.refundable if rv.option else None,
            "free_cancel_until": rv.option.free_cancel_until if rv.option else None,
            "booking_notes": rv.option.booking_notes if rv.option else None,
            "notes": rv.notes,
            "review_request": review_request(ctx["tj_id"], chosen.option_id,
                                             det.review_hash,
                                             correlation_id=det.correlation_id),
            "raw": rv.raw,
        }
    except TripJackError as e:
        return {"ok": False, "error": f"[{e.code}] {e.message}", "status": "failed"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "status": "failed"}


def _resolve(packet) -> dict:
    """Run the TripJack hotel-id lookup; degrade gracefully if the DB is
    not built or the resolver errors. Logs its steps into packet.run_log."""
    try:
        from yta.hoteldb.db import db_path
        if not db_path().exists():
            packet.log("TripJack lookup skipped — data/hotels.db not built")
            return {"available": False,
                    "note": "TripJack DB not built — run "
                            "`python -m yta.hoteldb.load <dump.xlsx>`"}
        from yta.hoteldb.link import resolve_packet
        import time
        h = packet.hotel
        packet.log(f"TripJack resolve: name={h.name!r} city={h.city!r} "
                   f"lat/lng={h.lat},{h.lng}")
        t = time.perf_counter()
        r = resolve_packet(packet)
        ms = round((time.perf_counter() - t) * 1000, 1)
        for L in getattr(r, "layers", []):
            packet.log(f"  {L}")
        m = r.match
        packet.log(f"TripJack → {r.band}"
                   + (f": tj_id={m.tj_id} unica_id={m.unica_id} "
                      f"'{m.hotel_name}' (score {m.score})" if m else " — no match")
                   + f"  [{ms} ms]")
        d = r.to_dict()
        d["available"] = True
        d["ms"] = ms

        # the Detail/Pricing request we'd send for a confident match
        if m:
            try:
                from yta.tripjack.hotel import pricing_request_from_packet
                d["detail_request"] = pricing_request_from_packet(
                    packet, m.tj_id, currency=os.environ.get("TRIPJACK_CURRENCY", "INR"))
                packet.log(f"built TripJack Detail request for tj_id {m.tj_id}")
            except ValueError as e:
                d["detail_request_error"] = str(e)
                packet.log(f"TripJack Detail request not buildable: {e}")

        # actually hit POST /hms/v3/hotel/pricing for a confident match
        if m and r.band in ("high", "medium") and "detail_request" in d:
            try:
                from yta.tripjack.client import TripJackClient, TripJackError
                from yta.tripjack.hotel import hotel_options
                client = TripJackClient.from_env()
                if not client.configured():
                    packet.log("TripJack pricing skipped — TRIPJACK_API_KEY not set")
                else:
                    s = packet.stay
                    t2 = time.perf_counter()
                    det = hotel_options(
                        m.tj_id, s.check_in, s.check_out,
                        s.occupancy or [{"adults": s.adults or 2,
                                         "children": s.children or 0,
                                         "child_ages": s.child_ages or []}],
                        currency=client.currency,      # account currency, NOT the OTA's
                        client=client)
                    pms = round((time.perf_counter() - t2) * 1000, 1)
                    d["detail"] = det.to_dict()
                    packet.log(f"TripJack pricing → {len(det.options)} option(s)"
                               + (f"; {det.notes[0]}" if det.notes else "")
                               + f"  [{pms} ms]")

                    # map the OTA requested offer onto a TJ ratekey
                    if det.options:
                        from yta.roommap import map_rooms
                        rm = map_rooms(
                            det.options, packet.requested_offer,
                            benchmark_price=packet.ota_benchmark.final_payable,
                            policy=packet.matching_policy, log=packet.log)
                        rmd = rm.to_dict()
                        b = packet.ota_benchmark
                        rmd["our_price"] = {
                            "final_payable": b.final_payable, "subtotal": b.subtotal,
                            "taxes": b.taxes, "fees": b.fees, "discount": b.discount,
                            "currency": b.currency,
                            "room_name": packet.requested_offer.room_name,
                            "meal_plan": packet.requested_offer.meal_plan,
                            "cancellation": packet.requested_offer.cancellation,
                        }
                        d["room_map"] = rmd
                        # prebook context — used on demand by POST /api/review
                        # when the user picks an option (option ids expire, so
                        # a fresh pricing call + re-match happens then)
                        d["prebook_ctx"] = {
                            "tj_id": m.tj_id, "check_in": det.check_in,
                            "check_out": det.check_out, "rooms_query": det.rooms_query,
                            "currency": det.currency}
                        packet.log(
                            f"room map → {'matched ' + str(rm.room_type_id) if rm.matched else 'no match'}"
                            f" [{rm.band}]; {len(rm.rate_options)} rate option(s)"
                            + ("; LLM used" if rm.llm_used else "")
                            + " — pick an option in the panel to prebook (Review)")
            except TripJackError as e:
                d["detail_error"] = str(e)
                packet.log(f"TripJack pricing error: {e}")
            except Exception as e:  # noqa: BLE001
                d["detail_error"] = f"{type(e).__name__}: {e}"
                packet.log(f"TripJack pricing error: {type(e).__name__}: {e}")
        return d
    except Exception as e:  # noqa: BLE001
        packet.log(f"TripJack lookup error: {type(e).__name__}: {e}")
        return {"available": False, "note": f"{type(e).__name__}: {e}"}


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"YourTravelAgent debug panel → http://{HOST}:{PORT}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
