from crowdsim.experiments.action_validator import ActionValidator
from crowdsim.experiments.controller_registry import ControllerRegistry
from crowdsim.experiments.effect_evaluator import EffectEvaluator
from crowdsim.experiments.gateway import CrowdSimGateway
from crowdsim.experiments.metrics import summarize_observations
from crowdsim.experiments.observation_builder import ObservationBuilder
from crowdsim.experiments.orchestrator import ExperimentOrchestrator
from crowdsim.experiments.repository import CrowdSimRepository
from crowdsim.experiments.report_service import ReportService
from crowdsim.experiments.routing_service import RoutingService
from crowdsim.experiments.statistics import paired_comparison
from crowdsim.experiments.contracts import ContractError, validate_experiment_config
from crowdsim.experiments.controllers.llm_controller import LlmController
from crowdsim.experiments.llm_client import ControlLlmClient
from crowdsim.experiments.service import ControlServiceBundle
import json
import threading
import httpx
import pytest


def frame(step=1, risk=0.5):
    return {
        "type": "update",
        "run_id": "sim",
        "runtime_state": "running",
        "snapshot_id": f"sim:{step}",
        "step_index": step,
        "step_seconds": step * 30,
        "metrics": {
            "pedestrian_count": 100,
            "pedestrian_avg_speed": 0.7,
            "congestion": risk,
            "population": {"arrived": step},
            "hotspot_metrics": {"east": {"entryId": "east_edge", "edgeId": "east_edge", "riskIndex": risk}},
            "edge_metrics": {"east_edge": {"density": 3.0}},
        },
    }


def test_observation_builder_and_effect_evaluation():
    builder = ObservationBuilder()
    before = builder.build(frame(1, 0.8), run_id="run")
    after = builder.build(frame(5, 0.4), run_id="run")
    decision = {"runId": "run", "decisionId": "dec"}
    evaluation = EffectEvaluator.evaluate(decision, before, after)
    assert evaluation["delta"]["riskReduction"] > 0


def test_observation_builder_maps_current_crowdsim_hotspot_schema():
    observation = ObservationBuilder().build({
        "type": "update",
        "snapshot_id": "sim:1",
        "step_index": 1,
        "step_seconds": 30,
        "metrics": {
            "pedestrian_count": 2,
            "pedestrian_avg_speed": 0.7,
            "population": {"planned": 10, "arrived": 1},
            "edge_metrics": {
                "edge-a": {"person_count": 2, "density_person_per_m2": 2.0, "avg_speed_mps": 0.7},
            },
            "hotspot_metrics": {
                "hotspot-a": {
                    "person_count": 2,
                    "density_person_per_m2": 2.0,
                    "avg_speed_mps": 0.7,
                    "low_speed_count": 1,
                    "entries": {"edges": ["entry-a"]},
                    "core": {"target_edges": ["edge-a"]},
                },
            },
        },
    }, run_id="run")
    assert observation["global"]["maxDensity"] == 2.0
    assert observation["global"]["completionRate"] == 0.1
    assert observation["regions"][0]["entryId"] == "entry-a"
    assert observation["regions"][0]["edgeId"] == "edge-a"
    assert observation["regions"][0]["density"] == 2.0
    assert observation["edges"][0]["density"] == 2.0


def test_risk_uses_control_adjusted_speed_instead_of_metered_queue_speed():
    payload = frame(1, 0.0)
    payload["metrics"].update({
        "pedestrian_avg_speed": 0.1,
        "pedestrian_risk_speed": 1.4,
        "density_levels": {},
    })
    observation = ObservationBuilder().build(payload, run_id="run")
    assert observation["global"]["riskIndex"] == 0.0


def test_statistical_validation_requires_requested_unique_seed_count():
    with pytest.raises(ContractError, match="at least 10"):
        validate_experiment_config({
            "name": "paired",
            "scenario": {
                "seedSet": [1, 2, 3],
                "durationSeconds": 60,
                "statisticalValidation": {"minimumSeeds": 10},
            },
            "methods": ["C0", "C5"],
        })


