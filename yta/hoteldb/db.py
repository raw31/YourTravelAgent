"""SQLite connection for the hotel master DB."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB = _ROOT / "data" / "hotels.db"
SCHEMA = Path(__file__).with_name("schema.sql")


def db_path() -> Path:
    return Path(os.environ.get("YTA_HOTEL_DB", DEFAULT_DB))


def connect(path=None, *, create: bool = False) -> sqlite3.Connection:
    p = Path(path or db_path())
    if not create and not p.exists():
        raise FileNotFoundError(
            f"Hotel DB not found at {p}. Build it:\n"
            f"    python -m yta.hoteldb.load <dump.xlsx>"
        )
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    if create:
        con.executescript(SCHEMA.read_text())
    return con
