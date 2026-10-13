"""
agent_pm.py — Agent PM reads DB LIMIT 50 and summarizes what is happening.

Locked arch: SQLite dumb text store (WAL) + Agent PM reader/manager.
Tiered routing: summarize = hard → paid 3.8B API (sunk cost), fallback free.
RTK + headroom budget checked before every LLM call (stub via config.estimate_tokens).

v1 stub: no real LLM call yet — builds deterministic summary from DB rows,
inserts into summaries, updates status. Swap `call_llm()` with real client later.

Usage:
    python src/agent_pm.py
    python src/agent_pm.py --limit 50 --post-discord
"""

import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

# allow `python src/agent_pm.py`
sys.path.insert(0, str(Path(__file__).parent))

from config import db_path, discord_webhook, estimate_tokens, route, FREE, PAID  # noqa: E402


def fetch_recent(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """
        SELECT id, source, title, description, url, published_at, symbols, created_at
        FROM raw_events
        ORDER BY datetime(published_at) DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    )
    return cur.fetchall()


def build_stub_summary(rows: list[sqlite3.Row]) -> str:
    """Deterministic stub so v1 works without LLM keys. Replace body with LLM later."""
    if not rows:
        return "No events in DB yet — run ingest first."

    total = len(rows)
    sources = Counter(r["source"] for r in rows)
    symbols = Counter()
    for r in rows:
        if r["symbols"]:
            for s in r["symbols"].split(","):
                s = s.strip()
                if s:
                    symbols[s] += 1

    latest = rows[0]
    oldest = rows[-1]

    lines = [
        f"Fin News PM Summary — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"Scope: last {total} events (LIMIT 50, dumb store)",
        f"Sources: {dict(sources)} | Symbols: {dict(symbols) if symbols else 'n/a'}",
        f"Window: {oldest['published_at'] or oldest['created_at']} → {latest['published_at'] or latest['created_at']}",
        "",
        "Top headlines:",
    ]
    for r in rows[:10]:
        sym = f" [{r['symbols']}]" if r["symbols"] else ""
        lines.append(f"- {r['title'] or '(no title)'}{sym} — {r['url'] or 'no url'}")
    if total > 10:
        lines.append(f"... +{total - 10} more in DB (not shown)")

    lines += [
        "",
        "Next: Evidence Bundle → Fact Extraction → Validation → Discord (full pipeline v1).",
        "Note: stub summary (no LLM). Wire call_llm() to paid tier when ready.",
    ]
    return "\n".join(lines)


def call_llm_stub(prompt: str, tier: str) -> str:
    """
    Placeholder for real LLM call.
    - tier == 'paid' → would use PAID_API_KEY (OpenAI/Anthropic 3.8B quota)
    - tier == 'free' → free-tier model
    For v1, return empty so caller falls back to stub. Keep signature stable.
    """
    # Example real wiring (commented, minimal):
    # if tier == PAID and os.getenv("PAID_API_KEY"):
    #     client = OpenAI(api_key=os.getenv("PAID_API_KEY"))
    #     resp = client.chat.completions.create(model="gpt-4o-mini", messages=[...])
    #     return resp.choices[0].message.content
    return ""


def post_discord(webhook_url: str, text: str) -> bool:
    try:
        r = requests.post(webhook_url, json={"content": text[:1900]}, timeout=10)
        return r.status_code in (200, 204)
    except Exception as e:
        print(f"[agent_pm] discord post failed: {e}")
        return False


def main():
    load_dotenv()
    import argparse

    p = argparse.ArgumentParser(description="Agent PM — summarize DB LIMIT 50")
    p.add_argument("--limit", type=int, default=50, help="rows to read (default 50)")
    p.add_argument("--post-discord", action="store_true", help="post summary to Discord if webhook set")
    p.add_argument("--dry-run", action="store_true", help="print only, don't write summaries table")
    args = p.parse_args()

    db = db_path()
    db_file = Path(db)
    if not db_file.exists():
        # ensure schema exists even on first run
        db_file.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        schema = Path(__file__).parent.parent / "schema.sql"
        if schema.exists():
            conn.executescript(schema.read_text())
            conn.commit()
        conn.close()

    conn = sqlite3.connect(db)
    try:
        # exam-week guard: read_only flag
        try:
            cur = conn.execute("SELECT value FROM status WHERE key='feature_flag'")
            row = cur.fetchone()
            if row and row[0] == "read_only":
                print("[agent_pm] feature_flag=read_only (exam week) — skipping summarization.")
                return
        except sqlite3.OperationalError:
            pass  # status table not yet created

        rows = fetch_recent(conn, limit=args.limit)
        print(f"[agent_pm] read {len(rows)} rows (LIMIT {args.limit}) from {db}")

        # tiered routing: summarize is hard → paid with budget check
        prompt_hint = "\n".join(r["title"] or "" for r in rows[:20])
        est = estimate_tokens(prompt_hint + " summarize USD/XAU macro")
        tier = route("summarize", estimated_tokens=est)
        print(f"[agent_pm] tier={tier} est_tokens~{est} (hard→paid, easy→free, headroom applied)")

        llm_text = call_llm_stub(prompt_hint, tier)
        summary = llm_text.strip() if llm_text else build_stub_summary(rows)
        print("--- summary preview ---")
        print(summary[:2000])
        print("--- end preview ---")

        if args.dry_run:
            print("[agent_pm] dry-run — not writing to DB")
            return

        # write to summaries (link to latest raw_event)
        latest_id = rows[0]["id"] if rows else None
        conn.execute(
            "INSERT INTO summaries (raw_event_id, summary_text, model_tier, model_name, tokens_used) VALUES (?, ?, ?, ?, ?)",
            (latest_id, summary, tier, f"stub-{tier}", est),
        )
        conn.execute(
            "INSERT INTO status(key, value) VALUES ('last_summary_at', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.commit()
        print(f"[agent_pm] wrote summary tier={tier} → summaries + status.last_summary_at")

        if args.post_discord:
            wh = discord_webhook()
            if wh:
                ok = post_discord(wh, summary)
                print(f"[agent_pm] discord post {'ok' if ok else 'failed'}")
            else:
                print("[agent_pm] --post-discord set but DISCORD_WEBHOOK_URL not configured")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
