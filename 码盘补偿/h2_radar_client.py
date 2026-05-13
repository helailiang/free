"""
H2（H1E0-02A）TCP 文本协议：登录 4.2.1 + 单次点云 4.2.22，与 H1 标定取数并行的一套接口。

依据《H1E0-02A 产品说明书（V2.0）》表 4-2、4.2.1、4.2.22；点云解析复用 libs.protocols.h2_txt_parse。
供 h2_resolution_gui_test.py、h2_single_scan_pointcount.py 使用。
"""
from __future__ import annotations

import socket
import sys
import time
from pathlib import Path
from typing import Any


def _import_root_dir() -> Path:
    """
    源码运行时：仓库根目录（含 libs），与 h2_radar_client 所在「码盘补偿」的上一级一致。
    PyInstaller 单文件/目录打包后：依赖与 libs 位于 sys._MEIPASS，须优先从此处解析 libs.protocols。
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parents[1]


_ROOT = _import_root_dir()
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from libs.protocols.h2_txt_parse import (  # noqa: E402
    H2_CMD_SINGLE_SCAN_DATA,
    H2_TEXT_SOF,
    parse_h2_pointcloud_frame,
)

H2_SINGLE_SCAN_REQUEST = bytes.fromhex("020202020009023043")


def checksum8(body_without_final_checksum: bytes) -> int:
    return sum(body_without_final_checksum) & 0xFF


def build_login_frame(permission: int, password4: bytes) -> bytes:
    if len(password4) != 4:
        raise ValueError("password4 须为 4 字节")
    inner = bytes([0x02, 0x01, permission & 0xFF]) + password4
    length = 4 + 2 + len(inner) + 1
    head = H2_TEXT_SOF + length.to_bytes(2, "big")
    body_wo_cs = head + inner
    return body_wo_cs + bytes([checksum8(body_wo_cs)])


def pop_complete_h2_frames(buf: bytearray) -> list[bytes]:
    out: list[bytes] = []
    sof = H2_TEXT_SOF
    while True:
        j = buf.find(sof)
        if j < 0:
            break
        if j > 0:
            del buf[:j]
        if len(buf) < 6:
            break
        L = int.from_bytes(buf[4:6], "big")
        if L < 12 or L > 4_000_000:
            del buf[:1]
            continue
        if len(buf) < L:
            break
        out.append(bytes(buf[:L]))
        del buf[:L]
    return out


def drain_socket(sock: socket.socket, max_bytes: int = 65536, window_s: float = 0.05) -> None:
    sock.settimeout(window_s)
    n = 0
    try:
        while n < max_bytes:
            chunk = sock.recv(8192)
            if not chunk:
                break
            n += len(chunk)
    except (socket.timeout, OSError):
        pass


def recv_pointcloud_after_single_request(
    sock: socket.socket,
    carry: bytearray,
    *,
    idle_s: float,
    max_total_s: float,
    scan_start_deg: float,
) -> list[dict[str, Any]]:
    """
    从 sock 读取 4.2.22 分包点云。carry 在多次调用间复用，避免半帧残留在前一次局部 buf 中被丢弃导致错位或后续 WinError。
    """
    sock.settimeout(idle_s)
    buf = carry
    parsed_packets: list[dict[str, Any]] = []
    deadline = time.monotonic() + max_total_s
    last_growth = time.monotonic()
    target_scan: int | None = None
    expected_circle: int | None = None
    acc_points = 0

    while time.monotonic() < deadline:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            if parsed_packets and expected_circle is not None and acc_points >= expected_circle:
                break
            if time.monotonic() - last_growth > idle_s and buf:
                break
            if time.monotonic() - last_growth > idle_s and not buf:
                break
            continue
        if not chunk:
            break
        buf.extend(chunk)
        last_growth = time.monotonic()

        for frame in pop_complete_h2_frames(buf):
            if len(frame) < 22 or frame[6] != 0x12 or frame[7] != H2_CMD_SINGLE_SCAN_DATA:
                continue
            info = parse_h2_pointcloud_frame(
                frame,
                len(parsed_packets),
                include_points=True,
                scan_start_deg=scan_start_deg,
                verify_checksum=False,
            )
            if info is None:
                continue
            if target_scan is None:
                target_scan = int(info["scan_cnt"])
                expected_circle = int(info["h2_points_per_circle"])
            elif int(info["scan_cnt"]) != target_scan:
                continue
            parsed_packets.append(info)
            acc_points += int(info["point_count"])
            if expected_circle is not None and acc_points >= expected_circle:
                deadline = min(deadline, time.monotonic() + idle_s)

    return parsed_packets


def merge_h2_points(packets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    pts: list[dict[str, Any]] = []
    for p in packets:
        pts.extend(p.get("points") or [])
    pts.sort(key=lambda x: int(x["point_index"]))
    return pts


class H2SingleScanRadar:
    """
    与 H1CalibrationRadar 对齐的调用面：connect_radar / close / configure_scan_parameters /
    optimized_single_measurement，内部走 4.2.22 单次点云（非 H1 标定 02 64 77）。
    """

    def __init__(self, host: str = "192.168.1.111", port: int = 2111, connect_timeout: float = 5.0) -> None:
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self._sock: socket.socket | None = None
        self.last_error = ""
        self.angular_resolution_deg = 0.1
        self.start_angle_deg = -45.0
        self.login_permission = 3
        self.login_password_4: bytes = bytes.fromhex("F4724744")
        self.recv_idle_s = 0.2
        self.recv_max_s = 3.0
        self._rx_carry = bytearray()

    @property
    def socket(self) -> socket.socket | None:
        return self._sock

    def configure_scan_parameters(
        self,
        angular_resolution_deg: float | None = None,
        scan_angle_range_deg: float | None = None,
        start_angle_deg: float | None = None,
    ) -> None:
        if angular_resolution_deg is not None and angular_resolution_deg > 0:
            self.angular_resolution_deg = float(angular_resolution_deg)
        if start_angle_deg is not None:
            self.start_angle_deg = float(start_angle_deg)
        _ = scan_angle_range_deg

    def close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            finally:
                self._sock = None
        self._rx_carry.clear()

    def connect_radar(self) -> bool:
        self.last_error = ""
        self.close()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 128 * 1024)
            s.settimeout(self.connect_timeout)
            s.connect((self.host, int(self.port)))
            drain_socket(s)
            lf = build_login_frame(int(self.login_permission), self.login_password_4)
            s.sendall(lf)
            resp = s.recv(4096)
            if not resp:
                self.last_error = "登录无应答"
                s.close()
                return False
            if b"\x12\x01\x01" not in resp:
                self.last_error = "登录应答中未检测到成功特征 12 01 01"
                s.close()
                return False
            drain_socket(s)
            self._rx_carry.clear()
            self._sock = s
            return True
        except OSError as e:
            self.last_error = str(e)
            self.close()
            return False

    def _merged_to_h1_style(self, merged: list[dict[str, Any]]) -> list[dict[str, Any]]:
        st = self.start_angle_deg
        ang = self.angular_resolution_deg
        out: list[dict[str, Any]] = []
        for pt in merged:
            idx = int(pt["point_index"])
            out.append(
                {
                    "index": idx,
                    "angle_deg": st + idx * ang,
                    "measured_distance": int(pt["r_mm"]),
                    "front_edge": 0,
                    "back_edge": 0,
                    "reflectivity": int(pt["reflectivity"]),
                }
            )
        return out

    def optimized_single_measurement(
        self, start_index: int, end_index: int, max_distance: int | None = None
    ) -> dict[str, Any] | None:
        if self._sock is None:
            self.last_error = "雷达未连接"
            return None
        try:
            self._sock.sendall(H2_SINGLE_SCAN_REQUEST)
            packets = recv_pointcloud_after_single_request(
                self._sock,
                self._rx_carry,
                idle_s=float(self.recv_idle_s),
                max_total_s=float(self.recv_max_s),
                scan_start_deg=float(self.start_angle_deg),
            )
        except OSError as e:
            self.last_error = str(e)
            return None

        if not packets:
            self.last_error = "未收到 4.2.22 点云应答（指令 0x30）"
            return None

        merged = merge_h2_points(packets)
        all_full = self._merged_to_h1_style(merged)
        lo, hi = int(start_index), int(end_index)
        all_results = [p for p in all_full if lo <= int(p["index"]) <= hi]
        if not all_results:
            self.last_error = f"索引窗 [{lo},{hi}] 内无点（本帧索引约 {all_full[0]['index']}～{all_full[-1]['index']}）"
            return None

        if max_distance is not None:
            filtered_results = [r for r in all_results if 10 < int(r["measured_distance"]) < int(max_distance)]
            has_consecutive = False
            consecutive_points: list[dict[str, Any]] = []
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
            "scan_angle_range_deg": 270.0,
            "start_angle_deg": self.start_angle_deg,
            "expected_point_count": int(packets[0].get("h2_points_per_circle", len(all_full))),
        }
