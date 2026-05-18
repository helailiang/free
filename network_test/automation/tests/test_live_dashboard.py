from __future__ import annotations

import json

from network_test.automation.live_dashboard import LiveDashboardSession


def test_live_dashboard_session_writes_snapshot(tmp_path) -> None:
    session = LiveDashboardSession(tmp_path, model="h1", host="192.168.1.86", preferred_port=0)

    session.update({"frames_received": 12, "loss_rate_percent": 0.0}, status="running")

    payload = json.loads(session.snapshot_path.read_text(encoding="utf-8"))
    assert payload["status"] == "running"
    assert payload["metrics"]["frames_received"] == 12
    assert payload["metrics"]["loss_rate_percent"] == 0.0
