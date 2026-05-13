#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
newpre.md 角分辨率 + Y100SC 连续步进测试（PySide6 独立界面）。

流程：在连续测试区内设置「起始索引」；可选「探测最高反射索引」按多帧统计
（日志每帧打印反射率最高的 10 个点及本轮采纳的 index）；再「开始连续测试」。

与 angle_resolution_test_app.py、newpre_resolution_cli_test.py 并列；不修改上述文件。

运行（在「码盘补偿」目录）:
  python newpre_resolution_gui_test.py

打包 exe（与目录内 pyproject 一致，需 uv + PyInstaller）:
  uv sync --group build && uv run pyinstaller --noconfirm newpre_resolution_gui.spec
  输出 dist\\NewpreResolutionGui.exe

依赖: PySide6、pyserial；可选 openpyxl 用于导出 .xlsx。
"""
from __future__ import annotations 

import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from h1_radar_reader import H1CalibrationRadar
from newpre_resolution_cli_test import (
    build_step_row,
    export_all_index_points,
    export_rows,
    fmt_rotate_cumulative,
    lookup_point_by_index,
    summarize,
    move_turntable,
)
from y100sc_client import Axis, Sign, Y100SCError


def top_n_reflectivity_in_window(
    all_results: list[dict[str, Any]], min_mm: int, max_mm: int, n: int = 10
) -> list[dict[str, Any]]:
    """距离窗内按反射率降序、index 升序，取前 n 个点（每条为解析字典）。"""
    cand = [p for p in all_results if min_mm < int(p["measured_distance"]) < max_mm]
    cand.sort(key=lambda p: (-int(p["reflectivity"]), int(p["index"])))
    return cand[: max(0, n)]


def pick_max_reflectivity_index(
    all_results: list[dict[str, Any]], min_mm: int, max_mm: int
) -> int | None:
    """在距离窗内取反射率最大的点索引（同强度时取较小 index）。"""
    top = top_n_reflectivity_in_window(all_results, min_mm, max_mm, 1)
    if not top:
        return None
    return int(top[0]["index"])


TABLE_HEADERS = [
    "step",
    "转台角度",
    "targetIndex",
    "index偏移",
    "雷达测得角(°)",
    "理论角度(°)",
    "角度误差(°)",
    "距离(m)",
    "强度",
    "异常",
]


@dataclass
class NewpreGuiConfig:
    radar_ip: str
    radar_port: int
    index_start: int
    index_end: int
    min_distance_mm: int
    max_distance_mm: int
    angular_res_deg: float
    step_angle_deg: float
    start_index: int
    com: str
    baud: int
    axis: Axis
    direction: Sign
    pulses_per_step: int
    settle_ms: int
    steps: int
    calibration_header_size: int


class NewpreSequenceThread(QThread):
    progress = Signal(int, int, object)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        cfg: NewpreGuiConfig,
        external_radar: H1CalibrationRadar | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._external_radar = external_radar
        self._stop = False

    def stop(self) -> None:
        self._stop = True
        self.requestInterruption()

    def _sleep_interruptible(self, total_s: float, chunk_s: float = 0.05) -> bool:
        """分段睡眠以便尽快响应停止；若用户停止返回 False。"""
        deadline = time.monotonic() + total_s
        while time.monotonic() < deadline:
            if self._stop or self.isInterruptionRequested():
                return False
            time.sleep(min(chunk_s, deadline - time.monotonic()))
        return True

    def run(self) -> None:
        cfg = self._cfg
        # 单连接雷达：若主界面已连同一 IP/端口，必须复用，不可再 connect 第二次。
        if self._external_radar is not None:
            radar = self._external_radar
            own_socket = False
        else:
            radar = H1CalibrationRadar(host=cfg.radar_ip, port=cfg.radar_port)
            if not radar.connect_radar():
                self.failed.emit(radar.last_error or "雷达连接失败")
                return
            own_socket = True
        radar.configure_scan_parameters(angular_resolution_deg=cfg.angular_res_deg)
        radar.calibration_header_size = int(cfg.calibration_header_size)
        sign = 1 if cfg.direction == "+" else -1
        rows: list[dict[str, Any]] = []

        try:
            self._run_sequence(cfg, radar, sign, rows)
        except Y100SCError as e:
            self.failed.emit(f"转台/控制器：{e}")
        except (OSError, ValueError, RuntimeError) as e:
            self.failed.emit(str(e))
        except Exception as e:
            self.failed.emit(f"未预期错误：{e!s}")
        finally:
            if own_socket:
                radar.close()

    def _run_sequence(
        self,
        cfg: NewpreGuiConfig,
        radar: H1CalibrationRadar,
        sign: int,
        rows: list[dict[str, Any]],
    ) -> None:
        m0 = radar.optimized_single_measurement(
            cfg.index_start, cfg.index_end, cfg.max_distance_mm
        )
        if not m0:
            self.failed.emit(radar.last_error or "step0：采集失败")
            return
        init_idx = int(cfg.start_index)
        all0 = m0.get("all_results") or []
        p0 = lookup_point_by_index(all0, init_idx)
        if p0 is None:
            self.failed.emit(
                f"step0：点云中无起始索引 index={init_idx}（检查索引起止是否包含该 index）"
            )
            return
        init_mm = int(p0["measured_distance"])
        init_i = int(p0["reflectivity"])

        ts0 = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        row0 = build_step_row(
            step=0,
            rotate_label="0°",
            rotate_deg=0.0,
            target_index=init_idx,
            index_start=init_idx,
            index_end=init_idx,
            delta_index=0,
            measured_angle_deg=0.0,
            theory_angle_deg=0.0,
            error_deg=0.0,
            timestamp=ts0,
            anomalies="",
            raw=p0,
        )
        rows.append(row0)
        self.progress.emit(0, cfg.steps, row0)

        settle_s = cfg.settle_ms / 1000.0

        for k in range(1, cfg.steps + 1):
            if self._stop or self.isInterruptionRequested():
                break
            move_turntable(cfg.com, cfg.baud, cfg.axis, cfg.direction, cfg.pulses_per_step)
            if not self._sleep_interruptible(settle_s):
                break

            theory_idx = init_idx + sign * k
            theory_deg = sign * k * cfg.step_angle_deg

            m1 = radar.optimized_single_measurement(
                cfg.index_start, cfg.index_end, cfg.max_distance_mm
            )
            if not m1:
                self.failed.emit(radar.last_error or f"step{k}：采集失败")
                return
            all1 = m1.get("all_results") or []
            pk = lookup_point_by_index(all1, theory_idx)
            if pk is None:
                self.failed.emit(
                    f"step{k}：点云中无 index={theory_idx}（请扩大索引起止以覆盖起始索引±步数）"
                )
                return
            cur_idx = theory_idx
            d_idx = cur_idx - init_idx
            measured_deg = d_idx * cfg.angular_res_deg
            err_deg = measured_deg - theory_deg

            anomalies: list[str] = []
            dm = int(pk["measured_distance"])
            if abs(dm - init_mm) > max(500, int(0.05 * max(init_mm, 1))):
                anomalies.append("距离突变")
            di = int(pk["reflectivity"])
            if init_i > 0 and abs(di - init_i) > max(80, int(0.5 * init_i)):
                anomalies.append("强度异常")

            ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
            row = build_step_row(
                step=k,
                rotate_label=fmt_rotate_cumulative(theory_deg),
                rotate_deg=float(theory_deg),
                target_index=cur_idx,
                index_start="",
                index_end="",
                delta_index=int(d_idx),
                measured_angle_deg=float(measured_deg),
                theory_angle_deg=float(theory_deg),
                error_deg=float(err_deg),
                timestamp=ts,
                anomalies="；".join(anomalies),
                raw=pk,
            )
            rows.append(row)
            self.progress.emit(k, cfg.steps, row)

        self.finished_ok.emit(rows)


class ProbeReflectThread(QThread):
    """连续若干帧采集，每帧在距离窗内取反射率最高的 index，再对 index 列表取中位数写入起始索引。"""

    detail_log = Signal(str)
    finished_ok = Signal(int, object)
    failed = Signal(str)

    def __init__(
        self,
        radar: H1CalibrationRadar,
        index_start: int,
        index_end: int,
        max_distance_mm: int,
        min_distance_mm: int,
        repeat: int,
        angular_res_deg: float,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._radar = radar
        self._index_start = index_start
        self._index_end = index_end
        self._max_distance_mm = max_distance_mm
        self._min_distance_mm = min_distance_mm
        self._repeat = max(1, repeat)
        self._angular_res_deg = angular_res_deg

    def run(self) -> None:
        self._radar.configure_scan_parameters(angular_resolution_deg=self._angular_res_deg)
        indices: list[int] = []
        for i in range(self._repeat):
            m = self._radar.optimized_single_measurement(
                self._index_start, self._index_end, self._max_distance_mm
            )
            if not m:
                self.failed.emit(self._radar.last_error or f"探测第{i + 1}次：采集失败")
                return
            ix = pick_max_reflectivity_index(
                m.get("all_results") or [], self._min_distance_mm, self._max_distance_mm
            )
            if ix is None:
                self.failed.emit(f"探测第{i + 1}次：距离窗内无有效点")
                return
            top10 = top_n_reflectivity_in_window(
                m.get("all_results") or [], self._min_distance_mm, self._max_distance_mm, 10
            )
            lines = [
                f"  #{j + 1:2d}  index={int(p['index']):4d}  反射率={int(p['reflectivity']):5d}  "
                f"距离={int(p['measured_distance'])}mm  angle={float(p['angle_deg']):.2f}°"
                for j, p in enumerate(top10)
            ]
            if not lines:
                lines = ["  (距离窗内无点)"]
            n_show = len(top10)
            self.detail_log.emit(
                f"探测第 {i + 1}/{self._repeat} 次 — 距离窗内按反射率从高到低列出（最多 10 条，"
                f"实际 {n_show} 条；#1 反射率最高，往下应递减或持平，勿从下往上读成递增）：\n"
                + "\n".join(lines)
                + f"\n  → 本轮采纳 index={ix}（窗内反射率全局最高；同率取较小 index）"
            )
            indices.append(ix)
            time.sleep(0.05)
        med = int(statistics.median(indices))
        self.finished_ok.emit(med, indices)


class NewpreResolutionMainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("newpre.md 角分辨率测试（PySide6）")
        self.resize(980, 760)
        self._worker: NewpreSequenceThread | None = None
        self._probe_thread: ProbeReflectThread | None = None
        self._session_rows: list[dict[str, Any]] = []

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        gb_radar = QGroupBox("雷达")
        g_r = QGridLayout(gb_radar)
        self.leRadarIp = QLineEdit("192.168.1.111")
        self.leRadarPort = QLineEdit("2111")
        self.btnRadarConnect = QPushButton("连接")
        self.btnRadarDisconnect = QPushButton("断开")
        self.lblRadarState = QLabel("雷达：未连接")
        self.dsbAngularRes = QDoubleSpinBox()
        self.dsbAngularRes.setRange(0.001, 10.0)
        self.dsbAngularRes.setDecimals(3)
        self.dsbAngularRes.setSingleStep(0.01)
        self.dsbAngularRes.setValue(0.1)
        self.dsbStepAngle = QDoubleSpinBox()
        self.dsbStepAngle.setRange(0.001, 10.0)
        self.dsbStepAngle.setDecimals(3)
        self.dsbStepAngle.setSingleStep(0.01)
        self.dsbStepAngle.setValue(0.1)
        g_r.addWidget(QLabel("IP"), 0, 0)
        g_r.addWidget(self.leRadarIp, 0, 1)
        g_r.addWidget(QLabel("端口"), 0, 2)
        g_r.addWidget(self.leRadarPort, 0, 3)
        g_r.addWidget(self.btnRadarConnect, 0, 4)
        g_r.addWidget(self.btnRadarDisconnect, 0, 5)
        g_r.addWidget(self.lblRadarState, 1, 0, 1, 4)
        g_r.addWidget(QLabel("角分辨率(°/index)"), 1, 4)
        g_r.addWidget(self.dsbAngularRes, 1, 5)
        g_r.addWidget(QLabel("每步理论角(°)"), 2, 4)
        g_r.addWidget(self.dsbStepAngle, 2, 5)
        self.sbIdx0 = QSpinBox()
        self.sbIdx0.setRange(0, 10000)
        self.sbIdx1 = QSpinBox()
        self.sbIdx1.setRange(0, 10000)
        self.sbIdx1.setValue(2700)
        self.sbMinMm = QSpinBox()
        self.sbMinMm.setRange(0, 50000)
        self.sbMinMm.setValue(10)
        self.sbMaxMm = QSpinBox()
        self.sbMaxMm.setRange(20, 50000)
        self.sbMaxMm.setValue(12000)
        g_r.addWidget(QLabel("索引起"), 2, 0)
        g_r.addWidget(self.sbIdx0, 2, 1)
        g_r.addWidget(QLabel("索引止"), 2, 2)
        g_r.addWidget(self.sbIdx1, 2, 3)
        g_r.addWidget(QLabel("距离(mm)"), 3, 0)
        g_r.addWidget(self.sbMinMm, 3, 1)
        g_r.addWidget(QLabel("～"), 3, 2)
        g_r.addWidget(self.sbMaxMm, 3, 3)
        self.sbCalibrationHeader = QSpinBox()
        self.sbCalibrationHeader.setRange(0, 64)
        self.sbCalibrationHeader.setValue(0)
        self.sbCalibrationHeader.setToolTip(
            "TCP 标定回包在首点之前的固定字节数。"
            "《盲区测试高分辨率桌面版本》为 0；若另一机型带 6 字节前缀可填 6。"
        )
        g_r.addWidget(QLabel("标定帧头(字节)"), 4, 0)
        g_r.addWidget(self.sbCalibrationHeader, 4, 1)
        hint_hdr = QLabel("0=与盲区桌面脚本一致")
        hint_hdr.setStyleSheet("color: gray;")
        g_r.addWidget(hint_hdr, 4, 2, 1, 4)
        root.addWidget(gb_radar)

        gb_tt = QGroupBox("Y100SC 转台")
        g_t = QGridLayout(gb_tt)
        self.leCom = QLineEdit("COM6")
        self.sbBaud = QSpinBox()
        self.sbBaud.setRange(1200, 921600)
        self.sbBaud.setValue(9600)
        self.cmbAxis = QComboBox()
        for a in ("X", "Y", "Z", "r", "t", "T"):
            self.cmbAxis.addItem(a)
        self.cmbDir = QComboBox()
        self.cmbDir.addItems(["+", "-"])
        self.sbPulses = QSpinBox()
        self.sbPulses.setRange(1, 10000)
        self.sbPulses.setValue(40)
        self.sbSettleMs = QSpinBox()
        self.sbSettleMs.setRange(0, 60000)
        self.sbSettleMs.setValue(300)
        g_t.addWidget(QLabel("串口"), 0, 0)
        g_t.addWidget(self.leCom, 0, 1)
        g_t.addWidget(QLabel("波特率"), 0, 2)
        g_t.addWidget(self.sbBaud, 0, 3)
        g_t.addWidget(QLabel("轴"), 0, 4)
        g_t.addWidget(self.cmbAxis, 0, 5)
        g_t.addWidget(QLabel("方向"), 1, 0)
        g_t.addWidget(self.cmbDir, 1, 1)
        g_t.addWidget(QLabel("每步脉冲"), 1, 2)
        g_t.addWidget(self.sbPulses, 1, 3)
        g_t.addWidget(QLabel("到位等待(ms)"), 1, 4)
        g_t.addWidget(self.sbSettleMs, 1, 5)
        root.addWidget(gb_tt)

        gb_run = QGroupBox(
            "连续测试（起始索引=step0；转台每步后按 起始索引±步数 取点云；可先探测填索引）"
        )
        vb_run = QVBoxLayout(gb_run)
        row_idx = QHBoxLayout()
        self.sbStartIndex = QSpinBox()
        self.sbStartIndex.setRange(0, 10000)
        self.sbStartIndex.setValue(1009)
        self.sbProbeRepeat = QSpinBox()
        self.sbProbeRepeat.setRange(1, 30)
        self.sbProbeRepeat.setValue(3)
        self.sbProbeRepeat.setToolTip(
            "连续采集此次数；每帧在日志中打印反射率最高的 10 个点，并取该帧最高反射率 index；"
            "多帧 index 取中位数填入起始索引"
        )
        self.btnProbeMaxReflect = QPushButton("探测最高反射索引")
        self.btnProbeMaxReflect.setEnabled(False)
        self.lblProbeResult = QLabel("探测：未执行")
        self.lblProbeResult.setMinimumWidth(200)
        row_idx.addWidget(QLabel("起始索引"))
        row_idx.addWidget(self.sbStartIndex)
        row_idx.addWidget(QLabel("探测次数"))
        row_idx.addWidget(self.sbProbeRepeat)
        row_idx.addWidget(self.btnProbeMaxReflect)
        row_idx.addWidget(self.lblProbeResult, stretch=1)
        vb_run.addLayout(row_idx)

        row_run = QHBoxLayout()
        self.sbSteps = QSpinBox()
        self.sbSteps.setRange(1, 2000)
        self.sbSteps.setValue(50)
        self.btnStart = QPushButton("开始连续测试")
        self.btnStop = QPushButton("停止")
        self.btnStop.setEnabled(False)
        self.btnExport = QPushButton("导出表格")
        self.btnExportAllIndices = QPushButton("导出索引窗全点云")
        self.btnExportAllIndices.setToolTip(
            "立即采一帧，把当前「索引起～索引止」内全部 index 的 angle/距离/前沿/后沿/反射率导出为 Excel/CSV，便于核对"
        )
        self.btnExportAllIndices.setEnabled(False)
        row_run.addWidget(QLabel("步数 N"))
        row_run.addWidget(self.sbSteps)
        row_run.addWidget(self.btnStart)
        row_run.addWidget(self.btnStop)
        row_run.addWidget(self.btnExport)
        row_run.addWidget(self.btnExportAllIndices)
        row_run.addStretch()
        vb_run.addLayout(row_run)

        self.lblStats = QLabel("统计：—")
        self.lblStats.setTextInteractionFlags(
            self.lblStats.textInteractionFlags() | Qt.TextInteractionFlag.TextSelectableByMouse
        )
        vb_run.addWidget(self.lblStats)
        root.addWidget(gb_run)

        self.table = QTableWidget(0, len(TABLE_HEADERS))
        self.table.setHorizontalHeaderLabels(TABLE_HEADERS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.table, stretch=1)

        self.teLog = QPlainTextEdit()
        self.teLog.setReadOnly(True)
        self.teLog.setMinimumHeight(160)
        root.addWidget(self.teLog)

        self.btnRadarConnect.clicked.connect(self._on_connect)
        self.btnRadarDisconnect.clicked.connect(self._on_disconnect)
        self.btnProbeMaxReflect.clicked.connect(self._on_probe_max_reflect)
        self.btnStart.clicked.connect(self._on_start)
        self.btnStop.clicked.connect(self._on_stop)
        self.btnExport.clicked.connect(self._on_export)
        self.btnExportAllIndices.clicked.connect(self._on_export_all_indices)
        self.sbIdx0.valueChanged.connect(self._sync_start_index_bounds)
        self.sbIdx1.valueChanged.connect(self._sync_start_index_bounds)

        self._radar: H1CalibrationRadar | None = None
        self.statusBar().showMessage("就绪")
        self._sync_start_index_bounds()

    def closeEvent(self, event: Any) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.stop()
            self._worker.wait(4000)
        if self._probe_thread and self._probe_thread.isRunning():
            self._probe_thread.wait(3000)
        if self._radar:
            self._radar.close()
            self._radar = None
        event.accept()

    def _log(self, s: str) -> None:
        self.teLog.appendPlainText(time.strftime("[%H:%M:%S] ") + s)
        cur = self.teLog.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        self.teLog.setTextCursor(cur)
        self.teLog.ensureCursorVisible()

    def _refresh_run_buttons(self) -> None:
        w_busy = self._worker is not None and self._worker.isRunning()
        p_busy = self._probe_thread is not None and self._probe_thread.isRunning()
        self.btnStart.setEnabled(not w_busy and not p_busy)
        self.btnProbeMaxReflect.setEnabled(self._radar is not None and not w_busy and not p_busy)
        self.btnExportAllIndices.setEnabled(self._radar is not None and not w_busy and not p_busy)
        self.sbProbeRepeat.setEnabled(not p_busy)
        self.sbStartIndex.setEnabled(not w_busy and not p_busy)

    def _set_busy(self, busy: bool) -> None:
        self.btnExport.setEnabled(not busy)
        self.btnStop.setEnabled(busy)
        self.btnRadarConnect.setEnabled(not busy)
        self._refresh_run_buttons()

    def _validate_range(self) -> bool:
        if self.sbIdx0.value() > self.sbIdx1.value():
            QMessageBox.warning(self, "参数", "索引起不能大于索引止")
            return False
        if self.sbMinMm.value() >= self.sbMaxMm.value():
            QMessageBox.warning(self, "参数", "最小距离须小于最大距离")
            return False
        return True

    @Slot()
    def _on_connect(self) -> None:
        if self._radar:
            self._log("雷达已连接")
            return
        try:
            port = int(self.leRadarPort.text().strip())
        except ValueError:
            QMessageBox.warning(self, "参数", "端口须为整数")
            return
        self._radar = H1CalibrationRadar(host=self.leRadarIp.text().strip(), port=port)
        self._radar.configure_scan_parameters(angular_resolution_deg=float(self.dsbAngularRes.value()))
        self._radar.calibration_header_size = int(self.sbCalibrationHeader.value())
        if not self._radar.connect_radar():
            err = self._radar.last_error or "未知错误"
            self._radar = None
            QMessageBox.warning(self, "连接失败", err)
            self.lblRadarState.setText("雷达：未连接")
            return
        self.lblRadarState.setText(f"雷达：已连接 {self.leRadarIp.text()}:{port}")
        self._log("雷达 TCP 已连接；开始连续测试时将复用本连接（单客户端）。")
        self._refresh_run_buttons()

    @Slot()
    def _on_disconnect(self) -> None:
        if self._radar:
            self._radar.close()
            self._radar = None
        self.lblRadarState.setText("雷达：未连接")
        self._log("雷达已断开。")
        if not (self._worker and self._worker.isRunning()):
            self._set_busy(False)
        self._refresh_run_buttons()

    def _sync_start_index_bounds(self) -> None:
        lo = int(self.sbIdx0.value())
        hi = int(self.sbIdx1.value())
        self.sbStartIndex.setMinimum(lo)
        self.sbStartIndex.setMaximum(hi)
        if self.sbStartIndex.value() < lo:
            self.sbStartIndex.setValue(lo)
        if self.sbStartIndex.value() > hi:
            self.sbStartIndex.setValue(hi)

    def _gather_config(self) -> NewpreGuiConfig | None:
        if not self._validate_range():
            return None
        try:
            rport = int(self.leRadarPort.text().strip())
        except ValueError:
            QMessageBox.warning(self, "参数", "端口须为整数")
            return None
        axis: Axis = self.cmbAxis.currentText()  # type: ignore[assignment]
        direction: Sign = self.cmbDir.currentText()  # type: ignore[assignment]
        self._sync_start_index_bounds()
        return NewpreGuiConfig(
            radar_ip=self.leRadarIp.text().strip(),
            radar_port=rport,
            index_start=int(self.sbIdx0.value()),
            index_end=int(self.sbIdx1.value()),
            min_distance_mm=int(self.sbMinMm.value()),
            max_distance_mm=int(self.sbMaxMm.value()),
            angular_res_deg=float(self.dsbAngularRes.value()),
            step_angle_deg=float(self.dsbStepAngle.value()),
            start_index=int(self.sbStartIndex.value()),
            com=self.leCom.text().strip(),
            baud=int(self.sbBaud.value()),
            axis=axis,
            direction=direction,
            pulses_per_step=int(self.sbPulses.value()),
            settle_ms=int(self.sbSettleMs.value()),
            steps=int(self.sbSteps.value()),
            calibration_header_size=int(self.sbCalibrationHeader.value()),
        )

    def _radar_matches_ui(self, cfg: NewpreGuiConfig) -> bool:
        r = self._radar
        if r is None or r.socket is None:
            return False
        return r.host == cfg.radar_ip and int(r.port) == int(cfg.radar_port)

    @Slot()
    def _on_probe_max_reflect(self) -> None:
        if not self._radar:
            QMessageBox.warning(self, "探测", "请先连接雷达。")
            return
        if not self._validate_range():
            return
        self._sync_start_index_bounds()
        if self._probe_thread and self._probe_thread.isRunning():
            return
        self._radar.configure_scan_parameters(angular_resolution_deg=float(self.dsbAngularRes.value()))
        self._radar.calibration_header_size = int(self.sbCalibrationHeader.value())
        self._probe_thread = ProbeReflectThread(
            self._radar,
            int(self.sbIdx0.value()),
            int(self.sbIdx1.value()),
            int(self.sbMaxMm.value()),
            int(self.sbMinMm.value()),
            int(self.sbProbeRepeat.value()),
            float(self.dsbAngularRes.value()),
            self,
        )
        self._probe_thread.detail_log.connect(self._log)
        self._probe_thread.finished_ok.connect(self._on_probe_finished_ok)
        self._probe_thread.failed.connect(self._on_probe_failed)
        self._probe_thread.finished.connect(self._on_probe_thread_finished)
        self._refresh_run_buttons()
        self._log(
            f"开始探测最高反射索引：次数={self.sbProbeRepeat.value()}，"
            f"距离窗 ({self.sbMinMm.value()},{self.sbMaxMm.value()}) mm"
        )
        self._probe_thread.start()

    @Slot(int, object)
    def _on_probe_finished_ok(self, median_idx: int, indices: object) -> None:
        assert isinstance(indices, list)
        self.sbStartIndex.setValue(median_idx)
        self.lblProbeResult.setText(f"中位数索引={median_idx}  各次={indices}")
        self._log(f"探测完成：中位数索引={median_idx}，各次索引={indices}")

    @Slot(str)
    def _on_probe_failed(self, msg: str) -> None:
        QMessageBox.warning(self, "探测最高反射", msg)
        self._log(f"探测失败：{msg}")

    @Slot()
    def _on_probe_thread_finished(self) -> None:
        self._probe_thread = None
        self._refresh_run_buttons()

    @Slot()
    def _on_start(self) -> None:
        cfg = self._gather_config()
        if not cfg:
            return
        external: H1CalibrationRadar | None = None
        if self._radar_matches_ui(cfg):
            external = self._radar
        elif self._radar is not None:
            # 已连接但与当前 IP/端口不一致：先断开，由线程内新建连接
            self._radar.close()
            self._radar = None
            self.lblRadarState.setText("雷达：未连接")
            self._log("已断开旧连接（与当前 IP/端口不一致），测试中将新建连接。")

        self.table.setRowCount(0)
        self._session_rows.clear()
        self._set_busy(True)
        self._worker = NewpreSequenceThread(cfg, external, self)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._on_worker_thread_finished)
        self._worker.start()
        self._log(
            f"开始连续测试：起始索引={cfg.start_index}，步数 N={cfg.steps}，方向={cfg.direction}，"
            f"每步理论角={cfg.step_angle_deg}°"
        )

    def _append_row(self, row: dict[str, Any]) -> None:
        i = self.table.rowCount()
        self.table.insertRow(i)
        self.table.setItem(i, 0, QTableWidgetItem(str(row["step"])))
        self.table.setItem(i, 1, QTableWidgetItem(str(row["rotate_label"])))
        self.table.setItem(i, 2, QTableWidgetItem(str(row["target_index"])))
        self.table.setItem(i, 3, QTableWidgetItem(str(row["delta_index"])))
        self.table.setItem(i, 4, QTableWidgetItem(f"{row['measured_angle_deg']:.4f}"))
        self.table.setItem(i, 5, QTableWidgetItem(f"{row['theory_angle_deg']:+.4f}"))
        self.table.setItem(i, 6, QTableWidgetItem(f"{row['error_deg']:+.4f}"))
        self.table.setItem(i, 7, QTableWidgetItem(f"{row['distance_m']:.3f}"))
        self.table.setItem(i, 8, QTableWidgetItem(str(row["intensity"])))
        self.table.setItem(i, 9, QTableWidgetItem(row.get("anomalies") or ""))

    @Slot(int, int, object)
    def _on_progress(self, cur: int, tot: int, row: object) -> None:
        assert isinstance(row, dict)
        self._session_rows.append(row)
        self._append_row(row)
        self._log(
            f"step {row['step']}/{tot}: idx={row['target_index']} Δ={row['delta_index']} "
            f"err={row['error_deg']:+.4f}°"
        )

    @Slot(object)
    def _on_finished(self, rows: object) -> None:
        self._set_busy(False)
        assert isinstance(rows, list)
        self._session_rows = list(rows)
        summary = summarize(self._session_rows)
        self.lblStats.setText("统计：" + summary)
        self._log(summary)
        self._log("测试结束。")
        self.statusBar().showMessage("完成")

    @Slot(str)
    def _on_failed(self, msg: str) -> None:
        self._set_busy(False)
        QMessageBox.warning(self, "连续测试", msg)
        self._log("失败：" + msg)
        if self._session_rows:
            self.lblStats.setText("统计（中断）：" + summarize(self._session_rows))

    @Slot()
    def _on_worker_thread_finished(self) -> None:
        """线程结束时清空引用；若未收到 finished_ok/failed（异常路径），此处恢复按钮。"""
        self._worker = None
        if not self.btnStart.isEnabled():
            self._set_busy(False)
        self._refresh_run_buttons()

    @Slot()
    def _on_stop(self) -> None:
        if self._worker:
            self._worker.stop()
            self._log("已请求停止…")

    @Slot()
    def _on_export_all_indices(self) -> None:
        if not self._radar:
            QMessageBox.warning(self, "导出", "请先连接雷达。")
            return
        if not self._validate_range():
            return
        path, _filt = QFileDialog.getSaveFileName(
            self,
            "导出索引窗内全部点",
            str(Path.home() / f"索引窗点云_{time.strftime('%Y%m%d_%H%M%S')}"),
            "Excel (*.xlsx);;CSV (*.csv)",
        )
        if not path:
            return
        self._radar.configure_scan_parameters(angular_resolution_deg=float(self.dsbAngularRes.value()))
        self._radar.calibration_header_size = int(self.sbCalibrationHeader.value())
        m = self._radar.optimized_single_measurement(
            int(self.sbIdx0.value()),
            int(self.sbIdx1.value()),
            int(self.sbMaxMm.value()),
        )
        if not m:
            QMessageBox.warning(self, "导出", self._radar.last_error or "采集失败")
            return
        all_rows = m.get("all_results") or []
        if not all_rows:
            QMessageBox.information(self, "导出", "无点云数据")
            return
        try:
            export_all_index_points(all_rows, Path(path))
        except SystemExit as e:
            QMessageBox.warning(self, "导出", str(e))
            return
        except OSError as e:
            QMessageBox.warning(self, "导出", str(e))
            return
        self._log(
            f"已导出索引窗全点云：索引起 {self.sbIdx0.value()}～索引止 {self.sbIdx1.value()}，"
            f"共 {len(all_rows)} 点 → {path}"
        )

    @Slot()
    def _on_export(self) -> None:
        if not self._session_rows:
            QMessageBox.information(self, "导出", "暂无数据")
            return
        path, filt = QFileDialog.getSaveFileName(
            self,
            "导出",
            str(Path.home() / f"newpre_gui_{time.strftime('%Y%m%d_%H%M%S')}"),
            "Excel (*.xlsx);;CSV (*.csv)",
        )
        if not path:
            return
        summary = summarize(self._session_rows)
        try:
            export_rows(self._session_rows, Path(path), summary)
        except SystemExit as e:
            QMessageBox.warning(self, "导出", str(e))
            return
        except OSError as e:
            QMessageBox.warning(self, "导出", str(e))
            return
        self._log(f"已导出：{path}")


def main() -> int:
    app = QApplication(sys.argv)
    w = NewpreResolutionMainWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
