"""Build finite hotspot-visit demand with optional background pedestrians."""

from __future__ import annotations

import copy
from collections import Counter
import json
import math
import random
import statistics
from pathlib import Path
import xml.etree.ElementTree as ET

import sumolib

from crowdsim.decision.position_aware_router import (
    EdgePosition,
    PositionAwarePedestrianRouter,
    PositionRouteUnavailable,
)
from crowdsim.domain.person_parameters import (
    GOAL_LOCK_PARAM,
    HOTSPOT_DWELL_SECONDS_PARAM,
    HOTSPOT_ENTRY_EDGE_PARAM,
    HOTSPOT_ID_PARAM,
    HOTSPOT_PARK_ENTRY_EDGE_PARAM,
    HOTSPOT_RELEASE_TIME_PARAM,
    HOTSPOT_TARGET_EDGE_PARAM,
    ITINERARY_LOCK_PARAM,
)


LOCK_PARAM = ITINERARY_LOCK_PARAM


def build_hotspot_demand(
    source_path: str | Path,
    network_path: str | Path,
    hotspot_config_path: str | Path,
    output_path: str | Path,
    *,
    hotspot_id: str = "chen_yi_square",
    seed: int = 20260908,
    visitor_count: int | None = None,
    background_count: int | None = None,
) -> dict:
    """Generate explicit people; no person is created after SUMO starts."""
    source_path = Path(source_path)
    output_path = Path(output_path)
    payload = json.loads(Path(hotspot_config_path).read_text(encoding="utf-8"))
    hotspot = next((item for item in payload.get("hotspots", ()) if item.get("id") == hotspot_id), None)
    if hotspot is None:
        raise ValueError(f"unknown hotspot: {hotspot_id}")
    target_edge = str(hotspot["target_edge"])
    target_edges = tuple(str(edge_id) for edge_id in hotspot.get("target_edges", (target_edge,)))
    visitor_count = int(hotspot["visitor_count"] if visitor_count is None else visitor_count)
    background_count = int(hotspot["background_count"] if background_count is None else background_count)
    if visitor_count < 0 or background_count < 0:
        raise ValueError("demand counts must be non-negative")
    align_to_zero = hotspot.get("align_first_visitor_to_zero", False)
    if type(align_to_zero) is not bool:
        raise ValueError("align_first_visitor_to_zero must be a boolean")

    tree = ET.parse(source_path)
    source_root = tree.getroot()
    source_people = list(source_root.findall("person"))
    if not source_people:
        raise ValueError("source demand contains no explicit people")
    network = sumolib.net.readNet(
        str(network_path), withInternal=True, withPedestrianConnections=True
    )
    position_router = PositionAwarePedestrianRouter(network)
    target_lanes = {}
    target_areas = {}
    for edge_id in target_edges:
        edge = network.getEdge(edge_id)
        lanes = [lane for lane in edge.getLanes() if lane.allows("pedestrian")]
        if not lanes:
            raise ValueError(f"target edge does not allow pedestrians: {edge_id}")
        target_lanes[edge_id] = lanes
        target_areas[edge_id] = edge.getLength() * sum(lane.getWidth() for lane in lanes)

    viewing_zone_specs = _viewing_zone_specs(network, hotspot, target_edges)
    if viewing_zone_specs:
        target_plan = _viewing_zone_schedule(viewing_zone_specs, visitor_count, rng_seed=seed)
    else:
        target_plan = None

    dynamic_route_choice = bool(hotspot.get("route_choice", {}).get("enabled", False))
    zone_templates = None
    legacy_templates = None
    if len(target_edges) > 1:
        target_distribution = hotspot.get("target_distribution", "pedestrian_area_weighted")
        if target_distribution not in {"pedestrian_area_weighted", "weighted_viewing_arcs"}:
            raise ValueError(f"unsupported multi-edge target distribution: {target_distribution}")
        if target_distribution == "weighted_viewing_arcs" and not viewing_zone_specs:
            raise ValueError("weighted_viewing_arcs requires viewing_zones")
        zone_templates = _build_zone_templates(network, position_router, hotspot, target_edges)
        missing = [edge_id for edge_id in target_edges if not zone_templates.get(edge_id)]
        if missing:
            raise ValueError(f"no valid visitor itinerary for target edges: {', '.join(missing)}")
        if target_plan is None:
            target_schedule = _weighted_schedule(target_areas, visitor_count, rng_seed=seed)
            target_plan = [(edge_id, None, None) for edge_id in target_schedule]
        spawn_schedule = _spawn_schedule(network, hotspot, visitor_count, rng_seed=seed)
        destination_schedule = _destination_schedule(
            network,
            hotspot,
            visitor_count,
            rng_seed=seed,
        )
        template_count = sum(len(items) for items in zone_templates.values())
        incoming_directions = {
            item["entry_edge"] for items in zone_templates.values() for item in items
            if item["entry_edge"] is not None
        }
    else:
        legacy_templates, incoming_directions = _legacy_templates(
            source_people,
            network,
            target_edge,
            float(hotspot.get("approach_max_distance_meters", 700.0)),
        )
        if target_plan is None:
            target_plan = [(target_edge, None, None)] * visitor_count
        spawn_schedule = []
        destination_schedule = []
        template_count = len(legacy_templates)

    rng = random.Random(seed)
    background = _even_sample(source_people, background_count)
    background_window = hotspot.get("background_departure_window_seconds", [0.0, 1200.0])
    if not isinstance(background_window, list) or len(background_window) != 2:
        raise ValueError("background_departure_window_seconds must contain [start, end]")
    background_start, background_end = map(float, background_window)
    if background_start < 0 or background_end < background_start:
        raise ValueError("invalid background departure window")
    _remap_departures(background, background_start, background_end)
    visitors = []
    target_counts = {edge_id: 0 for edge_id in target_edges}
    viewing_zone_counts = Counter()
    viewing_zone_positions = {item["id"]: [] for item in viewing_zone_specs}
    entry_counts = {edge_id: 0 for edge_id in _access_portal_edges(hotspot)}
    park_entry_counts = {edge_id: 0 for edge_id in hotspot.get("park_entry_edges", ())}
    planned_departure_times = []
    planned_arrival_times = []
    departure_clamped_count = 0
    planned_visit_end_times = []
    approach_distances = []
    park_approach_distances = []
    spawn_edge_counts = Counter()
    spawn_positions = {edge_id: [] for edge_id in hotspot.get("visitor_spawn_edges", ())}
    destination_edge_counts = Counter()
    destination_positions = {
        edge_id: [] for edge_id in hotspot.get("visitor_destination_edges", ())
    }
    departure_entry_counts = Counter()
    same_spawn_destination_edge_count = 0
    reference_speed = float(hotspot["reference_walking_speed_mps"])
    if not 0 < reference_speed:
        raise ValueError("invalid hotspot timing configuration")
    activity_window = hotspot.get("activity_window_seconds")
    release_delay_window = hotspot.get("visitor_release_delay_seconds", [0.0, 0.0])
    release_schedule = None
    dwell_start = dwell_end = None
    requirement_activity = hotspot.get("requirement_activity")
    if requirement_activity is not None:
        event_start, event_end = requirement_activity["start_seconds"], requirement_activity["end_seconds"]
        if event_end is not None:
            release_schedule = [event_end] * visitor_count
    elif activity_window is not None:
        event_start, event_end = _time_window(activity_window, "activity_window_seconds")
        release_delay_start, release_delay_end = _time_window(
            release_delay_window,
            "visitor_release_delay_seconds",
        )
        release_schedule = _progressive_time_schedule(
            event_end + release_delay_start,
            event_end + release_delay_end,
            visitor_count,
            rng_seed=seed ^ 0x6E21,
        )
        random.Random(seed ^ 0x48F3).shuffle(release_schedule)
    else:
        event_start = event_end = None
        dwell_start, dwell_end = map(float, hotspot["dwell_seconds"])
        if dwell_start <= 0.0 or dwell_end < dwell_start:
            raise ValueError("invalid hotspot dwell_seconds")
    direct_departure_window = hotspot.get("visitor_departure_window_seconds")
    arrival_profile = hotspot.get("visitor_arrival_profile")
    if direct_departure_window is not None and arrival_profile is not None:
        raise ValueError(
            "visitor_departure_window_seconds and visitor_arrival_profile are mutually exclusive"
        )
    arrival_schedule = None
    configured_arrival_profile = None
    if arrival_profile is not None:
        arrival_schedule, configured_arrival_profile = _arrival_profile_schedule(
            arrival_profile,
            visitor_count,
            rng_seed=seed,
        )
        arrival_start = min(arrival_schedule) if arrival_schedule else None
        arrival_end = max(arrival_schedule) if arrival_schedule else None
        departure_schedule = None
        visitor_schedule_mode = "target_arrival_profile"
    elif direct_departure_window is None:
        arrival_window = hotspot.get("arrival_window_seconds")
        if not isinstance(arrival_window, list) or len(arrival_window) != 2:
            raise ValueError(
                "hotspot requires visitor_departure_window_seconds or arrival_window_seconds"
            )
        arrival_start, arrival_end = map(float, arrival_window)
        if arrival_start < 0.0 or arrival_end < arrival_start:
            raise ValueError("invalid arrival_window_seconds")
        departure_schedule = None
        visitor_schedule_mode = "target_arrival_window"
    else:
        arrival_start = arrival_end = None
        if not isinstance(direct_departure_window, list) or len(direct_departure_window) != 2:
            raise ValueError("visitor_departure_window_seconds must contain [start, end]")
        departure_start, departure_end = map(float, direct_departure_window)
        if (
            not math.isfinite(departure_start)
            or not math.isfinite(departure_end)
            or departure_start < 0.0
            or departure_end < departure_start
        ):
            raise ValueError("invalid visitor_departure_window_seconds")
        departure_schedule = _progressive_time_schedule(
            departure_start,
            departure_end,
            visitor_count,
            rng_seed=seed,
        )
        visitor_schedule_mode = "direct_departure_window"

    for index, (assigned_target, configured_position, viewing_zone_id) in enumerate(target_plan):
        edge = network.getEdge(assigned_target)
        margin = min(2.0, edge.getLength() * 0.25)
        stop_position = (
            configured_position
            if configured_position is not None
            else rng.uniform(margin, max(margin, edge.getLength() - margin))
        )
        if zone_templates is not None:
            spawn_edge, depart_position = spawn_schedule[index]
            destination_edge = destination_position = None
            if destination_schedule:
                destination_edge, destination_position = destination_schedule[index]
            itinerary = _select_zone_itinerary(
                network,
                position_router,
                hotspot,
                assigned_target,
                stop_position,
                spawn_edge,
                depart_position,
                destination_edge=destination_edge,
                destination_position=destination_position,
            )
            inbound_edges = itinerary["inbound_edges"]
            outbound_edges = itinerary["outbound_edges"]
            outbound_arrival_position = itinerary["outbound_arrival_position"]
            prefix_distance = itinerary["approach_distance"]
            selected_entry = itinerary["entry_edge"]
            selected_park_entry = itinerary["park_entry_edge"]
            spawn_edge_counts[spawn_edge] += 1
            spawn_positions[spawn_edge].append(depart_position)
            selected_destination = itinerary["destination_edge"]
            if selected_destination is not None:
                destination_edge_counts[selected_destination] += 1
                destination_positions[selected_destination].append(outbound_arrival_position)
                if selected_destination == spawn_edge:
                    same_spawn_destination_edge_count += 1
            for entry_edge in _access_portal_edges(hotspot):
                if entry_edge in outbound_edges:
                    departure_entry_counts[entry_edge] += 1
            approach_distances.append(prefix_distance)
            park_approach_distances.append(itinerary["park_approach_distance"])
        else:
            edges, target_index, prefix_distance = legacy_templates[index % len(legacy_templates)]
            inbound_edges = tuple(edges[: target_index + 1])
            outbound_edges = tuple(edges[target_index:])
            outbound_arrival_position = None
            depart_position = None
            selected_entry = None
            selected_park_entry = None
            selected_destination = None
        if departure_schedule is None:
            planned_arrival = (
                arrival_schedule[index]
                if arrival_schedule is not None
                else rng.uniform(arrival_start, arrival_end)
            )
            unconstrained_departure = planned_arrival - prefix_distance / reference_speed
            depart = max(0.0, unconstrained_departure)
            if unconstrained_departure < 0.0:
                departure_clamped_count += 1
        else:
            # Keep subsequent stop-position and dwell samples stable relative to
            # the earlier arrival-window generator while departure is now direct.
            rng.random()
            depart = departure_schedule[index]
            planned_arrival = depart + prefix_distance / reference_speed
        planned_departure_times.append(depart)
        planned_arrival_times.append(planned_arrival)
        duration = None if requirement_activity is not None or release_schedule is not None else rng.uniform(dwell_start, dwell_end)
        planned_release = release_schedule[index] if release_schedule is not None else None
        if planned_release is not None or duration is not None:
            planned_visit_end_times.append(planned_release if planned_release is not None else planned_arrival + duration)
        person = ET.Element("person", {
            "id": f"hotspot.{hotspot_id}.{index:04d}",
            "depart": f"{depart:.2f}",
            **({"departPos": f"{depart_position:.2f}"} if depart_position is not None else {}),
        })
        lock_key = GOAL_LOCK_PARAM if dynamic_route_choice else LOCK_PARAM
        ET.SubElement(person, "param", {"key": lock_key, "value": "true"})
        ET.SubElement(person, "param", {"key": "crowdsim.cohort", "value": "hotspot_visitor"})
        ET.SubElement(person, "param", {"key": HOTSPOT_ID_PARAM, "value": hotspot_id})
        ET.SubElement(person, "param", {"key": HOTSPOT_TARGET_EDGE_PARAM, "value": assigned_target})
        if planned_release is not None:
            ET.SubElement(person, "param", {
                "key": HOTSPOT_RELEASE_TIME_PARAM,
                "value": f"{planned_release:.2f}",
            })
        elif duration is not None:
            ET.SubElement(person, "param", {
                "key": HOTSPOT_DWELL_SECONDS_PARAM,
                "value": f"{duration:.2f}",
            })
        if selected_park_entry is not None:
            ET.SubElement(person, "param", {
                "key": HOTSPOT_PARK_ENTRY_EDGE_PARAM,
                "value": selected_park_entry,
            })
        if selected_entry is not None:
            ET.SubElement(person, "param", {
                "key": HOTSPOT_ENTRY_EDGE_PARAM,
                "value": selected_entry,
            })
        ET.SubElement(person, "walk", {
            "edges": " ".join(inbound_edges),
            "arrivalPos": f"{stop_position:.2f}",
        })
        # Keep the visitor inside SUMO's striping walking model at the activity
        # boundary.  The runtime holds speed at zero until the configured
        # relative or absolute release deadline; a SUMO <stop> would move the
        # person 3m to the roadside.
        outbound_attributes = {"edges": " ".join(outbound_edges)}
        if outbound_arrival_position is not None:
            outbound_attributes["arrivalPos"] = f"{outbound_arrival_position:.2f}"
        ET.SubElement(person, "walk", outbound_attributes)
        visitors.append(person)
        target_counts[assigned_target] += 1
        if viewing_zone_id is not None:
            viewing_zone_counts[viewing_zone_id] += 1
            viewing_zone_positions[viewing_zone_id].append(stop_position)
        if selected_entry is not None:
            entry_counts[selected_entry] += 1
        if selected_park_entry is not None:
            park_entry_counts[selected_park_entry] += 1

    # Shift the completed schedule, not its inputs: route/position choices and
    # all random draws remain identical. Never trim a background population or
    # move an earlier activity/profile boundary to a negative simulation time.
    unshifted_departure_window = _schedule_window(planned_departure_times)
    effective_activity_window = list(activity_window) if activity_window is not None else None
    effective_arrival_profile = copy.deepcopy(configured_arrival_profile)
    effective_departure_window = list(direct_departure_window) if direct_departure_window is not None else None
    effective_arrival_window = (
        [arrival_start, arrival_end]
        if departure_schedule is None and arrival_profile is None else None
    )
    time_boundaries = [*planned_arrival_times, *planned_visit_end_times]
    for window in (effective_activity_window, effective_departure_window, effective_arrival_window):
        if window is not None:
            time_boundaries.extend(window)
    for segment in effective_arrival_profile or ():
        time_boundaries.extend(segment["window_seconds"])
    timeline_shift, alignment_status = _timeline_alignment(
        align_to_zero, planned_departure_times, bool(background), time_boundaries,
    )
    if timeline_shift:
        for values in (planned_departure_times, planned_arrival_times, planned_visit_end_times, release_schedule):
            if values is not None:
                values[:] = [value - timeline_shift for value in values]
        for window in (effective_activity_window, effective_departure_window, effective_arrival_window):
            if window is not None:
                window[:] = [value - timeline_shift for value in window]
        for segment in effective_arrival_profile or ():
            segment["window_seconds"] = [value - timeline_shift for value in segment["window_seconds"]]
        for index, person in enumerate(visitors):
            # Use the unrounded schedule, not the already formatted XML values.
            person.set("depart", f"{planned_departure_times[index]:.2f}")
            if release_schedule is not None:
                for param in person.findall("param"):
                    if param.get("key") == HOTSPOT_RELEASE_TIME_PARAM:
                        param.set("value", f"{release_schedule[index]:.2f}")

    for person in source_people:
        source_root.remove(person)
    combined = background + visitors
    combined.sort(key=lambda person: (float(person.get("depart", 0)), person.get("id", "")))
    for person in combined:
        source_root.append(person)
    source_root.insert(0, ET.Comment(
        f"background={len(background)} background_depart={background_start:.1f}-{background_end:.1f}s "
        f"hotspot={hotspot_id} visitors={len(visitors)} seed={seed}; "
        "hotspot timing and volume are engineering assumptions pending Bund calibration"
    ))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(tree, space="    ")
    tree.write(output_path, encoding="utf-8", xml_declaration=True)
    return {
        **({"requirement_activity": copy.deepcopy(requirement_activity)} if requirement_activity is not None else {}),
        "hotspot_id": hotspot_id,
        "target_edge": target_edge,
        "target_edges": list(target_edges),
        "target_edge_counts": target_counts,
        "viewing_zone_counts": dict(viewing_zone_counts),
        "viewing_zone_position_range": {
            zone_id: [min(values), max(values)] if values else [None, None]
            for zone_id, values in viewing_zone_positions.items()
        },
        "entry_counts": entry_counts,
        "park_entry_counts": park_entry_counts,
        "spawn_edge_counts": dict(spawn_edge_counts),
        "spawn_distribution": hotspot.get("spawn_distribution", "edge_start"),
        "initial_population": direct_departure_window == [0, 0] and not background,
        "spawn_position_range_by_edge": {
            edge_id: [min(values), max(values)] if values else [None, None]
            for edge_id, values in spawn_positions.items()
        },
        "destination_distribution": (
            hotspot.get("destination_distribution")
            if hotspot.get("visitor_destination_edges")
            else None
        ),
        "destination_edge_counts": dict(destination_edge_counts),
        "destination_position_range_by_edge": {
            edge_id: [min(values), max(values)] if values else [None, None]
            for edge_id, values in destination_positions.items()
        },
        "departure_entry_counts": dict(departure_entry_counts),
        "same_spawn_destination_edge_count": same_spawn_destination_edge_count,
        "background_count": len(background),
        "background_departure_window_seconds": [background_start, background_end],
        "visitor_schedule_mode": visitor_schedule_mode,
        "align_first_visitor_to_zero": align_to_zero,
        "timeline_shift_seconds": timeline_shift,
        "timeline_alignment_status": alignment_status,
        "unshifted_planned_departure_window_seconds": unshifted_departure_window,
        "configured_visitor_departure_window_seconds": (
            list(map(float, direct_departure_window))
            if direct_departure_window is not None
            else None
        ),
        "configured_visitor_arrival_profile": configured_arrival_profile,
        "configured_activity_window_seconds": list(activity_window) if activity_window is not None else None,
        "visitor_departure_window_seconds": effective_departure_window,
        "visitor_arrival_profile": effective_arrival_profile,
        "arrival_window_seconds": effective_arrival_window,
        "activity_window_seconds": effective_activity_window,
        "visitor_release_window_seconds": (
            [min(release_schedule), max(release_schedule)] if release_schedule else None
        ),
        "visitor_count": len(visitors),
        "total_count": len(combined),
        "template_count": template_count,
        "incoming_direction_count": len(incoming_directions),
        "approach_distance_meters": _range_summary(approach_distances),
        "park_approach_distance_meters": _range_summary(park_approach_distances),
        "estimated_park_entry_time_seconds": _range_summary([
            distance / reference_speed for distance in park_approach_distances
        ]),
        "planned_departure_window_seconds": [
            min(planned_departure_times) if planned_departure_times else None,
            max(planned_departure_times) if planned_departure_times else None,
        ],
        "departure_clamped_count": departure_clamped_count,
        "configured_arrival_window_seconds": (
            [arrival_start, arrival_end]
            if departure_schedule is None and arrival_profile is None
            else None
        ),
        "planned_arrival_window_seconds": [
            min(planned_arrival_times) if planned_arrival_times else None,
            max(planned_arrival_times) if planned_arrival_times else None,
        ],
        "planned_visit_end_window_seconds": [
            min(planned_visit_end_times) if planned_visit_end_times else None,
            max(planned_visit_end_times) if planned_visit_end_times else None,
        ],
        "dwell_model": (
            "common_event_release_hold" if release_schedule is not None
            else "walking_stage_speed_hold"
        ),
        "output": str(output_path.resolve()),
        "parameter_status": payload.get("parameter_status", "unspecified"),
    }


