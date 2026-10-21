"""Validated requirement-definition contract shared by WebSocket and runtime."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import re
import unicodedata
from typing import Any

from crowdsim.domain.observation_config import IMPLEMENTED_METRIC_IDS


SCHEMA_VERSION = 1
MAX_POPULATION = 10_000
SUPPORTED_EVENT_IDS = {
    "culture", "sports", "commerce", "social-leisure", "public-social",
    "professional", "emergency",
}
SUPPORTED_METRIC_IDS = {
    "global-density", "local-density", "global-speed", "local-speed",
    "boundary-density-difference", "evacuation-time", "evacuation-efficiency",
    "absolute-evacuation-density", "pedestrian-state-change",
    "pedestrian-psychology-change", "concise-decision-advice",
    "detailed-decision-plan", "concise-simulation-summary",
}
MACRO_METRIC_IDS = {
    "global-density", "local-density", "global-speed", "local-speed",
    "boundary-density-difference",
}
DECISION_OUTPUT_IDS = {"concise-decision-advice", "detailed-decision-plan"}
METRIC_SELECTION_VERSION = 2
SUPPORTED_LOCATION_IDS = {"memorial-tower", "east-nanjing-road"}
REQUIRED_DISTRIBUTIONS = {"crowd_role", "age_band", "origin", "gender"}


class RequirementValidationError(ValueError):
    """A user-facing validation failure in a requirement payload."""


def requirement_observation_duration(requirement: dict[str, Any]) -> float | None:
    """Optional simulation-time limit for a confirmed v2 class-one requirement."""
    research = requirement.get("research")
    if (requirement.get("schema_version") != 2 or not isinstance(research, dict)
            or type(research.get("problem_type")) is not int or research["problem_type"] != 1):
        return None
    experiment = requirement.get("experiment", {})
    if not isinstance(experiment, dict):
        raise RequirementValidationError("experiment must be an object")
    # Keep previously stored requirements without this field readable.
    if "duration_seconds" not in experiment:
        return None
    value = experiment["duration_seconds"]
    try:
        duration = float(value) if type(value) in (int, float) else math.nan
    except (OverflowError, ValueError):
        duration = math.nan
    if not math.isfinite(duration) or duration <= 0:
        raise RequirementValidationError("experiment.duration_seconds must be a finite positive number")
    return duration


@dataclass(frozen=True)
class RequirementSpec:
    payload: dict[str, Any]
    fingerprint: str

    @property
    def population_total(self) -> int:
        return int(self.payload["population"]["total"])

    @property
    def location_id(self) -> str:
        return str(self.payload["spatial_scope"]["location_id"])

    @property
    def distributions(self) -> dict[str, list[dict[str, Any]]]:
        return self.payload["population"]["distributions"]

    @property
    def metric_ids(self) -> tuple[str, ...]:
        return tuple(self.payload["observation"]["metric_ids"])

    @classmethod
    def parse(cls, raw: Any, *, allow_empty_population: bool = False) -> "RequirementSpec":
        if not isinstance(raw, dict):
            raise RequirementValidationError("requirement must be an object")
        payload = deepcopy(raw)
        if type(payload.get("schema_version")) is not int or payload["schema_version"] not in (1, 2):
            raise RequirementValidationError("schema_version must be 1 or 2")

        project = _object(payload, "project")
        _text(project, "title", minimum=1, maximum=120)
        _text(project, "description", minimum=1, maximum=2_000)

        spatial = _object(payload, "spatial_scope")
        source = _text(spatial, "source", minimum=1, maximum=20)
        if source not in ({"preset", "custom", "named"} if payload["schema_version"] == 2 else {"preset", "custom"}):
            raise RequirementValidationError("spatial_scope.source must be preset or custom")
        _text(spatial, "location_id", minimum=1, maximum=80)
        _text(spatial, "name", minimum=1, maximum=120)
        if spatial.get("crs") != "EPSG:4326":
            raise RequirementValidationError("spatial_scope.crs must be EPSG:4326")
        _coordinate(spatial.get("center"), "spatial_scope.center")
        if payload["schema_version"] == 2:
            _confirmed_location(spatial)
        else:
            _polygon(spatial.get("boundary"))

        population = _object(payload, "population")
        total = population.get("total")
        minimum_population = 0 if allow_empty_population else 1
        if type(total) is not int or not minimum_population <= total <= MAX_POPULATION:
            raise RequirementValidationError(
                f"population.total must be an integer between {minimum_population} and {MAX_POPULATION}"
            )
        distributions = _object(population, "distributions")
        missing = REQUIRED_DISTRIBUTIONS - set(distributions)
        if missing:
            raise RequirementValidationError(
                f"missing population distributions: {', '.join(sorted(missing))}"
            )
        for name in REQUIRED_DISTRIBUTIONS:
            _distribution(name, distributions[name])

        scenario = _object(payload, "scenario")
        event = _object(scenario, "event_category")
        event_id = _text(event, "id", minimum=1, maximum=80)
        if event_id not in SUPPORTED_EVENT_IDS:
            raise RequirementValidationError(f"unsupported event category: {event_id}")
        if (payload["schema_version"] == 2 and isinstance(payload.get("research"), dict)
                and payload["research"].get("problem_type") == 1):
            for field in ("hotspot_start_seconds", "hotspot_end_seconds"):
                value = scenario.get(field)
                if field not in scenario or field == "hotspot_end_seconds" and value is None:
                    continue
                try:
                    valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
                except OverflowError:
                    valid = False
                if not valid:
                    raise RequirementValidationError(f"scenario.{field} must be finite non-negative seconds")
            start, end = scenario.get("hotspot_start_seconds"), scenario.get("hotspot_end_seconds")
            if end is not None and (start is None or end <= start):
                raise RequirementValidationError("hotspot end must be later than hotspot start")

        observation = _object(payload, "observation")
        metric_ids = observation.get("metric_ids")
        if not isinstance(metric_ids, list) or not metric_ids:
            raise RequirementValidationError("observation.metric_ids must not be empty")
        if any(not isinstance(metric_id, str) for metric_id in metric_ids):
            raise RequirementValidationError("observation.metric_ids must contain strings")
        if len(metric_ids) != len(set(metric_ids)):
            raise RequirementValidationError("observation.metric_ids contains duplicates")
        unknown_metrics = set(metric_ids) - SUPPORTED_METRIC_IDS
        if unknown_metrics:
            raise RequirementValidationError(
                f"unsupported observation metrics: {', '.join(sorted(unknown_metrics))}"
            )
        # Old immutable records have no selection version: preserve their payloads
        # and fingerprints, including historical evacuation and micro metrics.
        if "selection_schema_version" in observation:
            if (payload["schema_version"] != 2
                    or type(observation["selection_schema_version"]) is not int
                    or observation["selection_schema_version"] != METRIC_SELECTION_VERSION):
                raise RequirementValidationError("unsupported observation.selection_schema_version")
            research = _object(payload, "research")
            problem_type = research.get("problem_type")
            if type(problem_type) is not int or problem_type not in (1, 2):
                raise RequirementValidationError("metric selection requires problem_type 1 or 2")
            if problem_type == 1 and not set(metric_ids) <= MACRO_METRIC_IDS | {"concise-simulation-summary"}:
                raise RequirementValidationError("class 1 only accepts macro metrics and concise simulation summary")
            if problem_type == 2 and (len(metric_ids) != 1 or metric_ids[0] not in DECISION_OUTPUT_IDS):
                raise RequirementValidationError("class 2 requires exactly one decision output")
        partition = observation.get("local_partition_mode", "uniform")
        if partition not in {"uniform", "random"}:
            raise RequirementValidationError(
                "observation.local_partition_mode must be uniform or random"
            )
        observation["local_partition_mode"] = partition
        requirement_observation_duration(payload)

        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return cls(payload=payload, fingerprint=hashlib.sha256(canonical.encode("utf-8")).hexdigest())

    def capabilities(self) -> dict[str, Any]:
        location_supported = (self.location_id == "memorial-tower" if self.payload["schema_version"] == 2 else self.location_id in SUPPORTED_LOCATION_IDS)
        implemented_metrics = set(IMPLEMENTED_METRIC_IDS)
        if not self.payload["spatial_scope"].get("boundary"):
            implemented_metrics.clear()
        if self.payload["observation"]["local_partition_mode"] != "uniform":
            implemented_metrics -= {"local-density", "local-speed", "boundary-density-difference", "absolute-evacuation-density"}
        metric_status = {
            metric_id: (
                "supported" if metric_id in implemented_metrics
                else "not_implemented"
            )
            for metric_id in self.metric_ids
        }
        warnings = []
        if not location_supported:
            warnings.append(
                f"地点 {self.location_id} 已保存，但当前 SUMO 场景尚不能据此初始化"
            )
        unavailable = [name for name, status in metric_status.items() if status == "not_implemented"]
        if unavailable:
            warnings.append(f"部分指标尚未实现：{', '.join(unavailable)}")
        return {
            "location": "supported" if location_supported else "not_implemented",
            "map_location": "supported",
            "population_total": "supported",
            "population_distributions": "supported",
            "event_category": "metadata_only",
            "metrics": metric_status,
            "warnings": warnings,
        }


def requirement_runtime_summary(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the accepted requirement fields exposed once in the init frame."""
    if not isinstance(record, dict):
        return None
    requirement = record.get("requirement")
    if not isinstance(requirement, dict):
        return None
    return {
        "requirement_id": record.get("requirement_id"),
        "schema_version": record.get("schema_version"),
        "fingerprint": record.get("fingerprint"),
        "project": requirement.get("project", {}),
        "spatial_scope": requirement.get("spatial_scope", {}),
        "population": requirement.get("population", {}),
        "scenario": requirement.get("scenario", {}),
        "observation": requirement.get("observation", {}),
        "project_title": requirement.get("project", {}).get("title"),
        "location_id": requirement.get("spatial_scope", {}).get("location_id"),
        "population_total": requirement.get("population", {}).get("total"),
        "event_category_id": requirement.get("scenario", {}).get("event_category", {}).get("id"),
        "metric_ids": requirement.get("observation", {}).get("metric_ids", []),
        "capabilities": RequirementSpec.parse(
            requirement, allow_empty_population=record.get("purpose") == "experiment",
        ).capabilities(),
    }


