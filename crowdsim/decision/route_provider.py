"""Generate and validate pedestrian routes against the loaded SUMO network."""

from __future__ import annotations

from collections import Counter, OrderedDict
from dataclasses import dataclass, replace
import math
from typing import Iterable

from crowdsim.domain.crowdsim_models import MotionSnapshot
from crowdsim.infrastructure.network_adapter import ResearchNetwork
from crowdsim.infrastructure.sumo_adapter import SumoAdapter
from crowdsim.decision.pedestrian_reachability import PedestrianReachability, resolve_position, walking_length
from crowdsim.decision.position_aware_router import (
    EdgePosition,
    PositionAwarePedestrianRouter,
    PositionRouteUnavailable,
)


class RouteUnavailable(ValueError):
    """A valid request has no walking connection (safe to cache briefly)."""


@dataclass(frozen=True)
class RouteCandidate:
    target_id: str
    target_edge: str
    arrival_position: float
    edges: tuple[str, ...]
    estimated_cost_seconds: float
    target_kind: str = "route"
    activity_duration: float | None = None
    next_route_edges: tuple[str, ...] = ()
    next_arrival_position: float | None = None
    next_target_id: str | None = None
    entry_edge: str | None = None
    base_cost_seconds: float | None = None
    congestion_delay_seconds: float = 0.0
    entry_density_person_per_m2: float = 0.0
    minimum_savings_seconds: float = 0.0
    switch_cooldown_seconds: float = 0.0


