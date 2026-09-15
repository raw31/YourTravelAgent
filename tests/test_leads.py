"""yta/leads/db.py -- the manual-call-flow queue a confirmed/declined v2
deal lands in. Fresh temp-path DB per test, same connect()-creates-schema
pattern as yta/hoteldb."""
import json

from yta.leads.db import connect, record_lead


class _Occupancy:
    def __init__(self, adults, children, child_ages):
        self.adults = adults
        self.children = children
        self.child_ages = child_ages


class _Packet:
    def __init__(self):
        self.hotel = type("H", (), {"name": "Fairmont Jaipur Hotel"})()

        class _Stay:
            check_in = "2026-09-22"
            check_out = "2026-09-23"
            occupancy = [_Occupancy(2, 0, [])]
        self.stay = _Stay()

        class _Offer:
            room_name = "Signature View King"
        self.requested_offer = _Offer()

        class _Benchmark:
            final_payable = 31683.0
            currency = "INR"
        self.ota_benchmark = _Benchmark()

    def to_dict(self):
        return {"hotel": {"name": self.hotel.name}, "note": "full snapshot"}


_RESOLUTION = {
    "room_map": {
        "matched": True,
        "rate_options": [
            {"option_id": "opt1", "room_name": "Signature View King", "meal_basis": "Breakfast",
             "refundable": True, "free_cancel_until": "2026-09-18", "currency": "INR", "total_price": 28015.64},
        ],
        "ratekey_option_ids": ["opt1"],
    }
}


def test_record_lead_writes_expected_row_and_returns_a_reference(tmp_path):
    db_path = tmp_path / "leads.db"
    ref = record_lead("919999999999", "confirmed", _Packet(), _RESOLUTION, path=db_path)

    assert ref.startswith("BMS-") and len(ref) == 12   # "BMS-" + 8 hex chars

    con = connect(db_path)
    row = con.execute("SELECT * FROM leads WHERE booking_ref = ?", (ref,)).fetchone()
    con.close()
    assert row is not None
    assert row["phone"] == "919999999999"
    assert row["status"] == "confirmed"
    assert row["hotel_name"] == "Fairmont Jaipur Hotel"
    assert row["room_name"] == "Signature View King"
    assert row["price"] == 28015.64
    assert row["ota_price"] == 31683.0
    assert row["refundable"] == 1
    occ = json.loads(row["occupancy_json"])
    assert occ == [{"adults": 2, "children": 0, "child_ages": []}]
    snapshot = json.loads(row["packet_json"])
    assert snapshot["note"] == "full snapshot"


def test_record_lead_handles_no_matched_option(tmp_path):
    db_path = tmp_path / "leads.db"
    ref = record_lead("919999999999", "declined", _Packet(), {"room_map": {"matched": False}}, path=db_path)
    con = connect(db_path)
    row = con.execute("SELECT * FROM leads WHERE booking_ref = ?", (ref,)).fetchone()
    con.close()
    assert row["status"] == "declined"
    assert row["room_name"] == "Signature View King"   # falls back to the requested offer's room name
    assert row["price"] is None


def test_record_lead_rejects_bad_status(tmp_path):
    try:
        record_lead("919999999999", "maybe", _Packet(), {}, path=tmp_path / "leads.db")
        assert False, "expected AssertionError"
    except AssertionError:
        pass


def test_record_lead_stores_referred_by_when_given(tmp_path):
    db_path = tmp_path / "leads.db"
    ref = record_lead("919999999999", "confirmed", _Packet(), _RESOLUTION,
                       path=db_path, referred_by="BMS-AAAAAAAA")
    con = connect(db_path)
    row = con.execute("SELECT referred_by FROM leads WHERE booking_ref = ?", (ref,)).fetchone()
    con.close()
    assert row["referred_by"] == "BMS-AAAAAAAA"


def test_record_lead_referred_by_defaults_to_null(tmp_path):
    db_path = tmp_path / "leads.db"
    ref = record_lead("919999999999", "confirmed", _Packet(), _RESOLUTION, path=db_path)
    con = connect(db_path)
    row = con.execute("SELECT referred_by FROM leads WHERE booking_ref = ?", (ref,)).fetchone()
    con.close()
    assert row["referred_by"] is None


def test_connect_migrates_a_pre_existing_db_missing_the_column(tmp_path):
    # Simulates the real AWS leads.db, built before referred_by existed --
    # CREATE TABLE IF NOT EXISTS alone would never add the column to it.
    import sqlite3
    db_path = tmp_path / "old_leads.db"
    old_schema_con = sqlite3.connect(str(db_path))
    old_schema_con.execute("""
        CREATE TABLE leads (
            booking_ref TEXT PRIMARY KEY, phone TEXT NOT NULL, status TEXT NOT NULL,
            created_at TEXT NOT NULL, hotel_name TEXT, check_in TEXT, check_out TEXT,
            room_name TEXT, meal_plan TEXT, refundable INTEGER, free_cancel_until TEXT,
            currency TEXT, price REAL, ota_price REAL, occupancy_json TEXT,
            packet_json TEXT NOT NULL
        )
    """)
    old_schema_con.execute(
        "INSERT INTO leads (booking_ref, phone, status, created_at, hotel_name, packet_json) "
        "VALUES ('BMS-OLD00001', '919999999999', 'confirmed', '2026-01-01T00:00:00', 'Old Hotel', '{}')"
    )
    old_schema_con.commit()
    old_schema_con.close()

    con = connect(db_path)   # must not raise, and must add the missing column
    row = con.execute("SELECT booking_ref, referred_by FROM leads WHERE booking_ref = 'BMS-OLD00001'").fetchone()
    con.close()
    assert row["referred_by"] is None   # pre-existing row, column just added

    # New writes against the migrated db work normally too.
    ref = record_lead("919999999999", "confirmed", _Packet(), _RESOLUTION,
                       path=db_path, referred_by="BMS-OLD00001")
    con = connect(db_path)
    row = con.execute("SELECT referred_by FROM leads WHERE booking_ref = ?", (ref,)).fetchone()
    con.close()
    assert row["referred_by"] == "BMS-OLD00001"
