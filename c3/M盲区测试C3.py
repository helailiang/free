from __future__ import annotations

"""
M 盲区测试（C3）

本文件是一个面向 C3 雷达的“盲区命中”测试工具，核心流程是：

- 通过 **命令端口** 下发控制指令（切换连续流格式、启动/停止连续取数、查询编码器时间等）
- 通过 **数据端口** 持续接收点云/测量数据流，并按协议组帧后解析
- 将“单圈扫描(scan)”聚合为业务可用的点序列，再按盲区测试规则筛选与统计

协议依据（仓库内文档）：
- `HOST-ARM通信协议.md`：定义了帧结构（Header/Payload/Tail）、SOF=0x5A、length 字段、小端传输、
  Header CRC（CRC16-MODBUS）以及 CMD/ACK/MSG 等 payload_type 的语义。

注意：
- 本文件的“帧解析/组帧”细节由 `libs.protocols.c3_common` 提供（例如 `parse_c3_frame`）。
- 为了便于理解业务，本文件会在关键位置标注协议字段含义与收发方向，但不更改任何业务逻辑。
"""

import csv
import ipaddress
import socket
import struct
import sys
import time
from collections import deque
from pathlib import Path
from statistics import median, pstdev

from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from libs.protocols.c3_common import (
    OFFSET_LENGTH,
    OFFSET_POINTS,
    PAYLOAD_TYPE_CMD,
    SOF,
    build_c3_frame,
    parse_c3_frame,
    parse_c3_transport_frame,
    u16_le,
)

try:
    import pyqtgraph as pg

    PG_AVAILABLE = True
except ImportError:
    PG_AVAILABLE = False


DEFAULT_RADAR_IP = "192.168.192.101"

# 下面三个命令为“完整帧”字节串（以 SOF=0x5A 开头，包含 length/payload_type/seq_num/CRC 等 Header 字段）。
# 用于快速下发常用控制指令；其字段含义与校验规则见 `HOST-ARM通信协议.md` 的帧结构/命令定义章节。
SET_POLAR_03_COMMAND = bytes.fromhex("5A 0D 00 00 00 7E A1 1F 03 DB 4D 8A 15")
START_COMMAND = bytes.fromhex("5A 0E 00 00 00 7E E5 02 00 01 EA 3D C2 8B")
STOP_COMMAND = bytes.fromhex("5A 0E 00 00 00 7E E5 02 00 00 7C 0D C5 FC")

# 0x25：查询“编码器一圈齿时间/相关计数”（具体返回 payload 布局以协议中 0x25 命令为准）。
# 这里通过 build_c3_frame 组一个 CMD 类型帧：payload = [cmd_id]
QUERY_ENCODER_TIME_COMMAND = build_c3_frame(PAYLOAD_TYPE_CMD, 0, bytes([0x25]))

# 端口约定：命令与数据分离
# - CMD_PORT：控制指令（CMD/ACK）
# - DATA_PORT：连续数据流（MSG/点云/测量数据）
CMD_PORT = 50000
DATA_PORT = 52000
MIN_FRAME_LENGTH = OFFSET_POINTS + 4


def normalize_c3_angle_deg(angle_deg: float) -> float:
    """将 C3 点云角度归一化到便于业务判断的范围。

    业务背景：
    - C3 点云通常以 0~360°（或等价范围）表示角度。
    - 盲区测试更关注 -90°~+270° 等连续区间，便于以“起始角 + 索引”方式定位点。

    这里将大于 270° 的角度映射到负角度（例如 350° -> -10°），以保持角度序列连续。
    """
    if angle_deg > 270.0:
        return angle_deg - 360.0
    return angle_deg


def estimate_angle_resolution_deg(points: list[dict]) -> float:
    """从已解析的点序列估算角分辨率（°）。

    点云协议里每个点带角度字段，但实际扫描的“步进角”会受配置影响。
    本函数取相邻点的正向角度差的中位数作为估计值，用于：
    - 估算单圈点数（角范围 / 分辨率）
    - 在 UI/统计中展示“实际分辨率”
    """
    if len(points) < 2:
        return 0.0

    diffs: list[float] = []
    previous_angle: float | None = None
    for point in points:
        angle = point["angle_deg"]
        if previous_angle is not None:
            diff = angle - previous_angle
            if diff > 0:
                diffs.append(diff)
        previous_angle = angle

    if not diffs:
        return 0.0
    return float(median(diffs))


def convert_c3_timestamp_delta_to_ms(raw_delta: int, time_type: int | None) -> float:
    """将协议里的时间差（raw_delta + time_type）转换为毫秒。

    说明：
    - 协议在某些消息中会携带时间戳/时间差，并用 time_type 指示单位或时基。
    - 这里做的是“显示/统计友好”的近似换算；真正的时基定义以协议文档为准。
    """
    if raw_delta <= 0:
        return 0.0

    if time_type == 0:
        return raw_delta / (float(2 ** 32) / 1000.0)
    if time_type in {1, 2}:
        return raw_delta / 1_000_000.0
    if raw_delta >= 1_000_000:
        return raw_delta / 1_000_000.0
    if raw_delta >= 1_000:
        return raw_delta / 1_000.0
    return float(raw_delta)


def calculate_float_stats(values: list[float]) -> dict[str, float]:
    """对一组数值做基础统计（用于延迟/抖动等指标展示）。"""
    if not values:
        return {
            "count": 0,
            "avg": 0.0,
            "min": 0.0,
            "max": 0.0,
            "jitter": 0.0,
        }

    return {
        "count": len(values),
        "avg": sum(values) / len(values),
        "min": min(values),
        "max": max(values),
        "jitter": pstdev(values) if len(values) > 1 else 0.0,
    }


def console_log(message: str) -> None:
    text = str(message)
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        sys.stdout.write(text + "\n")
    except UnicodeEncodeError:
        safe_text = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
        sys.stdout.write(safe_text + "\n")
    sys.stdout.flush()


def console_print_measurement_data(measurement: dict, *, max_rows: int = 12) -> None:
    points = measurement.get("results") or measurement.get("all_points") or []
    console_log(
        f"[DATA] scan_cnt={measurement.get('scan_cnt', '-')}, "
        f"索引 {measurement.get('start_index', '-')} - {measurement.get('end_index', '-')}, "
        f"显示 {min(len(points), max_rows)}/{len(points)} 条"
    )
    console_log("   索引 | 角度(°) | 距离(mm) | 反射率 | 包序号 | scan_cnt")
    for point in points[:max_rows]:
        console_log(
            f"   {point['index']:>4} | "
            f"{point['angle_deg']:>7.2f} | "
            f"{point['measured_distance']:>8} | "
            f"{point['reflectivity']:>6} | "
            f"{point['seq_num']:>6} | "
            f"{point['scan_cnt']:>8}"
        )
    if len(points) > max_rows:
        console_log(f"   ... 其余 {len(points) - max_rows} 条已省略")


def console_print_measurement_result(measurement: dict) -> None:
    console_log("")
    console_log(
        f"[MEASURE] scan_cnt={measurement.get('scan_cnt', '-')}, "
        f"索引 {measurement.get('start_index', '-')} - {measurement.get('end_index', '-')}"
    )
    console_log(
        f"[MEASURE] 包数={measurement.get('frame_count', '-')}, 点数={measurement.get('raw_point_count', '-')}, "
        f"角度范围={measurement.get('angle_min_deg', 0.0):.2f}° -> {measurement.get('angle_max_deg', 0.0):.2f}°"
    )
    console_log(
        f"[MEASURE] 命中结果: {measurement.get('filtered_count', 0)}/{measurement.get('total_count', 0)}, "
        f"连续条件={'满足' if measurement.get('has_consecutive_qualified') else '不满足'}"
    )
    consecutive_points = measurement.get("consecutive_points") or []
    if consecutive_points:
        console_log("[MEASURE] 连续命中点详情:")
        for point in consecutive_points:
            console_log(
                f"   索引={point['index']}, 角度={point['angle_deg']:.2f}°, "
                f"距离={point['measured_distance']}mm, 反射率={point['reflectivity']}, "
                f"seq={point['seq_num']}, scan_cnt={point['scan_cnt']}"
            )
    filtered_points = measurement.get("results") or []
    if filtered_points:
        console_print_measurement_data(measurement, max_rows=8)


def console_print_measurement_summary(summary: dict) -> None:
    latest = summary.get("latest_measurement") or {}
    success_count = summary.get("success_count", 0)
    total_time = summary.get("total_time", 0.0)
    console_log("")
    console_log("=" * 72)
    console_log("M盲区测试C3 统计摘要")
    console_log("=" * 72)
    console_log(f"请求测量次数: {summary.get('requested_iterations', 0)}")
    console_log(f"成功测量次数: {success_count}")
    console_log(f"连续命中圈数: {summary.get('qualified_circles', 0)}")
    console_log(f"累计总点数: {summary.get('total_points_all', 0)}")
    console_log(f"累计命中点数: {summary.get('filtered_points_all', 0)}")
    console_log(f"总耗时: {total_time:.2f} s")
    console_log(
        f"平均单次耗时: {total_time / success_count * 1000:.1f} ms"
        if success_count
        else "平均单次耗时: -"
    )
    console_log(
        f"平均测量频率: {success_count / total_time:.2f} 次/s"
        if success_count and total_time > 0
        else "平均测量频率: -"
    )
    if latest:
        console_log("-" * 72)
        console_log(
            f"最新单圈 scan_cnt={latest.get('scan_cnt', '-')}, "
            f"包序号={latest.get('first_seq_num', '-')} -> {latest.get('last_seq_num', '-')}, "
            f"命中={latest.get('filtered_count', 0)}/{latest.get('total_count', 0)}"
        )
    console_log("=" * 72)