class RouteProvider:
    def __init__(self, network: ResearchNetwork, adapter: SumoAdapter) -> None:
        self.network = network
        self.adapter = adapter
        self.walkable_edges = network.pedestrian_edge_ids()
        self.edge_lengths = {edge_id: walking_length(network, edge_id) for edge_id in self.walkable_edges}
        self.reachability = PedestrianReachability(network)
        self.position_router = PositionAwarePedestrianRouter(network)
        self.dynamic_risk_weights: dict[str, float] = {}
        self._routes = OrderedDict()
        self._failures = OrderedDict()
        self._topology_failures = OrderedDict()
        self.counters = Counter({name: 0 for name in (
            "sumo_route_queries", "route_cache_hits", "negative_cache_hits", "topology_rejections",
            "sumo_unreachable", "query_errors", "candidate_refresh_deferred", "goal_fallbacks")})
        self.cache_limit = 4096

    @property
    def diagnostics(self) -> dict:
        return {**self.reachability.diagnostics(), **self.position_router.diagnostics(), **dict(self.counters),
                "cached_candidates": len(self._routes),
                "unreachable_pairs": len(self._topology_failures) + len(self._failures),
                "cached_sumo_unreachable_pairs": len(self._failures)}

    def _remember(self, cache, key, value):
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > self.cache_limit:
            cache.popitem(last=False)

    def _walking_route(self, from_edge, to_edge, depart_pos, arrival_pos, now):
        if not self.reachability.can_reach(from_edge, to_edge):
            self.counters["topology_rejections"] += 1
            self._remember(self._topology_failures, (from_edge, to_edge), True)
            raise RouteUnavailable(f"no pedestrian connection: {from_edge} -> {to_edge}")
        try:
            route = self.position_router.route(
                EdgePosition(from_edge, depart_pos),
                EdgePosition(to_edge, arrival_pos),
                edge_costs=self._effective_edge_costs(None),
                default_speed_mps=1.35,
                cost_token=self._risk_cost_token(),
            )
        except PositionRouteUnavailable as exc:
            self.counters["sumo_unreachable"] += 1
            raise RouteUnavailable(str(exc)) from exc
        edges = self.validate_edges(route.edges, current_edge=from_edge)
        return edges, route.cost

    def set_edge_risk_weight(self, edge_id: str, weight: float | None) -> None:
        if edge_id not in self.walkable_edges:
            raise ValueError(f"unknown or non-pedestrian SUMO edge: {edge_id}")
        if weight is None or math.isclose(float(weight), 1.0):
            self.dynamic_risk_weights.pop(edge_id, None)
        elif not math.isfinite(float(weight)) or float(weight) <= 0:
            raise ValueError("edge risk weight must be positive and finite")
        else:
            self.dynamic_risk_weights[edge_id] = float(weight)

    def _risk_cost_token(self):
        return tuple(sorted((edge_id, round(weight, 6)) for edge_id, weight in self.dynamic_risk_weights.items()))

    def _effective_edge_costs(self, supplied: dict[str, float] | None) -> dict[str, float] | None:
        result = dict(supplied or {})
        for edge_id, weight in self.dynamic_risk_weights.items():
            result.setdefault(edge_id, self.edge_lengths[edge_id] / 1.35 * weight)
        return result or None

    def validate_edges(self, edges: Iterable[str], *, current_edge: str | None = None) -> tuple[str, ...]:
        route = tuple(edges)
        if not route:
            raise ValueError("walking route must not be empty")
        unknown = [edge for edge in route if edge not in self.network.edges]
        if unknown:
            raise ValueError(f"unknown SUMO edge(s): {unknown}")
        forbidden = [edge for edge in route if edge not in self.walkable_edges]
        if forbidden:
            raise ValueError(f"edge(s) do not allow pedestrians: {forbidden}")
        if current_edge and route[0] != current_edge:
            raise ValueError("replacement route must start on the person's current edge")
        for first, second in zip(route, route[1:]):
            first_edge = self.network.edges[first]
            second_edge = self.network.edges[second]
            # Pedestrian walks are connected by junction topology and may not
            # have vehicle-style <connection> elements in a pedestrian-only net.
            first_nodes = {first_edge.getFromNode().getID(), first_edge.getToNode().getID()}
            second_nodes = {second_edge.getFromNode().getID(), second_edge.getToNode().getID()}
            if first_nodes.isdisjoint(second_nodes):
                raise ValueError(f"disconnected pedestrian route: {first} -> {second}")
        return route

    def build_candidate(
        self,
        motion: MotionSnapshot,
        *,
        target_id: str,
        target_edge: str,
        arrival_position: float | None = None,
        edge_costs: dict[str, float] | None = None,
        cost_token=None,
        default_speed_mps: float = 1.35,
        forbidden_edges: Iterable[str] = (),
    ) -> RouteCandidate:
        if motion.edge_id.startswith(":"):
            raise ValueError("route replacement is deferred while the person is on an internal junction edge")
        position = resolve_position(self.network, target_edge, arrival_position)
        departure = resolve_position(self.network, motion.edge_id, motion.lane_position)
        edge_costs = self._effective_edge_costs(edge_costs)
        cost_token = (cost_token, self._risk_cost_token())
        try:
            route = self.position_router.route(
                EdgePosition(motion.edge_id, departure),
                EdgePosition(target_edge, position),
                forbidden_edges=forbidden_edges,
                edge_costs=edge_costs,
                default_speed_mps=default_speed_mps,
                cost_token=cost_token,
            )
            free_route = self.position_router.route(
                EdgePosition(motion.edge_id, departure),
                EdgePosition(target_edge, position),
                forbidden_edges=forbidden_edges,
                default_speed_mps=default_speed_mps,
            )
        except PositionRouteUnavailable as exc:
            raise RouteUnavailable(str(exc)) from exc
        edges = self.validate_edges(route.edges, current_edge=motion.edge_id)
        return RouteCandidate(
            target_id,
            target_edge,
            position,
            edges,
            route.cost,
            base_cost_seconds=free_route.cost,
            congestion_delay_seconds=max(0.0, route.cost - free_route.cost),
        )

    def build_via_candidate(
        self,
        motion: MotionSnapshot,
        *,
        target_id: str,
        target_edge: str,
        via_edge: str,
        arrival_position: float | None = None,
        edge_costs: dict[str, float] | None = None,
        cost_token=None,
        default_speed_mps: float = 1.35,
        forbidden_edges: Iterable[str] = (),
        via_orientation: int | None = None,
    ) -> RouteCandidate:
        """Build a pedestrian route that must traverse ``via_edge`` before its goal."""
        if motion.edge_id.startswith(":"):
            raise ValueError("route replacement is deferred while the person is on an internal junction edge")
        departure = resolve_position(self.network, motion.edge_id, motion.lane_position)
        target_position = resolve_position(self.network, target_edge, arrival_position)
        start = EdgePosition(motion.edge_id, departure)
        target = EdgePosition(target_edge, target_position)
        try:
            route = self.position_router.route_via_edges(
                start,
                target,
                (via_edge,),
                via_orientations=(via_orientation,),
                forbidden_edges=forbidden_edges,
                edge_costs=edge_costs,
                default_speed_mps=default_speed_mps,
                cost_token=cost_token,
            )
            free_route = self.position_router.route_via_edges(
                start,
                target,
                (via_edge,),
                via_orientations=(via_orientation,),
                forbidden_edges=forbidden_edges,
                default_speed_mps=default_speed_mps,
            )
        except PositionRouteUnavailable as exc:
            raise RouteUnavailable(str(exc)) from exc
        combined = self.validate_edges(route.edges, current_edge=motion.edge_id)
        cost = route.cost
        delay = max(0.0, cost - free_route.cost)
        return RouteCandidate(
            target_id,
            target_edge,
            target_position,
            combined,
            cost,
            target_kind="hotspot_route",
            entry_edge=via_edge,
            base_cost_seconds=free_route.cost,
            congestion_delay_seconds=delay,
        )

    def build_activity_candidate(self, motion: MotionSnapshot, target: dict, next_target: dict | None) -> RouteCandidate:
        first = self.build_candidate(motion, target_id=target["id"], target_edge=target["edge"],
                                     arrival_position=target.get("arrival_position", target.get("position")))
        next_edges: tuple[str, ...] = ()
        total_cost = first.estimated_cost_seconds
        next_position = None
        if target.get("kind") == "activity" and next_target is None:
            raise RouteUnavailable("activity requires a reachable onward target")
        if next_target is not None:
            next_position = resolve_position(self.network, next_target["edge"], next_target.get("arrival_position", next_target.get("position")))
            next_edges, next_cost = self._walking_route(target["edge"], next_target["edge"], first.arrival_position, next_position, motion.time_seconds)
            total_cost += next_cost
        stay = target.get("stay_seconds", [30.0, 30.0])
        duration = (float(stay[0]) + float(stay[1])) / 2.0 if target.get("kind") == "activity" else None
        return replace(first, estimated_cost_seconds=total_cost, target_kind=target.get("kind", "route"),
                       activity_duration=duration, next_route_edges=next_edges, next_arrival_position=next_position,
                       next_target_id=next_target["id"] if next_target else None)
