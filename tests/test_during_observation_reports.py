"""Only temporary persistence, fake transports and in-process HTTP handlers."""
from types import SimpleNamespace

import pytest

from crowdsim.experiments.contracts import ContractError
from crowdsim.experiments.during_observation import observation_config, verify_initialization
from crowdsim.experiments.during_observation_reports import interpretation, write_interpretation
from crowdsim.experiments.repository import CrowdSimRepository
from crowdsim.experiments.report_service import ReportService
from test_during_observation_experiments import study, sample
from test_during_observation_reset import reset_case, reset
from test_during_strategy import request


@pytest.fixture
def report_case(study, tmp_path):
    payload, requirement, model = study
    repo = CrowdSimRepository(tmp_path / "reports")
    config = observation_config(payload, "during-check")
    experiment = repo.create_experiment("u", "during-check", config)
    eid = experiment["experiment_id"]
    evidence = verify_initialization(config["duringObservation"], {"run_id": "runtime", "requirement": requirement, "artificial_society": model, "observation_geometry": {"area": 5}})
    for index, seed in enumerate(config["scenario"]["seedSet"]):
        run = repo.create_run("u", "during-check", eid, "C0", seed, {})
        repo.update_run("u", "during-check", run["run_id"], {"status": "completed" if index == 0 else "running" if index == 1 else "draft", "observationBinding": evidence})
        if index == 0:
            for time in (1, 31, 60):
                repo.record("observations", "u", "during-check", run["run_id"], {"observationMetrics": sample(time, density=4, speed=None)})
    repo.update_experiment("u", "during-check", eid, {"status": "running"})
    return SimpleNamespace(repository=repo, reports=ReportService(repo), eid=eid)


def write(case, payload):
    return write_interpretation(case.repository, case.reports, "u", "during-check", case.eid, payload)


def prepare(case, kind="interpretation"):
    return write(case, {"action": "prepare", "executionVersion": 0, "kind": kind})


def save(case, source, **changes):
    return write(case, {"action": "save", "executionVersion": 0, "kind": source.get("kind", "interpretation"), "sourceId": source["sourceId"], "text": "已有一轮有效结果，速度缺失。", "model": "GLM-5.3-Flash", **changes})


def test_interpretation_preserves_evidence_and_survives_reload_without_changing_runs(report_case):
    c = report_case
    before = c.repository.list_runs("u", "during-check", c.eid)
    assert interpretation(c.repository, "u", "during-check", c.eid) is None
    source = prepare(c)
    assert source["facts"]["validRunCount"] == 1
    assert source["facts"]["aggregate"]["global-speed"]["mean"] is None
    assert "series" not in source["facts"]["runs"][0]
    saved = save(c, source)
    assert saved["facts"] == source["facts"] and saved["reportVersion"] == source["reportVersion"]
    assert save(c, source) == saved  # Lost response retry is idempotent.
    restored = CrowdSimRepository(c.repository._root)
    assert interpretation(restored, "u", "during-check", c.eid) == saved
    assert restored.list_runs("u", "during-check", c.eid) == before
    # Regenerating a measured report cannot discard its saved interpretation.
    ReportService(restored).generate("u", "during-check", c.eid)
    assert interpretation(restored, "u", "during-check", c.eid) == saved


def test_running_report_can_save_its_original_source_after_new_results_arrive(report_case):
    c = report_case
    source = prepare(c)
    run = c.repository.list_runs("u", "during-check", c.eid)[1]
    c.repository.update_run("u", "during-check", run["run_id"], {"status": "failed", "error": "synthetic failure"})
    current = c.reports.generate("u", "during-check", c.eid)
    assert current["reportVersion"] != source["reportVersion"]
    saved = save(c, source)
    assert saved["reportVersion"] == source["reportVersion"]
    assert saved["facts"]["runs"][1]["status"] == "running"
    assert c.repository.get_report("u", "during-check", c.eid)["report"]["reportVersion"] == current["reportVersion"]


@pytest.mark.parametrize("kind", ["interpretation", "conclusion"])
def test_failed_save_preserves_the_previous_report_and_retry_remains_persistent(report_case, monkeypatch, kind):
    c = report_case
    previous = save(c, prepare(c, kind))
    source = prepare(c, kind)
    flush = c.repository._flush
    def fail(kind):
        if kind == "reports":
            raise OSError("synthetic disk failure")
        return flush(kind)
    monkeypatch.setattr(c.repository, "_flush", fail)
    with pytest.raises(OSError, match="synthetic disk failure"):
        save(c, source, text="new text")
    assert interpretation(c.repository, "u", "during-check", c.eid, kind=kind) == previous
    monkeypatch.setattr(c.repository, "_flush", flush)
    saved = save(c, source, text="new text")
    restored = CrowdSimRepository(c.repository._root)
    assert interpretation(restored, "u", "during-check", c.eid, kind=kind) == saved


