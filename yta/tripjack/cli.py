"""Call the TripJack Detail / Pricing API for one hotel.

    python -m yta.tripjack.cli 100001137288 2026-09-21 2026-09-22 --room 2A
    python -m yta.tripjack.cli 100001137288 2026-09-02 2026-09-03 \\
        --room 2A1C:3 --room 2A1C:2 --currency INR

  --room  <adults>A[<children>C:<age,age>]   repeatable, one per room
"""
from __future__ import annotations

import argparse
import json
import re
import sys

from yta.tripjack.client import TripJackClient, TripJackError
from yta.tripjack.hotel import hotel_options, pricing_request

_ROOM_RE = re.compile(r"^(\d+)A(?:(\d+)C(?::([\d,]+))?)?$", re.I)


def _parse_room(spec: str) -> dict:
    m = _ROOM_RE.match(spec.strip())
    if not m:
        raise SystemExit(f"bad --room {spec!r} (e.g. 2A  or  2A1C:5  or  2A2C:5,8)")
    adults = int(m.group(1))
    children = int(m.group(2)) if m.group(2) else 0
    ages = [int(a) for a in m.group(3).split(",")] if m.group(3) else []
    return {"adults": adults, "children": children, "child_ages": ages}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="yta.tripjack.cli")
    ap.add_argument("tj_id")
    ap.add_argument("check_in")
    ap.add_argument("check_out")
    ap.add_argument("--room", action="append", default=[], metavar="SPEC")
    ap.add_argument("--currency", default="INR")
    ap.add_argument("--nationality", default="106")
    ap.add_argument("--dry-run", action="store_true",
                    help="just print the request that would be sent (no key needed)")
    ap.add_argument("--raw", action="store_true", help="print the raw API response")
    args = ap.parse_args(argv)

    rooms = [_parse_room(r) for r in args.room] or [{"adults": 2}]

    if args.dry_run:
        req = pricing_request(args.tj_id, args.check_in, args.check_out, rooms,
                              currency=args.currency, nationality=args.nationality)
        print(f"{req['method']} {req['url']}")
        for k, v in req["headers"].items():
            print(f"{k}: {v}")
        print()
        print(json.dumps(req["body"], indent=2))
        return 0

    client = TripJackClient.from_env()
    if not client.configured():
        print("# TRIPJACK_API_KEY not set — add it to YourTravelAgent/.env "
              "(or use --dry-run to see the request)", file=sys.stderr)
        return 2

    try:
        d = hotel_options(args.tj_id, args.check_in, args.check_out, rooms,
                          currency=args.currency, nationality=args.nationality,
                          client=client)
    except TripJackError as e:
        print(json.dumps({"error": {"code": e.code, "message": e.message,
                                    "http": e.http_status}}, indent=2))
        return 1

    if args.raw:
        print(json.dumps(d.raw, indent=2))
        return 0

    print(json.dumps(d.to_dict(), indent=2, default=str))
    print(f"\n# {d.hotel_name or d.tj_id}: {len(d.options)} option(s)", file=sys.stderr)
    for o in d.options:
        fc = f" · free-cancel until {o.free_cancel_until}" if o.refundable else " · non-refundable"
        print(f"#  {o.meal_basis:16} {o.total_price:>10,.2f} {o.currency} "
              f"({o.commercial_type}){fc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
