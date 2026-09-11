// YourTravelAgent — MAIN-world capture.
//
// Runs in the PAGE's own JS realm (manifest "world":"MAIN", document_start —
// before the OTA's own scripts run) so it can patch fetch/XHR and see every
// request/response the page makes with its real, logged-in session. This
// mirrors yta/render.py's render() + _scan_embedded_state() byte-for-byte —
// same shapes, same caps, same "is this worth keeping" heuristics — so the
// server-side _json_digest()/llm_context() code needs zero changes to
// consume either source. Keep the two in sync if you tune one.
//
// Output entries: {url, kind: "request"|"response"|"embedded", body}
// (identical to RenderResult.xhr_json in yta/render.py).
(() => {
  if (window.__ytaCaptureInstalled) return;
  window.__ytaCaptureInstalled = true;

  const MAX_XHR = 22;                 // render.py _MAX_XHR
  const MAX_BYTES = 60000;            // render.py _MAX_XHR_BYTES
  const MAX_EMBED = 4;                // render.py _scan_embedded_state cap

  // render.py _API_URL / _default_xhr_keep, ported 1:1
  const API_URL_RE = /\/(api|graphql|gql|bff|mapi|rest|v\d|orchestrator|gateway|service)[/?]|\.(json|api)\b/i;
  const KEEP_RE = /\/api\/|mapi|graphql|bff|booking|hotel|room|price|tariff|detail/i;
  const NOISE_RE = /track|telemetry|analytics|pixel|beacon|consent|gtm|doubleclick|\/ads|awswaf/i;
  // render.py _STATE_SCRIPT
  const STATE_RE = /(?:window\.)?(?:__NEXT_DATA__|__INITIAL_STATE__|__PRELOADED_STATE__|__APOLLO_STATE__|__NUXT__|__data|__INITIAL_DATA__|__REDUX_STATE__|__STATE__)\s*=\s*(\{[\s\S]*?\})\s*[;<]/g;

  const buf = [];

  function keepUrl(u) {
    try {
      const ul = String(u).toLowerCase();
      const sameHost = ul.includes(location.hostname.toLowerCase());
      if (!(sameHost || KEEP_RE.test(ul))) return false;
      return !NOISE_RE.test(ul);
    } catch (e) { return false; }
  }

  function push(url, kind, body) {
    if (buf.length >= MAX_XHR) return;
    try { buf.push({ url: String(url).split("?")[0], kind, body }); } catch (e) {}
  }

  function tryParseBody(raw) {
    if (!raw || raw.length > MAX_BYTES) return null;
    const t = raw.trim();
    if (!t) return null;
    if (t[0] === "{" || t[0] === "[") {
      try { return JSON.parse(t); } catch (e) { return null; }
    }
    if (t.includes("=") && t.includes("&")) {          // form-encoded
      try {
        const o = {};
        for (const [k, v] of new URLSearchParams(t).entries()) o[k] = v;
        return Object.keys(o).length ? o : null;
      } catch (e) { return null; }
    }
    return null;
  }

  function queryAsBody(url) {
    try {
      const qs = new URL(url, location.href).searchParams;
      const o = {};
      for (const [k, v] of qs.entries()) o[k] = v;
      return Object.keys(o).length ? o : null;
    } catch (e) { return null; }
  }

  // -- fetch patch --------------------------------------------------
  const origFetch = window.fetch;
  if (origFetch) {
    window.fetch = function (input, init) {
      let url = "";
      try { url = typeof input === "string" ? input : (input && input.url) || ""; } catch (e) {}
      const method = String((init && init.method) || (input && input.method) || "GET").toUpperCase();
      try {
        if (keepUrl(url)) {
          if (["POST", "PUT", "PATCH"].includes(method) && init && typeof init.body === "string") {
            const b = tryParseBody(init.body);
            if (b) push(url, "request", b);
          } else if (method === "GET" && url.includes("?") && API_URL_RE.test(url)) {
            const b = queryAsBody(url);
            if (b) push(url, "request", b);
          }
        }
      } catch (e) {}
      const p = origFetch.apply(this, arguments);
      try {
        p.then((resp) => {
          try {
            if (keepUrl(url) && (resp.headers.get("content-type") || "").includes("application/json")) {
              resp.clone().text().then((t) => {
                const b = tryParseBody(t);
                if (b) push(url, "response", b);
              }).catch(() => {});
            }
          } catch (e) {}
        }).catch(() => {});
      } catch (e) {}
      return p;
    };
  }

  // -- XMLHttpRequest patch -----------------------------------------
  const OrigXHR = window.XMLHttpRequest;
  if (OrigXHR) {
    const origOpen = OrigXHR.prototype.open;
    const origSend = OrigXHR.prototype.send;
    OrigXHR.prototype.open = function (method, url) {
      this.__ytaMethod = String(method || "GET").toUpperCase();
      this.__ytaUrl = url;
      return origOpen.apply(this, arguments);
    };
    OrigXHR.prototype.send = function (body) {
      try {
        const url = this.__ytaUrl, method = this.__ytaMethod;
        if (url && keepUrl(url)) {
          if (["POST", "PUT", "PATCH"].includes(method) && typeof body === "string") {
            const b = tryParseBody(body);
            if (b) push(url, "request", b);
          } else if (method === "GET" && url.includes("?") && API_URL_RE.test(url)) {
            const b = queryAsBody(url);
            if (b) push(url, "request", b);
          }
          this.addEventListener("load", function () {
            try {
              const ct = this.getResponseHeader("content-type") || "";
              if (ct.includes("application/json")) {
                const b = tryParseBody(this.responseText);
                if (b) push(url, "response", b);
              }
            } catch (e) {}
          });
        }
      } catch (e) {}
      return origSend.apply(this, arguments);
    };
  }

  // -- embedded SPA state + JSON-LD, scanned on demand ---------------
  function looksBookingish(obj) {
    let blob = "";
    try { blob = JSON.stringify(obj).slice(0, 20000).toLowerCase(); } catch (e) { return false; }
    const keys = ["room", "adult", "child", "occup", "checkin", "check_in",
                 "guest", "price", "hotel", "night", "cancel"];
    let n = 0;
    for (const k of keys) if (blob.includes(k)) n++;
    return n >= 3;
  }

  function collectEmbedded() {
    const out = [];
    document.querySelectorAll('script[type="application/json"], script#__NEXT_DATA__')
      .forEach((node) => {
        if (out.length >= MAX_EMBED) return;
        try {
          const raw = node.textContent;
          if (!raw || raw.length > MAX_BYTES) return;
          const body = JSON.parse(raw);
          if (looksBookingish(body)) out.push({ url: "embedded:script-json", kind: "embedded", body });
        } catch (e) {}
      });
    if (out.length < MAX_EMBED) {
      try {
        const html = document.documentElement.outerHTML;
        STATE_RE.lastIndex = 0;
        let m;
        while (out.length < MAX_EMBED && (m = STATE_RE.exec(html))) {
          const blob = m[1];
          if (blob.length > MAX_BYTES) continue;
          try {
            const body = JSON.parse(blob);
            if (looksBookingish(body)) out.push({ url: "embedded:window-state", kind: "embedded", body });
          } catch (e) {}
        }
      } catch (e) {}
    }
    return out;
  }

  function collectJsonLd() {
    const out = [];
    document.querySelectorAll('script[type="application/ld+json"]').forEach((node) => {
      try { out.push(JSON.parse(node.textContent)); } catch (e) {}
    });
    return out;
  }

  // bridge.js (isolated world) asks for the current buffer on click; we
  // answer on the DOM (the only channel MAIN and ISOLATED worlds share).
  document.addEventListener("yta:collect", () => {
    const xhrJson = buf.slice(0, MAX_XHR).concat(collectEmbedded());
    const payload = {
      url: location.href,
      finalUrl: location.href,
      text: (document.body && document.body.innerText) || "",
      html: (document.documentElement && document.documentElement.outerHTML || "").slice(0, 500000),
      jsonLd: collectJsonLd(),
      xhrJson,
    };
    // stringify before crossing the world boundary — plain strings survive
    // the isolated/MAIN world split cleanly, objects can get realm-mangled.
    let detail;
    try { detail = JSON.stringify(payload); } catch (e) { detail = "{}"; }
    document.dispatchEvent(new CustomEvent("yta:collected", { detail }));
  });
})();
