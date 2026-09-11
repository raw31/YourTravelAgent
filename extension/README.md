# YourTravelAgent — Chrome extension

Reads hotel name, dates, occupancy and room/price straight out of the OTA
tab you already have open — your real, logged-in session, no bot-wall, no
session-expiry, no headless re-fetch — and runs it through the exact same
pipeline the debug panel uses (`yta.web`'s `/api/extract`). Nothing about
the existing CLI / panel / Playwright render path changes; this is a third,
additive input source (`page_data=`) alongside pasted text/HTML and
uploaded screenshots.

## How it captures data

| Channel | Same as `yta/render.py`'s |
|---|---|
| `fetch()` / `XMLHttpRequest` request bodies + API `GET` query strings | `_on_request` |
| JSON response bodies | `_on_response` |
| `__NEXT_DATA__` / `__INITIAL_STATE__` / redux / nuxt / bare `application/json` `<script>` blocks | `_scan_embedded_state` |
| `<script type="application/ld+json">` | JSON-LD collection |
| `document.body.innerText` | visible page text |

Same shapes (`{url, kind: "request"\|"response"\|"embedded", body}`), same
size caps, same keep/noise heuristics — ported line-for-line so the server's
`_json_digest()` / `llm_context()` need zero changes to consume either
source.

## Load it

1. Make sure the local panel is running:
   ```bash
   cd ~/YourTravelAgent && python -m yta.web
   ```
2. Chrome → `chrome://extensions` → enable **Developer mode** (top right) →
   **Load unpacked** → select this `extension/` folder.
3. Open the OTA tab you want to extract (a hotel review page, a checkout
   page you're logged into, etc.) — the page can be anything, no need to
   reload after installing unless you loaded the extension *after* opening
   the tab (content scripts only attach to tabs loaded after install/reload).
4. Click the extension icon → **Extract this page**.

The popup shows hotel / dates / occupancy / room / price and, if it
resolved, the TripJack match and the best matching rate. **Open full debug
panel →** takes you to `localhost:8765` for the live pricing table,
room→rate-plan mapping, and the Prebook (Review) button.

## The on-page price badge

When a room resolves, a small **"YourTravelAgent: ₹X (▼N% cheaper)"** badge
is dropped right next to the OTA's own displayed price, on the page itself.

**How it finds "the OTA's price" — generically, never by selector:** the
badge placer is never told which CSS class or element holds the price for
any given site. It is handed the *number* our own pipeline already
extracted from that same page (`ota_benchmark.final_payable`) and walks the
page's text nodes looking for that number, the same way a person would
recognise it by eye — not by knowing anything about the site. If the exact
figure isn't found verbatim on the page (rare — formatting differences,
canvas-rendered price, etc.), the popup still shows the numbers, it just
can't place the on-page badge. Nothing here is keyed to Booking.com,
Agoda, MakeMyTrip, or any other OTA by name.

## Files

- `manifest.json` — MV3. `content_scripts` run on every page: `capture.js`
  in the page's own `"world":"MAIN"` (so it can patch `fetch`/`XHR` before
  the OTA's own scripts run), `bridge.js` in the extension's normal isolated
  world (the only place `chrome.runtime` is reachable).
- `capture.js` — the patcher + on-demand collector. Answers a
  `yta:collect` DOM event with a `yta:collected` event carrying a JSON
  string (crossing the MAIN/isolated world boundary as a string avoids
  cross-realm object issues).
- `bridge.js` — relays `chrome.runtime.onMessage` ↔ those DOM events.
- `popup.html` / `popup.js` — the toolbar UI. Posts straight to
  `http://127.0.0.1:8765/api/extract` with `{url, page_data, resolve:true}`
  and polls `/api/job` — the same two endpoints the panel's own page already
  calls.

## Why this doesn't need new permissions per OTA

`content_scripts.matches: ["<all_urls>"]` — the point is exactly *not* to
maintain a per-OTA domain list. `host_permissions` only names
`127.0.0.1:8765` (so the popup can fetch the local API without CORS
friction); reading the active tab's data uses `activeTab`, which never
needs a host permission.

## Privacy

Nothing leaves your machine except the localhost call to your own running
`yta.web`. The capture only starts recording network bodies that match a
booking-shaped URL (`/api/`, `graphql`, `booking`, `hotel`, `room`, `price`,
`tariff`, `detail`, or same-host) and skips analytics/tracking/ads noise;
nothing is sent anywhere until you click **Extract this page**.
