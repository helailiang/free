"""
H1 参数读应答解析（表 4-2 应答操作码 0x12）。

依据 H1E0-02A 说明书第 4 章与现场脚本 `c2_h1/C225指令测试.py` 中的读命令；
IPv4 类参数取应答参数区前 4 字节；转速/角分辨率（指令 0x1A）为两个大端 uint16，
分别除以 100 与 10000。若固件与说明书不一致，以抓包为准并调整本模块偏移。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import struct
from typing import Any

from libs.protocols.h2_txt_parse import H2_OP_REPLY, split_h2_text_frames

# 现场 C225 脚本中已验证的读指令号（操作码 0x00 发、0x12 回）
H1_CMD_READ_IP = 0x10
H1_CMD_READ_GATEWAY = 0x12
H1_CMD_READ_SUBNET_MASK = 0x14
H1_CMD_READ_FREQ_RESOLUTION = 0x1A


def find_reply_frame(raw: bytes, cmd_id: int) -> bytes | None:
    """
    在可能含粘包的原始接收数据中提取第一条匹配的完整应答帧。

    `cmd_id` 为请求时的指令号；应答帧内同位置应出现 `H2_OP_REPLY` + `cmd_id`。
    """
    return raw
    for frame in split_h2_text_frames(raw):
        if len(frame) < 9:
            continue
        if frame[6] == H2_OP_REPLY and frame[7] == (cmd_id & 0xFF):
            return frame
    return None


def parse_ipv4_param(raw: bytes, cmd_id: int) -> str | None:
    """解析读 IP/网关/子网掩码类应答：参数区前 4 字节为点分十进制。"""
    print("get ip:", raw.hex())
    frame = find_reply_frame(raw, cmd_id)
    if frame is None or len(frame) < 13:
        return None
    # 参数区在指令号之后、校验和之前
    params = frame[8:-1]
    print("params:", params)
    if len(params) < 4:
        return None
    return ".".join(str(b) for b in params[:4])


def parse_freq_and_resolution(raw: bytes) -> tuple[float | None, float | None]:
    """
    解析读转速/角分辨率（0x1A）应答。

    现场写帧约定：转速 Hz = uint16_be / 100；角分辨率 ° = uint16_be / 10000。
    """
    frame = find_reply_frame(raw, H1_CMD_READ_FREQ_RESOLUTION)
    if frame is None or len(frame) < 13:
        return None, None
    params = frame[8:-1]
    if len(params) < 4:
        return None, None
    freq_raw, res_raw = struct.unpack_from(">HH", params, 0)
    spin_hz = freq_raw / 100.0
    angle_resolution_deg = res_raw / 10000.0
    return spin_hz, angle_resolution_deg


@dataclass(slots=True)
class H1NetworkParams:
    """一次 `read_network_params()` 汇总结果，供 Pytest 报告与 runner 冒烟使用。"""

    # 读 IP（指令 0x10）解析出的点分地址；None 表示未解析到合法帧
    ip: str | None = None
    # 读网关（0x12）
    gateway: str | None = None
    # 读子网掩码（0x14）
    subnet_mask: str | None = None
    # 读 0x1A 得到的转速，单位 Hz
    spin_hz: float | None = None
    # 读 0x1A 得到的角分辨率，单位 °/点
    angle_resolution_deg: float | None = None
    # 各读命令原始应答字节数，便于排查「有应答但解析失败」
    raw_lengths: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """扁平化为 JSON 可序列化字典。"""
        return asdict(self)

    def has_any_reply(self) -> bool:
        """至少有一条读命令收到非空原始数据。"""
        return any(n > 0 for n in self.raw_lengths.values())

    def has_parsed_value(self) -> bool:
        """至少成功解析出一项业务字段。"""
        return any(
            v is not None
            for v in (self.ip, self.gateway, self.subnet_mask, self.spin_hz, self.angle_resolution_deg)
        )
