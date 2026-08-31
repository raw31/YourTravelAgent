"""YourTravelAgent — v0 OTA extraction layer.

Given a pasted OTA booking URL, produce a structured Booking Intent packet
(hotel, dates, occupancy, room, rate terms, price) with per-field provenance.

    from yta import extract
    packet = extract("https://secure.booking.com/book.html?...")   # renders + LLM-parses
    packet = extract(url, render=False)                            # URL params only
"""
from yta.pipeline import extract
from yta.profiles import route

__all__ = ["extract", "route"]