def _schedule_window(values):
    return [min(values), max(values)] if values else [None, None]


def _timeline_alignment(enabled, departures, has_background, time_boundaries):
    """Only remove empty time; preserve earlier configured events and all gaps."""
    if not enabled:
        return 0.0, "disabled"
    if not departures:
        return 0.0, "no_visitors"
    if has_background:
        return 0.0, "background_present"
    first = min(departures)
    if first == 0.0:
        return 0.0, "already_aligned"
    shift = min([first, *time_boundaries])
    return shift, "aligned" if shift == first else "limited_by_earlier_time"


def _legacy_templates(source_people, network, target_edge: str, approach_limit: float):
    templates = []
    for person in source_people:
        walk = person.find("walk")
        edges = walk.get("edges", "").split() if walk is not None else []
        if target_edge not in edges:
            continue
        target_index = edges.index(target_edge)
        if target_index == 0 or target_index >= len(edges) - 1:
            continue
        start_index = target_index - 1
        prefix_distance = network.getEdge(target_edge).getLength() + network.getEdge(edges[start_index]).getLength()
        while start_index > 0:
            candidate_length = network.getEdge(edges[start_index - 1]).getLength()
            if prefix_distance + candidate_length > approach_limit:
                break
            start_index -= 1
            prefix_distance += candidate_length
        trimmed_edges = edges[start_index:]
        templates.append((trimmed_edges, target_index - start_index, prefix_distance))
    incoming_directions = {(edges[index - 1], edges[index + 1]) for edges, index, _ in templates}
    if not templates or len(incoming_directions) < 2:
        raise ValueError("source demand needs short, valid hotspot routes from at least two directions")
    return templates, incoming_directions


