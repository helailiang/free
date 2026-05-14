"""
连续取数与数据完整性测试。

该用例对应 PROT-02、NET-01 中的应用层数据连续性统计。第一版按配置中的圈数或时长
读取数据，重点检查是否有帧、解析错误和缺包率是否超过阈值。
"""

from __future__ import annotations

import pytest

from network_test.automation.clients.base import RadarClientError

from .conftest import attach_metrics


@pytest.mark.integration
def test_stream_sample_quality(radar_client, radar_config, request: pytest.FixtureRequest) -> None:
    """短时间启动连续取数，统计帧数、圈数、缺包率和解析错误。"""
    try:
        radar_client.connect()
        radar_client.start_streaming()
        stats = radar_client.read_stream_stats(
            duration_s=float(radar_config.stream.sample_duration_s),
            max_cycles=int(radar_config.stream.sample_cycles),
        )
        radar_client.stop_streaming()
    except RadarClientError as exc:
        pytest.fail(f"连续取数失败: {exc}")

    metrics = stats.to_dict()
    attach_metrics(request.node, metrics)

    assert stats.frames_received > 0, "未收到连续取数数据帧"
    assert stats.parse_errors == 0, f"存在 {stats.parse_errors} 个解析失败帧"
    assert stats.loss_rate_percent <= radar_config.stream_loss_limit_percent, (
        f"缺包率 {stats.loss_rate_percent}% 超过阈值 {radar_config.stream_loss_limit_percent}%"
    )
