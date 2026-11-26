"""
agent_pm.py — Agent PM reads DB LIMIT 50 and summarizes what is happening.

Locked arch: SQLite dumb text store (WAL) + Agent PM reader/manager.
LLM: free tier (FreeLLMAPI) via llm.py; deterministic stub is the fallback.
RTK + headroom budget checked before every LLM call (stub via config.estimate_tokens).

Builds a free-tier AI summary from DB rows (deterministic summary as fallback),
inserts into summaries, updates status. AI text comes from the free tier (llm.py).

Usage:
    python src/agent_pm.py
    python src/agent_pm.py --limit 50 --post-discord
"""

import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

# allow `python src/agent_pm.py` and `python -m src.agent_pm`
sys.path.insert(0, str(Path(__file__).parent))
try:
    from config import db_path, discord_webhook, estimate_tokens, route, FREE  # noqa: E402  # python src/agent_pm.py
    from store import migrate  # noqa: E402
    _HAS_CONFIG = True
except ImportError:
    from src.config import db_path, discord_webhook, estimate_tokens, route, FREE  # noqa: E402  # python -m src.agent_pm
    from src.store import migrate  # noqa: E402
    _HAS_CONFIG = True

# --- optional live price (Finnhub) — never log key ---
try:
    from price import get_quote, get_quotes, get_history  # type: ignore  # python src/agent_pm.py
    _HAS_PRICE = True
except ImportError:
    try:
        from src.price import get_quote, get_quotes, get_history  # type: ignore  # python -m src.agent_pm
        _HAS_PRICE = True
    except Exception:
        _HAS_PRICE = False
        get_quote = get_quotes = get_history = None  # type: ignore  # noqa: F811


def _meta_tag(r: sqlite3.Row) -> str:
    """Short provenance marker for the digest: old at fetch time, backup source, also carried elsewhere."""
    tags = []
    if r["is_stale"]:
        tags.append("old")
    if r["via_fallback"]:
        tags.append(f"backup source: {r['source']}")
    if r["also_seen_in"]:
        tags.append(f"also in {r['also_seen_in']}")
    return f" ({'; '.join(tags)})" if tags else ""


