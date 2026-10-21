"""Class-one repetitions: frozen requirements, existing C0 queue, measured reports."""

from copy import deepcopy
import hashlib
import json
import math
from statistics import mean

from .contracts import ContractError, public_time, validate_experiment_config
from crowdsim.domain.requirement_spec import requirement_observation_duration
from crowdsim.domain.hotspot_activity import requirement_hotspot_activity
from crowdsim.infrastructure.requirement_repository import RequirementRepository
from crowdsim.infrastructure.artificial_society_repository import ArtificialSocietyRepository
from crowdsim.domain.artificial_society_spec import ArtificialSocietySpec


def prepare_observation_model(payload, workspace_id):
    """Publish the existing default recipe, bound to this confirmed requirement."""
    if workspace_id != f"during-{payload.get('conversationId')}" or not payload.get("snapshotId"):
        raise ContractError("对话与需求快照不一致。")
    requirements = RequirementRepository()
    record = requirements.load(payload.get("requirementId"))
    if record["fingerprint"] != payload.get("requirementFingerprint") or record["requirement"].get("research", {}).get("problem_type") != 1:
        raise ContractError("需求版本或问题类别不匹配。")
    spec = ArtificialSocietySpec.parse(payload.get("artificialSociety"))
    if spec.requirement_id != record["requirement_id"] or spec.payload["requirement_fingerprint"] != record["fingerprint"]:
        raise ContractError("人工社会与需求版本不匹配。")
    models = ArtificialSocietyRepository(requirements=requirements)
    # A timed-out submission can safely recover the same immutable version.
    for path in sorted(models.root.glob("asm-*.json")):
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("requirement_id") == record["requirement_id"] and existing.get("fingerprint") == f"sha256:{spec.fingerprint}":
            return models.load(existing["model_id"])
    return models.create(spec)


METRICS = {
    "global-density": ("全局密度", "人/㎡"),
    "global-speed": ("全局平均速度", "米/秒"),
    "local-density": ("各采样时刻局部最大密度", "人/㎡"),
    "local-speed": ("各采样时刻非空网格最低平均速度", "米/秒"),
    "boundary-density-difference": ("各采样时刻最大边界密度绝对差", "人/㎡"),
}


def observation_config(payload, workspace_id, requirements=None, models=None):
    requirements = requirements or RequirementRepository()
    models = models or ArtificialSocietyRepository(requirements=requirements)
    conversation = payload.get("conversationId")
    if not isinstance(conversation, str) or workspace_id != f"during-{conversation}":
        raise ContractError("对话与实验工作区不一致。")
    snapshot = payload.get("snapshotId")
    if not isinstance(snapshot, str) or not snapshot or len(snapshot) > 128:
        raise ContractError("缺少已保存需求快照。")
    if not payload.get("clientRequestId"):
        raise ContractError("缺少实验提交请求编号。")
    record = requirements.load(payload.get("requirementId"))
    requirement = record["requirement"]
    if record["fingerprint"] != payload.get("requirementFingerprint"):
        raise ContractError("需求版本不一致，请重新核对保存回执。")
    if requirement.get("schema_version") != 2 or requirement.get("research", {}).get("problem_type") != 1:
        raise ContractError("此入口仅支持第一类观察问题。")
    if requirement["spatial_scope"]["location_id"] != "memorial-tower":
        raise ContractError("当前仅纪念塔场景已接通实验，其他地点暂不支持。")
    repetitions = requirement.get("experiment", {}).get("repetitions")
    if requirement.get("experiment", {}).get("intervention_request") not in (None, "", "none"):
        raise ContractError("第一类实验不支持外部干预，请重新确认无干预需求。")
    # Road-length spawning stays preset. New class-one activity times are read
    # from the immutable requirement by the runtime; legacy entries keep theirs.
    if (requirement["scenario"].get("additional_events") != []
            or requirement["scenario"].get("event_category", {}).get("id") != "culture"
            or requirement["observation"].get("local_partition_mode") != "uniform"
            or requirement.get("observation", {}).get("custom_request")
            or requirement.get("experiment", {}).get("baseline_control", "none") != "none"):
        raise ContractError("当前仅支持纪念塔文化活动、均匀分区，且无额外事件、自定义观测或额外控制的配置；其他配置已保留，暂未接入执行。")
    duration = requirement_observation_duration(requirement)
    if type(repetitions) is not int or not 1 <= repetitions <= 30 or duration is None:
        raise ContractError("请确认实验时长和 1–30 次重复实验。")
    metric_ids = requirement["observation"]["metric_ids"]
    if not set(metric_ids) <= set(METRICS) | {"concise-simulation-summary"}:
        raise ContractError("此入口仅支持第一类观察指标。")
    model = models.load(payload.get("modelId"))
    if (model["requirement_id"] != record["requirement_id"]
            or model["fingerprint"] != payload.get("modelFingerprint") or model.get("status") != "published"):
        raise ContractError("请发布与当前需求匹配的人工社会版本。")
    binding = {
        "conversationId": conversation, "snapshotId": snapshot,
        "requirementId": record["requirement_id"], "requirementFingerprint": record["fingerprint"],
        "modelId": model["model_id"], "modelFingerprint": model["fingerprint"],
    }
    return validate_experiment_config({
        "name": requirement["project"]["title"], "clientRequestId": payload.get("clientRequestId"),
        "methods": ["C0"], "mode": "formal", "observationIntervalSeconds": 30,
        "scenario": {"population": requirement["population"]["total"], "durationSeconds": duration,
                     "seedSet": list(range(20260908, 20260908 + repetitions)),
                     "observationBinding": binding},
        "duringObservation": {**binding, "requirement": deepcopy(requirement),
                              "artificialSociety": deepcopy(model["artificial_society"])},
    }, allow_during_observation=True)


