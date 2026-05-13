from __future__ import annotations

import socket
import time
from typing import Dict, List, Optional


DATA_HEADER = b"\x02\x02\x02\x02"
START_CONTINUOUS_COMMAND = bytes.fromhex("02 02 02 02 00 0A 02 31 01 46")
STOP_CONTINUOUS_COMMAND = bytes.fromhex("02 02 02 02 00 0A 02 31 00 45")

PACKET_HEADER_SIZE = 22
PACKET_TRAILER_SIZE = 11
MIN_PACKET_LENGTH = PACKET_HEADER_SIZE + PACKET_TRAILER_SIZE
MAX_PACKET_LENGTH = 4096


def _u16_be(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], byteorder="big", signed=False)


def parse_c2_packet(packet: bytes, packet_number: int = 0, include_points: bool = True) -> Optional[Dict]:
    if len(packet) < MIN_PACKET_LENGTH:
        return None

    if not packet.startswith(DATA_HEADER):
        return None

    packet_length = _u16_be(packet, 4)
    if packet_length != len(packet):
        return None

    points_in_packet = _u16_be(packet, 20)
    payload_end = PACKET_HEADER_SIZE + points_in_packet * 4
    if payload_end > len(packet) - PACKET_TRAILER_SIZE:
        return None

    resolution_deg = _u16_be(packet, 14) / 10000.0
    start_point_index = _u16_be(packet, 18)
    payload = packet[PACKET_HEADER_SIZE:payload_end]
    points: List[Dict] = []

    if include_points:
        for idx in range(points_in_packet):
            base = idx * 4
            distance_mm = int.from_bytes(payload[base : base + 2], byteorder="big", signed=False)
            reflectivity = int.from_bytes(payload[base + 2 : base + 4], byteorder="big", signed=False)
            point_index = start_point_index + idx
            points.append(
                {
                    "point_index": point_index,
                    "distance_mm": distance_mm,
                    "reflectivity": reflectivity,
                    "angle_deg": point_index * resolution_deg,
                }
            )

    return {
        "packet_number": packet_number,
        "packet_length": packet_length,
        "packet_type": packet[6:8].hex().upper(),
        "status_byte": packet[8],
        "scan_count": _u16_be(packet, 9),
        "packet_index": packet[11],
        "rotation_speed_hz": _u16_be(packet, 12) / 100.0,
        "resolution_deg": resolution_deg,
        "total_points_per_scan": _u16_be(packet, 16),
        "start_point_index": start_point_index,
        "points_in_packet": points_in_packet,
        "timestamp_hex": packet[-11:-1].hex().upper(),
        "checksum": packet[-1],
        "raw_packet": packet,
        "points": points,
    }


class C2RadarConnector:
    def __init__(
        self,
        host: str = "192.168.1.85",
        port: int = 2111,
        connect_timeout: float = 3.0,
        data_timeout: float = 1.5,
    ) -> None:
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.data_timeout = data_timeout
        self.socket: Optional[socket.socket] = None
        self.last_error = ""

    def connect(self) -> bool:
        self.close()
        self.last_error = ""

        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 256 * 1024)
            self.socket.settimeout(self.connect_timeout)
            self.socket.connect((self.host, self.port))
            self.socket.settimeout(self.data_timeout)
            return True
        except Exception as exc:
            self.last_error = str(exc)
            self.close()
            return False

    def start_stream(self) -> bool:
        if self.socket is None:
            self.last_error = "数据端口未连接"
            return False

        try:
            self.socket.sendall(START_CONTINUOUS_COMMAND)
            self._try_receive_acknowledge()
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False

    def stop_stream(self) -> bool:
        if self.socket is None:
            return True

        try:
            self.socket.sendall(STOP_CONTINUOUS_COMMAND)
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False

    def _try_receive_acknowledge(self) -> None:
        if self.socket is None:
            return

        original_timeout = self.socket.gettimeout()
        try:
            self.socket.settimeout(min(self.data_timeout, 0.5))
            peek = self.socket.recv(10, socket.MSG_PEEK)
            if len(peek) >= 10 and peek.startswith(DATA_HEADER) and peek[4:8] == b"\x00\x0A\x12\x31":
                self.socket.recv(10)
        except socket.timeout:
            pass
        finally:
            self.socket.settimeout(original_timeout)

    def close(self) -> None:
        if self.socket is not None:
            try:
                self.socket.close()
            finally:
                self.socket = None


