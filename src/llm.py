"""
llm.py — FREE-tier LLM client (FreeLLMAPI, OpenAI-compatible, local router).

Points at the FreeLLMAPI gateway already running on the Linux gateway:
    base: http://127.0.0.1:3001/v1   (override with FREE_API_BASE)
    key : FREE_API_KEY (unified "freellmapi-..." key from the dashboard Keys page)
    model: FREE_MODEL  (default "auto" — let the router pick + fail over)

Design rules (v1):
  - FREE only. No paid key is read here. Replaces the dead paid hook.
  - Defensive: any failure (no key, HTTP error, timeout, bad JSON) returns None
    so callers fall back to their existing deterministic behavior. Never raises
    into the 30s flash loop.
  - Never logs the key.

Public API:
    available() -> bool
    chat(messages, model=None, max_tokens=..., temperature=...) -> (text|None, meta)
    market_impact_note(title, cleaned, symbols, live_price=None) -> (text|None, meta)
    summarize(headlines_block, live_line=None) -> (text|None, meta)

`meta` is a dict: {"model": <resolved model>, "tokens": <int>, "ok": bool, "error": <str|None>}
"""

import json
import os
import urllib.error
import urllib.request

DEFAULT_BASE = "http://127.0.0.1:3001/v1"
DEFAULT_MODEL = "auto"
DEFAULT_TIMEOUT = 20  # seconds — keep short so a stalled provider can't hang the 30s tick


def _base() -> str:
    return (os.getenv("FREE_API_BASE") or DEFAULT_BASE).rstrip("/")


def _key() -> str:
    return os.getenv("FREE_API_KEY") or ""


def _model() -> str:
    return os.getenv("FREE_MODEL") or DEFAULT_MODEL


def available() -> bool:
    """True if a free key is configured. Does not make a network call."""
    return bool(_key())


