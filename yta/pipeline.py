"""Extraction pipeline: pasted URL (or pasted page / PDF / screenshots)
-> Booking Intent packet.

    route → get page content (render | pasted text/HTML | uploaded media)
          → LLM extraction (reads the URL params AND the page)
          → validate

One LLM path for every OTA. No per-OTA parsers. For session-bound checkout
URLs that don't survive a server-side re-fetch (secure.booking.com/book.html,
agoda /book/, mmt hotel-review) pass the live page's own content via
`page_text=` / `page_html=`, or upload a screenshot / PDF.
"""
from __future__ import annotations

import json
import re

from yta import extract_llm, llm
from yta.profiles import route, GENERIC
from yta.render import render as render_page, clean_text, focus, RenderUnavailable
from yta.schema import BookingIntent, Source, LLM, URL, now_iso, validate
from yta.urlfacts import latlng_from_url


def _text_from_html(html: str) -> str:
    try:
        from lxml import html as LH
        doc = LH.fromstring(html)
        for bad in doc.xpath("//script | //style | //noscript | //svg | //template"):
            bad.getparent().remove(bad)
        return clean_text(doc.text_content())
    except Exception:
        return clean_text(re.sub(r"<[^>]+>", " ", html))


def _found_summary(pkt) -> str:
    got = []
    if pkt.hotel.name:
        got.append("hotel name")
    if pkt.stay.check_in and pkt.stay.check_out:
        got.append("dates")
    if pkt.stay.occupancy:
        got.append(f"occupancy ({len(pkt.stay.occupancy)} room"
                   f"{'s' if len(pkt.stay.occupancy) != 1 else ''})")
    elif pkt.stay.rooms:
        got.append(f"{pkt.stay.rooms} room(s)")
    if pkt.requested_offer.room_name:
        got.append("room name")
    if pkt.requested_offer.description:
        got.append("room description")
    if pkt.ota_benchmark.final_payable:
        got.append("price")
    return ", ".join(got) if got else "nothing usable"


def _occ_repr(occ) -> str:
    parts = []
    for r in occ:
        s = f"{r.adults or '?'}A"
        if r.children:
            ages = f"({','.join(map(str, r.child_ages))})" if r.child_ages else ""
            s += f"+{r.children}C{ages}"
        parts.append(f"[{s}]")
    return f"{len(occ)}R: " + " ".join(parts)


