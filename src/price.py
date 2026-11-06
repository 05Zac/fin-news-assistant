"""
price.py — Finnhub live price + Yahoo history fallback for fin-news-assistant

Finnhub free plan (verified 2026-10-13):
  ✅ /quote?symbol=AAPL, TSLA, BINANCE:BTCUSDT — live c/o/h/l/pc, no premium
  ❌ /stock/candle & /forex/candle — "You don't have access to this resource" (premium)
So: live quotes = Finnhub, history/candles = Yahoo Finance v8 (no key, add UA header).

Usage:
  from price import get_quote, get_history, get_quotes
  get_quote("AAPL")                          # → {symbol, c, dp, ...}
  get_history("AAPL", days=7)                # → list of {date, open, high, low, close, volume}
  get_history("GC=F", days=7)                # gold futures proxy for XAU
  get_quotes(["AAPL","TSLA","BINANCE:BTCUSDT","GC=F"])

Env: FINNHUB_API_KEY in .env.
Never log the key; read via config.finnhub_api_key(). The key is sent in the
X-Finnhub-Token header (not the URL) so it can't appear in error messages.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

import requests

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*a, **kw):  # type: ignore
        return False

try:
    from config import finnhub_api_key  # noqa: E402  # when run as python src/price.py
    _HAS_CONFIG = True
except ImportError:
    from src.config import finnhub_api_key  # noqa: E402  # when run as python -m src.price
    _HAS_CONFIG = True

FINNHUB_QUOTE_URL = "https://finnhub.io/api/v1/quote"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

# Yahoo needs a UA or it returns empty body
_YAHOO_HEADERS = {"User-Agent": "fin-news-assistant/1.0 (+https://github.com/05Zac/fin-news-assistant)"}

# Map common aliases → Yahoo symbols for history
YAHOO_ALIASES: dict[str, str] = {
    "XAUUSD": "GC=F",
    "XAU_USD": "GC=F",
    "XAU": "GC=F",
    "GOLD": "GC=F",
    "BTC": "BTC-USD",
    "BTCUSDT": "BTC-USD",
}


def _fh_headers(token: str) -> dict[str, str]:
    return {"X-Finnhub-Token": token}


def _scrub(msg: str, token: str | None) -> str:
    """Belt-and-braces: never let the key text survive in an error string."""
    return msg.replace(token, "***") if token else msg


def _finnhub_token() -> str | None:
    load_dotenv()
    return finnhub_api_key()


def get_quote(symbol: str, token: str | None = None) -> dict[str, Any]:
    """
    Live quote via Finnhub /quote. Free plan, no premium needed.

    Returns dict with keys c (current), d, dp, h, l, o, pc, t (epoch).
    On error returns {"error": "..."}.
    """
    tok = token or _finnhub_token()
    if not tok:
        return {"error": "FINNHUB_API_KEY not set — add it to .env (FINNHUB_API_KEY=...)."}
    # Finnhub quotes are case-sensitive for some prefixes (BINANCE:...)
    params = {"symbol": symbol}
    try:
        r = requests.get(FINNHUB_QUOTE_URL, params=params, headers=_fh_headers(tok), timeout=12)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and data.get("error"):
            return {"error": data["error"], "symbol": symbol}
        # annotate
        data["symbol"] = symbol
        # human-readable time
        if data.get("t"):
            try:
                data["datetime_utc"] = datetime.fromtimestamp(int(data["t"]), tz=timezone.utc).isoformat()
            except Exception:
                pass
        return data
    except Exception as e:
        return {"error": _scrub(str(e), tok), "symbol": symbol}


def get_quotes(symbols: list[str], token: str | None = None, delay_s: float = 0.35) -> dict[str, dict]:
    """
    Batch quotes with small delay to respect Finnhub 60 req/min free limit.
    Returns {symbol: quote_dict}.
    """
    out: dict[str, dict] = {}
    for s in symbols:
        out[s] = get_quote(s, token=token)
        if delay_s and s != symbols[-1]:
            time.sleep(delay_s)
    return out


def get_history(
    symbol: str,
    days: int = 7,
    interval: str = "1d",
    token: str | None = None,
) -> list[dict[str, Any]]:
    """
    OHLCV history.

    1) Try Finnhub /stock/candle & /forex/candle (works only on premium).
    2) Fallback to Yahoo v8 chart (no key). Handles aliases: XAU→GC=F, BTC→BTC-USD.

    Returns list of {date, timestamp, open, high, low, close, volume} sorted asc.
    """
    yahoo_symbol = YAHOO_ALIASES.get(symbol.upper(), symbol)
    # Try Finnhub first if caller explicitly wants it (will fail on free plan but we try)
    tok = token or _finnhub_token()
    if tok:
        tried = _finnhub_candles(symbol, days, interval, tok)
        if tried and not tried[0].get("error"):
            return tried
        # if finnhub returned error, silently fall through to Yahoo

    # Yahoo fallback
    return _yahoo_history(yahoo_symbol, days, interval)


def _finnhub_candles(symbol: str, days: int, interval: str, token: str) -> list[dict]:
    """Try Finnhub candles; returns [] or [{"error":...}] on premium block so caller can fallback."""
    # Finnhub resolution map: 1,5,15,30,60,D,W,M — interval 1d → D
    res_map = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "1h": "60", "1d": "D", "1w": "W", "1M": "M"}
    res = res_map.get(interval, "D")
    now = int(time.time())
    frm = now - days * 86400
    # Try stock/candle then forex/candle
    for endpoint in (
        "https://finnhub.io/api/v1/stock/candle",
        "https://finnhub.io/api/v1/forex/candle",
    ):
        try:
            r = requests.get(
                endpoint,
                params={"symbol": symbol, "resolution": res, "from": frm, "to": now},
                headers=_fh_headers(token),
                timeout=12,
            )
            r.raise_for_status()
            j = r.json()
            if isinstance(j, dict) and j.get("error"):
                continue
            if j.get("s") == "no_data":
                continue
            if "c" not in j or not j["c"]:
                continue
            out: list[dict] = []
            for i, ts in enumerate(j.get("t") or []):
                out.append({
                    "date": datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat(),
                    "timestamp": int(ts),
                    "open": (j.get("o") or [])[i] if i < len(j.get("o") or []) else None,
                    "high": (j.get("h") or [])[i] if i < len(j.get("h") or []) else None,
                    "low": (j.get("l") or [])[i] if i < len(j.get("l") or []) else None,
                    "close": (j.get("c") or [])[i] if i < len(j.get("c") or []) else None,
                    "volume": (j.get("v") or [])[i] if j.get("v") and i < len(j["v"]) else None,
                })
            if out:
                return out
        except Exception:
            continue
    return [{"error": "You don't have access to this resource."}]


def _yahoo_history(symbol: str, days: int, interval: str) -> list[dict[str, Any]]:
    now = int(time.time())
    frm = now - days * 86400
    # Yahoo interval map
    y_interval = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "60m", "1d": "1d", "1w": "1wk", "1M": "1mo"}.get(interval, "1d")
    url = YAHOO_CHART_URL.format(symbol=symbol)
    try:
        r = requests.get(
            url,
            params={"period1": frm, "period2": now, "interval": y_interval},
            headers=_YAHOO_HEADERS,
            timeout=15,
        )
        r.raise_for_status()
        j = r.json()
        result = (j.get("chart") or {}).get("result") or []
        if not result:
            err = (j.get("chart") or {}).get("error") or j
            return [{"error": str(err)[:500], "symbol": symbol}]
        c = result[0]
        ts_list: list[int] = c.get("timestamp") or []
        quote = (c.get("indicators") or {}).get("quote") or [{}]
        q = quote[0] if quote else {}
        opens = q.get("open") or []
        highs = q.get("high") or []
        lows = q.get("low") or []
        closes = q.get("close") or []
        volumes = q.get("volume") or []
        out: list[dict] = []
        for i, ts in enumerate(ts_list):
            out.append({
                "date": datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat(),
                "timestamp": int(ts),
                "open": opens[i] if i < len(opens) else None,
                "high": highs[i] if i < len(highs) else None,
                "low": lows[i] if i < len(lows) else None,
                "close": closes[i] if i < len(closes) else None,
                "volume": volumes[i] if i < len(volumes) else None,
                "symbol": symbol,
            })
        return out
    except Exception as e:
        return [{"error": str(e), "symbol": symbol}]


# CLI for quick manual test
if __name__ == "__main__":
    import argparse
    import json

    p = argparse.ArgumentParser(description="Finnhub quote + Yahoo history (free plan)")
    p.add_argument("--quote", nargs="*", default=[], help="symbols for live quote (Finnhub)")
    p.add_argument("--history", nargs="*", default=[], help="symbols for OHLC history (Yahoo fallback)")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--interval", default="1d")
    args = p.parse_args()

    if args.quote:
        print(json.dumps(get_quotes(args.quote), indent=2))
    if args.history:
        for s in args.history:
            print(f"\n=== {s} (days={args.days} interval={args.interval}) ===")
            hist = get_history(s, days=args.days, interval=args.interval)
            print(json.dumps(hist[-7:], indent=2))
    if not args.quote and not args.history:
        # demo: live quotes + gold history
        print(json.dumps(get_quotes(["AAPL", "TSLA", "BINANCE:BTCUSDT"]), indent=2))
        print(json.dumps(get_history("GC=F", days=7)[:3], indent=2))
