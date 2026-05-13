import socket
import threading
import re
import time
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext


class RadarPressureTestGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("雷达压力测试工具")
        self.root.geometry("1000x700")

        self.stop_threads_dict = {}
        self.threading_list = []
        self.test_counters = {}  # 用于记录每个IP的测试次数

        self.setup_ui()

    def setup_ui(self):
        # 主框架
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 左侧配置区域
        config_frame = ttk.LabelFrame(main_frame, text="测试配置", padding="10")
        config_frame.pack(side=tk.LEFT, fill=tk.Y, padx=5, pady=5)

        # 右侧日志区域
        log_frame = ttk.LabelFrame(main_frame, text="测试日志", padding="10")
        log_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=5, pady=5)

        # 配置项目
        ttk.Label(config_frame, text="雷达IP地址(多个用、分割):").grid(row=0, column=0, sticky=tk.W, pady=5)
        self.ip_entry = ttk.Entry(config_frame, width=25)
        self.ip_entry.insert(0, "192.168.1.111")
        self.ip_entry.grid(row=0, column=1, sticky=tk.W, pady=5)

        ttk.Label(config_frame, text="每圈包数:").grid(row=1, column=0, sticky=tk.W, pady=5)
        self.pack_total_entry = ttk.Entry(config_frame, width=10)
        self.pack_total_entry.insert(0, "7")
        self.pack_total_entry.grid(row=1, column=1, sticky=tk.W, pady=5)

        ttk.Label(config_frame, text="时间戳容差:").grid(row=2, column=0, sticky=tk.W, pady=5)
        self.tolerance_entry = ttk.Entry(config_frame, width=10)
        self.tolerance_entry.insert(0, "0.2")
        self.tolerance_entry.grid(row=2, column=1, sticky=tk.W, pady=5)

        ttk.Label(config_frame, text="测试次数:").grid(row=3, column=0, sticky=tk.W, pady=5)
        self.test_times_entry = ttk.Entry(config_frame, width=10)
        self.test_times_entry.insert(0, "10")
        self.test_times_entry.grid(row=3, column=1, sticky=tk.W, pady=5)

        ttk.Label(config_frame, text="雷达转速:").grid(row=4, column=0, sticky=tk.W, pady=5)
        self.frequency_entry = ttk.Entry(config_frame, width=10)
        self.frequency_entry.insert(0, "15")
        self.frequency_entry.grid(row=4, column=1, sticky=tk.W, pady=5)

        ttk.Label(config_frame, text="角分辨率:").grid(row=5, column=0, sticky=tk.W, pady=5)
        self.res_entry = ttk.Entry(config_frame, width=10)
        self.res_entry.insert(0, "0.3333")
        self.res_entry.grid(row=5, column=1, sticky=tk.W, pady=5)

        ttk.Label(config_frame, text="扫描点数:").grid(row=6, column=0, sticky=tk.W, pady=5)
        self.scan_num_entry = ttk.Entry(config_frame, width=10)
        self.scan_num_entry.insert(0, "1080")
        self.scan_num_entry.grid(row=6, column=1, sticky=tk.W, pady=5)

        # 测试模式选择 - 只保留通断网测试
        self.test_mode = tk.IntVar(value=1)  # 默认选择通断网测试
        ttk.Radiobutton(config_frame, text="通断网测试", variable=self.test_mode, value=1).grid(row=7, column=0,
                                                                                                columnspan=2,
                                                                                                sticky=tk.W, pady=5)

        # 断网延迟设置
        ttk.Label(config_frame, text="断网延迟(秒):").grid(row=8, column=0, sticky=tk.W, pady=5)
        self.delay_entry = ttk.Entry(config_frame, width=10)
        self.delay_entry.insert(0, "5")
        self.delay_entry.grid(row=8, column=1, sticky=tk.W, pady=5)

        # 按钮区域
        button_frame = ttk.Frame(config_frame)
        button_frame.grid(row=9, column=0, columnspan=2, pady=20)

        self.start_button = ttk.Button(button_frame, text="开始测试", command=self.start_test)
        self.start_button.pack(side=tk.LEFT, padx=5)

        self.stop_button = ttk.Button(button_frame, text="停止测试", command=self.stop_test, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=5)

        # 进度显示
        self.progress_label = ttk.Label(config_frame, text="就绪")
        self.progress_label.grid(row=10, column=0, columnspan=2, pady=10)

        # 日志文本框
        self.log_text = scrolledtext.ScrolledText(log_frame, width=80, height=30, font=("Consolas", 10))
        self.log_text.pack(fill=tk.BOTH, expand=True)

    def log_message(self, message):
        """向日志区域添加消息"""
        self.log_text.insert(tk.END, f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {message}\n")
        self.log_text.see(tk.END)
        self.log_text.update()

    def start_test(self):
        """开始测试"""
        try:
            # 获取配置参数
            main_ip = self.ip_entry.get().strip()
            main_pack_total = int(self.pack_total_entry.get())
            main_timestamp_tolerance = float(self.tolerance_entry.get())
            main_test_times = int(self.test_times_entry.get())
            main_frequency = float(self.frequency_entry.get())
            main_res = float(self.res_entry.get())
            main_scan_num = int(self.scan_num_entry.get())
            main_on_off_network = self.test_mode.get()
            delay_time = float(self.delay_entry.get())

            if not main_ip:
                messagebox.showerror("错误", "请输入雷达IP地址")
                return

            main_ip_list = main_ip.split("、")

            self.log_message("=" * 50)
            self.log_message("开始压力测试")
            self.log_message(f"测试模式: 通断网测试")
            self.log_message(f"测试IP: {main_ip_list}")
            self.log_message(f"测试次数: {main_test_times}")
            self.log_message(f"断网延迟: {delay_time}秒")
            self.log_message("=" * 50)

            # 初始化测试计数器
            for ip in main_ip_list:
                self.stop_threads_dict[ip] = 1
                self.test_counters[ip] = 0

            # 创建并启动线程
            self.threading_list = []
            for ip in main_ip_list:
                thread = threading.Thread(
                    target=self.test_wrapper,
                    args=(ip, main_timestamp_tolerance, main_pack_total, main_test_times,
                          0, 'false', main_frequency, main_res, main_scan_num,
                          main_on_off_network, delay_time)
                )
                self.threading_list.append(thread)
                thread.start()

            # 更新按钮状态
            self.start_button.config(state=tk.DISABLED)
            self.stop_button.config(state=tk.NORMAL)

        except ValueError as e:
            messagebox.showerror("错误", f"参数格式错误: {str(e)}")
        except Exception as e:
            messagebox.showerror("错误", f"启动测试失败: {str(e)}")

    def test_wrapper(self, lidar_ip, timestamp_tolerance, pack_total, test_times,
                     frame_length, precision_frame, lidar_frequency, lidar_res,
                     lidar_scan_point_num, on_off_network, delay_time):
        """测试函数的包装器，用于在GUI中显示日志"""
        try:
            self.test(lidar_ip, timestamp_tolerance, pack_total, test_times,
                      frame_length, precision_frame, lidar_frequency, lidar_res,
                      lidar_scan_point_num, on_off_network, delay_time)
        except Exception as e:
            self.log_message(f"{lidar_ip}: 测试异常 - {str(e)}")

    def test(self, lidar_ip, timestamp_tolerance, pack_total, test_times, frame_length,
             precision_frame, lidar_frequency, lidar_res, lidar_scan_point_num,
             on_off_network, delay_time):
        """压力测试主函数 - 只保留通断网测试"""
        self.log_message(f"{lidar_ip}: 开始通断网测试...共{test_times}次，延迟{delay_time}秒")

        test_count = 0  # 当前测试次数计数器

        while test_count < test_times and self.stop_threads_dict.get(lidar_ip, 0) == 1:
            test_count += 1
            self.test_counters[lidar_ip] = test_count

            # 通断网测试模式 - 每次测试都输出日志
            self.log_message(f"{lidar_ip}: 开始第{test_count}次通断网测试")

            try:
                # 建立连接
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(5)
                    start_connect_time = time.time()
                    s.connect((lidar_ip, 2111))
                    connect_time = time.time() - start_connect_time

                    self.log_message(f"{lidar_ip}: 第{test_count}次测试 - 连接成功，耗时: {connect_time:.3f}秒")

                    # 发送启动命令
                    start_cmd_time = time.time()
                    s.send(bytes.fromhex('02 02 02 02 00 0A 02 31 01 46'.replace(' ', '')))
                    s.recv(20)
                    cmd_time = time.time() - start_cmd_time

                    self.log_message(f"{lidar_ip}: 第{test_count}次测试 - 命令发送成功，耗时: {cmd_time:.3f}秒")

                    # 短暂接收一些数据
                    start_time = time.time()
                    data_received = 0
                    packets_received = 0

                    while time.time() - start_time < 2 and self.stop_threads_dict.get(lidar_ip, 0) == 1:
                        try:
                            data = s.recv(545)
                            if data:
                                data_received += len(data)
                                packets_received += 1
                        except socket.timeout:
                            break

                    receive_time = time.time() - start_time
                    self.log_message(
                        f"{lidar_ip}: 第{test_count}次测试 - 接收数据: {data_received} 字节, {packets_received} 包, 耗时: {receive_time:.3f}秒")

                    # 关闭连接（模拟断网）
                    s.close()

                    self.log_message(f"{lidar_ip}: 第{test_count}次通断网测试完成")

            except socket.timeout:
                self.log_message(f"{lidar_ip}: 第{test_count}次测试 - 连接超时")
            except ConnectionRefusedError:
                self.log_message(f"{lidar_ip}: 第{test_count}次测试 - 连接被拒绝")
            except Exception as e:
                self.log_message(f"{lidar_ip}: 第{test_count}次测试失败 - {str(e)}")

            # 显示当前进度
            self.progress_label.config(text=f"{lidar_ip}: 进度 {test_count}/{test_times}")
            self.root.update()

            # 如果不是最后一次测试，等待延迟时间
            if test_count < test_times and self.stop_threads_dict.get(lidar_ip, 0) == 1:
                self.log_message(f"{lidar_ip}: 等待 {delay_time} 秒后进行下一次测试...")
                for i in range(int(delay_time)):
                    if self.stop_threads_dict.get(lidar_ip, 0) == 0:
                        break
                    time.sleep(1)
                    self.progress_label.config(text=f"{lidar_ip}: 等待中... {delay_time - i} 秒")
                    self.root.update()

        if self.stop_threads_dict.get(lidar_ip, 0) == 0:
            self.log_message(f"{lidar_ip}: 测试被用户中止")
        else:
            self.log_message(f"{lidar_ip}: 通断网测试完成，共进行{test_count}次测试")
            self.log_message(f"{lidar_ip}: 总测试时间: {test_count * (2 + delay_time):.1f} 秒")

    def stop_test(self):
        """停止所有测试"""
        for ip in self.stop_threads_dict:
            self.stop_threads_dict[ip] = 0

        self.log_message("正在停止测试...")

        # 等待所有线程结束
        for thread in self.threading_list:
            thread.join(timeout=3)

        self.log_message("所有测试已停止")

        # 显示最终测试结果
        self.log_message("=" * 50)
        self.log_message("测试结果汇总:")
        for ip, count in self.test_counters.items():
            self.log_message(f"{ip}: 完成 {count} 次测试")
        self.log_message("=" * 50)

        self.start_button.config(state=tk.NORMAL)
        self.stop_button.config(state=tk.DISABLED)
        self.progress_label.config(text="测试已停止")


def main():
    root = tk.Tk()
    app = RadarPressureTestGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()