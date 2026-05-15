"""
雷达 TCP 客户端基础能力。

这里集中处理 socket 生命周期、表 4-2 文本帧切包和连续取数统计。H1 与 C2 第一版
都使用 2111/TCP 与 `02 02 02 02 + length` 形态的数据帧，因此公共逻辑放在基类，
具体型号只覆盖登录、启动/停止命令和配置查询等差异点。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import socket
import time
from typing import Any

from libs.protocols.h2_txt_parse import H2_TEXT_SOF, parse_h2_pointcloud_frame
from network_test.automation.config import DeviceConfig, hex_to_bytes
from network_test.automation.metrics import PacketInfo, StreamMetrics, StreamStats


class RadarClientError(RuntimeError):
    """雷达客户端统一异常，测试层用它区分设备错误和测试代码错误。"""


class BaseRadarClient(ABC):
    """C2/H1 TCP 客户端基类，封装连接、发送、接收和文本帧拆分。"""

    def __init__(self, config: DeviceConfig) -> None:
        self.config = config
        self.sock: socket.socket | None = None
        self.last_error = ""
        self._rx_buffer = bytearray()

    @property
    def model(self) -> str:
        """返回归一化型号，报告中统一使用 c2/h1，避免 h2 历史命名混淆。"""
        return self.config.normalized_model

    def connect(self) -> None:
        """建立 TCP 连接，并交给子类执行型号特有的会话初始化。"""
        self.close()
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 256 * 1024)
            sock.settimeout(float(self.config.connect_timeout_s))
            sock.connect((self.config.host, int(self.config.port)))
            self.sock = sock
            self._rx_buffer.clear()
            self.after_connect()
        except OSError as exc:
            self.last_error = str(exc)
            self.close()
            raise RadarClientError(f"{self.config.host}:{self.config.port} 连接失败: {exc}") from exc

    def after_connect(self) -> None:
        """
        连接建立后的型号专属初始化。

        C2 当前脚本无登录步骤，H1 依据说明书 4.2.1 需要登录，所以默认留空由 H1 覆盖。
        """

    def close(self) -> None:
        """关闭 socket 并清理半帧缓存，保证下次连接不会继承旧数据。"""
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None
        self._rx_buffer.clear()

    def send_command(self, command: bytes, *, expect_reply: bool = True, recv_size: int = 4096) -> bytes:
        """发送一条协议命令；需要应答时返回原始字节，便于上层做特征判断。"""
        if self.sock is None:
            raise RadarClientError("雷达未连接，不能发送命令")
        try:
            self.sock.sendall(command)
            if not expect_reply:
                return b""
            self.sock.settimeout(float(self.config.recv_timeout_s))
            return self.sock.recv(recv_size)
        except OSError as exc:
            self.last_error = str(exc)
            raise RadarClientError(f"命令发送或接收失败: {exc}") from exc

    def drain_socket(self, *, max_bytes: int = 65536, idle_s: float = 0.05) -> None:
        """清空连接建立后残留的旧帧，避免第一条测试数据被历史缓存污染。"""
        if self.sock is None:
            return
        total = 0
        old_timeout = self.sock.gettimeout()
        self.sock.settimeout(idle_s)
        try:
            while total < max_bytes:
                chunk = self.sock.recv(min(8192, max_bytes - total))
                if not chunk:
                    break
                total += len(chunk)
        except (socket.timeout, OSError):
            pass
        finally:
            self.sock.settimeout(old_timeout)

    def pop_complete_frames(self) -> list[bytes]:
        """
        从接收缓存中按 `SOF + 大端长度` 弹出完整帧。

        H1 说明书表 4-2 的文本长度包含起始、长度、操作码、指令、参数和校验字节。
        C2 现有压力测试脚本也按同样位置读取长度，因此第一版共用该拆包策略。
        """
        frames: list[bytes] = []
        while True:
            start = self._rx_buffer.find(H2_TEXT_SOF)
            if start < 0:
                self._rx_buffer.clear()
                break
            if start > 0:
                del self._rx_buffer[:start]
            if len(self._rx_buffer) < 6:
                break
            frame_len = int.from_bytes(self._rx_buffer[4:6], "big")
            if frame_len < 9 or frame_len > 4_000_000:
                del self._rx_buffer[:1]
                continue
            if len(self._rx_buffer) < frame_len:
                break
            frames.append(bytes(self._rx_buffer[:frame_len]))
            del self._rx_buffer[:frame_len]
        return frames

    def decode_packet(self, frame: bytes, frame_index: int) -> PacketInfo | None:
        """
        把协议帧转换成统一统计包。

        优先使用仓库已有 `parse_h2_pointcloud_frame`，它覆盖 H1 4.2.22/4.2.23。
        如果 C2 固件返回字段相近但命令号或尾部略有差异，则使用旧 C200 脚本中的
        scan_index/pack_index 位置做最小兜底统计，保证现场能先看到连续性结果。
        """
        parsed = parse_h2_pointcloud_frame(
            frame,
            frame_index,
            include_points=False,
            scan_start_deg=float(self.config.stream.scan_start_deg),
            verify_checksum=False,
        )
        if parsed:
            return PacketInfo(
                scan_id=int(parsed["scan_cnt"]),
                packet_id=int(parsed["seq_num"]),
                point_count=int(parsed["point_count"]),
                timestamp=(parsed.get("timestamp") or 0),
                raw_length=len(frame),
                parse_source="h1_text_frame",
            )

        # C200 旧脚本按 hex 下标 18:22、22:24 抽取圈号和包号，对应字节 9:11、11。
        # 这里仅作为连续性统计兜底，不替代正式 C2 协议解析。
        if len(frame) >= 24:
            try:
                return PacketInfo(
                    scan_id=int.from_bytes(frame[9:11], "big"),
                    packet_id=int(frame[11]),
                    point_count=max(0, (len(frame) - 23) // 4),
                    raw_length=len(frame),
                    parse_source="c2_legacy_offsets",
                )
            except (ValueError, IndexError):
                return None
        return None

    def read_stream_stats(self, *, duration_s: float, max_cycles: int | None = None) -> StreamStats:
        """
        读取连续数据流并返回统计结果。

        `max_cycles` 表示收满多少「完整圈」（每圈 expected_packets_per_scan 个包号齐全），
        不是见到多少个不同圈号就停。`duration_s` 用于长稳窗口；二者先达到任一条件即停止。
        """
        if self.sock is None:
            raise RadarClientError("雷达未连接，不能读取数据流")

        metrics = StreamMetrics(
            model=self.model,
            host=self.config.host,
            expected_packets_per_scan=int(self.config.stream.expected_packets_per_scan),
        )
        deadline = time.monotonic() + max(0.1, float(duration_s))
        frame_index = 0
        self.sock.settimeout(float(self.config.recv_timeout_s))
        stop_after_cycles = False

        while time.monotonic() < deadline:
            if stop_after_cycles:
                break
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                continue
            except OSError as exc:
                self.last_error = str(exc)
                raise RadarClientError(f"读取数据流失败: {exc}") from exc

            if not chunk:
                break
            self._rx_buffer.extend(chunk)

            for frame in self.pop_complete_frames():
                packet = self.decode_packet(frame, frame_index)
                frame_index += 1
                if packet is None:
                    metrics.add_parse_error(f"无法解析帧，长度={len(frame)}")
                    continue
                metrics.add_packet(packet)
                if max_cycles is not None and metrics.completed_scan_count() >= int(max_cycles):
                    stop_after_cycles = True
                    break

        return metrics.finish()

    @abstractmethod
    def start_streaming(self) -> None:
        """启动连续取数，由具体型号决定命令和应答处理方式。"""

    @abstractmethod
    def stop_streaming(self) -> None:
        """停止连续取数，由具体型号决定命令和应答处理方式。"""

    def query_config(self) -> bytes:
        """读取设备配置；默认不支持，C2/H1 子类按协议补充。"""
        raise RadarClientError(f"{self.model} 客户端暂未实现配置查询")

    def command_bytes(self, name: str) -> bytes:
        """按配置字段名取 hex 命令并转换为 bytes，集中处理错误提示。"""
        value: Any = getattr(self.config.commands, name)
        return hex_to_bytes(str(value))
