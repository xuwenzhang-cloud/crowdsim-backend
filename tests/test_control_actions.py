import pytest

from crowdsim.domain.action_contract import validate_control_action
from crowdsim.domain.runtime_capabilities import control_capabilities
from crowdsim.environment.information_model import InformationModel
from crowdsim.environment.intervention_executor import InterventionExecutor


def action(action_type, target=None, parameters=None):
    return {
        "schemaVersion": "1.0",
        "actionId": "act-1",
        "actionType": action_type,
        "target": target or {},
        "parameters": parameters or {},
    }


def test_capabilities_and_validation_are_consistent():
    capabilities = control_capabilities()["supportedActions"]
    assert "set_inflow_rate" in capabilities
    validated = validate_control_action(action(
        "set_inflow_rate",
        {"entryId": "east"},
        {"rate": 0.5, "durationSeconds": 60},
    ))
    assert validated["parameters"]["rate"] == 0.5


def test_invalid_distribution_is_rejected():
    with pytest.raises(ValueError, match="total 1"):
        validate_control_action(action(
            "set_route_distribution",
            {"originId": "edge"},
            {"distributions": [{"routeId": "a", "routeEdges": ["edge", "out"], "ratio": 0.2}], "durationSeconds": 10},
        ))


def test_route_distribution_requires_executable_route_edges():
    with pytest.raises(ValueError, match="routeEdges"):
        validate_control_action(action(
            "set_route_distribution",
            {"originId": "edge"},
            {"distributions": [{"routeId": "a", "ratio": 1.0}]},
        ))


def test_executor_tracks_and_expires_physical_control():
    executor = InterventionExecutor(InformationModel())
    payload = action("set_edge_risk_weight", {"edgeId": "e"}, {"weight": 2, "durationSeconds": 10})
    record = executor.apply_command("set_edge_risk_weight", payload, 5)
    assert record["physical_change"] is True
    assert len(executor.active_controls()) == 1
    assert executor.expire(14.9) == []
    assert executor.expire(15)[0]["actionId"] == "act-1"


def test_legacy_information_controls_remain_compatible():
    executor = InterventionExecutor(InformationModel())
    assert executor.apply_command("observe_only", {}, 0)["physical_change"] is False


def test_guidance_command_is_validated_and_delivered():
    info = InformationModel()
    executor = InterventionExecutor(info)
    payload = action(
        "publish_guidance", {"regionId": "hotspot"},
        {"message": "leave", "command": "disperse", "durationSeconds": 30},
    )
    executor.apply_command("publish_guidance", payload, 0)
    message = next(iter(info.messages.values()))
    assert message.command_type == "disperse"
