"""v0 — today's behavior, moved here unchanged.

This is a straight relocation of what used to be yta.web._process_batch,
kept byte-for-byte behaviorally identical (same two branches: "is there
an open session, yes or no", same messages, same bugs and all) so it
stays the safe, known-quantity default while v1 (yta.wa_flows.v1) is
being tried out. Switch flows with YTA_WA_FLOW — see yta/wa_flows/__init__.py.
"""
from __future__ import annotations

import threading

from yta.wa_shared import ask_for_missing, extracted_lines, finish_and_reply, wa_send

# In-memory: phone number -> {"packet": BookingIntent, "missing": [paths]}.
# A customer is "mid-conversation" whenever they're in here — their NEXT
# message is treated as an answer to the missing-fields question, not as a
# fresh link/photo.
_WA_SESSIONS: dict = {}
_WA_SESSIONS_LOCK = threading.Lock()


def handle_batch(frm: str, items: list) -> None:
    from yta import whatsapp
    from yta.extract_llm import extract_clarification
    from yta.pipeline import extract
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
            # A clarification reply is often ANOTHER screenshot (e.g. "what's
            # the price?" answered with a photo of the price breakdown), not
            # typed text — without downloading it here, that reply was
            # silently dropped and the bot just re-asked the same question
            # forever. Same download path the fresh-submission branch uses.
            clarify_media_items = []
            for m in items:
                if m.get("type") in ("image", "document") and m.get("media_id"):
                    dl = whatsapp.download_media(m["media_id"])
                    if dl:
                        data, mime = dl
                        clarify_media_items.append({"name": "whatsapp-media", "mime": mime, "bytes": data})
                        print(f"[wa v0] downloaded clarification media: {len(data)} bytes, {mime}", flush=True)
            clarify_media = None
            if clarify_media_items:
                from yta.ingest import load_uploads
                clarify_media = load_uploads(clarify_media_items)
            print(f"[wa v0] treating batch as clarification for {frm}: {new_text[:160]!r} "
                  f"(accumulated: {accumulated[:200]!r}, media_count={len(clarify_media_items)})", flush=True)
            packet = session["packet"]
            fields, clarify = extract_clarification(session["missing"], accumulated, media=clarify_media)
            print(f"[wa v0] clarification filled: {list(fields.keys())}; note={clarify!r}", flush=True)
            for path, val in fields.items():
                packet.add(path, val, LLM, 0.7, "whatsapp clarification")
            packet.derive_stay()
            still_missing = packet.check_mandatory()
            print(f"[wa v0] still missing after clarification: {still_missing}", flush=True)
            if still_missing:
                with _WA_SESSIONS_LOCK:
                    _WA_SESSIONS[frm] = {"packet": packet, "missing": still_missing,
                                          "clarify_text": accumulated}
                ask_for_missing(frm, still_missing, clarify)
                return
            with _WA_SESSIONS_LOCK:
                _WA_SESSIONS.pop(frm, None)
            finish_and_reply(frm, packet)
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
                    print(f"[wa v0] downloaded media: {len(data)} bytes, {mime}", flush=True)
        media = None
        if media_items:
            from yta.ingest import load_uploads
            media = load_uploads(media_items)

        if not url and not media:
            print("[wa v0] no link or media found — sending the how-to-use reply", flush=True)
            wa_send(frm, "Send me a hotel booking link or a screenshot "
                         "of one and I'll check the best price for it.")
            return

        wa_send(frm, "Checking your deal")   # ack before the (vision) extraction runs

        print(f"[wa v0] extracting: url={url!r} media_count={len(media_items)}", flush=True)
        packet = extract(url or "", render=bool(url), media=media, log_sink=[])
        print(f"[wa v0] extraction done: hotel={packet.hotel.name!r} status={packet.status}", flush=True)

        wa_send(frm, "\n".join(["Your deal:", "----"] + extracted_lines(packet)))

        missing = packet.missing_mandatory or packet.check_mandatory()
        if missing:
            with _WA_SESSIONS_LOCK:
                _WA_SESSIONS[frm] = {"packet": packet, "missing": missing}
            ask_for_missing(frm, missing)
            return

        finish_and_reply(frm, packet)
    except Exception as e:  # noqa: BLE001
        print(f"[wa v0] ERROR handling batch: {type(e).__name__}: {e}", flush=True)
        with _WA_SESSIONS_LOCK:
            _WA_SESSIONS.pop(frm, None)
        wa_send(frm, f"Sorry, something went wrong: {type(e).__name__}: {e}")
