"""Deterministic experiment report payload generation."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .contracts import public_time
from .metrics import aggregate_method_runs, summarize_control_records, summarize_observations, summarize_during_runs
from .statistics import paired_comparison


class ReportService:
    def __init__(self, repository) -> None:
        self.repository = repository

    def generate(self, user_id: str, workspace_id: str, experiment_id: str) -> dict[str, Any]:
        experiment = self.repository.get_experiment(user_id, workspace_id, experiment_id)
        if not experiment:
            raise LookupError("EXPERIMENT_NOT_FOUND")
        runs = self.repository.list_runs(user_id, workspace_id, experiment_id)
        if experiment.get("duringObservation"):
            from .during_observation import observation_report
            # Do not publish a pre-reset report after its records were removed.
            with self.repository._lock:
                experiment = self.repository.get_experiment(user_id, workspace_id, experiment_id)
                runs = self.repository.list_runs(user_id, workspace_id, experiment_id)
                return observation_report(self.repository, user_id, workspace_id, experiment, runs)
        is_during = bool(experiment.get("scenario", {}).get("presetId"))
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        run_rows = []
        arde_evolution: dict[str, list[dict[str, Any]]] = {}
        llm_fallback_count = 0
        for run in runs:
            observations = self.repository.list_records("observations", user_id, workspace_id, run["run_id"], limit=100000)
            decisions = self.repository.list_records("decisions", user_id, workspace_id, run["run_id"], limit=100000)
            evaluations = self.repository.list_records("evaluations", user_id, workspace_id, run["run_id"], limit=100000)
            llm_records = self.repository.list_records("llm", user_id, workspace_id, run["run_id"], limit=100000)
            llm_fallback_count += sum(
                str(item.get("payload", item).get("status") or "").lower() == "fallback"
                for item in llm_records
            )
            summary = {
                **summarize_observations(observations),
                **summarize_control_records(
                    decisions,
                    self.repository.list_records("acks", user_id, workspace_id, run["run_id"], limit=100000),
                    evaluations,
                    llm_records,
                ),
            }
            if is_during and "during" not in summary:
                summary.update(finalCompletionRate=None, t95Seconds=None, duringRunComplete=False)
            row = {"runId": run["run_id"], "methodId": run["method_id"], "seed": run["seed"], "status": run["status"], "formal": run.get("formal", True), **summary}
            run_rows.append(row)
            # Completed debug runs are still valid descriptive evidence. Keep
            # them in summaries so exploratory experiments do not render as
            # empty cards; limitations below prevent statistical overclaiming.
            if (run["status"] == "completed" and summary
                    and (not is_during or (summary.get("duringRunComplete")
                         and summary.get("crowdingCoverage") == "complete"
                         and summary["during"].get("population_valid")
                         and summary["during"].get("preset_fingerprint") == experiment["scenario"]["presetFingerprint"]))):
                grouped[run["method_id"]].append(summary)
            if run["method_id"] == "C5":
                series = []
                for item in decisions:
                    decision = item.get("payload", item)
                    trace = decision.get("algorithmTrace") or {}
                    series.append({
                        "type": "decision", "decisionId": decision.get("decisionId"), "step": trace.get("step"),
                        "risk": trace.get("risk"), "strategyStats": trace.get("strategyStats"),
                        "outer": trace.get("outer"), "choices": trace.get("choices"),
                    })
                for item in evaluations:
                    evaluation = item.get("payload", item)
                    series.append({
                        "type": "evaluation", "decisionId": evaluation.get("decisionId"),
                        "window": evaluation.get("window"), "learning": evaluation.get("learning"),
                        "windowCensored": evaluation.get("windowCensored", False),
                    })
                arde_evolution[run["run_id"]] = series
        comparisons = []
        completed_rows = [row for row in run_rows if row.get("status") == "completed"]
        for baseline in ([] if is_during else sorted({row["methodId"] for row in completed_rows} - {"C5"})):
            for metric, lower_is_better in (
                ("cumulativeRiskExposurePersonSeconds", True),
                ("highRiskPersonMinutes", True),
                ("t95Seconds", True),
                ("finalCompletionRate", False),
                ("appliedSuccessRate", False),
            ):
                comparisons.append(paired_comparison(completed_rows, baseline, "C5", metric, lower_is_better=lower_is_better))
        limitations = []
        debug_run_count = sum(not row.get("formal", True) for row in completed_rows)
        if debug_run_count:
            limitations.append(
                f"当前报告包含 {debug_run_count} 个已完成的调试运行；页面展示其描述性指标与配对差值，但不将其作为正式统计结论。"
            )
        paired_seed_count = len(set(experiment.get("scenario", {}).get("seedSet") or []))
        if paired_seed_count < 2:
            limitations.append("当前仅有 1 个配对随机种子，比较结果仅为描述性结果，不能进行统计推断。")
        if llm_fallback_count:
            limitations.append(
                f"共有 {llm_fallback_count} 次 LLM 决策因模型调用失败而使用确定性降级策略；C2/C3/C4 结果不能视为真实大模型基线。"
            )
        missing_pair_count = sum(item.get("pairCount", 0) == 0 for item in comparisons)
        if missing_pair_count:
            limitations.append(
                f"有 {missing_pair_count} 项配对指标缺少双方均有效的数值，报告不会据此判断方法优劣。"
            )
        report = {
            "schemaVersion": "1.0",
            "experimentId": experiment_id,
            "name": experiment["name"],
            "generatedAt": public_time(),
            "methods": {method: aggregate_method_runs(values) for method, values in sorted(grouped.items())},
            "runs": run_rows,
            "pairedComparisons": comparisons,
            "ardeEvolution": arde_evolution,
            "limitations": limitations,
            "dataScope": {
                "completedRunCount": len(completed_rows),
                "formalRunCount": sum(row.get("formal", True) for row in completed_rows),
                "debugRunCount": debug_run_count,
                "descriptiveOnly": bool(debug_run_count or paired_seed_count < 2),
            },
        }
        if is_during:
            report["during"] = summarize_during_runs(run_rows, experiment["scenario"])
            report["dataScope"].update(descriptiveOnly=True, validDuringRunCount=report["during"]["validRunCount"])
            report["limitations"].append(
                "持续拥挤使用后端每步累计结果；频率仅统计完整结束且观测覆盖完整的轮次。"
                "它是预设仿真样本频率；完成率和 95% 时间指行程完成，不表示到达安全区。"
            )
        if experiment.get("duringReplay"):
            from .during_strategy import replay_comparison
            report["strategyComparison"] = replay_comparison(experiment, run_rows, self.repository, user_id, workspace_id)
            report["limitations"].append(report["strategyComparison"]["limitations"])
        self.repository.save_report(user_id, workspace_id, experiment_id, report)
        return report
