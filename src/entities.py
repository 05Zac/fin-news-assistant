"""
entities.py — what each article affects (v2 #5).

Tables
  entities          one row per thing news can affect: currency, commodity, crypto, index,
                    stock, sector, central_bank, theme. `aliases` are the words to look for;
                    `affects` lists entities that are implied when this one is named
                    (FED -> USD, XAU). Adding a currency or stock is a new row, not new code.
  article_entities  one row per (article, entity): via 'mention' (named in the text) or
                    'implied:<CODE>' (affected by something that was named), plus the matched text.

raw_events.symbols  tradable entities the article affects (title mentions first), e.g. "XAU,USD"
raw_events.topics   non-tradable ones by name, e.g. "Federal Reserve, Semiconductors"

Alias syntax (pipe-separated in entities.aliases)
  plain text     case-insensitive, whole-word:          gold | bullion
  ALL CAPS <= 5  ticker-like, matched case-sensitively:  USD | NVDA
  =Text          case-sensitive exactly as written:      =Meta | =AI
Longer aliases win, so "Australian dollar" tags AUD and is not also counted as USD's "dollar".

CLI
  python3 src/entities.py --list
  python3 src/entities.py --test "Fed's Powell says rates on hold; gold slips"
  python3 src/entities.py --add SGD --kind currency --name "Singapore dollar" --aliases "SGD|Singapore dollar"
  python3 src/entities.py --retag            # re-tag every article (after adding entities)
  python3 src/entities.py --llm-untagged 60  # free AI picks tickers where keywords found none

Sectors and themes fan out to tickers: "chip prices rise" -> NVDA, AMD, INTC, TSM, MU, AVGO.
SEED is the source of truth for the codes it lists (edits to those rows are reset on start);
entities added with --add are left alone.
"""

import argparse
import html
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

TRADABLE_KINDS = ("currency", "commodity", "crypto", "index", "stock")
BODY_CHARS = 600
MAX_SYMBOLS = 6

