// YourTravelAgent popup — collects the active tab's booking data via
// bridge.js/capture.js and posts it to the SAME local endpoint the debug
// panel (yta/web.py) already serves: no separate server, no separate
// pipeline. `page_data` just takes priority over Playwright render there.
const BASE = "http://127.0.0.1:8765";

const $status = document.getElementById("status");
const $out = document.getElementById("out");
const $go = document.getElementById("go");
const $link = document.getElementById("panelLink");

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

    if (p.ota_benchmark.final_payable) {
      chrome.tabs.sendMessage(tabId, {
        type: "yta:showPrice",
        ourPrice: p.ota_benchmark.final_payable,
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
          note.textContent = "Card not shown on the page.";
        } else if (resp.mode === "floating") {
          note.textContent = "Card shown bottom-right — couldn't match the exact "
            + "price text on this page to sit beside.";
        } else {
          return;                                  // placed inline, nothing to say
        }
        $out.appendChild(note);
      });
    }
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
