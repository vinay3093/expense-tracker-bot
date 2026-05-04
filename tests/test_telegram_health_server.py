"""Unit tests for the tiny HTTP health server.

Boots a real server on an OS-assigned ephemeral port (so tests are
parallel-safe) and hits it over HTTP.  No mocks — the entire stack
is socket-level so we exercise the same code path Hugging Face's
healthcheck will hit in production.
"""

from __future__ import annotations

import http.client
import socket
import urllib.request
from urllib.error import HTTPError

import pytest

from expense_tracker.telegram_app.health_server import (
    HealthServer,
    maybe_start_health_server,
)


def _free_port() -> int:
    """Ask the kernel for a port that's free *right now*.

    Tiny race window between this call and ``HealthServer.start()``,
    but vastly safer than hard-coding a port and clashing with a
    parallel test run.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def running_server():
    server = HealthServer(port=_free_port(), host="127.0.0.1")
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _get(server: HealthServer, path: str) -> tuple[int, bytes]:
    url = f"http://127.0.0.1:{server.port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310 - localhost
            return resp.status, resp.read()
    except HTTPError as exc:
        return exc.code, exc.read()


def _head(server: HealthServer, path: str) -> tuple[int, str | None]:
    """Issue a real HEAD request and return (status, content_length).

    ``urllib.request`` only speaks GET/POST out of the box, so we drop
    to ``http.client`` for direct verb control.  This is the same
    method UptimeRobot/Pingdom use for HTTP monitors by default.
    """
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=2)
    try:
        conn.request("HEAD", path)
        resp = conn.getresponse()
        return resp.status, resp.getheader("Content-Length")
    finally:
        conn.close()


def test_root_returns_alive(running_server):
    status, body = _get(running_server, "/")
    assert status == 200
    assert body == b"alive"


def test_health_returns_ok(running_server):
    status, body = _get(running_server, "/health")
    assert status == 200
    assert body == b"ok"


def test_unknown_route_returns_404(running_server):
    status, _body = _get(running_server, "/some-other-path")
    assert status == 404


def test_head_root_returns_200_with_content_length(running_server):
    """Regression: UptimeRobot defaults to HEAD; without do_HEAD we'd
    get 501 Not Implemented from BaseHTTPServer and the monitor would
    flip to 'Down' even though the bot is healthy.

    HEAD must mirror GET's status + Content-Length per RFC 7231 §4.3.2
    but transfer no body.
    """
    status, content_length = _head(running_server, "/")
    assert status == 200
    assert content_length == str(len(b"alive"))


def test_head_health_returns_200_with_content_length(running_server):
    status, content_length = _head(running_server, "/health")
    assert status == 200
    assert content_length == str(len(b"ok"))


def test_head_unknown_route_returns_404(running_server):
    status, _content_length = _head(running_server, "/some-other-path")
    assert status == 404


def test_double_start_is_no_op(running_server, caplog):
    """Idempotency — a second .start() warns and returns rather than crash."""
    with caplog.at_level("WARNING", logger="expense_tracker.telegram_app.health_server"):
        running_server.start()
    assert any(
        "called twice" in r.message for r in caplog.records
    ), "expected warning when start() called twice"


def test_stop_is_idempotent():
    server = HealthServer(port=_free_port(), host="127.0.0.1")
    server.start()
    server.stop()
    # Second stop must NOT raise — production exits hard but tests
    # may stop multiple times via fixtures.
    server.stop()


def test_maybe_start_returns_none_when_port_unset():
    assert maybe_start_health_server(None) is None


def test_maybe_start_returns_running_server_when_port_set():
    port = _free_port()
    server = maybe_start_health_server(port)
    try:
        assert server is not None
        # The handler is live — we can hit it.
        status, body = _get(server, "/")
        assert status == 200
        assert body == b"alive"
    finally:
        if server is not None:
            server.stop()
