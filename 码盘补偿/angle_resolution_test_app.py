"""
H1 角分辨率 + Y100SC 转台步进验证（依据 pre.md）。
运行：在「码盘补偿」目录下执行
  python angle_resolution_test_app.py
依赖：PySide6、pyserial
"""
from __future__ import annotations

import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QFile, QIODevice, QThread, Signal, Slot
from PySide6.QtGui import QTextCursor
from PySide6.QtUiTools import QUiLoader
from PySide6.QtWidgets import (
    QApplication,
    QHeaderView,
    QMainWindow,
    QMessageBox,
    QTableWidgetItem,
    QWidget,
)

from h1_radar_reader import H1CalibrationRadar
from y100sc_client import Axis, Sign, Y100SCClient, Y100SCError, Y100SCSerialConfig


def _find_longest_distance_run(
    all_results: list[dict[str, Any]], min_mm: int, max_mm: int
) -> tuple[list[dict[str, Any]] | None, str | None]:
    if not all_results:
        return None, "无点云数据"
    best: list[dict[str, Any]] = []
    cur: list[dict[str, Any]] = []
    for p in all_results:
        d = int(p["measured_distance"])
        ok = min_mm < d < max_mm
        if ok:
            if not cur:
                cur = [p]
            elif int(p["index"]) == int(cur[-1]["index"]) + 1:
                cur.append(p)
            else:
                if len(cur) > len(best):
                    best = list(cur)
                cur = [p]
        else:
            if len(cur) > len(best):
                best = list(cur)
            cur = []
    if len(cur) > len(best):
        best = cur
    if not best:
        return None, f"在距离 ({min_mm},{max_mm}) mm 内未找到连续目标段"
    return best, None