def _time_window(raw, field: str) -> tuple[float, float]:
    if not isinstance(raw, list) or len(raw) != 2:
        raise ValueError(f"{field} must contain [start, end]")
    start, end = map(float, raw)
    if not math.isfinite(start) or not math.isfinite(end) or start < 0.0 or end < start:
        raise ValueError(f"invalid {field}")
    return start, end


def _arrival_profile_schedule(raw, count: int, *, rng_seed: int):
    if not isinstance(raw, list) or not raw:
        raise ValueError("visitor_arrival_profile must be a non-empty list")
    windows = []
    weights = {}
    previous_end = None
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError("visitor_arrival_profile entries must be objects")
        start, end = _time_window(item.get("window_seconds"), "visitor_arrival_profile.window_seconds")
        if previous_end is not None and start < previous_end:
            raise ValueError("visitor_arrival_profile windows must be ordered and non-overlapping")
        previous_end = end
        weight = float(item.get("fraction", 0.0))
        if not math.isfinite(weight) or weight <= 0.0:
            raise ValueError("visitor_arrival_profile fractions must be positive")
        key = str(index)
        weights[key] = weight
        windows.append((start, end, weight))
    allocation = Counter(_weighted_schedule(weights, count, rng_seed=rng_seed ^ 0x19A7))
    schedule = []
    configured = []
    total_weight = sum(weights.values())
    for index, (start, end, weight) in enumerate(windows):
        segment_count = allocation[str(index)]
        schedule.extend(_progressive_time_schedule(
            start,
            end,
            segment_count,
            rng_seed=rng_seed ^ (0x34D1 + index * 977),
        ))
        configured.append({
            "window_seconds": [start, end],
            "fraction": weight / total_weight,
            "count": segment_count,
        })
    return sorted(schedule), configured


