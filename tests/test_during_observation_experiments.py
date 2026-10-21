"""Frozen class-one adapter and queue/report checks; every I/O boundary is fake."""
from copy import deepcopy

import pytest

from crowdsim.domain.requirement_spec import RequirementSpec
from crowdsim.domain.artificial_society_spec import ArtificialSocietySpec
from crowdsim.infrastructure.requirement_repository import RequirementRepository
from crowdsim.infrastructure.artificial_society_repository import ArtificialSocietyRepository
from crowdsim.experiments.contracts import ContractError, validate_experiment_config
from crowdsim.experiments.during_observation import observation_config, verify_initialization
from crowdsim.experiments.orchestrator import ExperimentOrchestrator
from crowdsim.experiments.repository import CrowdSimRepository
from crowdsim.experiments.report_service import ReportService
from crowdsim.experiments.controller_registry import ControllerRegistry
from tests.test_requirement_location_v2 import located_requirement
from tests.test_artificial_society_submission import artificial_society_payload
from test_during_strategy import request


@pytest.fixture
def study(tmp_path, monkeypatch):
    requirements = RequirementRepository(tmp_path / "requirements")
    models = ArtificialSocietyRepository(tmp_path / "models", requirements=requirements)
    raw = located_requirement(True)
    raw.update(research={"problem_type": 1}, experiment={"duration_seconds": 60, "repetitions": 3})
    raw["population"]["total"] = 24
    raw["population"]["initial_distribution"] = "road-length-weighted"
    raw["scenario"].update(activity="sightseeing-600-800", additional_events=[])
    raw["population"]["distributions"]["crowd_role"] = [{"code": "commuter", "label": "通勤者", "percent": 100}]
    raw["observation"]["metric_ids"] = ["global-density", "global-speed", "local-density", "local-speed", "boundary-density-difference", "concise-simulation-summary"]
    record = requirements.create(RequirementSpec.parse(raw))
    model = models.create(ArtificialSocietySpec.parse(artificial_society_payload(record)))
    payload = {"conversationId": "check", "snapshotId": "snapshot-1", "clientRequestId": "request-1",
               "requirementId": record["requirement_id"], "requirementFingerprint": record["fingerprint"],
               "modelId": model["model_id"], "modelFingerprint": model["fingerprint"]}
    monkeypatch.setattr("crowdsim.experiments.during_observation.RequirementRepository", lambda: requirements)
    monkeypatch.setattr("crowdsim.experiments.during_observation.ArtificialSocietyRepository", lambda **kwargs: models)
    return payload, record, model


def test_adapter_uses_confirmed_config_and_rejects_bypasses(study):
    payload, record, model = study
    config = observation_config(payload, "during-check")
    assert config["scenario"]["population"] == 24  # Never substitute the old 700-person preset.
    assert config["scenario"]["durationSeconds"] == 60
    assert config["duringObservation"]["requirement"] == record["requirement"]
    assert config["duringObservation"]["artificialSociety"] == model["artificial_society"]
    assert config["methods"] == ["C0"] and len(set(config["scenario"]["seedSet"])) == 3
    for patch in ({"requirementFingerprint": "stale"}, {"modelFingerprint": "stale"}, {"snapshotId": ""}, {"clientRequestId": ""}):
        with pytest.raises(ContractError):
            observation_config({**payload, **patch}, "during-check")
    with pytest.raises(ContractError):
        observation_config(payload, "during-other")
    with pytest.raises(ContractError):
        validate_experiment_config(config)


@pytest.mark.parametrize("section,field,value", [
    ("scenario", "additional_events", ["火灾"]),
    ("scenario", "event_category", {"id": "sports"}),
    ("observation", "local_partition_mode", "random"),
    ("observation", "custom_request", "自定义密度公式"),
    ("experiment", "intervention_request", "限制入口"),
    ("experiment", "baseline_control", "inflow-limit"),
])
def test_unimplemented_requested_behavior_is_rejected_without_substitution(study, tmp_path, section, field, value):
    payload, record, _ = study
    raw = deepcopy(record["requirement"])
    raw[section][field] = value
    requirements = RequirementRepository(tmp_path / "requirements")
    changed = requirements.create(RequirementSpec.parse(raw))
    models = ArtificialSocietyRepository(tmp_path / "models", requirements=requirements)
    model = models.create(ArtificialSocietySpec.parse(artificial_society_payload(changed)))
    with pytest.raises(ContractError):
        observation_config({**payload, "requirementId": changed["requirement_id"], "requirementFingerprint": changed["fingerprint"],
                            "modelId": model["model_id"], "modelFingerprint": model["fingerprint"]}, "during-check")


