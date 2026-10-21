"""Hotspot execution regressions using static networks and fake adapters only."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

import pytest
from shapely.geometry import LineString, Polygon
from traci import constants as tc

from crowdsim.core.simulation_runtime import SimulationRuntime
from crowdsim.decision.plan_executor import PlanExecutor
from crowdsim.decision.route_provider import RouteProvider
from crowdsim.domain.crowdsim_models import AgentState, MotionSnapshot
from crowdsim.domain.hotspot_activity import requirement_hotspot_activity, activity_phase
from crowdsim.domain.requirement_spec import RequirementSpec
from crowdsim.infrastructure.network_adapter import ResearchNetwork
from crowdsim.infrastructure.sumo_adapter import SumoStepResult
from crowdsim.experiments.during_observation import normal_early_completion, verify_initialization
from crowdsim.scenarios.generated_hotspot_demand import HotspotDemandSpec
from tests.test_requirement_location_v2 import located_requirement

ROOT = Path(__file__).resolve().parents[1]
BUND = ROOT / "scenarios/shanghai_bund"


@pytest.fixture(autouse=True)
def no_live_io(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Live model/network/process calls are forbidden")
    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr("socket.socket.connect", forbidden)


def requirement(start=300, end=900, duration=60):
    raw = located_requirement(True)
    raw.update(research={"problem_type": 1}, experiment={"duration_seconds": duration, "repetitions": 3})
    raw["scenario"].update(hotspot_start_seconds=start, hotspot_end_seconds=end)
    return RequirementSpec.parse(raw).payload


def test_activity_clock_is_opt_in_and_independent_of_the_observation_limit():
    raw = requirement(900, 1200, 60)
    original = deepcopy(raw)
    activity = requirement_hotspot_activity(raw)
    assert activity["start_seconds"] == 900 and activity["end_seconds"] == 1200
    assert [activity_phase(activity, t) for t in (0, 900, 1200)] == ["before_start", "active", "ended"]
    assert raw == original
    raw["scenario"]["hotspot_end_seconds"] = None
    assert activity_phase(requirement_hotspot_activity(raw), 100000) == "active"
    for patch in ({"schema_version": 1}, {"research": {"problem_type": 2}},
                  {"spatial_scope": {"location_id": "east-nanjing-road"}}, {"scenario": {}}):
        assert requirement_hotspot_activity({**raw, **patch}) is None


def test_reset_preserves_the_confirmed_clock_and_limit_but_clears_per_round_exit_state():
    spec = HotspotDemandSpec(BUND / "bund_ped.rou.xml", ROOT / "config/crowd_hotspots.json")
    raw = requirement(900, None, 60)
    record = {"requirement_id": "fake-requirement", "requirement": raw}
    r = SimulationRuntime(BUND / "bund.hotspot.sumocfg", demand_mode="generated_hotspot",
                          hotspot_demand_spec=spec, requirement_record=record, random_seed=10)
    r._hotspot_exit_applied.add("previous-visitor")
    previous_id = r.run_id
    with patch.object(SimulationRuntime, "close"), patch.object(SimulationRuntime, "initialize") as initialize:
        r.reset(seed=11)
    initialize.assert_called_once_with()
    assert r.run_id != previous_id and r._hotspot_exit_applied == set()
    assert r._hotspot_activity == requirement_hotspot_activity(raw)
    assert r.timeline_end_seconds == 60 and r.hotspot_demand_spec.seed == 11
    assert r.requirement_record == record
    ordinary = SimulationRuntime(BUND / "bund.hotspot.sumocfg", demand_mode="generated_hotspot", hotspot_demand_spec=spec)
    assert ordinary._hotspot_activity is None


@pytest.fixture(scope="module")
def network():
    return ResearchNetwork(str(BUND / "bund.net.xml"))


@pytest.mark.parametrize("end", [1200, None])
def test_generation_keeps_absolute_times_open_ends_and_real_external_exits(tmp_path, network, end):
    raw = requirement(900, end, 60)
    activity = requirement_hotspot_activity(raw)
    scope = Polygon([network.lonlat_to_xy_strict(*point) for point in raw["spatial_scope"]["boundary"]["coordinates"][0]])
    config = ROOT / "config/crowd_hotspots.json"
    original = config.read_bytes()
    spec = HotspotDemandSpec(BUND / "bund_ped.rou.xml", config)
    output, report = spec.generate(tmp_path / "new", BUND / "bund.net.xml", 12,
                                   activity=activity, observation_scope=scope)
    assert config.read_bytes() == original
    assert report["requirement_activity"] == activity
    assert report["timeline_shift_seconds"] == 0
    assert report["visitor_release_window_seconds"] == ([end, end] if end is not None else None)
    assert report["generation_version"] == 4
    for person in ET.parse(output).getroot().findall("person"):
        params = {p.get("key"): p.get("value") for p in person.findall("param")}
        release = params.get("crowdsim.hotspot_release_time")
        assert (release is None) if end is None else float(release) == end
        assert "crowdsim.hotspot_dwell_seconds" not in params
        walks = person.findall("walk")
        assert len(walks) == 2 and not person.findall("stop")
        edge_id = walks[-1].get("edges").split()[-1]
        lanes = [lane for lane in network.edges[edge_id].getLanes() if lane.allows("pedestrian")]
        assert all(not scope.intersects(LineString(lane.getShape())) for lane in lanes)
    assert json.loads((output.parent / "demand_generation.json").read_text())["requirement_activity"] == activity
    # Every existing spawn road can cancel its approach and reach an external
    # exit from a real position, including park dead ends and narrow portals.
    configured = json.loads((output.parent / "hotspot.config.json").read_text())
    hotspot = next(h for h in configured["hotspots"] if h["id"] == report["hotspot_id"])
    provider = RouteProvider(network, Mock())
    exit_edge = next(iter(report["destination_edge_counts"]))
    for edge_id in hotspot["visitor_spawn_edges"]:
        length = network.edges[edge_id].getLength()
        at = MotionSnapshot("check", 1200, 0, 0, 0, 0, 1, edge_id, f"{edge_id}_0", length / 2, 0, 0, tc.STAGE_WALKING)
        forbidden = set(hotspot["excluded_edges"])
        if edge_id not in hotspot["target_edges"]:
            forbidden.update(hotspot["target_edges"])
        candidate = provider.build_candidate(at, target_id="exit", target_edge=exit_edge,
                                             arrival_position=network.edges[exit_edge].getLength() / 2,
                                             forbidden_edges=forbidden - {edge_id})
        assert candidate.edges[0] == edge_id and candidate.edges[-1] == exit_edge
    # A later round rereads the frozen clock and uses its own seed; no mutable
    # overrides leak to the reusable spec or its ordinary generated-demand API.
    _, legacy = spec.generate(tmp_path / "legacy", BUND / "bund.net.xml", 2)
    assert legacy["activity_window_seconds"] == [600, 800]
    assert legacy["generation_version"] == 3 and "requirement_activity" not in legacy
    repeat, _ = spec.generate(tmp_path / "repeat", BUND / "bund.net.xml", 12,
                              activity=activity, observation_scope=scope)
    assert repeat.read_bytes() == output.read_bytes()


def motion(time=10, stages=1, edge="ring"):
    return MotionSnapshot("visitor", time, 1, 2, 0, 0, 1, edge, f"{edge}_0", 5, 0, 0,
                          tc.STAGE_WALKING, remaining_stage_count=stages)


def runtime(start=300, end=900, time=10, stages=1, edge="ring"):
    instance = SimulationRuntime.__new__(SimulationRuntime)
    instance._hotspot_activity = requirement_hotspot_activity(requirement(start, end))
    instance._hotspot_exit_applied = set()
    instance.run_id, instance.snapshot_index = "fake-run", 2
    instance.adapter = Mock()
    instance.route_provider = Mock()
    instance.route_provider.build_candidate.return_value = SimpleNamespace(
        target_id="hotspot-exit:outside", edges=(edge, "outside"), arrival_position=20)
    instance.adapter.remaining_person_stages.return_value = [SimpleNamespace(edges=(edge, "outside"), arrivalPos=20)]
    instance.plan_executor = PlanExecutor(instance.adapter, instance.route_provider)
    instance.plan_executor.base_limits["visitor"] = 1.2
    instance.population = SimpleNamespace(hotspot_ids={"visitor": "monument"},
        hotspot_target_edges={"visitor": "ring"}, hotspot_dwell_seconds={"visitor": 30},
        hotspot_release_times={"visitor": 890}, states={"visitor": AgentState("visitor")})
    instance.current = SumoStepResult(time, {"visitor": motion(time, stages, edge)}, {}, (), ())
    instance.hotspot_catalog = SimpleNamespace(hotspots={"monument": {"target_edges": ["ring"], "excluded_edges": []}})
    instance.apply_plan = Mock(return_value=SimpleNamespace(status="applied"))
    return instance


def test_early_arrival_waits_at_position_then_participates_until_exact_end():
    r = runtime()
    r._update_hotspot_activity(r.current)
    state = r.population.states["visitor"]
    assert state.activity_state == "hotspot_dwelling" and state.hotspot_activity_phase == "before_start"
    assert r.plan_executor.activity_hold_until == {"visitor": 900}
    r._maintain_hotspot_activity(300)
    assert state.hotspot_activity_phase == "active"
    r._maintain_hotspot_activity(899.5)
    assert "visitor" in r.plan_executor.activity_hold_until
    r._maintain_hotspot_activity(900)
    assert state.activity_state == "hotspot_departing" and state.hotspot_activity_phase == "ended"
    assert not r.plan_executor.activity_hold_until
    r.adapter.set_person_speed.assert_called_with("visitor", 1.2)
    r.apply_plan.assert_not_called()  # Native outward walk is preserved.


def test_open_end_never_uses_old_release_or_relative_dwell_and_can_be_released():
    r = runtime(end=None)
    r._update_hotspot_activity(r.current)
    assert r.plan_executor.activity_hold_until == {"visitor": None}
    assert r.population.states["visitor"].hotspot_dwell_until is None
    r._maintain_hotspot_activity(10000)
    assert r.plan_executor.effective_speed("visitor") == 0
    r.plan_executor.release_activity_hold("visitor")
    assert r.plan_executor.effective_speed("visitor") == 1.2
    r.adapter.set_person_speed.assert_called_with("visitor", 1.2)


def test_end_redirects_approaching_visitors_once_without_new_activity_or_external_control():
    r = runtime(time=900, stages=2, edge="park")
    r._maintain_hotspot_activity(900)
    plan = r.apply_plan.call_args.args[0]
    assert plan.proposed_action == "change_goal" and plan.route_edges == ("park", "outside")
    assert plan.activity_duration is None and not plan.next_route_edges
    assert "ring" in r.route_provider.build_candidate.call_args.kwargs["forbidden_edges"]
    assert r.population.states["visitor"].activity_state == "hotspot_departing"
    assert not r.plan_executor.activity_hold_until
    r._maintain_hotspot_activity(900.5)
    assert r.apply_plan.call_count == 1


def test_junction_redirect_is_deferred_and_failed_route_is_held_for_safe_retry():
    r = runtime(time=900, stages=2, edge=":junction")
    r._maintain_hotspot_activity(900)
    r.apply_plan.assert_not_called()
    r.current = SumoStepResult(901, {"visitor": motion(901, 2, "park")}, {}, (), ())
    r.route_provider.build_candidate.side_effect = ValueError("route unavailable")
    r._maintain_hotspot_activity(901)
    assert "visitor" in r.plan_executor.activity_hold_until and "visitor" not in r._hotspot_exit_applied
    r._maintain_hotspot_activity(902)
    assert r.route_provider.build_candidate.call_count == 1
    r.route_provider.build_candidate.side_effect = None
    r._maintain_hotspot_activity(911)
    assert r.apply_plan.call_count == 1 and "visitor" in r._hotspot_exit_applied


def test_late_arrivals_do_not_enter_a_new_hold_after_end():
    r = runtime(time=901)
    r._update_hotspot_activity(r.current)
    assert not r.plan_executor.activity_hold_until
    r._maintain_hotspot_activity(901)
    assert r.population.states["visitor"].activity_state == "hotspot_departing"


def completed_evidence(raw, time=45):
    count = raw["population"]["total"]
    return {"activity": requirement_hotspot_activity(raw), "state": "FINISHED",
            "end_reason": "no_expected_entities", "time_seconds": time,
            "population": {"planned": count, "departed": count, "arrived": count, "active": 0,
                           "explicitly_removed": 0, "unknown_disappearances": [], "conservation_error": 0}}


def test_normal_early_completion_needs_full_terminal_and_conservation_evidence():
    raw = requirement(0, 30, 60)
    evidence = completed_evidence(raw)
    assert normal_early_completion(evidence, raw, 45)
    for patch in ({"state": "CLOSED"}, {"end_reason": "timeline_end"}, {"time_seconds": 44},
                  {"activity": None}, {"population": {}}):
        assert not normal_early_completion({**evidence, **patch}, raw, 45)
    for key, value in (("active", 1), ("explicitly_removed", 1), ("unknown_disappearances", ["lost"]),
                       ("conservation_error", 1), ("arrived", 1), ("departed", True)):
        bad = deepcopy(evidence)
        bad["population"][key] = value
        assert not normal_early_completion(bad, raw, 45)
    assert not normal_early_completion(evidence, requirement(0, None), 45)
    assert not normal_early_completion(evidence, requirement(0, 50), 45)


def test_initialization_rejects_old_runtime_before_start_and_preserves_binding():
    raw = requirement()
    binding = {"requirement": raw, "requirementId": "req", "requirementFingerprint": "req-fp",
               "modelId": "model", "modelFingerprint": "model-fp", "snapshotId": "snapshot"}
    frame = {"run_id": "run", "requirement": {"requirement_id": "req", "fingerprint": "req-fp"},
             "artificial_society": {"model_id": "model", "fingerprint": "model-fp"},
             "observation_geometry": {"area": 10}}
    original = deepcopy(binding)
    with pytest.raises(RuntimeError, match="未应用当前热点活动时间"):
        verify_initialization(binding, frame)
    frame["scenario"] = {"hotspot_activity": requirement_hotspot_activity(raw)}
    saved = verify_initialization(binding, frame)
    assert saved["hotspotActivity"] == frame["scenario"]["hotspot_activity"]
    assert saved["snapshotId"] == "snapshot" and binding == original
