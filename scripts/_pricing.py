"""Shared pricing/cost logic for spend-reporting scripts.

Mirrors the PRICING / CACHE_MULTIPLIERS / REGIONAL_PREMIUM / OUTPUT_TYPES
logic in ../main.py. Kept in sync manually: when main.py's pricing changes,
update here as well. Duplicated (not imported) to keep the scripts runnable
without the service's dependencies.
"""
from __future__ import annotations

PRICING: dict[str, dict[str, float]] = {
    # Anthropic — verified 2026-06-05 against
    # https://platform.claude.com/docs/en/docs/about-claude/pricing
    "claude-opus-4-8":   {"input": 5.00,  "output": 25.00},
    "claude-opus-4-7":   {"input": 5.00,  "output": 25.00},
    "claude-opus-4-6":   {"input": 5.00,  "output": 25.00},
    "claude-opus-4-5":   {"input": 5.00,  "output": 25.00},
    "claude-opus-4-1":   {"input": 15.00, "output": 75.00},
    "claude-opus-4":     {"input": 15.00, "output": 75.00},
    "claude-sonnet-4-6": {"input": 3.00,  "output": 15.00},
    "claude-sonnet-4-5": {"input": 3.00,  "output": 15.00},
    "claude-sonnet-4":   {"input": 3.00,  "output": 15.00},
    "claude-haiku-4-5":  {"input": 1.00,  "output": 5.00},
    "claude-haiku-3-5":  {"input": 0.80,  "output": 4.00},
    # Pseudo-models that appear in the token_count metric at zero tokens.
    "count-tokens":      {"input": 0.00,  "output": 0.00},
    # Google
    "gemini-2.0-flash":          {"input": 0.15,  "output": 0.60},
    "gemini-2.0-flash-001":      {"input": 0.15,  "output": 0.60},
    "gemini-2.0-flash-lite":     {"input": 0.075, "output": 0.30},
    "gemini-2.0-flash-lite-001": {"input": 0.075, "output": 0.30},
    "gemini-2.5-flash":          {"input": 0.30,  "output": 2.50},
    "gemini-2.5-flash-lite":     {"input": 0.10,  "output": 0.40},
    "gemini-2.5-pro":            {"input": 1.25,  "output": 10.00},
    "gemini-3.0-flash":          {"input": 0.50,  "output": 3.00},
    "gemini-3.0-pro":            {"input": 2.00,  "output": 12.00},
}

# Used for models not in PRICING. Intentionally aggressive (Opus 4.1 tier):
# unknown models should be overestimated so enforcement fires early rather
# than letting spend slip past.
FALLBACK_PRICING: dict[str, float] = {"input": 15.00, "output": 75.00}

# Multiplier for regional endpoints (e.g. us-east5 charges 10% over global).
# Set to 1.0 if querying a deployment pinned to the global endpoint.
REGIONAL_PREMIUM: float = 1.10

CACHE_MULTIPLIERS: dict[str, float] = {
    "input":                1.0,
    "cache_read_input":     0.1,
    "cache_write_input":    1.25,
    "cache_write_5m_input": 1.25,
    "cache_write_1h_input": 2.0,
}

OUTPUT_TYPES: set[str] = {"output"}


def token_cost(model: str, token_type: str, count: int) -> float:
    """Return USD cost for `count` tokens of `token_type` for `model`.

    Unknown models fall through to FALLBACK_PRICING. Cache read/write token
    types are priced at the input rate times their cache multiplier. Output
    tokens are priced at the output rate. Regional premium is applied.
    """
    prices = PRICING.get(model, FALLBACK_PRICING)
    if token_type in OUTPUT_TYPES:
        base = prices["output"]
        mult = 1.0
    elif token_type in CACHE_MULTIPLIERS:
        base = prices["input"]
        mult = CACHE_MULTIPLIERS[token_type]
    else:
        base = prices["input"]
        mult = 1.0
    return (count / 1_000_000) * base * mult * REGIONAL_PREMIUM


def is_known(model: str) -> bool:
    return model in PRICING
