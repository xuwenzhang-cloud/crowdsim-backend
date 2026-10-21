"""Validated named crowd-observation areas on the loaded SUMO network."""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path

from crowdsim.infrastructure.network_adapter import ResearchNetwork


class HotspotCatalog:
    def __init__(self, network: ResearchNetwork, path: str | Path) -> None:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        self.parameter_status = str(payload.get("parameter_status", "unspecified"))
        self.default_hotspot_id = str(payload.get("default_hotspot_id", "")).strip() or None
        self.hotspots = {}
        for raw in payload.get("hotspots", ()):
            item = dict(raw)
            hotspot_id = str(item.get("id", "")).strip()
            if not hotspot_id or hotspot_id in self.hotspots:
                raise ValueError("hotspot ids must be non-empty and unique")

            measurement_edges = self._edge_ids(
                item.get("measurement_edges", ()), hotspot_id, "measurement_edges"
            )
            if not measurement_edges:
                raise ValueError(f"hotspot {hotspot_id} has no measurement_edges")
            target_edge = str(item.get("target_edge", "")).strip()
            configured_targets = item.get("target_edges")
            if configured_targets is None and not target_edge:
                raise ValueError(f"hotspot {hotspot_id} must define target_edge or target_edges")
            target_edges = self._edge_ids(
                configured_targets if configured_targets is not None else (target_edge,),
                hotspot_id,
                "target_edges",
            )
            if not target_edges:
                raise ValueError(f"hotspot {hotspot_id} must define target_edge or target_edges")
            if not target_edge:
                # Legacy single-edge consumers retain a stable representative
                # until demand generation and metrics become fully zone-aware.
                target_edge = target_edges[0]
            if target_edge not in target_edges:
                raise ValueError(f"hotspot {hotspot_id} target_edge must belong to target_edges")
            if not set(target_edges).issubset(measurement_edges):
                raise ValueError(f"hotspot {hotspot_id} target_edges must be measured")

            entry_edges = self._edge_ids(item.get("entry_edges", ()), hotspot_id, "entry_edges")
            access_portals = self._access_portals(item.get("access_portals"), hotspot_id)
            portal_edges = tuple(portal["edge"] for portal in access_portals)
            if portal_edges:
                if entry_edges and entry_edges != portal_edges:
                    raise ValueError(
                        f"hotspot {hotspot_id} access_portals must match entry_edges order"
                    )
                entry_edges = portal_edges
            else:
                access_portals = tuple(
                    {
                        "id": f"portal_{index + 1}",
                        "edge": edge_id,
                        "outside_side": "auto",
                        "inside_side": "auto",
                    }
                    for index, edge_id in enumerate(entry_edges)
                )
            legacy_approach_edges = self._edge_ids(
                item.get("park_access_edges", ()), hotspot_id, "park_access_edges"
            )
            configured_approach_edges = item.get("approach_edges")
            approach_edges = self._edge_ids(
                configured_approach_edges or legacy_approach_edges,
                hotspot_id,
                "approach_edges",
            )
            if (
                configured_approach_edges is not None
                and legacy_approach_edges
                and approach_edges != legacy_approach_edges
            ):
                raise ValueError(
                    f"hotspot {hotspot_id} approach_edges must match legacy park_access_edges"
                )
            park_access_edges = approach_edges
            excluded_edges = self._edge_ids(item.get("excluded_edges", ()), hotspot_id, "excluded_edges")
            visitor_spawn_edges = self._edge_ids(
                item.get("visitor_spawn_edges", ()), hotspot_id, "visitor_spawn_edges"
            )
            visitor_exit_edges = self._edge_ids(
                item.get("visitor_exit_edges", ()), hotspot_id, "visitor_exit_edges"
            )
            visitor_destination_edges = self._edge_ids(
                item.get("visitor_destination_edges", ()),
                hotspot_id,
                "visitor_destination_edges",
            )
            park_entry_edges = self._edge_ids(
                item.get("park_entry_edges", ()), hotspot_id, "park_entry_edges"
            )
            external_approach_edges = self._edge_ids(
                item.get("external_approach_edges", ()), hotspot_id, "external_approach_edges"
            )
            spawn_distribution = str(item.get("spawn_distribution", "edge_start")).strip()
            if spawn_distribution not in {"edge_start", "edge_length_weighted_random", "edge_length_uniform"}:
                raise ValueError(f"hotspot {hotspot_id} has unsupported spawn_distribution")
            spawn_margin = float(item.get("spawn_position_margin_meters", 0.0))
            if not math.isfinite(spawn_margin) or spawn_margin < 0.0:
                raise ValueError(f"hotspot {hotspot_id} has invalid spawn_position_margin_meters")
            destination_distribution = str(
                item.get("destination_distribution", "edge_uniform_random")
            ).strip()
            if destination_distribution not in {
                "edge_uniform_random",
                "edge_length_weighted_random",
            }:
                raise ValueError(
                    f"hotspot {hotspot_id} has unsupported destination_distribution"
                )
            destination_weights = item.get("destination_edge_weights")
            if destination_weights is not None:
                if (destination_distribution != "edge_uniform_random"
                        or not isinstance(destination_weights, dict)
                        or set(destination_weights) != set(visitor_destination_edges)):
                    raise ValueError(f"hotspot {hotspot_id} destination_edge_weights must cover exactly the uniform destination edges")
                destination_weights = {edge: float(weight) for edge, weight in destination_weights.items()}
                if any(not math.isfinite(weight) or weight <= 0 for weight in destination_weights.values()):
                    raise ValueError(f"hotspot {hotspot_id} destination_edge_weights must be positive and finite")
                item["destination_edge_weights"] = destination_weights
            destination_margin = float(item.get("destination_position_margin_meters", 0.0))
            if not math.isfinite(destination_margin) or destination_margin < 0.0:
                raise ValueError(
                    f"hotspot {hotspot_id} has invalid destination_position_margin_meters"
                )
            visitor_departure_window = self._optional_time_window(
                item.get("visitor_departure_window_seconds"),
                hotspot_id,
                "visitor_departure_window_seconds",
            )
            align_to_zero = item.get("align_first_visitor_to_zero", False)
            if type(align_to_zero) is not bool:
                raise ValueError(f"hotspot {hotspot_id} align_first_visitor_to_zero must be a boolean")
            item["align_first_visitor_to_zero"] = align_to_zero
            visitor_arrival_profile = self._arrival_profile(
                item.get("visitor_arrival_profile"), hotspot_id
            )
            if visitor_departure_window is not None and visitor_arrival_profile is not None:
                raise ValueError(
                    f"hotspot {hotspot_id} departure window and arrival profile are mutually exclusive"
                )
            activity_window = self._optional_time_window(
                item.get("activity_window_seconds"), hotspot_id, "activity_window_seconds"
            )
            release_delay_window = self._optional_time_window(
                item.get("visitor_release_delay_seconds"),
                hotspot_id,
                "visitor_release_delay_seconds",
            )
            if release_delay_window is not None and activity_window is None:
                raise ValueError(
                    f"hotspot {hotspot_id} release delay requires activity_window_seconds"
                )
            viewing_zones = self._viewing_zones(
                network, item.get("viewing_zones"), hotspot_id, target_edges
            )
            target_distribution = str(
                item.get(
                    "target_distribution",
                    "single_edge" if len(target_edges) == 1 else "pedestrian_area_weighted",
                )
            )
            if target_distribution not in {
                "single_edge",
                "pedestrian_area_weighted",
                "weighted_viewing_arcs",
            }:
                raise ValueError(
                    f"hotspot {hotspot_id} has unsupported target_distribution"
                )
            if target_distribution == "weighted_viewing_arcs" and not viewing_zones:
                raise ValueError(
                    f"hotspot {hotspot_id} weighted_viewing_arcs requires viewing_zones"
                )
            active_edges = (
                set(measurement_edges)
                | set(entry_edges)
                | set(park_access_edges)
                | set(visitor_spawn_edges)
                | set(visitor_exit_edges)
                | set(visitor_destination_edges)
                | set(park_entry_edges)
                | set(external_approach_edges)
            )
            if set(excluded_edges) & active_edges:
                raise ValueError(f"hotspot {hotspot_id} excluded_edges cannot be active hotspot edges")

            for edge_id in (
                *measurement_edges,
                *entry_edges,
                *park_access_edges,
                *visitor_spawn_edges,
                *visitor_exit_edges,
                *visitor_destination_edges,
                *park_entry_edges,
                *external_approach_edges,
                *excluded_edges,
            ):
                self._validate_pedestrian_edge(network, hotspot_id, edge_id)

            item["target_edge"] = target_edge
            item["target_edges"] = target_edges
            item["entry_edges"] = entry_edges
            item["access_portals"] = access_portals
            item["park_access_edges"] = park_access_edges
            item["approach_edges"] = approach_edges
            item["measurement_edges"] = measurement_edges
            item["excluded_edges"] = excluded_edges
            item["visitor_spawn_edges"] = visitor_spawn_edges
            item["visitor_exit_edges"] = visitor_exit_edges
            item["visitor_destination_edges"] = visitor_destination_edges
            item["park_entry_edges"] = park_entry_edges
            item["external_approach_edges"] = external_approach_edges
            item["spawn_distribution"] = spawn_distribution
            item["spawn_position_margin_meters"] = spawn_margin
            item["destination_distribution"] = destination_distribution
            item["destination_position_margin_meters"] = destination_margin
            item["visitor_departure_window_seconds"] = visitor_departure_window
            item["visitor_arrival_profile"] = visitor_arrival_profile
            item["activity_window_seconds"] = activity_window
            item["visitor_release_delay_seconds"] = release_delay_window
            item["viewing_zones"] = viewing_zones
            item["target_distribution"] = target_distribution
            item["route_choice"] = self._route_choice(item.get("route_choice"), hotspot_id)
            if item["route_choice"]["enabled"] and entry_edges and not park_access_edges:
                raise ValueError(
                    f"hotspot {hotspot_id} gated dynamic route choice requires approach edges"
                )
            if len(target_edges) > 1 and (
                not visitor_spawn_edges
                or (not visitor_destination_edges and not visitor_exit_edges)
            ):
                raise ValueError(
                    f"multi-edge hotspot {hotspot_id} requires spawn and destination edges"
                )
            if park_entry_edges and not set(park_entry_edges).issubset(park_access_edges):
                raise ValueError(f"hotspot {hotspot_id} park_entry_edges must be park access edges")
            allowed_spawn_edges = (
                set(external_approach_edges) | set(park_access_edges)
                | set(entry_edges) | set(target_edges)
            )
            if visitor_spawn_edges and not set(visitor_spawn_edges).issubset(allowed_spawn_edges):
                raise ValueError(
                    f"hotspot {hotspot_id} visitor_spawn_edges must be approach, portal or target edges"
                )
            external_spawn_edges = set(visitor_spawn_edges) & set(external_approach_edges)
            if external_spawn_edges and not park_entry_edges:
                raise ValueError(
                    f"hotspot {hotspot_id} external visitor spawn edges require park_entry_edges"
                )
            if spawn_distribution == "edge_length_weighted_random":
                too_short = [
                    edge_id for edge_id in visitor_spawn_edges
                    if network.edges[edge_id].getLength() <= 2.0 * spawn_margin
                ]
                if too_short:
                    raise ValueError(
                        f"hotspot {hotspot_id} spawn edges are too short for the configured margin: "
                        + ", ".join(too_short)
                    )
            if visitor_exit_edges and not set(visitor_exit_edges).issubset(external_approach_edges):
                raise ValueError(f"hotspot {hotspot_id} visitor_exit_edges must be external approach edges")
            allowed_destination_edges = set(external_approach_edges) | set(park_access_edges)
            if visitor_destination_edges and not set(visitor_destination_edges).issubset(
                allowed_destination_edges
            ):
                raise ValueError(
                    f"hotspot {hotspot_id} visitor_destination_edges must be external approach "
                    "or park access edges"
                )
            too_short_destinations = [
                edge_id for edge_id in visitor_destination_edges
                if network.edges[edge_id].getLength() <= 2.0 * destination_margin
            ]
            if too_short_destinations:
                raise ValueError(
                    f"hotspot {hotspot_id} destination edges are too short for the configured "
                    "margin: " + ", ".join(too_short_destinations)
                )
            if set(external_approach_edges) & set(park_access_edges):
                raise ValueError(f"hotspot {hotspot_id} external approach edges cannot be park access edges")
            self.hotspots[hotspot_id] = item
        if self.default_hotspot_id is not None and self.default_hotspot_id not in self.hotspots:
            raise ValueError(f"unknown default_hotspot_id: {self.default_hotspot_id}")

    @staticmethod
    def _edge_ids(raw_edges, hotspot_id: str, field: str) -> tuple[str, ...]:
        edges = tuple(str(edge_id).strip() for edge_id in raw_edges)
        if any(not edge_id for edge_id in edges):
            raise ValueError(f"hotspot {hotspot_id} {field} contains an empty edge id")
        if len(set(edges)) != len(edges):
            raise ValueError(f"hotspot {hotspot_id} {field} contains duplicate edge ids")
        return edges

    @staticmethod
    def _validate_pedestrian_edge(network: ResearchNetwork, hotspot_id: str, edge_id: str) -> None:
        edge = network.edges.get(edge_id)
        if edge is None:
            raise ValueError(f"hotspot {hotspot_id} references unknown edge {edge_id}")
        if not any(lane.allows("pedestrian") for lane in edge.getLanes()):
            raise ValueError(f"hotspot {hotspot_id} edge does not allow pedestrians: {edge_id}")

    @staticmethod
    def _access_portals(raw, hotspot_id: str) -> tuple[dict, ...]:
        if raw is None:
            return ()
        if not isinstance(raw, list):
            raise ValueError(f"hotspot {hotspot_id} access_portals must be a list")
        result = []
        seen_ids = set()
        seen_edges = set()
        aliases = {"from": "start", "to": "end"}
        allowed_sides = {"auto", "start", "end"}
        for portal in raw:
            if not isinstance(portal, dict):
                raise ValueError(f"hotspot {hotspot_id} access portal entries must be objects")
            portal_id = str(portal.get("id", "")).strip()
            edge_id = str(portal.get("edge", "")).strip()
            if not portal_id or portal_id in seen_ids:
                raise ValueError(f"hotspot {hotspot_id} access portal ids must be unique")
            if not edge_id or edge_id in seen_edges:
                raise ValueError(f"hotspot {hotspot_id} access portal edges must be unique")
            outside = aliases.get(
                str(portal.get("outside_side", "auto")).strip(),
                str(portal.get("outside_side", "auto")).strip(),
            )
            inside = aliases.get(
                str(portal.get("inside_side", "auto")).strip(),
                str(portal.get("inside_side", "auto")).strip(),
            )
            if outside not in allowed_sides or inside not in allowed_sides:
                raise ValueError(
                    f"hotspot {hotspot_id} access portal sides must be auto/start/end"
                )
            if outside != "auto" and inside != "auto" and outside == inside:
                raise ValueError(
                    f"hotspot {hotspot_id} access portal sides must be opposite"
                )
            seen_ids.add(portal_id)
            seen_edges.add(edge_id)
            result.append({
                "id": portal_id,
                "edge": edge_id,
                "outside_side": outside,
                "inside_side": inside,
            })
        return tuple(result)

    @staticmethod
    def _optional_time_window(raw, hotspot_id: str, field: str) -> tuple[float, float] | None:
        if raw is None:
            return None
        if not isinstance(raw, list) or len(raw) != 2:
            raise ValueError(f"hotspot {hotspot_id} {field} must contain [start, end]")
        start, end = map(float, raw)
        if not math.isfinite(start) or not math.isfinite(end) or start < 0.0 or end < start:
            raise ValueError(f"hotspot {hotspot_id} has invalid {field}")
        return start, end

    @classmethod
    def _arrival_profile(cls, raw, hotspot_id: str) -> tuple[dict, ...] | None:
        if raw is None:
            return None
        if not isinstance(raw, list) or not raw:
            raise ValueError(f"hotspot {hotspot_id} visitor_arrival_profile must be non-empty")
        result = []
        previous_end = None
        for segment in raw:
            if not isinstance(segment, dict):
                raise ValueError(
                    f"hotspot {hotspot_id} visitor_arrival_profile entries must be objects"
                )
            window = cls._optional_time_window(
                segment.get("window_seconds"),
                hotspot_id,
                "visitor_arrival_profile.window_seconds",
            )
            if window is None:
                raise ValueError(
                    f"hotspot {hotspot_id} arrival profile segment requires window_seconds"
                )
            if previous_end is not None and window[0] < previous_end:
                raise ValueError(
                    f"hotspot {hotspot_id} arrival profile windows must be ordered and non-overlapping"
                )
            fraction = float(segment.get("fraction", 0.0))
            if not math.isfinite(fraction) or fraction <= 0.0:
                raise ValueError(
                    f"hotspot {hotspot_id} arrival profile fractions must be positive"
                )
            result.append({"window_seconds": list(window), "fraction": fraction})
            previous_end = window[1]
        return tuple(result)

    @staticmethod
    def _viewing_zones(
        network,
        raw,
        hotspot_id: str,
        target_edges: tuple[str, ...],
    ) -> tuple[dict, ...]:
        if raw is None:
            return ()
        if not isinstance(raw, list) or not raw:
            raise ValueError(f"hotspot {hotspot_id} viewing_zones must be non-empty")
        result = []
        seen_ids = set()
        for zone in raw:
            if not isinstance(zone, dict):
                raise ValueError(f"hotspot {hotspot_id} viewing zone entries must be objects")
            zone_id = str(zone.get("id", "")).strip()
            edge_id = str(zone.get("edge", "")).strip()
            if not zone_id or zone_id in seen_ids:
                raise ValueError(f"hotspot {hotspot_id} viewing zone ids must be unique")
            if edge_id not in target_edges:
                raise ValueError(
                    f"hotspot {hotspot_id} viewing zone {zone_id} must use a target edge"
                )
            raw_range = zone.get("position_range_meters")
            if not isinstance(raw_range, list) or len(raw_range) != 2:
                raise ValueError(
                    f"hotspot {hotspot_id} viewing zone {zone_id} requires a position range"
                )
            start, end = map(float, raw_range)
            edge_length = float(network.edges[edge_id].getLength())
            if (
                not math.isfinite(start)
                or not math.isfinite(end)
                or start < 0.0
                or start >= end
                or end > edge_length
            ):
                raise ValueError(
                    f"hotspot {hotspot_id} viewing zone {zone_id} has an invalid position range"
                )
            weight = float(zone.get("weight", 0.0))
            if not math.isfinite(weight) or weight <= 0.0:
                raise ValueError(
                    f"hotspot {hotspot_id} viewing zone {zone_id} has invalid weight"
                )
            seen_ids.add(zone_id)
            result.append({
                "id": zone_id,
                "edge": edge_id,
                "position_range_meters": [start, end],
                "weight": weight,
            })
        return tuple(result)

    @staticmethod
    def _route_choice(raw, hotspot_id: str) -> dict:
        if raw is None:
            return {"enabled": False}
        if not isinstance(raw, dict):
            raise ValueError(f"hotspot {hotspot_id} route_choice must be an object")
        result = dict(raw)
        result["enabled"] = bool(result.get("enabled", False))
        numeric = {
            "decision_interval_seconds": (0.1, None),
            "switch_cooldown_seconds": (0.0, None),
            "minimum_savings_seconds": (0.0, None),
            "free_flow_density_person_per_m2": (0.0, None),
            "congested_density_person_per_m2": (0.0, None),
            "speed_floor_mps": (0.01, None),
        }
        defaults = {
            "decision_interval_seconds": 10.0,
            "switch_cooldown_seconds": 15.0,
            "minimum_savings_seconds": 20.0,
            "free_flow_density_person_per_m2": 0.5,
            "congested_density_person_per_m2": 1.5,
            "speed_floor_mps": 0.2,
        }
        for key, (minimum, maximum) in numeric.items():
            value = float(result.get(key, defaults[key]))
            if not math.isfinite(value) or value < minimum or (maximum is not None and value > maximum):
                raise ValueError(f"hotspot {hotspot_id} has invalid route_choice.{key}")
            result[key] = value
        if result["congested_density_person_per_m2"] < result["free_flow_density_person_per_m2"]:
            raise ValueError(f"hotspot {hotspot_id} congested density must not be below free-flow density")
        return result

    def apply_demand_timing(self, report: dict) -> None:
        """Expose the generated run's effective clock, without shifting twice."""
        item = self.hotspots[report["hotspot_id"]]
        for key in (
            "visitor_departure_window_seconds", "visitor_arrival_profile",
            "arrival_window_seconds", "activity_window_seconds",
            "timeline_shift_seconds", "timeline_alignment_status",
        ):
            item[key] = copy.deepcopy(report[key])
        if report.get("requirement_activity"):
            item["requirement_activity"] = copy.deepcopy(report["requirement_activity"])
            item["visitor_release_delay_seconds"] = [0, 0] if report["requirement_activity"]["end_seconds"] is not None else None
            item["visitor_destination_edges"] = list(report["destination_edge_counts"])
            item["destination_edge_weights"] = None

    def serialize(self) -> list[dict]:
        result = []
        for item in self.hotspots.values():
            result.append({
                "id": item["id"],
                "name": item.get("name", item["id"]),
                "is_default": item["id"] == self.default_hotspot_id,
                "anchor": dict(item.get("anchor", {})),
                "target_edge": item["target_edge"],
                "target_edges": list(item["target_edges"]),
                "entry_edges": list(item["entry_edges"]),
                "access_portals": [dict(portal) for portal in item["access_portals"]],
                "park_access_edges": list(item["park_access_edges"]),
                "approach_edges": list(item["approach_edges"]),
                "measurement_edges": list(item["measurement_edges"]),
                "excluded_edges": list(item["excluded_edges"]),
                "visitor_spawn_edges": list(item["visitor_spawn_edges"]),
                "visitor_exit_edges": list(item["visitor_exit_edges"]),
                **({"requirement_activity": copy.deepcopy(item["requirement_activity"])} if item.get("requirement_activity") else {}),
                "visitor_destination_edges": list(item["visitor_destination_edges"]),
                "park_entry_edges": list(item["park_entry_edges"]),
                "external_approach_edges": list(item["external_approach_edges"]),
                "spawn_distribution": item["spawn_distribution"],
                "spawn_position_margin_meters": item["spawn_position_margin_meters"],
                "destination_distribution": item["destination_distribution"],
                "destination_edge_weights": (
                    dict(item["destination_edge_weights"])
                    if item.get("destination_edge_weights") is not None else None
                ),
                "destination_position_margin_meters": item["destination_position_margin_meters"],
                "visitor_departure_window_seconds": (
                    list(item["visitor_departure_window_seconds"])
                    if item["visitor_departure_window_seconds"] is not None
                    else None
                ),
                "timeline_shift_seconds": item.get("timeline_shift_seconds", 0.0),
                "timeline_alignment_status": item.get("timeline_alignment_status", "not_applied"),
                "visitor_arrival_profile": (
                    [dict(segment) for segment in item["visitor_arrival_profile"]]
                    if item["visitor_arrival_profile"] is not None
                    else None
                ),
                "activity_window_seconds": (
                    list(item["activity_window_seconds"])
                    if item["activity_window_seconds"] is not None
                    else None
                ),
                "visitor_release_delay_seconds": (
                    list(item["visitor_release_delay_seconds"])
                    if item["visitor_release_delay_seconds"] is not None
                    else None
                ),
                "viewing_zones": [dict(zone) for zone in item["viewing_zones"]],
                "target_distribution": item["target_distribution"],
                "route_choice": dict(item["route_choice"]),
                "phase_thresholds": dict(item.get("phase_thresholds", {})),
                "parameter_status": self.parameter_status,
            })
        return result
