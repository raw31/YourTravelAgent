"""Load the TripJack hotel dump (Excel or CSV) into SQLite.

    python -m yta.hoteldb.load "/path/to/query_result_1.xlsx"
    python -m yta.hoteldb.load dump.csv --db data/hotels.db

    # Excel's own export row cap often splits one query result across
    # several files -- pass them all, they're merged into one table:
    python -m yta.hoteldb.load query_result_1.xlsx query_result_2.xlsx

Streams each file row by row (a dump is ~200-300 MB / ~1+ GB uncompressed
XML), so memory stays flat. Rebuilds the table from scratch each run --
across ALL given files, not per file (tj_id is the primary key, so a
hotel appearing in more than one file just keeps its last-seen values).
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

# source column name -> canonical (documentation only -- build() reads
# source columns by name directly via g(row, <source name>), this isn't
# actually consulted at runtime)
COLMAP = {
    "id": "tj_id",
    "unica_id": "unica_id",
    "hotel_name": "hotel_name",
    "hotel_full_name": "hotel_full_name",
    "rating": "rating",
    "location": "location",
    "region_name": "region_name",
    "tj_country_name": "country_name",
    "property_type": "property_type",
    "address": "address",
    "contact": "contact",
    "chain_name": "chain_name",
    "cover_image": "cover_image",
}

# name_norm/name_core are left NULL here and filled in by a single
# recompute pass AFTER every row is loaded (see the UPDATE near the end
# of build()) -- decouples the normalization logic from the raw load, so
# it can be re-run on its own if norm_name()/core_name() ever change,
# without needing a fresh dump.
_INSERT = """INSERT OR REPLACE INTO tj_hotels
 (tj_id, unica_id, hotel_name, hotel_full_name, rating,
  lat, lon, region_name, region_norm, country_name, country_norm,
  property_type, address, contact, chain_name, cover_image)
 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""


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


def _load_one(con, src: str, batch: int, t0: float, n_before: int) -> tuple[int, int]:
    """Streams one dump file into `con` (already open, schema already in
    place). Returns (rows loaded from this file, rows skipped). `n_before`
    is only used for the running rows/sec printout, so counts stay
    continuous across multiple files in the same build() call."""
    rows = _rows_xlsx(src) if src.lower().endswith((".xlsx", ".xlsm")) else _rows_csv(src)
    header = next(rows)
    idx = {name: i for i, name in enumerate(header)}
    missing = [c for c in ("id", "hotel_name", "location") if c not in idx]
    if missing:
        raise SystemExit(f"{src}: dump is missing expected column(s): {missing}\n"
                         f"got: {header}")

    def g(row, col):
        i = idx.get(col)
        return row[i] if i is not None and i < len(row) else None

    buf, n, skipped = [], 0, 0
    for row in rows:
        tj_id = _to_int(g(row, "id"))
        if tj_id is None:
            skipped += 1
            continue
        name = g(row, "hotel_name") or g(row, "hotel_full_name") or ""
        lat, lon = parse_location(g(row, "location"))
        region = g(row, "region_name")
        # tj_country_name is the clean canonical country ("UNITED STATES") --
        # the only country field this export has at all (the older dump's
        # messier plain "country_name" is gone; it was always overridden by
        # this one anyway).
        country = g(row, "tj_country_name")
        buf.append((
            tj_id, _str(g(row, "unica_id")), _str(name),
            _str(g(row, "hotel_full_name")),
            _to_float(g(row, "rating")), lat, lon,
            _str(region), norm_region(region),
            _str(country), norm_name(country),
            _str(g(row, "property_type")),
            _str(g(row, "address")), _str(g(row, "contact")),
            _str(g(row, "chain_name")), _str(g(row, "cover_image")),
        ))
        n += 1
        if len(buf) >= batch:
            con.executemany(_INSERT, buf)
            buf.clear()
            if (n_before + n) % 50000 < batch:
                con.commit()
                print(f"  {n_before + n:>9,} rows  ({(n_before + n) / (time.time() - t0):,.0f}/s)",
                      flush=True)
    if buf:
        con.executemany(_INSERT, buf)
    con.commit()
    return n, skipped


def build(src, db=None, batch: int = 5000) -> int:
    """`src` is a single dump path, or a list of them -- e.g. two exports
    that together cover the full catalog (Excel's own ~2^20-row export
    cap means one query result often has to be split across files).
    Every file must share the same column layout; tj_id is the primary
    key so INSERT OR REPLACE means a hotel appearing in more than one
    file just gets its last-seen values, never duplicated."""
    srcs = [src] if isinstance(src, str) else list(src)

    target = Path(db or db_path())
    if target.exists():
        target.unlink()
    con = connect(target, create=True)          # runs schema.sql fresh
    # bulk-load without index maintenance, then rebuild indexes at the end
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("PRAGMA cache_size=-200000")     # ~200 MB page cache
    for ix in ("ix_tj_country", "ix_tj_region", "ix_tj_lat", "ix_tj_unica"):
        con.execute(f"DROP INDEX IF EXISTS {ix}")

    t0 = time.time()
    n, skipped = 0, 0
    for s in srcs:
        print(f"==> {s}", flush=True)
        n_this, skipped_this = _load_one(con, s, batch, t0, n)
        n += n_this
        skipped += skipped_this
        print(f"  {n_this:,} rows loaded from this file ({skipped_this:,} skipped)", flush=True)

    print("  recomputing name_norm / name_core ...", flush=True)
    con.create_function("py_norm_name", 1, norm_name)
    con.create_function("py_core_name", 1, core_name)
    con.execute("UPDATE tj_hotels SET name_norm = py_norm_name(hotel_name), "
                "name_core = py_core_name(hotel_name)")
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
    con.execute("INSERT OR REPLACE INTO meta VALUES('source', ?)", ("; ".join(srcs),))
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
    ap.add_argument("dump", nargs="+",
                    help="TripJack hotel dump(s) (.xlsx or .csv) -- pass more "
                         "than one when a single export had to be split "
                         "across files (e.g. Excel's own row cap)")
    ap.add_argument("--db", help="output SQLite path (default data/hotels.db)")
    args = ap.parse_args(argv)
    build(args.dump, args.db)


if __name__ == "__main__":
    main()
