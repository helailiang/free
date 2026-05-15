"""
HW-03 网络基础连通性 ping/RTT 统计。

测试方案要求 HW-03 执行持续 ping，并记录成功率、平均延迟和最大延迟。该模块使用系统
自带 `ping` 命令，兼容 Windows 中文输出和常见英文输出；正式 7 天测试由 runner 的
`ping` 模式执行，Pytest 仅运行短样本以便快速回归。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import locale
import platform
import re
import subprocess
import time
from typing import Any

from network_test.automation.config import DeviceConfig


PING_METRIC_EXPLANATIONS: dict[str, str] = {
    "host": "被测雷达 IP 地址，也就是本次 ping 的目标设备。",
    "planned_duration_s": "计划测试时长，单位秒。正式 HW-03 建议 604800 秒，即 7 天。",
    "interval_s": "两次 ping 之间的间隔，单位秒；1.0 表示约每秒 ping 一次。",
    "timeout_ms": "单次 ping 等待应答的最长时间，单位毫秒；超过该时间算一次丢包。",
    "packet_size": "ping 负载大小，单位字节；用于固定每个 ICMP 包的数据长度。",
    "sent": "已发送 ping 包数量。",
    "received": "已收到应答的 ping 包数量。",
    "lost": "未收到应答的 ping 包数量，计算方式为 sent - received。",
    "success_rate_percent": "ping 成功率，计算方式为 received / sent * 100；HW-03 默认要求不低于 99.9%。",
    "rtt_min_ms": "最小 RTT，单位毫秒；表示最快一次往返耗时。",
    "rtt_avg_ms": "平均 RTT，单位毫秒；表示本次测试的平均网络往返延迟，局域网建议小于 2ms。",
    "rtt_max_ms": "最大 RTT，单位毫秒；表示最慢一次往返耗时，用来观察偶发网络卡顿。",
    "jitter_ms": "RTT 抖动，单位毫秒；按相邻两次 RTT 差值的平均值估算，越小说明延迟越稳定。",
    "started_at_s": "程序内部单调时钟开始时间，主要用于计算耗时，不是北京时间。",
    "ended_at_s": "程序内部单调时钟结束时间，ended_at_s - started_at_s 约等于实际运行时长。",
    "errors": "执行系统 ping 命令时发生的异常列表；空列表表示脚本执行层面没有异常。",
}


@dataclass(slots=True)
class PingStats:
    """一次 HW-03 ping 测试的统计结果。"""

    # 被测设备 IP；与配置 host 一致，报告里用于核对是否 ping 错目标。
    host: str
    # 计划跑满的总时长（秒）；短测用 pytest_duration_s，正式 HW-03 常用 604800（7 天）。
    planned_duration_s: float
    # 两次 ping 之间的间隔（秒）；越小采样越密，对网络和本机负载越高。
    interval_s: float
    # 单次 ping 等待应答的超时（毫秒）；超时应答记为丢包，与系统 ping -w 对齐。
    timeout_ms: int
    # ICMP 负载字节数；与系统 ping -l/-s 对齐，用于固定包长便于对比不同环境。
    packet_size: int
    # 已发出的 ping 次数（每轮循环 +1）；分母，用于算成功率。
    sent: int = 0
    # 已从 ping 输出解析到 RTT 的次数；有 RTT 视为收到应答，分子近似为 received。
    received: int = 0
    # 丢包数 = sent - received；排查链路时先看 lost 是否随交换机/网线动作上升。
    lost: int = 0
    # 成功率（%）= received / sent * 100；与 thresholds.ping_success_rate_min_percent 比对。
    success_rate_percent: float = 0.0
    # 本次所有成功样本中的最小 RTT（毫秒）；反映最好情况下的往返延迟。
    rtt_min_ms: float | None = None
    # 平均 RTT（毫秒）；局域网准入常看此项是否长期 <2ms。
    rtt_avg_ms: float | None = None
    # 最大 RTT（毫秒）；偶发尖峰、排队或 WiFi 抖动会拉高此项。
    rtt_max_ms: float | None = None
    # 相邻两次 RTT 差值绝对值的平均（毫秒）；越大说明延迟波动越大，非 ICMP 协议抖动。
    jitter_ms: float | None = None
    # 统计开始时刻：time.monotonic() 秒值，仅用于算墙钟无关的耗时差，不是日期时间。
    started_at_s: float = field(default_factory=time.monotonic)
    # 统计结束时刻：同上；ended_at_s - started_at_s ≈ 实际占用时长（含 sleep 与 ping 耗时）。
    ended_at_s: float = field(default_factory=time.monotonic)
    # 调系统 ping 子进程异常时的错误信息列表；空表示子进程调用层面无异常（仍可能有丢包）。
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转换为报告可序列化字典。"""
        return asdict(self)


