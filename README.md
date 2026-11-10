# cost-tracking-migration-demo

Cost tracking for agents on Amazon Bedrock, migrated from
[automat-it/ait-managed-agents](https://github.com/automat-it/ait-managed-agents)
as the first Foundation feature (section 3.1, "Cost tracking and cost tagging",
of that repository's `docs/AGENT-FOUNDATION-MIGRATION.md`).

## What the module does

The `cost_tracking` package (in `src/cost_tracking/`) has three parts:

| Module | What it does |
| --- | --- |
| `cost_tracking.pricing` | Estimates tokens (about 4 characters per token) and converts model, input, output and prompt-cache token counts into USD from a per-model price table. Unknown models are priced at the most expensive rate in the table, so a budget gate never under-counts. |
| `cost_tracking.usage_meter` | A per-tenant usage meter and pre-invoke budget gate. `UsageMeter.check` compares accumulated tokens per minute, tokens per day and monthly USD with a tenant's limits (`tpm`, `max_tokens_per_day`, `max_budget_usd`). `UsageMeter.record` adds a turn after it runs. Backends: `InMemoryUsageMeter` (local and tests) and `DynamoDBUsageMeter` (atomic counters with a TTL). `BudgetExceededError` is the exception for the caller to raise when `check` blocks. |
| `cost_tracking.tags` | `build_per_agent_tags` returns the cost allocation tag set for every resource created for an agent. |

```python
from cost_tracking import pricing
from cost_tracking.tags import build_per_agent_tags
from cost_tracking.usage_meter import make_usage_meter

meter = make_usage_meter()
gate = meter.check("acme", {"tpm": 50_000, "max_budget_usd": 100})
if not gate.allowed:
    ...  # refuse the call, retry after gate.retry_after_seconds

meter.record("acme", model="us.anthropic.claude-sonnet-5", input_tokens=900, output_tokens=120)
tags = build_per_agent_tags("acme", "agent-1", component="runtime", owner="platform", cost_center="cc-1234")
```

## Inputs

Every input has a default, so the module runs with no configuration. For each
one, an explicit argument wins over the environment variable, and the
environment variable wins over the default.

| Input | Environment variable | Argument | Default | Used by |
| --- | --- | --- | --- | --- |
| Region of the meter table | `COST_TRACKING_REGION`, then `AWS_REGION` | `DynamoDBUsageMeter(region_name=...)`, `resolve_region(region_name)` | `us-east-1` | `usage_meter` |
| Inference profile prefix (`us`, `eu`, `apac`, ...) | `COST_TRACKING_INFERENCE_PROFILE_PREFIX` | `pricing.default_prices(prefix)` | `us` | `pricing` (price table keys such as `us.anthropic.claude-sonnet-5`, and `base_model_id`) |
| `owner` tag value | `COST_TRACKING_OWNER` | `build_per_agent_tags(..., owner=...)` | `unassigned` | `tags` |
| `cost-center` tag value | `COST_TRACKING_COST_CENTER` | `build_per_agent_tags(..., cost_center=...)` | `unassigned` | `tags` |

Other settings that came from the source and are unchanged:

| Environment variable | Default | Effect |
| --- | --- | --- |
| `BEDROCK_TOKEN_PRICES` | built-in table | JSON `{"<model id>": [input_per_1k_usd, output_per_1k_usd]}`. Replaces the built-in price table, so keys must carry the full profile id. Invalid JSON falls back to the built-in table. |
| `USAGE_METER_TABLE` | unset | DynamoDB table with partition key `pk` and sort key `window`. When unset, `DynamoDBUsageMeter` is inactive: `active()` is false, `record` does nothing and the gate allows every call (fail-open). The table should enable TTL on the `ttl` attribute. |
| `STORAGE_BACKEND` | `dynamodb` | `make_usage_meter()` returns `InMemoryUsageMeter` for `sqlite` and `DynamoDBUsageMeter` otherwise. |

The `owner` and `cost-center` tags default to `unassigned` so that spend with
no owner is visible in the Cost and Usage Report. To use the tags for chargeback,
activate the keys `customer_id`, `agent_id`, `owner` and `cost-center` as
cost allocation tags in the Billing console.

## Run the tests

Python 3.12 or later and [uv](https://docs.astral.sh/uv/) are required.

```bash
make check    # ruff, then pytest
make lint     # ruff only
make test     # pytest only
```

Without `make`:

```bash
uv run --with ruff ruff check .
uv run --with pytest python -m pytest
```

`tests/conftest.py` sets `COST_TRACKING_INFERENCE_PROFILE_PREFIX=eu` for every
test, because the migrated tests price `eu.*` model ids, and it clears the other
inputs so the shell environment cannot change a result.

## Migration notes

What changed from the source and why.

Source files, relative to the source repository root, and where they went:

| Source | Target |
| --- | --- |
| `backend/control_plane/lib/pricing.py` | `src/cost_tracking/pricing.py` |
| `backend/control_plane/lib/usage_meter.py` | `src/cost_tracking/usage_meter.py` |
| `backend/control_plane/lib/tags.py` | `src/cost_tracking/tags.py` |
| `backend/control_plane/tests/test_pricing.py` | `tests/test_pricing.py` |
| `backend/control_plane/tests/test_usage_meter.py` | `tests/test_usage_meter.py` |
| `backend/control_plane/tests/test_tags.py` | `tests/test_tags.py` |

Changes:

1. **Imports.** `from control_plane.lib import pricing` is now `from cost_tracking import pricing`, in the code and the tests. The same applies to `usage_meter` and `tags`.
2. **Region is an input.** The source read `AWS_REGION` with a default of `eu-central-1`. The default is now `us-east-1`, and `COST_TRACKING_REGION` and a `region_name` argument can set it. This follows trap 5 in the source guide ("The code assumes the EU").
3. **Inference profile prefix is an input.** The source price table was keyed by `eu.*` ids only. The table now holds foundation-model ids, and `default_prices(prefix)` adds the prefix. The default is `us`. `base_model_id` strips `eu.`, `us.`, `apac.` and also the configured prefix. The price numbers are unchanged.
4. **`owner` and `cost-center` are inputs.** `tags.py` had no such tags. In the source they are fixed to Automat-it values in Terraform provider files (`infra/terraform/foundation/providers.tf` and others), which are not migrated. `build_per_agent_tags` now adds both tags, from arguments or environment variables, with the default `unassigned`. The key `cost-center` keeps its hyphen, to match the Terraform tag key.
5. **Comments that pointed at files outside the migration were removed or reworded** (`NEXT_STEPS.md`, `ADR-0011`, `usage_plans.py`, `make_tenant_registry`, `handlers/models.py`, `control_plane_stack.py`). The docstring of `build_per_agent_tags` no longer lists `hermes` as a backend (the source guide marks Hermes as retired), and the comment on the price table no longer claims `eu-central-1` prices.
6. **`datetime.timezone.utc` is now `datetime.UTC`** because the lint step in `make check` requires it (Python 3.12 or later).
7. **Tests.** Every migrated test that exercises the module still runs unchanged except for its imports. I added tests for the new inputs, and `tests/conftest.py` (see above).
8. **Tests not migrated.** Five tests in the source `test_usage_meter.py` call the invoke path (`control_plane.saga.invoke`, `control_plane.handlers.api` and `ddb`), which is outside the six migrated files and was not copied. They are `test_unary_invoke_blocks_when_budget_reached`, `test_unary_invoke_allows_and_records_when_under_cap`, `test_gate_is_noop_when_meter_inactive`, `test_invoke_route_maps_budget_error_to_http_429` and `test_stream_invoke_emits_429_error_frame_when_capped`, with their helpers `_agent_row` and `_wire_invoke`. The budget logic they cover (`check`, `record`, the caps and the fail-open behavior) is still tested through the unit tests of the meter. Bring these five tests back when the invoke path is migrated.

Left unchanged on purpose:

- The tag `managed_by` is still the fixed value `managed-agents`. In the source it had to match an IAM condition in a stack that is not migrated.
- The price numbers and the cache multipliers are copied as they were. They are representative list prices from the source, not verified here. Check them against the current AWS price list, or set `BEDROCK_TOKEN_PRICES`.

Not migrated (outside the six files, listed in the pull request): `scripts/governance/backfill_resource_tags.py`, `docs/COST-MODEL.md`, the Terraform tag values in `infra/terraform/foundation/providers.tf`, `infra/terraform/platform/providers.tf` and `infra/aws/app.py`, the invoke path that calls the budget gate, and AWS Budgets and Cost Categories (the source guide says these do not exist in the source and must be added).
