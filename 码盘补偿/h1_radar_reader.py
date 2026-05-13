"""
H1 标定取数：指令与每点 8 字节布局与《盲区测试高分辨率桌面版本》一致；
若固件在点云载荷前带固定长度帧头（与《高分辨率盲区测试-C2H1如30HZ 0.1》
中 header_size=6 一致），接收与解析需跳过该前缀，否则按 idx*8 从包首取块会
整体错位，反射率等字段会呈非物理的规律（例如随索引近似单调）。
默认 calibration_header_size=0。
若回包为 H1 常见定长帧（前 4 字节 02 02 02 02、第 5–6 字节为大端整包长度），
会在解析前自动截取该长度并去掉 6 字节头再按点解析，避免把帧头当点 0 导致反射率随索引假递增。
若载荷前另有固定填充，请把 calibration_header_size 设为该长度（勿与自动去帧头重复叠加）。
"""
from __future__ import annotations

import socket
import time
from typing import Any


class H1CalibrationRadar:
    """TCP 连接雷达，发送标定取数指令并解析指定索引范围的点云片段。"""

    DEFAULT_CMD_HEX = "02 02 02 02 00 09 02 64 77"
    _FRAME_MAGIC = b"\x02\x02\x02\x02"
    def __init__(self, host: str = "192.168.0.240", port: int = 2111, connect_timeout: float = 3.0) -> None:
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.socket: socket.socket | None = None
        self.last_error = ""
        self.block_size = 8
        #: 标定 TCP 回包中点云载荷前的固定字节数；0 与盲区桌面版一致，6 见 C2H1 另一接收脚本
        self.calibration_header_size = 0
        self.angular_resolution_deg = 0.100
        self.scan_angle_range_deg = 270.0
        self.start_angle_deg = -45.0
        self.expected_point_count = 2701
        self.expected_data_size = self.expected_point_count * self.block_size
        self.first_packet_timeout = 0.12
        self.idle_packet_timeout = 0.015
        self.max_receive_window = 0.25
        self.packet_drain_timeout = 0.02

    def full_calibration_wire_bytes(self) -> int:
        """单帧标定数据在链路上的总长度：帧头 + 整圈点云载荷。"""
        return self.calibration_header_size + self.expected_data_size

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
        self.expected_point_count = max(
            1,
            int(round(self.scan_angle_range_deg / self.angular_resolution_deg)) + 1,
        )
        self.expected_data_size = self.expected_point_count * self.block_size

    def calculate_required_bytes(self, end_index: int) -> int:
        """本帧至少需要从套接字读取的字节数（含帧头 + 至 end_index 的载荷）。"""
        target_blocks = max(1, end_index + 1)
        payload = min(self.expected_data_size, target_blocks * self.block_size)
        if self.calibration_header_size > 0:
            return self.calibration_header_size + payload
        # header=0 时多预留 6 字节，便于收齐「魔数+长度」前缀的定长帧
        return 6 + payload

    def _length_prefixed_frame_total(self, buf: bytes) -> int | None:
        """若为 H1 常见定长帧头，返回声明的整包字节数 L（含前 6 字节），否则 None。"""
        if len(buf) < 6 or buf[:4] != self._FRAME_MAGIC:
            return None
        L = int.from_bytes(buf[4:6], byteorder="big")
        if L < 6 + self.block_size or (L - 6) % self.block_size != 0:
            return None
        if L > len(buf) + 65536:
            return None
        return L

    def peel_length_prefixed_points_payload(self, raw: bytes) -> bytes | None:
        """
        当 calibration_header_size==0 且缓冲符合定长帧时，返回纯点云字节（长度 L-6，且为 8 的倍数）。
        显式设置了 calibration_header_size>0 时不调用（由 parse 侧按字节跳过）。
        """
        if self.calibration_header_size != 0:
            return None
        L = self._length_prefixed_frame_total(raw)
        if L is None or L > len(raw):
            return None
        return raw[6:L]

    def prepare_calibration_parse_buffer(self, raw: bytes) -> bytes:
        """接收完成后得到的一帧原始字节 → 交给 parse_data_range_fast 的缓冲。"""
        peeled = self.peel_length_prefixed_points_payload(raw)
        if peeled is not None:
            return peeled
        return raw

    def drain_current_packet_tail(self, already_received: int, frame_total_bytes: int | None = None) -> None:
        if not self.socket:
            return
        full_wire = frame_total_bytes if frame_total_bytes is not None else self.full_calibration_wire_bytes()
        remaining_budget = max(0, full_wire - already_received)
        if remaining_budget <= 0:
            return
        previous_timeout = None
        try:
            previous_timeout = self.socket.gettimeout()
            self.socket.settimeout(self.packet_drain_timeout)
            drained_bytes = 0
            while drained_bytes < remaining_budget:
                chunk = self.socket.recv(min(8192, remaining_budget - drained_bytes))
                if not chunk:
                    break
                drained_bytes += len(chunk)
        except socket.timeout:
            pass
        except OSError:
            pass
        finally:
            try:
                if previous_timeout is not None:
                    self.socket.settimeout(previous_timeout)
            except OSError:
                pass

    def connect_radar(self) -> bool:
        try:
            self.last_error = ""
            if self.socket is not None:
                return True
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 128 * 1024)
            self.socket.settimeout(self.connect_timeout)
            self.socket.connect((self.host, self.port))
            return True
        except OSError as e:
            self.last_error = str(e)
            self.close()
            return False

    def ensure_connection(self) -> bool:
        return self.socket is not None or self.connect_radar()

    def drain_pending_data(self) -> None:
        if not self.socket:
            return
        try:
            previous_timeout = self.socket.gettimeout()
            self.socket.setblocking(False)
            drain_start = time.perf_counter()
            drained_bytes = 0
            max_drain_window = 0.03
            max_drain_bytes = 64 * 1024
            while True:
                if (time.perf_counter() - drain_start) >= max_drain_window:
                    break
                if drained_bytes >= max_drain_bytes:
                    break
                try:
                    chunk = self.socket.recv(8192)
                    if not chunk:
                        break
                    drained_bytes += len(chunk)
                except BlockingIOError:
                    break
        except OSError:
            pass
        finally:
            try:
                self.socket.setblocking(True)
                self.socket.settimeout(previous_timeout)
            except OSError:
                pass

    def send_command(self, command_hex: str | None = None) -> bool:
        try:
            if not self.ensure_connection():
                return False
            self.drain_pending_data()
            hexs = (command_hex or self.DEFAULT_CMD_HEX).replace(" ", "")
            self.socket.send(bytes.fromhex(hexs))
            return True
        except OSError as e:
            self.last_error = str(e)
            return False

    def receive_radar_data_complete_fast(self, required_bytes: int | None = None) -> bytes | None:
        if not self.socket:
            return None
        try:
            target_size = max(
                self.block_size,
                required_bytes or self.full_calibration_wire_bytes(),
            )
            all_data = bytearray()
            start_time = time.perf_counter()
            last_data_time: float | None = None
            self.socket.settimeout(self.first_packet_timeout)
            while time.perf_counter() - start_time < self.max_receive_window:
                try:
                    chunk = self.socket.recv(min(8192, max(self.block_size, target_size - len(all_data))))
                    if not chunk:
                        break
                    all_data.extend(chunk)
                    last_data_time = time.perf_counter()
                    self.socket.settimeout(self.idle_packet_timeout)
                    if len(all_data) >= target_size:
                        break
                except socket.timeout:
                    if all_data:
                        if last_data_time and (time.perf_counter() - last_data_time) >= self.idle_packet_timeout:
                            break
                        break
                    return None
            if not all_data:
                return None
            raw_b = bytes(all_data)
            frame_total = self._length_prefixed_frame_total(raw_b)
            full_wire = frame_total if frame_total is not None else self.full_calibration_wire_bytes()
            if target_size < full_wire:
                self.drain_current_packet_tail(len(all_data), frame_total)
            return raw_b
        except OSError:
            self.close()
            return None

    def parse_data_range_fast(self, radar_data: bytes, start_index: int, end_index: int) -> list[dict[str, Any]]:
        if not radar_data:
            return []
        h = self.calibration_header_size
        if h < 0 or h > len(radar_data):
            return []
        payload = memoryview(radar_data)[h:]
        total_blocks = len(payload) // self.block_size
        if total_blocks == 0:
            return []
        actual_end = min(end_index, total_blocks - 1)
        actual_start = max(0, min(start_index, total_blocks - 1))
        results: list[dict[str, Any]] = []
        view = payload
        for idx in range(actual_start, actual_end + 1):
            offset = idx * self.block_size
            block = view[offset : offset + self.block_size]
            results.append(
                {
                    "index": idx,
                    "angle_deg": self.start_angle_deg + idx * self.angular_resolution_deg, # 手动计算角度
                    "measured_distance": int.from_bytes(block[4:6], byteorder="big"), # 距离（毫米）
                    "front_edge": int.from_bytes(block[0:2], byteorder="big"), # 前沿
                    "back_edge": int.from_bytes(block[2:4], byteorder="big"), # 后沿
                    "reflectivity": int.from_bytes(block[6:8], byteorder="big"), # 反射率
                }
            )
        return results

    def get_consecutive_qualified_points_fast(
        self, results: list[dict[str, Any]], max_distance: int, consecutive_count: int = 3
    ) -> list[dict[str, Any]]:
        if not results or len(results) < consecutive_count:
            return []
        consecutive_points: list[dict[str, Any]] = []
        for point in results:
            if 0 < point["measured_distance"] < max_distance:
                consecutive_points.append(point)
                if len(consecutive_points) >= consecutive_count:
                    return consecutive_points[-consecutive_count:]
            else:
                consecutive_points.clear()
        return []

    def has_consecutive_qualified_points_fast(
        self, results: list[dict[str, Any]], max_distance: int, consecutive_count: int = 3
    ) -> bool:
        return bool(self.get_consecutive_qualified_points_fast(results, max_distance, consecutive_count))

    def optimized_single_measurement(
        self, start_index: int, end_index: int, max_distance: int | None = None
    ) -> dict[str, Any] | None:
        if not self.send_command():
            return None
        radar_data = self.receive_radar_data_complete_fast(required_bytes=self.calculate_required_bytes(end_index))
        if not radar_data:
            return None
        radar_data = self.prepare_calibration_parse_buffer(radar_data)
        all_results = self.parse_data_range_fast(radar_data, start_index, end_index)
        if max_distance is not None:
            filtered_results = [r for r in all_results if 10 < r["measured_distance"] < max_distance]
            consecutive_points = self.get_consecutive_qualified_points_fast(all_results, max_distance, 3)
            has_consecutive = bool(consecutive_points)
            out_results = filtered_results
        else:
            filtered_results = all_results
            has_consecutive = False
            consecutive_points = []
            out_results = all_results
        return {
            "total_count": len(all_results),
            "filtered_count": len(filtered_results),
            "results": out_results,
            "all_results": all_results,
            "has_consecutive_qualified": has_consecutive,
            "consecutive_points": consecutive_points,
            "start_index": start_index,
            "end_index": end_index,
            "angular_resolution_deg": self.angular_resolution_deg,
            "scan_angle_range_deg": self.scan_angle_range_deg,
            "start_angle_deg": self.start_angle_deg,
            "expected_point_count": self.expected_point_count,
        }

    def close(self) -> None:
        if self.socket:
            try:
                self.socket.close()
            finally:
                self.socket = None
