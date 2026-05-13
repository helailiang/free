# -*- coding: utf-8 -*-
from __future__ import annotations

import ipaddress
import queue
import re
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from libs.protocols.c3_common import (
    DATA_TYPE_POLAR,
    OFFSET_DATA_TYPE,
    OFFSET_LENGTH,
    OFFSET_MSG_ID,
    OFFSET_PAYLOAD_TYPE,
    OFFSET_POINTS,
    OFFSET_SCAN_CNT,
    OFFSET_SEQ_NUM,
    OFFSET_TIME_TYPE,
    OFFSET_TIMESTAMP,
    PAYLOAD_TYPE_MSG,
    POINT_CLOUD_MSG_ID,
    POINT_SIZE,
    SOF,
    parse_c3_frame,
    u16_le,
    u64_le,
)


START_COMMAND = bytes.fromhex("5A 0E 00 00 00 7E E5 02 00 01 EA 3D C2 8B")
STOP_COMMAND = bytes.fromhex("5A 0E 00 00 00 7E E5 02 00 00 7C 0D C5 FC")
MIN_FRAME_LENGTH = OFFSET_POINTS + 4
INVALID_FRAME_HEAD_PREVIEW_BYTES = 32
INVALID_FRAME_TAIL_PREVIEW_BYTES = 16
LAUNCH_MODES = {"combined", "continuous", "network_cycle"}


def parse_frame(frame: bytes, frame_index: int) -> dict | None:
    parsed = parse_c3_frame(frame, frame_index, include_points=False)
    if parsed is None:
        return None
    return {
        "frame_index": parsed["frame_index"],
        "seq_num": parsed["seq_num"],
        "scan_cnt": parsed["scan_cnt"],
        "time_type": parsed["time_type"],
        "timestamp": parsed["timestamp"],
        "length": parsed["length"],
        "point_count": parsed["point_count"],
    }


def format_hex_preview(data: bytes, max_bytes: int) -> str:
    if not data:
        return "-"

    preview_bytes = data[:max_bytes]
    preview = " ".join(f"{byte:02X}" for byte in preview_bytes)
    if len(data) > max_bytes:
        preview += " ..."
    return preview


def format_optional_byte_hex(value: int | None) -> str:
    if value is None:
        return "-"
    return f"0x{value:02X}"


def inspect_invalid_frame(frame: bytes, frame_index: int) -> dict:
    sof_value = frame[0] if len(frame) >= 1 else None
    declared_length = u16_le(frame, OFFSET_LENGTH) if len(frame) >= OFFSET_LENGTH + 2 else None
    payload_type = frame[OFFSET_PAYLOAD_TYPE] if len(frame) > OFFSET_PAYLOAD_TYPE else None
    seq_num = frame[OFFSET_SEQ_NUM] if len(frame) > OFFSET_SEQ_NUM else None
    msg_id = frame[OFFSET_MSG_ID] if len(frame) > OFFSET_MSG_ID else None
    scan_cnt = frame[OFFSET_SCAN_CNT] if len(frame) > OFFSET_SCAN_CNT else None
    data_type = frame[OFFSET_DATA_TYPE] if len(frame) > OFFSET_DATA_TYPE else None
    time_type = frame[OFFSET_TIME_TYPE] if len(frame) > OFFSET_TIME_TYPE else None
    timestamp = u64_le(frame, OFFSET_TIMESTAMP) if len(frame) >= OFFSET_TIMESTAMP + 8 else None

    point_bytes: int | None = None
    point_remainder: int | None = None
    if len(frame) >= OFFSET_POINTS + 4:
        point_bytes = len(frame) - OFFSET_POINTS - 4
        point_remainder = point_bytes % POINT_SIZE

    failure_reasons: list[str] = []
    header_mismatch_count = 0
    header_mismatch_types: set[str] = set()
    if len(frame) < MIN_FRAME_LENGTH:
        failure_reasons.append(f"帧总长度过短({len(frame)}<{MIN_FRAME_LENGTH})")

    if sof_value is None:
        failure_reasons.append("SOF缺失")
    elif sof_value != SOF:
        failure_reasons.append(f"SOF错误({format_optional_byte_hex(sof_value)}，期望0x{SOF:02X})")
        header_mismatch_count += 1
        header_mismatch_types.add("sof")

    if declared_length is None:
        failure_reasons.append("长度字段缺失")
    elif declared_length != len(frame):
        failure_reasons.append(f"长度字段不一致(header={declared_length}, actual={len(frame)})")

    if payload_type is None:
        failure_reasons.append("payload_type缺失")
    elif payload_type != PAYLOAD_TYPE_MSG:
        failure_reasons.append(f"payload_type错误({format_optional_byte_hex(payload_type)}，期望0x{PAYLOAD_TYPE_MSG:02X})")
        header_mismatch_count += 1
        header_mismatch_types.add("payload_type")

    if msg_id is None:
        failure_reasons.append("msg_id缺失")
    elif msg_id != POINT_CLOUD_MSG_ID:
        failure_reasons.append(f"msg_id错误({format_optional_byte_hex(msg_id)}，期望0x{POINT_CLOUD_MSG_ID:02X})")
        header_mismatch_count += 1
        header_mismatch_types.add("msg_id")

    if data_type is None:
        failure_reasons.append("data_type缺失")
    elif data_type not in DATA_TYPE_POLAR:
        supported_text = "/".join(f"0x{item:02X}" for item in sorted(DATA_TYPE_POLAR))
        failure_reasons.append(f"data_type错误({format_optional_byte_hex(data_type)}，期望{supported_text})")
        header_mismatch_count += 1
        header_mismatch_types.add("data_type")

    if point_remainder is not None and point_remainder != 0:
        failure_reasons.append(
            f"点云区长度不是{POINT_SIZE}字节整数倍(point_bytes={point_bytes}, remainder={point_remainder})"
        )

    if not failure_reasons:
        failure_reasons.append("未通过C3点云协议校验，疑似帧错位或混入非点云消息")

    if len(frame) < MIN_FRAME_LENGTH or declared_length is None or declared_length != len(frame):
        category = "帧长异常"
    elif sof_value is None or sof_value != SOF:
        category = "帧头错误"
    elif header_mismatch_count >= 2:
        category = "疑似帧错位"
    elif "payload_type" in header_mismatch_types or "msg_id" in header_mismatch_types:
        category = "非点云消息"
    elif "data_type" in header_mismatch_types:
        category = "数据类型异常"
    elif point_remainder is not None and point_remainder != 0:
        category = "载荷长度异常"
    else:
        category = "协议校验失败"

    return {
        "frame_index": frame_index,
        "category": category,
        "failure_reason": "；".join(failure_reasons),
        "sof": format_optional_byte_hex(sof_value),
        "payload_type": format_optional_byte_hex(payload_type),
        "msg_id": format_optional_byte_hex(msg_id),
        "data_type": format_optional_byte_hex(data_type),
        "seq_num": seq_num,
        "scan_cnt": scan_cnt,
        "time_type": time_type,
        "timestamp": timestamp,
        "point_bytes": point_bytes,
        "point_remainder": point_remainder,
        "head_hex": format_hex_preview(frame, INVALID_FRAME_HEAD_PREVIEW_BYTES),
        "tail_hex": format_hex_preview(frame[-INVALID_FRAME_TAIL_PREVIEW_BYTES:], INVALID_FRAME_TAIL_PREVIEW_BYTES),
    }


