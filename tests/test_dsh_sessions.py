"""DSH session reading: the store is zstd JSONL, and we only ever read it."""

from __future__ import annotations

import json

import pytest
import zstandard

from nodus import dsh_sessions as ds

pytest.importorskip("zstandard")

NOW = 1_791_000_000_000


def make_session(root, project: str, sid: str, rows: list[dict]) -> None:
    folder = root / project / sid
    folder.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"
    with (folder / ds.FILE_NAME).open("wb") as f:
        with zstandard.ZstdCompressor().stream_writer(f) as writer:
            writer.write(payload.encode("utf-8"))


def sample_rows(sid: str = "session-abc123") -> list[dict]:
    return [
        {
            "type": "session",
            "version": 4,
            "id": sid,
            "createdAt": NOW,
            "cwd": "C:\\work\\demo",
            "agentPreset": "standard",
        },
        {"type": "session/title", "seq": 1, "time": NOW, "data": {"title": "测试会话"}},
        {
            "type": "user/message",
            "seq": 2,
            "time": NOW + 1000,
            "data": {
                "content": [{"type": "text", "text": "看看这个 https://example.com/x 并读 C:\\Users\\me\\a.md"}],
                "source": {"kind": "user"},
            },
        },
        {
            "type": "user/message",
            "seq": 3,
            "time": NOW + 2000,
            "data": {
                "content": [{"type": "text", "text": "系统提醒噪音，不应出现"}],
                "source": {"kind": "system-reminder"},
            },
        },
        {
            "type": "assistant/message",
            "seq": 4,
            "time": NOW + 3000,
            "data": {
                "message": {
                    "content": [
                        {"type": "reasoning", "text": "内部推理，不应默认出现"},
                        {"type": "text", "text": "读完了，结论在报告里。"},
                    ]
                }
            },
        },
    ]


@pytest.fixture()
def root(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(ds, "ROOT", sessions)
    return sessions


def test_list_sessions_reads_meta_and_title(root):
    make_session(root, "--C-work--", "session-abc123", sample_rows())
    items = ds.list_sessions()
    assert len(items) == 1
    item = items[0]
    assert item["id"] == "session-abc123"
    assert item["title"] == "测试会话"
    assert item["cwd"] == "C:\\work\\demo"
    assert item["user_messages"] == 1


def test_list_sessions_query_filters(root):
    make_session(root, "--C-work--", "session-abc123", sample_rows())
    assert ds.list_sessions(query="不存在的词") == []
    assert ds.list_sessions(query="测试")[0]["id"] == "session-abc123"


def test_messages_skip_system_reminders_and_reasoning(root):
    make_session(root, "--C-work--", "session-abc123", sample_rows())
    _, rows = ds.load("session-abc123")
    msgs = ds.messages(rows)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert "噪音" not in "\n".join(m["text"] for m in msgs)
    assert "内部推理" not in "\n".join(m["text"] for m in msgs)


def test_messages_include_reasoning_on_request(root):
    make_session(root, "--C-work--", "session-abc123", sample_rows())
    _, rows = ds.load("session-abc123")
    msgs = ds.messages(rows, include_reasoning=True)
    assert [m["role"] for m in msgs] == ["user", "assistant", "reasoning"]


def test_find_accepts_unique_prefix(root):
    make_session(root, "--C-work--", "session-abc123", sample_rows())
    assert ds.find("abc123").parent.name == "session-abc123"


def test_find_rejects_ambiguous_prefix(root):
    make_session(root, "--C-work--", "session-aaa111", sample_rows("session-aaa111"))
    make_session(root, "--C-work--", "session-aaa222", sample_rows("session-aaa222"))
    with pytest.raises(ValueError, match="ambiguous"):
        ds.find("aaa")


def test_find_unknown_raises(root):
    with pytest.raises(ValueError, match="not found"):
        ds.find("nope")


def test_transcript_renders_dialogue(root):
    make_session(root, "--C-work--", "session-abc123", sample_rows())
    text = ds.transcript("session-abc123")
    assert "测试会话" in text
    assert "看看这个" in text
    assert "读完了" in text
    assert "系统提醒噪音" not in text


def test_brief_keeps_source_words_and_refs(root):
    make_session(root, "--C-work--", "session-abc123", sample_rows())
    text = ds.brief("session-abc123", "把课程截止时间合并进学习计划")
    assert "把课程截止时间合并进学习计划" in text
    assert "来源会话：session-abc123" in text
    assert "https://example.com/x" in text
    assert "C:\\Users\\me\\a.md" in text
    assert "未改写" in text
    assert "系统提醒噪音" not in text


def test_write_brief_creates_file(root, tmp_path):
    make_session(root, "--C-work--", "session-abc123", sample_rows())
    out = tmp_path / "brief.md"
    path = ds.write_brief("session-abc123", "任务", out=str(out))
    assert path == out
    assert "任务" in path.read_text(encoding="utf-8")