class C3BlindZoneProcessor:
    """C3 盲区测试的“协议接入层 + 单圈聚合层”。

    角色划分（对应协议文档中的 master/slave 概念）：
    - 本机（测试工具）作为 master：下发控制命令、接收连续数据
    - 雷达（C3）作为 slave：响应命令、持续推送点云/测量帧

    本类负责的事情：
    - 建立两路 TCP 连接（命令端口 / 数据端口）
    - 维护数据接收缓冲区 `buffer`，按协议 SOF/length 切割出完整帧
    - 调用 `parse_c3_frame` 解析帧（包含 seq_num/scan_cnt/points 等）
    - 将同一圈扫描（scan_cnt 相同）的多帧聚合为一个 completed_scan

    协议要点（依据 `HOST-ARM通信协议.md` 的帧结构章节）：
    - SOF：起始字节 0x5A
    - length：帧长度字段（小端）
    - payload_type：CMD / ACK / MSG（本类主要消费数据流里的 MSG/点云类型）
    - Header CRC：CRC16-MODBUS（具体校验由解析函数处理）
    """

    def __init__(
        self,
        host: str = DEFAULT_RADAR_IP,
        cmd_port: int = CMD_PORT,
        data_port: int = DATA_PORT,
        connect_timeout: float = 3.0,
    ) -> None:
        self.host = host
        self.cmd_port = cmd_port
        self.data_port = data_port
        self.connect_timeout = connect_timeout

        self.cmd_socket: socket.socket | None = None
        self.data_socket: socket.socket | None = None
        self.connected = False
        self.streaming = False

        self.buffer = bytearray()
        self.completed_scans: deque[dict] = deque()
        self.current_scan_cnt: int | None = None
        self.current_scan_points: list[dict] = []
        self.current_scan_seq_nums: list[int] = []
        self.current_scan_frame_count = 0
        self.current_scan_timestamp: int | None = None
        self.current_scan_time_type: int | None = None
        self.current_scan_host_first_frame_ns: int | None = None
        self.current_scan_host_last_frame_ns: int | None = None
        self.discard_first_completed_scan = True
        self.frame_counter = 0

        self.last_error = ""
        self.last_settings: dict | None = None
        self.angular_resolution_deg = 0.33
        self.scan_angle_range_deg = 270.0
        self.start_angle_deg = -45.0
        self.expected_point_count = self.calculate_points_per_scan()

    def calculate_points_per_scan(self) -> int:
        if self.angular_resolution_deg <= 0 or self.scan_angle_range_deg <= 0:
            return 0
        return max(1, int(round(self.scan_angle_range_deg / self.angular_resolution_deg)) + 1)

    def configure_scan_parameters(
        self,
        angular_resolution_deg: float | None = None,
        scan_angle_range_deg: float | None = None,
        start_angle_deg: float | None = None,
    ) -> None:
        if angular_resolution_deg is not None and angular_resolution_deg > 0:
            self.angular_resolution_deg = angular_resolution_deg
        if scan_angle_range_deg is not None and scan_angle_range_deg > 0:
            self.scan_angle_range_deg = scan_angle_range_deg
        if start_angle_deg is not None:
            self.start_angle_deg = start_angle_deg

        self.expected_point_count = self.calculate_points_per_scan()

    def connect_radar(self) -> bool:
        """建立到 C3 的两路 TCP 连接。

        - cmd_socket：用于下发控制命令（例如切换连续流格式、启动/停止取数）
        - data_socket：用于接收连续点云/测量数据流
        """
        try:
            self.last_error = ""
            if self.connected:
                return True

            self.cmd_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.cmd_socket.settimeout(self.connect_timeout)
            self.cmd_socket.connect((self.host, self.cmd_port))

            self.data_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.data_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 256 * 1024)
            self.data_socket.settimeout(0.5)
            self.data_socket.connect((self.host, self.data_port))

            self.connected = True
            console_log(f"[INFO] C3 雷达连接成功: {self.host}:{self.cmd_port}/{self.data_port}")
            return True
        except Exception as exc:
            self.last_error = str(exc)
            console_log(f"[ERROR] C3 雷达连接失败: {exc}")
            self.close()
            return False

    def ensure_connection(self) -> bool:
        return self.connected or self.connect_radar()

    def send_command(
        self,
        command: bytes,
        action_name: str,
        *,
        require_response: bool = True,
    ) -> bytes | None:
        """向命令端口发送一条“完整协议帧”。

        入参 `command` 是已经按协议组好的帧（通常以 0x5A 开头），本函数只负责收发与超时处理。
        响应帧的 payload/字段解析由上层按需要调用 `parse_c3_transport_frame`/`parse_c3_frame` 完成。
        """
        if self.cmd_socket is None:
            raise ConnectionError("命令端口未连接")

        try:
            self.cmd_socket.sendall(command)
            if not require_response:
                console_log(f"[TX] {action_name}: {command.hex(' ').upper()}")
                return None
            response = self.cmd_socket.recv(1024)
            console_log(
                f"[TXRX] {action_name}: {command.hex(' ').upper()} | 响应 {len(response)}B"
            )
            return response
        except socket.timeout as exc:
            raise TimeoutError(f"{action_name}超时，未收到雷达响应") from exc
        except Exception as exc:
            raise RuntimeError(f"{action_name}失败: {exc}") from exc

    def configure_stream_format(self) -> None:
        self.send_command(SET_POLAR_03_COMMAND, "设置连续流格式为 0x03")

    def start_stream(self) -> None:
        if self.streaming:
            return
        self.send_command(START_COMMAND, "启动连续取数")
        self.streaming = True

    def stop_stream(self) -> None:
        if not self.connected or not self.streaming:
            return
        try:
            self.send_command(STOP_COMMAND, "停止连续取数")
        finally:
            self.streaming = False

    def drain_data_socket(self) -> None:
        if self.data_socket is None:
            return

        previous_timeout = None
        try:
            previous_timeout = self.data_socket.gettimeout()
            self.data_socket.setblocking(False)
            while True:
                try:
                    chunk = self.data_socket.recv(8192)
                    if not chunk:
                        break
                except BlockingIOError:
                    break
        except OSError:
            pass
        finally:
            try:
                self.data_socket.setblocking(True)
                self.data_socket.settimeout(previous_timeout if previous_timeout is not None else 0.5)
            except OSError:
                pass

    def reset_scan_state(self, *, discard_first_completed_scan: bool) -> None:
        self.buffer.clear()
        self.completed_scans.clear()
        self.current_scan_cnt = None
        self.current_scan_points = []
        self.current_scan_seq_nums = []
        self.current_scan_frame_count = 0
        self.current_scan_timestamp = None
        self.current_scan_time_type = None
        self.current_scan_host_first_frame_ns = None
        self.current_scan_host_last_frame_ns = None
        self.discard_first_completed_scan = discard_first_completed_scan

    def prepare_measurement_session(self) -> None:
        if not self.ensure_connection():
            raise ConnectionError(self.last_error or "连接雷达失败")

        # 业务目的：让“下一次读到的一圈(scan)”尽可能干净可用，避免历史残留数据影响盲区判定。
        # 因此顺序是：停流 -> 清空数据端口缓冲 -> 重置聚合状态 -> 切换连续流格式 -> 重新起流
        console_log("[INFO] 准备 C3 测量会话: 停止旧流 -> 清空缓存 -> 切换0x03 -> 启动连续取数")
        self.stop_stream()
        self.drain_data_socket()
        self.reset_scan_state(discard_first_completed_scan=True)
        self.configure_stream_format()
        self.start_stream()

    def finish_measurement_session(self) -> None:
        try:
            self.stop_stream()
        finally:
            console_log("[INFO] 结束 C3 测量会话")
            self.drain_data_socket()
            self.reset_scan_state(discard_first_completed_scan=True)

    def _start_new_scan(self, parsed: dict, host_recv_ns: int | None = None) -> None:
        self.current_scan_cnt = parsed["scan_cnt"]
        self.current_scan_points = []
        self.current_scan_seq_nums = []
        self.current_scan_frame_count = 0
        self.current_scan_timestamp = parsed["timestamp"]
        self.current_scan_time_type = parsed["time_type"]
        self.current_scan_host_first_frame_ns = host_recv_ns
        self.current_scan_host_last_frame_ns = host_recv_ns

    def _convert_points(self, parsed: dict) -> list[dict]:
        converted: list[dict] = []
        for point in parsed.get("points", []):
            angle_deg = normalize_c3_angle_deg(point["angle_deg"])
            converted.append(
                {
                    "index": -1,
                    "angle_deg": angle_deg,
                    "measured_distance": point["r_mm"],
                    "reflectivity": point["reflectivity"],
                    "seq_num": point["seq_num"],
                    "scan_cnt": point["scan_cnt"],
                    "timestamp": point["timestamp"],
                    "frame_index": point["frame_index"],
                    "point_index_in_packet": point["point_index"],
                }
            )
        return converted

    def _finalize_current_scan(self) -> None:
        if self.current_scan_cnt is None or not self.current_scan_points:
            self.current_scan_cnt = None
            self.current_scan_points = []
            self.current_scan_seq_nums = []
            self.current_scan_frame_count = 0
            self.current_scan_timestamp = None
            self.current_scan_time_type = None
            self.current_scan_host_first_frame_ns = None
            self.current_scan_host_last_frame_ns = None
            return

        points = sorted(
            self.current_scan_points,
            key=lambda item: (
                item["angle_deg"],
                item["seq_num"],
                item["point_index_in_packet"],
            ),
        )
        for index, point in enumerate(points):
            point["index"] = index

        angle_min_deg = points[0]["angle_deg"]
        angle_max_deg = points[-1]["angle_deg"]
        estimated_resolution_deg = estimate_angle_resolution_deg(points)
        completed_scan = {
            "scan_cnt": self.current_scan_cnt,
            "points": points,
            "point_count": len(points),
            "frame_count": self.current_scan_frame_count,
            "timestamp": self.current_scan_timestamp,
            "time_type": self.current_scan_time_type,
            "first_seq_num": self.current_scan_seq_nums[0] if self.current_scan_seq_nums else None,
            "last_seq_num": self.current_scan_seq_nums[-1] if self.current_scan_seq_nums else None,
            "first_host_recv_ns": self.current_scan_host_first_frame_ns,
            "last_host_recv_ns": self.current_scan_host_last_frame_ns,
            "angle_min_deg": angle_min_deg,
            "angle_max_deg": angle_max_deg,
            "scan_angle_range_deg": max(0.0, angle_max_deg - angle_min_deg),
            "estimated_resolution_deg": estimated_resolution_deg,
        }

        if self.discard_first_completed_scan:
            self.discard_first_completed_scan = False
        else:
            self.completed_scans.append(completed_scan)

        self.current_scan_cnt = None
        self.current_scan_points = []
        self.current_scan_seq_nums = []
        self.current_scan_frame_count = 0
        self.current_scan_timestamp = None
        self.current_scan_time_type = None
        self.current_scan_host_first_frame_ns = None
        self.current_scan_host_last_frame_ns = None

    def ingest_frame(self, parsed: dict, host_recv_ns: int | None = None) -> None:
        """消费一帧已解析数据，并按 scan_cnt 聚合成“单圈扫描”。

        关键字段（命名来自解析结果，语义来自协议）：
        - data_type：数据子类型（本工具只关心点云/测量类型；这里用 0x03 过滤）
        - scan_cnt：圈计数/扫描计数，用于把多帧拼成同一圈
        - seq_num：帧序号（用于诊断丢包/乱序）
        - timestamp/time_type：设备侧时间戳信息（用于统计延迟/抖动等）
        """
        if parsed.get("data_type") != 0x03:
            return

        if self.current_scan_cnt is None:
            self._start_new_scan(parsed, host_recv_ns)
        elif parsed["scan_cnt"] != self.current_scan_cnt:
            self._finalize_current_scan()
            self._start_new_scan(parsed, host_recv_ns)

        self.current_scan_points.extend(self._convert_points(parsed))
        self.current_scan_seq_nums.append(parsed["seq_num"])
        self.current_scan_frame_count += 1
        self.current_scan_timestamp = parsed["timestamp"]
        self.current_scan_time_type = parsed["time_type"]
        if host_recv_ns is not None:
            if self.current_scan_host_first_frame_ns is None:
                self.current_scan_host_first_frame_ns = host_recv_ns
            self.current_scan_host_last_frame_ns = host_recv_ns

    def process_buffer(self, host_recv_ns: int | None = None) -> None:
        """在 data_socket 的字节流缓冲区中切割并解析“完整协议帧”。

        依据协议帧结构（见 `HOST-ARM通信协议.md` 的 Header 字段表）：
        - sof(0x5A) 用于对齐帧起始
        - length（小端 uint16）给出整帧长度，用来从流中切割出 frame_data

        解析策略：
        - 先在缓冲区中找 SOF；若前面有杂字节则丢弃直到 SOF
        - 缓冲区不足以读取 length 时等待更多数据
        - length 不合理则丢 1 字节重新对齐（避免卡死在错误位置）
        - 拿到完整 frame_data 后交给 `parse_c3_frame`（内部完成协议层校验与字段解码）
        """
        while True:
            sof_pos = self.buffer.find(bytes([SOF]))
            if sof_pos < 0:
                if len(self.buffer) > 8192:
                    self.buffer.clear()
                return

            if sof_pos > 0:
                del self.buffer[:sof_pos]

            if len(self.buffer) < OFFSET_LENGTH + 2:
                return

            frame_length = u16_le(self.buffer, OFFSET_LENGTH)
            if frame_length < MIN_FRAME_LENGTH:
                del self.buffer[0]
                continue

            if len(self.buffer) < frame_length:
                return

            frame_data = bytes(self.buffer[:frame_length])
            del self.buffer[:frame_length]

            parsed = parse_c3_frame(frame_data, self.frame_counter, include_points=True, include_raw=False)
            self.frame_counter += 1
            if parsed is None:
                continue

            self.ingest_frame(parsed, host_recv_ns)

    def read_next_scan(self, *, timeout: float, stop_requested=None) -> dict | None:
        if self.data_socket is None:
            raise ConnectionError("数据端口未连接")

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if stop_requested is not None and stop_requested():
                return None

            if self.completed_scans:
                return self.completed_scans.popleft()

            try:
                chunk = self.data_socket.recv(4096)
                host_recv_ns = time.time_ns()
            except socket.timeout:
                continue

            if not chunk:
                raise ConnectionError("数据端口连接已关闭")

            self.buffer.extend(chunk)
            self.process_buffer(host_recv_ns)

        if self.completed_scans:
            return self.completed_scans.popleft()
        return None

    def get_consecutive_qualified_points(
        self,
        points: list[dict],
        max_distance: int,
        consecutive_count: int = 3,
    ) -> list[dict]:
        if not points or len(points) < consecutive_count:
            return []

        hit_points: list[dict] = []
        for point in points:
            if 10 < point["measured_distance"] < max_distance:
                hit_points.append(point)
                if len(hit_points) >= consecutive_count:
                    return hit_points[-consecutive_count:]
            else:
                hit_points.clear()

        return []

    def optimized_single_measurement(
        self,
        start_index: int,
        end_index: int,
        max_distance: int | None = None,
        *,
        stop_requested=None,
    ) -> dict | None:
        current_settings = dict(self.last_settings) if self.last_settings else {}
        iterations = current_settings.get("iterations", 1)
        self.last_settings = {
            **current_settings,
            "start_index": start_index,
            "end_index": end_index,
            "max_distance": max_distance,
            "iterations": iterations,
            "angular_resolution_deg": self.angular_resolution_deg,
            "scan_angle_range_deg": self.scan_angle_range_deg,
            "start_angle_deg": self.start_angle_deg,
        }

        scan = self.read_next_scan(timeout=4.0, stop_requested=stop_requested)
        if scan is None or not scan["points"]:
            return None

        points = scan["points"]
        actual_start = max(0, min(start_index, len(points) - 1))
        actual_end = max(actual_start, min(end_index, len(points) - 1))
        selected_points = points[actual_start:actual_end + 1]
        if not selected_points:
            return None

        if max_distance is not None:
            filtered_points = [
                point for point in selected_points if 10 < point["measured_distance"] < max_distance
            ]
            consecutive_points = self.get_consecutive_qualified_points(selected_points, max_distance, 3)
            has_consecutive = bool(consecutive_points)
        else:
            filtered_points = selected_points
            consecutive_points = []
            has_consecutive = False

        frame_signature = tuple(
            (
                point["index"],
                round(point["angle_deg"], 3),
                point["measured_distance"],
                point["reflectivity"],
            )
            for point in selected_points
        )

        return {
            "total_count": len(selected_points),
            "filtered_count": len(filtered_points),
            "results": filtered_points,
            "all_points": selected_points,
            "has_consecutive_qualified": has_consecutive,
            "consecutive_points": consecutive_points,
            "start_index": actual_start,
            "end_index": actual_end,
            "angular_resolution_deg": scan["estimated_resolution_deg"] or self.angular_resolution_deg,
            "scan_angle_range_deg": scan["scan_angle_range_deg"] or self.scan_angle_range_deg,
            "start_angle_deg": scan["angle_min_deg"],
            "expected_point_count": scan["point_count"],
            "frame_signature": frame_signature,
            "scan_cnt": scan["scan_cnt"],
            "frame_count": scan["frame_count"],
            "first_seq_num": scan["first_seq_num"],
            "last_seq_num": scan["last_seq_num"],
            "angle_min_deg": scan["angle_min_deg"],
            "angle_max_deg": scan["angle_max_deg"],
            "raw_point_count": scan["point_count"],
        }

    def query_encoder_time_once(self) -> dict:
        """下发 0x25 查询并解析返回。

        通信层说明：
        - 请求帧：CMD(payload_type=CMD)，payload 以命令字节 0x25 开头（见 `QUERY_ENCODER_TIME_COMMAND`）
        - 响应帧：通常为 ACK（或协议定义的响应类型），这里用 `parse_c3_transport_frame` 先做传输层拆包

        解析假设（以协议为准）：
        - payload[0] == 0x25：回包对应查询命令
        - payload[1] 为 ret_code：0 表示成功，非 0 为错误
        - payload[2:] 是若干个 little-endian uint32 值（这里按 4 字节对齐解包）
        """
        if not self.ensure_connection():
            raise ConnectionError(self.last_error or "连接雷达失败")
        if self.cmd_socket is None:
            raise ConnectionError("命令端口未连接")

        send_host_ns = time.time_ns()
        self.cmd_socket.sendall(QUERY_ENCODER_TIME_COMMAND)
        response = self.cmd_socket.recv(4096)
        recv_host_ns = time.time_ns()

        parsed = parse_c3_transport_frame(response, verify_crc=False)
        if parsed is None:
            raise RuntimeError("0x25 查询返回帧解析失败")

        payload = parsed["payload"]
        if len(payload) < 2 or payload[0] != 0x25:
            raise RuntimeError("0x25 查询返回内容不符合预期")

        ret_code = payload[1]
        if ret_code != 0:
            raise RuntimeError(f"0x25 查询失败，ret_code=0x{ret_code:02X}")

        value_bytes = payload[2:]
        value_count = len(value_bytes) // 4
        values = list(struct.unpack(f"<{value_count}I", value_bytes[: value_count * 4])) if value_count else []
        total_us = sum(values)
        total_ms = total_us / 1000.0

        return {
            "send_host_ns": send_host_ns,
            "receive_host_ns": recv_host_ns,
            "latency_ms": (recv_host_ns - send_host_ns) / 1_000_000.0,
            "value_count": value_count,
            "encoder_values_us": values,
            "encoder_total_us": total_us,
            "encoder_total_ms": total_ms,
            "encoder_mean_us": total_us / value_count if value_count else 0.0,
            "encoder_min_us": min(values) if values else 0,
            "encoder_max_us": max(values) if values else 0,
            "response_length": len(response),
        }

    def close(self) -> None:
        if self.connected or self.streaming:
            console_log("[INFO] 关闭 C3 雷达连接")
        self.streaming = False
        self.connected = False

        for sock in (self.cmd_socket, self.data_socket):
            if sock is None:
                continue
            try:
                sock.close()
            except OSError:
                pass

        self.cmd_socket = None
        self.data_socket = None
        self.reset_scan_state(discard_first_completed_scan=True)


