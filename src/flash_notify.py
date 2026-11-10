"""
flash_notify.py — live flash news → second Discord webhook (DISCORD_FLASH_WEBHOOK_URL)

Reads recent raw_events (limit 10, where published_at >= last check or just latest),
FREE-tier heuristic cleanup (strip HTML, truncate ~300 chars), classifies relevance
high/medium/low for XAU/USD via keywords gold/XAU/CPI/FOMC/PBOC/etc, skips low,
then formats readable Discord webhook embed (title, cleaned description, source,
published_at, url, ticker and topic tags).

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

sys.path.insert(0, str(Path(__file__).parent))
from prefilter import classify_relevance
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

# --- optional live price (Finnhub) — never log key ---
try:
    from price import get_quote, get_quotes, get_history  # type: ignore  # python src/flash_notify.py
    _HAS_PRICE = True
except ImportError:
    try:
        from src.price import get_quote, get_quotes, get_history  # type: ignore  # python -m src.flash_notify
        _HAS_PRICE = True
    except Exception:
        _HAS_PRICE = False
        get_quote = get_quotes = get_history = None  # type: ignore

# --- FREE-tier heuristic: HTML strip + truncate + keyword relevance ---
TAG_RE = re.compile(r"<[^>]+>")

EMBED_COLOR = 0x78909C


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
            SELECT id, source, title, description, url, published_at, symbols, topics, raw_json, created_at
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
        SELECT id, source, title, description, url, published_at, symbols, topics, raw_json, created_at
        FROM raw_events
        ORDER BY datetime(published_at) DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    )
    return cur.fetchall()


def build_embed(row: sqlite3.Row, cleaned: str, relevance: str = "") -> dict:
    # relevance is no longer shown (every article posts; the tickers say what it is about)
    title = (row["title"] or "(no title)").strip()[:256]
    url = sanitize_url((row["url"] or "").strip())
    source = row["source"] or "unknown"
    published_at = row["published_at"] or row["created_at"] or ""
    symbols = row["symbols"] or "—"

    # Discord embed: title + description + fields
    embed = {
        "title": title,
        "description": cleaned[:300] if cleaned else "_no description_",
        "color": EMBED_COLOR,
        "fields": [
            {"name": "Source", "value": source[:100], "inline": True},
            {"name": "Affects", "value": symbols[:50], "inline": True},
            {"name": "Published", "value": published_at[:50] or "—", "inline": True},
        ],
        "footer": {"text": "fin-news-assistant flash • FREE-tier heuristic (LLM hook ready)"},
        "timestamp": _to_iso_timestamp(published_at),
    }
    topics = row["topics"] if "topics" in row.keys() else None
    if topics:
        embed["fields"].insert(2, {"name": "Topics", "value": topics[:100], "inline": True})
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


# ---------- price helpers (Finnhub live) ----------
# Map display symbols → Finnhub query symbol
_PRICE_DISPLAY_MAP: dict[str, str] = {
    "XAU": "OANDA:XAU_USD",
    "XAUUSD": "OANDA:XAU_USD",
    "XAU_USD": "OANDA:XAU_USD",
    "GOLD": "OANDA:XAU_USD",
    "GC=F": "OANDA:XAU_USD",
    "BTC": "BINANCE:BTCUSDT",
    "BTC-USD": "BINANCE:BTCUSDT",
    "BTCUSD": "BINANCE:BTCUSDT",
    "BTCUSDT": "BINANCE:BTCUSDT",
    # Finnhub's "USD" ticker is an unrelated stock; use the UUP dollar-index ETF as the USD proxy
    "USD": "UUP",
    "DXY": "UUP",
    "USDX": "UUP",
}

_PRICE_REVERSE_DISPLAY: dict[str, str] = {
    "OANDA:XAU_USD": "XAU",
    "BINANCE:BTCUSDT": "BTC",
    "UUP": "USD idx (UUP)",
}


def _to_finnhub_symbol(sym: str) -> str:
    raw = (sym or "").strip().upper()
    if not raw or raw == "—":
        return ""
    if raw in _PRICE_DISPLAY_MAP:
        return _PRICE_DISPLAY_MAP[raw]
    # normalized lookup (strip - /)
    norm = raw.replace("-", "").replace("/", "").replace("=", "")
    if norm in _PRICE_DISPLAY_MAP:
        return _PRICE_DISPLAY_MAP[norm]
    return raw


def _display_for_finnhub(fin_sym: str) -> str:
    return _PRICE_REVERSE_DISPLAY.get(fin_sym, fin_sym)


def _collect_symbols_for_price(kept: list[tuple]) -> list[str]:
    """Collect unique Finnhub symbols from kept rows, capped at 5 for rate limit."""
    seen: list[str] = []
    for r, _, _ in kept:
        for s in (r["symbols"] or "").split(","):
            s = s.strip()
            if not s or s == "—":
                continue
            fin = _to_finnhub_symbol(s)
            if fin and fin not in seen:
                seen.append(fin)
                if len(seen) >= 5:
                    break
        if len(seen) >= 5:
            break
    return seen


def _fetch_price_line(symbols: list[str]) -> tuple[str | None, dict]:
    """Fetch get_quotes for symbols; returns (formatted line or None, quotes dict)."""
    if not symbols or not _HAS_PRICE or get_quotes is None:
        return None, {}
    try:
        quotes: dict = get_quotes(symbols)  # type: ignore
        # XAU fallback: if OANDA:XAU_USD missing/zero, try GC=F then Yahoo history
        if "OANDA:XAU_USD" in symbols:
            q = quotes.get("OANDA:XAU_USD") or {}
            needs_fallback = q.get("error") or not q.get("c")
            if needs_fallback and get_quote is not None and get_history is not None:
                try:
                    alt = get_quote("GC=F")  # type: ignore
                    if alt and alt.get("c") and alt.get("c") != 0:
                        quotes["OANDA:XAU_USD"] = alt
                    else:
                        hist = get_history("GC=F", days=2)  # type: ignore
                        if hist and isinstance(hist, list) and hist and hist[-1].get("close"):
                            last = hist[-1]
                            prev = hist[-2] if len(hist) >= 2 and hist[-2].get("close") else None
                            c = last["close"]
                            dp = 0.0
                            if prev and prev.get("close"):
                                try:
                                    dp = (c - prev["close"]) / prev["close"] * 100
                                except Exception:
                                    dp = 0.0
                            quotes["OANDA:XAU_USD"] = {"c": c, "d": c - (prev["close"] if prev else c), "dp": dp, "_fallback": "yahoo"}
                except Exception:
                    pass
        parts: list[str] = []
        for sym in symbols:
            q = quotes.get(sym) or {}
            if not q or q.get("error"):
                continue
            c = q.get("c")
            if c is None or c == 0:
                continue
            dp = q.get("dp")
            disp = _display_for_finnhub(sym)
            try:
                if dp is not None:
                    parts.append(f"{disp} ${c:,.2f} ({dp:+.2f}%)")
                else:
                    parts.append(f"{disp} ${c:,.2f}")
            except Exception:
                continue
        if not parts:
            return None, quotes
        return "Live: " + "  ".join(parts), quotes
    except Exception as e:
        print(f"[flash_notify] price fetch failed (non-blocking): {e}", file=sys.stderr)
        return None, {}


def _attach_price_to_embeds(embeds: list[dict], kept: list[tuple], quotes: dict) -> None:
    """Enrich each embed with a Live Price field matching its row symbols (mutates embeds)."""
    if not quotes:
        return
    for idx, (r, _, _) in enumerate(kept):
        if idx >= len(embeds):
            break
        row_syms = [s.strip() for s in (r["symbols"] or "").split(",") if s.strip()]
        fin_syms = [_to_finnhub_symbol(s) for s in row_syms]
        lines: list[str] = []
        for fs in fin_syms:
            q = quotes.get(fs)
            if not q or q.get("error") or not q.get("c"):
                continue
            c = q.get("c")
            dp = q.get("dp")
            disp = _display_for_finnhub(fs)
            try:
                if dp is not None:
                    lines.append(f"{disp} ${c:,.2f} ({dp:+.2f}%)")
                else:
                    lines.append(f"{disp} ${c:,.2f}")
            except Exception:
                continue
        if lines:
            # append as field; keep embed size safe
            try:
                embeds[idx].setdefault("fields", []).append(
                    {"name": "Live Price", "value": "  ".join(lines)[:512], "inline": False}
                )
            except Exception:
                pass


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
    p.add_argument("--post-all", action="store_true", help="no-op: every article is posted now")
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

        # --- live price enrichment (non-blocking, Finnhub) ---
        price_content: str | None = None
        price_quotes: dict = {}
        try:
            syms = _collect_symbols_for_price(kept)
            if syms and _HAS_PRICE:
                line, quotes = _fetch_price_line(syms)
                if line:
                    price_content = line
                    price_quotes = quotes
                    print(f"[flash_notify] price {line}")
                    _attach_price_to_embeds(embeds, kept, quotes)
                else:
                    print("[flash_notify] price (no quote data — offline or market closed)")
            else:
                if not _HAS_PRICE:
                    print("[flash_notify] price module not available (offline)")
        except Exception as e:
            print(f"[flash_notify] price enrichment skipped (non-blocking): {e}", file=sys.stderr)

        # preview
        print(f"[flash_notify] preview {len(embeds)} embed(s):")
        for i, e in enumerate(embeds[:3], 1):
            print(f"\n--- embed {i}/{len(embeds)} ---")
            print(json.dumps(e, indent=2, ensure_ascii=False)[:1800])
        if len(embeds) > 3:
            print(f"... +{len(embeds)-3} more embed(s) not shown")

        if price_content:
            print(f"[flash_notify] content price line: {price_content}")
        if args.dry_run:
            print(f"\n[flash_notify] dry-run — not posting (would post {len(embeds)} embed(s) to DISCORD_FLASH_WEBHOOK_URL len={len(webhook) if webhook else 0})")
            if price_content:
                print(f"[flash_notify] dry-run content would be: {price_content}")
            return

        if not webhook:
            print("[flash_notify] no webhook — aborting post.", file=sys.stderr)
            sys.exit(1)

        # Discord: max 10 embeds per webhook call (first chunk carries price content)
        ok_all = True
        for chunk_start in range(0, len(embeds), 10):
            chunk = embeds[chunk_start: chunk_start + 10]
            content = price_content if chunk_start == 0 else None
            ok, status = post_discord(webhook, chunk, content=content)
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
