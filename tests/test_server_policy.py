"""HTTP boundary rules: constant-time token check, body cap, loopback default.

Also the log-noise rule: a client that hung up is not a server crash.

daemon.log had ~5,800 ConnectionAbortedError tracebacks (WinError 10053). A
browser closes an SSE stream and both socketserver's default handler and the
handler guard printed a full stack for it, which drowned the real failures.
The tests below pin the filter, the server override and the guard.
"""

from __future__ import annotations

import errno
from http.server import ThreadingHTTPServer

import pytest

from sodus import config, server


def test_authorized_accepts_only_the_real_token(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    token = config.token()
    assert server.authorized(f"Bearer {token}") is True
    assert server.authorized("Bearer nope") is False
    assert server.authorized("") is False
    assert server.authorized(f"Bearer {token}extra") is False


def test_body_cap():
    assert server.body_allowed(0) is True
    assert server.body_allowed(server.MAX_BODY) is True
    assert server.body_allowed(server.MAX_BODY + 1) is False
    assert server.body_allowed(-1) is False


def test_non_loopback_bind_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.delenv("SODUS_ALLOW_REMOTE", raising=False)
    with pytest.raises(SystemExit):
        server.serve(port=1, host="0.0.0.0")


# ---- a disconnect is not a crash ---------------------------------------


def test_winerror_10053_and_10054_are_disconnects():
    assert server._is_disconnect(ConnectionAbortedError(10053)) is True
    assert server._is_disconnect(ConnectionResetError(10054)) is True
    assert server._is_disconnect(BrokenPipeError(errno.EPIPE, "broken pipe")) is True
    # A plain OSError can carry the Windows code instead of the mapped subclass.
    win = OSError("the client went away")
    win.winerror = 10053
    assert server._is_disconnect(win) is True
    assert server._is_disconnect(OSError(errno.ECONNRESET, "reset")) is True


def test_a_real_error_is_not_a_disconnect():
    assert server._is_disconnect(ValueError("boom")) is False
    assert server._is_disconnect(OSError(errno.ENOENT, "missing")) is False
    assert server._is_disconnect(None) is False


def _quiet_server() -> server._QuietServer:
    """The override is pure, so it needs no bound socket to test."""
    return object.__new__(server._QuietServer)


def test_the_server_drops_a_disconnect_without_a_traceback(monkeypatch):
    logged = []
    monkeypatch.setattr(server, "_log", logged.append)
    httpd = _quiet_server()

    try:
        raise ConnectionAbortedError(10053, "你的主机中的软件中止了一个已建立的连接")
    except ConnectionAbortedError:
        httpd.handle_error(None, ("127.0.0.1", 50000))

    assert logged == []


def test_the_server_still_logs_an_unexpected_crash(monkeypatch):
    logged = []
    monkeypatch.setattr(server, "_log", logged.append)
    httpd = _quiet_server()

    try:
        raise RuntimeError("boom")
    except RuntimeError:
        httpd.handle_error(None, ("127.0.0.1", 50000))

    assert len(logged) == 1
    assert "request crashed" in logged[0]
    assert "Traceback" in logged[0]
    assert "RuntimeError: boom" in logged[0]


def test_the_daemon_serves_through_the_quiet_server():
    # serve() builds this class; the override must be a real HTTPServer.
    assert issubclass(server._QuietServer, ThreadingHTTPServer)
    assert server._QuietServer.handle_error is not ThreadingHTTPServer.handle_error


def test_an_aborted_write_inside_a_handler_is_not_a_crash(monkeypatch):
    logged = []
    monkeypatch.setattr(server, "_log", logged.append)
    handler = object.__new__(server.Handler)
    handler.headers = __import__("email.message", fromlist=["Message"]).Message()
    handler.headers["Host"] = "localhost"

    def aborted():
        raise ConnectionAbortedError(10053, "the client went away")

    handler._guard(aborted)
    assert logged == []


def test_a_real_handler_crash_is_logged_and_answered(monkeypatch):
    logged, replies = [], []
    monkeypatch.setattr(server, "_log", logged.append)
    monkeypatch.setattr(
        server, "_json", lambda handler, code, payload: replies.append((code, payload))
    )
    handler = object.__new__(server.Handler)
    handler.headers = __import__("email.message", fromlist=["Message"]).Message()
    handler.headers["Host"] = "localhost"

    def boom():
        raise ValueError("bad json")

    handler._guard(boom)

    assert replies == [(500, {"error": "internal error"})]
    assert "handler crashed" in logged[0]
    assert "ValueError: bad json" in logged[0]
