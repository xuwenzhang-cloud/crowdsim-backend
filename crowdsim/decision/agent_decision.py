"""Shared rule/LLM BehaviorPlan contract."""

from __future__ import annotations

import asyncio
from typing import Any, Iterable

from crowdsim.domain.crowdsim_models import AgentProfile, AgentState, BehaviorPlan, Observation
from crowdsim.decision.route_provider import RouteCandidate
from crowdsim.decision.llm_gateway import LlmGateway


class AgentDecisionEngine:
    ACTIONS = {"continue", "slow_down", "wait", "reroute", "change_goal"}

    def __init__(self, gateway: LlmGateway | None = None, *, timeout: float = 8.0) -> None:
        self.gateway = gateway
        self.model = gateway.model_id if gateway is not None else ""
        self.timeout = max(1.0, float(timeout))
        self.enabled = gateway is not None
        self.last_error = ""
        self.llm_decisions = 0
        self.fallback_decisions = 0

    def rule_plan(self, profile: AgentProfile, state: AgentState, observation: Observation, candidates: Iterable[RouteCandidate] = ()) -> BehaviorPlan:
        candidates = tuple(candidates)
        common = dict(person_id=profile.person_id, snapshot_id=observation.snapshot_id, decided_at=observation.time_seconds, source="rule")
        hotspot_candidates = tuple(item for item in candidates if item.target_kind == "hotspot_route")
        if hotspot_candidates:
            return self._hotspot_route_plan(profile, state, observation, hotspot_candidates, common)
        goal_candidate = next((item for item in candidates if item.target_id == state.current_goal), None)
        visible_companions = set(state.companion_ids) & set(observation.neighbour_ids)
        rendezvous = next((item for item in candidates if item.target_id == state.rendezvous_id), None)
        if state.current_plan is None and state.group_id and state.companion_ids and not visible_companions and rendezvous is not None:
            return BehaviorPlan(**common, proposed_action="change_goal" if rendezvous.target_kind == "activity" else "reroute", target_id=rendezvous.target_id, route_edges=rendezvous.edges, arrival_position=rendezvous.arrival_position, activity_duration=rendezvous.activity_duration, next_route_edges=rendezvous.next_route_edges, next_arrival_position=rendezvous.next_arrival_position, next_target_id=rendezvous.next_target_id, reason="rejoin companions at known rendezvous")
        if not state.poi_plan_active and goal_candidate is not None and goal_candidate.target_kind == "activity":
            return BehaviorPlan(**common, proposed_action="change_goal", target_id=goal_candidate.target_id, route_edges=goal_candidate.edges, arrival_position=goal_candidate.arrival_position, activity_duration=goal_candidate.activity_duration, next_route_edges=goal_candidate.next_route_edges, next_arrival_position=goal_candidate.next_arrival_position, next_target_id=goal_candidate.next_target_id, reason="visit-purpose activity plan")
        guidance_commands = {
            str(detail.get("command") or "inform")
            for event_id, detail in state.known_events.items()
            if state.current_goal is None or str(event_id) == str(state.current_goal)
        }
        if guidance_commands & {"reroute", "disperse", "evacuate"} and candidates:
            candidate = min(candidates, key=lambda item: item.estimated_cost_seconds)
            return BehaviorPlan(**common, proposed_action="reroute", target_id=candidate.target_id, route_edges=candidate.edges, arrival_position=candidate.arrival_position, reason="trusted official guidance requests rerouting")
        if state.perceived_risk >= max(0.35, profile.risk_tolerance) and candidates:
            candidate = min(candidates, key=lambda item: item.estimated_cost_seconds * (1.2 - 0.2 * profile.familiarity))
            return BehaviorPlan(**common, proposed_action="reroute", target_id=candidate.target_id, route_edges=candidate.edges, arrival_position=candidate.arrival_position, reason="known risk exceeds tolerance")
        if observation.perceived_crowding >= max(0.45, profile.crowding_tolerance):
            return BehaviorPlan(**common, proposed_action="slow_down", speed_limit=max(0.2, profile.free_walking_speed * profile.mobility * 0.65), reason="perceived crowding exceeds tolerance")
        if state.blocked_duration >= 5.0 and profile.patience < 0.35:
            return BehaviorPlan(**common, proposed_action="wait", wait_until=observation.time_seconds + 2.0, reason="blocked and low patience")
        return BehaviorPlan(**common, proposed_action="continue", reason="current plan remains acceptable")

    @staticmethod
    def _hotspot_route_plan(profile, state, observation, candidates, common) -> BehaviorPlan:
        candidates = tuple(sorted(candidates, key=lambda item: (item.estimated_cost_seconds, item.entry_edge or "")))
        best = candidates[0]

        def reroute(candidate, reason):
            return BehaviorPlan(
                **common,
                proposed_action="reroute",
                target_id=candidate.target_id,
                route_edges=candidate.edges,
                arrival_position=candidate.arrival_position,
                selected_entry_edge=candidate.entry_edge,
                preserve_future_stages=True,
                reason=reason,
            )

        current = next((item for item in candidates if item.entry_edge == state.hotspot_entry_edge), None)
        if state.hotspot_entry_edge is None:
            return reroute(best, "select shortest expected-time hotspot entrance")

        last_change = state.hotspot_last_route_change_time
        cooldown = best.switch_cooldown_seconds
        if last_change is not None and observation.time_seconds - last_change < cooldown:
            return BehaviorPlan(**common, proposed_action="continue", reason="hotspot route switch cooldown")
        if current is None:
            return reroute(best, "current hotspot entrance is no longer reachable")
        if best.entry_edge == current.entry_edge:
            return BehaviorPlan(**common, proposed_action="continue", reason="current hotspot entrance remains fastest")

        guided = any(
            str(event_id) == str(state.current_goal)
            and str(detail.get("command") or "inform") in {"reroute", "disperse", "evacuate"}
            for event_id, detail in state.known_events.items()
        )
        if guided:
            return reroute(best, "trusted official guidance selects the safest available entrance")

        savings = current.estimated_cost_seconds - best.estimated_cost_seconds
        diversion = (
            0.25 * profile.familiarity
            + 0.25 * (1.0 - profile.crowding_tolerance)
            + 0.15 * profile.mobility
            + 0.10 * profile.endurance
            + 0.10 * (1.0 - profile.following_tendency)
            + 0.15 * (1.0 - profile.group_cohesion)
        )
        queue_preference = (
            0.40 * profile.patience
            + 0.30 * profile.crowding_tolerance
            + 0.15 * profile.following_tendency
            + 0.15 * profile.group_cohesion
        )
        threshold = current.minimum_savings_seconds * (1.25 - 0.5 * diversion)
        if current.congestion_delay_seconds > 0 and savings >= threshold and diversion > queue_preference:
            return reroute(best, f"profile accepts detour saving {savings:.1f}s")
        return BehaviorPlan(**common, proposed_action="continue", reason="profile prefers current entrance queue")

    async def decide(self, profile: AgentProfile, state: AgentState, observation: Observation, candidates: Iterable[RouteCandidate] = ()) -> BehaviorPlan:
        candidates = tuple(candidates)
        fallback = self.rule_plan(profile, state, observation, candidates)
        if any(item.target_kind == "hotspot_route" for item in candidates):
            # Hotspot alternatives are constrained by deterministic network and
            # profile rules; an LLM must not invent an unvalidated entrance.
            self.fallback_decisions += 1
            return fallback
        if not self.enabled:
            self.fallback_decisions += 1
            return fallback
        try:
            result = await asyncio.wait_for(asyncio.to_thread(self._request, self._prompt(profile, state, observation, candidates)), timeout=self.timeout)
            plan = self._parse_plan(result, profile, observation, candidates)
            self.llm_decisions += 1
            return plan
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.fallback_decisions += 1
            return BehaviorPlan(**{**vars(fallback), "source": "rule_fallback", "reason": f"LLM fallback: {self.last_error}"})

    def _prompt(self, profile: AgentProfile, state: AgentState, observation: Observation, candidates: tuple[RouteCandidate, ...]) -> dict:
        active_profile = {name: getattr(profile, name) for name in ("free_walking_speed", "mobility", "perception_radius", "familiarity", "risk_tolerance", "patience", "crowding_tolerance", "following_tendency", "authority_compliance", "information_trust", "group_cohesion", "stress_susceptibility", "recovery_seconds", "endurance")}
        return {"person_id": profile.person_id, "profile": active_profile, "state": {"stress": state.stress, "fatigue": state.fatigue, "perceived_risk": state.perceived_risk, "blocked_duration": state.blocked_duration, "known_event_ids": sorted(state.known_events), "current_goal": state.current_goal, "activity_state": state.activity_state, "group_id": state.group_id}, "observation": {"snapshot_id": observation.snapshot_id, "time_seconds": observation.time_seconds, "local_people_count": observation.local_people_count, "objective_density_per_m2": observation.objective_density_per_m2, "perceived_crowding": observation.perceived_crowding, "available_goal_ids": observation.available_goal_ids}, "candidates": [{"target_id": item.target_id, "edges": item.edges, "cost_seconds": item.estimated_cost_seconds, "target_kind": item.target_kind} for item in candidates], "allowed_actions": sorted(self.ACTIONS)}

    def _request(self, prompt: dict) -> dict:
        if self.gateway is None:
            raise RuntimeError("no LLM gateway has been configured")
        result = self.gateway.complete(prompt)
        if not isinstance(result, dict):
            raise TypeError("LLM gateway must return a JSON-compatible object")
        return result

    def _parse_plan(self, result: dict, profile: AgentProfile, observation: Observation, candidates: tuple[RouteCandidate, ...]) -> BehaviorPlan:
        action = str(result.get("action", ""))
        if action not in self.ACTIONS:
            raise ValueError(f"unsupported action: {action}")
        target_id = result.get("target_id")
        route_edges = ()
        if action in {"reroute", "change_goal"}:
            candidate = next((item for item in candidates if item.target_id == target_id), None)
            if candidate is None:
                raise ValueError("LLM selected a target outside supplied candidates")
            route_edges = candidate.edges
        speed_limit = None
        wait_until = None
        if action == "slow_down":
            speed_limit = max(0.0, min(profile.free_walking_speed * profile.mobility, float(result.get("speed_limit", 0.7))))
        if action == "wait":
            wait_until = observation.time_seconds + max(0.5, min(30.0, float(result.get("wait_seconds", 2.0))))
        activity_duration = candidate.activity_duration if action == "change_goal" else None
        next_route_edges = candidate.next_route_edges if action == "change_goal" else ()
        return BehaviorPlan(profile.person_id, observation.snapshot_id, action, target_id=target_id, route_edges=route_edges, speed_limit=speed_limit, wait_until=wait_until, activity_duration=activity_duration, next_route_edges=next_route_edges, arrival_position=candidate.arrival_position if route_edges else None, next_arrival_position=candidate.next_arrival_position if action == "change_goal" else None, next_target_id=candidate.next_target_id if action == "change_goal" else None, reason=str(result.get("reason", "model decision"))[:240], source="llm", decided_at=observation.time_seconds)

    def diagnostics(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "model": self.model, "llm_decisions": self.llm_decisions, "fallback_decisions": self.fallback_decisions, "last_error": self.last_error}