def verify_initialization(binding, frame):
    requirement, model = frame.get("requirement") or {}, frame.get("artificial_society") or {}
    if (not frame.get("run_id") or requirement.get("requirement_id") != binding["requirementId"]
            or requirement.get("fingerprint") != binding["requirementFingerprint"]
            or model.get("model_id") != binding["modelId"]
            or model.get("fingerprint") != binding["modelFingerprint"]):
        raise RuntimeError("实验初始化的需求或人工社会版本不匹配。")
    geometry = frame.get("observation_geometry")
    if not geometry:
        raise RuntimeError("实验初始化缺少观测区域定义。")
    expected_activity = requirement_hotspot_activity(binding["requirement"])
    actual_activity = (frame.get("scenario") or {}).get("hotspot_activity")
    if expected_activity is not None and actual_activity != expected_activity:
        raise RuntimeError("仿真服务未应用当前热点活动时间，请更新仿真服务后重试。")
    digest = hashlib.sha256(json.dumps(geometry, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {**binding, "geometryFingerprint": digest, "geometry": deepcopy(geometry),
            **({"hotspotActivity": deepcopy(actual_activity)} if expected_activity else {})}


def normal_early_completion(evidence, requirement, sim_time):
    """An early finish needs actual normal arrivals, not an empty UI frame."""
    activity = requirement_hotspot_activity(requirement)
    if (not activity or activity["end_seconds"] is None or sim_time < activity["end_seconds"]
            or not isinstance(evidence, dict) or evidence.get("activity") != activity
            or evidence.get("state") != "FINISHED" or evidence.get("end_reason") != "no_expected_entities"
            or evidence.get("time_seconds") != sim_time):
        return False
    population = evidence.get("population") or {}
    expected = requirement["population"]["total"]
    counts = {"planned": expected, "departed": expected, "arrived": expected, "active": 0,
              "explicitly_removed": 0, "conservation_error": 0}
    return (all(type(population.get(key)) is int and population[key] == value for key, value in counts.items())
            and population.get("unknown_disappearances") == [])


def number(value):
    return type(value) in (float, int) and math.isfinite(value)


def values_at(sample):
    g, cells, boundaries = sample.get("global") or {}, sample.get("cells") or [], sample.get("boundaries") or []
    def extrema(rows, key, operation):
        values = [row[key] for row in rows if number(row.get(key))]
        return operation(values) if values else None
    return {
        "global-density": g.get("density_person_per_m2"),
        "global-speed": g.get("avg_speed_mps"),
        "local-density": extrema(cells, "density_person_per_m2", max),
        "local-speed": extrema(cells, "avg_speed_mps", min),
        "boundary-density-difference": extrema([b for b in boundaries if b.get("status") == "valid"], "absolute_difference_person_per_m2", max),
    }


def stats(values):
    values = [v for v in values if number(v)]
    return {"mean": mean(values) if values else None, "min": min(values) if values else None,
            "max": max(values) if values else None, "validSamples": len(values)}


def observation_report(repository, user_id, workspace_id, experiment, runs):
    binding = experiment["duringObservation"]
    selected = binding["requirement"]["observation"]["metric_ids"]
    # A partially created matrix must expose absent seeds rather than quietly
    # reporting a smaller experiment as complete.
    runs = list(runs) + [{"run_id": f"missing-{seed}", "seed": seed, "status": "missing"}
                        for seed in experiment["scenario"]["seedSet"]
                        if not any(r["seed"] == seed for r in runs)]
    rows, geometry_fingerprint = [], None
    maximum_gap = experiment["observationIntervalSeconds"] + 1
    for run in runs:
        evidence = run.get("observationBinding") or {}
        samples = [r.get("payload", r).get("observationMetrics") for r in repository.list_records(
            "observations", user_id, workspace_id, run["run_id"], limit=100000)]
        samples = [s for s in samples if isinstance(s, dict)]
        times = [s.get("time_seconds") for s in samples]
        early_complete = bool(times) and number(times[-1]) and normal_early_completion(
            run.get("completion"), binding["requirement"], times[-1])
        complete = (bool(times) and all(number(t) for t in times)
                    and times[0] <= maximum_gap and (times[-1] >= experiment["scenario"]["durationSeconds"] or early_complete)
                    and all(0 < b - a <= maximum_gap for a, b in zip(times, times[1:])))
        reason = None
        if run["status"] != "completed":
            reason = run.get("error") or run["status"]
        elif not complete:
            reason = "观测缺失、时间覆盖不完整或时间顺序异常"
        elif any(evidence.get(k) != binding[k] for k in ("requirementId", "requirementFingerprint", "modelId", "modelFingerprint", "snapshotId")) or not evidence.get("geometryFingerprint"):
            reason = "需求、模型或观测区域证据缺失"
        elif geometry_fingerprint and geometry_fingerprint != evidence["geometryFingerprint"]:
            reason = "观测区域与其他有效轮次不一致"
        if not reason:
            geometry_fingerprint = evidence["geometryFingerprint"]
        series = [{"timeSeconds": s["time_seconds"], "personCount": (s.get("global") or {}).get("person_count"),
                   **{k: v for k, v in values_at(s).items() if k in selected}} for s in samples]
        metrics = {k: {"label": METRICS[k][0], "unit": METRICS[k][1], **stats(p.get(k) for p in series)} for k in selected if k in METRICS}
        rows.append({"runId": run["run_id"], "seed": run["seed"], "status": run["status"],
                     "valid": reason is None, "excludedReason": reason, "metrics": metrics,
                     "personCount": {**stats(p.get("personCount") for p in series),
                                     "first": series[0]["personCount"] if series else None,
                                     "last": series[-1]["personCount"] if series else None},
                     "series": series, "lastSample": samples[-1] if samples else None,
                     "hotspotActivity": deepcopy(evidence.get("hotspotActivity")),
                     "completion": deepcopy(run.get("completion"))})
    valid = [r for r in rows if r["valid"]]
    aggregate = {k: {"label": METRICS[k][0], "unit": METRICS[k][1],
                     **stats(r["metrics"][k]["mean"] for r in valid),
                     "peak": max((r["metrics"][k]["max"] for r in valid if number(r["metrics"][k]["max"])), default=None)}
                 for k in selected if k in METRICS}
    report = {"schemaVersion": "during-observation-1", "experimentId": experiment["experiment_id"],
              "executionVersion": experiment.get("executionVersion", 0), "experimentStatus": experiment["status"],
              "binding": {k: binding[k] for k in ("conversationId", "snapshotId", "requirementId", "requirementFingerprint", "modelId", "modelFingerprint")},
              "expectedRunCount": len(experiment["scenario"]["seedSet"]), "validRunCount": len(valid),
              "observationIntervalSeconds": experiment["observationIntervalSeconds"],
              "runs": rows, "aggregate": aggregate,
              "limitations": ["独立随机种子按顺序执行；汇总只含有效完成轮次。",
                  activity_execution_note(binding["requirement"], rows),
                  f"每 {experiment['observationIntervalSeconds']:g} 秒归档一次并包含末帧；均值按归档采样点计算。整组均值对各有效轮次等权，极值为采样值，不代表连续时间极值。",
                  "速度缺失保持为空；局部与边界指标采用固定观测区域，未观测的外边界不参与统计。",
                  "简要总结由大模型按实测报告解读，模型不可补造指标或作因果结论。"]}
    report["reportVersion"] = hashlib.sha256(json.dumps(report, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    report["generatedAt"] = public_time()
    repository.save_report(user_id, workspace_id, experiment["experiment_id"], report)
    return report


def activity_execution_note(requirement, rows):
    activity = requirement_hotspot_activity(requirement)
    executed = [row for row in rows if row.get("hotspotActivity") or row["status"] in {"completed", "failed", "cancelled"}]
    if activity and (not executed or all(row.get("hotspotActivity") == activity for row in executed)):
        end = "不设结束时间，持续至本轮仿真结束" if activity["end_seconds"] is None else f"{activity['end_seconds']:g} 秒结束后开始离场"
        return (f"初始分布固定按道路长度分布预设；热点活动按确认时间执行：{activity['start_seconds']:g} 秒开始，{end}。"
                "活动时间与单轮观察上限独立；提前到达者等待，正常全部离场可提前完成，不补造后续采样。各轮实际执行规则见逐轮记录。")
    return ("初始分布固定按道路长度分布预设；本组包含原预设或未确认执行规则的轮次，"
            "原活动过程固定按 600–800 秒观景预设执行，不能视为需求中的热点时间已生效。单轮时长按确认快照执行。")