def _snapshot_from_run(run: list[dict[str, Any]]) -> dict[str, Any]:
    mid = run[len(run) // 2]
    i0 = int(run[0]["index"])
    i1 = int(run[-1]["index"])
    return {
        "center_index": int(mid["index"]),
        "center_r_mm": int(mid["measured_distance"]),
        "index_start": i0,
        "index_end": i1,
        "run_len": len(run),
    }


def _fmt_snap(s: dict[str, Any] | None) -> str:
    if not s:
        return "—"
    return (
        f"中心 index={s['center_index']}, r={s['center_r_mm']} mm, "
        f"段 [{s['index_start']}…{s['index_end']}] 共 {s['run_len']} 点"
    )


@dataclass
class TurntableJob:
    port: str
    baud: int
    axis: Axis
    direction: Sign
    distance: int


class TurntableThread(QThread):
    finished_ok = Signal()
    failed = Signal(str)

    def __init__(self, job: TurntableJob, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._job = job

    def run(self) -> None:
        cfg = Y100SCSerialConfig(port=self._job.port, baudrate=self._job.baud, timeout_s=0.5)
        try:
            with Y100SCClient(cfg) as dev:
                dev.handshake()
                dev.move(self._job.axis, self._job.direction, self._job.distance, wait_timeout_s=180.0)
        except (Y100SCError, OSError, ValueError) as e:
            self.failed.emit(str(e))
            return
        self.finished_ok.emit()


class RadarMeasureThread(QThread):
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        radar: H1CalibrationRadar,
        start_index: int,
        end_index: int,
        max_distance: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._radar = radar
        self._start_index = start_index
        self._end_index = end_index
        self._max_distance = max_distance

    def run(self) -> None:
        try:
            snap = self._radar.optimized_single_measurement(
                self._start_index, self._end_index, self._max_distance
            )
        except OSError as e:
            self.failed.emit(str(e))
            return
        if not snap:
            self.failed.emit(self._radar.last_error or "取数失败（无数据或发送失败）")
            return
        self.finished_ok.emit(snap)


class RepeatCycleThread(QThread):
    progress = Signal(int, int, object)

    finished_ok = Signal()
    failed = Signal(str)

    def __init__(
        self,
        radar: H1CalibrationRadar,
        start_index: int,
        end_index: int,
        max_distance: int,
        min_distance: int,
        turntable_job_factory: Any,
        settle_s: float,
        reverse_after: bool,
        theory_deg: float,
        angular_res_deg: float,
        total: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._radar = radar
        self._start_index = start_index
        self._end_index = end_index
        self._max_distance = max_distance
        self._min_distance = min_distance
        self._job_factory = turntable_job_factory
        self._settle_s = settle_s
        self._reverse = reverse_after
        self._theory_deg = theory_deg
        self._angular_res_deg = angular_res_deg
        self._total = total
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def _measure(self) -> dict[str, Any] | None:
        return self._radar.optimized_single_measurement(
            self._start_index, self._end_index, self._max_distance
        )

    def _move_tt(self, job: TurntableJob) -> str | None:
        cfg = Y100SCSerialConfig(port=job.port, baudrate=job.baud, timeout_s=0.5)
        try:
            with Y100SCClient(cfg) as dev:
                dev.handshake()
                dev.move(job.axis, job.direction, job.distance, wait_timeout_s=180.0)
        except (Y100SCError, OSError, ValueError) as e:
            return str(e)
        return None

    def run(self) -> None:
        self._radar.configure_scan_parameters(angular_resolution_deg=self._angular_res_deg)
        for k in range(self._total):
            if self._stop:
                break
            m0 = self._measure()
            if not m0:
                self.failed.emit(self._radar.last_error or f"第{k+1}轮：初始采集失败")
                return
            run0, err0 = _find_longest_distance_run(
                m0.get("all_results") or [], self._min_distance, self._max_distance
            )
            if err0 or not run0:
                self.failed.emit(f"第{k+1}轮：{err0 or '目标提取失败'}")
                return
            s0 = _snapshot_from_run(run0)

            job = self._job_factory()
            err_m = self._move_tt(job)
            if err_m:
                self.failed.emit(f"第{k+1}轮转台：{err_m}")
                return
            time.sleep(self._settle_s)

            m1 = self._measure()
            if not m1:
                self.failed.emit(self._radar.last_error or f"第{k+1}轮：旋转后采集失败")
                return
            run1, err1 = _find_longest_distance_run(
                m1.get("all_results") or [], self._min_distance, self._max_distance
            )
            if err1 or not run1:
                self.failed.emit(f"第{k+1}轮（旋转后）：{err1 or '目标提取失败'}")
                return
            s1 = _snapshot_from_run(run1)

            d_idx = int(s1["center_index"] - s0["center_index"])
            d_theta = d_idx * self._angular_res_deg
            err_deg = d_theta - self._theory_deg

            if self._reverse:
                rev: Sign = "-" if job.direction == "+" else "+"
                rev_job = TurntableJob(job.port, job.baud, job.axis, rev, job.distance)
                err_r = self._move_tt(rev_job)
                if err_r:
                    self.failed.emit(f"第{k+1}轮回退：{err_r}")
                    return
                time.sleep(self._settle_s)

            self.progress.emit(
                k + 1,
                self._total,
                {
                    "round": k + 1,
                    "idx0": s0["center_index"],
                    "idx1": s1["center_index"],
                    "d_idx": d_idx,
                    "err_deg": err_deg,
                },
            )
        self.finished_ok.emit()


class FullCycleThread(QThread):
    finished_ok = Signal()
    failed = Signal(str)

    def __init__(self, fn: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            self._fn()
        except (RuntimeError, Y100SCError, OSError, ValueError) as e:
            self.failed.emit(str(e))
            return
        self.finished_ok.emit()


class AngleResolutionMainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self._radar: H1CalibrationRadar | None = None
        self._snap_before: dict[str, Any] | None = None
        self._snap_after: dict[str, Any] | None = None
        self._repeat_thread: RepeatCycleThread | None = None
        self._error_history: list[float] = []

        ui_path = Path(__file__).resolve().parent / "ui" / "angle_resolution_main.ui"
        loader = QUiLoader()
        f = QFile(str(ui_path))
        if not f.open(QIODevice.ReadOnly):
            raise RuntimeError(f"无法打开 UI 文件: {ui_path}")
        form = loader.load(f, self)
        f.close()
        if form is None:
            raise RuntimeError(loader.errorString())
        self.setCentralWidget(form)
        self.setWindowTitle(form.windowTitle())
        self.statusBar().showMessage("就绪")
        self._bind_widgets(form)
        self._wire()

    def _bind_widgets(self, root: QWidget) -> None:
        g = lambda name: root.findChild(QWidget, name)
        self.leRadarIp = g("leRadarIp")
        self.leRadarPort = g("leRadarPort")
        self.btnRadarConnect = g("btnRadarConnect")
        self.btnRadarDisconnect = g("btnRadarDisconnect")
        self.lblRadarState = g("lblRadarState")
        self.dsbAngularResDeg = g("dsbAngularResDeg")
        self.sbIndexStart = g("sbIndexStart")
        self.sbIndexEnd = g("sbIndexEnd")
        self.sbMaxDistanceMm = g("sbMaxDistanceMm")
        self.sbMinDistanceMm = g("sbMinDistanceMm")
        self.leComPort = g("leComPort")
        self.sbBaud = g("sbBaud")
        self.cmbAxis = g("cmbAxis")
        self.sbPulsePerStep = g("sbPulsePerStep")
        self.cmbDir = g("cmbDir")
        self.sbSettleMs = g("sbSettleMs")
        self.dsbTheoryDeg = g("dsbTheoryDeg")
        self.lblTurntableState = g("lblTurntableState")
        self.btnCaptureBefore = g("btnCaptureBefore")
        self.btnTurntableMove = g("btnTurntableMove")
        self.btnCaptureAfter = g("btnCaptureAfter")
        self.btnCompute = g("btnCompute")
        self.btnRunFullCycle = g("btnRunFullCycle")
        self.lblCapBefore = g("lblCapBefore")
        self.lblCapAfter = g("lblCapAfter")
        self.lblDelta = g("lblDelta")
        self.sbRepeatCount = g("sbRepeatCount")
        self.btnRepeatStart = g("btnRepeatStart")
        self.btnRepeatStop = g("btnRepeatStop")
        self.lblRepeatStats = g("lblRepeatStats")
        self.teLog = g("teLog")
        self.tableHistory = g("tableHistory")
        self.tableHistory.setHorizontalHeaderLabels(["轮次", "index₁", "index₂", "Δindex", "误差(°)"])
        self.tableHistory.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.teLog.setMinimumHeight(180)
        self.teLog.setLineWrapMode(self.teLog.LineWrapMode.WidgetWidth)

    def _wire(self) -> None:
        self.btnRadarConnect.clicked.connect(self._on_radar_connect)
        self.btnRadarDisconnect.clicked.connect(self._on_radar_disconnect)
        self.btnCaptureBefore.clicked.connect(self._on_capture_before)
        self.btnTurntableMove.clicked.connect(self._on_turntable_move)
        self.btnCaptureAfter.clicked.connect(self._on_capture_after)
        self.btnCompute.clicked.connect(self._on_compute)
        self.btnRunFullCycle.clicked.connect(self._on_full_cycle)
        self.btnRepeatStart.clicked.connect(self._on_repeat_start)
        self.btnRepeatStop.clicked.connect(self._on_repeat_stop)

    def closeEvent(self, event: Any) -> None:
        if self._repeat_thread and self._repeat_thread.isRunning():
            self._repeat_thread.stop()
            self._repeat_thread.wait(3000)
        if self._radar:
            self._radar.close()
            self._radar = None
        event.accept()

    def _log(self, msg: str) -> None:
        self.teLog.appendPlainText(time.strftime("[%H:%M:%S] ") + msg)
        cur = self.teLog.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        self.teLog.setTextCursor(cur)
        self.teLog.ensureCursorVisible()
        bar = self.teLog.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _set_busy(self, busy: bool) -> None:
        for b in (
            self.btnCaptureBefore,
            self.btnTurntableMove,
            self.btnCaptureAfter,
            self.btnCompute,
            self.btnRunFullCycle,
            self.btnRadarConnect,
            self.btnRepeatStart,
        ):
            b.setEnabled(not busy)
        self.btnRepeatStop.setEnabled(busy and self._repeat_thread is not None)

    def _sync_radar_geometry(self) -> None:
        if self._radar:
            self._radar.configure_scan_parameters(angular_resolution_deg=float(self.dsbAngularResDeg.value()))

    def _on_radar_connect(self) -> None:
        if self._radar:
            self._log("雷达已连接，请先断开再重连。")
            return
        try:
            port = int(self.leRadarPort.text().strip())
        except ValueError:
            QMessageBox.warning(self, "参数错误", "端口必须是整数")
            return
        self._radar = H1CalibrationRadar(host=self.leRadarIp.text().strip(), port=port)
        self._sync_radar_geometry()
        if not self._radar.connect_radar():
            err = self._radar.last_error or "未知错误"
            self._radar = None
            QMessageBox.warning(self, "连接失败", err)
            self.lblRadarState.setText("雷达：未连接")
            return
        self.lblRadarState.setText(f"雷达：已连接 {self.leRadarIp.text()}:{port}")
        self._log("雷达 TCP 已连接。")

    def _on_radar_disconnect(self) -> None:
        if self._radar:
            self._radar.close()
            self._radar = None
        self.lblRadarState.setText("雷达：未连接")
        self._log("雷达已断开。")

    def _measure_params(self) -> tuple[int, int, int, int] | None:
        a = int(self.sbIndexStart.value())
        b = int(self.sbIndexEnd.value())
        if a > b:
            QMessageBox.warning(self, "参数错误", "索引起不能大于索引止")
            return None
        max_mm = int(self.sbMaxDistanceMm.value())
        min_mm = int(self.sbMinDistanceMm.value())
        if min_mm >= max_mm:
            QMessageBox.warning(self, "参数错误", "最小距离须小于最大距离（判定区间为开区间）")
            return None
        return a, b, max_mm, min_mm

    def _apply_snap(self, measurement: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        all_rows = measurement.get("all_results") or []
        max_mm = int(self.sbMaxDistanceMm.value())
        min_mm = int(self.sbMinDistanceMm.value())
        run, err = _find_longest_distance_run(all_rows, min_mm, max_mm)
        if err or not run:
            return None, err or "无有效目标段"
        return _snapshot_from_run(run), None

    def _on_capture_before(self) -> None:
        p = self._measure_params()
        if not p:
            return
        if not self._radar:
            QMessageBox.warning(self, "未连接", "请先连接雷达")
            return
        self._set_busy(True)
        self._sync_radar_geometry()
        self._radar_th = RadarMeasureThread(self._radar, p[0], p[1], p[2], self)
        self._radar_th.finished_ok.connect(self._on_cap_before_ok)
        self._radar_th.failed.connect(self._on_measure_fail)
        self._radar_th.finished.connect(self._thread_done)
        self._radar_th.start()

    @Slot(object)
    def _on_cap_before_ok(self, m: object) -> None:
        snap, err = self._apply_snap(m)
        if err:
            self._snap_before = None
            self.lblCapBefore.setText("—")
            QMessageBox.warning(self, "采集", err)
            self._log(f"初始采集失败：{err}")
            return
        self._snap_before = snap
        self.lblCapBefore.setText(_fmt_snap(snap))
        self._log(_fmt_snap(snap))

    def _on_capture_after(self) -> None:
        p = self._measure_params()
        if not p:
            return
        if not self._radar:
            QMessageBox.warning(self, "未连接", "请先连接雷达")
            return
        self._set_busy(True)
        self._sync_radar_geometry()
        self._radar_th = RadarMeasureThread(self._radar, p[0], p[1], p[2], self)
        self._radar_th.finished_ok.connect(self._on_cap_after_ok)
        self._radar_th.failed.connect(self._on_measure_fail)
        self._radar_th.finished.connect(self._thread_done)
        self._radar_th.start()

    @Slot(object)
    def _on_cap_after_ok(self, m: object) -> None:
        snap, err = self._apply_snap(m)
        if err:
            self._snap_after = None
            self.lblCapAfter.setText("—")
            QMessageBox.warning(self, "采集", err)
            self._log(f"旋转后采集失败：{err}")
            return
        self._snap_after = snap
        self.lblCapAfter.setText(_fmt_snap(snap))
        self._log(_fmt_snap(snap))

    @Slot(str)
    def _on_measure_fail(self, msg: str) -> None:
        QMessageBox.warning(self, "雷达", msg)
        self._log(f"雷达线程错误：{msg}")

    @Slot()
    def _thread_done(self) -> None:
        self._set_busy(False)

    def _turntable_job(self) -> TurntableJob:
        axis: Axis = self.cmbAxis.currentText()  # type: ignore[assignment]
        direction: Sign = self.cmbDir.currentText()  # type: ignore[assignment]
        return TurntableJob(
            port=self.leComPort.text().strip(),
            baud=int(self.sbBaud.value()),
            axis=axis,
            direction=direction,
            distance=int(self.sbPulsePerStep.value()),
        )

    def _on_turntable_move(self) -> None:
        job = self._turntable_job()
        self._set_busy(True)
        self.lblTurntableState.setText("转台：运动中…")
        self._tt_th = TurntableThread(job, self)
        self._tt_th.finished_ok.connect(self._on_tt_ok)
        self._tt_th.failed.connect(self._on_tt_fail)
        self._tt_th.finished.connect(self._thread_done)
        self._tt_th.start()

    @Slot()
    def _on_tt_ok(self) -> None:
        self.lblTurntableState.setText("转台：本步完成")
        self._log(
            f"转台步进：轴={self.cmbAxis.currentText()} 方向={self.cmbDir.currentText()} "
            f"脉冲={self.sbPulsePerStep.value()}"
        )

    @Slot(str)
    def _on_tt_fail(self, msg: str) -> None:
        self.lblTurntableState.setText("转台：错误")
        QMessageBox.warning(self, "转台", msg)
        self._log(f"转台错误：{msg}")

    def _on_compute(self) -> None:
        if not self._snap_before or not self._snap_after:
            QMessageBox.information(self, "计算", "请先完成两次采集并得到有效目标段。")
            return
        i0 = int(self._snap_before["center_index"])
        i1 = int(self._snap_after["center_index"])
        d_idx = i1 - i0
        res_deg = float(self.dsbAngularResDeg.value())
        d_theta = d_idx * res_deg
        theory = float(self.dsbTheoryDeg.value())
        err = d_theta - theory
        self.lblDelta.setText(f"Δindex={d_idx}, Δθ={d_theta:.4f}°, 误差(Δθ−理论)={err:+.4f}°")
        self._log(self.lblDelta.text())

    def _on_full_cycle(self) -> None:
        p = self._measure_params()
        if not p:
            return
        if not self._radar:
            QMessageBox.warning(self, "未连接", "请先连接雷达")
            return

        radar = self._radar
        settle_s = float(self.sbSettleMs.value()) / 1000.0
        ang_deg = float(self.dsbAngularResDeg.value())
        job = self._turntable_job()
        min_mm = p[3]

        def run_seq() -> None:
            radar.configure_scan_parameters(angular_resolution_deg=ang_deg)
            m0 = radar.optimized_single_measurement(p[0], p[1], p[2])
            if not m0:
                raise RuntimeError(radar.last_error or "初始采集失败")
            run0, e0 = _find_longest_distance_run(m0.get("all_results") or [], min_mm, p[2])
            if e0 or not run0:
                raise RuntimeError(e0 or "初始目标提取失败")
            self._snap_before = _snapshot_from_run(run0)

            cfg = Y100SCSerialConfig(port=job.port, baudrate=job.baud, timeout_s=0.5)
            with Y100SCClient(cfg) as dev:
                dev.handshake()
                dev.move(job.axis, job.direction, job.distance, wait_timeout_s=180.0)
            time.sleep(settle_s)

            m1 = radar.optimized_single_measurement(p[0], p[1], p[2])
            if not m1:
                raise RuntimeError(radar.last_error or "旋转后采集失败")
            run1, e1 = _find_longest_distance_run(m1.get("all_results") or [], min_mm, p[2])
            if e1 or not run1:
                raise RuntimeError(e1 or "旋转后目标提取失败")
            self._snap_after = _snapshot_from_run(run1)

        self._set_busy(True)
        self._full_worker = FullCycleThread(run_seq, self)
        self._full_worker.finished_ok.connect(self._on_full_ok)
        self._full_worker.failed.connect(self._on_full_fail)
        self._full_worker.finished.connect(self._thread_done)
        self._full_worker.start()

    @Slot()
    def _on_full_ok(self) -> None:
        self.lblCapBefore.setText(_fmt_snap(self._snap_before))
        self.lblCapAfter.setText(_fmt_snap(self._snap_after))
        self._on_compute()
        self._log("一键单轮完成。")

    @Slot(str)
    def _on_full_fail(self, msg: str) -> None:
        QMessageBox.warning(self, "一键单轮", msg)
        self._log(f"一键单轮失败：{msg}")

    @Slot()
    def _on_repeat_start(self) -> None:
        p = self._measure_params()
        if not p:
            return
        if not self._radar:
            QMessageBox.warning(self, "未连接", "请先连接雷达")
            return
        self.tableHistory.setRowCount(0)
        self._error_history = []
        n = int(self.sbRepeatCount.value())
        self._set_busy(True)
        self._repeat_thread = RepeatCycleThread(
            self._radar,
            p[0],
            p[1],
            p[2],
            p[3],
            self._turntable_job,
            float(self.sbSettleMs.value()) / 1000.0,
            True,
            float(self.dsbTheoryDeg.value()),
            float(self.dsbAngularResDeg.value()),
            n,
            self,
        )
        self._repeat_thread.progress.connect(self._on_repeat_progress)
        self._repeat_thread.finished_ok.connect(self._on_repeat_finished_ok)
        self._repeat_thread.failed.connect(self._on_repeat_failed)
        self._repeat_thread.start()
        self.btnRepeatStop.setEnabled(True)
        self._log(f"开始重复测试 n={n}（每轮结束自动反向一步回退）")

    @Slot(int, int, object)
    def _on_repeat_progress(self, cur: int, tot: int, row: object) -> None:
        assert isinstance(row, dict)
        self._error_history.append(float(row["err_deg"]))
        i = self.tableHistory.rowCount()
        self.tableHistory.insertRow(i)
        self.tableHistory.setItem(i, 0, QTableWidgetItem(str(row["round"])))
        self.tableHistory.setItem(i, 1, QTableWidgetItem(str(row["idx0"])))
        self.tableHistory.setItem(i, 2, QTableWidgetItem(str(row["idx1"])))
        self.tableHistory.setItem(i, 3, QTableWidgetItem(str(row["d_idx"])))
        self.tableHistory.setItem(i, 4, QTableWidgetItem(f"{row['err_deg']:+.4f}"))
        self._log(f"重复 {cur}/{tot}: Δindex={row['d_idx']}, 误差={row['err_deg']:+.4f}°")

    @Slot()
    def _on_repeat_finished_ok(self) -> None:
        self._repeat_thread = None
        self._set_busy(False)
        errs = self._error_history
        if errs:
            mu = statistics.mean(errs)
            mx = max(abs(e) for e in errs)
            sd = statistics.stdev(errs) if len(errs) > 1 else 0.0
            t = f"统计（n={len(errs)}）：平均误差 {mu:+.4f}°，最大绝对误差 {mx:.4f}°，标准差 {sd:.4f}°"
            self.lblRepeatStats.setText(t)
            self._log(t)
        self._log("重复测试结束。")

    @Slot(str)
    def _on_repeat_failed(self, msg: str) -> None:
        self._repeat_thread = None
        self._set_busy(False)
        QMessageBox.warning(self, "重复测试", msg)
        self._log(f"重复测试中止：{msg}")

    @Slot()
    def _on_repeat_stop(self) -> None:
        if self._repeat_thread:
            self._repeat_thread.stop()
            self._log("已请求停止重复测试…")


def main() -> int:
    app = QApplication(sys.argv)
    win = AngleResolutionMainWindow()
    win.resize(960, 760)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
