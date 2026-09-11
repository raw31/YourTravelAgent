// YourTravelAgent popup — collects the active tab's booking data via
// bridge.js/capture.js and posts it to the SAME local endpoint the debug
// panel (yta/web.py) already serves: no separate server, no separate
// pipeline. `page_data` just takes priority over Playwright render there.
const BASE = "http://127.0.0.1:8765";

// TODO: set this to your own WhatsApp number (country code + number, no
// spaces/+/dashes — e.g. "919876543210") or a full https://wa.me/... link.
// Left blank ships safely: the book-with-me button still works, it just
// opens plain wa.me with no pre-selected contact until this is set.
const WHATSAPP_NUMBER = "";

const $status = document.getElementById("status");
const $out = document.getElementById("out");
const $go = document.getElementById("go");
const $link = document.getElementById("panelLink");
const $bookBtn = document.getElementById("bookBtn");
const $bookPanel = document.getElementById("bookPanel");
const $bookSummary = document.getElementById("bookSummary");
const $waLink = document.getElementById("waLink");

let lastDeal = null;   // populated by render() whenever TripJack matches a rate

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function occRepr(occ) {
  if (!occ || !occ.length) return "—";
  return occ.map((r) => `${r.adults ?? "?"}A${r.children ? "+" + r.children + "C" : ""}`).join(", ");
}

// The most relevant TripJack option for the matched room: prefer one that
// satisfies the requested rate plan (ratekey_option_ids), cheapest of those;
// otherwise just the cheapest option of the matched room. Mirrors
// RoomMapResult.best_option() server-side — same rule, so the number shown
// in the page badge always matches what "Prebook" in the full panel would
// pick first.
function pickBestOption(roomMap) {
  const opts = (roomMap && roomMap.rate_options) || [];
  if (!opts.length) return null;
  const keyed = new Set(roomMap.ratekey_option_ids || []);
  const pool = opts.filter((o) => keyed.has(o.option_id));
  const use = pool.length ? pool : opts;
  return use.reduce((a, b) => (b.total_price < a.total_price ? b : a));
}

async function getActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

function collectFromTab(tabId) {
  return new Promise((resolve, reject) => {
    chrome.tabs.sendMessage(tabId, { type: "yta:collect" }, (resp) => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message));
        return;
      }
      if (!resp || !resp.ok) {
        reject(new Error((resp && resp.error) || "no response from the page"));
        return;
      }
      resolve(resp.data);
    });
  });
}

// run_log entries carry `ms` = milliseconds since extraction started (set
// server-side in BookingIntent.log()). Rendered here as seconds, with the
// per-step delta — how long THAT step took, not just the running total.
function renderSteps(runLog, totalSec) {
  if (!runLog || !runLog.length) return "";
  let prevMs = 0;
  const lines = runLog.map((l) => {
    const deltaSec = ((l.ms - prevMs) / 1000).toFixed(1);
    prevMs = l.ms;
    return `<div class="step"><span class="t">+${deltaSec}s</span>${esc(l.msg)}</div>`;
  }).join("");
  return `<details style="margin-top:8px"><summary>Steps — ${runLog.length}, ${totalSec.toFixed(1)}s total</summary>
    <div class="steps">${lines}</div></details>`;
}

function poll(jobId, tabId, startedAt) {
  const t = setInterval(async () => {
    let s;
    try {
      s = await (await fetch(`${BASE}/api/job?id=${jobId}`)).json();
    } catch (e) {
      return;                                   // panel not reachable yet — keep trying
    }
    const elapsed = ((Date.now() - startedAt) / 1000).toFixed(1);
    if (!s.done) {
      $status.textContent = `Extracting… ${elapsed}s (${(s.log || []).length} step(s))`;
      return;
    }
    clearInterval(t);
    $go.disabled = false;
    if (s.error) {
      $status.textContent = `Failed after ${elapsed}s: ` + s.error;
      return;
    }
    render(s.result, tabId, parseFloat(elapsed));
  }, 500);
}

