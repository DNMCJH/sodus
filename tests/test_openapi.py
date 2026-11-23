"""The OpenAPI contract must stay honest about what `server.py` serves.

PyYAML is not a runtime dependency of Sodus. It is present in the development
environment, so these checks run there; where it is absent the module skips
instead of failing an environment that cannot read the file at all.
"""

from __future__ import annotations

import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from sodus import server

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
    # These four loopback metadata endpoints are reachable without the token.
    # Everything else inherits the global bearer requirement.
    expected = {
        ("get", "/"),
        ("get", "/health"),
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
        ("get", "/handoff"),
        ("get", "/handoffs"),
        ("get", "/handoff/check"),
        ("get", "/handoff/decisions"),
        ("get", "/"),
        ("get", "/health"),
        ("get", "/favicon.ico"),
        ("get", "/openapi.yaml"),
        ("get", "/state"),
        ("get", "/cost/today"),
        ("get", "/memory/harvest"),
        ("get", "/memory/candidates"),
        ("post", "/memory/remember"),
        ("get", "/agents"),
        ("get", "/providers"),
        ("get", "/routes"),
        ("get", "/profiles"),
        ("get", "/tasks"),
        ("post", "/tasks"),
        ("post", "/reconcile"),
        ("post", "/plan"),
        ("get", "/plans/{id}"),
        ("post", "/plans/{id}/review"),
        ("post", "/plans/{id}/execute"),
        ("post", "/plans/{id}/replan"),
        ("post", "/plans/{id}/approve"),
        ("post", "/ask"),
        ("get", "/asks"),
        ("get", "/ask/{id}"),
        ("post", "/ask/{id}/promote"),
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
        ("post", "/memory/adopt"),
        ("get", "/events"),
    }
    actual = {
        (method, path)
        for path, item in spec["paths"].items()
        for method in HTTP_METHODS
        if isinstance(item.get(method), dict)
    }
    assert actual == expected


def test_retired_routes_and_schemas_absent_from_openapi(spec):
    for retired in ("/pet", "/board", "/llm/chat"):
        assert retired not in spec["paths"]
    for schema_name in ("Board", "PetState", "LlmChatRequest", "LlmChatResult", "LlmError"):
        assert schema_name not in spec["components"]["schemas"]


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


def test_state_contract_is_authenticated_and_structured(spec):
    operation = spec["paths"]["/state"]["get"]
    assert operation.get("security", spec["security"]) == [{"bearerAuth": []}]
    schema = spec["components"]["schemas"]["State"]
    assert set(schema["required"]) == {"version", "workspace_id", "now", "running", "counts", "today", "asks", "memory"}
    assert schema["properties"]["running"]["maxItems"] == 20
    for name in ("counts", "today", "asks", "memory"):
        item = schema["properties"][name]
        assert set(item["required"]) == set(item["properties"])


def test_plan_execution_contract(spec):
    for action in ('execute', 'replan'):
        operation = spec['paths']['/plans/{id}/' + action]['post']
        assert {'202', '400', '401', '403', '404'} <= operation['responses'].keys()
    assert spec['paths']['/plans/{id}/replan']['post']['requestBody']['content']['application/json']['schema']['required'] == ['feedback']


def test_plan_contract(spec):
    assert spec['paths']['/plan']['post']['responses'].get('202')
    approve = spec['paths']['/plans/{id}/approve']['post']
    assert approve['deprecated'] is True
    assert set(approve['responses']) == {'403', '401'}
    assert approve['responses']['403']['content']['application/json']['schema']['properties']['error']['const'] == 'approve_requires_cli'
    assert spec['components']['schemas']['PlanExecution']['properties']['review_json']['properties']['verdict']['enum'] == ['approve', 'revise', 'reject']
