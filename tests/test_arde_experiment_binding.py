from copy import deepcopy
import pytest
from arde_emergency.experiment_service import ArdeExperimentOrchestrator, ArdeControlServiceBundle, bound_requirement
import json
from pathlib import Path
from crowdsim.domain.requirement_spec import RequirementSpec
from crowdsim.experiments.orchestrator import ExperimentOrchestrator
from crowdsim.experiments.controller_registry import ControllerRegistry
from crowdsim.experiments.contracts import ContractError, validate_decision


def scenario():
    raw = json.loads((Path(__file__).resolve().parents[1] / 'config/during_presets/memorial_tower_v1.json').read_text(encoding='utf-8'))['requirement']
    raw['population']['total'] = 50
    if raw.get('schema_version') == 2:
        raw.setdefault('experiment', {})['duration_seconds'] = 120
    return {'requirementId': 'req-accepted', 'ardeRequirementSnapshot': raw,
            'population': 50, 'durationSeconds': 120, 'seedSet': [7]}


class Gateway:
    def __init__(self):
        self.messages = []
        self.bad = False
    def add_listener(self, callback):
        pass
    def submit(self, payload):
        self.messages.append(deepcopy(payload))
        return 'command'
    def wait_for_ack(self, request_id, timeout):
        spec = RequirementSpec.parse(self.messages[-1]['requirement'])
        return {'type': 'requirement_accepted', 'requirement_id': 'req-derived',
                'fingerprint': 'bad' if self.bad else 'sha256:' + spec.fingerprint}


def test_existing_orchestrator_ignores_source_requirement_id():
    gateway = Gateway()
    gateway.wait_for_ack = lambda *a, **k: {'type': 'status', 'requirement': None}
    original = ExperimentOrchestrator(object(), gateway, object())
    assert 'requirement_id' not in original._prepare_reset_payload(scenario(), 7)
    assert gateway.messages == [{'action': 'get_status'}]


def test_arde_binds_same_snapshot_for_every_method_and_seed():
    gateway = Gateway()
    orchestrator = ArdeExperimentOrchestrator(object(), gateway, object())
    source = scenario()
    original = deepcopy(source)
    for seed in [7, 8]:
        for _method in range(6):
            assert orchestrator._prepare_reset_payload(source, seed) == {
                'action': 'reset', 'seed': seed, 'count': 50, 'requirement_id': 'req-derived'}
    assert len(gateway.messages) == 12
    assert all(m['requirement'] == source['ardeRequirementSnapshot'] for m in gateway.messages)
    assert source == original


def test_arde_refuses_mismatched_ack():
    gateway = Gateway()
    gateway.bad = True
    orchestrator = ArdeExperimentOrchestrator(object(), gateway, object())
    with pytest.raises(RuntimeError, match='fingerprint'):
        orchestrator._prepare_reset_payload(scenario(), 7)


def test_binding_rejects_mismatched_population_and_debug_id():
    data = scenario()
    data['population'] = 600
    with pytest.raises(ContractError, match='population'):
        bound_requirement(data)
    data = scenario()
    data['requirementId'] = 'req-default'
    with pytest.raises(ContractError, match='accepted'):
        bound_requirement(data)


def test_non_arde_resets_delegate_unchanged(monkeypatch):
    sentinel = {'old': True}
    monkeypatch.setattr(ExperimentOrchestrator, '_prepare_reset_payload', lambda *a: sentinel)
    orchestrator = ArdeExperimentOrchestrator(object(), Gateway(), object())
    assert orchestrator._prepare_reset_payload({'population': 40}, 7) is sentinel


def test_service_has_single_listener_and_opt_in_capability(tmp_path):
    bundle = ArdeControlServiceBundle(data_dir=str(tmp_path))
    assert len(bundle.gateway._listeners) == 1
    assert bundle.orchestrator.health()['ardeRequirementBinding'] == 'snapshot-v1'
    created = bundle.orchestrator.create_experiment('user', 'workspace', {
        'name': 'six', 'scenario': scenario(), 'methods': [f'C{i}' for i in range(6)], 'mode': 'debug'})
    assert created['scenario']['ardeRequirementSnapshot'] == scenario()['ardeRequirementSnapshot']
    bundle.gateway.close()


def test_registry_c5_produces_contract_valid_actions_and_learns():
    from test_arde_controller import observation, evaluation
    registry = ControllerRegistry()
    assert {x['methodId'] for x in registry.catalog()} == {f'C{i}' for i in range(6)}
    controller = registry.create('C5', {'randomSeed': 7})
    controller.reset({'runId': 'run', 'seed': 7})
    decision = controller.decide(observation())
    assert validate_decision(decision)['controllerId'] == 'arde_dynamic'
    for action in decision['actions']:
        controller.on_ack({'requestId': 'req-action', 'actionId': action['actionId'], 'status': 'applied', 'success': True})
    assert isinstance(controller.on_effect(evaluation(decision['decisionId']))['reward'], float)