class ConnectionThread(QThread):
    connection_complete = pyqtSignal(object)

    def __init__(self, host: str, connect_timeout: float = 3.0) -> None:
        super().__init__()
        self.host = host
        self.connect_timeout = connect_timeout

    def run(self) -> None:
        processor = C3BlindZoneProcessor(host=self.host, connect_timeout=self.connect_timeout)
        if processor.connect_radar():
            self.connection_complete.emit(
                {
                    "success": True,
                    "host": self.host,
                    "radar": processor,
                    "error": "",
                }
            )
            return

        error_message = processor.last_error or "连接失败"
        processor.close()
        self.connection_complete.emit(
            {
                "success": False,
                "host": self.host,
                "radar": None,
                "error": error_message,
            }
        )


class MeasurementThread(QThread):
    measurement_complete = pyqtSignal(object)
    measurement_progress = pyqtSignal(int, int, int, bool, float)
    measurement_error = pyqtSignal(str)

    def __init__(
        self,
        radar_processor: C3BlindZoneProcessor,
        start_index: int,
        end_index: int,
        max_distance: int | None,
        iterations: int,
        radar_frequency_hz: float,
    ) -> None:
        super().__init__()
        self.radar_processor = radar_processor
        self.start_index = start_index
        self.end_index = end_index
        self.max_distance = max_distance
        self.iterations = iterations
        self.radar_frequency_hz = radar_frequency_hz
        self._is_running = True
        self.progress_emit_interval_sec = 0.10

    def stop(self) -> None:
        self._is_running = False

    def _stop_requested(self) -> bool:
        return not self._is_running

    def get_measurement_period(self) -> float:
        if self.radar_frequency_hz <= 0:
            return 0.0
        return 1.0 / self.radar_frequency_hz

    def wait_for_next_cycle(self, scheduled_time: float | None) -> bool:
        period = self.get_measurement_period()
        if period <= 0 or scheduled_time is None:
            return True

        while self._is_running:
            remaining = scheduled_time - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(remaining, 0.01))

        return False

    def run(self) -> None:
        success_count = 0
        qualified_circles = 0
        total_points_all = 0
        filtered_points_all = 0
        iteration_numbers: list[int] = []
        filtered_counts: list[int] = []
        latest_measurement: dict | None = None
        start_time = time.time()
        measurement_period = self.get_measurement_period()
        next_capture_time = time.monotonic()
        last_frame_signature = None
        last_progress_emit_time = 0.0
        last_logged_consecutive_state: bool | None = None

        try:
            if not self.radar_processor.ensure_connection():
                self.measurement_error.emit(self.radar_processor.last_error or "连接雷达失败")
                return

            console_log("")
            console_log("[INFO] 开始 C3 批量盲区测量")
            console_log(f"   雷达: {self.radar_processor.host}")
            console_log(f"   索引范围: {self.start_index} - {self.end_index}")
            console_log(f"   测量次数: {self.iterations}")
            console_log(
                f"   距离过滤: 10 < 距离 < {self.max_distance}"
                if self.max_distance is not None
                else "   距离过滤: 未启用"
            )
            console_log(f"   动态采样频率: {self.radar_frequency_hz:.2f} Hz")
            console_log("=" * 72)

            self.radar_processor.prepare_measurement_session()
            try:
                for iteration in range(self.iterations):
                    if not self._is_running:
                        break

                    if measurement_period > 0 and not self.wait_for_next_cycle(next_capture_time):
                        break

                    capture_started_monotonic = time.monotonic()
                    if measurement_period > 0:
                        next_capture_time = capture_started_monotonic + measurement_period

                    measure_start = time.time()
                    result = self.radar_processor.optimized_single_measurement(
                        self.start_index,
                        self.end_index,
                        self.max_distance,
                        stop_requested=self._stop_requested,
                    )
                    if not self._is_running:
                        break
                    if result is None:
                        continue

                    current_signature = result.get("frame_signature")
                    if current_signature is not None and current_signature == last_frame_signature:
                        continue
                    last_frame_signature = current_signature

                    success_count += 1
                    total_points_all += result["total_count"]
                    filtered_points_all += result["filtered_count"]
                    latest_measurement = {
                        "iteration": iteration + 1,
                        **result,
                    }

                    console_log(
                            f"[PROGRESS] 第 {iteration + 1}/{self.iterations} 次: "
                            f"scan_cnt={result.get('scan_cnt', '-')}, "
                            f"命中 {result['filtered_count']}/{result['total_count']}, "
                            f"连续={'是' if result['has_consecutive_qualified'] else '否'}"
                        )
                    should_log_detail = (
                        iteration == 0
                        or iteration + 1 == self.iterations
                        or (iteration + 1) % 10 == 0
                        or (result["has_consecutive_qualified"] and last_logged_consecutive_state is False)
                    )
                    if should_log_detail:
                        console_print_measurement_result(latest_measurement)
                    last_logged_consecutive_state = result["has_consecutive_qualified"]

                    if result["has_consecutive_qualified"]:
                        qualified_circles += 1

                    iteration_numbers.append(iteration + 1)
                    filtered_counts.append(result["filtered_count"])

                    elapsed_ms = (time.time() - measure_start) * 1000.0
                    now_monotonic = time.monotonic()
                    should_emit_progress = (
                        success_count == 1
                        or iteration + 1 == self.iterations
                        or result["has_consecutive_qualified"]
                        or (now_monotonic - last_progress_emit_time) >= self.progress_emit_interval_sec
                    )
                    if should_emit_progress:
                        self.measurement_progress.emit(
                            iteration + 1,
                            self.iterations,
                            result["filtered_count"],
                            result["has_consecutive_qualified"],
                            elapsed_ms,
                        )
                        last_progress_emit_time = now_monotonic
            finally:
                self.radar_processor.finish_measurement_session()

            total_elapsed = time.time() - start_time
            summary = {
                "requested_iterations": self.iterations,
                "success_count": success_count,
                "qualified_circles": qualified_circles,
                "total_time": total_elapsed,
                "total_points_all": total_points_all,
                "filtered_points_all": filtered_points_all,
                "iteration_numbers": iteration_numbers,
                "filtered_counts": filtered_counts,
                "latest_measurement": latest_measurement,
            }
            console_print_measurement_summary(summary)
            self.measurement_complete.emit(summary)
        except Exception as exc:
            console_log(f"[ERROR] C3 批量盲区测量失败: {exc}")
            self.measurement_error.emit(str(exc))


