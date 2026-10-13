"""
ingest.py — Marketaux fetch → SQLite dumb store stub.

Pipeline position: discovery → SQLite (text-only, WAL, dumb store).
Agent PM reads DB later; this file only fetches + stores raw.
~50 req/day early; keep call count minimal.

Usage:
    python src/ingest.py
    python src/ingest.py --limit 10 --symbols USD,XAU

Needs: MARKET_AUX_API_TOKEN in .env
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*a, **kw):
        return False

from config import db_path, marketaux_token

MARKETAUX_URL = "https://api.marketaux.com/v1/news/all"

# Ensure project root on path when run as `python src/ingest.py`
sys.path.insert(0, str(Path(__file__).parent))


def ensure_schema(conn: sqlite3.Connection):
    """Ensure tables exist if schema.sql wasn't run. WAL set here too."""
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    schema_file = Path(__file__).parent.parent / "schema.sql"
    if schema_file.exists():
        conn.executescript(schema_file.read_text())
    conn.commit()


def fetch_marketaux(api_token: str, symbols: str = "USD,XAU", limit: int = 10) -> list[dict]:
    """Fetch from Marketaux. Returns list of article dicts."""
    params = {
        "api_token": api_token,
        "symbols": symbols,
        "filter_entities": "true",
        "language": "en",
        "limit": limit,
    }
    resp = requests.get(MARKETAUX_URL, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    # Marketaux wraps in {"data": [...], "meta": {...}}
    articles = data.get("data", []) if isinstance(data, dict) else []
    return articles


def store_raw(conn: sqlite3.Connection, articles: list[dict], source: str = "marketaux") -> int:
    """Dumb store: insert each article as raw_json TEXT. No dedup logic beyond URL ignore."""
    inserted = 0
    for a in articles:
        external_id = str(a.get("uuid") or a.get("id") or "")
        title = a.get("title")
        description = a.get("description") or a.get("snippet") or a.get("highlight")
        url = a.get("url")
        published_at = a.get("published_at") or a.get("published_at") or ""
        # symbols as comma string
        entities = a.get("entities") or []
        symbols = ",".join(
            e.get("symbol") for e in entities if isinstance(e, dict) and e.get("symbol")
        ) or None
        raw_json = json.dumps(a, ensure_ascii=False)

        # minimal dedup: skip if same url already stored today (keeps stub simple)
        if url:
            cur = conn.execute("SELECT 1 FROM raw_events WHERE url = ? LIMIT 1", (url,))
            if cur.fetchone():
                continue

        conn.execute(
            """
            INSERT INTO raw_events (source, external_id, title, description, url, published_at, symbols, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (source, external_id, title, description, url, published_at, symbols, raw_json),
        )
        inserted += 1
    conn.commit()
    # update status cursor
    conn.execute(
        "INSERT INTO status(key, value) VALUES ('last_ingest_at', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
        (datetime.now(timezone.utc).isoformat(),),
    )
    conn.commit()
    return inserted


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="Marketaux → SQLite dumb store")
    parser.add_argument("--limit", type=int, default=10, help="articles per call (default 10)")
    parser.add_argument("--symbols", default="USD,XAU", help="comma symbols")
    parser.add_argument("--dry-run", action="store_true", help="fetch only, don't write DB")
    args = parser.parse_args()

    token = marketaux_token()
    if not token:
        # dev fallback: allow dry-run without token using stub data
        print("[ingest] MARKET_AUX_API_TOKEN not set — using stub article for local test.")
        articles = [
            {
                "uuid": "stub-1",
                "title": "Stub: USD holds steady ahead of Fed comments",
                "description": "Dollar index flat; gold edges up on safe-haven demand.",
                "url": "https://example.com/stub-usd-gold",
                "published_at": datetime.now(timezone.utc).isoformat(),
                "entities": [{"symbol": "USD"}, {"symbol": "XAU"}],
            }
        ]
    else:
        print(f"[ingest] fetching Marketaux symbols={args.symbols} limit={args.limit}")
        try:
            articles = fetch_marketaux(token, symbols=args.symbols, limit=args.limit)
        except Exception as e:
            print(f"[ingest] fetch failed: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"[ingest] fetched {len(articles)} articles")

    if args.dry_run:
        print(json.dumps(articles[:2], indent=2, ensure_ascii=False))
        return

    db = db_path()
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    try:
        ensure_schema(conn)
        inserted = store_raw(conn, articles)
        print(f"[ingest] inserted {inserted}/{len(articles)} → {db} (WAL)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