class C3StressStats:
    MAX_ABNORMAL_EVENTS = 5000

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.valid_packets = 0
        self.completed_scans = 0
        self.invalid_frames = 0
        self.continuity_errors = 0
        self.missing_packets = 0
        self.max_single_loss = 0
        self.time_anomalies = 0
        self.last_seq_num: int | None = None
        self.last_scan_cnt: int | None = None
        self.current_scan_cnt: int | None = None
        self.current_scan_timestamp: int | None = None
        self.current_scan_time_type: int | None = None
        self.current_scan_seq_nums: list[int] = []
        self.current_scan_packet_count = 0
        self.last_completed_scan_timestamp: int | None = None
        self.last_completed_scan_packet_count = 0
        self.latest_interval_ms = 0.0
        self.scan_intervals_ms: deque[float] = deque(maxlen=1000)
        self.scan_packet_counts: deque[int] = deque(maxlen=1000)
        self.last_loss_event: dict | None = None
        self.timestamp_divisor: float | None = None
        self.timestamp_scale_label = "unknown"
        self.abnormal_events: deque[dict] = deque(maxlen=self.MAX_ABNORMAL_EVENTS)
        self.abnormal_events_dropped = 0
        self.network_cycles_started = 0
        self.network_cycles_completed = 0
        self.network_cycles_failed = 0
        self.last_network_cycle = 0
        self.last_network_status = "未执行通断网测试"

    def _record_abnormal_event(self, event_type: str, description: str, **details) -> None:
        if len(self.abnormal_events) >= self.abnormal_events.maxlen:
            self.abnormal_events_dropped += 1
        self.abnormal_events.append(
            {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "type": event_type,
                "description": description,
                "details": details,
            }
        )

    def add_invalid_frame(
        self,
        reason: str = "帧格式异常",
        *,
        frame_length: int | None = None,
        buffer_length: int | None = None,
        **extra_details,
    ) -> str:
        self.invalid_frames += 1
        detail_parts = [reason]
        category = extra_details.get("category")
        if category:
            detail_parts.append(f"一级分类={category}")
        failure_reason = extra_details.get("failure_reason")
        if failure_reason:
            detail_parts.append(f"failure_reason={failure_reason}")
        if frame_length is not None:
            detail_parts.append(f"frame_length={frame_length}")
        if buffer_length is not None:
            detail_parts.append(f"buffer_length={buffer_length}")
        description = "，".join(detail_parts)
        detail_payload = {
            "reason": reason,
            "frame_length": frame_length,
            "buffer_length": buffer_length,
            **extra_details,
        }
        self._record_abnormal_event(
            "异常帧",
            description,
            **detail_payload,
        )
        if category:
            return f"异常帧[{category}]: {description}"
        return f"异常帧: {description}"

    def _format_missing_packets(self, missing_packets: list[int], limit: int = 12) -> str:
        if not missing_packets:
            return "[]"
        preview = ",".join(str(item) for item in missing_packets[:limit])
        if len(missing_packets) > limit:
            preview += ",..."
        return f"[{preview}]"

    def _convert_raw_timestamp_to_ms(self, raw_delta: int, time_type: int | None) -> float:
        if time_type == 0:
            self.timestamp_divisor = float(2 ** 32) / 1000.0
            self.timestamp_scale_label = "q32->ms"
            return raw_delta / self.timestamp_divisor

        if self.timestamp_divisor is None:
            if raw_delta >= 1_000_000:
                self.timestamp_divisor = 1_000_000.0
                self.timestamp_scale_label = "ns->ms"
            elif raw_delta >= 1_000:
                self.timestamp_divisor = 1_000.0
                self.timestamp_scale_label = "us->ms"
            else:
                self.timestamp_divisor = 1.0
                self.timestamp_scale_label = "ms"
        return raw_delta / self.timestamp_divisor

    def _start_scan(self, parsed: dict) -> None:
        self.current_scan_cnt = parsed["scan_cnt"]
        self.current_scan_timestamp = parsed["timestamp"]
        self.current_scan_time_type = parsed["time_type"]
        self.current_scan_seq_nums = [parsed["seq_num"]]
        self.current_scan_packet_count = 1
        self.last_scan_cnt = parsed["scan_cnt"]
        self.last_seq_num = parsed["seq_num"]

    def _analyze_scan_sequences(self) -> tuple[int, int, list[int], int, int, int]:
        if not self.current_scan_seq_nums:
            return 0, 0, [], 0, 0, 0

        seq_list = self.current_scan_seq_nums
        missing_total = 0
        duplicate_count = 0
        largest_gap = 0
        missing_preview: list[int] = []
        previous_seq = seq_list[0]

        for current_seq in seq_list[1:]:
            if current_seq == previous_seq:
                duplicate_count += 1
                continue

            expected_seq = (previous_seq + 1) % 256
            if current_seq != expected_seq:
                missing_count = (current_seq - expected_seq) % 256
                if missing_count > 0:
                    missing_total += missing_count
                    largest_gap = max(largest_gap, missing_count)
                    remaining_slots = max(0, 50 - len(missing_preview))
                    for index in range(min(missing_count, remaining_slots)):
                        missing_preview.append((expected_seq + index) % 256)

            previous_seq = current_seq

        unique_count = len(set(seq_list))
        expected_count = unique_count + missing_total
        return missing_total, duplicate_count, missing_preview, largest_gap, unique_count, expected_count

    def _finalize_current_scan(self, *, partial: bool = False) -> list[str]:
        if self.current_scan_cnt is None or not self.current_scan_seq_nums:
            return []

        messages: list[str] = []
        first_seq = self.current_scan_seq_nums[0]
        last_seq = self.current_scan_seq_nums[-1]
        packet_count_this_scan = self.current_scan_packet_count
        missing_total, duplicate_count, missing_preview, largest_gap, unique_count, expected_count = self._analyze_scan_sequences()

        self.completed_scans += 1
        self.last_completed_scan_packet_count = packet_count_this_scan
        self.scan_packet_counts.append(packet_count_this_scan)

        if missing_total > 0:
            self.continuity_errors += 1
            self.missing_packets += missing_total
            self.max_single_loss = max(self.max_single_loss, largest_gap)
            self.last_loss_event = {
                "scan_cnt": self.current_scan_cnt,
                "first_seq": first_seq,
                "last_seq": last_seq,
                "missing": missing_total,
                "expected_count": expected_count,
                "actual_count": unique_count,
                "packet_count": packet_count_this_scan,
                "duplicate_count": duplicate_count,
                "missing_packets": missing_preview,
                "partial": partial,
            }
            partial_suffix = "（停止时按已接收顺序统计）" if partial else ""
            duplicate_suffix = f"，重复包={duplicate_count}" if duplicate_count else ""
            message_text = (
                f"圈连续性异常: scan_cnt={self.current_scan_cnt}, 起始序号={first_seq}, 结束序号={last_seq}, "
                f"实际包数={unique_count}, 期望包数={expected_count}, 缺失包={self._format_missing_packets(missing_preview)}"
                f"{duplicate_suffix}{partial_suffix}"
            )
            messages.append(message_text)
            self._record_abnormal_event(
                "丢包",
                message_text,
                scan_cnt=self.current_scan_cnt,
                first_seq=first_seq,
                last_seq=last_seq,
                missing=missing_total,
                expected_count=expected_count,
                actual_count=unique_count,
                packet_count=packet_count_this_scan,
                duplicate_count=duplicate_count,
                partial=partial,
            )

        if self.current_scan_timestamp is not None:
            if self.last_completed_scan_timestamp is not None:
                raw_delta = self.current_scan_timestamp - self.last_completed_scan_timestamp
                if raw_delta < 0:
                    self.time_anomalies += 1
                    message_text = (
                        f"时间戳异常: 上一圈={self.last_completed_scan_timestamp}, 当前圈={self.current_scan_timestamp}, 差值={raw_delta}"
                    )
                    messages.append(message_text)
                    self._record_abnormal_event(
                        "时间异常",
                        message_text,
                        previous_timestamp=self.last_completed_scan_timestamp,
                        current_timestamp=self.current_scan_timestamp,
                        raw_delta=raw_delta,
                        scan_cnt=self.current_scan_cnt,
                    )
                elif raw_delta > 0:
                    interval_ms = self._convert_raw_timestamp_to_ms(raw_delta, self.current_scan_time_type)
                    average_before = (
                        sum(self.scan_intervals_ms) / len(self.scan_intervals_ms)
                        if self.scan_intervals_ms else 0.0
                    )
                    self.scan_intervals_ms.append(interval_ms)
                    self.latest_interval_ms = interval_ms

                    if average_before > 0:
                        threshold_ms = max(average_before * 3.0, average_before + 100.0)
                        if interval_ms > threshold_ms:
                            self.time_anomalies += 1
                            message_text = (
                                f"圈时间间隔异常: scan_cnt={self.current_scan_cnt}, 当前间隔={interval_ms:.2f}ms, 阈值={threshold_ms:.2f}ms"
                            )
                            messages.append(message_text)
                            self._record_abnormal_event(
                                "时间异常",
                                message_text,
                                scan_cnt=self.current_scan_cnt,
                                interval_ms=interval_ms,
                                threshold_ms=threshold_ms,
                                raw_delta=raw_delta,
                            )

            self.last_completed_scan_timestamp = self.current_scan_timestamp

        self.current_scan_cnt = None
        self.current_scan_timestamp = None
        self.current_scan_time_type = None
        self.current_scan_seq_nums = []
        self.current_scan_packet_count = 0
        return messages

    def finalize_pending_scan(self) -> list[str]:
        return self._finalize_current_scan(partial=True)

    def clear_pending_scan(self) -> None:
        self.current_scan_cnt = None
        self.current_scan_timestamp = None
        self.current_scan_time_type = None
        self.current_scan_seq_nums = []
        self.current_scan_packet_count = 0

    def record_network_cycle_start(self, cycle_index: int, total_cycles: int) -> str:
        self.network_cycles_started += 1
        self.last_network_cycle = cycle_index
        self.last_network_status = f"第 {cycle_index}/{total_cycles} 轮开始"
        return self.last_network_status

    def record_network_cycle_success(
        self,
        cycle_index: int,
        total_cycles: int,
        verify_scans: int,
        elapsed_seconds: float,
    ) -> str:
        self.network_cycles_completed += 1
        self.last_network_cycle = cycle_index
        self.last_network_status = (
            f"第 {cycle_index}/{total_cycles} 轮通过，验收 {verify_scans} 圈，耗时 {elapsed_seconds:.2f} s"
        )
        return self.last_network_status

    def record_network_cycle_failure(self, cycle_index: int, total_cycles: int, reason: str) -> str:
        self.network_cycles_failed += 1
        self.last_network_cycle = cycle_index
        self.last_network_status = f"第 {cycle_index}/{total_cycles} 轮失败: {reason}"
        return self.last_network_status

    def ingest_frame(self, parsed: dict) -> list[str]:
        self.valid_packets += 1

        if self.current_scan_cnt is None:
            self._start_scan(parsed)
            return []

        current_scan_cnt = parsed["scan_cnt"]
        current_seq_num = parsed["seq_num"]

        messages: list[str] = []
        if current_scan_cnt != self.current_scan_cnt:
            messages.extend(self._finalize_current_scan())
            self._start_scan(parsed)
            return messages

        self.current_scan_seq_nums.append(current_seq_num)
        self.current_scan_packet_count += 1
        self.last_scan_cnt = current_scan_cnt
        self.last_seq_num = current_seq_num
        return messages

    def snapshot(self) -> dict:
        denominator = self.valid_packets + self.missing_packets
        loss_rate = (self.missing_packets / denominator * 100.0) if denominator else 0.0
        average_interval_ms = (
            sum(self.scan_intervals_ms) / len(self.scan_intervals_ms)
            if self.scan_intervals_ms else 0.0
        )
        max_interval_ms = max(self.scan_intervals_ms) if self.scan_intervals_ms else 0.0
        average_packets_per_scan = (
            sum(self.scan_packet_counts) / len(self.scan_packet_counts)
            if self.scan_packet_counts else 0.0
        )
        next_expected_seq = (self.last_seq_num + 1) % 256 if self.last_seq_num is not None else None
        return {
            "valid_packets": self.valid_packets,
            "completed_scans": self.completed_scans,
            "invalid_frames": self.invalid_frames,
            "continuity_errors": self.continuity_errors,
            "missing_packets": self.missing_packets,
            "loss_rate": loss_rate,
            "time_anomalies": self.time_anomalies,
            "latest_interval_ms": self.latest_interval_ms,
            "average_interval_ms": average_interval_ms,
            "max_interval_ms": max_interval_ms,
            "last_seq_num": self.last_seq_num,
            "next_expected_seq": next_expected_seq,
            "last_scan_cnt": self.last_scan_cnt,
            "max_single_loss": self.max_single_loss,
            "last_loss_event": self.last_loss_event,
            "timestamp_scale_label": self.timestamp_scale_label,
            "last_completed_scan_packet_count": self.last_completed_scan_packet_count,
            "average_packets_per_scan": average_packets_per_scan,
            "abnormal_events": list(self.abnormal_events),
            "abnormal_events_dropped": self.abnormal_events_dropped,
            "network_cycles_started": self.network_cycles_started,
            "network_cycles_completed": self.network_cycles_completed,
            "network_cycles_failed": self.network_cycles_failed,
            "last_network_cycle": self.last_network_cycle,
            "last_network_status": self.last_network_status,
        }


class C3RadarConnection:
    CMD_PORT = 50000
    DATA_PORT = 52000

    def __init__(self, radar_ip: str) -> None:
        self.radar_ip = radar_ip
        self.cmd_socket: socket.socket | None = None
        self.data_socket: socket.socket | None = None
        self.connected = False

    def connect(self) -> None:
        try:
            self.cmd_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.cmd_socket.settimeout(5)
            self.cmd_socket.connect((self.radar_ip, self.CMD_PORT))
        except Exception as exc:
            self.disconnect()
            raise ConnectionError(f"命令端口 {self.CMD_PORT} 连接失败: {exc}") from exc

        try:
            self.data_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.data_socket.settimeout(1)
            self.data_socket.connect((self.radar_ip, self.DATA_PORT))
        except Exception as exc:
            self.disconnect()
            raise ConnectionError(f"数据端口 {self.DATA_PORT} 连接失败: {exc}") from exc

        self.connected = True

    def disconnect(self) -> None:
        for sock in (self.cmd_socket, self.data_socket):
            if sock is None:
                continue
            try:
                sock.close()
            except OSError:
                pass

        self.cmd_socket = None
        self.data_socket = None
        self.connected = False

    def drain_data_socket(self, limit_bytes: int = 1_048_576) -> int:
        if self.data_socket is None:
            return 0

        drained_bytes = 0
        previous_timeout = self.data_socket.gettimeout()
        try:
            self.data_socket.settimeout(0.05)
            while drained_bytes < limit_bytes:
                try:
                    chunk = self.data_socket.recv(min(4096, limit_bytes - drained_bytes))
                except socket.timeout:
                    break
                except BlockingIOError:
                    break

                if not chunk:
                    break

                drained_bytes += len(chunk)
        finally:
            try:
                self.data_socket.settimeout(previous_timeout)
            except OSError:
                pass

        return drained_bytes

    def send_command(self, command: bytes, action_name: str) -> bytes | None:
        if self.cmd_socket is None:
            raise ConnectionError("命令端口未连接")

        try:
            self.cmd_socket.sendall(command)
            try:
                return self.cmd_socket.recv(1024)
            except socket.timeout:
                return None
        except Exception as exc:
            raise RuntimeError(f"{action_name}失败: {exc}") from exc

    def start_stream(self) -> None:
        response = self.send_command(START_COMMAND, "启动连续取数")
        if response is None:
            raise TimeoutError("启动连续取数后未收到雷达响应")

    def stop_stream(self) -> None:
        self.send_command(STOP_COMMAND, "停止连续取数")