# (code, kind, name, aliases, affects)
SEED = [
    # currencies
    ("USD", "currency", "US dollar", "USD|dollar|greenback|DXY|dollar index", ""),
    ("EUR", "currency", "Euro", "EUR|euro", ""),
    ("JPY", "currency", "Japanese yen", "JPY|yen", ""),
    ("GBP", "currency", "Pound sterling", "GBP|pound|sterling", ""),
    ("CHF", "currency", "Swiss franc", "CHF|Swiss franc|franc", ""),
    ("AUD", "currency", "Australian dollar", "AUD|Australian dollar|aussie", ""),
    ("NZD", "currency", "New Zealand dollar", "NZD|New Zealand dollar|kiwi", ""),
    ("CAD", "currency", "Canadian dollar", "CAD|Canadian dollar|loonie", ""),
    ("CNY", "currency", "Chinese yuan", "CNY|CNH|yuan|renminbi", ""),
    # commodities
    ("XAU", "commodity", "Gold", "XAU|gold|bullion", ""),
    ("XAG", "commodity", "Silver", "XAG|silver", ""),
    ("WTI", "commodity", "Crude oil", "WTI|Brent|crude|crude oil|oil|oil prices", ""),
    ("NATGAS", "commodity", "Natural gas", "natural gas|natgas|LNG", ""),
    ("COPPER", "commodity", "Copper", "copper", ""),
    # crypto
    ("BTC", "crypto", "Bitcoin", "BTC|bitcoin", ""),
    ("ETH", "crypto", "Ethereum", "ETH|ethereum", ""),
    # indices
    ("SPX", "index", "S&P 500", "SPX|S&P 500|S&P500|S&P|ES futures", ""),
    ("NDX", "index", "Nasdaq 100", "NDX|Nasdaq|Nasdaq 100|NQ futures", ""),
    ("DJI", "index", "Dow Jones", "DJI|DJIA|=Dow|Dow Jones", ""),
    ("NIKKEI", "index", "Nikkei 225", "Nikkei|Nikkei 225", ""),
    ("DAX", "index", "DAX", "DAX", ""),
    ("KOSPI", "index", "KOSPI", "KOSPI", ""),
    # stocks. A stock implies its sector (topic); a sector implies its main stocks (below).
    ("NVDA", "stock", "Nvidia", "NVDA|Nvidia", "SEMIS"),
    ("AMD", "stock", "AMD", "AMD|Advanced Micro Devices", "SEMIS"),
    ("MU", "stock", "Micron", "MU|Micron", "SEMIS"),
    ("INTC", "stock", "Intel", "INTC|=Intel", "SEMIS"),
    ("TSM", "stock", "TSMC", "TSM|TSMC|Taiwan Semiconductor", "SEMIS"),
    ("AVGO", "stock", "Broadcom", "AVGO|Broadcom", "SEMIS"),
    ("QCOM", "stock", "Qualcomm", "QCOM|Qualcomm", "SEMIS"),
    ("ASML", "stock", "ASML", "ASML", "SEMIS"),
    ("ARM", "stock", "Arm Holdings", "Arm Holdings|=Arm", "SEMIS"),
    ("SMCI", "stock", "Super Micro", "SMCI|Super Micro|Supermicro", "SEMIS"),
    ("AAPL", "stock", "Apple", "AAPL|=Apple", "TECH"),
    ("MSFT", "stock", "Microsoft", "MSFT|Microsoft", "TECH"),
    ("GOOGL", "stock", "Alphabet", "GOOGL|GOOG|Alphabet|Google", "TECH"),
    ("AMZN", "stock", "Amazon", "AMZN|Amazon", "TECH"),
    ("META", "stock", "Meta", "=META|=Meta|Facebook", "TECH"),
    ("ORCL", "stock", "Oracle", "ORCL|=Oracle", "TECH"),
    ("NFLX", "stock", "Netflix", "NFLX|Netflix", "TECH"),
    ("PLTR", "stock", "Palantir", "PLTR|Palantir", "TECH"),
    ("SNAP", "stock", "Snap", "SNAP|=Snap|Snapchat", "TECH"),
    ("BABA", "stock", "Alibaba", "BABA|Alibaba", "TECH"),
    ("TSLA", "stock", "Tesla", "TSLA|Tesla", "AUTOS"),
    ("GM", "stock", "General Motors", "GM|General Motors", "AUTOS"),
    ("F", "stock", "Ford", "=Ford", "AUTOS"),
    ("TM", "stock", "Toyota", "Toyota", "AUTOS"),
    ("XOM", "stock", "Exxon Mobil", "XOM|Exxon|ExxonMobil|Exxon Mobil", "ENERGY"),
    ("CVX", "stock", "Chevron", "CVX|Chevron", "ENERGY"),
    ("BA", "stock", "Boeing", "Boeing", ""),
    ("COIN", "stock", "Coinbase", "COIN|Coinbase", "CRYPTO"),
    # Banks are matched by ticker only: "Goldman says..." is a bank commenting, not news about it.
    ("JPM", "stock", "JPMorgan", "JPM", "BANKS"),
    ("BAC", "stock", "Bank of America", "BAC", "BANKS"),
    ("GS", "stock", "Goldman Sachs", "GS", "BANKS"),
    ("WFC", "stock", "Wells Fargo", "WFC|Wells Fargo", "BANKS"),
    ("SOL", "crypto", "Solana", "SOL|Solana", ""),
    ("XRP", "crypto", "XRP", "XRP|Ripple", ""),
    # sectors and market themes -> the tickers they move (skipped when the headline names a stock itself)
    ("SEMIS", "sector", "Semiconductors", "semiconductor|semiconductors|chip|chips|chipmaker|chipmakers|memory chips|microchip|microchips|processor|processors|GPU|GPUs|DRAM|NAND", "NVDA,AMD,INTC,TSM,MU,AVGO"),
    ("AI", "theme", "AI", "=AI|artificial intelligence|hyperscaler|hyperscalers|data center|data centers|OpenAI|Anthropic", "NVDA,MSFT,GOOGL"),
    ("TECH", "sector", "Technology", "tech|tech stocks|technology stocks|big tech|Magnificent 7|Magnificent Seven|Mag 7", "NDX,AAPL,MSFT,GOOGL,AMZN,META"),
    ("BANKS", "sector", "Banks", "banks|banking|bank stocks|lenders", "JPM,BAC,GS,WFC"),
    ("ENERGY", "sector", "Energy", "energy stocks|energy sector|oil majors|OPEC|OPEC+", "WTI,XOM,CVX"),
    ("AUTOS", "sector", "Autos", "automaker|automakers|carmaker|carmakers|EV|EVs|electric vehicle|electric vehicles", "TSLA,GM,F,TM"),
    ("EQUITIES", "theme", "Equities", "stocks|equities|stock market|Wall Street|indices|risk-off|risk-on|risk appetite", "SPX,NDX"),
    ("CRYPTO", "theme", "Crypto", "crypto|cryptocurrency|cryptocurrencies|stablecoin|stablecoins|altcoin|altcoins|Binance|Tether|USDT", "BTC,ETH"),
    # central banks -> their currency (Fed also gold)
    ("FED", "central_bank", "Federal Reserve", "=Fed|FOMC|Federal Reserve|Powell|fed funds", "USD,XAU"),
    ("ECB", "central_bank", "European Central Bank", "ECB|European Central Bank|Lagarde", "EUR"),
    ("BOJ", "central_bank", "Bank of Japan", "BOJ|BoJ|Bank of Japan|Ueda", "JPY"),
    ("BOE", "central_bank", "Bank of England", "BOE|BoE|Bank of England|Bailey", "GBP"),
    ("SNB", "central_bank", "Swiss National Bank", "SNB|Swiss National Bank", "CHF"),
    ("RBA", "central_bank", "Reserve Bank of Australia", "RBA|Reserve Bank of Australia", "AUD"),
    ("RBNZ", "central_bank", "Reserve Bank of New Zealand", "RBNZ|Reserve Bank of New Zealand", "NZD"),
    ("BOC", "central_bank", "Bank of Canada", "BOC|BoC|Bank of Canada", "CAD"),
    ("PBOC", "central_bank", "People's Bank of China", "PBOC|PBoC|People's Bank of China|Peoples Bank of China", "CNY"),
    # countries -> their currency (implied, so they rank after anything named directly)
    ("USA", "country", "United States", "US|U.S.|United States|=American|Washington", "USD"),
    ("CANADA", "country", "Canada", "Canada|Canadian", "CAD"),
    ("JAPAN", "country", "Japan", "Japan|Japanese|Tokyo", "JPY"),
    ("UK", "country", "United Kingdom", "UK|U.K.|Britain|British", "GBP"),
    ("EUROZONE", "country", "Eurozone", "eurozone|euro zone|euro area|Germany|German|France|French|Italy|Italian", "EUR"),
    ("AUSTRALIA", "country", "Australia", "Australia|Australian", "AUD"),
    ("NEW_ZEALAND", "country", "New Zealand", "New Zealand", "NZD"),
    ("SWITZERLAND", "country", "Switzerland", "Switzerland|Swiss", "CHF"),
    ("CHINA", "country", "China", "China|Chinese|Beijing", "CNY"),
    # themes. Only US-specific data implies USD and gold; plain "CPI"/"inflation" can be any country.
    ("US_JOBS", "theme", "US jobs data", "NFP|nonfarm payrolls|non-farm payrolls|jobless claims|ADP|JOLTS|JOLTs", "USD,XAU"),
    ("US_INFLATION", "theme", "US inflation data", "US CPI|U.S. CPI|US PCE|core PCE|US inflation|U.S. inflation|US PPI", "USD,XAU"),
    ("INFLATION", "theme", "Inflation", "CPI|inflation|PPI|consumer prices", ""),
    ("RATES", "theme", "Rates and yields", "bond yields|yields|yield|rate cut|rate cuts|rate hike|rate hikes|interest rates|central bank|central banks", ""),
    ("UST", "theme", "US Treasuries", "Treasury|Treasuries|Treasury yields|T-bills|T-bill", "USD"),
    ("US_DATA", "theme", "US economic data", "ISM|ISM manufacturing|ISM services|Philly Fed|Empire State|UMich|University of Michigan|Beige Book", "USD"),
    ("US_POLITICS", "theme", "US politics", "Trump|White House|Congress|midterms|midterm|Republican|Republicans|Democrats|government shutdown", "USD,SPX"),
    ("TRADE", "theme", "Trade policy", "tariff|tariffs|trade war|trade deal|trade talks", "USD,SPX"),
    ("GEOPOLITICS", "theme", "Geopolitics", "Iran|Houthi|Houthis|Israel|Gaza|Ukraine|Russia|sanctions|ceasefire|geopolitical", "XAU,WTI"),
    ("MIDEAST_OIL", "theme", "Middle East supply risk", "Strait of Hormuz|Hormuz|Red Sea", "WTI,XAU"),
    # No aliases: set only by llm_tag_untagged() when the AI finds nothing a headline moves.
    ("NO_IMPACT", "theme", "No market impact", "", ""),
]

