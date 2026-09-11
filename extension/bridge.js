// YourTravelAgent — ISOLATED-world bridge.
//
// capture.js runs in the page's own JS realm (world:"MAIN") so it can patch
// fetch/XHR; this script runs in the extension's normal isolated world, the
// only place with access to chrome.runtime. The two talk over the one
// channel they share: DOM CustomEvents on `document`.
//
// This script ALSO places the "YourTravelAgent price" badge on the page —
// pure DOM read/write, no MAIN-world access needed for that part.
(() => {
  // -- generic on-page price badge --------------------------------
  //
  // NEVER locate the OTA's price by selector/class/OTA name — that's exactly
  // the per-OTA branching this project has repeatedly ruled out. Instead we
  // already KNOW the number (ota_benchmark.final_payable came out of the
  // same page via the shared pipeline) and find the text node that renders
  // that number, the same way a person would recognise it by eye.
  let ytaLastBadge = null;

  function normalizeNum(s) {
    const cleaned = String(s).replace(/[^\d.]/g, "");
    if (!cleaned) return null;
    const n = parseFloat(cleaned);
    return Number.isNaN(n) ? null : n;
  }

  function findPriceElement(targetValue) {
    if (targetValue == null || !isFinite(targetValue)) return null;
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        const t = node.nodeValue;
        if (!t || t.length > 200 || !/\d/.test(t)) return NodeFilter.FILTER_REJECT;
        if (node.parentElement && node.parentElement.closest(".yta-price-badge")) {
          return NodeFilter.FILTER_REJECT;                  // never match our own badge
        }
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    let best = null, bestLen = Infinity;
    let node;
    while ((node = walker.nextNode())) {
      const matches = node.nodeValue.match(/[\d][\d,]*\.?\d*/g) || [];
      for (const m of matches) {
        const n = normalizeNum(m);
        if (n == null) continue;
        if (Math.abs(n - targetValue) < Math.max(1, targetValue * 0.001)) {
          const el = node.parentElement;
          const len = el ? el.textContent.trim().length : node.nodeValue.length;
          if (len < bestLen) { bestLen = len; best = el || node; }   // shortest = most specific
        }
      }
    }
    return best;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"]/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  function showPriceBadge({ ourPrice, ourCurrency, tjPrice, tjCurrency, band, tags }) {
    if (ytaLastBadge && ytaLastBadge.parentNode) ytaLastBadge.remove();
    const el = findPriceElement(ourPrice);
    if (!el) return { ok: false, placed: false, reason: "price text not found on page" };

    const diff = tjPrice - ourPrice;
    const pct = ourPrice ? (diff / ourPrice) * 100 : 0;
    const diffTxt = diff <= 0
      ? `▼ ${Math.abs(pct).toFixed(1)}% cheaper`
      : `▲ ${pct.toFixed(1)}% more`;

    const badge = document.createElement("span");
    badge.className = "yta-price-badge";
    badge.style.cssText =
      "display:inline-block;margin-left:8px;padding:2px 9px;border-radius:6px;" +
      "background:#e8f5e9;color:#1b5e20;font:600 12px -apple-system,BlinkMacSystemFont,sans-serif;" +
      "border:1px solid #4caf50;vertical-align:middle;white-space:nowrap;line-height:1.6;" +
      "z-index:2147483647;position:relative;";
    badge.title = (tags && tags.length ? tags.join(", ") : "") + (band ? `  [${band}]` : "");
    badge.innerHTML =
      `YourTravelAgent: ${escapeHtml(tjCurrency || ourCurrency || "")} ${escapeHtml(tjPrice)} ` +
      `<span style="opacity:.75">(${escapeHtml(diffTxt)})</span>`;

    el.insertAdjacentElement
      ? el.insertAdjacentElement("afterend", badge)
      : el.parentNode.insertBefore(badge, el.nextSibling);
    ytaLastBadge = badge;
    return { ok: true, placed: true };
  }

  // -- messaging ----------------------------------------------------
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (!msg) return false;

    if (msg.type === "yta:collect") {
      const onCollected = (e) => {
        document.removeEventListener("yta:collected", onCollected);
        try {
          sendResponse({ ok: true, data: JSON.parse(e.detail) });
        } catch (err) {
          sendResponse({ ok: false, error: String(err) });
        }
      };
      document.addEventListener("yta:collected", onCollected);
      document.dispatchEvent(new CustomEvent("yta:collect"));
      return true;      // capture.js answers same-tick, but keep the channel open
    }

    if (msg.type === "yta:showPrice") {
      try {
        sendResponse(showPriceBadge(msg));
      } catch (err) {
        sendResponse({ ok: false, error: String(err) });
      }
      return true;
    }

    return false;
  });
})();
