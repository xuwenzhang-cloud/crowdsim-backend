"""Local HTTP API for CrowdSim conversation storage."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
import socket
import re
from urllib.parse import urlsplit

from dialog_storage.repository import ConversationRepository
from dialog_storage.during_requirements import DuringRequirementRepository, RequirementConflict
from dialogue_service.logging_setup import configure_logging


MAX_BODY_BYTES = 16 * 1024 * 1024
logger = logging.getLogger(__name__)


def _allowed_origins() -> set[str]:
    hosts = {"localhost", "127.0.0.1"}
    try:
        hosts.update(address for address in socket.gethostbyname_ex(socket.gethostname())[2] if address)
    except OSError:
        pass
    origins = {f"http://{host}:{port}" for host in hosts for port in (8080, 9090)}
    origins.update(value.strip().rstrip("/") for value in os.environ.get("CROWDSIM_DIALOG_ALLOWED_ORIGINS", "").split(",") if value.strip())
    return origins


class DialogueHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, DialogueHandler)
        self.repository = ConversationRepository()
        self.workbench_repository = ConversationRepository(self.repository.root / "workbench")
        self.during_requirements = DuringRequirementRepository(self.repository.root / "during-requirements")
        self.repository.root.mkdir(parents=True, exist_ok=True)
        self.workbench_repository.root.mkdir(parents=True, exist_ok=True)
        self.allowed_origins = _allowed_origins()


class DialogueHandler(BaseHTTPRequestHandler):
    server: DialogueHttpServer

    def log_message(self, format_string: str, *args) -> None:
        logger.info("HTTP %s %s: %s", self.command, urlsplit(self.path).path, format_string % args)

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        logger.info("HTTP %s %s -> %s", self.command, urlsplit(self.path).path, code)

    def _headers(self, status: int, *, length: int = 0) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        origin = self.headers.get("Origin", "")
        if origin in self.server.allowed_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            if self.headers.get("Access-Control-Request-Private-Network") == "true":
                self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()

    def _send(self, status: int, payload: dict) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._headers(status, length=len(encoded))
        self.wfile.write(encoded)

    def _permitted(self) -> bool:
        host = self.headers.get("Host", "").lower()
        if host not in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}:
            self._send(403, {"message": "只允许本机访问对话服务"})
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in self.server.allowed_origins:
            self._send(403, {"message": "不允许的前端来源"})
            return False
        return True

    def _json_body(self) -> dict:
        if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
            raise ValueError("请求必须使用 application/json")
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("无效的请求长度") from exc
        if size <= 0 or size > MAX_BODY_BYTES:
            raise ValueError("请求内容为空或超过 16 MB")
        payload = json.loads(self.rfile.read(size))
        if not isinstance(payload, dict):
            raise ValueError("请求内容必须是 JSON 对象")
        return payload

    def do_OPTIONS(self) -> None:
        if self._permitted():
            self._headers(204)

    def do_GET(self) -> None:
        if not self._permitted():
            return
        path = urlsplit(self.path).path
        match = re.fullmatch(r"/during/conversations/([A-Za-z0-9_-]{1,120})/requirements", path)
        if match:
            try:
                return self._send(200, {"record": self.server.during_requirements.load(match.group(1))})
            except (OSError, ValueError):
                return self._send(503, {"message": "事中需求档案无法读取，未创建空白档案"})
        if path == "/health":
            self._send(200, {"service": "crowdsim-dialogue-storage"})
        elif path == "/conversations":
            self._send(200, {"conversations": self.server.repository.list_all()})
        elif path == "/workbench/conversations":
            self._send(200, {"conversations": self.server.workbench_repository.list_all()})
        else:
            self._send(404, {"message": "未找到接口"})

    def do_POST(self) -> None:
        if not self._permitted():
            return
        path = urlsplit(self.path).path
        try:
            match = re.fullmatch(r"/during/conversations/([A-Za-z0-9_-]{1,120})/requirements", path)
            if match:
                payload = self._json_body()
                record = self.server.during_requirements.save(match.group(1), state=payload.get("state"),
                    expected_revision=payload.get("expectedRevision"), mutation_id=payload.get("mutationId"))
                self._send(200, {"record": record})
            elif path == "/conversations/sync":
                payload = self._json_body()
                count = self.server.repository.replace_all(payload.get("conversations"))
                self._send(200, {"saved": count})
            elif path == "/workbench/conversations/sync":
                payload = self._json_body()
                self.server.workbench_repository.upsert(payload.get("conversation"))
                self._send(200, {"saved": 1})
            elif path == "/post-review/chat":
                from dialogue_service.post_agent import respond
                self._send(200, respond(self._json_body()))
            else:
                self._send(404, {"message": "未找到接口"})
        except RequirementConflict as exc:
            self._send(409, {"code": "DURING_REQUIREMENT_CONFLICT", "message": str(exc), "record": exc.record})
        except ValueError as exc:
            logger.warning("Invalid dialogue request %s: %s", path, exc)
            self._send(400, {"message": str(exc)})
        except OSError as exc:
            logger.exception("Dialogue request failed with OS error: %s", path)
            self._send(503, {"message": f"本机对话存储不可用：{exc}"})
        except Exception as exc:
            logger.exception("Dialogue request failed: %s", path)
            if isinstance(exc, RuntimeError):
                self._send(502, {"message": str(exc)})
            else:
                self._send(502, {"message": f"本机对话存储失败：{type(exc).__name__}"})


def main() -> None:
    parser = argparse.ArgumentParser(description="CrowdSim local conversation storage service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args()
    log_path = configure_logging()
    if args.host not in {"127.0.0.1", "localhost"}:
        parser.error("对话服务只能监听本机回环地址")
    server = DialogueHttpServer((args.host, args.port))
    logger.info("CrowdSim conversation storage listening on http://%s:%s; log=%s",
                args.host, args.port, log_path)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
