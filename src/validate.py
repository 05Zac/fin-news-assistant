"""
validate.py — deterministic checks + optional free-tier LLM verifier.

Deterministic stage (always): required fields, ISO dates, URL, symbol tagging.
LLM stage: when FREE_API_KEY is set, calls VERIFIER_MODEL (default "auto",
fallback "auto:smart" on low_confidence/conflict) via the local FreeLLMAPI router
to score {title,time,entity,source} credibility and return structured JSON.

Usage:
    python src/validate.py --limit 20
    python src/validate.py --limit 20 --with-llm        # force LLM verifier
    python src/validate.py --limit 20 --no-llm           # deterministic only
    VERIFIER_MODEL=auto:smart python src/validate.py --with-llm

Env: FREE_API_KEY, FREE_API_BASE, VERIFIER_MODEL, VERIFIER_FALLBACK, DB_PATH (see src/config.py)
"""

import argparse
import json
import os
import random
import sqlite3
import sys
import time
from pathlib import Path

from datetime import datetime, timezone
import zoneinfo

import requests
try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*a, **kw):
        return False

sys.path.insert(0, str(Path(__file__).parent))
from config import (  # noqa: E402
    VERIFIER_FALLBACK,
    discord_webhook,
    VERIFIER_MODEL,
    db_path,
    free_api_base,
    free_api_key,
)

LLM_BASE = free_api_base()  # local FreeLLMAPI router (OpenAI-compatible)
KL_TZ = zoneinfo.ZoneInfo("Asia/Kuala_Lumpur")

VERIFIER_SYSTEM = """You are a strict financial news verifier. Given an Evidence Bundle item \
(today_utc, title, published_at, url, symbols, description/snippet), score it WITHOUT hallucinating.\nJudge dates against today_utc, not your training data: news dated today or recently is NOT "future".

Return JSON only with keys:
- verdict: "ok" | "flag" | "reject"
- confidence: 0.0-1.0
- issues: string[]  (empty if ok; e.g. "stale: published 3y ago", "entity mismatch: XAU in title but symbols empty")
- credibility: {title: 0-10, time: 0-10, entity: 0-10, source: 0-10, overall: 0-10}
- reason: one sentence

Rules: never infer missing facts. If evidence is insufficient, flag with low confidence. \
Respond JSON only, no markdown."""

MARKET_REACTION_SYSTEM = """You are a concise financial market-reaction summarizer. Given today's VERIFIED headlines (and an analyst stub summary if present), describe how the market reacted / is likely reacting TODAY — not a forecast for tomorrow.

Focus lens: XAU/USD and USD drivers (CPI, FOMC/Fed speak, DXY, yields, risk sentiment). Never invent prices, levels, or % moves. If evidence is thin, say so explicitly and lower confidence.

Return JSON only with keys:
- market_reaction: 2-4 sentences grounded in the provided headlines (what drove attention / implied direction for gold or USD)
- summary: one-sentence overall take for today
- confidence: 0.0-1.0 (calibrated to evidence coverage; thin evidence -> <=0.55)
- drivers: string[] (e.g. ["CPI repricing","Fed speak","risk-off"])

Rules: cite only provided titles. No tomorrow prediction."""

# ---------------------------------------------------------------------------
# Resilience helpers
# ---------------------------------------------------------------------------
TRANSIENT_SUBSTRINGS = [
    "429",
    "503",
    "502",
    "504",
    "failed to resolve",
    "no route to host",
    "timed out",
    "timeout",
    "connection aborted",
    "connection reset",
    "temporarily unavailable",
    "name or service not known",
    "getaddrinfo failed",
    "name resolution",
    "dns",
]

def _is_transient_error(exc: BaseException) -> bool:
    """True for network / rate-limit / temporary LLM failures that must not be fatal."""
    if isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError) and getattr(exc, "response", None) is not None:
        try:
            if exc.response.status_code in (429, 503, 502, 504):
                return True
        except Exception:
            pass
    msg = str(exc).lower()
    for kw in TRANSIENT_SUBSTRINGS:
        if kw in msg:
            return True
    return False

