"""Targeted proof for the continuous-improvement service and API wiring."""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from types import SimpleNamespace

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.schemas.improvement import ImprovementCompareIn  # noqa: E402
from app.services.improvement import _category_deltas  # noqa: E402


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    payload = ImprovementCompareIn(
        previous_scan_id="00000000-0000-0000-0000-000000000001",
        current_scan_id="00000000-0000-0000-0000-000000000002",
    )
    check(payload.previous_scan_id != payload.current_scan_id, "scan ids parsed")
    old = SimpleNamespace(category="metadata", normalized_score=70.0)
    new = SimpleNamespace(category="metadata", normalized_score=82.5)
    deltas = _category_deltas(
        SimpleNamespace(category_scores=[old]),
        SimpleNamespace(category_scores=[new]),
    )
    check(deltas["metadata"]["delta"] == 12.5, "category delta is deterministic")
    router_source = (BACKEND / "app" / "api" / "router.py").read_text()
    check("from app.api.routes import (" in router_source, "router imports routes")
    check("improvements," in router_source, "improvement router is wired")
    route_source = (BACKEND / "app" / "api" / "routes" / "improvements.py").read_text()
    check("prefix=" in route_source and "/improvements" in route_source, "routes exist")
    source = (BACKEND / "app" / "services" / "improvement.py").read_text()
    tree = ast.parse(source)
    imported = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    check(
        not any(name.startswith("app.models.analytics") for name in imported),
        "improvement service does not touch analytics",
    )
    print("Improvement verification passed (5 checks).")


if __name__ == "__main__":
    main()
