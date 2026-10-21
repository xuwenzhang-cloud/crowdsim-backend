import pytest
from arde_emergency.experiment_audit import fingerprint, peak_context
from arde_emergency.experiment_service import ArdeControlServiceBundle, bound_requirement
from test_arde_experiment_binding import scenario


def setup_run(tmp_path):
    bundle = ArdeControlServiceBundle(data_dir=str(tmp_path))
    experiment = bundle.orchestrator.create_experiment('u', 'w', {'name': 'audit', 'mode': 'debug',
        'scenario': scenario(), 'methods': [f'C{i}' for i in range(6)]})
    run = bundle.repository.create_run('u', 'w', experiment['experiment_id'], 'C5', 7, {})
    active = {'run': run, 'experiment': experiment, 'user_id': 'u', 'workspace_id': 'w'}
    result = {'type': 'init', 'run_id': 'runtime-1', 'requirement': {
        'fingerprint': 'sha256:' + bound_requirement(experiment['scenario']).fingerprint, 'population_total': 50},
        'demand': {'effective_count': 50}, 'scenario': {'network_sha256': 'net'}}
    return bundle, active, result


@pytest.mark.parametrize('field,value', [('fingerprint', 'wrong'), ('population_total', 30)])
def test_reset_rejects_runtime_mismatch(tmp_path, field, value):
    bundle, active, result = setup_run(tmp_path)
    try:
        result['requirement'][field] = value
        with pytest.raises(RuntimeError, match='reset evidence'):
            bundle.orchestrator.audit_ack(active, {'action': 'reset', 'seed': 7, 'count': 50}, result)
        assert not bundle.repository.get_run('u', 'w', active['run']['run_id'])['ardeAudit']['reset']['verified']
    finally: bundle.gateway.close()


def test_reset_checks_seed_command_and_generated_count(tmp_path):
    bundle, active, result = setup_run(tmp_path)
    try:
        for command, planned in [({'action':'reset','seed':8,'count':50},50), ({'action':'reset','seed':7,'count':50},30)]:
            result['demand']['effective_count'] = planned
            with pytest.raises(RuntimeError): bundle.orchestrator.audit_ack(active, command, result)
    finally: bundle.gateway.close()


def test_gateway_persists_real_ack_and_manual_control(tmp_path):
    bundle, active, result = setup_run(tmp_path)
    try:
        original = bundle.orchestrator.gateway.gateway
        original.submit = lambda command: 'request'
        original.wait_for_ack = lambda *a, **k: result
        bundle.orchestrator._active = active
        gateway = bundle.orchestrator.gateway
        request = gateway.submit({'action':'reset','seed':7,'count':50})
        assert gateway.wait_for_ack(request, timeout=1) == result
        assert gateway.pending == {}
        bundle.orchestrator._mark_manual('u','w',active['run']['run_id'],'pause')
        run = bundle.repository.get_run('u','w',active['run']['run_id'])
        assert run['manualIntervention']
        assert run['ardeAudit']['manualControls'] == ['pause']
        assert run['ardeAudit']['reset']['verified']
    finally: bundle.gateway.close()


def test_peak_location_never_invents_a_hotspot_and_links_prior_decisions():
    observations = [{'observationId':'a','simTimeSeconds':10,'global':{'maxDensity':.5},
        'edges':[{'edgeId':'e1','density':.5}], 'regions':[{'regionId':'r1','density':.2}]},
        {'observationId':'b','simTimeSeconds':20,'global':{'maxDensity':.1}}]
    peak = peak_context(observations,[{'observationId':'a','decisionId':'d1','actions':[{'actionType':'observe_only'}]},
                                     {'observationId':'b','decisionId':'future','actions':[]}])
    assert peak['timeSeconds'] == 10
    assert peak['locations'] == [{'kind':'edges','id':'e1'}]
    assert [d['decisionId'] for d in peak['precedingDecisions']] == ['d1']
    observations[0]['edges'] = []
    assert peak_context(observations,[])['locationStatus'] == 'not_available'


def test_old_or_manually_intervened_runs_do_not_form_valid_pairs(tmp_path):
    bundle, active, result = setup_run(tmp_path)
    try:
        bundle.orchestrator.audit_ack(active,{'action':'reset','seed':7,'count':50},result)
        bundle.orchestrator._mark_manual('u','w',active['run']['run_id'],'pause')
        for method in ['C0','C2']:
            bundle.repository.create_run('u','w',active['experiment']['experiment_id'],method,7,{})
        for run in bundle.repository.list_runs('u','w',active['experiment']['experiment_id']):
            bundle.repository.update_run('u','w',run['run_id'],{'status':'completed'})
        report=bundle.reports.generate('u','w',active['experiment']['experiment_id'])
        assert report['fairComparison']['eligibleRunCount'] == 0
        assert all(p['pairCount'] == 0 for p in report['pairedComparisons'])
        assert '存在人工干预' in next(r for r in report['runs'] if r['methodId']=='C5')['comparisonExclusions']
    finally: bundle.gateway.close()


