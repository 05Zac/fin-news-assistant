"""
resolver.py — discovery → resolver → Evidence Bundle stub.

Reads raw_events dumb store, scores related articles, bundles evidence.
v1 stub: deterministic grouping by published date + symbols.
Later: title/time/entity/keyword scoring + source credibility.

Usage:
    python src/resolver.py
    python src/resolver.py --limit 20 --symbols USD,XAU
"""

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import db_path  # noqa: E402


def fetch_candidates(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """
        SELECT id, source, title, description, url, published_at, symbols, raw_json
        FROM raw_events
        ORDER BY datetime(published_at) DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    )
    return cur.fetchall()


import re

# Currency extraction: look for parenthesized 3-letter ISO code in title (forexfactory pattern)
_CURRENCY_RE = re.compile(r'\(([A-Z]{3})\)')

# Symbol priority for bundle ordering (lower = more important)
_SYMBOL_PRIORITY = {
    "USD": 0,
    "XAU": 1,
    "EUR": 2,
    "CNY": 3,
    "GBP": 4,
    "JPY": 5,
    "GENERAL": 99,
}


def _extract_symbol(row: sqlite3.Row) -> str:
    """
    Resolve the true tradable symbol for an event.

    Priority:
      1. Parenthesized ISO currency in title (e.g. "Calendar: CPI m/m (USD)")
      2. First symbol from the `symbols` field
      3. GENERAL fallback
    """
    title = row["title"] or ""
    m = _CURRENCY_RE.search(title)
    if m:
        return m.group(1)
    symbols = row["symbols"] or "GENERAL"
    return symbols.split(",")[0].strip() or "GENERAL"


def _symbol_sort_key(sym: str) -> int:
    return _SYMBOL_PRIORITY.get(sym, 50)


def build_bundles(rows: list[sqlite3.Row]) -> list[dict]:
    """
    Deterministic grouping by calendar date + resolved symbol.

    Sorting:
      - bundles: date DESC, then symbol priority (USD > XAU > EUR > …)
      - evidence within bundle: published_at DESC, id DESC
    """
    buckets: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        date = (r["published_at"] or "")[:10] or "unknown-date"
        sym = _extract_symbol(r)
        buckets[(date, sym)].append(r)

    bundles = []
    for (date, sym), bucket in buckets.items():
        bucket.sort(key=lambda r: (r["published_at"] or "", r["id"]), reverse=True)
        bundles.append({
            "bundle_key": f"{date}::{sym}",
            "date": date,
            "symbol": sym,
            "count": len(bucket),
            "headlines": [b["title"] for b in bucket[:5]],
            "evidence": [
                {
                    "id": b["id"],
                    "title": b["title"],
                    "url": b["url"],
                    "published_at": b["published_at"],
                    "source": b["source"],
                    "preview": (b["description"] or "")[:200],
                }
                for b in bucket
            ],
            "note": "deterministic date+symbol grouping; currency resolved from title when present",
        })

    bundles.sort(key=lambda b: (b["date"], _symbol_sort_key(b["symbol"])), reverse=True)
    return bundles


def main():
    import argparse

    p = argparse.ArgumentParser(description="Resolver → Evidence Bundle (stub)")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--symbols", default="", help="filter e.g. USD,XAU (stub: ignored, full filter in ingest)")
    p.add_argument("--json-out", type=str, default="", help="write bundles to file")
    args = p.parse_args()

    db = db_path()
    db_file = Path(db)
    if not db_file.exists():
        print(f"[resolver] DB not found at {db} — run ingest first.")
        sys.exit(0)

    conn = sqlite3.connect(db)
    try:
        rows = fetch_candidates(conn, limit=args.limit)
        print(f"[resolver] read {len(rows)} candidates (LIMIT {args.limit})")
        if not rows:
            print("[resolver] no rows — nothing to bundle.")
            return
        bundles = build_bundles(rows)
        print(f"[resolver] built {len(bundles)} bundle(s):")
        for b in bundles[:5]:
            print(f"  - {b['bundle_key']}  count={b['count']}  e.g. {b['headlines'][0] if b['headlines'] else '—'}")
        if args.json_out:
            Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.json_out).write_text(json.dumps(bundles, indent=2, ensure_ascii=False))
            print(f"[resolver] wrote {args.json_out}")
        else:
            # preview first bundle
            print(json.dumps(bundles[0], indent=2, ensure_ascii=False)[:3000])
    finally:
        conn.close()


if __name__ == "__main__":
    main()