@pytest.mark.parametrize("initial_distribution,activity", [
    ("在区域内道路上均匀分布", "向纪念塔聚集"),
    ("custom-distribution", "custom-activity"),
    ("", ""),
    (None, None),
    ("missing", "missing"),
])
def test_runtime_presets_accept_requirement_text_without_rewriting_versions(
        study, tmp_path, monkeypatch, initial_distribution, activity):
    payload, record, _ = study
    raw = deepcopy(record["requirement"])
    for section, field, value in (("population", "initial_distribution", initial_distribution),
                                   ("scenario", "activity", activity)):
        if value == "missing":
            raw[section].pop(field)
        else:
            raw[section][field] = value
    original = deepcopy(raw)
    requirements = RequirementRepository(tmp_path / "requirements")
    changed = requirements.create(RequirementSpec.parse(raw))
    models = ArtificialSocietyRepository(tmp_path / "models", requirements=requirements)
    model = models.create(ArtificialSocietySpec.parse(artificial_society_payload(changed)))
    payload = {**payload, "requirementId": changed["requirement_id"], "requirementFingerprint": changed["fingerprint"],
               "modelId": model["model_id"], "modelFingerprint": model["fingerprint"]}
    repo, gateway, orchestrator, _ = setup((payload, changed, model), monkeypatch)
    experiment = orchestrator.create_observation_experiment("u", "during-check", payload)
    assert experiment["duringObservation"]["requirement"] == original == raw
    assert experiment["duringObservation"]["artificialSociety"] == model["artificial_society"]
    assert experiment["scenario"]["durationSeconds"] == 60
    assert len(experiment["scenario"]["seedSet"]) == 3
    # Reset keeps the original IDs; neither free-text field is sent as a runtime override.
    assert orchestrator._prepare_reset_payload(experiment["scenario"], 123) == {
        "action": "reset", "seed": 123, "count": 24, "requirement_id": changed["requirement_id"],
        "artificial_society_id": model["model_id"], "artificial_society_fingerprint": model["fingerprint"],
    }
    report = ReportService(repo).generate("u", "during-check", experiment["experiment_id"])
    assert any("固定按道路长度分布预设" in note and "600–800 秒观景预设" in note for note in report["limitations"])
    for key in ("conversationId", "snapshotId", "requirementId", "requirementFingerprint", "modelId", "modelFingerprint"):
        assert report["binding"][key] == payload[key]
    assert requirements.load(changed["requirement_id"]) == changed
    assert models.load(model["model_id"]) == model
    assert not gateway.commands  # Preparing and reporting do not start a simulation.


def sample(t, density=2, speed=1):
    return {"time_seconds": t, "metric_ids": ["global-density", "local-density", "global-speed", "local-speed", "boundary-density-difference"],
            "global": {"person_count": 10, "density_person_per_m2": density, "avg_speed_mps": speed},
            "cells": [{"cell_id": "a", "person_count": 10, "density_person_per_m2": density, "avg_speed_mps": speed}],
            "boundaries": [{"boundary_id": "a-b", "status": "valid", "difference_person_per_m2": -1, "absolute_difference_person_per_m2": 1},
                           {"boundary_id": "outside", "status": "outside_not_observed", "absolute_difference_person_per_m2": None}]}


class Gateway:
    capabilities = {}
    def __init__(self, record, model):
        self.record, self.model, self.commands = record, model, []
        self.state = "READY"
        self.closed = False
    def add_listener(self, callback):
        self.callback = callback
    def start(self, **kwargs):
        return True
    def close(self):
        self.closed = True
    def submit(self, payload):
        self.commands.append(deepcopy(payload))
        return len(self.commands) - 1
    def wait_for_ack(self, request_id, **kwargs):
        command = self.commands[request_id]
        if command["action"] == "get_status":
            return {"type": "status", "state": self.state, "run_id": "external-run"}
        if command["action"] == "close_run":
            return {"type": "run_closed"}
        if command["action"] == "reset":
            self.runtime_id = f"runtime-{len(self.commands)}"
            return {"type": "init", "run_id": self.runtime_id, "requirement": self.record,
                    "artificial_society": self.model, "observation_geometry": {"area_m2": 5, "cells": ["a"]}}
        if command["action"] == "start":
            gateway = self
            class Terminal:
                flag = False
                def is_set(self):
                    return self.flag
                def set(self):
                    self.flag = True
                def wait(self, timeout):
                    # A late previous-run frame must not end the current run.
                    gateway.callback({"type": "update", "run_id": "stale", "step_seconds": 60})
                    for t in (1, 31, 60):
                        gateway.callback({"type": "update", "run_id": gateway.runtime_id, "step_seconds": t,
                                          "runtime_state": "FINISHED" if t == 60 else "RUNNING",
                                          "metrics": {"observations": sample(t)}})
                    return self.flag
            self.orchestrator._active["terminal"] = Terminal()
        return {"status": "applied"}


