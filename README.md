# Fin News Assistant — v1 Scaffold

> Personal tool + proof-of-work. Locked arch: Linux gateway 24/7 prod, SQLite dumb store + Agent PM reader.

## v1 Scope (ship single loop first)

- **Assets:** USD + XAU (gold) + macro events
- **Volume:** ~50 req/day early
- **Source:** Marketaux primary (GDELT/Currents + Forex Factory calendar as backup — not in v1 loop)
- **Pipeline (single loop, no 3-agent factory):** `discovery → resolver → Evidence Bundle → AI Fact Extraction (tiered) → Python Validation → Market Analysis → Verification → SQLite → Discord`
  - Evidence Bundle = discovery preview + full + additional + official
  - 3-agent factory cut until single loop stable (Nova + Fable verdict 2026-10-11)
- **Output:** Discord only
- **Infra:** Linux gateway sole 24/7 prod (systemd timers + Docker), laptop dev only
- **Non-negotiable:** Degree > all — weekends time-blocked study, exam weeks read-only/feature-flag, queue drains, no maintenance

Out of scope for v1: multi-agent factory, Jin10, dashboard UI, trading signals, realtime websocket.

## Stack

| Layer | Choice | Notes |
|-------|--------|-------|
| Orchestrator | n8n (thin) | Timer + Discord webhook only; logic in Python/SQL so n8n crash doesn't kill evidence |
| Language | Python 3.11+ | scraper / extraction / cleaning / dedup / validation |
| Store | SQLite WAL, text-only | dumb store, not for manual reading; Agent PM reads & summarizes; daily Parquet snapshots |
| Agent PM | Paid 3.8B API (sunk cost) | reads DB LIMIT 50, summarizes what's happening |
| AI routing | Tiered: easy→free / hard→paid | RTK + headroom budget before every call |
| Cost | ~RM0 new spend | Linux gateway sunk + self-hosted n8n/SQLite; paid API already subscribed |

## Architecture Principles

1. SQLite = storage, Agent = reader/manager — never manually browse DB as source of truth.
2. Python/SQL/n8n/AI separation — n8n thin, logic portable.
3. Make it work first, then optimize. Minimal working slice > perfect design.
4. Free tier for grunt (fetch/clean/dedup), paid for reasoning (extraction/analysis).
5. RTK + headroom check before every LLM call — budget > quota.

## Project Structure

```
fin-news-assistant/
├── README.md
├── schema.sql          # WAL + 3 tables + indexes
├── .env.example
├── src/
│   ├── config.py       # tiered router stub
│   ├── ingest.py       # Marketaux → SQLite dumb store
│   └── agent_pm.py     # reads DB LIMIT 50, summarizes
└── data/               # gitignored, SQLite file lives here
```

## Run Steps

```bash
# 1. Clone / enter
cd fin-news-assistant

# 2. Env
cp .env.example .env
# edit .env → MARKET_AUX_API_TOKEN, DISCORD_WEBHOOK_URL, PAID_API_KEY

# 3. Python env
python -m venv .venv && source .venv/bin/activate
pip install requests python-dotenv  # + openai/anthropic client as needed for tiered routing

# 4. Init DB (WAL)
sqlite3 data/fin_news.db < schema.sql
# verify: sqlite3 data/fin_news.db "PRAGMA journal_mode;"

# 5. Ingest (dumb store)
python src/ingest.py

# 6. Agent PM summary (reads DB, tiered call stub)
python src/agent_pm.py

# 7. Prod (Linux gateway) — systemd timer daily + Docker for n8n
# n8n workflow: Cron → Execute Python (ingest + agent_pm) → Discord
```

## Env Vars

See `.env.example`. Never commit `.env`.

## Degree Guardrail

Weekends = study. Exam weeks = `FEATURE_FLAG=read_only` — ingest queues, Discord muted, no deploys.

## Next after v1 stable

Parquet snapshots → GDELT backup source → Forex Factory calendar (actual/forecast/prev/impact) → Evidence Bundle verification hardening.
