"""Hotel resolver — against a tiny fixture DB (no network, no big dump)."""
import sqlite3

import pytest

from yta.hoteldb.db import SCHEMA
from yta.hoteldb.normalize import core_name, norm_name, norm_region, parse_location
from yta.hoteldb.resolver import resolve

FIXTURE = [
    # tj_id, unica, name, full, rating, lat, lon, region, country
    (1, "u1", "Aloha on the Ganges by Leisure Hotels",
     "Aloha on the Ganges by Leisure Hotels, Rishikesh", 4.0, 30.13083, 78.32835,
     "RISHIKESH", "India"),
    (2, "u2", "Ganga Kinare - A Riverside Boutique Hotel",
     "Ganga Kinare, Rishikesh", 4.0, 30.11500, 78.30800, "RISHIKESH", "India"),
    (3, "u3", "Aloha Resort", "Aloha Resort, Kovalam", 3.0, 8.40000, 76.97800,
     "KOVALAM", "India"),
    (4, "u4", "The Roseate Ganges", "The Roseate Ganges, Rishikesh", 5.0,
     30.13200, 78.32700, "RISHIKESH", "India"),
    (5, "u5", "Sunway Velocity Hotel Kuala Lumpur", "Sunway Velocity Hotel", 4.0,
     3.128294, 101.723946, "KUALA LUMPUR", "Malaysia"),
]


@pytest.fixture
def con():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA.read_text())
    for (tj, u, name, full, rating, lat, lon, region, country) in FIXTURE:
        c.execute(
            "INSERT INTO tj_hotels (tj_id,unica_id,hotel_name,hotel_full_name,"
            "name_norm,name_core,rating,lat,lon,region_name,region_norm,"
            "country_name,country_norm,property_type,description) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tj, u, name, full, norm_name(name), core_name(name), rating, lat, lon,
             region, norm_region(region), country, norm_name(country), "Hotel", ""))
    c.execute("INSERT INTO tj_hotels_fts(tj_hotels_fts) VALUES('rebuild')")
    c.commit()
    yield c
    c.close()


def test_parse_location():
    assert parse_location('{"lat": 30.13, "lon": 78.32}') == (30.13, 78.32)
    assert parse_location('{"lat": 999, "lon": 1}') == (None, 1.0)
    assert parse_location(None) == (None, None)
    assert parse_location("garbage") == (None, None)


def test_core_name_strips_brand_and_generic():
    assert core_name("Aloha on the Ganges by Leisure Hotels") == "aloha ganges"
    assert core_name("The Roseate Ganges") == "roseate ganges"


def test_exact_name_plus_geo_is_high(con):
    r = resolve("Aloha on the Ganges by Leisure Hotels", city="Rishikesh",
                lat=30.13083, lng=78.32835, country="India", con=con)
    assert r.band == "high"
    assert r.match.tj_id == 1
    assert r.match.distance_m < 50


def test_geo_disambiguates_same_name(con):
    # "Aloha" alone matches both tj 1 (Rishikesh) and tj 3 (Kovalam);
    # coordinates must pick Rishikesh.
    r = resolve("Aloha", lat=30.131, lng=78.328, con=con)
    assert r.match.tj_id == 1


def test_name_only_still_resolves(con):
    r = resolve("Aloha on the Ganges by Leisure Hotels", city="Rishikesh", con=con)
    assert r.match.tj_id == 1
    assert r.band in ("high", "medium")


def test_wrong_hotel_right_spot_held_back(con):
    # coordinates land on Aloha (tj 1) but the name is clearly The Roseate;
    # resolver should prefer the name and not blindly trust the pin.
    r = resolve("The Roseate Ganges", lat=30.13083, lng=78.32835, con=con)
    assert r.match.tj_id == 4


def test_no_match_for_unknown(con):
    r = resolve("Nonexistent Palace Hotel Xyz", city="Reykjavik",
                country="Iceland", con=con)
    assert r.band == "none" or (r.match and r.match.score < 0.5)


def test_result_is_auditable(con):
    r = resolve("Aloha on the Ganges", lat=30.131, lng=78.328, con=con)
    d = r.to_dict()
    assert d["candidates"][0]["name_score"] is not None
    assert d["candidates"][0]["geo_score"] is not None
    assert "layers" in d