def test_six_method_queue_and_report_preserve_binding_speed_and_fallback(tmp_path, monkeypatch):
    bundle = ArdeControlServiceBundle(data_dir=str(tmp_path))
    orchestrator = bundle.orchestrator
    monkeypatch.setattr(orchestrator, '_ensure_worker', lambda: None)
    experiment = orchestrator.create_experiment('u', 'w', {
        'name': 'smoke', 'scenario': scenario(), 'mode': 'debug', 'methods': [f'C{i}' for i in range(6)]})
    runs = orchestrator.start_experiment('u', 'w', experiment['experiment_id'])
    assert len(runs) == 6
    assert {r['seed'] for r in runs} == {7}
    assert {r['method_id'] for r in runs} == {f'C{i}' for i in range(6)}
    assert orchestrator._queue.qsize() == 6
    for run in runs:
        rid = run['run_id']
        bundle.repository.update_run('u', 'w', rid, {'status': 'completed'})
        for time, speed in [(0, .8), (120, .4)]:
            bundle.repository.record('observations', 'u', 'w', rid, {
                'simTimeSeconds': time, 'global': {'meanSpeed': speed, 'riskIndex': .5, 'activePopulation': 50}})
        if run['method_id'] == 'C2':
            bundle.repository.record('llm', 'u', 'w', rid, {'status': 'fallback'})
    report = bundle.reports.generate('u', 'w', experiment['experiment_id'])
    assert report['ardeBinding']['sourceRequirementId'] == 'req-accepted'
    assert report['methods']['C5']['meanObservedSpeed']['mean'] == pytest.approx(.6)
    assert report['methods']['C5']['minimumObservedMeanSpeed']['mean'] == .4
    assert report['methods']['C2']['llmFallbackCount']['mean'] == 1
    assert any('不能视为真实大模型基线' in line for line in report['limitations'])
    assert len([c for c in report['pairedComparisons'] if c['metric'] == 'meanObservedSpeed']) == 5
    bundle.gateway.close()

def test_arde_http_history_records_respect_workspace(tmp_path):
    import threading
    import httpx
    from arde_emergency.experiment_service import create_server
    server = create_server('127.0.0.1', 0, data_dir=str(tmp_path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}', trust_env=False) as client:
            headers = {'X-CrowdSim-User': 'crowdsim-local-user', 'X-CrowdSim-Workspace': 'arde-control-v1'}
            assert client.get('/crowdSim/control/health').json()['data']['ardeRequirementBinding'] == 'snapshot-v1'
            response = client.post('/crowdSim/control/experiments', headers=headers, json={
                'name': 'http-smoke', 'mode': 'debug', 'scenario': scenario(), 'methods': [f'C{i}' for i in range(6)]})
            assert response.status_code == 201
            experiment_id = response.json()['data']['experiment_id']
            run = server.bundle.repository.create_run('crowdsim-local-user', 'arde-control-v1', experiment_id, 'C5', 7, {})
            rid = run['run_id']
            server.bundle.repository.record('acks', 'crowdsim-local-user', 'arde-control-v1', rid, {'status': 'applied', 'requestId': 'request-1'})
            records = client.get(f'/crowdSim/control/runs/{rid}/arde-records', headers=headers)
            assert records.status_code == 200
            assert records.json()['data']['acks'][0]['payload']['status'] == 'applied'
            assert client.get(f'/crowdSim/control/runs/{rid}/arde-records').status_code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

def reviewed_scenario():
    data = scenario()
    source = deepcopy(data['ardeRequirementSnapshot'])
    source['population']['total'] = 600
    data['ardeSourceRequirementSnapshot'] = source
    data['ardeConfigurationConfirmation'] = {'confirmed': True, 'changes': [
        {'field': 'population', 'label': '人数', 'source': 600, 'value': 50, 'unit': '人'}]}
    return data


def test_arde_review_accepts_explicit_adjustments_without_modifying_source():
    data = reviewed_scenario()
    assert bound_requirement(data).population_total == 50
    assert data['ardeSourceRequirementSnapshot']['population']['total'] == 600


@pytest.mark.parametrize('confirmation', [{}, {'confirmed': False, 'changes': []}, {'confirmed': True, 'changes': []}])
def test_arde_review_rejects_missing_or_inaccurate_confirmation(confirmation):
    data = reviewed_scenario()
    data['ardeConfigurationConfirmation'] = confirmation
    with pytest.raises(ContractError, match='confirmed review'):
        bound_requirement(data)


def test_arde_review_rejects_unrelated_changes_to_source():
    data = reviewed_scenario()
    data['ardeRequirementSnapshot']['project']['title'] = 'silently replaced scene'
    with pytest.raises(ContractError, match='only adjust'):
        bound_requirement(data)


def test_arde_health_discloses_configuration_review_version(tmp_path):
    bundle = ArdeControlServiceBundle(data_dir=str(tmp_path))
    health = bundle.orchestrator.health()
    assert health['ardeFlowVersion'] == '20261021.4'
    assert health['ardeConfigurationReview'] == 'review-v1'
    bundle.gateway.close()
