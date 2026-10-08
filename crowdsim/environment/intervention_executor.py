"""Explicit information interventions; unsupported physics is rejected."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict

from crowdsim.control.intervention_lifecycle import InterventionLifecycle
from crowdsim.domain.action_contract import ACTION_TYPES, validate_control_action
from crowdsim.environment.information_model import InformationMessage, InformationModel


class InterventionExecutor:
    LEGACY_SUPPORTED = {"police_guidance", "temporary_diversion", "observe_only"}
    SUPPORTED = LEGACY_SUPPORTED | ACTION_TYPES

    def __init__(self, information: InformationModel) -> None:
        self.information = information
        self.active_policy = ""
        self.records = []
        self.lifecycle = InterventionLifecycle()

    def apply_command(self, name: str, data: Dict[str, Any], now: float) -> dict:
        if name not in self.SUPPORTED:
            raise ValueError(f"unsupported intervention: {name}")
        if name in ACTION_TYPES and "actionType" in data:
            return self._apply_control_action(validate_control_action(data), now)
        record = {"name": name, "applied_at": now, "physical_change": False}
        if name != "observe_only":
            message_id = str(data.get("id") or f"policy-{name}-{now:g}")
            self.information.publish(InformationMessage(message_id=message_id, content=str(data.get("message") or name), source_type="official", created_at=now, deliver_at=now, expires_at=now + max(1.0, float(data.get("duration", 60))), x=data.get("x"), y=data.get("y"), radius=data.get("radius"), event_id=data.get("event_id")))
            record["message_id"] = message_id
        self.active_policy = name
        self.records.append(record)
        return record

    def _apply_control_action(self, action: dict[str, Any], now: float) -> dict:
        action_type = action["actionType"]
        parameters = action.get("parameters") or {}
        detail: dict[str, Any] = {"validated": True}
        if action_type == "publish_guidance":
            target = action.get("target") or {}
            message_id = f"control-{action['actionId']}"
            self.information.publish(InformationMessage(
                message_id=message_id,
                content=str(parameters["message"]),
                source_type="official",
                created_at=now,
                deliver_at=now,
                expires_at=now + float(parameters["durationSeconds"]),
                x=target.get("x"),
                y=target.get("y"),
                radius=target.get("radius"),
                event_id=target.get("regionId"),
                command_type=str(parameters.get("command") or "inform"),
            ))
            detail["messageId"] = message_id
        record = self.lifecycle.activate(action, now, detail)
        record.update({
            "name": action_type,
            "applied_at": now,
            "physical_change": action_type not in {"observe_only", "publish_guidance"},
            "action": deepcopy(action),
        })
        self.active_policy = action_type
        self.records.append(record)
        return record

    def expire(self, now: float) -> list[dict[str, Any]]:
        return self.lifecycle.expire(now)

    def active_controls(self) -> list[dict[str, Any]]:
        return self.lifecycle.serialize()
