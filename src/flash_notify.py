"""
flash_notify.py — live flash news → second Discord webhook (DISCORD_FLASH_WEBHOOK_URL)

Reads recent raw_events (limit 10, where published_at >= last check or just latest),
FREE-tier heuristic cleanup (strip HTML, truncate ~300 chars), classifies relevance
high/medium/low for XAU/USD via keywords gold/XAU/CPI/FOMC/PBOC/etc, skips low,
then formats readable Discord webhook embed (title, cleaned description, source,
published_at, url, symbol tags, relevance badge).

Env: DISCORD_FLASH_WEBHOOK_URL (second webhook, flash), DB_PATH
Usage:
    python src/flash_notify.py --dry-run --limit 3
    python src/flash_notify.py --limit 10
    python src/flash_notify.py --limit 10 --dry-run

Tier: cleanup = FREE (heuristic, no LLM). Hook for fin-classifier LLM left as stub.
Chain position: after flash_xau + ingest + resolver, before validate/agent_pm.
"""

import argparse
import html
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from urllib.parse import quote

import requests
try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*a, **kw):
        return False

sys.path.insert(0, str(Path(__file__).parent))
try:
    from config import db_path, route, FREE  # noqa: E402
except ImportError:
    def db_path():  # fallback
        import os as _os
        return _os.getenv("DB_PATH", "data/fin_news.db")
    FREE = "free"
    def route(*a, **kw):
        return FREE

import os

# --- FREE-tier heuristic: HTML strip + truncate + keyword relevance ---
TAG_RE = re.compile(r"<[^>]+>")

# High = direct gold signal; Medium = macro that moves gold; Low = skip
HIGH_KEYWORDS = {"gold", "xau", "xauusd", "xau/usd", "pboc", "peoples bank", "people's bank"}
MEDIUM_KEYWORDS = {
    "cpi", "fomc", "fed", "federal reserve", "powell", "pce", "nfp", "nonfarm", "payroll",
    "rate decision", "rate hike", "rate cut", "dxy", "dollar index", "inflation",
    "gdp", "unemployment", "jobless", "ism", "pmi", "ecb", "boj", "treasury", "yields",
    "geopolitical", "safe haven", "safe-haven",
}
# combined for quick scan
ALL_RELEVANT = HIGH_KEYWORDS | MEDIUM_KEYWORDS

RELEVANCE_BADGE = {"high": "🔴 HIGH", "medium": "🟡 MEDIUM", "low": "⚪ LOW"}
RELEVANCE_COLOR = {"high": 0xE53935, "medium": 0xFB8C00, "low": 0x78909C}


def clean_description(raw: str, limit: int = 300) -> str:
    """FREE-tier cleanup: strip HTML tags, unescape, collapse whitespace, truncate ~300."""
    if not raw:
        return ""
    # heuristic tier routing marker (no LLM)
    # route("clean", ...) would return FREE — we keep it deterministic here
    text = TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def classify_relevance(title: str, description: str, symbols: str = "") -> str:
    """
    FREE-tier classifier (heuristic). Returns high|medium|low.
    Hook for future LLM fin-classifier: replace body with route("classify") → PAID/FREE LLM call.
    """
    hay = f"{title or ''} {description or ''} {symbols or ''}".lower()
    # hook placeholder — when fin-classifier LLM is ready:
    #   tier = route("classify", estimate_tokens(hay))
    #   if tier == PAID: return call_llm_classifier(hay)
    if any(k in hay for k in HIGH_KEYWORDS):
        return "high"
    if any(k in hay for k in MEDIUM_KEYWORDS):
        return "medium"
    # also tag XAU symbol as medium at minimum if symbols explicitly XAU
    if "xau" in (symbols or "").lower():
        return "medium"
    return "low"


def classify_relevance_llm_hook(*_a, **_kw):
    """Stub for future fin-classifier LLM integration. Keep signature stable."""
    return None


def get_last_flash_at(conn: sqlite3.Connection) -> str | None:
    try:
        cur = conn.execute("SELECT value FROM status WHERE key='last_flash_notify_at'")
        row = cur.fetchone()
        if row and row[0]:
            return row[0]
    except sqlite3.OperationalError:
        pass
    return None