def _sleep_with_jitter(base: float, attempt: int) -> None:
    delay = base * (2 ** attempt) + random.uniform(0, 0.5)
    time.sleep(delay)


def _parse_json(content) -> dict:
    """Parse a JSON verdict, tolerating models that wrap it in reasoning or <think> text."""
    import re
    if not isinstance(content, str):
        return content
    text = re.sub(r"<think>.*?(</think>|$)", "", content, flags=re.S | re.I).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    dec = json.JSONDecoder()
    found = None
    for m in re.finditer(r"\{", text):  # keep the LAST valid object (the final answer)
        try:
            obj, _ = dec.raw_decode(text, m.start())
            if isinstance(obj, dict):
                found = obj
        except ValueError:
            continue
    if found is None:
        raise ValueError("no JSON object in verifier reply")
    return found


def _parse_iso(s: str) -> datetime | None:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None

def _kl_date(iso_str: str) -> str | None:
    dt = _parse_iso(iso_str)
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(KL_TZ).date().isoformat()

def _today_kl_str() -> str:
    return datetime.now(KL_TZ).date().isoformat()

def fetch_analyst_summaries_today(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    today = _today_kl_str()
    try:
        cur = conn.execute("SELECT id, summary_text, model_name, created_at FROM summaries ORDER BY id DESC LIMIT 50")
        rows = cur.fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for r in rows:
        d = _kl_date(r["created_at"])
        if d == today:
            out.append(r)
    return out

def call_market_reaction_llm(rows: list[sqlite3.Row], analyst_rows: list[sqlite3.Row], api_key: str, model: str, timeout: int = 60, retries: int = 3, base_delay: float = 2.0) -> dict | None:
    """One LLM call to summarize today's market reaction (not tomorrow forecast). Returns JSON or None. Retries on 429/503/timeout."""
    import requests as req
    # build compact evidence: verified titles + analyst preview
    headlines = []
    for r in rows[:20]:
        headlines.append(f"- id={r['id']} [{r['published_at']}] {r['title'] or ''} ({r['symbols'] or 'n/a'}) — {r['url'] or ''}")
    analyst_preview = ""
    if analyst_rows:
        analyst_preview = (analyst_rows[0]["summary_text"] or "")[:800]
    user_payload = json.dumps({
        "headlines": headlines,
        "analyst_preview": analyst_preview,
        "count": len(rows),
    }, ensure_ascii=False)
    url = f"{LLM_BASE.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": MARKET_REACTION_SYSTEM},
            {"role": "user", "content": user_payload},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "max_tokens": 3000,
    }
    last_exc: BaseException | None = None
    for attempt in range(retries + 1):
        try:
            r = req.post(url, headers=headers, json=body, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            content = data["choices"][0]["message"]["content"]
            return _parse_json(content)
        except Exception as e:
            last_exc = e
            if _is_transient_error(e) and attempt < retries:
                _sleep_with_jitter(base_delay, attempt)
                continue
            raise
    if last_exc:
        raise last_exc
    return None

def post_discord_first_webhook(text: str) -> tuple[bool, str]:
    wh = discord_webhook()
    if not wh:
        return False, "DISCORD_WEBHOOK_URL not set"
    try:
        payload = {"content": text[:1900]}
        r = requests.post(wh, json=payload, timeout=10)
        if r.status_code in (200, 204):
            return True, f"ok {r.status_code}"
        return False, f"http {r.status_code} {r.text[:200]}"
    except Exception as e:
        return False, str(e)

def build_discord_report(today: str, analyst_rows: list[sqlite3.Row], checked: int, ok: int, bad: int, llm_calls: int, flags: int, use_llm: bool, model: str, details: list[str], reaction: dict | None = None) -> str:
    lines = []
    lines.append(f"**Fin Verifier — {today} Asia/KL**  ({'LLM ' + model if use_llm else 'deterministic only'})")
    if analyst_rows:
        a = analyst_rows[0]
        preview = (a["summary_text"] or "")[:500].replace("\n", " ")
        lines.append(f"Analyst input today: {len(analyst_rows)} summary(ies) — latest id={a['id']} {a['model_name']} — _{preview[:300]}…_")
    else:
        lines.append(f"Analyst input today: none (no summaries for {today} KL) — verifying raw_events for today instead")
    lines.append(f"Checked today KL: **{checked}** rows — {ok} ok, {bad} flagged — LLM calls {llm_calls} flags {flags}")
    if reaction and isinstance(reaction, dict):
        mr = (reaction.get("market_reaction") or "").strip()
        summ = (reaction.get("summary") or "").strip()
        conf = reaction.get("confidence")
        drivers = reaction.get("drivers") or []
        # clamp confidence display
        try:
            conf_f = float(conf) if conf is not None else None
            conf_s = f"{conf_f:.1f}/10" if conf_f is not None and conf_f <= 1 else f"{conf_f:.1f}" if conf_f else "n/a"
            # normalize 0-1 to 0-10 if needed
            if conf_f is not None and conf_f <= 1:
                conf_s = f"{conf_f*10:.1f}/10"
        except Exception:
            conf_s = str(conf)
        lines.append("")
        lines.append(f"**Market reaction today** (confidence {conf_s})")
        if mr:
            lines.append(mr[:700])
        if summ:
            lines.append(f"_Summary: {summ[:300]}_")
        if drivers:
            lines.append(f"Drivers: {', '.join(str(d) for d in drivers[:5])}")
    if details:
        lines.append("")
        for d in details[:8]:
            lines.append(d[:280])
        if checked > len(details):
            lines.append(f"_…+{checked - len(details)} more_")
    else:
        lines.append("_No issues — all items grounded for today._")
    lines.append(f"\nSource: raw_events where published_at KL date = {today} (same-day analyst window)")
    return "\n".join(lines)

def validate_row(row: sqlite3.Row) -> list[str]:
    errors = []
    if not (row["title"] or "").strip():
        errors.append("missing title")
    if not (row["published_at"] or "").strip():
        errors.append("missing published_at")
    else:
        pa = row["published_at"]
        if len(pa) < 10 or " " not in pa and "T" not in pa and "-" not in pa:
            errors.append(f"published_at not ISO-ish: {pa[:40]}")
    if row["url"] and not row["url"].startswith(("http://", "https://")):
        errors.append(f"bad url: {row['url'][:60]}")
    return errors


def build_verifier_user_payload(row: sqlite3.Row) -> str:
    raw_json = row["raw_json"] if "raw_json" in row.keys() else None
    snippet = ""
    if raw_json:
        try:
            j = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
            snippet = (j.get("description") or j.get("snippet") or j.get("highlight") or "")[:600]
        except Exception:
            pass
    if not snippet:
        snippet = (row["description"] if "description" in row.keys() else "") or ""
        snippet = snippet[:600]
    payload = {
        # Free models don't know the current date; without this they flag real news as "future".
        "today_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "title": row["title"],
        "published_at": row["published_at"],
        "url": row["url"],
        "symbols": row["symbols"] if "symbols" in row.keys() else None,
        "snippet": snippet,
    }
    return json.dumps(payload, ensure_ascii=False)


def call_verifier_llm(row: sqlite3.Row, api_key: str, model: str, timeout: int = 60, retries: int = 3, base_delay: float = 2.0) -> dict | None:
    """Call the free-tier verifier with exponential backoff on 429/503/timeout. Returns parsed JSON verdict or raises."""
    import requests as req
    url = f"{LLM_BASE.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": VERIFIER_SYSTEM},
            {"role": "user", "content": build_verifier_user_payload(row)},
        ],
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
        "max_tokens": 3000,
    }
    last_exc: BaseException | None = None
    for attempt in range(retries + 1):
        try:
            r = req.post(url, headers=headers, json=body, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            content = data["choices"][0]["message"]["content"]
            return _parse_json(content)
        except Exception as e:
            last_exc = e
            if _is_transient_error(e) and attempt < retries:
                _sleep_with_jitter(base_delay, attempt)
                continue
            raise
    if last_exc:
        raise last_exc
    return None


def llm_verdict_to_errors(verdict: dict | None) -> list[str]:
    if verdict is None:
        return ["verifier: no response (network/error)"]
    v = (verdict.get("verdict") or "").lower()
    conf = verdict.get("confidence")
    issues = verdict.get("issues") or []
    overall = (verdict.get("credibility") or {}).get("overall")
    errs = []
    if v == "reject":
        errs.append(f"verifier reject (conf {conf}, overall {overall}): {'; '.join(issues)[:200]}")
    elif v == "flag":
        errs.append(f"verifier flag (conf {conf}): {'; '.join(issues)[:200]}")
    # ok -> no error; but surface low confidence even when ok
    try:
        if conf is not None and float(conf) < 0.55 and v == "ok":
            errs.append(f"verifier low confidence {conf} despite ok: {verdict.get('reason','')[:120]}")
    except Exception:
        pass
    return errs


def main():
    try:
        load_dotenv(dotenv_path=str(Path(__file__).parent.parent / ".env"))
    except AssertionError:
        load_dotenv(dotenv_path=".env")
    p = argparse.ArgumentParser(description="Validate raw_events — deterministic + optional free-tier verifier")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--with-llm", action="store_true", help="force LLM verifier (needs FREE_API_KEY)")
    p.add_argument("--no-llm", action="store_true", help="skip LLM verifier")
    p.add_argument("--verifier-model", default=None, help="override VERIFIER_MODEL for this run")
    p.add_argument("--today", action="store_true", help="only verify items where published_at KL date == today (Asia/Kuala_Lumpur) — used for fin-analyst same-day input")
    p.add_argument("--post-discord", action="store_true", help="post verification report to first Discord webhook (DISCORD_WEBHOOK_URL) only")
    args = p.parse_args()

    db = db_path()
    db_file = Path(db)
    # allow running from repo root or src/
    if not db_file.exists() and not db_file.is_absolute():
        alt = Path(__file__).parent.parent / db
        if alt.exists():
            db = str(alt)
            db_file = alt
    if not db_file.exists():
        print(f"[validate] DB not found at {db} — run ingest first.")
        sys.exit(0)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        # --- fetch rows: today mode vs legacy limit ---
        analyst_rows: list[sqlite3.Row] = []
        if args.today:
            today = _today_kl_str()
            analyst_rows = fetch_analyst_summaries_today(conn)
            # --today must not be starved by future-dated calendar rows (e.g. 2026-10-14) when ordered by published_at DESC.
            # Fetch generously, then filter to today KL. Fallback to id DESC window if published_at ordering misses today.
            cur = conn.execute(
                "SELECT id, title, description, published_at, url, symbols, raw_json FROM raw_events ORDER BY published_at DESC, id DESC LIMIT ?",
                (max(args.limit * 8, 100),),
            )
            all_rows = cur.fetchall()
            rows = [r for r in all_rows if _kl_date(r["published_at"]) == today][: args.limit]
            if not rows:
                # fallback: last 200 by insertion order may contain today's non-calendar news
                cur2 = conn.execute(
                    "SELECT id, title, description, published_at, url, symbols, raw_json FROM raw_events ORDER BY id DESC LIMIT 200"
                )
                alt = cur2.fetchall()
                rows = [r for r in alt if _kl_date(r["published_at"]) == today][: args.limit]
                if rows:
                    print(f"[validate] today KL {today}: recovered {len(rows)} rows via id-DESC fallback (published_at ordering was all future)")
            if not rows:
                print(f"[validate] no rows for today KL {today} (checked {len(all_rows)} recent). analyst summaries today: {len(analyst_rows)}")
                if args.post_discord:
                    msg = build_discord_report(today, analyst_rows, 0, 0, 0, 0, 0, False, "", [f"No news for {today} KL — analyst had {len(analyst_rows)} summaries, nothing to verify."])
                    ok, status = post_discord_first_webhook(msg)
                    print(f"[validate] discord post {'ok' if ok else 'failed'}: {status} (first webhook only)")
                return
            print(f"[validate] today KL {today}: analyst summaries {len(analyst_rows)}, raw_events today {len(rows)}/{len(all_rows)} recent")
        else:
            cur = conn.execute(
                "SELECT id, title, description, published_at, url, symbols, raw_json FROM raw_events ORDER BY id DESC LIMIT ?",
                (args.limit,),
            )
            rows = cur.fetchall()
            if not rows:
                print("[validate] no rows to validate.")
                return

        # decide LLM mode
        api_key = free_api_key()
        use_llm = False
        if args.no_llm:
            use_llm = False
        elif args.with_llm:
            use_llm = True
            if not api_key:
                print("[validate] --with-llm requested but FREE_API_KEY not set — deterministic only.", file=sys.stderr)
                use_llm = False
        else:
            # auto: LLM only if key present and at least one row looks like real news (not stub)
            use_llm = bool(api_key)

        model = args.verifier_model or VERIFIER_MODEL
        fallback = VERIFIER_FALLBACK

        deterministic_bad = 0
        llm_calls = 0
        llm_flags = 0
        llm_errors = 0
        detail_lines: list[str] = []
        for idx, r in enumerate(rows):
            det_errs = validate_row(r)
            llm_verdict = None
            llm_display_errs: list[str] = []
            if use_llm and api_key:
                if idx > 0:
                    time.sleep(0.3)
                try:
                    llm_verdict = call_verifier_llm(r, api_key, model)
                    llm_calls += 1
                    conf = llm_verdict.get("confidence") if isinstance(llm_verdict, dict) else None
                    needs_escalation = False
                    try:
                        needs_escalation = conf is not None and float(conf) < 0.55
                    except Exception:
                        pass
                    if needs_escalation and fallback and fallback != model:
                        print(f"  [verifier] low conf {conf} on id={r['id']} → escalating to {fallback}")
                        try:
                            if llm_calls > 0:
                                time.sleep(0.3)
                            llm_verdict = call_verifier_llm(r, api_key, fallback)
                            llm_calls += 1
                        except Exception as e2:
                            if _is_transient_error(e2):
                                llm_errors += 1
                                print(f"  [verifier] fallback {fallback} transient error id={r['id']}: {e2} — counted as llm_error", file=sys.stderr)
                            else:
                                llm_errors += 1
                                print(f"  [verifier] fallback {fallback} failed id={r['id']}: {e2}", file=sys.stderr)
                    llm_display_errs = llm_verdict_to_errors(llm_verdict)
                    if llm_display_errs:
                        llm_flags += 1
                except Exception as e:
                    if _is_transient_error(e):
                        llm_errors += 1
                        print(f"  [verifier] transient error id={r['id']} ({model}): {e} — counted as llm_error, not fatal", file=sys.stderr)
                    else:
                        llm_errors += 1
                        llm_display_errs = [f"verifier error ({model}): {e}"]
                        print(f"  [verifier] error id={r['id']} ({model}): {e}", file=sys.stderr)

            has_det_err = len(det_errs) > 0
            has_llm_issue = len(llm_display_errs) > 0
            if has_det_err:
                deterministic_bad += 1
            if has_det_err or has_llm_issue:
                tag = f" | llm={llm_verdict.get('verdict')}/{llm_verdict.get('confidence')}" if use_llm and isinstance(llm_verdict, dict) else ""
                if has_det_err and llm_display_errs:
                    combined = "; ".join(det_errs + llm_display_errs)
                elif has_det_err:
                    combined = "; ".join(det_errs)
                elif llm_display_errs:
                    combined = "; ".join(llm_display_errs)
                else:
                    combined = "(transient llm_error — see stderr)"
                prefix = "!" if has_det_err else "⚠"
                line = f"  {prefix} id={r['id']}{tag} {combined} | title={(r['title'] or '')[:70]}"
                print(line)
                if has_det_err:
                    v = llm_verdict.get('verdict') if isinstance(llm_verdict, dict) else 'deterministic'
                    detail_lines.append(f"❌ id={r['id']} {v} — {(r['title'] or '')[:90]}  ({'; '.join(det_errs)[:120]})")
                else:
                    v = llm_verdict.get('verdict') if isinstance(llm_verdict, dict) else 'llm_error'
                    llm_reason = "; ".join(llm_display_errs)[:120] if llm_display_errs else "transient network/rate-limit"
                    detail_lines.append(f"⚠️ id={r['id']} {v} — {(r['title'] or '')[:90]}  ({llm_reason})")
            else:
                if use_llm and isinstance(llm_verdict, dict):
                    print(f"  ✓ id={r['id']} verifier ok (conf {llm_verdict.get('confidence')}) | {(r['title'] or '')[:60]}")
                    detail_lines.append(f"✅ id={r['id']} ok (conf {llm_verdict.get('confidence')}) — {(r['title'] or '')[:80]}")
                else:
                    if det_errs:
                        print(f"  ! id={r['id']} {'; '.join(det_errs)} | {(r['title'] or '')[:60]}")
                    else:
                        if not use_llm:
                            print(f"  ✓ id={r['id']} ok | {(r['title'] or '')[:60]}")
                    detail_lines.append(f"✅ id={r['id']} ok — {(r['title'] or '')[:80]}")

        mode_tag = f" + LLM {model}" if use_llm else " (deterministic only)"
        ok_count = len(rows) - deterministic_bad
        print(f"[validate] checked {len(rows)} rows{mode_tag} — {deterministic_bad} deterministic bad, {ok_count} ok | llm_calls={llm_calls} flags={llm_flags} llm_errors={llm_errors} (flags/errors informational, not fatal)")
        # Market reaction today (one LLM call, only when --today + --with-llm; never predicts tomorrow)
        reaction = None
        if args.today and use_llm and api_key and rows:
            try:
                reaction = call_market_reaction_llm(rows, analyst_rows, api_key, model)
                llm_calls += 1
                print(f"[validate] market reaction: {reaction.get('summary','')[:100] if isinstance(reaction, dict) else reaction}")
            except Exception as e:
                if _is_transient_error(e):
                    llm_errors += 1
                    print(f"[validate] market reaction transient error: {e} — counted as llm_error", file=sys.stderr)
                else:
                    print(f"[validate] market reaction failed: {e}", file=sys.stderr)
                reaction = None
        if args.today:
            today = _today_kl_str()
            flagged = [d for d in detail_lines if d.startswith("❌") or d.startswith("⚠️")]
            ok_lines = [d for d in detail_lines if d.startswith("✅")]
            ordered = flagged + ok_lines
            if args.post_discord:
                msg = build_discord_report(today, analyst_rows, len(rows), len(rows)-deterministic_bad, deterministic_bad, llm_calls, llm_flags, use_llm, model, ordered, reaction)
                ok, status = post_discord_first_webhook(msg)
                print(f"[validate] discord post {'ok' if ok else 'failed'}: {status} (first webhook DISCORD_WEBHOOK_URL only)")
        elif args.post_discord:
            today = _today_kl_str()
            analyst_rows = fetch_analyst_summaries_today(conn)
            flagged = [d for d in detail_lines if d.startswith("❌") or d.startswith("⚠️")]
            ok_lines = [d for d in detail_lines if d.startswith("✅")]
            ordered = flagged + ok_lines
            msg = build_discord_report(today, analyst_rows, len(rows), len(rows)-deterministic_bad, deterministic_bad, llm_calls, llm_flags, use_llm, model, ordered, reaction)
            ok, status = post_discord_first_webhook(msg)
            print(f"[validate] discord post {'ok' if ok else 'failed'}: {status} (first webhook DISCORD_WEBHOOK_URL only)")
        # EXIT CODE: fatal ONLY on deterministic schema errors; LLM flags/rejects and transient network errors are informational (exit 0)
        sys.exit(1 if deterministic_bad else 0)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
