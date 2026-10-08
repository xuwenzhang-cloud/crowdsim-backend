"""Validate behavior plans and apply only explicit TraCI person operations."""

from __future__ import annotations

from typing import Dict, Optional
from collections import Counter
import math

from traci import constants as tc
from traci._simulation import Stage

from crowdsim.domain.crowdsim_models import AgentProfile, BehaviorPlan, MotionSnapshot, PlanExecutionResult
from crowdsim.decision.route_provider import RouteProvider
from crowdsim.infrastructure.sumo_adapter import SumoAdapter
from crowdsim.decision.pedestrian_reachability import resolve_position


EXECUTABLE_ACTIONS = {"continue", "slow_down", "wait", "reroute", "change_goal"}


class ItineraryPartialFailure(RuntimeError):
    """SUMO state was changed and could not be restored safely."""


class PlanExecutor:
    def __init__(self, adapter: SumoAdapter, route_provider: RouteProvider) -> None:
        self.adapter = adapter
        self.route_provider = route_provider
        self.strategy_limits: Dict[str, float] = {}
        self.hazard_limits: Dict[str, float] = {}
        self.wait_until: Dict[str, float] = {}
        self.activity_hold_until: Dict[str, float] = {}
        self.flow_hold_reasons: Dict[str, set[str]] = {}
        self.base_limits: Dict[str, float] = {}
        self.results: Dict[str, PlanExecutionResult] = {}
        self.route_retry_after: Dict[str, float] = {}
        self.route_failures: Dict[str, int] = {}
        self.counters = Counter({name: 0 for name in (
            "route_plans_applied", "route_failures", "itinerary_rollbacks", "partial_failures", "rejected", "partial_failure")})
        self.last_route_error = None

    def route_ready(self, person_id: str, now: float) -> bool:
        return now >= self.route_retry_after.get(person_id, 0.0)

    def defer_route(self, person_id: str, now: float, reason: str) -> None:
        failures = min(4, self.route_failures.get(person_id, 0) + 1)
        self.route_failures[person_id] = failures
        self.route_retry_after[person_id] = now + min(60.0, 10.0 * 2 ** (failures - 1))
        self.counters["route_failures"] += 1
        self.last_route_error = {"person_id": person_id, "time_seconds": now, "reason": reason[:300]}

    @property
    def diagnostics(self) -> dict:
        return {**dict(self.counters), "retry_tracked_people": len(self.route_retry_after),
                "active_activity_holds": len(self.activity_hold_until),
                "active_inflow_holds": len(self.flow_hold_reasons),
                "last_route_error": self.last_route_error}

    def retain_active(self, person_ids) -> None:
        active = set(person_ids)
        for mapping in (self.strategy_limits, self.hazard_limits, self.wait_until,
                        self.activity_hold_until, self.base_limits,
                        self.results, self.route_retry_after, self.route_failures):
            for person_id in set(mapping) - active:
                mapping.pop(person_id, None)
        for person_id in set(self.flow_hold_reasons) - active:
            self.flow_hold_reasons.pop(person_id, None)

    def register_profile(self, profile: AgentProfile) -> None:
        self.base_limits[profile.person_id] = max(0.0, profile.free_walking_speed * profile.mobility)

    def set_strategy_limit(self, person_id: str, limit: float | None) -> None:
        if limit is None:
            self.strategy_limits.pop(person_id, None)
        else:
            if not math.isfinite(float(limit)) or float(limit) < 0:
                raise ValueError("strategy speed limit must be non-negative and finite")
            self.strategy_limits[person_id] = float(limit)
        self._apply_effective_speed(person_id)

    def validate_plan(self, plan: BehaviorPlan, motion: MotionSnapshot, snapshot_id: str, now: float) -> None:
        if plan.person_id != motion.person_id:
            raise ValueError("plan person_id does not match motion snapshot")
        if plan.snapshot_id != snapshot_id:
            raise ValueError("stale plan snapshot_id")
        if plan.expires_at is not None and plan.expires_at < now:
            raise ValueError("plan has expired")
        if plan.proposed_action not in EXECUTABLE_ACTIONS:
            raise ValueError(f"action must be reduced before execution: {plan.proposed_action}")
        if plan.proposed_action == "slow_down" and (plan.speed_limit is None or not math.isfinite(plan.speed_limit) or plan.speed_limit < 0):
            raise ValueError("slow_down requires a non-negative speed_limit")
        if plan.proposed_action == "wait" and (plan.wait_until is None or not math.isfinite(plan.wait_until) or plan.wait_until <= now):
            raise ValueError("wait requires a future wait_until")
        if plan.proposed_action in {"reroute", "change_goal"}:
            if not self.route_ready(plan.person_id, now):
                raise ValueError("route replacement is in retry cooldown")
            if motion.stage_type != tc.STAGE_WALKING or motion.edge_id.startswith(":"):
                raise ValueError("route replacement is deferred until a normal walking edge")
            self.route_provider.validate_edges(plan.route_edges, current_edge=motion.edge_id)
            resolve_position(self.route_provider.network, motion.edge_id, motion.lane_position)
            resolve_position(self.route_provider.network, plan.route_edges[-1], plan.arrival_position)
            if plan.next_route_edges:
                self.route_provider.validate_edges((*plan.route_edges[-1:], *plan.next_route_edges))
                resolve_position(self.route_provider.network, plan.next_route_edges[-1], plan.next_arrival_position)
            if plan.activity_duration is not None:
                if not math.isfinite(plan.activity_duration) or plan.activity_duration <= 0:
                    raise ValueError("activity_duration must be positive and finite")
                if plan.proposed_action != "change_goal" or not plan.next_route_edges:
                    raise ValueError("activity goal requires a preplanned next walking stage")
            if plan.preserve_future_stages and plan.proposed_action != "reroute":
                raise ValueError("only reroute may preserve future SUMO stages")
            if plan.preserve_future_stages and plan.activity_duration is not None:
                raise ValueError("preserved future stages and a new activity are mutually exclusive")
            if plan.preserve_future_stages and plan.arrival_position is None:
                raise ValueError("preserved future stages require an explicit arrival_position")

    def _replace_itinerary(self, plan: BehaviorPlan, motion: MotionSnapshot) -> None:
        # Validate everything before touching SUMO. TraCI has no transaction API;
        # save all stages and restore them on failure, never silently accept half a plan.
        original = self.adapter.remaining_person_stages(plan.person_id)
        if not original or original[0].type != tc.STAGE_WALKING:
            raise ValueError("SUMO person is no longer in a walking stage")
        arrival_position = resolve_position(
            self.route_provider.network, plan.route_edges[-1], plan.arrival_position
        )
        if plan.preserve_future_stages:
            original_edges = tuple(original[0].edges)
            if not original_edges or plan.route_edges[-1] != original_edges[-1]:
                raise ValueError(
                    "preserved future stages require the reroute to keep the original target edge"
                )
            original_position = float(original[0].arrivalPos)
            if not math.isfinite(original_position):
                raise ValueError("original walking stage has no finite arrival position")
            if not math.isclose(arrival_position, original_position, abs_tol=1e-4):
                raise ValueError(
                    "preserved future stages require the original target position "
                    f"({original_position}), got {arrival_position}"
                )
            # Use the stage value after checking it, so future stages remain
            # spatially continuous even if SUMO normalizes numeric precision.
            arrival_position = original_position
        stage = Stage(type=tc.STAGE_WALKING, edges=list(plan.route_edges), departPos=motion.lane_position,
                      arrivalPos=arrival_position,
                      description=f"crowdsim:{plan.proposed_action}:{plan.target_id or ''}")
        try:
            self.adapter.remove_future_person_stages(plan.person_id)
            self.adapter.anchor_current_person_stage(plan.person_id)
            self.adapter.replace_current_person_stage(plan.person_id, stage)
            if plan.preserve_future_stages:
                for future in original[1:]:
                    self.adapter.append_person_stage(plan.person_id, future)
            elif plan.activity_duration is not None:
                self.adapter.append_waiting_stage(plan.person_id, plan.activity_duration, f"activity:{plan.target_id or ''}")
                onward = plan.next_route_edges
                if onward[0] != plan.route_edges[-1]:
                    onward = (*plan.route_edges[-1:], *onward)
                self.adapter.append_walking_stage(plan.person_id, onward,
                    resolve_position(self.route_provider.network, plan.next_route_edges[-1], plan.next_arrival_position))
        except Exception as exc:
            try:
                self.adapter.remove_future_person_stages(plan.person_id)
                self.adapter.anchor_current_person_stage(plan.person_id)
                original[0].departPos = motion.lane_position
                if motion.edge_id in original[0].edges:
                    original[0].edges = list(original[0].edges[original[0].edges.index(motion.edge_id):])
                self.adapter.replace_current_person_stage(plan.person_id, original[0])
                for future in original[1:]:
                    self.adapter.append_person_stage(plan.person_id, future)
                self.counters["itinerary_rollbacks"] += 1
            except Exception as restore_exc:
                self.counters["partial_failures"] += 1
                raise ItineraryPartialFailure(f"partial itinerary failure: {exc}; restoration failed: {restore_exc}") from exc
            raise ValueError(f"itinerary rejected and restored: {exc}") from exc

    def apply(self, plan: BehaviorPlan, motion: MotionSnapshot, snapshot_id: str, now: float) -> PlanExecutionResult:
        action = plan.proposed_action
        itinerary_applied = False
        try:
            self.validate_plan(plan, motion, snapshot_id, now)
            action = plan.proposed_action
            if action == "continue":
                self.strategy_limits.pop(plan.person_id, None)
                self.wait_until.pop(plan.person_id, None)
                self._apply_effective_speed(plan.person_id)
            elif action == "slow_down":
                self.strategy_limits[plan.person_id] = float(plan.speed_limit)
                self._apply_effective_speed(plan.person_id)
            elif action == "wait":
                self.strategy_limits[plan.person_id] = 0.0
                self.wait_until[plan.person_id] = float(plan.wait_until)
                self._apply_effective_speed(plan.person_id)
            else:
                self._replace_itinerary(plan, motion)
                itinerary_applied = True
                self._apply_effective_speed(plan.person_id)
                self.route_retry_after.pop(plan.person_id, None)
                self.route_failures.pop(plan.person_id, None)
                self.counters["route_plans_applied"] += 1
            result = PlanExecutionResult(plan.person_id, action, action, "applied", now, plan.reason)
        except Exception as exc:
            partial = itinerary_applied or isinstance(exc, ItineraryPartialFailure)
            status = "partial_failure" if partial else "rejected"
            if action in {"reroute", "change_goal"} and self.route_ready(plan.person_id, now):
                self.defer_route(plan.person_id, now, f"{type(exc).__name__}: {exc}")
            self.counters[status] += 1
            applied_action = action if itinerary_applied else ("unknown" if partial else "continue")
            result = PlanExecutionResult(plan.person_id, action, applied_action, status, now, f"{type(exc).__name__}: {exc}")
        self.results[plan.person_id] = result
        return result

    def maintain(self, now: float) -> None:
        expired = [person_id for person_id, deadline in self.wait_until.items() if now >= deadline]
        for person_id in expired:
            self.wait_until.pop(person_id, None)
            self.strategy_limits.pop(person_id, None)
            self._apply_effective_speed(person_id)

    def set_hazard_limit(self, person_id: str, limit: Optional[float]) -> None:
        if limit is None:
            self.hazard_limits.pop(person_id, None)
        else:
            self.hazard_limits[person_id] = max(0.0, float(limit))
        self._apply_effective_speed(person_id)

    def hold_activity(self, person_id: str, until: float) -> None:
        deadline = float(until)
        if not math.isfinite(deadline):
            raise ValueError("activity hold deadline must be finite")
        self.activity_hold_until[person_id] = deadline
        self._apply_effective_speed(person_id)

    def release_activity_hold(self, person_id: str) -> None:
        if self.activity_hold_until.pop(person_id, None) is not None:
            self._apply_effective_speed(person_id)

    def set_inflow_hold(self, person_id: str, action_id: str, held: bool) -> None:
        reasons = self.flow_hold_reasons.setdefault(person_id, set())
        if held:
            reasons.add(str(action_id))
        else:
            reasons.discard(str(action_id))
            if not reasons:
                self.flow_hold_reasons.pop(person_id, None)
        self._apply_effective_speed(person_id)

    def effective_speed(self, person_id: str) -> float:
        values = [self.base_limits.get(person_id, 1.35)]
        if person_id in self.strategy_limits:
            values.append(self.strategy_limits[person_id])
        if person_id in self.hazard_limits:
            values.append(self.hazard_limits[person_id])
        if person_id in self.activity_hold_until:
            values.append(0.0)
        if person_id in self.flow_hold_reasons:
            values.append(0.0)
        return min(values)

    def _apply_effective_speed(self, person_id: str) -> None:
        self.adapter.set_person_speed(person_id, self.effective_speed(person_id))
