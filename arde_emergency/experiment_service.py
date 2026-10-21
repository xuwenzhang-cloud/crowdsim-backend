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
    def health(self):
        return {**super().health(), "ardeRequirementBinding": "snapshot-v1",
                "ardeConfigurationReview": "review-v1", "ardeFlowVersion": "20261021.2"}

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
