"""Tiny HTTP health endpoint that runs alongside the Telegram poll loop.

Why this exists
---------------

Some hosts (Hugging Face Spaces, Render, Railway, Fly.io, ...) demand
that the container expose a *listening port* — they assume every
service is an HTTP app, not a long-poll worker.  If nothing is
listening, the platform marks the deploy as "unhealthy" and either
restarts it endlessly or refuses to keep it running.

We're a Telegram long-poll bot — we don't actually need to receive
HTTP traffic.  But we DO need to satisfy the platform.  So this
module spins up a 1-route HTTP server in a daemon thread that:

* Returns ``200 alive`` on ``GET /``      → satisfies platform probes.
* Returns ``200 ok``    on ``GET /health`` → for UptimeRobot /
  GitHub Actions cron pings (the same workflow is what keeps a
  free-tier HF Space awake past the 48-hour idle window).
* Returns ``404`` on anything else        → no surface area for abuse.

The server is a daemon thread so when the Telegram process dies
(``Ctrl-C`` or container shutdown) the listener disappears with it
— no zombie sockets to clean up.

Disabled by default.  Set ``TELEGRAM_HEALTH_PORT=7860`` (or whatever
your host wants) to enable.
"""

from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_log = logging.getLogger(__name__)


class _HealthHandler(BaseHTTPRequestHandler):
    """Minimal handler — no routing framework needed for two routes."""

    # Suppress the default "127.0.0.1 - - [date] GET / 200" noise.
    # Health pings happen every few minutes; logging each one drowns
    # the *interesting* logs (LLM calls, expense writes).
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        if self.path in ("/", "/health"):
            body = b"alive" if self.path == "/" else b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


class HealthServer:
    """Wraps a daemon-thread HTTP server you can start once and forget."""

    def __init__(self, port: int, host: str = "0.0.0.0") -> None:
        # 0.0.0.0 is mandatory inside containers — 127.0.0.1 isn't
        # reachable from the host's healthcheck, only from inside the
        # container's own loopback.
        self._port = port
        self._host = host
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        """Actual bound port.  Differs from the requested port when 0 is passed."""
        if self._server is None:
            return self._port
        return self._server.server_address[1]

    def start(self) -> None:
        """Start the listener on a background daemon thread.

        Idempotent — calling twice is a no-op (with a warning).  Bind
        failures (e.g. port already in use) propagate so the bot
        never silently runs without the health endpoint a host needs.
        """
        if self._server is not None:
            _log.warning("HealthServer.start() called twice — ignoring.")
            return
        self._server = ThreadingHTTPServer((self._host, self._port), _HealthHandler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="expense-tracker-health",
            daemon=True,
        )
        self._thread.start()
        _log.info(
            "Health endpoint listening on http://%s:%d/ "
            "(GET / -> 'alive', GET /health -> 'ok')",
            self._host, self.port,
        )

    def stop(self, timeout: float = 2.0) -> None:
        """Shut the server down cleanly.  Used by tests; production exits hard."""
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._server = None
        self._thread = None


def maybe_start_health_server(port: int | None) -> HealthServer | None:
    """Convenience: start a server when the port is set, else no-op.

    Returns the running :class:`HealthServer` (so callers can
    ``stop()`` it in tests) or ``None`` when the port is unset.
    """
    if port is None:
        return None
    server = HealthServer(port=port)
    server.start()
    return server


__all__ = ["HealthServer", "maybe_start_health_server"]