function render(result, tabId, totalSec) {
  const p = result.packet;
  const rz = result.resolution || {};
  const ok = p.status === "ok";
  const timeTxt = totalSec != null ? ` in ${totalSec.toFixed(1)}s` : "";
  $status.textContent = ok ? `✓ Extracted${timeTxt}`
    : `✗ Missing${timeTxt}: ` + (p.missing_mandatory || []).join(", ") + " — try pasting the page in the full panel.";

  const rows = [
    ["Hotel", p.hotel.name || "—"],
    ["Dates", `${p.stay.check_in || "?"} → ${p.stay.check_out || "?"}`],
    ["Occupancy", occRepr(p.stay.occupancy)],
    ["Room", p.requested_offer.room_name || "—"],
    ["Price", p.ota_benchmark.final_payable
      ? `${p.ota_benchmark.currency || ""} ${p.ota_benchmark.final_payable}` : "—"],
  ];
  if (rz.available && rz.match) {
    const band = esc(rz.band);
    rows.push(["TripJack",
      `<span class="band band-${band}">${band}</span> ${esc(rz.match.tj_id)} · ${esc(rz.match.hotel_name)}`]);
  } else if (rz.available === false) {
    rows.push(["TripJack", esc(rz.note || "not resolved")]);
  }

  const best = rz.room_map && rz.room_map.matched ? pickBestOption(rz.room_map) : null;
  lastDeal = best ? {
    hotelName: p.hotel.name, checkIn: p.stay.check_in, checkOut: p.stay.check_out,
    occupancy: occRepr(p.stay.occupancy), roomName: best.room_name,
    mealBasis: best.meal_basis, refundable: best.refundable,
    tjPrice: best.total_price, tjCurrency: best.currency,
    ourPrice: p.ota_benchmark.final_payable, ourCurrency: p.ota_benchmark.currency,
    url: p.source && p.source.url,
  } : null;
  $bookBtn.style.display = lastDeal ? "block" : "none";
  $bookPanel.style.display = "none";
  if (!best && rz.available && rz.match) {
    // hotel matched in TripJack but no price came through — say why instead
    // of silently showing nothing (pricing outage, IP-allowlist rejection,
    // no inventory for these dates, or no confident room match).
    let why = "no price available";
    if (rz.detail_error) why = "pricing call failed: " + rz.detail_error;
    else if (rz.detail && !((rz.detail.options || []).length)) why = "TripJack has no inventory for this hotel/stay";
    else if (rz.room_map && !rz.room_map.matched) why = `no confident room match (best score ${rz.room_map.score ?? "?"})`;
    rows.push(["YourTravelAgent", `<span style="color:#8b949e">${esc(why)}</span>`]);
  }
  if (best) {
    const band = esc(rz.room_map.band);
    rows.push(["YourTravelAgent",
      `<span class="band band-${band === "strong" ? "high" : "medium"}">${band}</span> ` +
      `${esc(best.currency)} ${best.total_price}`]);

    // Always attempt the on-page card once TJ has returned a matched,
    // priced option — do NOT additionally require the OTA's own price to
    // have been extracted. (Bug: this used to be gated on
    // `p.ota_benchmark.final_payable`, so a run where TJ pricing succeeded
    // but final_payable itself happened to be one of the missing fields —
    // possible since resolve/pricing only needs hotel.name, not a fully
    // "ok" packet — silently sent no message at all: no card, no error,
    // nothing. showPriceCard() below handles a missing ourPrice by always
    // floating instead of trying to locate it on the page.)
    chrome.tabs.sendMessage(tabId, {
      type: "yta:showPrice",
      ourPrice: p.ota_benchmark.final_payable ?? null,
      ourCurrency: p.ota_benchmark.currency,
      tjPrice: best.total_price,
      tjCurrency: best.currency,
      band: rz.room_map.band,
      tags: best.tags,
      roomName: best.room_name,
      mealBasis: best.meal_basis,
      refundable: best.refundable,
    }, (resp) => {
      if (chrome.runtime.lastError) {
        // most likely: the extension/tab needs a reload after an update
        const note = document.createElement("div");
        note.style.cssText = "margin-top:6px;color:#f87171;font-size:11px;";
        note.textContent = "Card not shown — reload this tab (and the extension "
          + "if it was just updated) and try again.";
        $out.appendChild(note);
        return;
      }
      const note = document.createElement("div");
      note.style.cssText = "margin-top:6px;color:#8b949e;font-size:11px;";
      if (!resp || !resp.placed) {
        note.textContent = "Card not shown on the page" + (resp && resp.error ? `: ${resp.error}` : ".");
      } else if (resp.mode === "floating") {
        note.textContent = "Card shown bottom-right — couldn't match the exact "
          + "price text on this page to sit beside.";
      } else {
        return;                                  // placed inline, nothing to say
      }
      $out.appendChild(note);
    });
  }

  $out.innerHTML = rows.map(([k, v]) =>
    `<div class="row"><span class="k">${esc(k)}</span><span class="v">${v}</span></div>`
  ).join("") + renderSteps(p.run_log, totalSec ?? 0);
  $link.href = BASE;
  $link.style.display = "block";
}

