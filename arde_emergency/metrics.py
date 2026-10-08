"""Metrics used by ARDE's outer and inner layers."""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping

from .contracts import finite_number


def normalized_entropy(distribution: Mapping[str, Any] | Iterable[float]) -> float | None:
    values = list(distribution.values()) if isinstance(distribution, Mapping) else list(distribution)
    clean = [max(0.0, float(value)) for value in values if finite_number(value) is not None]
    total = sum(clean)
    if total <= 0 or len(clean) < 2:
        return None
    probabilities = [value / total for value in clean if value > 0]
    entropy = -sum(value * math.log(value) for value in probabilities)
    maximum = math.log(len(clean))
    return max(0.0, min(1.0, entropy / maximum)) if maximum > 0 else None


def gini(values: Mapping[str, Any] | Iterable[float]) -> float | None:
    raw = list(values.values()) if isinstance(values, Mapping) else list(values)
    clean = sorted(max(0.0, float(value)) for value in raw if finite_number(value) is not None)
    if not clean or sum(clean) <= 0:
        return None
    n = len(clean)
    weighted = sum((index + 1) * value for index, value in enumerate(clean))
    result = (2 * weighted) / (n * sum(clean)) - (n + 1) / n
    return max(0.0, min(1.0, result))


def risk_from_observation(observation: Mapping[str, Any]) -> float:
    global_metrics = dict(observation.get("global") or {})
    explicit = finite_number(global_metrics.get("riskIndex"))
    if explicit is not None:
        return max(0.0, min(1.0, explicit))
    density = max(0.0, finite_number(global_metrics.get("maxDensity"), default=0.0) or 0.0)
    speed = max(0.0, finite_number(global_metrics.get("meanSpeed"), default=1.4) or 0.0)
    congestion = max(0.0, finite_number(global_metrics.get("congestion"), default=0.0) or 0.0)
    return max(0.0, min(1.0, 0.45 * density / 6.0 + 0.30 * (1 - min(speed / 1.4, 1)) + 0.25 * congestion))


def efficiency_from_observation(observation: Mapping[str, Any]) -> float:
    values = dict(observation.get("global") or {})
    explicit = finite_number(values.get("efficiency"))
    if explicit is not None:
        return max(0.0, min(1.0, explicit))
    completion = finite_number(values.get("completionRate"), default=0.0) or 0.0
    speed = finite_number(values.get("meanSpeed"), default=0.0) or 0.0
    return max(0.0, min(1.0, 0.65 * completion + 0.35 * min(speed / 1.4, 1.0)))


def strategy_statistics(counts: Mapping[str, int]) -> dict[str, Any]:
    total = sum(max(0, int(value)) for value in counts.values())
    distribution = {key: (max(0, int(value)) / total if total else 0.0) for key, value in counts.items()}
    return {
        "distribution": distribution,
        "entropy": normalized_entropy(counts),
        "gini": gini(counts),
    }