def test_emergency_event_requires_coordinates():
    with pytest.raises(ContractError, match="requires finite"):
        validate_experiment_config({
            "name": "emergency",
            "scenario": {
                "seedSet": [1],
                "durationSeconds": 60,
                "emergencyEvent": {"enabled": True, "eventType": "fire"},
            },
            "methods": ["C5"],
        })


def test_experiment_config_rejects_persisted_model_credentials():
    with pytest.raises(ContractError, match="environment variables"):
        validate_experiment_config({
            "name": "secret rejection",
            "scenario": {"seedSet": [1]},
            "methods": ["C2"],
            "llmConfig": {"apiKey": "must-not-be-persisted"},
        })

    with pytest.raises(ContractError, match="environment variables"):
        validate_experiment_config({
            "name": "nested secret rejection",
            "scenario": {"seedSet": [1]},
            "methods": ["C2"],
            "controllerConfig": {"C2": {"accessToken": "must-not-be-persisted"}},
        })


def test_action_validator_uses_capabilities():
    validator = ActionValidator({"supportedActions": {"set_inflow_rate": {"rate": {"min": 0, "max": 1}}}})
    action = validator.validate({"actionType": "set_inflow_rate", "target": {"entryId": "e"}, "parameters": {"rate": 0.5}})
    assert action["parameters"]["rate"] == 0.5


def test_route_distribution_requires_executable_route_edges():
    validator = ActionValidator()
    with pytest.raises(ValueError):
        validator.validate({
            "actionType": "set_route_distribution",
            "target": {"originId": "origin"},
            "parameters": {"distributions": [{"routeId": "r1", "ratio": 1.0}]},
        })


def test_registry_constructs_all_controllers():
    registry = ControllerRegistry()
    assert len(registry.catalog()) == 6
    assert registry.create("C0").controller_id == "no_control"
    assert registry.create("C1").controller_id == "rule_based"
    assert registry.create("C5").controller_id == "arde_dynamic"


def test_repository_scopes_experiments_and_runs_in_memory():
    repository = CrowdSimRepository()
    experiment = repository.create_experiment("u", "w", {"name": "x", "scenario": {}, "methods": [], "schemaVersion": "1.0"})
    run = repository.create_run("u", "w", experiment["experiment_id"], "C0", 1, {})
    assert repository.get_experiment("other", "w", experiment["experiment_id"]) is None
    assert repository.get_run("u", "w", run["run_id"])["method_id"] == "C0"


def test_experiment_start_is_idempotent(monkeypatch):
    class Gateway:
        capabilities = {}
        def add_listener(self, callback):
            self.callback = callback
        def health(self):
            return {"connected": False}

    repository = CrowdSimRepository()
    orchestrator = ExperimentOrchestrator(repository, Gateway(), ControllerRegistry())
    monkeypatch.setattr(orchestrator, "_ensure_worker", lambda: None)
    monkeypatch.setattr("crowdsim.experiments.orchestrator.runtime_provenance", lambda: {})
    experiment = orchestrator.create_experiment("u", "w", {
        "name": "matrix", "scenario": {"seedSet": [1], "durationSeconds": 60}, "methods": ["C0", "C5"],
    })
    first = orchestrator.start_experiment("u", "w", experiment["experiment_id"])
    second = orchestrator.start_experiment("u", "w", experiment["experiment_id"])
    assert [item["run_id"] for item in second] == [item["run_id"] for item in first]
    assert len(repository.list_runs("u", "w", experiment["experiment_id"])) == 2


def test_formal_run_rejects_manual_action():
    class Gateway:
        capabilities = {}
        def add_listener(self, callback):
            pass
        def health(self):
            return {}
    repository = CrowdSimRepository()
    orchestrator = ExperimentOrchestrator(repository, Gateway(), ControllerRegistry())
    orchestrator._active = {
        "user_id": "u", "workspace_id": "w", "experiment": {"mode": "formal"},
        "run": {"run_id": "run"}, "last_observation": frame(),
    }
    with pytest.raises(RuntimeError, match="MANUAL_ACTION_FORBIDDEN"):
        orchestrator.manual_action("u", "w", "run", {"actionType": "observe_only"}, reason="test")


