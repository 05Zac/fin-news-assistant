-- fin-news-assistant v1 — SQLite dumb text store (WAL)
-- Usage: sqlite3 data/fin_news.db < schema.sql

PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

-- 1) raw_events: dumb store for Marketaux (and later GDELT) raw payloads
CREATE TABLE IF NOT EXISTS raw_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL DEFAULT 'marketaux', -- marketaux | gdelt | forexfactory
    external_id  TEXT,                               -- provider's article id/hash
    title        TEXT,
    description  TEXT,                               -- snippet / preview
    url          TEXT,
    published_at TEXT,                               -- ISO8601 as TEXT (SQLite)
    symbols      TEXT,                               -- comma-separated e.g. "USD,XAU"
    raw_json     TEXT NOT NULL,                      -- full provider JSON as TEXT (no blob)
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- 2) summaries: Agent PM output (paid tier summarizes what is happening)
CREATE TABLE IF NOT EXISTS summaries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_event_id INTEGER REFERENCES raw_events(id) ON DELETE SET NULL,
    summary_text TEXT NOT NULL,
    model_tier   TEXT NOT NULL CHECK (model_tier IN ('free','paid')),
    model_name   TEXT,
    tokens_used  INTEGER DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- 3) status: pipeline cursors / feature flags / health (key-value)
CREATE TABLE IF NOT EXISTS status (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- Seed status keys (idempotent)
INSERT OR IGNORE INTO status(key, value) VALUES ('last_ingest_at', '');
INSERT OR IGNORE INTO status(key, value) VALUES ('last_summary_at', '');
INSERT OR IGNORE INTO status(key, value) VALUES ('feature_flag', 'active'); -- active | read_only (exam weeks)

-- 4) flash_queue: 30s poll → prefilter → queue → event agent (FIFO, dedup, retry)
CREATE TABLE IF NOT EXISTS flash_queue (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_event_id  INTEGER REFERENCES raw_events(id) ON DELETE SET NULL,
    dedupe_key    TEXT NOT NULL,
    relevance     TEXT NOT NULL CHECK (relevance IN ('high','medium','low')),
    status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','processing','done','skipped','failed')),
    attempts      INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    processed_at  TEXT
);

-- Indexes — keep minimal, cover v1 queries
CREATE INDEX IF NOT EXISTS idx_raw_events_published ON raw_events(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_raw_events_source ON raw_events(source);
CREATE INDEX IF NOT EXISTS idx_raw_events_symbols ON raw_events(symbols);
CREATE INDEX IF NOT EXISTS idx_raw_events_url ON raw_events(url);
CREATE INDEX IF NOT EXISTS idx_summaries_created ON summaries(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_summaries_raw_event ON summaries(raw_event_id);
CREATE INDEX IF NOT EXISTS idx_flash_queue_status_created ON flash_queue(status, created_at);
CREATE INDEX IF NOT EXISTS idx_flash_queue_dedupe ON flash_queue(dedupe_key);
CREATE INDEX IF NOT EXISTS idx_flash_queue_raw_event ON flash_queue(raw_event_id);
