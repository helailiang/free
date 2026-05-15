"""
H1（历史文件名中也可能写作 H2）网络通信客户端。

协议依据：H1E0-02A 产品说明书第 4 章、表 4-2、4.2.1 登录、
4.2.22 单次数据、4.2.23 连续请求数据开关，以及 C225 现场读参数命令。
这里不使用 C3 的 HOST-ARM 二进制协议。
"""

from __future__ import annotations

import time

from libs.protocols.h2_txt_parse import H2_TEXT_SOF
from network_test.automation.config import hex_to_bytes

from .base import BaseRadarClient, RadarClientError
from .h1_param_parse import (
    H1NetworkParams,
    H1_CMD_READ_FREQ_RESOLUTION,
    H1_CMD_READ_GATEWAY,
    H1_CMD_READ_IP,
    H1_CMD_READ_SUBNET_MASK,
    parse_freq_and_resolution,
    parse_ipv4_param,
)

# 4.2.23 连续请求数据「开」的成功应答（表 4-2：操作码 0x12、指令 0x31、参数 0x01、校验 0x56）。
H1_START_STREAM_ACK = bytes([0x02, 0x02, 0x02, 0x02, 0x00, 0x0A, 0x12, 0x31, 0x01, 0x56])


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


def build_h1_read_frame(cmd_id: int, params: bytes = b"") -> bytes:
    """
    构造表 4-2「读」帧：操作码 0x00 + 指令号 + 0~9 个参数 + 校验和。

    无参数读（如读 IP）时 `params` 为空；有参数读时在配置里写完整 hex 或在此传入。
    """
    payload = bytes([0x00, cmd_id & 0xFF]) + params
    length = 4 + 2 + len(payload) + 1
    head = H2_TEXT_SOF + length.to_bytes(2, "big")
    body = head + payload
    return body + bytes([checksum8(body)])


class H1RadarClient(BaseRadarClient):
    """H1 TCP 文本帧客户端：登录、参数读、单次/连续取数。"""

    # 连续发多条读命令时，两条之间的间隔（秒），避免应答粘在一起难以切帧
    _READ_CMD_GAP_S: float = 0.05

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

    def read_parameter(self, config_key: str, *, recv_size: int = 256) -> bytes:
        """
        按 `DeviceConfig.commands` 中的字段名发送一条读命令。

        新增指令测试时：先在 JSON 增加 `read_xxx_hex`，再调用
        `read_parameter("read_xxx_hex")` 或封装 `read_xxx()` 方法。
        """
        reply = self.send_command(
            self.command_bytes(config_key),
            expect_reply=True,
            recv_size=recv_size,
        )
        time.sleep(self._READ_CMD_GAP_S)
        return reply

    def read_ip(self) -> bytes:
        """读设备 IP（指令 0x10，默认 hex 见 `read_ip_hex`）。"""
        return self.read_parameter("read_ip_hex")

    def read_gateway(self) -> bytes:
        """读网关（指令 0x12）。"""
        return self.read_parameter("read_gateway_hex")

    def read_subnet_mask(self) -> bytes:
        """读子网掩码（指令 0x14）。"""
        return self.read_parameter("read_subnet_mask_hex")

    def read_freq_and_resolution(self) -> bytes:
        """读转速与角分辨率（指令 0x1A）；应答可能较短，接收缓冲与 C2 脚本一致取 545。"""
        return self.read_parameter("read_freq_resolution_hex", recv_size=545)

    def read_network_params(self) -> H1NetworkParams:
        """
        依次读取 IP/网关/掩码/转速角分辨率并解析为结构化结果。

        PROT-01/02 与 runner 冒烟优先使用本方法；需要原始 hex 时可对各 `read_*` 单独调用。
        """
        raw_ip = self.read_ip()
        raw_gw = self.read_gateway()
        raw_mask = self.read_subnet_mask()
        raw_freq = self.read_freq_and_resolution()
        spin_hz, angle_res = parse_freq_and_resolution(raw_freq)
        return H1NetworkParams(
            ip=parse_ipv4_param(raw_ip, H1_CMD_READ_IP),
            gateway=parse_ipv4_param(raw_gw, H1_CMD_READ_GATEWAY),
            subnet_mask=parse_ipv4_param(raw_mask, H1_CMD_READ_SUBNET_MASK),
            spin_hz=spin_hz,
            angle_resolution_deg=angle_res,
            raw_lengths={
                "read_ip": len(raw_ip),
                "read_gateway": len(raw_gw),
                "read_subnet_mask": len(raw_mask),
                "read_freq_resolution": len(raw_freq),
            },
        )

    def query_config(self) -> bytes:
        """
        聚合多条参数读命令的原始应答（兼容 `BaseRadarClient.query_config` 签名）。

        解析请使用 `read_network_params()`；点云协议活性请用 `probe_protocol()`。
        """
        parts = [
            self.read_ip(),
            self.read_gateway(),
            self.read_subnet_mask(),
            self.read_freq_and_resolution(),
        ]
        return b"".join(parts)

    def probe_protocol(self) -> bytes:
        """4.2.22 单次点云，用于验证「能出数」而非读配置寄存器。"""
        return self.request_single_scan()

    def request_single_scan(self) -> bytes:
        """发送 4.2.22 单次数据请求，返回首段原始应答供协议冒烟测试使用。"""
        return self.send_command(self.command_bytes("single_scan_hex"), expect_reply=True, recv_size=65536)

    def start_streaming(self) -> None:
        """
        发送 4.2.23 连续请求数据开命令，并校验短应答。

        现场与说明书约定成功应答为 `02 02 02 02 00 0A 12 31 01 56`（10 字节）；
        允许接收缓冲中含少量前导残留，以子串方式匹配。
        """
        recv = self.send_command(self.command_bytes("start_stream_hex"), expect_reply=True, recv_size=128)
        if H1_START_STREAM_ACK not in recv:
            got = recv.hex(" ").upper() if recv else "(空)"
            want = H1_START_STREAM_ACK.hex(" ").upper()
            raise RadarClientError(f"H1 连续取数开失败：期望应答含 {want}，实际 {got}")

    def stop_streaming(self) -> None:
        """发送 4.2.23 连续请求数据关命令；停止失败时让测试报告暴露异常。"""
        self.send_command(self.command_bytes("stop_stream_hex"), expect_reply=False)