_TAG_RE = re.compile(r"<[^>]+>")
_matcher_cache: dict[int, tuple] = {}


def migrate(conn: sqlite3.Connection) -> None:
    """Create the entity tables, seed missing entities, add raw_events.topics/tagged_at. Idempotent."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS entities (
            code     TEXT PRIMARY KEY,
            kind     TEXT NOT NULL,
            name     TEXT NOT NULL,
            aliases  TEXT NOT NULL DEFAULT '',
            affects  TEXT NOT NULL DEFAULT '',
            active   INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS article_entities (
            article_id   INTEGER NOT NULL REFERENCES raw_events(id) ON DELETE CASCADE,
            entity_code  TEXT NOT NULL REFERENCES entities(code),
            via          TEXT NOT NULL,
            matched      TEXT,
            in_title     INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (article_id, entity_code)
        );
        CREATE INDEX IF NOT EXISTS idx_article_entities_entity ON article_entities(entity_code, article_id);
        """
    )
    conn.executemany(
        "INSERT INTO entities (code, kind, name, aliases, affects) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(code) DO UPDATE SET kind=excluded.kind, name=excluded.name, aliases=excluded.aliases, "
        "affects=excluded.affects WHERE kind != excluded.kind OR name != excluded.name "
        "OR aliases != excluded.aliases OR affects != excluded.affects",
        SEED,
    )
    have = {r[1] for r in conn.execute("PRAGMA table_info(raw_events)")}
    if have:
        if "topics" not in have:
            conn.execute("ALTER TABLE raw_events ADD COLUMN topics TEXT")
        if "tagged_at" not in have:
            conn.execute("ALTER TABLE raw_events ADD COLUMN tagged_at TEXT")
    conn.commit()


