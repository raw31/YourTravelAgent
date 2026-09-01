"""Render layer — load an OTA page in a real headless browser and hand back
everything the LLM extraction layer might use: rendered visible text,
embedded JSON-LD, and JSON XHR/fetch responses the page made.

Plain HTTP is not enough for any target OTA (Booking.com = AWS WAF JS
challenge, Agoda/MMT = JS-rendered SPA shells). Bundled Chromium is also
often fingerprinted — we prefer the system's real Chrome via
`channel="chrome"` and fall back to bundled Chromium.

Deep *checkout* URLs (secure.booking.com/book.html, agoda.com/book/,
mmt hotel-review) are session-bound: they expire or 500 when re-fetched
outside the user's original funnel. For those, pass the page's own
content to the pipeline instead (`page_text=` / `page_html=`) — that's
what the browser-extension product does with the live tab.

Install:  pip install playwright && playwright install chromium
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

_WS = re.compile(r"[ \t ]+")
_BLANKS = re.compile(r"\n{3,}")
_MAX_XHR = 18
_MAX_XHR_BYTES = 60_000


class RenderUnavailable(RuntimeError):
    pass


@dataclass
class RenderResult:
    url: str
    final_url: str = ""
    text: str = ""
    json_ld: list = field(default_factory=list)
    xhr_json: list = field(default_factory=list)      # [{"url":..., "body":...}]
    notes: list = field(default_factory=list)
    browser_channel: str = ""
    coords: tuple | None = None                       # (lat, lng, source) from geoscan

    def llm_context(self, max_text: int = 7000, max_json: int = 2500,
                    max_digest: int = 9000) -> str:
        parts = [f"PAGE URL: {self.final_url or self.url}", "",
                 "RENDERED PAGE TEXT (booking-relevant excerpt):",
                 focus(self.text, max_text)]
        if self.json_ld:
            parts += ["", "EMBEDDED JSON-LD:",
                      json.dumps(self.json_ld, ensure_ascii=False)[:max_json]]
        if self.xhr_json:
            digest = _json_digest(self.xhr_json, max_digest)
            parts += ["", "CAPTURED API DATA (booking-relevant fields pulled from "
                      "the JSON the page fetched — often carries the per-room "
                      "guest split, child ages, price breakup and cancellation "
                      "rules that the visible page keeps behind a click):",
                      digest]
        return "\n".join(parts)

    def has_content(self) -> bool:
        if len(self.text) > 450 or self.json_ld:
            return True
        # a JSON XHR body only counts if it looks like hotel/room/price data
        for x in self.xhr_json:
            blob = str(x.get("body", "")).lower()
            if any(k in blob for k in ("room", "tariff", "price", "hotelname",
                                       "cancellation", "checkin")):
                return True
        return False

    def blocked(self) -> bool:
        """The OTA bounced us (bot wall / redirect to search)."""
        f = self.final_url.lower()
        return (any("hzpj-" in x["url"] or "awswaf" in x["url"] or "px-captcha"
                    in x["url"] for x in self.xhr_json)
                or "/hotel-details" in f or "no_availability" in f
                or "not a robot" in self.text.lower()[:2000])


# keys / values worth surfacing from a captured SPA API payload
_JSON_KEEP = re.compile(
    r"(?i)room|guest|adult|child|infant|pax|occup|bed|price|amount|total|"
    r"payable|tax|fee|discount|fare|cancel|refund|penalt|deadline|"
    r"check.?in|check.?out|night|\bdate\b|meal|board|breakfast|inclusion|"
    r"rate.?plan|non.?refundable|hotel.?name|address|latitude|longitude")
# UI plumbing / promo noise that also matches _JSON_KEEP — drop it
_JSON_DROP = re.compile(
    r"(?i)coupon|voucher|supercoin|super.?coin|salutation|raventrack|"
    r"eventname|actionlist|\bcta\b|_api_call|navigate|redirect|tooltip|"
    r"iconid|placeholder|validation|add new guest|saved guest|"
    r"^[A-Z][A-Z0-9_]{4,}$")
# leaves whose path names a high-value field — surface these first
_JSON_PRIORITY = re.compile(
    r"(?i)roomguest|occup|paxinfo|childrenages|childage|adultstring|"
    r"childrenstring|cancellation|penalt|breakup|break-up|pricingdetail|"
    r"check.?in|check.?out|roomname|room_type|hotelname|hotel_name|"
    r"mealbasis|meal_plan|totalprice|total_amount|finalprice|amount")


def _json_digest(xhr_list: list, max_chars: int) -> str:
    """Walk captured API JSON and keep only booking-relevant scalar leaves, so
    a big SPA payload still surfaces per-room occupancy / child ages / price
    breakup / cancellation rules to a small-context model (a blind prefix
    truncation would cut them off — the good bits sit deep in the tree).
    High-value fields (roomGuests, cancellation, price breakup …) come first."""
    keep: list[tuple[int, str]] = []
    seen: set[str] = set()

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                kp = f"{path}.{k}" if path else str(k)
                if isinstance(v, (dict, list)):
                    walk(v, kp)
                    continue
                if v in (None, "", "null"):
                    continue
                sv = str(v)
                if len(sv) >= 160 or _JSON_DROP.search(kp) or _JSON_DROP.search(sv):
                    continue
                if not (_JSON_KEEP.search(str(k)) or _JSON_KEEP.search(sv)):
                    continue
                line = f"{'.'.join(kp.split('.')[-3:])} = {v}"
                if line in seen:
                    continue
                seen.add(line)
                keep.append((0 if _JSON_PRIORITY.search(kp) else 1, line))
        elif isinstance(node, list):
            for i, v in enumerate(node[:60]):
                walk(v, f"{path}[{i}]")

    for blob in xhr_list:
        walk(blob.get("body"), "")

    keep.sort(key=lambda t: t[0])                 # priority lines first, stable
    return "\n".join(line for _, line in keep)[:max_chars]


# expand collapsed / clickable content before scraping the text — accordions,
# "show more", price break-ups, "guest information", room-detail sheets, fare
# rules. Generic: no per-OTA selectors.
_EXPAND_JS = r"""
() => {
  const RX = /\b(show|view|see|read)\s+(more|all|details|breakup|break-up|breakdown|rules)\b|\bmore\s+details\b|\b(guest|room|rate|price|fare|booking|tax)\s+(info|information|details|rules|breakup|break-up)\b|\bcancellation\s+(policy|details|charges|info)\b|\bprice\s+breakup\b|\+\s*\d+\s+more\b|^details$|^see details$|^view details$/i;
  const DENY = /log ?in|sign ?up|sign ?in|checkout|pay ?now|continue to pay|book now|proceed|delete|remove|cancel booking|apply|coupon|redeem/i;
  let n = 0;
  document.querySelectorAll('details:not([open])').forEach(d => { try { d.open = true; n++; } catch(e){} });
  const els = document.querySelectorAll(
    'button,a,summary,[role="button"],[aria-expanded="false"],' +
    '[class*="accordion" i],[class*="expand" i],[class*="collaps" i],' +
    '[class*="disclosure" i],[data-testid*="detail" i],[data-testid*="expand" i]');
  for (const el of els) {
    if (n >= 25) break;
    const txt = (el.innerText || el.getAttribute('aria-label') || '').trim().slice(0, 80);
    const expandable = el.getAttribute('aria-expanded') === 'false'
      || el.tagName === 'SUMMARY' || RX.test(txt);
    if (!expandable || DENY.test(txt)) continue;
    const href = el.getAttribute('href');
    if (href && /^https?:/i.test(href)) continue;      // don't navigate away
    try { el.click(); n++; } catch(e){}
  }
  return n;
}
"""


def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except ImportError as e:
        raise RenderUnavailable(
            "Playwright not installed. Run:\n"
            "    pip install playwright && playwright install chromium"
        ) from e


def clean_text(text: str) -> str:
    text = _WS.sub(" ", text)
    text = _BLANKS.sub("\n\n", text)
    return "\n".join(ln.strip() for ln in text.splitlines() if ln.strip())


_FOCUS_KW = re.compile(
    r"(?i)(hotel|resort|room|suite|apartment|villa|deluxe|check[\s-]?in|"
    r"check[\s-]?out|night|guest|adult|child|breakfast|meal|board|"
    r"cancel|refund|non-refundable|free cancellation|pay|prepaid|price|"
    r"total|amount|payable|tax|fee|₹|rs\.?|inr|usd|eur|address|rishikesh|"
    r"\bview\b|bed|king|twin|queen|balcony)")


def focus(text: str, max_chars: int) -> str:
    """Keep lines that look booking-relevant (plus their immediate
    neighbours) so a small-context model sees signal, not chrome."""
    if len(text) <= max_chars:
        return text
    lines = text.splitlines()
    keep = set()
    for i, ln in enumerate(lines):
        if _FOCUS_KW.search(ln):
            keep.update((i - 1, i, i + 1))
    picked, total = [], 0
    for i, ln in enumerate(lines):
        if i in keep:
            total += len(ln) + 1
            if total > max_chars:
                break
            picked.append(ln)
    out = "\n".join(picked)
    return out[:max_chars] if out else text[:max_chars]


def _launch(p, prefer_chrome: bool):
    args = ["--disable-blink-features=AutomationControlled"]
    if prefer_chrome:
        try:
            return p.chromium.launch(headless=True, channel="chrome", args=args), "chrome"
        except Exception:
            pass
    return p.chromium.launch(headless=True, args=args), "chromium"


def _default_xhr_keep(page_netloc: str) -> Callable[[str], bool]:
    def keep(u: str) -> bool:
        ul = u.lower()
        if not any(h in ul for h in (page_netloc, "/api/", "mapi", "graphql",
                                     "bff", "booking", "hotel", "room", "price",
                                     "tariff", "detail")):
            return False
        # skip obvious noise
        return not any(b in ul for b in ("track", "telemetry", "analytics",
                                         "pixel", "beacon", "consent", "gtm",
                                         "doubleclick", "/ads", "awswaf"))
    return keep


def render(url: str, *,
           wait: str = "domcontentloaded",
           settle_ms: int = 9000,
           content_selector: Optional[str] = None,
           xhr_keep: Optional[Callable[[str], bool]] = None,
           timeout_ms: int = 40000,
           prefer_chrome: bool = True,
           on_step: Optional[Callable[[str], None]] = None) -> RenderResult:
    from urllib.parse import urlparse
    sync_playwright = _require_playwright()
    res = RenderResult(url=url)
    keep = xhr_keep or _default_xhr_keep(urlparse(url).netloc.lower())
    step = on_step or (lambda _m: None)

    def _on_response(resp):
        if len(res.xhr_json) >= _MAX_XHR:
            return
        try:
            if "application/json" not in resp.headers.get("content-type", ""):
                return
            if not keep(resp.url):
                return
            raw = resp.text()
            if len(raw) > _MAX_XHR_BYTES or not raw.strip():
                return
            res.xhr_json.append({"url": resp.url.split("?")[0], "body": json.loads(raw)})
        except Exception:
            pass

    with sync_playwright() as p:
        step("launching headless browser")
        browser, channel = _launch(p, prefer_chrome)
        res.browser_channel = channel
        ctx = browser.new_context(user_agent=_UA, locale="en-US",
                                  viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        page.on("response", _on_response)
        step(f"opening the URL in {channel} …")
        try:
            page.goto(url, wait_until=wait, timeout=timeout_ms)
        except Exception as e:
            res.notes.append(f"navigation issue: {type(e).__name__}")
        step(f"waiting {settle_ms}ms for the page to settle (SPA / lazy content)")
        page.wait_for_timeout(min(settle_ms, 4000))
        try:
            page.mouse.wheel(0, 4000)          # trigger lazy room/price blocks
            page.wait_for_timeout(max(settle_ms - 4000, 1500))
        except Exception:
            pass

        head = (page.content() or "")[:4000].lower()
        if "awswafintegration" in head or "verify that you're not a robot" in head:
            step("bot-challenge page detected — waiting for it to clear")
            res.notes.append("bot-challenge page — waiting for auto-retry")
            page.wait_for_timeout(7000)

        # open accordions / "show more" / guest-info & price-breakup sheets so
        # their text gets scraped too (generic — no per-OTA selectors)
        try:
            opened = page.evaluate(_EXPAND_JS)
            if opened:
                step(f"expanded {opened} collapsible/clickable section(s)")
                page.wait_for_timeout(1500)
        except Exception:
            pass

        res.final_url = page.url
        for node in page.query_selector_all('script[type="application/ld+json"]'):
            try:
                res.json_ld.append(json.loads(node.inner_text()))
            except Exception:
                pass
        step(f"captured {len(res.xhr_json)} JSON API response(s), "
             f"{len(res.json_ld)} JSON-LD block(s)")

        text = ""
        if content_selector:
            el = page.query_selector(content_selector)
            if el:
                text = el.inner_text()
        if not text:
            text = page.inner_text("body")
        res.text = clean_text(text)

        # deterministic coordinate scan of the raw markup (the LLM never sees it)
        try:
            from yta.geoscan import find_latlng
            res.coords = find_latlng(page.content(), res.json_ld, res.xhr_json)
        except Exception:
            res.coords = None
        if res.coords:
            step(f"found coordinates in the page ({res.coords[2]}): "
                 f"{res.coords[0]}, {res.coords[1]}")

        ctx.close()
        browser.close()

    if not res.has_content():
        res.notes.append(
            "little/no content rendered — the URL is likely session-bound "
            "(expired checkout link). Paste the page's text/HTML instead.")
    return res
