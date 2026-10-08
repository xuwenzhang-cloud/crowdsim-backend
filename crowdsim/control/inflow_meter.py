"""Deterministic token-bucket admission control for pedestrian entry gates."""

from __future__ import annotations

from dataclasses import dataclass, field
import math


@dataclass
class InflowMeter:
    action_id: str
    entry_id: str
    rate: float
    nominal_capacity_per_second: float
    last_time: float
    tokens: float = 0.0
    admitted: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        if not 0.0 <= self.rate <= 1.0:
            raise ValueError("inflow rate must be in [0, 1]")
        if not math.isfinite(self.nominal_capacity_per_second) or self.nominal_capacity_per_second <= 0:
            raise ValueError("nominal gate capacity must be positive and finite")
        self.tokens = max(self.tokens, 1.0 if self.rate > 0 else 0.0)

    def regulate(self, candidates: set[str], now: float) -> tuple[set[str], set[str]]:
        candidates = set(candidates)
        self.admitted.intersection_update(candidates)
        if self.rate >= 1.0:
            self.admitted.update(candidates)
            self.last_time = float(now)
            return set(), set(candidates)
        elapsed = max(0.0, float(now) - self.last_time)
        self.last_time = float(now)
        self.tokens = min(
            self.nominal_capacity_per_second * 5.0,
            self.tokens + elapsed * self.nominal_capacity_per_second * self.rate,
        )
        waiting = sorted(candidates - self.admitted)
        count = min(len(waiting), int(self.tokens))
        released = set(waiting[:count])
        self.tokens -= count
        self.admitted.update(released)
        return candidates - self.admitted, released