async function extractCurrentTab() {
  const startedAt = Date.now();
  $go.disabled = true;
  $out.innerHTML = "";
  $link.style.display = "none";
  $bookBtn.style.display = "none";
  $bookPanel.style.display = "none";
  lastDeal = null;
  $status.textContent = "Reading the page…";

  const tab = await getActiveTab();
  if (!tab || !tab.url || !tab.url.startsWith("http")) {
    $status.textContent = "Open an OTA hotel/booking page first.";
    $go.disabled = false;
    return;
  }

  let data;
  try {
    data = await collectFromTab(tab.id);
  } catch (e) {
    $status.textContent = "Could not read the page (" + e.message + ") — try reloading the tab.";
    $go.disabled = false;
    return;
  }

  $status.textContent = "Sending to the local panel…";
  const body = {
    url: tab.url,
    render: false,
    resolve: true,
    page_data: {
      text: data.text, html: data.html,
      json_ld: data.jsonLd, xhr_json: data.xhrJson,
      final_url: data.finalUrl,
    },
  };

  let jobId;
  try {
    const r = await fetch(`${BASE}/api/extract`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const j = await r.json();
    if (!r.ok || j.error) throw new Error(j.error || `HTTP ${r.status}`);
    jobId = j.job_id;
  } catch (e) {
    $status.textContent = `Could not reach ${BASE} — is "python -m yta.web" running? (${e.message})`;
    $go.disabled = false;
    return;
  }

  $status.textContent = "Extracting…";
  poll(jobId, tab.id, startedAt);
}

$go.addEventListener("click", extractCurrentTab);

// "Book this rate with me" — build a human-readable summary of the matched
// deal and hand it to the user as a pre-filled WhatsApp message. This is
// deliberately NOT a real booking flow (no payment, no PII collection,
// nothing sent anywhere automatically) — it just saves the user retyping
// the details when they message the person who'll book it for them.
function dealSummaryText(d) {
  const meal = [d.mealBasis, d.refundable === true ? "refundable"
    : d.refundable === false ? "non-refundable" : null].filter(Boolean).join(" · ");
  const savings = (d.ourPrice != null)
    ? `\n💰 Best rate: ${d.tjCurrency} ${d.tjPrice}  (page showed: ${d.ourCurrency || ""} ${d.ourPrice})`
    : `\n💰 Best rate: ${d.tjCurrency} ${d.tjPrice}`;
  return `Hi! I'd like to book this via YourTravelAgent 🧳\n\n`
    + `🏨 ${d.hotelName || "—"}\n`
    + `🛏️ ${d.roomName || "—"}${meal ? " · " + meal : ""}\n`
    + `📅 ${d.checkIn || "?"} → ${d.checkOut || "?"}\n`
    + `👥 ${d.occupancy || "—"}` + savings
    + (d.url ? `\n\nOriginal page: ${d.url}` : "")
    + `\n\nPlease confirm and book this for me.`;
}

function waLink(text) {
  const digits = (WHATSAPP_NUMBER || "").replace(/\D/g, "");
  const base = digits ? `https://wa.me/${digits}` : "https://wa.me/";
  return `${base}?text=${encodeURIComponent(text)}`;
}

$bookBtn.addEventListener("click", () => {
  if (!lastDeal) return;
  const text = dealSummaryText(lastDeal);
  $bookSummary.textContent = text;
  $waLink.href = waLink(text);
  $bookPanel.style.display = "block";
});
