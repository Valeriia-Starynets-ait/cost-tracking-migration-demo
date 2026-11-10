"""Token pricing helpers — base-model-id derivation + cost."""

from __future__ import annotations

import pytest

from cost_tracking import pricing


def test_base_model_id_strips_inference_profile_prefix():
    # CountTokens rejects the cross-region profile id but accepts the FM id.
    assert pricing.base_model_id("eu.anthropic.claude-sonnet-5") == "anthropic.claude-sonnet-5"
    assert pricing.base_model_id("us.anthropic.claude-haiku-4-5") == "anthropic.claude-haiku-4-5"
    assert pricing.base_model_id("apac.amazon.nova-pro-v1:0") == "amazon.nova-pro-v1:0"


def test_base_model_id_passes_through_plain_ids():
    assert pricing.base_model_id("anthropic.claude-sonnet-5") == "anthropic.claude-sonnet-5"
    assert pricing.base_model_id("") == ""


def test_cost_usd_uses_sonnet_table():
    # 1k in + 1k out at (0.003, 0.015) = 0.018
    assert pricing.cost_usd("eu.anthropic.claude-sonnet-5", 1000, 1000) == 0.018


# ---------------------------------------------------------------------------
# Cache-aware cost accounting
# ---------------------------------------------------------------------------

def test_cache_read_tokens_cost_a_tenth_of_fresh_input():
    """A cache read is billed at ~10% of the input rate. Charging it as fresh
    input would erase the saving in our own accounting even when Bedrock had
    already discounted the call.
    """
    fresh = pricing.cost_usd("eu.anthropic.claude-sonnet-5", 10_000, 0)
    cached = pricing.cost_usd(
        "eu.anthropic.claude-sonnet-5", 0, 0, cache_read_tokens=10_000
    )
    assert cached == pytest.approx(fresh * 0.1)


def test_cache_write_tokens_cost_a_premium_over_fresh_input():
    """Writing the cache costs more than plain input (a 25% premium), which is
    why caching only pays when the prefix is reused."""
    fresh = pricing.cost_usd("eu.anthropic.claude-sonnet-5", 10_000, 0)
    written = pricing.cost_usd(
        "eu.anthropic.claude-sonnet-5", 0, 0, cache_write_tokens=10_000
    )
    assert written == pytest.approx(fresh * 1.25)


def test_cache_tokens_default_to_zero_so_existing_callers_are_unchanged():
    model = "eu.anthropic.claude-sonnet-5"
    assert pricing.cost_usd(model, 1_000, 500) == pricing.cost_usd(
        model, 1_000, 500, cache_read_tokens=0, cache_write_tokens=0
    )


def test_cache_tokens_are_additive_not_a_subset_of_input_tokens():
    """Bedrock reports cacheReadInputTokens SEPARATELY from inputTokens, so the
    cost is the sum. Treating cache reads as already-included would undercount.
    """
    model = "eu.anthropic.claude-haiku-4-5"
    total = pricing.cost_usd(model, 100, 50, cache_read_tokens=900)
    parts = (
        pricing.cost_usd(model, 100, 50)
        + pricing.cost_usd(model, 0, 0, cache_read_tokens=900)
    )
    assert total == pytest.approx(parts)


def test_unpriced_model_still_prices_cache_tokens_on_the_fallback_tier():
    cached = pricing.cost_usd("some.unknown-model", 0, 0, cache_read_tokens=10_000)
    assert cached > 0


def test_claude_5_5_prices_follow_the_aws_price_list():
    sonnet = "eu.anthropic.claude-sonnet-5-5"
    assert pricing.cost_usd(sonnet, 0, 0, cache_read_tokens=1_000_000) == pytest.approx(0.11, abs=1e-9)
    haiku = pricing.quote_model("eu.anthropic.claude-haiku-5-5")
    assert haiku.status == "known"
    assert pricing.cost_usd("eu.anthropic.claude-haiku-5-5", 0, 0, cache_read_tokens=1_000_000) == pytest.approx(0.011, abs=1e-9)
    assert pricing.cost_usd("eu.anthropic.claude-opus-5-5", 1_000_000, 1_000_000) == pytest.approx(26.4, abs=1e-9)
    assert pricing.cache_read_multiplier("eu.anthropic.claude-opus-5-5") == 0.05
    assert pricing.cache_read_multiplier("eu.anthropic.claude-haiku-5-5") == 0.1
    assert pricing.cache_read_multiplier("eu.anthropic.claude-sonnet-5") == 0.1
    assert pricing.quote_model(sonnet).cache_read_multiplier == pricing.Decimal("0.05")
    consumption, _ = pricing.cost_for_consumption(sonnet, 0, 0, cache_read_tokens=1_000_000)
    assert float(consumption) == pytest.approx(0.11, abs=1e-9)


# ---------------------------------------------------------------------------
# Migration: the inference-profile prefix is an input
# ---------------------------------------------------------------------------

def test_default_inference_profile_prefix_is_eu(monkeypatch):
    monkeypatch.delenv(pricing.INFERENCE_PROFILE_PREFIX_ENV, raising=False)
    assert pricing.inference_profile_prefix() == "eu."


def test_inference_profile_prefix_env_selects_the_default_price_table(monkeypatch):
    monkeypatch.setenv(pricing.INFERENCE_PROFILE_PREFIX_ENV, "us")  # dot is added
    assert pricing.inference_profile_prefix() == "us."
    assert pricing.quote_model("us.anthropic.claude-sonnet-5").status == "known"
    assert pricing.cost_usd("us.anthropic.claude-sonnet-5", 1000, 1000) == 0.018
    # The old prefix is no longer priced, so it hits the conservative fallback.
    assert pricing.quote_model("eu.anthropic.claude-sonnet-5").status == "estimated_fallback"


def test_empty_prefix_prices_plain_foundation_model_ids(monkeypatch):
    monkeypatch.setenv(pricing.INFERENCE_PROFILE_PREFIX_ENV, "")
    assert pricing.cost_usd("anthropic.claude-sonnet-5", 1000, 1000) == 0.018


def test_base_model_id_strips_a_configured_prefix_outside_the_builtin_list(monkeypatch):
    monkeypatch.setenv(pricing.INFERENCE_PROFILE_PREFIX_ENV, "global.")
    assert pricing.base_model_id("global.anthropic.claude-sonnet-5") == "anthropic.claude-sonnet-5"
    assert pricing.cache_read_multiplier("global.anthropic.claude-sonnet-5-5") == 0.05


def test_bedrock_token_prices_env_still_overrides_the_table(monkeypatch):
    monkeypatch.setenv("BEDROCK_TOKEN_PRICES", '{"my.model": [1.0, 2.0]}')
    assert pricing.cost_usd("my.model", 1000, 1000) == 3.0
