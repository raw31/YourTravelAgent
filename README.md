# YourTravelAgent — OTA-to-wholesale booking agent (v0)

See `OTA_to_Wholesale_Hotel_Booking_Agent_Strategy.docx`.

**Flow:** pasted OTA URL (or pasted page / PDF / screenshots)
→ **Phase 1** extract a structured **Booking Intent packet**
(hotel, dates, occupancy, room, rate terms, price) with per-field provenance
→ **Phase 2** resolve the hotel to its **TripJack `tj_id`** (+ `unica_id`)
against the local hotel master
→ *(next)* TripJack Hotel Detail API → equivalence match → quote.

```python
from yta import extract
from yta.hoteldb.link import resolve_packet

packet = extract("https://www.booking.com/hotel/in/aloha-on-the-ganges.html?...")
match  = resolve_packet(packet)      # match.match.tj_id / .unica_id / band
```

Both stages run in the debug panel (`python -m yta.web`) and CLI
(`python -m yta.cli "<url>"`).

```python
from yta import extract
packet = extract("https://www.booking.com/hotel/in/aloha-on-the-ganges.html?...")
packet = extract(url, render=False)                 # URL params only, no browser/LLM
packet = extract(url, page_html="<html>...")        # parse pasted page content
packet = extract(media=[("application/pdf", pdf_bytes)])   # PDF / screenshots of the page
```

## Pipeline

```
paste URL  (or pasted page text/HTML, or uploaded screenshots / PDF)
   ▼
route → OTA profile (booking / agoda / mmt / generic) — routing + render spec only
   ▼
① page content, when available — one of:
     • render (Playwright, real Chrome) — works for non-session-bound URLs
     • pasted text/HTML — for session-bound checkout links (from the live tab)
     • uploaded PDF / screenshots → vision model (Gemini)
   + captured JSON XHR responses
   ▼
② LLM extraction — ONE path for every OTA. Reads the booking URL params
   (date formats, occupancy encodings) AND the page. Produces:
     hotel name / address, per-room occupancy, room name, bed, view,
     meal plan, cancellation text, payment terms, price breakdown
     (text → Groq openai/gpt-oss-120b ;  image/PDF → Gemini flash)
   ▼
validate + mandatory-field gate → BookingIntent packet
   status "ok" | "fail" (+ missing_mandatory[]).  A request FAILS unless all of:
     hotel.name · stay.check_in · stay.check_out · stay.rooms ·
     stay.occupancy · requested_offer.room_name · requested_offer.description ·
     ota_benchmark.final_payable
   are present.  (Agoda /book/ URLs alone always fail — paste the page.)
   ▼
③ resolve → yta.hoteldb  (local SQLite, ~1M TripJack hotels)
     cascade: FTS name + geo box → country → city → address → coord rank
     → tj_id + unica_id + band (high / medium / low / none) + ranked candidates
```

**No per-OTA parsers.** The OTA-URL decoding knowledge lives in the LLM
prompt (`yta/extract_llm.py`), not scattered across Python. When the render
is blocked (MMT) or a URL is session-bound, extraction falls back to the
URL params alone and warns; paste the page or upload a screenshot to fill
in names/room/price.

Build the hotel DB once: `python -m yta.hoteldb.load "<dump.xlsx>"`
(see `yta/hoteldb/README.md`).

## Run

```bash
python -m yta.web                       # http://127.0.0.1:8765  (debug panel)
python -m yta.cli "<url>"               # render + LLM parse
python -m yta.cli "<url>" --no-render   # URL params only
python -m yta.cli "<url>" --html page.html --evidence
python -m yta.cli --image shot1.png --image shot2.png     # screenshots of the page
python -m yta.cli --pdf booking.pdf                       # PDF of the page
python -m yta.cli "<url>" --pdf booking.pdf               # URL hints + PDF
```

## Layout

