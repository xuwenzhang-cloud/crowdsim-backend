"""File-backed repository for standalone CrowdSim control experiments."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
import uuid
from typing import Any


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _serialize(value):
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    return value


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class CrowdSimRepository:
    """Persist experiments under ``runs/control_experiments`` without MongoDB."""

    COLLECTIONS = (
        "experiments", "runs", "observations", "decisions", "acks",
        "evaluations", "metrics", "llm", "reports", "leases",
    )
    APPEND_ONLY = {"observations", "decisions", "acks", "evaluations", "metrics", "llm"}

    def __init__(self, data_dir: str | Path | None = None) -> None:
        self._root = Path(data_dir).expanduser().resolve() if data_dir else None
        self._lock = threading.RLock()
        self._memory: dict[str, list[dict[str, Any]]] = {name: [] for name in self.COLLECTIONS}
        if self._root is not None:
            self._root.mkdir(parents=True, exist_ok=True)
            self._load()

    @property
    def persistent(self) -> bool:
        return self._root is not None

    def _path(self, key: str) -> Path:
        if self._root is None:
            raise RuntimeError("repository is running in memory")
        return self._root / (f"{key}.jsonl" if key in self.APPEND_ONLY else f"{key}.json")

    def _load(self) -> None:
        for key in self.COLLECTIONS:
            path = self._path(key)
            if not path.is_file():
                continue
            try:
                if key in self.APPEND_ONLY:
                    values = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
                else:
                    values = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(values, list):
                    self._memory[key] = [item for item in values if isinstance(item, dict)]
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"cannot load CrowdSim experiment data from {path}: {exc}") from exc

    def _flush(self, key: str) -> None:
        if not self.persistent:
            return
        path = self._path(key)
        if key in self.APPEND_ONLY:
            text = "".join(json.dumps(_serialize(item), ensure_ascii=False, separators=(",", ":")) + "\n" for item in self._memory[key])
        else:
            text = json.dumps(_serialize(self._memory[key]), ensure_ascii=False, indent=2)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(text, encoding="utf-8")
        try:
            temporary.replace(path)
        except PermissionError:
            # Some Windows file-system providers reject atomic replacement even
            # though both files are writable. Preserve persistence in that case;
            # normal local disks still use the atomic path above.
            path.write_text(text, encoding="utf-8")
            temporary.unlink(missing_ok=True)

    def _insert(self, key: str, document: dict[str, Any]) -> dict[str, Any]:
        if key not in self._memory:
            raise ValueError(f"unknown record kind: {key}")
        doc = deepcopy(document)
        with self._lock:
            self._memory[key].append(doc)
            if self.persistent and key in self.APPEND_ONLY:
                with self._path(key).open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(json.dumps(_serialize(doc), ensure_ascii=False, separators=(",", ":")) + "\n")
            else:
                self._flush(key)
        return _serialize(deepcopy(doc))

    def _find_one(self, key: str, query: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            doc = next((item for item in self._memory[key] if all(item.get(name) == value for name, value in query.items())), None)
        return _serialize(deepcopy(doc)) if doc else None

    def _update(self, key: str, query: dict[str, Any], values: dict[str, Any]) -> dict[str, Any] | None:
        values = {**deepcopy(values), "updated_at": _now()}
        with self._lock:
            for item in self._memory[key]:
                if all(item.get(name) == value for name, value in query.items()):
                    item.update(values)
                    self._flush(key)
                    break
        return self._find_one(key, query)

    def create_experiment(self, user_id: str, workspace_id: str, config: dict[str, Any]) -> dict[str, Any]:
        # A browser may lose the create response. Retrying the same confirmed
        # draft must recover its experiment, including after a service restart.
        with self._lock:
            request_id = config.get("clientRequestId")
            if request_id:
                existing = self._find_one("experiments", {
                    "user_id": str(user_id), "workspace_id": str(workspace_id), "clientRequestId": request_id,
                })
                if existing:
                    if any(existing.get(key) != value for key, value in config.items()):
                        raise ValueError("CLIENT_REQUEST_CONFLICT")
                    return existing
            now = _now()
            return self._insert("experiments", {
                **deepcopy(config), "experiment_id": f"exp_{uuid.uuid4().hex}", "user_id": str(user_id),
                "workspace_id": str(workspace_id), "status": "draft", "created_at": now, "updated_at": now,
            })

    def get_experiment(self, user_id: str, workspace_id: str, experiment_id: str):
        return self._find_one("experiments", {"user_id": str(user_id), "workspace_id": str(workspace_id), "experiment_id": str(experiment_id)})

    def list_experiments(self, user_id: str, workspace_id: str, limit: int = 50):
        query = {"user_id": str(user_id), "workspace_id": str(workspace_id)}
        with self._lock:
            docs = [deepcopy(item) for item in self._memory["experiments"] if all(item.get(k) == v for k, v in query.items())]
        return [_serialize(doc) for doc in reversed(docs[-max(1, int(limit)):])]

    def update_experiment(self, user_id, workspace_id, experiment_id, values):
        return self._update("experiments", {"user_id": str(user_id), "workspace_id": str(workspace_id), "experiment_id": str(experiment_id)}, values)

    def create_run(self, user_id, workspace_id, experiment_id, method_id, seed, config, *, formal=True):
        now = _now()
        return self._insert("runs", {
            "run_id": f"run_{uuid.uuid4().hex}", "user_id": str(user_id), "workspace_id": str(workspace_id),
            "experiment_id": str(experiment_id), "method_id": str(method_id), "seed": int(seed),
            "status": "draft", "config": deepcopy(config), "controller_state": None,
            "formal": bool(formal), "manualIntervention": False, "created_at": now, "updated_at": now,
            "started_at": None, "completed_at": None, "error": None,
        })

    def get_run(self, user_id, workspace_id, run_id):
        return self._find_one("runs", {"user_id": str(user_id), "workspace_id": str(workspace_id), "run_id": str(run_id)})

    def list_runs(self, user_id, workspace_id, experiment_id=None):
        query = {"user_id": str(user_id), "workspace_id": str(workspace_id)}
        if experiment_id is not None:
            query["experiment_id"] = str(experiment_id)
        with self._lock:
            docs = [deepcopy(item) for item in self._memory["runs"] if all(item.get(k) == v for k, v in query.items())]
        return [_serialize(item) for item in docs]

    def update_run(self, user_id, workspace_id, run_id, values):
        return self._update("runs", {"user_id": str(user_id), "workspace_id": str(workspace_id), "run_id": str(run_id)}, values)

    def validate_observation_reset(self, user_id, workspace_id, experiment_id, expected_run_ids):
        from .during_observation_reset import runtime_paths, validate_reset
        with self._lock:
            experiment = self.get_experiment(user_id, workspace_id, experiment_id)
            runs = self.list_runs(user_id, workspace_id, experiment_id)
            validate_reset(experiment, runs, expected_run_ids)
            ids = {run["run_id"] for run in runs}
            owns = lambda row: row.get("user_id") == str(user_id) and row.get("workspace_id") == str(workspace_id)
            other_runs = [row for row in self._memory["runs"] if not (owns(row) and row["run_id"] in ids)]
            if any((row.get("duringReplay") or {}).get("sourceExperimentId") == experiment_id
                   for row in self._memory["experiments"] if row["experiment_id"] != experiment_id):
                from .contracts import ContractError
                raise ContractError("此实验仍有关联策略重跑，未清除来源结果。")
            runtime_paths(runs, other_runs)
            return experiment, runs

    def reset_observation(self, user_id, workspace_id, experiment_id, expected_run_ids):
        from .during_observation_reset import remove_runtime_files
        with self._lock:
            experiment, runs = self.validate_observation_reset(user_id, workspace_id, experiment_id, expected_run_ids)
            if not runs and experiment["status"] == "draft":
                return self.get_experiment(user_id, workspace_id, experiment_id)
            ids = {run["run_id"] for run in runs}
            owns = lambda row: row.get("user_id") == str(user_id) and row.get("workspace_id") == str(workspace_id)
            remove_runtime_files(runs, [])
            # Keep the run IDs until every result collection is flushed. If a
            # write fails, another attempt can finish deletion without backups.
            for kind in (*self.APPEND_ONLY, "reports", "runs"):
                def deleted(row):
                    return owns(row) and (row.get("experiment_id") == experiment_id if kind == "reports" else row.get("run_id") in ids)
                previous = self._memory[kind]
                self._memory[kind] = [row for row in previous if not deleted(row)]
                try:
                    self._flush(kind)
                except Exception:
                    self._memory[kind] = previous
                    raise
            version = self.get_experiment(user_id, workspace_id, experiment_id).get("executionVersion", 0)
            document = next(row for row in self._memory["experiments"] if owns(row) and row["experiment_id"] == experiment_id)
            previous = deepcopy(document)
            try:
                self.update_experiment(user_id, workspace_id, experiment_id, {"status": "draft", "stop_requested": False, "executionVersion": version + 1})
            except Exception:
                document.clear(); document.update(previous)
                raise
            return self.get_experiment(user_id, workspace_id, experiment_id)

    def record(self, kind: str, user_id: str, workspace_id: str, run_id: str, payload: dict[str, Any]):
        if kind not in self.APPEND_ONLY:
            raise ValueError(f"unknown record kind: {kind}")
        identity = f"{kind[:-1]}_id" if kind.endswith("s") else f"{kind}_id"
        return self._insert(kind, {
            identity: f"{kind[:4]}_{uuid.uuid4().hex}", "user_id": str(user_id),
            "workspace_id": str(workspace_id), "run_id": str(run_id), "payload": deepcopy(payload), "created_at": _now(),
        })

    def list_records(self, kind: str, user_id: str, workspace_id: str, run_id: str, limit: int = 1000):
        if kind not in self.APPEND_ONLY:
            raise ValueError(f"unknown record kind: {kind}")
        query = {"user_id": str(user_id), "workspace_id": str(workspace_id), "run_id": str(run_id)}
        with self._lock:
            docs = [deepcopy(item) for item in self._memory[kind] if all(item.get(k) == v for k, v in query.items())]
        return [_serialize(item) for item in docs[:max(1, int(limit))]]

    def save_report(self, user_id, workspace_id, experiment_id, report):
        query = {"user_id": str(user_id), "workspace_id": str(workspace_id), "experiment_id": str(experiment_id)}
        existing = self._find_one("reports", query)
        if existing:
            return self._update("reports", query, {"report": deepcopy(report)})
        now = _now()
        return self._insert("reports", {**query, "report": deepcopy(report), "created_at": now, "updated_at": now})

    def get_report(self, user_id, workspace_id, experiment_id):
        return self._find_one("reports", {"user_id": str(user_id), "workspace_id": str(workspace_id), "experiment_id": str(experiment_id)})

    def acquire_gateway_lease(self, owner_id: str, *, ttl_seconds: float = 60.0, lease_key: str = "default") -> bool:
        now, expires = _now(), _now() + timedelta(seconds=max(5.0, float(ttl_seconds)))
        with self._lock:
            existing = next((item for item in self._memory["leases"] if item.get("lease_key") == lease_key), None)
            existing_expiry = _parse_time(existing.get("expires_at")) if existing else None
            if existing and existing.get("owner_id") != owner_id and existing_expiry and existing_expiry > now:
                return False
            if existing:
                existing.update({"owner_id": owner_id, "expires_at": expires, "updated_at": now})
            else:
                self._memory["leases"].append({"lease_key": lease_key, "owner_id": owner_id, "expires_at": expires, "created_at": now, "updated_at": now})
            self._flush("leases")
            return True

    def release_gateway_lease(self, owner_id: str, *, lease_key: str = "default") -> None:
        with self._lock:
            self._memory["leases"] = [item for item in self._memory["leases"] if not (item.get("lease_key") == lease_key and item.get("owner_id") == owner_id)]
            self._flush("leases")
