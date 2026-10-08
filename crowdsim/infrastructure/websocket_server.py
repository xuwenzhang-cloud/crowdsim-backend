"""Single-client WebSocket adapter for :class:`SimulationRuntime`."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from dataclasses import asdict
import json
import time
from typing import Any, Dict, Optional

import websockets

from crowdsim.core.simulation_runtime import RuntimeState
from crowdsim.domain.requirement_spec import RequirementSpec, RequirementValidationError
from crowdsim.infrastructure.requirement_repository import (
    RequirementNotFoundError,
    RequirementRepository,
)


class OverlayServer:
    def __init__(
        self,
        simulator: Any,
        host: str,
        port: int,
        *,
        requirement_repository: RequirementRepository | None = None,
    ) -> None:
        self.simulator = simulator
        self.host = host
        self.port = port
        self.client: Any = None
        self.task: Optional[asyncio.Task] = None
        self.processed_request_ids: set[str] = set()
        self.requirements = requirement_repository or RequirementRepository()

    async def start(self) -> None:
        server = await websockets.serve(self._handler, self.host, self.port)
        print(f"[CrowdSim] WebSocket listening at ws://{self.host}:{self.port}")
        await server.wait_closed()

    async def _handler(self, websocket: Any) -> None:
        if self.client is not None:
            await websocket.send(json.dumps(self._error("single_client_only", "Only one client is supported.")))
            await websocket.close()
            return
        self.client = websocket
        self.processed_request_ids.clear()
        try:
            async for message in websocket:
                await self._handle_message(message)
        except websockets.ConnectionClosed:
            pass
        finally:
            try:
                await self._stop_runtime()
            finally:
                self.client = None
                self.processed_request_ids.clear()

    async def _stop_loop(self) -> None:
        task = self.task
        if task is not None:
            try:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            finally:
                self.task = None

    async def _stop_runtime(self) -> None:
        try:
            await self._stop_loop()
        finally:
            self.simulator.close()

    def _validate_count(self, count) -> None:
        if count is None:
            return
        if type(count) is not int or count < 0:
            raise CommandError("invalid_count", "count must be a non-negative integer")
        if self.simulator.demand_mode == "generated_hotspot":
            try:
                self.simulator.hotspot_demand_spec.validate_count(count)
            except ValueError as exc:
                raise CommandError("invalid_count", str(exc)) from exc
        if self.simulator.demand_mode != "fixed" and not self.simulator.demand_count_configurable:
            raise CommandError("unsupported_demand_count", "count requires explicit <person> demand")

    async def _send_init(self, request_id) -> None:
        await self._send({**self.simulator.init_frame(), "request_id": request_id})

    async def _prepare(self, request_id, count, *, reset=False, seed=None):
        if self.simulator.demand_mode == "generated_hotspot":
            await self._send({"type": "preparing", "request_id": request_id,
                              "message": "正在生成本轮需求并初始化 SUMO，请稍候。"})
        # The message handler awaits this work: no tick/reset can overlap it.
        # A worker keeps WebSocket ping/pong responsive during larger generation.
        if reset:
            if self.simulator.demand_mode == "generated_hotspot" and count is None:
                operation = asyncio.to_thread(
                    self.simulator.reset, use_default_count=True, **({"seed": seed} if seed is not None else {})
                )
            else:
                operation = asyncio.to_thread(
                    self.simulator.reset, count, **({"seed": seed} if seed is not None else {})
                )
        else:
            operation = asyncio.to_thread(self.simulator.initialize)
        worker = asyncio.create_task(operation)
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            # Cancelling a to_thread await does not stop its worker. Finish it
            # before the handler's finally closes SUMO and its recorder.
            try:
                await worker
            finally:
                raise

    async def _configure(self, data, request_id) -> None:
        requirement_record = None
        requirement_id = data.get("requirement_id")
        if requirement_id is not None:
            try:
                requirement_record = self.requirements.load(requirement_id)
            except RequirementNotFoundError as exc:
                raise CommandError("unknown_requirement", str(exc)) from exc
            except (OSError, ValueError) as exc:
                raise CommandError("invalid_requirement_record", str(exc)) from exc
            location_status = requirement_record.get("capabilities", {}).get("location")
            if location_status != "supported":
                location_id = requirement_record["requirement"]["spatial_scope"]["location_id"]
                raise CommandError(
                    "unsupported_requirement_location",
                    f"location {location_id} is stored but cannot initialize the current SUMO scenario",
                )
            if not self.simulator.supports_requirement(requirement_record):
                raise CommandError(
                    "unsupported_runtime_scenario",
                    "this requirement needs the hotspot backend preset; start the server with --scenario hotspot",
                )
            current_requirement_id = getattr(self.simulator, "requirement_id", None)
            if (
                current_requirement_id
                and current_requirement_id != requirement_id
                and self.simulator.state not in {RuntimeState.CREATED, RuntimeState.CLOSED, RuntimeState.ERROR}
            ):
                raise CommandError(
                    "requirement_requires_reset",
                    "changing requirement requires a closed runtime or explicit reset",
                )
            self.simulator.configure_requirement(requirement_record)

        count = data.get("count")
        if requirement_record is not None:
            requirement_count = int(requirement_record["requirement"]["population"]["total"])
            if count is not None and count != requirement_count:
                raise CommandError(
                    "requirement_count_mismatch",
                    "count must match requirement.population.total",
                )
            count = requirement_count
        if count is not None and (type(count) is not int or count < 0):
            raise CommandError("invalid_count", "count must be a non-negative integer")
        if (self.simulator.state in {RuntimeState.RUNNING, RuntimeState.PAUSED}
                and self.simulator.demand_mode != "fixed" and count is not None):
            effective_count = self.simulator.demand_count
            if effective_count is None:
                effective_count = len(self.simulator.population.ledger.planned_ids)
            if count != effective_count:
                raise CommandError("count_requires_reset", "changing demand requires explicit reset")
        self._validate_count(count)
        state = self.simulator.state
        if state in {RuntimeState.CLOSED, RuntimeState.ERROR}:
            await self._stop_loop()
            await self._prepare(request_id, count, reset=True)
        elif state == RuntimeState.CREATED:
            if count is not None:
                self.simulator.configure_demand(count)
            await self._prepare(request_id, count)
        elif count is not None:
            if self.simulator.demand_mode == "fixed":
                self.simulator.configure_demand(count)  # Record ignore; never rebuild.
            else:
                effective_count = self.simulator.demand_count
                if effective_count is None:
                    effective_count = len(self.simulator.population.ledger.planned_ids)
                if count != effective_count:
                    raise CommandError("count_requires_reset", "changing demand requires explicit reset")
        self.simulator.set_playback(speed_factor=data.get("speedFactor"), push_fps=data.get("pushFps"))
        await self._send_init(request_id)

    async def _submit_requirement(self, data, request_id) -> None:
        try:
            spec = RequirementSpec.parse(data.get("requirement"))
            record = self.requirements.create(spec)
        except RequirementValidationError as exc:
            raise CommandError("invalid_requirement", str(exc)) from exc
        except OSError as exc:
            raise CommandError("requirement_storage_failed", str(exc)) from exc
        await self._send({
            "type": "requirement_accepted",
            "request_id": request_id,
            "requirement_id": record["requirement_id"],
            "schema_version": record["schema_version"],
            "fingerprint": record["fingerprint"],
            "status": record["status"],
            "capabilities": record["capabilities"],
            "warnings": record["capabilities"].get("warnings", []),
        })

    async def _handle_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            await self._send(self._error("invalid_json", "Message must be valid JSON."))
            return
        if not isinstance(data, dict):
            await self._send(self._error("invalid_message", "Message must be an object."))
            return
        action = data.get("action")
        if action == "performance_report":
            # Best-effort diagnostics only: no commands, acknowledgements or
            # simulation state changes, and no growth of processed_request_ids.
            self.simulator.performance.frontend(data.get("report"))
            return
        request_id = data.get("request_id")
        if request_id is not None and str(request_id) in self.processed_request_ids:
            await self._send(self._error("duplicate_request", "request_id was already processed", request_id))
            return
        try:
            if action == "submit_requirement":
                await self._submit_requirement(data, request_id)
            elif action == "configure":
                await self._configure(data, request_id)
            elif action == "set_speed":
                self.simulator.set_playback(speed_factor=data.get("speedFactor"))
                await self._send({"type": "speed", "request_id": request_id, "speedFactor": self.simulator.sim_speed_factor, "step_length": self.simulator.step_length, "real_step_interval": self.simulator.real_step_interval})
            elif action == "start":
                if self.simulator.state == RuntimeState.CREATED:
                    await self._prepare(request_id, None)
                self.simulator.start()
                await self._command_result(action, request_id, "applied")
                if self.task is None or self.task.done():
                    self.task = asyncio.create_task(self._run_loop())
            elif action == "pause":
                self.simulator.pause()
                await self._command_result(action, request_id, "applied")
            elif action == "get_status":
                await self._send({"type": "status", "request_id": request_id, **self.simulator.diagnostics()})
            elif action == "get_agent_state":
                person_id = str(data.get("id", ""))
                state = self.simulator.population.state_for(person_id)
                motion = self.simulator.current.persons.get(person_id) if self.simulator.current else None
                if state is None and motion is None:
                    raise CommandError("unknown_agent", f"Unknown person id: {person_id}")
                await self._send({"type": "agent_state", "request_id": request_id, "snapshot_id": self.simulator.snapshot_id, "time": self.simulator.time_seconds, "id": person_id, "profile": asdict(self.simulator.profile_for(person_id)), "state": asdict(state) if state else None, "motion": asdict(motion) if motion else None})
            elif action == "evaluate_routes":
                from crowdsim.decision.routing_registry import run_routing_algorithm

                routes = data.get("routes") or []
                if self.simulator.route_provider is None:
                    raise ValueError("routing topology is unavailable until the simulator is initialized")
                for route in routes:
                    if not isinstance(route, dict):
                        raise ValueError("each route must be an object")
                    self.simulator.route_provider.validate_edges(tuple(map(str, route.get("routeEdges") or route.get("edges") or ())))
                algorithm = str(data.get("algorithm") or "")
                if algorithm == "validate_only":
                    result = {"algorithm": algorithm, "valid": True, "routeCount": len(routes)}
                else:
                    result = run_routing_algorithm(algorithm, routes, **dict(data.get("parameters") or {}))
                await self._send({"type": "routing_result", "request_id": request_id, "result": result})
            elif action in {"update_flood_source", "set_event", "trigger_event", "event_decision", "set_policy", "apply_policy", "apply_action", "set_group"}:
                queued = self.simulator.queue_command(action, data, request_id)
                if self.simulator.state != RuntimeState.RUNNING:
                    self.simulator.process_pending_commands()
                    for result in self.simulator.take_command_results():
                        await self._send(result)
                else:
                    await self._send(queued)
            elif action == "reset":
                self._validate_count(data.get("count"))
                seed = data.get("seed")
                if seed is not None and type(seed) is not int:
                    raise CommandError("invalid_seed", "seed must be an integer")
                await self._stop_loop()
                await self._prepare(request_id, data.get("count"), reset=True, seed=seed)
                await self._send_init(request_id)
            else:
                raise CommandError("unknown_action", f"Unknown action: {action}")
            if request_id is not None:
                self.processed_request_ids.add(str(request_id))
        except CommandError as exc:
            error = self._error(exc.code, str(exc), request_id)
            if exc.code == "unsupported_demand_count":
                error["demand"] = self.simulator.demand_diagnostics()
            await self._send(error)
        except (RuntimeError, ValueError) as exc:
            await self._send(self._error("invalid_state", str(exc), request_id))
        except Exception as exc:
            await self._send(self._error("operation_failed", f"{type(exc).__name__}: {exc}", request_id))

    async def _run_loop(self) -> None:
        while self.client is not None:
            running = self.simulator.state == RuntimeState.RUNNING
            probe = self.simulator.performance
            started, cpu_started, simulated = time.perf_counter(), time.process_time(), self.simulator.time_seconds
            terminal = False
            try:
                if running:
                    with probe.measure('tick_total'):
                        await self.simulator.tick_async()
                    for result in self.simulator.take_command_results():
                        await self._send(result)
                    await self._send(self.simulator.frame())
                terminal = self.simulator.state in {RuntimeState.FINISHED, RuntimeState.ERROR, RuntimeState.CLOSED}
                if not terminal:
                    target_interval = self.simulator.real_step_interval
                    sleep_seconds = target_interval
                    if running:
                        # Budget includes tick, frame building, encoding and send.
                        # Start fresh each iteration: never accumulate catch-up
                        # debt after a slow step, a pause or a speed change.
                        elapsed = time.perf_counter() - started
                        sleep_seconds = max(0.0, target_interval - elapsed)
                        probe.sample('target_step_interval', target_interval * 1000)
                        probe.sample('requested_sleep', sleep_seconds * 1000)
                    # sleep(0) deliberately yields even when over budget, so
                    # pause/reset/connection tasks still get an opportunity.
                    with probe.measure('pacing_sleep') if running else nullcontext():
                        await asyncio.sleep(sleep_seconds)
            finally:
                if running:
                    elapsed = time.perf_counter() - started
                    probe.sample('cycle_total', elapsed * 1000)
                    probe.cycle(self.simulator.time_seconds - simulated, elapsed, time.process_time() - cpu_started)
                probe.flush(self.simulator, force=self.simulator.state != RuntimeState.RUNNING)
            if terminal:
                return

    async def _command_result(self, action: str, request_id: Any, status: str) -> None:
        result = {"type": "command_result", "request_id": request_id, "action": action, "status": status, "applied_at": self.simulator.time_seconds}
        if self.simulator.recorder is not None:
            self.simulator.recorder.record_command({"action": action, "request_id": request_id}, result)
        await self._send(result)

    @staticmethod
    def _error(code: str, message: str, request_id: Any = None) -> Dict[str, Any]:
        return {"type": "error", "code": code, "message": message, "request_id": request_id}

    async def _send(self, payload: Dict[str, Any]) -> None:
        if self.client is not None:
            probe = self.simulator.performance
            if payload.get('type') == 'update':
                with probe.measure('json_encode'):
                    encoded = json.dumps(payload, default=str)
                # json.dumps uses ASCII escaping: character count equals UTF-8
                # bytes here, BEFORE WebSocket compression/framing.
                probe.sample('update_payload_bytes', len(encoded), 'bytes')
                with probe.measure('websocket_send'):
                    await self.client.send(encoded)
            else:
                await self.client.send(json.dumps(payload, default=str))


class CommandError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
