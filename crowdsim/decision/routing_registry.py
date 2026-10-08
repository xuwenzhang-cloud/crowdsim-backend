"""Deterministic routing algorithms over validated candidate routes.

Candidate generation and topology validation remain the responsibility of
RouteProvider.  These algorithms only rank or allocate validated candidates.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class RouteOption:
    route_id: str
    edges: tuple[str, ...]
    length: float
    risk: float = 0.0
    capacity: float = math.inf
    free_flow_time: float | None = None

    @classmethod
    def parse(cls, value: Mapping[str, Any]) -> "RouteOption":
        edges = tuple(map(str, value.get("edges") or value.get("routeEdges") or ()))
        if not edges:
            raise ValueError("route option requires edges")
        length = float(value.get("length", value.get("cost", 0.0)))
        capacity = float(value.get("capacity", math.inf))
        if not math.isfinite(length) or length < 0 or capacity <= 0:
            raise ValueError("invalid route length or capacity")
        return cls(
            route_id=str(value.get("routeId") or value.get("id") or ""),
            edges=edges,
            length=length,
            risk=max(0.0, float(value.get("risk", 0.0))),
            capacity=capacity,
            free_flow_time=float(value["freeFlowTime"]) if value.get("freeFlowTime") is not None else None,
        )


def _options(values: Iterable[Mapping[str, Any] | RouteOption]) -> list[RouteOption]:
    result = [value if isinstance(value, RouteOption) else RouteOption.parse(value) for value in values]
    if not result:
        raise ValueError("at least one route option is required")
    if any(not item.route_id for item in result) or len({item.route_id for item in result}) != len(result):
        raise ValueError("route ids must be non-empty and unique")
    return result


def dijkstra(values: Iterable[Mapping[str, Any] | RouteOption]) -> dict[str, Any]:
    selected = min(_options(values), key=lambda item: (item.length, item.route_id))
    return {"algorithm": "dijkstra", "routeId": selected.route_id, "routeEdges": list(selected.edges), "cost": selected.length}


def risk_aware_astar(values: Iterable[Mapping[str, Any] | RouteOption], risk_weight: float = 1.0) -> dict[str, Any]:
    if risk_weight < 0:
        raise ValueError("risk_weight must be non-negative")
    selected = min(_options(values), key=lambda item: (item.length + risk_weight * item.risk, item.route_id))
    return {
        "algorithm": "risk_aware_astar",
        "routeId": selected.route_id,
        "routeEdges": list(selected.edges),
        "cost": selected.length + risk_weight * selected.risk,
    }


def min_cost_flow(values: Iterable[Mapping[str, Any] | RouteOption], demand: float, risk_weight: float = 1.0) -> dict[str, Any]:
    if demand < 0:
        raise ValueError("demand must be non-negative")
    routes = sorted(_options(values), key=lambda item: (item.length + risk_weight * item.risk, item.route_id))
    remaining = float(demand)
    allocations = []
    for route in routes:
        flow = min(remaining, route.capacity)
        allocations.append({"routeId": route.route_id, "routeEdges": list(route.edges), "flow": flow})
        remaining -= flow
        if remaining <= 1e-9:
            break
    if remaining > 1e-9:
        raise ValueError("route capacity is insufficient for demand")
    for item in allocations:
        item["ratio"] = 0.0 if demand == 0 else item["flow"] / demand
    return {"algorithm": "min_cost_flow", "demand": demand, "allocations": allocations}


def dynamic_traffic_assignment(
    values: Iterable[Mapping[str, Any] | RouteOption],
    demand: float,
    *,
    iterations: int = 20,
    congestion_alpha: float = 0.15,
    congestion_beta: float = 4.0,
) -> dict[str, Any]:
    routes = _options(values)
    if demand < 0 or iterations < 1:
        raise ValueError("demand and iterations are invalid")
    flows = {item.route_id: demand / len(routes) for item in routes}
    for iteration in range(1, iterations + 1):
        costs = {}
        for item in routes:
            base = item.free_flow_time if item.free_flow_time is not None else item.length
            ratio = 0.0 if math.isinf(item.capacity) else flows[item.route_id] / item.capacity
            costs[item.route_id] = base * (1.0 + congestion_alpha * max(0.0, ratio) ** congestion_beta) + item.risk
        best = min(routes, key=lambda item: (costs[item.route_id], item.route_id)).route_id
        step = 1.0 / iteration
        for item in routes:
            target = demand if item.route_id == best else 0.0
            flows[item.route_id] += step * (target - flows[item.route_id])
    allocations = [
        {
            "routeId": item.route_id,
            "routeEdges": list(item.edges),
            "flow": flows[item.route_id],
            "ratio": 0.0 if demand == 0 else flows[item.route_id] / demand,
        }
        for item in routes
    ]
    return {"algorithm": "dynamic_traffic_assignment", "demand": demand, "iterations": iterations, "allocations": allocations}


ROUTING_REGISTRY = {
    "dijkstra": dijkstra,
    "risk_aware_astar": risk_aware_astar,
    "min_cost_flow": min_cost_flow,
    "dynamic_traffic_assignment": dynamic_traffic_assignment,
}


def run_routing_algorithm(name: str, routes, **parameters) -> dict[str, Any]:
    try:
        algorithm = ROUTING_REGISTRY[name]
    except KeyError as exc:
        raise ValueError(f"unknown routing algorithm: {name}") from exc
    return algorithm(routes, **parameters)