def _compile(alias: str) -> re.Pattern:
    exact = alias.startswith("=")
    text = alias[1:] if exact else alias
    case_sensitive = exact or (text.isupper() and len(text) <= 5)
    return re.compile(
        r"(?<![A-Za-z0-9])" + re.escape(text) + r"(?![A-Za-z0-9])", 0 if case_sensitive else re.IGNORECASE
    )


def _load(conn: sqlite3.Connection) -> tuple:
    """(aliases longest-first as (pattern, code), {code: (kind, name, [affects])}). Cached per connection."""
    sig = conn.execute("SELECT count(*), coalesce(sum(length(aliases) + length(affects) + active), 0) FROM entities").fetchone()
    cached = _matcher_cache.get(id(conn))
    if cached and cached[0] == sig:
        return cached[1], cached[2]
    info, aliases = {}, []
    for code, kind, name, al, affects in conn.execute(
        "SELECT code, kind, name, aliases, affects FROM entities WHERE active = 1"
    ):
        info[code] = (kind, name, [a.strip() for a in affects.split(",") if a.strip()])
        for a in al.split("|"):
            a = a.strip()
            if a:
                aliases.append((len(a.lstrip("=")), a, code))
    aliases.sort(key=lambda x: -x[0])
    patterns = [(_compile(a), code) for _, a, code in aliases]
    _matcher_cache[id(conn)] = (sig, patterns, info)
    return patterns, info