def _viewing_zone_specs(network, hotspot: dict, target_edges: tuple[str, ...]) -> list[dict]:
    raw_zones = hotspot.get("viewing_zones")
    if raw_zones is None:
        return []
    if not isinstance(raw_zones, list) or not raw_zones:
        raise ValueError("viewing_zones must be a non-empty list")
    result = []
    seen_ids = set()
    for raw in raw_zones:
        if not isinstance(raw, dict):
            raise ValueError("viewing_zones entries must be objects")
        zone_id = str(raw.get("id", "")).strip()
        edge_id = str(raw.get("edge", "")).strip()
        if not zone_id or zone_id in seen_ids:
            raise ValueError("viewing zone ids must be non-empty and unique")
        if edge_id not in target_edges:
            raise ValueError(f"viewing zone {zone_id} edge must belong to target_edges")
        start, end = _time_window(raw.get("position_range_meters"), "position_range_meters")
        edge_length = float(network.getEdge(edge_id).getLength())
        if start >= end or end > edge_length:
            raise ValueError(f"viewing zone {zone_id} position range exceeds edge {edge_id}")
        weight = float(raw.get("weight", 0.0))
        if not math.isfinite(weight) or weight <= 0.0:
            raise ValueError(f"viewing zone {zone_id} weight must be positive")
        seen_ids.add(zone_id)
        result.append({
            "id": zone_id,
            "edge": edge_id,
            "start": start,
            "end": end,
            "weight": weight,
        })
    return result


