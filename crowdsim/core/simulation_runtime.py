"""Top-level simulation lifecycle and deterministic tick boundary."""

from __future__ import annotations

from dataclasses import replace
from enum import Enum
import json
from pathlib import Path
from typing import Any, Iterable, Optional
import uuid
import xml.etree.ElementTree as ET

from crowdsim.decision.agent_decision import AgentDecisionEngine
from crowdsim.decision.decision_scheduler import DecisionScheduler
from crowdsim.decision.hotspot_route_choice import HotspotRouteChoice
from crowdsim.decision.plan_executor import PlanExecutor
from crowdsim.decision.route_provider import RouteProvider
from crowdsim.control.inflow_meter import InflowMeter
from crowdsim.domain.crowdsim_models import AgentProfile, GroupRecord
from crowdsim.domain.crowd_visual_state import CrowdVisualPolicy, STATE_COLORS
from crowdsim.domain.group_manager import GroupManager
from crowdsim.domain.population_profiles import PopulationProfileSampler
from crowdsim.domain.requirement_spec import requirement_runtime_summary
from crowdsim.environment.activity_planner import ActivityPlanner
from crowdsim.environment.crowd_environment import CrowdEnvironment
from crowdsim.environment.event_catalog import event_defaults
from crowdsim.environment.event_manager import EventManager
from crowdsim.environment.hazard_model import HazardModel, HazardZone
from crowdsim.environment.information_model import InformationModel
from crowdsim.environment.intervention_executor import InterventionExecutor
from crowdsim.environment.hotspot_catalog import HotspotCatalog
from crowdsim.environment.poi_catalog import PoiCatalog
from crowdsim.infrastructure.experiment_recorder import ExperimentRecorder
from crowdsim.infrastructure.frame_serializer import FrameSerializer
from crowdsim.infrastructure.metrics import MetricsCollector
from crowdsim.infrastructure.network_adapter import ResearchNetwork
from crowdsim.infrastructure.sumo_adapter import SumoAdapter, SumoStepResult
from crowdsim.core.population_manager import PopulationManager
from crowdsim.core.runtime_commands import RuntimeCommand, RuntimeCommandQueue
from crowdsim.core.state_updater import StateUpdater
from crowdsim.infrastructure.performance_probe import PerformanceProbe, timed
from crowdsim.scenarios.generated_hotspot_demand import HotspotDemandSpec
from traci import constants as tc


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class RuntimeState(str, Enum):
    CREATED = "CREATED"
    READY = "READY"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    FINISHED = "FINISHED"
    ERROR = "ERROR"
    CLOSED = "CLOSED"


