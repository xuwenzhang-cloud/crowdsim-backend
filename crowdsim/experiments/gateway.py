"""Single-owner WebSocket gateway used by the experiment service."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
from copy import deepcopy
import json
import inspect
import logging
import queue
import threading
import time
import uuid
from typing import Any, Callable
from urllib.parse import urlsplit


class CrowdSimGateway:
    def __init__(self, url: str = "ws://127.0.0.1:8765", *, reconnect_seconds: float = 2.0) -> None:
        self.url = str(url)
        self.reconnect_seconds = max(0.2, float(reconnect_seconds))
        self.logger = logging.getLogger("crowdsim.experiments.gateway")
        self._state = "disconnected"
        self._state_error: str | None = None
        self._state_lock = threading.RLock()
        self._connected = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._websocket = None
        self._send_lock = None
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._pending: dict[str, tuple[threading.Event, dict[str, Any]]] = {}
        self._pending_lock = threading.RLock()
        self.latest_frame: dict[str, Any] | None = None
        self.capabilities: dict[str, Any] = {}
        self.last_message_at: float | None = None

    def add_listener(self, callback: Callable[[dict[str, Any]], None]) -> None:
        with self._state_lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[dict[str, Any]], None]) -> None:
        with self._state_lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def _set_state(self, state: str, error: str | None = None) -> None:
        with self._state_lock:
            self._state = state
            self._state_error = error
        self._notify({"type": "gateway_state", "state": state, "error": error})

    def health(self) -> dict[str, Any]:
        with self._pending_lock:
            pending_commands = len(self._pending)
        with self._state_lock:
            return {
                "state": self._state,
                "url": self.url,
                "connected": self._connected.is_set(),
                "error": self._state_error,
                "lastMessageAt": self.last_message_at,
                "capabilities": deepcopy(self.capabilities),
                "pendingCommands": pending_commands,
            }

    def start(self, *, wait_seconds: float = 0.0) -> bool:
        with self._state_lock:
            if self._thread is None or not self._thread.is_alive():
                self._stopping.clear()
                self._thread = threading.Thread(target=self._thread_main, name="crowdsim-gateway", daemon=True)
                self._thread.start()
        # Never wait while holding _state_lock: the connection thread needs that
        # lock to publish its connecting/ready transition.
        return self._connected.wait(max(0.0, float(wait_seconds))) if wait_seconds else self._connected.is_set()

    def close(self) -> None:
        self._stopping.set()
        loop = self._loop
        websocket = self._websocket
        if loop is not None and websocket is not None:
            try:
                asyncio.run_coroutine_threadsafe(websocket.close(), loop).result(timeout=3)
            except Exception:
                pass
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)
        self._connected.clear()
        self._set_state("disconnected")

    def submit(self, payload: dict[str, Any], *, request_id: str | None = None, connect_timeout: float = 5.0) -> str:
        message = deepcopy(dict(payload))
        request_id = str(request_id or message.get("request_id") or f"req_{uuid.uuid4().hex}")
        message["request_id"] = request_id
        self.start()
        if not self._connected.wait(connect_timeout):
            raise RuntimeError("CrowdSim gateway is not connected")
        event = threading.Event()
        with self._pending_lock:
            if request_id in self._pending:
                raise ValueError(f"duplicate request_id: {request_id}")
            self._pending[request_id] = (event, {})
        loop = self._loop
        if loop is None:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise RuntimeError("CrowdSim gateway event loop is unavailable")
        try:
            if threading.current_thread() is self._thread:
                # Frame listeners run on the gateway event-loop thread. Waiting
                # on run_coroutine_threadsafe from that same thread deadlocks
                # until connect_timeout and prevents closed-loop actions from
                # reaching CrowdSim. Queue the send directly in this case; the
                # normal acknowledgement path still resolves the pending entry.
                task = loop.create_task(self._send(message))
                task.add_done_callback(
                    lambda completed, rid=request_id: self._handle_send_completion(rid, completed)
                )
            else:
                asyncio.run_coroutine_threadsafe(self._send(message), loop).result(timeout=connect_timeout)
        except Exception:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise
        return request_id

    def _handle_send_completion(self, request_id: str, task: asyncio.Task) -> None:
        """Resolve an asynchronously queued gateway-thread send failure."""
        if task.cancelled():
            error = "gateway send was cancelled"
        else:
            exception = task.exception()
            if exception is None:
                return
            error = f"{type(exception).__name__}: {exception}"
        with self._pending_lock:
            pending = self._pending.get(str(request_id))
            if pending is not None:
                pending[1].update({
                    "type": "error",
                    "code": "gateway_send_failed",
                    "status": "rejected",
                    "message": error,
                    "request_id": str(request_id),
                })
                pending[0].set()
        self._notify({
            "type": "error",
            "code": "gateway_send_failed",
            "status": "rejected",
            "message": error,
            "request_id": str(request_id),
        })

    def wait_for_ack(self, request_id: str, timeout: float = 10.0) -> dict[str, Any] | None:
        with self._pending_lock:
            pending = self._pending.get(str(request_id))
        if pending is None:
            return None
        event, result = pending
        if not event.wait(timeout):
            return None
        with self._pending_lock:
            self._pending.pop(str(request_id), None)
        return deepcopy(result)

    def forget_request(self, request_id: str) -> None:
        with self._pending_lock:
            self._pending.pop(str(request_id), None)

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._connection_loop())
        finally:
            self._loop = None
            self._connected.clear()

    async def _connection_loop(self) -> None:
        import websockets

        self._loop = asyncio.get_running_loop()
        while not self._stopping.is_set():
            self._set_state("connecting")
            try:
                async with websockets.connect(self.url, **self._connection_options(websockets.connect)) as websocket:
                    self._websocket = websocket
                    self._send_lock = asyncio.Lock()
                    self._connected.set()
                    self._set_state("ready")
                    async for raw in websocket:
                        await self._handle_raw(raw)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self._set_state("unavailable", f"{type(exc).__name__}: {exc}")
            finally:
                self._websocket = None
                self._connected.clear()
                self._reject_pending("gateway_disconnected")
            if not self._stopping.is_set():
                await asyncio.sleep(self.reconnect_seconds)

    def _connection_options(self, connect) -> dict[str, Any]:
        options = {"ping_interval": 20, "ping_timeout": 20, "max_size": 32 * 1024 * 1024}
        # New websockets versions discover system proxies (including macOS VPN
        # proxies). A loopback simulator connection must stay on this machine.
        # Older supported versions have no proxy option and already connect directly.
        if urlsplit(self.url).hostname in {"localhost", "127.0.0.1", "::1"} and "proxy" in inspect.signature(connect).parameters:
            options["proxy"] = None
        return options

    async def _send(self, payload: dict[str, Any]) -> None:
        websocket = self._websocket
        if websocket is None:
            raise RuntimeError("CrowdSim websocket is unavailable")
        async with self._send_lock:
            await websocket.send(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    async def _handle_raw(self, raw: str | bytes) -> None:
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            self._notify({"type": "gateway_protocol_error", "message": str(exc)})
            return
        if not isinstance(payload, dict):
            self._notify({"type": "gateway_protocol_error", "message": "CrowdSim message must be an object"})
            return
        self._handle_payload(payload)

    def _handle_payload(self, payload: dict[str, Any]) -> None:
        self.last_message_at = time.time()
        message_type = payload.get("type")
        if message_type in {"init", "update"}:
            self.latest_frame = deepcopy(payload)
        if message_type == "init":
            self.capabilities = deepcopy(payload.get("capabilities") or {})
        request_id = payload.get("request_id") or payload.get("requestId")
        if request_id is not None and message_type in {"command_result", "error", "init", "status", "speed", "requirement_accepted", "routing_result", "run_closed"}:
            with self._pending_lock:
                pending = self._pending.get(str(request_id))
                if pending is not None:
                    pending[1].update(deepcopy(payload))
                    pending[0].set()
        self._notify(payload)

    def _reject_pending(self, reason: str) -> None:
        with self._pending_lock:
            for event, result in self._pending.values():
                result.update({"type": "error", "code": reason, "status": "rejected", "message": reason})
                event.set()

    def _notify(self, payload: dict[str, Any]) -> None:
        with self._state_lock:
            listeners = list(self._listeners)
        for callback in listeners:
            try:
                callback(deepcopy(payload))
            except Exception:
                self.logger.exception("CrowdSim gateway listener failed")

    # Test hook: dispatch an already decoded message without a live socket.
    def dispatch_for_test(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            raise TypeError("payload must be an object")
        self._handle_payload(deepcopy(payload))
