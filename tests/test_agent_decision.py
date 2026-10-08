import asyncio
import unittest

from crowdsim.decision.agent_decision import AgentDecisionEngine
from crowdsim.domain.crowdsim_models import AgentProfile, AgentState, MotionSnapshot, Observation
from crowdsim.decision.route_provider import RouteCandidate


def context(crowding=0.0, risk=0.0):
    profile = AgentProfile("p", crowding_tolerance=0.4, risk_tolerance=0.4)
    state = AgentState("p", perceived_risk=risk)
    motion = MotionSnapshot("p", 1, 0, 0, 0, 0, 1, "e", "e_0", 0, 0, 0, 2)
    observation = Observation("p", "s1", 1, motion, (), 1, 10, 0.1, crowding)
    return profile, state, observation


class AgentDecisionTests(unittest.TestCase):
    def test_rule_plan_uses_structured_contract(self):
        engine = AgentDecisionEngine()
        profile, state, observation = context(crowding=0.8)
        plan = engine.rule_plan(profile, state, observation)
        self.assertEqual("slow_down", plan.proposed_action)
        self.assertEqual("s1", plan.snapshot_id)
        self.assertIsNotNone(plan.speed_limit)

    def test_rule_reroute_uses_only_candidate(self):
        engine = AgentDecisionEngine()
        profile, state, observation = context(risk=0.9)
        candidate = RouteCandidate("safe", "goal", -1, ("e", "goal"), 12)
        plan = engine.rule_plan(profile, state, observation, [candidate])
        self.assertEqual(candidate.edges, plan.route_edges)

    def test_invalid_llm_target_falls_back_to_rule(self):
        engine = AgentDecisionEngine()
        engine.enabled = True
        engine._request = lambda prompt: {"action": "reroute", "target_id": "hallucinated"}
        profile, state, observation = context()
        candidate = RouteCandidate("safe", "goal", -1, ("e", "goal"), 12)
        plan = asyncio.run(engine.decide(profile, state, observation, [candidate]))
        self.assertEqual("rule_fallback", plan.source)
        self.assertNotEqual("hallucinated", plan.target_id)

    def test_prompt_excludes_background_culture_fields(self):
        engine = AgentDecisionEngine()
        profile, state, observation = context()
        prompt = engine._prompt(profile, state, observation, ())
        self.assertNotIn("nationality", prompt["profile"])
        self.assertNotIn("native_language", prompt["profile"])

    def test_hotspot_initial_route_uses_lowest_expected_time_and_locks_goal_only(self):
        engine = AgentDecisionEngine()
        profile, state, observation = context()
        slower = RouteCandidate("monument", "ring", 5, ("e", "north", "ring"), 50,
                                target_kind="hotspot_route", entry_edge="north",
                                base_cost_seconds=40, congestion_delay_seconds=10,
                                minimum_savings_seconds=20, switch_cooldown_seconds=15)
        faster = RouteCandidate("monument", "ring", 5, ("e", "south", "ring"), 30,
                                target_kind="hotspot_route", entry_edge="south",
                                base_cost_seconds=30, minimum_savings_seconds=20,
                                switch_cooldown_seconds=15)

        plan = engine.rule_plan(profile, state, observation, (slower, faster))

        self.assertEqual("reroute", plan.proposed_action)
        self.assertEqual("monument", plan.target_id)
        self.assertEqual("south", plan.selected_entry_edge)
        self.assertTrue(plan.preserve_future_stages)

    def test_hotspot_profile_can_switch_away_from_congested_entrance(self):
        engine = AgentDecisionEngine()
        profile = AgentProfile("p", familiarity=1, crowding_tolerance=0, patience=0,
                               mobility=1, endurance=1, following_tendency=0)
        state = AgentState("p", hotspot_entry_edge="north", hotspot_last_route_change_time=-100)
        _, _, observation = context()
        north = RouteCandidate("monument", "ring", 5, ("e", "north", "ring"), 100,
                              target_kind="hotspot_route", entry_edge="north",
                              base_cost_seconds=35, congestion_delay_seconds=65,
                              minimum_savings_seconds=20, switch_cooldown_seconds=15)
        south = RouteCandidate("monument", "ring", 5, ("e", "south", "ring"), 40,
                              target_kind="hotspot_route", entry_edge="south",
                              base_cost_seconds=40, minimum_savings_seconds=20,
                              switch_cooldown_seconds=15)

        plan = engine.rule_plan(profile, state, observation, (north, south))

        self.assertEqual("reroute", plan.proposed_action)
        self.assertEqual("south", plan.selected_entry_edge)

    def test_hotspot_patient_crowd_tolerant_profile_keeps_queue(self):
        engine = AgentDecisionEngine()
        profile = AgentProfile("p", familiarity=0, crowding_tolerance=1, patience=1,
                               mobility=0.65, endurance=0, following_tendency=1)
        state = AgentState("p", hotspot_entry_edge="north", hotspot_last_route_change_time=-100)
        _, _, observation = context()
        north = RouteCandidate("monument", "ring", 5, ("e", "north", "ring"), 100,
                              target_kind="hotspot_route", entry_edge="north",
                              base_cost_seconds=35, congestion_delay_seconds=65,
                              minimum_savings_seconds=20, switch_cooldown_seconds=15)
        south = RouteCandidate("monument", "ring", 5, ("e", "south", "ring"), 40,
                              target_kind="hotspot_route", entry_edge="south",
                              base_cost_seconds=40, minimum_savings_seconds=20,
                              switch_cooldown_seconds=15)

        plan = engine.rule_plan(profile, state, observation, (north, south))

        self.assertEqual("continue", plan.proposed_action)
        self.assertIn("queue", plan.reason)

    def test_hotspot_switch_cooldown_prevents_route_oscillation(self):
        engine = AgentDecisionEngine()
        profile = AgentProfile("p", familiarity=1, crowding_tolerance=0, patience=0,
                               mobility=1, endurance=1, following_tendency=0)
        state = AgentState("p", hotspot_entry_edge="north", hotspot_last_route_change_time=0)
        _, _, observation = context()
        north = RouteCandidate("monument", "ring", 5, ("e", "north", "ring"), 100,
                              target_kind="hotspot_route", entry_edge="north",
                              base_cost_seconds=35, congestion_delay_seconds=65,
                              minimum_savings_seconds=20, switch_cooldown_seconds=15)
        south = RouteCandidate("monument", "ring", 5, ("e", "south", "ring"), 40,
                              target_kind="hotspot_route", entry_edge="south",
                              base_cost_seconds=40, minimum_savings_seconds=20,
                              switch_cooldown_seconds=15)

        plan = engine.rule_plan(profile, state, observation, (north, south))

        self.assertEqual("continue", plan.proposed_action)
        self.assertIn("cooldown", plan.reason)

    def test_trusted_guidance_overrides_queue_preference(self):
        engine = AgentDecisionEngine()
        profile = AgentProfile("p", crowding_tolerance=1, patience=1, following_tendency=1)
        state = AgentState(
            "p", current_goal="monument", hotspot_entry_edge="north",
            hotspot_last_route_change_time=-100,
            known_events={"monument": {"command": "disperse", "expires_at": 100}},
        )
        _, _, observation = context()
        north = RouteCandidate("monument", "ring", 5, ("e", "north", "ring"), 100,
                              target_kind="hotspot_route", entry_edge="north",
                              base_cost_seconds=35, congestion_delay_seconds=65,
                              minimum_savings_seconds=200, switch_cooldown_seconds=15)
        south = RouteCandidate("monument", "ring", 5, ("e", "south", "ring"), 40,
                              target_kind="hotspot_route", entry_edge="south",
                              base_cost_seconds=40, minimum_savings_seconds=200,
                              switch_cooldown_seconds=15)

        plan = engine.rule_plan(profile, state, observation, (north, south))

        self.assertEqual("reroute", plan.proposed_action)
        self.assertEqual("south", plan.selected_entry_edge)
        self.assertIn("guidance", plan.reason)


if __name__ == "__main__":
    unittest.main()
