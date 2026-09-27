-- AeroIndex database schema
-- Run once (or on every startup via connection.py) – all statements are idempotent.

-- ── Raw fare observations ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS fares (
    id                  SERIAL PRIMARY KEY,
    route               TEXT NOT NULL,
    source              TEXT NOT NULL,
    scraped_at          TIMESTAMP NOT NULL,
    flight_date         DATE,
    advance_purchase_days INTEGER,
    raw_price           NUMERIC NOT NULL,
    base_fare           NUMERIC,
    taxes_fees          NUMERIC,
    is_outlier          BOOLEAN DEFAULT FALSE,
    UNIQUE(route, source, scraped_at)
);

CREATE INDEX IF NOT EXISTS idx_fares_route_date ON fares(route, scraped_at);

-- ── Reference weights & base prices ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS route_weights (
    route       TEXT PRIMARY KEY,
    weight      NUMERIC NOT NULL,
    base_price  NUMERIC,
    updated_at  TIMESTAMP
);

-- ── Computed daily index ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS daily_index (
    id          SERIAL PRIMARY KEY,
    date        DATE NOT NULL UNIQUE,
    index_value NUMERIC NOT NULL,
    pct_change  NUMERIC,
    computed_at TIMESTAMP NOT NULL
);

-- ── LLM-generated spike explanations ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS spike_explanations (
    id                    SERIAL PRIMARY KEY,
    date                  DATE NOT NULL REFERENCES daily_index(date),
    pct_change            NUMERIC NOT NULL,
    matched_calendar_event TEXT,
    explanation           TEXT NOT NULL,
    generated_at          TIMESTAMP NOT NULL
);
