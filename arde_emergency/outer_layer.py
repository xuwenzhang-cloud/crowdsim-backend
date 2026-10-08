"""Bounded outer-layer adaptation."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Mapping

from .config import ArdeConfig
from .state import ArdeState


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def adapt_outer_layer(
    state: ArdeState,
    *,
    entropy: float | None,
    gini: float | None,
    risk: float,
    config: ArdeConfig,
    llm_adjustments: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    before = asdict(state.outer)
    step = config.outer_step
    if entropy is not None and entropy < config.entropy_target_low:
        state.outer.diversity_weight += step
        state.outer.exploration_rate += step
    elif entropy is not None and entropy > config.entropy_target_high:
        state.outer.exploration_rate -= step
    if gini is not None and gini > config.gini_limit:
        state.outer.polarization_penalty += step
        state.outer.coordination_strength += step
    if risk >= config.risk_high:
        state.outer.exploration_rate -= 2 * step
        state.outer.coordination_strength += step
        state.outer.action_budget = max(state.outer.action_budget, 1.0)
    adjustments = dict(llm_adjustments or {})
    for key, limit in {
        "exploration_rate": (config.min_exploration, config.max_exploration),
        "diversity_weight": (0.0, 1.5),
        "polarization_penalty": (0.0, 1.5),
        "coordination_strength": (0.0, 1.0),
        "action_budget": (0.1, 2.0),
    }.items():
        current = float(getattr(state.outer, key))
        proposal = adjustments.get(key)
        if isinstance(proposal, (int, float)) and not isinstance(proposal, bool):
            proposal = _clamp(float(proposal), current - step, current + step)
            current = proposal
        setattr(state.outer, key, _clamp(current, *limit))
    return {"before": before, "after": asdict(state.outer), "risk": risk, "entropy": entropy, "gini": gini}
