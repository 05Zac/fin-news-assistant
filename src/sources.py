"""
sources.py — extra news sources beyond InvestingLive (v2 #3).

  fed     Federal Reserve press releases (official RSS). Alerted like InvestingLive.
  ecb     ECB press releases (official RSS). Alerted.
  sec     SEC 8-K filings for the stocks in the entities table, via api-anything
          (`npx -y api-anything call sec-gov listFilings`). Alerted (stale ones are not).
  ecb_fx  ECB EUR/USD reference rate (official RSS, daily). Stored for search, not alerted.
  gdelt  GDELT DOC 2.0 article search, finance themes, English, last hour. No key.
         Broad and noisy (press releases, syndicated local papers), so GDELT rows are
         stored and tagged for search but NOT alerted, digested or verified.

Per-source cool-off: when a source fails, it is skipped until status key
`cooloff_until:<source>` passes, so one bad source never holds up the others.

Normalised article dict (same shape as flash_xau.fetch_investinglive):
    {uuid, title, description, url, published_at (ISO UTC), entities: [], _source}
"""

import html
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

UA = {"User-Agent": "fin-news-assistant/1.0"}
RSS_FEEDS = {
    "fed": "https://www.federalreserve.gov/feeds/press_all.xml",
    "ecb": "https://www.ecb.europa.eu/rss/press.html",
    "ecb_fx": "https://www.ecb.europa.eu/rss/fxref-usd.html",
}
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
GDELT_QUERY = (
    "(theme:ECON_STOCKMARKET OR theme:ECON_EARNINGSREPORT OR theme:ECON_INTEREST_RATES "
    "OR theme:ECON_INFLATION OR theme:ECON_OILPRICE OR theme:ECON_CURRENCY_EXCHANGE_RATE) sourcelang:english"
)
# GDELT asks for >= 5s between requests and still 429s most first tries (seen 2026-11-27).
GDELT_TRIES = 4
GDELT_GAP_S = 15

# Sources that are stored for search only: no Discord alert, not in digest/verifier.
SEARCH_ONLY = {"gdelt", "ecb_fx"}
COOLOFF_MIN = {"fed": 30, "ecb": 30, "ecb_fx": 120, "gdelt": 50, "sec": 120}  # gdelt < 1h so the next hourly run still tries


class RateLimited(Exception):
    pass


_DC_DATE = "{http://purl.org/dc/elements/1.1/}date"


def fetch_rss(source: str, limit: int = 20) -> list[dict]:
    """Fetch an official RSS feed (RSS 2.0 or the RDF/RSS 1.0 the ECB uses)."""
    resp = requests.get(RSS_FEEDS[source], timeout=20, headers=UA)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    items = root.findall(".//item") or root.findall(".//{http://purl.org/rss/1.0/}item")
    out = []
    for item in items[:limit]:
        def get(tag):
            return (item.findtext(tag) or item.findtext("{http://purl.org/rss/1.0/}" + tag) or "").strip()
        link = re.sub(r"(?<!:)//+", "/", get("link"))  # ECB links carry '.eu//press'
        pub = get("pubDate") or (item.findtext(_DC_DATE) or "").strip()
        try:
            pub = parsedate_to_datetime(pub).astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError):
            try:
                pub = datetime.fromisoformat(pub).astimezone(timezone.utc).isoformat()
            except ValueError:
                pass
        out.append({
            "uuid": link,
            "title": html.unescape(get("title")),
            "description": html.unescape(get("description"))[:800],
            "url": link,
            "published_at": pub,
            "entities": [],
            "_source": source,
        })
    return out


# 8-K item numbers -> plain words, so the alert says what was filed
SEC_ITEMS = {
    "1.01": "material agreement", "1.02": "agreement terminated", "1.05": "cybersecurity incident",
    "2.01": "acquisition or disposal", "2.02": "results (earnings)", "2.03": "new debt",
    "2.05": "restructuring costs", "2.06": "impairment", "3.01": "delisting notice",
    "4.01": "auditor change", "4.02": "restatement", "5.01": "change of control",
    "5.02": "director or officer change", "5.03": "bylaws change", "5.07": "shareholder vote",
    "7.01": "Reg FD disclosure", "8.01": "other events",
}  # 9.01 (exhibits) is left out: it rides along with everything
SEC_DAYS = 3


def _sec_title(name: str, ticker: str, form: str, desc: str) -> str:
    items = [SEC_ITEMS[i] for i in re.findall(r"\b(\d\.\d\d)\b", desc or "") if i in SEC_ITEMS]
    return f"{name} ({ticker}) {form}: {', '.join(items) or 'current report'}"


