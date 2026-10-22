"""Hidden checks for render-deployment: structure, formatting, error path."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

WORK = Path(__file__).resolve().parent.parent

EXPECTED = {
    "name": "checkout",
    "image": "checkout:latest",
    "replicas": 3,
    "ports": ["8080:8080", "8443:8443"],
    "env": {"LOG_LEVEL": "info", "REGION": "cn-east"},
    "features": {"retry": True, "timeout_s": 30},
    "healthcheck": {"path": "/health", "port": 8080},
}


def load_render():
    sys.path.insert(0, str(WORK))
    try:
        import render
    finally:
        sys.path.pop(0)
    return render


def test_render_structure():
    render = load_render()
    cfg = json.loads((WORK / "config.json").read_text(encoding="utf-8"))
    assert render.render(cfg) == EXPECTED


def test_render_drops_unknown_keys_and_uses_first_port():
    render = load_render()
    cfg = {
        "service": "api",
        "replicas": 1,
        "ports": [9000],
        "features": {},
        "environment": {},
        "owner": "someone",
        "debug": True,
    }
    assert render.render(cfg) == {
        "name": "api",
        "image": "api:latest",
        "replicas": 1,
        "ports": ["9000:9000"],
        "env": {},
        "features": {},
        "healthcheck": {"path": "/health", "port": 9000},
    }


def test_cli_writes_sorted_indented_json_with_one_newline():
    result = subprocess.run(
        [sys.executable, str(WORK / "render.py"), str(WORK / "config.json")],
        cwd=str(WORK),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0
    assert result.stdout.endswith("\n") and not result.stdout.endswith("\n\n")
    text = result.stdout[:-1]
    assert text.splitlines()[1].startswith('  "env":')
    assert json.loads(text) == EXPECTED
    assert "\n  \"image\"" in text


@pytest.mark.parametrize("name,body", [("missing.json", None), ("bad.json", "{not json")])
def test_bad_input_exits_1_with_an_error_line(tmp_path, name, body):
    path = tmp_path / name
    if body is not None:
        path.write_text(body, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(WORK / "render.py"), str(path)],
        cwd=str(WORK),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 1
    assert result.stderr.strip().startswith("error:")
    assert result.stdout.strip() == ""
