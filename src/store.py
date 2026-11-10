"""
store.py — the one place articles are written to raw_events (v2 phase 1).

Every ingest path (ingest.py, flash_xau.py, flash_poll.py) calls insert_article(), so
dedup and per-row metadata are applied the same way everywhere.

Dedup, in order:
  1. URL        — same URL already stored (calendar rows: same URL on the same event date).
  2. content    — SHA-256 of normalised title + first 500 chars of body. The same story
                  carried by two sources (or re-published under a new URL) merges into the
                  first row: dup_count is bumped and the other source is noted in also_seen_in.

Metadata on every row:
  source, published_at  (already there)
  fetched_at            when this pipeline fetched it
  via_fallback          1 when it came from a backup source (set by the caller)
  is_stale              1 when it was already older than STALE_HOURS at fetch time
  content_hash, dup_count, also_seen_in

Usage:
    from store import migrate, insert_article
    migrate(conn)
    res = insert_article(conn, "investinglive", article, default_symbols="USD")
    # res = {"status": "inserted"|"url_exists"|"content_dup", "id": int|None, "is_stale": bool}
"""

import hashlib
import html
import json
import os
import re
import sqlite3
from datetime import datetime, timezone

import entities

STALE_HOURS = float(os.getenv("STALE_HOURS", "24"))
BODY_CHARS = 500

# Calendar rows reuse one URL and one description for every occurrence of an event,
# so their identity must include the event date.
CALENDAR_SOURCES = {"forexfactory"}

_TAG_RE = re.compile(r"<[^>]+>")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

_NEW_COLUMNS = [
    ("fetched_at", "TEXT"),
    ("via_fallback", "INTEGER NOT NULL DEFAULT 0"),
    ("is_stale", "INTEGER NOT NULL DEFAULT 0"),
    ("content_hash", "TEXT"),
    ("dup_count", "INTEGER NOT NULL DEFAULT 0"),
    ("also_seen_in", "TEXT"),
]


def _norm(text: str, limit: int | None = None) -> str:
    t = html.unescape(_TAG_RE.sub(" ", text or "")).lower()
    t = _NON_ALNUM_RE.sub(" ", t).strip()
    return t[:limit] if limit else t


def content_hash(title: str, body: str, extra: str = "") -> str:
    """Stable identity for a story: normalised title + first BODY_CHARS of normalised body."""
    raw = f"{_norm(title)}|{_norm(body, BODY_CHARS)}|{extra}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_ts(s: str) -> datetime | None:
    """Parse the ISO timestamps the sources use (Z, +00:00, -05:00, fractional seconds)."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _is_stale(published_at: str, fetched: datetime) -> bool:
    pub = parse_ts(published_at)
    if pub is None:
        return False
    return (fetched - pub).total_seconds() > STALE_HOURS * 3600


def _hash_extra(source: str, published_at: str) -> str:
    return (published_at or "")[:10] if source in CALENDAR_SOURCES else ""


def _body_of(article: dict) -> str:
    return article.get("description") or article.get("snippet") or article.get("highlight") or ""


def migrate(conn: sqlite3.Connection) -> None:
    """Add the phase-1 columns to an existing DB and backfill them. Safe to call every run."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(raw_events)")}
    if not have:
        return  # table not created yet; schema.sql creates it with the columns
    added = False
    for name, decl in _NEW_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE raw_events ADD COLUMN {name} {decl}")
            added = True
    conn.execute("CREATE INDEX IF NOT EXISTS idx_raw_events_content_hash ON raw_events(content_hash)")
    if added:
        conn.commit()

    rows = conn.execute(
        "SELECT id, source, title, description, published_at, created_at FROM raw_events WHERE content_hash IS NULL"
    ).fetchall()
    for rid, source, title, desc, published_at, created_at in rows:
        fetched = parse_ts(created_at) or datetime.now(timezone.utc)
        conn.execute(
            "UPDATE raw_events SET content_hash=?, fetched_at=COALESCE(fetched_at, ?), is_stale=? WHERE id=?",
            (
                content_hash(title or "", desc or "", _hash_extra(source, published_at or "")),
                created_at,
                1 if _is_stale(published_at or "", fetched) else 0,
                rid,
            ),
        )
    if rows:
        conn.commit()

    entities.migrate(conn)
    entities.tag_pending(conn)  # tags anything stored before tagging existed


def insert_article(
    conn: sqlite3.Connection,
    source: str,
    article: dict,
    default_symbols: str | None = None,  # ignored since v2 #5: tags come from entities.py
    via_fallback: bool = False,
) -> dict:
    """Store one article unless it is a duplicate. Commits. Returns {status, id, is_stale}."""
    title = article.get("title")
    description = _body_of(article)
    url = article.get("url")
    published_at = article.get("published_at") or ""
    is_calendar = source in CALENDAR_SOURCES

    # 1) URL dedup
    if url:
        if is_calendar:
            row = conn.execute(
                "SELECT id FROM raw_events WHERE url = ? AND substr(published_at, 1, 10) = ? LIMIT 1",
                (url, published_at[:10]),
            ).fetchone()
        else:
            row = conn.execute("SELECT id FROM raw_events WHERE url = ? LIMIT 1", (url,)).fetchone()
        if row:
            return {"status": "url_exists", "id": row[0], "is_stale": False}

    # 2) content dedup — merge into the first row that carried this story
    chash = content_hash(title or "", description, _hash_extra(source, published_at))
    row = conn.execute(
        "SELECT id, source, also_seen_in FROM raw_events WHERE content_hash = ? ORDER BY id LIMIT 1", (chash,)
    ).fetchone()
    if row:
        seen = [s for s in (row[2] or "").split(",") if s]
        if source != row[1] and source not in seen:
            seen.append(source)
        conn.execute(
            "UPDATE raw_events SET dup_count = dup_count + 1, also_seen_in = ? WHERE id = ?",
            (",".join(seen) or None, row[0]),
        )
        conn.commit()
        return {"status": "content_dup", "id": row[0], "is_stale": False}

    # 3) insert
    now = datetime.now(timezone.utc)
    stale = _is_stale(published_at, now)
    cur = conn.execute(
        """
        INSERT INTO raw_events
            (source, external_id, title, description, url, published_at, symbols, raw_json,
             fetched_at, via_fallback, is_stale, content_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source,
            str(article.get("uuid") or article.get("id") or ""),
            title,
            description,
            url,
            published_at,
            None,  # symbols: set by entities.tag_article below
            json.dumps(article, ensure_ascii=False),
            now.isoformat(),
            1 if via_fallback else 0,
            1 if stale else 0,
            chash,
        ),
    )
    # What does it affect? Sets symbols (tradable) and topics from the entities table.
    # They stay empty when nothing in the text is recognised: no placeholder tags.
    entities.tag_article(conn, cur.lastrowid, title or "", description, commit=False)
    conn.commit()
    return {"status": "inserted", "id": cur.lastrowid, "is_stale": stale}