def extract(url: str = "", *, render: bool = True, page_text: str | None = None,
            page_html: str | None = None, media: list | None = None,
            timeout_ms: int = 40000, run_validate: bool = True,
            log_sink: list | None = None) -> BookingIntent:
    profile = route(url) if url else GENERIC
    pkt = BookingIntent(source=Source(
        ota=profile.name, url=url, page_type=profile.page_type,
        extraction_method="url_only", extracted_at=now_iso(),
    ))
    if log_sink is not None:
        pkt.run_log = log_sink          # share the list so a poller sees it grow live
    if url:
        pkt.log(f"opening URL: {url}")
        pkt.log(f"site: {profile.name}")

    # -- coordinates straight from the URL (deterministic, no LLM) ----
    if url:
        lat, lng = latlng_from_url(url)
        if lat is not None:
            pkt.add("hotel.lat", lat, URL, 1.0, "URL coordinate param")
            pkt.add("hotel.lng", lng, URL, 1.0, "URL coordinate param")
            pkt.log(f"coordinates found in the URL: {lat}, {lng}")

    # -- assemble page content ---------------------------------------
    context, content_src = None, "url"
    if page_html:
        context = focus(_text_from_html(page_html), 7000)
        content_src = "url+pasted_html"
        pkt.log(f"input: pasted HTML — {len(page_html):,} chars → "
                f"{len(context):,} chars after focus()")
    elif page_text:
        context = focus(clean_text(page_text), 7000)
        content_src = "url+pasted_text"
        pkt.log(f"input: pasted text — {len(page_text):,} chars → "
                f"{len(context):,} chars after focus()")
    elif media:
        content_src = f"{'url+' if url else ''}upload[{len(media)} file(s)]"
        pkt.log(f"reading {len(media)} uploaded file(s): "
                f"{', '.join(m[0] for m in media)}")
    elif render and url:
        pkt.log("fetching the page in a real browser …")
        try:
            rr = render_page(url, wait=profile.render_wait, settle_ms=profile.settle_ms,
                             content_selector=profile.content_selector,
                             timeout_ms=timeout_ms, on_step=pkt.log)
        except RenderUnavailable as e:
            pkt.warnings.append(str(e))
            pkt.log(f"browser not available: {e}")
            rr = None
        if rr is not None:
            pkt.source.rendered = True
            pkt.log(f"page loaded — {len(rr.text):,} chars of visible text")
            for n in rr.notes:
                pkt.warnings.append(f"render: {n}")
                pkt.log(f"note: {n}")
            if rr.coords and not _is_set(pkt, "hotel.lat"):
                lat, lng, src = rr.coords
                pkt.add("hotel.lat", lat, URL, 0.9, f"page ({src})")
                pkt.add("hotel.lng", lng, URL, 0.9, f"page ({src})")
            if rr.has_content() and not rr.blocked():
                context = rr.llm_context()
                content_src = f"url+render[{rr.browser_channel}]"
                pkt.log(f"prepared {len(context):,} chars of page content for parsing")
            else:
                why = ("OTA blocked the render (bot wall / redirect to search)"
                       if rr.blocked() else "page rendered no readable content "
                       "(session-bound URL)")
                pkt.log(f"render UNUSABLE — {why}; falling back to URL params only")
                pkt.warnings.append(
                    f"Could not read the page — {why}. Paste the page's "
                    f"text/HTML from your open tab, or upload a screenshot / PDF. "
                    f"(Extracted below from the URL alone.)")
    elif url:
        pkt.log("browser step skipped — reading the URL parameters only")

    if not url and context is None and not media:
        pkt.warnings.append("nothing to extract from")
        pkt.log("nothing to extract from — no URL, no page, no upload")
        if run_validate:
            validate(pkt)
        return pkt

    # -- LLM extraction: primary provider, then fall back / fill gaps --
    try:
        chain = llm.provider_chain(media=bool(media))
        pkt.log(f"parsing the booking with the LLM ({' → '.join(chain)})")
    except llm.LLMUnavailable as e:
        pkt.warnings.append(str(e))
        pkt.log(f"no LLM available: {e}")
        chain = []

    used = []
    for prov in chain:
        pkt.log(f"asking {prov} to extract the data points …")
        try:
            res = extract_llm.extract(context, url=url, media=media, provider=prov)
        except llm.SizeLimitError:
            pkt.warnings.append(
                f"{prov}: request over its token/min limit — trying next provider")
            pkt.log(f"{prov} rejected the request (too large for its free tier) "
                    f"— switching provider")
            continue
        except (llm.LLMUnavailable, json.JSONDecodeError) as e:
            pkt.warnings.append(f"{prov}: {type(e).__name__} — trying next provider")
            pkt.log(f"{prov} failed ({type(e).__name__}) — switching provider")
            continue

        _apply(pkt, res, fill_only=bool(used))
        used.append(f"{res.provider}:{res.model}")
        pkt.derive_stay()
        pkt.log(f"{res.provider} returned: {_found_summary(pkt)}"
                + ("  (gap-fill pass)" if len(used) > 1 else ""))
        if res.contradictions:
            pkt.log(f"{res.provider} flags a URL/page mismatch: {res.contradictions}")
        if not pkt.check_mandatory():
            pkt.log("all mandatory fields present")
            break
        if prov != chain[-1]:
            pkt.warnings.append(
                f"{res.provider} missing {', '.join(pkt.missing_mandatory)} — "
                f"asking the next provider to fill the gaps")
            pkt.log(f"still missing: {', '.join(pkt.missing_mandatory)} "
                    f"— asking the next provider to fill the gaps")

    if used:
        pkt.source.extraction_method = f"{content_src}+llm:{'+'.join(used)}"

    pkt.log("running sanity checks")
    if run_validate:
        validate(pkt)

    missing = pkt.check_mandatory()
    if missing:
        tail = ("" if (page_html or page_text or media) else
                " — paste the page's text/HTML from your open tab, or upload a "
                "screenshot / PDF")
        pkt.warnings.insert(0, f"FAIL: missing mandatory field(s): "
                               f"{', '.join(missing)}{tail}")
        pkt.log(f"result: FAIL — could not find {', '.join(missing)}")
    else:
        pkt.log("result: OK — every mandatory field found")
    return pkt


def _is_set(pkt, path: str) -> bool:
    obj_path, _, attr = path.rpartition(".")
    target = pkt
    for part in obj_path.split("."):
        target = getattr(target, part)
    v = getattr(target, attr)
    return bool(v)


# coordinates parsed straight from the URL are authoritative — the LLM may
# only supply them when they aren't already set
_URL_WINS = ("hotel.lat", "hotel.lng")


def _apply(pkt: BookingIntent, res, fill_only: bool = False) -> None:
    for path, val in res.fields.items():
        conf = res.confidence.get(path, 0.6)
        if path == "stay.occupancy":
            if fill_only and pkt.stay.occupancy:
                continue
            rooms = _norm_occ(val)
            if rooms:
                pkt.stay.set_occupancy(rooms)
                pkt.note("stay.occupancy", _occ_repr(pkt.stay.occupancy),
                         LLM, conf, f"url+page ({res.provider})")
        else:
            if (fill_only or path in _URL_WINS) and _is_set(pkt, path):
                continue
            pkt.add(path, val, LLM, conf, f"url+page ({res.provider})")

    for c in res.contradictions:
        pkt.warnings.append(f"LLM flagged: {c}")


def _norm_occ(val):
    out = []
    for r in val or []:
        if not isinstance(r, dict):
            continue
        ages = [int(a) for a in (r.get("child_ages") or [])
                if str(a).lstrip("-").isdigit()]
        ch = r.get("children")
        ch = int(ch) if str(ch).lstrip("-").isdigit() else (len(ages) if ages else 0)
        ad = r.get("adults")
        out.append({"adults": int(ad) if str(ad).lstrip("-").isdigit() else None,
                    "children": ch, "child_ages": ages})
    return out
