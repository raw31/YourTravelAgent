"""OTA meal-plan free text  ->  TripJack `mealBasis` enum.

TripJack pricing only ever returns one of five values:

    Room Only · Breakfast · Half Board · Full Board · All Inclusive

The OTA side is free text ("Breakfast included", "Bed & Breakfast", "HB",
"Room only, no meals", "All-inclusive (food/beverages)"). Vocabulary drawn
from the production `meal_mappings.json`.
"""
from __future__ import annotations

import re

TJ_MEALS = ("Room Only", "Breakfast", "Half Board", "Full Board", "All Inclusive")

# exact meal codes (whole-string match only)
_CODES = {
    "ro": "Room Only", "ep": "Room Only", "ep": "Room Only", "nm": "Room Only",
    "bb": "Breakfast", "cp": "Breakfast", "bo": "Breakfast",
    "hb": "Half Board", "map": "Half Board",
    "fb": "Full Board", "ap": "Full Board",
    "ai": "All Inclusive", "all": "All Inclusive",
}


def meal_to_tj(text: str | None) -> str | None:
    """Return the TJ mealBasis enum value, or None if the text does not map."""
    if not text:
        return None
    n = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    if not n:
        return None

    if n in _CODES:
        return _CODES[n]

    # order matters: check richer plans before plainer ones (a Half Board
    # blurb usually still contains the word "breakfast")
    if any(k in n for k in (
            "all inclusive", "allinclusive", "all meals", "all meal",
            "fully inclusive")):
        return "All Inclusive"
    if any(k in n for k in (
            "full board", "fullboard", "three meals", "3 meals",
            "breakfast lunch and dinner", "breakfast lunch dinner",
            "american plan")):
        return "Full Board"
    if any(k in n for k in (
            "half board", "halfboard", "modified american",
            "breakfast and dinner", "breakfast dinner",
            "breakfast and lunch", "breakfast lunch",
            "dinner included", "two meals", "2 meals")):
        return "Half Board"
    if any(k in n for k in (
            "room only", "roomonly", "no meal", "no meals", "without breakfast",
            "without meal", "self catering", "self-catering", "room with no")):
        return "Room Only"
    if "breakfast" in n or n in ("continental", "bed and breakfast"):
        return "Breakfast"
    return None
