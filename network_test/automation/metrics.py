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
    # 本机收到该包的墙钟时刻（Unix 秒）；用于按完整圈完成时刻估算圈间隔。
    received_wall_at_s: float = field(default_factory=time.time)
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
    # 参与缺包率统计的圈数；窗口首尾被截断的不完整圈不计入。
    loss_evaluated_scans: int
    # 因采样窗口边界截断而跳过缺包率统计的不完整圈数。
    boundary_partial_scans_ignored: int
    # 通过完整圈内设备时间戳计算出的相邻完整圈间隔数量。
    scan_timestamp_interval_count: int
    # 通过设备时间戳计算出的平均相邻完整圈间隔，单位秒。
    scan_timestamp_interval_avg_s: float
    # 通过设备时间戳计算出的最近两完整圈间隔，单位秒。
    scan_timestamp_interval_latest_s: float
    # 设备时间戳平均圈间隔的友好显示值；小于 1 秒时显示 ms。
    scan_timestamp_interval_avg_display: str
    # 设备时间戳最近圈间隔的友好显示值；小于 1 秒时显示 ms。
    scan_timestamp_interval_latest_display: str
    # 通过本机墙钟“完整圈收齐时刻”计算出的相邻完整圈间隔数量。
    completed_scan_wall_interval_count: int
    # 本机墙钟完整圈平均间隔，单位秒。
    completed_scan_wall_interval_avg_s: float
    # 本机墙钟最近两完整圈间隔，单位秒。
    completed_scan_wall_interval_latest_s: float
    # 本机墙钟完整圈平均间隔的友好显示值；小于 1 秒时显示 ms。
    completed_scan_wall_interval_avg_display: str
    # 本机墙钟最近圈间隔的友好显示值；小于 1 秒时显示 ms。
    completed_scan_wall_interval_latest_display: str
    # 各帧 point_count 之和；粗看点云吞吐，不替代点数正确性校验。
    points_received: int
    # 无法按协议解析的帧次数；>0 时优先查粘包、校验或固件版本。
    parse_errors: int
    # 相同 (scan_id, packet_id) 重复到达次数；网络重传或重复订阅时上升。
    duplicate_packets: int
    # 在可判定圈内按每圈应有包数推算的缺包总数；与 loss_rate_percent 分子一致。
    missing_packets: int
    # 理论应收包总数 = 参与缺包率统计的圈数 × expected_packets_per_scan。
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
    # 原始帧 JSONL 落盘路径；未开启抓包时为空字符串。
    raw_capture_path: str = ""
    # 实际写入原始帧文件的帧数。
    raw_frames_captured: int = 0
    # 原始帧抓取是否因 raw_capture_max_frames 上限而截断。
    raw_capture_truncated: bool = False

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
        self._scan_timestamps: dict[int, float] = {}
        self._completed_scan_ids: set[int] = set()
        self._completed_scan_wall_times: list[tuple[int, float]] = []
        self._completed_scan_device_timestamps: list[tuple[int, float | None]] = []
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
        scan_id = int(packet.scan_id)
        self._scan_packets.setdefault(scan_id, set()).add(int(packet.packet_id))
        if (
            scan_id not in self._scan_timestamps
            and math.isfinite(float(packet.timestamp))
            and float(packet.timestamp) > 0
        ):
            self._scan_timestamps[scan_id] = float(packet.timestamp)

        if (
            self.expected_packets_per_scan > 0
            and scan_id not in self._completed_scan_ids
            and len(self._scan_packets[scan_id]) >= self.expected_packets_per_scan
        ):
            self._completed_scan_ids.add(scan_id)
            self._completed_scan_wall_times.append((scan_id, float(packet.received_wall_at_s)))
            timestamp = self._scan_timestamps.get(scan_id)
            self._completed_scan_device_timestamps.append((scan_id, timestamp))

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

    def _loss_evaluation_scan_items(self) -> list[tuple[int, set[int]]]:
        """
        返回可用于缺包率统计的圈。

        按时间截断采样时，窗口首圈或末圈可能天然不完整，不能和中间圈的真实缺包混为一谈。
        Python dict 保留插入顺序，这里用“首次观察到的圈”和“最后观察到的圈”识别窗口边界。
        """
        scan_items = list(self._scan_packets.items())
        if self.expected_packets_per_scan <= 0 or not scan_items:
            return scan_items

        need = self.expected_packets_per_scan
        boundary_scan_ids: set[int] = set()
        first_scan_id, first_packets = scan_items[0]
        last_scan_id, last_packets = scan_items[-1]
        if len(first_packets) < need:
            boundary_scan_ids.add(first_scan_id)
        if len(last_packets) < need:
            boundary_scan_ids.add(last_scan_id)

        return [
            (scan_id, packets)
            for scan_id, packets in scan_items
            if scan_id not in boundary_scan_ids
        ]

    @staticmethod
    def _format_interval(value_s: float) -> str:
        """格式化时间间隔：小于 1 秒时用 ms，否则用 s。"""
        if not math.isfinite(value_s) or value_s < 0:
            return ""
        if value_s < 1.0:
            return f"{value_s * 1000.0:.3f} ms"
        return f"{value_s:.4f} s"

    @classmethod
    def _interval_summary(cls, values: list[float | None]) -> dict[str, object]:
        """汇总相邻完整圈时间间隔。"""
        intervals = [
            b - a
            for a, b in zip(values, values[1:])
            if a is not None and b is not None and math.isfinite(a) and math.isfinite(b) and b >= a
        ]
        if not intervals:
            return {
                "count": 0,
                "avg_s": 0.0,
                "latest_s": 0.0,
                "avg_display": "",
                "latest_display": "",
            }
        avg_s = sum(intervals) / len(intervals)
        latest_s = intervals[-1]
        return {
            "count": len(intervals),
            "avg_s": round(avg_s, 6),
            "latest_s": round(latest_s, 6),
            "avg_display": cls._format_interval(avg_s),
            "latest_display": cls._format_interval(latest_s),
        }

    def finish(self) -> StreamStats:
        """计算最终统计值，所有除法都做空数据保护，避免设备未连接时二次报错。"""
        now = time.monotonic()
        self.ended_at_s = max(self.ended_at_s, now)
        duration = max(0.001, self.ended_at_s - self.started_at_s)
        scans_seen = len(self._scan_packets)

        completed_scans = 0
        missing_packets = 0
        expected_packets = 0
        loss_evaluation_scan_items = self._loss_evaluation_scan_items()
        boundary_partial_scans_ignored = scans_seen - len(loss_evaluation_scan_items)
        if self.expected_packets_per_scan > 0:
            for packets in self._scan_packets.values():
                missing = max(0, self.expected_packets_per_scan - len(packets))
                if missing == 0:
                    completed_scans += 1
            for _, packets in loss_evaluation_scan_items:
                expected_packets += self.expected_packets_per_scan
                missing_packets += max(0, self.expected_packets_per_scan - len(packets))

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
        notes = self.notes[:]
        if boundary_partial_scans_ignored:
            notes.append(
                f"缺包率统计已忽略 {boundary_partial_scans_ignored} 个窗口边界不完整圈"
            )
        device_interval_summary = self._interval_summary(
            [timestamp for _, timestamp in self._completed_scan_device_timestamps]
        )
        wall_interval_summary = self._interval_summary(
            [wall_time for _, wall_time in self._completed_scan_wall_times]
        )

        return StreamStats(
            model=self.model,
            host=self.host,
            started_at_s=self.started_at_s,
            ended_at_s=self.ended_at_s,
            frames_received=self.frames_received,
            scans_seen=scans_seen,
            completed_scans=completed_scans,
            loss_evaluated_scans=len(loss_evaluation_scan_items),
            boundary_partial_scans_ignored=boundary_partial_scans_ignored,
            scan_timestamp_interval_count=int(device_interval_summary["count"]),
            scan_timestamp_interval_avg_s=float(device_interval_summary["avg_s"]),
            scan_timestamp_interval_latest_s=float(device_interval_summary["latest_s"]),
            scan_timestamp_interval_avg_display=str(device_interval_summary["avg_display"]),
            scan_timestamp_interval_latest_display=str(device_interval_summary["latest_display"]),
            completed_scan_wall_interval_count=int(wall_interval_summary["count"]),
            completed_scan_wall_interval_avg_s=float(wall_interval_summary["avg_s"]),
            completed_scan_wall_interval_latest_s=float(wall_interval_summary["latest_s"]),
            completed_scan_wall_interval_avg_display=str(wall_interval_summary["avg_display"]),
            completed_scan_wall_interval_latest_display=str(wall_interval_summary["latest_display"]),
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
            notes=notes,
        )