class TimestampDiagnosticsThread(QThread):
    diagnostics_progress = pyqtSignal(str)
    diagnostics_complete = pyqtSignal(str, object)
    diagnostics_error = pyqtSignal(str, str)

    def __init__(
        self,
        radar_processor: C3BlindZoneProcessor,
        mode: str,
        *,
        query_iterations: int = 0,
        query_interval_ms: float = 0.0,
        stream_scan_count: int = 0,
    ) -> None:
        super().__init__()
        self.radar_processor = radar_processor
        self.mode = mode
        self.query_iterations = query_iterations
        self.query_interval_ms = query_interval_ms
        self.stream_scan_count = stream_scan_count
        self._is_running = True

    def stop(self) -> None:
        self._is_running = False

    def _stop_requested(self) -> bool:
        return not self._is_running

    def _sleep_interval(self, interval_sec: float) -> bool:
        if interval_sec <= 0:
            return self._is_running

        deadline = time.monotonic() + interval_sec
        while self._is_running:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(remaining, 0.01))
        return False

    def _run_query_mode(self) -> None:
        if not self.radar_processor.ensure_connection():
            raise ConnectionError(self.radar_processor.last_error or "连接雷达失败")

        self.radar_processor.stop_stream()
        self.radar_processor.drain_data_socket()
        self.radar_processor.reset_scan_state(discard_first_completed_scan=True)

        results: list[dict] = []
        previous_receive_ns: int | None = None

        for index in range(self.query_iterations):
            if not self._is_running:
                break

            sample = self.radar_processor.query_encoder_time_once()
            sample["index"] = index + 1
            if previous_receive_ns is not None:
                host_interval_ms = (sample["receive_host_ns"] - previous_receive_ns) / 1_000_000.0
                sample["host_interval_ms"] = host_interval_ms
                sample["host_minus_encoder_ms"] = host_interval_ms - sample["encoder_total_ms"]
            else:
                sample["host_interval_ms"] = None
                sample["host_minus_encoder_ms"] = None
            previous_receive_ns = sample["receive_host_ns"]
            results.append(sample)

            self.diagnostics_progress.emit(
                f"0x25 查询 {sample['index']}/{self.query_iterations}: "
                f"时延 {sample['latency_ms']:.2f} ms, 码盘总时间 {sample['encoder_total_ms']:.3f} ms"
            )

            if index + 1 < self.query_iterations:
                if not self._sleep_interval(self.query_interval_ms / 1000.0):
                    break

        latency_stats = calculate_float_stats([item["latency_ms"] for item in results])
        encoder_total_stats = calculate_float_stats([item["encoder_total_ms"] for item in results])
        host_interval_stats = calculate_float_stats(
            [item["host_interval_ms"] for item in results if item["host_interval_ms"] is not None]
        )
        host_minus_encoder_stats = calculate_float_stats(
            [item["host_minus_encoder_ms"] for item in results if item["host_minus_encoder_ms"] is not None]
        )

        summary = {
            "sample_count": len(results),
            "latency_stats": latency_stats,
            "encoder_total_stats": encoder_total_stats,
            "host_interval_stats": host_interval_stats,
            "host_minus_encoder_stats": host_minus_encoder_stats,
            "value_count": results[0]["value_count"] if results else 0,
        }
        self.diagnostics_complete.emit("query", {"results": results, "summary": summary})

    def _run_stream_mode(self) -> None:
        if not self.radar_processor.ensure_connection():
            raise ConnectionError(self.radar_processor.last_error or "连接雷达失败")

        results: list[dict] = []
        previous_scan: dict | None = None
        baseline_scan: dict | None = None
        scan_step_errors = 0
        timestamp_non_increasing = 0

        self.radar_processor.prepare_measurement_session()
        try:
            while self._is_running and len(results) < self.stream_scan_count:
                scan = self.radar_processor.read_next_scan(timeout=4.0, stop_requested=self._stop_requested)
                if scan is None:
                    continue

                sample = {
                    "index": len(results) + 1,
                    "scan_cnt": scan["scan_cnt"],
                    "timestamp": scan["timestamp"],
                    "time_type": scan.get("time_type"),
                    "frame_count": scan["frame_count"],
                    "point_count": scan["point_count"],
                    "first_seq_num": scan["first_seq_num"],
                    "last_seq_num": scan["last_seq_num"],
                    "first_host_recv_ns": scan.get("first_host_recv_ns"),
                    "timestamp_delta_ms": None,
                    "host_interval_ms": None,
                    "host_minus_radar_ms": None,
                    "timestamp_elapsed_ms": None,
                    "host_capture_elapsed_ms": None,
                    "host_capture_minus_timestamp_ms": None,
                    "scan_step": None,
                    "scan_step_ok": None,
                    "timestamp_increasing": None,
                }

                if baseline_scan is None:
                    baseline_scan = scan
                    sample["timestamp_elapsed_ms"] = 0.0
                    if scan.get("first_host_recv_ns") is not None:
                        sample["host_capture_elapsed_ms"] = 0.0
                        sample["host_capture_minus_timestamp_ms"] = 0.0

                if previous_scan is not None:
                    scan_step = (scan["scan_cnt"] - previous_scan["scan_cnt"]) % 256
                    raw_delta = scan["timestamp"] - previous_scan["timestamp"]
                    timestamp_delta_ms = convert_c3_timestamp_delta_to_ms(raw_delta, scan.get("time_type"))
                    host_interval_ms = None
                    if scan.get("first_host_recv_ns") is not None and previous_scan.get("first_host_recv_ns") is not None:
                        host_interval_ms = (
                            scan["first_host_recv_ns"] - previous_scan["first_host_recv_ns"]
                        ) / 1_000_000.0

                    sample["timestamp_delta_ms"] = timestamp_delta_ms
                    sample["host_interval_ms"] = host_interval_ms
                    sample["host_minus_radar_ms"] = (
                        host_interval_ms - timestamp_delta_ms if host_interval_ms is not None else None
                    )
                    sample["scan_step"] = scan_step
                    sample["scan_step_ok"] = scan_step == 1
                    sample["timestamp_increasing"] = raw_delta > 0

                    if scan_step != 1:
                        scan_step_errors += 1
                    if raw_delta <= 0:
                        timestamp_non_increasing += 1

                if baseline_scan is not None:
                    raw_elapsed = scan["timestamp"] - baseline_scan["timestamp"]
                    if raw_elapsed >= 0:
                        sample["timestamp_elapsed_ms"] = convert_c3_timestamp_delta_to_ms(
                            raw_elapsed,
                            scan.get("time_type"),
                        )

                    baseline_host_ns = baseline_scan.get("first_host_recv_ns")
                    current_host_ns = scan.get("first_host_recv_ns")
                    if baseline_host_ns is not None and current_host_ns is not None:
                        host_capture_elapsed_ms = (current_host_ns - baseline_host_ns) / 1_000_000.0
                        sample["host_capture_elapsed_ms"] = host_capture_elapsed_ms
                        if sample["timestamp_elapsed_ms"] is not None:
                            sample["host_capture_minus_timestamp_ms"] = (
                                host_capture_elapsed_ms - sample["timestamp_elapsed_ms"]
                            )

                results.append(sample)
                previous_scan = scan

                if sample["timestamp_delta_ms"] is None:
                    self.diagnostics_progress.emit(
                        f"连续流分析 {sample['index']}/{self.stream_scan_count}: "
                        f"scan_cnt={sample['scan_cnt']}，正在建立基线"
                    )
                else:
                    self.diagnostics_progress.emit(
                        f"连续流分析 {sample['index']}/{self.stream_scan_count}: "
                        f"scan_cnt={sample['scan_cnt']}，雷达间隔 {sample['timestamp_delta_ms']:.3f} ms"
                    )
        finally:
            self.radar_processor.finish_measurement_session()

        timestamp_delta_stats = calculate_float_stats(
            [item["timestamp_delta_ms"] for item in results if item["timestamp_delta_ms"] is not None]
        )
        host_interval_stats = calculate_float_stats(
            [item["host_interval_ms"] for item in results if item["host_interval_ms"] is not None]
        )
        host_minus_radar_stats = calculate_float_stats(
            [item["host_minus_radar_ms"] for item in results if item["host_minus_radar_ms"] is not None]
        )
        host_capture_minus_timestamp_stats = calculate_float_stats(
            [
                item["host_capture_minus_timestamp_ms"]
                for item in results
                if item["host_capture_minus_timestamp_ms"] is not None
            ]
        )
        summary = {
            "sample_count": len(results),
            "scan_step_errors": scan_step_errors,
            "timestamp_non_increasing": timestamp_non_increasing,
            "timestamp_delta_stats": timestamp_delta_stats,
            "host_interval_stats": host_interval_stats,
            "host_minus_radar_stats": host_minus_radar_stats,
            "host_capture_minus_timestamp_stats": host_capture_minus_timestamp_stats,
            "time_type": results[0]["time_type"] if results else None,
        }
        self.diagnostics_complete.emit("stream", {"results": results, "summary": summary})

    def run(self) -> None:
        try:
            if self.mode == "query":
                self._run_query_mode()
            elif self.mode == "stream":
                self._run_stream_mode()
            else:
                raise ValueError(f"不支持的诊断模式: {self.mode}")
        except Exception as exc:
            self.diagnostics_error.emit(self.mode, str(exc))


