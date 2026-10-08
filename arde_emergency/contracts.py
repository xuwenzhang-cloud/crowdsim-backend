"""Versioned, dependency-free contracts used by the ARDE controller."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import math
import uuid
from typing import Any, Mapping


SCHEMA_VERSION = "1.0"
ACTION_TYPES = {
    "observe_only",
    "publish_guidance",
    "set_inflow_rate",
    "set_edge_capacity",
    "set_route_distribution",
    "reroute_group",
    "set_edge_risk_weight",
}


class ContractError(ValueError):
    """Raised when a controller input or output violates the shared contract."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def finite_number(value: Any, *, default: float | None = None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    result = float(value)
    return result if math.isfinite(result) else default


def validate_observation(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ContractError("observation must be an object")
    data = deepcopy(dict(payload))
    if str(data.get("schemaVersion") or SCHEMA_VERSION) != SCHEMA_VERSION:
        raise ContractError("unsupported observation schemaVersion")
    for key in ("observationId", "runId"):
        if not str(data.get(key) or "").strip():
            raise ContractError(f"observation.{key} is required")
    sim_time = finite_number(data.get("simTimeSeconds"))
    if sim_time is None or sim_time < 0:
        raise ContractError("observation.simTimeSeconds must be non-negative")
    for key in ("global", "strategyStats", "trend", "availability"):
        value = data.get(key)
        if value is None:
            data[key] = {}
        elif not isinstance(value, Mapping):
            raise ContractError(f"observation.{key} must be an object")
    for key in ("regions", "edges", "hazards"):
        value = data.get(key)
        if value is None:
            data[key] = []
        elif not isinstance(value, list):
            raise ContractError(f"observation.{key} must be an array")
    data["schemaVersion"] = SCHEMA_VERSION
    data["simTimeSeconds"] = sim_time
    return data


def validate_action(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ContractError("action must be an object")
    action = deepcopy(dict(payload))
    action_type = str(action.get("actionType") or "")
    if action_type not in ACTION_TYPES:
        raise ContractError(f"unsupported actionType: {action_type}")
    action["actionId"] = str(action.get("actionId") or new_id("act"))
    action["target"] = dict(action.get("target") or {})
    action["parameters"] = dict(action.get("parameters") or {})
    priority = action.get("priority", 50)
    if isinstance(priority, bool) or not isinstance(priority, (int, float)):
        raise ContractError("action.priority must be numeric")
    action["priority"] = max(0, min(100, int(priority)))
    return action


def build_decision(
    *,
    observation: Mapping[str, Any],
    controller_id: str,
    controller_version: str,
    actions: list[Mapping[str, Any]],
    reason: str,
    trigger: Mapping[str, Any] | None = None,
    expected_effect: Mapping[str, Any] | None = None,
    algorithm_trace: Mapping[str, Any] | None = None,
    model_trace: Mapping[str, Any] | None = None,
    validity_seconds: float = 120.0,
) -> dict[str, Any]:
    obs = validate_observation(observation)
    valid_from = float(obs["simTimeSeconds"])
    return {
        "schemaVersion": SCHEMA_VERSION,
        "decisionId": new_id("dec"),
        "runId": obs["runId"],
        "observationId": obs["observationId"],
        "controllerId": str(controller_id),
        "controllerVersion": str(controller_version),
        "trigger": dict(trigger or {"type": "observation_cycle"}),
        "reason": str(reason),
        "actions": [validate_action(item) for item in actions],
        "expectedEffect": dict(expected_effect or {}),
        "validFrom": valid_from,
        "validUntil": valid_from + max(1.0, float(validity_seconds)),
        "algorithmTrace": deepcopy(dict(algorithm_trace or {})),
        "modelTrace": deepcopy(dict(model_trace)) if model_trace is not None else None,
        "createdAt": utc_now(),
    }


def validate_ack(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ContractError("acknowledgement must be an object")
    ack = deepcopy(dict(payload))
    status = str(ack.get("status") or "")
    if status not in {"queued", "applied", "rejected"}:
        raise ContractError("acknowledgement.status is invalid")
    if not str(ack.get("requestId") or ack.get("request_id") or ""):
        raise ContractError("acknowledgement.requestId is required")
    return ack
