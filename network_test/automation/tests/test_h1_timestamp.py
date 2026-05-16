from __future__ import annotations

import math

from network_test.automation.clients.base import _parse_h1_tail_timestamp


def test_parse_h1_tail_timestamp_skips_cccc_marker() -> None:
    point_count = 128
    frame = (
        b"\x00" * 22
        + b"\x00" * (4 * point_count)
        + bytes.fromhex("CC CC 00 00 03 1D A2 CB F3 7D")
        + b"\x98"
    )

    timestamp = _parse_h1_tail_timestamp(frame, point_count)

    expected = 797 + int("A2CBF37D", 16) / 2**32
    assert math.isclose(timestamp, expected)
