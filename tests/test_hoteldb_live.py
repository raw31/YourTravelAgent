"""Resolver checks against the real loaded DB. Skipped if it isn't built."""
import pytest

from yta.hoteldb.db import db_path
from yta.hoteldb.resolver import resolve

pytestmark = pytest.mark.skipif(not db_path().exists(),
                                reason="data/hotels.db not built")


@pytest.mark.parametrize("name,kw,tj_id", [
    ("Aloha on the Ganges by Leisure Hotels",
     dict(city="Rishikesh", lat=30.13083, lng=78.32835, country="India"),
     100001137288),
    ("Aloha on the Ganges by Leisure Hotels",
     dict(city="Rishikesh", country="India"), 100001137288),
    ("The Roseate Ganges", dict(city="Rishikesh", country="India"), 100000297299),
    ("Comfort Inn Flagstaff South I-17",
     dict(city="Flagstaff", country="United States"), 100000197379),
])
def test_known_hotels_resolve(name, kw, tj_id):
    r = resolve(name, **kw)
    assert r.match is not None and r.match.tj_id == tj_id
    assert r.band in ("high", "medium")


def test_generic_name_is_not_high_confidence():
    r = resolve("Comfort Inn", city="Flagstaff", country="USA")
    assert r.band in ("medium", "low", "none")   # too generic to auto-pick


def test_resolve_is_fast():
    import time
    t = time.perf_counter()
    resolve("Sunway Velocity Hotel Kuala Lumpur", city="Kuala Lumpur",
            country="Malaysia")
    assert (time.perf_counter() - t) < 0.5