def _viewing_zone_schedule(zones: list[dict], count: int, *, rng_seed: int):
    zone_by_id = {item["id"]: item for item in zones}
    zone_ids = _weighted_schedule(
        {item["id"]: item["weight"] for item in zones},
        count,
        rng_seed=rng_seed ^ 0x52B9,
    )
    counts = Counter(zone_ids)
    rng = random.Random(rng_seed ^ 0x70C3)
    positions = {}
    for zone_id, zone_count in counts.items():
        zone = zone_by_id[zone_id]
        length = zone["end"] - zone["start"]
        values = [
            zone["start"] + (index + rng.random()) * length / zone_count
            for index in range(zone_count)
        ]
        rng.shuffle(values)
        positions[zone_id] = iter(values)
    return [
        (zone_by_id[zone_id]["edge"], next(positions[zone_id]), zone_id)
        for zone_id in zone_ids
    ]


def _spawn_schedule(network, hotspot: dict, count: int, *, rng_seed: int) -> list[tuple[str, float]]:
    spawn_edges = tuple(str(item) for item in hotspot.get("visitor_spawn_edges", ()))
    if not spawn_edges and count:
        raise ValueError("hotspot demand requires visitor_spawn_edges")
    margin = float(hotspot.get("spawn_position_margin_meters", 0.0))
    distribution = str(hotspot.get("spawn_distribution", "edge_start"))
    # Uniform scene initialization includes short connector edges. Keep their
    # endpoints clear without deleting them from the physical spawn domain.
    margins = {
        edge_id: min(margin, network.getEdge(edge_id).getLength() * 0.25)
        if distribution == "edge_length_uniform" else margin
        for edge_id in spawn_edges
    }
    usable_lengths = {
        edge_id: network.getEdge(edge_id).getLength() - 2.0 * margins[edge_id]
        for edge_id in spawn_edges
    }
    if any(length <= 0.0 for length in usable_lengths.values()):
        raise ValueError("spawn_position_margin_meters leaves no usable spawn edge length")
    weights = (
        {edge_id: network.getEdge(edge_id).getLength() for edge_id in spawn_edges}
        if distribution == "edge_length_uniform" else usable_lengths
    )
    edge_schedule = _weighted_schedule(weights, count, rng_seed=rng_seed ^ 0x31B7)
    counts = Counter(edge_schedule)
    rng = random.Random(rng_seed ^ 0x7C45)
    positions = {}
    for edge_id, edge_count in counts.items():
        usable = usable_lengths[edge_id]
        if distribution in {"edge_length_weighted_random", "edge_length_uniform"}:
            edge_positions = [
                margins[edge_id] + (index + rng.random()) * usable / edge_count
                for index in range(edge_count)
            ]
            rng.shuffle(edge_positions)
        elif distribution == "edge_start":
            edge_positions = [margins[edge_id]] * edge_count
        else:
            raise ValueError(f"unsupported spawn_distribution: {distribution}")
        positions[edge_id] = iter(edge_positions)
    return [(edge_id, next(positions[edge_id])) for edge_id in edge_schedule]


def _destination_schedule(
    network,
    hotspot: dict,
    count: int,
    *,
    rng_seed: int,
) -> list[tuple[str, float]]:
    destination_edges = tuple(
        str(item) for item in hotspot.get("visitor_destination_edges", ())
    )
    if not destination_edges:
        return []
    margin = float(hotspot.get("destination_position_margin_meters", 0.0))
    distribution = str(hotspot.get("destination_distribution", "edge_uniform_random"))
    usable_lengths = {
        edge_id: network.getEdge(edge_id).getLength() - 2.0 * margin
        for edge_id in destination_edges
    }
    if any(length <= 0.0 for length in usable_lengths.values()):
        raise ValueError("destination_position_margin_meters leaves no usable destination edge length")
    if distribution == "edge_uniform_random":
        weights = {edge_id: 1.0 for edge_id in destination_edges}
        # Splitting one physical destination road must not double its quota.
        # Explicit segment weights preserve its original aggregate weight.
        configured = hotspot.get("destination_edge_weights")
        if configured is not None:
            if not isinstance(configured, dict) or set(configured) != set(destination_edges):
                raise ValueError("destination_edge_weights must cover exactly visitor_destination_edges")
            weights = {edge_id: float(configured[edge_id]) for edge_id in destination_edges}
            if any(not math.isfinite(weight) or weight <= 0 for weight in weights.values()):
                raise ValueError("destination_edge_weights must be positive and finite")
    elif distribution == "edge_length_weighted_random":
        weights = usable_lengths
    else:
        raise ValueError(f"unsupported destination_distribution: {distribution}")
    edge_schedule = _weighted_schedule(weights, count, rng_seed=rng_seed ^ 0x6C3D)
    counts = Counter(edge_schedule)
    rng = random.Random(rng_seed ^ 0x2B91)
    positions = {}
    for edge_id, edge_count in counts.items():
        usable = usable_lengths[edge_id]
        edge_positions = [
            margin + (index + rng.random()) * usable / edge_count
            for index in range(edge_count)
        ]
        rng.shuffle(edge_positions)
        positions[edge_id] = iter(edge_positions)
    return [(edge_id, next(positions[edge_id])) for edge_id in edge_schedule]


