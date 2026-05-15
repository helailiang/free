"""
协议响应测试。

H1：登录后读 IP/网关/掩码/转速角分辨率（PROT-01/02 子集）；
C2：旧 C200 脚本中的 0x1A 配置查询。
目标：命令能发、有应答、关键字段能解析或记录原始长度。
"""

from __future__ import annotations

import pytest

from network_test.automation.clients.base import RadarClientError
from network_test.automation.clients.h1_client import H1RadarClient

from .conftest import attach_metrics


@pytest.mark.integration
def test_protocol_query_has_reply(radar_client, radar_config, request: pytest.FixtureRequest) -> None:
    """验证设备对基础协议查询有非空应答，作为 PROT-02 第一版准入。"""
    try:
        radar_client.connect()
        if radar_config.normalized_model == "h1":
            assert isinstance(radar_client, H1RadarClient)
            params = radar_client.read_network_params()
            attach_metrics(request.node, {"model": "h1", **params.to_dict()})
            assert params.has_any_reply(), "H1 参数读均无原始应答"
            assert params.has_parsed_value(), "H1 参数读有应答但均未解析成功，请核对固件应答格式"
        else:
            reply = radar_client.query_config()
            attach_metrics(
                request.node,
                {
                    "model": radar_config.normalized_model,
                    "reply_bytes": len(reply),
                    "reply_hex_prefix": reply[:64].hex(" ").upper(),
                },
            )
            assert reply, "协议查询无应答"
    except RadarClientError as exc:
        pytest.fail(f"协议查询失败: {exc}")
