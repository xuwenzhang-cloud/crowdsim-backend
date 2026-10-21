"""ARDE-only experiment service; shared CrowdSim workflows remain unchanged.

Run: python -m arde_emergency.experiment_service --port 8766
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import os
import re

from crowdsim.domain.requirement_spec import RequirementSpec, requirement_observation_duration
from crowdsim.experiments.contracts import ContractError
from crowdsim.experiments.orchestrator import ExperimentOrchestrator
from crowdsim.experiments.service import ControlServiceBundle
from crowdsim.experiments.serve import ControlHttpServer, ControlRequestHandler, CONTROL_PREFIX
from .experiment_report import ArdeReportService
from .experiment_audit import AuditGateway, fingerprint


def bound_requirement(scenario):
    raw = scenario.get("ardeRequirementSnapshot")
    if raw is None:
        return None
    spec = RequirementSpec.parse(raw)
    if not scenario.get("requirementId") or scenario["requirementId"] == "req-default":
        raise ContractError("ARDE requires an accepted source requirement")
    if scenario.get("presetId"):
        raise ContractError("ARDE snapshot matrices cannot override a bound during preset")
    if scenario.get("population") != spec.population_total:
        raise ContractError("ARDE requirement population differs from experiment population")
    duration = requirement_observation_duration(spec.payload)
    if duration is not None and duration != scenario.get("durationSeconds"):
        raise ContractError("ARDE requirement duration differs from experiment duration")
    source_raw = scenario.get("ardeSourceRequirementSnapshot")
    if source_raw is not None:
        source = RequirementSpec.parse(source_raw)
        expected = deepcopy(source.payload)
        expected["population"]["total"] = spec.population_total
        if expected.get("schema_version") == 2:
            expected["experiment"] = {**expected.get("experiment", {}), "duration_seconds": scenario["durationSeconds"]}
        if spec.payload != expected:
            raise ContractError("ARDE may only adjust source population and duration")
        changes = []
        for field, label, old, new, unit in (
            ("population", "人数", source.population_total, spec.population_total, "人"),
            ("durationSeconds", "时长", source.payload.get("experiment", {}).get("duration_seconds"), scenario["durationSeconds"], "秒"),
        ):
            if old is not None and old != new:
                changes.append({"field": field, "label": label, "source": old, "value": new, "unit": unit})
        confirmation = scenario.get("ardeConfigurationConfirmation") or {}
        if confirmation.get("confirmed") is not True or confirmation.get("changes") != changes:
            raise ContractError("ARDE source adjustments require an exact confirmed review")
        event = scenario.get("emergencyEvent") or {}
        if event.get("enabled") and source.payload["scenario"]["event_category"]["id"] != "emergency":
            raise ContractError("ARDE fire injection requires an emergency source requirement")
    return spec


class ArdeExperimentOrchestrator(ExperimentOrchestrator):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.gateway = AuditGateway(self.gateway, self)

    def audit_ack(self, active, command, result):
        run = active['run']
        scenario = active['experiment']['scenario']
        evidence = active.setdefault('ardeAudit', {'version': 'audit-v1', 'seedRequested': run['seed'],
            'scenarioFingerprint': fingerprint(scenario), 'manualControls': []})
        action = command['action']
        if action == 'reset':
            actual = (result or {}).get('requirement') or {}
            expected = 'sha256:' + bound_requirement(scenario).fingerprint
            demand = (result or {}).get('demand') or {}
            planned = demand.get('effective_count', demand.get('planned'))
            valid = ((result or {}).get('type') == 'init' and bool((result or {}).get('run_id'))
                and actual.get('fingerprint') == expected and actual.get('population_total') == scenario['population']
                and command.get('seed') == run['seed'] and command.get('count') == scenario['population']
                and (planned is None or planned == scenario['population']))
            evidence['reset'] = {'verified': valid, 'runtimeRunId': (result or {}).get('run_id'),
                'requirementFingerprint': actual.get('fingerprint'), 'population': actual.get('population_total'),
                'seedRequested': command.get('seed'), 'demand': deepcopy((result or {}).get('demand', {})),
                'scenario': deepcopy((result or {}).get('scenario', {}))}
        elif action == 'set_event':
            event = scenario.get('emergencyEvent') or {}
            matches = all(command.get(key) == event.get(key, default) for key, default in
                [('lng', None), ('lat', None), ('radius', 60.0), ('intensity', .85), ('growthSeconds', 30.0), ('decaySeconds', 0.0)])
            matches = matches and command.get('duration') == event.get('durationSeconds', scenario['durationSeconds']) and command.get('eventType') == event.get('eventType', 'fire')
            evidence['event'] = {'command': {k: v for k, v in command.items() if k != 'id'},
                'id': command['id'], 'applied': (result or {}).get('status') == 'applied' and matches, 'beforeStart': not evidence.get('started', False)}
        elif action == 'start':
            evidence['started'] = (result or {}).get('status') == 'applied'
        self.repository.update_run(active['user_id'], active['workspace_id'], run['run_id'], {'ardeAudit': deepcopy(evidence)})
        if action == 'reset' and not evidence['reset']['verified']:
            raise RuntimeError('ARDE reset evidence does not match the confirmed requirement fingerprint/population')
        if action == 'set_event' and not evidence['event']['applied']:
            raise RuntimeError('ARDE event evidence does not match the confirmed emergency')

    def _mark_manual(self, user, workspace, run_id, action):
        run = self.repository.get_run(user, workspace, run_id)
        experiment = self.repository.get_experiment(user, workspace, run['experiment_id'])
        if experiment['scenario'].get('ardeRequirementSnapshot'):
            audit = deepcopy(run.get('ardeAudit') or {})
            audit.setdefault('manualControls', []).append(action)
            self.repository.update_run(user, workspace, run_id, {'ardeAudit': audit, 'manualIntervention': True})
            if self._active and self._active['run']['run_id'] == run_id:
                self._active['ardeAudit'] = audit

    def _handle_frame(self, active, frame):
        if (active['experiment']['scenario'].get('ardeRequirementSnapshot')
                and active.get('accept_frames') and not active['terminal'].is_set()
                and frame.get('run_id') == active.get('runtime_run_id')
                and 'initialFrame' not in active.get('ardeAudit', {})):
            crowd = sorted([{'id': p.get('id'), 'lng': p.get('lng'), 'lat': p.get('lat'),
                'edge': p.get('edge'), 'state': p.get('state')} for p in frame.get('pedestrians', [])], key=lambda p: str(p['id']))
            audit = active.setdefault('ardeAudit', {})
            audit['initialFrame'] = {'timeSeconds': frame.get('step_seconds', frame.get('step')),
                'personCount': len(crowd), 'fingerprint': fingerprint(crowd),
                'scope': 'first observed frame before controller decisions; not complete future demand'}
            self.repository.update_run(active['user_id'], active['workspace_id'], active['run']['run_id'], {'ardeAudit': deepcopy(audit)})
        return super()._handle_frame(active, frame)

    def pause_run(self, user, workspace, run_id):
        result = super().pause_run(user, workspace, run_id)
        self._mark_manual(user, workspace, run_id, 'pause')
        return result

    def resume_run(self, user, workspace, run_id):
        result = super().resume_run(user, workspace, run_id)
        self._mark_manual(user, workspace, run_id, 'resume')
        return result

    def stop_run(self, user, workspace, run_id):
        result = super().stop_run(user, workspace, run_id)
        self._mark_manual(user, workspace, run_id, 'stop')
        return result

    def manual_action(self, user, workspace, run_id, action, *, reason):
        result = super().manual_action(user, workspace, run_id, action, reason=reason)
        self._mark_manual(user, workspace, run_id, 'manual_action')
        return result

    def health(self):
        return {**super().health(), "ardeRequirementBinding": "snapshot-v1",
                "ardeConfigurationReview": "review-v1", "ardeFlowVersion": "20261021.4", "ardeFairComparison": "audit-v1"}

    def create_experiment(self, user_id, workspace_id, payload):
        scenario = payload.get("scenario") or {}
        if "ardeRequirementSnapshot" in scenario:
            bound_requirement(scenario)
            if set(payload.get("methods") or []) != {"C0", "C1", "C2", "C3", "C4", "C5"}:
                raise ContractError("ARDE snapshot comparisons require all six methods C0-C5")
        return super().create_experiment(user_id, workspace_id, payload)

    def _prepare_reset_payload(self, scenario, seed):
        spec = bound_requirement(scenario)
        if spec is None:
            return super()._prepare_reset_payload(scenario, seed)
        # Called after acquiring the shared gateway lease, before EVERY method/seed.
        request_id = self.gateway.submit({
            "action": "submit_experiment_requirement", "requirement": deepcopy(spec.payload),
        })
        accepted = self.gateway.wait_for_ack(request_id, timeout=20)
        if (not accepted or accepted.get("type") != "requirement_accepted"
                or not accepted.get("requirement_id")
                or accepted.get("fingerprint") != "sha256:" + spec.fingerprint):
            raise RuntimeError("ARDE requirement snapshot was not acknowledged with its fingerprint")
        return {"action": "reset", "seed": int(seed), "count": spec.population_total,
                "requirement_id": accepted["requirement_id"]}


class ArdeControlServiceBundle(ControlServiceBundle):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.reports = ArdeReportService(self.repository)
        # Replace only the listener for the unstarted default orchestrator.
        self.gateway.remove_listener(self.orchestrator._on_gateway_event)
        self.orchestrator = ArdeExperimentOrchestrator(
            self.repository, self.gateway, self.registry, event_bus=self.events,
        )


class ArdeRequestHandler(ControlRequestHandler):
    def do_GET(self):
        path, _ = self._path()
        match = re.fullmatch(rf"{CONTROL_PREFIX}/runs/([^/]+)/arde-records", path)
        if not match:
            return super().do_GET()
        user, workspace = self._context()
        try:
            run_id = match.group(1)
            if not self.bundle.repository.get_run(user, workspace, run_id):
                raise LookupError("RUN_NOT_FOUND")
            return self._send_json(200, {
                name: self.bundle.repository.list_records(name, user, workspace, run_id, limit=100000)
                for name in ("acks", "evaluations")
            })
        except Exception as exc:
            self._send_error(exc)


def create_server(host="127.0.0.1", port=8766, *, data_dir=None, gateway_url=None):
    server = ControlHttpServer((host, port), ArdeControlServiceBundle(data_dir=data_dir, gateway_url=gateway_url))
    server.RequestHandlerClass = ArdeRequestHandler
    return server


def main():
    parser = argparse.ArgumentParser(description="ARDE-bound C0-C5 experiment service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--gateway-url", default=os.environ.get("CROWDSIM_WS_URL", "ws://127.0.0.1:8765"))
    parser.add_argument("--data-dir", default=os.environ.get("CROWDSIM_EXPERIMENT_DIR"))
    args = parser.parse_args()
    server = create_server(args.host, args.port, data_dir=args.data_dir, gateway_url=args.gateway_url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
