-- TripJack hotel master (static dump) + matching indexes.
-- One row per TripJack hotel id.
-- Source columns: id, unica_id, hotel_name, hotel_full_name, rating,
--   location{"lat","lon"}, region_name, country_name, property_type,
--   display_description, tj_country_name

CREATE TABLE IF NOT EXISTS tj_hotels (
    tj_id           INTEGER PRIMARY KEY,   -- TripJack hotel id (also the rowid)
    unica_id        TEXT,                  -- Unica / Vervotech mapping id
    hotel_name      TEXT,
    hotel_full_name TEXT,
    name_norm       TEXT,                  -- normalized name for matching
    name_core       TEXT,                  -- name_norm minus generic tokens / brand suffix
    rating          REAL,
    lat             REAL,
    lon             REAL,
    region_name     TEXT,                  -- city / locality (as given)
    region_norm     TEXT,
    country_name    TEXT,
    country_norm    TEXT,
    property_type   TEXT,
    description     TEXT
);

CREATE INDEX IF NOT EXISTS ix_tj_country ON tj_hotels(country_norm);
CREATE INDEX IF NOT EXISTS ix_tj_region  ON tj_hotels(region_norm);
CREATE INDEX IF NOT EXISTS ix_tj_lat     ON tj_hotels(lat);
CREATE INDEX IF NOT EXISTS ix_tj_unica   ON tj_hotels(unica_id);

-- FTS over the normalized name + locality, for candidate retrieval when we
-- have no coordinates. External-content table — repopulated after bulk load.
CREATE VIRTUAL TABLE IF NOT EXISTS tj_hotels_fts USING fts5(
    name_norm, name_core, region_name,
    content='tj_hotels', content_rowid='tj_id', tokenize='unicode61'
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
