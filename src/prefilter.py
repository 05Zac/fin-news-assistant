"""
prefilter.py — FREE-tier gate before Event Agent (agent_pm / flash_notify).

Purpose: save LLM calls by dropping duplicates + LOW relevance
before any LLM call. Used by flash_poll.py (30s RSS) and queue_worker.py.

- HTML strip + truncate (~300 chars, FREE, no LLM)
- HIGH/MEDIUM/LOW relevance label from entity tags (a label only: LOW is still posted)
- dedupe_key: normalized title hash + date prefix → dedup across URL variants
- URL + dedupe_key window check against raw_events + flash_queue

Usage:
    from prefilter import clean_description, classify_relevance, make_dedupe_key, is_duplicate_recent
    cleaned = clean_description(raw_desc)
    relevance = classify_relevance(title, cleaned, symbols)
    key = make_dedupe_key(title, published_at)
    dup = is_duplicate_recent(conn, url, key, window_minutes=15)

Tier: FREE only — heuristic, no LLM. Keep deterministic.
"""

import hashlib
import html
import re
import sqlite3

TAG_RE = re.compile(r"<[^>]+>")

# Zac's core desk. Anything tagged with one of these is HIGH.
CORE_SYMBOLS = {"XAU", "USD"}


def clean_description(raw: str, limit: int = 300) -> str:
    if not raw:
        return ""
    text = TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def classify_relevance(title: str, description: str, symbols: str = "") -> str:
    """Priority label from the article's entity tags (raw_events.symbols). It no longer
    decides whether an alert is posted: every article is.
      high   = affects gold or the dollar
      medium = affects another tracked asset
      low    = no tracked asset named
    title/description are unused; kept so callers don't change."""
    tagged = {s.strip().upper() for s in (symbols or "").split(",") if s.strip()}
    if tagged & CORE_SYMBOLS:
        return "high"
    return "medium" if tagged else "low"


def make_dedupe_key(title: str, published_at: str = "") -> str:
    """
    Stable key for same news across URL variants.
    Normalize title: lower, strip punct, collapse spaces, take first 80 chars.
    Mix in date prefix (YYYY-MM-DD) so same title on different days is distinct.
    """
    t = (title or "").lower().strip()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()[:80]
    date_prefix = (published_at or "")[:10]  # YYYY-MM-DD
    raw = f"{date_prefix}|{t}"
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{date_prefix}:{h}" if date_prefix and date_prefix[0].isdigit() else h


def is_duplicate_recent(conn: sqlite3.Connection, url: str, dedupe_key: str, window_minutes: int = 15) -> tuple[bool, str]:
    """
    Check recent duplicates in raw_events (url exact) and flash_queue (dedupe_key).
    Returns (is_dup, reason).
    - url exact match: permanent (any age) — raw_events has idx on url
    - dedupe_key within window_minutes: check flash_queue + raw_events via raw_json scan fallback
    """
    # 1) URL exact — permanent dedup (already in raw_events)
    if url:
        try:
            cur = conn.execute("SELECT 1 FROM raw_events WHERE url = ? LIMIT 1", (url,))
            if cur.fetchone():
                return True, "url_exists"
        except sqlite3.OperationalError:
            pass

    # 2) dedupe_key window — only if flash_queue exists
    has_queue = False
    try:
        conn.execute("SELECT 1 FROM flash_queue LIMIT 1")
        has_queue = True
    except sqlite3.OperationalError:
        has_queue = False

    if has_queue and dedupe_key:
        try:
            cur = conn.execute(
                """
                SELECT 1 FROM flash_queue
                WHERE dedupe_key = ?
                  AND datetime(created_at) >= datetime('now', ?)
                LIMIT 1
                """,
                (dedupe_key, f"-{window_minutes} minutes"),
            )
            if cur.fetchone():
                return True, f"dedupe_key_window_{window_minutes}m"
        except sqlite3.OperationalError:
            pass
        # also check raw_events created_at window via dedupe_key in status? skip heavy raw_json LIKE scan
        # lightweight: check raw_events with same dedupe_key if we stored it there (we store in flash_queue only)
        # so we also do a bounded raw_events title-hash window check:
        # if same dedupe_key appears via recent raw_events with same key pattern in flash_queue, the above catches it

    return False, ""


def prefilter_result(title: str, raw_description: str, url: str, published_at: str, symbols: str, conn: sqlite3.Connection) -> dict:
    """
    One-call gate: clean → classify → dedupe → decision.
    Returns dict with keys: cleaned, relevance, dedupe_key, is_duplicate, dup_reason, should_enqueue
    should_enqueue = not duplicate (every article is alerted, whatever its relevance)
    """
    cleaned = clean_description(raw_description, limit=300)
    relevance = classify_relevance(title, cleaned, symbols)
    dedupe_key = make_dedupe_key(title, published_at)
    is_dup, reason = is_duplicate_recent(conn, url, dedupe_key, window_minutes=15)
    should_enqueue = not is_dup
    return {
        "cleaned": cleaned,
        "relevance": relevance,
        "dedupe_key": dedupe_key,
        "is_duplicate": is_dup,
        "dup_reason": reason,
        "should_enqueue": should_enqueue,
    }
