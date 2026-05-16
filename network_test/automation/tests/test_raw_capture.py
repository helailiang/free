from __future__ import annotations

import json

from network_test.automation.metrics import PacketInfo
from network_test.automation.raw_capture import RawFrameCapture


def test_raw_frame_capture_writes_jsonl_and_respects_frame_limit(tmp_path) -> None:
    capture = RawFrameCapture(tmp_path, model="h1", host="192.168.1.86", max_frames=1)
    packet = PacketInfo(scan_id=7, packet_id=3, point_count=120, raw_length=4)

    capture.write_frame(
        frame_index=0,
        received_at_s=123.456789,
        frame=b"\x02\x02\x00\x04",
        packet=packet,
    )
    capture.write_frame(
        frame_index=1,
        received_at_s=124.0,
        frame=b"\x02\x02\x00\x04",
        packet=None,
    )
    capture.close()

    lines = capture.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert capture.frames_written == 1
    assert capture.truncated is True

    row = json.loads(lines[0])
    assert row["frame_index"] == 0
    assert row["received_at_s"] == 123.456789
    assert row["raw_length"] == 4
    assert row["parsed"] is True
    assert row["packet"]["scan_id"] == 7
    assert row["packet"]["packet_id"] == 3
    assert row["frame_hex"] == "02 02 00 04"
