"""Lifecycle store for active, expiring control actions."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


class InterventionLifecycle:
    def __init__(self) -> None:
        self.active: dict[str, dict[str, Any]] = {}

    def activate(self, action: dict[str, Any], now: float, detail: dict[str, Any]) -> dict[str, Any]:
        duration = float((action.get("parameters") or {}).get("durationSeconds", 0) or 0)
        record = {
            "actionId": action["actionId"],
            "actionType": action["actionType"],
            "target": deepcopy(action.get("target") or {}),
            "parameters": deepcopy(action.get("parameters") or {}),
            "appliedAt": float(now),
            "expiresAt": float(now) + duration if duration > 0 else None,
            "detail": deepcopy(detail),
        }
        if record["expiresAt"] is not None:
            self.active[action["actionId"]] = record
        return record

    def expire(self, now: float) -> list[dict[str, Any]]:
        expired = []
        for action_id, record in list(self.active.items()):
            expires_at = record.get("expiresAt")
            if expires_at is not None and float(expires_at) <= float(now):
                expired.append(self.active.pop(action_id))
        return expired

    def serialize(self) -> list[dict[str, Any]]:
        return [deepcopy(value) for value in self.active.values()]