def fetch_sec(limit: int = 0, conn: sqlite3.Connection | None = None, run=None) -> list[dict]:
    """Recent 8-Ks for every active stock in the entities table (one api-anything call each)."""
    import json
    import subprocess
    run = run or (lambda args: subprocess.run(args, capture_output=True, text=True, timeout=90).stdout)
    stocks = conn.execute("SELECT code, name FROM entities WHERE kind = 'stock' AND active = 1 ORDER BY code").fetchall()
    since = (datetime.now(timezone.utc) - timedelta(days=SEC_DAYS)).date().isoformat()
    out, failed = [], 0
    for ticker, name in stocks:
        try:
            line = next((l for l in run(["npx", "-y", "api-anything", "call", "sec-gov", "listFilings",
                                         f"ticker={ticker}", "formType=8-K"]).splitlines() if l.startswith("{")), "")
            res = json.loads(line)
        except (ValueError, StopIteration, OSError, subprocess.TimeoutExpired):
            failed += 1
            continue
        if not res.get("ok"):
            failed += 1
            continue
        for f in res.get("data") or []:
            if (f.get("filed") or "") < since or not f.get("filing"):
                continue
            out.append({
                "uuid": f["filing"],
                "title": _sec_title(name, ticker, f.get("type") or "8-K", f.get("description") or ""),
                "description": f.get("description") or "",
                "url": f["filing"],
                "published_at": f"{f['filed']}T00:00:00+00:00",  # SEC gives the date only
                "entities": [],
                "_source": "sec",
            })
    if stocks and failed == len(stocks):
        raise RuntimeError(f"sec: all {failed} api-anything calls failed")
    if failed:
        print(f"[sources] sec: {failed}/{len(stocks)} ticker lookups failed")
    return out


_SPACE_BEFORE = re.compile(r"\s+([,.;:?!)%])")
_SPACE_AFTER = re.compile(r"([($])\s+")


def _gdelt_title(t: str) -> str:
    """GDELT tokenises titles ('Abbott ( ABT ) , Inc . 21 . 6'); put the punctuation back."""
    t = _SPACE_AFTER.sub(r"\1", _SPACE_BEFORE.sub(r"\1", t or ""))
    t = re.sub(r"(\d)\s*\.\s*(\d)", r"\1.\2", t)
    t = re.sub(r"\s+-\s+(?=\w)", "-", t) if " - " in t and t.count(" - ") > 2 else t
    return re.sub(r"\s+", " ", t).strip()


def _gdelt_time(s: str) -> str:
    try:
        return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return s or ""


def fetch_gdelt(limit: int = 250, sleep=time.sleep) -> list[dict]:
    params = {"query": GDELT_QUERY, "mode": "artlist", "format": "json",
              "maxrecords": str(min(limit, 250)), "timespan": "1h", "sort": "datedesc"}
    for attempt in range(GDELT_TRIES):
        if attempt:
            sleep(GDELT_GAP_S)
        resp = requests.get(GDELT_URL, params=params, timeout=60, headers=UA)
        if resp.status_code == 429:
            continue
        resp.raise_for_status()
        arts = resp.json().get("articles") or []
        return [{
            "uuid": a.get("url"),
            "title": _gdelt_title(a.get("title")),
            "description": "",
            "url": a.get("url"),
            "published_at": _gdelt_time(a.get("seendate")),
            "entities": [],
            "_source": "gdelt",
            "domain": a.get("domain"),
            "sourcecountry": a.get("sourcecountry"),
        } for a in arts if a.get("url") and a.get("title")]
    raise RateLimited(f"gdelt 429 on all {GDELT_TRIES} tries")


FETCHERS = {
    "fed": lambda limit, conn: fetch_rss("fed", limit),
    "ecb": lambda limit, conn: fetch_rss("ecb", limit),
    "ecb_fx": lambda limit, conn: fetch_rss("ecb_fx", limit),
    "gdelt": lambda limit, conn: fetch_gdelt(limit),
    "sec": lambda limit, conn: fetch_sec(limit, conn),
}


def _status(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM status WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _set_status(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO status(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
        "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
        (key, value),
    )
    conn.commit()


def fetch_with_cooloff(conn: sqlite3.Connection, source: str, limit: int, now: datetime | None = None) -> list[dict] | None:
    """Fetch one source unless it is cooling off. None = skipped or failed (reason printed)."""
    now = now or datetime.now(timezone.utc)
    until = _status(conn, f"cooloff_until:{source}")
    if until and until > now.isoformat():
        print(f"[sources] {source} cooling off until {until}, skipped")
        return None
    try:
        arts = FETCHERS[source](limit, conn)
    except Exception as e:  # any failure: back off this source only
        until = (now + timedelta(minutes=COOLOFF_MIN[source])).isoformat()
        _set_status(conn, f"cooloff_until:{source}", until)
        print(f"[sources] {source} failed ({type(e).__name__}: {str(e)[:120]}); cooling off until {until}")
        return None
    if until:
        conn.execute("DELETE FROM status WHERE key = ?", (f"cooloff_until:{source}",))
        conn.commit()
    return arts
