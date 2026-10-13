"""
queue_worker.py — FIFO drain for flash_queue → event agent (LLM) + Discord.

Polls flash_queue for pending high/medium items one-by-one.
Guarantees:
  - at most ONE item in 'processing' at a time (transaction + status check)
  - FIFO by created_at
  - idempotent Discord post (one embed per queued event)
  - retry up to max_attempts (default 3), then 'failed'
  - skipped/low never processed
  - respects feature_flag=read_only (drains without posting)

Flow per item:
  BEGIN IMMEDIATE → SELECT oldest pending → UPDATE to processing (attempts+1)
  → fetch raw_events row → validate deterministic → build Discord embed
  → post to DISCORD_FLASH_WEBHOOK_URL → mark done (or failed+error)
  COMMIT

Usage:
  python src/queue_worker.py --once --dry-run     # process one item, preview only
  python src/queue_worker.py --once               # process one pending
  python src/queue_worker.py --drain --limit 10   # drain up to 10 in sequence
  python src/queue_worker.py --drain --limit 50 --loop  # loop until empty (for systemd)

Chain: flash_poll.py (30s enqueue) → this worker (30s dequeue) → Discord.
Event agent hook: call_agent_pm_for_event() stub kept for paid tier.

Tier: processing uses FREE heuristic for embed; agent_pm hook may use PAID.
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*a, **kw):
        return False

sys.path.insert(0, str(Path(__file__).parent))

from config import db_path
from prefilter import clean_description, classify_relevance

# Reuse Discord posting + embed helpers from flash_notify if available
try:
    from flash_notify import build_embed as _build_embed, post_discord, sanitize_url
except ImportError:
    _build_embed = None  # type: ignore
    post_discord = None  # type: ignore
    sanitize_url = None  # type: ignore


def get_feature_flag(conn: sqlite3.Connection) -> str:
    try:
        cur = conn.execute("SELECT value FROM status WHERE key='feature_flag'")
        row = cur.fetchone()
        if row and row[0]:
            return row[0]
    except sqlite3.OperationalError:
        pass
    return "active"


def ensure_schema(conn: sqlite3.Connection):
    schema_file = Path(__file__).parent.parent / "schema.sql"
    if schema_file.exists():
        conn.executescript(schema_file.read_text())
        conn.commit()


def dequeue_one(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """
    Atomically claim the oldest 'pending' row.
    Uses BEGIN IMMEDIATE to serialize workers (flock also guards n8n overlap).
    Returns the claimed row or None if nothing pending or another worker holds 'processing'.
    """
    # Guard: if something is already 'processing', don't claim another (single-flight)
    cur = conn.execute("SELECT 1 FROM flash_queue WHERE status='processing' LIMIT 1")
    if cur.fetchone():
        return None

    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            """
            SELECT id, raw_event_id, dedupe_key, relevance, status, attempts, created_at
            FROM flash_queue
            WHERE status='pending'
            ORDER BY datetime(created_at) ASC, id ASC
            LIMIT 1
            """
        )
        row = cur.fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None

        # Claim it
        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            UPDATE flash_queue
            SET status='processing', attempts=attempts+1, processed_at=?
            WHERE id=? AND status='pending'
            """,
            (now_iso, row["id"]),
        )
        if conn.total_changes == 0:
            conn.execute("ROLLBACK")
            return None
        conn.execute("COMMIT")
        # Re-read the claimed row
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT * FROM flash_queue WHERE id=?", (row["id"],))
        return cur.fetchone()
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise


def fetch_raw_event(conn: sqlite3.Connection, raw_event_id: int) -> sqlite3.Row | None:
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """
        SELECT id, source, title, description, url, published_at, symbols, raw_json, created_at
        FROM raw_events WHERE id=?
        """,
        (raw_event_id,),
    )
    return cur.fetchone()


