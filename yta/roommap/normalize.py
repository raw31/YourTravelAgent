"""Room-name normalisation + similarity scoring.

Ported verbatim from the production MMT room-mapping service
(`HotelBookingFlowUIScraperService/app/services/mmt/room_normalization.py`).
DOM-free — depends only on `rapidfuzz` and a config object. View is NOT
scored here; it is split off (`split_name_and_view`) and verified separately.

Reference: `~/mmtroommapping.md`.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

from rapidfuzz import fuzz, distance


# -- tunables (env-overridable, same names as the production config) ----------

@dataclass(frozen=True)
class RoomMatchConfig:
    MIN_BASE_SCORE: float = float(os.getenv("ROOM_MATCH_MIN_BASE_SCORE", 0.85))
    VIEW_CHECK_SCORE_THRESHOLD: float = float(
        os.getenv("ROOM_VIEW_CHECK_SCORE_THRESHOLD", 0.80))

    TEXT_WEIGHTS_FUZZY: float = float(os.getenv("ROOM_MATCH_TEXT_WEIGHTS_FUZZY", 0.4))
    TEXT_WEIGHTS_JACCARD: float = float(os.getenv("ROOM_MATCH_TEXT_WEIGHTS_JACCARD", 0.3))
    TEXT_WEIGHTS_LEVENSHTEIN: float = float(
        os.getenv("ROOM_MATCH_TEXT_WEIGHTS_LEVENSHTEIN", 0.3))

    ATTRIBUTE_WEIGHT_CATEGORY: float = float(
        os.getenv("ROOM_MATCH_ATTRIBUTE_WEIGHT_CATEGORY", 0.6))
    ATTRIBUTE_WEIGHT_BED: float = float(os.getenv("ROOM_MATCH_ATTRIBUTE_WEIGHT_BED", 0.4))

    COMBINED_WEIGHT_TEXT: float = float(os.getenv("ROOM_MATCH_COMBINED_WEIGHT_TEXT", 0.6))
    COMBINED_WEIGHT_ATTRIBUTES: float = float(
        os.getenv("ROOM_MATCH_COMBINED_WEIGHT_ATTRIBUTES", 0.4))

    THRESHOLD_STRONG: float = float(os.getenv("ROOM_MATCH_THRESHOLD_STRONG", 0.85))
    THRESHOLD_GOOD: float = float(os.getenv("ROOM_MATCH_THRESHOLD_GOOD", 0.70))
    THRESHOLD_WEAK: float = float(os.getenv("ROOM_MATCH_THRESHOLD_WEAK", 0.55))

    # top-2 buckets within this delta -> hand off to the LLM tie-breaker
    LLM_TIEBREAK_DELTA: float = float(os.getenv("ROOM_MATCH_LLM_TIEBREAK_DELTA", 0.05))


CONFIG = RoomMatchConfig()


class RoomNormalizationService:
    """Normalises room names and computes a blended similarity score."""

    PUNCT_PATTERN = re.compile(
        r"[,\.;:\-\—\–_/\\\|\(\)\[\]\{\}!?\@\#\$\%\^\&\*\+\=\"\'‘’“”"
        r"¢£¥©®™•…·†‡°]+"
    )

    # Canonical recognised view phrases. `split_name_and_view()` reuses these
    # to strip a view suffix out of a room name before scoring.
    PHRASE_REPLACEMENTS = [
        (r"\bcity view\b", " cityview "),
        (r"\bsea view\b", " seaview "),
        (r"\bocean view\b", " oceanview "),
        (r"\bpool view\b", " poolview "),
        (r"\bgarden view\b", " gardenview "),
        (r"\bmountain view\b", " mountainview "),
        (r"\bmoutain view\b", " moutainview "),
        (r"\bhill view\b", " hillview "),
        (r"\bforest view\b", " forestview "),
        (r"\bjungle view\b", " jungleview "),
        (r"\blagoon view\b", " lagoonview "),
        (r"\blake view\b", " lakeview "),
        (r"\briver view\b", " riverview "),
        (r"\bcreek view\b", " creekview "),
        (r"\bcanal view\b", " canalview "),
        (r"\bgulf view\b", " gulfview "),
        (r"\bbay view\b", " bayview "),
        (r"\bbeach view\b", " beachview "),
        (r"\bisland view\b", " islandview "),
        (r"\bpalm view\b", " palmview "),
        (r"\bvalley view\b", " valleyview "),
        (r"\bganges view\b", " gangesview "),
        (r"\baravalli view\b", " aravalliview "),
        (r"\bmangrove view\b", " mangroveview "),
        (r"\bsunset view\b", " sunsetview "),
        (r"\bsunrise view\b", " sunriseview "),
        (r"\bdesert view\b", " desertview "),
        (r"\btropical view\b", " tropicalview "),
        (r"\bgrove view\b", " groveview "),
        (r"\bhorizon view\b", " horizonview "),
        (r"\bpaddy view\b", " paddyview "),
        (r"\bricefield view\b", " ricefieldview "),
        (r"\bcanyon view\b", " canyonview "),
        (r"\bbackwater view\b", " backwaterview "),
        (r"\bcourtyard view\b", " courtyardview "),
        (r"\bpatio view\b", " patioview "),
        (r"\bskyline view\b", " skylineview "),
        (r"\bcityscape view\b", " cityscapeview "),
        (r"\bdowntown view\b", " downtownview "),
        (r"\bstreet view\b", " streetview "),
        (r"\bplaza view\b", " plazaview "),
        (r"\bharbor view\b", " harborview "),
        (r"\bharbour view\b", " harbourview "),
        (r"\bmarina view\b", " marinaview "),
        (r"\btower view\b", " towerview "),
        (r"\bwaterfront view\b", " waterfrontview "),
        (r"\bintracoastal view\b", " intracoastalview "),
        (r"\bcity hall view\b", " cityhallview "),
        (r"\bburj khalifa view\b", " burjkhalifaview "),
        (r"\bfountain view\b", " fountainview "),
        (r"\btemple view\b", " templeview "),
        (r"\btaj view\b", " tajview "),
        (r"\bpyramid view\b", " pyramidsview "),
        (r"\bacropolis view\b", " acropolisview "),
        (r"\bklcc view\b", " klccview "),
        (r"\bbangalore view\b", " bangaloreview "),
        (r"\bcastle view\b", " castleview "),
        (r"\bturf view\b", " turfview "),
        (r"\bbosphorus view\b", " bosphorusview "),
        (r"\bsheikh zayed view\b", " sheikhzayedview "),
        (r"\brace track view\b", " racetrackview "),
        (r"\brunway view\b", " runwayview "),
        (r"\bmt\.? fuji view\b", " mtfujiview "),
        (r"\bkingdom tower view\b", " kingdomtowerview "),
        (r"\bresort view\b", " resortview "),
        (r"\bgolf view\b", " golfview "),
        (r"\bgolf course view\b", " golfcourseview "),
        (r"\batrium view\b", " atriumview "),
        (r"\bracecourse view\b", " racecourseview "),
        (r"\bpanoramic view\b", " panoramicview "),
        (r"\bpartial view\b", " partialview "),
        (r"\blimited view\b", " limitedview "),
        (r"\bvarious view\b", " variousview "),
        (r"\bscenic view\b", " scenicview "),
    ]

    STOPWORDS = set("""
    with the a an and of to for on in by or at from
    room rooms guestroom guest type stay included including
    offering offers feature featuring equipped appointed furnished
    per night day rate price area space layout
    sqm sqft meter m2 size spacious large small compact
    floor high low tower wing block section zone level
    deal offer promo promotion package pkg exclusive limited only
    """.split())

    SYNONYMS = {
        "std": "standard", "sup": "superior", "dlx": "deluxe",
        "delux": "deluxe", "dlux": "deluxe", "exec": "executive",
        "prem": "premium", "fam": "family", "ste": "suite",
        "junior": "junior_suite", "superdeluxe": "superior_deluxe",
        "dbl": "double", "sgl": "single", "twn": "twin",
        "kng": "king", "qn": "queen", "sview": "seaview", "sv": "seaview",
    }

    CATEGORIES = {
        "standard", "superior", "deluxe", "superior_deluxe", "executive", "premium",
        "family", "suite", "junior_suite", "studio", "signature", "classic",
        "club", "business", "economy", "villa", "bungalow", "loft", "penthouse",
    }

    BED_TYPES = {"single", "double", "twin", "queen", "king", "sofabed", "bunkbed", "rollaway"}

    def __init__(self, settings: RoomMatchConfig | None = None):
        self.settings = settings or CONFIG

    def pre_normalize(self, text: str) -> str:
        if not isinstance(text, str):
            return ""
        t = text.lower()
        for pattern, replacement in self.PHRASE_REPLACEMENTS:
            t = re.sub(pattern, replacement, t)
        return t

    def normalize_text(self, text: str) -> str:
        if not isinstance(text, str):
            return ""
        t = self.pre_normalize(text)
        t = self.PUNCT_PATTERN.sub(" ", t)
        t = t.replace("-", " ").replace("/", " ")
        tokens = []
        for tok in t.split():
            tok = self.SYNONYMS.get(tok, tok)
            if tok not in self.STOPWORDS:
                tokens.append(tok)
        return " ".join(sorted(set(tokens)))

    def extract_category(self, normalized_text: str) -> str:
        return next((tok for tok in normalized_text.split() if tok in self.CATEGORIES), "")

    def extract_bed(self, normalized_text: str) -> str:
        beds = [tok for tok in normalized_text.split() if tok in self.BED_TYPES]
        return "+".join(beds) if beds else ""

    def sim_equal(self, a: str, b: str, missing: float = 0.6) -> float:
        if not a and not b:
            return 1.0
        if not a or not b:
            return missing
        return 1.0 if a == b else 0.0

    def compute_text_similarity(self, a: str, b: str):
        fuzzy = fuzz.token_sort_ratio(a, b) / 100

        a_tokens = set(a.split())
        b_tokens = set(b.split())
        jaccard = len(a_tokens & b_tokens) / max(1, len(a_tokens | b_tokens))

        lev = distance.Levenshtein.normalized_similarity(a, b)

        final = (
            fuzzy * self.settings.TEXT_WEIGHTS_FUZZY
            + jaccard * self.settings.TEXT_WEIGHTS_JACCARD
            + lev * self.settings.TEXT_WEIGHTS_LEVENSHTEIN
        )
        return fuzzy, jaccard, lev, final

    def recommend(self, score: float) -> str:
        if score >= self.settings.THRESHOLD_STRONG:
            return "strong"
        if score >= self.settings.THRESHOLD_GOOD:
            return "good"
        if score >= self.settings.THRESHOLD_WEAK:
            return "weak"
        return "none"

    def analyze_similarity(self, standard_room_name: str, supplier_room_name: str) -> dict:
        std_norm = self.normalize_text(standard_room_name)
        sup_norm = self.normalize_text(supplier_room_name)

        attributes = {
            "category_std": self.extract_category(std_norm),
            "category_sup": self.extract_category(sup_norm),
            "bed_std": self.extract_bed(std_norm),
            "bed_sup": self.extract_bed(sup_norm),
        }

        category_sim = self.sim_equal(attributes["category_std"], attributes["category_sup"])
        bed_sim = self.sim_equal(attributes["bed_std"], attributes["bed_sup"])

        attribute_final_score = (
            category_sim * self.settings.ATTRIBUTE_WEIGHT_CATEGORY
            + bed_sim * self.settings.ATTRIBUTE_WEIGHT_BED
        )

        fuzzy, jaccard, lev, text_final_score = self.compute_text_similarity(std_norm, sup_norm)

        final_similarity = (
            text_final_score * self.settings.COMBINED_WEIGHT_TEXT
            + attribute_final_score * self.settings.COMBINED_WEIGHT_ATTRIBUTES
        )

        explanation = (
            f"TEXT={text_final_score:.3f} (F={fuzzy:.2f},J={jaccard:.2f},L={lev:.2f}) | "
            f"ATTR={attribute_final_score:.3f} "
            f"(CAT={category_sim:.2f},BED={bed_sim:.2f}) | FINAL={final_similarity:.3f}"
        )

        return {
            "std_normalized": std_norm,
            "sup_normalized": sup_norm,
            **attributes,
            "category_similarity": category_sim,
            "bed_similarity": bed_sim,
            "fuzzy_similarity": fuzzy,
            "jaccard_similarity": jaccard,
            "levenshtein_similarity": lev,
            "text_final_score": text_final_score,
            "attribute_final_score": attribute_final_score,
            "final_similarity": final_similarity,
            "map_recommendation": self.recommend(final_similarity),
            "explanation": explanation,
        }


# -- view splitting (ported from mmt/review.py) ------------------------------

_VIEW_PATTERNS = [pattern for pattern, _ in RoomNormalizationService.PHRASE_REPLACEMENTS]


def split_name_and_view(name: str):
    """(base_name, view | None) — strip a trailing/embedded known view phrase
    so it does not distort the base-name similarity. View is verified
    separately as a hard match."""
    if not name:
        return name, None
    for pattern in _VIEW_PATTERNS:
        match = re.search(pattern, name, flags=re.IGNORECASE)
        if match:
            view = match.group(0).strip()
            base = name[: match.start()] + name[match.end():]
            return base.strip(" -,"), view
    return name, None


def _normalize_view_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").lower())


def views_match(requested_view: str, actual_view: str | None) -> bool:
    """Exact equality after lowercasing + stripping non-alphanumerics."""
    if not actual_view:
        return False
    return _normalize_view_token(requested_view) == _normalize_view_token(actual_view)
