"""Serializable ARDE controller state."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping


@dataclass
class OuterParameters:
    exploration_rate: float = 0.12
    diversity_weight: float = 0.20
    polarization_penalty: float = 0.20
    coordination_strength: float = 0.50
    action_budget: float = 1.00


@dataclass
class ArdeState:
    episode_id: str = ""
    decision_step: int = 0
    outer: OuterParameters = field(default_factory=OuterParameters)
    q_values: dict[str, dict[str, float]] = field(default_factory=dict)
    strategy_counts: dict[str, int] = field(default_factory=dict)
    assignments: dict[str, str] = field(default_factory=dict)
    last_observation: dict[str, Any] | None = None
    last_decision: dict[str, Any] | None = None
    pending_choices: list[dict[str, Any]] = field(default_factory=list)
    pending_decisions: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    effect_history: list[dict[str, Any]] = field(default_factory=list)
    ack_history: list[dict[str, Any]] = field(default_factory=list)
    last_decision_time: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return deepcopy(asdict(self))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ArdeState":
        raw = deepcopy(dict(value))
        outer_raw = raw.pop("outer", {})
        raw["outer"] = OuterParameters(**dict(outer_raw))
        return cls(**raw)
