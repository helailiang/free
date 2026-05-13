from __future__ import annotations

import re
import struct
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional

SOF = 0x5A

OFFSET_LENGTH = 1
OFFSET_PAYLOAD_TYPE = 3
OFFSET_SEQ_NUM = 4
OFFSET_HEADER_CRC = 5
OFFSET_MSG_ID = 7
OFFSET_SCAN_CNT = 8
OFFSET_DATA_TYPE = 9
OFFSET_TIME_TYPE = 10
OFFSET_TIMESTAMP = 11
OFFSET_POINTS = 19

PAYLOAD_TYPE_CMD = 0x00
PAYLOAD_TYPE_ACK = 0x01
PAYLOAD_TYPE_MSG = 0x02
POINT_CLOUD_MSG_ID = 0x05
DATA_TYPE_POLAR = {0x02, 0x03, 0x04}
POINT_SIZE = 6


def u16_le(buf: bytes, offset: int) -> int:
    return struct.unpack_from("<H", buf, offset)[0]


def u32_le(buf: bytes, offset: int) -> int:
    return struct.unpack_from("<I", buf, offset)[0]


def u64_le(buf: bytes, offset: int) -> int:
    return struct.unpack_from("<Q", buf, offset)[0]


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def crc32_ieee(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def build_c3_frame(payload_type: int, seq_num: int, payload: bytes) -> bytes:
    length = 1 + 2 + 1 + 1 + 2 + len(payload) + 4
    header = bytes(
        [
            SOF,
            length & 0xFF,
            (length >> 8) & 0xFF,
            payload_type & 0xFF,
            seq_num & 0xFF,
        ]
    )
    header_crc = crc16_modbus(header)
    payload_crc = crc32_ieee(payload)
    return (
        header
        + struct.pack("<H", header_crc)
        + payload
        + struct.pack("<I", payload_crc)
    )


def parse_c3_transport_frame(frame: bytes, *, verify_crc: bool = False) -> Optional[Dict[str, Any]]:
    if len(frame) < 12:
        return None
    if frame[0] != SOF:
        return None

    length = u16_le(frame, OFFSET_LENGTH)
    if length != len(frame):
        return None

    header_crc = u16_le(frame, OFFSET_HEADER_CRC)
    payload = frame[7:-4]
    payload_crc = u32_le(frame, len(frame) - 4)

    if verify_crc:
        if header_crc != crc16_modbus(frame[:5]):
            return None
        if payload_crc != crc32_ieee(payload):
            return None

    return {
        "length": length,
        "payload_type": frame[OFFSET_PAYLOAD_TYPE],
        "seq_num": frame[OFFSET_SEQ_NUM],
        "header_crc": header_crc,
        "payload": payload,
        "payload_crc": payload_crc,
        "raw_data": frame,
    }


def circular_angle_diff_deg(a: float, b: float) -> float:
    diff = abs(a - b) % 360.0
    return min(diff, 360.0 - diff)


def read_hex_stream(txt_path: str) -> bytes:
    text = Path(txt_path).read_text(encoding="utf-8", errors="ignore")
    tokens = re.findall(r'0x[0-9A-Fa-f]{2}|(?<![0-9A-Fa-f])[0-9A-Fa-f]{2}(?![0-9A-Fa-f])', text)
    hex_str = ''.join(token.replace('0x', '').replace('0X', '') for token in tokens)
    if len(hex_str) % 2 != 0:
        raise ValueError('Extracted hex string has odd length')
    return bytes.fromhex(hex_str)


def split_frames(stream: bytes, *, min_length: int = 12) -> List[bytes]:
    frames: List[bytes] = []
    i = 0
    n = len(stream)

    while i < n:
        if stream[i] != SOF:
            i += 1
            continue

        if i + 3 > n:
            break

        length = u16_le(stream, i + OFFSET_LENGTH)
        if length < min_length:
            i += 1
            continue
        if i + length > n:
            break

        frame = stream[i:i + length]
        if frame[0] == SOF:
            frames.append(frame)
            i += length
        else:
            i += 1

    return frames


def parse_c3_frame(
    frame: bytes,
    frame_index: int,
    *,
    include_points: bool = True,
    include_raw: bool = False,
) -> Optional[Dict[str, Any]]:
    if len(frame) < OFFSET_POINTS + 4:
        return None

    length = u16_le(frame, OFFSET_LENGTH)
    if length != len(frame):
        return None

    if frame[OFFSET_PAYLOAD_TYPE] != PAYLOAD_TYPE_MSG:
        return None
    if frame[OFFSET_MSG_ID] != POINT_CLOUD_MSG_ID:
        return None

    data_type = frame[OFFSET_DATA_TYPE]
    if data_type not in DATA_TYPE_POLAR:
        return None

    seq_num = frame[OFFSET_SEQ_NUM]
    scan_cnt = frame[OFFSET_SCAN_CNT]
    time_type = frame[OFFSET_TIME_TYPE]
    timestamp = u64_le(frame, OFFSET_TIMESTAMP)
    points_region = frame[OFFSET_POINTS: len(frame) - 4]
    point_count = len(points_region) // POINT_SIZE

    parsed: Dict[str, Any] = {
        'frame_index': frame_index,
        'seq_num': seq_num,
        'scan_cnt': scan_cnt,
        'data_type': data_type,
        'time_type': time_type,
        'timestamp': timestamp,
        'point_count': point_count,
        'length': length,
    }

    if include_points:
        points: List[Dict[str, Any]] = []
        for i in range(point_count):
            base = i * POINT_SIZE
            distance = u16_le(points_region, base + 0)
            angle_raw = u16_le(points_region, base + 2)
            reflectivity = u16_le(points_region, base + 4)
            points.append(
                {
                    'frame_index': frame_index,
                    'seq_num': seq_num,
                    'scan_cnt': scan_cnt,
                    'timestamp': timestamp,
                    'point_index': i,
                    'r_mm': distance,
                    'angle_raw': angle_raw,
                    'angle_deg': angle_raw / 100.0,
                    'reflectivity': reflectivity,
                }
            )
        parsed['points'] = points

    if include_raw:
        parsed['raw_data'] = frame

    return parsed


def parse_txt_frames(
    txt_path: str,
    *,
    include_points: bool = True,
    include_raw: bool = False,
) -> List[Dict[str, Any]]:
    stream = read_hex_stream(txt_path)
    frames = split_frames(stream)
    parsed_frames: List[Dict[str, Any]] = []
    for idx, frame in enumerate(frames):
        info = parse_c3_frame(frame, idx, include_points=include_points, include_raw=include_raw)
        if info is not None:
            parsed_frames.append(info)
    return parsed_frames


def flatten_points(parsed_frames: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    all_points: List[Dict[str, Any]] = []
    for frame in parsed_frames:
        all_points.extend(frame.get('points', []))
    return all_points


def filter_points_by_angle(
    points: List[Dict[str, Any]],
    target_angle_deg: float,
    tolerance_deg: float,
) -> List[Dict[str, Any]]:
    return [
        point
        for point in points
        if circular_angle_diff_deg(point['angle_deg'], target_angle_deg) <= tolerance_deg
    ]
