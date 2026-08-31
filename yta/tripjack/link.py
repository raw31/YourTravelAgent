"""Bridge: Booking Intent packet + resolved tj_id -> TripJack Detail call."""
from __future__ import annotations

from yta.tripjack.hotel import hotel_options, SupplierDetail


def options_for_packet(packet, tj_id, *, client=None) -> SupplierDetail:
    """packet: BookingIntent (needs stay.check_in/out and stay.occupancy).
    tj_id: the resolved TripJack hotel id (from yta.hoteldb.resolve)."""
    s = packet.stay
    if not (s.check_in and s.check_out):
        raise ValueError("packet has no stay dates — cannot call TripJack Detail")
    occ = s.occupancy or [{"adults": s.adults or 2, "children": s.children or 0,
                           "child_ages": s.child_ages or []}]
    currency = packet.ota_benchmark.currency or "INR"
    return hotel_options(tj_id, s.check_in, s.check_out, occ,
                         currency=currency, client=client)