def test_gateway_dispatch_updates_capabilities_without_network():
    gateway = CrowdSimGateway()
    received = []
    gateway.add_listener(received.append)
    gateway.dispatch_for_test({"type": "init", "capabilities": {"supportedActions": {"observe_only": {}}}})
    assert "observe_only" in gateway.capabilities["supportedActions"]
    assert received[-1]["type"] == "init"


def test_gateway_correlates_decoded_websocket_ack():
    gateway = CrowdSimGateway("ws://fake")
    event = threading.Event()
    with gateway._pending_lock:
        gateway._pending["req-test"] = (event, {})
    raw = json.dumps({"type": "command_result", "request_id": "req-test", "status": "applied"})
    coroutine = gateway._handle_raw(raw)
    with pytest.raises(StopIteration):
        coroutine.send(None)
    result = gateway.wait_for_ack("req-test", timeout=0.1)
    assert result["status"] == "applied"
    assert result["request_id"] == "req-test"


@pytest.mark.parametrize("request_key", ["request_id", "requestId"])
def test_gateway_correlates_run_closed_without_consuming_other_requests(request_key):
    gateway = CrowdSimGateway("ws://fake")
    closed, other = threading.Event(), threading.Event()
    gateway._pending.update({"close-1": (closed, {}), "other": (other, {})})
    gateway.latest_frame = {"type": "update", "run_id": "finished-run"}
    received = []
    gateway.add_listener(received.append)
    # A closure without the matching request ID must not unblock this waiter.
    for message in ({"type": "run_closed"}, {"type": "run_closed", request_key: "unrelated"}):
        gateway.dispatch_for_test(message)
        assert gateway.wait_for_ack("close-1", timeout=0) is None
    message = {"type": "run_closed", request_key: "close-1", "run_id": "finished-run", "runtime_state": "CLOSED"}
    coroutine = gateway._handle_raw(json.dumps(message))
    with pytest.raises(StopIteration):
        coroutine.send(None)
    assert closed.is_set()
    assert gateway.wait_for_ack("close-1", timeout=0) == message
    assert "close-1" not in gateway._pending
    assert not other.is_set()
    assert gateway.wait_for_ack("other", timeout=0) is None
    assert received[-1] == message
    assert gateway.latest_frame == {"type": "update", "run_id": "finished-run"}
    assert gateway._thread is None


def test_control_service_saves_requirement_without_claiming_websocket(tmp_path, monkeypatch):
    bundle = ControlServiceBundle(data_dir=str(tmp_path), gateway_url="ws://127.0.0.1:8765")
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(201, json={"type": "requirement_accepted", "requirement_id": "req-accepted"})
    monkeypatch.setattr("crowdsim.experiments.service.httpx.post", post)
    monkeypatch.setattr(bundle.gateway, "start", lambda **_: pytest.fail("must not claim simulation connection"))
    result = bundle.submit_requirement({"schema_version": 1})
    assert result["requirement_id"] == "req-accepted"
    assert calls == [("http://127.0.0.1:8765/api/v1/requirements", {
        "json": {"requirement": {"schema_version": 1}}, "timeout": 20.0, "trust_env": False,
    })]
    assert bundle.gateway.health()["connected"] is False


@pytest.mark.parametrize("url,expected,trust_env", [
    ("ws://localhost:8765", "http://localhost:8765/api/v1/requirements", False),
    ("ws://[::1]:8765", "http://[::1]:8765/api/v1/requirements", False),
    ("wss://simulator.example.test", "https://simulator.example.test/api/v1/requirements", True),
])
def test_requirement_http_endpoint_tracks_configured_simulator(tmp_path, monkeypatch, url, expected, trust_env):
    bundle = ControlServiceBundle(data_dir=str(tmp_path), gateway_url=url)
    def post(target, **kwargs):
        assert target == expected
        assert kwargs["trust_env"] is trust_env
        return httpx.Response(201, json={"type": "requirement_accepted"})
    monkeypatch.setattr("crowdsim.experiments.service.httpx.post", post)
    assert bundle.submit_requirement({})["type"] == "requirement_accepted"


