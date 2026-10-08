"""Message delivery, trust, expiry and deduplication."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Dict, Iterable, Optional

from crowdsim.domain.crowdsim_models import AgentProfile, AgentState, MotionSnapshot


@dataclass(frozen=True)
class InformationMessage:
    message_id: str
    content: str
    source_type: str
    created_at: float
    deliver_at: float
    expires_at: float
    x: Optional[float] = None
    y: Optional[float] = None
    radius: Optional[float] = None
    event_id: Optional[str] = None
    recipient_ids: tuple[str, ...] = ()
    command_type: str = "inform"


@dataclass(frozen=True)
class DeliveryRecord:
    message_id: str
    person_id: str
    delivered_at: float
    trusted: bool
    understood: bool = True


class InformationModel:
    def __init__(self) -> None:
        self.messages: Dict[str, InformationMessage] = {}
        self.delivered: set[tuple[str, str]] = set()
        self.records: list[DeliveryRecord] = []

    def publish(self, message: InformationMessage) -> None:
        if message.message_id in self.messages:
            return
        self.messages[message.message_id] = message

    def deliver(self, now: float, motions: Dict[str, MotionSnapshot], profiles: Dict[str, AgentProfile], states: Dict[str, AgentState]) -> list[DeliveryRecord]:
        new_records = []
        for message in self.messages.values():
            if now < message.deliver_at or now >= message.expires_at:
                continue
            for person_id, motion in motions.items():
                key = (message.message_id, person_id)
                if key in self.delivered or not self._in_scope(message, person_id, motion):
                    continue
                trust = profiles[person_id].information_trust.get(message.source_type, 0.5)
                if message.source_type == "official":
                    trust *= 0.5 + 0.5 * profiles[person_id].authority_compliance
                trusted = trust >= 0.5
                state = states[person_id]
                state.received_messages.append(message.message_id)
                if trusted and message.event_id:
                    state.known_events[message.event_id] = {
                        "source": message.source_type,
                        "content": message.content,
                        "command": message.command_type,
                        "expires_at": message.expires_at,
                    }
                record = DeliveryRecord(message.message_id, person_id, now, trusted)
                self.delivered.add(key)
                self.records.append(record)
                new_records.append(record)
        return new_records

    def expire(self, now: float, states: Dict[str, AgentState]) -> None:
        for state in states.values():
            state.known_events = {event_id: detail for event_id, detail in state.known_events.items() if float(detail.get("expires_at", now + 1)) > now}

    @staticmethod
    def _in_scope(message: InformationMessage, person_id: str, motion: MotionSnapshot) -> bool:
        if message.recipient_ids and person_id in message.recipient_ids:
            return True
        if message.x is None or message.y is None or message.radius is None:
            return not message.recipient_ids
        return math.hypot(motion.x - message.x, motion.y - message.y) <= message.radius