def _progressive_time_schedule(
    start: float,
    end: float,
    count: int,
    *,
    rng_seed: int,
) -> list[float]:
    """Spread departures across the full window with stable, small jitter.

    The first and last visitors use the configured endpoints. Intermediate
    departures are jittered around an even cadence so the demand begins at the
    start boundary without inserting the full crowd in one simulation step.
    """
    if count <= 0:
        return []
    if count == 1 or end == start:
        return [start] * count
    interval = (end - start) / (count - 1)
    rng = random.Random(rng_seed ^ 0x2D91)
    schedule = [start]
    for index in range(1, count - 1):
        jitter = (rng.random() - 0.5) * interval * 0.4
        schedule.append(start + index * interval + jitter)
    schedule.append(end)
    return sorted(schedule)


def _select_zone_itinerary(
    network,
    router: PositionAwarePedestrianRouter,
    hotspot: dict,
    target_edge: str,
    target_position: float,
    spawn_edge: str,
    depart_position: float,
    *,
    destination_edge: str | None = None,
    destination_position: float | None = None,
) -> dict:
    """Choose an approach from the park, a portal, the ring or outside."""
    park_access_edges = set(_approach_edges(hotspot))
    entry_edges = _access_portal_edges(hotspot)
    excluded_edges = set(map(str, hotspot.get("excluded_edges", ())))
    core_spawn = spawn_edge in set(hotspot.get("target_edges", ())) | set(entry_edges)
    if not entry_edges or core_spawn:
        try:
            if core_spawn:
                direct = _core_spawn_route(
                    router, hotspot, spawn_edge, depart_position, target_edge, target_position,
                )
            else:
                direct = router.route(
                    EdgePosition(spawn_edge, depart_position),
                    EdgePosition(target_edge, target_position),
                    forbidden_edges=excluded_edges,
                )
        except PositionRouteUnavailable as exc:
            raise ValueError(
                f"no valid route from {spawn_edge}@{depart_position:.2f} "
                f"to {target_edge}@{target_position:.2f}"
            ) from exc
        selected = {
            "inbound_edges": direct.edges,
            "park_entry_edge": None,
            "entry_edge": spawn_edge if spawn_edge in entry_edges else None,
            "park_approach_distance": 0.0,
            "approach_distance": direct.distance_m,
        }
    elif spawn_edge in park_access_edges:
        candidates = []
        for entry_edge in entry_edges:
            try:
                route = router.route_via_edges(
                    EdgePosition(spawn_edge, depart_position),
                    EdgePosition(target_edge, target_position),
                    (entry_edge,),
                    via_orientations=(_portal_orientation(hotspot, entry_edge),),
                    forbidden_edges=excluded_edges | (set(entry_edges) - {entry_edge}),
                )
            except PositionRouteUnavailable:
                continue
            inbound = route.edges
            if not _valid_internal_inbound(inbound, spawn_edge, entry_edge, hotspot):
                continue
            candidates.append({
                "inbound_edges": inbound,
                "park_entry_edge": None,
                "entry_edge": entry_edge,
                "park_approach_distance": 0.0,
                "approach_distance": route.distance_m,
            })
        if not candidates:
            raise ValueError(
                f"no valid internal route from {spawn_edge}@{depart_position:.2f} "
                f"to {target_edge}@{target_position:.2f}"
            )
        selected = min(
            candidates,
            key=lambda item: (item["approach_distance"], item["entry_edge"]),
        )
    else:
        candidates_by_gate = {}
        for park_entry_edge in map(str, hotspot.get("park_entry_edges", ())):
            park_position = _edge_end_position(network, park_entry_edge)
            outside, outside_cost = _pedestrian_path(
                router,
                spawn_edge,
                park_entry_edge,
                from_pos=depart_position,
                to_pos=park_position,
            )
            if not outside or not _valid_park_approach(outside, park_entry_edge, hotspot):
                continue
            for entry_edge in entry_edges:
                try:
                    full_route = router.route_via_edges(
                        EdgePosition(spawn_edge, depart_position),
                        EdgePosition(target_edge, target_position),
                        (park_entry_edge, entry_edge),
                        via_orientations=(None, _portal_orientation(hotspot, entry_edge)),
                        forbidden_edges=excluded_edges
                        | (set(entry_edges) - {entry_edge})
                        | (set(map(str, hotspot.get("park_entry_edges", ()))) - {park_entry_edge}),
                    )
                except PositionRouteUnavailable:
                    continue
                inbound = full_route.edges
                if not _valid_inbound(inbound, park_entry_edge, entry_edge, hotspot):
                    continue
                candidates_by_gate.setdefault(park_entry_edge, []).append({
                    "inbound_edges": inbound,
                    "park_entry_edge": park_entry_edge,
                    "entry_edge": entry_edge,
                    "park_approach_distance": outside_cost,
                    "approach_distance": full_route.distance_m,
                })
        if not candidates_by_gate:
            raise ValueError(
                f"no valid route from {spawn_edge}@{depart_position:.2f} "
                f"to {target_edge}@{target_position:.2f}"
            )
        selected_gate = min(
            candidates_by_gate,
            key=lambda edge_id: (candidates_by_gate[edge_id][0]["park_approach_distance"], edge_id),
        )
        selected = min(
            candidates_by_gate[selected_gate],
            key=lambda item: (item["approach_distance"], item["entry_edge"]),
        )

    if destination_edge is not None:
        if destination_position is None:
            raise ValueError("destination_position is required with destination_edge")
        route, cost = _pedestrian_path(
            router,
            target_edge,
            destination_edge,
            from_pos=target_position,
            to_pos=destination_position,
        )
        if not route or set(route) & set(hotspot.get("excluded_edges", ())):
            raise ValueError(
                f"no valid route from {target_edge}@{target_position:.2f} "
                f"to destination {destination_edge}@{destination_position:.2f}"
            )
        outbound = route
        outbound_position = destination_position
        outbound_destination_edge = destination_edge
    else:
        outbound_options = []
        margin = float(hotspot.get("spawn_position_margin_meters", 0.0))
        for exit_edge in map(str, hotspot.get("visitor_exit_edges", ())):
            exit_length = network.getEdge(exit_edge).getLength()
            exit_position = max(margin, min(exit_length - margin, exit_length * 0.5))
            route, cost = _pedestrian_path(
                router,
                target_edge,
                exit_edge,
                from_pos=target_position,
                to_pos=exit_position,
            )
            if route and not (set(route) & set(hotspot.get("excluded_edges", ()))):
                outbound_options.append((cost, exit_edge, route, exit_position))
        if not outbound_options:
            raise ValueError(f"no valid exit route from {target_edge}@{target_position:.2f}")
        # Legacy hotspots select their nearest external observation boundary.
        _, outbound_destination_edge, outbound, outbound_position = min(outbound_options)
    return {
        **selected,
        "outbound_edges": outbound,
        "outbound_arrival_position": outbound_position,
        "destination_edge": outbound_destination_edge,
    }


