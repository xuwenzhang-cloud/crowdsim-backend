"""Compatibility serializer from one frozen runtime snapshot."""

from __future__ import annotations

from typing import Any, Dict

from crowdsim.domain.crowdsim_models import AgentState, MotionSnapshot
from crowdsim.domain.runtime_capabilities import control_capabilities


class FrameSerializer:
    def build_init(self, runtime: Any) -> Dict[str, Any]:
        return {"type": "init", "crowd_visual_state": runtime.visual_policy.metadata(), "run_id": runtime.run_id, "runtime_state": runtime.state.value, "center": list(runtime.center), "speedFactor": runtime.sim_speed_factor, "step_length": runtime.step_length, "real_step_interval": runtime.real_step_interval, "scenario": runtime.scenario_diagnostics(), "requirement": runtime.requirement_diagnostics(), "capabilities": control_capabilities(), "flood_points": runtime.flood_points, "flooded_roads": runtime.flooded_roads, "hotspots": runtime.hotspot_catalog.serialize() if runtime.hotspot_catalog else [], "demand": runtime.demand_diagnostics(), "metrics": self._metrics(runtime)}

    def build_frame(self, runtime: Any) -> Dict[str, Any]:
        current = runtime.current
        if current is None:
            raise RuntimeError("runtime has no SUMO snapshot")
        pedestrians = [self._pedestrian(runtime, motion) for motion in current.persons.values()]
        vehicles = [{"id": item["id"], "lng": item["lon"], "lat": item["lat"], "speed": round(item["speed"], 3), "color": self._motion_color(item["speed"], 0.0), "flood_impact": 0.0, "congestion": 0.0, "edge": item["edge_id"], "display_edge": item["edge_id"], "synthetic": False, "data_source": "sumo_simulation"} for item in current.vehicles.values()]
        return {"type": "update", "run_id": runtime.run_id, "runtime_state": runtime.state.value, "snapshot_id": runtime.snapshot_id, "step": current.time_seconds, "step_seconds": current.time_seconds, "step_index": runtime.snapshot_index, "step_length": runtime.step_length, "speed_factor": runtime.sim_speed_factor, "real_step_interval": runtime.real_step_interval, "scenario": runtime.scenario_diagnostics(), "vehicles": vehicles, "pedestrians": pedestrians, "flood_points": runtime.flood_points, "flooded_roads": runtime.flooded_roads, "events": runtime.serialized_events, "metrics": self._metrics(runtime, pedestrians, vehicles), "event_state": runtime.event_state, "active_interventions": runtime.interventions.active_controls()}

    def _pedestrian(self, runtime: Any, motion: MotionSnapshot) -> Dict[str, Any]:
        state = runtime.population.state_for(motion.person_id) or AgentState(motion.person_id)
        profile = runtime.profile_for(motion.person_id)
        congestion = runtime.congestion_for(motion.person_id)
        return {"id": motion.person_id, "lng": motion.lon, "lat": motion.lat, "speed": round(motion.speed, 3), **runtime.visual_states[motion.person_id], "flood_impact": round(runtime.hazard_impact_for(motion.person_id), 3), "congestion": round(congestion, 3), "edge": motion.edge_id, "display_edge": runtime.adapter.display_edge(motion.person_id, motion.edge_id), "synthetic": False, "data_source": "sumo_simulation", "state": {"age_group": profile.age_group, "age_band": profile.age_band, "gender": profile.gender, "origin": profile.origin, "crowd_role": profile.crowd_role, "nationality": profile.nationality, "native_language": profile.native_language, "mobility": round(profile.mobility, 3), "risk_tolerance": round(profile.risk_tolerance, 3), "familiarity": round(profile.familiarity, 3), "group_size": 1 + len(state.companion_ids), "perception_radius": round(profile.perception_radius, 2), "crowding_tolerance": round(profile.crowding_tolerance, 3), "following_tendency": round(profile.following_tendency, 3), "authority_compliance": round(profile.authority_compliance, 3), "stress_susceptibility": round(profile.stress_susceptibility, 3), "group_cohesion": round(profile.group_cohesion, 3), "nearby_people": max(0, runtime.local_people_count(motion.person_id) - 1), "local_density": runtime.local_density_for(motion.person_id), "density_level": runtime.density_level_for(motion.person_id), "stress": round(state.stress, 3), "perceived_risk": round(state.perceived_risk, 3), "perceived_crowding": round(state.perceived_crowding, 3), "fatigue": round(state.fatigue, 3), "activity_state": state.activity_state, "hotspot_dwell_until": state.hotspot_dwell_until, "decision": state.current_plan.proposed_action if state.current_plan else "continue", "decision_reason": state.current_plan.reason if state.current_plan else "not_due", "decision_confidence": state.current_plan.confidence if state.current_plan else None, "decision_source": state.current_plan.source if state.current_plan else "rule"}}

    @staticmethod
    def _motion_color(speed: float, congestion: float) -> str:
        if congestion >= 0.8:
            return "red"
        if congestion >= 0.5:
            return "orange"
        return "blue" if speed < 0.2 else "green"

    def _metrics(self, runtime: Any, pedestrians=None, vehicles=None) -> Dict[str, Any]:
        pedestrians, vehicles = pedestrians or [], vehicles or []
        combined = pedestrians + vehicles
        average = lambda items: sum(item["speed"] for item in items) / max(1, len(items))
        measured = runtime.latest_metrics
        population = runtime.population.diagnostics()
        hotspots = measured.get("hotspots", {})
        entry_metrics = measured.get("entries", {})
        if not entry_metrics and isinstance(hotspots, dict):
            entry_metrics = {
                str(values["entryId"]): {"regionId": str(region_id), **dict(values)}
                for region_id, values in hotspots.items()
                if isinstance(values, dict) and values.get("entryId")
            }
        return {
            "visual_state_counts": measured.get("visual_state_counts", {}),
            "visual_density_unknown_count": measured.get("visual_density_unknown_count", 0),
            "avg_speed": round(average(combined), 3),
            "pedestrian_avg_speed": round(measured.get("pedestrian_avg_speed_mps", average(pedestrians)), 3),
            "pedestrian_risk_speed": round(measured.get("pedestrian_risk_speed_mps", measured.get("pedestrian_avg_speed_mps", average(pedestrians))), 3),
            "vehicle_avg_speed": round(measured.get("vehicle_avg_speed_mps", average(vehicles)), 3),
            "pedestrian_count": len(pedestrians),
            "vehicle_count": len(vehicles),
            "affected": sum(item.get("flood_impact", 0) > 0 for item in pedestrians),
            "congestion": round(sum(item.get("congestion", 0) for item in pedestrians) / max(1, len(pedestrians)), 3),
            "water_count": len(runtime.flood_points),
            "policy": runtime.active_policy,
            "pedestrian_engine": runtime.adapter.diagnostics,
            "population": population,
            "decision_engine": runtime.decision_diagnostics,
            "routing": runtime.routing_diagnostics,
            "route_metrics": runtime.routing_diagnostics,
            "entry_metrics": entry_metrics,
            "exit_metrics": measured.get("exits", {"arrived": population.get("arrived", population.get("arrived_count", 0))}),
            "density_levels": runtime.density_level_counts(),
            "queue": measured.get("queue", {}),
            "control_queue": measured.get("control_queue", {}),
            "edge_metrics": measured.get("edges", {}),
            "hotspot_metrics": hotspots,
            "units": measured.get("units", {}),
        }
