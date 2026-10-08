"""Auditable reward calculation for ARDE."""

from __future__ import annotations

from typing import Any, Mapping

from .config import ArdeConfig
from .contracts import finite_number


def _score(container: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    return finite_number(container.get(key), default=default) or 0.0


def calculate_reward(
    evaluation: Mapping[str, Any],
    *,
    entropy: float | None,
    gini: float | None,
    strategy_rarity: float,
    action_cost: float,
    config: ArdeConfig,
    diversity_weight: float | None = None,
    polarization_penalty: float | None = None,
) -> tuple[float, dict[str, float]]:
    before = dict(evaluation.get("before") or {})
    after = dict(evaluation.get("after") or {})
    delta = dict(evaluation.get("delta") or {})
    efficiency_gain = _score(delta, "efficiency", _score(after, "efficiency") - _score(before, "efficiency"))
    risk_reduction = _score(delta, "riskReduction", _score(before, "riskIndex") - _score(after, "riskIndex"))
    spillover = _score(evaluation.get("spillover") or {}, "riskIncrease", 0.0)
    diversity = 0.0 if entropy is None else entropy
    polarization = 0.0 if gini is None else gini
    weights = config.reward
    objective = _score(evaluation, "objectiveScore", 0.0)
    parts = {
        "base": objective,
        "diversity": (weights.diversity if diversity_weight is None else diversity_weight) * diversity,
        "rarity": weights.rarity * max(0.0, min(1.0, strategy_rarity)),
        "efficiency": weights.efficiency * efficiency_gain,
        "risk": weights.risk * risk_reduction,
        "polarizationPenalty": -(weights.polarization if polarization_penalty is None else polarization_penalty) * polarization,
        "actionCostPenalty": -weights.action_cost * max(0.0, action_cost),
        "spilloverPenalty": -weights.spillover * max(0.0, spillover),
    }
    total = sum(parts.values())
    if objective < 0.0 and total > objective:
        parts["objectiveAlignmentPenalty"] = objective - total
        total = objective
    return total, parts