def mark_done(conn: sqlite3.Connection, queue_id: int):
    conn.execute(
        "UPDATE flash_queue SET status='done', processed_at=? WHERE id=?",
        (datetime.now(timezone.utc).isoformat(), queue_id),
    )
    conn.commit()
    # also bump cursor so flash_notify 'since' catches up
    try:
        conn.execute(
            "INSERT INTO status(key, value) VALUES ('last_flash_notify_at', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.commit()
    except Exception:
        pass


def mark_failed(conn: sqlite3.Connection, queue_id: int, error: str, max_attempts: int = 3):
    conn.row_factory = sqlite3.Row
    cur = conn.execute("SELECT attempts FROM flash_queue WHERE id=?", (queue_id,))
    row = cur.fetchone()
    attempts = row["attempts"] if row and "attempts" in row.keys() else 0
    if attempts >= max_attempts:
        conn.execute(
            "UPDATE flash_queue SET status='failed', error=?, processed_at=? WHERE id=?",
            (error[:500], datetime.now(timezone.utc).isoformat(), queue_id),
        )
    else:
        # Retry: back to pending so next tick picks it up
        conn.execute(
            "UPDATE flash_queue SET status='pending', error=? WHERE id=?",
            (f"retry {attempts}/{max_attempts}: {error[:400]}", queue_id),
        )
    conn.commit()


def mark_skipped(conn: sqlite3.Connection, queue_id: int, reason: str):
    conn.execute(
        "UPDATE flash_queue SET status='skipped', error=?, processed_at=? WHERE id=?",
        (reason[:500], datetime.now(timezone.utc).isoformat(), queue_id),
    )
    conn.commit()


def validate_row_deterministic(row: sqlite3.Row) -> list[str]:
    errs = []
    if not (row["title"] or "").strip():
        errs.append("missing title")
    if row["url"] and not row["url"].startswith(("http://", "https://")):
        errs.append(f"bad url: {row['url'][:60]}")
    return errs


def call_agent_pm_for_event(row: sqlite3.Row, relevance: str) -> str | None:
    """
    Hook for paid Event Agent (agent_pm) per-event analysis.
    Stub: returns None (no LLM call). Wire real call later via config.route("summarize").
    Keeping signature stable so queue_worker → agent_pm is one line change.
    """
    # Example when ready:
    #   from config import route, estimate_tokens, PAID
    #   tier = route("summarize", estimate_tokens(row["title"]+row["description"]))
    #   if tier == PAID: call LLM and return summary
    return None


def process_one(conn: sqlite3.Connection, dry_run: bool = False, max_attempts: int = 3) -> str:
    """
    Claim and process one queued item. Returns status string for logging.
    Handles its own transaction for dequeue.
    """
    # Ensure row_factory is sqlite.Row for dequeue
    conn.row_factory = sqlite3.Row
    qrow = dequeue_one(conn)
    if qrow is None:
        # Check if processing is active vs empty
        cur = conn.execute("SELECT 1 FROM flash_queue WHERE status='processing' LIMIT 1")
        if cur.fetchone():
            return "busy: another item in 'processing' — queue will drain next tick"
        cur = conn.execute("SELECT COUNT(*) FROM flash_queue WHERE status='pending'")
        n = cur.fetchone()[0]
        if n == 0:
            return "empty: no pending items"
        return "busy: could not claim (concurrent worker)"

    qid = qrow["id"]
    raw_id = qrow["raw_event_id"]
    relevance = qrow["relevance"]
    dedupe_key = qrow["dedupe_key"]

    # Read-only guard — mark done without posting, still drains queue
    flag = get_feature_flag(conn)
    if flag == "read_only":
        mark_done(conn, qid)
        return f"read_only: drained queue id={qid} raw_id={raw_id} relevance={relevance} (no Discord)"

    # Fetch the raw event
    rrow = fetch_raw_event(conn, int(raw_id))
    if rrow is None:
        mark_failed(conn, qid, f"raw_event id={raw_id} not found", max_attempts)
        return f"failed: queue id={qid} raw_event {raw_id} missing"

    # Defensive: forexfactory/calendar should never be in pending, but guard
    src = (rrow["source"] or "").strip().lower()
    ttl = (rrow["title"] or "").lstrip()
    if src == "forexfactory" or ttl.startswith("Calendar:"):
        mark_skipped(conn, qid, "calendar/forexfactory — not posted by flash queue")
        return f"skipped: queue id={qid} calendar/forexfactory"

    # Defensive: re-check relevance (prefilter may have evolved)
    # Don't downgrade already-queued high/medium to skipped just because cleanup changed,
    # but if it's now low and was pending, honor skip
    # We trust the enqueue-time relevance, but log if it diverged
    raw_desc = rrow["description"] or ""
    try:
        j = json.loads(rrow["raw_json"]) if isinstance(rrow["raw_json"], str) and rrow["raw_json"] else None
        if isinstance(j, dict):
            jd = j.get("description") or j.get("snippet") or ""
            if len(jd) > len(raw_desc):
                raw_desc = jd
    except Exception:
        pass
    cleaned = clean_description(raw_desc, 300)
    # Use stored relevance as source of truth for FREE gate; but compute current for logging
    current_rel = classify_relevance(rrow["title"] or "", cleaned, rrow["symbols"] or "")

    if relevance == "low" or current_rel == "low":
        # Shouldn't happen for pending, but guard token spend
        mark_skipped(conn, qid, f"low relevance (stored={relevance} current={current_rel})")
        return f"skipped low: queue id={qid} raw_id={raw_id} stored={relevance} current={current_rel}"

    # Deterministic validation
    v_errs = validate_row_deterministic(rrow)
    if v_errs:
        mark_failed(conn, qid, f"validate: {'; '.join(v_errs)}", max_attempts)
        return f"failed validate: queue id={qid} {v_errs}"

    if dry_run:
        # Preview what would be posted — roll back to pending so real run still processes it
        preview = (rrow["title"] or "")[:100]
        print(f"[queue_worker] dry-run preview queue id={qid} raw_id={raw_id} relevance={relevance} ({current_rel}) dedupe={dedupe_key}")
        print(f"  title: {preview}")
        print(f"  cleaned: {cleaned[:150]}")
        print(f"  url: {(rrow['url'] or '')[:120]}")
        # revert claim so next real run still processes
        conn.execute("UPDATE flash_queue SET status='pending', processed_at=NULL WHERE id=?", (qid,))
        conn.commit()
        return f"dry-run: queue id={qid} would post '{preview[:60]}' [{relevance}]"

    # Build Discord embed (reuse flash_notify logic)
    webhook = os.getenv("DISCORD_FLASH_WEBHOOK_URL") or ""
    if not webhook:
        mark_failed(conn, qid, "DISCORD_FLASH_WEBHOOK_URL not set", max_attempts)
        return f"failed: queue id={qid} no DISCORD_FLASH_WEBHOOK_URL"

    if _build_embed is None or post_discord is None:
        mark_failed(conn, qid, "flash_notify helpers not available", max_attempts)
        return f"failed: queue id={qid} flash_notify helpers missing"

    embed = _build_embed(rrow, cleaned, relevance)  # type: ignore

    # Optional paid Event Agent hook (stub)
    agent_note = call_agent_pm_for_event(rrow, relevance)
    if agent_note:
        # If agent returns a note, attach as embed field or content
        embed.setdefault("fields", []).append({"name": "Agent", "value": agent_note[:400], "inline": False})

    ok, status = post_discord(webhook, [embed])  # type: ignore
    if not ok:
        mark_failed(conn, qid, f"discord {status}", max_attempts)
        return f"failed discord: queue id={qid} raw_id={raw_id} {status}"

    mark_done(conn, qid)
    return f"done: queue id={qid} raw_id={raw_id} [{relevance}] posted {status} — {(rrow['title'] or '')[:80]}"


def main():
    load_dotenv()
    p = argparse.ArgumentParser(description="Queue worker — FIFO flash_queue → Discord (FREE gate + optional paid hook)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true", help="process one pending item and exit")
    g.add_argument("--drain", action="store_true", help="drain up to --limit items in sequence")
    p.add_argument("--limit", type=int, default=10, help="max items when --drain (default 10)")
    p.add_argument("--dry-run", action="store_true", help="preview without posting; claim reverted to pending")
    p.add_argument("--max-attempts", type=int, default=3, help="retry limit before 'failed' (default 3)")
    p.add_argument("--loop", action="store_true", help="with --drain, loop until empty (for systemd long-run)")
    args = p.parse_args()

    if not args.once and not args.drain:
        args.once = True  # default

    db = db_path()
    db_file = Path(db)
    if not db_file.exists():
        # Try alt relative to src/
        alt = Path(__file__).parent.parent / db
        if alt.exists():
            db = str(alt)
            db_file = alt
    if not db_file.exists():
        print(f"[queue_worker] DB not found at {db} — run flash_poll first.", file=sys.stderr)
        sys.exit(0)

    conn = sqlite3.connect(db, timeout=10)
    try:
        ensure_schema(conn)
        # Print queue depth header
        try:
            cur = conn.execute("SELECT status, COUNT(*) FROM flash_queue GROUP BY status")
            depths = dict(cur.fetchall())
            if depths:
                print(f"[queue_worker] depths: {depths}")
            else:
                print("[queue_worker] queue empty")
        except sqlite3.OperationalError as e:
            print(f"[queue_worker] queue table not ready: {e}", file=sys.stderr)

        if args.once:
            res = process_one(conn, dry_run=args.dry_run, max_attempts=args.max_attempts)
            print(f"[queue_worker] {res}")
        else:
            # drain
            n = 0
            while n < args.limit:
                # Re-check depths for loop logging
                res = process_one(conn, dry_run=args.dry_run, max_attempts=args.max_attempts)
                print(f"[queue_worker] {res}")
                n += 1
                if res.startswith("empty") or res.startswith("busy"):
                    if not args.loop or res.startswith("busy"):
                        break
                    if res.startswith("empty"):
                        break
                # Small pause between items to avoid Discord rate limit
                if not args.dry_run and n < args.limit:
                    time.sleep(0.4)
                if args.dry_run:
                    # dry-run reverts claim, so break after one to avoid infinite loop
                    break
            print(f"[queue_worker] drain complete: processed {n} item(s)")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
