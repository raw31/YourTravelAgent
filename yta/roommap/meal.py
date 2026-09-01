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

# meal ladder — weak to strong. `same_or_better` (the default matching policy)
# keeps a TJ option when its rank >= the requested rank.
MEAL_RANK = {
    "Room Only": 0,
    "Breakfast": 1,
    "Half Board": 2,       # breakfast + one more meal
    "Full Board": 3,       # breakfast + lunch + dinner
    "All Inclusive": 4,    # full board + drinks / more
}


def meal_rank(meal: str | None) -> int:
    """Rank of a meal on the ladder; -1 if unrecognised. Accepts either a
    canonical TJ enum value or a richer string TJ sometimes passes through
    ("Breakfast for 2", "Breakfast buffet", "Half board (buffet dinner)")."""
    if not meal:
        return -1
    if meal in MEAL_RANK:
        return MEAL_RANK[meal]
    return MEAL_RANK.get(meal_to_tj(meal) or "", -1)


# exact meal codes (whole-string match only)
_CODES = {
    "ro": "Room Only", "ep": "Room Only", "nm": "Room Only", "sc": "Room Only",
    "bb": "Breakfast", "cp": "Breakfast", "bo": "Breakfast", "b&b": "Breakfast",
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
            "without meal", "meal not included", "meals not included",
            "self catering", "self-catering", "european plan", "room with no")):
        return "Room Only"
    if any(k in n for k in (
            "breakfast", "bed and breakfast", "continental plan", "b b")) \
            or n == "continental":
        return "Breakfast"
    return None
