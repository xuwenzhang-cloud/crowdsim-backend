"""Map stable ARDE strategies to the shared executable action vocabulary."""

from __future__ import annotations

from typing import Any, Mapping

from .contracts import new_id
from .strategy import Strategy


def _base(action_type: str, target: dict, parameters: dict, priority: int) -> dict[str, Any]:
    return {
        "actionId": new_id("act"),
        "actionType": action_type,
        "target": target,
        "parameters": parameters,
        "priority": priority,
    }


def map_strategy(strategy: str, region: Mapping[str, Any], *, risk: float, validity_seconds: float) -> dict[str, Any]:
    region_id = str(region.get("regionId") or region.get("id") or "global")
    priority = max(1, min(100, round(40 + risk * 60)))
    if strategy == Strategy.OBSERVE.value:
        return _base("observe_only", {"regionId": region_id}, {"reason": "ARDE observation strategy"}, priority)
    if strategy == Strategy.INFORM_GUIDE.value:
        return _base(
            "publish_guidance",
            {"regionId": region_id},
            {
                "message": f"请按现场引导有序离开 {region_id}",
                "command": "disperse",
                "durationSeconds": validity_seconds,
            },
            priority,
        )
    if strategy == Strategy.FLOW_REGULATE.value:
        entry_id = region.get("entryId")
        if entry_id:
            return _base(
                "set_inflow_rate",
                {"entryId": str(entry_id)},
                {"rate": round(max(0.15, 1.0 - 0.65 * risk), 3), "durationSeconds": validity_seconds},
                priority,
            )
        return _base(
            "observe_only",
            {"regionId": region_id},
            {"reason": "ARDE flow regulation requires an executable entry gate"},
            priority,
        )
    options = [
        option for option in (region.get("routeOptions") or [])
        if str(option.get("id") or option.get("routeId") or "").strip()
        and isinstance(option.get("routeEdges") or option.get("edges"), list)
        and (option.get("routeEdges") or option.get("edges"))
    ]
    if options:
        weights = []
        for option in options:
            score = max(0.01, 1.0 + float(option.get("cost", 0.0)) + 2.0 * float(option.get("risk", 0.0)))
            weights.append((str(option.get("id") or option.get("routeId")), list(option.get("routeEdges") or option.get("edges") or []), 1.0 / score))
        total = sum(value for _, _, value in weights)
        distributions = []
        assigned = 0.0
        for index, (route_id, route_edges, value) in enumerate(weights):
            ratio = round(1.0 - assigned, 6) if index == len(weights) - 1 else round(value / total, 6)
            assigned += ratio
            distributions.append({"routeId": route_id, "routeEdges": route_edges, "ratio": ratio})
        return _base(
            "set_route_distribution",
            {"originId": str(region.get("originId") or region_id)},
            {"distributions": distributions, "durationSeconds": validity_seconds},
            priority,
        )
    edge_id = str(region.get("edgeId") or region_id)
    return _base(
        "set_edge_risk_weight",
        {"edgeId": edge_id},
        {"weight": round(1.0 + 2.0 * risk, 3), "durationSeconds": validity_seconds},
        priority,
    )