def setup(study, monkeypatch):
    payload, record, model = study
    repo, gateway = CrowdSimRepository(), Gateway(record, model)
    orchestrator = ExperimentOrchestrator(repo, gateway, ControllerRegistry())
    gateway.orchestrator = orchestrator
    monkeypatch.setattr(orchestrator, "_ensure_worker", lambda: None)
    monkeypatch.setattr("crowdsim.experiments.orchestrator.runtime_provenance", lambda: {})
    return repo, gateway, orchestrator, payload


@pytest.fixture
def timed_study(study, tmp_path, monkeypatch):
    payload, previous, _ = study
    raw = deepcopy(previous["requirement"])
    raw["scenario"].update(hotspot_start_seconds=0, hotspot_end_seconds=30)
    requirements = RequirementRepository(tmp_path / "timed-requirements")
    record = requirements.create(RequirementSpec.parse(raw))
    models = ArtificialSocietyRepository(tmp_path / "timed-models", requirements=requirements)
    model = models.create(ArtificialSocietySpec.parse(artificial_society_payload(record)))
    monkeypatch.setattr("crowdsim.experiments.during_observation.RequirementRepository", lambda: requirements)
    monkeypatch.setattr("crowdsim.experiments.during_observation.ArtificialSocietyRepository", lambda **kwargs: models)
    return {**payload, "requirementId": record["requirement_id"], "requirementFingerprint": record["fingerprint"],
            "modelId": model["model_id"], "modelFingerprint": model["fingerprint"]}, record, model


@pytest.mark.parametrize("normal", [True, False])
def test_timed_rounds_accept_only_proven_normal_early_exits_and_preserve_report_evidence(timed_study, monkeypatch, normal):
    from crowdsim.domain.hotspot_activity import requirement_hotspot_activity
    payload, record, model = timed_study
    activity = requirement_hotspot_activity(record["requirement"])
    repo, gateway, orchestrator, _ = setup(timed_study, monkeypatch)
    original_wait = gateway.wait_for_ack

    def wait(request_id, **kwargs):
        command = gateway.commands[request_id]
        if command["action"] == "reset":
            return {**original_wait(request_id, **kwargs), "scenario": {"hotspot_activity": activity}}
        if command["action"] != "start":
            return original_wait(request_id, **kwargs)

        class Terminal:
            flag = False
            def is_set(self):
                return self.flag
            def set(self):
                self.flag = True
            def wait(self, timeout):
                for t in (1, 31, 45):
                    metrics = {"observations": sample(t)}
                    if t == 45:
                        count = record["requirement"]["population"]["total"]
                        metrics["observations"]["global"]["person_count"] = 0
                        metrics["observation_completion"] = {
                            "activity": activity, "time_seconds": t, "state": "FINISHED",
                            "end_reason": "no_expected_entities", "population": {
                                "planned": count, "departed": count, "arrived": count, "active": 0,
                                "explicitly_removed": 0 if normal else 1,
                                "unknown_disappearances": [], "conservation_error": 0}}
                    gateway.callback({"type": "update", "run_id": gateway.runtime_id, "step_seconds": t,
                                      "runtime_state": "FINISHED" if t == 45 else "RUNNING", "metrics": metrics})
                return self.flag
        orchestrator._active["terminal"] = Terminal()
        return {"status": "applied"}

    gateway.wait_for_ack = wait
    experiment = orchestrator.create_observation_experiment("u", "during-check", payload)
    orchestrator.start_experiment("u", "during-check", experiment["experiment_id"])
    orchestrator._queue.put(None)
    orchestrator._worker_loop()
    runs = repo.list_runs("u", "during-check", experiment["experiment_id"])
    assert len(runs) == 3 and all(r["status"] == ("completed" if normal else "failed") for r in runs)
    report = ReportService(repo).generate("u", "during-check", experiment["experiment_id"])
    assert report["validRunCount"] == (3 if normal else 0)
    assert experiment["scenario"]["durationSeconds"] == 60
    assert [r["seed"] for r in runs] == list(range(20260908, 20260911))
    assert all(r["observationBinding"]["hotspotActivity"] == activity for r in runs)
    assert all(r["observationBinding"]["requirementId"] == payload["requirementId"] for r in runs)
    assert all(r["observationBinding"]["modelId"] == payload["modelId"] for r in runs)
    assert "600–800" not in report["limitations"][1]
    if normal:
        assert all(r["completion"]["time_seconds"] == 45 for r in runs)
        assert all(r["series"][-1]["timeSeconds"] == 45 and len(r["series"]) == 3 for r in report["runs"])
        # A completion receipt cannot make missing terminal observations valid.
        repo._memory["observations"] = [row for row in repo._memory["observations"]
                                         if row["payload"]["observationMetrics"]["time_seconds"] != 45]
        assert ReportService(repo).generate("u", "during-check", experiment["experiment_id"])["validRunCount"] == 0