class SimulationRuntime:
    """The only application component that advances simulation time."""

    def __init__(
        self,
        config_path: str | Path,
        *,
        pedestrian_route_files: Iterable[str | Path] = (),
        sumo_binary: str | None = None,
        extra_sumo_args: Optional[Iterable[str]] = None,
        decision_engine: Any | None = None,
        use_llm: bool = False,
        scenario_name: str = "custom",
        demand_mode: str = "configurable",
        timeline_end_seconds: float | None = None,
        hotspot_demand_spec: HotspotDemandSpec | None = None,
        requirement_record: dict | None = None,
        random_seed: int | None = None,
    ) -> None:
        if demand_mode not in {"configurable", "fixed", "generated_hotspot"}:
            raise ValueError("unknown demand_mode")
        if demand_mode == "generated_hotspot" and hotspot_demand_spec is None:
            raise ValueError("generated_hotspot requires hotspot_demand_spec")
        if timeline_end_seconds is not None and float(timeline_end_seconds) <= 0:
            raise ValueError("timeline_end_seconds must be positive")
        self.config_path = Path(config_path).resolve()
        self._pedestrian_route_files = tuple(Path(path).resolve() for path in pedestrian_route_files)
        self._sumo_binary = sumo_binary
        self._extra_sumo_args = tuple(extra_sumo_args or ())
        if demand_mode == "generated_hotspot" and any(
            arg == "--route-files" or arg.startswith("--route-files=") for arg in self._extra_sumo_args
        ):
            raise ValueError("generated_hotspot cannot be combined with an explicit route-file override")
        self.run_id = f"run-{uuid.uuid4().hex}"
        self.performance = PerformanceProbe(self.run_id)
        self.network: ResearchNetwork | None = None
        self.adapter = SumoAdapter(
            self.config_path,
            sumo_binary=sumo_binary,
            extra_args=self._extra_sumo_args,
        )
        if random_seed is not None and hotspot_demand_spec is not None:
            hotspot_demand_spec = replace(hotspot_demand_spec, seed=int(random_seed))
        profile_sampler = PopulationProfileSampler(seed=int(random_seed)) if random_seed is not None else None
        self.population = PopulationManager(self._pedestrian_route_files, profile_sampler=profile_sampler)
        self.random_seed = int(self.population.profile_sampler.seed)
        self.state = RuntimeState.CREATED
        self.current: SumoStepResult | None = None
        self.snapshot_index = 0
        self.last_error: str | None = None
        self._close_complete = False
        self.close_errors: list[dict] = []
        self.sim_speed_factor = 1.0
        self.push_fps = 8.0
        self.step_length = 0.5
        self.serializer = FrameSerializer()
        self.flood_points: list[dict] = []
        self.flooded_roads: list[dict] = []
        self.serialized_events: list[dict] = []
        self.event_state: dict = {"crowd_gathering": {"active": False, "decision": "", "step": -1}}
        self.active_policy = ""
        self.decision_engine = decision_engine if decision_engine is not None else AgentDecisionEngine()
        self.decision_scheduler = DecisionScheduler(self.decision_engine)
        self.use_llm = bool(use_llm)
        self.scenario_name = str(scenario_name)
        self.demand_mode = demand_mode
        self.hotspot_demand_spec = hotspot_demand_spec
        self.demand_generation_report = None
        self.demand_capabilities = hotspot_demand_spec.capabilities() if hotspot_demand_spec else {}
        self.timeline_end_seconds = float(timeline_end_seconds) if timeline_end_seconds is not None else None
        self.ignored_demand_count: int | None = None
        self.environment: CrowdEnvironment | None = None
        self.visual_policy = CrowdVisualPolicy.load()
        self.visual_states = {}
        self.state_updater = StateUpdater(self.visual_policy)
        self.information = InformationModel()
        self.hazards = HazardModel()
        self.interventions = InterventionExecutor(self.information)
        self._control_speed_limits: dict[str, dict[str, float]] = {}
        self._control_risk_weights: dict[str, tuple[str, float]] = {}
        self._inflow_meters: dict[str, InflowMeter] = {}
        self._inflow_holds: dict[str, set[str]] = {}
        self.event_manager: EventManager | None = None
        self.groups = GroupManager()
        self.route_provider: RouteProvider | None = None
        self.plan_executor: PlanExecutor | None = None
        self.observations = {}
        self.demand_count: int | None = None
        self.prepared_demand_path: Path | None = None
        self.metrics_collector: MetricsCollector | None = None
        self.latest_metrics: dict = {}
        self.recorder: ExperimentRecorder | None = None
        self.poi_catalog: PoiCatalog | None = None
        self.activity_planner: ActivityPlanner | None = None
        self.hotspot_catalog: HotspotCatalog | None = None
        self.hotspot_route_choice: HotspotRouteChoice | None = None
        self.decision_candidates = {}
        self.commands = RuntimeCommandQueue()
        self.requirement_record: dict | None = None
        self.requirement_id: str | None = None
        if requirement_record is not None:
            self.configure_requirement(requirement_record)

    @property
    def time_seconds(self) -> float:
        return self.current.time_seconds if self.current else 0.0

    @property
    def snapshot_id(self) -> str:
        return f"{self.run_id}:{self.snapshot_index}"

    @property
    def center(self) -> tuple[float, float]:
        if self.network is None:
            raise RuntimeError("runtime is not initialized")
        return self.network.center

    @property
    def real_step_interval(self) -> float:
        return self.step_length / max(0.01, self.sim_speed_factor)

    @property
    def running(self) -> bool:
        return self.state == RuntimeState.RUNNING

    def initialize(self) -> SumoStepResult:
        if self.state not in {RuntimeState.CREATED}:
            raise RuntimeError(f"initialize is invalid in state {self.state.value}")
        try:
            self.step_length = self._config_float("time", "step-length", 0.5)
            run_directory = PROJECT_ROOT / "runs" / self.run_id
            run_directory.mkdir(parents=True, exist_ok=True)
            if self.requirement_record is not None:
                (run_directory / "requirement.json").write_text(
                    json.dumps(self.requirement_record, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            self.performance.attach(run_directory)
            if "--log" not in self.adapter.extra_args:
                self.adapter.extra_args.extend(["--log", str(run_directory / "sumo.log")])
            self.network = ResearchNetwork(str(self._net_path_from_config()))
            self.environment = CrowdEnvironment(self.network)
            self.event_manager = EventManager(self.network)
            poi_path = PROJECT_ROOT / "config" / "pois.json"
            if self.config_path.name.startswith("bund.") and poi_path.is_file():
                self.poi_catalog = PoiCatalog(self.network, poi_path)
                self.activity_planner = ActivityPlanner(self.poi_catalog)
                hotspot_path = PROJECT_ROOT / "config" / "crowd_hotspots.json"
                if hotspot_path.is_file():
                    self.hotspot_catalog = HotspotCatalog(self.network, hotspot_path)
            if self.demand_mode == "generated_hotspot":
                source, self.demand_generation_report = self.hotspot_demand_spec.generate(
                    run_directory, self._net_path_from_config(), self.demand_count
                )
                if self.hotspot_catalog is not None:
                    self.hotspot_catalog.apply_demand_timing(self.demand_generation_report)
                self.population = PopulationManager([source], profile_sampler=self.population.profile_sampler)
                # Empty baseline runs must still replace the preset route file.
                self.prepared_demand_path = source
            if self.population.ledger.planned_ids:
                self.prepared_demand_path = PROJECT_ROOT / "runs" / self.run_id / "demand.rou.xml"
                self.population.prepare_demand(
                    self.prepared_demand_path,
                    None if self.demand_mode == "generated_hotspot" else self.demand_count,
                )
            if self.prepared_demand_path is not None:
                # Replay and specialised experiments may already provide a complete
                # route-file override.  SUMO rejects duplicate occurrences of this
                # option, so only construct the normal override when none exists.
                if "--route-files" not in self.adapter.extra_args:
                    replaced = set(self._pedestrian_route_files) | set(self.population.route_files)
                    route_files = [path for path in self._route_files_from_config() if path not in replaced]
                    route_files.append(self.prepared_demand_path)
                    self.adapter.extra_args.extend(["--route-files", ",".join(str(path) for path in route_files)])
            self.current = self.adapter.start()
            self.population.reconcile(self.current)
            self.route_provider = RouteProvider(self.network, self.adapter)
            if self.hotspot_catalog is not None:
                self.hotspot_route_choice = HotspotRouteChoice(self.network, self.route_provider)
            self.plan_executor = PlanExecutor(self.adapter, self.route_provider)
            for person_id in self.current.persons:
                self.plan_executor.register_profile(self.population.profile_for(person_id))
            self._update_hotspot_activity(self.current)
            self.metrics_collector = MetricsCollector(
                self.network,
                hotspots=self.hotspot_catalog,
                population_manager=self.population,
            )
            self.latest_metrics = self.metrics_collector.measure(self.current, self.population.states, self.population.diagnostics())
            self._refresh_visual_states()
            self.recorder = ExperimentRecorder(self.run_id, self.config_path, self.adapter.diagnostics)
            demand_source = self.prepared_demand_path or (self.population.route_files[0] if len(self.population.route_files) == 1 else None)
            if demand_source is not None:
                self.recorder.archive_demand(demand_source)
            self.recorder.update_manifest(
                mode="llm" if self.use_llm else "rule",
                model_id=self.decision_engine.model if self.use_llm else None,
                step_length=self.step_length,
                profile_seed=self.population.profile_sampler.seed,
                crowd_visual_state=self.visual_policy.metadata(),
                demand=self.demand_diagnostics(),
                demand_generation=self.demand_generation_report,
                requirement=self.requirement_diagnostics(),
                performance_measurement={'enabled': self.performance.enabled, 'interval_wall_seconds': self.performance.interval, 'version': 1},
                pedestrian_route_files=[str(path) for path in self.population.route_files],
            )
            self.state = RuntimeState.READY
            self.performance.start_system(self)
            self.recorder.update_manifest(system_performance_measurement=self.performance.system_metadata())
            return self.current
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.state = RuntimeState.ERROR
            self._abort_resources()
            raise

    def start(self) -> None:
        if self.state not in {RuntimeState.READY, RuntimeState.PAUSED}:
            raise RuntimeError(f"start is invalid in state {self.state.value}")
        self.state = RuntimeState.RUNNING

    def pause(self) -> None:
        if self.state != RuntimeState.RUNNING:
            raise RuntimeError(f"pause is invalid in state {self.state.value}")
        self.state = RuntimeState.PAUSED

    def tick(self) -> SumoStepResult:
        if self.state != RuntimeState.RUNNING:
            raise RuntimeError(f"tick is invalid in state {self.state.value}")
        try:
            due = self._prepare_boundary()
            with self.performance.measure('decision_resolve'):
                plans = self.decision_scheduler.resolve_rule(due, self.population.profiles, self.population.states, self.observations, self.decision_candidates)
            return self._step_after_plans(plans)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.state = RuntimeState.ERROR
            self._abort_resources()
            raise

    async def tick_async(self) -> SumoStepResult:
        if self.state != RuntimeState.RUNNING:
            raise RuntimeError(f"tick is invalid in state {self.state.value}")
        try:
            due = self._prepare_boundary()
            with self.performance.measure('decision_resolve'):
                plans = await self.decision_scheduler.resolve(due, self.population.profiles, self.population.states, self.observations, use_llm=self.use_llm, candidates=self.decision_candidates)
            if self.state != RuntimeState.RUNNING:
                return self.current
            return self._step_after_plans(plans)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.state = RuntimeState.ERROR
            self._abort_resources()
            raise

    @timed('boundary_total')
    def _prepare_boundary(self) -> list[str]:
        self.process_pending_commands()
        self._expire_control_actions()
        self.hazards.update(self.time_seconds)
        if self.event_manager is not None:
            self.event_manager.step(self.time_seconds)
            self.serialized_events = self.event_manager.serialize(self.time_seconds)
            for event in self.event_manager.events:
                if event.id in self.hazards.zones:
                    self.hazards.zones[event.id].intensity = event.intensity
        if self.current is not None:
            self.information.expire(self.time_seconds, self.population.states)
            deliveries = self.information.deliver(self.time_seconds, self.current.persons, self.population.profiles, self.population.states)
            if self.recorder is not None:
                self.recorder.record_messages(deliveries)
        if self.plan_executor is not None:
            self._refresh_dynamic_control_targets()
            self._maintain_hotspot_activity(self.time_seconds)
            self.plan_executor.maintain(self.time_seconds)
            for person_id, state in self.population.states.items():
                if state.planned_wait_until is not None and person_id not in self.plan_executor.wait_until:
                    state.planned_wait_until = None
            if self.current is not None:
                for person_id, motion in self.current.persons.items():
                    profile = self.population.profile_for(person_id)
                    base_speed = profile.free_walking_speed * profile.mobility
                    self.plan_executor.set_hazard_limit(person_id, self.hazards.speed_limit_for(motion, base_speed))
        due = self.decision_scheduler.collect_due(self.population.states, self.observations, self.time_seconds)
        with self.performance.measure('route_context'):
            self._refresh_agent_context(due)
        return due

    def queue_command(self, action: str, payload: dict, request_id=None) -> dict:
        command = RuntimeCommand(
            action=action,
            payload=dict(payload),
            request_id=None if request_id is None else str(request_id),
            submitted_at=self.time_seconds,
            submitted_snapshot=self.snapshot_id,
        )
        self.commands.submit(command)
        return {
            "type": "command_result",
            "request_id": request_id,
            "action": action,
            "status": "queued",
            "submitted_at": self.time_seconds,
            "snapshot_id": self.snapshot_id,
        }

    def process_pending_commands(self) -> list[dict]:
        results = self.commands.apply_all(
            self._apply_queued_command,
            applied_at=self.time_seconds,
            snapshot_id=self.snapshot_id,
        )
        if self.recorder is not None:
            for result in results:
                self.recorder.record_command(
                    {"action": result["action"], "request_id": result["request_id"]},
                    result,
                )
        return results

    def take_command_results(self) -> list[dict]:
        return self.commands.take_results()

    def _apply_queued_command(self, action: str, data: dict):
        if action == "update_flood_source":
            return self.set_flood(data)
        if action in {"set_event", "trigger_event"}:
            return self.set_event(data)
        if action in {"event_decision", "set_policy", "apply_policy"}:
            return self.apply_policy(data)
        if action == "apply_action":
            return self.apply_control_action(data)
        if action == "set_group":
            record = GroupRecord(
                group_id=str(data.get("group_id") or ""),
                member_ids=[str(item) for item in data.get("member_ids", ())],
                leader_id=str(data["leader_id"]) if data.get("leader_id") is not None else None,
                rendezvous_id=str(data["rendezvous_id"]) if data.get("rendezvous_id") is not None else None,
            )
            if not record.group_id:
                raise ValueError("group_id is required")
            if record.rendezvous_id and (self.poi_catalog is None or record.rendezvous_id not in self.poi_catalog.pois):
                raise ValueError("rendezvous_id must reference a configured POI")
            self.groups.register(record, self.population.ledger.planned_ids | self.population.ledger.departed_ids)
            self.groups.update_groups(self.population.states)
            return {"group_id": record.group_id, "member_count": len(record.member_ids)}
        raise ValueError(f"unsupported queued command: {action}")

    def _step_after_plans(self, plans) -> SumoStepResult:
        if self.current is not None and self.plan_executor is not None:
            for plan in plans:
                if plan.person_id in self.current.persons:
                    execution = self.apply_plan(plan)
                    if execution.status == "partial_failure":
                        raise RuntimeError(execution.reason)
        with self.performance.measure('sumo_step'):
            result = self.adapter.step()
        self.population.reconcile(result)
        if self.plan_executor is not None:
            self.plan_executor.retain_active(result.persons)
        for person_id in result.persons:
            if self.plan_executor is not None and person_id not in self.plan_executor.base_limits:
                self.plan_executor.register_profile(self.population.profile_for(person_id))
        self._update_hotspot_activity(result)
        old_states = dict(self.population.states)
        if self.environment is not None:
            with self.performance.measure('neighbour_observation'):
                observations = self.environment.observe(result.persons, self.population.profiles, old_states, f"{self.run_id}:{self.snapshot_index + 1}", self.hazards.zones.values())
            with self.performance.measure('state_update'):
                new_states = self.state_updater.update_all(old_states, self.population.profiles, observations, self.step_length)
            self.population.commit_states(new_states)
            self.observations = observations
            self.groups.update_groups(self.population.states)
        self.current = result
        self.snapshot_index += 1
        if self.metrics_collector is not None:
            with self.performance.measure('metrics'):
                self.latest_metrics = self.metrics_collector.measure(result, self.population.states, self.population.diagnostics())
                planned_holds = set(self.plan_executor.flow_hold_reasons) | set(self.plan_executor.activity_hold_until) if self.plan_executor is not None else set()
                moving = [motion.speed for person_id, motion in result.persons.items() if person_id not in planned_holds]
                self.latest_metrics["pedestrian_risk_speed_mps"] = sum(moving) / max(1, len(moving))
                self.latest_metrics["control_queue"] = {
                    "inflow_held": len(self.plan_executor.flow_hold_reasons) if self.plan_executor is not None else 0,
                    "planned_activity_held": len(self.plan_executor.activity_hold_until) if self.plan_executor is not None else 0,
                }
        self._refresh_visual_states()
        if self.recorder is not None:
            with self.performance.measure('record_step'):
                self.recorder.record_step(self, result, self.latest_metrics)
        reached_timeline_end = (
            self.timeline_end_seconds is not None
            and result.time_seconds + 1e-9 >= self.timeline_end_seconds
        )
        if reached_timeline_end or self.adapter.min_expected_number <= 0:
            self.state = RuntimeState.FINISHED
        return result

    @timed('visual_classification')
    def _refresh_visual_states(self) -> None:
        self.visual_states = {
            person_id: self.visual_policy.classify(motion, self.observations.get(person_id), self.population.states[person_id])
            for person_id, motion in self.current.persons.items()
        }
        counts = dict.fromkeys(STATE_COLORS, 0)
        for indicator in self.visual_states.values():
            counts[indicator["visual_state"]] += 1
        self.latest_metrics["visual_state_counts"] = counts
        self.latest_metrics["visual_density_unknown_count"] = sum(
            not indicator["density_valid"] for indicator in self.visual_states.values()
        )

    def _refresh_agent_context(self, person_ids=()) -> None:
        self.decision_candidates = {}
        if self.route_provider is None:
            return
        for person_id in person_ids:
            observation = self.observations[person_id]
            if person_id in self.population.locked_itinerary_ids:
                # This person already has an explicit walking/waiting/walking
                # itinerary in the demand file.  Autonomous POI selection must
                # not overwrite that externally declared scenario treatment.
                continue
            hotspot_id = getattr(self.population, "goal_locked_hotspot_ids", {}).get(person_id)
            if hotspot_id is not None and self._refresh_hotspot_route_context(person_id, hotspot_id, observation):
                continue
            if self.activity_planner is None or self.poi_catalog is None:
                continue
            state = self.population.states[person_id]
            profile = self.population.profile_for(person_id)
            self.activity_planner.initialize(profile, state)
            motion = observation.own_motion
            if state.poi_plan_active and motion.stage_type == tc.STAGE_WALKING and self.adapter.remaining_stage_count(person_id) == 1:
                # SUMO has finished the activity walk AND waiting stage. Advance
                # only now, not when a rule/LLM emits an intervening continue.
                if state.current_goal in state.activity_plan:
                    index = state.activity_plan.index(state.current_goal)
                    state.activity_plan = state.activity_plan[index + 1:]
                state.current_goal = state.pending_goal
                state.pending_goal = None
                state.poi_plan_active = False
            if (motion.stage_type != tc.STAGE_WALKING or motion.edge_id.startswith(":")
                    or not self.plan_executor.route_ready(person_id, self.time_seconds)):
                self.observations[person_id] = replace(observation, available_goal_ids=())
                self.route_provider.counters["candidate_refresh_deferred"] += 1
                continue
            known_ids = set(state.activity_plan)
            if state.rendezvous_id:
                known_ids.add(state.rendezvous_id)
            available = tuple(item["id"] for item in self.poi_catalog.available(self.time_seconds, known_ids))
            order = {target_id: index for index, target_id in enumerate(state.activity_plan)}
            available = tuple(sorted(available, key=lambda target_id: order.get(target_id, len(order))))
            candidates = []
            last_error = None
            for target_id in available:
                target = self.poi_catalog.pois[target_id]
                if target.get("kind") == "activity":
                    index = state.activity_plan.index(target_id) if target_id in state.activity_plan else -1
                    onward = [item for item in state.activity_plan[index + 1:] if item != target_id and item in available]
                else:
                    onward = [None]
                for next_id in onward:
                    next_target = self.poi_catalog.pois.get(next_id) if next_id else None
                    try:
                        candidate = self.route_provider.build_activity_candidate(motion, target, next_target)
                        candidates.append(candidate)
                        break
                    except ValueError as exc:
                        last_error = str(exc)
                    except Exception as exc:
                        # Retry transport failures later; don't poison topology cache.
                        last_error = f"{type(exc).__name__}: {exc}"
                        break
            if candidates and not state.poi_plan_active and not any(item.target_id == state.current_goal for item in candidates):
                # Skip an inaccessible/closed preferred POI, rather than retrying
                # the same goal every decision period or cycling back to it.
                state.current_goal = candidates[0].target_id
                self.route_provider.counters["goal_fallbacks"] += 1
            if not candidates and available:
                self.plan_executor.defer_route(person_id, self.time_seconds, last_error or "no reachable onward POI")
            self.decision_candidates[person_id] = tuple(candidates)
            self.observations[person_id] = replace(observation, available_goal_ids=tuple(item.target_id for item in candidates))

    def _refresh_hotspot_route_context(self, person_id, hotspot_id, observation) -> bool:
        if self.hotspot_catalog is None or self.hotspot_route_choice is None or self.plan_executor is None:
            return False
        hotspot = self.hotspot_catalog.hotspots.get(hotspot_id)
        if hotspot is None or not hotspot.get("route_choice", {}).get("enabled"):
            return False
        assigned_target = getattr(self.population, "hotspot_target_edges", {}).get(
            person_id, hotspot.get("target_edge")
        )
        if assigned_target is not None and assigned_target not in hotspot.get("target_edges", (assigned_target,)):
            raise ValueError(
                f"hotspot visitor {person_id} has target outside {hotspot_id}: {assigned_target}"
            )
        assigned_position = getattr(self.population, "hotspot_target_positions", {}).get(person_id)
        if assigned_position is None:
            self.plan_executor.defer_route(
                person_id,
                self.time_seconds,
                f"hotspot visitor {person_id} has no preserved target position",
            )
            self.decision_candidates[person_id] = ()
            self.observations[person_id] = replace(observation, available_goal_ids=())
            return True
        route_hotspot = {
            **hotspot,
            **({"target_edge": assigned_target} if assigned_target is not None else {}),
            "target_position": assigned_position,
        }
        state = self.population.states[person_id]
        if state.activity_state in {"hotspot_dwelling", "hotspot_departing"}:
            self.decision_candidates[person_id] = ()
            self.observations[person_id] = replace(observation, available_goal_ids=())
            return True
        if state.hotspot_entry_edge is None:
            state.hotspot_entry_edge = getattr(
                self.population, "hotspot_initial_entry_edges", {}
            ).get(person_id)
        state.current_goal = hotspot_id
        motion = observation.own_motion
        if (motion.stage_type != tc.STAGE_WALKING or motion.edge_id.startswith(":")
                or not self.plan_executor.route_ready(person_id, self.time_seconds)):
            self.observations[person_id] = replace(observation, available_goal_ids=())
            self.route_provider.counters["candidate_refresh_deferred"] += 1
            return True
        try:
            candidates = self.hotspot_route_choice.build_candidates(
                motion,
                self.population.profile_for(person_id),
                state,
                route_hotspot,
                self.latest_metrics.get("edges", {}),
            )
        except ValueError as exc:
            self.plan_executor.defer_route(person_id, self.time_seconds, str(exc))
            candidates = ()
        self.decision_candidates[person_id] = tuple(candidates)
        available = (hotspot_id,) if candidates else ()
        self.observations[person_id] = replace(observation, available_goal_ids=available)
        return True

    def _update_hotspot_activity(self, step: SumoStepResult) -> None:
        """Start a position-preserving hotspot dwell after the arrival walk ends."""
        if self.plan_executor is None:
            return
        hotspot_ids = getattr(self.population, "hotspot_ids", {})
        target_edges = getattr(self.population, "hotspot_target_edges", {})
        dwell_seconds = getattr(self.population, "hotspot_dwell_seconds", {})
        release_times = getattr(self.population, "hotspot_release_times", {})
        for person_id, motion in step.persons.items():
            if person_id not in hotspot_ids:
                continue
            state = self.population.states[person_id]
            if state.activity_state == "walking":
                state.activity_state = "hotspot_approaching"
            if state.activity_state != "hotspot_approaching":
                continue
            target_edge = target_edges.get(person_id)
            if (
                target_edge is None
                or motion.stage_type != tc.STAGE_WALKING
                or motion.edge_id != target_edge
                or motion.remaining_stage_count > 1
            ):
                continue
            release_time = release_times.get(person_id)
            duration = dwell_seconds.get(person_id)
            if release_time is not None:
                deadline = float(release_time)
            elif duration is not None:
                deadline = step.time_seconds + float(duration)
            else:
                raise ValueError(
                    f"hotspot visitor {person_id} has no dwell duration or release time"
                )
            self.plan_executor.hold_activity(person_id, deadline)
            state.activity_state = "hotspot_dwelling"
            state.hotspot_dwell_until = deadline
            state.current_goal = hotspot_ids[person_id]
            state.next_decision_time = max(state.next_decision_time, deadline)

    def _maintain_hotspot_activity(self, now: float) -> None:
        """Release completed hotspot dwells without changing the walking stage."""
        if self.plan_executor is None:
            return
        for person_id, deadline in tuple(self.plan_executor.activity_hold_until.items()):
            state = self.population.states.get(person_id)
            guided_exit = bool(state and any(
                str(event_id) == str(state.current_goal)
                and str(detail.get("command") or "inform") in {"disperse", "evacuate"}
                for event_id, detail in state.known_events.items()
            ))
            if not guided_exit and now + 1e-9 < deadline:
                continue
            self.plan_executor.release_activity_hold(person_id)
            if state is not None:
                state.activity_state = "hotspot_departing"
                state.hotspot_dwell_until = None
                state.next_decision_time = min(state.next_decision_time, now)

    def _abort_resources(self) -> None:
        self._cleanup_resources()

    def _record_close_error(self, stage, exc) -> None:
        message = f"{type(exc).__name__}: {exc}"
        item = {"stage": stage, "error": message}
        if item not in self.close_errors:
            self.close_errors.append(item)
            detail = f"close {stage}: {message}"
            self.last_error = f"{self.last_error}; {detail}" if self.last_error else detail

    def _cleanup_resources(self) -> list[Exception]:
        errors = []
        # Each cleanup is attempted even if the previous one fails.
        for stage, cleanup in (
            ("system_performance", self.performance.close_system),
            ("engine", self.adapter.close),
            ("decision_log", lambda: self.recorder.close_logs(self) if self.recorder is not None else None),
        ):
            try:
                cleanup()
            except Exception as exc:
                errors.append(exc)
                self._record_close_error(stage, exc)
        return errors

    def close(self) -> None:
        if self._close_complete:
            return
        self.performance.flush(self, force=True)
        errors = self._cleanup_resources()
        self.state = RuntimeState.ERROR if errors else RuntimeState.CLOSED
        if self.recorder is not None:
            try:
                self.recorder.write_summary(self)
            except Exception as exc:
                errors.append(exc)
                self._record_close_error("summary", exc)
                self.state = RuntimeState.ERROR
        self._close_complete = not errors
        if errors:
            # Retain every failure in diagnostics, and propagate the first one.
            # A later explicit close may retry unfinished cleanup/publication.
            raise errors[0]

    def set_playback(self, *, speed_factor=None, push_fps=None) -> None:
        if isinstance(speed_factor, (int, float)) and speed_factor > 0:
            self.sim_speed_factor = max(0.05, min(20.0, float(speed_factor)))
        if isinstance(push_fps, (int, float)) and push_fps > 0:
            self.push_fps = max(1.0, min(60.0, float(push_fps)))

    @property
    def demand_count_configurable(self) -> bool:
        return self.demand_mode == "generated_hotspot" or (
            self.demand_mode == "configurable" and self.population.count_configurable
        )

    def configure_requirement(self, record: dict) -> None:
        requirement = record.get("requirement") if isinstance(record, dict) else None
        if not isinstance(requirement, dict):
            raise ValueError("invalid requirement record")
        distributions = requirement.get("population", {}).get("distributions")
        if not isinstance(distributions, dict):
            raise ValueError("requirement population distributions are missing")
        self.requirement_record = json.loads(json.dumps(record, ensure_ascii=False))
        self.requirement_id = str(record.get("requirement_id") or "") or None
        self.population.profile_sampler.configure_distributions(distributions)

    def supports_requirement(self, record: dict) -> bool:
        location_id = record.get("requirement", {}).get("spatial_scope", {}).get("location_id")
        if location_id == "memorial-tower":
            return self.demand_mode == "generated_hotspot"
        return False

    def configure_demand(self, count) -> bool:
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError("count must be a non-negative integer")
        if self.demand_mode == "generated_hotspot":
            self.hotspot_demand_spec.validate_count(count)
        if self.demand_mode == "fixed":
            self.ignored_demand_count = count
            return False
        if not self.demand_count_configurable:
            raise ValueError("count requires a demand file with explicit <person> elements")
        if self.state != RuntimeState.CREATED:
            raise RuntimeError("count can only be changed before SUMO initialization; use reset")
        self.demand_count = count
        return True

    def reset(self, count=None, *, use_default_count=False, seed=None):
        count = self.demand_count if count is None and not use_default_count else count
        config_path = self.config_path
        route_files = self._pedestrian_route_files
        sumo_binary = self._sumo_binary
        extra_args = self._extra_sumo_args
        decision_engine = self.decision_engine
        use_llm = self.use_llm
        scenario_name = self.scenario_name
        demand_mode = self.demand_mode
        timeline_end_seconds = self.timeline_end_seconds
        hotspot_demand_spec = self.hotspot_demand_spec
        requirement_record = self.requirement_record
        random_seed = self.random_seed if seed is None else int(seed)
        self.close()
        self.__init__(
            config_path,
            pedestrian_route_files=route_files,
            sumo_binary=sumo_binary,
            extra_sumo_args=extra_args,
            decision_engine=decision_engine,
            use_llm=use_llm,
            scenario_name=scenario_name,
            demand_mode=demand_mode,
            timeline_end_seconds=timeline_end_seconds,
            hotspot_demand_spec=hotspot_demand_spec,
            requirement_record=requirement_record,
            random_seed=random_seed,
        )
        if count is not None:
            self.configure_demand(count)
        return self.initialize()

    @timed('frame_build')
    def frame(self) -> dict:
        return self.serializer.build_frame(self)

    def init_frame(self) -> dict:
        return {**self.serializer.build_init(self), 'performance_measurement': {
            'enabled': self.performance.enabled, 'interval_wall_seconds': self.performance.interval, 'version': 1,
        }}

    @timed('plan_apply_including_log')
    def apply_plan(self, plan):
        if self.current is None or self.plan_executor is None:
            raise RuntimeError("runtime is not initialized")
        motion = self.current.persons.get(plan.person_id)
        if motion is None:
            raise ValueError(f"person is not active: {plan.person_id}")
        result = self.plan_executor.apply(plan, motion, self.snapshot_id, self.time_seconds)
        if result.status == "partial_failure":
            self.last_error = result.reason
            self.state = RuntimeState.ERROR
        if result.status == "applied":
            state = self.population.states[plan.person_id]
            state.current_plan = plan
            if plan.proposed_action in {"reroute", "change_goal"}:
                state.current_goal = plan.target_id
                state.poi_plan_active = plan.proposed_action == "change_goal" and plan.activity_duration is not None
                state.pending_goal = plan.next_target_id if state.poi_plan_active else None
                if plan.selected_entry_edge is not None:
                    state.hotspot_entry_edge = plan.selected_entry_edge
                    state.hotspot_last_route_change_time = self.time_seconds
            if plan.proposed_action == "wait":
                state.planned_wait_until = plan.wait_until
            elif plan.proposed_action == "continue":
                state.planned_wait_until = None
            hold_until = plan.wait_until or (self.time_seconds + self.decision_scheduler.period_seconds)
            state.next_decision_time = max(state.next_decision_time, hold_until)
        if self.recorder is not None:
            with self.performance.measure('decision_log'):
                self.recorder.record_decision(
                    plan,
                    result,
                    context={
                        "profile": self.population.profile_for(plan.person_id),
                        "state": self.population.states.get(plan.person_id),
                        "observation": self.observations.get(plan.person_id),
                    },
                    candidates=self.decision_candidates.get(plan.person_id, ()),
                )
        return result

    @property
    def decision_diagnostics(self) -> dict:
        return self.decision_scheduler.diagnostics()

    def profile_for(self, person_id: str) -> AgentProfile:
        return self.population.profile_for(person_id)

    def local_people_count(self, person_id: str) -> int:
        observation = self.observations.get(person_id)
        return observation.local_people_count if observation else 0

    def local_density_for(self, person_id: str):
        observation = self.observations.get(person_id)
        return observation.objective_density_per_m2 if observation else None

    def density_level_for(self, person_id: str) -> str:
        observation = self.observations.get(person_id)
        return self.environment.classify_density(observation.objective_density_per_m2) if observation and self.environment else "unknown"

    def density_level_counts(self) -> dict:
        result = {"free": 0, "busy": 0, "crowded": 0, "critical": 0, "unknown": 0}
        for person_id in self.current.persons if self.current else ():
            result[self.density_level_for(person_id)] += 1
        return result

    def congestion_for(self, person_id: str) -> float:
        observation = self.observations.get(person_id)
        if observation is None or observation.objective_density_per_m2 is None:
            return 0.0
        # Display score only: objective density remains the authoritative metric.
        # 0.5 person/m2 starts visible load and 3.5 person/m2 maps to full severity.
        return max(0.0, min(1.0, (observation.objective_density_per_m2 - 0.5) / 3.0))

    def hazard_impact_for(self, person_id: str) -> float:
        motion = self.current.persons.get(person_id) if self.current else None
        return self.hazards.impact_for(motion) if motion else 0.0

    def set_flood(self, data: dict) -> None:
        if data.get("mode") == "clear":
            self.hazards.replace_type("flood", ())
        elif data.get("mode") == "static_points":
            if self.network is None:
                raise RuntimeError("runtime is not initialized")
            zones = []
            for index, item in enumerate(data.get("points", ())):
                lon = float(item.get("lng", item.get("lon", item.get("longitude"))))
                lat = float(item.get("lat", item.get("latitude")))
                x, y = self.network.lonlat_to_xy(lon, lat)
                zones.append(HazardZone(str(item.get("id") or f"flood-{index}"), "flood", x, y, max(1.0, float(item.get("radius", data.get("radius", 30)))), max(0.0, min(1.0, float(item.get("depth", item.get("currentDepth", 0.25))))), 0.75, lon=lon, lat=lat))
            self.hazards.replace_type("flood", zones)
        else:
            raise ValueError("only static_points and clear flood modes are supported")
        self.flood_points = self.hazards.serialized_points()

    def set_event(self, data: dict) -> None:
        if self.event_manager is None:
            raise RuntimeError("runtime is not initialized")
        before_ids = {event.id for event in self.event_manager.events}
        event = self.event_manager.apply(data, self.time_seconds, self.snapshot_index)
        if event is None:
            after_ids = {item.id for item in self.event_manager.events}
            for removed_id in before_ids - after_ids:
                self.hazards.zones.pop(removed_id, None)
            self.serialized_events = self.event_manager.serialize(self.time_seconds)
            return
        defaults = event_defaults(event.event_type)
        if defaults["physical"]:
            self.hazards.add(HazardZone(event.id, event.event_type, event.x, event.y, event.radius, event.intensity, float(defaults["speed_reduction"]), event.expires_at))
        if event.event_type in {"alarm", "rumor"}:
            from crowdsim.environment.information_model import InformationMessage
            self.information.publish(InformationMessage(f"message-{event.id}", str(data.get("message") or event.event_type), str(defaults["message_source"]), self.time_seconds, self.time_seconds + max(0.0, float(data.get("messageDelay", 0))), event.expires_at, event.x, event.y, event.radius, event.id))
        self.serialized_events = self.event_manager.serialize(self.time_seconds)

    def apply_policy(self, data: dict) -> dict:
        name = str(data.get("decision") or data.get("policy") or data.get("name") or "")
        record = self.interventions.apply_command(name, data, self.time_seconds)
        self.active_policy = name
        self.event_state = {"crowd_gathering": {"active": bool(name), "decision": name, "step": self.snapshot_index}}
        return record

    def apply_control_action(self, data: dict) -> dict:
        from crowdsim.domain.action_contract import validate_control_action

        action = validate_control_action(data.get("controlAction") or data.get("control_action") or data)
        action_type = action["actionType"]
        target = action.get("target") or {}
        parameters = action.get("parameters") or {}
        detail: dict = {"actionId": action["actionId"], "affectedAgents": 0}
        if action_type == "set_inflow_rate":
            if self.current is None or self.plan_executor is None:
                raise RuntimeError("runtime is not initialized")
            edge_id = str(target.get("entryId") or "")
            if self.network is None or edge_id not in self.network.edges:
                raise ValueError(f"unknown target edge: {edge_id}")
            rate = float(parameters["rate"])
            edge = self.network.edges[edge_id]
            width = sum(lane.getWidth() for lane in edge.getLanes() if lane.allows("pedestrian"))
            nominal_capacity = max(0.5, width * 1.3)
            self._inflow_meters[action["actionId"]] = InflowMeter(
                action_id=action["actionId"], entry_id=edge_id, rate=rate,
                nominal_capacity_per_second=nominal_capacity, last_time=self.time_seconds,
            )
            self._inflow_holds[action["actionId"]] = set()
            self._refresh_inflow_controls()
            held = self._inflow_holds.get(action["actionId"], set())
            detail.update({
                "targetEdge": edge_id, "effectiveRate": rate,
                "nominalCapacityPerSecond": nominal_capacity,
                "affectedAgents": len(held), "controlMode": "token_bucket_entry_meter",
            })
        elif action_type == "set_edge_capacity":
            if self.current is None or self.plan_executor is None:
                raise RuntimeError("runtime is not initialized")
            edge_id = str(target.get("edgeId") or "")
            if self.network is None or edge_id not in self.network.edges:
                raise ValueError(f"unknown target edge: {edge_id}")
            factor = float(parameters.get("multiplier"))
            limits = {}
            for person_id, motion in self.current.persons.items():
                if motion.edge_id == edge_id:
                    profile = self.population.profile_for(person_id)
                    limits[person_id] = max(0.0, profile.free_walking_speed * profile.mobility * factor)
            self._control_speed_limits[action["actionId"]] = limits
            self._refresh_control_speed_limits()
            detail.update({"targetEdge": edge_id, "effectiveFactor": factor, "affectedAgents": len(limits)})
        elif action_type == "set_edge_risk_weight":
            if self.route_provider is None:
                raise RuntimeError("routing is not initialized")
            edge_id = str(target["edgeId"])
            if self.network is None or edge_id not in self.network.edges:
                raise ValueError(f"unknown target edge: {edge_id}")
            weight = float(parameters["weight"])
            self._control_risk_weights[action["actionId"]] = (edge_id, weight)
            self._refresh_control_risk_weights()
            detail.update({"targetEdge": edge_id, "effectiveWeight": weight})
        elif action_type == "reroute_group":
            detail.update(self._reroute_people(
                group_id=str(target["groupId"]),
                route_edges=tuple(map(str, parameters["routeEdges"])),
            ))
        elif action_type == "set_route_distribution":
            detail.update(self._apply_route_distribution(action))
        record = self.interventions.apply_command(action_type, action, self.time_seconds)
        record["detail"].update(detail)
        record["decisionId"] = data.get("decisionId")
        self.active_policy = action_type
        return record

    def _reroute_people(self, *, group_id: str, route_edges: tuple[str, ...]) -> dict:
        if self.current is None or self.plan_executor is None or self.route_provider is None:
            raise RuntimeError("runtime is not initialized")
        self.route_provider.validate_edges(route_edges)
        matched = [person_id for person_id, state in self.population.states.items() if state.group_id == group_id]
        result = self._reroute_person_ids(matched, route_edges)
        return {"groupId": group_id, "matchedAgents": len(matched), **result}

    def _reroute_person_ids(self, matched, route_edges: tuple[str, ...]) -> dict:
        applied, failed = 0, 0
        from crowdsim.domain.crowdsim_models import BehaviorPlan
        for person_id in matched:
            motion = self.current.persons.get(person_id)
            if motion is None:
                continue
            route = route_edges
            if route[0] != motion.edge_id:
                failed += 1
                continue
            try:
                plan = BehaviorPlan(
                    person_id=person_id,
                    snapshot_id=self.snapshot_id,
                    proposed_action="reroute",
                    route_edges=route,
                    arrival_position=None,
                    reason="MACE group control",
                    source="external_control",
                    decided_at=self.time_seconds,
                    expires_at=self.time_seconds + 30,
                    preserve_future_stages=False,
                )
                result = self.plan_executor.apply(plan, motion, self.snapshot_id, self.time_seconds)
                applied += result.status == "applied"
                failed += result.status != "applied"
            except (RuntimeError, ValueError):
                failed += 1
        return {"affectedAgents": applied, "failedAgents": failed}

    def _apply_route_distribution(self, action: dict) -> dict:
        if self.current is None or self.route_provider is None:
            raise RuntimeError("runtime is not initialized")
        origin = str(action["target"]["originId"])
        choices = []
        for item in action["parameters"]["distributions"]:
            route = item.get("routeEdges")
            if not isinstance(route, list) or not route:
                raise ValueError("each executable route distribution requires routeEdges")
            route_edges = tuple(map(str, route))
            self.route_provider.validate_edges(route_edges)
            if route_edges[0] != origin:
                raise ValueError(f"route {item['routeId']} must start at origin edge {origin}")
            choices.append((float(item["ratio"]), str(item["routeId"]), route_edges))
        candidates = sorted(person_id for person_id, motion in self.current.persons.items() if motion.edge_id == origin)
        applied = failed = 0
        cursor = 0.0
        thresholds = []
        for ratio, route_id, route in choices:
            cursor += ratio
            thresholds.append((cursor, route_id, route))
        from hashlib import sha256
        for person_id in candidates:
            unit = int.from_bytes(sha256(f"{action['actionId']}:{person_id}".encode()).digest()[:8], "big") / 2**64
            _, _, route = next(item for item in thresholds if unit <= item[0] + 1e-12)
            result = self._reroute_person_ids([person_id], route)
            applied += result["affectedAgents"]
            failed += result["failedAgents"]
        return {"originId": origin, "matchedAgents": len(candidates), "affectedAgents": applied, "failedAgents": failed}

    def _expire_control_actions(self) -> None:
        expired = self.interventions.expire(self.time_seconds)
        changed_speed = changed_risk = False
        for record in expired:
            action_id = record["actionId"]
            changed_speed = self._control_speed_limits.pop(action_id, None) is not None or changed_speed
            changed_risk = self._control_risk_weights.pop(action_id, None) is not None or changed_risk
            if action_id in self._inflow_meters:
                self._inflow_meters.pop(action_id, None)
                for person_id in self._inflow_holds.pop(action_id, set()):
                    if self.plan_executor is not None:
                        self.plan_executor.set_inflow_hold(person_id, action_id, False)
        if changed_speed:
            self._refresh_control_speed_limits()
        if changed_risk:
            self._refresh_control_risk_weights()

    def _refresh_control_speed_limits(self) -> None:
        if self.plan_executor is None:
            return
        controlled = set().union(*(set(value) for value in self._control_speed_limits.values())) if self._control_speed_limits else set()
        existing = set(self.plan_executor.strategy_limits)
        for person_id in existing | controlled:
            limits = [mapping[person_id] for mapping in self._control_speed_limits.values() if person_id in mapping]
            self.plan_executor.set_strategy_limit(person_id, min(limits) if limits else None)

    def _refresh_dynamic_control_targets(self) -> None:
        if self.current is None:
            return
        self._refresh_inflow_controls()
        active = {item["actionId"]: item for item in self.interventions.active_controls()}
        changed = False
        for action_id, record in active.items():
            action_type = record.get("actionType")
            if action_type != "set_edge_capacity":
                continue
            target, parameters = record.get("target") or {}, record.get("parameters") or {}
            edge_id = str(target.get("edgeId") or "")
            factor = float(parameters.get("multiplier"))
            limits = {}
            for person_id, motion in self.current.persons.items():
                if motion.edge_id == edge_id:
                    profile = self.population.profile_for(person_id)
                    limits[person_id] = max(0.0, profile.free_walking_speed * profile.mobility * factor)
            if self._control_speed_limits.get(action_id) != limits:
                self._control_speed_limits[action_id] = limits
                changed = True
        if changed:
            self._refresh_control_speed_limits()

    def _refresh_inflow_controls(self) -> None:
        if self.current is None or self.plan_executor is None:
            return
        for action_id, meter in self._inflow_meters.items():
            candidates = self._entry_meter_candidates(meter.entry_id)
            held, _ = meter.regulate(candidates, self.time_seconds)
            previous = self._inflow_holds.get(action_id, set())
            for person_id in previous - held:
                self.plan_executor.set_inflow_hold(person_id, action_id, False)
            for person_id in held - previous:
                self.plan_executor.set_inflow_hold(person_id, action_id, True)
            self._inflow_holds[action_id] = held
            lifecycle_record = self.interventions.lifecycle.active.get(action_id)
            if lifecycle_record is not None:
                lifecycle_record.setdefault("detail", {}).update({
                    "currentlyHeldAgents": len(held),
                    "admittedAgents": len(meter.admitted),
                    "controlMode": "token_bucket_entry_meter",
                })

    def _entry_meter_candidates(self, entry_id: str) -> set[str]:
        if self.current is None:
            return set()
        candidates = set()
        for person_id, motion in self.current.persons.items():
            if motion.stage_type != tc.STAGE_WALKING or motion.edge_id.startswith(":"):
                continue
            try:
                route = tuple(map(str, self.adapter.current_person_stage(person_id).edges))
            except Exception:
                continue
            positions = [index for index, edge_id in enumerate(route) if edge_id == motion.edge_id]
            if any(index + 1 < len(route) and route[index + 1] == entry_id for index in positions):
                candidates.add(person_id)
        return candidates

    def _refresh_control_risk_weights(self) -> None:
        if self.route_provider is None:
            return
        edges = set(self.route_provider.dynamic_risk_weights)
        edges.update(edge_id for edge_id, _ in self._control_risk_weights.values())
        for edge_id in edges:
            weights = [weight for candidate, weight in self._control_risk_weights.values() if candidate == edge_id]
            self.route_provider.set_edge_risk_weight(edge_id, max(weights) if weights else None)

    def _config_root(self):
        return ET.parse(self.config_path).getroot()

    def _config_float(self, section: str, name: str, default: float) -> float:
        element = self._config_root().find(f"./{section}/{name}")
        try:
            return float(element.get("value")) if element is not None else default
        except (TypeError, ValueError):
            return default

    def _net_path_from_config(self) -> Path:
        element = self._config_root().find("./input/net-file")
        if element is None or not element.get("value"):
            raise ValueError(f"No net-file in {self.config_path}")
        return (self.config_path.parent / element.get("value")).resolve()

    def _route_files_from_config(self) -> list[Path]:
        element = self._config_root().find("./input/route-files")
        if element is None:
            return []
        return [(self.config_path.parent / value.strip()).resolve() for value in element.get("value", "").split(",") if value.strip()]

    def diagnostics(self) -> dict:
        return {
            "state": self.state.value,
            "snapshot_index": self.snapshot_index,
            "time_seconds": self.time_seconds,
            "last_error": self.last_error,
            "close_errors": list(self.close_errors),
            "engine": self.adapter.diagnostics,
            "decision_engine": self.decision_diagnostics,
            "decision_log": self.recorder.decision_log_diagnostics if self.recorder else None,
            "performance": {"enabled": self.performance.enabled, "error": self.performance.error,
                            "system": self.performance.system_metadata()},
            "population": self.population.diagnostics(),
            "scenario": self.scenario_diagnostics(),
            "demand": self.demand_diagnostics(),
            "requirement": self.requirement_diagnostics(),
            "routing": self.routing_diagnostics,
        }

    @property
    def routing_diagnostics(self) -> dict:
        return {**(self.route_provider.diagnostics if self.route_provider else {}),
                "execution": {**self.plan_executor.diagnostics, "cooldown_people": sum(
                    deadline > self.time_seconds for deadline in self.plan_executor.route_retry_after.values())}
                if self.plan_executor else {}}

    def scenario_diagnostics(self) -> dict:
        steps = None
        if self.timeline_end_seconds is not None:
            steps = int(round(self.timeline_end_seconds / self.step_length))
        configured_hotspots = set(getattr(self.population, "hotspot_ids", {}).values())
        active_hotspot_id = next(iter(configured_hotspots)) if len(configured_hotspots) == 1 else None
        return {
            "name": self.scenario_name,
            "active_hotspot_id": active_hotspot_id,
            "timeline_start_seconds": 0.0,
            "timeline_end_seconds": self.timeline_end_seconds,
            "timeline_step_count": steps,
        }

    def demand_diagnostics(self) -> dict:
        return {
            **self.population.diagnostics(),
            **self.demand_capabilities,
            "mode": self.demand_mode,
            "requested_count": self.demand_count,
            "effective_count": len(self.population.ledger.planned_ids),
            "count_configurable": self.demand_count_configurable,
            "requested_count_ignored": self.ignored_demand_count,
        }

    def requirement_diagnostics(self) -> dict | None:
        return requirement_runtime_summary(self.requirement_record)

    def __enter__(self) -> "SimulationRuntime":
        self.initialize()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()
