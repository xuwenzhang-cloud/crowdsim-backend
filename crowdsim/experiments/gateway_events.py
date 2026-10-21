"""In-process event stream with replay sequence for REST/SSE consumers."""

from __future__ import annotations

from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
import threading
from typing import Any


class EventBus:
    def __init__(self, max_events: int = 5000) -> None:
        self._events: deque[dict[str, Any]] = deque(maxlen=max_events)
        self._sequence = 0
        self._condition = threading.Condition()

    def publish(self, event_type: str, payload: dict[str, Any] | None = None, *, run_id: str | None = None) -> dict[str, Any]:
        with self._condition:
            self._sequence += 1
            event = {
                "sequence": self._sequence,
                "type": str(event_type),
                "runId": run_id,
                "payload": deepcopy(payload or {}),
                "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
            self._events.append(event)
            self._condition.notify_all()
            return deepcopy(event)

    def after(self, sequence: int, *, run_id: str | None = None) -> list[dict[str, Any]]:
        with self._condition:
            return [deepcopy(event) for event in self._events if event["sequence"] > sequence and (run_id is None or event["runId"] == run_id)]

    def discard_runs(self, run_ids) -> None:
        with self._condition:
            self._events = deque((event for event in self._events if event["runId"] not in run_ids), maxlen=self._events.maxlen)

    def wait_after(self, sequence: int, *, run_id: str | None = None, timeout: float = 15.0) -> list[dict[str, Any]]:
        with self._condition:
            matches = [event for event in self._events if event["sequence"] > sequence and (run_id is None or event["runId"] == run_id)]
            if not matches:
                self._condition.wait(timeout)
            return [deepcopy(event) for event in self._events if event["sequence"] > sequence and (run_id is None or event["runId"] == run_id)]
