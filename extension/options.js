// YourTravelAgent admin page — adjusts the markup applied on top of
// TripJack's price wherever it's quoted outward (the on-page comparison
// card, the WhatsApp "book this rate" summary). Lives on the same
// chrome-extension://<id>/ origin as popup.html, so localStorage here is
// the exact same store popup.js reads from — no messaging needed.
const $pct = document.getElementById("markupPct");
const $flat = document.getElementById("markupFlat");
const $saved = document.getElementById("savedTag");
const $pvCost = document.getElementById("pvCost");
const $pvSell = document.getElementById("pvSell");

const EXAMPLE_COST = 10000;
let savedTimer = null;

function load() {
  try {
    return {
      pct: parseFloat(localStorage.getItem("yta_markup_pct")) || 0,
      flat: parseFloat(localStorage.getItem("yta_markup_flat")) || 0,
    };
  } catch (e) {
    return { pct: 0, flat: 0 };
  }
}

function save() {
  const pct = parseFloat($pct.value) || 0;
  const flat = parseFloat($flat.value) || 0;
  try {
    localStorage.setItem("yta_markup_pct", String(pct));
    localStorage.setItem("yta_markup_flat", String(flat));
  } catch (e) { /* private browsing or storage disabled — settings won't persist */ }
  flashSaved();
  updatePreview(pct, flat);
}

function flashSaved() {
  $saved.classList.add("show");
  clearTimeout(savedTimer);
  savedTimer = setTimeout(() => $saved.classList.remove("show"), 1200);
}

function fmt(n) {
  return "₹ " + Math.round(n).toLocaleString("en-IN");
}

function updatePreview(pct, flat) {
  const sell = EXAMPLE_COST * (1 + pct / 100) + flat;
  $pvCost.textContent = fmt(EXAMPLE_COST);
  $pvSell.textContent = fmt(sell);
}

const initial = load();
if (initial.pct) $pct.value = initial.pct;
if (initial.flat) $flat.value = initial.flat;
updatePreview(initial.pct, initial.flat);

$pct.addEventListener("input", save);
$flat.addEventListener("input", save);
