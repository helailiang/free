import sys
import socket
import time
import re
import ipaddress
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QLabel, QLineEdit, QPushButton, QTextEdit, QGroupBox,
                             QTabWidget, QTableWidget, QTableWidgetItem, QHeaderView,
                             QSpinBox, QDoubleSpinBox, QCheckBox, QProgressBar, QMessageBox)
from PyQt5.QtCore import Qt, QThread, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QFont, QPalette
import numpy as np


def console_log(message):
    text = str(message)
    stream = getattr(sys, "stdout", None)
    if stream is None:
        return

    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        stream.write(text + "\n")
    except UnicodeEncodeError:
        safe_text = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
        try:
            stream.write(safe_text + "\n")
        except Exception:
            return
    except Exception:
        return

    try:
        stream.flush()
    except Exception:
        pass

# 尝试导入pyqtgraph
try:
    import pyqtgraph as pg

    PG_AVAILABLE = True
except ImportError:
    console_log("[WARN] pyqtgraph not installed, visualization features will be limited.")
    console_log("[WARN] Please install: pip install pyqtgraph")
    PG_AVAILABLE = False


class MeasurementThread(QThread):
    """测量线程"""
    measurement_complete = pyqtSignal(object)
    measurement_progress = pyqtSignal(int, int, int, bool, float)
    measurement_error = pyqtSignal(str)

    def __init__(self, radar_processor, start_index, end_index, max_distance, iterations, radar_frequency_hz):
        super().__init__()
        self.radar_processor = radar_processor
        self.start_index = start_index
        self.end_index = end_index
        self.max_distance = max_distance
        self.iterations = iterations
        self.radar_frequency_hz = radar_frequency_hz
        self._is_running = True
        self.progress_emit_interval_sec = 0.10

    def get_measurement_period(self):
        if self.radar_frequency_hz <= 0:
            return 0.0
        return 1.0 / self.radar_frequency_hz

    def wait_for_next_cycle(self, scheduled_time):
        period = self.get_measurement_period()
        if period <= 0 or scheduled_time is None:
            return True

        while self._is_running:
            remaining = scheduled_time - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(remaining, 0.01))

        return False

    def run(self):
        try:
            if not self.radar_processor.ensure_connection():
                self.measurement_error.emit("连接雷达失败")
                return

            # 预热连接
            for _ in range(2):
                if not self._is_running:
                    return
                self.radar_processor.optimized_single_measurement(self.start_index, self.end_index, self.max_distance)

            success_count = 0
            qualified_circles = 0
            total_points_all = 0
            filtered_points_all = 0
            iteration_numbers = []
            filtered_counts = []
            latest_measurement = None
            measurement_period = self.get_measurement_period()
            next_capture_time = time.monotonic()
            last_progress_emit_time = 0.0
            start_time = time.time()

            for i in range(self.iterations):
                if not self._is_running:
                    break

                if measurement_period > 0 and not self.wait_for_next_cycle(next_capture_time):
                    break

                capture_started_monotonic = time.monotonic()
                if measurement_period > 0:
                    next_capture_time = capture_started_monotonic + measurement_period

                iteration_start = time.time()

                try:
                    result = self.radar_processor.optimized_single_measurement(
                        self.start_index, self.end_index, self.max_distance
                    )
                    iteration_time = time.time() - iteration_start

                    if result and result['total_count'] > 0:
                        latest_measurement = {
                            'iteration': i + 1,
                            'total_count': result['total_count'],
                            'filtered_count': result['filtered_count'],
                            'results': result['results'],
                            'has_consecutive_qualified': result['has_consecutive_qualified'],
                            'start_index': result['start_index'],
                            'end_index': result['end_index'],
                            'angular_resolution_deg': result['angular_resolution_deg'],
                            'scan_angle_range_deg': result['scan_angle_range_deg'],
                            'start_angle_deg': result['start_angle_deg'],
                            'expected_point_count': result['expected_point_count']
                        }
                        success_count += 1
                        total_points_all += result['total_count']
                        filtered_points_all += result['filtered_count']
                        iteration_numbers.append(i + 1)
                        filtered_counts.append(result['filtered_count'])

                        if result['has_consecutive_qualified']:
                            qualified_circles += 1

                        filtered_for_emit = result['filtered_count']
                        consecutive_for_emit = result['has_consecutive_qualified']
                    else:
                        filtered_for_emit = 0
                        consecutive_for_emit = False

                    now_monotonic = time.monotonic()
                    should_emit_progress = (
                        i == 0 or
                        i + 1 == self.iterations or
                        consecutive_for_emit or
                        (now_monotonic - last_progress_emit_time) >= self.progress_emit_interval_sec
                    )

                    if should_emit_progress:
                        self.measurement_progress.emit(
                            i + 1,
                            self.iterations,
                            filtered_for_emit,
                            consecutive_for_emit,
                            iteration_time * 1000
                        )
                        last_progress_emit_time = now_monotonic

                except Exception as e:
                    continue

            total_elapsed = time.time() - start_time
            self.measurement_complete.emit({
                'requested_iterations': self.iterations,
                'success_count': success_count,
                'qualified_circles': qualified_circles,
                'total_time': total_elapsed,
                'total_points_all': total_points_all,
                'filtered_points_all': filtered_points_all,
                'iteration_numbers': iteration_numbers,
                'filtered_counts': filtered_counts,
                'latest_measurement': latest_measurement,
            })

        except Exception as e:
            self.last_error = str(e)
            self.measurement_error.emit(str(e))

    def stop(self):
        self._is_running = False


class ConnectionThread(QThread):
    connection_complete = pyqtSignal(object)

    def __init__(self, host, port, connect_timeout=3.0):
        super().__init__()
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout

    def run(self):
        radar = RadarDataProcessor(
            host=self.host,
            port=self.port,
            connect_timeout=self.connect_timeout
        )

        if radar.connect_radar():
            self.connection_complete.emit({
                'success': True,
                'host': self.host,
                'port': self.port,
                'radar': radar,
                'error': ''
            })
            return

        error_message = radar.last_error or "连接失败"
        radar.close()
        self.connection_complete.emit({
            'success': False,
            'host': self.host,
            'port': self.port,
            'radar': None,
            'error': error_message
        })


class RadarVisualizationApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.radar = None
        self.connection_thread = None
        self.measurement_thread = None
        self.measurement_summary = None
        self.init_ui()

    def init_ui(self):
        self.setWindowTitle('雷达数据可视化测量系统')
        self.setGeometry(100, 100, 1400, 900)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        self.tab_widget = QTabWidget()
        main_layout.addWidget(self.tab_widget)

        self.create_connection_tab()
        self.create_control_tab()
        self.create_data_tab()
        self.create_visualization_tab()
        self.create_statistics_tab()

        self.status_bar = self.statusBar()
        self.status_bar.showMessage('就绪')

    def create_connection_tab(self):
        connection_tab = QWidget()
        layout = QVBoxLayout(connection_tab)

        settings_group = QGroupBox("雷达连接设置")
        settings_layout = QVBoxLayout()

        ip_layout = QHBoxLayout()
        ip_layout.addWidget(QLabel("雷达IP地址:"))
        self.ip_input = QLineEdit()
        self.ip_input.setText("192.168.0.240")
        ip_layout.addWidget(self.ip_input)
        settings_layout.addLayout(ip_layout)

        port_layout = QHBoxLayout()
        port_layout.addWidget(QLabel("端口:"))
        self.port_input = QLineEdit()
        self.port_input.setText("2111")
        port_layout.addWidget(self.port_input)
        settings_layout.addLayout(port_layout)

        settings_group.setLayout(settings_layout)
        layout.addWidget(settings_group)

        status_group = QGroupBox("连接状态")
        status_layout = QVBoxLayout()

        self.connection_status = QLabel("未连接")
        self.connection_status.setStyleSheet("color: red; font-weight: bold;")
        status_layout.addWidget(self.connection_status)

        self.connection_info = QLabel("")
        self.connection_info.setStyleSheet("color: gray;")
        status_layout.addWidget(self.connection_info)

        self.connect_btn = QPushButton("连接雷达")
        self.connect_btn.clicked.connect(self.connect_radar)
        status_layout.addWidget(self.connect_btn)

        self.disconnect_btn = QPushButton("断开连接")
        self.disconnect_btn.clicked.connect(self.disconnect_radar)
        self.disconnect_btn.setEnabled(False)
        status_layout.addWidget(self.disconnect_btn)

        status_group.setLayout(status_layout)
        layout.addWidget(status_group)

        # 连接历史记录
        history_group = QGroupBox("连接历史")
        history_layout = QVBoxLayout()

        self.history_text = QTextEdit()
        self.history_text.setReadOnly(True)
        self.history_text.setMaximumHeight(100)
        self.history_text.setFont(QFont("Arial", 9))
        history_layout.addWidget(self.history_text)

        history_group.setLayout(history_layout)
        layout.addWidget(history_group)

        layout.addStretch()
        self.tab_widget.addTab(connection_tab, "连接设置")

    def create_control_tab(self):
        control_tab = QWidget()
        layout = QVBoxLayout(control_tab)

        params_group = QGroupBox("测量参数设置")
        params_layout = QVBoxLayout()

        index_layout = QHBoxLayout()
        index_layout.addWidget(QLabel("索引范围:"))
        self.start_index_input = QSpinBox()
        self.start_index_input.setRange(0, 4000)
        self.start_index_input.setValue(0)
        index_layout.addWidget(self.start_index_input)
        index_layout.addWidget(QLabel("-"))
        self.end_index_input = QSpinBox()
        self.end_index_input.setRange(0, 4000)
        self.end_index_input.setValue(1350)
        index_layout.addWidget(self.end_index_input)
        index_layout.addStretch()
        params_layout.addLayout(index_layout)

        filter_layout = QHBoxLayout()
        self.enable_filter = QCheckBox("启用距离过滤")
        filter_layout.addWidget(self.enable_filter)
        filter_layout.addWidget(QLabel("最大距离:"))
        self.max_distance_input = QSpinBox()
        self.max_distance_input.setRange(0, 10000)
        self.max_distance_input.setValue(2000)
        self.max_distance_input.setEnabled(False)
        filter_layout.addWidget(self.max_distance_input)
        filter_layout.addWidget(QLabel("(毫米)"))
        filter_layout.addStretch()
        params_layout.addLayout(filter_layout)

        self.enable_filter.stateChanged.connect(
            lambda state: self.max_distance_input.setEnabled(state == Qt.Checked)
        )

        iteration_layout = QHBoxLayout()
        iteration_layout.addWidget(QLabel("测量次数:"))
        self.iterations_input = QSpinBox()
        self.iterations_input.setRange(1, 1000)
        self.iterations_input.setValue(1)
        iteration_layout.addWidget(self.iterations_input)
        iteration_layout.addWidget(QLabel("次"))
        iteration_layout.addStretch()
        params_layout.addLayout(iteration_layout)

        frequency_layout = QHBoxLayout()
        frequency_layout.addWidget(QLabel("雷达频率(Hz):"))
        self.radar_frequency_input = QDoubleSpinBox()
        self.radar_frequency_input.setDecimals(2)
        self.radar_frequency_input.setRange(0.01, 100.0)
        self.radar_frequency_input.setSingleStep(0.01)
        self.radar_frequency_input.setValue(15)
        frequency_layout.addWidget(self.radar_frequency_input)
        frequency_layout.addWidget(QLabel("每圈约"))
        self.scan_period_label = QLabel("3.03 秒")
        frequency_layout.addWidget(self.scan_period_label)
        frequency_layout.addStretch()
        params_layout.addLayout(frequency_layout)

        self.radar_frequency_input.valueChanged.connect(self.update_scan_period_label)
        self.update_scan_period_label(self.radar_frequency_input.value())

        angular_layout = QHBoxLayout()
        angular_layout.addWidget(QLabel("角分辨率(°/点):"))
        self.angular_resolution_input = QDoubleSpinBox()
        self.angular_resolution_input.setDecimals(3)
        self.angular_resolution_input.setRange(0.001, 10.0)
        self.angular_resolution_input.setSingleStep(0.01)
        self.angular_resolution_input.setValue(0.100)
        angular_layout.addWidget(self.angular_resolution_input)

        angular_layout.addWidget(QLabel("扫描角度范围(°):"))
        self.scan_angle_range_input = QDoubleSpinBox()
        self.scan_angle_range_input.setDecimals(1)
        self.scan_angle_range_input.setRange(1.0, 360.0)
        self.scan_angle_range_input.setSingleStep(1.0)
        self.scan_angle_range_input.setValue(270.0)
        angular_layout.addWidget(self.scan_angle_range_input)

        angular_layout.addWidget(QLabel("起始角(°):"))
        self.start_angle_input = QDoubleSpinBox()
        self.start_angle_input.setDecimals(1)
        self.start_angle_input.setRange(-360.0, 360.0)
        self.start_angle_input.setSingleStep(1.0)
        self.start_angle_input.setValue(-45.0)
        angular_layout.addWidget(self.start_angle_input)
        angular_layout.addStretch()
        params_layout.addLayout(angular_layout)

        self.estimated_points_label = QLabel("整圈点数估算: --")
        params_layout.addWidget(self.estimated_points_label)

        self.index_angle_info_label = QLabel("当前索引绝对角度范围: --")
        params_layout.addWidget(self.index_angle_info_label)

        self.start_index_input.valueChanged.connect(self.update_scan_geometry_info)
        self.end_index_input.valueChanged.connect(self.update_scan_geometry_info)
        self.angular_resolution_input.valueChanged.connect(self.update_scan_geometry_info)
        self.scan_angle_range_input.valueChanged.connect(self.update_scan_geometry_info)
        self.start_angle_input.valueChanged.connect(self.update_scan_geometry_info)
        self.update_scan_geometry_info()

        params_group.setLayout(params_layout)
        layout.addWidget(params_group)

        control_group = QGroupBox("测量控制")
        control_layout = QVBoxLayout()

        self.progress_bar = QProgressBar()
        control_layout.addWidget(self.progress_bar)

        self.progress_label = QLabel("等待开始...")
        control_layout.addWidget(self.progress_label)

        button_layout = QHBoxLayout()
        self.start_btn = QPushButton("开始测量")
        self.start_btn.clicked.connect(self.start_measurement)
        self.start_btn.setEnabled(False)

        self.stop_btn = QPushButton("停止测量")
        self.stop_btn.clicked.connect(self.stop_measurement)
        self.stop_btn.setEnabled(False)

        self.repeat_btn = QPushButton("重复上次测量")
        self.repeat_btn.clicked.connect(self.repeat_last_measurement)
        self.repeat_btn.setEnabled(False)

        button_layout.addWidget(self.start_btn)
        button_layout.addWidget(self.stop_btn)
        button_layout.addWidget(self.repeat_btn)
        button_layout.addStretch()
        control_layout.addLayout(button_layout)

        control_group.setLayout(control_layout)
        layout.addWidget(control_group)

        layout.addStretch()
        self.tab_widget.addTab(control_tab, "控制面板")

    def create_data_tab(self):
        # “数据查看”已并入“统计分析”页，保留该方法仅用于兼容原调用顺序。
        return

    def create_visualization_tab(self):
        viz_tab = QWidget()
        layout = QVBoxLayout(viz_tab)

        if PG_AVAILABLE:
            pg.setConfigOptions(antialias=False)

            self.distance_widget = pg.GraphicsLayoutWidget()
            self.distance_plot = self.distance_widget.addPlot(title="距离分布")
            self.distance_plot.setLabel('left', '测量距离 (mm)')
            self.distance_plot.setLabel('bottom', '数据点索引')
            self.distance_curve = self.distance_plot.plot(pen='y')

            self.reflectivity_widget = pg.GraphicsLayoutWidget()
            self.reflectivity_plot = self.reflectivity_widget.addPlot(title="反射率分布")
            self.reflectivity_plot.setLabel('left', '反射率')
            self.reflectivity_plot.setLabel('bottom', '数据点索引')
            self.reflectivity_curve = self.reflectivity_plot.plot(pen='g')

            self.history_widget = pg.GraphicsLayoutWidget()
            self.history_plot = self.history_widget.addPlot(title="历史测量统计")
            self.history_plot.setLabel('left', '符合条件点数')
            self.history_plot.setLabel('bottom', '测量次数')
            self.history_curve = self.history_plot.plot(pen='r')

            layout.addWidget(QLabel("距离分布:"))
            layout.addWidget(self.distance_widget)
            layout.addWidget(QLabel("反射率分布:"))
            layout.addWidget(self.reflectivity_widget)
            layout.addWidget(QLabel("历史测量:"))
            layout.addWidget(self.history_widget)
        else:
            warning_label = QLabel("⚠️ 需要安装pyqtgraph库")
            warning_label.setAlignment(Qt.AlignCenter)
            layout.addWidget(warning_label)

        self.tab_widget.addTab(viz_tab, "可视化")

    def create_statistics_tab(self):
        stats_tab = QWidget()
        layout = QVBoxLayout(stats_tab)

        self.stats_text = QTextEdit()
        self.stats_text.setReadOnly(True)
        self.stats_text.setFont(QFont("Courier", 10))
        self.stats_text.setMaximumHeight(360)

        layout.addWidget(QLabel("详细统计信息:"))
        layout.addWidget(self.stats_text)

        layout.addWidget(QLabel("最近一次测量数据:"))

        self.data_table = QTableWidget()
        self.data_table.setColumnCount(6)
        headers = ['索引', '测量距离(mm)', '前沿', '后沿', '脉宽', '反射率']
        self.data_table.setHorizontalHeaderLabels(headers)
        self.data_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.data_table.setAlternatingRowColors(True)
        self.data_table.setMaximumHeight(280)
        self.data_table.setStyleSheet("""
            QTableWidget {
                gridline-color: #cccccc;
                font-family: Arial;
                font-size: 11px;
            }
            QTableWidget::item {
                padding: 4px;
            }
            QTableWidget::item:selected {
                background-color: #4a86e8;
                color: white;
            }
        """)
        self.data_table.setColumnWidth(0, 80)
        self.data_table.setColumnWidth(1, 120)
        self.data_table.setColumnWidth(2, 80)
        self.data_table.setColumnWidth(3, 80)
        self.data_table.setColumnWidth(4, 80)
        self.data_table.setColumnWidth(5, 80)
        layout.addWidget(self.data_table)

        stats_layout = QHBoxLayout()
        self.total_points_label = QLabel("总点数: 0")
        self.filtered_points_label = QLabel("符合条件点数: 0")
        self.qualified_label = QLabel("有效圈: 否")
        stats_layout.addWidget(self.total_points_label)
        stats_layout.addWidget(self.filtered_points_label)
        stats_layout.addWidget(self.qualified_label)
        stats_layout.addStretch()
        layout.addLayout(stats_layout)

        self.tab_widget.addTab(stats_tab, "统计分析")

    def add_to_history(self, message):
        current_time = time.strftime("%Y-%m-%d %H:%M:%S")
        self.history_text.append(f"[{current_time}] {message}")

    def update_scan_period_label(self, frequency_hz):
        if frequency_hz <= 0:
            self.scan_period_label.setText("--")
            return

        self.scan_period_label.setText(f"{1.0 / frequency_hz:.2f} 秒")

    def calculate_points_per_scan(self, angular_resolution_deg=None, scan_angle_range_deg=None):
        resolution = angular_resolution_deg if angular_resolution_deg is not None else self.angular_resolution_input.value()
        scan_range = scan_angle_range_deg if scan_angle_range_deg is not None else self.scan_angle_range_input.value()

        if resolution <= 0 or scan_range <= 0:
            return 0

        return max(1, int(round(scan_range / resolution)) + 1)

    def update_scan_geometry_info(self):
        resolution = self.angular_resolution_input.value()
        scan_range = self.scan_angle_range_input.value()
        start_angle = self.start_angle_input.value()
        point_count = self.calculate_points_per_scan(resolution, scan_range)

        if point_count <= 0:
            self.estimated_points_label.setText("整圈点数估算: --")
            self.index_angle_info_label.setText("当前索引绝对角度范围: --")
            return

        start_index = self.start_index_input.value()
        end_index = self.end_index_input.value()
        start_index_angle = start_angle + start_index * resolution
        end_index_angle = start_angle + end_index * resolution
        span_angle = max(0.0, (end_index - start_index) * resolution)
        expected_bytes = point_count * 8

        self.estimated_points_label.setText(
            f"整圈点数估算: {point_count} 点，扫描范围: {start_angle:.1f}° 到 {start_angle + scan_range:.1f}°，预估数据长度: {expected_bytes} 字节"
        )
        self.index_angle_info_label.setText(
            f"当前索引绝对角度范围: {start_index_angle:.3f}° - {end_index_angle:.3f}°，角宽约 {span_angle:.3f}°"
        )

    def validate_ip_input(self, ip):
        try:
            ipaddress.ip_address(ip)
            return True
        except ValueError:
            return False

    def set_connecting_state(self, is_connecting):
        self.connect_btn.setEnabled(not is_connecting)
        self.ip_input.setEnabled(not is_connecting)
        self.port_input.setEnabled(not is_connecting)

        if is_connecting:
            self.start_btn.setEnabled(False)
            self.disconnect_btn.setEnabled(False)
            self.connection_status.setText("连接中...")
            self.connection_status.setStyleSheet("color: #d9822b; font-weight: bold;")

    @pyqtSlot(object)
    def handle_connection_complete(self, result):
        thread = self.connection_thread
        self.connection_thread = None
        self.set_connecting_state(False)

        if thread is not None:
            thread.deleteLater()

        if result['success']:
            if self.radar and self.radar is not result['radar']:
                self.radar.close()

            self.radar = result['radar']
            self.connection_status.setText("已连接")
            self.connection_status.setStyleSheet("color: green; font-weight: bold;")
            self.connection_info.setText(f"IP: {result['host']}, 端口: {result['port']}")
            self.connect_btn.setEnabled(False)
            self.disconnect_btn.setEnabled(True)
            self.start_btn.setEnabled(True)
            self.status_bar.showMessage(f"雷达连接成功: {result['host']}:{result['port']}")
            self.add_to_history(f"连接成功: {result['host']}:{result['port']}")
            return

        self.radar = None
        self.connection_status.setText("连接失败")
        self.connection_status.setStyleSheet("color: red; font-weight: bold;")
        self.connection_info.setText("")
        self.disconnect_btn.setEnabled(False)
        self.start_btn.setEnabled(False)
        self.status_bar.showMessage("雷达连接失败，请检查 IP 后重试")
        self.add_to_history(f"连接失败: {result['host']}:{result['port']} - {result['error']}")
        self.ip_input.setFocus()
        self.ip_input.selectAll()
        QMessageBox.warning(
            self,
            "连接失败",
            f"IP 输入错误或设备不可达，请重新输入。\n\n详细信息: {result['error']}"
        )

    def connect_radar(self):
        ip = self.ip_input.text().strip()
        port_text = self.port_input.text().strip()

        if not ip:
            QMessageBox.warning(self, "输入错误", "请输入雷达IP地址")
            self.ip_input.setFocus()
            return

        if not port_text:
            QMessageBox.warning(self, "输入错误", "请输入端口号")
            self.port_input.setFocus()
            return

        try:
            port = int(port_text)
        except ValueError:
            QMessageBox.warning(self, "输入错误", "端口号必须是数字")
            self.port_input.setFocus()
            self.port_input.selectAll()
            return

        if port <= 0 or port > 65535:
            QMessageBox.warning(self, "输入错误", "端口号必须在 1 到 65535 之间")
            self.port_input.setFocus()
            self.port_input.selectAll()
            return

        if not self.validate_ip_input(ip):
            QMessageBox.warning(self, "IP错误", "IP 地址格式不正确，请重新输入")
            self.ip_input.setFocus()
            self.ip_input.selectAll()
            return

        if self.connection_thread and self.connection_thread.isRunning():
            return

        self.status_bar.showMessage(f"正在连接雷达 {ip}:{port}...")
        self.connection_info.setText(f"IP: {ip}, 端口: {port}")
        self.set_connecting_state(True)
        self.connection_thread = ConnectionThread(ip, port, connect_timeout=3.0)
        self.connection_thread.connection_complete.connect(self.handle_connection_complete)
        self.connection_thread.start()
        return

        ip = self.ip_input.text().strip()
        port_text = self.port_input.text().strip()

        if not ip:
            QMessageBox.warning(self, "输入错误", "请输入雷达IP地址")
            return

        if not port_text:
            QMessageBox.warning(self, "输入错误", "请输入端口号")
            return

        try:
            port = int(port_text)
        except ValueError:
            QMessageBox.warning(self, "输入错误", "端口号必须是数字")
            return

        try:
            self.status_bar.showMessage(f"正在连接雷达 {ip}:{port}...")
            self.radar = RadarDataProcessor(host=ip, port=port)

            if self.radar.connect_radar():
                self.connection_status.setText("已连接")
                self.connection_status.setStyleSheet("color: green; font-weight: bold;")
                self.connection_info.setText(f"IP: {ip}, 端口: {port}")

                self.connect_btn.setEnabled(False)
                self.disconnect_btn.setEnabled(True)
                self.start_btn.setEnabled(True)

                self.status_bar.showMessage(f"雷达连接成功: {ip}:{port}")
                self.add_to_history(f"连接成功: {ip}:{port}")
            else:
                self.connection_status.setText("连接失败")
                self.connection_status.setStyleSheet("color: red; font-weight: bold;")
                self.status_bar.showMessage("雷达连接失败")
                self.add_to_history(f"连接失败: {ip}:{port}")

        except Exception as e:
            QMessageBox.critical(self, "连接错误", f"连接失败: {str(e)}")

    def disconnect_radar(self):
        if self.radar:
            self.radar.close()
            self.radar = None

        self.connection_status.setText("未连接")
        self.connection_status.setStyleSheet("color: red; font-weight: bold;")
        self.connection_info.setText("")

        self.connect_btn.setEnabled(True)
        self.disconnect_btn.setEnabled(False)
        self.start_btn.setEnabled(False)

        self.status_bar.showMessage("已断开连接")
        self.add_to_history("断开连接")

    def start_measurement(self):
        if not self.radar:
            QMessageBox.warning(self, "错误", "请先连接雷达")
            return

        start_index = self.start_index_input.value()
        end_index = self.end_index_input.value()

        if start_index >= end_index:
            QMessageBox.warning(self, "参数错误", "起始索引必须小于结束索引")
            return

        max_distance = self.max_distance_input.value() if self.enable_filter.isChecked() else None
        iterations = self.iterations_input.value()
        radar_frequency_hz = self.radar_frequency_input.value()
        angular_resolution_deg = self.angular_resolution_input.value()
        scan_angle_range_deg = self.scan_angle_range_input.value()
        start_angle_deg = self.start_angle_input.value()
        estimated_points = self.calculate_points_per_scan(angular_resolution_deg, scan_angle_range_deg)

        if end_index >= estimated_points:
            QMessageBox.warning(
                self,
                "参数错误",
                f"当前角分辨率 {angular_resolution_deg:.3f}°/点、扫描角度范围 {scan_angle_range_deg:.1f}° 时，"
                f"整圈点数约为 {estimated_points}。\n结束索引 {end_index} 已超出范围。"
            )
            return

        self.radar.configure_scan_parameters(
            angular_resolution_deg=angular_resolution_deg,
            scan_angle_range_deg=scan_angle_range_deg,
            start_angle_deg=start_angle_deg
        )
        self.radar.last_settings = {
            'start_index': start_index,
            'end_index': end_index,
            'max_distance': max_distance,
            'iterations': iterations,
            'radar_frequency_hz': radar_frequency_hz,
            'angular_resolution_deg': angular_resolution_deg,
            'scan_angle_range_deg': scan_angle_range_deg,
            'start_angle_deg': start_angle_deg
        }

        self.reset_measurement_ui()

        self.measurement_thread = MeasurementThread(
            self.radar, start_index, end_index, max_distance, iterations, radar_frequency_hz
        )
        self.measurement_thread.measurement_progress.connect(self.update_progress)
        self.measurement_thread.measurement_complete.connect(self.measurement_completed)
        self.measurement_thread.measurement_error.connect(self.measurement_error)

        self.measurement_thread.start()

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.repeat_btn.setEnabled(False)
        self.status_bar.showMessage("测量进行中...")

        filter_info = f"过滤: {max_distance}mm" if max_distance else "无过滤"
        self.add_to_history(
            f"开始测量: 索引{start_index}-{end_index}, {filter_info}, {iterations}次, "
            f"频率{radar_frequency_hz:.2f}Hz, 角分辨率{angular_resolution_deg:.3f}°/点, "
            f"起始角{start_angle_deg:.1f}°, 扫描范围{scan_angle_range_deg:.1f}°"
            f"({start_angle_deg:.1f}°到{start_angle_deg + scan_angle_range_deg:.1f}°), 整圈约{estimated_points}点"
        )

    def repeat_last_measurement(self):
        if not self.radar:
            QMessageBox.warning(self, "错误", "请先连接雷达")
            return

        if not self.radar.last_settings:
            QMessageBox.warning(self, "错误", "没有找到上一次的测量设置")
            return

        settings = self.radar.last_settings
        self.start_index_input.setValue(settings['start_index'])
        self.end_index_input.setValue(settings['end_index'])

        if settings['max_distance']:
            self.enable_filter.setChecked(True)
            self.max_distance_input.setValue(settings['max_distance'])
        else:
            self.enable_filter.setChecked(False)

        self.iterations_input.setValue(settings['iterations'])
        self.radar_frequency_input.setValue(settings.get('radar_frequency_hz', 0.33))
        self.angular_resolution_input.setValue(settings.get('angular_resolution_deg', 0.100))
        self.scan_angle_range_input.setValue(settings.get('scan_angle_range_deg', 270.0))
        self.start_angle_input.setValue(settings.get('start_angle_deg', -45.0))
        self.start_measurement()

    def stop_measurement(self):
        if self.measurement_thread and self.measurement_thread.isRunning():
            self.measurement_thread.stop()
            self.measurement_thread.wait()
            self.status_bar.showMessage("测量已停止")
            self.add_to_history("测量已停止")

        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.repeat_btn.setEnabled(True if self.radar and self.radar.last_settings else False)

    def reset_measurement_ui(self):
        self.progress_bar.setValue(0)
        self.progress_label.setText("等待开始...")
        # 清空表格
        self.data_table.clearContents()
        self.data_table.setRowCount(0)
        self.total_points_label.setText("总点数: 0")
        self.filtered_points_label.setText("符合条件点数: 0")
        self.qualified_label.setText("有效圈: 否")

    @pyqtSlot(int, int, int, bool, float)
    def update_progress(self, current, total, filtered_count, has_consecutive, time_ms):
        progress = int(current / total * 100)
        self.progress_bar.setValue(progress)

        qualified_status = "是" if has_consecutive else "否"
        self.progress_label.setText(
            f"进度: {current}/{total} ({progress}%) - "
            f"符合条件: {filtered_count} - "
            f"有效圈: {qualified_status} - "
            f"耗时: {time_ms:.0f}ms"
        )
        self.status_bar.showMessage(f"测量中... {current}/{total}")

    @pyqtSlot(object)
    def measurement_completed(self, summary):
        self.measurement_summary = summary
        success_count = summary['success_count']
        qualified_circles = summary['qualified_circles']
        total_time = summary['total_time']

        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.repeat_btn.setEnabled(True if self.radar and self.radar.last_settings else False)

        self.progress_label.setText(
            f"测量完成! 成功: {success_count}次, 有效圈: {qualified_circles}次, 总耗时: {total_time:.2f}秒")
        self.status_bar.showMessage("测量完成")
        self.add_to_history(f"测量完成: 成功{success_count}次, 有效圈{qualified_circles}次, 耗时{total_time:.2f}秒")

        if summary['latest_measurement']:
            self.update_data_display(summary['latest_measurement'])
            self.update_statistics_summary(summary)
            self.update_visualizations_summary(summary)

    @pyqtSlot(str)
    def measurement_error(self, error_msg):
        QMessageBox.critical(self, "测量错误", f"测量过程中发生错误: {error_msg}")
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.repeat_btn.setEnabled(True if self.radar and self.radar.last_settings else False)
        self.status_bar.showMessage(f"测量错误: {error_msg}")
        self.add_to_history(f"测量错误: {error_msg}")

    def update_data_display(self, measurement):
        """更新数据表格显示"""
        if not measurement or not measurement['results']:
            return

        self.populate_data_table_fast(measurement)
        return

        results = measurement['results']

        print(f"\n📋 准备更新表格显示:")
        print(f"  收到结果数量: {len(results)}")
        print(f"  解析结果示例 (前3个):")
        for i in range(min(3, len(results))):
            r = results[i]
            print(f"    结果{i}: 索引={r['index']}, 距离={r['measured_distance']}mm")

        # 清空表格
        self.data_table.clearContents()
        self.data_table.setRowCount(len(results))

        # 按索引排序，确保显示顺序正确
        sorted_results = sorted(results, key=lambda x: x['index'])

        print(f"  排序后显示 {len(sorted_results)} 行数据:")

        for i, result in enumerate(sorted_results):
            # 第一列显示实际的数据索引
            index_item = QTableWidgetItem(str(result['index']))
            index_item.setTextAlignment(Qt.AlignCenter)
            self.data_table.setItem(i, 0, index_item)

            # 第二列显示测量距离（毫米）
            distance_item = QTableWidgetItem(str(result['measured_distance']))
            distance_item.setTextAlignment(Qt.AlignCenter)
            self.data_table.setItem(i, 1, distance_item)

            # 第三列显示前沿
            front_item = QTableWidgetItem(str(result['front_edge']))
            front_item.setTextAlignment(Qt.AlignCenter)
            self.data_table.setItem(i, 2, front_item)

            # 第四列显示后沿
            back_item = QTableWidgetItem(str(result['back_edge']))
            back_item.setTextAlignment(Qt.AlignCenter)
            self.data_table.setItem(i, 3, back_item)

            # 第五列显示脉宽（后沿-前沿）
            pulse_width = result['back_edge'] - result['front_edge']
            pulse_item = QTableWidgetItem(str(pulse_width))
            pulse_item.setTextAlignment(Qt.AlignCenter)
            self.data_table.setItem(i, 4, pulse_item)

            # 第六列显示反射率
            reflectivity_item = QTableWidgetItem(str(result['reflectivity']))
            reflectivity_item.setTextAlignment(Qt.AlignCenter)
            self.data_table.setItem(i, 5, reflectivity_item)

            # 实时验证显示的数据
            if i < 3:  # 只验证前3行
                print(f"    行{i}: 索引={result['index']}, "
                      f"距离={result['measured_distance']}mm, "
                      f"前沿={result['front_edge']}, "
                      f"后沿={result['back_edge']}")

        self.total_points_label.setText(f"总点数: {measurement['total_count']}")
        self.filtered_points_label.setText(f"符合条件点数: {measurement['filtered_count']}")
        qualified_status = "是" if measurement['has_consecutive_qualified'] else "否"
        self.qualified_label.setText(f"有效圈: {qualified_status}")

        # 强制刷新表格
        self.data_table.viewport().update()

        # 读取并显示表格中的实际数据
        print(f"  读取表格验证 (前3行):")
        for i in range(min(3, self.data_table.rowCount())):
            try:
                index = self.data_table.item(i, 0).text() if self.data_table.item(i, 0) else "空"
                distance = self.data_table.item(i, 1).text() if self.data_table.item(i, 1) else "空"
                front = self.data_table.item(i, 2).text() if self.data_table.item(i, 2) else "空"
                back = self.data_table.item(i, 3).text() if self.data_table.item(i, 3) else "空"
                print(f"    行{i}: 索引={index}, 距离={distance}, 前沿={front}, 后沿={back}")
            except Exception as e:
                print(f"    行{i}: 读取失败 - {e}")

        print(f"  表格状态: 行数={self.data_table.rowCount()}, 列数={self.data_table.columnCount()}")

    def update_statistics(self, all_measurements, iterations, total_time, qualified_circles):
        if not all_measurements:
            return

        total_points_all = sum(m['total_count'] for m in all_measurements)
        filtered_points_all = sum(m['filtered_count'] for m in all_measurements)
        total_circles = len(all_measurements)

        overall_ratio = filtered_points_all / total_points_all * 100 if total_points_all > 0 else 0
        qualified_ratio = qualified_circles / total_circles * 100 if total_circles > 0 else 0

        stats_text = f"""
{'=' * 70}
📊 快速批量测量统计结果
{'=' * 70}
🔢 测量次数: {iterations} 次
✅ 成功测量: {total_circles} 次
🎯 有效圈数: {qualified_circles} 次
📊 有效圈比例: {qualified_ratio:.2f}%
📈 总数据点数: {total_points_all}
🎯 符合条件点数: {filtered_points_all}
📊 总过滤比例: {overall_ratio:.2f}%
⏱️  总耗时: {total_time:.2f} 秒
🚀 平均每次: {total_time / iterations * 1000:.1f} 毫秒
📡 测量频率: {iterations / total_time:.1f} 次/秒
{'=' * 70}"""
        self.stats_text.setText(stats_text)

    def update_visualizations(self, all_measurements):
        if not PG_AVAILABLE:
            return

        if not all_measurements or not all_measurements[-1]['results']:
            return

        results = all_measurements[-1]['results']
        # 按索引排序以确保正确绘制
        sorted_results = sorted(results, key=lambda x: x['index'])
        indices = [r['index'] for r in sorted_results]
        distances = [r['measured_distance'] for r in sorted_results]
        self.distance_curve.setData(indices, distances)

        reflectivities = [r['reflectivity'] for r in sorted_results]
        self.reflectivity_curve.setData(indices, reflectivities)

        if len(all_measurements) > 1:
            iteration_numbers = [m['iteration'] for m in all_measurements]
            filtered_counts = [m['filtered_count'] for m in all_measurements]
            self.history_curve.setData(iteration_numbers, filtered_counts)

    def populate_data_table_fast(self, measurement):
        results = measurement['results']

        self.data_table.setUpdatesEnabled(False)
        self.data_table.setSortingEnabled(False)

        try:
            self.data_table.clearContents()
            self.data_table.setRowCount(len(results))

            for row, result in enumerate(results):
                row_values = (
                    result['index'],
                    result['measured_distance'],
                    result['front_edge'],
                    result['back_edge'],
                    result['back_edge'] - result['front_edge'],
                    result['reflectivity'],
                )

                for column, value in enumerate(row_values):
                    item = QTableWidgetItem(str(value))
                    item.setTextAlignment(Qt.AlignCenter)
                    self.data_table.setItem(row, column, item)
        finally:
            self.data_table.setUpdatesEnabled(True)
            self.data_table.viewport().update()

        self.total_points_label.setText(f"总点数: {measurement['total_count']}")
        self.filtered_points_label.setText(f"符合条件点数: {measurement['filtered_count']}")
        qualified_status = "是" if measurement['has_consecutive_qualified'] else "否"
        self.qualified_label.setText(f"有效圈: {qualified_status}")
        return
        """

        self.total_points_label.setText(f"鎬荤偣鏁? {measurement['total_count']}")
        self.filtered_points_label.setText(f"绗﹀悎鏉′欢鐐规暟: {measurement['filtered_count']}")
        qualified_status = "鏄? if measurement['has_consecutive_qualified'] else "鍚?
        self.qualified_label.setText(f"鏈夋晥鍦? {qualified_status}")

        """
    def update_statistics_summary(self, summary):
        if not summary or summary['success_count'] == 0:
            return

        total_points_all = summary['total_points_all']
        filtered_points_all = summary['filtered_points_all']
        requested_iterations = summary.get('requested_iterations', summary['success_count'])
        total_circles = summary['success_count']
        qualified_circles = summary['qualified_circles']
        total_time = summary['total_time']
        latest_measurement = summary.get('latest_measurement') or {}
        angular_resolution_deg = latest_measurement.get('angular_resolution_deg', self.angular_resolution_input.value())
        scan_angle_range_deg = latest_measurement.get('scan_angle_range_deg', self.scan_angle_range_input.value())
        start_angle_deg = latest_measurement.get('start_angle_deg', self.start_angle_input.value())
        start_index = latest_measurement.get('start_index', self.start_index_input.value())
        end_index = latest_measurement.get('end_index', self.end_index_input.value())
        expected_point_count = latest_measurement.get('expected_point_count', self.calculate_points_per_scan(angular_resolution_deg, scan_angle_range_deg))
        scan_end_angle_deg = start_angle_deg + scan_angle_range_deg
        index_start_angle_deg = start_angle_deg + start_index * angular_resolution_deg
        index_end_angle_deg = start_angle_deg + end_index * angular_resolution_deg

        overall_ratio = filtered_points_all / total_points_all * 100 if total_points_all > 0 else 0
        qualified_ratio = qualified_circles / total_circles * 100 if total_circles > 0 else 0
        avg_time_ms = total_time / total_circles * 1000 if total_circles > 0 else 0
        frequency = total_circles / total_time if total_time > 0 else 0

        stats_text = f"""
{'=' * 70}
测量统计分析
{'=' * 70}
测量次数: {requested_iterations}
成功测量: {total_circles}
有效圈数: {qualified_circles}
有效圈占比: {qualified_ratio:.2f}%
角分辨率: {angular_resolution_deg:.3f} °/点
起始角: {start_angle_deg:.1f} °
扫描角度范围: {scan_angle_range_deg:.1f} °
整圈绝对角度范围: {start_angle_deg:.1f} ° 到 {scan_end_angle_deg:.1f} °
当前索引绝对角度范围: {index_start_angle_deg:.3f} ° 到 {index_end_angle_deg:.3f} °
整圈点数估算: {expected_point_count}
总点数: {total_points_all}
符合条件点数: {filtered_points_all}
过滤占比: {overall_ratio:.2f}%
总耗时: {total_time:.2f} 秒
平均耗时: {avg_time_ms:.1f} 毫秒
测量频率: {frequency:.1f} 次/秒
{'=' * 70}"""
        self.stats_text.setText(stats_text)
        return

        stats_text = f"""
{'=' * 70}
馃搳 蹇€熸壒閲忔祴閲忕粺璁＄粨鏋?
{'=' * 70}
馃敘 娴嬮噺娆℃暟: {total_circles} 娆?
鉁?鎴愬姛娴嬮噺: {total_circles} 娆?
馃幆 鏈夋晥鍦堟暟: {qualified_circles} 娆?
馃搳 鏈夋晥鍦堟瘮渚? {qualified_ratio:.2f}%
馃搱 鎬绘暟鎹偣鏁? {total_points_all}
馃幆 绗﹀悎鏉′欢鐐规暟: {filtered_points_all}
馃搳 鎬昏繃婊ゆ瘮渚? {overall_ratio:.2f}%
鈴憋笍  鎬昏€楁椂: {total_time:.2f} 绉?
馃殌 骞冲潎姣忔: {avg_time_ms:.1f} 姣
馃摗 娴嬮噺棰戠巼: {frequency:.1f} 娆?绉?
{'=' * 70}"""
        self.stats_text.setText(stats_text)

    def update_visualizations_summary(self, summary):
        if not PG_AVAILABLE:
            return

        latest_measurement = summary.get('latest_measurement')
        if not latest_measurement or not latest_measurement['results']:
            return

        results = latest_measurement['results']
        indices = [r['index'] for r in results]
        distances = [r['measured_distance'] for r in results]
        reflectivities = [r['reflectivity'] for r in results]

        self.distance_curve.setData(indices, distances)
        self.reflectivity_curve.setData(indices, reflectivities)

        if summary['iteration_numbers']:
            self.history_curve.setData(summary['iteration_numbers'], summary['filtered_counts'])

    def closeEvent(self, event):
        self.stop_measurement()
        if self.radar:
            self.radar.close()
        event.accept()


