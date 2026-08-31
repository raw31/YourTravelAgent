"""
MMT hotel-review page extractor (v0)

Fastest approach: don't scrape rendered DOM text. Load the page in a real
(headless) browser and capture the JSON API response(s) the page itself
uses to render hotel/room/price data. That JSON is structured, complete,
and far less likely to break on a frontend redesign than CSS selectors.

Usage:
    pip install playwright
    playwright install chromium
    python mmt_extract.py "<mmt_review_url>"

First run: inspect captured_responses.json to find which response holds
hotel name / room / price, then fill in the extraction paths marked TODO
below. After that, every subsequent run is fully automated.
"""
import sys
import json
from urllib.parse import urlparse, parse_qs
from playwright.sync_api import sync_playwright


def parse_url_fields(url: str) -> dict:
    """Everything gettable for free, no fetch required."""
    q = parse_qs(urlparse(url).query)

    def first(k, default=None):
        return q.get(k, [default])[0]

    def to_iso(d):
        # MMT uses DDMMYYYY
        if not d or len(d) != 8:
            return None
        return f"{d[4:8]}-{d[2:4]}-{d[0:2]}"

    # rsc format: rooms 'e' adults 'e' children, e.g. "1e2e0e"
    rsc_parts = first("rsc", "").split("e")

    def to_int(v):
        return int(v) if v and v.isdigit() else None

    return {
        "source": {
            "ota": "makemytrip",
            "url": url,
            "hotel_id": first("hotelId"),
        },
        "hotel": {
            "city_code": first("locusId"),
            "lat": float(first("lat")) if first("lat") else None,
            "lng": float(first("lng")) if first("lng") else None,
        },
        "stay": {
            "check_in": to_iso(first("checkin")),
            "check_out": to_iso(first("checkout")),
            "rooms": to_int(rsc_parts[0]) if len(rsc_parts) > 0 else None,
            "adults": to_int(rsc_parts[1]) if len(rsc_parts) > 1 else None,
            "children": to_int(rsc_parts[2]) if len(rsc_parts) > 2 else None,
        },
    }


def capture_api_payloads(url: str, timeout_ms: int = 25000) -> list:
    """Load the page in a real browser and capture JSON responses that
    look like they carry hotel/room/price data, instead of reading the
    rendered DOM."""
    captured = []

    def looks_relevant(resp):
        ct = resp.headers.get("content-type", "")
        if "application/json" not in ct:
            return False
        # TODO: once you confirm the exact host/path in DevTools
        # (Network tab -> XHR/Fetch, look for the response containing
        # the hotel name/price), narrow this filter to just that host.
        return "makemytrip.com" in resp.url and (
            "mapi." in resp.url or "/api/" in resp.url or "hotel" in resp.url.lower()
        )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            )
        )

        def on_response(resp):
            try:
                if looks_relevant(resp):
                    captured.append({"url": resp.url, "body": resp.json()})
            except Exception:
                pass  # response wasn't JSON / body already consumed — skip

        page.on("response", on_response)
        try:
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
        except Exception as e:
            print(f"Warning: page load didn't fully settle ({e}). "
                  f"Captured {len(captured)} response(s) so far anyway.")
        browser.close()

    return captured


def main():
    if len(sys.argv) < 2:
        print("Usage: python mmt_extract.py <mmt_review_url>")
        sys.exit(1)

    url = sys.argv[1]
    packet = parse_url_fields(url)
    api_hits = capture_api_payloads(url)

    packet["_debug"] = {
        "api_responses_captured": len(api_hits),
        "api_urls": [h["url"] for h in api_hits],
    }

    # TODO once you've inspected captured_responses.json:
    # packet["hotel"]["name"] = ...
    # packet["hotel"]["address"] = ...
    # packet["requested_offer"] = {
    #     "room_name": ...,
    #     "meal_plan": ...,
    #     "cancellation": ...,
    #     "description": ...,
    # }
    # packet["ota_offer"] = {"final_payable": ..., "currency": "INR"}

    print(json.dumps(packet, indent=2))

    with open("captured_responses.json", "w") as f:
        json.dump(api_hits, f, indent=2)
    print(f"\nSaved {len(api_hits)} raw API response(s) to "
          f"captured_responses.json — inspect this to find the exact "
          f"field paths, then fill in the TODOs above.")


if __name__ == "__main__":
    main()