_PAIR_RE = re.compile(r"(?<![A-Za-z])([A-Z]{3})\s?/?\s?([A-Z]{3})(?![A-Za-z])")


def _find(text: str, patterns: list, found: dict, in_title: bool, info: dict) -> None:
    """Match aliases longest-first; blank out each hit so a shorter alias can't match inside it."""
    # Pairs such as EURUSD, USD/CNY or XAUUSD tag both sides.
    for m in _PAIR_RE.finditer(text):
        a, b = m.group(1), m.group(2)
        if a in info and b in info and info[a][0] in TRADABLE_KINDS and info[b][0] in TRADABLE_KINDS:
            for code, pos in ((a, m.start(1)), (b, m.start(2))):
                found.setdefault(code, (m.group(0), in_title, pos))
            text = text[: m.start()] + " " * len(m.group(0)) + text[m.end():]
    for pat, code in patterns:
        m = pat.search(text)
        if not m:
            continue
        if code not in found:
            found[code] = (m.group(0), in_title, m.start())
        text = pat.sub(lambda mm: " " * len(mm.group(0)), text)


def tag_text(conn: sqlite3.Connection, title: str, body: str = "") -> list[dict]:
    """Entities an article affects, in order: named in title, named in body, then implied."""
    patterns, info = _load(conn)
    found: dict[str, tuple] = {}
    _find(title or "", patterns, found, True, info)
    clean_body = html.unescape(_TAG_RE.sub(" ", body or ""))[:BODY_CHARS]
    _find(clean_body, patterns, found, False, info)

    tags = [
        {"code": c, "kind": info[c][0], "name": info[c][1], "via": "mention", "matched": m, "in_title": t}
        for c, (m, t, _pos) in sorted(found.items(), key=lambda kv: (not kv[1][1], kv[1][2]))
    ]
    seen = set(found)
    by_code = {t["code"]: t for t in tags}
    # "Chip prices rise" fans out to the chip stocks; "Nvidia slumps as chips sell off" is about Nvidia.
    stock_in_title = any(t["kind"] == "stock" and t["in_title"] for t in tags)
    for t in list(tags):
        for target in info[t["code"]][2]:
            if target in by_code and t["in_title"]:
                by_code[target]["in_title"] = True  # body mention confirmed by the headline
            if stock_in_title and target in info and info[target][0] == "stock":
                continue
            if target in info and target not in seen:
                seen.add(target)
                tags.append(
                    {"code": target, "kind": info[target][0], "name": info[target][1],
                     "via": f"implied:{t['code']}", "matched": t["matched"], "in_title": t["in_title"]}
                )
    return tags


def summarize_tags(tags: list[dict]) -> tuple[str | None, str | None]:
    """(symbols, topics) strings for raw_events.

    The headline decides: anything named in or implied by the title. Body text mentions many
    markets in passing, so body mentions are used only when the title gives nothing (max 3).
    Every tag, title or body, is still kept in article_entities for searching.
    """
    def pick(match_kind):
        title = [t for t in tags if match_kind(t) and t["in_title"]]
        if title:
            return sorted(title, key=lambda t: t["via"] != "mention")  # named first, then implied
        return [t for t in tags if match_kind(t) and t["via"] == "mention"][:3]

    symbols = [t["code"] for t in pick(lambda t: t["kind"] in TRADABLE_KINDS)][:MAX_SYMBOLS]
    topics = [t["name"] for t in pick(lambda t: t["kind"] not in TRADABLE_KINDS)]
    return (",".join(symbols) or None, ", ".join(topics) or None)


