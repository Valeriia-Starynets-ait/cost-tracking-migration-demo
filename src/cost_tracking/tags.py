"""Per-agent cost allocation tag builder.

Every AWS resource created per agent gets the same tag set so operators can
slice the Cost and Usage Report (CUR) by customer for FinOps / chargeback. The
keys here must match the user-defined cost allocation tags an account admin
activates in the Billing console.

The ``owner`` and ``cost-center`` tag values are inputs. Pass ``owner=`` /
``cost_center=`` or set ``COST_TRACKING_OWNER`` / ``COST_TRACKING_COST_CENTER``.
When neither is given the value is ``unassigned``, so untagged spend is easy to
find in the CUR.
"""

from __future__ import annotations

import os

#: Env vars that supply the ``owner`` and ``cost-center`` tag values.
OWNER_ENV = "COST_TRACKING_OWNER"
COST_CENTER_ENV = "COST_TRACKING_COST_CENTER"

#: Tag value used when no owner / cost center is provided.
DEFAULT_TAG_VALUE = "unassigned"


def _resolve(explicit: str | None, env_name: str) -> str:
    """Pick the explicit value, else the env var, else :data:`DEFAULT_TAG_VALUE`."""
    return explicit or os.environ.get(env_name) or DEFAULT_TAG_VALUE


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
    in the Billing console, except ``owner`` and ``cost-center`` which keep the
    key spelling used by the infrastructure-level tags. ``component`` is the
    resource type (runtime, harness, role, memory, gateway, gateway_target,
    endpoint, inference_profile, identity); ``backend`` is the agentic backend
    (hermes, claude_sdk, agentcore) or "shared" for cross-backend resources;
    ``image_channel`` (stable|edge) is set only for container runtimes.

    ``owner`` and ``cost_center`` are the values of the ``owner`` and
    ``cost-center`` tags. They fall back to ``COST_TRACKING_OWNER`` and
    ``COST_TRACKING_COST_CENTER``, then to ``unassigned``.
    """
    tags = {
        "customer_id": customer_id,
        "agent_id": agent_id,
        "isolation_tier": isolation_tier,
        # MUST equal the value any IAM condition on the provisioning role
        # requires (aws:ResourceTag/RequestTag managed_by == "managed-agents").
        # A mismatch makes every TAGGED resource fail to create.
        "managed_by": "managed-agents",
        "component": component,
        "backend": backend,
        "owner": _resolve(owner, OWNER_ENV),
        "cost-center": _resolve(cost_center, COST_CENTER_ENV),
    }
    if image_channel:
        tags["image_channel"] = image_channel
    return tags
