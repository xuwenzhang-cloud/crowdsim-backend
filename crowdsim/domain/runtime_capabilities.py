"""Truthful CrowdSim control capabilities exposed during initialization."""

from __future__ import annotations


def control_capabilities() -> dict:
    return {
        "schemaVersion": "1.0",
        "supportedActions": {
            "observe_only": {},
            "publish_guidance": {"durationSeconds": {"min": 1, "max": 1800}},
            "set_inflow_rate": {
                "rate": {"min": 0.0, "max": 1.0},
                "durationSeconds": {"min": 1, "max": 1800},
                "implementation": "token-bucket admission meter queues pedestrians before the configured entry edge",
            },
            "set_edge_capacity": {
                "multiplier": {"min": 0.1, "max": 1.0},
                "durationSeconds": {"min": 1, "max": 1800},
                "implementation": "effective pedestrian speed capacity",
            },
            "set_route_distribution": {
                "ratio": {"min": 0.0, "max": 1.0},
                "requires": "each route item must include routeEdges at execution time",
            },
            "reroute_group": {"requires": "groupId and connected routeEdges"},
            "set_edge_risk_weight": {
                "weight": {"min": 0.1, "max": 10.0},
                "durationSeconds": {"min": 1, "max": 1800},
            },
        },
        "routingAlgorithms": ["dijkstra", "risk_aware_astar", "min_cost_flow", "dynamic_traffic_assignment"],
        "limitations": [
            "runtime polygon barriers and physical flood-fluid dynamics are not supported",
            "entry metering requires the configured entry edge to occur in each pedestrian's active walking route",
        ],
    }