@pytest.mark.parametrize("response,error", [
    (httpx.Response(404), "CROWDSIM_REQUIREMENT_ENDPOINT_UNAVAILABLE"),
    (httpx.Response(500, text="invalid"), "CROWDSIM_REQUIREMENT_INVALID_RESPONSE"),
    (httpx.Response(200, json=[]), "CROWDSIM_REQUIREMENT_INVALID_RESPONSE"),
    (httpx.Response(500, json={"type": "requirement_accepted"}), "CROWDSIM_REQUIREMENT_INVALID_RESPONSE"),
    (httpx.Response(400, json={"type": "error", "code": "invalid_requirement", "message": "bad location"}), "invalid_requirement: bad location"),
])
def test_requirement_http_errors_are_reported_without_websocket_fallback(tmp_path, monkeypatch, response, error):
    bundle = ControlServiceBundle(data_dir=str(tmp_path))
    monkeypatch.setattr("crowdsim.experiments.service.httpx.post", lambda *a, **kw: response)
    with pytest.raises((RuntimeError, ValueError), match=error):
        bundle.submit_requirement({})
    assert bundle.gateway._thread is None


@pytest.mark.parametrize("exception,error", [
    (httpx.ConnectError("offline"), "CROWDSIM_UNAVAILABLE"),
    (httpx.ReadTimeout("slow"), "CROWDSIM_REQUIREMENT_TIMEOUT"),
])
def test_requirement_http_connection_errors_do_not_start_gateway(tmp_path, monkeypatch, exception, error):
    bundle = ControlServiceBundle(data_dir=str(tmp_path))
    def post(*args, **kwargs):
        raise exception
    monkeypatch.setattr("crowdsim.experiments.service.httpx.post", post)
    with pytest.raises((RuntimeError, TimeoutError), match=error):
        bundle.submit_requirement({})
    assert bundle.gateway._thread is None


def test_gateway_submit_from_listener_thread_queues_send_without_blocking():
    class Loop:
        def __init__(self):
            self.created = []

        def create_task(self, coroutine):
            self.created.append(coroutine)

            class Task:
                def add_done_callback(self, callback):
                    self.callback = callback

            return Task()

    gateway = CrowdSimGateway("ws://fake")
    gateway._thread = threading.current_thread()
    gateway._loop = Loop()
    gateway._connected.set()
    gateway._websocket = object()
    request_id = gateway.submit({"action": "apply_action"}, request_id="req-listener")
    assert request_id == "req-listener"
    assert len(gateway._loop.created) == 1
    gateway._loop.created[0].close()


def test_orchestrator_keeps_action_correlation_until_terminal_ack():
    class Repository:
        def __init__(self):
            self.records = []

        def record(self, collection, *args):
            self.records.append((collection, args[-1]))

    class Gateway:
        capabilities = {}

        def __init__(self):
            self.forgotten = []

        def add_listener(self, callback):
            self.callback = callback

        def forget_request(self, request_id):
            self.forgotten.append(request_id)

        def health(self):
            return {}

    class Controller:
        def __init__(self):
            self.acks = []

        def on_ack(self, ack):
            self.acks.append(ack)

    repository, gateway, controller = Repository(), Gateway(), Controller()
    orchestrator = ExperimentOrchestrator(repository, gateway, ControllerRegistry())
    pending = {"applied": 0, "rejected": 0}
    active = {"user_id": "u", "workspace_id": "w", "run": {"run_id": "run"}, "controller": controller}
    orchestrator._request_actions["req-action"] = {
        "active": active,
        "decision": {"decisionId": "decision"},
        "action": {"actionId": "action"},
        "pending": pending,
    }
    orchestrator._handle_ack(active, {"request_id": "req-action", "status": "queued"})
    assert "req-action" in orchestrator._request_actions
    assert repository.records == []
    orchestrator._handle_ack(active, {"request_id": "req-action", "status": "applied"})
    assert "req-action" not in orchestrator._request_actions
    assert gateway.forgotten == ["req-action"]
    assert pending["applied"] == 1
    assert controller.acks[0]["status"] == "applied"


