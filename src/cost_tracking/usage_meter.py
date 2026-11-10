"""Per-tenant usage meter + pre-invoke budget gate.

RPM is expected to be enforced at the API edge. The three remaining
per-tenant caps — ``tpm`` (tokens/minute), ``max_tokens_per_day``
and ``max_budget_usd`` — cannot be enforced at the edge because the cost of a
turn is only known after the model runs. This module promotes them from
*metered* to a *hard gate*:

  * pre-invoke, :func:`UsageMeter.check` compares the tenant's *accumulated*
    usage in the current minute / UTC day / calendar month against its caps and
    blocks the call when a cap is already reached. Blocking on accumulated
    usage is the honest hard-gate semantics: once a tenant crosses a cap the
    next request is refused until the window rolls over.
  * post-invoke, :func:`UsageMeter.record` adds the turn's tokens (real usage
    when the runtime surfaces it, otherwise a char/4 estimate) and dollar cost
    to the rolling counters.

Two backends, selected by the ``STORAGE_BACKEND`` env var (see
:func:`make_usage_meter`):

    DynamoDBUsageMeter — production. Atomic ``ADD`` counters on the
        ``USAGE_METER_TABLE`` with a per-window TTL so old buckets self-expire.
        When the table is not configured the meter is a no-op that *allows*
        (fail-open) so an un-provisioned edge never breaks invokes.

    InMemoryUsageMeter — local dev / tests. Same window math, process-local
        counters, no TTL.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from cost_tracking import pricing

#: Env var that selects the AWS Region of the DynamoDB meter table.
REGION_ENV = "COST_TRACKING_REGION"

#: Region used when neither an explicit ``region_name``, ``COST_TRACKING_REGION``
#: nor ``AWS_REGION`` is set.
DEFAULT_REGION = "us-east-1"


def resolve_region(region_name: str | None = None) -> str:
    """Region for the meter table: argument > COST_TRACKING_REGION > AWS_REGION > default."""
    return (
        region_name
        or os.environ.get(REGION_ENV)
        or os.environ.get("AWS_REGION")
        or DEFAULT_REGION
    )


class BudgetExceededError(Exception):
    """Raised by the invoke path when a tenant has reached a usage cap.

    Carries the cap that tripped (``scope``) and a ``retry_after_seconds`` hint
    so the route layer can return HTTP 429 with a ``Retry-After`` header.
    """

    def __init__(self, reason: str, *, scope: str, retry_after_seconds: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.scope = scope
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True)
class Usage:
    minute_tokens: int = 0
    day_tokens: int = 0
    month_cost_usd: float = 0.0


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    scope: str | None = None  # tpm | daily_tokens | budget
    reason: str | None = None
    retry_after_seconds: int = 0


def _now() -> float:
    return time.time()


def _windows(now: float) -> tuple[str, str, str]:
    """Return (minute_key, day_key, month_key) for the timestamp ``now``."""
    dt = datetime.fromtimestamp(now, tz=UTC)
    return (
        f"min#{int(now // 60)}",
        f"day#{dt:%Y-%m-%d}",
        f"mon#{dt:%Y-%m}",
    )


def _seconds_to_next_minute(now: float) -> int:
    return max(1, 60 - int(now % 60))


def _seconds_to_next_day(now: float) -> int:
    dt = datetime.fromtimestamp(now, tz=UTC)
    secs_into_day = dt.hour * 3600 + dt.minute * 60 + dt.second
    return max(1, 86_400 - secs_into_day)


def _evaluate(usage: Usage, limits: dict, now: float) -> GateResult:
    """Pure cap comparison. ``0``/absent limit means unlimited."""
    tpm = int(limits.get("tpm") or 0)
    daily = int(limits.get("max_tokens_per_day") or 0)
    budget = float(limits.get("max_budget_usd") or 0.0)

    if tpm and usage.minute_tokens >= tpm:
        return GateResult(
            allowed=False,
            scope="tpm",
            reason=f"tokens-per-minute cap reached ({usage.minute_tokens}/{tpm})",
            retry_after_seconds=_seconds_to_next_minute(now),
        )
    if daily and usage.day_tokens >= daily:
        return GateResult(
            allowed=False,
            scope="daily_tokens",
            reason=f"daily token cap reached ({usage.day_tokens}/{daily})",
            retry_after_seconds=_seconds_to_next_day(now),
        )
    if budget and usage.month_cost_usd >= budget:
        return GateResult(
            allowed=False,
            scope="budget",
            reason=(
                f"monthly budget reached (${usage.month_cost_usd:.4f}/${budget:.2f})"
            ),
            retry_after_seconds=_seconds_to_next_day(now),
        )
    return GateResult(allowed=True)


class UsageMeter:
    """Interface — see backend subclasses."""

    def active(self) -> bool:
        """Whether the gate should run. False for an un-provisioned backend
        (no meter table) so the invoke path stays fail-open until deploy."""
        return True

    def usage(self, customer_id: str) -> Usage:  # pragma: no cover - interface
        raise NotImplementedError

    def record(
        self,
        customer_id: str,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def check(self, customer_id: str, limits: dict) -> GateResult:
        """Compare accumulated usage against ``limits`` (pre-invoke gate)."""
        return _evaluate(self.usage(customer_id), limits or {}, _now())


class InMemoryUsageMeter(UsageMeter):
    """Process-local counters for local dev and tests."""

    def __init__(self) -> None:
        # {(customer_id, window_key): value}; tokens are ints, cost is float.
        self._tokens: dict[tuple[str, str], int] = {}
        self._cost: dict[tuple[str, str], float] = {}

    def usage(self, customer_id: str) -> Usage:
        minute_k, day_k, month_k = _windows(_now())
        return Usage(
            minute_tokens=self._tokens.get((customer_id, minute_k), 0),
            day_tokens=self._tokens.get((customer_id, day_k), 0),
            month_cost_usd=self._cost.get((customer_id, month_k), 0.0),
        )

    def record(
        self,
        customer_id: str,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> None:
        # Cache tokens count toward the token windows (they are still tokens
        # processed, so tpm and the daily cap must see them) but are costed at
        # their own discounted/premium rates via pricing.cost_usd.
        total = (int(input_tokens) + int(output_tokens)
                 + int(cache_read_tokens) + int(cache_write_tokens))
        cost = pricing.cost_usd(
            model, int(input_tokens), int(output_tokens),
            cache_read_tokens=int(cache_read_tokens),
            cache_write_tokens=int(cache_write_tokens),
        )
        minute_k, day_k, month_k = _windows(_now())
        for k in (minute_k, day_k):
            self._tokens[(customer_id, k)] = self._tokens.get((customer_id, k), 0) + total
        self._cost[(customer_id, month_k)] = (
            self._cost.get((customer_id, month_k), 0.0) + cost
        )


class DynamoDBUsageMeter(UsageMeter):
    """Atomic-counter meter on ``USAGE_METER_TABLE`` (PK ``pk``, SK ``window``)."""

    # TTLs (seconds) keep each bucket alive a little past its window so a
    # straddling read is correct, then let DynamoDB reap it.
    _MIN_TTL = 180
    _DAY_TTL = 2 * 86_400
    _MON_TTL = 40 * 86_400

    def __init__(self, *, table=None, region_name: str | None = None) -> None:
        self._explicit_table = table
        self._region_name = region_name

    def active(self) -> bool:
        return self._table() is not None

    def _table(self):
        if self._explicit_table is not None:
            return self._explicit_table
        name = os.environ.get("USAGE_METER_TABLE")
        if not name:
            return None
        import boto3

        return boto3.resource(
            "dynamodb", region_name=resolve_region(self._region_name)
        ).Table(name)

    def usage(self, customer_id: str) -> Usage:
        table = self._table()
        if table is None:
            return Usage()
        minute_k, day_k, month_k = _windows(_now())

        def _get(window: str, attr: str) -> float:
            resp = table.get_item(Key={"pk": customer_id, "window": window})
            item = resp.get("Item") or {}
            return float(item.get(attr, 0) or 0)

        return Usage(
            minute_tokens=int(_get(minute_k, "tokens")),
            day_tokens=int(_get(day_k, "tokens")),
            month_cost_usd=_get(month_k, "cost"),
        )

    def record(
        self,
        customer_id: str,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> None:
        table = self._table()
        if table is None:
            return  # fail-open until the meter table is provisioned
        from decimal import Decimal

        # See InMemoryUsageMeter.record: cache tokens count toward the token
        # windows but carry their own price multipliers.
        total = (int(input_tokens) + int(output_tokens)
                 + int(cache_read_tokens) + int(cache_write_tokens))
        cost = pricing.cost_usd(
            model, int(input_tokens), int(output_tokens),
            cache_read_tokens=int(cache_read_tokens),
            cache_write_tokens=int(cache_write_tokens),
        )
        now = _now()
        minute_k, day_k, month_k = _windows(now)

        def _add(window: str, attr: str, value, ttl: int) -> None:
            table.update_item(
                Key={"pk": customer_id, "window": window},
                UpdateExpression="ADD #v :inc SET #ttl = :ttl",
                ExpressionAttributeNames={"#v": attr, "#ttl": "ttl"},
                ExpressionAttributeValues={":inc": value, ":ttl": int(now) + ttl},
            )

        _add(minute_k, "tokens", total, self._MIN_TTL)
        _add(day_k, "tokens", total, self._DAY_TTL)
        _add(month_k, "cost", Decimal(str(round(cost, 6))), self._MON_TTL)


def make_usage_meter() -> UsageMeter:
    """Pick the backend from ``STORAGE_BACKEND``: ``sqlite`` -> in-memory, else DynamoDB."""
    backend = os.environ.get("STORAGE_BACKEND", "dynamodb").lower()
    if backend == "sqlite":
        return InMemoryUsageMeter()
    return DynamoDBUsageMeter()
