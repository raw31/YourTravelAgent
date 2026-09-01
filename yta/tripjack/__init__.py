"""TripJack Hotel API v3 client (Phase 3).

Given a resolved TripJack hotel id + the stay + per-room occupancy, fetch
that hotel's live bookable options (rooms, rates, meal plans, cancellation).

    from yta.tripjack import hotel_options
    detail = hotel_options("100001137288", "2026-09-21", "2026-09-22",
                           occupancy=[{"adults": 2, "children": 0}])
    detail.options            # list[SupplierOption]
    detail.review_hash        # -> Review API

Spec: docs/tripjack-api-v3.md
"""
from yta.tripjack.client import TripJackClient, TripJackError
from yta.tripjack.hotel import (hotel_options, pricing_request,
                                pricing_request_from_packet, SupplierDetail,
                                SupplierOption, ReviewResult, review_option,
                                review_request, review_from_detail)

__all__ = ["TripJackClient", "TripJackError", "hotel_options", "pricing_request",
           "pricing_request_from_packet", "SupplierDetail", "SupplierOption",
           "ReviewResult", "review_option", "review_request", "review_from_detail"]
