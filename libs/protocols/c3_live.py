from __future__ import annotations

import socket
import time
import zlib
from typing import Dict, List, Optional

from libs.protocols.c3_common import OFFSET_LENGTH, SOF, parse_c3_frame, u16_le


HEADER_WITHOUT_CRC_LEN = 5
HEADER_CRC_OFFSET = 5
PAYLOAD_OFFSET = 7
CRC32_SIZE = 4
MIN_FRAME_LENGTH = PAYLOAD_OFFSET + CRC32_SIZE
MAX_FRAME_LENGTH = 65535


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


class C3RadarConnector:
    def __init__(
        self,
        host: str = "192.168.1.111",
        cmd_port: int = 50000,
        data_port: int = 52000,
        connect_timeout: float = 3.0,
        data_timeout: float = 1.0,
    ) -> None:
        self.host = host
        self.cmd_port = cmd_port
        self.data_port = data_port
        self.connect_timeout = connect_timeout
        self.data_timeout = data_timeout
        self.cmd_socket: Optional[socket.socket] = None
        self.data_socket: Optional[socket.socket] = None
        self.last_error = ""

    def connect(self) -> bool:
        self.close()
        self.last_error = ""

        try:
            self.cmd_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.cmd_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.cmd_socket.settimeout(self.connect_timeout)
            self.cmd_socket.connect((self.host, self.cmd_port))

            self.data_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.data_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.data_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 256 * 1024)
            self.data_socket.settimeout(self.data_timeout)
            self.data_socket.connect((self.host, self.data_port))
            return True
        except Exception as exc:
            self.last_error = str(exc)
            self.close()
            return False

    def initialize(self) -> bool:
        if self.cmd_socket is None:
            self.last_error = "命令端口未连接"
            return False

        try:
            cmd1 = bytes.fromhex("5A1000000078CD0038C720CB0C986FFE")
            expected_prefix = bytes.fromhex("5A0D0001007F310000FF12")
            if not self._send_command(cmd1):
                return False

            response1 = self._receive_response()
            if not response1 or not response1.startswith(expected_prefix):
                self.last_error = "初始化命令1响应异常"
                return False

            cmd2 = bytes.fromhex("5A0E000002FF24020001EA3DC28B")
            if not self._send_command(cmd2):
                return False

            response2 = self._receive_response()
            if not response2:
                self.last_error = "初始化命令2无响应"
                return False

            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False

    def _send_command(self, payload: bytes) -> bool:
        try:
            assert self.cmd_socket is not None
            self.cmd_socket.send(payload)
            return True
        except Exception as exc:
            self.last_error = str(exc)
            return False

    def _receive_response(self, timeout: float = 3.0) -> Optional[bytes]:
        try:
            assert self.cmd_socket is not None
            self.cmd_socket.settimeout(timeout)
            return self.cmd_socket.recv(1024)
        except Exception as exc:
            self.last_error = str(exc)
            return None

    def close(self) -> None:
        if self.cmd_socket is not None:
            try:
                self.cmd_socket.close()
            finally:
                self.cmd_socket = None

        if self.data_socket is not None:
            try:
                self.data_socket.close()
            finally:
                self.data_socket = None


class C3FrameStreamDecoder:
    def __init__(self) -> None:
        self.buffer = bytearray()
        self.frame_index = 0
        self.valid_frame_count = 0
        self.invalid_frame_count = 0

    def feed(self, chunk: bytes) -> List[Dict]:
        if not chunk:
            return []

        self.buffer.extend(chunk)
        parsed_frames: List[Dict] = []

        while True:
            sof_index = self._find_sof()
            if sof_index < 0:
                break

            if len(self.buffer) < 3:
                break

            frame_length = u16_le(self.buffer, OFFSET_LENGTH)
            if frame_length < MIN_FRAME_LENGTH or frame_length > MAX_FRAME_LENGTH:
                del self.buffer[0]
                self.invalid_frame_count += 1
                continue

            if len(self.buffer) < frame_length:
                break

            frame = bytes(self.buffer[:frame_length])
            del self.buffer[:frame_length]

            if not self._is_valid_frame(frame):
                self.invalid_frame_count += 1
                continue

            parsed = parse_c3_frame(frame, self.frame_index, include_points=True, include_raw=False)
            self.frame_index += 1
            if parsed is None:
                self.invalid_frame_count += 1
                continue

            parsed_frames.append(parsed)
            self.valid_frame_count += 1

        if len(self.buffer) > 1024 * 1024:
            self.buffer.clear()

        return parsed_frames

    def _find_sof(self) -> int:
        if not self.buffer:
            return -1

        sof_index = self.buffer.find(bytes([SOF]))
        if sof_index < 0:
            self.buffer.clear()
            return -1

        if sof_index > 0:
            del self.buffer[:sof_index]

        return 0

    def _is_valid_frame(self, frame: bytes) -> bool:
        if len(frame) < MIN_FRAME_LENGTH:
            return False

        header_crc_recv = u16_le(frame, HEADER_CRC_OFFSET)
        header_crc_calc = crc16_modbus(frame[:HEADER_WITHOUT_CRC_LEN])
        if header_crc_recv != header_crc_calc:
            return False

        payload = frame[PAYLOAD_OFFSET:-CRC32_SIZE]
        payload_crc_recv = int.from_bytes(frame[-CRC32_SIZE:], byteorder="little", signed=False)
        payload_crc_calc = zlib.crc32(payload) & 0xFFFFFFFF
        return payload_crc_recv == payload_crc_calc

