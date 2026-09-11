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

function poll(jobId) {
  const t = setInterval(async () => {
    let s;
    try {
      s = await (await fetch(`${BASE}/api/job?id=${jobId}`)).json();
    } catch (e) {
      return;                                   // panel not reachable yet — keep trying
    }
    if (!s.done) {
      $status.textContent = `Extracting… (${(s.log || []).length} step(s))`;
      return;
    }
    clearInterval(t);
    $go.disabled = false;
    if (s.error) {
      $status.textContent = "Failed: " + s.error;
      return;
    }
    render(s.result);
  }, 500);
}

function render(result) {
  const p = result.packet;
  const rz = result.resolution || {};
  const ok = p.status === "ok";
  $status.textContent = ok ? "✓ Extracted"
    : "✗ Missing: " + (p.missing_mandatory || []).join(", ") + " — try pasting the page in the full panel.";

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

  $out.innerHTML = rows.map(([k, v]) =>
    `<div class="row"><span class="k">${esc(k)}</span><span class="v">${v}</span></div>`
  ).join("");
  $link.href = BASE;
  $link.style.display = "block";
}

async function extractCurrentTab() {
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
  poll(jobId);
}

$go.addEventListener("click", extractCurrentTab);
