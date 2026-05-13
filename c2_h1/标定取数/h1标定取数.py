# -*- coding: utf-8 -*-
"""
H1 标定取数（原始报文落盘）

职责：
  - 通过网口 TCP 连接雷达（默认与仓库内其它 H1 脚本一致：2111 端口）。
  - 支持「先连接、后多次取数」：由界面「连接雷达 / 断开雷达」维护长连接，开始取数时不再重复 connect。
  - 在界面设定的「延时」结束后自动开始采集，减少操作者仓促点击带来的干扰。
  - 默认 **H1 标定取数**（非表 4-2 标准条）；应答为 **纯 Payload**。按次写入时：除「每次应答一行 hex」的主 .txt 外，另存一份 **无换行** 的 ``*_flat.txt``，将各次应答 hex **直接拼接**（便于整段复制）；连续取数仍为单文件流式 hex。

运行前提：
  - 已安装 PySide6： pip install PySide6
  - 雷达 IP/端口可达，且设备接受标定取数指令。

主要入口：
  - 直接运行本文件打开 GUI。
"""

from __future__ import annotations

import os
from typing import Optional
import socket
import sys
import time
from datetime import datetime

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

# ---------------------------------------------------------------------------
# H1「标定取数」启动指令（十六进制字符串，可含空格）。
# 与 c2_h1/盲区测试高分辨率桌面版本.py 中 RadarDataProcessor.send_command 默认帧一致。
# 注意：该业务**不属于** H2 说明书表 4-2 所列标准指令体系；详见 .cursor/skills/h2-comm-protocol/SKILL.md 扩展节。
# ---------------------------------------------------------------------------
H1_CALIB_CAPTURE_CMD_HEX = "02 02 02 02 00 09 02 64 77"

# 可选对照：H1 连续原始数据流（时间戳测试等场景），非本工具默认行为。
H1_CONTINUOUS_RAW_CMD_HEX = "02 02 02 02 00 0A 02 31 01 46"


def _hex_to_bytes(command_hex: str) -> bytes:
    """将界面/常量中的十六进制字符串转为 bytes；容忍空格分隔。"""
    compact = command_hex.replace(" ", "").strip()
    return bytes.fromhex(compact)


