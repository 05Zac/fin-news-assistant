"""
flash_poll.py — Lean 30s InvestingLive poller → prefilter → DB → queue (enqueue-only).

Called every 30s by n8n (or systemd). Does NOT post to Discord directly.
The queue_worker.py drains the queue (one-by-one) and posts.

Flow:
  fetch_investinglive(limit) → for each item:
    prefilter (clean → relevance → dedupe_key → is_duplicate window)
    if url dup or dedupe_key dup in 15m → skip entirely
    else store to raw_events, then enqueue to flash_queue:
      high/medium → status='pending' (worker will process → paid event agent)
      low         → status='skipped' (stored for audit, no LLM cost)

Enqueue-only: keeps poll fast (<2s) and idempotent via URL+dedupe_key.

Usage:
  python src/flash_poll.py --dry-run --limit 15
  python src/flash_poll.py --limit 15 --source investinglive
  python src/flash_poll.py --limit 15 --source investinglive --enqueue-only  # default for 30s loop
  flock -n /tmp/fin-flash.lock python src/flash_poll.py --source investinglive --limit 15 --enqueue-only

Tier: FREE only (prefilter heuristic), no LLM.
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*a, **kw):
        return False

sys.path.insert(0, str(Path(__file__).parent))

from config import db_path
from prefilter import prefilter_result, make_dedupe_key

# Reuse fetch + store helpers from flash_xau without re-importing side effects
# We import fetch_investinglive and store_raw directly
try:
    from flash_xau import fetch_investinglive, store_raw, ensure_schema, is_feature_active
except ImportError:
    fetch_investinglive = None  # type: ignore
    store_raw = None  # type: ignore
    ensure_schema = None  # type: ignore
    is_feature_active = None  # type: ignore


def enqueue(conn: sqlite3.Connection, raw_event_id: int, dedupe_key: str, relevance: str, status: str) -> int:
    cur = conn.execute(
        """
        INSERT INTO flash_queue (raw_event_id, dedupe_key, relevance, status)
        VALUES (?, ?, ?, ?)
        """,
        (raw_event_id, dedupe_key, relevance, status),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore


def main():
    load_dotenv()
    p = argparse.ArgumentParser(description="30s flash poll → prefilter → queue (enqueue-only, FREE)")
    p.add_argument("--limit", type=int, default=15)
    p.add_argument("--source", choices=["investinglive", "forexfactory", "all"], default="investinglive",
                   help="30s loop should be investinglive only (default)")
    p.add_argument("--dry-run", action="store_true", help="fetch + prefilter preview, no DB write")
    p.add_argument("--enqueue-only", action="store_true", help="enqueue high/medium to pending, low to skipped (no direct Discord)")
    p.add_argument("--window-minutes", type=int, default=15, help="dedupe_key window in minutes (default 15)")
    args = p.parse_args()

    if fetch_investinglive is None:
        print("[flash_poll] failed to import flash_xau helpers", file=sys.stderr)
        sys.exit(1)

    # Fetch
    all_articles: list[tuple[str, list[dict]]] = []
    if args.source in ("investinglive", "all"):
        try:
            arts = fetch_investinglive(limit=args.limit)
            print(f"[flash_poll] investinglive fetched {len(arts)}")
            all_articles.append(("investinglive", arts))
        except Exception as e:
            print(f"[flash_poll] investinglive failed: {e}", file=sys.stderr)

    if args.source in ("forexfactory", "all"):
        try:
            from flash_xau import fetch_ff_calendar  # lazy
            arts = fetch_ff_calendar(limit=args.limit)
            print(f"[flash_poll] forexfactory fetched {len(arts)}")
            all_articles.append(("forexfactory", arts))
        except Exception as e:
            print(f"[flash_poll] forexfactory failed: {e}", file=sys.stderr)

    total = sum(len(a) for _, a in all_articles)
    print(f"[flash_poll] total {total} fetched")

    if args.dry_run:
        # Preview prefilter without DB
        # Use in-memory conn for dedupe check preview (url_exists will be false)
        import tempfile
        tmp = sqlite3.connect(":memory:")
        # still run ensure_schema so is_duplicate_recent doesn't error on missing tables
        # but we skip DB creation for dry-run preview
        for src, arts in all_articles:
            print(f"\n--- {src} prefilter preview (first 3) ---")
            for a in arts[:3]:
                title = a.get("title") or ""
                desc = a.get("description") or ""
                url = a.get("url") or ""
                pub = a.get("published_at") or ""
                sym = ",".join(e.get("symbol") for e in (a.get("entities") or []) if isinstance(e, dict) and e.get("symbol")) or "USD"
                # lightweight preview without DB
                from prefilter import clean_description, classify_relevance, make_dedupe_key
                cleaned = clean_description(desc, 300)
                rel = classify_relevance(title, cleaned, sym)
                key = make_dedupe_key(title, pub)
                print(f"  [{rel:6s}] dedupe={key} title={(title or '')[:90]}")
                print(f"           url={(url or '')[:80]} cleaned={(cleaned or '')[:100]}")
        print(f"\n[flash_poll] dry-run — no DB write (enqueue-only={args.enqueue_only})")
        return

    # Real path — open DB
    db = db_path()
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db, timeout=10)
    try:
        if ensure_schema:
            ensure_schema(conn)
        else:
            # fallback ensure flash_queue exists
            schema_file = Path(__file__).parent.parent / "schema.sql"
            if schema_file.exists():
                conn.executescript(schema_file.read_text())
                conn.commit()

        if is_feature_active and not is_feature_active(conn):
            print("[flash_poll] feature_flag=read_only — skipping store/enqueue.")
            return

        # Stats
        inserted = 0
        enqueued_pending = 0
        enqueued_skipped = 0
        dup_url = 0
        dup_key = 0

        for src, arts in all_articles:
            for a in arts:
                title = a.get("title") or ""
                description = a.get("description") or ""
                url = a.get("url") or ""
                published_at = a.get("published_at") or ""
                entities = a.get("entities") or []
                symbols = ",".join(e.get("symbol") for e in entities if isinstance(e, dict) and e.get("symbol")) or "USD"

                # Prefilter BEFORE store (checks url_exists + dedupe_key window)
                pf = prefilter_result(title, description, url, published_at, symbols, conn)
                if pf["is_duplicate"]:
                    if pf["dup_reason"] == "url_exists":
                        dup_url += 1
                    else:
                        dup_key += 1
                    continue

                # Not duplicate — store to raw_events
                # Use store_raw per-article to get inserted id
                raw_json = json.dumps(a, ensure_ascii=False)
                external_id = str(a.get("uuid") or a.get("id") or "")
                # Double-check url still not inserted by race (store_raw also checks)
                if url:
                    cur = conn.execute("SELECT 1 FROM raw_events WHERE url = ? LIMIT 1", (url,))
                    if cur.fetchone():
                        dup_url += 1
                        continue

                cur = conn.execute(
                    "INSERT INTO raw_events (source, external_id, title, description, url, published_at, symbols, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (src, external_id, title, description, url, published_at, symbols, raw_json),
                )
                raw_id = cur.lastrowid
                conn.commit()

                # Update ingest cursor
                conn.execute(
                    "INSERT INTO status(key, value) VALUES ('last_ingest_at', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                    (datetime.now(timezone.utc).isoformat(),),
                )
                conn.commit()
                inserted += 1

                # Enqueue (or skip) based on relevance
                relevance = pf["relevance"]
                dedupe_key = pf["dedupe_key"]
                if pf["should_enqueue"]:
                    enqueue(conn, int(raw_id), dedupe_key, relevance, "pending")  # type: ignore
                    enqueued_pending += 1
                else:
                    # low relevance → still audit in queue as skipped so we don't re-evaluate same item
                    # but worker will never process skipped
                    enqueue(conn, int(raw_id), dedupe_key, relevance, "skipped")  # type: ignore
                    enqueued_skipped += 1

        print(f"[flash_poll] done: fetched={total} inserted={inserted} pending={enqueued_pending} skipped_low={enqueued_skipped} dup_url={dup_url} dup_key_window={dup_key}")

        # Quick queue depth for monitoring
        try:
            cur = conn.execute("SELECT status, COUNT(*) FROM flash_queue GROUP BY status")
            depths = dict(cur.fetchall())
            if depths:
                print(f"[flash_poll] queue depths: {depths}")
        except sqlite3.OperationalError:
            pass

    finally:
        conn.close()


if __name__ == "__main__":
    main()
