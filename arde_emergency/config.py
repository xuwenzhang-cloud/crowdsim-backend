"""Configuration for the stateful ARDE controller."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping


@dataclass
class RewardWeights:
    diversity: float = 0.03
    rarity: float = 0.02
    efficiency: float = 0.45
    risk: float = 0.85
    polarization: float = 0.05
    action_cost: float = 0.08
    spillover: float = 0.60


@dataclass
class ArdeConfig:
    version: str = "arde_dynamic_v1"
    learning_rate: float = 0.20
    discount_factor: float = 0.90
    initial_exploration: float = 0.12
    min_exploration: float = 0.02
    max_exploration: float = 0.40
    entropy_target_low: float = 0.55
    entropy_target_high: float = 0.85
    gini_limit: float = 0.45
    risk_high: float = 0.70
    outer_step: float = 0.03
    decision_cooldown_seconds: float = 30.0
    action_validity_seconds: float = 120.0
    llm_enabled: bool = False
    llm_timeout_seconds: float = 8.0
    random_seed: int = 20261007
    ablation_mode: str = "full"
    reward: RewardWeights = field(default_factory=RewardWeights)

    def __post_init__(self) -> None:
        if not 0 < self.learning_rate <= 1:
            raise ValueError("learning_rate must be in (0, 1]")
        if not 0 <= self.discount_factor <= 1:
            raise ValueError("discount_factor must be in [0, 1]")
        if not 0 <= self.min_exploration <= self.initial_exploration <= self.max_exploration <= 1:
            raise ValueError("exploration bounds are inconsistent")
        if not 0 <= self.entropy_target_low <= self.entropy_target_high <= 1:
            raise ValueError("entropy targets must be in [0, 1]")
        if self.decision_cooldown_seconds < 0:
            raise ValueError("decision_cooldown_seconds must be non-negative")
        if self.ablation_mode not in {"full", "no_outer", "no_inner", "no_diversity", "no_llm"}:
            raise ValueError("unknown ARDE ablation_mode")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "ArdeConfig":
        raw = dict(value or {})
        reward = raw.pop("reward", None)
        if isinstance(reward, Mapping):
            raw["reward"] = RewardWeights(**dict(reward))
        return cls(**raw)