@dataclass
class RadarSession:
    client: C3RadarConnection
    stats: C3StressStats = field(default_factory=C3StressStats)
    buffer: bytearray = field(default_factory=bytearray)
    receiver_thread: threading.Thread | None = None
    receiver_active: bool = False
    finalized_for_test: bool = False

    @property
    def radar_ip(self) -> str:
        return self.client.radar_ip


class C3RadarStressTestApp:
    def __init__(self, root: tk.Tk, launch_mode: str = "combined") -> None:
        self.root = root
        self.launch_mode = launch_mode if launch_mode in LAUNCH_MODES else "combined"
        if self.launch_mode == "continuous":
            self.root.title("C3连续取数压力测试 (50000/52000)")
        elif self.launch_mode == "network_cycle":
            self.root.title("C3通断网测试 (50000/52000)")
        else:
            self.root.title("C3雷达压力测试 (50000/52000)")
        self.root.geometry("1280x900")
        self.root.minsize(1120, 760)

        self.sessions: dict[str, RadarSession] = {}
        self.is_running = False
        self.stop_requested = False
        self.test_start_time: float | None = None
        self.last_elapsed_seconds = 0.0
        self.ui_refresh_job: str | None = None
        self.active_receiver_count = 0
        self.receiver_exit_reasons: dict[str, str] = {}
        self.finish_called = False
        self.test_mode = "idle"
        self.last_test_mode = "idle"
        self.ui_task_queue: queue.Queue = queue.Queue()
        self.ui_task_poller_job: str | None = None

        self.stat_vars: dict[str, tk.StringVar] = {}
        self.radar_configs: list[dict] = []
        self.next_radar_config_id = 1
        self.radar_card_vars: dict[int, dict[str, object]] = {}
        self.connection_errors: dict[str, str] = {}
        self.create_widgets()
        self.schedule_ui_task_poll()
        self.refresh_stats()
        self.log_message("工具已启动，支持多个 C3 雷达同时连接")
        self.log_message("连接配置区支持 + 增加雷达，每行配置一台设备")
        self.log_message("协议: 50000 发指令 / 52000 收数据")
        if self.launch_mode == "continuous":
            self.log_message("当前入口: 连续取数专用")
        elif self.launch_mode == "network_cycle":
            self.log_message("当前入口: 通断网测试专用")
        else:
            self.log_message("当前入口: 连续取数 + 通断网测试")
        self.log_message("支持连续取数压力测试与通断网测试")
        self.log_message("连续性按 scan_cnt 分圈统计，并支持 seq_num 在圈内跨 255 回绕")

    def supports_continuous_mode(self) -> bool:
        return self.launch_mode in {"combined", "continuous"}

    def supports_network_mode(self) -> bool:
        return self.launch_mode in {"combined", "network_cycle"}

    def create_widgets(self) -> None:
        main_frame = ttk.Frame(self.root, padding=10)
        main_frame.pack(fill="both", expand=True)
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(3, weight=1)
        main_frame.rowconfigure(4, weight=1)

        connection_frame = ttk.LabelFrame(main_frame, text="连接配置", padding=10)
        connection_frame.grid(row=0, column=0, sticky="ew")
        connection_frame.columnconfigure(0, weight=1)

        config_header = ttk.Frame(connection_frame)
        config_header.grid(row=0, column=0, sticky="ew")
        config_header.columnconfigure(0, weight=1)
        ttk.Label(
            config_header,
            text="每行配置一台雷达，可单独查看连接状态和实时统计",
            foreground="gray",
        ).grid(row=0, column=0, sticky="w")

        protocol_frame = ttk.Frame(config_header)
        protocol_frame.grid(row=0, column=1, sticky="e")
        ttk.Label(protocol_frame, text="命令端口:").pack(side="left")
        ttk.Label(protocol_frame, text="50000", foreground="blue").pack(side="left", padx=(4, 12))
        ttk.Label(protocol_frame, text="数据端口:").pack(side="left")
        ttk.Label(protocol_frame, text="52000", foreground="blue").pack(side="left", padx=(4, 12))
        ttk.Label(protocol_frame, text="协议:").pack(side="left")
        ttk.Label(protocol_frame, text="C3 50000/52000", foreground="blue").pack(side="left", padx=(4, 0))

        self.radar_config_list_frame = ttk.Frame(connection_frame)
        self.radar_config_list_frame.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.radar_config_list_frame.columnconfigure(0, weight=1)

        button_frame = ttk.Frame(connection_frame)
        button_frame.grid(row=2, column=0, sticky="ew", pady=(10, 0))

        self.add_radar_btn = ttk.Button(button_frame, text="+ 增加雷达", command=self.add_radar_config_row)
        self.add_radar_btn.pack(side="left", padx=(0, 12))

        self.connect_btn = ttk.Button(button_frame, text="连接雷达", command=self.connect_radar)
        self.connect_btn.pack(side="left", padx=(0, 8))

        start_button_text = "开始测试" if self.launch_mode != "combined" else "开始测试"
        self.start_btn = ttk.Button(button_frame, text=start_button_text, command=self.start_test, state="disabled")
        if self.supports_continuous_mode():
            self.start_btn.pack(side="left", padx=(0, 8))

        self.network_test_btn = ttk.Button(
            button_frame,
            text="通断网测试" if self.launch_mode == "combined" else "开始测试",
            command=self.start_network_test,
            state="disabled",
        )
        if self.supports_network_mode():
            self.network_test_btn.pack(side="left", padx=(0, 8))

        self.stop_btn = ttk.Button(button_frame, text="停止测试", command=self.stop_test, state="disabled")
        self.stop_btn.pack(side="left", padx=(0, 8))

        self.disconnect_btn = ttk.Button(button_frame, text="断开连接", command=self.disconnect_radar, state="disabled")
        self.disconnect_btn.pack(side="left", padx=(0, 8))

        export_btn = ttk.Button(button_frame, text="导出汇总", command=self.export_summary)
        export_btn.pack(side="left", padx=(0, 8))

        clear_log_btn = ttk.Button(button_frame, text="清空日志", command=self.clear_log)
        clear_log_btn.pack(side="left")

        network_param_frame = ttk.LabelFrame(connection_frame, text="通断网测试参数", padding=10)
        if self.supports_network_mode():
            network_param_frame.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        for column in range(8):
            network_param_frame.columnconfigure(column, weight=1 if column % 2 == 1 else 0)

        ttk.Label(network_param_frame, text="通断网轮次:").grid(row=0, column=0, sticky="w")
        self.network_cycle_count_var = tk.StringVar(value="10")
        self.network_cycle_count_entry = ttk.Entry(
            network_param_frame,
            textvariable=self.network_cycle_count_var,
            width=10,
        )
        self.network_cycle_count_entry.grid(row=0, column=1, sticky="ew", padx=(4, 12))

        ttk.Label(network_param_frame, text="每轮验收圈数:").grid(row=0, column=2, sticky="w")
        self.network_verify_scans_var = tk.StringVar(value="3")
        self.network_verify_scans_entry = ttk.Entry(
            network_param_frame,
            textvariable=self.network_verify_scans_var,
            width=10,
        )
        self.network_verify_scans_entry.grid(row=0, column=3, sticky="ew", padx=(4, 12))

        ttk.Label(network_param_frame, text="重连等待(s):").grid(row=0, column=4, sticky="w")
        self.network_reconnect_delay_var = tk.StringVar(value="2")
        self.network_reconnect_delay_entry = ttk.Entry(
            network_param_frame,
            textvariable=self.network_reconnect_delay_var,
            width=10,
        )
        self.network_reconnect_delay_entry.grid(row=0, column=5, sticky="ew", padx=(4, 12))

        ttk.Label(network_param_frame, text="单轮超时(s):").grid(row=0, column=6, sticky="w")
        self.network_cycle_timeout_var = tk.StringVar(value="15")
        self.network_cycle_timeout_entry = ttk.Entry(
            network_param_frame,
            textvariable=self.network_cycle_timeout_var,
            width=10,
        )
        self.network_cycle_timeout_entry.grid(row=0, column=7, sticky="ew", padx=(4, 0))

        self.network_param_entries = [
            self.network_cycle_count_entry,
            self.network_verify_scans_entry,
            self.network_reconnect_delay_entry,
            self.network_cycle_timeout_entry,
        ]

        status_frame = ttk.LabelFrame(main_frame, text="汇总状态", padding=10)
        status_frame.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        status_frame.columnconfigure(0, weight=1)
        self.status_label = ttk.Label(status_frame, text="未连接", foreground="red")
        self.status_label.grid(row=0, column=0, sticky="w")

        stats_frame = ttk.LabelFrame(main_frame, text="连接概览", padding=10)
        stats_frame.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        for column in range(4):
            stats_frame.columnconfigure(column, weight=1)

        stats_config = [
            ("已配置雷达", "configured_radars"),
            ("已连接雷达", "connected_radars"),
            ("运行中雷达", "active_radars"),
            ("运行时长", "elapsed_seconds"),
        ]

        for index, (label_text, key) in enumerate(stats_config):
            row = index // 2
            column = (index % 2) * 2
            ttk.Label(stats_frame, text=f"{label_text}:").grid(row=row, column=column, padx=6, pady=4, sticky="w")
            var = tk.StringVar(value="0")
            self.stat_vars[key] = var
            ttk.Label(stats_frame, textvariable=var, foreground="blue").grid(
                row=row, column=column + 1, padx=6, pady=4, sticky="w"
            )

        radar_frame = ttk.LabelFrame(main_frame, text="分雷达显示", padding=10)
        radar_frame.grid(row=3, column=0, sticky="nsew", pady=(10, 0))
        radar_frame.rowconfigure(0, weight=1)
        radar_frame.columnconfigure(0, weight=1)

        self.radar_cards_canvas = tk.Canvas(radar_frame, highlightthickness=0)
        self.radar_cards_canvas.grid(row=0, column=0, sticky="nsew")

        radar_scrollbar = ttk.Scrollbar(radar_frame, orient="vertical", command=self.radar_cards_canvas.yview)
        radar_scrollbar.grid(row=0, column=1, sticky="ns")
        self.radar_cards_canvas.configure(yscrollcommand=radar_scrollbar.set)

        self.radar_cards_container = ttk.Frame(self.radar_cards_canvas)
        self.radar_cards_window = self.radar_cards_canvas.create_window(
            (0, 0),
            window=self.radar_cards_container,
            anchor="nw",
        )
        self.radar_cards_container.columnconfigure(0, weight=1)
        self.radar_cards_container.columnconfigure(1, weight=1)
        self.radar_cards_container.bind("<Configure>", self.on_radar_cards_container_configure)
        self.radar_cards_canvas.bind("<Configure>", self.on_radar_canvas_configure)

        log_frame = ttk.LabelFrame(main_frame, text="运行日志", padding=10)
        log_frame.grid(row=4, column=0, sticky="nsew", pady=(10, 0))
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)

        self.log_text = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, font=("Consolas", 10))
        self.log_text.grid(row=0, column=0, sticky="nsew")

        self.add_radar_config_row("192.168.192.100")
        self.add_radar_config_row("192.168.192.101")
        self.set_radar_config_editable(True)
        self.refresh_radar_cards()

    def on_radar_cards_container_configure(self, _event=None) -> None:
        self.radar_cards_canvas.configure(scrollregion=self.radar_cards_canvas.bbox("all"))

    def on_radar_canvas_configure(self, event) -> None:
        self.radar_cards_canvas.itemconfigure(self.radar_cards_window, width=event.width)

    def add_radar_config_row(self, default_ip: str = "") -> None:
        config_id = self.next_radar_config_id
        self.next_radar_config_id += 1

        row_frame = ttk.Frame(self.radar_config_list_frame)
        row_frame.columnconfigure(1, weight=1)

        title_label = ttk.Label(row_frame, width=10)
        title_label.grid(row=0, column=0, padx=(0, 8), sticky="w")

        ip_var = tk.StringVar(value=default_ip)
        ip_var.trace_add("write", lambda *_args, cid=config_id: self.on_radar_config_changed(cid))
        entry = ttk.Entry(row_frame, textvariable=ip_var)
        entry.grid(row=0, column=1, sticky="ew")

        remove_btn = ttk.Button(
            row_frame,
            text="删除",
            width=8,
            command=lambda cid=config_id: self.remove_radar_config_row(cid),
        )
        remove_btn.grid(row=0, column=2, padx=(8, 0))

        self.radar_configs.append(
            {
                "id": config_id,
                "frame": row_frame,
                "title_label": title_label,
                "ip_var": ip_var,
                "entry": entry,
                "remove_btn": remove_btn,
            }
        )
        self.create_radar_card(config_id)
        self.rebuild_radar_config_rows()
        self.refresh_radar_cards()

    def remove_radar_config_row(self, config_id: int) -> None:
        if len(self.radar_configs) <= 1:
            return

        target_config = next((item for item in self.radar_configs if item["id"] == config_id), None)
        if target_config is None:
            return

        target_config["frame"].destroy()
        self.radar_configs = [item for item in self.radar_configs if item["id"] != config_id]

        card_info = self.radar_card_vars.pop(config_id, None)
        if card_info is not None:
            card_info["frame"].destroy()

        self.rebuild_radar_config_rows()
        self.refresh_radar_cards()

    def rebuild_radar_config_rows(self) -> None:
        for index, config in enumerate(self.radar_configs, start=1):
            config["title_label"].config(text=f"雷达{index} IP:")
            config["frame"].grid(row=index - 1, column=0, sticky="ew", pady=4)

            card_info = self.radar_card_vars.get(config["id"])
            if card_info is not None:
                card_info["frame"].grid(
                    row=(index - 1) // 2,
                    column=(index - 1) % 2,
                    sticky="nsew",
                    padx=6,
                    pady=6,
                )

        self.set_radar_config_editable(
            not self.connected_sessions() and not self.is_running and not self.stop_requested
        )
        self.on_radar_cards_container_configure()

    def create_radar_card(self, config_id: int) -> None:
        card_frame = ttk.LabelFrame(self.radar_cards_container, text="雷达", padding=10)
        card_frame.columnconfigure(1, weight=1)

        ttk.Label(card_frame, text="连接状态:").grid(row=0, column=0, sticky="w")
        status_var = tk.StringVar(value="未配置")
        ttk.Label(card_frame, textvariable=status_var, foreground="blue").grid(row=0, column=1, sticky="w")

        ttk.Label(card_frame, text="运行状态:").grid(row=1, column=0, sticky="w", pady=(4, 0))
        runtime_var = tk.StringVar(value="空闲")
        ttk.Label(card_frame, textvariable=runtime_var, foreground="blue").grid(row=1, column=1, sticky="w", pady=(4, 0))

        ttk.Label(card_frame, text="最近状态:").grid(row=2, column=0, sticky="nw", pady=(8, 0))
        reason_var = tk.StringVar(value="请填写雷达 IP")
        ttk.Label(
            card_frame,
            textvariable=reason_var,
            justify="left",
            wraplength=460,
        ).grid(row=2, column=1, sticky="w", pady=(8, 0))

        ttk.Label(card_frame, text="实时统计:").grid(row=3, column=0, sticky="nw", pady=(8, 0))
        stats_var = tk.StringVar(value="")
        ttk.Label(
            card_frame,
            textvariable=stats_var,
            justify="left",
            wraplength=460,
        ).grid(row=3, column=1, sticky="w", pady=(8, 0))

        self.radar_card_vars[config_id] = {
            "frame": card_frame,
            "status_var": status_var,
            "runtime_var": runtime_var,
            "reason_var": reason_var,
            "stats_var": stats_var,
        }

    def on_radar_config_changed(self, _config_id: int) -> None:
        self.refresh_radar_cards()

    def set_radar_config_editable(self, editable: bool) -> None:
        add_button_state = "normal" if editable else "disabled"
        self.add_radar_btn.config(state=add_button_state)

        for config in self.radar_configs:
            entry_state = "normal" if editable else "disabled"
            remove_state = "normal" if editable and len(self.radar_configs) > 1 else "disabled"
            config["entry"].config(state=entry_state)
            config["remove_btn"].config(state=remove_state)

    def set_network_param_editable(self, editable: bool) -> None:
        entry_state = "normal" if editable else "disabled"
        for entry in self.network_param_entries:
            entry.config(state=entry_state)

    def list_configured_radar_ips(self) -> list[str]:
        radar_ips: list[str] = []
        for config in self.radar_configs:
            radar_ip = str(config["ip_var"].get()).strip()
            if radar_ip:
                radar_ips.append(radar_ip)
        return radar_ips

    def get_configured_radar_ips(self) -> list[str]:
        configured_ips = self.list_configured_radar_ips()
        if not configured_ips:
            raise ValueError("请至少填写一台雷达 IP")

        unique_ips: list[str] = []
        seen: set[str] = set()
        for radar_ip in configured_ips:
            self.validate_ip(radar_ip)
            if radar_ip in seen:
                raise ValueError(f"存在重复 IP，请检查: {radar_ip}")
            seen.add(radar_ip)
            unique_ips.append(radar_ip)
        return unique_ips

    def format_radar_stats_text(self, snapshot: dict) -> str:
        last_seq_num = "-" if snapshot["last_seq_num"] is None else str(snapshot["last_seq_num"])
        next_expected_seq = "-" if snapshot["next_expected_seq"] is None else str(snapshot["next_expected_seq"])
        last_scan_cnt = "-" if snapshot["last_scan_cnt"] is None else str(snapshot["last_scan_cnt"])
        text = (
            f"有效包={snapshot['valid_packets']}  完成圈={snapshot['completed_scans']}  "
            f"最近一圈包数={snapshot['last_completed_scan_packet_count']}  平均每圈包数={snapshot['average_packets_per_scan']:.2f}\n"
            f"异常帧={snapshot['invalid_frames']}  连续性异常={snapshot['continuity_errors']}  "
            f"丢包={snapshot['missing_packets']}  丢包率={snapshot['loss_rate']:.2f}%  时间异常={snapshot['time_anomalies']}\n"
            f"最近圈间隔={snapshot['latest_interval_ms']:.2f} ms  平均圈间隔={snapshot['average_interval_ms']:.2f} ms  "
            f"最大圈间隔={snapshot['max_interval_ms']:.2f} ms  最大单次丢包={snapshot['max_single_loss']}\n"
            f"最近序号={last_seq_num}  期望下一包={next_expected_seq}  最近 scan_cnt={last_scan_cnt}  "
            f"时间戳换算={snapshot['timestamp_scale_label']}"
        )
        if (
            snapshot["network_cycles_started"] > 0
            or snapshot["network_cycles_completed"] > 0
            or snapshot["network_cycles_failed"] > 0
        ):
            last_network_cycle = "-" if snapshot["last_network_cycle"] <= 0 else str(snapshot["last_network_cycle"])
            text += (
                "\n"
                f"通断网启动/完成/失败={snapshot['network_cycles_started']}/"
                f"{snapshot['network_cycles_completed']}/{snapshot['network_cycles_failed']}  "
                f"最近轮次={last_network_cycle}  最近状态={snapshot['last_network_status']}"
            )
        return text

    def format_abnormal_event_lines(self, snapshot: dict) -> list[str]:
        abnormal_events = snapshot.get("abnormal_events", [])
        dropped_count = snapshot.get("abnormal_events_dropped", 0)
        if not abnormal_events:
            return ["无"]

        lines: list[str] = []
        for index, event in enumerate(abnormal_events, start=1):
            details = event.get("details", {})
            detail_parts: list[str] = []
            for key, value in details.items():
                if value is None:
                    continue
                if isinstance(value, float):
                    detail_text = f"{value:.2f}"
                else:
                    detail_text = str(value)
                detail_parts.append(f"{key}={detail_text}")

            detail_suffix = f" | {'; '.join(detail_parts)}" if detail_parts else ""
            lines.append(
                f"{index}. [{event.get('time', '-')}] {event.get('type', '异常')} | "
                f"{event.get('description', '')}{detail_suffix}"
            )

        if dropped_count > 0:
            lines.append(
                f"注意: 当前仅保留最近 {len(abnormal_events)} 条异常记录，较早 {dropped_count} 条未保留。"
            )
        return lines

    def resolve_radar_display(self, radar_ip: str) -> tuple[str, str, str, dict]:
        if not radar_ip:
            return "未配置", "空闲", "请填写雷达 IP", self.base_snapshot()

        session = self.sessions.get(radar_ip)
        if session is not None:
            snapshot = session.stats.snapshot()
            connect_status = "已连接" if session.client.connected else "已断开"
            network_status = snapshot["last_network_status"]
            if session.receiver_active:
                if self.test_mode == "network_cycle":
                    runtime_status = "通断网测试中"
                    reason_text = network_status
                else:
                    runtime_status = "测试中"
                    reason_text = "正在接收 52000 端口点云数据"
            elif self.stop_requested:
                runtime_status = "停止中"
                default_reason = network_status if self.test_mode == "network_cycle" else "等待接收线程退出"
                reason_text = self.receiver_exit_reasons.get(radar_ip, default_reason)
            elif session.finalized_for_test:
                runtime_status = "已停止"
                default_reason = network_status if snapshot["network_cycles_started"] > 0 else "本轮测试已结束"
                reason_text = self.receiver_exit_reasons.get(radar_ip, default_reason)
            else:
                runtime_status = "待机"
                default_reason = network_status if snapshot["network_cycles_started"] > 0 else "已连接，等待开始测试"
                reason_text = self.receiver_exit_reasons.get(radar_ip, default_reason)
            return connect_status, runtime_status, reason_text, snapshot

        if radar_ip in self.connection_errors:
            return "连接失败", "未运行", self.connection_errors[radar_ip], self.base_snapshot()

        if radar_ip in self.receiver_exit_reasons:
            return "已断开", "已停止", self.receiver_exit_reasons[radar_ip], self.base_snapshot()

        return "未连接", "空闲", "等待连接", self.base_snapshot()

    def refresh_radar_cards(self) -> None:
        for index, config in enumerate(self.radar_configs, start=1):
            radar_ip = str(config["ip_var"].get()).strip()
            card_info = self.radar_card_vars.get(config["id"])
            if card_info is None:
                continue

            card_title = f"雷达{index}"
            if radar_ip:
                card_title += f"  {radar_ip}"
            else:
                card_title += "  未填写 IP"
            card_info["frame"].config(text=card_title)

            status_text, runtime_text, reason_text, snapshot = self.resolve_radar_display(radar_ip)
            card_info["status_var"].set(status_text)
            card_info["runtime_var"].set(runtime_text)
            card_info["reason_var"].set(reason_text)
            card_info["stats_var"].set(self.format_radar_stats_text(snapshot))

    def set_status(self, message: str, color: str = "black") -> None:
        self.status_label.config(text=message, foreground=color)

    def log_message(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n")
        self.log_text.see(tk.END)

    def log_radar_message(self, radar_ip: str, message: str) -> None:
        self.log_message(f"[{radar_ip}] {message}")

    def enqueue_ui_task(self, callback) -> None:
        self.ui_task_queue.put(callback)

    def process_ui_tasks(self) -> None:
        while True:
            try:
                callback = self.ui_task_queue.get_nowait()
            except queue.Empty:
                break

            try:
                callback()
            except Exception as exc:
                try:
                    self.log_message(f"UI任务执行失败: {exc}")
                except Exception:
                    pass

    def schedule_ui_task_poll(self) -> None:
        self.process_ui_tasks()
        try:
            self.ui_task_poller_job = self.root.after(50, self.schedule_ui_task_poll)
        except tk.TclError:
            self.ui_task_poller_job = None

    def thread_log(self, message: str) -> None:
        self.enqueue_ui_task(lambda msg=message: self.log_message(msg))

    def clear_log(self) -> None:
        self.log_text.delete("1.0", tk.END)
        self.log_message("日志已清空")

    def current_elapsed_seconds(self) -> float:
        if self.test_start_time is not None:
            return time.time() - self.test_start_time
        return self.last_elapsed_seconds

    def describe_test_mode(self, mode: str | None = None) -> str:
        current_mode = mode
        if current_mode is None:
            current_mode = self.test_mode if self.test_mode != "idle" else self.last_test_mode

        if current_mode == "continuous":
            return "连续取数压力测试"
        if current_mode == "network_cycle":
            return "通断网测试"
        return "未开始测试"

    def get_network_test_params(self) -> tuple[int, int, float, float]:
        try:
            cycle_count = int(self.network_cycle_count_var.get().strip())
        except ValueError as exc:
            raise ValueError("通断网轮次请输入正整数") from exc

        try:
            verify_scans = int(self.network_verify_scans_var.get().strip())
        except ValueError as exc:
            raise ValueError("每轮验收圈数请输入正整数") from exc

        try:
            reconnect_delay = float(self.network_reconnect_delay_var.get().strip())
        except ValueError as exc:
            raise ValueError("重连等待时间请输入数字") from exc

        try:
            cycle_timeout = float(self.network_cycle_timeout_var.get().strip())
        except ValueError as exc:
            raise ValueError("单轮超时时间请输入数字") from exc

        if cycle_count <= 0:
            raise ValueError("通断网轮次必须大于 0")
        if verify_scans <= 0:
            raise ValueError("每轮验收圈数必须大于 0")
        if reconnect_delay < 0:
            raise ValueError("重连等待时间不能小于 0")
        if cycle_timeout <= 0:
            raise ValueError("单轮超时时间必须大于 0")

        return cycle_count, verify_scans, reconnect_delay, cycle_timeout

    def prepare_sessions_for_test(self, sessions: list[RadarSession]) -> None:
        for session in sessions:
            session.stats.reset()
            session.stats.clear_pending_scan()
            session.buffer.clear()
            session.receiver_thread = None
            session.receiver_active = False
            session.finalized_for_test = False
            if session.client.connected:
                try:
                    session.client.drain_data_socket()
                except OSError:
                    pass

    def sleep_with_stop(self, seconds: float) -> bool:
        end_time = time.time() + max(0.0, seconds)
        while time.time() < end_time:
            if self.stop_requested:
                return False
            time.sleep(min(0.1, max(0.0, end_time - time.time())))
        return not self.stop_requested

    def is_normal_exit_reason(self, exit_reason: str) -> bool:
        return exit_reason in {"用户停止测试", "测试结束"} or exit_reason.startswith("通断网测试完成")

    def receive_until_scans(
        self,
        session: RadarSession,
        cycle_index: int,
        total_cycles: int,
        start_completed_scans: int,
        verify_scans: int,
        cycle_timeout: float,
    ) -> tuple[bool, str]:
        data_socket = session.client.data_socket
        if data_socket is None:
            return False, f"第 {cycle_index}/{total_cycles} 轮失败: 数据端口未连接"

        target_completed_scans = start_completed_scans + verify_scans
        previous_timeout = data_socket.gettimeout()
        last_logged_completed = 0
        deadline = time.time() + cycle_timeout

        try:
            while not self.stop_requested:
                completed_in_cycle = session.stats.completed_scans - start_completed_scans
                if completed_in_cycle >= verify_scans:
                    return True, f"第 {cycle_index}/{total_cycles} 轮完成 {completed_in_cycle} 圈验收"

                if completed_in_cycle > last_logged_completed:
                    last_logged_completed = completed_in_cycle
                    self.thread_log(
                        f"[{session.radar_ip}] 第 {cycle_index}/{total_cycles} 轮验收进度: "
                        f"{completed_in_cycle}/{verify_scans} 圈"
                    )

                remaining = deadline - time.time()
                if remaining <= 0:
                    return (
                        False,
                        f"第 {cycle_index}/{total_cycles} 轮超时，已完成 {completed_in_cycle}/{verify_scans} 圈验收",
                    )

                data_socket.settimeout(min(1.0, max(0.1, remaining)))
                try:
                    data = data_socket.recv(4096)
                except socket.timeout:
                    continue

                if not data:
                    return False, f"第 {cycle_index}/{total_cycles} 轮数据端口连接已关闭"

                session.buffer.extend(data)
                self.process_buffer(session)

                if session.stats.completed_scans >= target_completed_scans:
                    return True, f"第 {cycle_index}/{total_cycles} 轮完成 {verify_scans} 圈验收"

            return False, "用户停止测试"
        finally:
            try:
                data_socket.settimeout(previous_timeout)
            except OSError:
                pass

    def parse_radar_ips(self, raw_text: str) -> list[str]:
        normalized = (
            raw_text.replace("，", ",")
            .replace("；", ";")
            .replace("\n", ",")
            .replace("\t", ",")
        )
        candidates = [item.strip() for item in re.split(r"[,;\s]+", normalized) if item.strip()]
        if not candidates:
            raise ValueError("请输入至少一个雷达 IP")

        unique_ips: list[str] = []
        seen: set[str] = set()
        for radar_ip in candidates:
            self.validate_ip(radar_ip)
            if radar_ip in seen:
                continue
            seen.add(radar_ip)
            unique_ips.append(radar_ip)
        return unique_ips

    def connected_sessions(self) -> list[RadarSession]:
        return [session for session in self.sessions.values() if session.client.connected]

    def reset_runtime_state(self) -> None:
        self.active_receiver_count = 0
        self.receiver_exit_reasons.clear()
        self.finish_called = False
        self.test_mode = "idle"

    def base_snapshot(self) -> dict:
        return {
            "connected_radars": 0,
            "active_radars": 0,
            "valid_packets": 0,
            "completed_scans": 0,
            "last_completed_scan_packet_count": 0,
            "average_packets_per_scan": 0.0,
            "invalid_frames": 0,
            "missing_packets": 0,
            "loss_rate": 0.0,
            "continuity_errors": 0,
            "time_anomalies": 0,
            "latest_interval_ms": 0.0,
            "average_interval_ms": 0.0,
            "max_interval_ms": 0.0,
            "last_seq_num": None,
            "next_expected_seq": None,
            "last_scan_cnt": None,
            "max_single_loss": 0,
            "last_loss_event": None,
            "timestamp_scale_label": "unknown",
            "network_cycles_started": 0,
            "network_cycles_completed": 0,
            "network_cycles_failed": 0,
            "last_network_cycle": 0,
            "last_network_status": "未执行通断网测试",
        }

    def aggregate_stats(self) -> dict:
        if not self.sessions:
            return self.base_snapshot()

        aggregate = self.base_snapshot()
        aggregate["connected_radars"] = len(self.connected_sessions())
        aggregate["active_radars"] = self.active_receiver_count

        total_packets_and_losses = 0
        weighted_packets_total = 0.0
        weighted_packets_count = 0
        weighted_interval_total = 0.0
        weighted_interval_count = 0
        latest_intervals: list[float] = []
        timestamp_labels: set[str] = set()
        last_loss_event: dict | None = None
        single_snapshot: dict | None = None

        for session in self.sessions.values():
            snapshot = session.stats.snapshot()
            if len(self.sessions) == 1:
                single_snapshot = snapshot

            aggregate["valid_packets"] += snapshot["valid_packets"]
            aggregate["completed_scans"] += snapshot["completed_scans"]
            aggregate["last_completed_scan_packet_count"] += snapshot["last_completed_scan_packet_count"]
            aggregate["invalid_frames"] += snapshot["invalid_frames"]
            aggregate["missing_packets"] += snapshot["missing_packets"]
            aggregate["continuity_errors"] += snapshot["continuity_errors"]
            aggregate["time_anomalies"] += snapshot["time_anomalies"]
            aggregate["max_single_loss"] = max(aggregate["max_single_loss"], snapshot["max_single_loss"])
            aggregate["max_interval_ms"] = max(aggregate["max_interval_ms"], snapshot["max_interval_ms"])
            aggregate["network_cycles_started"] += snapshot["network_cycles_started"]
            aggregate["network_cycles_completed"] += snapshot["network_cycles_completed"]
            aggregate["network_cycles_failed"] += snapshot["network_cycles_failed"]

            total_packets_and_losses += snapshot["valid_packets"] + snapshot["missing_packets"]

            if snapshot["completed_scans"] > 0:
                weighted_packets_total += snapshot["average_packets_per_scan"] * snapshot["completed_scans"]
                weighted_packets_count += snapshot["completed_scans"]
                weighted_interval_total += snapshot["average_interval_ms"] * snapshot["completed_scans"]
                weighted_interval_count += snapshot["completed_scans"]

            if snapshot["latest_interval_ms"] > 0:
                latest_intervals.append(snapshot["latest_interval_ms"])

            if snapshot["timestamp_scale_label"] and snapshot["timestamp_scale_label"] != "unknown":
                timestamp_labels.add(snapshot["timestamp_scale_label"])

            if snapshot["last_loss_event"] is not None:
                last_loss_event = {
                    **snapshot["last_loss_event"],
                    "radar_ip": session.radar_ip,
                }

        aggregate["loss_rate"] = (
            aggregate["missing_packets"] / total_packets_and_losses * 100.0
            if total_packets_and_losses
            else 0.0
        )
        aggregate["average_packets_per_scan"] = (
            weighted_packets_total / weighted_packets_count
            if weighted_packets_count
            else 0.0
        )
        aggregate["average_interval_ms"] = (
            weighted_interval_total / weighted_interval_count
            if weighted_interval_count
            else 0.0
        )
        aggregate["latest_interval_ms"] = (
            sum(latest_intervals) / len(latest_intervals)
            if latest_intervals
            else 0.0
        )
        aggregate["last_loss_event"] = last_loss_event

        if len(timestamp_labels) == 1:
            aggregate["timestamp_scale_label"] = next(iter(timestamp_labels))
        elif len(timestamp_labels) > 1:
            aggregate["timestamp_scale_label"] = "mixed"

        if single_snapshot is not None:
            aggregate["last_seq_num"] = single_snapshot["last_seq_num"]
            aggregate["next_expected_seq"] = single_snapshot["next_expected_seq"]
            aggregate["last_scan_cnt"] = single_snapshot["last_scan_cnt"]
            aggregate["last_network_cycle"] = single_snapshot["last_network_cycle"]
            aggregate["last_network_status"] = single_snapshot["last_network_status"]

        return aggregate

    def refresh_stats(self) -> None:
        snapshot = self.aggregate_stats()
        elapsed_seconds = self.current_elapsed_seconds()

        value_map = {
            "configured_radars": str(len(self.radar_configs)),
            "connected_radars": str(snapshot["connected_radars"]),
            "active_radars": str(snapshot["active_radars"]),
            "elapsed_seconds": f"{elapsed_seconds:.1f} s",
        }

        for key, value in value_map.items():
            self.stat_vars[key].set(value)
        self.refresh_radar_cards()

    def schedule_ui_refresh(self) -> None:
        self.refresh_stats()
        if self.is_running or self.stop_requested:
            self.ui_refresh_job = self.root.after(500, self.schedule_ui_refresh)
        else:
            self.ui_refresh_job = None

    def validate_ip(self, radar_ip: str) -> None:
        if not radar_ip:
            raise ValueError("请输入雷达 IP")
        try:
            ipaddress.ip_address(radar_ip)
        except ValueError as exc:
            raise ValueError(f"IP 地址格式不正确: {radar_ip}") from exc

    def build_summary_lines(self) -> list[str]:
        elapsed_seconds = self.current_elapsed_seconds()
        configured_ips = self.list_configured_radar_ips()
        radar_ip_list = configured_ips[:]
        for radar_ip in sorted(self.sessions.keys()):
            if radar_ip not in radar_ip_list:
                radar_ip_list.append(radar_ip)
        radar_ips = ", ".join(radar_ip_list) if radar_ip_list else "-"

        lines = [
            "C3 雷达压力测试结果（分雷达）",
            f"导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "",
            f"雷达 IP: {radar_ips}",
            "协议: C3 50000/52000",
            f"测试模式: {self.describe_test_mode()}",
            f"已配置雷达: {len(self.radar_configs)}",
            f"已连接雷达: {len(self.connected_sessions())}",
            f"运行中雷达: {self.active_receiver_count}",
            f"运行时长: {elapsed_seconds:.1f} s",
        ]

        for radar_ip in radar_ip_list:
            connect_status, runtime_status, reason_text, session_snapshot = self.resolve_radar_display(radar_ip)
            lines.extend(
                [
                    "",
                    f"雷达 {radar_ip}",
                    f"有效包数: {session_snapshot['valid_packets']}",
                    f"完成圈数: {session_snapshot['completed_scans']}",
                    f"最近一圈包数: {session_snapshot['last_completed_scan_packet_count']}",
                    f"平均每圈包数: {session_snapshot['average_packets_per_scan']:.2f}",
                    f"异常帧数: {session_snapshot['invalid_frames']}",
                    f"连续性异常: {session_snapshot['continuity_errors']}",
                    f"丢包总数: {session_snapshot['missing_packets']}",
                    f"丢包率: {session_snapshot['loss_rate']:.2f}%",
                    f"时间异常: {session_snapshot['time_anomalies']}",
                    f"最近圈间隔: {session_snapshot['latest_interval_ms']:.2f} ms",
                    f"平均圈间隔: {session_snapshot['average_interval_ms']:.2f} ms",
                    f"最大圈间隔: {session_snapshot['max_interval_ms']:.2f} ms",
                    f"最大单次丢包: {session_snapshot['max_single_loss']}",
                    f"时间戳换算: {session_snapshot['timestamp_scale_label']}",
                    f"最近序号: {'-' if session_snapshot['last_seq_num'] is None else session_snapshot['last_seq_num']}",
                    f"期望下一包: {'-' if session_snapshot['next_expected_seq'] is None else session_snapshot['next_expected_seq']}",
                    f"最近 scan_cnt: {'-' if session_snapshot['last_scan_cnt'] is None else session_snapshot['last_scan_cnt']}",
                    f"通断网启动轮次: {session_snapshot['network_cycles_started']}",
                    f"通断网完成轮次: {session_snapshot['network_cycles_completed']}",
                    f"通断网失败轮次: {session_snapshot['network_cycles_failed']}",
                    f"最近通断网轮次: {'-' if session_snapshot['last_network_cycle'] <= 0 else session_snapshot['last_network_cycle']}",
                    f"最近通断网状态: {session_snapshot['last_network_status']}",
                    f"连接状态: {connect_status}",
                    f"运行状态: {runtime_status}",
                    f"最近状态: {reason_text}",
                ]
            )

            last_loss_event = session_snapshot["last_loss_event"]
            if last_loss_event:
                partial_note = "（停止时按已接收顺序统计）" if last_loss_event.get("partial") else ""
                duplicate_note = f"，重复包={last_loss_event['duplicate_count']}" if last_loss_event.get("duplicate_count") else ""
                lines.append(
                    "最近一次丢包事件: "
                    f"scan_cnt={last_loss_event['scan_cnt']}, "
                    f"起始序号={last_loss_event['first_seq']}, "
                    f"结束序号={last_loss_event['last_seq']}, "
                    f"缺失包数={last_loss_event['missing']}, "
                    f"缺失列表={C3StressStats()._format_missing_packets(last_loss_event['missing_packets'])}"
                    f"{duplicate_note}{partial_note}"
                )

            lines.append("异常明细:")
            lines.extend(self.format_abnormal_event_lines(session_snapshot))

        lines.append("")
        lines.append("说明: 所有统计均按单个雷达分别输出，不做聚合求和。")
        return lines

    def export_summary(self) -> None:
        has_data = any(
            session.stats.snapshot()["valid_packets"] > 0
            or session.stats.snapshot()["completed_scans"] > 0
            or session.stats.snapshot()["invalid_frames"] > 0
            or session.stats.snapshot()["network_cycles_started"] > 0
            or session.stats.snapshot()["network_cycles_failed"] > 0
            for session in self.sessions.values()
        )
        if not has_data:
            messagebox.showwarning("提示", "当前没有可导出的测试数据")
            return

        default_name = f"c3_stress_summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        filename = filedialog.asksaveasfilename(
            title="导出测试汇总",
            initialdir=str(ROOT_DIR),
            initialfile=default_name,
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not filename:
            return

        Path(filename).write_text("\n".join(self.build_summary_lines()) + "\n", encoding="utf-8")
        self.log_message(f"测试汇总已导出: {filename}")
        messagebox.showinfo("导出完成", f"测试汇总已导出:\n{filename}")

    def connect_radar(self) -> None:
        if self.connected_sessions():
            messagebox.showinfo("提示", "已有雷达连接，请先断开后再重新连接")
            return

        try:
            radar_ips = self.get_configured_radar_ips()
        except ValueError as exc:
            messagebox.showerror("输入错误", str(exc))
            return

        if self.sessions:
            for session in self.sessions.values():
                session.client.disconnect()
            self.sessions.clear()
            self.reset_runtime_state()
            self.last_elapsed_seconds = 0.0
            self.refresh_stats()

        self.connection_errors.clear()
        self.set_radar_config_editable(False)
        self.connect_btn.config(state="disabled")
        self.start_btn.config(state="disabled")
        self.network_test_btn.config(state="disabled")
        self.disconnect_btn.config(state="disabled")
        self.set_status(f"正在连接 {len(radar_ips)} 台雷达...", "orange")
        self.log_message(f"开始连接雷达: {', '.join(radar_ips)}")

        def worker() -> None:
            connected_sessions: list[RadarSession] = []
            failed_results: list[tuple[str, str]] = []

            for radar_ip in radar_ips:
                try:
                    client = C3RadarConnection(radar_ip)
                    client.connect()
                    connected_sessions.append(RadarSession(client=client))
                except Exception as exc:
                    failed_results.append((radar_ip, str(exc)))

            self.enqueue_ui_task(
                lambda sessions=connected_sessions, failures=failed_results: self.on_connect_complete(sessions, failures)
            )

        threading.Thread(target=worker, daemon=True).start()

    def on_connect_complete(
        self,
        connected_sessions: list[RadarSession],
        failed_results: list[tuple[str, str]],
    ) -> None:
        self.sessions = {session.radar_ip: session for session in connected_sessions}
        self.connection_errors = {radar_ip: error_message for radar_ip, error_message in failed_results}
        self.refresh_stats()

        for session in connected_sessions:
            self.log_radar_message(session.radar_ip, "连接成功: 命令端口 50000，数据端口 52000")

        for radar_ip, error_message in failed_results:
            self.log_radar_message(radar_ip, f"连接失败: {error_message}")

        if connected_sessions:
            self.connect_btn.config(state="disabled")
            self.start_btn.config(state="normal")
            self.network_test_btn.config(state="normal")
            self.disconnect_btn.config(state="normal")
            self.set_radar_config_editable(False)
            self.set_network_param_editable(True)
            if failed_results:
                self.set_status(f"已连接 {len(connected_sessions)} 台，失败 {len(failed_results)} 台", "orange")
                messagebox.showwarning(
                    "部分连接失败",
                    "\n".join(
                        [f"{radar_ip}: {error_message}" for radar_ip, error_message in failed_results]
                    ),
                )
            else:
                self.set_status(f"已连接 {len(connected_sessions)} 台雷达", "green")
            return

        self.connect_btn.config(state="normal")
        self.start_btn.config(state="disabled")
        self.network_test_btn.config(state="disabled")
        self.disconnect_btn.config(state="disabled")
        self.set_radar_config_editable(True)
        self.set_network_param_editable(True)
        self.set_status("连接失败", "red")
        error_message = "\n".join(
            [f"{radar_ip}: {message}" for radar_ip, message in failed_results]
        ) or "未连接到任何雷达"
        self.log_message(f"连接失败: {error_message}")
        messagebox.showerror("连接失败", error_message)

    def disconnect_radar(self) -> None:
        if self.is_running or self.stop_requested:
            self.stop_test()
            return

        for session in self.sessions.values():
            session.client.disconnect()

        self.sessions.clear()
        self.connection_errors.clear()
        self.reset_runtime_state()
        self.test_start_time = None
        self.last_elapsed_seconds = 0.0
        self.refresh_stats()

        self.connect_btn.config(state="normal")
        self.start_btn.config(state="disabled")
        self.network_test_btn.config(state="disabled")
        self.stop_btn.config(state="disabled")
        self.disconnect_btn.config(state="disabled")
        self.set_radar_config_editable(True)
        self.set_network_param_editable(True)
        self.set_status("未连接", "red")
        self.log_message("雷达连接已断开")

    def start_test(self) -> None:
        active_sessions = self.connected_sessions()
        if not active_sessions:
            messagebox.showwarning("提示", "请先连接雷达")
            return
        if self.is_running or self.stop_requested:
            return

        started_sessions: list[RadarSession] = []
        try:
            self.prepare_sessions_for_test(list(self.sessions.values()))
            self.reset_runtime_state()
            self.test_mode = "continuous"
            self.last_test_mode = "continuous"
            self.refresh_stats()

            for session in active_sessions:
                session.client.start_stream()
                started_sessions.append(session)
        except Exception as exc:
            for session in started_sessions:
                try:
                    session.client.stop_stream()
                except Exception:
                    pass
            self.log_message(f"启动测试失败: {exc}")
            messagebox.showerror("启动失败", str(exc))
            return

        self.is_running = True
        self.stop_requested = False
        self.test_start_time = time.time()
        self.last_elapsed_seconds = 0.0
        self.active_receiver_count = len(started_sessions)
        self.start_btn.config(state="disabled")
        self.network_test_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.disconnect_btn.config(state="disabled")
        self.connect_btn.config(state="disabled")
        self.set_network_param_editable(False)
        self.set_status(f"测试中... {len(started_sessions)} 台雷达", "orange")
        self.log_message("========== 开始 C3 压力测试 ==========")
        self.log_message(f"测试模式: {self.describe_test_mode('continuous')}")
        self.log_message(f"已发送连续取数命令，开始监听 {len(started_sessions)} 台雷达的数据流")
        self.log_message("统计口径: 每台雷达独立按 scan_cnt 分圈，界面与导出均按单台雷达分别显示")

        for session in started_sessions:
            session.receiver_active = True
            self.log_radar_message(session.radar_ip, "已发送连续取数命令，开始监听 52000 数据流")
            session.receiver_thread = threading.Thread(
                target=self.receive_loop,
                args=(session.radar_ip,),
                daemon=True,
            )
            session.receiver_thread.start()

        if self.ui_refresh_job is None:
            self.schedule_ui_refresh()

    def start_network_test(self) -> None:
        active_sessions = self.connected_sessions()
        if not active_sessions:
            messagebox.showwarning("提示", "请先连接雷达")
            return
        if self.is_running or self.stop_requested:
            return

        try:
            cycle_count, verify_scans, reconnect_delay, cycle_timeout = self.get_network_test_params()
        except ValueError as exc:
            messagebox.showerror("参数错误", str(exc))
            return

        self.prepare_sessions_for_test(list(self.sessions.values()))
        self.reset_runtime_state()
        self.test_mode = "network_cycle"
        self.last_test_mode = "network_cycle"
        for session in active_sessions:
            session.stats.last_network_status = f"准备执行 {cycle_count} 轮通断网测试"

        self.is_running = True
        self.stop_requested = False
        self.test_start_time = time.time()
        self.last_elapsed_seconds = 0.0
        self.active_receiver_count = len(active_sessions)
        self.refresh_stats()

        self.start_btn.config(state="disabled")
        self.network_test_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.disconnect_btn.config(state="disabled")
        self.connect_btn.config(state="disabled")
        self.set_network_param_editable(False)
        self.set_status(f"通断网测试中... {len(active_sessions)} 台雷达", "orange")
        self.log_message("========== 开始 C3 通断网测试 ==========")
        self.log_message(
            f"测试参数: 轮次={cycle_count}, 每轮验收圈数={verify_scans}, "
            f"重连等待={reconnect_delay:.1f}s, 单轮超时={cycle_timeout:.1f}s"
        )

        for session in active_sessions:
            session.receiver_active = True
            session.receiver_thread = threading.Thread(
                target=self.network_cycle_loop,
                args=(session.radar_ip, cycle_count, verify_scans, reconnect_delay, cycle_timeout),
                daemon=True,
            )
            session.receiver_thread.start()

        if self.ui_refresh_job is None:
            self.schedule_ui_refresh()

    def stop_test(self) -> None:
        if not self.is_running and not self.stop_requested:
            return

        if self.stop_requested:
            return

        self.stop_requested = True
        self.is_running = False
        self.stop_btn.config(state="disabled")
        stop_text = "正在停止通断网测试" if self.test_mode == "network_cycle" else "正在停止测试"
        self.set_status(f"{stop_text}... 剩余 {self.active_receiver_count} 台", "orange")
        if self.test_mode == "network_cycle":
            self.log_message("正在停止通断网测试，已请求各线程尽快结束当前轮次")
        else:
            self.log_message("正在停止测试，准备向所有雷达发送停止取数命令")

        for session in self.connected_sessions():
            try:
                session.client.stop_stream()
            except Exception as exc:
                self.log_radar_message(session.radar_ip, f"发送停止取数命令失败: {exc}")

    def receive_loop(self, radar_ip: str) -> None:
        session = self.sessions.get(radar_ip)
        if session is None:
            return

        exit_reason = "测试结束"
        try:
            while self.is_running and session.client.data_socket:
                try:
                    data = session.client.data_socket.recv(4096)
                except socket.timeout:
                    continue

                if not data:
                    exit_reason = "数据端口连接已关闭"
                    break

                session.buffer.extend(data)
                self.process_buffer(session)
        except Exception as exc:
            exit_reason = f"接收异常: {exc}"
        finally:
            if self.stop_requested:
                exit_reason = "用户停止测试"
            pending_messages = session.stats.finalize_pending_scan()
            for message in pending_messages:
                self.thread_log(f"[{radar_ip}] {message}")
            self.enqueue_ui_task(
                lambda ip=radar_ip, reason=exit_reason: self.on_receiver_finished(ip, reason)
            )

    def network_cycle_loop(
        self,
        radar_ip: str,
        cycle_count: int,
        verify_scans: int,
        reconnect_delay: float,
        cycle_timeout: float,
    ) -> None:
        session = self.sessions.get(radar_ip)
        if session is None:
            return

        exit_reason = "通断网测试完成"
        try:
            for cycle_index in range(1, cycle_count + 1):
                if self.stop_requested:
                    exit_reason = "用户停止测试"
                    break

                self.thread_log(f"[{radar_ip}] {session.stats.record_network_cycle_start(cycle_index, cycle_count)}")

                if not session.client.connected:
                    try:
                        session.client.connect()
                        self.thread_log(f"[{radar_ip}] 第 {cycle_index}/{cycle_count} 轮连接成功")
                    except Exception as exc:
                        exit_reason = session.stats.record_network_cycle_failure(
                            cycle_index,
                            cycle_count,
                            f"连接失败: {exc}",
                        )
                        break

                session.stats.clear_pending_scan()
                session.buffer.clear()
                try:
                    drained_before_start = session.client.drain_data_socket()
                except OSError:
                    drained_before_start = 0
                if drained_before_start > 0:
                    self.thread_log(
                        f"[{radar_ip}] 第 {cycle_index}/{cycle_count} 轮开始前清理残留数据 {drained_before_start} 字节"
                    )

                start_completed_scans = session.stats.completed_scans
                cycle_start_time = time.time()
                stream_started = False
                success = False
                cycle_message = ""

                try:
                    session.client.start_stream()
                    stream_started = True
                    success, cycle_message = self.receive_until_scans(
                        session,
                        cycle_index,
                        cycle_count,
                        start_completed_scans,
                        verify_scans,
                        cycle_timeout,
                    )
                except Exception as exc:
                    cycle_message = f"第 {cycle_index}/{cycle_count} 轮异常: {exc}"
                finally:
                    if stream_started:
                        try:
                            session.client.stop_stream()
                        except Exception as exc:
                            if success:
                                success = False
                                cycle_message = f"第 {cycle_index}/{cycle_count} 轮停止取数失败: {exc}"
                            else:
                                self.thread_log(
                                    f"[{radar_ip}] 第 {cycle_index}/{cycle_count} 轮停止取数时异常: {exc}"
                                )

                if self.stop_requested:
                    exit_reason = "用户停止测试"
                    break

                if not success:
                    exit_reason = session.stats.record_network_cycle_failure(cycle_index, cycle_count, cycle_message)
                    break

                session.stats.clear_pending_scan()
                session.buffer.clear()
                try:
                    drained_after_stop = session.client.drain_data_socket()
                except OSError:
                    drained_after_stop = 0
                session.client.disconnect()

                success_message = session.stats.record_network_cycle_success(
                    cycle_index,
                    cycle_count,
                    verify_scans,
                    time.time() - cycle_start_time,
                )
                self.thread_log(f"[{radar_ip}] {success_message}")
                if drained_after_stop > 0:
                    self.thread_log(
                        f"[{radar_ip}] 第 {cycle_index}/{cycle_count} 轮停止后清理残留数据 {drained_after_stop} 字节"
                    )

                if cycle_index < cycle_count:
                    self.thread_log(
                        f"[{radar_ip}] 第 {cycle_index}/{cycle_count} 轮已断开，等待 {reconnect_delay:.1f} s 后进入下一轮"
                    )
                    if not self.sleep_with_stop(reconnect_delay):
                        exit_reason = "用户停止测试"
                        break

            if exit_reason == "通断网测试完成":
                try:
                    if not session.client.connected:
                        session.client.connect()
                    try:
                        session.client.drain_data_socket()
                    except OSError:
                        pass
                    exit_reason = "通断网测试完成"
                    self.thread_log(f"[{radar_ip}] 通断网测试完成，已恢复连接待机")
                except Exception as exc:
                    exit_reason = f"通断网测试完成，测试后重连失败: {exc}"
        finally:
            if self.stop_requested or not self.is_normal_exit_reason(exit_reason):
                try:
                    session.client.stop_stream()
                except Exception:
                    pass
                session.client.disconnect()

            if not exit_reason.startswith("通断网测试完成"):
                pending_messages = session.stats.finalize_pending_scan()
                for message in pending_messages:
                    self.thread_log(f"[{radar_ip}] {message}")
            else:
                session.stats.clear_pending_scan()

            session.buffer.clear()
            self.enqueue_ui_task(
                lambda ip=radar_ip, reason=exit_reason: self.on_receiver_finished(ip, reason)
            )

    def process_buffer(self, session: RadarSession) -> None:
        while True:
            sof_pos = session.buffer.find(bytes([SOF]))
            if sof_pos < 0:
                if len(session.buffer) > 8192:
                    session.buffer.clear()
                return

            if sof_pos > 0:
                del session.buffer[:sof_pos]

            if len(session.buffer) < OFFSET_LENGTH + 2:
                return

            frame_length = u16_le(session.buffer, OFFSET_LENGTH)
            if frame_length < MIN_FRAME_LENGTH:
                preview_data = bytes(session.buffer[:INVALID_FRAME_HEAD_PREVIEW_BYTES])
                message_text = session.stats.add_invalid_frame(
                    reason=f"帧长度过短，小于最小长度 {MIN_FRAME_LENGTH}",
                    category="帧长异常",
                    frame_length=frame_length,
                    buffer_length=len(session.buffer),
                    sof=format_optional_byte_hex(session.buffer[0] if session.buffer else None),
                    head_hex=format_hex_preview(preview_data, INVALID_FRAME_HEAD_PREVIEW_BYTES),
                )
                del session.buffer[0]
                self.thread_log(f"[{session.radar_ip}] {message_text}")
                continue

            if len(session.buffer) < frame_length:
                return

            frame_data = bytes(session.buffer[:frame_length])
            del session.buffer[:frame_length]

            parsed = parse_frame(frame_data, session.stats.valid_packets)
            if parsed is None:
                invalid_info = inspect_invalid_frame(
                    frame_data,
                    session.stats.valid_packets + session.stats.invalid_frames + 1,
                )
                message_text = session.stats.add_invalid_frame(
                    reason="帧解析失败",
                    category=invalid_info["category"],
                    frame_length=frame_length,
                    buffer_length=len(frame_data),
                    frame_index=invalid_info["frame_index"],
                    failure_reason=invalid_info["failure_reason"],
                    sof=invalid_info["sof"],
                    payload_type=invalid_info["payload_type"],
                    msg_id=invalid_info["msg_id"],
                    data_type=invalid_info["data_type"],
                    seq_num=invalid_info["seq_num"],
                    scan_cnt=invalid_info["scan_cnt"],
                    time_type=invalid_info["time_type"],
                    timestamp=invalid_info["timestamp"],
                    point_bytes=invalid_info["point_bytes"],
                    point_remainder=invalid_info["point_remainder"],
                    head_hex=invalid_info["head_hex"],
                    tail_hex=invalid_info["tail_hex"],
                )
                self.thread_log(f"[{session.radar_ip}] {message_text}")
                continue

            messages = session.stats.ingest_frame(parsed)
            for message in messages:
                self.thread_log(f"[{session.radar_ip}] {message}")

            if session.stats.valid_packets % 300 == 0:
                snapshot = session.stats.snapshot()
                self.thread_log(
                    f"[{session.radar_ip}] 进度汇总: 有效包={snapshot['valid_packets']}, 完成圈数={snapshot['completed_scans']}, "
                    f"丢包={snapshot['missing_packets']}, 丢包率={snapshot['loss_rate']:.2f}%"
                )

    def on_receiver_finished(self, radar_ip: str, exit_reason: str) -> None:
        session = self.sessions.get(radar_ip)
        if session is None or not session.receiver_active:
            return

        session.receiver_active = False
        session.finalized_for_test = True
        self.active_receiver_count = max(0, self.active_receiver_count - 1)
        self.receiver_exit_reasons[radar_ip] = exit_reason

        if not self.stop_requested and not self.is_normal_exit_reason(exit_reason):
            self.log_radar_message(radar_ip, f"接收线程退出: {exit_reason}")
            if self.active_receiver_count > 0:
                self.log_message("检测到有雷达异常退出，正在停止其他雷达")
                self.stop_test()

        if self.active_receiver_count > 0:
            if self.stop_requested:
                stop_text = "正在停止通断网测试" if self.test_mode == "network_cycle" else "正在停止测试"
                self.set_status(f"{stop_text}... 剩余 {self.active_receiver_count} 台", "orange")
            else:
                run_text = "通断网测试中" if self.test_mode == "network_cycle" else "测试中"
                self.set_status(f"{run_text}... {self.active_receiver_count} 台雷达", "orange")
            self.refresh_stats()
            return

        self.finish_test()

    def finish_test(self) -> None:
        if self.finish_called:
            return

        self.finish_called = True
        completed_mode = self.test_mode if self.test_mode != "idle" else self.last_test_mode
        was_running = (
            self.stop_requested
            or self.is_running
            or any(
                session.stats.snapshot()["valid_packets"] > 0
                or session.stats.snapshot()["invalid_frames"] > 0
                or session.stats.snapshot()["network_cycles_started"] > 0
                for session in self.sessions.values()
            )
        )

        self.last_elapsed_seconds = self.current_elapsed_seconds()
        self.test_start_time = None
        self.is_running = False
        self.stop_requested = False
        self.active_receiver_count = 0
        self.last_test_mode = completed_mode
        self.test_mode = "idle"
        self.refresh_stats()

        if self.ui_refresh_job is not None:
            try:
                self.root.after_cancel(self.ui_refresh_job)
            except ValueError:
                pass
            self.ui_refresh_job = None

        abnormal_exit = any(
            not self.is_normal_exit_reason(reason) for reason in self.receiver_exit_reasons.values()
        )
        if abnormal_exit:
            for session in self.sessions.values():
                session.client.disconnect()
            self.refresh_stats()

        if self.connected_sessions():
            self.start_btn.config(state="normal")
            self.network_test_btn.config(state="normal")
            self.disconnect_btn.config(state="normal")
            self.connect_btn.config(state="disabled")
            self.set_radar_config_editable(False)
            self.set_network_param_editable(True)
            finish_text = "通断网测试已完成" if completed_mode == "network_cycle" else "测试已停止"
            self.set_status(f"已连接 {len(self.connected_sessions())} 台，{finish_text}", "blue")
        else:
            self.start_btn.config(state="disabled")
            self.network_test_btn.config(state="disabled")
            self.disconnect_btn.config(state="normal" if self.sessions else "disabled")
            self.connect_btn.config(state="normal")
            self.set_radar_config_editable(True)
            self.set_network_param_editable(True)
            self.set_status("测试已停止，请重新连接", "red" if self.sessions else "red")

        self.stop_btn.config(state="disabled")

        if was_running:
            self.log_message("========== 测试结束 ==========")
            self.log_message(f"测试模式: {self.describe_test_mode(completed_mode)}")
            for radar_ip in sorted(self.receiver_exit_reasons.keys()):
                self.log_radar_message(radar_ip, f"测试结束原因: {self.receiver_exit_reasons[radar_ip]}")
            if abnormal_exit:
                self.log_message("检测到异常退出，已断开当前连接，请重新连接后再开始下一轮测试")
            self.log_summary()

    def log_summary(self) -> None:
        self.log_message("========== 本轮测试结果（分雷达） ==========")
        self.log_message(f"测试模式: {self.describe_test_mode()}")
        self.log_message(f"已配置雷达: {len(self.radar_configs)}")
        self.log_message(f"已连接雷达: {len(self.connected_sessions())}")
        self.log_message(f"运行时长: {self.current_elapsed_seconds():.1f} s")

        for radar_ip in sorted(self.sessions.keys()):
            session = self.sessions[radar_ip]
            session_snapshot = session.stats.snapshot()
            self.log_radar_message(radar_ip, "----- 单雷达统计 -----")
            self.log_radar_message(radar_ip, f"有效包数: {session_snapshot['valid_packets']}")
            self.log_radar_message(radar_ip, f"完成圈数: {session_snapshot['completed_scans']}")
            self.log_radar_message(radar_ip, f"最近一圈包数: {session_snapshot['last_completed_scan_packet_count']}")
            self.log_radar_message(radar_ip, f"平均每圈包数: {session_snapshot['average_packets_per_scan']:.2f}")
            self.log_radar_message(radar_ip, f"异常帧数: {session_snapshot['invalid_frames']}")
            self.log_radar_message(radar_ip, f"连续性异常次数: {session_snapshot['continuity_errors']}")
            self.log_radar_message(radar_ip, f"丢包总数: {session_snapshot['missing_packets']}")
            self.log_radar_message(radar_ip, f"丢包率: {session_snapshot['loss_rate']:.2f}%")
            self.log_radar_message(radar_ip, f"时间异常次数: {session_snapshot['time_anomalies']}")
            self.log_radar_message(radar_ip, f"最近圈间隔: {session_snapshot['latest_interval_ms']:.2f} ms")
            self.log_radar_message(radar_ip, f"平均圈间隔: {session_snapshot['average_interval_ms']:.2f} ms")
            self.log_radar_message(radar_ip, f"最大圈间隔: {session_snapshot['max_interval_ms']:.2f} ms")
            self.log_radar_message(radar_ip, f"最大单次丢包: {session_snapshot['max_single_loss']}")
            self.log_radar_message(radar_ip, f"时间戳换算方式: {session_snapshot['timestamp_scale_label']}")
            self.log_radar_message(
                radar_ip,
                "最近序号/期望下一包/最近 scan_cnt: "
                f"{session_snapshot['last_seq_num']} / {session_snapshot['next_expected_seq']} / {session_snapshot['last_scan_cnt']}"
            )
            self.log_radar_message(
                radar_ip,
                "通断网启动/完成/失败/最近轮次: "
                f"{session_snapshot['network_cycles_started']} / {session_snapshot['network_cycles_completed']} / "
                f"{session_snapshot['network_cycles_failed']} / "
                f"{'-' if session_snapshot['last_network_cycle'] <= 0 else session_snapshot['last_network_cycle']}"
            )
            self.log_radar_message(radar_ip, f"最近通断网状态: {session_snapshot['last_network_status']}")

            last_loss_event = session_snapshot["last_loss_event"]
            if last_loss_event:
                partial_note = "（停止时按已接收顺序统计）" if last_loss_event.get("partial") else ""
                duplicate_note = f"，重复包={last_loss_event['duplicate_count']}" if last_loss_event.get("duplicate_count") else ""
                self.log_radar_message(
                    radar_ip,
                    "最近一次丢包事件: "
                    f"scan_cnt={last_loss_event['scan_cnt']}, "
                    f"起始序号={last_loss_event['first_seq']}, "
                    f"结束序号={last_loss_event['last_seq']}, "
                    f"缺失包数={last_loss_event['missing']}, "
                    f"缺失列表={C3StressStats()._format_missing_packets(last_loss_event['missing_packets'])}"
                    f"{duplicate_note}{partial_note}"
                )

    def on_closing(self) -> None:
        if self.is_running or self.stop_requested:
            self.stop_test()
            self.root.after(300, self.on_closing)
            return

        if self.ui_task_poller_job is not None:
            try:
                self.root.after_cancel(self.ui_task_poller_job)
            except ValueError:
                pass
            self.ui_task_poller_job = None

        for session in self.sessions.values():
            session.client.disconnect()
        self.sessions.clear()

        self.root.destroy()


def main(launch_mode: str = "combined") -> None:
    root = tk.Tk()
    app = C3RadarStressTestApp(root, launch_mode=launch_mode)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()


if __name__ == "__main__":
    main()

