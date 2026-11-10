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
from store import insert_article, migrate

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
    migrate(conn)


def fetch_marketaux(api_token: str, symbols: str = "USD,XAU", limit: int = 10) -> list[dict]:
    """Fetch from Marketaux. Returns list of article dicts."""
    params = {
        "api_token": api_token,
        "symbols": symbols,
        "filter_entities": "true",
        "language": "en",
        "limit": limit,
    }
    # Marketaux only takes the key as a URL param, and requests puts the full URL
    # (key included) in its error messages — scrub it so it never reaches the logs.
    try:
        resp = requests.get(MARKETAUX_URL, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        raise RuntimeError(str(e).replace(api_token, "***")) from None
    # Marketaux wraps in {"data": [...], "meta": {...}}
    articles = data.get("data", []) if isinstance(data, dict) else []
    return articles


def store_raw(conn: sqlite3.Connection, articles: list[dict], source: str = "marketaux") -> int:
    """Store articles through store.insert_article (URL + content dedup, row metadata)."""
    inserted = 0
    for a in articles:
        if insert_article(conn, source, a)["status"] == "inserted":
            inserted += 1
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
