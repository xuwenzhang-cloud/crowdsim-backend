"""Stable state JSON serialization."""

from __future__ import annotations

import json
from typing import Any, Mapping

from .state import ArdeState


def dumps_state(state: ArdeState) -> str:
    return json.dumps(state.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def loads_state(value: str | bytes | Mapping[str, Any]) -> ArdeState:
    raw = json.loads(value) if isinstance(value, (str, bytes)) else dict(value)
    if not isinstance(raw, dict):
        raise ValueError("ARDE state must be an object")
    return ArdeState.from_mapping(raw)
