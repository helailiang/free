from __future__ import annotations

import math

from network_test.automation.metrics import PacketInfo, StreamMetrics


def _add_scan(
    metrics: StreamMetrics,
    scan_id: int,
    packet_ids: list[int],
    *,
    timestamp: float = 0.0,
    completed_wall_at_s: float = 0.0,
) -> None:
    for offset, packet_id in enumerate(packet_ids):
        metrics.add_packet(
            PacketInfo(
                scan_id=scan_id,
                packet_id=packet_id,
                point_count=1,
                timestamp=timestamp,
                received_wall_at_s=completed_wall_at_s + offset * 0.001,
            )
        )


def test_finish_ignores_trailing_partial_scan_for_loss_rate() -> None:
    metrics = StreamMetrics(model="h1", host="127.0.0.1", expected_packets_per_scan=4)
    _add_scan(metrics, 1, [0, 1, 2, 3])
    _add_scan(metrics, 2, [0, 1, 2, 3])
    _add_scan(metrics, 3, [0])

    stats = metrics.finish()

    assert stats.frames_received == 9
    assert stats.scans_seen == 3
    assert stats.completed_scans == 2
    assert stats.loss_evaluated_scans == 2
    assert stats.boundary_partial_scans_ignored == 1
    assert stats.expected_packets == 8
    assert stats.missing_packets == 0
    assert stats.loss_rate_percent == 0.0


def test_finish_counts_internal_partial_scan_as_loss() -> None:
    metrics = StreamMetrics(model="h1", host="127.0.0.1", expected_packets_per_scan=4)
    _add_scan(metrics, 1, [0, 1, 2, 3])
    _add_scan(metrics, 2, [0, 1, 3])
    _add_scan(metrics, 3, [0, 1, 2, 3])

    stats = metrics.finish()

    assert stats.scans_seen == 3
    assert stats.completed_scans == 2
    assert stats.loss_evaluated_scans == 3
    assert stats.boundary_partial_scans_ignored == 0
    assert stats.expected_packets == 12
    assert stats.missing_packets == 1
    assert stats.loss_rate_percent == 8.3333


def test_finish_ignores_leading_and_trailing_partial_scans_for_loss_rate() -> None:
    metrics = StreamMetrics(model="h1", host="127.0.0.1", expected_packets_per_scan=4)
    _add_scan(metrics, 1, [2, 3])
    _add_scan(metrics, 2, [0, 1, 2, 3])
    _add_scan(metrics, 3, [0])

    stats = metrics.finish()

    assert stats.frames_received == 7
    assert stats.scans_seen == 3
    assert stats.completed_scans == 1
    assert stats.loss_evaluated_scans == 1
    assert stats.boundary_partial_scans_ignored == 2
    assert stats.expected_packets == 4
    assert stats.missing_packets == 0
    assert stats.loss_rate_percent == 0.0


def test_finish_reports_complete_scan_intervals_from_timestamp_and_wall_clock() -> None:
    metrics = StreamMetrics(model="h1", host="127.0.0.1", expected_packets_per_scan=4)
    _add_scan(metrics, 1, [0, 1, 2, 3], timestamp=10.0, completed_wall_at_s=100.0)
    _add_scan(metrics, 2, [0, 1, 2, 3], timestamp=10.05, completed_wall_at_s=100.066)
    _add_scan(metrics, 3, [0, 1, 2, 3], timestamp=10.1, completed_wall_at_s=100.132)

    stats = metrics.finish()

    assert stats.scan_timestamp_interval_count == 2
    assert math.isclose(stats.scan_timestamp_interval_avg_s, 0.05)
    assert math.isclose(stats.scan_timestamp_interval_latest_s, 0.05)
    assert stats.scan_timestamp_interval_avg_display == "50.000 ms"
    assert stats.scan_timestamp_interval_latest_display == "50.000 ms"
    assert stats.completed_scan_wall_interval_count == 2
    assert math.isclose(stats.completed_scan_wall_interval_avg_s, 0.066)
    assert math.isclose(stats.completed_scan_wall_interval_latest_s, 0.066)
    assert stats.completed_scan_wall_interval_avg_display == "66.000 ms"
    assert stats.completed_scan_wall_interval_latest_display == "66.000 ms"


def test_finish_does_not_bridge_missing_scan_timestamps() -> None:
    metrics = StreamMetrics(model="h1", host="127.0.0.1", expected_packets_per_scan=4)
    _add_scan(metrics, 1, [0, 1, 2, 3], timestamp=10.0, completed_wall_at_s=100.0)
    _add_scan(metrics, 2, [0, 1, 2, 3], timestamp=0.0, completed_wall_at_s=100.066)
    _add_scan(metrics, 3, [0, 1, 2, 3], timestamp=10.1, completed_wall_at_s=100.132)

    stats = metrics.finish()

    assert stats.scan_timestamp_interval_count == 0
    assert stats.scan_timestamp_interval_avg_display == ""
    assert stats.completed_scan_wall_interval_count == 2
