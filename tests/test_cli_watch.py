"""The CLI watch loop must survive an idle event connection.

A worker running a two-minute shell command leaves the event endpoint quiet;
the socket timeout used to abort the CLI while the run itself kept going.
"""

from __future__ import annotations

from sodus.cli import client


def test_watch_survives_an_idle_event_connection(monkeypatch):
    calls: list[str] = []

    def fake_call(method, path, body=None, retries=2):
        calls.append(path)
        if len(calls) == 1:
            raise TimeoutError("timed out")
        if path.endswith("/events?after=0"):
            return [{"seq": 1, "type": "message", "payload": {"text": "hi"}}]
        return {"id": "run-x", "status": "completed"}

    monkeypatch.setattr(client, "call", fake_call)
    monkeypatch.setattr(client.time, "sleep", lambda seconds: None)

    assert client.watch("run-x") == 0
    assert any(path.endswith("/events?after=0") for path in calls)
