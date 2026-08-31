"""Deterministic lat/lng extraction from URLs — every common format."""
import pytest

from yta.urlfacts import latlng_from_url

RISHIKESH = (30.13083, 78.32835)


@pytest.mark.parametrize("url,expected", [
    # separate params
    ("https://www.makemytrip.com/hotels/hotel-review/?lat=30.13083&lng=78.32835",
     RISHIKESH),
    ("https://x.com/?latitude=30.13083&longitude=78.32835", RISHIKESH),
    ("https://x.com/?LAT=30.13083&LON=78.32835", RISHIKESH),
    ("https://x.com/?hotelLat=30.13083&hotelLng=78.32835", RISHIKESH),
    # combined param
    ("https://x.com/?ll=30.13083,78.32835", RISHIKESH),
    ("https://x.com/?geo=30.13083%2C78.32835", RISHIKESH),
    ("https://x.com/?latlng=30.13083;78.32835", RISHIKESH),
    ("https://x.com/?center=30.13083|78.32835", RISHIKESH),
    ("https://x.com/?location=30.13083%20-78.32835", (30.13083, -78.32835)),
    # google-maps @lat,lng
    ("https://www.google.com/maps/place/Hotel/@30.13083,78.32835,17z", RISHIKESH),
    # combined param, longitude first — recoverable (only one value > 90)
    ("https://x.com/?ll=118.5,30.13083", (30.13083, 118.5)),
])
def test_extracts(url, expected):
    lat, lng = latlng_from_url(url)
    assert lat == pytest.approx(expected[0])
    assert lng == pytest.approx(expected[1])


@pytest.mark.parametrize("url", [
    "https://www.agoda.com/en-in/book/?roomName=Standard+Room&isEasyCancel=false",
    "https://x.com/?lat=999&lng=1",              # out of range
    "https://x.com/?lat=0&lng=0",                # null island
    "https://secure.booking.com/book.html?hotel_id=1194299&checkin=2026-09-21",
    "",
])
def test_no_coords(url):
    assert latlng_from_url(url) == (None, None)
