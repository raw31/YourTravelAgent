-- Confirmed/declined WhatsApp deals -- the "manual call flow" queue.
-- One row per customer decision on a presented deal (v2 flow only).
-- packet_json is a full BookingIntent.to_dict() snapshot so whoever makes
-- the follow-up call has everything, not just the summarized columns --
-- same reasoning as the packet's own run_log/evidence trail.

CREATE TABLE IF NOT EXISTS leads (
    booking_ref       TEXT PRIMARY KEY,   -- "BMS-XXXXXXXX", shown to the customer
    phone             TEXT NOT NULL,
    status            TEXT NOT NULL,      -- 'confirmed' | 'declined'
    created_at        TEXT NOT NULL,      -- ISO timestamp, UTC

    hotel_name        TEXT,
    check_in          TEXT,
    check_out         TEXT,
    room_name         TEXT,
    meal_plan         TEXT,
    refundable        INTEGER,            -- 0/1/NULL
    free_cancel_until TEXT,
    currency          TEXT,
    price             REAL,               -- BookMyStay's confirmed sell price
    ota_price         REAL,               -- OTA's shown price, for reference
    occupancy_json    TEXT,               -- raw occupancy array, as JSON
    referred_by       TEXT,               -- a prior booking_ref, if this lead mentioned one

    packet_json       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_leads_phone       ON leads(phone);
CREATE INDEX IF NOT EXISTS ix_leads_status      ON leads(status);
CREATE INDEX IF NOT EXISTS ix_leads_referred_by ON leads(referred_by);
