"""Debug CLI — paste an OTA URL (or a saved page / screenshots / PDF), see
the extracted packet + evidence trail + validation warnings.

    python -m yta.cli "<ota_url>"                    # render + LLM parse
    python -m yta.cli "<ota_url>" --no-render        # URL params only
    python -m yta.cli "<ota_url>" --html page.html   # parse saved HTML
    python -m yta.cli --image shot1.png --image shot2.png   # screenshots
    python -m yta.cli --pdf booking.pdf              # PDF of the page
    python -m yta.cli "<ota_url>" --pdf booking.pdf  # URL hints + PDF
"""
from __future__ import annotations

import argparse
import json
import sys

from yta.ingest import load_paths
from yta.pipeline import extract
from yta.profiles import route


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="yta")
    ap.add_argument("url", nargs="?", default="", help="OTA booking URL")
    ap.add_argument("--no-render", action="store_true",
                    help="skip the browser + LLM step; URL params only")
    ap.add_argument("--html", metavar="FILE", help="parse this saved page HTML")
    ap.add_argument("--text", metavar="FILE", help="parse this saved page text")
    ap.add_argument("--image", metavar="FILE", action="append", default=[],
                    help="screenshot of the page (repeatable)")
    ap.add_argument("--pdf", metavar="FILE", action="append", default=[],
                    help="PDF of the page (repeatable)")
    ap.add_argument("--evidence", action="store_true",
                    help="print the per-field provenance trail")
    ap.add_argument("--no-resolve", action="store_true",
                    help="skip the TripJack hotel-id lookup")
    ap.add_argument("--price", action="store_true",
                    help="on a confident match, actually POST /hms/v3/hotel/pricing")
    ap.add_argument("--timeout", type=int, default=40000)
    ap.add_argument("--compact", action="store_true")
    args = ap.parse_args(argv)

    media = load_paths(args.image + args.pdf) if (args.image or args.pdf) else None
    html = open(args.html, encoding="utf-8", errors="ignore").read() if args.html else None
    text = open(args.text, encoding="utf-8", errors="ignore").read() if args.text else None

    if not (args.url or media or html or text):
        ap.error("give a URL, or --image/--pdf/--html/--text")

    if args.url:
        print(f"# profile: {route(args.url).name}", file=sys.stderr)

    packet = extract(args.url, render=not args.no_render, timeout_ms=args.timeout,
                     page_html=html, page_text=text, media=media)
    data = packet.to_dict()
    if not args.evidence:
        data.pop("evidence", None)

    # -- TripJack resolution (also logs into packet.run_log) -----------
    tj = None
    if not args.no_resolve and packet.hotel.name:
        from yta.hoteldb.db import db_path
        if not db_path().exists():
            packet.log("TripJack lookup skipped — data/hotels.db not built")
        else:
            from yta.hoteldb.link import resolve_packet
            packet.log(f"TripJack resolve: {packet.hotel.name!r}")
            tj = resolve_packet(packet)
            m = tj.match
            packet.log(f"TripJack → {tj.band}" + (
                f": tj_id={m.tj_id} unica_id={m.unica_id} score {m.score}"
                if m else " — no match"))

    if not args.compact:
        data["run_log"] = packet.run_log
    print(f"# method: {packet.source.extraction_method}", file=sys.stderr)
    print(f"# status: {'OK' if packet.status == 'ok' else 'FAIL — missing: ' + ', '.join(packet.missing_mandatory)}",
          file=sys.stderr)
    print(json.dumps(data, indent=None if args.compact else 2, default=str))

    print("\n# ── run log ──", file=sys.stderr)
    for e in packet.run_log:
        print(f"#  {e['ms']:>6} ms  {e['msg']}", file=sys.stderr)

    if packet.warnings:
        print("\n# warnings:", file=sys.stderr)
        for w in packet.warnings:
            print(f"  - {w}", file=sys.stderr)

    if tj and tj.match:
        m = tj.match
        print(f"\n# ── TripJack match ──  band: {tj.band}", file=sys.stderr)
        print(f"#   tj_id      {m.tj_id}", file=sys.stderr)
        print(f"#   unica_id   {m.unica_id}", file=sys.stderr)
        print(f"#   hotel_name {m.hotel_name}", file=sys.stderr)
        for nt in tj.notes:
            print(f"#   ! {nt}", file=sys.stderr)

        # the Detail/Pricing request built from this packet + tj_id
        try:
            from yta.tripjack.hotel import pricing_request_from_packet
            req = pricing_request_from_packet(packet, m.tj_id)
            print(f"\n# ── TripJack Detail request ──  "
                  f"{req['method']} {req['url']}", file=sys.stderr)
            print(json.dumps(req["body"], indent=2))
        except ValueError as e:
            print(f"\n# TripJack Detail request not buildable: {e}", file=sys.stderr)
            req = None

        if args.price and req and tj.band in ("high", "medium"):
            from yta.tripjack.client import TripJackClient, TripJackError
            from yta.tripjack.hotel import hotel_options
            client = TripJackClient.from_env()
            if not client.configured():
                print("\n# TripJack pricing skipped — TRIPJACK_API_KEY not set",
                      file=sys.stderr)
            else:
                s = packet.stay
                try:
                    det = hotel_options(
                        m.tj_id, s.check_in, s.check_out,
                        s.occupancy or [{"adults": s.adults or 2,
                                         "children": s.children or 0,
                                         "child_ages": s.child_ages or []}],
                        currency=packet.ota_benchmark.currency or "INR",
                        client=client)
                    print(f"\n# ── TripJack live options ──  {len(det.options)} option(s)",
                          file=sys.stderr)
                    for nt in det.notes:
                        print(f"#   ! {nt}", file=sys.stderr)
                    print(json.dumps(det.to_dict(), indent=2, default=str))

                    if det.options:
                        from yta.roommap import map_rooms
                        rm = map_rooms(
                            det.options, packet.requested_offer,
                            benchmark_price=packet.ota_benchmark.final_payable,
                            policy=packet.matching_policy)
                        print(f"\n# ── room → rate-plan mapping ──  "
                              f"{'matched ' + rm.room_type_id if rm.matched else 'NO MATCH'}"
                              f"  [{rm.band}]" + ("  (LLM)" if rm.llm_used else ""),
                              file=sys.stderr)
                        for nt in rm.notes:
                            print(f"#   ! {nt}", file=sys.stderr)
                        if rm.view_flag:
                            print(f"#   ⚑ {rm.view_flag}", file=sys.stderr)
                        print(json.dumps(rm.to_dict(), indent=2, default=str))
                except TripJackError as e:
                    print(f"\n# TripJack pricing call failed: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
