"""Safe deterministic fallback decisions."""

from __future__ import annotations

from typing import Any, Mapping

from .action_mapper import map_strategy
from .strategy import Strategy


def fallback_strategy(region: Mapping[str, Any], risk: float) -> str:
    if risk >= 0.8:
        return Strategy.FLOW_REGULATE.value
    if risk >= 0.55:
        return Strategy.ROUTE_REDISTRIBUTE.value
    if risk >= 0.30:
        return Strategy.INFORM_GUIDE.value
    return Strategy.OBSERVE.value


def fallback_actions(regions: list[Mapping[str, Any]], risk: float, validity_seconds: float) -> list[dict[str, Any]]:
    return [map_strategy(fallback_strategy(region, risk), region, risk=risk, validity_seconds=validity_seconds) for region in regions]
