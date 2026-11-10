"""Tests for the per-tenant usage meter + pre-invoke budget gate.

Covers the pure cap evaluation, the in-memory meter's rolling counters, and the
invoke-path integration (TPM / daily-token / budget -> HTTP 429 unary, SSE
error frame streaming). Real AWS is never touched: the in-memory meter is
injected via the module-level ``_usage_meter`` handle.
"""

from __future__ import annotations

import importlib.util
from types import SimpleNamespace

import pytest

from cost_tracking import pricing, usage_meter
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


# ---- invoke integration ---------------------------------------------------

# These tests exercise the invoke path (``control_plane.saga.invoke`` and the
# ``control_plane.handlers.api`` route), which is NOT part of the cost-tracking
# migration. They are kept verbatim and run automatically wherever the
# ``control_plane`` package is importable; otherwise they are reported as skipped.
requires_control_plane = pytest.mark.skipif(
    importlib.util.find_spec("control_plane") is None,
    reason="invoke path (control_plane.saga.invoke / handlers.api) not migrated",
)

def _agent_row(c, a):
    return {
        "customer_id": c,
        "agent_id": a,
        "model": "eu.anthropic.claude-sonnet-5",
        "status": "ACTIVE",
        "runtime_arn": "arn:aws:bedrock-agentcore:eu-central-1:1:runtime/x",
    }


def _wire_invoke(monkeypatch, *, limits: dict, meter: InMemoryUsageMeter):
    from control_plane.saga import invoke as invoke_mod

    monkeypatch.setattr(invoke_mod, "_resolve_agent", _agent_row)
    monkeypatch.setattr(invoke_mod, "_usage_meter", meter)
    monkeypatch.setattr(invoke_mod.ddb, "get_customer", lambda c: {"limits": limits})
    monkeypatch.setattr(invoke_mod, "emit_invoke_metrics", lambda **kw: None)
    monkeypatch.setattr(invoke_mod.ddb, "record_session_turn", lambda **kw: None)

    class _Tracker:
        def bind_or_verify(self, *a, **k):
            return True

    monkeypatch.setattr(invoke_mod, "_session_tracker", _Tracker())
    return invoke_mod


@requires_control_plane
def test_unary_invoke_blocks_when_budget_reached(monkeypatch):
    meter = InMemoryUsageMeter()
    # Pre-load usage past the daily cap.
    meter.record("c", model="eu.anthropic.claude-sonnet-5", input_tokens=6000, output_tokens=0)
    invoke_mod = _wire_invoke(
        monkeypatch, limits={"max_tokens_per_day": 5000}, meter=meter
    )

    body = SimpleNamespace(prompt="hi", session_id="s" * 36)
    with pytest.raises(usage_meter.BudgetExceededError) as exc:
        invoke_mod.invoke_unary(
            customer_id="c", agent_id="a", body=body, end_user_id="u"
        )
    assert exc.value.scope == "daily_tokens"


@requires_control_plane
def test_unary_invoke_allows_and_records_when_under_cap(monkeypatch):
    meter = InMemoryUsageMeter()
    invoke_mod = _wire_invoke(monkeypatch, limits={"tpm": 1_000_000}, meter=meter)
    # Runtime returns one token.
    monkeypatch.setattr(
        invoke_mod,
        "_runtime_sse",
        lambda row, body, **_kw: iter(
            [b'data: {"token": "hello"}\n\n', b'data: {"done":true}\n\n']
        ),
    )

    body = SimpleNamespace(prompt="a prompt", session_id="s" * 36)
    out = invoke_mod.invoke_unary(
        customer_id="c", agent_id="a", body=body, end_user_id="u"
    )
    assert out["response"] == "hello"
    # Usage was recorded for the turn.
    assert meter.usage("c").minute_tokens > 0


@requires_control_plane
def test_gate_is_noop_when_meter_inactive(monkeypatch):
    """A DynamoDB meter with no table must not block or read tenant limits."""
    from control_plane.saga import invoke as invoke_mod

    class _Inactive:
        def active(self):
            return False

    monkeypatch.setattr(invoke_mod, "_usage_meter", _Inactive())

    def _boom(_c):
        raise AssertionError("get_customer must not be called when meter inactive")

    monkeypatch.setattr(invoke_mod.ddb, "get_customer", _boom)
    # Should simply return without raising.
    invoke_mod._enforce_budget("c", "a")


@requires_control_plane
def test_invoke_route_maps_budget_error_to_http_429():
    """The unary /invoke route converts BudgetExceededError -> HTTP 429."""
    import json
    from types import SimpleNamespace
    from unittest.mock import patch

    from control_plane.handlers.api import handler

    from cost_tracking.usage_meter import BudgetExceededError

    ctx = SimpleNamespace(
        function_name="t",
        memory_limit_in_mb=512,
        invoked_function_arn="arn:aws:lambda:eu-central-1:111111111111:function:t",
        aws_request_id="r",
    )
    event = {
        "httpMethod": "POST",
        "path": "/customers/cust-A/agents/01J/invoke",
        "resource": "/customers/cust-A/agents/01J/invoke",
        "pathParameters": None,
        "queryStringParameters": None,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"prompt": "hi"}),
        "requestContext": {
            "authorizer": {"claims": {"custom:customer_id": "cust-A", "sub": "u"}},
            "requestId": "r",
        },
    }
    with patch(
        "control_plane.handlers.api.invoke.invoke_unary",
        side_effect=BudgetExceededError("daily token cap reached", scope="daily_tokens", retry_after_seconds=60),
    ):
        resp = handler(event, ctx)
    assert resp["statusCode"] == 429


@requires_control_plane
def test_stream_invoke_emits_429_error_frame_when_capped(monkeypatch):
    meter = InMemoryUsageMeter()
    meter.record("c", model="eu.anthropic.claude-sonnet-5", input_tokens=200, output_tokens=0)
    invoke_mod = _wire_invoke(monkeypatch, limits={"tpm": 100}, meter=meter)

    # If the gate fails, this would be called — assert it is NOT.
    monkeypatch.setattr(
        invoke_mod,
        "_runtime_sse",
        lambda r, b, **_kw: (_ for _ in ()).throw(AssertionError("runtime should not run")),
    )

    body = SimpleNamespace(prompt="hi", session_id="s" * 36)
    frames = b"".join(invoke_mod.stream_invoke("c", "a", body))
    assert b'"error"' in frames
    assert b'"code":"BUDGET_EXCEEDED"' in frames
    assert b"per-minute" not in frames
    assert b'"type":"done"' not in frames


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
# Migration: the AWS Region of the DynamoDB meter is an input
# ---------------------------------------------------------------------------

def test_dynamodb_meter_region_argument_beats_env(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    assert usage_meter.DynamoDBUsageMeter(region_name="ap-southeast-2")._region() == "ap-southeast-2"


def test_dynamodb_meter_region_falls_back_to_env_then_default(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    assert usage_meter.DynamoDBUsageMeter()._region() == "us-west-2"
    monkeypatch.delenv("AWS_REGION")
    assert usage_meter.DynamoDBUsageMeter()._region() == usage_meter.DEFAULT_REGION


def test_make_usage_meter_passes_region_to_the_dynamodb_backend(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "dynamodb")
    meter = usage_meter.make_usage_meter(region_name="eu-west-1")
    assert meter._region() == "eu-west-1"
