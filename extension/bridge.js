// YourTravelAgent — ISOLATED-world bridge.
//
// capture.js runs in the page's own JS realm (world:"MAIN") so it can patch
// fetch/XHR; this script runs in the extension's normal isolated world, the
// only place with access to chrome.runtime. The two talk over the one
// channel they share: DOM CustomEvents on `document`.
(() => {
  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (!msg || msg.type !== "yta:collect") return false;

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

    // capture.js answers synchronously (same tick), but keep the message
    // channel open in case a future version of it goes async.
    return true;
  });
})();
