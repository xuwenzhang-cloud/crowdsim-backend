"""Versioned, conversation-scoped during requirements, separate from chat sync."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import threading

from .repository import CONVERSATION_ID


class RequirementConflict(Exception):
    def __init__(self, record):
        super().__init__("事中需求已有更新，本次旧版本未保存")
        self.record = record


def validate_state(state, conversation_id):
    if not isinstance(state, dict) or set(state) - {"requirement", "observation", "legacy"}:
        raise ValueError("invalid during requirement state")
    requirement = state.get("requirement")
    draft = requirement.get("draft") if isinstance(requirement, dict) else None
    if (not isinstance(draft, dict) or requirement.get("version") != 3
            or not isinstance(draft.get("requirement"), dict) or draft["requirement"].get("schema_version") != 2
            or not isinstance(draft.get("id"), str) or not draft["id"]
            or type(draft.get("revision")) is not int or draft["revision"] < 0
            or not isinstance(requirement.get("snapshots"), list)
            or not isinstance(requirement.get("submissions"), dict)):
        raise ValueError("invalid v3 requirement draft")
    ids = set()
    for snapshot in requirement["snapshots"]:
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("id"), str) or snapshot["id"] in ids:
            raise ValueError("invalid requirement snapshot")
        ids.add(snapshot["id"])
    if set(requirement["submissions"]) - ids:
        raise ValueError("requirement submission has no snapshot")
    for receipt in requirement["submissions"].values():
        if not isinstance(receipt, dict) or not receipt.get("requirement_id") or not receipt.get("fingerprint"):
            raise ValueError("invalid requirement receipt")
    if state.get("legacy") is not None and not isinstance(state["legacy"], dict):
        raise ValueError("invalid legacy experiment binding")
    observation = state.get("observation")
    if observation is not None:
        if not isinstance(observation, dict) or not isinstance(observation.get("entries"), list):
            raise ValueError("invalid observation bindings")
        for entry in observation["entries"]:
            if not isinstance(entry, dict) or entry.get("conversationId") != conversation_id or not entry.get("clientRequestId"):
                raise ValueError("observation belongs to another conversation")


def _preserve_bindings(previous, state):
    before, after = previous["requirement"], state["requirement"]
    snapshots = {item["id"]: item for item in after["snapshots"]}
    if any(snapshots.get(item["id"]) != item for item in before["snapshots"]):
        raise ValueError("saved requirement snapshots cannot be removed or changed")
    if any(after["submissions"].get(key) != value for key, value in before["submissions"].items()):
        raise ValueError("saved requirement receipts cannot be removed or changed")
    entries = {item["clientRequestId"]: item for item in (state.get("observation") or {}).get("entries", [])}
    for item in (previous.get("observation") or {}).get("entries", []):
        updated = entries.get(item["clientRequestId"], {})
        # experimentId may be filled after recovering an idempotent submission.
        if any(updated.get(key) != value for key, value in item.items() if key != "experimentId") or (
                item.get("experimentId") and updated.get("experimentId") != item["experimentId"]):
            raise ValueError("saved experiment bindings cannot be removed or changed")


class DuringRequirementRepository:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._lock = threading.RLock()

    def _path(self, conversation_id):
        if not isinstance(conversation_id, str) or not CONVERSATION_ID.fullmatch(conversation_id):
            raise ValueError("invalid conversation id")
        return self.root / f"{conversation_id}.json"

    def load(self, conversation_id):
        with self._lock:
            path = self._path(conversation_id)
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def save(self, conversation_id, *, state, expected_revision, mutation_id):
        path = self._path(conversation_id)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expectedRevision must be a nonnegative integer")
        if not isinstance(mutation_id, str) or not CONVERSATION_ID.fullmatch(mutation_id):
            raise ValueError("invalid mutationId")
        validate_state(state, conversation_id)
        with self._lock:
            previous = self.load(conversation_id)
            if previous and previous.get("mutationId") == mutation_id:
                if previous["state"] != state:
                    raise ValueError("mutationId reused with different data")
                return previous
            revision = previous["revision"] if previous else 0
            if revision != expected_revision:
                raise RequirementConflict(previous)
            if previous:
                _preserve_bindings(previous["state"], state)
            record = {"schemaVersion": 1, "conversationId": conversation_id, "revision": revision + 1,
                      "mutationId": mutation_id, "updatedAt": datetime.now(timezone.utc).isoformat(),
                      "state": deepcopy(state)}
            self.root.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.root, delete=False) as handle:
                    temporary = Path(handle.name)
                    json.dump(record, handle, ensure_ascii=False, allow_nan=False, indent=2)
                    handle.write("\n")
                temporary.replace(path)
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
            return record
