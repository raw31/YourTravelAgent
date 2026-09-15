"""SQLite connection + writer for the leads DB — the "manual call flow"
queue a confirmed/declined WhatsApp deal (v2 flow) lands in. Same
connection pattern as yta/hoteldb/db.py, but this DB is small and
lightweight enough to just create itself on first write rather than
needing an explicit build step."""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB = _ROOT / "data" / "leads.db"
SCHEMA = Path(__file__).with_name("schema.sql")


def db_path() -> Path:
    return Path(os.environ.get("YTA_LEADS_DB", DEFAULT_DB))


def connect(path=None) -> sqlite3.Connection:
    p = Path(path or db_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(SCHEMA.read_text())
    return con


def _best_option(resolution: dict | None) -> dict:
    # Same "pick the cheapest matched option" logic wa_shared.whatsapp_reply()
    # uses to decide what to actually quote — duplicated rather than shared
    # so v2 doesn't require touching wa_shared.py at all (see the plan this
    # was built from: v0/v1/wa_shared stay completely untouched by v2 work).
    rz = resolution or {}
    room_map = rz.get("room_map") or {}
    if not room_map.get("matched"):
        return {}
    opts = room_map.get("rate_options") or []
    keyed = set(room_map.get("ratekey_option_ids") or [])
    pool = [o for o in opts if o.get("option_id") in keyed] or opts
    if not pool:
        return {}
    return min(pool, key=lambda o: o.get("total_price", float("inf")))


def record_lead(phone: str, status: str, packet, resolution: dict | None,
                 path=None) -> str:
    """Write one row for a customer's confirm/decline on a presented deal.
    Returns the generated booking reference ("BMS-XXXXXXXX")."""
    assert status in ("confirmed", "declined"), status
    best = _best_option(resolution)
    ref = "BMS-" + uuid.uuid4().hex[:8].upper()
    occ = packet.stay.occupancy or []
    occ_json = json.dumps([
        o if isinstance(o, dict) else
        {"adults": getattr(o, "adults", None), "children": getattr(o, "children", None),
         "child_ages": getattr(o, "child_ages", None)}
        for o in occ
    ])
    con = connect(path)
    try:
        con.execute(
            "INSERT INTO leads (booking_ref, phone, status, created_at, hotel_name, "
            "check_in, check_out, room_name, meal_plan, refundable, free_cancel_until, "
            "currency, price, ota_price, occupancy_json, packet_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ref, phone, status, datetime.now(timezone.utc).isoformat(),
             packet.hotel.name, packet.stay.check_in, packet.stay.check_out,
             best.get("room_name") or packet.requested_offer.room_name,
             best.get("meal_basis"), best.get("refundable"),
             best.get("free_cancel_until"), best.get("currency"),
             best.get("total_price"), packet.ota_benchmark.final_payable,
             occ_json, json.dumps(packet.to_dict())),
        )
        con.commit()
    finally:
        con.close()
    return ref