def _object(container: dict[str, Any], name: str) -> dict[str, Any]:
    value = container.get(name)
    if not isinstance(value, dict):
        raise RequirementValidationError(f"{name} must be an object")
    return value


def _text(container: dict[str, Any], name: str, *, minimum: int, maximum: int) -> str:
    value = container.get(name)
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
        raise RequirementValidationError(
            f"{name} must contain between {minimum} and {maximum} characters"
        )
    container[name] = value.strip()
    return container[name]


def _coordinate(value: Any, field: str) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) != 2:
        raise RequirementValidationError(f"{field} must be [longitude, latitude]")
    longitude, latitude = value
    if not all(isinstance(item, (int, float)) and math.isfinite(item) for item in value):
        raise RequirementValidationError(f"{field} must contain finite numbers")
    if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
        raise RequirementValidationError(f"{field} is outside WGS84 bounds")
    return float(longitude), float(latitude)


def _polygon(value: Any) -> None:
    if not isinstance(value, dict) or value.get("type") != "Polygon":
        raise RequirementValidationError("spatial_scope.boundary must be a GeoJSON Polygon")
    coordinates = value.get("coordinates")
    if not isinstance(coordinates, list) or len(coordinates) != 1:
        raise RequirementValidationError("only a single polygon ring is supported")
    ring = coordinates[0]
    if not isinstance(ring, list) or not 4 <= len(ring) <= 2_001:
        raise RequirementValidationError("polygon ring must contain 3 to 2000 points plus closure")
    normalized = [list(_coordinate(point, "spatial_scope.boundary coordinate")) for point in ring]
    if normalized[0] != normalized[-1]:
        raise RequirementValidationError("polygon ring must be closed")
    if len({tuple(point) for point in normalized[:-1]}) < 3:
        raise RequirementValidationError("polygon must contain at least 3 distinct points")
    value["coordinates"] = [normalized]