# ==============================================================================
# 雷达数据处理类 - 修正数据接收问题
# ==============================================================================

class RadarDataProcessor:
    def __init__(self, host='192.168.0.240', port=2111, connect_timeout=3.0):
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.socket = None
        self.last_settings = None
        self.last_error = ""
        self.block_size = 8
        self.angular_resolution_deg = 0.100
        self.scan_angle_range_deg = 270.0
        self.start_angle_deg = -45.0
        self.expected_point_count = 2701
        self.expected_data_size = self.expected_point_count * self.block_size
        self.debug_logging = False
        self.first_packet_timeout = 0.12
        self.idle_packet_timeout = 0.015
        self.max_receive_window = 0.25
        self.packet_drain_timeout = 0.02

    def _debug_print(self, message):
        if self.debug_logging:
            print(message)

    def configure_scan_parameters(self, angular_resolution_deg=None, scan_angle_range_deg=None, start_angle_deg=None):
        if angular_resolution_deg is not None and angular_resolution_deg > 0:
            self.angular_resolution_deg = angular_resolution_deg
        if scan_angle_range_deg is not None and scan_angle_range_deg > 0:
            self.scan_angle_range_deg = scan_angle_range_deg
        if start_angle_deg is not None:
            self.start_angle_deg = start_angle_deg

        self.expected_point_count = max(
            1,
            int(round(self.scan_angle_range_deg / self.angular_resolution_deg)) + 1
        )
        self.expected_data_size = self.expected_point_count * self.block_size

    def calculate_required_bytes(self, end_index):
        target_blocks = max(1, end_index + 1)
        return min(self.expected_data_size, target_blocks * self.block_size)

    def drain_current_packet_tail(self, already_received):
        if not self.socket:
            return

        remaining_budget = max(0, self.expected_data_size - already_received)
        if remaining_budget <= 0:
            return

        previous_timeout = None
        try:
            previous_timeout = self.socket.gettimeout()
            self.socket.settimeout(self.packet_drain_timeout)
            drained_bytes = 0
            while drained_bytes < remaining_budget:
                chunk = self.socket.recv(min(8192, remaining_budget - drained_bytes))
                if not chunk:
                    break
                drained_bytes += len(chunk)
        except socket.timeout:
            pass
        except Exception:
            pass
        finally:
            try:
                self.socket.settimeout(previous_timeout)
            except Exception:
                pass

    def connect_radar(self):
        try:
            self.last_error = ""
            if self.socket is not None:
                return True
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 128 * 1024)  # 128KB接收缓冲区
            self.socket.settimeout(self.connect_timeout)
            self.socket.connect((self.host, self.port))
            console_log(f"[INFO] Connected radar: {self.host}:{self.port}")
            return True
        except Exception as e:
            console_log(f"[ERROR] Connect failed: {e}")
            self.close()
            return False

    def ensure_connection(self):
        return self.socket is not None or self.connect_radar()

    def drain_pending_data(self):
        if not self.socket:
            return

        try:
            previous_timeout = self.socket.gettimeout()
            self.socket.setblocking(False)
            drain_start = time.perf_counter()
            drained_bytes = 0
            max_drain_window = 0.03
            max_drain_bytes = 64 * 1024
            while True:
                if (time.perf_counter() - drain_start) >= max_drain_window:
                    break
                if drained_bytes >= max_drain_bytes:
                    break
                try:
                    chunk = self.socket.recv(8192)
                    if not chunk:
                        break
                    drained_bytes += len(chunk)
                except BlockingIOError:
                    break
        except Exception:
            pass
        finally:
            try:
                self.socket.setblocking(True)
                self.socket.settimeout(previous_timeout)
            except Exception:
                pass

    def send_command(self, command_hex='02 02 02 02 00 09 02 64 77'):
        try:
            if not self.ensure_connection():
                return False
            self.drain_pending_data()
            cmd_rawdata = command_hex.replace(' ', '')
            command_bytes = bytes.fromhex(cmd_rawdata)
            self.socket.send(command_bytes)
            return True
        except Exception as e:
            console_log(f"[ERROR] Send command failed: {e}")
            return False

    def receive_radar_data_complete_fast(self, required_bytes=None):
        try:
            target_size = max(self.block_size, required_bytes or self.expected_data_size)
            all_data = bytearray()
            start_time = time.perf_counter()
            last_data_time = None
            self.socket.settimeout(self.first_packet_timeout)

            while time.perf_counter() - start_time < self.max_receive_window:
                try:
                    chunk = self.socket.recv(min(8192, max(self.block_size, target_size - len(all_data))))
                    if not chunk:
                        break
                    all_data.extend(chunk)
                    last_data_time = time.perf_counter()
                    self.socket.settimeout(self.idle_packet_timeout)
                    if len(all_data) >= target_size:
                        break
                except socket.timeout:
                    if all_data:
                        if last_data_time and (time.perf_counter() - last_data_time) >= self.idle_packet_timeout:
                            break
                        break
                    return None
            if not all_data:
                return None

            if target_size < self.expected_data_size:
                self.drain_current_packet_tail(len(all_data))

            return bytes(all_data)
        except Exception:
            self.close()
            return None

    def parse_data_range_fast(self, radar_data, start_index, end_index):
        if not radar_data:
            return []

        total_blocks = len(radar_data) // self.block_size
        if total_blocks == 0:
            return []

        actual_end = min(end_index, total_blocks - 1)
        actual_start = max(0, min(start_index, total_blocks - 1))

        results = []
        view = memoryview(radar_data)
        append_result = results.append

        for idx in range(actual_start, actual_end + 1):
            offset = idx * self.block_size
            block = view[offset:offset + self.block_size]
            append_result({
                'index': idx,
                'angle_deg': self.start_angle_deg + idx * self.angular_resolution_deg,
                'measured_distance': int.from_bytes(block[4:6], byteorder='big'),
                'front_edge': int.from_bytes(block[0:2], byteorder='big'),
                'back_edge': int.from_bytes(block[2:4], byteorder='big'),
                'reflectivity': int.from_bytes(block[6:8], byteorder='big')
            })

        return results

    def get_consecutive_qualified_points_fast(self, results, max_distance, consecutive_count=3):
        if not results or len(results) < consecutive_count:
            return []

        consecutive_points = []
        for point in results:
            if 0 < point['measured_distance'] < max_distance:
                consecutive_points.append(point)
                if len(consecutive_points) >= consecutive_count:
                    return consecutive_points[-consecutive_count:]
            else:
                consecutive_points.clear()

        return []

    def print_blind_zone_points(self, start_index, end_index, results, filtered_results, has_consecutive, consecutive_points):
        if not self.debug_logging:
            return

        print(f"\n◆ 本次盲区测量：索引 {start_index}-{end_index}")
        print(f"◆ 实际处理点数：{len(results)}")
        print(f"◆ 当前角分辨率：{self.angular_resolution_deg:.3f}°/点")
        print(f"◆ 当前起始角：{self.start_angle_deg:.1f}°")
        print(f"◆ 当前扫描角度范围：{self.scan_angle_range_deg:.1f}°")
        print(f"◆ 当前整圈绝对角度范围：{self.start_angle_deg:.1f}° - {self.start_angle_deg + self.scan_angle_range_deg:.1f}°")
        print(f"◆ 当前整圈点数估算：{self.expected_point_count}")

        if results:
            distances = [r['measured_distance'] for r in results]
            print("◆ 统计信息：")
            print(f"   索引范围：{results[0]['index']} - {results[-1]['index']}")
            print(f"   绝对角度范围：{results[0]['angle_deg']:.3f}° - {results[-1]['angle_deg']:.3f}°")
            print(f"   距离范围：{min(distances)} - {max(distances)} mm")
            print(f"   平均距离：{sum(distances) / len(distances):.1f} mm")

        if filtered_results:
            print(f"◆ 命中盲区判断点：{len(filtered_results)} 个")
            for idx, point in enumerate(filtered_results, 1):
                print(
                    f"   点{idx}: 索引={point['index']}, 绝对角度={point['angle_deg']:.3f}°, 距离={point['measured_distance']}mm, "
                    f"前沿={point['front_edge']}, 后沿={point['back_edge']}, 反射率={point['reflectivity']}"
                )
        else:
            print("◆ 命中盲区判断点：0 个")

        if has_consecutive and consecutive_points:
            print(f"◆ 连续条件：满足（连续 {len(consecutive_points)} 个点）")
            for idx, point in enumerate(consecutive_points, 1):
                print(
                    f"   连续点{idx}: 索引={point['index']}, 绝对角度={point['angle_deg']:.3f}°, 距离={point['measured_distance']}mm, "
                    f"前沿={point['front_edge']}, 后沿={point['back_edge']}, 反射率={point['reflectivity']}"
                )
        else:
            print("◆ 连续条件：不满足")

    def has_consecutive_qualified_points_fast(self, results, max_distance, consecutive_count=3):
        return bool(self.get_consecutive_qualified_points_fast(results, max_distance, consecutive_count))

    def optimized_single_measurement(self, start_index, end_index, max_distance=None):
        current_settings = dict(self.last_settings) if self.last_settings else {}
        iterations = current_settings.get('iterations', 1)
        self.last_settings = {
            **current_settings,
            'start_index': start_index,
            'end_index': end_index,
            'max_distance': max_distance,
            'iterations': iterations,
            'angular_resolution_deg': self.angular_resolution_deg,
            'scan_angle_range_deg': self.scan_angle_range_deg,
            'start_angle_deg': self.start_angle_deg
        }

        if not self.send_command():
            return None

        radar_data = self.receive_radar_data_complete_fast(
            required_bytes=self.calculate_required_bytes(end_index)
        )
        if not radar_data:
            return None

        results = self.parse_data_range_fast(radar_data, start_index, end_index)

        if max_distance:
            filtered_results = [
                r for r in results
                if 10 < r['measured_distance'] < max_distance
            ]
            consecutive_points = self.get_consecutive_qualified_points_fast(results, max_distance, 3)
            has_consecutive = bool(consecutive_points)
            self.print_blind_zone_points(
                start_index,
                end_index,
                results,
                filtered_results,
                has_consecutive,
                consecutive_points
            )
        else:
            filtered_results = results
            has_consecutive = False
            consecutive_points = []

        return {
            'total_count': len(results),
            'filtered_count': len(filtered_results),
            'results': filtered_results,
            'has_consecutive_qualified': has_consecutive,
            'consecutive_points': consecutive_points,
            'start_index': start_index,
            'end_index': end_index,
            'angular_resolution_deg': self.angular_resolution_deg,
            'scan_angle_range_deg': self.scan_angle_range_deg,
            'start_angle_deg': self.start_angle_deg,
            'expected_point_count': self.expected_point_count,
            'frame_signature': tuple(
                (
                    point['index'],
                    round(point['angle_deg'], 6),
                    point['measured_distance'],
                    point['front_edge'],
                    point['back_edge'],
                    point['reflectivity']
                )
                for point in results
            )
        }

    def receive_radar_data_complete(self):
        """接收完整的雷达数据"""
        try:
            print(f"\n🚀 开始接收雷达数据...")
            all_data = b""
            start_time = time.time()
            self.socket.settimeout(3.0)  # 设置接收超时

            # 先等待数据开始
            time.sleep(0.2)

            expected_size = self.expected_data_size
            print(f"   期望数据大小: {expected_size} 字节")

            bytes_received = 0
            chunk_count = 0

            while time.time() - start_time < 5.0:  # 最大等待5秒
                try:
                    # 尝试接收数据
                    chunk = self.socket.recv(8192)
                    if chunk:
                        all_data += chunk
                        chunk_count += 1
                        bytes_received = len(all_data)

                        print(f"   数据块{chunk_count}: 收到 {len(chunk)} 字节, 累计 {bytes_received} 字节")

                        # 检查是否达到预期大小
                        if bytes_received >= expected_size:
                            print(f"✅ 收到完整数据: {bytes_received} 字节")
                            break

                        # 如果没有更多数据，等待一下
                        if len(chunk) < 8192:
                            time.sleep(0.1)
                    else:
                        time.sleep(0.1)

                except socket.timeout:
                    if bytes_received > 0:
                        print(f"⚠️  接收超时，已收到 {bytes_received} 字节")
                        break
                    continue

            if bytes_received < expected_size:
                print(f"⚠️  警告: 数据不完整，期望 {expected_size} 字节，实际收到 {bytes_received} 字节")
            else:
                print(f"✅ 成功接收 {bytes_received} 字节数据")

            return all_data

        except Exception as e:
            print(f"❌ 接收数据失败: {e}")
            return None

    def get_data_range_fast(self, radar_data, start_index, end_index):
        """快速处理数据范围"""
        if not radar_data:
            print("❌ 雷达数据为空")
            return []

        total_bytes = len(radar_data)
        print(f"\n🔍 数据处理:")
        print(f"   接收数据总长度: {total_bytes} 字节")
        print(f"   期望数据长度: {self.expected_data_size} 字节")

        # 转换为16进制字符串
        hex_data = radar_data.hex().upper()

        print(f"   16进制数据长度: {len(hex_data)} 字符")

        # 计算数据块数量（每16个字符一个数据块）
        data_blocks = [hex_data[i:i + 16] for i in range(0, len(hex_data), 16) if i + 16 <= len(hex_data)]
        total_blocks = len(data_blocks)

        print(f"   数据块数量: {total_blocks} (16字符格式)")
        print(f"   理论最大索引: {total_blocks - 1}")

        if total_blocks == 0:
            print("⚠️  没有找到有效数据块")
            return []

        # 显示数据块详细信息
        print(f"   前5个数据块解析验证:")
        for i in range(min(5, total_blocks)):
            block = data_blocks[i]
            try:
                distance = int(block[8:12], 16)
                front_edge = int(block[0:4], 16)
                back_edge = int(block[4:8], 16)
                reflectivity = int(block[12:16], 16)
                print(f"     块 {i}: {block}")
                print(f"       距离: {distance}mm, 前沿: {front_edge}, 后沿: {back_edge}, 反射率: {reflectivity}")
            except Exception as e:
                print(f"     块 {i}: {block} - 解析失败: {e}")

        # 检查请求的索引范围
        actual_end = min(end_index, total_blocks - 1)
        actual_start = max(0, min(start_index, total_blocks - 1))

        print(f"\n🎯 请求索引范围: {start_index}-{end_index}")
        print(f"   实际处理范围: {actual_start}-{actual_end}")
        print(f"   预计处理点数: {actual_end - actual_start + 1}")

        results = []

        # 处理指定范围的数据
        for idx in range(actual_start, actual_end + 1):
            try:
                block = data_blocks[idx]
                measured_distance = int(block[8:12], 16)
                front_edge = int(block[0:4], 16)
                back_edge = int(block[4:8], 16)
                reflectivity = int(block[12:16], 16)

                results.append({
                    'index': idx,
                    'measured_distance': measured_distance,
                    'front_edge': front_edge,
                    'back_edge': back_edge,
                    'reflectivity': reflectivity
                })

            except Exception as e:
                print(f"❌ 解析数据块 {idx} 失败: {e}")
                continue

        print(f"✅ 成功解析 {len(results)} 个数据点")

        # 显示统计信息
        if results:
            print(f"\n📊 统计信息:")
            print(f"   索引范围: {results[0]['index']} - {results[-1]['index']}")

            distances = [r['measured_distance'] for r in results]
            print(f"   距离范围: {min(distances)} - {max(distances)} mm")
            print(f"   平均距离: {sum(distances) / len(distances):.1f} mm")

            # 显示前几个点的详细信息
            print(f"   前3个数据点:")
            for i in range(min(3, len(results))):
                r = results[i]
                print(f"     点{i}: 索引={r['index']}, 距离={r['measured_distance']}mm, "
                      f"前沿={r['front_edge']}, 后沿={r['back_edge']}, 反射率={r['reflectivity']}")

        return results

    def has_consecutive_qualified_points(self, results, max_distance, consecutive_count=3):
        if not results or len(results) < consecutive_count:
            return False

        sorted_results = sorted(results, key=lambda x: x['index'])

        for i in range(len(sorted_results) - consecutive_count + 1):
            consecutive_qualified = True
            for j in range(consecutive_count):
                point = sorted_results[i + j]
                if point['measured_distance'] == 0 or point['measured_distance'] >= max_distance:
                    consecutive_qualified = False
                    break

            if consecutive_qualified:
                return True

        return False

    def fast_single_measurement(self, start_index, end_index, max_distance=None):
        """快速单次测量"""
        print(f"\n🔧 开始单次测量: 索引 {start_index}-{end_index}")

        # 保存设置
        self.last_settings = {
            'start_index': start_index,
            'end_index': end_index,
            'max_distance': max_distance,
            'iterations': 1,
            'angular_resolution_deg': self.angular_resolution_deg,
            'scan_angle_range_deg': self.scan_angle_range_deg,
            'start_angle_deg': self.start_angle_deg
        }

        if not self.send_command():
            print("❌ 发送指令失败")
            return None

        radar_data = self.receive_radar_data_complete()  # 使用完整的数据接收方法
        if not radar_data:
            print("❌ 接收数据失败")
            return None

        results = self.get_data_range_fast(radar_data, start_index, end_index)

        if max_distance:
            filtered_results = [
                r for r in results
                if 10 < r['measured_distance'] < max_distance
            ]

            has_consecutive = self.has_consecutive_qualified_points(results, max_distance, 3)
            print(f"🎯 过滤结果: {len(filtered_results)}/{len(results)} 个点符合条件")
            print(f"🎯 连续条件: {'满足' if has_consecutive else '不满足'}")
        else:
            filtered_results = results
            has_consecutive = False
            print(f"📊 未启用过滤，总点数: {len(results)}")

        return {
            'total_count': len(results),
            'filtered_count': len(filtered_results),
            'results': filtered_results,
            'has_consecutive_qualified': has_consecutive
        }

    def close(self):
        if self.socket:
            self.socket.close()
            self.socket = None


def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    window = RadarVisualizationApp()
    window.show()
    sys.exit(app.exec_())


if __name__ == '__main__':
    main()
