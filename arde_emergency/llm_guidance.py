"""Optional, bounded outer-layer guidance adapter."""

from __future__ import annotations

from typing import Any, Callable, Mapping


ALLOWED_KEYS = {
    "exploration_rate",
    "diversity_weight",
    "polarization_penalty",
    "coordination_strength",
    "action_budget",
}


def request_guidance(
    provider: Callable[[dict[str, Any]], Mapping[str, Any]] | None,
    payload: dict[str, Any],
) -> tuple[dict[str, float], dict[str, Any]]:
    if provider is None:
        return {}, {"status": "disabled"}
    try:
        response = provider(payload)
        if not isinstance(response, Mapping):
            raise ValueError("LLM guidance must be an object")
        raw = response.get("adjustments") or {}
        if not isinstance(raw, Mapping):
            raise ValueError("LLM adjustments must be an object")
        cleaned = {
            key: float(value)
            for key, value in raw.items()
            if key in ALLOWED_KEYS and isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        return cleaned, {
            "status": "accepted",
            "reason": str(response.get("reason") or ""),
            "confidence": response.get("confidence"),
            "raw": dict(response),
        }
    except Exception as exc:  # provider failures must never stop the controller
        return {}, {"status": "fallback", "error": f"{type(exc).__name__}: {exc}"}