def test_orchestrator_ignores_frames_after_terminal_state():
    class Repository:
        def acquire_gateway_lease(self, *_args, **_kwargs):
            raise AssertionError("a trailing frame must not renew the lease")

    class Gateway:
        capabilities = {}

        def add_listener(self, callback):
            self.callback = callback

        def health(self):
            return {}

    orchestrator = ExperimentOrchestrator(Repository(), Gateway(), ControllerRegistry())
    active = {"terminal": threading.Event()}
    active["terminal"].set()
    orchestrator._handle_frame(active, frame())


def test_orchestrator_ignores_trailing_frame_before_new_run_is_armed():
    class Repository:
        def acquire_gateway_lease(self, *_args, **_kwargs):
            raise AssertionError("a frame received during reset must be ignored")

    class Gateway:
        capabilities = {}

        def add_listener(self, callback):
            self.callback = callback

        def health(self):
            return {}

    orchestrator = ExperimentOrchestrator(Repository(), Gateway(), ControllerRegistry())
    active = {"terminal": threading.Event(), "accept_frames": False}
    orchestrator._handle_frame(active, frame(step=21))


def test_run_controls_cannot_operate_on_a_different_or_queued_run():
    class Gateway:
        capabilities = {}

        def __init__(self):
            self.submitted = []

        def add_listener(self, callback):
            self.callback = callback

        def health(self):
            return {}

        def submit(self, payload):
            self.submitted.append(payload)
            return "request"

    repository = CrowdSimRepository()
    experiment = repository.create_experiment("u", "w", {
        "name": "run ownership", "scenario": {"seedSet": [1]}, "methods": ["C0", "C5"],
    })
    active_run = repository.create_run("u", "w", experiment["experiment_id"], "C0", 1, {})
    queued_run = repository.create_run("u", "w", experiment["experiment_id"], "C5", 1, {})
    repository.update_run("u", "w", active_run["run_id"], {"status": "running"})
    gateway = Gateway()
    orchestrator = ExperimentOrchestrator(repository, gateway, ControllerRegistry())
    orchestrator._active = {
        "user_id": "u", "workspace_id": "w", "run": active_run, "terminal": threading.Event(),
    }

    with pytest.raises(RuntimeError, match="RUN_NOT_ACTIVE"):
        orchestrator.stop_run("u", "w", queued_run["run_id"])

    assert gateway.submitted == []
    assert repository.get_run("u", "w", queued_run["run_id"])["status"] == "draft"


def test_report_discloses_single_seed_llm_fallback_and_missing_pairs():
    repository = CrowdSimRepository()
    experiment = repository.create_experiment("u", "w", {
        "name": "audit", "scenario": {"seedSet": [1]}, "methods": ["C2", "C5"],
    })
    llm_run = repository.create_run("u", "w", experiment["experiment_id"], "C2", 1, {})
    arde_run = repository.create_run("u", "w", experiment["experiment_id"], "C5", 1, {})
    repository.update_run("u", "w", llm_run["run_id"], {"status": "completed"})
    repository.update_run("u", "w", arde_run["run_id"], {"status": "completed"})
    repository.record("llm", "u", "w", llm_run["run_id"], {"status": "fallback"})

    limitations = ReportService(repository).generate("u", "w", experiment["experiment_id"])["limitations"]

    assert any("1 个配对随机种子" in item for item in limitations)
    assert any("确定性降级策略" in item for item in limitations)
    assert any("缺少双方均有效" in item for item in limitations)


