"""
协议响应测试。

H1 使用登录后的单次数据请求作为协议冒烟；C2 使用旧 C200 压力测试脚本中的配置查询
命令。目标不是覆盖所有参数读写，而是先保证“命令能发、设备有应答、返回长度可记录”。
"""

from __future__ import annotations

import pytest

from network_test.automation.clients.base import RadarClientError

from .conftest import attach_metrics


@pytest.mark.integration
def test_protocol_query_has_reply(radar_client, radar_config, request: pytest.FixtureRequest) -> None:
    """验证设备对基础协议查询有非空应答，作为 PROT-02 第一版准入。"""
    try:
        radar_client.connect()
        reply = radar_client.query_config()
    except RadarClientError as exc:
        pytest.fail(f"协议查询失败: {exc}")

    attach_metrics(
        request.node,
        {
            "model": radar_config.normalized_model,
            "reply_bytes": len(reply),
            "reply_hex_prefix": reply[:64].hex(" ").upper(),
        },
    )
    assert reply, "协议查询无应答"
