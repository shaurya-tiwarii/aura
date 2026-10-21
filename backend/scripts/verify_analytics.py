"""Focused proof for analytics normalisation (run from ``backend/``)."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.models.enums import MetricKind, MetricSource, Platform  # noqa: E402
from app.schemas.analytics import AnalyticsMetricIn  # noqa: E402
from app.services.analytics import _normalise_fields  # noqa: E402


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    linkedin = _normalise_fields(
        Platform.LINKEDIN.value,
        {"impressionCount": 12, "clickCount": 3},
    )
    check(linkedin["impressions"] == (12.0, "impressionCount"), "LinkedIn mapping")
    check(linkedin["clicks"] == (3.0, "clickCount"), "source field retained")

    reddit = _normalise_fields(Platform.REDDIT.value, {"score": 5, "num_comments": 2})
    check(reddit["likes"][0] == 5.0, "Reddit score mapping")
    check(reddit["comments"][0] == 2.0, "Reddit comment mapping")

    payload = AnalyticsMetricIn(
        post_id="00000000-0000-0000-0000-000000000001",
        platform=Platform.WORDPRESS,
        measured_on=date(2026, 1, 1),
        source=MetricSource.DEMO,
        metrics={MetricKind.REACH: 10},
    )
    check(payload.source == MetricSource.DEMO, "source is mandatory and preserved")
    check(MetricSource.DEMO.value == "demo", "demo source label")
    try:
        AnalyticsMetricIn(
            post_id=payload.post_id,
            platform=payload.platform,
            measured_on=payload.measured_on,
            source=MetricSource.USER_ENTERED,
            metrics={MetricKind.REACH: -1},
        )
    except ValueError:
        pass
    else:
        raise AssertionError("negative metrics must be rejected")
    print("analytics verification passed (7 checks)")


if __name__ == "__main__":
    main()
