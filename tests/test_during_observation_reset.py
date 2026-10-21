"""Reset checks use temporary files, in-memory transports and no live services."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from crowdsim.experiments.contracts import ContractError
from crowdsim.experiments.controller_registry import ControllerRegistry
from crowdsim.experiments.during_observation import observation_config
from crowdsim.experiments.orchestrator import ExperimentOrchestrator
from crowdsim.experiments.repository import CrowdSimRepository
from crowdsim.experiments.report_service import ReportService
from test_during_observation_experiments import study
from test_during_strategy import request


class CloseGateway:
    capabilities = {}
    latest_frame = None
    connected = True
    closed = False
    close_ok = True
    state = "FINISHED"
    def __init__(self):
        self.commands = []
    def add_listener(self, callback):
        self.callback = callback
    def health(self):
        return {"connected": self.connected}
    def start(self, **kwargs):
        self.connected = True
        return True
    def submit(self, payload):
        self.commands.append(deepcopy(payload))
        return len(self.commands) - 1
    def wait_for_ack(self, request_id, **kwargs):
        command = self.commands[request_id]
        if command["action"] == "get_status":
            return {"type": "status", "state": self.state, "run_id": self.runtime_id}
        assert command == {"action": "close_run", "run_id": self.runtime_id}
        if self.close_ok:
            self.state = "CLOSED"
            return {"type": "run_closed", "run_id": self.runtime_id, "runtime_state": "CLOSED"}
        return {"type": "error", "message": "synthetic close failure"}
    def close(self):
        self.closed = True
        self.connected = False


@pytest.fixture
def reset_case(study, tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    root.mkdir()
    monkeypatch.setattr("crowdsim.experiments.during_observation_reset.RUNTIME_ROOT", root)
    repo = CrowdSimRepository(tmp_path / "control")
    gateway = CloseGateway()
    orchestrator = ExperimentOrchestrator(repo, gateway, ControllerRegistry())
    monkeypatch.setattr(orchestrator, "_ensure_worker", lambda: None)
    monkeypatch.setattr("crowdsim.experiments.orchestrator.runtime_provenance", lambda: {})
    experiment = repo.create_experiment("u", "during-check", observation_config(study[0], "during-check"))
    eid = experiment["experiment_id"]
    for i, status in enumerate(("completed", "failed", "completed"), 1):
        run = repo.create_run("u", "during-check", eid, "C0", i, {})
        runtime_id = f"run-{i:032x}"
        directory = root / runtime_id
        directory.mkdir(); (directory / "raw-result.json").write_text('"old measured data"')
        repo.update_run("u", "during-check", run["run_id"], {"status": status, "runtime_run_id": runtime_id})
        for kind in repo.APPEND_ONLY:
            repo.record(kind, "u", "during-check", run["run_id"], {"old": True})
        orchestrator.events.publish("run_completed", {"old": True}, run_id=run["run_id"])
    repo.update_experiment("u", "during-check", eid, {"status": "completed_with_failures", "stop_requested": True})
    repo.save_report("u", "during-check", eid, {"old": True})
    runs = repo.list_runs("u", "during-check", eid)
    gateway.runtime_id = runs[-1]["runtime_run_id"]
    gateway.latest_frame = {"run_id": gateway.runtime_id, "old": True}
    orchestrator._runtime_view = {"scope": ("u", "during-check", eid), "init": {"old": True}, "frame": {"old": True}}
    return SimpleNamespace(repo=repo, gateway=gateway, orchestrator=orchestrator, eid=eid, runs=runs, root=root,
                           payload={"expectedRunIds": [run["run_id"] for run in runs]}, study=study)


def reset(case):
    return case.orchestrator.reset_observation_experiment("u", "during-check", case.eid, case.payload)


def test_reset_permanently_removes_only_selected_runs_and_allows_a_fresh_matrix(reset_case):
    c = reset_case
    original = c.repo.get_experiment("u", "during-check", c.eid)
    other = c.repo.create_experiment("other", "other-workspace", {"name": "unrelated"})
    other_run = c.repo.create_run("other", "other-workspace", other["experiment_id"], "C0", 1, {})
    for kind in c.repo.APPEND_ONLY:
        c.repo.record(kind, "other", "other-workspace", other_run["run_id"], {"keep": True})
    c.repo.save_report("other", "other-workspace", other["experiment_id"], {"keep": True})
    c.orchestrator.events.publish("run_completed", {"keep": True}, run_id=other_run["run_id"])
    status, result, _ = request(SimpleNamespace(orchestrator=c.orchestrator), f"/experiments/{c.eid}/reset-observation", c.payload, workspace="during-check")
    assert status == 200 and result["status"] == "draft" and result["runs"] == []
    assert result["duringObservation"] == original["duringObservation"]
    assert result["scenario"] == original["scenario"] and result["clientRequestId"] == original["clientRequestId"]
    assert not list(c.root.iterdir())
    restored = CrowdSimRepository(c.repo._root)
    assert restored.get_experiment("u", "during-check", c.eid)["status"] == "draft"
    assert restored.list_runs("u", "during-check", c.eid) == []
    assert restored.get_report("u", "during-check", c.eid) is None
    for kind in restored.APPEND_ONLY:
        assert all(not restored.list_records(kind, "u", "during-check", run["run_id"]) for run in c.runs)
        assert restored.list_records(kind, "other", "other-workspace", other_run["run_id"])[0]["payload"] == {"keep": True}
    assert restored.get_report("other", "other-workspace", other["experiment_id"])["report"] == {"keep": True}
    assert len(c.orchestrator.events.after(0)) == 1
    assert c.orchestrator.events.after(0)[0]["runId"] == other_run["run_id"]
    assert c.orchestrator._runtime_view is None and c.gateway.latest_frame is None
    assert [command["action"] for command in c.gateway.commands] == ["get_status", "close_run"]
    assert c.gateway.closed
    assert reset(c)["status"] == "draft"  # Lost response retries are harmless.
    with pytest.raises(ContractError, match="刷新"):
        c.orchestrator.start_experiment("u", "during-check", c.eid)
    assert c.repo.list_runs("u", "during-check", c.eid) == []
    new_runs = c.orchestrator.start_experiment("u", "during-check", c.eid, execution_version=result["executionVersion"])
    assert len(new_runs) == 3 and not set(c.payload["expectedRunIds"]) & {run["run_id"] for run in new_runs}
    with pytest.raises(ContractError, match="已变化"):
        reset(c)
    assert len(c.repo.list_runs("u", "during-check", c.eid)) == 3
    assert not list(c.repo._root.rglob('*.bak'))


@pytest.mark.parametrize("status", ["running", "paused", "preparing", "draft"])
def test_reset_rejects_unfinished_rounds_before_any_gateway_or_file_change(reset_case, status):
    c = reset_case
    c.repo.update_run("u", "during-check", c.runs[0]["run_id"], {"status": status})
    with pytest.raises(ContractError, match="结束本组"):
        reset(c)
    assert len(list(c.root.iterdir())) == 3 and not c.gateway.commands


@pytest.mark.parametrize("in_flight", ["processing", "queued"])
def test_reset_waits_for_worker_cleanup_even_after_terminal_status_is_visible(reset_case, in_flight):
    c = reset_case
    if in_flight == "processing":
        c.orchestrator._processing = {"run": c.runs[0]}
    else:
        c.orchestrator._queue.put({"run": c.runs[0]})
    with pytest.raises(ContractError, match="收尾"):
        reset(c)
    assert not c.gateway.commands and len(list(c.root.iterdir())) == 3


def test_failed_close_keeps_records_and_files_available_for_retry(reset_case):
    c = reset_case
    c.gateway.close_ok = False
    with pytest.raises(ContractError, match="完成关闭"):
        reset(c)
    assert len(c.repo.list_runs("u", "during-check", c.eid)) == 3
    assert c.repo.get_report("u", "during-check", c.eid)["report"] == {"old": True}
    assert len(list(c.root.iterdir())) == 3
    c.gateway.close_ok = True
    assert reset(c)["status"] == "draft"


def test_reset_does_not_close_or_disconnect_an_unrelated_running_simulation(reset_case):
    c = reset_case
    c.gateway.runtime_id = "run-" + "f" * 32
    c.gateway.state = "RUNNING"
    reset(c)
    assert [command["action"] for command in c.gateway.commands] == ["get_status"]
    assert c.gateway.state == "RUNNING" and not c.gateway.closed


def test_a_reactivated_old_runtime_is_not_interrupted_by_reset(reset_case):
    c = reset_case
    c.gateway.state = "RUNNING"
    with pytest.raises(ContractError, match="仍在运行"):
        reset(c)
    assert [command["action"] for command in c.gateway.commands] == ["get_status"]
    assert not c.gateway.closed and len(list(c.root.iterdir())) == 3


@pytest.mark.parametrize("kind", ["wrong-scope", "other-phase", "shared-files", "path", "symlink"])
def test_reset_rejects_out_of_scope_or_unsafe_targets_without_any_commands(reset_case, tmp_path, kind):
    c = reset_case
    if kind == "wrong-scope":
        with pytest.raises(LookupError):
            c.orchestrator.reset_observation_experiment("other", "during-check", c.eid, c.payload)
    else:
        if kind == "other-phase":
            c.repo.update_experiment("u", "during-check", c.eid, {"duringObservation": None})
        elif kind == "shared-files":
            other = c.repo.create_run("another", "workspace", "another-experiment", "C0", 1, {})
            c.repo.update_run("another", "workspace", other["run_id"], {"runtime_run_id": c.runs[0]["runtime_run_id"]})
        elif kind == "path":
            c.repo.update_run("u", "during-check", c.runs[0]["run_id"], {"runtime_run_id": "../outside"})
        else:
            target = tmp_path / "outside"; target.mkdir(); (target / "keep.txt").write_text("keep")
            runtime_id = "run-" + "a" * 32
            (c.root / runtime_id).symlink_to(target, target_is_directory=True)
            c.repo.update_run("u", "during-check", c.runs[0]["run_id"], {"runtime_run_id": runtime_id})
        with pytest.raises(ContractError):
            reset(c)
    assert not c.gateway.commands
    assert all((c.root / run["runtime_run_id"] / "raw-result.json").exists() for run in c.runs)


def test_delete_failure_retains_matrix_and_retry_completes_without_backup(reset_case, monkeypatch):
    c = reset_case
    import shutil
    original = shutil.rmtree
    monkeypatch.setattr("crowdsim.experiments.during_observation_reset.shutil.rmtree", lambda _: (_ for _ in ()).throw(OSError("synthetic disk failure")))
    with pytest.raises(OSError, match="synthetic disk"):
        reset(c)
    assert len(c.repo.list_runs("u", "during-check", c.eid)) == 3
    monkeypatch.setattr("crowdsim.experiments.during_observation_reset.shutil.rmtree", original)
    assert reset(c)["status"] == "draft"
    assert not list(c.root.iterdir())


def test_report_cannot_republish_deleted_run_results_during_reset(reset_case, monkeypatch):
    import threading
    from crowdsim.experiments import during_observation
    c = reset_case
    entered, release, reset_done = threading.Event(), threading.Event(), threading.Event()
    original = during_observation.observation_report
    def blocked(*args):
        entered.set()
        assert release.wait(2)
        return original(*args)
    monkeypatch.setattr(during_observation, "observation_report", blocked)
    errors = []
    def report():
        try:
            ReportService(c.repo).generate("u", "during-check", c.eid)
        except Exception as exc:
            errors.append(exc)
    def clear():
        try:
            reset(c)
        except Exception as exc:
            errors.append(exc)
        finally:
            reset_done.set()
    reader = threading.Thread(target=report)
    cleaner = threading.Thread(target=clear)
    reader.start()
    try:
        assert entered.wait(2)
        cleaner.start()
        assert not reset_done.is_set()
    finally:
        release.set()
        reader.join(2)
        if cleaner.ident is not None:
            cleaner.join(2)
    assert not errors and not reader.is_alive() and not cleaner.is_alive()
    assert c.repo.get_report("u", "during-check", c.eid) is None


@pytest.mark.parametrize("collection", ["reports", "runs", "experiments"])
def test_partial_persistence_failure_can_be_retried_after_service_reload(reset_case, monkeypatch, collection):
    c = reset_case
    flush, failed = c.repo._flush, []
    def fail_once(kind):
        if kind == collection and not failed:
            failed.append(kind)
            raise OSError("synthetic persistence failure")
        return flush(kind)
    monkeypatch.setattr(c.repo, "_flush", fail_once)
    with pytest.raises(OSError, match="persistence failure"):
        reset(c)
    repo = CrowdSimRepository(c.repo._root)
    orchestrator = ExperimentOrchestrator(repo, c.gateway, ControllerRegistry())
    updated = orchestrator.reset_observation_experiment("u", "during-check", c.eid, c.payload)
    assert updated["status"] == "draft" and updated["executionVersion"] == 1
    restored = CrowdSimRepository(c.repo._root)
    assert restored.get_experiment("u", "during-check", c.eid)["status"] == "draft"
    assert not restored.list_runs("u", "during-check", c.eid)
    assert restored.get_report("u", "during-check", c.eid) is None
    assert not list(c.root.iterdir())
