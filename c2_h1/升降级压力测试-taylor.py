import socket
import re
import time
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import os
from datetime import datetime


class LidarPressureTest:
    def __init__(self, root):
        self.root = root
        self.root.title("激光雷达升级降级压力测试工具")
        self.root.geometry("900x700")

        # 测试控制变量
        self.is_testing = False
        self.current_cycle = 0
        self.total_cycles = 0
        self.success_count = 0
        self.fail_count = 0

        # 固件文件
        self.upgrade_file = ""
        self.downgrade_file = ""

        self.setup_ui()

    def setup_ui(self):
        # 主框架
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))

        # 配置区域
        config_frame = ttk.LabelFrame(main_frame, text="测试配置", padding="5")
        config_frame.grid(row=0, column=0, columnspan=2, sticky=(tk.W, tk.E), pady=5)

        # IP地址
        ttk.Label(config_frame, text="雷达IP:").grid(row=0, column=0, sticky=tk.W, pady=2)
        self.ip_var = tk.StringVar(value="192.168.1.85")
        ttk.Entry(config_frame, textvariable=self.ip_var, width=15).grid(row=0, column=1, sticky=tk.W, pady=2)

        # 端口
        ttk.Label(config_frame, text="端口:").grid(row=0, column=2, sticky=tk.W, pady=2, padx=(20, 0))
        self.port_var = tk.StringVar(value="2111")
        ttk.Entry(config_frame, textvariable=self.port_var, width=10).grid(row=0, column=3, sticky=tk.W, pady=2)

        # 循环次数
        ttk.Label(config_frame, text="循环次数:").grid(row=1, column=0, sticky=tk.W, pady=2)
        self.cycle_var = tk.StringVar(value="10")
        ttk.Entry(config_frame, textvariable=self.cycle_var, width=15).grid(row=1, column=1, sticky=tk.W, pady=2)

        # 固件文件选择
        ttk.Label(config_frame, text="升级固件:").grid(row=2, column=0, sticky=tk.W, pady=2)
        self.upgrade_file_var = tk.StringVar()
        ttk.Entry(config_frame, textvariable=self.upgrade_file_var, width=50).grid(row=2, column=1, columnspan=2,
                                                                                   sticky=(tk.W, tk.E), pady=2)
        ttk.Button(config_frame, text="选择", command=self.select_upgrade_file).grid(row=2, column=3, pady=2, padx=5)

        ttk.Label(config_frame, text="降级固件:").grid(row=3, column=0, sticky=tk.W, pady=2)
        self.downgrade_file_var = tk.StringVar()
        ttk.Entry(config_frame, textvariable=self.downgrade_file_var, width=50).grid(row=3, column=1, columnspan=2,
                                                                                     sticky=(tk.W, tk.E), pady=2)
        ttk.Button(config_frame, text="选择", command=self.select_downgrade_file).grid(row=3, column=3, pady=2, padx=5)

        # 控制按钮区域
        button_frame = ttk.Frame(main_frame)
        button_frame.grid(row=1, column=0, columnspan=2, pady=10)

        self.start_button = ttk.Button(button_frame, text="开始测试", command=self.start_test)
        self.start_button.pack(side=tk.LEFT, padx=5)

        self.stop_button = ttk.Button(button_frame, text="停止测试", command=self.stop_test, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=5)

        # 进度条
        self.progress = ttk.Progressbar(main_frame, mode='determinate')
        self.progress.grid(row=2, column=0, columnspan=2, sticky=(tk.W, tk.E), pady=5)

        # 状态显示区域
        status_frame = ttk.LabelFrame(main_frame, text="测试状态", padding="5")
        status_frame.grid(row=3, column=0, columnspan=2, sticky=(tk.W, tk.E), pady=5)

        # 进度信息
        self.progress_var = tk.StringVar(value="准备就绪")
        ttk.Label(status_frame, textvariable=self.progress_var, font=('Arial', 10)).grid(row=0, column=0, sticky=tk.W,
                                                                                         pady=2)

        # 统计信息
        stats_frame = ttk.Frame(status_frame)
        stats_frame.grid(row=1, column=0, sticky=(tk.W, tk.E), pady=5)

        ttk.Label(stats_frame, text="当前循环:").grid(row=0, column=0, sticky=tk.W)
        self.current_cycle_var = tk.StringVar(value="0")
        ttk.Label(stats_frame, textvariable=self.current_cycle_var).grid(row=0, column=1, sticky=tk.W, padx=(5, 20))

        ttk.Label(stats_frame, text="成功次数:").grid(row=0, column=2, sticky=tk.W)
        self.success_var = tk.StringVar(value="0")
        ttk.Label(stats_frame, textvariable=self.success_var).grid(row=0, column=3, sticky=tk.W, padx=(5, 20))

        ttk.Label(stats_frame, text="失败次数:").grid(row=0, column=4, sticky=tk.W)
        self.fail_var = tk.StringVar(value="0")
        ttk.Label(stats_frame, textvariable=self.fail_var).grid(row=0, column=5, sticky=tk.W, padx=(5, 20))

        # 版本信息
        version_frame = ttk.Frame(status_frame)
        version_frame.grid(row=2, column=0, sticky=(tk.W, tk.E), pady=5)

        ttk.Label(version_frame, text="旧版本:").grid(row=0, column=0, sticky=tk.W)
        self.old_version_var = tk.StringVar(value="未知")
        ttk.Label(version_frame, textvariable=self.old_version_var).grid(row=0, column=1, sticky=tk.W, padx=(5, 20))

        ttk.Label(version_frame, text="新版本:").grid(row=0, column=2, sticky=tk.W)
        self.new_version_var = tk.StringVar(value="未知")
        ttk.Label(version_frame, textvariable=self.new_version_var).grid(row=0, column=3, sticky=tk.W, padx=(5, 20))

        # 日志区域
        log_frame = ttk.LabelFrame(main_frame, text="测试日志", padding="5")
        log_frame.grid(row=4, column=0, columnspan=2, sticky=(tk.W, tk.E, tk.N, tk.S), pady=5)

        # 创建文本框和滚动条
        self.log_text = tk.Text(log_frame, height=20, width=100)
        scrollbar = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)

        self.log_text.grid(row=0, column=0, sticky=(tk.W, tk.E, tk.N, tk.S))
        scrollbar.grid(row=0, column=1, sticky=(tk.N, tk.S))

        # 配置权重
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(4, weight=1)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        config_frame.columnconfigure(1, weight=1)

    def select_upgrade_file(self):
        filename = filedialog.askopenfilename(title="选择升级固件文件")
        if filename:
            self.upgrade_file_var.set(filename)
            self.upgrade_file = filename

    def select_downgrade_file(self):
        filename = filedialog.askopenfilename(title="选择降级固件文件")
        if filename:
            self.downgrade_file_var.set(filename)
            self.downgrade_file = filename

    def log_message(self, message):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"[{timestamp}] {message}\n"
        self.log_text.insert(tk.END, log_entry)
        self.log_text.see(tk.END)
        self.root.update()

    def update_progress(self, value):
        self.progress['value'] = value
        self.root.update()

    def start_test(self):
        if not self.upgrade_file or not self.downgrade_file:
            messagebox.showerror("错误", "请选择升级和降级固件文件")
            return

        try:
            self.total_cycles = int(self.cycle_var.get())
            if self.total_cycles <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("错误", "请输入有效的循环次数")
            return

        self.is_testing = True
        self.current_cycle = 0
        self.success_count = 0
        self.fail_count = 0
        self.progress['value'] = 0

        self.start_button.config(state=tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL)

        # 在新线程中运行测试
        self.test_thread = threading.Thread(target=self.run_pressure_test)
        self.test_thread.daemon = True
        self.test_thread.start()

    def stop_test(self):
        self.is_testing = False
        self.log_message("测试停止中...")

    def run_pressure_test(self):
        self.log_message(f"开始压力测试，总循环次数: {self.total_cycles}")

        while self.current_cycle < self.total_cycles and self.is_testing:
            self.current_cycle += 1
            self.current_cycle_var.set(str(self.current_cycle))

            self.log_message(f"开始第 {self.current_cycle} 轮循环")

            # 升级测试
            self.progress_var.set(f"第 {self.current_cycle} 轮 - 升级中...")
            upgrade_success = self.lidar_upgrade(self.upgrade_file, "升级")

            if upgrade_success and self.is_testing:
                # 降级测试
                self.progress_var.set(f"第 {self.current_cycle} 轮 - 降级中...")
                downgrade_success = self.lidar_upgrade(self.downgrade_file, "降级")

                if downgrade_success:
                    self.success_count += 1
                    self.success_var.set(str(self.success_count))
                    self.log_message(f"第 {self.current_cycle} 轮循环完成 - 成功")
                else:
                    self.fail_count += 1
                    self.fail_var.set(str(self.fail_count))
                    self.log_message(f"第 {self.current_cycle} 轮循环完成 - 降级失败")
            else:
                self.fail_count += 1
                self.fail_var.set(str(self.fail_count))
                self.log_message(f"第 {self.current_cycle} 轮循环完成 - 升级失败")

            if not self.is_testing:
                break

            # 更新总体进度
            overall_progress = (self.current_cycle / self.total_cycles) * 100
            self.update_progress(overall_progress)

            # 循环间隔
            time.sleep(5)

        # 测试完成
        self.is_testing = False
        self.progress_var.set("测试完成")
        self.log_message(f"压力测试结束 - 成功: {self.success_count}, 失败: {self.fail_count}")

        self.start_button.config(state=tk.NORMAL)
        self.stop_button.config(state=tk.DISABLED)

    def lidar_upgrade(self, software_address, operation_type):
        try:
            lidar_ip = self.ip_var.get()
            port = int(self.port_var.get())

            self.log_message(f"{operation_type}操作: 连接雷达 {lidar_ip}:{port}")

            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(30)
            s.connect((lidar_ip, port))

            # 发送登录指令
            log_in = '02020202000E0201022021051879'
            s.send(bytes.fromhex(log_in))

            while True:
                recv_log = s.recv(545).hex().upper()
                if recv_log == '02020202000A12010126':
                    self.log_message(f"{operation_type}操作: 登录雷达成功")
                    break
                else:
                    self.log_message(f"{operation_type}操作: 登录雷达失败")
                    s.close()
                    return False

            # 查询雷达当前固件版本号
            info = '020202020009020C1f'
            s.send(bytes.fromhex(info))
            recv_info = s.recv(545).hex().upper()
            old_firmware = recv_info[16:24]
            self.old_version_var.set(old_firmware)
            self.log_message(f"{operation_type}前固件版本: {old_firmware}")

            # 读取固件
            with open(software_address, 'rb') as f:
                bin_data = f.read().hex().upper()

            data_list = re.findall(r'\w{2048}', bin_data)
            num = len(data_list)
            last_data = bin_data[num * 2048:]

            self.log_message(f"{operation_type}操作: 固件大小 {num} 包")

            # 固件传输
            baotou = '02020202040B0197'
            checksum = 0
            i = 0

            # 第一包需要补ff
            self.log_message(f"{operation_type}操作: 发送第1包")
            p = data_list[i]
            o = 'f' * 672
            p = p.replace(p[:672], o)
            zhiling1 = baotou + '00' + '0' + hex(i)[2:] + p
            bytes_list = re.findall(r'\w{2}', zhiling1)
            for n in range(len(bytes_list)):
                checksum += int(bytes_list[n], 16)
            check = str(hex(checksum))[-2:]
            zhiling = zhiling1 + check
            checksum = 0
            data = ''
            s.send(bytes.fromhex(zhiling))
            while len(data) < 20:
                data += s.recv(545).hex().upper()
            i += 1

            # 第2包到第256包
            while i < 16:
                if not self.is_testing:
                    s.close()
                    return False

                zhiling1 = baotou + '00' + '0' + hex(i)[2:] + data_list[i]
                bytes_list = re.findall(r'\w{2}', zhiling1)
                for n in range(len(bytes_list)):
                    checksum += int(bytes_list[n], 16)
                check = str(hex(checksum))[-2:]
                zhiling = zhiling1 + check
                checksum = 0
                data = ''
                s.send(bytes.fromhex(zhiling))
                while len(data) < 20:
                    data += s.recv(545).hex().upper()
                i += 1

            for i in range(16, 256):
                if not self.is_testing:
                    s.close()
                    return False

                zhiling1 = baotou + '00' + hex(i)[2:] + data_list[i]
                bytes_list = re.findall(r'\w{2}', zhiling1)
                for n in range(len(bytes_list)):
                    checksum += int(bytes_list[n], 16)
                check = str(hex(checksum))[-2:]
                zhiling = zhiling1 + check
                checksum = 0
                data = ''
                s.send(bytes.fromhex(zhiling))
                while len(data) < 20:
                    data += s.recv(545).hex().upper()
                if i == 127:
                    s.send(bytes.fromhex('02020202000A029600AA'))
                    s.recv(545)
                    time.sleep(6)
                    self.log_message(f"{operation_type}进度: 10%")
                elif i == 255:
                    s.send(bytes.fromhex('02020202000A029601AB'))
                    s.recv(545)
                    time.sleep(6)
                    self.log_message(f"{operation_type}进度: 25%")
                i += 1

            # 第257包到第512包
            for i in range(256, 512):
                if not self.is_testing:
                    s.close()
                    return False

                zhiling1 = baotou + '0' + hex(i)[2:] + data_list[i]
                bytes_list = re.findall(r'\w{2}', zhiling1)
                for n in range(len(bytes_list)):
                    checksum += int(bytes_list[n], 16)
                check = str(hex(checksum))[-2:]
                zhiling = zhiling1 + check
                checksum = 0
                data = ''
                s.send(bytes.fromhex(zhiling))
                while len(data) < 20:
                    data += s.recv(545).hex().upper()
                if i == 383:
                    s.send(bytes.fromhex('02020202000A029602AC'))
                    s.recv(545)
                    time.sleep(6)
                    self.log_message(f"{operation_type}进度: 50%")
                elif i == 511:
                    s.send(bytes.fromhex('02020202000A029603AD'))
                    s.recv(545)
                    time.sleep(6)
                    self.log_message(f"{operation_type}进度: 75%")
                i += 1

            # 第513包到第num包
            for i in range(512, num):
                if not self.is_testing:
                    s.close()
                    return False

                zhiling1 = baotou + '0' + hex(i)[2:] + data_list[i]
                bytes_list = re.findall(r'\w{2}', zhiling1)
                for n in range(len(bytes_list)):
                    checksum += int(bytes_list[n], 16)
                check = str(hex(checksum))[-2:]
                zhiling = zhiling1 + check
                checksum = 0
                data = ''
                s.send(bytes.fromhex(zhiling))
                while len(data) < 20:
                    data += s.recv(545).hex().upper()
                i += 1

            # 最后残留的一包,第num包
            if not self.is_testing:
                s.close()
                return False

            a = 2048 - len(last_data)
            zhiling1 = baotou + '0' + hex(num).upper()[2:] + last_data + '0' * a
            bytes_list = re.findall(r'\w{2}', zhiling1)
            for n in range(len(bytes_list)):
                checksum += int(bytes_list[n], 16)
            check = str(hex(checksum))[-2:]
            zhiling = zhiling1 + check
            s.send(bytes.fromhex(zhiling))
            checksum = 0
            data = ''
            s.send(bytes.fromhex(zhiling))
            while len(data) < 20:
                data += s.recv(545).hex().upper()

            # 用0补齐到第640包
            for i in range(num + 1, 640):
                if not self.is_testing:
                    s.close()
                    return False

                zhiling1 = baotou + '0' + hex(i).upper()[2:] + '0' * 2048
                bytes_list = re.findall(r'\w{2}', zhiling1)
                for n in range(len(bytes_list)):
                    checksum += int(bytes_list[n], 16)
                check = str(hex(checksum))[-2:]
                zhiling = zhiling1 + check
                checksum = 0
                data = ''
                s.send(bytes.fromhex(zhiling))
                while len(data) < 20:
                    data += s.recv(545).hex().upper()
                if i == 639:
                    s.send(bytes.fromhex('02020202000A029604AE'))
                    s.recv(545)
                    time.sleep(6)
                    self.log_message(f"{operation_type}进度: 100%")
                i += 1

            time.sleep(5)
            # 重启雷达
            s.send(bytes.fromhex('020202020009020013'))
            self.log_message(f"{operation_type}操作: 正在重启雷达，请稍等")
            s.close()
            time.sleep(20)

            # 再次连接雷达验证
            self.log_message(f"{operation_type}操作: 验证升级结果")
            for retry in range(10):
                if not self.is_testing:
                    return False

                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(10)
                    s.connect((lidar_ip, port))
                    time.sleep(0.1)
                    s.send(bytes.fromhex('020202020009020b1e'))
                    data = s.recv(545).hex().upper()
                    if data[16:18] in ['01', '04']:
                        self.log_message(f"{operation_type}操作: 雷达状态回复正常")
                        break
                    else:
                        s.close()
                        time.sleep(5)
                except:
                    time.sleep(5)
                    continue
            else:
                self.log_message(f"{operation_type}操作: 雷达重启后连接失败")
                return False

            # 查询升级完成后的固件版本
            s.send(bytes.fromhex('020202020009020c1f'))
            data = ''
            while len(data) < 26:
                data += s.recv(545).hex().upper()
            new_firmware = data[16:24]
            self.new_version_var.set(new_firmware)
            s.close()

            self.log_message(f"{operation_type}操作完成")
            self.log_message(f"旧固件版本: {old_firmware}")
            self.log_message(f"新固件版本: {new_firmware}")

            return True

        except Exception as e:
            self.log_message(f"{operation_type}操作错误: {str(e)}")
            try:
                s.close()
            except:
                pass
            return False


def main():
    root = tk.Tk()
    app = LidarPressureTest(root)
    root.mainloop()


if __name__ == "__main__":
    main()