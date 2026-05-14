"""
C2/H1 雷达通信客户端工厂。

测试用例只通过 `create_radar_client` 获取统一对象，避免在 Pytest 中散落型号判断。
"""

from __future__ import annotations

from network_test.automation.config import DeviceConfig

from .base import BaseRadarClient
from .c2_client import C2RadarClient
from .h1_client import H1RadarClient


def create_radar_client(config: DeviceConfig) -> BaseRadarClient:
    """按配置型号创建客户端；历史 h2 命名在配置层已归一为 h1。"""
    if config.normalized_model == "h1":
        return H1RadarClient(config)
    return C2RadarClient(config)


__all__ = ["BaseRadarClient", "C2RadarClient", "H1RadarClient", "create_radar_client"]
