"""Versioned validation for standalone CrowdSim control experiments."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import math
import uuid
from typing import Any, Mapping

from crowdsim.domain.requirement_spec import MAX_POPULATION
from crowdsim.domain.during_preset import bind_preset_scenario


SCHEMA_VERSION = "1.0"
METHOD_IDS = {"C0", "C1", "C2", "C3", "C4", "C5", "D2"}
ACTION_TYPES = {
    "observe_only", "publish_guidance", "set_inflow_rate", "set_edge_capacity",
    "set_route_distribution", "reroute_group", "set_edge_risk_weight",
}


class ContractError(ValueError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def public_time(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat().replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def finite(value: Any, default: float | None = None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = float(value)
    return number if math.isfinite(number) else default


def _reject_secret_fields(value: Any, path: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = "".join(character for character in str(key).lower() if character.isalnum())
            if normalized in {"apikey", "authorization", "password", "secret"} or normalized.endswith("token"):
                raise ContractError(
                    f"{path}.{key} is not allowed; configure model credentials with backend environment variables"
                )
            _reject_secret_fields(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secret_fields(item, f"{path}[{index}]")


def validate_experiment_config(payload: Mapping[str, Any], *, allow_during_replay=False, allow_during_observation=False) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ContractError("experiment config must be an object")
    data = deepcopy(dict(payload))
    if not allow_during_observation and ("duringObservation" in data or (
            isinstance(data.get("scenario"), Mapping) and "observationBinding" in data["scenario"])):
        raise ContractError("第一类实验必须通过已确认需求的专用接口创建。")
    if (data.get("duringReplay") is not None or "D2" in (data.get("methods") or [])) and not allow_during_replay:
        raise ContractError("策略重跑必须通过来源实验的确认接口创建。")
    request_id = data.get("clientRequestId")
    if request_id is not None and (
        not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 128
        or any(not (c.isascii() and (c.isalnum() or c in "_-")) for c in request_id)
    ):
        raise ContractError("clientRequestId must contain 1-128 ASCII letters, digits, underscores or hyphens")
    name = str(data.get("name") or "").strip()
    if not name:
        raise ContractError("name is required")
    scenario = data.get("scenario")
    if not isinstance(scenario, Mapping):
        raise ContractError("scenario must be an object")
    scenario = dict(scenario)
    if scenario.get("presetId") is not None:
        try:
            scenario = bind_preset_scenario(scenario)
        except (ValueError, OSError) as exc:
            raise ContractError(str(exc)) from exc
        data.setdefault("methods", ["C0"])
        if data["methods"] != ["C0"] and not (allow_during_replay and data["methods"] == ["D2"]):
            raise ContractError("during stage 1 supports only the C0 baseline")
    elif any(key in scenario for key in ("duringPreset", "presetVersion", "presetFingerprint")):
        raise ContractError("during preset metadata requires scenario.presetId")
    population = scenario.get("population")
    if population is not None and (type(population) is not int or not 0 <= population <= MAX_POPULATION):
        raise ContractError(f"scenario.population must be an integer between 0 and {MAX_POPULATION}")
    seeds = scenario.get("seedSet") or []
    if not isinstance(seeds, list) or not seeds or any(isinstance(seed, bool) or not isinstance(seed, int) for seed in seeds):
        raise ContractError("scenario.seedSet must be a non-empty integer array")
    methods = data.get("methods") or []
    if not isinstance(methods, list) or not methods or any(method not in METHOD_IDS for method in methods):
        raise ContractError("methods must contain C0-C5 ids")
    duration = finite(scenario.get("durationSeconds"), 3600.0)
    if duration is None or duration <= 0:
        raise ContractError("scenario.durationSeconds must be positive")
    interval = finite(data.get("observationIntervalSeconds"), 30.0)
    window = finite(data.get("evaluationWindowSeconds"), 120.0)
    if interval is None or interval <= 0 or window is None or window <= 0:
        raise ContractError("observation and evaluation intervals must be positive")
    mode = str(data.get("mode") or "formal")
    if mode not in {"formal", "debug"}:
        raise ContractError("mode must be formal or debug")
    controller_config = data.get("controllerConfig") or {}
    llm_config = data.get("llmConfig") or {}
    if not isinstance(controller_config, Mapping) or not isinstance(llm_config, Mapping):
        raise ContractError("controllerConfig and llmConfig must be objects")
    _reject_secret_fields(controller_config, "controllerConfig")
    _reject_secret_fields(llm_config, "llmConfig")
    statistical = scenario.get("statisticalValidation") or {}
    if not isinstance(statistical, Mapping):
        raise ContractError("scenario.statisticalValidation must be an object")
    minimum_seeds = int(statistical.get("minimumSeeds", 1) or 1)
    if minimum_seeds < 1 or minimum_seeds > 30:
        raise ContractError("scenario.statisticalValidation.minimumSeeds must be in [1, 30]")
    if len(set(seeds)) < minimum_seeds:
        raise ContractError(f"scenario.seedSet requires at least {minimum_seeds} unique seeds")
    emergency_event = scenario.get("emergencyEvent")
    if emergency_event is not None:
        if not isinstance(emergency_event, Mapping):
            raise ContractError("scenario.emergencyEvent must be an object")
        emergency_event = dict(emergency_event)
        if emergency_event.get("enabled", True):
            lon = finite(emergency_event.get("lng", emergency_event.get("lon")))
            lat = finite(emergency_event.get("lat"))
            intensity = finite(emergency_event.get("intensity"), 0.85)
            radius = finite(emergency_event.get("radius"), 60.0)
            if lon is None or lat is None:
                raise ContractError("enabled scenario.emergencyEvent requires finite lng/lon and lat")
            if intensity is None or not 0 <= intensity <= 1 or radius is None or radius <= 0:
                raise ContractError("scenario.emergencyEvent intensity/radius is invalid")
            emergency_event.update({"lng": lon, "lat": lat, "intensity": intensity, "radius": radius})
        scenario["emergencyEvent"] = emergency_event
    data.update({
        "schemaVersion": SCHEMA_VERSION,
        "name": name,
        "scenario": {**scenario, "durationSeconds": duration, "seedSet": list(dict.fromkeys(seeds))},
        "methods": list(dict.fromkeys(methods)),
        "mode": mode,
        "observationIntervalSeconds": interval,
        "evaluationWindowSeconds": window,
        "controllerConfig": dict(controller_config),
        "llmConfig": dict(llm_config),
        "metricSet": list(data.get("metricSet") or []),
    })
    if "clarificationSnapshot" in data:
        from .clarification import validate_clarification_snapshot
        try:
            _reject_secret_fields(data["clarificationSnapshot"], "clarificationSnapshot")
            validate_clarification_snapshot(data)
        except (ValueError, TypeError, KeyError) as exc:
            raise ContractError(str(exc)) from exc
    if allow_during_replay:
        from .during_strategy import validate_replay_binding
        validate_replay_binding(data)
    return data


def validate_observation(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ContractError("observation must be an object")
    data = deepcopy(dict(payload))
    for key in ("observationId", "runId"):
        if not str(data.get(key) or ""):
            raise ContractError(f"observation.{key} is required")
    sim_time = finite(data.get("simTimeSeconds"))
    if sim_time is None or sim_time < 0:
        raise ContractError("observation.simTimeSeconds must be non-negative")
    data["schemaVersion"] = SCHEMA_VERSION
    data["simTimeSeconds"] = sim_time
    for key in ("global", "strategyStats", "trend", "availability"):
        data[key] = dict(data.get(key) or {})
    for key in ("regions", "edges", "hazards"):
        if not isinstance(data.get(key, []), list):
            raise ContractError(f"observation.{key} must be an array")
        data.setdefault(key, [])
    return data


def validate_decision(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ContractError("decision must be an object")
    data = deepcopy(dict(payload))
    for key in ("decisionId", "runId", "observationId", "controllerId"):
        if not str(data.get(key) or ""):
            raise ContractError(f"decision.{key} is required")
    if not isinstance(data.get("actions"), list):
        raise ContractError("decision.actions must be an array")
    data["actions"] = [validate_action(action) for action in data["actions"]]
    data["schemaVersion"] = SCHEMA_VERSION
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
    action["priority"] = max(0, min(100, int(action.get("priority", 50))))
    return action