def test_http_create_retry_queue_repetitions_and_measured_report(study, monkeypatch):
    from types import SimpleNamespace
    repo, gateway, orchestrator, payload = setup(study, monkeypatch)
    bundle = SimpleNamespace(orchestrator=orchestrator)
    response = request(bundle, "/during-observations", payload, workspace="during-check")
    assert response[0] == 201
    experiment = response[1]
    assert orchestrator.create_observation_experiment("u", "during-check", payload)["experiment_id"] == experiment["experiment_id"]
    runs = orchestrator.start_experiment("u", "during-check", experiment["experiment_id"])
    assert len(runs) == orchestrator._queue.qsize() == 3
    orchestrator._queue.put(None)
    orchestrator._worker_loop()
    resets = [c for c in gateway.commands if c["action"] == "reset"]
    assert [c["seed"] for c in resets] == list(range(20260908, 20260911))
    assert all(c["count"] == 24 and c["requirement_id"] == payload["requirementId"] and c["artificial_society_id"] == payload["modelId"] for c in resets)
    report = ReportService(repo).generate("u", "during-check", experiment["experiment_id"])
    assert report["validRunCount"] == 3
    assert report["aggregate"]["global-density"]["mean"] == 2
    assert report["aggregate"]["global-speed"]["validSamples"] == 3
    assert report["runs"][0]["personCount"]["max"] == 10
    assert len(report["runs"][0]["series"]) == 3
    assert report["reportVersion"] == ReportService(repo).generate("u", "during-check", experiment["experiment_id"])["reportVersion"]
    assert len(orchestrator.start_experiment("u", "during-check", experiment["experiment_id"])) == 3
    assert all(r["status"] == "completed" for r in repo.list_runs("u", "during-check"))
    with pytest.raises(LookupError):
        ReportService(repo).generate("u", "during-other", experiment["experiment_id"])


def test_three_rounds_accept_real_gateway_close_ack_and_preserve_results(study, monkeypatch):
    import threading
    from crowdsim.experiments.gateway import CrowdSimGateway

    repo, gateway, orchestrator, payload = setup(study, monkeypatch)
    protocol = CrowdSimGateway("ws://fake")
    monkeypatch.setattr(protocol, "start", lambda **_: pytest.fail("no live gateway connection"))
    original_wait = gateway.wait_for_ack
    gateway.state = "CLOSED"

    def wait_for_ack(request_id, **kwargs):
        action = gateway.commands[request_id]["action"]
        if action == "close_run":
            # Transport is fake; correlation and acknowledgement waiting use
            # the production gateway, which previously discarded run_closed.
            key = str(request_id)
            protocol._pending[key] = (threading.Event(), {})
            protocol.dispatch_for_test({"type": "run_closed", "request_id": key,
                                        "run_id": gateway.runtime_id, "runtime_state": "CLOSED"})
            gateway.state = "CLOSED"
            return protocol.wait_for_ack(key, timeout=0)
        result = original_wait(request_id, **kwargs)
        if action == "start":
            gateway.state = "FINISHED"
        return result

    monkeypatch.setattr(gateway, "wait_for_ack", wait_for_ack)
    experiment = orchestrator.create_observation_experiment("u", "during-check", payload)
    original_binding = deepcopy(experiment["duringObservation"])
    orchestrator.start_experiment("u", "during-check", experiment["experiment_id"])
    orchestrator._queue.put(None)
    orchestrator._worker_loop()
    runs = repo.list_runs("u", "during-check", experiment["experiment_id"])
    assert [r["status"] for r in runs] == ["completed"] * 3
    assert len({r["runtime_run_id"] for r in runs}) == 3
    assert [c["seed"] for c in gateway.commands if c["action"] == "reset"] == list(range(20260908, 20260911))
    assert sum(c["action"] == "close_run" for c in gateway.commands) == 2
    assert repo.get_experiment("u", "during-check", experiment["experiment_id"])["duringObservation"] == original_binding
    report = ReportService(repo).generate("u", "during-check", experiment["experiment_id"])
    assert report["validRunCount"] == report["expectedRunCount"] == 3
    assert all(r["valid"] and len(r["series"]) == 3 for r in report["runs"])
    assert not protocol._pending and protocol._thread is None


