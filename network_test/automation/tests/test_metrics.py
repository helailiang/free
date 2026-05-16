from __future__ import annotations

from network_test.automation.metrics import PacketInfo, StreamMetrics


def _add_scan(metrics: StreamMetrics, scan_id: int, packet_ids: list[int]) -> None:
    for packet_id in packet_ids:
        metrics.add_packet(PacketInfo(scan_id=scan_id, packet_id=packet_id, point_count=1))


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
