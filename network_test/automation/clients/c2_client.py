"""
C2 网络通信客户端。

第一版 C2 实现以现有 `C200压力测试(连续取数+通断网).py` 为依据：默认端口 2111，
连续取数命令 `02 02 02 02 00 0A 02 31 01 46`，停止命令
`02 02 02 02 00 0A 02 31 00 45`，配置查询命令
`02 02 02 02 00 09 00 1A 2B`。如果后续拿到 C2 正式协议文档，可在本文件替换兜底解析。
"""

from __future__ import annotations

from .base import BaseRadarClient


class C2RadarClient(BaseRadarClient):
    """C2 TCP 客户端，保留与旧 GUI 压力测试脚本一致的命令序列。"""

    def start_streaming(self) -> None:
        """
        启动连续取数。

        旧脚本启动后立即 `recv(20)` 读取短应答；这里同样读取短应答，防止应答帧混入
        后续数据流统计。
        """
        self.send_command(self.command_bytes("start_stream_hex"), expect_reply=True, recv_size=128)

    def stop_streaming(self) -> None:
        """停止连续取数；停止命令不强制等待应答，避免设备已断开时阻塞退出。"""
        self.send_command(self.command_bytes("stop_stream_hex"), expect_reply=False)

    def query_config(self) -> bytes:
        """
        查询 C2 扫描配置。

        旧脚本以返回 hex 长度 26 作为“参数获取成功”的粗判据；自动化用例会进一步
        检查是否收到非空应答，并把长度写入报告。
        """
        return self.send_command(self.command_bytes("c2_query_config_hex"), expect_reply=True, recv_size=545)
