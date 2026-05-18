"""Smoke: start live dashboard, push metrics, poll /metrics.json — not part of pytest."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from network_test.automation.live_dashboard import LiveDashboardSession  # noqa: E402


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    sess = LiveDashboardSession(
        tmp,
        model="h1",
        host="127.0.0.1",
        bind_host="127.0.0.1",
        preferred_port=8765,
    ).start()
    print("dashboard_url:", sess.url)
    if ":8765/" not in sess.url:
        print("WARN: 首选 8765 被占用，实际 URL 见上；浏览器请打开打印的 dashboard_url")

    stop = threading.Event()

    def pump() -> None:
        n = 0
        while not stop.is_set() and n < 40:
            n += 1
            sess.update(
                {
                    "frames_received": n * 10,
                    "loss_rate_percent": 0.01 * (n % 7),
                    "model": "h1",
                    "host": "192.168.1.1",
                    "window_duration_s": float(n),
                    "completed_scans": n,
                    "stream_loss_limit_percent_applied": 0.5,
                },
                status="running",
            )
            time.sleep(0.35)

    th = threading.Thread(target=pump, daemon=True)
    th.start()

    prev_fr: object | None = None
    ok_steps = 0
    base = sess.url.rstrip("/")
    for i in range(10):
        time.sleep(0.5)
        with urlopen(base + "/metrics.json?ts=" + str(time.time())) as r:
            payload = json.loads(r.read().decode("utf-8"))
        m = payload.get("metrics") or {}
        fr = m.get("frames_received")
        ua = payload.get("updated_at")
        print(f"poll {i + 1}: updated_at={ua!r} frames_received={fr!r}")
        if prev_fr is not None and fr != prev_fr:
            ok_steps += 1
        prev_fr = fr

    stop.set()
    th.join(timeout=2.0)
    sess.close()

    print("---")
    if ok_steps >= 5:
        print("RESULT: OK（多次轮询间 frames_received 有变化；用浏览器打开 dashboard_url 应每秒看到刷新）")
        return 0
    print("RESULT: FAIL（变化次数不足）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