def create_radar_socket(host: str, port: int, connect_timeout_sec: float = 5.0) -> socket.socket:
    """
    创建并 connect 雷达 TCP 套接字；与盲区测试脚本思路一致（NODELAY、尽量放大接收缓冲）。

    供主界面「连接雷达」与采集线程内「临时建连」复用，避免两处参数漂移。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 128 * 1024)
    except OSError:
        pass
    s.settimeout(connect_timeout_sec)
    s.connect((host.strip(), int(port)))
    return s


def _bytes_to_full_hex_txt(data: bytes) -> str:
    """
    将原始字节转为「全量」十六进制文本：连续大写 hex，无空格、无换行、无偏移列。

    与标定取数「应答即 Payload、不落盘解析」的需求一致，便于整段复制或外部工具处理。
    """
    return data.hex().upper()


def _normalize_cmd_hex(command_hex: str) -> str:
    """去掉空白并转大写，便于与常量比较是否同一指令。"""
    return "".join(command_hex.split()).upper()


def is_continuous_raw_command(command_hex: str) -> bool:
    """
    是否为 H1「连续原始数据流」指令（与 H1时间戳测试通用版本.py 中一致）。

    仅在该模式下启用「采集时长」流式 recv；其它 hex 一律走「采集次数」逐次收发。
    """
    return _normalize_cmd_hex(command_hex) == _normalize_cmd_hex(H1_CONTINUOUS_RAW_CMD_HEX)


class CalibCaptureWorker(QObject):
    """
    在独立线程中完成：倒计时 -> 复用已连接套接字 -> 按指令类型分支取数并写 txt。

    设计说明：
      - **连续取数**（hex 与 ``H1_CONTINUOUS_RAW_CMD_HEX`` 一致）：发令一次后按「最大采集时长」流式 recv，hex 全量拼接（与此前连续流行为一致）。
      - **其它指令**（如标定取数）：按「采集次数」循环「发令 -> 收完一次 Payload」，每次应答写**一行** hex，行与行之间 ``\\n``；相邻两次之间可按配置**间隔**休眠（0 为无间隔），休眠期间可响应停止。
      - 若使用主界面传入的已连接套接字，结束采集时**不** shutdown/close；复用连接时停止仅置位，依赖短超时 recv 退出连续流循环。
    """

    # 供界面刷新的信号：日志一行、倒计时剩余秒、一次采集正常结束（带输出路径）、异常结束
    log_line = Signal(str)
    countdown_remaining = Signal(int)
    capture_finished = Signal(str)
    capture_failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._host = "192.168.0.240"
        self._port = 2111
        self._delay_sec = 10
        self._max_capture_sec = 0  # 0 表示仅手动停止（仅连续取数模式使用）
        self._capture_count = 1  # 非连续模式：至少 1 次
        self._inter_round_interval_ms = 0  # 非连续模式：相邻两次取数之间的间隔（毫秒），0 表示不等待
        self._output_dir = os.getcwd()
        self._command_hex = H1_CALIB_CAPTURE_CMD_HEX

        self._stop_requested = False
        self._sock: socket.socket | None = None
        self._shared_socket: socket.socket | None = None  # 主界面「连接雷达」后的长连接；采集线程只借用不销毁
        self._owns_socket = False  # True 表示本线程内自建套接字，应在 finally 中 close（当前流程通常恒为 False）
        self._reuse_connection = False  # True 表示使用主界面传入的套接字，不在此处 shutdown/close
        self._output_path = ""

    # --- 由主窗口在启动线程前注入参数（单位：秒、网络字节序由设备决定，本工具不解析）---

    def configure(
        self,
        host: str,
        port: int,
        delay_sec: int,
        max_capture_sec: int,
        capture_count: int,
        inter_round_interval_ms: int,
        output_dir: str,
        command_hex: str,
        shared_socket: socket.socket | None = None,
    ) -> None:
        self._host = host.strip()
        self._port = int(port)
        self._delay_sec = max(0, int(delay_sec))
        self._max_capture_sec = max(0, int(max_capture_sec))
        self._capture_count = max(1, int(capture_count))
        self._inter_round_interval_ms = max(0, int(inter_round_interval_ms))
        self._output_dir = output_dir
        self._command_hex = command_hex.strip() or H1_CALIB_CAPTURE_CMD_HEX
        self._shared_socket = shared_socket
        # 主界面已连接：复用同一 fd，采集结束不关套接字、停止时不 shutdown
        self._reuse_connection = shared_socket is not None

    def request_stop(self) -> None:
        """
        请求结束采集循环。

        自建连接时：shutdown 可尽快打断可能长时间无数据的 recv。
        复用长连接时：**禁止** shutdown，否则同一套接字无法再次取数；仅依赖短超时 recv 轮询退出。
        """
        self._stop_requested = True
        if self._sock is not None and not self._reuse_connection:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _drain_pending(self, sock: socket.socket, window_sec: float = 0.05) -> None:
        """
        发送指令前尽量清空内核缓冲区中的旧数据，避免与本次采集文件混在一起。

        使用非阻塞短窗口 drain，避免在异常网络下无限等待。
        """
        deadline = time.monotonic() + window_sec
        try:
            prev_timeout = sock.gettimeout()
            sock.setblocking(False)
            while time.monotonic() < deadline:
                try:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                except BlockingIOError:
                    break
        except OSError:
            pass
        finally:
            try:
                sock.setblocking(True)
                sock.settimeout(prev_timeout)
            except Exception:
                pass

    def _recv_discrete_payload(
        self,
        sock: socket.socket,
        *,
        first_timeout_sec: float = 3.0,
        idle_timeout_sec: float = 0.05,
        max_response_sec: float = 30.0,
    ) -> bytes:
        """
        收「单次指令」对应的 Payload：先等首包，再在短超时下拼包直到一次 idle 超时认为一帧结束。

        标定等指令无表 4-2 头长度字段，无法按协议切包，只能用「首包到达 + 读空闲」启发式结束；
        若设备一次回齐，通常首包后一次 idle 超时即返回。
        """
        buf = bytearray()
        deadline = time.monotonic() + max_response_sec
        awaiting_first = True
        while time.monotonic() < deadline:
            if self._stop_requested:
                break
            sock.settimeout(first_timeout_sec if awaiting_first else idle_timeout_sec)
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                if buf:
                    break
                continue
            if not chunk:
                break
            buf.extend(chunk)
            awaiting_first = False
        return bytes(buf)

    def _sleep_interruptible(self, total_sec: float) -> None:
        """
        在两次按次取数之间休眠 total_sec 秒；切分为短 sleep，便于尽快响应「停止」。

        total_sec <= 0 时立即返回（等价于无间隔）。
        """
        if total_sec <= 0:
            return
        deadline = time.monotonic() + total_sec
        while time.monotonic() < deadline:
            if self._stop_requested:
                return
            time.sleep(min(0.05, deadline - time.monotonic()))

    def _execute_continuous_capture(self, cmd: bytes) -> tuple[int, Optional[str]]:
        """
        连续原始流：发令一次，首包可选，再按最大采集时长（秒）或手动停止持续 recv；
        文件内为**无换行**的连续大写 hex（各 recv 结果直接拼接）。
        """
        self._sock.sendall(cmd)
        preamble = b""
        try:
            self._sock.settimeout(2.0)
            preamble = self._sock.recv(4096) or b""
            if preamble:
                self.log_line.emit(f"指令后已读取 {len(preamble)} 字节（连续流首段）。")
        except socket.timeout:
            self.log_line.emit("指令后 2s 内无首包，仍进入采集循环。")

        self.log_line.emit(f"开始写入连续流 hex：{self._output_path}")
        t_start = time.monotonic()
        bytes_written = 0
        self._sock.settimeout(0.5)
        with open(self._output_path, "w", encoding="utf-8", newline="\n") as out_f:
            if preamble:
                out_f.write(_bytes_to_full_hex_txt(preamble))
                bytes_written += len(preamble)
            while not self._stop_requested:
                if self._max_capture_sec > 0 and (time.monotonic() - t_start) >= self._max_capture_sec:
                    self.log_line.emit("已达到「最大采集时长」，自动停止。")
                    break
                try:
                    chunk = self._sock.recv(65536)
                except socket.timeout:
                    continue
                except OSError as exc:
                    self.log_line.emit(f"读取套接字异常（可能为手动停止）：{exc}")
                    break
                if not chunk:
                    self.log_line.emit("对端关闭连接，recv 返回空，采集结束。")
                    break
                out_f.write(_bytes_to_full_hex_txt(chunk))
                bytes_written += len(chunk)
                if (bytes_written & 0x3FFFFF) == 0:
                    out_f.flush()
        return bytes_written, None

    def _execute_discrete_capture(self, cmd: bytes) -> tuple[int, str]:
        """
        非连续指令：循环「发令 -> 收一帧 Payload」共 capture_count 次。

        写两份 UTF-8 文本：
          - 主文件：每次应答一行连续 hex，行尾 ``\\n``；
          - ``*_flat.txt``：各次应答 hex **直接追加、无换行**，等价于去掉主文件中的换行后拼接。
        最后一轮完成后不再插入间隔。
        """
        root, ext = os.path.splitext(self._output_path)
        flat_path = f"{root}_flat{ext or '.txt'}"
        self.log_line.emit(
            f"开始按次写入 hex — 主文件（按行）：{self._output_path}；无换行副本：{flat_path}"
        )
        bytes_written = 0
        with open(self._output_path, "w", encoding="utf-8", newline="\n") as out_f, open(
            flat_path, "w", encoding="utf-8", newline="\n"
        ) as out_flat:
            for idx in range(self._capture_count):
                if self._stop_requested:
                    self.log_line.emit(f"已停止：已完成 {idx} 次（目标 {self._capture_count} 次）。")
                    break
                self._drain_pending(self._sock, 0.03)
                self._sock.sendall(cmd)
                payload = self._recv_discrete_payload(self._sock)
                hex_line = _bytes_to_full_hex_txt(payload)
                out_f.write(hex_line + "\n")
                out_flat.write(hex_line)
                bytes_written += len(payload)
                self.log_line.emit(f"第 {idx + 1}/{self._capture_count} 次：收到 {len(payload)} 字节。")
                if (bytes_written & 0x1FFFFF) == 0:
                    out_f.flush()
                    out_flat.flush()
                # 相邻两次标定取数之间的间隔；最后一次之后不等待
                if (
                    idx < self._capture_count - 1
                    and self._inter_round_interval_ms > 0
                    and not self._stop_requested
                ):
                    self.log_line.emit(
                        f"间隔等待 {self._inter_round_interval_ms} ms 后进行下一次…"
                    )
                    self._sleep_interruptible(self._inter_round_interval_ms / 1000.0)
        return bytes_written, flat_path

    @Slot()
    def run(self) -> None:
        """线程入口：由 QThread.start() 间接触发。"""
        self._stop_requested = False
        self._sock = None
        self._owns_socket = False

        try:
            # ---------- 阶段 A：倒计时（秒），界面可提示操作者离开或准备被测件 ----------
            for remaining in range(self._delay_sec, 0, -1):
                if self._stop_requested:
                    self.capture_failed.emit("采集已在倒计时阶段被取消。")
                    return
                self.countdown_remaining.emit(remaining)
                self.log_line.emit(f"倒计时：{remaining} 秒后开始取数…")
                time.sleep(1.0)

            if self._stop_requested:
                self.capture_failed.emit("采集已在倒计时阶段被取消。")
                return

            self.countdown_remaining.emit(0)

            # ---------- 阶段 B：使用主界面已建立的套接字（不在此重复 connect）----------
            if self._shared_socket is None:
                self.capture_failed.emit("未检测到已连接套接字，请先点击「连接雷达」。")
                return

            self._sock = self._shared_socket
            self._owns_socket = False
            self.log_line.emit("倒计时结束，使用已连接套接字开始取数…")
            self._drain_pending(self._sock)

            cmd = _hex_to_bytes(self._command_hex)
            continuous = is_continuous_raw_command(self._command_hex)

            os.makedirs(self._output_dir, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self._output_path = os.path.join(self._output_dir, f"h1_calib_raw_{stamp}.txt")

            flat_path: Optional[str] = None
            if continuous:
                self.log_line.emit("模式：连续取数（使用「最大采集时长」，次数参数忽略）。")
                bytes_written, flat_path = self._execute_continuous_capture(cmd)
            else:
                iv = self._inter_round_interval_ms
                iv_txt = "无间隔" if iv <= 0 else f"两次间隔 {iv} ms"
                self.log_line.emit(
                    f"模式：按次取数（共 {self._capture_count} 次；{iv_txt}；每次一行 hex；采集时长参数忽略）。"
                )
                bytes_written, flat_path = self._execute_discrete_capture(cmd)

            self.log_line.emit(f"采集结束，约 {bytes_written} 字节 Payload 已写入：{self._output_path}")
            if flat_path:
                self.log_line.emit(f"无换行副本：{flat_path}")
            finished_msg = (
                f"{self._output_path}\n{flat_path}" if flat_path else self._output_path
            )
            self.capture_finished.emit(finished_msg)

        except Exception as exc:  # noqa: BLE001 — 工作线程需兜底任何异常并上报界面
            self.capture_failed.emit(f"采集过程异常：{exc}")
        finally:
            # 仅关闭本线程自建的套接字；长连接由主界面负责 disconnect
            if self._owns_socket and self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass
            self._sock = None


class MainWindow(QMainWindow):
    """标定取数主界面：参数输入、目录选择、开始/停止、日志区。"""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("H1 标定取数（原始报文）")
        self.resize(720, 520)

        self._thread: QThread | None = None
        self._worker: CalibCaptureWorker | None = None
        # 主线程持有的长连接；采集工作线程仅借用，不在线程内 close（由「断开雷达」或窗口关闭负责）
        self._persistent_sock: socket.socket | None = None

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # --- 网络参数 ---
        net_box = QGroupBox("雷达连接")
        net_layout = QHBoxLayout(net_box)
        self._host_edit = QLineEdit("192.168.0.240")
        self._port_spin = QSpinBox()
        self._port_spin.setRange(1, 65535)
        self._port_spin.setValue(2111)
        net_layout.addWidget(QLabel("IP"))
        net_layout.addWidget(self._host_edit, stretch=1)
        net_layout.addWidget(QLabel("端口"))
        net_layout.addWidget(self._port_spin)
        self._btn_connect = QPushButton("连接雷达")
        self._btn_disconnect = QPushButton("断开雷达")
        self._btn_disconnect.setEnabled(False)
        self._btn_connect.clicked.connect(self._on_connect_radar)
        self._btn_disconnect.clicked.connect(self._on_disconnect_radar)
        net_layout.addWidget(self._btn_connect)
        net_layout.addWidget(self._btn_disconnect)
        self._lbl_conn = QLabel("状态：未连接")
        net_layout.addWidget(self._lbl_conn)
        root.addWidget(net_box)

        # --- 取数行为：延时、可选最大时长、输出目录、指令（高级）---
        cap_box = QGroupBox("取数参数")
        cap_layout = QVBoxLayout(cap_box)

        row_delay = QHBoxLayout()
        row_delay.addWidget(QLabel("开始取数前延时（秒）"))
        self._delay_spin = QSpinBox()
        self._delay_spin.setRange(0, 3600)
        self._delay_spin.setValue(10)
        self._delay_spin.setToolTip(
            "点击「开始」后先倒计时，再在当前已连接套接字上发指令取数（不再重复 connect），减少人为操作仓促。"
        )
        row_delay.addWidget(self._delay_spin)
        row_delay.addStretch(1)
        cap_layout.addLayout(row_delay)

        row_cap = QHBoxLayout()
        self._lbl_duration = QLabel("最大采集时长（秒，仅连续取数；0=手动停止）")
        self._max_cap_spin = QSpinBox()
        self._max_cap_spin.setRange(0, 86400)
        self._max_cap_spin.setValue(0)
        self._max_cap_spin.setToolTip(
            "仅当启动指令为连续原始流（02…31 01 46）时生效；其它指令下本项无效。"
        )
        row_cap.addWidget(self._lbl_duration)
        row_cap.addWidget(self._max_cap_spin)
        row_cap.addStretch(1)
        cap_layout.addLayout(row_cap)

        row_cnt = QHBoxLayout()
        self._lbl_count = QLabel("采集次数（仅非连续指令；每次一行 hex）")
        self._capture_count_spin = QSpinBox()
        self._capture_count_spin.setRange(1, 100_000)
        self._capture_count_spin.setValue(1)
        self._capture_count_spin.setToolTip(
            "标定取数等：按次数重复「发令→收 Payload」，每行一条连续大写 hex，行间换行。"
        )
        row_cnt.addWidget(self._lbl_count)
        row_cnt.addWidget(self._capture_count_spin)
        row_cnt.addStretch(1)
        cap_layout.addLayout(row_cnt)

        row_gap = QHBoxLayout()
        self._lbl_interval = QLabel("标定取数间隔（毫秒，仅按次；0=无间隔）")
        self._interval_spin = QSpinBox()
        self._interval_spin.setRange(0, 3_600_000)
        self._interval_spin.setSingleStep(1)
        self._interval_spin.setValue(0)
        self._interval_spin.setToolTip(
            "仅在非连续指令（如标定 02…64 77）时生效：每完成一轮收发后，再等待本毫秒数才发下一轮；0 表示立即连续发。最大 3600000 ms（1 小时）。"
        )
        row_gap.addWidget(self._lbl_interval)
        row_gap.addWidget(self._interval_spin)
        row_gap.addStretch(1)
        cap_layout.addLayout(row_gap)

        row_dir = QHBoxLayout()
        row_dir.addWidget(QLabel("保存目录"))
        self._dir_edit = QLineEdit(os.path.join(os.getcwd(), "h1_calib_captures"))
        btn_browse = QPushButton("浏览…")
        btn_browse.clicked.connect(self._pick_output_dir)
        row_dir.addWidget(self._dir_edit, stretch=1)
        row_dir.addWidget(btn_browse)
        cap_layout.addLayout(row_dir)

        row_cmd = QHBoxLayout()
        row_cmd.addWidget(QLabel("启动指令(hex，可含空格)"))
        self._cmd_edit = QLineEdit(H1_CALIB_CAPTURE_CMD_HEX)
        self._cmd_edit.setToolTip(
            "默认：H1 标定取数 02…64 77（非 H2 表 4-2 标准条）；连续原始流可用 02…31 01 46。"
        )
        row_cmd.addWidget(self._cmd_edit, stretch=1)
        cap_layout.addLayout(row_cmd)
        self._cmd_edit.textChanged.connect(self._refresh_capture_mode_widgets)

        root.addWidget(cap_box)

        # --- 控制按钮 ---
        btn_row = QHBoxLayout()
        self._btn_start = QPushButton("开始（先延时再取数）")
        self._btn_stop = QPushButton("停止")
        self._btn_stop.setEnabled(False)
        self._lbl_countdown = QLabel("倒计时：—")
        self._lbl_countdown.setAlignment(Qt.AlignCenter)
        btn_row.addWidget(self._btn_start)
        btn_row.addWidget(self._btn_stop)
        root.addLayout(btn_row)
        root.addWidget(self._lbl_countdown)

        self._log = QTextEdit()
        self._log.setReadOnly(True)
        root.addWidget(self._log, stretch=1)

        self._btn_start.clicked.connect(self._on_start)
        self._btn_stop.clicked.connect(self._on_stop)

        self._sync_connection_ui()
        self._refresh_capture_mode_widgets()

    @Slot()
    def _refresh_capture_mode_widgets(self) -> None:
        """根据启动指令是否为「连续取数」切换「时长」与「次数」哪一侧可用。"""
        cont = is_continuous_raw_command(self._cmd_edit.text())
        self._max_cap_spin.setEnabled(cont)
        self._lbl_duration.setEnabled(cont)
        self._capture_count_spin.setEnabled(not cont)
        self._lbl_count.setEnabled(not cont)
        self._interval_spin.setEnabled(not cont)
        self._lbl_interval.setEnabled(not cont)

    def _pick_output_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择原始报文保存目录", self._dir_edit.text())
        if path:
            self._dir_edit.setText(path)

    def _sync_connection_ui(self) -> None:
        """根据 self._persistent_sock 更新按钮与 IP/端口控件的可用性。"""
        connected = self._persistent_sock is not None
        self._btn_connect.setEnabled(not connected and not self._is_capturing())
        self._btn_disconnect.setEnabled(connected and not self._is_capturing())
        self._host_edit.setEnabled(not connected)
        self._port_spin.setEnabled(not connected)
        self._lbl_conn.setText("状态：已连接" if connected else "状态：未连接")

    def _is_capturing(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    def _close_persistent_socket(self, log_message: str | None = None) -> None:
        """关闭长连接并清空引用；用于断开按钮、异常后的清理、窗口关闭。"""
        if self._persistent_sock is not None:
            try:
                self._persistent_sock.close()
            except OSError:
                pass
            self._persistent_sock = None
        if log_message:
            self._append_log(log_message)
        self._sync_connection_ui()

    @Slot()
    def _on_connect_radar(self) -> None:
        """在主线程建立 TCP；成功后多次「开始取数」共用此套接字。"""
        if self._persistent_sock is not None:
            QMessageBox.information(self, "提示", "当前已处于连接状态。")
            return
        if self._is_capturing():
            QMessageBox.warning(self, "提示", "正在取数中，请稍后再连接。")
            return
        host = self._host_edit.text().strip()
        port = self._port_spin.value()
        try:
            self._persistent_sock = create_radar_socket(host, port, connect_timeout_sec=5.0)
            # 默认阻塞模式；具体超时由采集线程在 recv 时设置
            self._persistent_sock.settimeout(None)
            self._append_log(f"已连接雷达 {host}:{port}")
        except OSError as exc:
            self._persistent_sock = None
            QMessageBox.critical(self, "连接失败", str(exc))
        self._sync_connection_ui()

    @Slot()
    def _on_disconnect_radar(self) -> None:
        """主动断开长连接；取数进行中禁止断开（需先停止）。"""
        if self._is_capturing():
            QMessageBox.warning(self, "提示", "请先点击「停止」结束当前取数，再断开雷达。")
            return
        self._close_persistent_socket("已断开雷达连接。")

    def _append_log(self, text: str) -> None:
        self._log.append(text)
        self._log.ensureCursorVisible()

    @Slot(int)
    def _on_countdown(self, sec: int) -> None:
        if sec > 0:
            self._lbl_countdown.setText(f"倒计时：{sec} 秒")
        else:
            self._lbl_countdown.setText("倒计时：已开始取数")

    def _on_start(self) -> None:
        if self._thread is not None:
            QMessageBox.information(self, "提示", "采集已在进行中。")
            return

        if self._persistent_sock is None:
            QMessageBox.warning(self, "未连接", "请先点击「连接雷达」，再开始取数。")
            return

        out_dir = self._dir_edit.text().strip()
        if not out_dir:
            QMessageBox.warning(self, "参数错误", "请填写有效的保存目录。")
            return

        self._btn_start.setEnabled(False)
        self._btn_stop.setEnabled(True)
        self._sync_connection_ui()

        self._thread = QThread()
        self._worker = CalibCaptureWorker()
        self._worker.moveToThread(self._thread)

        self._worker.configure(
            host=self._host_edit.text(),
            port=self._port_spin.value(),
            delay_sec=self._delay_spin.value(),
            max_capture_sec=self._max_cap_spin.value(),
            capture_count=self._capture_count_spin.value(),
            inter_round_interval_ms=self._interval_spin.value(),
            output_dir=out_dir,
            command_hex=self._cmd_edit.text(),
            shared_socket=self._persistent_sock,
        )

        self._thread.started.connect(self._worker.run)
        self._worker.log_line.connect(self._append_log)
        self._worker.countdown_remaining.connect(self._on_countdown)
        self._worker.capture_finished.connect(self._on_capture_finished)
        self._worker.capture_failed.connect(self._on_capture_failed)
        self._worker.capture_finished.connect(self._thread.quit)
        self._worker.capture_failed.connect(self._thread.quit)
        self._worker.capture_finished.connect(self._worker.deleteLater)
        self._worker.capture_failed.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._on_thread_finished)

        self._append_log("—— 新一轮采集线程已启动 ——")
        self._thread.start()

    @Slot()
    def _on_stop(self) -> None:
        if self._worker is not None:
            self._worker.request_stop()
            self._append_log("已请求停止（正在等待读取循环退出）…")

    @Slot(str)
    def _on_capture_finished(self, path: str) -> None:
        # 按次模式可能为两行路径：主文件 + *_flat.txt（无换行拼接副本）
        QMessageBox.information(self, "完成", f"十六进制文本已保存：\n{path}")

    @Slot(str)
    def _on_capture_failed(self, msg: str) -> None:
        self._append_log(msg)
        # 倒计时阶段取消不破坏连接；其它失败（协议/网络/内部错误）倾向于释放套接字，避免误用半残 fd
        if self._persistent_sock is not None and "倒计时阶段" not in msg:
            self._close_persistent_socket("因错误已释放连接，请重新「连接雷达」。")
        if "倒计时阶段" in msg:
            QMessageBox.warning(self, "已取消", msg)
        else:
            QMessageBox.critical(self, "失败", msg)
        self._sync_connection_ui()

    @Slot()
    def _on_thread_finished(self) -> None:
        self._thread = None
        self._worker = None
        self._btn_start.setEnabled(True)
        self._btn_stop.setEnabled(False)
        self._lbl_countdown.setText("倒计时：—")
        self._sync_connection_ui()

    def closeEvent(self, event) -> None:  # noqa: N802 — Qt 命名约定
        if self._worker is not None:
            self._worker.request_stop()
        if self._thread is not None and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(3000)
        self._close_persistent_socket()
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