class C2PacketStreamDecoder:
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.packet_number = 0
        self.valid_packet_count = 0
        self.invalid_packet_count = 0
        self.discarded_bytes = 0

    def feed(self, chunk: bytes) -> List[Dict]:
        if not chunk:
            return []

        self.buffer.extend(chunk)
        packets: List[Dict] = []

        while True:
            header_pos = self.buffer.find(DATA_HEADER)
            if header_pos < 0:
                if len(self.buffer) > len(DATA_HEADER):
                    self.discarded_bytes += len(self.buffer) - len(DATA_HEADER)
                    del self.buffer[:-len(DATA_HEADER)]
                break

            if header_pos > 0:
                self.discarded_bytes += header_pos
                del self.buffer[:header_pos]

            if len(self.buffer) < 6:
                break

            packet_length = _u16_be(self.buffer, 4)
            if packet_length < MIN_PACKET_LENGTH or packet_length > MAX_PACKET_LENGTH:
                del self.buffer[0]
                self.invalid_packet_count += 1
                self.discarded_bytes += 1
                continue

            if len(self.buffer) < packet_length:
                break

            packet = bytes(self.buffer[:packet_length])
            del self.buffer[:packet_length]

            parsed = parse_c2_packet(packet, self.packet_number, include_points=True)
            self.packet_number += 1
            if parsed is None:
                self.invalid_packet_count += 1
                continue

            self.valid_packet_count += 1
            packets.append(parsed)

        if len(self.buffer) > 1024 * 1024:
            self.buffer.clear()

        return packets


class C2ScanAssembler:
    def __init__(self) -> None:
        self.current_scan_count: Optional[int] = None
        self.current_packets: Dict[int, Dict] = {}
        self.current_total_points = 0
        self.completed_scan_index = 0
        self.last_emit_monotonic: Optional[float] = None

    def add_packet(self, packet: Dict) -> List[Dict]:
        completed_scans: List[Dict] = []

        if self.current_scan_count is None:
            self.current_scan_count = packet["scan_count"]

        if packet["scan_count"] != self.current_scan_count:
            previous_scan = self._build_scan(complete=self._is_current_complete())
            if previous_scan is not None:
                completed_scans.append(previous_scan)
            self._reset_current()
            self.current_scan_count = packet["scan_count"]

        packet_index = packet["packet_index"]
        existing_packet = self.current_packets.get(packet_index)
        if existing_packet is None or packet["points_in_packet"] >= existing_packet["points_in_packet"]:
            self.current_packets[packet_index] = packet
            self.current_total_points = max(self.current_total_points, packet["total_points_per_scan"])

        if self._is_current_complete():
            completed_scan = self._build_scan(complete=True)
            if completed_scan is not None:
                completed_scans.append(completed_scan)
            self._reset_current()

        return completed_scans

    def flush(self) -> List[Dict]:
        if not self.current_packets:
            return []

        scan = self._build_scan(complete=self._is_current_complete())
        self._reset_current()
        return [scan] if scan is not None else []

    def _reset_current(self) -> None:
        self.current_scan_count = None
        self.current_packets = {}
        self.current_total_points = 0

    def _is_current_complete(self) -> bool:
        if not self.current_packets or self.current_total_points <= 0:
            return False

        expected_start = 0
        for packet in sorted(self.current_packets.values(), key=lambda item: item["start_point_index"]):
            if packet["start_point_index"] != expected_start:
                return False
            expected_start += packet["points_in_packet"]
            if expected_start >= self.current_total_points:
                return True
        return False

    def _build_scan(self, complete: bool) -> Optional[Dict]:
        if not self.current_packets:
            return None

        packets = sorted(self.current_packets.values(), key=lambda item: item["start_point_index"])
        points: List[Dict] = []
        for packet in packets:
            points.extend(packet["points"])

        if not points:
            return None

        now = time.monotonic()
        scan_period_s = 0.0
        scan_rate_hz = 0.0
        if self.last_emit_monotonic is not None:
            scan_period_s = now - self.last_emit_monotonic
            if scan_period_s > 0:
                scan_rate_hz = 1.0 / scan_period_s
        self.last_emit_monotonic = now

        distances = [point["distance_mm"] for point in points if point["distance_mm"] > 0]
        distance_min_mm = min(distances) if distances else 0
        distance_max_mm = max(distances) if distances else 0
        distance_mean_mm = (sum(distances) / len(distances)) if distances else 0.0

        self.completed_scan_index += 1
        return {
            "scan_index": self.completed_scan_index,
            "scan_count": packets[0]["scan_count"],
            "packet_count": len(packets),
            "packet_index_min": min(packet["packet_index"] for packet in packets),
            "packet_index_max": max(packet["packet_index"] for packet in packets),
            "points_received": len(points),
            "total_points_expected": self.current_total_points,
            "missing_points": max(self.current_total_points - len(points), 0),
            "resolution_deg": packets[0]["resolution_deg"],
            "rotation_speed_hz": packets[0]["rotation_speed_hz"],
            "frame_complete": complete,
            "scan_rate_hz": scan_rate_hz,
            "scan_period_s": scan_period_s,
            "distance_min_mm": distance_min_mm,
            "distance_max_mm": distance_max_mm,
            "distance_mean_mm": distance_mean_mm,
            "points": points,
        }
