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
            "country_name,country_norm,property_type) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tj, u, name, full, norm_name(name), core_name(name), rating, lat, lon,
             region, norm_region(region), country, norm_name(country), "Hotel"))
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


def test_match_carries_cover_image_through(con):
    # For sending a hotel's photo alongside its rates once identified
    # (yta.wa_flows v5/v6 _present_deal) -- the column has to actually
    # survive resolve()'s SELECT * -> Candidate construction.
    con.execute("UPDATE tj_hotels SET cover_image = ? WHERE tj_id = 1",
                ("https://example.com/aloha.jpg",))
    con.commit()
    r = resolve("Aloha on the Ganges by Leisure Hotels", city="Rishikesh",
                lat=30.13083, lng=78.32835, country="India", con=con)
    assert r.match.cover_image == "https://example.com/aloha.jpg"


def test_match_cover_image_is_none_when_not_set(con):
    r = resolve("The Roseate Ganges", lat=30.13083, lng=78.32700, country="India", con=con)
    assert r.match.cover_image is None


# -- live 2026-10-08: "Taj MG road banagalore" -> "couldn't find the hotel" ------------

BLR_FIXTURE = [
    (11, "u11", "Taj MG Road", "Taj MG Road, Bengaluru", 5.0, 12.9750, 77.6070, "BENGALURU", "India"),
    (12, "u12", "Taj Bangalore", "Taj Bangalore", 5.0, 12.9700, 77.5900, "BANGALORE", "India"),
    (13, "u13", "Hotel Bangalore Palace", "Hotel Bangalore Palace", 3.0, 12.9900, 77.5800, "BANGALORE", "India"),
    (14, "u14", "Taj Mahal Palace", "The Taj Mahal Palace Mumbai", 5.0, 18.9220, 72.8330, "MUMBAI", "India"),
    (15, "u15", "The LaLiT New Delhi", "The LaLiT New Delhi", 5.0, 28.6320, 77.2190, "NEW DELHI", "India"),
    (16, "u16", "Lalit hotel", "Lalit hotel", 3.0, 22.5726, 88.3639, "KOLKATA", "India"),
]


@pytest.fixture
def blr():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA.read_text())
    for (tj, u, name, full, rating, lat, lon, region, country) in BLR_FIXTURE:
        c.execute(
            "INSERT INTO tj_hotels (tj_id,unica_id,hotel_name,hotel_full_name,"
            "name_norm,name_core,rating,lat,lon,region_name,region_norm,"
            "country_name,country_norm,property_type) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tj, u, name, full, norm_name(name), core_name(name), rating, lat, lon,
             region, norm_region(region), country, norm_name(country), "Hotel"))
    c.execute("INSERT INTO tj_hotels_fts(tj_hotels_fts) VALUES('rebuild')")
    c.commit()
    yield c
    c.close()


def test_city_aliases_cover_the_common_renamed_cities():
    from yta.hoteldb.resolver import _city_forms
    assert {"bangalore", "bengaluru"} <= _city_forms("bangalore")
    assert {"mumbai", "bombay"} <= _city_forms("bombay")
    assert "kolkata" in _city_forms("calcutta") and "gurugram" in _city_forms("gurgaon")
    assert _city_forms("rishikesh") == {"rishikesh"}                  # unknown cities are untouched
    assert _city_forms("") == set()


def test_a_misspelt_city_inside_the_name_no_longer_hides_the_right_hotel(blr):
    # The model gave name "Taj MG road banagalore" + city "Bangalore"; the catalog says
    # "Bengaluru". The city filter kept only the hotels that literally say "Bangalore".
    r = resolve("Taj MG road banagalore", city="Bangalore", country="India", con=blr)
    assert r.band in ("high", "medium") and r.match.tj_id == 11


def test_the_guests_city_spelling_matches_the_catalogs_spelling(blr):
    for city in ("Bangalore", "Bengaluru", "Banglore"):
        r = resolve("Taj MG Road", city=city, country="India", con=blr)
        assert r.band == "high" and r.match.tj_id == 11, city
    assert resolve("Taj Mahal Palace", city="Bombay", country="India", con=blr).match.tj_id == 14


def test_a_typo_in_the_name_with_no_city_is_still_offered_for_confirmation(blr):
    r = resolve("Taj MG road banagalore", con=blr)
    assert r.band in ("high", "medium") and r.match.tj_id == 11        # medium -> the bot asks "Is the hotel ...?"


def test_a_hotel_that_does_not_exist_is_still_not_matched_when_the_city_is_aliased(blr):
    r = resolve("Zzyzx Nowhere Inn", city="Bangalore", country="India", con=blr)
    assert r.band == "none" and r.match is None


def test_a_typo_in_the_city_field_is_matched_to_the_catalogs_spelling(blr):
    # "lalit deli": the model may keep the typo as the CITY ("Deli"); that used to fall
    # through to an unrelated "Lalit hotel" in another city.
    r = resolve("lalit deli", city="Deli", country="India", con=blr)
    assert r.match is not None and r.match.tj_id == 15
    assert any("close to" in l for l in r.layers)


def test_lalit_deli_resolves_to_the_delhi_property_however_the_model_split_it(blr):
    for city in (None, "Delhi", "Deli"):
        r = resolve("lalit deli", city=city, country="India", con=blr)
        assert r.match is not None and r.match.tj_id == 15, city


def test_city_typo_tolerance_does_not_invent_a_match_for_an_unknown_hotel(blr):
    r = resolve("Zzyzx Nowhere Inn", city="Deli", country="India", con=blr)
    assert r.band == "none" and r.match is None