def chat(
    messages: list[dict],
    model: str | None = None,
    max_tokens: int = 220,
    temperature: float = 0.2,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[str | None, dict]:
    """
    One non-streaming chat completion through FreeLLMAPI.
    Returns (text|None, meta). Never raises.
    """
    meta = {"model": model or _model(), "tokens": 0, "ok": False, "error": None, "finish": None}
    key = _key()
    if not key:
        meta["error"] = "no_free_api_key"
        return None, meta

    url = f"{_base()}/chat/completions"
    body = json.dumps(
        {
            "model": meta["model"],
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        data = json.loads(raw)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        meta["error"] = f"http_{e.code}:{detail}"
        return None, meta
    except (urllib.error.URLError, TimeoutError) as e:
        meta["error"] = f"conn:{e}"
        return None, meta
    except (ValueError, json.JSONDecodeError) as e:
        meta["error"] = f"badjson:{e}"
        return None, meta

    try:
        choice = data["choices"][0]
        text = (choice["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError) as e:
        meta["error"] = f"shape:{e}"
        return None, meta

    usage = data.get("usage") or {}
    meta["tokens"] = int(usage.get("total_tokens") or 0)
    meta["finish"] = choice.get("finish_reason")
    # FreeLLMAPI echoes the resolved model it actually used
    meta["model"] = data.get("model") or meta["model"]
    if meta["finish"] == "length":
        # Truncated by max_tokens (common with reasoning models that spend the
        # budget on hidden reasoning). Flag it so callers can log/widen.
        meta["error"] = "truncated_length"
    meta["ok"] = bool(text)
    return (text or None), meta


# ---------- task helpers ----------

_IMPACT_SYSTEM = (
    "You are a terse financial-news desk analyst for a USD/XAU (gold) + macro trader. "
    "Given one news headline, output at most 2 sentences: the likely directional impact "
    "on USD and gold (XAU) and why. Be concrete and non-committal where the news is vague. "
    "Start directly with the impact — do NOT restate, quote, summarize or repeat the "
    "headline or any input, and do not prefix labels like 'Headline:', 'Impact:' or "
    "'Analysis:'. No preamble, no disclaimers, no markdown. If the item is not "
    "market-relevant, reply exactly: SKIP."
)

import re as _re

# Leading labels/echoes a model might still emit despite the instruction.
_ECHO_PREFIX_RE = _re.compile(
    r"^\s*(headline|detail|tagged symbols|symbols|live prices?|news|input|impact|analysis|summary|note)\s*:\s*",
    _re.IGNORECASE,
)


def _strip_echo(text: str, title: str) -> str:
    """Remove a leading restatement of the headline / labeled prefix from a note."""
    if not text:
        return text
    out = text.strip()
    t = (title or "").strip().rstrip(".!?").lower()
    # Iterate: peel a leading label OR a verbatim headline echo, until neither matches.
    for _ in range(6):
        before = out
        m = _ECHO_PREFIX_RE.match(out)
        if m:
            out = out[m.end():].lstrip()
        if t and len(t) > 12 and out.lower().startswith(t):
            after = out[len(t):].lstrip(" .:,-—–\n\t")
            if after:
                out = after
        if out == before:
            break
    out = out.strip()
    # If peeling left a lowercase opener, capitalize the first letter for readability.
    if out and out != text.strip() and out[0].islower():
        out = out[0].upper() + out[1:]
    return out


_ANSWER_RULE = (
    " Think silently. Your reply must contain ONLY the final answer wrapped in "
    "<answer></answer> tags — nothing before or after the tags."
)

_ANSWER_RE = _re.compile(r"<answer>(.*?)(?:</answer>|$)", _re.IGNORECASE | _re.DOTALL)
_THINK_RE = _re.compile(r"<think>.*?(?:</think>|$)", _re.IGNORECASE | _re.DOTALL)
_LEAK_RE = _re.compile(
    r"(\bthe user\b|\blet'?s (analy|think|write|check|refine|formulate|look)|\blet me\b|"
    r"\bwe need to\b|\bi need to\b|\bconstraints?:|\bsentence \d|^output:|^style:|^focus:)",
    _re.IGNORECASE | _re.MULTILINE,
)
# Retry on a different routing profile so a reasoning-dump model isn't picked twice.
_RETRY_MODELS = ("auto:reliable", "auto:smart")


def _extract_answer(text: str | None, meta: dict) -> str | None:
    """Return only the clean final answer, or None if the reply is leaked reasoning / cut off."""
    if not text:
        return None
    t = _THINK_RE.sub("", text).strip()
    m = _ANSWER_RE.search(t)
    if m:
        closed = "</answer>" in t.lower()
        ans = m.group(1).strip()
        if not closed and meta.get("finish") == "length":
            return None  # answer itself was cut off
    else:
        ans = t
    if not ans or _LEAK_RE.search(ans):
        return None
    if meta.get("finish") == "length" and not m:
        return None
    return ans


def _guarded_chat(messages: list[dict], **kw) -> tuple[str | None, dict]:
    """chat() + answer extraction, retrying on other routing profiles if the reply is unusable."""
    tried = []
    meta: dict = {}
    for model in (None, *_RETRY_MODELS):
        text, meta = chat(messages, model=model, **kw)
        tried.append(meta.get("model"))
        if meta.get("error") and not text:
            continue
        ans = _extract_answer(text, meta)
        if ans:
            meta["ok"] = True
            meta["error"] = None
            meta["tried"] = tried
            return ans, meta
        meta["error"] = "leaked_or_truncated"
    meta["ok"] = False
    meta["tried"] = tried
    return None, meta


def market_impact_note(
    title: str,
    cleaned: str,
    symbols: str = "",
    live_price: str | None = None,
) -> tuple[str | None, dict]:
    """
    Short per-event impact note for a flash Discord embed.
    Returns (note|None, meta). None on SKIP, leaked reasoning, or any failure.
    """
    user = f"Headline: {title}\n"
    if cleaned:
        user += f"Detail: {cleaned}\n"
    if symbols:
        user += f"Tagged symbols: {symbols}\n"
    if live_price:
        user += f"Live prices: {live_price}\n"
    text, meta = _guarded_chat(
        [
            {"role": "system", "content": _IMPACT_SYSTEM + _ANSWER_RULE},
            {"role": "user", "content": user.strip()},
        ],
        max_tokens=1200,
        temperature=0.2,
    )
    if text and text.strip().upper().startswith("SKIP"):
        meta["ok"] = False
        meta["error"] = "model_skip"
        return None, meta
    if text:
        text = _strip_echo(text, title)
        if not text or text.upper().startswith("SKIP"):
            meta["ok"] = False
            meta["error"] = "model_skip"
            return None, meta
    return text, meta


_SUMMARY_SYSTEM = (
    "You are the PM analyst for a USD/XAU (gold) + macro news desk. Given a list of recent "
    "headlines and optional live prices, write a tight briefing: 3-6 bullet points covering "
    "the dominant themes, USD and gold bias, and any scheduled risk events to watch. "
    "Plain text bullets starting with '- ', no markdown headers, no preamble."
)


def summarize(headlines_block: str, live_line: str | None = None) -> tuple[str | None, dict]:
    """Daily/periodic PM summary from a block of headlines. Returns (text|None, meta)."""
    user = ""
    if live_line:
        user += f"{live_line}\n\n"
    user += f"Recent headlines:\n{headlines_block}"
    return _guarded_chat(
        [
            {"role": "system", "content": _SUMMARY_SYSTEM + _ANSWER_RULE},
            {"role": "user", "content": user.strip()},
        ],
        max_tokens=3000,
        temperature=0.3,
        timeout=90,
    )
