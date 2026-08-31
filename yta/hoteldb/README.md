# yta.hoteldb — TripJack hotel resolution (Phase 2, step 1)

Given the Phase-1 Booking Intent packet (hotel name + city / address /
coordinates), find the **exact TripJack hotel id** in the static master
dump, so we can call the TripJack Hotel Detail API.

## Build the local DB (one-time)

```bash
python -m yta.hoteldb.load "/path/to/query_result_*.xlsx"
python -m yta.hoteldb.cli --stats
```

Streams the ~288 MB Excel dump into `data/hotels.db` (SQLite, gitignored).
`location` `{"lat","lon"}` is split into real `lat` / `lon` columns; the
name is stored normalized (`name_norm`) and stripped to its distinctive
core (`name_core` — brand suffix + generic words like "hotel/resort/the"
removed). An FTS5 index over the names powers no-coordinate lookups.

Source columns → table: `id`→`tj_id`, `unica_id`, `hotel_name`,
`hotel_full_name`, `rating`, `location`→`lat`+`lon`, `region_name`,
`country_name`, `property_type`, `display_description`→`description`.

## Resolve

```bash
python -m yta.hoteldb.cli "Aloha on the Ganges by Leisure Hotels" \
    --city Rishikesh --lat 30.13083 --lng 78.32835 --country India
```

```python
from yta.hoteldb import resolve
r = resolve("Aloha on the Ganges by Leisure Hotels",
            city="Rishikesh", lat=30.13083, lng=78.32835, country="India")
r.band           # "high" | "medium" | "low" | "none"
r.match.tj_id    # the TripJack id  (None if band == "none")
r.candidates     # ranked alternatives, each with score breakdown

# straight from a Phase-1 packet:
from yta.hoteldb.link import resolve_packet
r = resolve_packet(packet)
```

## Matching cascade (strategy doc §9)

```
L0  retrieve pool
      coords?  → lat/lon bounding box ±2 km  (widens ×4 if < 5 hits)
      + FTS on the DISTINCTIVE name tokens   (AND first, OR fallback, bm25-ranked, cap 250)
        — "hotel / the / on / by / resort …" and the query's own city
          tokens are dropped so "Comfort Inn Flagstaff" isn't matched on "flagstaff"
L1  country     fuzzy narrow (uses tj_country_name — clean; skip if it empties the pool)
L2  region/city narrow (region_name OR city token inside hotel_full_name)
L3  address     token-overlap → score boost (never a filter)
L4  coordinates rank; a strong name overrides a *stale* pin (≤ 60 km),
                not a pin on another continent
```

**Per-candidate score** = `max(geo-trust, name-trust)`:

| | geo-trust (coords present) | name-trust |
|---|---|---|
| coordinates (≤150 m → 1.0, ≥3 km → 0.0) | 0.46 | — |
| name (`rapidfuzz` token-set + token-sort on distinctive tokens; anchor token must appear or capped 0.33) | 0.40 | 0.74 |
| city / locality | 0.09 | 0.16 |
| address overlap | 0.05 | 0.10 |

- Perfect-geo + wrong-name (the hotel next door) → capped 0.52.
- **Bands:** ≥ 0.84 `high` (auto) · ≥ 0.66 `medium` (confirm) · ≥ 0.50 `low` · else `none`.
- Demotions to `medium`: top-2 within 0.04 · chosen hotel > 3 km from the pin · name score < 0.6.
- Result carries the ranked candidates + every per-signal score + the `layers` trace — auditable, never a bare id.

Typical latency: **5–15 ms** (name+city) · ~100–250 ms (geo box in a dense city).

## Next

`tj_id` → TripJack Hotel API v3 **Detail** call (Listing → Detail → Review
→ Book). Blocked on TripJack API credentials (UAT/self-cert per §10).
