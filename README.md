# Fin News Assistant

Personal macro news desk. Pulls financial news every 30 seconds, tags each article with the
tickers it affects, and posts it to Discord with a short AI note on the likely market impact.
Once a day it posts an AI briefing of the last day's headlines.

All AI runs on free models through a local FreeLLMAPI router
(OpenAI-compatible). Every AI call is time-boxed; if it fails, the alert still posts without the note.

## How it works

```
every 30s   flash_poll.py ── InvestingLive RSS
                 │            store.py: URL + content dedup, row metadata
                 │            entities.py: tag tickers + topics
                 ▼
            flash_queue ──► queue_worker.py ──► Discord alert, then AI note edited in

daily 08:00 backup_db.py → flash_xau.py → ingest.py (Marketaux, Forex Factory calendar)
            → resolver.py → flash_notify.py → agent_pm.py (AI digest to Discord)
            → entities.py --llm-untagged (AI tickers for articles keywords missed)
            → validate.py (AI verifier)
```

Each daily step runs on its own, so one failing source does not stop the rest.

### Tagging (`src/entities.py`)

Every article gets **tickers** (`raw_events.symbols`) and **topics** (`raw_events.topics`).

- An `entities` table lists what news can affect: currencies, commodities, crypto, indices, stocks,
  sectors, central banks, countries and themes, each with aliases and the codes it implies.
- Named things are tagged directly; sectors and themes fan out to their tickers
  (a chip story tags NVDA, AMD, INTC, TSM, MU, AVGO; the Fed tags USD and XAU).
  When the headline names a company, the story is about that company and does not fan out.
- The headline decides. Body text is used only when the headline names nothing.
- Articles the keyword rules leave untagged go to the free AI, which may only pick codes from the
  `entities` table. If nothing fits they are marked "No market impact".
- `article_entities` records why each tag applies: `mention`, `implied:<CODE>` or `llm`.

```bash
python3 src/entities.py --test "Microchip prices rise as AI demand outstrips supply"
python3 src/entities.py --list
python3 src/entities.py --add SGD --kind currency --name "Singapore dollar" --aliases "SGD|Singapore dollar"
python3 src/entities.py --retag                 # re-tag every stored article
python3 src/entities.py --llm-untagged 60       # AI tickers for untagged articles
```

### Storage (`src/store.py`)

SQLite in WAL mode (`data/fin_news.db`, gitignored). `insert_article()` is the only write path:

- Dedup by URL, then by a SHA-256 of the normalised title plus the first 500 characters of the body,
  so the same story from two sources merges into one row (`dup_count`, `also_seen_in`).
  Calendar rows are keyed by URL plus event date.
- Each row records `fetched_at`, `is_stale` (already over 24h old when fetched; stored but not alerted)
  and `via_fallback`.
- `backup_db.py` writes a daily checked snapshot to `~/backups/fin-news-assistant/` (14 kept).

## Layout

```
src/
  flash_poll.py     30s RSS poll → store → queue
  queue_worker.py   posts queued alerts, adds the AI note
  store.py          dedup + metadata, the one write path
  entities.py       ticker/topic tagging + AI fallback
  prefilter.py      cleanup + dedupe key
  flash_notify.py   Discord embed builder, batch notifier
  flash_xau.py      XAU/USD news pull
  ingest.py         Marketaux + Forex Factory calendar
  resolver.py       resolve article links
  agent_pm.py       daily AI digest
  validate.py       AI verifier
  llm.py            FreeLLMAPI client (answer extraction, retries, time budget)
  price.py          live quotes (Finnhub, Yahoo fallback)
  backup_db.py      daily DB snapshot
  config.py         env helpers
systemd/            user units for the 30s and daily timers
tests/              unittest suite
schema.sql
```

## Setup

```bash
cp .env.example .env          # fill in the values; never commit .env
pip install requests
python3 -c "import sqlite3; sqlite3.connect('data/fin_news.db').executescript(open('schema.sql').read())"
python3 -m unittest discover -s tests

# run once by hand (scripts read .env from the environment)
( set -a; . ./.env; set +a; python3 src/flash_poll.py --source investinglive --limit 15 --enqueue-only )

# install the timers
cp systemd/* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now fin-flash.timer fin-news.timer
```

The units point at `/home/user/.openclaw/workspace-gpt/fin-news-assistant`; change
`WorkingDirectory` and `EnvironmentFile` if you clone elsewhere.

## Roadmap

- Backup sources with per-source cool-off (Fed, BoE, BoJ feeds, GDELT) so one outage doesn't stop alerts
- Grounded digest: each claim cites article IDs; claims without a real source are dropped
- Saved daily digests with a "what changed since yesterday" section
- v3: interactive Discord app built on the ticker tags
