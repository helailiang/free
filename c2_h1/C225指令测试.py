import struct
import math
import time
import socket
import re
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
import threading


def angle_to_hex(angle_degrees):
    """
    将角度值转换为对应的32位十六进制数

    参数:
    angle_degrees: 角度值（度数）

    返回:
    对应的十六进制字符串（格式: XX XX XX XX）
    """
    # 将角度转换为弧度
    angle_radians = math.radians(angle_degrees)

    # 使用IEEE 754单精度浮点数格式（32位）编码
    # 使用struct将浮点数打包为4字节
    packed_data = struct.pack('>f', angle_radians)

    # 将字节数据转换为十六进制字符串
    hex_string = ''.join(f'{b:02X}' for b in packed_data)

    return hex_string


# 读取序列号
cmd_read_sn = '02 02 02 02 00 09 00 18 29'
# 读取温补系数
cmd_read_temp_coefficient = '02 02 02 02 00 09 00 68 79'
# 读取高压温补系数
cmd_read_high_voltage_temp_coefficient = '02 02 02 02 00 09 00 5B 6C'
# 读取拟合系数参数
cmd_read_fit_coefficient = '02 02 02 02 00 09 00 7C 8D'
# 查询设备状态
cmd_read_lidar_status = '02 02 02 02 00 09 02 0B 1E'
# 读取TDC窗口
cmd_read_tdc_window = '02 02 02 02 00 09 00 52 63'
# 读取距离上下限
cmd_read_dis_upper_lower_limits = '02 02 02 02 00 09 00 5D 6E'
# 读取子网掩码
cmd_read_subnet_mask = '02 02 02 02 00 09 00 14 25'
# 读取网关
cmd_read_gateway = '02 02 02 02 00 09 00 12 23'
# 查询有无IAP程序
cmd_read_iap_exist = '02 02 02 02 00 09 00 F1 02'
# 读取MAC地址
cmd_read_mac = '02 02 02 02 00 09 00 16 27'
# 读取角度偏移量
cmd_read_angle_offset = '02 02 02 02 00 09 00 5F 70'
# 读取出光状态
cmd_read_light = '02 02 02 02 00 09 00 46 57'
# 读取标定模式状态
cmd_read_demarcate = '02 02 02 02 00 09 00 44 55'
# 读取定值修正
cmd_read_dis_offset = '02 02 02 02 00 09 00 6C 7D'
# 查询设备ID
cmd_read_id = '02 02 02 02 00 09 02 0A 1D'
# 读取ip
cmd_read_ip = '02 02 02 02 00 09 00 10 21'
# 加载出厂设置
cmd_load_factory_set = '02 02 02 02 00 09 02 07 1A'
# 读取扫描角度
cmd_read_angle = '02 02 02 02 00 09 00 1C 2D'
# 读取转速角分辨率
cmd_read_freq = '02 02 02 02 00 09 00 1A 2B'
# 读取固件版本
cmd_read_version = '02 02 02 02 00 09 02 0C 1F'


cmd_read_dict = {
    '读取序列号': cmd_read_sn,
    '读取温补系数': cmd_read_temp_coefficient,
    '读取高压温补系数': cmd_read_high_voltage_temp_coefficient,
    '读取拟合系数参数': cmd_read_fit_coefficient,
    '查询设备状态': cmd_read_lidar_status,
    '读取TDC窗口': cmd_read_tdc_window,
    '读取距离上下限': cmd_read_dis_upper_lower_limits,
    '读取子网掩码': cmd_read_subnet_mask,
    '查询有无IAP程序': cmd_read_iap_exist,
    '读取MAC地址': cmd_read_mac,
    '读取角度偏移量': cmd_read_angle_offset,
    '读取出光状态': cmd_read_light,
    '读取标定模式状态': cmd_read_demarcate,
    '读取定值修正': cmd_read_dis_offset,
    '查询设备ID': cmd_read_id,
    '读取雷达版本': cmd_read_version,
}

cmd_read_list_value = list(cmd_read_dict.values())