def fetch_recent(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    migrate(conn)  # make sure the v2 metadata columns exist
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """
        SELECT id, source, title, description, url, published_at, symbols, created_at,
               is_stale, via_fallback, also_seen_in, topics
        FROM raw_events
        WHERE source NOT IN ('gdelt', 'ecb_fx')  -- search-only source, see sources.SEARCH_ONLY
        ORDER BY datetime(published_at) DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    )
    return cur.fetchall()


# ---------- price helpers (Finnhub live) ----------
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
    norm = raw.replace("-", "").replace("/", "").replace("=", "")
    if norm in _PRICE_DISPLAY_MAP:
        return _PRICE_DISPLAY_MAP[norm]
    return raw

def _display_for_finnhub(fin_sym: str) -> str:
    return _PRICE_REVERSE_DISPLAY.get(fin_sym, fin_sym)

def _pm_price_snapshot(rows: list[sqlite3.Row]) -> str | None:
    """Build live price line for PM summary: top symbols from rows + defaults. Never raises, never logs key."""
    if not _HAS_PRICE or get_quotes is None:
        return None
    try:
        # top symbols from recent rows
        cnt = Counter()
        for r in rows:
            for s in (r["symbols"] or "").split(","):
                s = s.strip().upper()
                if s and s != "—":
                    cnt[s] += 1
        top = [k for k, _ in cnt.most_common(6)]
        seen: list[str] = []
        for s in top:
            fin = _to_finnhub_symbol(s)
            if fin and fin not in seen:
                seen.append(fin)
            if len(seen) >= 3:
                break
        # defaults (XAU + majors) — keep total <=5 for rate limit
        defaults = ["OANDA:XAU_USD", "AAPL", "TSLA", "BINANCE:BTCUSDT"]
        for d in defaults:
            if d not in seen and len(seen) < 5:
                seen.append(d)
        if not seen:
            seen = defaults[:4]
        # batch fetch
        quotes: dict = get_quotes(seen)  # type: ignore
        # XAU fallback like flash_notify
        if "OANDA:XAU_USD" in seen:
            q = quotes.get("OANDA:XAU_USD") or {}
            needs_fb = q.get("error") or not q.get("c")
            if needs_fb and get_quote is not None and get_history is not None:
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
        for sym in seen:
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
            return None
        return "Live: " + "  ".join(parts)
    except Exception as e:
        print(f"[agent_pm] price snapshot failed (non-blocking): {e}", file=sys.stderr)
        return None

def _price_snapshot_for_rows(rows: list[sqlite3.Row]) -> str | None:
    """Alias for _pm_price_snapshot — spec name."""
    return _pm_price_snapshot(rows)


def _build_price_section(rows: list[sqlite3.Row]) -> str | None:
    """Alias for _pm_price_snapshot — spec name."""
    return _pm_price_snapshot(rows)


DISCORD_LIMIT = 2000
MAX_POINTS = 6
TOP_HEADLINES = 8
_BULLET_RE = re.compile(r"^\s*(?:[-*\u2022\u2013]|\d+[.)])\s+(.*\S)")
_MD_RE = re.compile(r"\*\*|__|^#+\s*")


def clean_points(text: str | None) -> list[str]:
    """The AI's bullets, whatever style the model used, as plain '- ' lines (max MAX_POINTS)."""
    points = []
    for line in (text or "").splitlines():
        m = _BULLET_RE.match(line)
        if not m:
            continue
        point = _MD_RE.sub("", m.group(1)).strip()
        if point and not point.lower().startswith("live:"):
            points.append(point)
    return points[:MAX_POINTS]


def build_summary(rows: list[sqlite3.Row], points: list[str], live_line: str | None,
                  now: datetime | None = None) -> str:
    """Daily briefing. Same layout every day; only 'Key points' depends on the AI."""
    now = now or datetime.now(timezone.utc)
    lines = [f"**Daily briefing \u2014 {now.strftime('%a %d %b %Y')}**"]
    if live_line:
        lines.append(live_line)
    lines += ["", "**Key points**"]
    lines += [f"- {p}" for p in points] or ["_AI summary unavailable today \u2014 see the headlines below._"]
    lines += ["", "**Top headlines**"]
    for r in rows[:TOP_HEADLINES]:
        title = (r["title"] or "(no title)").strip().replace("[", "(").replace("]", ")")
        title = f"[{title}](<{r['url']}>)" if r["url"] else title
        tags = f" `{r['symbols'].replace(',', ', ')}`" if r["symbols"] else ""
        lines.append(f"- {title}{tags}{_meta_tag(r)}")
    if len(rows) > TOP_HEADLINES:
        lines.append(f"_+{len(rows) - TOP_HEADLINES} more in the last {len(rows)} articles_")
    return "\n".join(lines)


def split_message(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """Split on line breaks so no message is cut mid-line (Discord rejects > 2000 chars)."""
    lines = []
    for line in text.splitlines():
        lines += [line[i : i + limit] for i in range(0, len(line), limit)] or [""]  # only a >2000-char line is cut
    parts, cur = [], ""
    for line in lines:
        cand = f"{cur}\n{line}" if cur else line
        if len(cand) > limit:
            parts.append(cur)
            cur = line
        else:
            cur = cand
    parts.append(cur)
    return [p for p in parts if p.strip()]


def post_discord(webhook_url: str, text: str) -> bool:
    try:
        for part in split_message(text):
            r = requests.post(webhook_url, json={"content": part, "allowed_mentions": {"parse": []}}, timeout=10)
            if r.status_code not in (200, 204):
                print(f"[agent_pm] discord post http {r.status_code}: {r.text[:200]}")
                return False
        return True
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

        prompt_hint = "\n".join(r["title"] or "" for r in rows[:20])
        est = estimate_tokens(prompt_hint + " summarize USD/XAU macro")
        tier = FREE

        # FREE tier via FreeLLMAPI; deterministic stub is the fallback.
        llm_text = None
        model_name = f"stub-{tier}"
        used_tokens = est
        used_tier = tier
        try:
            import llm as _llm  # local FreeLLMAPI client
        except ImportError:
            try:
                from src import llm as _llm  # type: ignore
            except ImportError:
                _llm = None  # type: ignore
        live_line = None
        try:
            live_line = _pm_price_snapshot(rows)
        except Exception as e:
            print(f"[agent_pm] price snapshot skipped (non-blocking): {e}", file=sys.stderr)
        if _llm is not None and _llm.available():
            headlines_block = "\n".join(
                f"- {(r['title'] or '').strip()}" + (f" [affects: {r['symbols']}]" if r["symbols"] else "") + (f" [topics: {r['topics']}]" if r["topics"] else "") + _meta_tag(r)
                for r in rows[:20]
            )
            text, meta = _llm.summarize(headlines_block, live_line=live_line)
            if text:
                llm_text = text
                model_name = meta.get("model") or "free"
                used_tokens = meta.get("tokens") or est
                used_tier = FREE
                fin = meta.get("finish")
                warn = " [WARN truncated: raise FREE summarize max_tokens]" if fin == "length" else ""
                print(f"[agent_pm] free-ai summary ok model={model_name} tokens={used_tokens} finish={fin}{warn}")
            else:
                print(f"[agent_pm] free-ai summary unavailable ({meta.get('error')}) — using deterministic stub")
        else:
            print("[agent_pm] FREE_API_KEY not set — using deterministic stub")
        points = clean_points(llm_text)
        if llm_text and not points:
            print("[agent_pm] free-ai summary had no bullet points — posting headlines only")
            model_name = f"stub-{tier}"
        summary = build_summary(rows, points, live_line)
        print("--- summary preview ---")
        print(summary)
        print("--- end preview ---")

        if args.dry_run:
            print("[agent_pm] dry-run — not writing to DB")
            return

        # write to summaries (link to latest raw_event)
        latest_id = rows[0]["id"] if rows else None
        conn.execute(
            "INSERT INTO summaries (raw_event_id, summary_text, model_tier, model_name, tokens_used) VALUES (?, ?, ?, ?, ?)",
            (latest_id, summary, used_tier, model_name, used_tokens),
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
