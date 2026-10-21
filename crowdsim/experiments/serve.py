"""HTTP/SSE service for standalone C0-C5 comparison experiments."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .service import ControlServiceBundle
from crowdsim.domain.during_preset import PRESET_FILES, load_during_preset, preset_fingerprint


CONTROL_PREFIX = "/crowdSim/control"


class ControlRequestHandler(BaseHTTPRequestHandler):
    server_version = "CrowdSimControl/1.0"
    protocol_version = "HTTP/1.1"

    @property
    def bundle(self) -> ControlServiceBundle:
        return self.server.bundle  # type: ignore[attr-defined]

    def log_message(self, format: str, *args) -> None:
        if os.environ.get("CROWDSIM_CONTROL_QUIET") != "1":
            super().log_message(format, *args)

    def _context(self) -> tuple[str, str]:
        user_id = str(self.headers.get("X-CrowdSim-User") or "local-user").strip()
        workspace_id = str(self.headers.get("X-CrowdSim-Workspace") or "local-workspace").strip()
        return user_id or "local-user", workspace_id or "local-workspace"

    def _path(self) -> tuple[str, dict[str, list[str]]]:
        parsed = urlsplit(self.path)
        path = parsed.path
        if path.startswith("/api/"):
            path = path[4:]
        return path.rstrip("/") or "/", parse_qs(parsed.query)

    def _json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(min(length, 8 * 1024 * 1024))
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("request body must be a JSON object")
        return data

    def _cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        allowed = os.environ.get("CROWDSIM_CONTROL_ALLOW_ORIGIN")
        if allowed:
            self.send_header("Access-Control-Allow-Origin", allowed)
        elif origin and re.match(r"^https?://(?:localhost|127\.0\.0\.1)(?::\d+)?$", origin):
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-CrowdSim-User, X-CrowdSim-Workspace")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _send_json(self, status: int, data: Any, *, success: bool = True, error_code: str | None = None) -> None:
        payload = {"success": success, "data": data} if success else {
            "success": False,
            "errorCode": error_code or "INVALID_REQUEST",
            "message": str(data),
        }
        encoded = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_error(self, exc: Exception) -> None:
        message = str(exc)
        status = HTTPStatus.BAD_REQUEST
        if message in {"EXPERIMENT_NOT_FOUND", "RUN_NOT_FOUND"}:
            status = HTTPStatus.NOT_FOUND
        elif message in {"EXPERIMENT_ALREADY_RUNNING", "SIMULATOR_BUSY", "RUN_NOT_ACTIVE"}:
            status = HTTPStatus.CONFLICT
        elif message == "MANUAL_ACTION_FORBIDDEN":
            status = HTTPStatus.FORBIDDEN
        elif message in {"CROWDSIM_UNAVAILABLE", "OBSERVATION_NOT_READY"}:
            status = HTTPStatus.SERVICE_UNAVAILABLE
        self._send_json(int(status), message, success=False, error_code=message if message.isupper() else type(exc).__name__)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        path, query = self._path()
        user_id, workspace_id = self._context()
        try:
            if path == f"{CONTROL_PREFIX}/health":
                data = self.bundle.orchestrator.health()
                data.update({"storage": "jsonl", "controllerRegistry": "ready", "arde": "ready"})
                return self._send_json(200, data)
            if path == f"{CONTROL_PREFIX}/controllers":
                return self._send_json(200, self.bundle.registry.catalog())
            if path == f"{CONTROL_PREFIX}/capabilities":
                return self._send_json(200, self.bundle.gateway.health().get("capabilities") or {})
            if path == f"{CONTROL_PREFIX}/during-presets":
                presets = [load_during_preset(preset_id) for preset_id in PRESET_FILES]
                return self._send_json(200, [{**preset, "fingerprint": preset_fingerprint(preset)} for preset in presets])
            if path == f"{CONTROL_PREFIX}/experiments":
                limit = max(1, min(200, int((query.get("limit") or [50])[0])))
                return self._send_json(200, self.bundle.repository.list_experiments(user_id, workspace_id, limit))

            match = re.fullmatch(rf"{CONTROL_PREFIX}/experiments/([^/]+)/strategy-context", path)
            if match:
                return self._send_json(200, self.bundle.during_strategies.context(user_id, workspace_id, match.group(1)))
            match = re.fullmatch(rf"{CONTROL_PREFIX}/experiments/([^/]+)/report", path)
            if match:
                return self._send_json(200, self.bundle.reports.generate(user_id, workspace_id, match.group(1)))
            match = re.fullmatch(rf"{CONTROL_PREFIX}/experiments/([^/]+)/observation-(interpretation|conclusion)", path)
            if match:
                from .during_observation_reports import interpretation
                return self._send_json(200, interpretation(self.bundle.repository, user_id, workspace_id, match.group(1), kind=match.group(2)))
            match = re.fullmatch(rf"{CONTROL_PREFIX}/experiments/([^/]+)/runtime", path)
            if match:
                if query.get("summary") == ["1"]:
                    return self._send_json(200, self.bundle.orchestrator.runtime_view(user_id, workspace_id, match.group(1), summary_only=True))
                return self._send_json(200, self.bundle.orchestrator.runtime_view(user_id, workspace_id, match.group(1)))
            match = re.fullmatch(rf"{CONTROL_PREFIX}/experiments/([^/]+)", path)
            if match:
                item = self.bundle.repository.get_experiment(user_id, workspace_id, match.group(1))
                if not item:
                    raise LookupError("EXPERIMENT_NOT_FOUND")
                item["runs"] = self.bundle.repository.list_runs(user_id, workspace_id, match.group(1))
                return self._send_json(200, item)

            match = re.fullmatch(rf"{CONTROL_PREFIX}/runs/([^/]+)/(metrics|decisions)", path)
            if match:
                if not self.bundle.repository.get_run(user_id, workspace_id, match.group(1)):
                    raise LookupError("RUN_NOT_FOUND")
                return self._send_json(200, self.bundle.repository.list_records(match.group(2), user_id, workspace_id, match.group(1), limit=100000))
            match = re.fullmatch(rf"{CONTROL_PREFIX}/runs/([^/]+)/events", path)
            if match:
                return self._stream_events(user_id, workspace_id, match.group(1), int((query.get("after") or [self.headers.get("Last-Event-ID") or 0])[0]))
            match = re.fullmatch(rf"{CONTROL_PREFIX}/runs/([^/]+)", path)
            if match:
                item = self.bundle.repository.get_run(user_id, workspace_id, match.group(1))
                if not item:
                    raise LookupError("RUN_NOT_FOUND")
                return self._send_json(200, item)
            self._send_json(404, "NOT_FOUND", success=False, error_code="NOT_FOUND")
        except Exception as exc:
            self._send_error(exc)

    def do_POST(self) -> None:
        path, _ = self._path()
        user_id, workspace_id = self._context()
        try:
            data = self._json_body()
            if path == f"{CONTROL_PREFIX}/requirements":
                requirement = data.get("requirement") if "requirement" in data else data
                if not isinstance(requirement, dict):
                    raise ValueError("requirement must be a JSON object")
                return self._send_json(201, self.bundle.submit_requirement(requirement))
            if path == f"{CONTROL_PREFIX}/experiments":
                return self._send_json(201, self.bundle.orchestrator.create_experiment(user_id, workspace_id, data))
            if path == f"{CONTROL_PREFIX}/during-observations":
                return self._send_json(201, self.bundle.orchestrator.create_observation_experiment(user_id, workspace_id, data))
            if path == f"{CONTROL_PREFIX}/during-observations/model":
                from .during_observation import prepare_observation_model
                return self._send_json(201, prepare_observation_model(data, workspace_id))
            match = re.fullmatch(rf"{CONTROL_PREFIX}/experiments/([^/]+)/observation-(interpretation|conclusion)", path)
            if match:
                from .during_observation_reports import write_interpretation
                return self._send_json(200, write_interpretation(self.bundle.repository, self.bundle.reports, user_id, workspace_id, match.group(1), {**data, "kind": match.group(2)}))
            match = re.fullmatch(rf"{CONTROL_PREFIX}/experiments/([^/]+)/(strategy-preview|strategy-replays)", path)
            if match:
                method = self.bundle.during_strategies.preview if match.group(2) == "strategy-preview" else self.bundle.during_strategies.create_replay
                return self._send_json(200 if match.group(2) == "strategy-preview" else 201, method(user_id, workspace_id, match.group(1), data))
            match = re.fullmatch(rf"{CONTROL_PREFIX}/experiments/([^/]+)/start", path)
            if match:
                runs = self.bundle.orchestrator.start_experiment(user_id, workspace_id, match.group(1), execution_version=data.get("executionVersion"))
                return self._send_json(202, {"runs": runs})
            match = re.fullmatch(rf"{CONTROL_PREFIX}/experiments/([^/]+)/stop", path)
            if match:
                return self._send_json(200, self.bundle.orchestrator.stop_experiment(user_id, workspace_id, match.group(1)))
            match = re.fullmatch(rf"{CONTROL_PREFIX}/experiments/([^/]+)/reset-observation", path)
            if match:
                return self._send_json(200, self.bundle.orchestrator.reset_observation_experiment(user_id, workspace_id, match.group(1), data))
            match = re.fullmatch(rf"{CONTROL_PREFIX}/runs/([^/]+)/(pause|resume|stop)", path)
            if match:
                item = getattr(self.bundle.orchestrator, f"{match.group(2)}_run")(user_id, workspace_id, match.group(1))
                return self._send_json(200, item)
            if path == f"{CONTROL_PREFIX}/manual-actions":
                queued = self.bundle.orchestrator.manual_action(
                    user_id, workspace_id, str(data.get("runId") or ""), data.get("action") or {},
                    reason=str(data.get("reason") or ""),
                )
                return self._send_json(202, queued)
            if path == f"{CONTROL_PREFIX}/routing/evaluate":
                result = self.bundle.routing.evaluate(str(data.get("methodId") or ""), data, user_id=user_id)
                return self._send_json(200, result)
            if path == f"{CONTROL_PREFIX}/routing/compare":
                return self._send_json(200, self.bundle.routing.compare(data, user_id=user_id))
            self._send_json(404, "NOT_FOUND", success=False, error_code="NOT_FOUND")
        except Exception as exc:
            self._send_error(exc)

    def _stream_events(self, user_id: str, workspace_id: str, run_id: str, sequence: int) -> None:
        if not self.bundle.repository.get_run(user_id, workspace_id, run_id):
            raise LookupError("RUN_NOT_FOUND")
        self.send_response(200)
        self._cors_headers()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        while True:
            events = self.bundle.events.wait_after(sequence, run_id=run_id, timeout=15)
            try:
                if not events:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                for event in events:
                    sequence = max(sequence, int(event["sequence"]))
                    payload = json.dumps(event, ensure_ascii=False, default=str)
                    block = f"id: {event['sequence']}\nevent: {event['type']}\ndata: {payload}\n\n".encode("utf-8")
                    self.wfile.write(block)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return


class ControlHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, bundle: ControlServiceBundle):
        super().__init__(address, ControlRequestHandler)
        self.bundle = bundle

    def server_close(self) -> None:
        self.bundle.gateway.close()
        super().server_close()


def create_server(host: str = "127.0.0.1", port: int = 8766, *, data_dir: str | None = None, gateway_url: str | None = None):
    return ControlHttpServer((host, int(port)), ControlServiceBundle(data_dir=data_dir, gateway_url=gateway_url))


def main() -> None:
    parser = argparse.ArgumentParser(description="CrowdSim C0-C5 control experiment service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--gateway-url", default=os.environ.get("CROWDSIM_WS_URL", "ws://127.0.0.1:8765"))
    parser.add_argument("--data-dir", default=os.environ.get("CROWDSIM_EXPERIMENT_DIR"))
    args = parser.parse_args()
    server = create_server(args.host, args.port, data_dir=args.data_dir, gateway_url=args.gateway_url)
    print(f"CrowdSim control service listening on http://{args.host}:{args.port}{CONTROL_PREFIX}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
