"""Shared test configuration.

The migrated tests price models by their ``eu.`` inference-profile ids, as they
did in the source repository. The package default prefix is ``us``, so the
suite pins the prefix input to ``eu`` and clears every other cost-tracking input
so a developer's shell cannot change the results.
"""

import pytest


@pytest.fixture(autouse=True)
def _cost_tracking_inputs(monkeypatch):
    monkeypatch.setenv("COST_TRACKING_INFERENCE_PROFILE_PREFIX", "eu")
    for name in (
        "BEDROCK_TOKEN_PRICES",
        "COST_TRACKING_REGION",
        "AWS_REGION",
        "COST_TRACKING_OWNER",
        "COST_TRACKING_COST_CENTER",
    ):
        monkeypatch.delenv(name, raising=False)