def _edge_end_position(network, edge_id: str) -> float:
    length = network.getEdge(edge_id).getLength()
    return max(0.0, length - min(0.01, length * 0.5))


def _core_spawn_route(router, hotspot, spawn_edge, depart_position, target_edge, target_position):
    # A person already on the ring must stay on it. A person on a portal
    # traverses only its remaining inward segment before following the ring.
    allowed = set(map(str, hotspot["target_edges"])) | {spawn_edge}
    return router.route(
        EdgePosition(spawn_edge, depart_position),
        EdgePosition(target_edge, target_position),
        forbidden_edges={edge for edge in router.edge_lengths if not edge.startswith(":")} - allowed,
    )


def _build_zone_templates(
    network,
    router: PositionAwarePedestrianRouter,
    hotspot: dict,
    target_edges: tuple[str, ...],
) -> dict[str, list[dict]]:
    spawn_edges = tuple(str(item) for item in hotspot.get("visitor_spawn_edges", ()))
    exit_edges = tuple(str(item) for item in (
        hotspot.get("visitor_destination_edges") or hotspot.get("visitor_exit_edges", ())
    ))
    entry_edges = _access_portal_edges(hotspot)
    park_entry_edges = tuple(str(item) for item in hotspot.get("park_entry_edges", ()))
    excluded = set(str(item) for item in hotspot.get("excluded_edges", ()))
    if not spawn_edges or not exit_edges:
        raise ValueError("multi-edge hotspot demand requires spawn and destination edges")
    park_access_edges = set(_approach_edges(hotspot))
    internal_edges = park_access_edges | set(entry_edges) | set(target_edges)
    if any(edge_id not in internal_edges for edge_id in spawn_edges) and not park_entry_edges:
        raise ValueError("external hotspot spawn edges require park entry edges")
    templates = {target_edge: [] for target_edge in target_edges}
    for target_edge in target_edges:
        outbound_options = []
        for exit_edge in exit_edges:
            route, _ = _pedestrian_path(router, target_edge, exit_edge)
            if route and not (set(route) & excluded):
                outbound_options.append(route)
        if not outbound_options:
            continue
        option_index = 0
        for spawn_edge in spawn_edges:
            if spawn_edge in set(entry_edges) | set(target_edges):
                try:
                    direct = _core_spawn_route(
                        router, hotspot, spawn_edge, network.getEdge(spawn_edge).getLength() * 0.5,
                        target_edge, network.getEdge(target_edge).getLength() * 0.5,
                    )
                except PositionRouteUnavailable:
                    continue
                outbound = outbound_options[option_index % len(outbound_options)]
                option_index += 1
                templates[target_edge].append({
                    "inbound_edges": direct.edges,
                    "outbound_edges": outbound,
                    "park_entry_edge": None,
                    "entry_edge": spawn_edge if spawn_edge in entry_edges else None,
                    "spawn_edge": spawn_edge,
                    "exit_edge": outbound[-1],
                    "approach_distance": direct.distance_m,
                })
                continue
            if not entry_edges:
                inbound, inbound_cost = _pedestrian_path(router, spawn_edge, target_edge)
                if not inbound or set(inbound) & excluded:
                    continue
                outbound = outbound_options[option_index % len(outbound_options)]
                option_index += 1
                templates[target_edge].append({
                    "inbound_edges": inbound,
                    "outbound_edges": outbound,
                    "park_entry_edge": None,
                    "entry_edge": None,
                    "spawn_edge": spawn_edge,
                    "exit_edge": outbound[-1],
                    "approach_distance": inbound_cost,
                })
                continue
            if spawn_edge in park_access_edges:
                for entry_edge in entry_edges:
                    inside, _ = _pedestrian_path(router, spawn_edge, entry_edge)
                    target_leg, _ = _pedestrian_path(router, entry_edge, target_edge)
                    if not inside or not target_leg:
                        continue
                    inbound = _join_paths(inside, target_leg)
                    if not _valid_internal_inbound(inbound, spawn_edge, entry_edge, hotspot):
                        continue
                    outbound = outbound_options[option_index % len(outbound_options)]
                    option_index += 1
                    templates[target_edge].append({
                        "inbound_edges": inbound,
                        "outbound_edges": outbound,
                        "park_entry_edge": None,
                        "entry_edge": entry_edge,
                        "spawn_edge": spawn_edge,
                        "exit_edge": outbound[-1],
                        "approach_distance": sum(
                            network.getEdge(edge_id).getLength() for edge_id in inbound
                        ),
                    })
                continue
            for park_entry_edge in park_entry_edges:
                outside, _ = _pedestrian_path(router, spawn_edge, park_entry_edge)
                if not outside or not _valid_park_approach(outside, park_entry_edge, hotspot):
                    continue
                for entry_edge in entry_edges:
                    inside, _ = _pedestrian_path(router, park_entry_edge, entry_edge)
                    target_leg, _ = _pedestrian_path(router, entry_edge, target_edge)
                    if not inside or not target_leg:
                        continue
                    inbound = _join_paths(_join_paths(outside, inside), target_leg)
                    if not _valid_inbound(inbound, park_entry_edge, entry_edge, hotspot):
                        continue
                    outbound = outbound_options[option_index % len(outbound_options)]
                    option_index += 1
                    templates[target_edge].append({
                        "inbound_edges": inbound,
                        "outbound_edges": outbound,
                        "park_entry_edge": park_entry_edge,
                        "entry_edge": entry_edge,
                        "spawn_edge": spawn_edge,
                        "exit_edge": outbound[-1],
                        "approach_distance": sum(network.getEdge(edge_id).getLength() for edge_id in inbound),
                    })
    return templates


