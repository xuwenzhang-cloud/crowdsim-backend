"""ARDE emergency governance algorithms.

``ArdeController`` is the stateful v1 closed-loop implementation used by MACE.
The legacy optimization and policy helpers remain exported for compatibility
with the existing HTTP adapter and demonstrations.
"""

from .config import ArdeConfig, RewardWeights
from .controller import ArdeController
from .optimizer import build_arde_optimization
from .policy_registry import apply_payload_overrides, event_control_factor, resolve_arde_policy

__all__ = [
    "build_arde_optimization",
    "ArdeController",
    "ArdeConfig",
    "RewardWeights",
    "resolve_arde_policy",
    "apply_payload_overrides",
    "event_control_factor",
]
