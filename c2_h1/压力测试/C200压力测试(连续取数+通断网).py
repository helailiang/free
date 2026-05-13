import socket
import threading
import time
import re
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import json
from datetime import datetime


class Converter:
    @staticmethod
    def to_ascii(h):
        list_s = []
        for i in range(0, len(h), 2):
            list_s.append(chr(int(h[i:i + 2], 16)))
        return ''.join(list_s)

    @staticmethod
    def to_hex(s):
        list_h = []
        for c in s:
            list_h.append(str(hex(ord(c))[2:]))
        return ''.join(list_h)


class LidarTestGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("激光雷达压力测试工具")
        self.root.geometry("1000x700")

        self.stop_threads = False
        self.test_thread = None

        self.setup_ui()

    def setup_ui(self):
        # 创建笔记本控件用于分页
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # 连续取数测试页
        self.continuous_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.continuous_frame, text="连续取数测试")

        # 通断网测试页
        self.network_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.network_frame, text="通断网测试")

        # 设置连续取数测试界面
        self.setup_continuous_test()

        # 设置通断网测试界面
        self.setup_network_test()

        # 日志输出区域
        self.setup_log_area()

    def setup_continuous_test(self):
        # 参数输入框架
        param_frame = ttk.LabelFrame(self.continuous_frame, text="测试参数", padding=10)
        param_frame.pack(fill=tk.X, padx=10, pady=5)

        # 创建网格布局
        row = 0
        ttk.Label(param_frame, text="雷达IP:").grid(row=row, column=0, sticky=tk.W, padx=5, pady=2)
        self.continuous_ip = ttk.Entry(param_frame, width=15)
        self.continuous_ip.grid(row=row, column=1, sticky=tk.W, padx=5, pady=2)
        self.continuous_ip.insert(0, "192.168.10.7")

        row += 1
        ttk.Label(param_frame, text="扫描频率:").grid(row=row, column=0, sticky=tk.W, padx=5, pady=2)
        self.frequency = ttk.Entry(param_frame, width=10)
        self.frequency.grid(row=row, column=1, sticky=tk.W, padx=5, pady=2)
        self.frequency.insert(0, "30")

        row += 1
        ttk.Label(param_frame, text="角分辨率:").grid(row=row, column=0, sticky=tk.W, padx=5, pady=2)
        self.resolution = ttk.Entry(param_frame, width=10)
        self.resolution.grid(row=row, column=1, sticky=tk.W, padx=5, pady=2)
        self.resolution.insert(0, "0.1")

        row += 1
        ttk.Label(param_frame, text="扫描点数:").grid(row=row, column=0, sticky=tk.W, padx=5, pady=2)
        self.scan_points = ttk.Entry(param_frame, width=10)
        self.scan_points.grid(row=row, column=1, sticky=tk.W, padx=5, pady=2)
        self.scan_points.insert(0, "2701")

        row += 1
        ttk.Label(param_frame, text="每圈包数:").grid(row=row, column=0, sticky=tk.W, padx=5, pady=2)
        self.pack_total = ttk.Entry(param_frame, width=10)
        self.pack_total.grid(row=row, column=1, sticky=tk.W, padx=5, pady=2)
        self.pack_total.insert(0, "22")

        row += 1
        ttk.Label(param_frame, text="时间戳容差:").grid(row=row, column=0, sticky=tk.W, padx=5, pady=2)
        self.timestamp_tol = ttk.Entry(param_frame, width=10)
        self.timestamp_tol.grid(row=row, column=1, sticky=tk.W, padx=5, pady=2)
        self.timestamp_tol.insert(0, "0.2")

        row += 1
        ttk.Label(param_frame, text="测试圈数:").grid(row=row, column=0, sticky=tk.W, padx=5, pady=2)
        self.test_cycles = ttk.Entry(param_frame, width=10)
        self.test_cycles.grid(row=row, column=1, sticky=tk.W, padx=5, pady=2)
        self.test_cycles.insert(0, "1000")

        # 控制按钮
        btn_frame = ttk.Frame(self.continuous_frame)
        btn_frame.pack(fill=tk.X, padx=10, pady=10)

        self.start_btn = ttk.Button(btn_frame, text="开始连续取数测试", command=self.start_continuous_test)
        self.start_btn.pack(side=tk.LEFT, padx=5)

        self.stop_btn = ttk.Button(btn_frame, text="停止测试", command=self.stop_test, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=5)

        # 进度显示
        self.progress_frame = ttk.Frame(self.continuous_frame)
        self.progress_frame.pack(fill=tk.X, padx=10, pady=5)

        ttk.Label(self.progress_frame, text="进度:").pack(side=tk.LEFT)
        self.progress_var = tk.StringVar()
        self.progress_var.set("0/0")
        ttk.Label(self.progress_frame, textvariable=self.progress_var).pack(side=tk.LEFT, padx=5)

    def setup_network_test(self):
        # 参数输入框架
        param_frame = ttk.LabelFrame(self.network_frame, text="通断网测试参数", padding=10)
        param_frame.pack(fill=tk.X, padx=10, pady=5)

        row = 0
        ttk.Label(param_frame, text="雷达IP:").grid(row=row, column=0, sticky=tk.W, padx=5, pady=2)
        self.network_ip = ttk.Entry(param_frame, width=15)
        self.network_ip.grid(row=row, column=1, sticky=tk.W, padx=5, pady=2)
        self.network_ip.insert(0, "192.168.10.7")

        row += 1
        ttk.Label(param_frame, text="扫描频率:").grid(row=row, column=0, sticky=tk.W, padx=5, pady=2)
        self.net_frequency = ttk.Entry(param_frame, width=10)
        self.net_frequency.grid(row=row, column=1, sticky=tk.W, padx=5, pady=2)
        self.net_frequency.insert(0, "30")

        row += 1
        ttk.Label(param_frame, text="角分辨率:").grid(row=row, column=0, sticky=tk.W, padx=5, pady=2)
        self.net_resolution = ttk.Entry(param_frame, width=10)
        self.net_resolution.grid(row=row, column=1, sticky=tk.W, padx=5, pady=2)
        self.net_resolution.insert(0, "0.1")

        row += 1
        ttk.Label(param_frame, text="测试次数:").grid(row=row, column=0, sticky=tk.W, padx=5, pady=2)
        self.net_test_times = ttk.Entry(param_frame, width=10)
        self.net_test_times.grid(row=row, column=1, sticky=tk.W, padx=5, pady=2)
        self.net_test_times.insert(0, "50")

        # 控制按钮
        btn_frame = ttk.Frame(self.network_frame)
        btn_frame.pack(fill=tk.X, padx=10, pady=10)

        self.net_start_btn = ttk.Button(btn_frame, text="开始通断网测试", command=self.start_network_test)
        self.net_start_btn.pack(side=tk.LEFT, padx=5)

        self.net_stop_btn = ttk.Button(btn_frame, text="停止测试", command=self.stop_test, state=tk.DISABLED)
        self.net_stop_btn.pack(side=tk.LEFT, padx=5)

    def setup_log_area(self):
        log_frame = ttk.LabelFrame(self.root, text="测试日志", padding=10)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.log_text = scrolledtext.ScrolledText(log_frame, height=15, width=100)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.log_text.config(state=tk.DISABLED)

    def log_message(self, message):
        """向日志区域添加消息"""
        self.log_text.config(state=tk.NORMAL)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.log_text.insert(tk.END, f"[{timestamp}] {message}\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)
        self.root.update()

    def start_continuous_test(self):
        """开始连续取数测试"""
        try:
            lidar_ip = self.continuous_ip.get()
            frequency = float(self.frequency.get())
            resolution = float(self.resolution.get())
            scan_points = int(self.scan_points.get())
            pack_total = int(self.pack_total.get())
            timestamp_tol = float(self.timestamp_tol.get())
            test_cycles = int(self.test_cycles.get())

            self.stop_threads = False
            self.start_btn.config(state=tk.DISABLED)
            self.stop_btn.config(state=tk.NORMAL)
            self.net_start_btn.config(state=tk.DISABLED)

            self.test_thread = threading.Thread(
                target=self.continuous_data_test,
                args=(lidar_ip, timestamp_tol, pack_total, test_cycles,
                      0, 'false', frequency, resolution, scan_points)
            )
            self.test_thread.daemon = True
            self.test_thread.start()

        except ValueError as e:
            messagebox.showerror("输入错误", f"请输入有效的数值: {e}")

    def start_network_test(self):
        """开始通断网测试"""
        try:
            lidar_ip = self.network_ip.get()
            frequency = float(self.net_frequency.get())
            resolution = float(self.net_resolution.get())
            test_times = int(self.net_test_times.get())

            self.stop_threads = False
            self.net_start_btn.config(state=tk.DISABLED)
            self.net_stop_btn.config(state=tk.NORMAL)
            self.start_btn.config(state=tk.DISABLED)

            self.test_thread = threading.Thread(
                target=self.network_on_off_test,
                args=(lidar_ip, 0.2, 22, test_times,
                      0, 'false', frequency, resolution, 2701)
            )
            self.test_thread.daemon = True
            self.test_thread.start()

        except ValueError as e:
            messagebox.showerror("输入错误", f"请输入有效的数值: {e}")

    def stop_test(self):
        """停止测试"""
        self.stop_threads = True
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self.net_start_btn.config(state=tk.NORMAL)
        self.net_stop_btn.config(state=tk.DISABLED)
        self.log_message("测试已停止")

    def update_progress(self, current, total):
        """更新进度显示"""
        self.progress_var.set(f"{current}/{total}")
        self.root.update()

    def continuous_data_test(self, lidar_ip, timestamp_tolerance, pack_total, test_times,
                             frame_length, precision_frame, lidar_frequency, lidar_res, lidar_scan_point_num):
        """连续取数测试"""
        self.log_message(f"{lidar_ip}: 开始连续取数压力测试，共{test_times}圈数据")

        cycles_completed = 0
        error_count = 0

        while cycles_completed < test_times and not self.stop_threads:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(5)
                    s.connect((lidar_ip, 2111))

                    # 启动连续取数
                    start_cmd = '02 02 02 02 00 0A 02 31 01 46'.replace(' ', '')
                    s.send(bytes.fromhex(start_cmd))
                    s.recv(20)

                    self.log_message(f"{lidar_ip}: 连续取数已启动")

                    data_buffer = ""
                    last_scan_index = -1

                    while cycles_completed < test_times and not self.stop_threads:
                        try:
                            recv_data = s.recv(545)
                            if not recv_data:
                                break

                            data_buffer += recv_data.hex().upper()

                            # 处理数据包
                            while len(data_buffer) >= 12:
                                if data_buffer[:8] != '02020202':
                                    data_buffer = data_buffer[2:]
                                    continue

                                pack_length = int(data_buffer[8:12], 16)
                                if len(data_buffer) < pack_length * 2:
                                    break

                                # 解析包信息
                                scan_index = int(data_buffer[18:22], 16)
                                pack_index = int(data_buffer[22:24], 16)

                                if pack_index == 0:  # 新的一圈开始
                                    cycles_completed += 1
                                    self.update_progress(cycles_completed, test_times)
                                    self.log_message(f"{lidar_ip}: 第{cycles_completed}圈数据接收完成")

                                data_buffer = data_buffer[pack_length * 2:]

                        except socket.timeout:
                            self.log_message(f"{lidar_ip}: 数据接收超时")
                            break
                        except Exception as e:
                            self.log_message(f"{lidar_ip}: 数据处理错误: {e}")
                            error_count += 1
                            break

                    # 停止连续取数
                    stop_cmd = '02 02 02 02 00 0A 02 31 00 45'.replace(' ', '')
                    s.send(bytes.fromhex(stop_cmd))

            except Exception as e:
                self.log_message(f"{lidar_ip}: 连接错误: {e}")
                error_count += 1
                time.sleep(2)

        self.log_message(f"{lidar_ip}: 连续取数测试完成，共完成{cycles_completed}圈，错误{error_count}次")
        self.stop_test()

    def network_on_off_test(self, lidar_ip, timestamp_tolerance, pack_total, test_times,
                            frame_length, precision_frame, lidar_frequency, lidar_res, lidar_scan_point_num):
        """通断网测试"""
        self.log_message(f"{lidar_ip}: 开始通断网测试，共{test_times}次")

        for test_count in range(1, test_times + 1):
            if self.stop_threads:
                break

            self.log_message(f"{lidar_ip}: 第{test_count}次通断网测试开始")

            try:
                # 连接并获取参数
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(3)
                    s.connect((lidar_ip, 2111))

                    # 获取扫描配置
                    config_cmd = '02 02 02 02 00 09 00 1A 2B'.replace(' ', '')
                    s.send(bytes.fromhex(config_cmd))
                    config_data = s.recv(545).hex().upper()

                    if len(config_data) == 26:
                        self.log_message(f"{lidar_ip}: 参数获取成功")
                    else:
                        self.log_message(f"{lidar_ip}: 参数获取失败")

                    # 短暂连接后断开
                    time.sleep(4)

            except Exception as e:
                self.log_message(f"{lidar_ip}: 第{test_count}次测试失败: {e}")

            # 等待一段时间再进行下一次测试
            for i in range(5):
                if self.stop_threads:
                    break
                time.sleep(1)

        self.log_message(f"{lidar_ip}: 通断网测试完成，共进行{test_times}次")
        self.stop_test()


def main():
    root = tk.Tk()
    app = LidarTestGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()