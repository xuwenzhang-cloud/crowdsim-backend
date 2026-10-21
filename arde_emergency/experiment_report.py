"""Extra ARDE comparison fields; original report service remains unchanged."""
import math
from statistics import mean
from crowdsim.experiments.report_service import ReportService
from crowdsim.experiments.metrics import aggregate_method_runs
from crowdsim.experiments.statistics import paired_comparison


def speed_summary(observations):
    samples = [item.get('payload', item).get('global', {}).get('meanSpeed') for item in observations]
    samples = [float(v) for v in samples if type(v) in (int, float) and math.isfinite(v)]
    return {'meanObservedSpeed': mean(samples) if samples else None,
            'minimumObservedMeanSpeed': min(samples) if samples else None}


class ArdeReportService(ReportService):
    def generate(self, user_id, workspace_id, experiment_id):
        report = super().generate(user_id, workspace_id, experiment_id)
        experiment = self.repository.get_experiment(user_id, workspace_id, experiment_id)
        if not experiment.get('scenario', {}).get('ardeRequirementSnapshot'):
            return report
        for row in report['runs']:
            row.update(speed_summary(self.repository.list_records('observations', user_id, workspace_id, row['runId'], limit=100000)))
            calls = self.repository.list_records('llm', user_id, workspace_id, row['runId'], limit=100000)
            row['llmFallbackCount'] = sum(item.get('payload', item).get('status') == 'fallback' for item in calls)
        completed = [r for r in report['runs'] if r['status'] == 'completed']
        for method, summary in report['methods'].items():
            summary.update(aggregate_method_runs([
                {key: row[key] for key in ('meanObservedSpeed', 'minimumObservedMeanSpeed', 'llmFallbackCount')}
                for row in completed if row['methodId'] == method
            ]))
        for baseline in sorted({r['methodId'] for r in completed} - {'C5'}):
            for metric, lower in [('meanRiskIndex', True), ('peakRiskIndex', True), ('peakDensity', True),
                                  ('meanObservedSpeed', False), ('minimumObservedMeanSpeed', False)]:
                report['pairedComparisons'].append(paired_comparison(completed, baseline, 'C5', metric, lower_is_better=lower))
        report['ardeBinding'] = {'sourceRequirementId': experiment['scenario']['requirementId'], 'mode': 'snapshot-v1'}
        report['limitations'].append('速度为观测帧全场平均速度的样本均值及最小值；不是单个行人的最低速度。C5 默认不启用可选 LLM 指导。')
        self.repository.save_report(user_id, workspace_id, experiment_id, report)
        return report
