"""ARDE policy presets that match OverlayNetworkSimulator.policy fields.

Existing backend presets (police_guidance / temporary_diversion / observe_only)
remain unchanged. This registry only adds ARDE emergency decisions and a
payload overlay so aggregatedPolicy can actually change simulation parameters.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


BUILTIN_PRESETS: Dict[str, Dict[str, float]] = {
    "police_guidance": {
        "strength": 0.9,
        "capacity_multiplier": 2.05,
        "speed_recovery": 0.48,
        "water_decay_per_second": 0.030,
        "diversion": 0.58,
        "visual_relief": 0.72,
    },
    "temporary_diversion": {
        "strength": 0.82,
        "capacity_multiplier": 1.65,
        "speed_recovery": 0.34,
        "water_decay_per_second": 0.015,
        "diversion": 0.86,
        "visual_relief": 0.58,
    },
    "observe_only": {
        "strength": 0.18,
        "capacity_multiplier": 0.96,
        "speed_recovery": 0.0,
        "water_decay_per_second": 0.0,
        "diversion": 0.04,
        "visual_relief": 0.0,
    },
}

ARDE_PRESETS: Dict[str, Dict[str, float]] = {
    "arde_warning": {
        "strength": 0.42,
        "capacity_multiplier": 1.18,
        "speed_recovery": 0.16,
        "water_decay_per_second": 0.008,
        "diversion": 0.22,
        "visual_relief": 0.28,
    },
    "arde_crowd_response": {
        "strength": 0.88,
        "capacity_multiplier": 2.12,
        "speed_recovery": 0.50,
        "water_decay_per_second": 0.012,
        "diversion": 0.64,
        "visual_relief": 0.76,
    },
    "arde_flood_response": {
        "strength": 0.86,
        "capacity_multiplier": 1.88,
        "speed_recovery": 0.36,
        "water_decay_per_second": 0.038,
        "diversion": 0.72,
        "visual_relief": 0.62,
    },
    "arde_combined_response": {
        "strength": 0.92,
        "capacity_multiplier": 2.18,
        "speed_recovery": 0.52,
        "water_decay_per_second": 0.034,
        "diversion": 0.78,
        "visual_relief": 0.80,
    },
    "arde_recovery": {
        "strength": 0.46,
        "capacity_multiplier": 1.28,
        "speed_recovery": 0.22,
        "water_decay_per_second": 0.018,
        "diversion": 0.30,
        "visual_relief": 0.34,
    },
}

POLICY_NUMERIC_KEYS = (
    "strength",
    "capacity_multiplier",
    "speed_recovery",
    "water_decay_per_second",
    "diversion",
    "visual_relief",
)


def _number(value: Any, default: float) -> float:
    try:
        parsed = float(value)
        return parsed if parsed == parsed else default
    except (TypeError, ValueError):
        return default


def resolve_arde_policy(
    decision: Any,
    payload: Optional[Dict[str, Any]] = None,
    step_index: int = 0,
) -> Optional[Dict[str, Any]]:
    """Return a policy dict if *decision* is an ARDE preset, else None."""
    name = str(decision or "").strip()
    preset = ARDE_PRESETS.get(name)
    if preset is None:
        return None
    policy = {
        "name": name,
        "started_step": step_index,
        **preset,
    }
    apply_payload_overrides(policy, payload or {})
    return policy


def apply_payload_overrides(policy: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
    """Write frontend / ARDE payload numbers onto an existing policy dict."""
    if not isinstance(payload, dict) or not isinstance(policy, dict):
        return policy

    raw_strength = payload.get("containmentLevel", payload.get("strength"))
    if raw_strength is not None:
        strength = _number(raw_strength, 70.0)
        if strength > 1.0:
            strength = strength / 100.0
        policy["strength"] = max(0.05, min(1.0, strength))

    for key in POLICY_NUMERIC_KEYS:
        if key == "strength":
            continue
        if key not in payload or payload.get(key) is None:
            continue
        value = _number(payload.get(key), float("nan"))
        if value != value:
            continue
        if key in {"diversion", "visual_relief", "speed_recovery"}:
            policy[key] = max(0.0, min(1.0, value))
        elif key == "capacity_multiplier":
            policy[key] = max(0.5, min(3.5, value))
        elif key == "water_decay_per_second":
            policy[key] = max(0.0, min(0.08, value))
    return policy


def event_control_factor(decision_name: Any) -> float:
    """How strongly a policy suppresses the active event (lower = stronger control)."""
    name = str(decision_name or "")
    if name in {"police_guidance", "arde_crowd_response", "arde_flood_response", "arde_combined_response"}:
        return 0.35
    if name in {"temporary_diversion", "arde_recovery"}:
        return 0.50
    if name == "arde_warning":
        return 0.62
    return 0.90


def list_arde_decisions() -> Dict[str, Dict[str, float]]:
    return {name: dict(values) for name, values in ARDE_PRESETS.items()}