def _distribution(name: str, value: Any) -> None:
    if not isinstance(value, list) or not value:
        raise RequirementValidationError(f"distribution {name} must not be empty")
    total = 0.0
    identities = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise RequirementValidationError(f"distribution {name}[{index}] must be an object")
        label = item.get("label")
        if not isinstance(label, str) or not label.strip():
            raise RequirementValidationError(f"distribution {name}[{index}].label is required")
        item["label"] = label.strip()
        identity = str(item.get("code") or item.get("id") or item["label"]).strip()
        if not identity or identity in identities:
            raise RequirementValidationError(f"distribution {name} contains duplicate items")
        identities.add(identity)
        percent = item.get("percent")
        if not isinstance(percent, (int, float)) or isinstance(percent, bool) or not math.isfinite(percent):
            raise RequirementValidationError(f"distribution {name}[{index}].percent must be numeric")
        if not 0 <= percent <= 100:
            raise RequirementValidationError(f"distribution {name}[{index}].percent is outside 0..100")
        total += float(percent)
    if abs(total - 100.0) >= 0.05:
        raise RequirementValidationError(f"distribution {name} must total 100%")


def _is_memorial_address(value):
    if not isinstance(value, str):
        return False
    address = re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))
    address = re.sub(r"^(?:上海市?)?(?:黄浦区)?", "", address)
    return address == "中山东一路475号黄浦公园(苏州河口南侧的黄浦江西岸)"


