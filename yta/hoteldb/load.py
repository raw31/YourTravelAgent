"""Load the TripJack hotel dump (Excel or CSV) into SQLite.

    python -m yta.hoteldb.load "/path/to/query_result_*.xlsx"
    python -m yta.hoteldb.load dump.csv --db data/hotels.db

Streams the file row by row (the dump is ~300 MB / ~1.4 GB uncompressed
XML), so memory stays flat. Rebuilds the table from scratch each run.
"""
from __future__ import annotations

import argparse
import csv
import sys
import time

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

from datetime import datetime, timezone
from pathlib import Path

from yta.hoteldb.db import connect, db_path
from yta.hoteldb.normalize import core_name, norm_name, norm_region, parse_location

# source column name -> canonical
COLMAP = {
    "id": "tj_id",
    "unica_id": "unica_id",
    "hotel_name": "hotel_name",
    "hotel_full_name": "hotel_full_name",
    "rating": "rating",
    "location": "location",
    "region_name": "region_name",
    "country_name": "country_name",
    "property_type": "property_type",
    "display_description": "description",
    "tj_country_name": "tj_country_name",
}

_INSERT = """INSERT OR REPLACE INTO tj_hotels
 (tj_id, unica_id, hotel_name, hotel_full_name, name_norm, name_core, rating,
  lat, lon, region_name, region_norm, country_name, country_norm,
  property_type, description)
 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""


_XL_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _col_idx(ref: str) -> int:
    """'A1' -> 0, 'AB12' -> 27."""
    n = 0
    for ch in ref:
        if ch.isalpha():
            n = n * 26 + (ord(ch.upper()) - 64)
        else:
            break
    return n - 1


def _rows_xlsx(path):
    """Stream a single-sheet .xlsx via lxml.iterparse over the raw sheet XML —
    ~15x faster than openpyxl read_only on a multi-hundred-MB file, flat
    memory. Assumes inline strings (this dump has an empty sharedStrings)."""
    import zipfile
    from lxml import etree

    zf = zipfile.ZipFile(path)
    sheet = next((n for n in zf.namelist()
                  if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")),
                 "xl/worksheets/sheet1.xml")
    fh = zf.open(sheet)
    header_len = 0
    for _, row in etree.iterparse(fh, tag=f"{_XL_NS}row"):
        cells = {}
        for c in row:
            ci = _col_idx(c.get("r", "A"))
            v = c.find(f"{_XL_NS}v")
            if v is not None and v.text is not None:
                cells[ci] = v.text
            else:
                t = c.find(f"{_XL_NS}is/{_XL_NS}t")
                cells[ci] = t.text if t is not None and t.text is not None else None
        width = max(cells) + 1 if cells else 0
        out = [cells.get(i) for i in range(max(width, header_len))]
        row.clear()
        while row.getprevious() is not None:
            del row.getparent()[0]
        if header_len == 0:
            header_len = len(out)
            yield [(str(x).strip() if x is not None else "") for x in out]
        else:
            yield out
    zf.close()


def _rows_csv(path):
    f = open(path, newline="", encoding="utf-8-sig")
    r = csv.reader(f)
    header = [h.strip() for h in next(r)]
    yield header
    for row in r:
        yield row
    f.close()


def _to_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build(src: str, db=None, batch: int = 5000) -> int:
    rows = _rows_xlsx(src) if src.lower().endswith((".xlsx", ".xlsm")) else _rows_csv(src)
    header = next(rows)
    idx = {name: i for i, name in enumerate(header)}
    missing = [c for c in ("id", "hotel_name", "location") if c not in idx]
    if missing:
        raise SystemExit(f"dump is missing expected column(s): {missing}\n"
                         f"got: {header}")

    target = Path(db or db_path())
    if target.exists():
        target.unlink()
    con = connect(target, create=True)          # runs schema.sql fresh
    # bulk-load without index maintenance, then rebuild indexes at the end
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("PRAGMA cache_size=-200000")     # ~200 MB page cache
    for ix in ("ix_tj_country", "ix_tj_region", "ix_tj_lat", "ix_tj_unica"):
        con.execute(f"DROP INDEX IF EXISTS {ix}")

    def g(row, col):
        i = idx.get(col)
        return row[i] if i is not None and i < len(row) else None

    t0 = time.time()
    buf, n, skipped = [], 0, 0
    for row in rows:
        tj_id = _to_int(g(row, "id"))
        if tj_id is None:
            skipped += 1
            continue
        name = g(row, "hotel_name") or g(row, "hotel_full_name") or ""
        lat, lon = parse_location(g(row, "location"))
        region = g(row, "region_name")
        # tj_country_name is the clean canonical country ("UNITED STATES");
        # country_name is messy (8+ spellings). Prefer tj_country_name.
        country = g(row, "tj_country_name") or g(row, "country_name")
        buf.append((
            tj_id, _str(g(row, "unica_id")), _str(name),
            _str(g(row, "hotel_full_name")), norm_name(name), core_name(name),
            _to_float(g(row, "rating")), lat, lon,
            _str(region), norm_region(region),
            _str(country), norm_name(country),
            _str(g(row, "property_type")), _str(g(row, "display_description")),
        ))
        n += 1
        if len(buf) >= batch:
            con.executemany(_INSERT, buf)
            buf.clear()
            if n % 50000 == 0:
                con.commit()
                print(f"  {n:>9,} rows  ({n / (time.time() - t0):,.0f}/s)", flush=True)
    if buf:
        con.executemany(_INSERT, buf)
    con.commit()

    print("  building indexes ...", flush=True)
    con.execute("CREATE INDEX ix_tj_country ON tj_hotels(country_norm)")
    con.execute("CREATE INDEX ix_tj_region  ON tj_hotels(region_norm)")
    con.execute("CREATE INDEX ix_tj_lat     ON tj_hotels(lat)")
    con.execute("CREATE INDEX ix_tj_unica   ON tj_hotels(unica_id)")
    con.commit()
    print("  building FTS index ...", flush=True)
    con.execute("INSERT INTO tj_hotels_fts(tj_hotels_fts) VALUES('rebuild')")
    con.execute("INSERT OR REPLACE INTO meta VALUES('rows', ?)", (str(n),))
    con.execute("INSERT OR REPLACE INTO meta VALUES('source', ?)", (src,))
    con.execute("INSERT OR REPLACE INTO meta VALUES('loaded_at', ?)",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"),))
    con.commit()
    con.execute("ANALYZE")
    con.commit()

    with_geo = con.execute("SELECT count(*) FROM tj_hotels WHERE lat IS NOT NULL").fetchone()[0]
    countries = con.execute("SELECT count(DISTINCT country_norm) FROM tj_hotels").fetchone()[0]
    con.close()
    dt = time.time() - t0
    print(f"\ndone: {n:,} hotels ({skipped:,} skipped) in {dt:,.0f}s")
    print(f"  with coordinates: {with_geo:,} ({with_geo / max(n, 1):.0%})")
    print(f"  distinct countries: {countries}")
    print(f"  db: {db_path()}")
    return n


def _str(v):
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="yta.hoteldb.load")
    ap.add_argument("dump", help="TripJack hotel dump (.xlsx or .csv)")
    ap.add_argument("--db", help="output SQLite path (default data/hotels.db)")
    args = ap.parse_args(argv)
    build(args.dump, args.db)


if __name__ == "__main__":
    main()
