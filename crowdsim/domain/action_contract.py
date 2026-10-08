"""Validated runtime action contract shared with the MACE control plane."""

from __future__ import annotations

from copy import deepcopy
import math
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


def _number(value: Any, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result) or not low <= result <= high:
        raise ValueError(f"{name} must be in [{low}, {high}]")
    return result


def _duration(parameters: dict[str, Any]) -> float:
    return _number(parameters.get("durationSeconds", 120), "durationSeconds", 1, 1800)


def validate_control_action(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ValueError("controlAction must be an object")
    action = deepcopy(dict(payload))
    action_type = str(action.get("actionType") or "")
    if action_type not in ACTION_TYPES:
        raise ValueError(f"unsupported control action: {action_type}")
    action_id = str(action.get("actionId") or "").strip()
    if not action_id:
        raise ValueError("actionId is required")
    target = action.get("target") or {}
    parameters = action.get("parameters") or {}
    if not isinstance(target, Mapping) or not isinstance(parameters, Mapping):
        raise ValueError("target and parameters must be objects")
    target, parameters = dict(target), dict(parameters)
    if action_type == "publish_guidance":
        if not str(parameters.get("message") or "").strip():
            raise ValueError("publish_guidance requires message")
        command = str(parameters.get("command") or "inform")
        if command not in {"inform", "reroute", "disperse", "evacuate", "queue"}:
            raise ValueError("publish_guidance command is unsupported")
        parameters["command"] = command
        parameters["durationSeconds"] = _duration(parameters)
    elif action_type == "set_inflow_rate":
        if not str(target.get("entryId") or "").strip():
            raise ValueError("set_inflow_rate requires target.entryId")
        parameters["rate"] = _number(parameters.get("rate"), "rate", 0, 1)
        parameters["durationSeconds"] = _duration(parameters)
    elif action_type == "set_edge_capacity":
        if not str(target.get("edgeId") or "").strip():
            raise ValueError("set_edge_capacity requires target.edgeId")
        parameters["multiplier"] = _number(parameters.get("multiplier"), "multiplier", 0.1, 1.0)
        parameters["durationSeconds"] = _duration(parameters)
    elif action_type == "set_edge_risk_weight":
        if not str(target.get("edgeId") or "").strip():
            raise ValueError("set_edge_risk_weight requires target.edgeId")
        parameters["weight"] = _number(parameters.get("weight"), "weight", 0.1, 10.0)
        parameters["durationSeconds"] = _duration(parameters)
    elif action_type == "set_route_distribution":
        if not str(target.get("originId") or "").strip():
            raise ValueError("set_route_distribution requires target.originId")
        distributions = parameters.get("distributions")
        if not isinstance(distributions, list) or not distributions:
            raise ValueError("set_route_distribution requires distributions")
        total = 0.0
        for item in distributions:
            if not isinstance(item, Mapping) or not str(item.get("routeId") or ""):
                raise ValueError("each route distribution requires routeId")
            route = item.get("routeEdges")
            if not isinstance(route, list) or not route or any(not str(edge).strip() for edge in route):
                raise ValueError("each route distribution requires non-empty routeEdges")
            total += _number(item.get("ratio"), "distribution.ratio", 0, 1)
        if abs(total - 1.0) > 1e-5:
            raise ValueError("route distribution ratios must total 1")
        parameters["durationSeconds"] = _duration(parameters)
    elif action_type == "reroute_group":
        if not str(target.get("groupId") or "").strip():
            raise ValueError("reroute_group requires target.groupId")
        route = parameters.get("routeEdges")
        if not isinstance(route, list) or not route:
            raise ValueError("reroute_group requires parameters.routeEdges")
    action["schemaVersion"] = SCHEMA_VERSION
    action["target"] = target
    action["parameters"] = parameters
    return action
