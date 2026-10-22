"""Choreography: one mood becomes a short sequence of clips, not one clip.

Pure planning tests only — no pet, no window, no sleeping in the test suite.
"""

from __future__ import annotations

from nodus import mood, pet


def test_plan_falls_back_to_the_single_animation(monkeypatch):
    monkeypatch.setattr(pet, "pet_for", lambda agent="": "claude")
    monkeypatch.setattr(pet, "anim_for", lambda m, agent="": "待机-翻书")
    beats = mood.plan("done", "claude", beats={})
    assert beats == [("待机-翻书", mood.TAIL)]


def test_plan_uses_per_character_beats(monkeypatch):
    monkeypatch.setattr(pet, "pet_for", lambda agent="": "claude")
    table = {"claude": {"done": ["欢快舞蹈", ["优雅转圈", 1.5]]}}
    beats = mood.plan("done", "claude", beats=table)
    assert [name for name, _ in beats] == ["欢快舞蹈", "优雅转圈"]
    assert beats[1][1] == 1.5


def test_plan_ignores_beats_for_another_character(monkeypatch):
    monkeypatch.setattr(pet, "pet_for", lambda agent="": "gemini")
    monkeypatch.setattr(pet, "anim_for", lambda m, agent="": "左右观察")
    beats = mood.plan("done", "gemini", beats={"claude": {"done": ["欢快舞蹈"]}})
    assert beats == [("左右观察", mood.TAIL)]


def test_plan_is_empty_when_nothing_is_known(monkeypatch):
    monkeypatch.setattr(pet, "pet_for", lambda agent="": "nobody")
    monkeypatch.setattr(pet, "anim_for", lambda m, agent="": "")
    assert mood.plan("unknown-mood", "", beats={}) == []


def test_play_reports_the_planned_names(monkeypatch):
    monkeypatch.setattr(pet, "pet_for", lambda agent="": "claude")
    monkeypatch.setattr(mood, "plan", lambda m, a="", beats=None: [("欢快舞蹈", 0.1), ("优雅转圈", 0.1)])
    played: list[str] = []
    monkeypatch.setattr(pet, "animate", lambda name, agent="": played.append(name) or True)
    monkeypatch.setattr(mood.time, "sleep", lambda _s: None)  # no real sleeping
    names = mood.play("done", "claude", background=False)
    assert names == ["欢快舞蹈", "优雅转圈"]
    assert played == names
