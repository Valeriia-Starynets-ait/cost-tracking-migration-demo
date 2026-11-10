"""Token pricing + estimation for the budget gate.

The control plane meters token spend so the per-tenant budget cap
(``max_budget_usd``) can be enforced pre-invoke. Two pure helpers:

  * :func:`estimate_tokens` — a dependency-free char/4 approximation used when
    the runtime does not surface a usage frame. Real usage is preferred when
    available; the estimate is the floor so a runaway agent is still caught.
  * :func:`cost_usd` — converts (model, input_tokens, output_tokens) into a
    dollar figure using a per-model price table.

Prices are USD per 1,000 tokens, expressed as ``(input, output)``. They are
representative on-demand Bedrock list prices and are intentionally overridable
via the ``BEDROCK_TOKEN_PRICES`` env var (JSON ``{model: [in, out]}``) so a
price change never needs a code deploy. Unknown models fall back to the
Opus 5 rate (the most expensive in the table) so an unpriced model is never
under-charged against a tenant's budget.

The cross-region inference-profile prefix that keys the built-in price table
(``us``, ``eu``, ``apac``, ...) is an input: set ``COST_TRACKING_INFERENCE_PROFILE_PREFIX``
(default :data:`DEFAULT_INFERENCE_PROFILE_PREFIX`) or call
:func:`default_prices` with an explicit prefix.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from decimal import Decimal

#: Env var that selects the inference-profile prefix of the built-in price table.
INFERENCE_PROFILE_PREFIX_ENV = "COST_TRACKING_INFERENCE_PROFILE_PREFIX"

#: Prefix used when :data:`INFERENCE_PROFILE_PREFIX_ENV` is not set.
DEFAULT_INFERENCE_PROFILE_PREFIX = "us"

# USD per 1,000 tokens: (input, output), keyed by the foundation-model id with
# no inference-profile prefix. Representative on-demand list prices; the
# prefix is added by default_prices().
_BASE_PRICES: dict[str, tuple[float, float]] = {
    "anthropic.claude-opus-5-5": (0.0044, 0.022),
    "anthropic.claude-sonnet-5-5": (0.0022, 0.011),
    "anthropic.claude-haiku-5-5": (0.00011, 0.00055),
    "anthropic.claude-opus-5": (0.005, 0.025),
    "anthropic.claude-sonnet-5": (0.003, 0.015),
    "anthropic.claude-haiku-4-5": (0.0008, 0.004),
    "amazon.nova-pro-v1:0": (0.0008, 0.0032),
}


def inference_profile_prefix() -> str:
    """The configured inference-profile prefix, without a trailing dot."""
    raw = os.environ.get(INFERENCE_PROFILE_PREFIX_ENV, "").strip().strip(".")
    return raw or DEFAULT_INFERENCE_PROFILE_PREFIX


def default_prices(prefix: str | None = None) -> dict[str, tuple[float, float]]:
    """The built-in price table keyed by ``<prefix>.<foundation-model id>``."""
    p = (prefix or "").strip(".") or inference_profile_prefix()
    return {f"{p}.{base}": pair for base, pair in _BASE_PRICES.items()}

# Fallback tier for an unknown/unpriced model — the most expensive offered, so
# the budget gate never under-counts. Tracks Opus 5: leaving this at the Sonnet
# rate once Opus is selectable would under-count every unpriced model by ~40%.
_FALLBACK = (0.005, 0.025)

#: Prompt-caching multipliers applied to the model's *input* rate.
#:
#: Reading a cached prefix is billed at a tenth of fresh input; writing it costs
#: a 25% premium. That asymmetry is the whole economics of caching: a prefix has
#: to be reused before it pays for itself. Charging a cache read as fresh input
#: would hide a real discount from our own budget gate and per-tenant cost view.
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25

#: Sonnet 5.5 and Opus 5.5 bill a cache read at 0.05x of input (AWS Price List).
_HALF_CACHE_READ_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5")


def cache_read_multiplier(model: str) -> float:
    """Cache read price as a multiple of the input rate for ``model``."""
    base = base_model_id(model or "")
    if any(m in base for m in _HALF_CACHE_READ_MODELS):
        return 0.05
    return CACHE_READ_MULTIPLIER


@dataclass(frozen=True)
class PriceQuote:
    """A model price with provenance for cost-attribution surfaces."""

    model_id: str
    input_per_1k_usd: Decimal
    output_per_1k_usd: Decimal
    cache_read_multiplier: Decimal
    cache_write_multiplier: Decimal
    status: str


def _prices() -> dict[str, tuple[float, float]]:
    raw = os.environ.get("BEDROCK_TOKEN_PRICES")
    if not raw:
        return default_prices()
    try:
        loaded = json.loads(raw)
        return {k: (float(v[0]), float(v[1])) for k, v in loaded.items()}
    except (TypeError, ValueError, KeyError, IndexError):
        return default_prices()


def quote_model(model: str) -> PriceQuote:
    """Return the configured rate and whether a conservative fallback was used."""

    prices = _prices()
    pair = prices.get(model)
    status = "known" if pair is not None else "estimated_fallback"
    input_rate, output_rate = pair or _FALLBACK
    return PriceQuote(
        model_id=model,
        input_per_1k_usd=Decimal(str(input_rate)),
        output_per_1k_usd=Decimal(str(output_rate)),
        cache_read_multiplier=Decimal(str(cache_read_multiplier(model))),
        cache_write_multiplier=Decimal(str(CACHE_WRITE_MULTIPLIER)),
        status=status,
    )


def cost_for_consumption(
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> tuple[Decimal, str]:
    """Calculate a Decimal estimate and carry the price provenance."""

    quote = quote_model(model)
    thousand = Decimal(1000)
    amount = (
        Decimal(input_tokens) / thousand * quote.input_per_1k_usd
        + Decimal(output_tokens) / thousand * quote.output_per_1k_usd
        + Decimal(cache_read_tokens) / thousand
        * quote.input_per_1k_usd
        * quote.cache_read_multiplier
        + Decimal(cache_write_tokens) / thousand
        * quote.input_per_1k_usd
        * quote.cache_write_multiplier
    )
    return amount, quote.status


#: Well-known cross-region inference-profile geo prefixes. The Bedrock
#: ``CountTokens`` API rejects the profile id (e.g.
#: ``eu.anthropic.claude-sonnet-4-6`` -> ``ValidationException: model doesn't
#: support counting tokens``) but accepts the underlying foundation-model id
#: (``anthropic.claude-sonnet-4-6``). The configured prefix (see
#: :func:`inference_profile_prefix`) is stripped as well.
_PROFILE_PREFIXES = ("eu.", "us.", "apac.")


def base_model_id(model: str) -> str:
    """Strip a cross-region inference-profile prefix to the foundation-model id.

    ``eu.anthropic.claude-sonnet-4-6`` -> ``anthropic.claude-sonnet-4-6``. Used
    only for ``CountTokens`` (which needs the FM id); pricing keys stay on the
    full profile id. Unknown/plain ids pass through unchanged.
    """
    if not model:
        return model
    for p in (*_PROFILE_PREFIXES, f"{inference_profile_prefix()}."):
        if model.startswith(p):
            return model[len(p):]
    return model


def estimate_tokens(text: str) -> int:
    """Approximate token count for a UTF-8 string (~4 chars/token).

    Used only when the runtime does not return real usage. Always returns at
    least 1 for non-empty input so an invoke never meters as zero.
    """
    if not text:
        return 0
    return max(1, len(text) // 4)


def cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    """Dollar cost of a turn for ``model`` given its token counts.

    Cache counts are **additive**: Bedrock reports ``cacheReadInputTokens`` and
    ``cacheWriteInputTokens`` separately from ``inputTokens``, so they are not a
    subset to be discounted out of it. Both default to zero, so callers that
    predate caching are unaffected.
    """
    price_in, price_out = _prices().get(model, _FALLBACK)
    return (
        (input_tokens / 1000.0) * price_in
        + (output_tokens / 1000.0) * price_out
        + (cache_read_tokens / 1000.0) * price_in * cache_read_multiplier(model)
        + (cache_write_tokens / 1000.0) * price_in * CACHE_WRITE_MULTIPLIER
    )
