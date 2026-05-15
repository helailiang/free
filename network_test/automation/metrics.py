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

    # 扫描圈号（scan_cnt）；H1 来自点云帧参数区，C2 来自 legacy 偏移 9:11，大端。
    scan_id: int
    # 圈内分包序号（seq_num / pack_index）；与 expected_packets_per_scan 一起算缺包。
    packet_id: int
    # 本包解析出的测距点数 N；长稳只累计个数，不保留点坐标以省内存。
    point_count: int
    # 设备时间戳（大端整数）；H1 点云尾 10 字节，无则为 0，报告里可选展示。
    timestamp: float = 0
    # 原始 TCP 帧字节长度；排查「帧过短/过长」时对照协议长度字段。
    raw_length: int = 0
    # 本机收到该包的时刻（time.monotonic 秒）；用于算帧间隔与 max_inter_frame_gap_s。
    received_at_s: float = field(default_factory=time.monotonic)
    # 解析来源标识，如 h1_text_frame / c2_legacy_offsets；区分 H1 正式解析与 C2 兜底。
    parse_source: str = "protocol"


@dataclass(slots=True)
class StreamStats:
    """一次连续取数窗口的汇总指标，直接进入 JSON/CSV/HTML 报告。"""

    # 设备型号（c2/h1），与配置文件 normalized_model 一致。
    model: str
    # 被测雷达 IP，与配置 host 一致。
    host: str
    # 统计窗口开始时刻（monotonic 秒）；非墙钟时间。
    started_at_s: float
    # 统计窗口结束时刻（monotonic 秒）；ended - started ≈ 采样时长。
    ended_at_s: float
    # 成功解析并计入统计的帧总数；对应 StreamMetrics.frames_received。
    frames_received: int
    # 观测到的不重复 scan_id 个数；反映「见过多少圈」。
    scans_seen: int
    # 包号齐全、无缺包的完整圈数；需配置 expected_packets_per_scan > 0 才有意义。
    completed_scans: int
    # 各帧 point_count 之和；粗看点云吞吐，不替代点数正确性校验。
    points_received: int
    # 无法按协议解析的帧次数；>0 时优先查粘包、校验或固件版本。
    parse_errors: int
    # 相同 (scan_id, packet_id) 重复到达次数；网络重传或重复订阅时上升。
    duplicate_packets: int
    # 按每圈应有包数推算的缺包总数；与 loss_rate_percent 分子一致。
    missing_packets: int
    # 理论应收包总数 = 圈数 × expected_packets_per_scan；作缺包率分母。
    expected_packets: int
    # 缺包率（%）= missing_packets / expected_packets × 100；与 thresholds 中 stream 阈值比对。
    loss_rate_percent: float
    # 平均帧率（Hz）= frames_received / 窗口秒数；反映数据流是否持续。
    frame_rate_hz: float
    # 相邻两帧接收时间间隔的最大值（秒）；突增可能表示卡顿或断流。
    max_inter_frame_gap_s: float
    # 长稳/恢复测试中记录的重连次数；由 StreamMetrics.add_reconnect 累加。
    reconnect_count: int = 0
    # 与 max_inter_frame_gap_s 同源，保留别名便于报告字段兼容旧版。
    longest_data_gap_s: float = 0.0
    # 人工或脚本附带的说明字符串列表，如解析失败摘要、现场备注。
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

    def completed_scan_count(self) -> int:
        """
        当前已收满 expected_packets_per_scan 个不同包号的圈数。

        用于 `max_cycles` 停止条件：必须收满整圈再计数，不能「见到新圈号首包」就停，
        否则最后一圈会被误判缺 21 包（21/110 ≈ 19.09%）。
        """
        if self.expected_packets_per_scan <= 0:
            return len(self._scan_packets)
        need = self.expected_packets_per_scan
        return sum(1 for packets in self._scan_packets.values() if len(packets) >= need)

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
