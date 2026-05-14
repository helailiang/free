"""
基础连通性测试。

该文件覆盖测试方案中的 HW-03：先验证 TCP 端口可达，再由各型号客户端完成必要的
登录或会话初始化。Ping 因 Windows/Linux 输出格式差异较大，第一版先以 TCP 可达作为
自动化准入，Ping 可在长稳阶段作为人工环境记录补充。
"""

from __future__ import annotations

import time

import pytest

from network_test.automation.clients.base import RadarClientError

from .conftest import attach_metrics


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