def tag_article(conn: sqlite3.Connection, article_id: int, title: str, body: str, commit: bool = True) -> list[dict]:
    """Tag one stored article: rewrite its article_entities rows and its symbols/topics."""
    tags = tag_text(conn, title, body)
    symbols, topics = summarize_tags(tags)
    if symbols:
        conn.execute("DELETE FROM article_entities WHERE article_id = ?", (article_id,))
    else:
        # No ticker from keywords: keep and use what the AI chose earlier, if anything.
        conn.execute("DELETE FROM article_entities WHERE article_id = ? AND via != 'llm'", (article_id,))
        _, info = _load(conn)
        ai = [r[0] for r in conn.execute(
            "SELECT entity_code FROM article_entities WHERE article_id = ? AND via = 'llm' ORDER BY rowid", (article_id,))]
        symbols = ",".join(c for c in ai if c in info and info[c][0] in TRADABLE_KINDS) or None
        if "NO_IMPACT" in ai:
            topics = ", ".join(filter(None, [topics, info["NO_IMPACT"][1]]))
    conn.executemany(
        "INSERT OR IGNORE INTO article_entities (article_id, entity_code, via, matched, in_title) VALUES (?, ?, ?, ?, ?)",
        [(article_id, t["code"], t["via"], t["matched"], 1 if t["in_title"] else 0) for t in tags],
    )
    conn.execute(
        "UPDATE raw_events SET symbols = ?, topics = ?, tagged_at = ? WHERE id = ?",
        (symbols, topics, datetime.now(timezone.utc).isoformat(), article_id),
    )
    if commit:
        conn.commit()
    return tags


def tag_pending(conn: sqlite3.Connection, retag_all: bool = False, batch: int = 500) -> int:
    """Tag articles that have never been tagged (or every article with retag_all)."""
    where = "" if retag_all else "WHERE tagged_at IS NULL"
    ids = [r[0] for r in conn.execute(f"SELECT id FROM raw_events {where} ORDER BY id")]
    done = 0
    for i in range(0, len(ids), batch):
        chunk = ids[i : i + batch]
        rows = conn.execute(
            f"SELECT id, title, description FROM raw_events WHERE id IN ({','.join('?' * len(chunk))})", chunk
        ).fetchall()
        for rid, title, desc in rows:
            tag_article(conn, rid, title or "", desc or "", commit=False)
            done += 1
        conn.commit()  # short write transactions so the 30s loop is never blocked for long
    return done


_LLM_SYSTEM = (
    "You tag financial news for a trading desk. For each numbered headline, pick the tickers from the "
    "allowed list whose price it would most plausibly move (0 to 4, most affected first). Use only codes "
    "from the list. Adverts, broker promotions, awards and general commentary move nothing: answer NONE. "
    "Reply with one line per headline and nothing else, in the form  <number>: CODE,CODE  or  <number>: NONE"
)
_LLM_LINE_RE = re.compile(r"^\s*(\d+)\s*[:.)-]\s*(.+)$", re.MULTILINE)