def test_matching_seed_boundaries_qualify_but_different_initial_people_do_not(tmp_path):
    bundle, active, result = setup_run(tmp_path)
    try:
        exp = active['experiment']
        for method in ['C0','C1','C2','C3','C4']:
            bundle.repository.create_run('u','w',exp['experiment_id'],method,7,{})
        runs = bundle.repository.list_runs('u','w',exp['experiment_id'])
        for run in runs:
            audit = {'version':'audit-v1','scenarioFingerprint':fingerprint(exp['scenario']),
                'started':True,'reset':{'verified':True,'population':50,'scenario':{'network_sha256':'net'}},
                'initialFrame':{'fingerprint':'same','timeSeconds':1,'personCount':50},'manualControls':[]}
            bundle.repository.update_run('u','w',run['run_id'],{'status':'completed','ardeAudit':audit})
            if run['method_id'] in {'C2','C3','C4'}:
                bundle.repository.record('llm','u','w',run['run_id'],{'status':'accepted'})
        assert bundle.reports.generate('u','w',exp['experiment_id'])['fairComparison']['eligibleRunCount'] == 6
        audit['initialFrame']['fingerprint'] = 'different-people'
        bundle.repository.update_run('u','w',runs[-1]['run_id'],{'ardeAudit':audit})
        report = bundle.reports.generate('u','w',exp['experiment_id'])
        assert report['fairComparison']['eligibleRunCount'] == 0
        assert all('同种子运行边界证据不一致' in r['comparisonExclusions'] for r in report['runs'])
    finally: bundle.gateway.close()


def test_initial_people_are_captured_before_the_first_decision(tmp_path, monkeypatch):
    import threading
    from crowdsim.experiments.orchestrator import ExperimentOrchestrator
    bundle, active, _ = setup_run(tmp_path)
    try:
        active.update(accept_frames=True,runtime_run_id='runtime',terminal=threading.Event())
        def parent(self, active, frame):
            assert active['ardeAudit']['initialFrame']['personCount'] == 1
        monkeypatch.setattr(ExperimentOrchestrator,'_handle_frame',parent)
        frame={'run_id':'runtime','step_seconds':1,'pedestrians':[{'id':'p','lng':121,'lat':31,'state':{'age':20}}]}
        bundle.orchestrator._handle_frame(active,frame)
        saved=bundle.repository.get_run('u','w',active['run']['run_id'])['ardeAudit']['initialFrame']
        assert saved['timeSeconds'] == 1
        assert saved['fingerprint']
    finally: bundle.gateway.close()


def test_peak_window_links_receipts_and_preserves_missing_followup():
    points = [{'observationId':'a','simTimeSeconds':10,'global':{'maxDensity':.8,'riskIndex':.3}},
              {'observationId':'b','simTimeSeconds':40,'global':{'maxDensity':.4,'riskIndex':.2}}]
    decisions = [{'observationId':'a','decisionId':'d','actions':[{'actionId':'x','actionType':'set_inflow_rate'}]}]
    receipts = [{'decisionId':'d','actionId':'x','status':'applied'}, {'decisionId':'other','status':'rejected'}]
    effect = peak_context(points, decisions, receipts, 30)['precedingDecisions'][0]
    assert effect['afterTimeSeconds'] == 40
    assert effect['deltas']['maxDensity'] == pytest.approx(-.4)
    assert len(effect['receipts']) == 1
    missing = peak_context(points[:1], decisions, receipts, 30)['precedingDecisions'][0]
    assert missing['after'] is None
    assert missing['deltas'] == {}


def test_generated_intervention_is_not_counted_as_applied_without_terminal_receipt(tmp_path):
    bundle, active, _ = setup_run(tmp_path)
    try:
        run = active['run']
        bundle.repository.record('decisions','u','w',run['run_id'],{'actions':[
            {'actionId':'a','actionType':'observe_only'}, {'actionId':'b','actionType':'set_inflow_rate'},
            {'actionId':'c','actionType':'publish_guidance'}]})
        for action, status in [('a','applied'),('b','queued'),('c','applied')]:
            bundle.repository.record('acks','u','w',run['run_id'],{'actionId':action,'status':status})
        row=bundle.reports.generate('u','w',active['experiment']['experiment_id'])['runs'][0]
        assert row['observationOnlyActionCount'] == 1
        assert row['interventionActionCount'] == 2
        assert row['appliedInterventionActionCount'] == 1
    finally: bundle.gateway.close()
