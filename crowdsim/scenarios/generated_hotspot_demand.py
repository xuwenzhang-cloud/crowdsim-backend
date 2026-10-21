"""Generate one isolated, reproducible hotspot demand for a service run."""

from dataclasses import dataclass
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from crowdsim.scenarios.hotspot_demand import build_hotspot_demand


@dataclass(frozen=True)
class HotspotDemandSpec:
    source_path: Path
    config_path: Path
    hotspot_id: str | None = None
    seed: int = 20260908
    max_count: int = 10000

    def configuration(self):
        payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        hotspot_id = self.hotspot_id or payload.get("default_hotspot_id")
        hotspot = next((item for item in payload.get("hotspots", []) if item["id"] == hotspot_id), None)
        if hotspot is None:
            raise ValueError(f"unknown generated hotspot: {hotspot_id}")
        self.validate_count(hotspot["visitor_count"])
        return payload, hotspot

    def validate_count(self, count):
        if type(count) is not int or not 0 <= count <= self.max_count:
            raise ValueError(f"hotspot count must be an integer between 0 and {self.max_count}")

    def capabilities(self):
        _, hotspot = self.configuration()
        return {"default_count": hotspot["visitor_count"], "count_min": 0,
                "count_max": self.max_count, "count_step": 1,
                "count_scope": "total_hotspot_visitors", "background_count": 0,
                "hotspot_id": hotspot["id"]}

    def generate(self, directory, network_path, requested_count=None, *, activity=None, observation_scope=None):
        payload, hotspot = self.configuration()
        count = hotspot["visitor_count"] if requested_count is None else requested_count
        self.validate_count(count)
        if activity is not None:
            # Only the per-run copy changes; the shared scene and other entry
            # points retain their calibrated preset and random departure delays.
            hotspot["requirement_activity"] = deepcopy(activity)
            end = activity["end_seconds"]
            hotspot["activity_window_seconds"] = [activity["start_seconds"], end] if end is not None else None
            hotspot["visitor_release_delay_seconds"] = [0, 0] if end is not None else None
            hotspot["align_first_visitor_to_zero"] = False
            hotspot["visitor_destination_edges"] = _outside_destinations(network_path, hotspot, observation_scope)
            hotspot.pop("destination_edge_weights", None)
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        config_snapshot = directory / "hotspot.config.json"
        config_snapshot.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        output = directory / "hotspot.generated.rou.xml"
        report = build_hotspot_demand(
            self.source_path, network_path, config_snapshot, output,
            hotspot_id=hotspot["id"], seed=self.seed,
            visitor_count=count, background_count=0,
        )
        if report["visitor_count"] != count or report["total_count"] != count:
            raise ValueError("generated demand count does not match requested count")
        report.update({
            "generation_version": 4 if activity is not None else 3, "seed": self.seed,
            "requested_count": requested_count, "effective_count": count,
            "default_count": hotspot["visitor_count"],
            "source_path": str(self.source_path.resolve()),
            "source_sha256": hashlib.sha256(self.source_path.read_bytes()).hexdigest(),
            "hotspot_config_sha256": hashlib.sha256(config_snapshot.read_bytes()).hexdigest(),
        })
        (directory / "demand_generation.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return output, report


def _outside_destinations(network_path, hotspot, scope):
    """Keep final walking positions outside the real observation polygon."""
    import sumolib
    from shapely.geometry import LineString
    from crowdsim.decision.position_aware_router import EdgePosition, PositionAwarePedestrianRouter, PositionRouteUnavailable

    if scope is None:
        raise ValueError("热点活动执行缺少研究区域，无法确认离场终点")
    network = sumolib.net.readNet(str(network_path), withInternal=True, withPedestrianConnections=True)
    router = PositionAwarePedestrianRouter(network)
    excluded = set(hotspot.get("excluded_edges", ()))
    candidates = dict.fromkeys((*hotspot.get("visitor_destination_edges", ()),
                                *hotspot.get("external_approach_edges", ())))
    exits = []
    minimum_length = 2 * float(hotspot.get("destination_position_margin_meters", 0))
    for edge_id in candidates:
        edge = network.getEdge(edge_id)
        lanes = [lane for lane in edge.getLanes() if lane.allows("pedestrian")]
        if (edge_id in excluded or not lanes or edge.getLength() <= minimum_length
                or any(scope.intersects(LineString(lane.getShape())) for lane in lanes)):
            continue
        try:
            for target_id in hotspot.get("target_edges", (hotspot["target_edge"],)):
                router.route(EdgePosition(target_id, network.getEdge(target_id).getLength() / 2),
                             EdgePosition(edge_id, edge.getLength() / 2), forbidden_edges=excluded)
        except PositionRouteUnavailable:
            continue
        exits.append(edge_id)
    if not exits:
        raise ValueError("现有路网中未找到研究区域外可达的离场终点")
    return exits
