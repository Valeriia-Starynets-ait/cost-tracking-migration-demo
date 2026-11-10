# cost-tracking-migration-demo

Cost tracking for agent workloads, migrated from the Foundation scope of
`automat-it/ait-managed-agents` (guide section 3.1, "Cost tracking and cost
tagging").

## What the module does

The package `cost_tracking` (in `src/cost_tracking/`) has three parts.

| Module | What it does |
| --- | --- |
| `cost_tracking.pricing` | Token pricing and estimation. `cost_usd()` turns (model, input tokens, output tokens, cache read tokens, cache write tokens) into dollars from a per-model price table. `quote_model()` and `cost_for_consumption()` return a `Decimal` figure with its provenance (`known` or `estimated_fallback`). `estimate_tokens()` is a char/4 estimate for when the runtime reports no usage. An unpriced model is billed at the most expensive rate in the table, so the budget gate never under-counts. |
| `cost_tracking.usage_meter` | A per-tenant usage meter and a pre-invoke budget gate. `UsageMeter.check()` compares the usage so far in the current minute, UTC day and calendar month with the caps `tpm`, `max_tokens_per_day` and `max_budget_usd` (`0` or absent means unlimited). `UsageMeter.record()` adds a turn after the call. Backends: `InMemoryUsageMeter` (local and tests) and `DynamoDBUsageMeter` (atomic counters with a TTL on the table named by `USAGE_METER_TABLE`; with no table it allows everything). `make_usage_meter()` picks the backend from `STORAGE_BACKEND` (`sqlite` gives the in-memory meter). The caller raises `BudgetExceededError` when `check()` returns `allowed=False`. |
| `cost_tracking.tags` | `build_per_agent_tags()` returns the cost allocation tag set for a resource of one agent: `customer_id`, `agent_id`, `isolation_tier`, `managed_by`, `component`, `backend`, `owner`, `cost-center` and, for container runtimes, `image_channel`. |

```python
from cost_tracking import pricing
from cost_tracking.tags import build_per_agent_tags
from cost_tracking.usage_meter import make_usage_meter

meter = make_usage_meter(region_name="us-east-1")
gate = meter.check("acme", {"tpm": 50_000, "max_budget_usd": 100})
if not gate.allowed:
    ...  # refuse the call, retry after gate.retry_after_seconds

meter.record("acme", model="eu.anthropic.claude-sonnet-5", input_tokens=900, output_tokens=120)
tags = build_per_agent_tags("acme", "agent-1", component="runtime", owner="platform", cost_center="cc-42")
```

## Inputs

Nothing is tied to one organisation, Region or geography. These are the inputs.
Each is read when it is used, so you can change it without a code change.

| Input | Where | Default | What it does |
| --- | --- | --- | --- |
| AWS Region | `DynamoDBUsageMeter(region_name=...)` or `make_usage_meter(region_name=...)`; otherwise the env var `AWS_REGION` | `eu-central-1` | Region of the DynamoDB table `USAGE_METER_TABLE`. Order: argument, then `AWS_REGION`, then the default. |
| Inference profile prefix | Env var `COST_TRACKING_INFERENCE_PROFILE_PREFIX` | `eu.` | Prefix of the cross-region inference profile ids in the default price table (`eu.anthropic.claude-sonnet-5`, and so on). Use `us.`, `apac.`, `global.`, and so on, to match the profiles your account calls. A missing trailing dot is added. An empty value prices plain foundation-model ids. `base_model_id()` also strips this prefix. |
| `owner` tag value | `build_per_agent_tags(..., owner=...)`; otherwise the env var `COST_TRACKING_OWNER` | `unassigned` | Value of the `owner` tag. Order: argument, then env var, then the default. |
| `cost-center` tag value | `build_per_agent_tags(..., cost_center=...)`; otherwise the env var `COST_TRACKING_COST_CENTER` | `unassigned` | Value of the `cost-center` tag. Same order. |

Other settings, unchanged from the source:

| Input | Default | What it does |
| --- | --- | --- |
| `BEDROCK_TOKEN_PRICES` | unset | JSON `{"model-id": [input_usd_per_1k, output_usd_per_1k]}`. Replaces the default price table, so a price change needs no code change. A value that does not parse is ignored. |
| `USAGE_METER_TABLE` | unset | DynamoDB table (key `pk`, sort key `window`, TTL attribute `ttl`). With no table the DynamoDB meter does not block and does not record. |
| `STORAGE_BACKEND` | `dynamodb` | `sqlite` selects the in-memory meter in `make_usage_meter()`. |

The defaults for Region and prefix are the values the source ran with, so prices
resolve the same as before. For an account in another geography, set both. With
a mismatched prefix a model is not found in the table and is billed at the
fallback rate, which over-counts and is safe for a budget gate but not exact.

## Run the tests

You need Python 3.12 or later and [uv](https://docs.astral.sh/uv/).

```sh
make check      # ruff, then pytest
make lint       # ruff only
make test       # pytest only
```

Without `make`, run the two commands from the `Makefile`:

```sh
uv run --with ruff ruff check .
uv run --with pytest python -m pytest
```

Five tests in `tests/test_usage_meter.py` test the invoke path of the source
control plane (`control_plane.saga.invoke` and `control_plane.handlers.api`).
That code is not part of this module, so pytest reports them as skipped. They are
unchanged, and they run on their own when the `control_plane` package can be
imported.

## Migration notes

Source: `backend/control_plane/lib/{pricing,usage_meter,tags}.py` and
`backend/control_plane/tests/test_{pricing,usage_meter,tags}.py` in
`automat-it/ait-managed-agents`. The behaviour and the tests are copied. The
changes are these.

What changed, and why:

- **Layout and imports.** The files moved to `src/cost_tracking/` and `tests/`.
  `from control_plane.lib import pricing` became `from cost_tracking import pricing`,
  in the module and in the tests. A new `__init__.py` exports the three modules.
- **Inference profile prefix is an input.** The price table was keyed by `eu.*`
  ids only (guide section 5, trap 5), so an account that calls `us.*` profiles
  got the fallback rate for every model. The table is now built from
  `COST_TRACKING_INFERENCE_PROFILE_PREFIX`. The default `eu.` keeps the source
  prices. `base_model_id()` strips the configured prefix in addition to
  `eu.`, `us.` and `apac.`.
- **Region is an input.** `eu-central-1` was fixed in `DynamoDBUsageMeter`. It
  is now a `region_name` argument, then `AWS_REGION`, then a named default
  (`DEFAULT_REGION`). `make_usage_meter()` accepts `region_name`.
- **`owner` and `cost-center` are inputs.** In the source, these two tag values
  are fixed to Automat-it values in Terraform and CDK files (guide section 3.1)
  that are not part of this migration. `tags.py` did not build them. Now
  `build_per_agent_tags()` adds them from the `owner` and `cost_center`
  arguments or from env vars, with the default `unassigned` so that untagged
  spend is easy to find. This is the only new behaviour. The tag key
  `cost-center` has a hyphen because the infrastructure tags in the source use
  that spelling; the older tags stay snake_case.
- **Comments and docstrings.** References to files that do not exist here
  (`usage_plans.py`, `handlers/models.py`, `NEXT_STEPS.md`, ADR-0011,
  `control_plane_stack.py`, `make_tenant_registry`) were removed or reworded.
  The code behind them is unchanged.
- **Lint.** `ruff` now runs on this code: imports are sorted and
  `timezone.utc` is `datetime.UTC` (Python 3.12). No change in behaviour.
- **Tests.** No test was deleted or weakened. Eleven tests were added for the new
  inputs. The five invoke-path tests are skipped when `control_plane` is not
  importable (see "Run the tests").

Kept as in the source, on purpose:

- The `managed_by` tag value `managed-agents`. A consumer may have IAM
  conditions on it, so it is not an input. Check it against your IAM policy.
- The fallback rate for an unpriced model (the most expensive in the table), the
  cache multipliers and the default prices. Prices are representative list
  prices; set `BEDROCK_TOKEN_PRICES` to your own.

Not migrated (outside the six source files):

- The invoke path that calls the meter: `control_plane/saga/invoke.py`, the API
  handler `control_plane/handlers/api.py` and the DynamoDB helpers they use.
- The Terraform and CDK that set tag values and create the meter table.
- `scripts/governance/backfill_resource_tags.py` and `docs/COST-MODEL.md`.
- AWS Budgets and Cost Categories. The guide says they do not exist in the source
  and must be added; they are not part of this change.