class LidarConfigTool:
    def __init__(self, root):
        self.root = root
        self.root.title("雷达设备配置工具")
        self.root.geometry("900x600")

        # 主框架
        main_frame = ttk.Frame(root)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # 左侧配置面板
        left_frame = ttk.Frame(main_frame)
        left_frame.pack(side=tk.LEFT, fill=tk.Y, padx=10, pady=10)

        # 右侧日志区域
        right_frame = ttk.Frame(main_frame)
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=10, pady=10)

        # IP地址设置
        ttk.Label(left_frame, text="雷达IP地址:").grid(row=0, column=0, sticky=tk.W, pady=5)
        self.ip_var = tk.StringVar(value="192.168.1.85")
        ttk.Entry(left_frame, textvariable=self.ip_var, width=15).grid(row=0, column=1, pady=5)

        # 新IP地址设置
        ttk.Label(left_frame, text="新IP地址:").grid(row=1, column=0, sticky=tk.W, pady=5)
        self.new_ip_var = tk.StringVar(value="192.168.1.86")
        ttk.Entry(left_frame, textvariable=self.new_ip_var, width=15).grid(row=1, column=1, pady=5)

        # 转速设置
        ttk.Label(left_frame, text="转速(Hz):").grid(row=2, column=0, sticky=tk.W, pady=5)
        self.freq_var = tk.StringVar(value="15")
        ttk.Entry(left_frame, textvariable=self.freq_var, width=15).grid(row=2, column=1, pady=5)

        # 角分辨率设置
        ttk.Label(left_frame, text="角分辨率(°):").grid(row=3, column=0, sticky=tk.W, pady=5)
        self.resolution_var = tk.StringVar(value="0.3333")
        ttk.Entry(left_frame, textvariable=self.resolution_var, width=15).grid(row=3, column=1, pady=5)

        # 起始角度设置
        ttk.Label(left_frame, text="起始角度:").grid(row=4, column=0, sticky=tk.W, pady=5)
        self.start_angle_var = tk.StringVar(value="-45")
        ttk.Entry(left_frame, textvariable=self.start_angle_var, width=15).grid(row=4, column=1, pady=5)

        # 终止角度设置
        ttk.Label(left_frame, text="终止角度:").grid(row=5, column=0, sticky=tk.W, pady=5)
        self.stop_angle_var = tk.StringVar(value="225")
        ttk.Entry(left_frame, textvariable=self.stop_angle_var, width=15).grid(row=5, column=1, pady=5)

        # 操作按钮
        ttk.Button(left_frame, text="连接雷达", command=self.connect_lidar).grid(row=6, column=0, columnspan=2, pady=10)
        ttk.Button(left_frame, text="读取所有参数", command=self.read_all_params).grid(row=7, column=0, columnspan=2,
                                                                                       pady=5)
        ttk.Button(left_frame, text="设置参数", command=self.set_params).grid(row=8, column=0, columnspan=2, pady=5)
        ttk.Button(left_frame, text="加载出厂设置", command=self.load_factory).grid(row=9, column=0, columnspan=2,
                                                                                    pady=5)
        ttk.Button(left_frame, text="保存用户参数设置", command=self.save_settings).grid(row=10, column=0, columnspan=2, pady=5)
        ttk.Button(left_frame, text="重启雷达", command=self.restart_lidar).grid(row=11, column=0, columnspan=2, pady=5)

        # 日志区域
        ttk.Label(right_frame, text="操作日志:").pack(anchor=tk.W)
        self.log_area = scrolledtext.ScrolledText(right_frame, width=60, height=30, font=("Consolas", 10))
        self.log_area.pack(fill=tk.BOTH, expand=True, pady=5)

        # 状态栏
        self.status_var = tk.StringVar()
        self.status_var.set("就绪")
        self.status_bar = tk.Label(root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W)
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)

        # 雷达连接状态
        self.lidar_connected = False
        self.lidar_socket = None

    def log_message(self, message):
        self.log_area.insert(tk.END, f"{time.strftime('%Y-%m-%d %H:%M:%S')} - {message}\n")
        self.log_area.see(tk.END)
        self.status_var.set(message)
        self.root.update()

    def negative_num_to_hex(self, value, bits):
        if value < 0:
            abs_value = abs(value)
            binary_str = bin(abs_value)[2:].zfill(bits)

            inverted_binary_str = ''
            for bit in binary_str:
                if bit == '0':
                    inverted_binary_str += '1'
                else:
                    inverted_binary_str += '0'

            carry = 1
            complement_binary_str = ''
            for i in range(bits - 1, -1, -1):
                if inverted_binary_str[i] == '1' and carry == 1:
                    complement_binary_str = '0' + complement_binary_str
                elif inverted_binary_str[i] == '0' and carry == 1:
                    complement_binary_str = '1' + complement_binary_str
                    carry = 0
                else:
                    complement_binary_str = inverted_binary_str[i] + complement_binary_str

            complement_binary_str = complement_binary_str.lstrip('0')
            hex_str = hex(int(complement_binary_str, 2))[2:]
        else:
            hex_str = hex(value)[2:]

        hex_str_formatted = str(hex_str).upper().zfill(int(bits / 4))
        return hex_str_formatted

    def connect_lidar(self):
        def connect_thread():
            try:
                lidar_ip = self.ip_var.get()
                self.log_message(f"正在连接雷达 {lidar_ip}...")

                self.lidar_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.lidar_socket.settimeout(2)

                try_num = 0
                while try_num < 5:
                    try:
                        self.lidar_socket.connect((lidar_ip, 2111))
                        self.log_message('连接成功')
                        self.lidar_connected = True
                        self.login_lidar()
                        break
                    except Exception as e:
                        try_num += 1
                        self.log_message(f'连接失败，重试中 ({try_num}/5): {str(e)}')
                        if try_num == 5:
                            self.log_message('多次连接失败，请检查网络连接是否正常')
                            self.lidar_connected = False
                        time.sleep(1)

            except Exception as e:
                self.log_message(f"连接失败: {str(e)}")
                self.lidar_connected = False

        threading.Thread(target=connect_thread, daemon=True).start()

    def login_lidar(self):
        try:
            self.log_message("正在登录雷达...")
            cmd_login = '02 02 02 02 00 0E 02 01 02 20 21 05 18 79'
            rec_cmd_login = '02 02 02 02 00 0A 12 01 01 26'

            self.lidar_socket.send(bytes.fromhex(cmd_login.replace(' ', '')))
            data_rec_login = self.lidar_socket.recv(545)

            if data_rec_login.hex().upper() == rec_cmd_login.replace(' ', ''):
                self.log_message('登录成功')
            else:
                self.log_message('登录失败')
                # TODO： 抛异常
        except Exception as e:
            self.log_message(f'登录失败: {str(e)}')

    def read_all_params(self):
        if not self.lidar_connected:
            messagebox.showerror("错误", "请先连接雷达")
            return

        def read_thread():
            try:
                self.log_message("开始读取所有参数...")
                for name, cmd in cmd_read_dict.items():
                    try:
                        self.lidar_socket.send(bytes.fromhex(cmd.replace(' ', '')))
                        data_recv = self.lidar_socket.recv(200).hex().upper()
                        self.log_message(f"{name}: {data_recv}")
                        time.sleep(0.1)
                    except Exception as e:
                        self.log_message(f"读取{name}失败: {str(e)}")
                self.log_message("参数读取完成")
            except Exception as e:
                self.log_message(f"读取参数过程中发生错误: {str(e)}")

        threading.Thread(target=read_thread, daemon=True).start()

    def set_params(self):
        if not self.lidar_connected:
            messagebox.showerror("错误", "请先连接雷达")
            return

        def set_thread():
            try:
                self.log_message("开始设置参数...")

                # 设置扫描角度
                start_angle = float(self.start_angle_var.get())
                stop_angle = float(self.stop_angle_var.get())
                self.set_angle(start_angle, stop_angle)

                # 设置转速和角分辨率
                lidar_freq = float(self.freq_var.get())
                lidar_resolution = float(self.resolution_var.get())
                self.set_freq(lidar_freq, lidar_resolution)

                # 设置IP
                lidar_ip_set = self.new_ip_var.get()
                self.set_ip(lidar_ip_set)

                self.log_message("参数设置完成")
            except Exception as e:
                self.log_message(f"设置参数过程中发生错误: {str(e)}")

        threading.Thread(target=set_thread, daemon=True).start()

    def set_angle(self, start_angle, stop_angle):
        try:
            # 使用新的角度转十六进制函数
            start_angle_hex = angle_to_hex(start_angle)
            stop_angle_hex = angle_to_hex(stop_angle)

            # 构建设置角度的命令
            set_angle_cmd = f'020202020012011B{start_angle_hex}{stop_angle_hex}'

            # 计算校验和
            checksum = 0
            bytes_list = re.findall(r'\w{2}', set_angle_cmd)
            for n in range(len(bytes_list)):
                checksum += int(bytes_list[n], 16)
            checksum_str = str(hex(checksum))[-2:].upper()
            set_angle_cmd = set_angle_cmd + checksum_str

            # 停止取数，清缓存
            cmd_top_data = '02 02 02 02 00 0A 02 31 00 45'
            self.lidar_socket.send(bytes.fromhex((cmd_top_data.replace(' ', ''))))
            self.lidar_socket.recv(50000)

            # 发送扫描角度变更指令
            self.lidar_socket.send(bytes.fromhex(set_angle_cmd))
            self.lidar_socket.recv(545)
            self.log_message(f'修改扫描角度为{start_angle}°至{stop_angle}°')

        except Exception as e:
            self.log_message(f"设置角度失败: {str(e)}")

    # def set_freq(self, lidar_freq, lidar_resolution):
    #     try:
    #         cmd_set_freq = '02020202000D011905DC0D05'
    #         # 02 02 02 02 00 0D 01 19 05 DC 0D 05 22
    #         freq_hex = self.negative_num_to_hex(lidar_freq * 100, bits=16)
    #         resolution_hex = self.negative_num_to_hex(int(lidar_resolution * 10000), bits=16)
    #         cmd_set_freq = cmd_set_freq.replace('05DC0D05', freq_hex + resolution_hex)
    #
    #         checksum = 0
    #         bytes_list = re.findall(r'\w{2}', cmd_set_freq)
    #         for n in range(len(bytes_list)):
    #             checksum += int(bytes_list[n], 16)
    #         checksum_str = str(hex(checksum))[-2:].upper()
    #         cmd_set_freq = cmd_set_freq + checksum_str
    #
    #         self.lidar_socket.send(bytes.fromhex(cmd_set_freq))
    #         recv_set_freq = self.lidar_socket.recv(545).hex().upper()
    #         self.log_message(f'修改转速角分辨率为{lidar_freq}Hz及{lidar_resolution}°')
    #
    #     except Exception as e:
    #         self.log_message(f"设置转速和角分辨率失败: {str(e)}")
    def set_freq(self, lidar_freq, lidar_resolution):
        try:
            cmd_set_freq = '02020202000D011905DC0D05'
            # 02 02 02 02 00 0D 01 19 05 DC 0D 05 22

            # 转速转换: 频率值 × 100 = 十六进制值
            freq_value = int(lidar_freq * 100)
            freq_hex = format(freq_value, '04X')  # 转换为4位十六进制

            # 角分辨率转换: 分辨率值 × 10000 = 十六进制值
            resolution_value = int(lidar_resolution * 10000)
            resolution_hex = format(resolution_value, '04X')  # 转换为4位十六进制

            cmd_set_freq = cmd_set_freq.replace('05DC0D05', freq_hex + resolution_hex)

            checksum = 0
            bytes_list = re.findall(r'\w{2}', cmd_set_freq)
            for n in range(len(bytes_list)):
                checksum += int(bytes_list[n], 16)
            checksum_str = str(hex(checksum))[-2:].upper()
            cmd_set_freq = cmd_set_freq + checksum_str

            self.lidar_socket.send(bytes.fromhex(cmd_set_freq))
            recv_set_freq = self.lidar_socket.recv(545).hex().upper()
            self.log_message(f'修改转速角分辨率为{lidar_freq}Hz及{lidar_resolution}°')

        except Exception as e:
            self.log_message(f"设置转速和角分辨率失败: {str(e)}")
    def set_ip(self, lidar_ip_set):
        try:
            lidar_ip_set_list = lidar_ip_set.split('.')
            ip_1 = self.negative_num_to_hex(int(lidar_ip_set_list[0]), bits=8)
            ip_2 = self.negative_num_to_hex(int(lidar_ip_set_list[1]), bits=8)
            ip_3 = self.negative_num_to_hex(int(lidar_ip_set_list[2]), bits=8)
            ip_4 = self.negative_num_to_hex(int(lidar_ip_set_list[3]), bits=8)
            ip_str = ip_1 + ip_2 + ip_3 + ip_4
            gateway_str = ip_1 + ip_2 + ip_3 + '01'

            cmd_set_ip = '02020202000D010FC0A8016F'.replace('C0A8016F', ip_str)
            cmd_set_gateway = '02020202000D0111C0A80101'.replace('C0A80101', gateway_str)

            # 计算IP指令校验和
            checksum = 0
            bytes_list = re.findall(r'\w{2}', cmd_set_ip)
            for n in range(len(bytes_list)):
                checksum += int(bytes_list[n], 16)
            checksum_str = str(hex(checksum))[-2:].upper()
            cmd_set_ip = cmd_set_ip + checksum_str

            # 计算网关指令校验和
            checksum = 0
            bytes_list = re.findall(r'\w{2}', cmd_set_gateway)
            for n in range(len(bytes_list)):
                checksum += int(bytes_list[n], 16)
            checksum_str = str(hex(checksum))[-2:].upper()
            cmd_set_gateway = cmd_set_gateway + checksum_str

            # 发送IP设置指令
            self.lidar_socket.send(bytes.fromhex(cmd_set_ip))
            self.lidar_socket.recv(545)

            # 发送网关设置指令
            self.lidar_socket.send(bytes.fromhex(cmd_set_gateway))
            self.lidar_socket.recv(545)

            self.log_message(f'修改IP为{lidar_ip_set}')

        except Exception as e:
            self.log_message(f"设置IP失败: {str(e)}")

    def load_factory(self):
        if not self.lidar_connected:
            messagebox.showerror("错误", "请先连接雷达")
            return

        def load_thread():
            try:
                self.log_message("加载出厂设置...")
                self.lidar_socket.send(bytes.fromhex(cmd_load_factory_set.replace(' ', '')))
                data_rec_login = self.lidar_socket.recv(545)

                if data_rec_login.hex().upper() == '02 02 02 02 00 0A 12 07 01 2C'.replace(' ', ''):
                    self.log_message('加载成功')
                else:
                    self.log_message('加载失败')

            except Exception as e:
                self.log_message(f"加载出厂设置失败: {str(e)}")

        threading.Thread(target=load_thread, daemon=True).start()

    def save_settings(self):
        if not self.lidar_connected:
            messagebox.showerror("错误", "请先连接雷达")
            return

        def save_thread():
            try:
                self.log_message("保存设置...")
                cmd_save_para = '02 02 02 02 00 09 02 06 19'
                rec_save_para = '02 02 02 02 00 0A 12 06 01 2B'

                self.lidar_socket.send(bytes.fromhex(cmd_save_para.replace(' ', '')))
                data_rec_save = self.lidar_socket.recv(545)

                if data_rec_save.hex().upper() == rec_save_para.replace(' ', ''):
                    self.log_message('保存完成')
                else:
                    self.log_message('保存异常')

            except Exception as e:
                self.log_message(f"保存设置失败: {str(e)}")

        threading.Thread(target=save_thread, daemon=True).start()

    def restart_lidar(self):
        if not self.lidar_connected:
            messagebox.showerror("错误", "请先连接雷达")
            return

        def restart_thread():
            try:
                self.log_message("重启雷达...")
                cmd_restart = '02 02 02 02 00 09 02 00 13'
                self.lidar_socket.send(bytes.fromhex(cmd_restart.replace(' ', '')))
                self.lidar_socket.recv(545)
                self.lidar_socket.close()
                self.lidar_connected = False
                self.log_message('重启雷达，请等待20s')

            except Exception as e:
                self.log_message(f"重启雷达失败: {str(e)}")

        threading.Thread(target=restart_thread, daemon=True).start()


if __name__ == "__main__":
    root = tk.Tk()
    app = LidarConfigTool(root)
    root.mainloop()