| Path | Role |
|------|------|
| `yta/schema.py` | `BookingIntent` packet (Appendix A, `schema_version` 1.0), `evidence[]`, `clean_text()`, `validate()` |
| `yta/profiles.py` | domain → profile; `url_hints()`, render spec per OTA |
| `yta/render.py` | Playwright render (real Chrome) + XHR/JSON-LD capture + `focus()` trimming |
| `yta/ingest.py` | uploaded PDF / image → media parts (mime detection, size guards) |
| `yta/extract_llm.py` | LLM extraction layer — strict names-first schema; text or multimodal |
| `yta/llm.py` | provider resolution — text (Groq) + vision (Gemini), from `.env` |
| `yta/pipeline.py` | orchestration + URL↔page merge / cross-check |
| `yta/web.py` · `yta/cli.py` | debug panel + CLI |
| `tests/` | `pytest` (24, no network) |

## Per-OTA reality (live-tested, Aloha on the Ganges, Sept 2026)

| | URL params | Server render | Notes |
|---|---|---|---|
| **Booking.com** public hotel page | checkin, checkout, occupancy | ✅ real Chrome | full content: name, address, rooms, prices, policies |
| **Booking.com** `secure.booking.com/book.html` | + `interval`→nights, `rt_selected_total_price`, hotel slug | ❌ session expires → 404 | paste page content |
| **Agoda** `/book/` | roomName, isEasyCancel, roomToken (`sai`=price, `rcy`=currency); **no dates** | ❌ signed-token/cookie bound → API 500 | paste page content |
| **MakeMyTrip** `hotel-review/` | checkin/checkout (**MMDDYYYY**, not DDMMYYYY), `_uCurrency`, `rsc` occupancy, `searchText` city | ❌ bot-blocked (PerimeterX `/hzpj-*/`, HTTP/2 fingerprint) → bounced to search | paste page content |

The three checkout URLs are **session-bound by design** — the strategy doc
(§14) says extraction should happen client-side in the user's live browser,
not via server-side crawling. The paste path is that; server render is a
bonus for earlier-funnel and other OTAs.

## LLM — two-provider ensemble

- **Text**: **Groq** `openai/gpt-oss-120b` (`GROQ_API_KEY`).
- **Image / PDF**: **Google Gemini** `gemini-flash-latest` (`GOOGLE_API_KEY`),
  falls back to `gemini-flash-lite-latest`. Reads scanned PDFs / screenshots
  directly — no local OCR.

The pipeline tries providers in order (`yta/llm.provider_chain`) and:
- **Groq's free tier caps at 8k tokens/min** → a bigger request returns 413,
  surfaced as `SizeLimitError` → the pipeline retries on **Gemini** (much
  larger free limit).
- If a provider succeeds but **any mandatory field is still null**, the next
  provider runs in *fill-only* mode — it can supply the missing fields but
  cannot overwrite what the first one already found.

`method` shows every provider used, e.g. `url+render[chrome]+llm:groq:...`
or `...+llm:groq:...+gemini:...`.

Keys are in `.env`, reused from the Delhivery retention project. Swappable
via `LLM_PROVIDER` / `GROK_API_KEY` / `ANTHROPIC_API_KEY`. Grok (xAI, `xai-…`)
≠ Groq (`gsk_…`).

## Open TODOs

1. Wire the browser extension as the real `page_html` source (Phase 1 in
   the strategy doc: Chrome/Edge extension + DOM extractor + debug panel).
2. Agoda: accept an earlier-funnel URL (search / hotel page) that carries
   dates, or get dates from the pasted page.
3. `hotel.lat`/`lng` — pull from the page / JSON-LD (currently only MMT URL
   provides them).
4. Batch/queue LLM calls or upgrade the Groq tier for throughput.
5. Phase 2: TripJack adapter, hotel-identity resolver, equivalence engine.

## Standing caveat

Strategy doc §14: automated OTA extraction sits near MMT's user-agreement
clause restricting travel-agent / resale use. Prefer client-side extraction
of data already shown to the user; get legal review before prototype.
