from copy import deepcopy

from arde_emergency.controller import ArdeController
from arde_emergency.metrics import gini, normalized_entropy
from arde_emergency.serialization import dumps_state, loads_state


def observation(step=1, risk=0.8):
    return {
        "schemaVersion": "1.0",
        "observationId": f"run:obs:{step}",
        "runId": "run",
        "snapshotId": f"sim:{step}",
        "tick": step,
        "simTimeSeconds": step * 30.0,
        "global": {"riskIndex": risk, "completionRate": 0.2, "meanSpeed": 0.6},
        "regions": [
            {"regionId": "east", "entryId": "east_gate", "riskIndex": risk, "density": 4.0, "speed": 0.4},
            {"regionId": "west", "edgeId": "edge_w", "riskIndex": risk * 0.7, "density": 2.0, "speed": 0.8},
        ],
        "edges": [],
        "hazards": [],
        "strategyStats": {},
        "trend": {},
        "availability": {},
    }


def evaluation(decision_id, objective=0.2):
    return {
        "decisionId": decision_id,
        "before": {"riskIndex": 0.8, "efficiency": 0.2},
        "after": {"riskIndex": 0.7, "efficiency": 0.3},
        "delta": {"riskReduction": 0.1, "efficiency": 0.1},
        "spillover": {"riskIncrease": 0.0},
        "objectiveScore": objective,
    }


def test_entropy_and_gini_boundaries():
    assert normalized_entropy([1, 1, 1, 1]) == 1.0
    assert normalized_entropy([0, 0]) is None
    assert gini([1, 1, 1]) == 0.0
    assert 0.6 < gini([0, 0, 10]) < 0.7


def test_controller_is_reproducible_and_emits_valid_actions():
    left = ArdeController({"random_seed": 7, "initial_exploration": 0.2})
    right = ArdeController({"random_seed": 7, "initial_exploration": 0.2})
    left.reset({"runId": "run", "seed": 11})
    right.reset({"runId": "run", "seed": 11})
    first = left.decide(observation())
    second = right.decide(observation())
    comparable = lambda item: [(action["actionType"], action["target"], action["parameters"]) for action in item["actions"]]
    assert comparable(first) == comparable(second)
    assert first["controllerId"] == "arde_dynamic"


def test_effect_updates_q_values_and_state_round_trips():
    controller = ArdeController({"random_seed": 3})
    controller.reset({"runId": "run", "seed": 3})
    controller.decide(observation())
    outcome = controller.on_effect({
        "before": {"riskIndex": 0.8, "efficiency": 0.2},
        "after": {"riskIndex": 0.5, "efficiency": 0.4},
        "spillover": {"riskIncrease": 0.0},
        "objectiveScore": 0.5,
    })
    assert outcome["reward"] > 0
    assert controller.state.q_values
    restored = loads_state(dumps_state(controller.state))
    assert restored.q_values == controller.state.q_values


def test_llm_failure_falls_back_without_stopping_decision():
    def broken(_payload):
        raise TimeoutError("late")

    controller = ArdeController({"llm_enabled": True}, llm_provider=broken)
    controller.reset({"runId": "run"})
    decision = controller.decide(deepcopy(observation()))
    assert decision["modelTrace"]["status"] == "fallback"
    assert decision["actions"]


def test_controller_snapshot_restores_rng_continuation():
    first = ArdeController({"random_seed": 99, "initial_exploration": 0.4})
    first.reset({"runId": "run", "seed": 99})
    first.decide(observation(1))
    snapshot = first.snapshot_state()
    expected = first.decide(observation(2))
    restored = ArdeController({"random_seed": 99, "initial_exploration": 0.4})
    restored.restore_state(snapshot)
    actual = restored.decide(observation(2))
    shape = lambda item: [(action["actionType"], action["target"], action["parameters"]) for action in item["actions"]]
    assert shape(actual) == shape(expected)


def test_no_inner_ablation_keeps_q_table_unchanged():
    controller = ArdeController({"ablation_mode": "no_inner", "initial_exploration": 0.0, "min_exploration": 0.0})
    controller.reset({"runId": "run", "seed": 7})
    controller.decide(observation())
    before = deepcopy(controller.state.q_values)
    controller.on_effect({"before": {"riskIndex": 0.8}, "after": {"riskIndex": 0.5}, "delta": {"riskReduction": 0.3}})
    assert controller.state.q_values == before


def test_high_risk_safety_constraint_prevents_observe_only():
    controller = ArdeController({"initial_exploration": 0.0, "min_exploration": 0.0})
    controller.reset({"runId": "run", "seed": 1})
    decision = controller.decide(observation(risk=0.9))
    assert decision["actions"][0]["actionType"] != "observe_only"
    assert decision["algorithmTrace"]["choices"][0]["safetyConstraintActive"] is True


def test_empty_region_never_emits_physical_control():
    controller = ArdeController({"initial_exploration": 0.0, "min_exploration": 0.0})
    controller.reset({"runId": "run", "seed": 1})
    empty = observation()
    empty["regions"][0]["person_count"] = 0
    decision = controller.decide(empty)
    assert decision["actions"][0]["actionType"] == "observe_only"
    assert decision["algorithmTrace"]["choices"][0]["emptyRegionFallback"] is True


def test_effect_is_attributed_by_decision_id_with_overlapping_windows():
    controller = ArdeController({"random_seed": 4})
    controller.reset({"runId": "run", "seed": 4})
    first = controller.decide(observation(1))
    second = controller.decide(observation(2))
    result = controller.on_effect(evaluation(first["decisionId"]))
    assert result["decisionId"] == first["decisionId"]
    assert result["updatedChoiceCount"] == 2
    assert first["decisionId"] not in controller.state.pending_decisions
    assert second["decisionId"] in controller.state.pending_decisions


def test_negative_objective_cannot_become_positive_diversity_reward():
    controller = ArdeController({"random_seed": 5})
    controller.reset({"runId": "run", "seed": 5})
    decision = controller.decide(observation())
    result = controller.on_effect(evaluation(decision["decisionId"], objective=-0.05))
    assert result["reward"] <= -0.05
    assert "objectiveAlignmentPenalty" in result["rewardBreakdown"]
