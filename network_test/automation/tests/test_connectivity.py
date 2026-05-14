"""
基础连通性测试。

该文件覆盖两层连通性：
1. HW-03 的 ping/RTT 短样本统计，用于快速确认成功率和 RTT 字段可采集。
2. TCP 端口与 H1 登录，用于后续协议和取数用例的前置确认。

正式 HW-03 仍应通过 runner 的 `--mode ping --duration-s 604800` 执行 7 天测试。
"""

from __future__ import annotations

import time

import pytest

from network_test.automation.clients.base import RadarClientError
from network_test.automation.ping import run_ping_test

from .conftest import attach_metrics


@pytest.mark.integration
def test_hw03_ping_rtt_sample(radar_config, request: pytest.FixtureRequest) -> None:
    """执行 HW-03 ping 短样本，统计成功率、平均 RTT、最大 RTT 和抖动。"""
    stats = run_ping_test(radar_config, duration_s=float(radar_config.ping.pytest_duration_s))
    metrics = stats.to_dict()
    attach_metrics(request.node, metrics)

    assert stats.sent > 0, "未发送任何 ping 包"
    assert stats.received > 0, "未收到任何 ping 应答"
    assert stats.success_rate_percent >= radar_config.thresholds.ping_success_rate_min_percent, (
        f"ping 成功率 {stats.success_rate_percent}% 低于阈值 "
        f"{radar_config.thresholds.ping_success_rate_min_percent}%"
    )
    assert stats.rtt_avg_ms is not None, "未统计到平均 RTT"
    assert stats.rtt_avg_ms < 2.0, f"平均 RTT {stats.rtt_avg_ms}ms 不满足局域网 <2ms 建议标准"


@pytest.mark.integration
def test_tcp_connectivity_and_session(radar_client, radar_config, request: pytest.FixtureRequest) -> None:
    """验证雷达 TCP 端口可连接，H1 还会在 `connect()` 内完成 4.2.1 登录。"""
    started = time.monotonic()
    try:
        radar_client.connect()
    except RadarClientError as exc:
        pytest.fail(f"TCP 连接或会话初始化失败: {exc}")

    attach_metrics(
        request.node,
        {
            "model": radar_config.normalized_model,
            "host": radar_config.host,
            "port": radar_config.port,
            "connect_elapsed_s": round(time.monotonic() - started, 4),
            "recovery_timeout_s": radar_config.recovery_timeout_s,
        },
    )