def _pedestrian_path(
    router: PositionAwarePedestrianRouter,
    first_edge: str,
    last_edge: str,
    *,
    from_pos: float | None = None,
    to_pos: float | None = None,
) -> tuple[tuple[str, ...], float]:
    first_position = 0.0 if from_pos is None else float(from_pos)
    last_position = router.edge_lengths[last_edge] if to_pos is None else float(to_pos)
    try:
        route = router.route(
            EdgePosition(first_edge, first_position),
            EdgePosition(last_edge, last_position),
        )
    except PositionRouteUnavailable:
        return (), float("inf")
    return route.edges, route.distance_m


def _access_portal_edges(hotspot: dict) -> tuple[str, ...]:
    portals = hotspot.get("access_portals")
    if portals is not None:
        return tuple(str(item["edge"]) for item in portals)
    return tuple(str(item) for item in hotspot.get("entry_edges", ()))


def _approach_edges(hotspot: dict) -> tuple[str, ...]:
    values = hotspot.get("approach_edges")
    if values is None:
        values = hotspot.get("park_access_edges", ())
    return tuple(str(item) for item in values)


def _portal_orientation(hotspot: dict, edge_id: str) -> int | None:
    """Return the configured outside-to-inside portal orientation, if fixed."""
    for portal in hotspot.get("access_portals", ()):
        if str(portal.get("edge")) != edge_id:
            continue
        outside = str(portal.get("outside_side", "auto"))
        inside = str(portal.get("inside_side", "auto"))
        if outside in {"start", "from"}:
            return 0
        if outside in {"end", "to"}:
            return 1
        if inside in {"start", "from"}:
            return 1
        if inside in {"end", "to"}:
            return 0
    return None


def _join_paths(first: tuple[str, ...], second: tuple[str, ...]) -> tuple[str, ...]:
    return first + second[1:] if first[-1] == second[0] else first + second


def _valid_park_approach(edges: tuple[str, ...], park_entry_edge: str, hotspot: dict) -> bool:
    if park_entry_edge not in edges:
        return False
    entry_index = edges.index(park_entry_edge)
    prefix = edges[:entry_index]
    if set(prefix) & set(_approach_edges(hotspot)):
        return False
    if any(edge_id not in set(hotspot.get("external_approach_edges", ())) for edge_id in prefix):
        return False
    other_entries = set(hotspot.get("park_entry_edges", ())) - {park_entry_edge}
    return not (set(edges) & other_entries)


def _valid_inbound(
    edges: tuple[str, ...],
    park_entry_edge: str,
    entry_edge: str,
    hotspot: dict,
) -> bool:
    if entry_edge not in edges or set(edges) & set(hotspot.get("excluded_edges", ())):
        return False
    if park_entry_edge not in edges or edges.index(park_entry_edge) >= edges.index(entry_edge):
        return False
    entry_index = edges.index(entry_edge)
    if set(edges[:entry_index]) & set(hotspot.get("target_edges", ())):
        return False
    other_entries = set(_access_portal_edges(hotspot)) - {entry_edge}
    if set(edges) & other_entries:
        return False
    park_index = edges.index(park_entry_edge)
    allowed_inside = set(_approach_edges(hotspot)) | {park_entry_edge}
    return all(edge_id in allowed_inside for edge_id in edges[park_index:entry_index])


def _valid_internal_inbound(
    edges: tuple[str, ...],
    spawn_edge: str,
    entry_edge: str,
    hotspot: dict,
) -> bool:
    """Validate a route that begins on an already-internal park edge."""
    if not edges or edges[0] != spawn_edge or entry_edge not in edges:
        return False
    if set(edges) & set(hotspot.get("excluded_edges", ())):
        return False
    entry_index = edges.index(entry_edge)
    if set(edges[:entry_index]) & set(hotspot.get("target_edges", ())):
        return False
    other_entries = set(_access_portal_edges(hotspot)) - {entry_edge}
    if set(edges) & other_entries:
        return False
    allowed_inside = set(_approach_edges(hotspot))
    return all(edge_id in allowed_inside for edge_id in edges[:entry_index])


def _weighted_schedule(weights: dict[str, float], count: int, *, rng_seed: int) -> list[str]:
    if count <= 0:
        return []
    total = sum(max(0.0, value) for value in weights.values())
    if total <= 0:
        raise ValueError("target pedestrian area must be positive")
    raw = {key: count * max(0.0, value) / total for key, value in weights.items()}
    allocation = {key: int(value) for key, value in raw.items()}
    remaining = count - sum(allocation.values())
    order = sorted(weights, key=lambda key: (-(raw[key] - allocation[key]), key))
    for key in order[:remaining]:
        allocation[key] += 1
    schedule = [key for key in weights for _ in range(allocation[key])]
    random.Random(rng_seed ^ 0x5A17).shuffle(schedule)
    return schedule


def _range_summary(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"minimum": None, "median": None, "maximum": None}
    return {
        "minimum": min(values),
        "median": statistics.median(values),
        "maximum": max(values),
    }


def _even_sample(items: list[ET.Element], count: int) -> list[ET.Element]:
    if count == 0:
        return []
    return [copy.deepcopy(items[min(len(items) - 1, int(index * len(items) / count))]) for index in range(min(count, len(items)))]


def _remap_departures(people: list[ET.Element], target_start: float, target_end: float) -> None:
    """Preserve the source demand shape while fitting it into the scenario window."""
    if not people:
        return
    departures = [float(person.get("depart", "0")) for person in people]
    source_start = min(departures)
    source_end = max(departures)
    source_span = source_end - source_start
    target_span = target_end - target_start
    for person, departure in zip(people, departures):
        fraction = 0.0 if source_span <= 0 else (departure - source_start) / source_span
        person.set("depart", f"{target_start + fraction * target_span:.2f}")
