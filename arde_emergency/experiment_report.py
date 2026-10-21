"""Extra ARDE comparison fields; original report service remains unchanged."""
import math
from statistics import mean
from crowdsim.experiments.report_service import ReportService
from crowdsim.experiments.metrics import aggregate_method_runs
from crowdsim.experiments.statistics import paired_comparison
from .experiment_audit import peak_context, fingerprint


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
        persisted = {r['run_id']: r for r in self.repository.list_runs(user_id, workspace_id, experiment_id)}
        for row in report['runs']:
            observations = self.repository.list_records('observations', user_id, workspace_id, row['runId'], limit=100000)
            decisions = self.repository.list_records('decisions', user_id, workspace_id, row['runId'], limit=100000)
            acks = self.repository.list_records('acks', user_id, workspace_id, row['runId'], limit=100000)
            row.update(speed_summary(observations))
            row['peakDensityContext'] = peak_context(observations, decisions, acks, experiment['evaluationWindowSeconds'])
            calls = self.repository.list_records('llm', user_id, workspace_id, row['runId'], limit=100000)
            row['llmFallbackCount'] = sum(item.get('payload', item).get('status') == 'fallback' for item in calls)
            actions = [a for d in decisions for a in d.get('payload', d).get('actions', [])]
            row['observationOnlyActionCount'] = sum(a.get('actionType') == 'observe_only' for a in actions)
            row['interventionActionCount'] = sum(a.get('actionType') != 'observe_only' for a in actions)
            applied = {a.get('payload', a).get('actionId') for a in acks if a.get('payload', a).get('status') == 'applied'}
            row['appliedInterventionActionCount'] = sum(a.get('actionType') != 'observe_only' and a.get('actionId') in applied for a in actions)
            row['llmAcceptedCount'] = sum(item.get('payload', item).get('status') == 'accepted' for item in calls)
            actual = persisted[row['runId']]
            audit = actual.get('ardeAudit') or {}
            row['fairnessAudit'] = audit
            reasons = []
            if row['status'] != 'completed': reasons.append('运行未完成')
            if not audit.get('reset', {}).get('verified'): reasons.append('缺少已验证的重置回执')
            if audit.get('scenarioFingerprint') != fingerprint(experiment['scenario']): reasons.append('场景配置指纹缺失或不一致')
            if actual.get('manualIntervention') or audit.get('manualControls'): reasons.append('存在人工干预')
            if not audit.get('started'): reasons.append('缺少仿真启动成功回执')
            if not audit.get('initialFrame'): reasons.append('缺少控制前首帧人群证据')
            if row['llmFallbackCount']: reasons.append('LLM 调用回退')
            if row['methodId'] in {'C2', 'C3', 'C4'} and (not calls or row['llmAcceptedCount'] != len(calls)):
                reasons.append('模型调用未全部通过真实响应校验')
            if experiment['scenario'].get('emergencyEvent', {}).get('enabled') and not (audit.get('event', {}).get('applied') and audit.get('event', {}).get('beforeStart')):
                reasons.append('缺少启动前事件应用回执')
            row['comparisonExclusions'] = reasons
        completed = [r for r in report['runs'] if r['status'] == 'completed']
        for method, summary in report['methods'].items():
            summary.update(aggregate_method_runs([
                {key: row[key] for key in ('meanObservedSpeed', 'minimumObservedMeanSpeed', 'llmFallbackCount')}
                for row in completed if row['methodId'] == method
            ]))
        # Compare runtime boundary evidence for each paired seed, ignoring IDs
        # which are expected to be unique for each independent simulation.
        for seed in experiment['scenario']['seedSet']:
            group = [r for r in report['runs'] if persisted[r['runId']]['seed'] == seed]
            boundaries = {fingerprint({'scenario': r['fairnessAudit'].get('reset', {}).get('scenario'),
                'population': r['fairnessAudit'].get('reset', {}).get('population'),
                'event': r['fairnessAudit'].get('event', {}).get('command'),
                'initialFrame': r['fairnessAudit'].get('initialFrame')}) for r in group if r['fairnessAudit']}
            if len(boundaries) > 1:
                for row in group: row['comparisonExclusions'].append('同种子运行边界证据不一致')
        eligible = [r for r in completed if not r['comparisonExclusions']]
        report['pairedComparisons'] = []
        for baseline in sorted(set(experiment['methods']) - {'C5'}):
            for metric, lower in [('cumulativeRiskExposurePersonSeconds', True), ('highRiskPersonMinutes', True),
                                  ('t95Seconds', True), ('finalCompletionRate', False), ('appliedSuccessRate', False),
                                  ('meanRiskIndex', True), ('peakRiskIndex', True), ('peakDensity', True),
                                  ('meanObservedSpeed', False), ('minimumObservedMeanSpeed', False)]:
                report['pairedComparisons'].append(paired_comparison(eligible, baseline, 'C5', metric, lower_is_better=lower))
        report['fairComparison'] = {'version': 'audit-v1', 'eligibleRunCount': len(eligible),
            'verificationLevel': 'boundary-only',
            'excludedRunCount': len(report['runs']) - len(eligible),
            'runtimeSeedVerified': False, 'fullInitialPopulationVerified': False,
            'scope': '需求/人数重置回执、场景配置、事件应用与人工干预；不等于完整初始人群相同的证明'}
        report['dataScope'].update(descriptiveOnly=True, auditedRunCount=len(eligible))
        report['limitations'].append('方法卡片保留全部已完成运行的描述性指标；配对表仅使用通过审计且无人工干预、无模型回退的运行。')
        report['limitations'].append('当前仿真回执未提供实际随机种子和完整初始人群指纹，不能宣称已证明初始人群完全相同。峰值位置和之前动作仅反映时间关联，不证明因果。')
        report['ardeBinding'] = {'sourceRequirementId': experiment['scenario']['requirementId'], 'mode': 'snapshot-v1'}
        report['limitations'].append('速度为观测帧全场平均速度的样本均值及最小值；不是单个行人的最低速度。C5 默认不启用可选 LLM 指导。')
        self.repository.save_report(user_id, workspace_id, experiment_id, report)
        return report
