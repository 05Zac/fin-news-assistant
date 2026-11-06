"""
config.py — env helpers + LLM routing.

All LLM work runs on the FREE tier: the local FreeLLMAPI router
(OpenAI-compatible, see llm.py). The old paid tier is gone.

Usage:
    from config import route, estimate_tokens, FREE
"""

import os

FREE = "free"

# Verifier models on FreeLLMAPI (router profiles; see llm.py / FreeLLMAPI docs)
VERIFIER_MODEL = os.getenv("VERIFIER_MODEL", "auto")
VERIFIER_FALLBACK = os.getenv("VERIFIER_FALLBACK", "auto:smart")


# Rough token estimate: ~4 chars per token
def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


def route(task: str, estimated_tokens: int = 0, used_this_month: int = 0) -> str:
    """Kept for callers' compatibility: every task runs on the free tier."""
    return FREE

# Env helpers
def db_path() -> str:
    return os.getenv("DB_PATH", "data/fin_news.db")

def marketaux_token() -> str | None:
    return os.getenv("MARKETAUX_API_TOKEN")

# --- FREE tier (FreeLLMAPI local router, OpenAI-compatible) ---
def free_api_key() -> str | None:
    return os.getenv("FREE_API_KEY")

def free_api_base() -> str:
    return os.getenv("FREE_API_BASE", "http://127.0.0.1:3001/v1")

def free_model() -> str:
    return os.getenv("FREE_MODEL", "auto")

def discord_webhook() -> str | None:
    return os.getenv("DISCORD_WEBHOOK_URL")

def finnhub_api_key() -> str | None:
    return os.getenv("FINNHUB_API_KEY")

def finnhub_token() -> str | None:
    """Alias for finnhub_api_key — keeps naming consistent with Marketaux helper."""
    return os.getenv("FINNHUB_API_KEY")
