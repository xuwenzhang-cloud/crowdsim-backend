"""Stateful ARDE controller implementing the platform controller protocol."""

from __future__ import annotations

from copy import deepcopy
import random
from typing import Any, Callable, Mapping

from .action_mapper import map_strategy
from .config import ArdeConfig
from .contracts import build_decision, new_id, validate_ack, validate_observation
from .inner_layer import select_action, update_q
from .llm_guidance import request_guidance
from .metrics import efficiency_from_observation, risk_from_observation, strategy_statistics
from .outer_layer import adapt_outer_layer
from .reward import calculate_reward
from .state import ArdeState, OuterParameters
from .strategy import ALL_STRATEGIES, region_state_key


class ArdeController:
    controller_id = "arde_dynamic"
    version = "1.0.0"

    def __init__(
        self,
        config: ArdeConfig | Mapping[str, Any] | None = None,
        *,
        llm_provider: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None,
    ) -> None:
        self.config = config if isinstance(config, ArdeConfig) else ArdeConfig.from_mapping(config)
        if self.config.ablation_mode == "no_llm":
            self.config.llm_enabled = False
        if self.config.ablation_mode == "no_diversity":
            self.config.reward.diversity = 0.0
            self.config.reward.rarity = 0.0
            self.config.reward.polarization = 0.0
        self.llm_provider = llm_provider
        self.state = ArdeState(
            outer=OuterParameters(
                exploration_rate=self.config.initial_exploration,
                diversity_weight=self.config.reward.diversity,
                polarization_penalty=self.config.reward.polarization,
            )
        )
        self._rng = random.Random(self.config.random_seed)

    def reset(self, experiment_context: Mapping[str, Any] | None = None) -> None:
        context = dict(experiment_context or {})
        seed = int(context.get("seed", self.config.random_seed))
        self._rng = random.Random(seed)
        self.state = ArdeState(
            episode_id=str(context.get("runId") or context.get("episodeId") or new_id("episode")),
            outer=OuterParameters(
                exploration_rate=self.config.initial_exploration,
                diversity_weight=self.config.reward.diversity,
                polarization_penalty=self.config.reward.polarization,
            ),
            strategy_counts={action: 0 for action in ALL_STRATEGIES},
        )

    def should_decide(self, observation: Mapping[str, Any], context: Mapping[str, Any] | None = None) -> bool:
        obs = validate_observation(observation)
        if self.state.last_decision_time is None:
            return True
        elapsed = float(obs["simTimeSeconds"]) - self.state.last_decision_time
        if elapsed >= self.config.decision_cooldown_seconds:
            return True
        trigger = str((context or {}).get("trigger") or "")
        return trigger in {"effect_completed", "critical_risk"}

    def decide(self, observation: Mapping[str, Any], control_context: Mapping[str, Any] | None = None) -> dict[str, Any]:
        obs = validate_observation(observation)
        risk = risk_from_observation(obs)
        stats = strategy_statistics(self.state.strategy_counts)
        llm_adjustments: dict[str, float] = {}
        llm_trace: dict[str, Any] | None = None
        if self.config.llm_enabled:
            llm_adjustments, llm_trace = request_guidance(self.llm_provider, {
                "observation": obs,
                "outerParameters": self.state.to_dict()["outer"],
                "recentEffects": self.state.effect_history[-5:],
            })
        if self.config.ablation_mode == "no_outer":
            outer = deepcopy(self.state.to_dict()["outer"])
            outer_trace = {"before": outer, "after": outer, "ablation": "no_outer"}
        else:
            outer_trace = adapt_outer_layer(
                self.state,
                entropy=stats["entropy"],
                gini=stats["gini"],
                risk=risk,
                config=self.config,
                llm_adjustments=llm_adjustments,
            )
        regions = list(obs.get("regions") or [])
        if not regions:
            global_metrics = dict(obs.get("global") or {})
            regions = [{"regionId": "global", **global_metrics}]
        actions = []
        choices = []
        skipped_regions = []
        for region in regions:
            state_key = region_state_key(region, obs)
            region_risk = float(region.get("riskIndex", risk) or risk)
            population_value = next((region.get(key) for key in ("person_count", "population", "activePopulation") if key in region), None)
            empty_region = population_value is not None and int(population_value or 0) <= 0
            if empty_region:
                candidates = ("observe",)
                strategy = "observe"
                skipped_regions.append(str(region.get("regionId") or region.get("id") or "global"))
            else:
                candidates = ALL_STRATEGIES[1:] if region_risk >= self.config.risk_high else ALL_STRATEGIES
                strategy = select_action(
                    self.state.q_values,
                    state_key,
                    candidates,
                    self.state.outer.exploration_rate,
                    self._rng,
                )
            action = map_strategy(
                strategy,
                region,
                risk=max(0.0, min(1.0, region_risk)),
                validity_seconds=self.config.action_validity_seconds,
            )
            actions.append(action)
            region_id = str(region.get("regionId") or region.get("id") or "global")
            self.state.assignments[region_id] = strategy
            self.state.strategy_counts[strategy] = self.state.strategy_counts.get(strategy, 0) + 1
            choices.append({
                "regionId": region_id,
                "stateKey": state_key,
                "strategy": strategy,
                "actionId": action["actionId"],
                "allowedStrategies": list(candidates),
                "safetyConstraintActive": region_risk >= self.config.risk_high,
                "emptyRegionFallback": empty_region,
            })
        self.state.decision_step += 1
        self.state.last_observation = deepcopy(obs)
        self.state.last_decision_time = float(obs["simTimeSeconds"])
        decision = build_decision(
            observation=obs,
            controller_id=self.controller_id,
            controller_version=self.version,
            actions=actions,
            reason=f"ARDE step {self.state.decision_step}: risk={risk:.3f}, efficiency={efficiency_from_observation(obs):.3f}",
            trigger={"type": str((control_context or {}).get("trigger") or "observation_cycle")},
            expected_effect={"riskDirection": "decrease", "efficiencyDirection": "increase"},
            algorithm_trace={
                "episodeId": self.state.episode_id,
                "step": self.state.decision_step,
                "risk": risk,
                "strategyStats": stats,
                "outer": outer_trace,
                "choices": deepcopy(choices),
                "skippedEmptyRegions": skipped_regions,
            },
            model_trace=llm_trace,
            validity_seconds=self.config.action_validity_seconds,
        )
        self.state.pending_choices = deepcopy(choices)
        self.state.pending_decisions[decision["decisionId"]] = deepcopy(choices)
        self.state.last_decision = deepcopy(decision)
        return decision

    def on_ack(self, acknowledgement: Mapping[str, Any]) -> None:
        ack = validate_ack(acknowledgement)
        self.state.ack_history.append(deepcopy(ack))
        self.state.ack_history = self.state.ack_history[-200:]

    def on_effect(self, evaluation: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(evaluation, Mapping):
            raise ValueError("evaluation must be an object")
        decision_id = str(evaluation.get("decisionId") or "")
        choices = self.state.pending_decisions.pop(decision_id, None) if decision_id else None
        if choices is None and len(self.state.pending_decisions) == 1:
            _, choices = self.state.pending_decisions.popitem()
        if choices is None:
            choices = []
        stats = strategy_statistics(self.state.strategy_counts)
        action_cost = len(choices) / max(1.0, self.state.outer.action_budget * 10.0)
        rarity = 0.0
        if choices:
            counts = self.state.strategy_counts
            rarity = sum(1.0 / max(1, counts.get(item["strategy"], 1)) for item in choices) / len(choices)
        reward, breakdown = calculate_reward(
            evaluation,
            entropy=stats["entropy"],
            gini=stats["gini"],
            strategy_rarity=rarity,
            action_cost=action_cost,
            config=self.config,
            diversity_weight=self.state.outer.diversity_weight,
            polarization_penalty=self.state.outer.polarization_penalty,
        )
        next_observation = evaluation.get("afterObservation")
        for choice in choices:
            if self.config.ablation_mode == "no_inner":
                continue
            next_key = choice["stateKey"]
            if isinstance(next_observation, Mapping):
                region = next((item for item in next_observation.get("regions", []) if str(item.get("regionId") or item.get("id")) == choice["regionId"]), None)
                if region is not None:
                    next_key = region_state_key(region, next_observation)
            update_q(
                self.state.q_values,
                state_key=choice["stateKey"],
                action=choice["strategy"],
                reward=reward,
                next_state_key=next_key,
                actions=tuple(choice.get("allowedStrategies") or ALL_STRATEGIES),
                learning_rate=self.config.learning_rate,
                discount_factor=self.config.discount_factor,
            )
        record = {"evaluation": deepcopy(dict(evaluation)), "reward": reward, "rewardBreakdown": breakdown, "decisionId": decision_id}
        self.state.effect_history.append(record)
        self.state.effect_history = self.state.effect_history[-100:]
        self.state.pending_choices = deepcopy(next(reversed(self.state.pending_decisions.values()), [])) if self.state.pending_decisions else []
        return {"reward": reward, "rewardBreakdown": breakdown, "decisionId": decision_id, "updatedChoiceCount": len(choices)}

    def snapshot_state(self) -> dict[str, Any]:
        state = self.state.to_dict()
        state["rngState"] = self._rng.getstate()
        state["config"] = self.config.to_dict()
        return state

    def restore_state(self, payload: Mapping[str, Any]) -> None:
        raw = dict(payload)
        rng_state = raw.pop("rngState", None)
        raw.pop("config", None)
        self.state = ArdeState.from_mapping(raw)
        if self.state.pending_choices and not self.state.pending_decisions and self.state.last_decision:
            decision_id = str(self.state.last_decision.get("decisionId") or "")
            if decision_id:
                self.state.pending_decisions[decision_id] = deepcopy(self.state.pending_choices)
        self._rng = random.Random(self.config.random_seed)
        if rng_state is not None:
            def as_tuple(value):
                return tuple(as_tuple(item) for item in value) if isinstance(value, (list, tuple)) else value
            self._rng.setstate(as_tuple(rng_state))
