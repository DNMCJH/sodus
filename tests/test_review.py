"""Review parsing and context assembly. No network, no agents."""

from __future__ import annotations

import hashlib
import json

from nodus import review


def test_extract_plain_json():
    obj = review.extract_json_object('{"verdict": "accept", "findings": []}')
    assert obj == {"verdict": "accept", "findings": []}


def test_extract_fenced_json():
    text = '先说一句。\n```json\n{"verdict": "revise", "findings": []}\n```\n完。'
    assert review.extract_json_object(text)["verdict"] == "revise"


def test_extract_json_wrapped_in_prose_with_braces():
    text = '结论 {见下}: {"verdict": "reject", "findings": [{"severity": "high"}]} 谢'
    obj = review.extract_json_object(text)
    assert obj["verdict"] == "reject"
    assert obj["findings"][0]["severity"] == "high"


def test_extract_json_returns_none_for_prose_only():
    assert review.extract_json_object("我觉得还行，没有 JSON。") is None


def test_normalize_sorts_by_severity_and_counts():
    out = review.normalize(
        {
            "verdict": "Revise",
            "summary": " 有问题 ",
            "findings": [
                {"severity": "low", "file": "b.py", "issue": "minor"},
                {"severity": "HIGH", "file": "a.py", "line": "7", "issue": "oops"},
                {"severity": "nonsense", "file": "c.py", "issue": "?"},
            ],
            "test_gaps": ["没有负数用例", "  "],
        }
    )
    assert out["verdict"] == "revise"
    assert out["summary"] == "有问题"
    assert [f["severity"] for f in out["findings"]] == ["high", "medium", "low"]
    assert out["findings"][0]["line"] == 7
    assert out["counts"] == {"critical": 0, "high": 1, "medium": 1, "low": 1}
    assert out["test_gaps"] == ["没有负数用例"]


def test_normalize_rejects_unknown_verdict_but_keeps_findings():
    out = review.normalize({"verdict": "LGTM", "findings": []})
    assert out["verdict"] == "unknown"
    assert out["findings"] == []


def test_normalize_none_is_unparsed():
    out = review.normalize(None)
    assert out["verdict"] == "unparsed"
    assert out["counts"] == {"critical": 0, "high": 0, "medium": 0, "low": 0}


def test_file_context_is_relative_and_bounded(tmp_path):
    target = tmp_path / "calc.py"
    target.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    ctx = review._file_context(
        tmp_path, [{"path": str(target), "rel_path": "calc.py", "kind": "file"}]
    )
    assert ctx["mode"] == "files"
    assert "### calc.py" in ctx["text"]
    assert "return a + b" in ctx["text"]
    assert ctx["files"] == ["calc.py"]
    assert ctx["truncated"] is False


def test_file_context_marks_drift_from_run_record(tmp_path):
    target = tmp_path / "calc.py"
    target.write_text("v2", encoding="utf-8")
    stale = hashlib.sha256(b"v1").hexdigest()[:16]
    ctx = review._file_context(
        tmp_path, [{"path": str(target), "rel_path": "calc.py", "hash": stale, "kind": "file"}]
    )
    assert "当前内容与 run 记录不一致" in ctx["text"]

    current = hashlib.sha256(b"v2").hexdigest()[:16]
    ctx = review._file_context(
        tmp_path, [{"path": str(target), "rel_path": "calc.py", "hash": current, "kind": "file"}]
    )
    assert "不一致" not in ctx["text"]


def test_file_context_skips_binary_and_missing(tmp_path):
    binary = tmp_path / "blob.bin"
    binary.write_bytes(b"\xff\xfe\x00\x01")
    ctx = review._file_context(
        tmp_path,
        [
            {"path": str(binary), "kind": "file"},
            {"path": str(tmp_path / "gone.py"), "kind": "file"},
        ],
    )
    assert ctx["text"] == ""
    assert ctx["files"] == []


def test_render_md_surfaces_findings_and_verdict():
    data = {
        "task_title": "demo",
        "reviewer": "claude",
        "review_run": "run-2",
        "target_run": "run-1",
        "target_agent": "codex",
        "verdict": "revise",
        "summary": "边界没处理",
        "counts": {"critical": 0, "high": 1, "medium": 0, "low": 0},
        "findings": [
            {
                "severity": "high",
                "file": "calc.py",
                "line": 3,
                "issue": "空字符串返回 None",
                "evidence": "return None",
                "suggestion": "返回 ''",
            }
        ],
        "test_gaps": ["空输入"],
        "context_mode": "files",
        "context_files": ["calc.py"],
    }
    md = review.render_md(data)
    assert "revise" in md
    assert "[HIGH] calc.py:3" in md
    assert "空字符串返回 None" in md
    assert "空输入" in md


def test_render_md_round_trips_through_json():
    data = {
        "task_title": "demo",
        "reviewer": "claude",
        "review_run": "run-2",
        "target_run": "run-1",
        "target_agent": "codex",
        "verdict": "accept",
        "summary": "",
        "counts": {"critical": 0, "high": 0, "medium": 0, "low": 0},
        "findings": [],
        "test_gaps": [],
        "context_mode": "git",
        "context_files": [],
    }
    assert "accept" in review.render_md(json.loads(json.dumps(data)))