def test_independent_paused_run_is_not_reset_or_started(study, monkeypatch):
    repo, gateway, orchestrator, payload = setup(study, monkeypatch)
    experiment = orchestrator.create_observation_experiment("u", "during-check", payload)
    run = repo.create_run("u", "during-check", experiment["experiment_id"], "C0", 1, {})
    gateway.state = "PAUSED"
    with pytest.raises(RuntimeError, match="独立仿真"):
        orchestrator._execute_run({"user_id": "u", "workspace_id": "during-check", "experiment": experiment, "run": run})
    assert [c["action"] for c in gateway.commands] == ["get_status"]


def test_report_excludes_failed_cancelled_incomplete_and_missing_without_zero_filling(study):
    payload, record, model = study
    repo = CrowdSimRepository()
    config = observation_config(payload, "during-check")
    config["scenario"]["seedSet"] = list(range(1, 7))
    experiment = repo.create_experiment("u", "during-check", config)
    evidence = verify_initialization(config["duringObservation"], {"run_id": "r", "requirement": record, "artificial_society": model, "observation_geometry": {"area": 5}})
    for seed, status in enumerate(("completed", "completed", "failed", "cancelled", "completed"), 1):
        run = repo.create_run("u", "during-check", experiment["experiment_id"], "C0", seed, {})
        repo.update_run("u", "during-check", run["run_id"], {"status": status, "observationBinding": evidence})
        for t in ((1, 31, 60) if seed != 5 else (1,)):
            repo.record("observations", "u", "during-check", run["run_id"], {"observationMetrics": sample(t, density=0 if seed == 1 else 4, speed=None)})
    report = ReportService(repo).generate("u", "during-check", experiment["experiment_id"])
    assert report["validRunCount"] == 2 and len(report["runs"]) == 6
    assert report["runs"][-1]["status"] == "missing"
    assert report["aggregate"]["global-density"]["mean"] == 2
    assert report["aggregate"]["global-speed"]["mean"] is None
    assert report["aggregate"]["global-speed"]["validSamples"] == 0
    assert all(r["excludedReason"] for r in report["runs"][2:])
    for run in repo.list_runs("u", "during-check"):
        repo.update_run("u", "during-check", run["run_id"], {"status": "failed"})
    empty = ReportService(repo).generate("u", "during-check", experiment["experiment_id"])
    assert empty["validRunCount"] == 0 and empty["aggregate"]["global-density"]["mean"] is None


def test_runtime_view_scopes_frames_and_get_does_not_acquire_simulator(study, monkeypatch):
    from types import SimpleNamespace
    repo, gateway, orchestrator, payload = setup(study, monkeypatch)
    exp = orchestrator.create_observation_experiment('u', 'during-check', payload)
    runs = orchestrator.start_experiment('u', 'during-check', exp['experiment_id'])
    orchestrator._execute_run(orchestrator._queue.get())
    commands = deepcopy(gateway.commands)
    view = orchestrator.runtime_view('u', 'during-check', exp['experiment_id'])
    assert view['runId'] == runs[0]['run_id']
    assert view['frame']['run_id'] == view['init']['run_id']
    assert view['frame']['step_seconds'] == 60
    view['frame']['step_seconds'] = -1
    bundle = SimpleNamespace(orchestrator=orchestrator)
    status, fresh, _ = request(bundle, f"/experiments/{exp['experiment_id']}/runtime", workspace='during-check')
    assert status == 200 and fresh['frame']['step_seconds'] == 60
    assert request(bundle, f"/experiments/{exp['experiment_id']}/runtime", workspace='other')[0] == 404
    assert gateway.commands == commands


