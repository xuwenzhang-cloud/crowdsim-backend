"""Deterministic-seed epsilon-greedy Q-learning helpers."""

from __future__ import annotations

import random
from typing import Iterable


def ensure_actions(q_values: dict[str, dict[str, float]], state_key: str, actions: Iterable[str]) -> dict[str, float]:
    row = q_values.setdefault(state_key, {})
    for action in actions:
        row.setdefault(action, 0.0)
    return row


def select_action(
    q_values: dict[str, dict[str, float]],
    state_key: str,
    actions: tuple[str, ...],
    exploration_rate: float,
    rng: random.Random,
) -> str:
    row = ensure_actions(q_values, state_key, actions)
    if rng.random() < exploration_rate:
        return actions[rng.randrange(len(actions))]
    best = max(row.values())
    return next(action for action in actions if row[action] == best)


def update_q(
    q_values: dict[str, dict[str, float]],
    *,
    state_key: str,
    action: str,
    reward: float,
    next_state_key: str,
    actions: tuple[str, ...],
    learning_rate: float,
    discount_factor: float,
) -> float:
    row = ensure_actions(q_values, state_key, actions)
    next_row = ensure_actions(q_values, next_state_key, actions)
    old = row[action]
    updated = old + learning_rate * (reward + discount_factor * max(next_row.values()) - old)
    row[action] = updated
    return updated