_RTT_PATTERNS = [
    re.compile(r"(?:time|时间)\s*[=<]\s*(\d+(?:\.\d+)?)\s*ms", re.IGNORECASE),
    re.compile(r"时间\s*[=<]\s*(\d+(?:\.\d+)?)\s*毫秒", re.IGNORECASE),
]


def _ping_command(host: str, *, timeout_ms: int, packet_size: int) -> list[str]:
    """构造单次 ping 命令；循环由 Python 控制，便于按 interval 和 duration 做长期统计。"""
    if platform.system().lower().startswith("win"):
        return ["ping", "-n", "1", "-w", str(timeout_ms), "-l", str(packet_size), host]
    timeout_s = max(1, int(round(timeout_ms / 1000)))
    return ["ping", "-c", "1", "-W", str(timeout_s), "-s", str(packet_size), host]


def _extract_rtt_ms(output: str) -> float | None:
    """
    从 ping 输出中提取单包 RTT。

    Windows 中文系统可能输出 `时间=1ms` 或 `时间<1ms`，英文系统常见 `time=1.23 ms`。
    对 `<1ms` 按 1ms 计入，保守用于准入统计。
    """
    for pattern in _RTT_PATTERNS:
        match = pattern.search(output)
        if match:
            return float(match.group(1))
    return None


def run_ping_test(config: DeviceConfig, *, duration_s: float | None = None) -> PingStats:
    """
    执行 HW-03 ping/RTT 测试。

    统计字段包括发送数、接收数、成功率、RTT 最小/平均/最大和简单抖动（相邻 RTT 差值均值）。
    如果现场要严格执行 7 天测试，应使用配置中的 `ping.duration_s=604800` 或命令行覆盖。
    """
    planned_duration = float(duration_s if duration_s is not None else config.ping.duration_s)
    interval_s = max(0.1, float(config.ping.interval_s))
    timeout_ms = int(config.ping.timeout_ms)
    packet_size = int(config.ping.packet_size)
    stats = PingStats(
        host=config.host,
        planned_duration_s=planned_duration,
        interval_s=interval_s,
        timeout_ms=timeout_ms,
        packet_size=packet_size,
    )
    rtts: list[float] = []
    deadline = time.monotonic() + max(0.1, planned_duration)

    while time.monotonic() < deadline:
        loop_started = time.monotonic()
        stats.sent += 1
        cmd = _ping_command(config.host, timeout_ms=timeout_ms, packet_size=packet_size)
        try:
            result = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
                encoding=locale.getpreferredencoding(False),
                errors="ignore",
                timeout=max(1.0, timeout_ms / 1000 + 1.0),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            stats.errors.append(str(exc))
            result = None

        output = ""
        if result is not None:
            output = f"{result.stdout}\n{result.stderr}"
        rtt = _extract_rtt_ms(output)
        if rtt is not None:
            stats.received += 1
            rtts.append(rtt)

        sleep_s = interval_s - (time.monotonic() - loop_started)
        if sleep_s > 0 and time.monotonic() + sleep_s < deadline:
            time.sleep(sleep_s)

    stats.ended_at_s = time.monotonic()
    stats.lost = max(0, stats.sent - stats.received)
    stats.success_rate_percent = round((stats.received / stats.sent * 100.0) if stats.sent else 0.0, 4)
    if rtts:
        stats.rtt_min_ms = round(min(rtts), 4)
        stats.rtt_avg_ms = round(sum(rtts) / len(rtts), 4)
        stats.rtt_max_ms = round(max(rtts), 4)
        if len(rtts) > 1:
            diffs = [abs(b - a) for a, b in zip(rtts, rtts[1:])]
            stats.jitter_ms = round(sum(diffs) / len(diffs), 4)
        else:
            stats.jitter_ms = 0.0
    return stats