def llm_tag_untagged(conn: sqlite3.Connection, limit: int = 60, batch: int = 10, chat=None) -> int:
    """Ask the free AI for tickers on articles the keyword rules left without one.

    The AI may only choose from the entities table; anything else it writes is dropped.
    An article it finds nothing for is marked NO_IMPACT so it is not asked about again.
    Returns the number of articles the AI answered for.
    """
    if chat is None:
        sys.path.insert(0, str(Path(__file__).parent))
        import llm
        if not llm.available():
            print("[entities] FREE_API_KEY is not set: AI tagging skipped", file=sys.stderr)
            return 0
        chat = lambda messages: llm._guarded_chat(messages, max_tokens=4000, temperature=0.1, timeout=90)[0]  # noqa: E731
    _, info = _load(conn)
    allowed = {c: v[1] for c, v in info.items() if v[0] in TRADABLE_KINDS}
    codes = ", ".join(f"{c} ({n})" for c, n in allowed.items())
    rows = conn.execute(
        "SELECT id, title, description FROM raw_events r WHERE symbols IS NULL AND title IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM article_entities a WHERE a.article_id = r.id AND a.via = 'llm') "
        "ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    done = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i : i + batch]
        lines = [
            f"{n}: {title.strip()} — {html.unescape(_TAG_RE.sub(' ', desc or ''))[:160].strip()}"
            for n, (_id, title, desc) in enumerate(chunk, 1)
        ]
        reply = chat([
            {"role": "system", "content": _LLM_SYSTEM + llm_answer_rule()},
            {"role": "user", "content": f"Allowed codes: {codes}\n\nHeadlines:\n" + "\n".join(lines)},
        ])
        for num, answer in _LLM_LINE_RE.findall(reply or ""):
            n = int(num)
            if not 1 <= n <= len(chunk):
                continue
            picked = [c for c in re.findall(r"[A-Z_0-9]+", answer.upper()) if c in allowed][:4]
            if not picked and "NONE" not in answer.upper():
                continue  # unusable line: leave the article for the next run
            rid, title, desc = chunk[n - 1]
            conn.executemany(
                "INSERT OR IGNORE INTO article_entities (article_id, entity_code, via, matched, in_title) VALUES (?, ?, 'llm', NULL, 0)",
                [(rid, c) for c in (picked or ["NO_IMPACT"])],
            )
            tag_article(conn, rid, title or "", desc or "", commit=False)
            done += 1
        conn.commit()
    return done


def llm_answer_rule() -> str:
    return " Think silently. Put the lines inside <answer></answer> tags, with nothing before or after the tags."


def main():
    sys.path.insert(0, str(Path(__file__).parent))
    from config import db_path

    p = argparse.ArgumentParser(description="Entities: what each article affects")
    p.add_argument("--list", action="store_true", help="list entities with article counts")
    p.add_argument("--test", metavar="TEXT", help="show how a headline would be tagged")
    p.add_argument("--retag", action="store_true", help="re-tag every article")
    p.add_argument("--llm-untagged", type=int, metavar="N", help="ask the free AI for tickers on up to N articles the keywords left without one")
    p.add_argument("--add", metavar="CODE", help="add or update an entity")
    p.add_argument("--kind", choices=TRADABLE_KINDS + ("sector", "central_bank", "country", "theme"))
    p.add_argument("--name")
    p.add_argument("--aliases", help="pipe-separated, e.g. 'SGD|Singapore dollar'")
    p.add_argument("--affects", default="", help="comma-separated entity codes this one implies")
    args = p.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    conn = sqlite3.connect(db_path(), timeout=30)
    migrate(conn)

    if args.add:
        if not (args.kind and args.name and args.aliases):
            p.error("--add needs --kind, --name and --aliases")
        conn.execute(
            "INSERT INTO entities (code, kind, name, aliases, affects) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(code) DO UPDATE SET kind=excluded.kind, name=excluded.name, aliases=excluded.aliases, affects=excluded.affects, active=1",
            (args.add, args.kind, args.name, args.aliases, args.affects),
        )
        conn.commit()
        print(f"[entities] saved {args.add}. Run --retag to apply it to stored articles.")
    if args.test:
        for t in tag_text(conn, args.test):
            print(f"  {t['code']:12s} {t['kind']:12s} {t['via']:18s} matched '{t['matched']}'")
        print("  symbols=%s | topics=%s" % summarize_tags(tag_text(conn, args.test)))
    if args.retag:
        print(f"[entities] re-tagged {tag_pending(conn, retag_all=True)} article(s)")
    if args.llm_untagged:
        print(f"[entities] AI tagged {llm_tag_untagged(conn, limit=args.llm_untagged)} article(s)")
    if args.list:
        q = ("SELECT e.code, e.kind, e.name, count(a.article_id) FROM entities e "
             "LEFT JOIN article_entities a ON a.entity_code = e.code WHERE e.active = 1 "
             "GROUP BY e.code ORDER BY e.kind, count(a.article_id) DESC")
        for code, kind, name, n in conn.execute(q):
            print(f"  {kind:12s} {code:12s} {name:28s} {n} article(s)")
    conn.close()


if __name__ == "__main__":
    main()
