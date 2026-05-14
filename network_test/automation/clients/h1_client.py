"""
H1（历史文件名中也可能写作 H2）网络通信客户端。

协议依据：H1E0-02A 产品说明书第 4 章、表 4-2、4.2.1 登录、
4.2.22 单次数据和 4.2.23 连续请求数据开关。这里不使用 C3 的 HOST-ARM 二进制协议。
"""

from __future__ import annotations

from libs.protocols.h2_txt_parse import H2_TEXT_SOF
from network_test.automation.config import hex_to_bytes

from .base import BaseRadarClient, RadarClientError


def checksum8(body_without_final_checksum: bytes) -> int:
    """H1 文本帧校验：文本结束字节为前面所有字节求和后的低 8 位。"""
    return sum(body_without_final_checksum) & 0xFF


def build_h1_login_frame(permission: int, password4: bytes) -> bytes:
    """
    构造 H1 4.2.1 登录帧。

    `permission` 为权限等级，`password4` 为 4 字节密码；默认值来自仓库现有
    `码盘补偿/h2_radar_client.py`，该脚本已用于 H1/H2 单次点云测试。
    """
    if len(password4) != 4:
        raise ValueError("H1 登录密码必须是 4 字节")
    payload = bytes([0x02, 0x01, permission & 0xFF]) + password4
    length = 4 + 2 + len(payload) + 1
    head = H2_TEXT_SOF + length.to_bytes(2, "big")
    body = head + payload
    return body + bytes([checksum8(body)])


class H1RadarClient(BaseRadarClient):
    """H1 TCP 文本帧客户端，实现登录、单次/连续取数相关命令。"""

    def after_connect(self) -> None:
        """
        建立 TCP 后立即登录。

        说明书 4.2.1 规定业务操作需要先登录；现有脚本通过应答中包含
        `12 01 01` 判断登录成功，因此这里沿用该现场验证特征。
        """
        self.drain_socket()
        password = hex_to_bytes(self.config.commands.h1_login_password_hex)
        frame = build_h1_login_frame(int(self.config.commands.h1_login_permission), password)
        reply = self.send_command(frame, expect_reply=True)
        if b"\x12\x01\x01" not in reply:
            raise RadarClientError("H1 登录失败：应答中未检测到成功特征 12 01 01")
        self.drain_socket()

    def request_single_scan(self) -> bytes:
        """发送 4.2.22 单次数据请求，返回首段原始应答供协议冒烟测试使用。"""
        return self.send_command(self.command_bytes("single_scan_hex"), expect_reply=True, recv_size=65536)

    def start_streaming(self) -> None:
        """发送 4.2.23 连续请求数据开命令，读取短应答后进入数据流接收。"""
        self.send_command(self.command_bytes("start_stream_hex"), expect_reply=True, recv_size=128)

    def stop_streaming(self) -> None:
        """发送 4.2.23 连续请求数据关命令；停止失败时让测试报告暴露异常。"""
        self.send_command(self.command_bytes("stop_stream_hex"), expect_reply=False)

    def query_config(self) -> bytes:
        """
        H1 第一版配置查询使用单次数据请求做协议活性验证。

        后续如需覆盖 IP、网关、子网掩码、角分辨率读取，可在配置中增加对应 4.2.x 命令。
        """
        return self.request_single_scan()