def _confirmed_location(spatial):
    """V2 accepts confirmed map-only places; only the existing tower binds a scene."""
    _text(spatial, "address", minimum=1, maximum=1000)
    resolution = _object(spatial, "location_resolution")
    if resolution.get("confirmed") is not True or resolution.get("provider") != "amap":
        raise RequirementValidationError("location must be queried and confirmed")
    if resolution.get("crs") != "EPSG:4326" or resolution.get("provider_crs") != "GCJ-02":
        raise RequirementValidationError("location coordinate systems are required")
    _coordinate(resolution.get("center"), "location_resolution.center")
    _coordinate(resolution.get("provider_center"), "location_resolution.provider_center")
    for name in ("confirmed_at", "original_query", "queried_at", "name", "address"):
        _text(resolution, name, minimum=1, maximum=1000)
    if resolution["address"] != spatial["address"] or resolution["name"] != spatial["name"]:
        raise RequirementValidationError("location differs from confirmed search result")
    if spatial["location_id"] == "memorial-tower":
        from pathlib import Path
        preset = json.loads((Path(__file__).resolve().parents[2] / "config/during_presets/memorial_tower_v1.json").read_text())
        expected = preset["requirement"]["spatial_scope"]
        lng, lat = resolution["center"]
        tower_match = (re.fullmatch(r"(?:上海(?:市)?)?人民英雄纪念塔", resolution["name"])
                       and str(resolution.get("adcode", "")).startswith("310")
                       and abs(lng - 121.48682) < .003 and abs(lat - 31.24428) < .003)
        if ((not tower_match and not _is_memorial_address(resolution["address"]))
                or spatial.get("boundary") != expected["boundary"] or spatial["center"] != expected["center"]):
            raise RequirementValidationError("confirmed location does not match memorial scene")
        _polygon(spatial.get("boundary"))
    elif spatial.get("boundary") is not None or spatial["center"] != resolution["center"]:
        raise RequirementValidationError("named locations have a point only, without a boundary")