def test_report_includes_completed_debug_runs_as_descriptive_results():
    repository = CrowdSimRepository()
    experiment = repository.create_experiment("u", "w", {
        "name": "debug comparison", "mode": "debug",
        "scenario": {"seedSet": [7]}, "methods": ["C0", "C5"],
    })
    for method_id, risk in (("C0", 0.45), ("C5", 0.30)):
        run = repository.create_run(
            "u", "w", experiment["experiment_id"], method_id, 7, {}, formal=False,
        )
        repository.record("observations", "u", "w", run["run_id"], {
            "simTimeSeconds": 0,
            "global": {
                "riskIndex": risk, "activePopulation": 100,
                "evacuatedPopulation": 0, "completionRate": 0,
                "maxDensity": 2.0,
            },
            "regions": [],
        })
        repository.update_run("u", "w", run["run_id"], {"status": "completed"})

    report = ReportService(repository).generate("u", "w", experiment["experiment_id"])

    assert report["methods"]["C0"]["count"] == 1
    assert report["methods"]["C5"]["count"] == 1
    assert report["dataScope"]["debugRunCount"] == 2
    assert report["dataScope"]["descriptiveOnly"] is True
    exposure = next(
        item for item in report["pairedComparisons"]
        if item["baseline"] == "C0" and item["metric"] == "cumulativeRiskExposurePersonSeconds"
    )
    assert exposure["pairCount"] == 1
    assert any("调试运行" in item for item in report["limitations"])


def test_arde_routing_returns_complete_allocation():
    result = RoutingService._arde_routing([
        {"routeId": "a", "edges": ["x", "a"], "length": 10, "risk": 1, "capacity": 8},
        {"routeId": "b", "edges": ["x", "b"], "length": 12, "risk": 0, "capacity": 8},
    ], 10)
    assert abs(sum(item["ratio"] for item in result["allocations"]) - 1) < 1e-9
    assert abs(sum(item["flow"] for item in result["allocations"]) - 10) < 1e-9
    assert all(item["flow"] <= 8 + 1e-9 for item in result["allocations"])


def test_routing_compare_isolates_method_failures():
    class Gateway:
        def submit(self, payload):
            assert payload["algorithm"] == "validate_only"
            return "route-check"
        def wait_for_ack(self, request_id, timeout):
            return {"type": "routing_result", "result": {"valid": True}}
    service = RoutingService(gateway=Gateway())
    result = service.compare({
        "methodIds": ["R6"],
        "demand": 10,
        "routes": [{"routeId": "a", "edges": ["x", "a"], "length": 10, "risk": 1, "capacity": 10}],
    })
    assert result["results"]["R6"]["status"] == "completed"


def test_r3_forwards_top_level_demand_and_parameter_aliases():
    class Gateway:
        def submit(self, payload):
            self.payload = payload
            return "route"
        def wait_for_ack(self, request_id, timeout):
            return {"type": "routing_result", "result": {"algorithm": "min_cost_flow", "allocations": []}}
    gateway = Gateway()
    RoutingService(gateway).evaluate("R3", {
        "demand": 12,
        "parameters": {"riskWeight": 2},
        "routes": [{"routeId": "r", "routeEdges": ["a", "b"], "length": 2, "capacity": 20}],
    })
    assert gateway.payload["parameters"] == {"risk_weight": 2, "demand": 12.0}


def test_paired_comparison_matches_seeds():
    rows = [
        {"methodId": "C0", "seed": 1, "finalRiskIndex": 0.8},
        {"methodId": "C5", "seed": 1, "finalRiskIndex": 0.5},
        {"methodId": "C0", "seed": 2, "finalRiskIndex": 0.7},
        {"methodId": "C5", "seed": 2, "finalRiskIndex": 0.4},
    ]
    result = paired_comparison(rows, "C0", "C5", "finalRiskIndex", lower_is_better=True)
    assert result["pairCount"] == 2
    assert abs(result["meanBenefit"] - 0.3) < 1e-9


def test_run_metrics_integrate_on_simulation_time():
    rows = [
        {"simTimeSeconds": 0, "global": {"riskIndex": 0.8, "activePopulation": 100, "evacuatedPopulation": 0, "completionRate": 0.0, "maxDensity": 3.0}, "regions": []},
        {"simTimeSeconds": 60, "global": {"riskIndex": 0.4, "activePopulation": 50, "evacuatedPopulation": 50, "completionRate": 0.5, "maxDensity": 1.5}, "regions": []},
        {"simTimeSeconds": 120, "global": {"riskIndex": 0.1, "activePopulation": 4, "evacuatedPopulation": 96, "completionRate": 0.96, "maxDensity": 1.0}, "regions": []},
    ]
    summary = summarize_observations(rows)
    assert summary["cumulativeRiskExposurePersonSeconds"] == 6000
    assert summary["highRiskPersonMinutes"] == 100
    assert summary["t95Seconds"] == 120
    assert summary["safeOutflowPerSimMinute"] == 48