def test_new_prepare_rejects_an_older_model_reply_and_preserves_saved_text(report_case):
    c = report_case
    original = save(c, prepare(c))
    older, newer = prepare(c), prepare(c)
    with pytest.raises(ContractError, match="已更新或清除"):
        save(c, older)
    assert interpretation(c.repository, "u", "during-check", c.eid) == original
    assert save(c, newer)["sourceId"] == newer["sourceId"]


def test_conclusion_and_general_interpretation_keep_independent_sources_and_saved_results(report_case):
    c = report_case
    general_source, conclusion_source = prepare(c), prepare(c, "conclusion")
    assert conclusion_source["kind"] == "conclusion"
    assert "concise-simulation-summary" in conclusion_source["facts"]["selectedMetricIds"]
    requirement = c.repository.get_experiment("u", "during-check", c.eid)["duringObservation"]["requirement"]
    assert conclusion_source["facts"]["researchQuestion"] == requirement["project"]["description"]
    with pytest.raises(ContractError, match="来源已更新"):
        save(c, general_source, kind="conclusion")
    general = save(c, general_source, text="原来的大模型解读。这里允许多句。")
    conclusion = save(c, conclusion_source, text="阶段性结果：已有一轮有效观测，速度数据不足以判断流动趋势。")
    assert interpretation(c.repository, "u", "during-check", c.eid) == general
    assert interpretation(c.repository, "u", "during-check", c.eid, kind="conclusion") == conclusion
    # A newer request of one kind cannot invalidate the other kind's pending source.
    next_conclusion = prepare(c, "conclusion")
    prepare(c)
    updated = save(c, next_conclusion, text="阶段性结果：现有观测暂不足以确认人群聚集趋势。")
    restored = CrowdSimRepository(c.repository._root)
    assert interpretation(restored, "u", "during-check", c.eid) == general
    assert interpretation(restored, "u", "during-check", c.eid, kind="conclusion") == updated
    c.reports.generate("u", "during-check", c.eid)
    assert interpretation(c.repository, "u", "during-check", c.eid, kind="conclusion") == updated
    status, result, _ = request(c, f"/experiments/{c.eid}/observation-conclusion", workspace="during-check")
    assert status == 200 and result == updated
    assert request(c, f"/experiments/{c.eid}/observation-conclusion", workspace="another")[0] == 404


def test_conclusion_http_prepare_and_save_do_not_write_the_general_interpretation(report_case):
    c = report_case
    general = save(c, prepare(c), text="原有实验报告解读。")
    endpoint = f"/experiments/{c.eid}/observation-conclusion"
    status, source, _ = request(c, endpoint, {"action": "prepare", "executionVersion": 0, "kind": "interpretation"}, workspace="during-check")
    assert status == 200 and source["kind"] == "conclusion"
    status, saved, _ = request(c, endpoint, {"action": "save", "executionVersion": 0, "sourceId": source["sourceId"], "text": "阶段性结果：现有观测仍不足以判断聚集趋势。", "model": "GLM"}, workspace="during-check")
    assert status == 200 and saved["sourceId"] == source["sourceId"]
    assert request(c, endpoint, workspace="during-check")[1] == saved
    assert interpretation(c.repository, "u", "during-check", c.eid) == general


@pytest.mark.parametrize("text", ["第一句。第二句。", "第一行\n第二行", "x" * 201, "Is density 1.2. Speed 0.", "结论？补充说明"])
def test_conclusion_rejects_multi_sentence_multiline_or_overlong_output_without_overwriting(report_case, text):
    c = report_case
    previous = save(c, prepare(c, "conclusion"))
    source = prepare(c, "conclusion")
    with pytest.raises(ContractError, match="单句、单行"):
        save(c, source, text=text)
    assert interpretation(c.repository, "u", "during-check", c.eid, kind="conclusion") == previous
    assert save(c, source, text="阶段性结果：观测均值为 1.2，但现有证据不足以判断趋势。")["kind"] == "conclusion"


def test_conclusion_requires_selection_in_frozen_experiment_and_valid_measured_results(report_case):
    c = report_case
    experiment = c.repository.get_experiment("u", "during-check", c.eid)
    binding = experiment["duringObservation"]
    binding["requirement"]["observation"]["metric_ids"].remove("concise-simulation-summary")
    c.repository.update_experiment("u", "during-check", c.eid, {"duringObservation": binding})
    with pytest.raises(ContractError, match="未选择"):
        prepare(c, "conclusion")
    assert request(c, f"/experiments/{c.eid}/observation-conclusion", workspace="during-check")[0] == 400
    assert prepare(c)["kind"] == "interpretation"
    binding["requirement"]["observation"]["metric_ids"].append("concise-simulation-summary")
    c.repository.update_experiment("u", "during-check", c.eid, {"duringObservation": binding})
    for run in c.repository.list_runs("u", "during-check", c.eid):
        c.repository.update_run("u", "during-check", run["run_id"], {"status": "failed"})
    with pytest.raises(ContractError, match="有效完成轮次"):
        prepare(c, "conclusion")
    with pytest.raises(ContractError, match="不支持"):
        prepare(c, "arbitrary-output")


