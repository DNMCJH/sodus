"""The OpenAPI contract must stay honest about what `server.py` serves.

PyYAML is not a runtime dependency of Nodus. It is present in the development
environment, so these checks run there; where it is absent the module skips
instead of failing an environment that cannot read the file at all.
"""

from __future__ import annotations

import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from nodus import server

yaml = pytest.importorskip("yaml", reason="PyYAML is needed to read openapi.yaml")

SPEC = Path(__file__).resolve().parent.parent / "openapi.yaml"
HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options", "trace")


@pytest.fixture(scope="module")
def spec() -> dict:
    assert SPEC.is_file(), f"missing contract: {SPEC}"
    with SPEC.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_version_is_3_1(spec):
    assert str(spec.get("openapi", "")).startswith("3.1.")


def test_every_path_is_absolute(spec):
    paths = spec.get("paths") or {}
    assert paths, "the contract has no paths"
    relative = [p for p in paths if not p.startswith("/")]
    assert not relative, f"paths must start with '/': {relative}"


def test_every_operation_has_responses(spec):
    missing = [
        f"{method.upper()} {path}"
        for path, item in spec["paths"].items()
        for method in HTTP_METHODS
        if isinstance(item.get(method), dict) and not item[method].get("responses")
    ]
    assert not missing, f"operations without responses: {missing}"


def test_public_endpoints_opt_out_of_auth(spec):
    # These five are deliberately reachable without the token: the board has to
    # load before it can send one, and the pet/probe/contract have no token at
    # all. Everything else inherits the global bearer requirement.
    expected = {
        ("get", "/"),
        ("get", "/health"),
        ("get", "/pet"),
        ("get", "/favicon.ico"),
        ("get", "/openapi.yaml"),
    }
    actual = {
        (method, path)
        for path, item in spec["paths"].items()
        for method in HTTP_METHODS
        if isinstance(item.get(method), dict) and item[method].get("security") == []
    }
    assert actual == expected


def test_contract_covers_every_route_server_serves(spec):
    expected = {
        ("get", "/"),
        ("get", "/health"),
        ("get", "/pet"),
        ("get", "/favicon.ico"),
        ("get", "/openapi.yaml"),
        ("get", "/board"),
        ("get", "/agents"),
        ("get", "/providers"),
        ("get", "/routes"),
        ("get", "/profiles"),
        ("get", "/tasks"),
        ("post", "/tasks"),
        ("get", "/tasks/{id}"),
        ("get", "/runs"),
        ("post", "/runs"),
        ("get", "/runs/{id}"),
        ("get", "/runs/{id}/events"),
        ("get", "/runs/{id}/artifacts"),
        ("get", "/runs/{id}/handoff"),
        ("post", "/runs/{id}/stop"),
        ("get", "/pipelines"),
        ("post", "/pipelines"),
        ("get", "/pipelines/{id}"),
        ("get", "/pipelines/{id}/events"),
        ("post", "/pipelines/{id}/stop"),
        ("get", "/proposals"),
        ("post", "/proposals/{id}/merge"),
        ("post", "/proposals/{id}/reject"),
        ("get", "/events"),
    }
    actual = {
        (method, path)
        for path, item in spec["paths"].items()
        for method in HTTP_METHODS
        if isinstance(item.get(method), dict)
    }
    assert actual == expected


def test_board_contract_declares_the_recovery_fields(spec):
    # The page reads these; `additionalProperties: false` would make the real
    # response invalid the moment the daemon added them without the contract.
    board = spec["components"]["schemas"]["Board"]
    added = {"unverified_runs", "needs_you", "auto_recovering", "deterministic",
             "history_failures"}
    assert added <= set(board["properties"])
    assert added <= set(board["required"])
    counts = spec["components"]["schemas"]["DecisionCounts"]
    assert {"needs_you", "auto_recovering", "deterministic", "history_failed"} <= set(
        counts["required"]
    )


def test_openapi_yaml_is_served_without_a_token():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{httpd.server_port}/openapi.yaml"
        with urllib.request.urlopen(url, timeout=10) as response:
            body = response.read()
            assert response.status == 200
            assert response.headers["Content-Type"].startswith("application/yaml")
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=10)
    assert body == SPEC.read_bytes()
