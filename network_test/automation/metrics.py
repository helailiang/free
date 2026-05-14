"""
雷达数据流指标统计。

统计逻辑不关心底层是 C2 还是 H1，只要求客户端把每个数据包整理成统一的
`PacketInfo`。这样后续新增其它单线雷达型号时，只需要写新的客户端解析层，
报告和 Pytest 判定仍然可以复用。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
import time
from typing import Any


@dataclass(slots=True)
class PacketInfo:
    """单个应用层数据包的最小统计信息。"""

    scan_id: int
    packet_id: int
    point_count: int
    timestamp: int = 0
    raw_length: int = 0
    received_at_s: float = field(default_factory=time.monotonic)
    parse_source: str = "protocol"


@dataclass(slots=True)
class StreamStats:
    """一次连续取数窗口的汇总指标，直接进入 JSON/CSV/HTML 报告。"""

    model: str
    host: str
    started_at_s: float
    ended_at_s: float
    frames_received: int
    scans_seen: int
    completed_scans: int
    points_received: int
    parse_errors: int
    duplicate_packets: int
    missing_packets: int
    expected_packets: int
    loss_rate_percent: float
    frame_rate_hz: float
    max_inter_frame_gap_s: float
    reconnect_count: int = 0
    longest_data_gap_s: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """转换为基础类型字典，便于 JSON 序列化和报告模板复用。"""
        return asdict(self)


class StreamMetrics:
    """
    连续取数统计器。

    统计器只保存每圈已收到的包号集合，不保存完整点云，避免 72 小时长稳时内存持续增长。
    点云原始数据如需排查，可由客户端单独落盘到 raw 目录。
    """

    def __init__(self, *, model: str, host: str, expected_packets_per_scan: int) -> None:
        self.model = model
        self.host = host
        self.expected_packets_per_scan = max(0, int(expected_packets_per_scan))
        self.started_at_s = time.monotonic()
        self.ended_at_s = self.started_at_s
        self.frames_received = 0
        self.points_received = 0
        self.parse_errors = 0
        self.duplicate_packets = 0
        self.reconnect_count = 0
        self.notes: list[str] = []
        self._scan_packets: dict[int, set[int]] = {}
        self._packet_keys: set[tuple[int, int]] = set()
        self._receive_times: list[float] = []

    def add_packet(self, packet: PacketInfo) -> None:
        """记录一个解析成功的数据包，并更新缺包统计需要的 scan/packet 索引集合。"""
        self.frames_received += 1
        self.points_received += max(0, int(packet.point_count))
        self.ended_at_s = packet.received_at_s
        self._receive_times.append(packet.received_at_s)

        key = (int(packet.scan_id), int(packet.packet_id))
        if key in self._packet_keys:
            self.duplicate_packets += 1
        self._packet_keys.add(key)
        self._scan_packets.setdefault(int(packet.scan_id), set()).add(int(packet.packet_id))

    def add_parse_error(self, note: str | None = None) -> None:
        """记录协议帧长度、校验、字段范围等解析失败事件。"""
        self.parse_errors += 1
        if note:
            self.notes.append(note)

    def add_reconnect(self) -> None:
        """记录长稳或恢复测试中的一次主动/被动重连。"""
        self.reconnect_count += 1

    def finish(self) -> StreamStats:
        """计算最终统计值，所有除法都做空数据保护，避免设备未连接时二次报错。"""
        now = time.monotonic()
        self.ended_at_s = max(self.ended_at_s, now)
        duration = max(0.001, self.ended_at_s - self.started_at_s)
        scans_seen = len(self._scan_packets)

        completed_scans = 0
        missing_packets = 0
        expected_packets = 0
        if self.expected_packets_per_scan > 0:
            for packets in self._scan_packets.values():
                expected_packets += self.expected_packets_per_scan
                missing = max(0, self.expected_packets_per_scan - len(packets))
                missing_packets += missing
                if missing == 0:
                    completed_scans += 1

        if expected_packets > 0:
            loss_rate_percent = missing_packets / expected_packets * 100.0
        else:
            loss_rate_percent = 0.0

        gaps = [
            b - a
            for a, b in zip(self._receive_times, self._receive_times[1:])
            if math.isfinite(b - a) and b >= a
        ]
        max_gap = max(gaps) if gaps else 0.0

        return StreamStats(
            model=self.model,
            host=self.host,
            started_at_s=self.started_at_s,
            ended_at_s=self.ended_at_s,
            frames_received=self.frames_received,
            scans_seen=scans_seen,
            completed_scans=completed_scans,
            points_received=self.points_received,
            parse_errors=self.parse_errors,
            duplicate_packets=self.duplicate_packets,
            missing_packets=missing_packets,
            expected_packets=expected_packets,
            loss_rate_percent=round(loss_rate_percent, 4),
            frame_rate_hz=round(self.frames_received / duration, 4),
            max_inter_frame_gap_s=round(max_gap, 4),
            reconnect_count=self.reconnect_count,
            longest_data_gap_s=round(max_gap, 4),
            notes=self.notes[:],
        )