def fetch_recent(conn: sqlite3.Connection, limit: int = 10, since: str | None = None) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    if since:
        # published_at stored as ISO TEXT; string compare works for ISO8601 UTC
        cur = conn.execute(
            """
            SELECT id, source, title, description, url, published_at, symbols, raw_json, created_at
            FROM raw_events
            WHERE published_at >= ?
            ORDER BY datetime(published_at) DESC, id DESC
            LIMIT ?
            """,
            (since, limit),
        )
        rows = cur.fetchall()
        if rows:
            return rows
        # fallback to latest if since filter yields 0 (e.g. stale cursor)
    cur = conn.execute(
        """
        SELECT id, source, title, description, url, published_at, symbols, raw_json, created_at
        FROM raw_events
        ORDER BY datetime(published_at) DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    )
    return cur.fetchall()


def build_embed(row: sqlite3.Row, cleaned: str, relevance: str) -> dict:
    title = (row["title"] or "(no title)").strip()[:256]
    url = sanitize_url((row["url"] or "").strip())
    source = row["source"] or "unknown"
    published_at = row["published_at"] or row["created_at"] or ""
    symbols = row["symbols"] or "—"
    badge = RELEVANCE_BADGE.get(relevance, relevance)
    color = RELEVANCE_COLOR.get(relevance, 0x78909C)

    # Discord embed: title + description + fields
    embed = {
        "title": f"{badge} {title}"[:256],
        "description": cleaned[:300] if cleaned else "_no description_",
        "color": color,
        "fields": [
            {"name": "Source", "value": source[:100], "inline": True},
            {"name": "Symbols", "value": symbols[:50], "inline": True},
            {"name": "Relevance", "value": badge, "inline": True},
            {"name": "Published", "value": published_at[:50] or "—", "inline": True},
        ],
        "footer": {"text": "fin-news-assistant flash • FREE-tier heuristic (LLM hook ready)"},
        "timestamp": _to_iso_timestamp(published_at),
    }
    if url:
        embed["url"] = url[:512]
        # also add as field for visibility if title url not rendered
        embed["fields"].append({"name": "Link", "value": url[:512], "inline": False})
    return embed


def sanitize_url(u: str) -> str:
    """URL-encode unsafe chars (e.g. spaces in fragments) so Discord accepts embed url.

    Discord 400s with {"embeds": ["<idx>"]} when the url field is not a valid URL
    (observed 2026-10-12: forexfactory calendar links carry raw spaces in #fragments).
    """
    if not u:
        return ""
    return quote(u, safe=":/?#&=%")


def _to_iso_timestamp(s: str) -> str | None:
    if not s:
        return None
    try:
        # try parse common forms; fallback to now
        from email.utils import parsedate_to_datetime
        # try ISO first
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        try:
            from email.utils import parsedate_to_datetime as pdt
            dt = pdt(s)
            if dt:
                return dt.astimezone(timezone.utc).isoformat()
        except Exception:
            pass
    return None


def post_discord(webhook_url: str, embeds: list[dict], content: str | None = None) -> tuple[bool, str]:
    """Post embeds to Discord webhook. Returns (ok, status_text)."""
    # Discord allows max 10 embeds per request; chunk if needed
    payload = {"embeds": embeds[:10]}
    if content:
        payload["content"] = content[:1900]
    try:
        r = requests.post(webhook_url, json=payload, timeout=15)
        if r.status_code in (200, 204):
            return True, f"{r.status_code}"
        return False, f"{r.status_code} {r.text[:300]}"
    except Exception as e:
        return False, str(e)


def main():
    load_dotenv()
    p = argparse.ArgumentParser(description="Flash notify → second Discord webhook (DISCORD_FLASH_WEBHOOK_URL)")
    p.add_argument("--limit", type=int, default=10, help="recent rows to consider (default 10)")
    p.add_argument("--dry-run", action="store_true", help="preview without posting")
    p.add_argument("--since", type=str, default=None, help="override since filter (ISO published_at)")
    p.add_argument("--post-all", action="store_true", help="post even low relevance (default skips low)")
    p.add_argument("--include-calendar", action="store_true", help="include forexfactory/calendar rows (default excludes them; still stored in raw_events, just not posted)")
    args = p.parse_args()

    webhook = os.getenv("DISCORD_FLASH_WEBHOOK_URL") or ""
    if not webhook and not args.dry_run:
        print("[flash_notify] DISCORD_FLASH_WEBHOOK_URL not set — use --dry-run to preview or set env.", file=sys.stderr)
        # still allow dry-run without webhook

    db = db_path()
    db_file = Path(db)
    if not db_file.exists() and not db_file.is_absolute():
        alt = Path(__file__).parent.parent / db
        if alt.exists():
            db = str(alt)
            db_file = alt
    if not db_file.exists():
        print(f"[flash_notify] DB not found at {db} — run flash_xau/ingest first. (dry-run will show 0 rows)")
        # create empty preview for dry-run
        if not args.dry_run:
            sys.exit(0)

    conn = sqlite3.connect(db) if db_file.exists() else None
    try:
        if conn is None:
            rows: list[sqlite3.Row] = []
            since_val = args.since
        else:
            # ensure schema exists (idempotent)
            try:
                schema_file = Path(__file__).parent.parent / "schema.sql"
                if schema_file.exists():
                    conn.executescript(schema_file.read_text())
                    conn.commit()
            except Exception:
                pass
            since_val = args.since if args.since is not None else get_last_flash_at(conn)
            if since_val:
                print(f"[flash_notify] since filter: {since_val}")
            rows = fetch_recent(conn, limit=args.limit, since=since_val)
        print(f"[flash_notify] fetched {len(rows)} candidate(s) (limit {args.limit})")

        # FREE-tier cleanup + classify — skip forexfactory/calendar by default (still stored, just not posted)
        kept: list[tuple[sqlite3.Row, str, str]] = []  # (row, cleaned, relevance)
        skipped_low = 0
        skipped_calendar = 0
        for r in rows:
            # --- forexfactory exclusion (default) ---
            if not args.include_calendar:
                src = (r["source"] or "").strip().lower()
                ttl = (r["title"] or "").lstrip()
                if src == "forexfactory" or ttl.startswith("Calendar:"):
                    skipped_calendar += 1
                    continue
            raw_desc = r["description"] or ""
            # try raw_json description fallback for richer HTML
            try:
                j = json.loads(r["raw_json"]) if isinstance(r["raw_json"], str) and r["raw_json"] else None
                if isinstance(j, dict):
                    # prefer raw_json description if longer
                    jd = j.get("description") or j.get("snippet") or ""
                    if len(jd) > len(raw_desc):
                        raw_desc = jd
            except Exception:
                pass
            cleaned = clean_description(raw_desc, limit=300)
            relevance = classify_relevance(r["title"] or "", cleaned, r["symbols"] or "")
            if relevance == "low" and not args.post_all:
                skipped_low += 1
                continue
            kept.append((r, cleaned, relevance))

        print(f"[flash_notify] FREE-tier heuristic: kept {len(kept)} (high/medium), skipped low={skipped_low}, skipped calendar/forexfactory={skipped_calendar}")
        if not kept:
            print("[flash_notify] nothing to notify (all low/calendar or no rows).")
            if args.dry_run:
                # still show what was skipped for visibility
                for r in rows[:5]:
                    raw = (r["description"] or "")[:120]
                    rel = classify_relevance(r["title"] or "", raw, r["symbols"] or "")
                    src = (r["source"] or "").strip()
                    is_cal = src.lower() == "forexfactory" or (r["title"] or "").lstrip().startswith("Calendar:")
                    tag = "calendar/forexfactory" if is_cal else f"low={rel}"
                    print(f"  - skip preview {tag} id={r['id']} source={src} title={(r['title'] or '')[:80]}")
            return

        embeds = [build_embed(r, cleaned, rel) for r, cleaned, rel in kept]

        # preview
        print(f"[flash_notify] preview {len(embeds)} embed(s):")
        for i, e in enumerate(embeds[:3], 1):
            print(f"\n--- embed {i}/{len(embeds)} ---")
            print(json.dumps(e, indent=2, ensure_ascii=False)[:1800])
        if len(embeds) > 3:
            print(f"... +{len(embeds)-3} more embed(s) not shown")

        if args.dry_run:
            print(f"\n[flash_notify] dry-run — not posting (would post {len(embeds)} embed(s) to DISCORD_FLASH_WEBHOOK_URL len={len(webhook) if webhook else 0})")
            return

        if not webhook:
            print("[flash_notify] no webhook — aborting post.", file=sys.stderr)
            sys.exit(1)

        # Discord: max 10 embeds per webhook call
        ok_all = True
        for chunk_start in range(0, len(embeds), 10):
            chunk = embeds[chunk_start: chunk_start + 10]
            ok, status = post_discord(webhook, chunk)
            print(f"[flash_notify] post chunk {chunk_start//10+1} size={len(chunk)} -> {status} {'ok' if ok else 'FAILED'}")
            ok_all = ok_all and ok
            if not ok:
                break

        if ok_all and conn is not None:
            now_iso = datetime.now(timezone.utc).isoformat()
            try:
                conn.execute(
                    "INSERT INTO status(key, value) VALUES ('last_flash_notify_at', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                    (now_iso,),
                )
                conn.commit()
                print(f"[flash_notify] updated status.last_flash_notify_at={now_iso}")
            except Exception as e:
                print(f"[flash_notify] status update failed: {e}", file=sys.stderr)

        if not ok_all:
            sys.exit(1)

    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    main()
