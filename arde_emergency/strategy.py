"""ARDE strategy vocabulary and state discretisation."""

from __future__ import annotations

from enum import Enum
from typing import Any, Mapping

from .contracts import finite_number


class Strategy(str, Enum):
    OBSERVE = "observe"
    INFORM_GUIDE = "inform_guide"
    FLOW_REGULATE = "flow_regulate"
    ROUTE_REDISTRIBUTE = "route_redistribute"


ALL_STRATEGIES = tuple(item.value for item in Strategy)


def level(value: float, low: float, high: float) -> str:
    if value < low:
        return "low"
    if value < high:
        return "medium"
    return "high"


def region_state_key(region: Mapping[str, Any], observation: Mapping[str, Any]) -> str:
    risk = finite_number(region.get("riskIndex"), default=0.0) or 0.0
    density = finite_number(region.get("density"), default=0.0) or 0.0
    speed = finite_number(region.get("speed"), default=1.4) or 0.0
    trend = finite_number(region.get("riskTrend"), default=0.0) or 0.0
    spillover = finite_number(region.get("spilloverRisk"), default=0.0) or 0.0
    return "|".join((
        level(risk, 0.35, 0.70),
        level(density, 1.5, 3.5),
        level(max(0.0, 1.4 - speed), 0.3, 0.8),
        "rising" if trend > 0.03 else "falling" if trend < -0.03 else "stable",
        level(spillover, 0.25, 0.60),
    ))
