"""
H1E0-02A（H2 系列）网口 TCP 文本帧点云解析：表 4-2、4.2.22 单次/分包、4.2.23 连续数据（0x32）。
与 C3 HOST-ARM 二进制帧无关；请勿与 `c3_common` 混用拆帧逻辑。
"""

from __future__ import annotations

import struct
from typing import Any, Dict, List, Optional

from libs.protocols.c3_common import filter_points_by_angle, flatten_points, read_hex_stream

H2_TEXT_SOF = bytes([0x02, 0x02, 0x02, 0x02])
H2_OP_REPLY = 0x12
H2_CMD_SINGLE_SCAN_DATA = 0x30
H2_CMD_CONTINUOUS_SCAN_DATA = 0x32


def u16_be(buf: bytes, offset: int) -> int:
    return struct.unpack_from(">H", buf, offset)[0]


def split_h2_text_frames(stream: bytes, *, min_length: int = 12) -> List[bytes]:
    """按表 4-2 大端「文本长度」切包（对齐 `H1时间戳测试通用版本.py` recv_radar_packet）。"""
    frames: List[bytes] = []
    i = 0
    n = len(stream)
    sof = H2_TEXT_SOF

    while i < n:
        j = stream.find(sof, i)
        if j < 0:
            break
        if j + 6 > n:
            break
        length = u16_be(stream, j + 4)
        if length < min_length or j + length > n:
            i = j + 1
            continue
        frames.append(stream[j : j + length])
        i = j + length

    return frames


def angle_deg_to_h2_point_index(
    angle_deg: float,
    *,
    scan_start_deg: float,
    angle_resolution_deg: float,
) -> int:
    """目标角度（度）→ 全局点索引；与按索引取点的脚本互为逆映射。"""
    if angle_resolution_deg <= 0:
        raise ValueError("angle_resolution_deg must be > 0")
    return int(round((angle_deg - scan_start_deg) / angle_resolution_deg))


def h2_point_index_to_angle_deg(
    point_index: int,
    *,
    scan_start_deg: float,
    angle_resolution_deg: float,
) -> float:
    return scan_start_deg + float(point_index) * angle_resolution_deg


def _parse_h2_timestamp_10b(payload_tail: bytes) -> int:
    if len(payload_tail) < 10:
        return 0
    return int.from_bytes(payload_tail[:10], byteorder="big", signed=False)


def parse_h2_pointcloud_frame(
    frame: bytes,
    frame_index: int,
    *,
    include_points: bool = True,
    include_raw: bool = False,
    scan_start_deg: float = -45.0,
    verify_checksum: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    解析 4.2.22/4.2.23 点云应答：参数 7 首点全局索引、参数 8=N、参数 9=4N（距离+反射率，大端）。
    """
    min_total = 22 + 10 + 1
    if len(frame) < min_total:
        return None
    if frame[:4] != H2_TEXT_SOF:
        return None

    length = u16_be(frame, 4)
    if length != len(frame):
        return None

    if verify_checksum and (sum(frame[:-1]) & 0xFF) != frame[-1]:
        return None

    op = frame[6]
    cmd = frame[7]
    if op != H2_OP_REPLY or cmd not in (H2_CMD_SINGLE_SCAN_DATA, H2_CMD_CONTINUOUS_SCAN_DATA):
        return None

    status = frame[8]
    scan_cnt = u16_be(frame, 9)
    packet_num = frame[11]
    freq_x100 = u16_be(frame, 12)
    angle_res_x10000 = u16_be(frame, 14)
    points_per_circle = u16_be(frame, 16)
    first_point_index = u16_be(frame, 18)
    n_in_packet = u16_be(frame, 20)

    angle_resolution_deg = angle_res_x10000 / 10000.0
    payload_points = frame[22 : 22 + 4 * n_in_packet]
    if len(payload_points) != 4 * n_in_packet:
        return None

    tail_after_points = frame[22 + 4 * n_in_packet : -1]
    ts_compact = _parse_h2_timestamp_10b(tail_after_points[:10])

    parsed: Dict[str, Any] = {
        "frame_index": frame_index,
        "seq_num": packet_num,
        "scan_cnt": scan_cnt,
        "data_type": 0xFD,
        "time_type": status,
        "timestamp": ts_compact,
        "point_count": n_in_packet,
        "length": length,
        "h2_packet_num": packet_num,
        "h2_freq_x100": freq_x100,
        "h2_angle_resolution_deg": angle_resolution_deg,
        "h2_points_per_circle": points_per_circle,
        "h2_first_point_index": first_point_index,
        "h2_scan_start_deg": scan_start_deg,
    }

    if include_points:
        points: List[Dict[str, Any]] = []
        for i in range(n_in_packet):
            base = i * 4
            distance = u16_be(payload_points, base + 0)
            reflectivity = u16_be(payload_points, base + 2)
            global_idx = first_point_index + i
            angle_deg = h2_point_index_to_angle_deg(
                global_idx,
                scan_start_deg=scan_start_deg,
                angle_resolution_deg=angle_resolution_deg,
            )
            angle_raw = int(round(angle_deg * 100))
            points.append(
                {
                    "frame_index": frame_index,
                    "seq_num": packet_num,
                    "scan_cnt": scan_cnt,
                    "timestamp": ts_compact,
                    "point_index": global_idx,
                    "r_mm": distance,
                    "angle_raw": angle_raw,
                    "angle_deg": angle_deg,
                    "reflectivity": reflectivity,
                }
            )
        parsed["points"] = points

    if include_raw:
        parsed["raw_data"] = frame

    return parsed


def parse_h2_txt_frames(
    txt_path: str,
    *,
    include_points: bool = True,
    include_raw: bool = False,
    scan_start_deg: float = -45.0,
    verify_checksum: bool = False,
) -> List[Dict[str, Any]]:
    """从 hex 文本日志解析 H2 点云帧（与 `c3_common.parse_txt_frames` 入口对称，仅协议不同）。"""
    stream = read_hex_stream(txt_path)
    frames = split_h2_text_frames(stream)
    parsed_frames: List[Dict[str, Any]] = []
    for idx, frame in enumerate(frames):
        info = parse_h2_pointcloud_frame(
            frame,
            idx,
            include_points=include_points,
            include_raw=include_raw,
            scan_start_deg=scan_start_deg,
            verify_checksum=verify_checksum,
        )
        if info is not None:
            parsed_frames.append(info)
    return parsed_frames
