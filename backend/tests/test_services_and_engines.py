from __future__ import annotations

# Assertions are the test contract, not production validation.
# ruff: noqa: S101
from types import SimpleNamespace

import pytest

from app.schemas.analytics import AnalyticsMetricIn
from app.schemas.improvement import ImprovementCompareIn
from app.services.analytics import _normalise_fields
from app.services.improvement import _category_deltas


def test_analytics_normalisation_and_negative_values():
    mapped = _normalise_fields("linkedin", {"impressionCount": 12, "clickCount": 3})
    assert mapped["impressions"] == (12.0, "impressionCount")
    assert mapped["clicks"] == (3.0, "clickCount")
    with pytest.raises(ValueError):
        AnalyticsMetricIn(
            post_id="00000000-0000-0000-0000-000000000001",
            platform="wordpress",
            measured_on="2026-01-01",
            source="demo",
            metrics={"reach": -1},
        )


def test_improvement_deltas_are_deterministic_and_ids_are_validated():
    old = SimpleNamespace(category="metadata", normalized_score=70.0)
    new = SimpleNamespace(category="metadata", normalized_score=82.5)
    assert (
        _category_deltas(
            SimpleNamespace(category_scores=[old]),
            SimpleNamespace(category_scores=[new]),
        )["metadata"]["delta"]
        == 12.5
    )
    payload = ImprovementCompareIn(
        previous_scan_id="00000000-0000-0000-0000-000000000001",
        current_scan_id="00000000-0000-0000-0000-000000000002",
    )
    assert payload.previous_scan_id != payload.current_scan_id
