"""
自动化测试配置读取。

设计原则：
1. 不强依赖 PyYAML 等额外库，配置文件使用 JSONC 写法，允许 `//` 与 `/* ... */` 注释。
2. 把 C2/H1 的网络参数、协议命令和准入阈值放入配置文件，避免现场换设备时改代码。
3. 保留 H1/H2 命名兼容：项目历史文件里有 h2 命名，但业务上按用户确认统一视为 H1。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Literal

RadarModel = Literal["c2", "h1"]


@dataclass(slots=True)
class ThresholdConfig:
    """测试准入阈值，单位在字段名中尽量显式标出，便于报告和现场复核。"""

    ping_success_rate_min_percent: float = 99.9
    protocol_success_rate_min_percent: float = 100.0
    c2_recovery_timeout_s: float = 10.0
    h1_recovery_timeout_s: float = 5.0
    stream_loss_rate_max_percent: float = 1.0
    h1_stream_loss_rate_max_percent: float = 0.5
    frame_rate_tolerance_percent: float = 10.0

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "ThresholdConfig":
        """从配置字典构造阈值；未知字段忽略，避免后续扩展配置时破坏旧脚本。"""
        if not raw:
            return cls()
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in raw.items() if k in allowed})


@dataclass(slots=True)
class PingConfig:
    """HW-03 持续 ping 测试配置，用于统计成功率、RTT 均值、最大值和抖动。"""

    duration_s: float = 604800.0
    pytest_duration_s: float = 10.0
    interval_s: float = 1.0
    timeout_ms: int = 1000
    packet_size: int = 32

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "PingConfig":
        """读取 ping 配置；正式测试默认 7 天，Pytest 默认只跑短样本避免阻塞开发。"""
        if not raw:
            return cls()
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in raw.items() if k in allowed})


@dataclass(slots=True)
class StreamConfig:
    """连续取数相关配置，覆盖 C2/H1 第一阶段共同需要的统计维度。"""

    sample_cycles: int = 5
    sample_duration_s: float = 10.0
    expected_points_per_scan: int = 2701
    expected_packets_per_scan: int = 22
    scan_start_deg: float = -45.0
    angle_resolution_deg: float = 0.1
    expected_frame_rate_hz: float = 30.0
    raw_capture_enabled: bool = False
    raw_capture_max_frames: int = 10000

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "StreamConfig":
        """读取取数配置，默认值按 H1/C2 常见 270°、0.1°、30Hz 测试场景设置。"""
        if not raw:
            return cls()
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in raw.items() if k in allowed})


@dataclass(slots=True)
class CommandConfig:
    """
    设备协议命令配置。

    H1 依据 H1E0-02A 说明书第 4 章表 4-2，C2 第一版先按现有 C200 压力测试脚本中
    已验证的启动/停止/配置查询命令执行。字段保存为带空格或不带空格的 hex 字符串均可。
    """

    h1_login_permission: int = 3
    h1_login_password_hex: str = "F4 72 47 44"
    single_scan_hex: str = "02 02 02 02 00 09 02 30 43"
    start_stream_hex: str = "02 02 02 02 00 0A 02 31 01 46"
    stop_stream_hex: str = "02 02 02 02 00 0A 02 31 00 45"
    c2_query_config_hex: str = "02 02 02 02 00 09 00 1A 2B"
    # H1 参数读（现场 C225 脚本；与 c2_query_config_hex 中 0x1A 读转速/角分辨率相同）
    read_ip_hex: str = "02 02 02 02 00 09 00 10 21"
    read_gateway_hex: str = "02 02 02 02 00 09 00 12 23"
    read_subnet_mask_hex: str = "02 02 02 02 00 09 00 14 25"
    read_freq_resolution_hex: str = "02 02 02 02 00 09 00 1A 2B"

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "CommandConfig":
        """读取命令配置；命令来自现场脚本时通常是 hex 文本，所以统一延后转 bytes。"""
        if not raw:
            return cls()
        allowed = {field.name for field in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in raw.items() if k in allowed})


@dataclass(slots=True)
class DeviceConfig:
    """单台雷达的自动化测试配置。"""

    model: RadarModel
    host: str
    port: int = 2111
    name: str = "radar"
    connect_timeout_s: float = 5.0
    recv_timeout_s: float = 0.5
    report_dir: str = "network_test/automation/reports_output"
    raw_dir: str = "network_test/automation/raw_output"
    commands: CommandConfig = field(default_factory=CommandConfig)
    ping: PingConfig = field(default_factory=PingConfig)
    stream: StreamConfig = field(default_factory=StreamConfig)
    thresholds: ThresholdConfig = field(default_factory=ThresholdConfig)

    @property
    def normalized_model(self) -> RadarModel:
        """把历史 h2 叫法归一到 h1，防止旧配置或旧文件名影响后续测试判断。"""
        return "h1" if str(self.model).lower() in {"h1", "h2"} else "c2"

    @property
    def recovery_timeout_s(self) -> float:
        """按型号返回恢复时间准入阈值：方案建议 C2 10 秒，H1 5 秒。"""
        if self.normalized_model == "h1":
            return float(self.thresholds.h1_recovery_timeout_s)
        return float(self.thresholds.c2_recovery_timeout_s)

    @property
    def stream_loss_limit_percent(self) -> float:
        """按型号返回应用层丢包/缺包准入阈值，H1 默认更严格。"""
        if self.normalized_model == "h1":
            return float(self.thresholds.h1_stream_loss_rate_max_percent)
        return float(self.thresholds.stream_loss_rate_max_percent)


def clean_hex(hex_text: str) -> str:
    """清理命令中的空格、换行和 0x 前缀，保证 `bytes.fromhex` 能稳定解析。"""
    return (
        hex_text.replace("0x", "")
        .replace("0X", "")
        .replace(" ", "")
        .replace("\n", "")
        .replace("\t", "")
    )


def hex_to_bytes(hex_text: str) -> bytes:
    """把现场文档或脚本里常见的 hex 文本转换为字节命令。"""
    cleaned = clean_hex(hex_text)
    if len(cleaned) % 2 != 0:
        raise ValueError(f"hex 命令长度必须为偶数: {hex_text!r}")
    return bytes.fromhex(cleaned)


def strip_json_comments(text: str) -> str:
    """
    去除 JSONC 注释后交给标准库 `json.loads`。

    这里按字符扫描而不是简单正则替换，避免把 hex 字符串或 Windows 路径中的 `//`
    误当成注释删除。支持 `// 行注释` 与 `/* 块注释 */`。
    """
    out: list[str] = []
    i = 0
    in_string = False
    escaped = False
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "/":
            i += 2
            while i < len(text) and text[i] not in "\r\n":
                i += 1
            continue
        if ch == "/" and nxt == "*":
            i += 2
            while i + 1 < len(text) and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def load_device_config(path: str | Path) -> DeviceConfig:
    """
    从 JSON/JSONC 文件读取设备配置。

    配置文件只描述一台设备，批量测试时由命令行或 CI 分多次调用，避免多个设备之间
    的失败原因互相干扰。
    """
    p = Path(path)
    raw = json.loads(strip_json_comments(p.read_text(encoding="utf-8")))

    model = str(raw.get("model", "")).lower()
    if model == "h2":
        model = "h1"
    if model not in {"c2", "h1"}:
        raise ValueError("model 必须是 c2、h1 或历史兼容写法 h2")

    return DeviceConfig(
        model=model,  # type: ignore[arg-type]
        host=str(raw["host"]),
        port=int(raw.get("port", 2111)),
        name=str(raw.get("name", model.upper())),
        connect_timeout_s=float(raw.get("connect_timeout_s", 5.0)),
        recv_timeout_s=float(raw.get("recv_timeout_s", 0.5)),
        report_dir=str(raw.get("report_dir", "network_test/automation/reports_output")),
        raw_dir=str(raw.get("raw_dir", "network_test/automation/raw_output")),
        commands=CommandConfig.from_dict(raw.get("commands")),
        ping=PingConfig.from_dict(raw.get("ping")),
        stream=StreamConfig.from_dict(raw.get("stream")),
        thresholds=ThresholdConfig.from_dict(raw.get("thresholds")),
    )