class C3BlindZoneApp(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.radar: C3BlindZoneProcessor | None = None
        self.connection_thread: ConnectionThread | None = None
        self.measurement_thread: MeasurementThread | None = None
        self.diagnostics_thread: TimestampDiagnosticsThread | None = None
        self.measurement_summary: dict | None = None
        self.measurement_transition_pending = False
        self.measurement_restart_cooldown_ms = 250
        self.current_diagnostics_headers: list[str] = []
        self.current_diagnostics_rows: list[list[str]] = []
        self.current_diagnostics_export_name = "c3_diagnostics_table"
        self._build_ui()

    def _build_ui(self) -> None:
        self.setWindowTitle("M盲区测试C3")
        self.setGeometry(120, 80, 1380, 920)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        self.tab_widget = QTabWidget()
        main_layout.addWidget(self.tab_widget)

        self.create_connection_tab()
        self.create_control_tab()
        self.create_data_tab()
        self.create_visualization_tab()
        self.create_statistics_tab()
        self.create_diagnostics_tab()

        self.status_bar = self.statusBar()
        self.status_bar.showMessage("就绪")

    def bind_thread_lifecycle(self, thread: QThread, finished_slot) -> None:
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(finished_slot)

    @pyqtSlot()
    def on_connection_thread_finished(self) -> None:
        thread = self.sender()
        if thread is self.connection_thread:
            self.connection_thread = None

    @pyqtSlot()
    def on_measurement_thread_finished(self) -> None:
        thread = self.sender()
        if thread is self.measurement_thread:
            self.measurement_thread = None
        QTimer.singleShot(self.measurement_restart_cooldown_ms, self.finish_measurement_transition)

    @pyqtSlot()
    def on_diagnostics_thread_finished(self) -> None:
        thread = self.sender()
        if thread is self.diagnostics_thread:
            self.diagnostics_thread = None
        self.reset_measurement_ui()

    @pyqtSlot()
    def finish_measurement_transition(self) -> None:
        if self.measurement_thread is not None and self.measurement_thread.isRunning():
            return
        self.measurement_transition_pending = False
        self.reset_measurement_ui()

    def create_connection_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        settings_group = QGroupBox("雷达连接设置")
        settings_layout = QVBoxLayout()

        ip_layout = QHBoxLayout()
        ip_layout.addWidget(QLabel("雷达 IP:"))
        self.ip_input = QLineEdit(DEFAULT_RADAR_IP)
        ip_layout.addWidget(self.ip_input)
        settings_layout.addLayout(ip_layout)

        ports_layout = QHBoxLayout()
        ports_layout.addWidget(QLabel(f"命令端口: {CMD_PORT}"))
        ports_layout.addWidget(QLabel(f"数据端口: {DATA_PORT}"))
        ports_layout.addStretch()
        settings_layout.addLayout(ports_layout)

        note_label = QLabel("连接后会按 C3 协议先切换 52000 输出格式为 0x03，再进行盲区测试。")
        note_label.setWordWrap(True)
        settings_layout.addWidget(note_label)

        settings_group.setLayout(settings_layout)
        layout.addWidget(settings_group)

        status_group = QGroupBox("连接状态")
        status_layout = QVBoxLayout()

        self.connection_status = QLabel("未连接")
        self.connection_status.setStyleSheet("color: red; font-weight: bold;")
        status_layout.addWidget(self.connection_status)

        self.connection_info = QLabel("")
        self.connection_info.setStyleSheet("color: gray;")
        status_layout.addWidget(self.connection_info)

        button_layout = QHBoxLayout()
        self.connect_btn = QPushButton("连接雷达")
        self.connect_btn.clicked.connect(self.connect_radar)
        button_layout.addWidget(self.connect_btn)

        self.disconnect_btn = QPushButton("断开连接")
        self.disconnect_btn.clicked.connect(self.disconnect_radar)
        self.disconnect_btn.setEnabled(False)
        button_layout.addWidget(self.disconnect_btn)
        button_layout.addStretch()
        status_layout.addLayout(button_layout)

        status_group.setLayout(status_layout)
        layout.addWidget(status_group)

        history_group = QGroupBox("连接历史")
        history_layout = QVBoxLayout()
        self.history_text = QTextEdit()
        self.history_text.setReadOnly(True)
        self.history_text.setMaximumHeight(160)
        self.history_text.setFont(QFont("Consolas", 9))
        history_layout.addWidget(self.history_text)
        history_group.setLayout(history_layout)
        layout.addWidget(history_group)

        layout.addStretch()
        self.tab_widget.addTab(tab, "连接")

    def create_control_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        params_group = QGroupBox("测量参数")
        params_layout = QVBoxLayout()

        range_layout = QHBoxLayout()
        range_layout.addWidget(QLabel("索引范围:"))
        self.start_index_input = QSpinBox()
        self.start_index_input.setRange(0, 5000)
        self.start_index_input.setValue(0)
        range_layout.addWidget(self.start_index_input)

        range_layout.addWidget(QLabel("-"))
        self.end_index_input = QSpinBox()
        self.end_index_input.setRange(0, 5000)
        self.end_index_input.setValue(120)
        range_layout.addWidget(self.end_index_input)
        range_layout.addStretch()
        params_layout.addLayout(range_layout)

        filter_layout = QHBoxLayout()
        self.enable_filter = QCheckBox("启用距离过滤")
        self.enable_filter.setChecked(True)
        filter_layout.addWidget(self.enable_filter)
        filter_layout.addWidget(QLabel("最大距离:"))
        self.max_distance_input = QSpinBox()
        self.max_distance_input.setRange(0, 10000)
        self.max_distance_input.setValue(2000)
        filter_layout.addWidget(self.max_distance_input)
        filter_layout.addWidget(QLabel("mm"))
        filter_layout.addStretch()
        params_layout.addLayout(filter_layout)

        self.enable_filter.stateChanged.connect(
            lambda state: self.max_distance_input.setEnabled(state == Qt.Checked)
        )

        iteration_layout = QHBoxLayout()
        iteration_layout.addWidget(QLabel("测量次数:"))
        self.iterations_input = QSpinBox()
        self.iterations_input.setRange(1, 10000)
        self.iterations_input.setValue(10)
        iteration_layout.addWidget(self.iterations_input)
        iteration_layout.addWidget(QLabel("次"))
        iteration_layout.addStretch()
        params_layout.addLayout(iteration_layout)

        frequency_layout = QHBoxLayout()
        frequency_layout.addWidget(QLabel("动态采样频率(Hz):"))
        self.radar_frequency_input = QDoubleSpinBox()
        self.radar_frequency_input.setDecimals(2)
        self.radar_frequency_input.setRange(0.1, 100.0)
        self.radar_frequency_input.setSingleStep(0.5)
        self.radar_frequency_input.setValue(15.0)
        frequency_layout.addWidget(self.radar_frequency_input)
        frequency_layout.addWidget(QLabel("周期:"))
        self.scan_period_label = QLabel("66.67 ms")
        frequency_layout.addWidget(self.scan_period_label)
        frequency_layout.addStretch()
        params_layout.addLayout(frequency_layout)

        self.radar_frequency_input.valueChanged.connect(self.update_scan_period_label)
        self.update_scan_period_label(self.radar_frequency_input.value())

        geometry_layout = QHBoxLayout()
        geometry_layout.addWidget(QLabel("角分辨率(°/点):"))
        self.angular_resolution_input = QDoubleSpinBox()
        self.angular_resolution_input.setDecimals(3)
        self.angular_resolution_input.setRange(0.001, 10.0)
        self.angular_resolution_input.setSingleStep(0.01)
        self.angular_resolution_input.setValue(0.33)
        geometry_layout.addWidget(self.angular_resolution_input)

        geometry_layout.addWidget(QLabel("扫描角度范围(°):"))
        self.scan_angle_range_input = QDoubleSpinBox()
        self.scan_angle_range_input.setDecimals(1)
        self.scan_angle_range_input.setRange(1.0, 360.0)
        self.scan_angle_range_input.setSingleStep(1.0)
        self.scan_angle_range_input.setValue(270.0)
        geometry_layout.addWidget(self.scan_angle_range_input)

        geometry_layout.addWidget(QLabel("起始角(°):"))
        self.start_angle_input = QDoubleSpinBox()
        self.start_angle_input.setDecimals(1)
        self.start_angle_input.setRange(-360.0, 360.0)
        self.start_angle_input.setSingleStep(1.0)
        self.start_angle_input.setValue(-45.0)
        geometry_layout.addWidget(self.start_angle_input)
        geometry_layout.addStretch()
        params_layout.addLayout(geometry_layout)

        self.estimated_points_label = QLabel("")
        self.index_angle_info_label = QLabel("")
        self.usage_note_label = QLabel(
            "说明: C3 连续流自带角度，程序会先按角度排序后重新编号；索引范围针对排序后的单圈点云。"
        )
        self.usage_note_label.setWordWrap(True)
        params_layout.addWidget(self.estimated_points_label)
        params_layout.addWidget(self.index_angle_info_label)
        params_layout.addWidget(self.usage_note_label)

        self.start_index_input.valueChanged.connect(self.update_scan_geometry_info)
        self.end_index_input.valueChanged.connect(self.update_scan_geometry_info)
        self.angular_resolution_input.valueChanged.connect(self.update_scan_geometry_info)
        self.scan_angle_range_input.valueChanged.connect(self.update_scan_geometry_info)
        self.start_angle_input.valueChanged.connect(self.update_scan_geometry_info)
        self.update_scan_geometry_info()

        params_group.setLayout(params_layout)
        layout.addWidget(params_group)

        control_group = QGroupBox("测量控制")
        control_layout = QVBoxLayout()

        self.progress_bar = QProgressBar()
        control_layout.addWidget(self.progress_bar)

        self.progress_label = QLabel("等待开始")
        control_layout.addWidget(self.progress_label)

        button_layout = QHBoxLayout()
        self.start_btn = QPushButton("开始测量")
        self.start_btn.clicked.connect(self.start_measurement)
        self.start_btn.setEnabled(False)
        button_layout.addWidget(self.start_btn)

        self.stop_btn = QPushButton("停止测量")
        self.stop_btn.clicked.connect(self.stop_measurement)
        self.stop_btn.setEnabled(False)
        button_layout.addWidget(self.stop_btn)

        self.repeat_btn = QPushButton("重复上次测量")
        self.repeat_btn.clicked.connect(self.repeat_last_measurement)
        self.repeat_btn.setEnabled(False)
        button_layout.addWidget(self.repeat_btn)
        button_layout.addStretch()

        control_layout.addLayout(button_layout)
        control_group.setLayout(control_layout)
        layout.addWidget(control_group)

        layout.addStretch()
        self.tab_widget.addTab(tab, "控制")

    def create_data_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        self.data_table = QTableWidget()
        self.data_table.setColumnCount(6)
        self.data_table.setHorizontalHeaderLabels(
            ["索引", "角度(°)", "距离(mm)", "反射率", "包序号", "scan_cnt"]
        )
        self.data_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.data_table.setAlternatingRowColors(True)
        self.data_table.setFont(QFont("Consolas", 10))
        layout.addWidget(self.data_table)

        stats_layout = QHBoxLayout()
        self.total_points_label = QLabel("总点数: 0")
        self.filtered_points_label = QLabel("命中点数: 0")
        self.qualified_label = QLabel("连续命中: 否")
        stats_layout.addWidget(self.total_points_label)
        stats_layout.addWidget(self.filtered_points_label)
        stats_layout.addWidget(self.qualified_label)
        stats_layout.addStretch()
        layout.addLayout(stats_layout)

        self.tab_widget.addTab(tab, "数据")

    def create_visualization_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        if PG_AVAILABLE:
            pg.setConfigOptions(antialias=False)

            self.distance_widget = pg.GraphicsLayoutWidget()
            self.distance_plot = self.distance_widget.addPlot(title="距离分布")
            self.distance_plot.setLabel("left", "距离 (mm)")
            self.distance_plot.setLabel("bottom", "索引")
            self.distance_curve = self.distance_plot.plot(pen="y")
            layout.addWidget(self.distance_widget)

            self.reflectivity_widget = pg.GraphicsLayoutWidget()
            self.reflectivity_plot = self.reflectivity_widget.addPlot(title="反射率分布")
            self.reflectivity_plot.setLabel("left", "反射率")
            self.reflectivity_plot.setLabel("bottom", "索引")
            self.reflectivity_curve = self.reflectivity_plot.plot(pen="g")
            layout.addWidget(self.reflectivity_widget)

            self.history_widget = pg.GraphicsLayoutWidget()
            self.history_plot = self.history_widget.addPlot(title="历史命中统计")
            self.history_plot.setLabel("left", "命中点数")
            self.history_plot.setLabel("bottom", "测量次数")
            self.history_curve = self.history_plot.plot(pen="r")
            layout.addWidget(self.history_widget)
        else:
            warning_label = QLabel("未安装 pyqtgraph，无法显示曲线。")
            warning_label.setAlignment(Qt.AlignCenter)
            layout.addWidget(warning_label)

        self.tab_widget.addTab(tab, "可视化")

    def create_statistics_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        self.stats_text = QTextEdit()
        self.stats_text.setReadOnly(True)
        self.stats_text.setFont(QFont("Consolas", 10))
        layout.addWidget(self.stats_text)

        self.tab_widget.addTab(tab, "统计")

    def create_diagnostics_tab(self) -> None:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        settings_group = QGroupBox("时间戳 / 码盘时间诊断")
        settings_layout = QVBoxLayout()

        query_layout = QHBoxLayout()
        query_layout.addWidget(QLabel("0x25 查询次数:"))
        self.query_iterations_input = QSpinBox()
        self.query_iterations_input.setRange(1, 1000)
        self.query_iterations_input.setValue(20)
        query_layout.addWidget(self.query_iterations_input)
        query_layout.addWidget(QLabel("查询间隔(ms):"))
        self.query_interval_input = QDoubleSpinBox()
        self.query_interval_input.setDecimals(1)
        self.query_interval_input.setRange(0.0, 5000.0)
        self.query_interval_input.setSingleStep(10.0)
        self.query_interval_input.setValue(80.0)
        query_layout.addWidget(self.query_interval_input)
        query_layout.addStretch()
        settings_layout.addLayout(query_layout)

        stream_layout = QHBoxLayout()
        stream_layout.addWidget(QLabel("52000 分析圈数:"))
        self.stream_scan_count_input = QSpinBox()
        self.stream_scan_count_input.setRange(2, 500)
        self.stream_scan_count_input.setValue(30)
        stream_layout.addWidget(self.stream_scan_count_input)
        stream_layout.addWidget(QLabel("说明: 将统计 scan_cnt 连续性、雷达时间戳间隔和主机接收间隔。"))
        stream_layout.addStretch()
        settings_layout.addLayout(stream_layout)

        note_label = QLabel(
            "说明: 0x25 返回的是码盘齿时间数组(单位 us)，52000 连续流里才有协议定义的圈级时间戳。"
        )
        note_label.setWordWrap(True)
        settings_layout.addWidget(note_label)

        button_layout = QHBoxLayout()
        self.query_diag_btn = QPushButton("执行 0x25 查询诊断")
        self.query_diag_btn.clicked.connect(self.start_query_diagnostics)
        self.query_diag_btn.setEnabled(False)
        button_layout.addWidget(self.query_diag_btn)

        self.stream_diag_btn = QPushButton("执行 52000 时间戳诊断")
        self.stream_diag_btn.clicked.connect(self.start_stream_diagnostics)
        self.stream_diag_btn.setEnabled(False)
        button_layout.addWidget(self.stream_diag_btn)

        self.stop_diag_btn = QPushButton("停止诊断")
        self.stop_diag_btn.clicked.connect(self.stop_diagnostics)
        self.stop_diag_btn.setEnabled(False)
        button_layout.addWidget(self.stop_diag_btn)

        self.export_diag_btn = QPushButton("导出表格")
        self.export_diag_btn.clicked.connect(self.export_diagnostics_table)
        self.export_diag_btn.setEnabled(False)
        button_layout.addWidget(self.export_diag_btn)
        button_layout.addStretch()
        settings_layout.addLayout(button_layout)

        self.diagnostics_status_label = QLabel("诊断状态: 待机")
        settings_layout.addWidget(self.diagnostics_status_label)

        settings_group.setLayout(settings_layout)
        layout.addWidget(settings_group)

        summary_group = QGroupBox("诊断摘要")
        summary_layout = QVBoxLayout()
        self.diagnostics_summary_text = QTextEdit()
        self.diagnostics_summary_text.setReadOnly(True)
        self.diagnostics_summary_text.setFont(QFont("Consolas", 10))
        self.diagnostics_summary_text.setMaximumHeight(220)
        self.diagnostics_summary_text.setText("尚未执行诊断。")
        summary_layout.addWidget(self.diagnostics_summary_text)
        summary_group.setLayout(summary_layout)
        layout.addWidget(summary_group)

        log_group = QGroupBox("诊断日志")
        log_layout = QVBoxLayout()
        self.diagnostics_log_text = QTextEdit()
        self.diagnostics_log_text.setReadOnly(True)
        self.diagnostics_log_text.setMaximumHeight(150)
        self.diagnostics_log_text.setFont(QFont("Consolas", 9))
        log_layout.addWidget(self.diagnostics_log_text)
        log_group.setLayout(log_layout)
        layout.addWidget(log_group)

        self.diagnostics_table = QTableWidget()
        self.diagnostics_table.setAlternatingRowColors(True)
        self.diagnostics_table.setFont(QFont("Consolas", 9))
        self.diagnostics_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.diagnostics_table)

        self.tab_widget.addTab(tab, "诊断")

    def add_to_history(self, message: str) -> None:
        current_time = time.strftime("%Y-%m-%d %H:%M:%S")
        self.history_text.append(f"[{current_time}] {message}")

    def update_scan_period_label(self, frequency_hz: float) -> None:
        if frequency_hz <= 0:
            self.scan_period_label.setText("--")
            return
        self.scan_period_label.setText(f"{1000.0 / frequency_hz:.2f} ms")

    def validate_ip_input(self, ip: str) -> bool:
        try:
            ipaddress.ip_address(ip)
            return True
        except ValueError:
            return False

    def update_scan_geometry_info(self) -> None:
        resolution = self.angular_resolution_input.value()
        scan_range = self.scan_angle_range_input.value()
        start_angle = self.start_angle_input.value()
        point_count = max(1, int(round(scan_range / resolution)) + 1) if resolution > 0 else 0

        if point_count <= 0:
            self.estimated_points_label.setText("整圈点数估算: --")
            self.index_angle_info_label.setText("当前索引角度范围: --")
            return

        start_index = self.start_index_input.value()
        end_index = self.end_index_input.value()
        index_start_angle = start_angle + start_index * resolution
        index_end_angle = start_angle + end_index * resolution
        payload_bytes = point_count * 6

        self.estimated_points_label.setText(
            f"整圈点数估算: {point_count} 点，整圈角度范围约 {start_angle:.2f}° 到 {start_angle + scan_range:.2f}°，"
            f"点载荷约 {payload_bytes} 字节"
        )
        self.index_angle_info_label.setText(
            f"当前索引范围对应角度约: {index_start_angle:.2f}° 到 {index_end_angle:.2f}°"
        )

    def set_connecting_state(self, is_connecting: bool) -> None:
        self.connect_btn.setEnabled(not is_connecting)
        self.disconnect_btn.setEnabled(False if is_connecting else self.radar is not None)
        self.ip_input.setEnabled(not is_connecting)
        if is_connecting:
            self.connection_status.setText("连接中...")
            self.connection_status.setStyleSheet("color: #d9822b; font-weight: bold;")

    @pyqtSlot(object)
    def handle_connection_complete(self, result: dict) -> None:
        self.set_connecting_state(False)

        if result["success"]:
            if self.radar and self.radar is not result["radar"]:
                self.radar.close()

            self.radar = result["radar"]
            self.connection_status.setText("已连接")
            self.connection_status.setStyleSheet("color: green; font-weight: bold;")
            self.connection_info.setText(
                f"IP: {result['host']} | 命令端口: {CMD_PORT} | 数据端口: {DATA_PORT}"
            )
            self.connect_btn.setEnabled(False)
            self.disconnect_btn.setEnabled(True)
            self.start_btn.setEnabled(True)
            self.query_diag_btn.setEnabled(True)
            self.stream_diag_btn.setEnabled(True)
            self.status_bar.showMessage(f"已连接到 C3 雷达 {result['host']}")
            self.add_to_history(f"连接成功: {result['host']}")
            return

        self.radar = None
        self.connection_status.setText("连接失败")
        self.connection_status.setStyleSheet("color: red; font-weight: bold;")
        self.connection_info.setText("")
        self.start_btn.setEnabled(False)
        self.query_diag_btn.setEnabled(False)
        self.stream_diag_btn.setEnabled(False)
        self.stop_diag_btn.setEnabled(False)
        self.disconnect_btn.setEnabled(False)
        self.status_bar.showMessage("连接失败")
        self.add_to_history(f"连接失败: {result['host']} - {result['error']}")
        QMessageBox.warning(self, "连接失败", f"连接雷达失败:\n{result['error']}")

    def connect_radar(self) -> None:
        ip = self.ip_input.text().strip()
        if not ip:
            QMessageBox.warning(self, "输入错误", "请输入雷达 IP")
            return
        if not self.validate_ip_input(ip):
            QMessageBox.warning(self, "输入错误", "IP 地址格式不正确")
            return
        if self.connection_thread and self.connection_thread.isRunning():
            return

        self.set_connecting_state(True)
        self.connection_info.setText(f"IP: {ip}")
        self.status_bar.showMessage(f"正在连接 {ip} ...")
        self.connection_thread = ConnectionThread(ip, connect_timeout=3.0)
        self.bind_thread_lifecycle(self.connection_thread, self.on_connection_thread_finished)
        self.connection_thread.connection_complete.connect(self.handle_connection_complete)
        self.connection_thread.start()

    def disconnect_radar(self) -> None:
        self.stop_measurement()
        self.stop_diagnostics()
        if self.diagnostics_thread and self.diagnostics_thread.isRunning():
            self.diagnostics_thread.wait(3000)
        if self.radar:
            self.radar.close()
            self.radar = None

        self.connection_status.setText("未连接")
        self.connection_status.setStyleSheet("color: red; font-weight: bold;")
        self.connection_info.setText("")
        self.connect_btn.setEnabled(True)
        self.disconnect_btn.setEnabled(False)
        self.start_btn.setEnabled(False)
        self.repeat_btn.setEnabled(False)
        self.query_diag_btn.setEnabled(False)
        self.stream_diag_btn.setEnabled(False)
        self.stop_diag_btn.setEnabled(False)
        self.status_bar.showMessage("已断开连接")
        self.add_to_history("已断开连接")
        console_log("[INFO] 已断开 C3 雷达连接")

    def start_measurement(self) -> None:
        if self.radar is None:
            QMessageBox.warning(self, "提示", "请先连接雷达")
            return
        if self.diagnostics_thread and self.diagnostics_thread.isRunning():
            QMessageBox.warning(self, "提示", "请先停止正在进行的诊断任务")
            return
        if self.measurement_thread and self.measurement_thread.isRunning():
            return
        if self.measurement_transition_pending:
            self.status_bar.showMessage("涓婁竴娆℃祴閲忔鍦ㄦ敹灏撅紝璇风◢鍚?...")
            return

        start_index = self.start_index_input.value()
        end_index = self.end_index_input.value()
        if end_index < start_index:
            QMessageBox.warning(self, "输入错误", "结束索引不能小于起始索引")
            return

        self.radar.configure_scan_parameters(
            angular_resolution_deg=self.angular_resolution_input.value(),
            scan_angle_range_deg=self.scan_angle_range_input.value(),
            start_angle_deg=self.start_angle_input.value(),
        )

        max_distance = self.max_distance_input.value() if self.enable_filter.isChecked() else None
        iterations = self.iterations_input.value()
        radar_frequency_hz = self.radar_frequency_input.value()
        current_settings = dict(self.radar.last_settings) if self.radar.last_settings else {}
        current_settings["radar_frequency_hz"] = radar_frequency_hz
        self.radar.last_settings = current_settings

        self.measurement_thread = MeasurementThread(
            self.radar,
            start_index,
            end_index,
            max_distance,
            iterations,
            radar_frequency_hz,
        )
        self.measurement_transition_pending = True
        self.bind_thread_lifecycle(self.measurement_thread, self.on_measurement_thread_finished)
        self.measurement_thread.measurement_progress.connect(self.update_progress)
        self.measurement_thread.measurement_complete.connect(self.measurement_completed)
        self.measurement_thread.measurement_error.connect(self.measurement_error)
        self.measurement_thread.start()

        self.progress_bar.setValue(0)
        self.progress_label.setText("开始测量...")
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.repeat_btn.setEnabled(False)
        self.disconnect_btn.setEnabled(False)
        self.query_diag_btn.setEnabled(False)
        self.stream_diag_btn.setEnabled(False)
        self.status_bar.showMessage("测量进行中")
        self.add_to_history(
            f"开始测量: 索引 {start_index}-{end_index}, 次数 {iterations}, "
            f"{'启用' if max_distance is not None else '未启用'}距离过滤, 动态频率 {radar_frequency_hz:.2f} Hz"
        )
        console_log(
            f"[INFO] 启动测量: 索引 {start_index}-{end_index}, 次数 {iterations}, "
            f"{'启用' if max_distance is not None else '未启用'}距离过滤, 动态频率 {radar_frequency_hz:.2f} Hz"
        )

    def repeat_last_measurement(self) -> None:
        if self.radar is None or not self.radar.last_settings:
            return

        settings = self.radar.last_settings
        self.start_index_input.setValue(settings["start_index"])
        self.end_index_input.setValue(settings["end_index"])
        self.iterations_input.setValue(settings.get("iterations", 1))
        self.radar_frequency_input.setValue(settings.get("radar_frequency_hz", 15.0))
        self.angular_resolution_input.setValue(settings.get("angular_resolution_deg", 0.33))
        self.scan_angle_range_input.setValue(settings.get("scan_angle_range_deg", 270.0))
        self.start_angle_input.setValue(settings.get("start_angle_deg", -45.0))

        max_distance = settings.get("max_distance")
        self.enable_filter.setChecked(max_distance is not None)
        if max_distance is not None:
            self.max_distance_input.setValue(max_distance)

        self.start_measurement()

    def stop_measurement(self) -> None:
        if self.measurement_thread and self.measurement_thread.isRunning():
            self.measurement_thread.stop()
            self.progress_label.setText("正在停止...")
            self.stop_btn.setEnabled(False)
            self.status_bar.showMessage("正在停止测量")
            console_log("[INFO] 请求停止测量")

    def reset_measurement_ui(self) -> None:
        measurement_running = self.measurement_thread is not None and self.measurement_thread.isRunning()
        diagnostics_running = self.diagnostics_thread is not None and self.diagnostics_thread.isRunning()
        actions_enabled = (
            self.radar is not None
            and not measurement_running
            and not diagnostics_running
            and not self.measurement_transition_pending
        )
        self.start_btn.setEnabled(actions_enabled)
        self.stop_btn.setEnabled(False)
        self.repeat_btn.setEnabled(actions_enabled and bool(self.radar.last_settings))
        self.disconnect_btn.setEnabled(actions_enabled)
        self.query_diag_btn.setEnabled(actions_enabled)
        self.stream_diag_btn.setEnabled(actions_enabled)
        self.stop_diag_btn.setEnabled(diagnostics_running)
        self.export_diag_btn.setEnabled(not diagnostics_running and bool(self.current_diagnostics_rows))

    @pyqtSlot(int, int, int, bool, float)
    def update_progress(
        self,
        current: int,
        total: int,
        filtered_count: int,
        has_consecutive: bool,
        time_ms: float,
    ) -> None:
        progress = int(current / total * 100) if total else 0
        self.progress_bar.setValue(progress)
        status_text = "连续命中" if has_consecutive else "未连续命中"
        self.progress_label.setText(
            f"第 {current}/{total} 次 | 命中点 {filtered_count} | {status_text} | 耗时 {time_ms:.1f} ms"
        )

    @pyqtSlot(object)
    def measurement_completed(self, summary: dict) -> None:
        self.measurement_summary = summary
        self.reset_measurement_ui()
        self.progress_bar.setValue(100 if summary["requested_iterations"] else 0)
        self.progress_label.setText(
            f"测量完成 | 成功 {summary['success_count']} 次 | 连续命中 {summary['qualified_circles']} 次"
        )
        self.status_bar.showMessage("测量完成")
        self.add_to_history(
            f"测量完成: 成功 {summary['success_count']} 次, 连续命中 {summary['qualified_circles']} 次"
        )
        console_log(
            f"[INFO] 测量完成: 成功 {summary['success_count']} 次, 连续命中 {summary['qualified_circles']} 次"
        )

        latest_measurement = summary.get("latest_measurement")
        if latest_measurement:
            self.update_data_display(latest_measurement)
        self.update_statistics_summary(summary)
        self.update_visualizations_summary(summary)

    @pyqtSlot(str)
    def measurement_error(self, error_msg: str) -> None:
        self.reset_measurement_ui()
        self.progress_label.setText("测量失败")
        self.status_bar.showMessage(f"测量失败: {error_msg}")
        self.add_to_history(f"测量失败: {error_msg}")
        console_log(f"[ERROR] 测量失败: {error_msg}")
        QMessageBox.critical(self, "测量错误", error_msg)

    def format_host_ns(self, host_ns: int | None) -> str:
        if host_ns is None:
            return "-"
        seconds = host_ns / 1_000_000_000.0
        return (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(seconds))
            + f".{int((host_ns // 1_000_000) % 1000):03d}"
        )

    def format_radar_timestamp_readable(self, timestamp: int | None, time_type: int | None) -> str:
        if timestamp is None:
            return "-"
        timestamp_ms = convert_c3_timestamp_delta_to_ms(int(timestamp), time_type)
        return self._format_duration_ms(timestamp_ms)

    @staticmethod
    def _format_duration_ms(total_ms: float) -> str:
        if total_ms < 0:
            total_ms = 0.0

        total_ms_int = int(round(total_ms))
        days, remainder_ms = divmod(total_ms_int, 86_400_000)
        hours, remainder_ms = divmod(remainder_ms, 3_600_000)
        minutes, remainder_ms = divmod(remainder_ms, 60_000)
        seconds, milliseconds = divmod(remainder_ms, 1_000)

        if days > 0:
            return f"{days}d {hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"

    def populate_diagnostics_table(
        self,
        headers: list[str],
        rows: list[list[str]],
        *,
        export_name: str | None = None,
    ) -> None:
        self.current_diagnostics_headers = [str(header) for header in headers]
        self.current_diagnostics_rows = [[str(value) for value in row] for row in rows]
        if export_name:
            self.current_diagnostics_export_name = export_name

        self.diagnostics_table.setUpdatesEnabled(False)
        try:
            self.diagnostics_table.clearContents()
            self.diagnostics_table.setColumnCount(len(headers))
            self.diagnostics_table.setHorizontalHeaderLabels(headers)
            self.diagnostics_table.setRowCount(len(rows))
            for row_index, row_values in enumerate(rows):
                for column_index, value in enumerate(row_values):
                    item = QTableWidgetItem(str(value))
                    item.setTextAlignment(Qt.AlignCenter)
                    self.diagnostics_table.setItem(row_index, column_index, item)
        finally:
            self.diagnostics_table.setUpdatesEnabled(True)
            self.diagnostics_table.viewport().update()
            self.diagnostics_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.export_diag_btn.setEnabled(
            not (self.diagnostics_thread is not None and self.diagnostics_thread.isRunning())
            and bool(self.current_diagnostics_rows)
        )

    def export_diagnostics_table(self) -> None:
        if not self.current_diagnostics_headers or not self.current_diagnostics_rows:
            QMessageBox.information(self, "提示", "当前没有可导出的诊断表格数据")
            return

        default_name = f"{self.current_diagnostics_export_name}_{time.strftime('%Y%m%d_%H%M%S')}.csv"
        filename, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "导出诊断表格",
            str(ROOT_DIR / default_name),
            "CSV Files (*.csv);;All Files (*.*)",
        )
        if not filename:
            return

        with open(filename, "w", newline="", encoding="utf-8-sig") as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(self.current_diagnostics_headers)
            writer.writerows(self.current_diagnostics_rows)

        self.status_bar.showMessage(f"诊断表格已导出: {filename}")
        self.add_to_history(f"导出诊断表格: {filename}")
        console_log(f"[INFO] 诊断表格已导出: {filename}")

    def start_query_diagnostics(self) -> None:
        if self.radar is None:
            QMessageBox.warning(self, "提示", "请先连接雷达")
            return
        if self.measurement_thread and self.measurement_thread.isRunning():
            QMessageBox.warning(self, "提示", "请先停止正在进行的测量")
            return
        if self.diagnostics_thread and self.diagnostics_thread.isRunning():
            return

        self.diagnostics_log_text.clear()
        self.diagnostics_summary_text.setText("正在执行 0x25 码盘时间查询诊断...")
        self.diagnostics_status_label.setText("诊断状态: 0x25 查询诊断进行中...")
        self.status_bar.showMessage("正在执行 0x25 查询诊断")
        self.add_to_history("启动 0x25 码盘时间查询诊断")

        self.diagnostics_thread = TimestampDiagnosticsThread(
            self.radar,
            "query",
            query_iterations=self.query_iterations_input.value(),
            query_interval_ms=self.query_interval_input.value(),
        )
        self.bind_thread_lifecycle(self.diagnostics_thread, self.on_diagnostics_thread_finished)
        self.diagnostics_thread.diagnostics_progress.connect(self.append_diagnostics_log)
        self.diagnostics_thread.diagnostics_complete.connect(self.handle_diagnostics_complete)
        self.diagnostics_thread.diagnostics_error.connect(self.handle_diagnostics_error)
        self.reset_measurement_ui()
        self.query_diag_btn.setEnabled(False)
        self.stream_diag_btn.setEnabled(False)
        self.stop_diag_btn.setEnabled(True)
        self.start_btn.setEnabled(False)
        self.repeat_btn.setEnabled(False)
        self.disconnect_btn.setEnabled(False)
        self.diagnostics_thread.start()

    def start_stream_diagnostics(self) -> None:
        if self.radar is None:
            QMessageBox.warning(self, "提示", "请先连接雷达")
            return
        if self.measurement_thread and self.measurement_thread.isRunning():
            QMessageBox.warning(self, "提示", "请先停止正在进行的测量")
            return
        if self.diagnostics_thread and self.diagnostics_thread.isRunning():
            return

        self.diagnostics_log_text.clear()
        self.diagnostics_summary_text.setText("正在执行 52000 连续流时间戳诊断...")
        self.diagnostics_status_label.setText("诊断状态: 52000 时间戳诊断进行中...")
        self.status_bar.showMessage("正在执行 52000 连续流时间戳诊断")
        self.add_to_history("启动 52000 连续流时间戳诊断")

        self.diagnostics_thread = TimestampDiagnosticsThread(
            self.radar,
            "stream",
            stream_scan_count=self.stream_scan_count_input.value(),
        )
        self.bind_thread_lifecycle(self.diagnostics_thread, self.on_diagnostics_thread_finished)
        self.diagnostics_thread.diagnostics_progress.connect(self.append_diagnostics_log)
        self.diagnostics_thread.diagnostics_complete.connect(self.handle_diagnostics_complete)
        self.diagnostics_thread.diagnostics_error.connect(self.handle_diagnostics_error)
        self.reset_measurement_ui()
        self.query_diag_btn.setEnabled(False)
        self.stream_diag_btn.setEnabled(False)
        self.stop_diag_btn.setEnabled(True)
        self.start_btn.setEnabled(False)
        self.repeat_btn.setEnabled(False)
        self.disconnect_btn.setEnabled(False)
        self.diagnostics_thread.start()

    def stop_diagnostics(self) -> None:
        if self.diagnostics_thread and self.diagnostics_thread.isRunning():
            self.diagnostics_thread.stop()
            self.diagnostics_status_label.setText("诊断状态: 正在停止...")
            self.stop_diag_btn.setEnabled(False)
            self.status_bar.showMessage("正在停止诊断")

    @pyqtSlot(str)
    def append_diagnostics_log(self, message: str) -> None:
        current_time = time.strftime("%Y-%m-%d %H:%M:%S")
        self.diagnostics_log_text.append(f"[{current_time}] {message}")
        console_log(f"[DIAG] {message}")

    @pyqtSlot(str, object)
    def handle_diagnostics_complete(self, mode: str, payload: dict) -> None:
        self.reset_measurement_ui()
        if mode == "query":
            self.populate_query_diagnostics(payload)
            self.diagnostics_status_label.setText("诊断状态: 0x25 查询诊断完成")
            self.status_bar.showMessage("0x25 查询诊断完成")
            self.add_to_history("0x25 码盘时间查询诊断完成")
            console_log("[INFO] 0x25 码盘时间查询诊断完成")
        elif mode == "stream":
            self.populate_stream_diagnostics(payload)
            self.diagnostics_status_label.setText("诊断状态: 52000 时间戳诊断完成")
            self.status_bar.showMessage("52000 时间戳诊断完成")
            self.add_to_history("52000 连续流时间戳诊断完成")
            console_log("[INFO] 52000 连续流时间戳诊断完成")
        else:
            self.diagnostics_status_label.setText("诊断状态: 已完成")

    @pyqtSlot(str, str)
    def handle_diagnostics_error(self, mode: str, error_msg: str) -> None:
        self.reset_measurement_ui()
        self.diagnostics_status_label.setText("诊断状态: 失败")
        self.status_bar.showMessage(f"诊断失败: {error_msg}")
        self.add_to_history(f"{mode} 诊断失败: {error_msg}")
        console_log(f"[ERROR] {mode} 诊断失败: {error_msg}")
        QMessageBox.critical(self, "诊断错误", error_msg)

    def populate_query_diagnostics(self, payload: dict) -> None:
        results = payload.get("results", [])
        summary = payload.get("summary", {})
        latency_stats = summary.get("latency_stats", {})
        encoder_total_stats = summary.get("encoder_total_stats", {})
        host_interval_stats = summary.get("host_interval_stats", {})
        host_minus_encoder_stats = summary.get("host_minus_encoder_stats", {})

        summary_text = (
            f"{'=' * 72}\n"
            f"0x25 码盘时间查询诊断\n"
            f"{'=' * 72}\n"
            f"样本数: {summary.get('sample_count', 0)}\n"
            f"每次返回数值个数: {summary.get('value_count', 0)}\n"
            f"查询时延平均/最小/最大: {latency_stats.get('avg', 0.0):.3f} / "
            f"{latency_stats.get('min', 0.0):.3f} / {latency_stats.get('max', 0.0):.3f} ms\n"
            f"查询时延抖动: {latency_stats.get('jitter', 0.0):.3f} ms\n"
            f"码盘总时间平均/最小/最大: {encoder_total_stats.get('avg', 0.0):.3f} / "
            f"{encoder_total_stats.get('min', 0.0):.3f} / {encoder_total_stats.get('max', 0.0):.3f} ms\n"
            f"主机相邻回复间隔平均: {host_interval_stats.get('avg', 0.0):.3f} ms\n"
            f"主机间隔 - 码盘总时间 平均: {host_minus_encoder_stats.get('avg', 0.0):.3f} ms\n"
            f"主机间隔 - 码盘总时间 抖动: {host_minus_encoder_stats.get('jitter', 0.0):.3f} ms\n"
            f"{'=' * 72}"
        )
        self.diagnostics_summary_text.setText(summary_text)
        console_log(summary_text)

        headers = [
            "序号",
            "主机接收时间",
            "时延(ms)",
            "值个数",
            "码盘总时间(ms)",
            "平均齿时间(us)",
            "主机相邻间隔(ms)",
            "主机间隔-码盘(ms)",
        ]
        rows = []
        for item in results:
            rows.append(
                [
                    str(item["index"]),
                    self.format_host_ns(item.get("receive_host_ns")),
                    f"{item['latency_ms']:.3f}",
                    str(item["value_count"]),
                    f"{item['encoder_total_ms']:.3f}",
                    f"{item['encoder_mean_us']:.3f}",
                    "-" if item["host_interval_ms"] is None else f"{item['host_interval_ms']:.3f}",
                    "-" if item["host_minus_encoder_ms"] is None else f"{item['host_minus_encoder_ms']:.3f}",
                ]
            )
        self.populate_diagnostics_table(headers, rows, export_name="c3_query_diagnostics")

    def populate_stream_diagnostics(self, payload: dict) -> None:
        results = payload.get("results", [])
        summary = payload.get("summary", {})
        radar_stats = summary.get("timestamp_delta_stats", {})
        host_stats = summary.get("host_interval_stats", {})
        offset_stats = summary.get("host_minus_radar_stats", {})
        capture_compare_stats = summary.get("host_capture_minus_timestamp_stats", {})

        summary_text = (
            f"{'=' * 72}\n"
            f"52000 连续流时间戳诊断\n"
            f"{'=' * 72}\n"
            f"采样圈数: {summary.get('sample_count', 0)}\n"
            f"time_type: {summary.get('time_type', '-')}\n"
            f"scan_cnt 步进异常次数: {summary.get('scan_step_errors', 0)}\n"
            f"时间戳非递增次数: {summary.get('timestamp_non_increasing', 0)}\n"
            f"雷达圈间隔平均/最小/最大: {radar_stats.get('avg', 0.0):.3f} / "
            f"{radar_stats.get('min', 0.0):.3f} / {radar_stats.get('max', 0.0):.3f} ms\n"
            f"雷达圈间隔抖动: {radar_stats.get('jitter', 0.0):.3f} ms\n"
            f"主机接收圈间隔平均: {host_stats.get('avg', 0.0):.3f} ms\n"
            f"主机-雷达间隔差平均: {offset_stats.get('avg', 0.0):.3f} ms\n"
            f"主机-雷达间隔差抖动: {offset_stats.get('jitter', 0.0):.3f} ms\n"
            f"相对首圈抓包-时间戳平均/最小/最大: {capture_compare_stats.get('avg', 0.0):.3f} / "
            f"{capture_compare_stats.get('min', 0.0):.3f} / {capture_compare_stats.get('max', 0.0):.3f} ms\n"
            f"相对首圈抓包-时间戳抖动: {capture_compare_stats.get('jitter', 0.0):.3f} ms\n"
            f"{'=' * 72}"
        )
        self.diagnostics_summary_text.setText(summary_text)
        console_log(summary_text)

        headers = [
            "序号",
            "scan_cnt",
            "包数",
            "点数",
            "时间戳原值",
            "抓包时间",
            "时间戳相对首圈(ms)",
            "抓包相对首圈(ms)",
            "抓包-时间戳(ms)",
            "雷达间隔(ms)",
            "主机间隔(ms)",
            "主机-雷达(ms)",
            "scan步进",
        ]
        headers.insert(5, "时间戳可读时间")
        rows = []
        for item in results:
            rows.append(
                [
                    str(item["index"]),
                    str(item["scan_cnt"]),
                    str(item["frame_count"]),
                    str(item["point_count"]),
                    str(item["timestamp"]),
                    self.format_radar_timestamp_readable(item.get("timestamp"), item.get("time_type")),
                    self.format_host_ns(item.get("first_host_recv_ns")),
                    "-" if item["timestamp_elapsed_ms"] is None else f"{item['timestamp_elapsed_ms']:.3f}",
                    "-" if item["host_capture_elapsed_ms"] is None else f"{item['host_capture_elapsed_ms']:.3f}",
                    "-"
                    if item["host_capture_minus_timestamp_ms"] is None
                    else f"{item['host_capture_minus_timestamp_ms']:.3f}",
                    "-" if item["timestamp_delta_ms"] is None else f"{item['timestamp_delta_ms']:.3f}",
                    "-" if item["host_interval_ms"] is None else f"{item['host_interval_ms']:.3f}",
                    "-" if item["host_minus_radar_ms"] is None else f"{item['host_minus_radar_ms']:.3f}",
                    "-" if item["scan_step"] is None else str(item["scan_step"]),
                ]
            )
        self.populate_diagnostics_table(headers, rows, export_name="c3_stream_diagnostics")

    def update_data_display(self, measurement: dict) -> None:
        self.populate_data_table_fast(measurement)

    def populate_data_table_fast(self, measurement: dict) -> None:
        rows = measurement.get("results", [])
        self.data_table.setUpdatesEnabled(False)
        try:
            self.data_table.clearContents()
            self.data_table.setRowCount(len(rows))
            for row, result in enumerate(rows):
                values = (
                    result["index"],
                    f"{result['angle_deg']:.2f}",
                    result["measured_distance"],
                    result["reflectivity"],
                    result["seq_num"],
                    result["scan_cnt"],
                )
                for column, value in enumerate(values):
                    item = QTableWidgetItem(str(value))
                    item.setTextAlignment(Qt.AlignCenter)
                    self.data_table.setItem(row, column, item)
        finally:
            self.data_table.setUpdatesEnabled(True)
            self.data_table.viewport().update()

        self.total_points_label.setText(f"总点数: {measurement['total_count']}")
        self.filtered_points_label.setText(f"命中点数: {measurement['filtered_count']}")
        self.qualified_label.setText(
            f"连续命中: {'是' if measurement['has_consecutive_qualified'] else '否'}"
        )

    def update_statistics_summary(self, summary: dict) -> None:
        if not summary:
            return

        latest = summary.get("latest_measurement") or {}
        requested_iterations = summary.get("requested_iterations", 0)
        success_count = summary.get("success_count", 0)
        qualified_circles = summary.get("qualified_circles", 0)
        total_points_all = summary.get("total_points_all", 0)
        filtered_points_all = summary.get("filtered_points_all", 0)
        total_time = summary.get("total_time", 0.0)

        hit_ratio = filtered_points_all / total_points_all * 100 if total_points_all else 0.0
        qualified_ratio = qualified_circles / success_count * 100 if success_count else 0.0
        avg_time_ms = total_time / success_count * 1000 if success_count else 0.0
        measure_frequency = success_count / total_time if total_time > 0 else 0.0

        stats_text = (
            f"{'=' * 72}\n"
            f"M盲区测试C3 统计摘要\n"
            f"{'=' * 72}\n"
            f"请求测量次数: {requested_iterations}\n"
            f"成功测量次数: {success_count}\n"
            f"连续命中圈数: {qualified_circles}\n"
            f"连续命中占比: {qualified_ratio:.2f}%\n"
            f"累计总点数: {total_points_all}\n"
            f"累计命中点数: {filtered_points_all}\n"
            f"累计命中占比: {hit_ratio:.2f}%\n"
            f"总耗时: {total_time:.2f} s\n"
            f"平均单次耗时: {avg_time_ms:.1f} ms\n"
            f"平均测量频率: {measure_frequency:.2f} 次/s\n"
            f"{'-' * 72}\n"
            f"最新单圈 scan_cnt: {latest.get('scan_cnt', '-')}\n"
            f"最新单圈包数: {latest.get('frame_count', '-')}\n"
            f"最新单圈点数: {latest.get('raw_point_count', '-')}\n"
            f"包序号范围: {latest.get('first_seq_num', '-')} -> {latest.get('last_seq_num', '-')}\n"
            f"角度范围: {latest.get('angle_min_deg', 0.0):.2f}° -> {latest.get('angle_max_deg', 0.0):.2f}°\n"
            f"估算角分辨率: {latest.get('angular_resolution_deg', 0.0):.3f}°/点\n"
            f"本次索引范围: {latest.get('start_index', '-')} -> {latest.get('end_index', '-')}\n"
            f"本次命中点数: {latest.get('filtered_count', 0)} / {latest.get('total_count', 0)}\n"
            f"本次连续命中: {'是' if latest.get('has_consecutive_qualified') else '否'}\n"
            f"{'=' * 72}"
        )
        self.stats_text.setText(stats_text)

    def update_visualizations_summary(self, summary: dict) -> None:
        if not PG_AVAILABLE:
            return

        latest = summary.get("latest_measurement")
        if not latest:
            return

        plot_points = latest.get("all_points") or latest.get("results") or []
        if plot_points:
            indices = [point["index"] for point in plot_points]
            distances = [point["measured_distance"] for point in plot_points]
            reflectivities = [point["reflectivity"] for point in plot_points]
            self.distance_curve.setData(indices, distances)
            self.reflectivity_curve.setData(indices, reflectivities)

        if summary.get("iteration_numbers"):
            self.history_curve.setData(summary["iteration_numbers"], summary["filtered_counts"])

    def closeEvent(self, event) -> None:  # noqa: N802
        self.stop_measurement()
        self.stop_diagnostics()
        if self.measurement_thread and self.measurement_thread.isRunning():
            self.measurement_thread.wait(3000)
        if self.diagnostics_thread and self.diagnostics_thread.isRunning():
            self.diagnostics_thread.wait(3000)
        if self.connection_thread and self.connection_thread.isRunning():
            self.connection_thread.wait(3000)
        if self.radar:
            self.radar.close()
        event.accept()


def main() -> None:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = C3BlindZoneApp()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
