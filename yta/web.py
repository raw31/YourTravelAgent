"""Phase-1 debug panel — a tiny local web UI over `yta.extract`.

    python -m yta.web            # then open http://127.0.0.1:8765

Paste an OTA booking URL, get the Booking Intent packet, the per-field
evidence trail, and validation warnings. Zero extra dependencies (stdlib
http.server). Localhost only — this is an internal review tool, not a
service.
"""
from __future__ import annotations

import base64
import json
import threading
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from yta.pipeline import extract
from yta.profiles import route

HOST, PORT = "127.0.0.1", 8765

# in-memory job registry: job_id -> {log: [...], done: bool, result / error}
_JOBS: dict = {}
_JOBS_LOCK = threading.Lock()


def _run_job(job_id: str, req: dict) -> None:
    job = _JOBS[job_id]
    try:
        url = (req.get("url") or "").strip()
        do_render = bool(req.get("render", True))
        paste = (req.get("paste") or "").strip() or None
        uploads = req.get("files") or []

        media = None
        if uploads:
            from yta.ingest import load_uploads
            media = load_uploads([
                {"name": u.get("name", ""), "mime": u.get("mime"),
                 "bytes": base64.b64decode(u["b64"])}
                for u in uploads if u.get("b64")
            ])

        if not media and (not urlparse(url).scheme or not urlparse(url).netloc):
            job["error"] = "Enter an absolute http(s) URL, or attach a PDF / screenshot"
            return

        is_html = bool(paste) and paste.lstrip()[:1] == "<"
        packet = extract(
            url, render=do_render, media=media, log_sink=job["log"],
            page_html=paste if (is_html and not media) else None,
            page_text=paste if (paste and not is_html and not media) else None,
        )

        resolution = None
        if req.get("resolve", True):
            if not packet.hotel.name:
                packet.log("TripJack lookup skipped — no hotel name extracted")
            else:
                resolution = _resolve(packet)

        job["result"] = {"adapter": route(url).name if url else "generic",
                         "packet": packet.to_dict(), "resolution": resolution}
    except Exception as e:  # noqa: BLE001
        job["error"] = f"{type(e).__name__}: {e}"
        job["trace"] = traceback.format_exc()
    finally:
        job["done"] = True

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>YourTravelAgent — extraction debug panel</title>
<style>
  :root {
    --bg:#0f1115; --panel:#181b22; --panel2:#1f232c; --line:#2b303b;
    --fg:#e6e8ec; --muted:#9aa3b2; --accent:#5b9dff;
    --warn:#f0b34e; --bad:#ef6b6b; --mono:ui-monospace,SFMono-Regular,Menlo,monospace;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
    font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  header { padding:18px 24px; border-bottom:1px solid var(--line); }
  header h1 { margin:0; font-size:15px; font-weight:600; letter-spacing:.02em; }
  header p { margin:4px 0 0; color:var(--muted); font-size:12px; }
  main { max-width:1000px; margin:0 auto; padding:24px; }
  form { display:flex; flex-direction:column; gap:10px; }
  textarea { width:100%; min-height:90px; resize:vertical; padding:12px;
    background:var(--panel); color:var(--fg); border:1px solid var(--line);
    border-radius:8px; font:12px/1.5 var(--mono); }
  .row { display:flex; gap:14px; align-items:center; flex-wrap:wrap; }
  button { background:var(--accent); color:#04102a; border:0; border-radius:8px;
    padding:9px 18px; font-weight:600; cursor:pointer; }
  button:disabled { opacity:.5; cursor:default; }
  label.chk { color:var(--muted); display:flex; gap:6px; align-items:center; cursor:pointer; }
  .adapter { color:var(--muted); font-size:12px; }
  .adapter b { color:var(--accent); }
  section { margin-top:22px; background:var(--panel); border:1px solid var(--line);
    border-radius:10px; overflow:hidden; }
  section > h2 { margin:0; padding:10px 16px; font-size:12px; font-weight:600;
    text-transform:uppercase; letter-spacing:.06em; color:var(--muted);
    background:var(--panel2); border-bottom:1px solid var(--line); }
  .body { padding:14px 16px; }
  .grid { display:grid; grid-template-columns:140px 1fr; gap:6px 16px; }
  .grid div:nth-child(odd) { color:var(--muted); }
  .null { color:#5b6270; font-style:italic; }
  table { width:100%; border-collapse:collapse; font-size:12.5px; }
  th,td { text-align:left; padding:6px 10px; border-bottom:1px solid var(--line); }
  th { color:var(--muted); font-weight:500; }
  td.mono, .grid .mono { font-family:var(--mono); font-size:12px; }
  .src { font-size:11px; padding:1px 7px; border-radius:20px; border:1px solid var(--line); }
  .src-url { color:#7fd7a6; border-color:#2f5c44; }
  .src-inferred { color:#f0b34e; border-color:#5c4a2f; }
  .src-network { color:#5b9dff; border-color:#2f4a6b; }
  .src-llm { color:#c58bff; border-color:#4a2f6b; }
  .band { font-size:11px; padding:2px 9px; border-radius:20px; margin-left:8px;
    text-transform:uppercase; letter-spacing:.04em; vertical-align:middle; }
  .band-high { background:#1f4a34; color:#7fd7a6; }
  .band-medium { background:#4d3f1c; color:#f0b34e; }
  .band-low { background:#4a2f2f; color:#ef9b9b; }
  .band-none { background:#333; color:#9aa3b2; }
  .status { display:flex; align-items:center; gap:10px; padding:12px 16px;
    border-radius:10px; margin-top:18px; font-weight:600; }
  .status-ok { background:#16301f; color:#7fd7a6; border:1px solid #2f5c44; }
  .status-fail { background:#3a1d1d; color:#ef9b9b; border:1px solid #6b2f2f; }
  .status .missing { font-weight:400; color:var(--fg); font-size:12.5px; }
  .conf { font-family:var(--mono); }
  .warn-list { margin:0; padding-left:18px; }
  .warn-list li { color:var(--warn); margin:3px 0; }
  pre.raw { margin:0; padding:14px 16px; overflow:auto; font:12px/1.5 var(--mono);
    color:var(--fg); background:#12151b; max-height:420px; }
  .err { color:var(--bad); font-family:var(--mono); white-space:pre-wrap; }
  details summary { cursor:pointer; color:var(--muted); padding:10px 16px;
    background:var(--panel2); }
  .muted { color:var(--muted); }
  .rooms { margin-top:4px; font-size:12.5px; }
  .rooms > div { padding:1px 0; }
  table.log { width:100%; border-collapse:collapse; font-size:12.5px; }
  table.log td { padding:4px 12px; border-bottom:1px solid var(--line);
    vertical-align:top; }
  table.log td:first-child { white-space:pre; width:80px; text-align:right; }
  .spin { display:inline-block; color:var(--accent); animation:blink 1s steps(2) infinite; }
  @keyframes blink { 50% { opacity:0.2; } }
</style>
</head>
<body>
<header>
  <h1>YourTravelAgent — extraction debug panel</h1>
  <p>Paste an OTA booking URL (MakeMyTrip / Booking.com / Agoda / other). Phase-1 URL extraction.</p>
</header>
<main>
  <form id="f">
    <textarea id="url" placeholder="https://secure.booking.com/book.html?..." autofocus></textarea>
    <details id="pastebox">
      <summary class="muted">or paste the page's text / HTML (for expired checkout links — copy from the live tab)</summary>
      <textarea id="paste" placeholder="Select-all + copy on the open booking page, paste here. Leave URL above filled in too."></textarea>
    </details>
    <div class="row">
      <label class="chk">📎 upload PDF / screenshots of the page
        <input type="file" id="files" accept="image/*,.pdf,application/pdf" multiple></label>
      <span class="muted" id="filenames"></span>
    </div>
    <div class="row">
      <button id="go" type="submit">Extract</button>
      <label class="chk"><input type="checkbox" id="render" checked> render page + LLM parse (~20-40s)</label>
      <span class="adapter" id="adapter"></span>
    </div>
  </form>
  <div id="out"></div>
</main>
<script>
const $ = s => document.querySelector(s);
const esc = s => String(s).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const val = v => (v === null || v === undefined || v === '' || (Array.isArray(v) && !v.length))
  ? '<span class="null">null</span>' : esc(Array.isArray(v) ? v.join(', ') : v);

const readB64 = f => new Promise((res, rej) => {
  const r = new FileReader();
  r.onload = () => res({name: f.name, mime: f.type, b64: String(r.result).split(',')[1]});
  r.onerror = rej;
  r.readAsDataURL(f);
});

$('#files').addEventListener('change', () => {
  const fs = [...$('#files').files];
  $('#filenames').textContent = fs.length ? fs.map(f => f.name).join(', ') : '';
});

function log_section(entries, running) {
  const spin = running ? ' <span class="spin">▍</span>' : '';
  return `<section><details open><summary>Logs — ${entries.length} step(s)${spin}</summary>
    <table class="log">${entries.map(l => `<tr>
      <td class="mono muted">${String(l.ms).padStart(6)} ms</td>
      <td>${esc(l.msg)}</td></tr>`).join('')}</table></details></section>`;
}

$('#f').addEventListener('submit', async e => {
  e.preventDefault();
  const url = $('#url').value.trim();
  const paste = $('#paste').value.trim();
  const fileList = [...$('#files').files];
  if (!url && !fileList.length) { render_error('Enter a URL or attach a PDF / screenshot.'); return; }
  $('#go').disabled = true;
  $('#go').textContent = 'Working…';
  $('#adapter').textContent = ''; $('#out').innerHTML = log_section([], true);

  let jobId;
  try {
    const files = await Promise.all(fileList.map(readB64));
    const r = await fetch('/api/extract', {
      method:'POST', headers:{'content-type':'application/json'},
      body: JSON.stringify({url, render: $('#render').checked, paste, files})
    });
    const j = await r.json();
    if (!r.ok || j.error) { render_error(j.error || ('HTTP '+r.status)); $('#go').disabled=false; $('#go').textContent='Extract'; return; }
    jobId = j.job_id;
  } catch (err) { render_error(err.message); $('#go').disabled=false; $('#go').textContent='Extract'; return; }

  // poll the running job — show the log live, then the full result
  const poll = setInterval(async () => {
    let s;
    try { s = await (await fetch('/api/job?id=' + jobId)).json(); }
    catch (err) { return; }
    if (!s.done) {
      $('#out').innerHTML = log_section(s.log || [], true);
      return;
    }
    clearInterval(poll);
    $('#go').disabled = false; $('#go').textContent = 'Extract';
    if (s.error) { render_error(s.error, s.trace); return; }
    render(s.result);
  }, 400);
});

function render_error(msg, trace) {
  $('#out').innerHTML = `<section><h2>Error</h2><div class="body err">${esc(msg)}${trace ? '\\n\\n'+esc(trace) : ''}</div></section>`;
}

function render(d) {
  const p = d.packet;
  $('#adapter').innerHTML = `adapter: <b>${esc(d.adapter)}</b> &nbsp;·&nbsp; method: ${esc(p.source.extraction_method)}`;
  const s = p.stay, h = p.hotel, o = p.requested_offer, b = p.ota_benchmark;
  let occ;
  if (s.occupancy && s.occupancy.length) {
    occ = `${s.occupancy.length} room(s), ${s.adults||'?'} adult(s)`
      + (s.children ? `, ${s.children} child` : '')
      + '<div class="rooms">' + s.occupancy.map((r,i) => {
          const kids = r.children ? ` + ${r.children} child`
            + (r.child_ages && r.child_ages.length ? ` (age ${r.child_ages.join(', ')})` : '') : '';
          return `<div><span class="muted">Room ${i+1}</span> — ${r.adults||'?'} adult${kids}</div>`;
        }).join('') + '</div>';
  } else {
    occ = [s.rooms && s.rooms+' room(s)', s.adults && s.adults+' adult(s)',
      s.children ? s.children+' child' : null].filter(Boolean).join(', ');
  }

  const st = p.status === 'ok'
    ? `<div class="status status-ok">✓ OK — all mandatory fields present</div>`
    : `<div class="status status-fail">✗ FAIL
        <span class="missing">missing: ${(p.missing_mandatory||[]).map(esc).join(', ')}</span></div>`;

  let html = st + `
  <section><h2>Summary</h2><div class="body"><div class="grid">
    <div>Hotel</div><div>${val(h.name)} ${h.city ? '<span class="muted">· '+esc(h.city)+'</span>':''}</div>
    <div>Stay</div><div>${val(s.check_in)} → ${val(s.check_out)} ${s.nights ? '<span class="muted">('+s.nights+'n)</span>':''}</div>
    <div>Occupancy</div><div>${occ || '<span class="null">null</span>'}</div>
    <div>Room</div><div>${val(o.room_name)}</div>
    <div>Meal / cancel</div><div>${val(o.meal_plan)} <span class="muted">/</span> ${val(o.cancellation)}</div>
    <div>Price</div><div>${val(b.final_payable)} ${val(b.currency)}</div>
    <div>Source IDs</div><div class="mono">hotel=${val(p.source.source_hotel_id)} room=${val(p.source.source_room_id)}</div>
  </div></div></section>`;

  html += render_resolution(d.resolution);

  html += `<section><h2>Evidence — ${p.evidence.length} field(s)</h2>
    <table><tr><th>Field</th><th>Value</th><th>Source</th><th>Conf.</th><th>Pointer</th></tr>
    ${p.evidence.map(e => `<tr>
      <td class="mono">${esc(e.field)}</td>
      <td>${val(e.value)}</td>
      <td><span class="src src-${esc(e.source)}">${esc(e.source)}</span></td>
      <td class="conf">${e.confidence}</td>
      <td class="mono muted">${e.pointer ? esc(e.pointer) : ''}</td></tr>`).join('')}
    </table></section>`;

  if (p.warnings.length) html += `<section><h2>Warnings — ${p.warnings.length}</h2>
    <div class="body"><ul class="warn-list">${p.warnings.map(w=>`<li>${esc(w)}</li>`).join('')}</ul></div></section>`;

  if (p.run_log && p.run_log.length) html += log_section(p.run_log, false);

  html += `<section><details><summary>Raw packet JSON</summary>
    <pre class="raw">${esc(JSON.stringify(p, null, 2))}</pre></details></section>`;

  $('#out').innerHTML = html;
}

function render_resolution(rz) {
  if (!rz) return '';
  if (!rz.available)
    return `<section><h2>TripJack match</h2><div class="body muted">${esc(rz.note || 'unavailable')}</div></section>`;

  const m = rz.match;
  const found = !!m;
  let head = `<section><h2>TripJack match
      <span class="band ${found ? 'band-' + rz.band : 'band-none'}">${found ? esc(rz.band) : 'not found'}</span>
      <span class="muted" style="font-weight:400"> ${rz.ms} ms</span></h2><div class="body">`;

  if (found) {
    head += `<div class="grid">
      <div>tj_id</div><div class="mono" style="font-size:14px">${esc(m.tj_id)}</div>
      <div>unica_id</div><div class="mono">${val(m.unica_id)}</div>
      <div>Hotel name</div><div>${esc(m.hotel_name)}</div>
      <div>Locality</div><div>${val(m.region_name)} <span class="muted">· ${val(m.country_name)}</span> ${m.rating ? '· '+m.rating+'★' : ''}</div>
      <div>Score</div><div class="mono">${m.score}
        <span class="muted">(name ${m.name_score}${m.geo_score!=null ? ' · geo '+m.geo_score : ''}${m.city_score!=null ? ' · city '+m.city_score : ''}${m.distance_m!=null ? ' · '+m.distance_m+' m' : ''})</span></div>
    </div>`;
  } else {
    const best = (rz.candidates || [])[0];
    head += `<div class="muted">No confident match`
      + (best ? ` — best candidate scored <span class="mono">${best.score}</span> (need ≥ 0.75).` : '.')
      + `</div>`;
  }

  if (rz.notes && rz.notes.length)
    head += `<ul class="warn-list" style="margin-top:10px">${rz.notes.map(n=>`<li>${esc(n)}</li>`).join('')}</ul>`;

  // when nothing was confident enough, show the discarded candidates in full
  const cand = found ? (rz.candidates || []).slice(1) : (rz.candidates || []);
  if (cand.length) {
    const tbl = `<table><tr><th>tj_id</th><th>unica_id</th><th>name</th><th>locality · country</th><th>score</th><th>name</th><th>geo</th><th>dist</th></tr>
      ${cand.map(c=>`<tr>
        <td class="mono">${esc(c.tj_id)}</td>
        <td class="mono muted">${val(c.unica_id)}</td>
        <td>${esc(c.hotel_name)}</td>
        <td class="muted">${val(c.region_name)} · ${val(c.country_name)}</td>
        <td class="mono">${c.score}</td>
        <td class="mono muted">${c.name_score}</td>
        <td class="mono muted">${c.geo_score!=null ? c.geo_score : ''}</td>
        <td class="mono muted">${c.distance_m!=null ? Math.round(c.distance_m)+'m' : ''}</td></tr>`).join('')}
      </table>`;
    head += found
      ? `<details><summary>${cand.length} other candidate(s)</summary>${tbl}</details>`
      : `<div style="margin-top:10px"><div class="muted" style="margin-bottom:4px">discarded candidates (${cand.length}):</div>${tbl}</div>`;
  }
  if (rz.detail_request) {
    const dr = rz.detail_request;
    head += `<details><summary>TripJack Detail request — POST ${esc(dr.url)}</summary>
      <pre class="raw">${esc(JSON.stringify(dr.body, null, 2))}</pre></details>`;
  } else if (rz.detail_request_error) {
    head += `<div class="muted" style="margin-top:8px">Detail request not buildable: ${esc(rz.detail_request_error)}</div>`;
  }
  if (rz.detail) {
    const dt = rz.detail, opts = dt.options || [];
    head += `<div style="margin-top:14px"><h3 style="margin:0 0 6px">TripJack live options
      <span class="muted" style="font-weight:400">· ${esc(dt.hotel_name||'')} · ${opts.length} option(s)</span></h3>`;
    if (dt.notes && dt.notes.length)
      head += `<ul class="warn-list">${dt.notes.map(n=>`<li>${esc(n)}</li>`).join('')}</ul>`;
    if (opts.length) {
      head += `<table><tr><th>room</th><th>meal</th><th>refundable</th><th>free-cancel until</th><th>total</th><th>type</th></tr>
        ${opts.map(o=>`<tr>
          <td>${esc((o.rooms||[]).map(r=>r.name).join(' + ')||'—')}</td>
          <td>${esc(o.meal_basis||'—')}</td>
          <td>${o.refundable ? 'yes' : 'no'}</td>
          <td class="muted">${val(o.free_cancel_until)}</td>
          <td class="mono">${esc(o.currency||'')} ${o.total_price}</td>
          <td class="mono muted">${esc(o.option_type||'')}</td></tr>`).join('')}
        </table>`;
    }
    head += `<details><summary>raw pricing response</summary><pre class="raw">${esc(JSON.stringify(dt, null, 2))}</pre></details></div>`;
  } else if (rz.detail_error) {
    head += `<div class="muted" style="margin-top:8px">TripJack pricing call failed: ${esc(rz.detail_error)}</div>`;
  }
  if (rz.room_map) {
    const rm = rz.room_map;
    head += `<div style="margin-top:14px"><h3 style="margin:0 0 6px">Room → rate-plan mapping
      <span class="band ${rm.matched ? 'band-'+(rm.band==='strong'?'high':'medium') : 'band-none'}">${rm.matched ? esc(rm.band) : 'no match'}</span>
      ${rm.llm_used ? '<span class="muted" style="font-weight:400">· LLM tie-break</span>' : ''}</h3>`;
    if (rm.matched)
      head += `<div class="muted" style="margin-bottom:6px">room_type_id <span class="mono">${esc(rm.room_type_id)}</span> · score <span class="mono">${rm.score}</span>
        ${rm.meal_filter ? '· meal <span class="mono">'+esc(rm.meal_filter)+'</span>' : ''}
        ${rm.refundable_filter!=null ? '· '+(rm.refundable_filter?'refundable':'non-refundable') : ''}</div>`;
    if (rm.view_flag)
      head += `<div class="muted" style="margin-bottom:6px">⚑ ${esc(rm.view_flag)}</div>`;
    if (rm.notes && rm.notes.length)
      head += `<ul class="warn-list">${rm.notes.map(n=>`<li>${esc(n)}</li>`).join('')}</ul>`;
    if ((rm.rate_options||[]).length) {
      head += `<table><tr><th>optionId</th><th>room</th><th>meal</th><th>total</th><th>tags</th></tr>
        ${rm.rate_options.map(o=>`<tr>
          <td class="mono">${esc((o.option_id||'').slice(0,8))}</td>
          <td>${esc(o.room_name)}</td>
          <td>${esc(o.meal_basis)}</td>
          <td class="mono">${esc(o.currency)} ${o.total_price}</td>
          <td class="muted">${(o.tags||[]).map(esc).join(', ')}</td></tr>`).join('')}
        </table>`;
    }
    head += `<details><summary>all ${(rm.ranked_buckets||[]).length} room-type buckets (ranked)</summary>
      <table><tr><th>room_type_id</th><th>best-matched name</th><th>score</th><th>band</th><th>#opt</th></tr>
      ${(rm.ranked_buckets||[]).map(b=>`<tr>
        <td class="mono">${esc(b.room_type_id)}</td><td>${esc(b.canonical)}</td>
        <td class="mono">${b.score}</td><td class="muted">${esc(b.band)}</td>
        <td class="mono">${b.n_options}</td></tr>`).join('')}
      </table></details></div>`;
  }
  head += `</div><details><summary>cascade trace</summary><pre class="raw">${esc((rz.layers||[]).join('\\n'))}</pre></details>`;
  return head + `</section>`;
}
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if self.path.startswith("/api/job"):
            jid = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
            job = _JOBS.get(jid)
            if not job:
                self._send(404, b'{"error":"unknown job"}')
                return
            out = {"done": job["done"], "log": list(job["log"])}
            if job["done"]:
                out["result"] = job.get("result")
                out["error"] = job.get("error")
                out["trace"] = job.get("trace")
            self._send(200, json.dumps(out, default=str).encode("utf-8"))
            return
        self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        if self.path != "/api/extract":
            self._send(404, b'{"error":"not found"}')
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:  # noqa: BLE001
            self._send(400, json.dumps({"error": f"bad request: {e}"}).encode())
            return

        job_id = uuid.uuid4().hex[:12]
        with _JOBS_LOCK:
            _JOBS[job_id] = {"log": [], "done": False}
            for old in [k for k in _JOBS if _JOBS[k]["done"]][:-15]:
                _JOBS.pop(old, None)          # keep the last ~15 finished jobs
        threading.Thread(target=_run_job, args=(job_id, req), daemon=True).start()
        self._send(202, json.dumps({"job_id": job_id}).encode())

    def log_message(self, fmt, *args):  # quieter console
        return


def _resolve(packet) -> dict:
    """Run the TripJack hotel-id lookup; degrade gracefully if the DB is
    not built or the resolver errors. Logs its steps into packet.run_log."""
    try:
        from yta.hoteldb.db import db_path
        if not db_path().exists():
            packet.log("TripJack lookup skipped — data/hotels.db not built")
            return {"available": False,
                    "note": "TripJack DB not built — run "
                            "`python -m yta.hoteldb.load <dump.xlsx>`"}
        from yta.hoteldb.link import resolve_packet
        import time
        h = packet.hotel
        packet.log(f"TripJack resolve: name={h.name!r} city={h.city!r} "
                   f"lat/lng={h.lat},{h.lng}")
        t = time.perf_counter()
        r = resolve_packet(packet)
        ms = round((time.perf_counter() - t) * 1000, 1)
        for L in getattr(r, "layers", []):
            packet.log(f"  {L}")
        m = r.match
        packet.log(f"TripJack → {r.band}"
                   + (f": tj_id={m.tj_id} unica_id={m.unica_id} "
                      f"'{m.hotel_name}' (score {m.score})" if m else " — no match")
                   + f"  [{ms} ms]")
        d = r.to_dict()
        d["available"] = True
        d["ms"] = ms

        # the Detail/Pricing request we'd send for a confident match
        if m:
            try:
                from yta.tripjack.hotel import pricing_request_from_packet
                d["detail_request"] = pricing_request_from_packet(packet, m.tj_id)
                packet.log(f"built TripJack Detail request for tj_id {m.tj_id}")
            except ValueError as e:
                d["detail_request_error"] = str(e)
                packet.log(f"TripJack Detail request not buildable: {e}")

        # actually hit POST /hms/v3/hotel/pricing for a confident match
        if m and r.band in ("high", "medium") and "detail_request" in d:
            try:
                from yta.tripjack.client import TripJackClient, TripJackError
                from yta.tripjack.hotel import hotel_options
                client = TripJackClient.from_env()
                if not client.configured():
                    packet.log("TripJack pricing skipped — TRIPJACK_API_KEY not set")
                else:
                    s = packet.stay
                    t2 = time.perf_counter()
                    det = hotel_options(
                        m.tj_id, s.check_in, s.check_out,
                        s.occupancy or [{"adults": s.adults or 2,
                                         "children": s.children or 0,
                                         "child_ages": s.child_ages or []}],
                        currency=packet.ota_benchmark.currency or "INR",
                        client=client)
                    pms = round((time.perf_counter() - t2) * 1000, 1)
                    d["detail"] = det.to_dict()
                    packet.log(f"TripJack pricing → {len(det.options)} option(s)"
                               + (f"; {det.notes[0]}" if det.notes else "")
                               + f"  [{pms} ms]")

                    # map the OTA requested offer onto a TJ ratekey
                    if det.options:
                        from yta.roommap import map_rooms
                        rm = map_rooms(
                            det.options, packet.requested_offer,
                            benchmark_price=packet.ota_benchmark.final_payable,
                            policy=packet.matching_policy, log=packet.log)
                        d["room_map"] = rm.to_dict()
                        packet.log(
                            f"room map → {'matched ' + str(rm.room_type_id) if rm.matched else 'no match'}"
                            f" [{rm.band}]; {len(rm.rate_options)} rate option(s)"
                            + ("; LLM used" if rm.llm_used else ""))
            except TripJackError as e:
                d["detail_error"] = str(e)
                packet.log(f"TripJack pricing error: {e}")
            except Exception as e:  # noqa: BLE001
                d["detail_error"] = f"{type(e).__name__}: {e}"
                packet.log(f"TripJack pricing error: {type(e).__name__}: {e}")
        return d
    except Exception as e:  # noqa: BLE001
        packet.log(f"TripJack lookup error: {type(e).__name__}: {e}")
        return {"available": False, "note": f"{type(e).__name__}: {e}"}


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"YourTravelAgent debug panel → http://{HOST}:{PORT}  (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
