from __future__ import annotations

import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Any


class StatusServer:
    """Small localhost HTTP endpoint for VM probes and monitoring agents."""

    def __init__(
        self,
        host: str,
        port: int,
        snapshot: Callable[[], dict[str, Any]],
    ) -> None:
        self.host = host
        self.port = port
        self.snapshot = snapshot
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.port <= 0:
            return
        snapshot = self.snapshot

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path not in {"/", "/healthz", "/readyz", "/status"}:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                state = snapshot()
                bot_state = str(state.get("state", "unknown"))
                # Liveness (/healthz) differs from trading readiness.
                # A stale, paused, stopped or unknown bot must never appear
                # ready to a supervisor or load-balancer health check.
                ready = bot_state == "healthy"
                if self.path == "/healthz":
                    payload = {"ok": True, "state": bot_state}
                    status = HTTPStatus.OK
                elif self.path == "/readyz":
                    payload = {"ready": ready, "state": bot_state}
                    status = HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE
                else:
                    payload = state
                    status = HTTPStatus.OK
                body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="status-http",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2)
