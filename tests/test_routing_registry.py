import pytest

from crowdsim.decision.routing_registry import run_routing_algorithm


ROUTES = [
    {"routeId": "short-risky", "edges": ["a", "b"], "length": 10, "risk": 8, "capacity": 4},
    {"routeId": "long-safe", "edges": ["a", "c"], "length": 15, "risk": 0, "capacity": 10},
]


def test_dijkstra_and_risk_aware_choose_different_routes():
    assert run_routing_algorithm("dijkstra", ROUTES)["routeId"] == "short-risky"
    assert run_routing_algorithm("risk_aware_astar", ROUTES, risk_weight=1)["routeId"] == "long-safe"


def test_min_cost_flow_respects_capacity():
    result = run_routing_algorithm("min_cost_flow", ROUTES, demand=8, risk_weight=0)
    assert sum(item["flow"] for item in result["allocations"]) == 8
    assert result["allocations"][0]["flow"] == 4


def test_insufficient_capacity_is_rejected():
    with pytest.raises(ValueError, match="insufficient"):
        run_routing_algorithm("min_cost_flow", ROUTES, demand=20)


def test_dta_allocations_sum_to_demand():
    result = run_routing_algorithm("dynamic_traffic_assignment", ROUTES, demand=8, iterations=10)
    assert abs(sum(item["flow"] for item in result["allocations"]) - 8) < 1e-7
