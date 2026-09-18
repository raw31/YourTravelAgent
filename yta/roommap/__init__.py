"""Room + rate-plan mapping — OTA requested offer -> TripJack pricing option.

    ratekey = room_type x meal_plan x isRefundable

`map_rooms(options, offer)` -> RoomMapResult.  See `match.py`.
Scoring engine (`RoomNormalizationService`) ported from the production MMT
room-mapping service; spec in `~/mmtroommapping.md`.
"""
from yta.roommap.match import (
    RateOption, RoomBucket, RoomMapResult, map_rooms,
    RoomVariantsResult, list_cheapest_room_variants,
)
from yta.roommap.meal import meal_to_tj
from yta.roommap.normalize import (
    CONFIG, RoomMatchConfig, RoomNormalizationService,
    split_name_and_view, views_match,
)

__all__ = [
    "map_rooms", "RoomMapResult", "RoomBucket", "RateOption",
    "RoomVariantsResult", "list_cheapest_room_variants",
    "meal_to_tj", "RoomNormalizationService", "split_name_and_view",
    "views_match", "CONFIG", "RoomMatchConfig",
]
