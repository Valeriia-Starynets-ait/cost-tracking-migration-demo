"""Tests for the per-tenant usage meter + pre-invoke budget gate.

Covers the pure cap evaluation and the in-memory meter's rolling counters. Real
AWS is never touched.
"""

from __future__ import annotations

from cost_tracking import pricing
from cost_tracking.usage_meter import (
    InMemoryUsageMeter,
    Usage,
    _evaluate,
)

# ---- pricing --------------------------------------------------------------

def test_estimate_tokens_is_char_over_four_and_floor_one():
    assert pricing.estimate_tokens("") == 0
    assert pricing.estimate_tokens("a") == 1  # floor 1 for non-empty
    assert pricing.estimate_tokens("a" * 40) == 10


def test_cost_usd_uses_per_model_table():
    # sonnet: 0.003 in / 0.015 out per 1k
    cost = pricing.cost_usd("eu.anthropic.claude-sonnet-5", 1000, 1000)
    assert round(cost, 6) == round(0.003 + 0.015, 6)


def test_cost_usd_unknown_model_falls_back_to_the_dearest_offered():
    """An unpriced model must bill at the most expensive rate the platform offers.

    The budget gate is a pre-invoke ceiling, so an under-counting fallback lets
    spend past the limit. Once Opus 5 became selectable, a fallback still pinned
    to the Sonnet rate would have under-counted every unpriced model by ~40%.
    """
    cost = pricing.cost_usd("some-unpriced-model", 1000, 0)
    assert round(cost, 6) == 0.005  # Opus 5 input rate per 1k


# ---- pure evaluation ------------------------------------------------------

def test_evaluate_allows_when_no_limits():
    res = _evaluate(Usage(minute_tokens=10**9), {}, now=1_000_000.0)
    assert res.allowed is True


def test_evaluate_zero_limit_is_unlimited():
    res = _evaluate(Usage(minute_tokens=10**9), {"tpm": 0}, now=1_000_000.0)
    assert res.allowed is True


def test_evaluate_blocks_on_tpm():
    res = _evaluate(Usage(minute_tokens=100), {"tpm": 100}, now=1_000_000.0)
    assert res.allowed is False
    assert res.scope == "tpm"
    assert 1 <= res.retry_after_seconds <= 60


def test_evaluate_blocks_on_daily_tokens():
    res = _evaluate(
        Usage(day_tokens=5000), {"tpm": 0, "max_tokens_per_day": 5000}, now=1_000_000.0
    )
    assert res.allowed is False
    assert res.scope == "daily_tokens"


def test_evaluate_blocks_on_budget():
    res = _evaluate(
        Usage(month_cost_usd=10.0), {"max_budget_usd": 10.0}, now=1_000_000.0
    )
    assert res.allowed is False
    assert res.scope == "budget"


def test_evaluate_tpm_takes_precedence_over_daily():
    res = _evaluate(
        Usage(minute_tokens=100, day_tokens=10_000),
        {"tpm": 100, "max_tokens_per_day": 5000},
        now=1_000_000.0,
    )
    assert res.scope == "tpm"  # checked first


# ---- in-memory meter ------------------------------------------------------

def test_in_memory_meter_accumulates_and_blocks():
    m = InMemoryUsageMeter()
    assert m.active() is True
    # First turn: 50 in + 50 out = 100 tokens.
    m.record("acme", model="eu.anthropic.claude-sonnet-5", input_tokens=50, output_tokens=50)
    u = m.usage("acme")
    assert u.minute_tokens == 100
    assert u.day_tokens == 100
    assert u.month_cost_usd > 0

    # tpm cap of 100 is now reached -> next check blocks.
    res = m.check("acme", {"tpm": 100})
    assert res.allowed is False and res.scope == "tpm"

    # A different tenant is unaffected.
    assert m.check("other", {"tpm": 100}).allowed is True


# ---------------------------------------------------------------------------
# Cache tokens in metering
# ---------------------------------------------------------------------------

def test_in_memory_meter_bills_cache_reads_at_the_discounted_rate():
    """A turn served largely from cache must cost less than the same token
    volume charged as fresh input, or the budget gate cannot see the saving.
    """
    from cost_tracking.usage_meter import InMemoryUsageMeter

    model = "eu.anthropic.claude-sonnet-5"
    fresh = InMemoryUsageMeter()
    fresh.record("t1", model=model, input_tokens=10_000, output_tokens=100)

    cached = InMemoryUsageMeter()
    cached.record("t1", model=model, input_tokens=0, output_tokens=100,
                  cache_read_tokens=10_000)

    assert cached.usage("t1").month_cost_usd < fresh.usage("t1").month_cost_usd


def test_in_memory_meter_counts_cache_tokens_toward_the_token_windows():
    """Cache reads are still tokens processed, so they count against tpm and the
    daily cap even though they are cheaper. Excluding them would let a cached
    workload run past a rate limit it is genuinely consuming."""
    from cost_tracking.usage_meter import InMemoryUsageMeter

    m = InMemoryUsageMeter()
    m.record("t1", model="eu.anthropic.claude-sonnet-5", input_tokens=10,
             output_tokens=5, cache_read_tokens=1_000, cache_write_tokens=200)
    assert m.usage("t1").day_tokens == 10 + 5 + 1_000 + 200


def test_meter_record_cache_kwargs_are_optional():
    from cost_tracking.usage_meter import InMemoryUsageMeter

    m = InMemoryUsageMeter()
    m.record("t1", model="eu.anthropic.claude-sonnet-5", input_tokens=10, output_tokens=5)
    assert m.usage("t1").day_tokens == 15


# ---------------------------------------------------------------------------
# Region input (added in the migration)
# ---------------------------------------------------------------------------

def test_region_defaults_to_us_east_1():
    from cost_tracking.usage_meter import resolve_region

    assert resolve_region() == "us-east-1"


def test_region_resolution_order(monkeypatch):
    from cost_tracking.usage_meter import resolve_region

    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
    assert resolve_region() == "ap-southeast-2"
    monkeypatch.setenv("COST_TRACKING_REGION", "eu-west-1")
    assert resolve_region() == "eu-west-1"
    assert resolve_region("sa-east-1") == "sa-east-1"


def test_dynamodb_meter_builds_its_table_in_the_configured_region(monkeypatch):
    import sys
    from types import SimpleNamespace

    from cost_tracking.usage_meter import DynamoDBUsageMeter

    seen = {}

    def _resource(service, region_name=None):
        seen["service"], seen["region"] = service, region_name
        return SimpleNamespace(Table=lambda name: ("table", name))

    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(resource=_resource))
    monkeypatch.setenv("USAGE_METER_TABLE", "meter")
    monkeypatch.setenv("COST_TRACKING_REGION", "eu-west-1")

    assert DynamoDBUsageMeter().active() is True
    assert seen == {"service": "dynamodb", "region": "eu-west-1"}

    DynamoDBUsageMeter(region_name="us-west-2").active()
    assert seen["region"] == "us-west-2"
