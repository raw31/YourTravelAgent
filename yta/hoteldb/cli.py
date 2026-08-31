"""Resolve a scraped hotel against the TripJack master.

    python -m yta.hoteldb.cli "Aloha on the Ganges by Leisure Hotels" \\
        --city Rishikesh --lat 30.13083 --lng 78.32835 --country India

    python -m yta.hoteldb.cli --stats
"""
from __future__ import annotations

import argparse
import json
import sys

from yta.hoteldb.db import connect
from yta.hoteldb.resolver import resolve


def _stats():
    con = connect()
    m = dict(con.execute("SELECT key, value FROM meta").fetchall())
    n = con.execute("SELECT count(*) FROM tj_hotels").fetchone()[0]
    geo = con.execute("SELECT count(*) FROM tj_hotels WHERE lat IS NOT NULL").fetchone()[0]
    cc = con.execute("SELECT count(DISTINCT country_norm) FROM tj_hotels").fetchone()[0]
    top = con.execute("SELECT country_name, count(*) c FROM tj_hotels "
                      "GROUP BY country_norm ORDER BY c DESC LIMIT 8").fetchall()
    con.close()
    print(f"hotels        {n:,}")
    print(f"with lat/lon  {geo:,} ({geo / max(n,1):.0%})")
    print(f"countries     {cc}")
    print(f"loaded_at     {m.get('loaded_at','?')}")
    print("top countries:")
    for r in top:
        print(f"  {r[0]:<28} {r[1]:>9,}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="yta.hoteldb.cli")
    ap.add_argument("name", nargs="?", default="")
    ap.add_argument("--city")
    ap.add_argument("--address")
    ap.add_argument("--lat", type=float)
    ap.add_argument("--lng", type=float)
    ap.add_argument("--country")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args(argv)

    if args.stats:
        _stats()
        return 0
    if not args.name:
        ap.error("give a hotel name (or --stats)")

    r = resolve(args.name, city=args.city, address=args.address,
                lat=args.lat, lng=args.lng, country=args.country, limit=args.limit)

    print(f"# band: {r.band}   retrieval: {r.retrieval}", file=sys.stderr)
    for note in r.notes:
        print(f"# note: {note}", file=sys.stderr)
    print(json.dumps(r.to_dict(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
