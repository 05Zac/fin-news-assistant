"""
config.py — tiered router stub (easy→free / hard→paid) + RTK+headroom budget.

Locked arch: Linux gateway sole 24/7 prod, SQLite dumb store (WAL),
free tier for grunt, paid 3.8B API (sunk cost) for reasoning.
RTK + headroom check before every LLM call.

Usage:
    from config import route, Tier, estimate_tokens, check_budget
"""

import os

# --- tiers ---
FREE = "free"
PAID = "paid"

# Hard tasks go paid; easy tasks go free. Keep list explicit so n8n/Python stays thin.
HARD_TASKS = {"fact_extraction", "market_analysis", "verification", "summarize"}
EASY_TASKS = {"dedup", "clean", "classify", "tag"}

# Fin verifier — cost-no-object pick for Evidence Bundle → Fact Extract → Validate
# Primary: claude-opus-4.7 (most deterministic verifier, temp 0.0 + json_mode)
# Fallback/escalation: claude-fable-5.1 when opus-4.7 returns low confidence / conflict
# Note: claude-opus-5.5 does not exist on CodeCraft (33-model list cc_7uzr...Rsj);
# claude-opus-5 exists but is more creative → higher hallucination risk for this role.
VERIFIER_MODEL = os.getenv("VERIFIER_MODEL", "claude-opus-4.7")
VERIFIER_FALLBACK = os.getenv("VERIFIER_FALLBACK", "claude-fable-5.1")

# Budget: 3.8B tokens/month paid API (~126M/day). Keep headroom so we never hit ceiling.
# For v1 we use a simple daily counter stub; replace with RTK/redis later if needed.
MONTHLY_BUDGET = int(os.getenv("PAID_MONTHLY_BUDGET", "3800000000"))
HEADROOM_PCT = float(os.getenv("HEADROOM_PCT", "0.10"))  # 10% headroom

# Rough token estimate: ~4 chars per token (RTK would be precise tokenizer)
def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)

def check_budget(estimated_tokens: int, used_this_month: int = 0) -> bool:
    """Return True if we have headroom to call paid tier."""
    effective_limit = int(MONTHLY_BUDGET * (1 - HEADROOM_PCT))
    return (used_this_month + estimated_tokens) <= effective_limit

def route(task: str, estimated_tokens: int = 0, used_this_month: int = 0) -> str:
    """
    Tiered router stub.
    - hard tasks → paid (if budget allows, else fallback to free with warning)
    - easy tasks → free
    """
    task = task.lower().strip()
    if task in HARD_TASKS:
        if check_budget(estimated_tokens, used_this_month):
            return PAID
        # fallback: hard task but no headroom → still free (log warning)
        print(f"[router] WARN: hard task '{task}' falling back to FREE (budget headroom hit)")
        return FREE
    # default: easy / unknown → free
    return FREE

# Env helpers
def db_path() -> str:
    return os.getenv("DB_PATH", "data/fin_news.db")

def marketaux_token() -> str | None:
    return os.getenv("MARKETAUX_API_TOKEN")

def paid_api_key() -> str | None:
    return os.getenv("PAID_API_KEY")

def discord_webhook() -> str | None:
    return os.getenv("DISCORD_WEBHOOK_URL")