def test_stop_group_cancels_queued_rounds_without_resetting_or_touching_another_group(study, monkeypatch):
    from types import SimpleNamespace
    repo, gateway, orchestrator, payload = setup(study, monkeypatch)
    exp = orchestrator.create_observation_experiment('u', 'during-check', payload)
    runs = orchestrator.start_experiment('u', 'during-check', exp['experiment_id'])
    other = orchestrator.create_observation_experiment('u', 'during-check', {**payload, 'clientRequestId': 'another'})
    repo.update_run('u', 'during-check', runs[0]['run_id'], {'status': 'completed'})
    status, _, _ = request(SimpleNamespace(orchestrator=orchestrator), f"/experiments/{exp['experiment_id']}/stop", {}, workspace='during-check')
    assert status == 200
    assert [r['status'] for r in repo.list_runs('u', 'during-check', exp['experiment_id'])] == ['completed', 'cancelled', 'cancelled']
    assert repo.get_experiment('u', 'during-check', other['experiment_id'])['status'] == 'draft'
    for _ in range(3):
        item = orchestrator._queue.get()
        if item['run']['run_id'] != runs[0]['run_id']:
            orchestrator._execute_run(item)
    assert gateway.commands == []
    assert len(orchestrator.start_experiment('u', 'during-check', exp['experiment_id'])) == 3
    assert request(SimpleNamespace(orchestrator=orchestrator), f"/experiments/{exp['experiment_id']}/stop", {}, workspace='foreign')[0] == 404


def test_stop_group_wakes_active_round_and_preserves_completed_evidence(study, monkeypatch):
    import threading
    repo, gateway, orchestrator, payload = setup(study, monkeypatch)
    exp = orchestrator.create_observation_experiment('u', 'during-check', payload)
    runs = orchestrator.start_experiment('u', 'during-check', exp['experiment_id'])
    active = {'user_id': 'u', 'workspace_id': 'during-check', 'experiment': exp, 'run': runs[0],
              'runtime_run_id': 'active-runtime', 'terminal': threading.Event()}
    orchestrator._active = active
    repo.update_run('u', 'during-check', runs[0]['run_id'], {'status': 'running'})
    orchestrator.stop_experiment('u', 'during-check', exp['experiment_id'])
    assert active['terminal'].is_set()
    assert gateway.commands == [{'action': 'pause', 'run_id': 'active-runtime'}]
    assert all(r['status'] == 'cancelled' for r in repo.list_runs('u', 'during-check', exp['experiment_id']))


def test_default_model_publication_is_version_bound_and_retryable(study):
    from crowdsim.experiments.during_observation import prepare_observation_model
    payload, record, model = study
    default = artificial_society_payload(record)
    default['overview']['preparation'] = 'runtime defaults'
    data = {**payload, 'artificialSociety': default}
    first = prepare_observation_model(data, 'during-check')
    assert first['requirement_id'] == record['requirement_id']
    assert first['artificial_society']['individual_model']['distributions'] == record['requirement']['population']['distributions']
    assert prepare_observation_model(data, 'during-check')['model_id'] == first['model_id']
    assert first['model_id'] != model['model_id']
    with pytest.raises(ContractError):
        prepare_observation_model({**data, 'requirementFingerprint': 'stale'}, 'during-check')
    with pytest.raises(ContractError):
        prepare_observation_model(data, 'during-foreign')


def test_group_cancel_during_initialization_does_not_start_any_round(study, monkeypatch):
    repo, gateway, orchestrator, payload = setup(study, monkeypatch)
    exp = orchestrator.create_observation_experiment('u', 'during-check', payload)
    runs = orchestrator.start_experiment('u', 'during-check', exp['experiment_id'])
    original = gateway.wait_for_ack
    def ack(request_id, **kwargs):
        result = original(request_id, **kwargs)
        if gateway.commands[request_id]['action'] == 'reset':
            orchestrator.stop_experiment('u', 'during-check', exp['experiment_id'])
        return result
    monkeypatch.setattr(gateway, 'wait_for_ack', ack)
    for _ in runs:
        orchestrator._execute_run(orchestrator._queue.get())
    assert not any(c['action'] == 'start' for c in gateway.commands)
    saved = repo.list_runs('u', 'during-check', exp['experiment_id'])
    assert all(r['status'] == 'cancelled' for r in saved)
    assert saved[0]['runtime_run_id'] == gateway.runtime_id
