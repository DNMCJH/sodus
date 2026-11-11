"""Local daemon HTTP transport for MCP integration tests; never use the live daemon."""
import threading
from http.server import ThreadingHTTPServer
import pytest


@pytest.fixture
def mcp_http(monkeypatch):
    from sodus import server
    from sodus.cli import client
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
    httpd.daemon_threads = True
    monkeypatch.setattr(client, 'BASE', f'http://127.0.0.1:{httpd.server_port}')
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
