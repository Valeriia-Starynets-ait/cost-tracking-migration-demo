"""Per-agent cost allocation tag builder.

Every AWS resource created per agent gets the same tag set so operators can
slice the Cost and Usage Report (CUR) by customer for FinOps / chargeback. The
keys here must match the user-defined cost allocation tags an account admin
activates in the Billing console.

``owner`` and ``cost-center`` are inputs: pass them to
:func:`build_per_agent_tags`, or set ``COST_TRACKING_OWNER`` and
``COST_TRACKING_COST_CENTER``. When neither is given they default to
:data:`DEFAULT_OWNER` and :data:`DEFAULT_COST_CENTER`, so unallocated spend is
visible in the report.
"""

from __future__ import annotations

import os

#: Env var that supplies the ``owner`` tag value.
OWNER_ENV = "COST_TRACKING_OWNER"

#: Env var that supplies the ``cost-center`` tag value.
COST_CENTER_ENV = "COST_TRACKING_COST_CENTER"

#: ``owner`` tag value when no input is given.
DEFAULT_OWNER = "unassigned"

#: ``cost-center`` tag value when no input is given.
DEFAULT_COST_CENTER = "unassigned"


def build_per_agent_tags(
    customer_id: str,
    agent_id: str,
    *,
    component: str,
    backend: str = "shared",
    image_channel: str | None = None,
    isolation_tier: str = "regulated",
    owner: str | None = None,
    cost_center: str | None = None,
) -> dict[str, str]:
    """Return the canonical per-agent tag set as a plain dict.

    Keys are snake_case to match the cost-allocation tag keys already activated
    in the Billing console, except ``cost-center``, which keeps its hyphenated
    key. ``component`` is the resource type (runtime, harness, role, memory,
    gateway, gateway_target, endpoint, inference_profile, identity);
    ``backend`` is the agentic backend (for example agentcore or claude_sdk) or
    "shared" for cross-backend resources; ``image_channel`` (stable|edge) is set
    only for container runtimes.

    ``owner`` and ``cost_center`` resolve as: argument, then the
    ``COST_TRACKING_OWNER`` / ``COST_TRACKING_COST_CENTER`` env var, then the
    module default.
    """
    tags = {
        "customer_id": customer_id,
        "agent_id": agent_id,
        "isolation_tier": isolation_tier,
        # Fixed marker for resources this tooling manages. If you scope IAM
        # permissions on aws:ResourceTag/managed_by, the policy must use this
        # exact value.
        "managed_by": "managed-agents",
        "component": component,
        "backend": backend,
        "owner": owner or os.environ.get(OWNER_ENV) or DEFAULT_OWNER,
        "cost-center": (
            cost_center or os.environ.get(COST_CENTER_ENV) or DEFAULT_COST_CENTER
        ),
    }
    if image_channel:
        tags["image_channel"] = image_channel
    return tags
