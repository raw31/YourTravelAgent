# OTA-to-Wholesale Hotel Booking Agent — v0 Extraction Layer

## Handover context
This picks up from a strategy doc + design conversation. Full strategy doc
(competitive landscape, architecture, TripJack integration, pricing,
legal considerations, MVP roadmap) should be uploaded alongside this file.
This handover covers the **v0 extraction layer only**: given a pasted OTA
URL, produce a structured Booking Intent packet (hotel, dates, occupancy,
room, price).

## Decided architecture
```
Pasted URL
     │
     ▼
OTA Router — match domain against adapter registry
     │
     ├── Known OTA (dedicated adapter) → per-OTA extraction method (varies — see below)
     │
     └── Unknown OTA → Generic fallback:
           1. Fetch/render page content
           2. Clean to plain text
           3. LLM extraction → packet schema, null for anything unclear
           4. Validate (sanity-check types, ranges, suspicious defaults)
```

Key principle: **each OTA needs its own adapter, not a shared scraper** —
the three OTAs tested so far each require a genuinely different
extraction method. Do not assume Playwright-everywhere or
selectors-everywhere; check each new OTA's rendering behavior before
building its adapter.

## Files already built (attached)
- `mmt_extract.py` — MMT adapter skeleton. Parses URL params (free fields)
  + Playwright network-response capture (NOT DOM scraping — MMT's data
  loads via async API call, confirmed empty `window.__INITIAL_STATE__`
  on load). Has a `TODO` where the exact API response field paths need
  to be filled in after a first manual run against
  `captured_responses.json`.
- `llm_generic_parser.py` — generic fallback for OTAs without a
  dedicated adapter. Takes cleaned page text, calls Claude API with a
  strict schema (null over guessing), returns per-field confidence
  scores, includes a `validate_packet()` sanity-check pass.

## Per-OTA findings (hand-verified against one live test hotel — Aloha on
the Ganges, Rishikesh, Sept 2026 dates)

### MakeMyTrip (hotel-review page)
- **Rendering**: client-side SPA. Confirmed `window.__INITIAL_STATE__`
  exists in initial HTML but is an empty scaffold (`null`/`loading:false`)
  — no hotel/room/price data present until an async API call resolves.
- **Extraction method**: Playwright + capture the JSON network response
  (not DOM text — more robust to frontend redesigns). Exact API host not
  yet isolated — traffic captured was dominated by ad-tracking noise
  (DoubleClick, Facebook Pixel, Google Ads conversion pixels). Also saw
  obfuscated POST endpoints (`/hzpj-YlWf/...` pattern) consistent with
  bot-mitigation tooling (PerimeterX/Akamai-style) — expect this OTA to
  actively fingerprint automated traffic.
- **URL params give for free**: checkin, checkout (DDMMYYYY format),
  hotelId, lat/lng, occupancy (`rsc`/`roomStayQualifier`, format
  `roomsEadultsEchildren`), selected room-type ID.
- **Next step**: run `mmt_extract.py`, inspect `captured_responses.json`
  for the real hotel-detail response, fill in the field-path TODOs.

### Booking.com (`secure.booking.com/book.html`)
- **Rendering**: appears server-rendered — hotel name, room name, price,
  breakfast inclusion, cancellation terms were all present in the DOM
  immediately after page load (unlike MMT). Not yet confirmed with a pure
  zero-JS HTTP test (worth doing before committing to "no browser
  needed").
- **Extraction method**: likely a plain HTTP GET is sufficient — meaningfully
  cheaper than Playwright if confirmed. Verify with `requests.get()` +
  realistic User-Agent against a real URL; check the same strings land
  in the raw response body.
- **URL params give for free**: checkin (ISO format), occupancy
  (`room1=A,A` — comma-separated `A` per adult), and notably **price**
  (`rt_selected_total_price`) — MMT never exposed price in its URL.

### Agoda (`agoda.com/en-in/book/`)
- **Rendering**: server-rendered, similar to Booking.com.
- **URL params give for free**: room name (`roomName`), breakfast flag
  (`isBreakfastIncluded`), refundability flag (`isEasyCancel` — false =
  non-refundable), and price (`sai` param in the `roomToken` blob) — the
  richest URL-embedded data of the three OTAs.
- **Critical gotcha**: **no `checkin`/`checkout` param in this URL
  format.** Tested live — the page silently defaulted to today/tomorrow's
  date with no error. The real dates the user selected exist only in
  Agoda's server-side session (tied to cookies from the earlier
  search → hotel-page → room-selection flow), reconstructed via the
  `roomToken`/`secdat`/`sarg` signed blobs — not decodable client-side.
- **Required safeguard**: always sanity-check extracted `check_in`
  against "is this suspiciously == today's date" — if so, flag the
  extraction as unreliable rather than trust it silently. Check whether
  Agoda has an earlier-funnel URL (hotel/search results page) that
  carries dates explicitly — likely safer for users to paste than this
  deep-linked booking-form URL.

## Open TODOs for this session
1. Finish MMT adapter: isolate real API response in `captured_responses.json`.
2. Verify Booking.com plain-HTTP-works hypothesis; drop Playwright for
   that adapter if confirmed (cost/speed win).
3. Build Agoda adapter with the today-date sanity check as a hard guard.
4. Build the `OTAAdapter` registry/router (domain → adapter mapping,
   fallback to `llm_generic_parser.py`).
5. Test `llm_generic_parser.py` against MMT/Booking.com page text first
   (known-good comparison) before trusting it on a genuinely unknown OTA.
6. Not yet started: TripJack supplier adapter, matching engine, pricing
   engine, quote UI (Phases 2–3 in the strategy doc).

## Standing caveat (from strategy doc Section 14, unresolved)
Automated extraction from OTA pages — whether server-side fetch or
browser-rendered — sits near a published MMT user-agreement clause
restricting travel-agent/commercial/resale use without prior
registration. Not resolved; flagged here so it isn't lost in handover.
Worth actual legal review before this goes past prototype/personal use.
