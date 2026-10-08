"""HTTP sidecar for ARDE emergency optimization.

CrowdSim overlay WebSocket only accepts one client. Optimization therefore
runs on a separate HTTP port, matching the evaluation-engine split:

    python -m arde_emergency.serve --port 8767
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from .optimizer import build_arde_optimization  # noqa: E402
from .policy_registry import list_arde_decisions  # noqa: E402
from .demo_effectiveness import run_demo  # noqa: E402

EXAMPLES = Path(__file__).resolve().parent / "examples"
EFFECTIVENESS_PATH = EXAMPLES / "demo_effectiveness_result.json"
SAMPLE_REQUEST_PATH = EXAMPLES / "sample_optimize_request.json"
SAMPLE_CATALOG_PATH = EXAMPLES / "sample_requests_catalog.json"

# In-memory effectiveness runs keyed by (population, warmup, delay, horizon)
_EFFECTIVENESS_CACHE: Dict[str, Dict[str, Any]] = {}
_DEFAULT_DEMO = {
    "warmup": 8,
    "delay": 6,
    "horizon": 48,
    "population": 900,
}


def _cache_key(population: int, warmup: int, delay: int, horizon: int) -> str:
    return f"p{population}_w{warmup}_d{delay}_h{horizon}"


def _clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _run_effectiveness(
    *,
    population: int,
    warmup: int,
    delay: int,
    horizon: int,
    use_cache: bool = True,
) -> Dict[str, Any]:
    key = _cache_key(population, warmup, delay, horizon)
    if use_cache and key in _EFFECTIVENESS_CACHE:
        cached = dict(_EFFECTIVENESS_CACHE[key])
        cached["source"] = "memory_cache"
        return cached

    # Fast path: default demo already baked into examples/ (only when cache allowed)
    if (
        use_cache
        and population == _DEFAULT_DEMO["population"]
        and warmup == _DEFAULT_DEMO["warmup"]
        and delay == _DEFAULT_DEMO["delay"]
        and horizon == _DEFAULT_DEMO["horizon"]
    ):
        loaded = _read_json_file(EFFECTIVENESS_PATH)
        if loaded.get("ok") and isinstance(loaded.get("data"), dict):
            payload = dict(loaded["data"])
            payload["ok"] = True
            payload["source"] = "cached_demo"
            payload["path"] = str(EFFECTIVENESS_PATH.relative_to(ROOT))
            _EFFECTIVENESS_CACHE[key] = dict(payload)
            return payload

    print(
        f"[arde-serve] running effectiveness demo population={population} "
        f"warmup={warmup} delay={delay} horizon={horizon}",
        flush=True,
    )
    payload = run_demo(warmup, delay, horizon, population)
    payload = dict(payload)
    payload["ok"] = True
    payload["source"] = "live_demo"
    _EFFECTIVENESS_CACHE[key] = dict(payload)
    return payload


def _density_from_population(population: int, base_pop: int = 900, base_density: float = 4.6) -> float:
    """Map agent count to a plausible peak density for /optimize."""
    scale = max(0.35, min(6.0, float(population) / float(base_pop)))
    return round(base_density * scale, 3)


def _read_json_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"ok": False, "error": f"missing file: {path.name}"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"invalid json in {path.name}: {exc}"}
    return {"ok": True, "data": data}


def _load_sample_catalog() -> List[Dict[str, Any]]:
    loaded = _read_json_file(SAMPLE_CATALOG_PATH)
    if loaded.get("ok") and isinstance(loaded.get("data"), list):
        return loaded["data"]
    # fallback single sample
    return [{
        "id": "combined",
        "label": "复合：聚集 + 内涝",
        "description": "默认样例",
        "file": "sample_optimize_request.json",
        "profile": "combined",
    }]


def _load_sample_by_id(sample_id: str) -> Dict[str, Any]:
    catalog = _load_sample_catalog()
    match = next((item for item in catalog if item.get("id") == sample_id), None)
    if match is None and sample_id in {"default", "sample", ""}:
        match = catalog[0] if catalog else None
    if match is None:
        return {"ok": False, "error": f"unknown sample id: {sample_id}"}
    path = EXAMPLES / str(match.get("file") or "")
    loaded = _read_json_file(path)
    if not loaded.get("ok"):
        return loaded
    return {
        "ok": True,
        "id": match.get("id"),
        "label": match.get("label"),
        "description": match.get("description"),
        "profile": match.get("profile"),
        "mapScene": match.get("mapScene") or (
            "zhengzhou720" if match.get("profile") == "urban_flood_720" else "bund"
        ),
        "request": loaded["data"],
    }


def _json_response(handler: BaseHTTPRequestHandler, code: int, payload: Dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.end_headers()
    handler.wfile.write(body)


class ArdeOptimizeHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path in {"/", "/health"}:
            _json_response(self, 200, {"ok": True, "engine": "ARDE-Emergency-v1", "service": "arde-optimize"})
            return
        if path in {"/policies", "/decisions"}:
            _json_response(self, 200, {"ok": True, "decisions": list_arde_decisions()})
            return
        if path in {"/effectiveness", "/api/crowdsim/arde/effectiveness"}:
            query = parse_qs(urlparse(self.path).query)
            population = _clamp_int(
                (query.get("population") or [None])[0],
                _DEFAULT_DEMO["population"],
                100,
                10000,
            )
            warmup = _clamp_int((query.get("warmup") or [None])[0], _DEFAULT_DEMO["warmup"], 2, 40)
            delay = _clamp_int((query.get("delay") or [None])[0], _DEFAULT_DEMO["delay"], 1, 30)
            horizon = _clamp_int((query.get("horizon") or [None])[0], _DEFAULT_DEMO["horizon"], 8, 120)
            # GET without params → instant cached file; with population → may run live demo
            if "population" not in query and "warmup" not in query and "horizon" not in query:
                loaded = _read_json_file(EFFECTIVENESS_PATH)
                if not loaded.get("ok") or not isinstance(loaded.get("data"), dict):
                    _json_response(self, 404, {"ok": False, "error": loaded.get("error") or "invalid effectiveness file"})
                    return
                payload = dict(loaded["data"])
                payload["ok"] = True
                payload["source"] = "cached_demo"
                payload["path"] = str(EFFECTIVENESS_PATH.relative_to(ROOT))
                _json_response(self, 200, payload)
                return
            try:
                payload = _run_effectiveness(
                    population=population,
                    warmup=warmup,
                    delay=delay,
                    horizon=horizon,
                )
                _json_response(self, 200, payload)
            except Exception as exc:  # noqa: BLE001
                _json_response(self, 500, {"ok": False, "error": f"effectiveness run failed: {exc}"})
            return
        if path in {"/sample-request", "/api/crowdsim/arde/sample-request"}:
            query = parse_qs(urlparse(self.path).query)
            sample_id = (query.get("id") or ["combined"])[0]
            loaded = _load_sample_by_id(sample_id)
            code = 200 if loaded.get("ok") else 404
            _json_response(self, code, loaded)
            return
        if path in {"/sample-requests", "/api/crowdsim/arde/sample-requests"}:
            _json_response(self, 200, {"ok": True, "items": _load_sample_catalog()})
            return
        _json_response(self, 404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            _json_response(self, 400, {"ok": False, "error": "invalid json"})
            return
        if not isinstance(payload, dict):
            _json_response(self, 400, {"ok": False, "error": "payload must be an object"})
            return

        if path in {"/effectiveness", "/effectiveness/run", "/api/crowdsim/arde/effectiveness", "/api/crowdsim/arde/effectiveness/run"}:
            population = _clamp_int(payload.get("population"), _DEFAULT_DEMO["population"], 100, 10000)
            warmup = _clamp_int(payload.get("warmup"), _DEFAULT_DEMO["warmup"], 2, 40)
            delay = _clamp_int(payload.get("delay"), _DEFAULT_DEMO["delay"], 1, 30)
            horizon = _clamp_int(payload.get("horizon"), _DEFAULT_DEMO["horizon"], 8, 120)
            use_cache = bool(payload.get("useCache", True))
            try:
                result = _run_effectiveness(
                    population=population,
                    warmup=warmup,
                    delay=delay,
                    horizon=horizon,
                    use_cache=use_cache,
                )
                result["request"] = {
                    "population": population,
                    "warmup": warmup,
                    "delay": delay,
                    "horizon": horizon,
                }
                result["implied_density"] = _density_from_population(population)
                _json_response(self, 200, result)
            except Exception as exc:  # noqa: BLE001
                _json_response(self, 500, {"ok": False, "error": f"effectiveness run failed: {exc}"})
            return

        if path not in {"/optimize", "/api/crowdsim/arde/optimize"}:
            _json_response(self, 404, {"ok": False, "error": "not found"})
            return

        # Empty body → use sample request so the effectiveness page can one-click demo.
        if not payload:
            loaded = _read_json_file(SAMPLE_REQUEST_PATH)
            if not loaded.get("ok") or not isinstance(loaded.get("data"), dict):
                _json_response(self, 400, {"ok": False, "error": loaded.get("error") or "sample request invalid"})
                return
            payload = loaded["data"]

        # Optional: inject density from population so optimize tracks the same N.
        population = payload.get("population") or (payload.get("options") or {}).get("population")
        if population is not None:
            dens = _density_from_population(_clamp_int(population, 900, 100, 10000))
            workflow = payload.setdefault("workflow", {})
            experiment = workflow.setdefault("experiment", {})
            metrics = experiment.setdefault("averageMetrics", {})
            metrics["density"] = dens
            exp_payload = payload.setdefault("experimentPayload", {})
            baseline = exp_payload.setdefault("baselineMetrics", {})
            baseline["density"] = dens
            options = payload.setdefault("options", {})
            options["population"] = _clamp_int(population, 900, 100, 10000)

        result = build_arde_optimization(payload)
        _json_response(self, 200, result)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ARDE emergency optimization HTTP sidecar for CrowdSim.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    server = ThreadingHTTPServer((args.host, args.port), ArdeOptimizeHandler)
    print(f"ARDE emergency optimizer listening on http://{args.host}:{args.port}/optimize", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
