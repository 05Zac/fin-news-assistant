"""
flash_xau.py — XAUUSD flash for fin-news-assistant (Fable's last-night recommendation)

English, XAUUSD-focused, 0 API keys. Deploys on Linux gateway 24/7, text-only SQLite WAL.
Providers:
  1) InvestingLive (ex-ForexLive) RSS — https://www.investinglive.com/feed/ — FX/macro flash <10s, no key
  2) ForexFactory Calendar JSON — https://nfs.faireconomy.media/ff_calendar_thisweek.json — CPI/NFP/FOMC, no key

Same pattern as ingest.py: text-only, WAL, feature_flag guard, url dedupe → raw_events.

Usage:
  python src/flash_xau.py --dry-run        # fetch + preview, no DB write
  python src/flash_xau.py                  # fetch → store to SQLite
  python src/flash_xau.py --limit 20       # cap per-provider items
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
import xml.etree.ElementTree as ET

import requests
try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*a, **kw):
        return False

sys.path.insert(0, str(Path(__file__).parent))
from config import db_path  # noqa: E402

INVESTINGLIVE_RSS = "https://www.investinglive.com/feed/"
FF_CALENDAR_JSON = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# Keywords that matter for XAUUSD — used to tag relevance, not to drop rows
XAU_KEYWORDS = {"gold", "xau", "xauusd", "federal reserve", "fed", "cpi", "nfp", "nonfarm", "payroll", "fomc", "rate decision", "dxy", "dollar", "inflation", "pce", "gdp", "unemployment"}


def ensure_schema(conn: sqlite3.Connection):
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    schema_file = Path(__file__).parent.parent / "schema.sql"
    if schema_file.exists():
        conn.executescript(schema_file.read_text())
    conn.commit()


def is_feature_active(conn: sqlite3.Connection) -> bool:
    try:
        cur = conn.execute("SELECT value FROM status WHERE key='feature_flag'")
        row = cur.fetchone()
        if row and row[0] == "read_only":
            return False
    except sqlite3.OperationalError:
        pass
    return True


def fetch_investinglive(limit: int = 20) -> list[dict]:
    """Fetch InvestingLive RSS, return normalized article dicts."""
    resp = requests.get(INVESTINGLIVE_RSS, timeout=20, headers={"User-Agent": "fin-news-assistant/1.0"})
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    # RSS: channel/item
    items = []
    for item in root.findall(".//item")[:limit]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "").strip()
        # dc:creator or pubDate
        pub = (item.findtext("pubDate") or item.findtext("{http://purl.org/dc/elements/1.1/}date") or "").strip()
        # try parse pubDate to ISO
        published_at = pub
        try:
            # e.g. Tue, 11 Oct 2026 07:00:00 GMT
            from email.utils import parsedate_to_datetime
            dt = parsedate_to_datetime(pub)
            published_at = dt.astimezone(timezone.utc).isoformat()
        except Exception:
            pass
        # relevance tagging
        hay = f"{title} {desc}".lower()
        symbols = "XAU,USD" if any(k in hay for k in XAU_KEYWORDS) else "USD"
        items.append({
            "uuid": link or title[:80],
            "title": title,
            "description": desc[:800],
            "url": link,
            "published_at": published_at,
            "entities": [{"symbol": s} for s in symbols.split(",")],
            "_source": "investinglive",
        })
    return items


def fetch_ff_calendar(limit: int = 20) -> list[dict]:
    """Fetch ForexFactory calendar JSON, return normalized event dicts."""
    resp = requests.get(FF_CALENDAR_JSON, timeout=20, headers={"User-Agent": "fin-news-assistant/1.0"})
    resp.raise_for_status()
    data = resp.json()
    # FF JSON is list of events: {title, country, category, impact, date, time, actual, forecast, previous}
    events = data if isinstance(data, list) else data.get("events", []) if isinstance(data, dict) else []
    # Filter to USD/high-impact that moves gold, but keep all USD for completeness and cap
    filtered = []
    for e in events:
        country = (e.get("country") or e.get("currency") or "").upper()
        impact = (e.get("impact") or "").lower()
        title = (e.get("title") or e.get("event") or "").strip()
        # Keep USD events, especially high/medium; keep XAU-relevant titles regardless
        hay = title.lower()
        is_usd = country in ("USD", "US", "USA")
        is_high = impact in ("high", "red", "3")
        is_xau_relevant = any(k in hay for k in XAU_KEYWORDS)
        if not (is_usd or is_xau_relevant):
            # skip low-impact non-USD noise
            if not is_high:
                continue
        filtered.append(e)
        if len(filtered) >= limit * 3:  # over-fetch then trim
            break
    # Normalize to article-like dicts for same raw_events table
    items = []
    for e in filtered[:limit]:
        title = (e.get("title") or e.get("event") or "FF Calendar Event").strip()
        country = e.get("country") or e.get("currency") or "USD"
        impact = e.get("impact") or e.get("importance") or ""
        dt_str = e.get("date") or e.get("datetime") or e.get("timestamp") or ""
        # FF date often "2026-10-11" + time "08:30"
        if e.get("date") and e.get("time"):
            dt_str = f"{e.get('date')}T{e.get('time')}:00Z"
        actual = e.get("actual") or ""
        forecast = e.get("forecast") or ""
        prev = e.get("previous") or ""
        desc = f"[{country} {impact}] {title} — Actual:{actual} Forecast:{forecast} Prev:{prev}".strip()
        url = f"https://www.forexfactory.com/calendar?week=this#{title[:30]}"
        items.append({
            "uuid": f"ff-{dt_str}-{title[:30]}",
            "title": f"Calendar: {title} ({country})",
            "description": desc[:800],
            "url": url,
            "published_at": dt_str,
            "entities": [{"symbol": "USD"}, {"symbol": "XAU"}],
            "_source": "forexfactory",
            "_raw": e,
        })
    return items


def store_raw(conn: sqlite3.Connection, articles: list[dict], source: str) -> int:
    inserted = 0
    for a in articles:
        external_id = str(a.get("uuid") or a.get("id") or "")
        title = a.get("title")
        description = a.get("description") or a.get("snippet") or ""
        url = a.get("url")
        published_at = a.get("published_at") or ""
        entities = a.get("entities") or []
        symbols = ",".join(e.get("symbol") for e in entities if isinstance(e, dict) and e.get("symbol")) or "USD"
        raw_json = json.dumps(a, ensure_ascii=False)
        if url:
            cur = conn.execute("SELECT 1 FROM raw_events WHERE url = ? LIMIT 1", (url,))
            if cur.fetchone():
                continue
        conn.execute(
            "INSERT INTO raw_events (source, external_id, title, description, url, published_at, symbols, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (source, external_id, title, description, url, published_at, symbols, raw_json),
        )
        inserted += 1
    conn.commit()
    if inserted:
        conn.execute(
            "INSERT INTO status(key, value) VALUES ('last_ingest_at', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.commit()
    return inserted


def main():
    load_dotenv()
    p = argparse.ArgumentParser(description="XAUUSD flash — InvestingLive RSS + FF Calendar → SQLite WAL")
    p.add_argument("--limit", type=int, default=15, help="per-provider limit (default 15)")
    p.add_argument("--dry-run", action="store_true", help="fetch + preview, no DB write")
    p.add_argument("--source", choices=["investinglive", "forexfactory", "all"], default="all")
    args = p.parse_args()

    all_articles: list[tuple[str, list[dict]]] = []

    if args.source in ("investinglive", "all"):
        try:
            arts = fetch_investinglive(limit=args.limit)
            print(f"[flash_xau] investinglive fetched {len(arts)}")
            all_articles.append(("investinglive", arts))
        except Exception as e:
            print(f"[flash_xau] investinglive failed: {e}", file=sys.stderr)

    if args.source in ("forexfactory", "all"):
        try:
            arts = fetch_ff_calendar(limit=args.limit)
            print(f"[flash_xau] forexfactory fetched {len(arts)}")
            all_articles.append(("forexfactory", arts))
        except Exception as e:
            print(f"[flash_xau] forexfactory failed: {e}", file=sys.stderr)

    total = sum(len(a) for _, a in all_articles)
    print(f"[flash_xau] total {total} articles/events")

    if args.dry_run:
        for src, arts in all_articles:
            print(f"\n--- {src} preview (2) ---")
            print(json.dumps(arts[:2], indent=2, ensure_ascii=False)[:3000])
        return

    db = db_path()
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    try:
        ensure_schema(conn)
        if not is_feature_active(conn):
            print("[flash_xau] feature_flag=read_only (exam week) — skipping store.")
            return
        inserted_total = 0
        for src, arts in all_articles:
            ins = store_raw(conn, arts, source=src)
            print(f"[flash_xau] inserted {ins}/{len(arts)} ({src}) → {db}")
            inserted_total += ins
        print(f"[flash_xau] done inserted {inserted_total}/{total} → {db} (WAL)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