@pytest.mark.parametrize("patch", [{"executionVersion": 1}, {"executionVersion": False}, {"text": ""}, {"text": "x" * 16001}, {"model": ""}, {"sourceId": "foreign"}, {"action": "start"}])
def test_malformed_or_stale_writes_do_not_replace_saved_interpretation(report_case, patch):
    c = report_case
    original = save(c, prepare(c))
    source = prepare(c)
    with pytest.raises(ContractError):
        save(c, source, **patch)
    assert interpretation(c.repository, "u", "during-check", c.eid) == original


def test_routes_are_scoped_to_during_and_do_not_create_an_experiment(report_case):
    c = report_case
    assert request(c, f"/experiments/{c.eid}/observation-interpretation", workspace="during-check")[1] is None
    status, source, _ = request(c, f"/experiments/{c.eid}/observation-interpretation", {"action": "prepare", "executionVersion": 0}, workspace="during-check")
    assert status == 200
    assert request(c, f"/experiments/{c.eid}/observation-interpretation", user="other", workspace="during-check")[0] == 404
    assert request(c, f"/experiments/{c.eid}/observation-interpretation", workspace="post-check")[0] == 404
    other = c.repository.create_experiment("u", "during-check", {"name": "legacy pre/post"})
    assert request(c, f"/experiments/{other['experiment_id']}/observation-interpretation", workspace="during-check")[0] == 400
    assert c.repository.get_experiment("u", "during-check", c.eid)["status"] == "running"
    assert source["facts"]["binding"]["conversationId"] == "check"


def test_prepare_requires_measured_valid_results(report_case):
    c = report_case
    for run in c.repository.list_runs("u", "during-check", c.eid):
        c.repository.update_run("u", "during-check", run["run_id"], {"status": "failed"})
    with pytest.raises(ContractError, match="有效完成轮次"):
        prepare(c)


def test_destructive_reset_removes_saved_and_pending_interpretations(reset_case):
    c = reset_case
    report = ReportService(c.repo).generate("u", "during-check", c.eid)
    query = {"user_id": "u", "workspace_id": "during-check", "experiment_id": c.eid}
    pending = {"sourceId": "pending", "executionVersion": 0, "facts": {"old": True}}
    c.repo._update("reports", query, {"interpretation": {**pending, "text": "old"}, "interpretationSource": pending,
                                      "conclusion": {**pending, "kind": "conclusion", "text": "old conclusion"}, "conclusionSource": pending})
    reset(c)
    assert interpretation(c.repo, "u", "during-check", c.eid) is None
    assert interpretation(c.repo, "u", "during-check", c.eid, kind="conclusion") is None
    with pytest.raises(ContractError, match="重新运行"):
        write_interpretation(c.repo, ReportService(c.repo), "u", "during-check", c.eid, {"action": "save", "executionVersion": 0, "sourceId": "pending", "text": "late", "model": "GLM"})
    with pytest.raises(ContractError, match="重新运行"):
        write_interpretation(c.repo, ReportService(c.repo), "u", "during-check", c.eid, {"kind": "conclusion", "action": "save", "executionVersion": 0, "sourceId": "pending", "text": "late", "model": "GLM"})
    fresh = ReportService(c.repo).generate("u", "during-check", c.eid)
    assert fresh["executionVersion"] == 1 and fresh["reportVersion"] != report["reportVersion"]
    assert not c.repo.get_report("u", "during-check", c.eid).get("interpretationSource")
    assert not c.repo.get_report("u", "during-check", c.eid).get("conclusionSource")


def test_summary_query_omits_visual_frames_without_acquiring_or_controlling_simulator(reset_case):
    c = reset_case
    c.orchestrator._runtime_view.update(runId=c.runs[-1]["run_id"], frame={"step_seconds": 55, "entities": ["large frame"]})
    bundle = SimpleNamespace(orchestrator=c.orchestrator)
    status, summary, _ = request(bundle, f"/experiments/{c.eid}/runtime?summary=1", workspace="during-check")
    assert status == 200 and summary["elapsedSeconds"] == 55
    assert summary["binding"] == {key: c.repo.get_experiment("u", "during-check", c.eid)["duringObservation"][key] for key in summary["binding"]}
    assert "frame" not in summary and "init" not in summary and not c.gateway.commands
    summary["runs"][0]["status"] = "forged"
    assert c.repo.get_run("u", "during-check", c.runs[0]["run_id"])["status"] == "completed"
    assert request(bundle, f"/experiments/{c.eid}/runtime?summary=1", workspace="foreign")[0] == 404
    assert not c.gateway.commands