def test_file_repository_survives_restart(tmp_path):
    first = CrowdSimRepository(tmp_path)
    experiment = first.create_experiment("u", "w", {
        "name": "persisted", "scenario": {}, "methods": ["C0"], "schemaVersion": "1.0",
    })
    run = first.create_run("u", "w", experiment["experiment_id"], "C0", 7, {})
    first.record("observations", "u", "w", run["run_id"], {"simTimeSeconds": 1, "global": {}})

    reopened = CrowdSimRepository(tmp_path)

    assert reopened.get_experiment("u", "w", experiment["experiment_id"])["name"] == "persisted"
    assert reopened.get_run("u", "w", run["run_id"])["seed"] == 7
    assert reopened.list_records("observations", "u", "w", run["run_id"])[0]["payload"]["simTimeSeconds"] == 1


def test_openai_compatible_control_client_uses_injected_transport():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("Authorization")
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={
            "model": "test-control-model",
            "choices": [{"message": {"content": '{"reason":"ok","actions":[]}'}}],
            "usage": {"input_tokens": 10, "output_tokens": 4},
        })

    client = ControlLlmClient(
        base_url="https://example.invalid/v1",
        api_key="test-secret",
        model="test-control-model",
        transport=httpx.MockTransport(handler),
    )
    text, metadata = client.complete([{"role": "user", "content": "test"}])

    assert json.loads(text)["reason"] == "ok"
    assert metadata["model"] == "test-control-model"
    assert captured["authorization"] == "Bearer test-secret"
    assert captured["payload"]["response_format"] == {"type": "json_object"}


def test_llm_controller_periodic_and_event_defaults_match_catalog():
    response = lambda *_args, **_kwargs: ('{"reason":"ok","actions":[]}', {})
    periodic = LlmController("periodic", {}, llm_call=response)
    event = LlmController("event", {}, llm_call=response)
    low = ObservationBuilder().build(frame(step=1, risk=0.1), run_id="run")
    high = ObservationBuilder().build(frame(step=2, risk=0.35), run_id="run")
    high["global"]["riskIndex"] = 0.35

    assert periodic.should_decide(low)
    periodic.decide(low)
    low["simTimeSeconds"] += 29
    assert not periodic.should_decide(low)
    low["simTimeSeconds"] += 1
    assert periodic.should_decide(low)
    assert event.should_decide(high)


def test_control_service_bundle_exposes_catalog_and_persistent_experiment(tmp_path):
    bundle = ControlServiceBundle(data_dir=str(tmp_path), gateway_url="ws://127.0.0.1:1")
    catalog = bundle.registry.catalog()
    created = bundle.orchestrator.create_experiment("u", "w", {
        "name": "service smoke",
        "scenario": {"seedSet": [1], "durationSeconds": 60},
        "methods": ["C0", "C5"],
    })
    fetched = bundle.repository.get_experiment("u", "w", created["experiment_id"])

    assert [item["methodId"] for item in catalog] == ["C0", "C1", "C2", "C3", "C4", "C5"]
    assert fetched["name"] == "service smoke"
    assert (tmp_path / "experiments.json").is_file()


def test_gateway_loopback_bypasses_system_proxy_and_preserves_older_websockets_support():
    from crowdsim.experiments.gateway import CrowdSimGateway

    def modern_connect(uri, *, proxy=True, **kwargs):
        pass

    def older_connect(uri, **kwargs):
        pass

    for url in ("ws://127.0.0.1:8765", "ws://localhost:8765", "ws://[::1]:8765"):
        gateway = CrowdSimGateway(url)
        assert gateway._connection_options(modern_connect)["proxy"] is None
        assert "proxy" not in gateway._connection_options(older_connect)
    remote = CrowdSimGateway("wss://simulation.example.test")
    assert "proxy" not in remote._connection_options(modern_connect)
