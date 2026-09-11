// YourTravelAgent — ISOLATED-world bridge.
//
// capture.js runs in the page's own JS realm (world:"MAIN") so it can patch
// fetch/XHR; this script runs in the extension's normal isolated world, the
// only place with access to chrome.runtime. The two talk over the one
// channel they share: DOM CustomEvents on `document`.
//
// This script ALSO places a small "YourTravelAgent" price-comparison card
// on the page — pure DOM read/write, no MAIN-world access needed for that.
(() => {
  // -- generic on-page price locator --------------------------------
  //
  // NEVER locate the OTA's price by selector/class/OTA name — that's exactly
  // the per-OTA branching this project has repeatedly ruled out. Instead we
  // already KNOW the number (ota_benchmark.final_payable came out of the
  // same page via the shared pipeline) and find the text node that renders
  // that number, the same way a person would recognise it by eye. Works
  // identically on any OTA; nothing here is keyed to a particular site.
  let ytaLastCard = null;

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
        if (node.parentElement && node.parentElement.closest(".yta-price-card-host")) {
          return NodeFilter.FILTER_REJECT;                  // never match our own card
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

  // A generic <div> comparison card, isolated in a Shadow DOM so the host
  // page's CSS (wildly different per OTA) never leaks in and our styles
  // never leak out. Same reason we don't use selectors: this has to look
  // right next to ANY site's price, unstyled by that site.
  function buildCard({ ourPrice, ourCurrency, tjPrice, tjCurrency, band, tags,
                       roomName, mealBasis, refundable }) {
    const diff = tjPrice - ourPrice;
    const pct = ourPrice ? (diff / ourPrice) * 100 : 0;
    const cheaper = diff <= 0;
    const diffTxt = cheaper
      ? `▼ ${Math.abs(pct).toFixed(1)}% cheaper`
      : `▲ ${pct.toFixed(1)}% more`;
    const meta = [roomName, mealBasis,
                 refundable === true ? "refundable" : refundable === false ? "non-refundable" : null]
      .filter(Boolean).map(escapeHtml).join(" · ");

    const host = document.createElement("span");
    host.className = "yta-price-card-host";
    const shadow = host.attachShadow({ mode: "open" });
    shadow.innerHTML = `
      <style>
        :host { all: initial; }
        .card { display: inline-flex; flex-direction: column; gap: 3px;
          margin-left: 8px; padding: 7px 10px; min-width: 172px; max-width: 260px;
          border-radius: 8px; background: #f0fdf4; border: 1px solid #86efac;
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          font-size: 12px; color: #14532d; vertical-align: middle;
          box-shadow: 0 1px 4px rgba(0,0,0,.15); position: relative; }
        .head { display: flex; justify-content: space-between; align-items: center; }
        .brand { font-weight: 700; font-size: 11px; letter-spacing: .02em; color: #166534; }
        .band { display: inline-block; padding: 0 5px; border-radius: 4px; margin-left: 4px;
          background: #bbf7d0; color: #14532d; font-size: 9.5px; font-weight: 700; text-transform: uppercase; }
        .close { cursor: pointer; color: #4d7c58; font-size: 13px; line-height: 1; padding: 0 2px; border: none; background: none; }
        .close:hover { color: #14532d; }
        .row { display: flex; justify-content: space-between; gap: 10px; }
        .row .k { color: #4d7c58; }
        .row .v { font-weight: 600; white-space: nowrap; }
        .tj .v { color: #15803d; font-size: 13px; }
        .diff { font-size: 11px; font-weight: 600; }
        .diff.up { color: #b91c1c; }
        .diff.down { color: #15803d; }
        .meta { font-size: 10.5px; color: #4d7c58; opacity: .85;
          white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      </style>
      <div class="card">
        <div class="head">
          <span class="brand">YourTravelAgent${band ? `<span class="band">${escapeHtml(band)}</span>` : ""}</span>
          <button class="close" title="dismiss">✕</button>
        </div>
        <div class="row"><span class="k">This page</span><span class="v">${escapeHtml(ourCurrency || "")} ${escapeHtml(ourPrice)}</span></div>
        <div class="row tj"><span class="k">TripJack</span><span class="v">${escapeHtml(tjCurrency || ourCurrency || "")} ${escapeHtml(tjPrice)}</span></div>
        <div class="diff ${cheaper ? "down" : "up"}">${escapeHtml(diffTxt)}</div>
        ${meta ? `<div class="meta" title="${meta}">${meta}</div>` : ""}
      </div>`;
    shadow.querySelector(".close").addEventListener("click", () => host.remove());
    if (tags && tags.length) host.title = tags.join(", ");
    return host;
  }

  function showPriceCard(msg) {
    if (ytaLastCard && ytaLastCard.parentNode) ytaLastCard.remove();
    const el = findPriceElement(msg.ourPrice);
    if (!el) return { ok: false, placed: false, reason: "price text not found on page" };

    const card = buildCard(msg);
    el.insertAdjacentElement
      ? el.insertAdjacentElement("afterend", card)
      : el.parentNode.insertBefore(card, el.nextSibling);
    ytaLastCard = card;
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
        sendResponse(showPriceCard(msg));
      } catch (err) {
        sendResponse({ ok: false, error: String(err) });
      }
      return true;
    }

    return false;
  });
})();
