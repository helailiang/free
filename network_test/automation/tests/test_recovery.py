"""
基础恢复能力测试。

该用例不自动拔网线，而是模拟“应用层断开后重新建立连接”的恢复流程。真实拔网线、
交换机重启和 IP 冲突仍建议在长稳 runner 中通过人工事件记录配合完成。
"""

from __future__ import annotations

import time

import pytest

from network_test.automation.clients import create_radar_client
from network_test.automation.clients.base import RadarClientError

from .conftest import attach_metrics


@pytest.mark.integration
def test_application_reconnect_within_threshold(radar_config, request: pytest.FixtureRequest) -> None:
    """主动连接、关闭、再连接，验证应用层重连耗时不超过方案阈值。"""
    first_client = create_radar_client(radar_config)
    second_client = create_radar_client(radar_config)
    try:
        first_client.connect()
        first_client.close()

        started = time.monotonic()
        second_client.connect()
        elapsed = time.monotonic() - started
    except RadarClientError as exc:
        pytest.fail(f"应用层重连失败: {exc}")
    finally:
        first_client.close()
        second_client.close()

    attach_metrics(
        request.node,
        {
            "model": radar_config.normalized_model,
            "reconnect_elapsed_s": round(elapsed, 4),
            "threshold_s": radar_config.recovery_timeout_s,
            "note": "该用例验证应用层主动重连，物理断网恢复需用长稳 runner + 人工事件记录。",
        },
    )
    assert elapsed <= radar_config.recovery_timeout_s
