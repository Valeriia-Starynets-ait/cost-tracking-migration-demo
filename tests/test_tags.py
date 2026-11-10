from cost_tracking.tags import build_per_agent_tags


def test_runtime_tags_include_backend_and_channel():
    t = build_per_agent_tags(
        "acme", "01JXYZ123456", component="runtime", backend="claude_sdk", image_channel="stable"
    )
    assert t["component"] == "runtime"
    assert t["backend"] == "claude_sdk"
    assert t["image_channel"] == "stable"
    assert t["managed_by"] == "managed-agents"
    assert t["customer_id"] == "acme"
    assert t["agent_id"] == "01JXYZ123456"
    assert t["isolation_tier"] == "regulated"


def test_image_channel_omitted_when_none():
    t = build_per_agent_tags("acme", "01JX", component="memory", backend="shared")
    assert "image_channel" not in t
    assert t["backend"] == "shared"
    assert t["component"] == "memory"


def test_isolation_tier_overridable():
    t = build_per_agent_tags("acme", "01JX", component="role", backend="hermes", isolation_tier="standard")
    assert t["isolation_tier"] == "standard"


def test_component_is_required_keyword():
    import pytest
    with pytest.raises(TypeError):
        build_per_agent_tags("acme", "01JX")  # missing component=


# ---------------------------------------------------------------------------
# Migration: owner and cost-center are inputs
# ---------------------------------------------------------------------------

def test_owner_and_cost_center_default_to_unassigned(monkeypatch):
    monkeypatch.delenv("COST_TRACKING_OWNER", raising=False)
    monkeypatch.delenv("COST_TRACKING_COST_CENTER", raising=False)
    t = build_per_agent_tags("acme", "01JX", component="role")
    assert t["owner"] == "unassigned"
    assert t["cost-center"] == "unassigned"


def test_owner_and_cost_center_come_from_env(monkeypatch):
    monkeypatch.setenv("COST_TRACKING_OWNER", "platform-team")
    monkeypatch.setenv("COST_TRACKING_COST_CENTER", "cc-42")
    t = build_per_agent_tags("acme", "01JX", component="role")
    assert t["owner"] == "platform-team"
    assert t["cost-center"] == "cc-42"


def test_owner_and_cost_center_arguments_beat_env(monkeypatch):
    monkeypatch.setenv("COST_TRACKING_OWNER", "from-env")
    monkeypatch.setenv("COST_TRACKING_COST_CENTER", "from-env")
    t = build_per_agent_tags(
        "acme", "01JX", component="role", owner="data-team", cost_center="cc-7"
    )
    assert t["owner"] == "data-team"
    assert t["cost-center"] == "cc-7"
