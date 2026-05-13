import socket
import time
import csv
from collections import defaultdict
import os
from datetime import datetime


def get_radar_model():
    print("请选择雷达型号:")
    print("1. H1雷达")
    print("2. C2雷达")
    while True:
        choice = input("请输入选择 (1 或 2): ").strip()
        if choice == '1':
            return 'H1'
        elif choice == '2':
            return 'C2'
        else:
            print("无效选择，请重新输入")


def parse_h1_timestamp(data):
    # print("==========="+ data[-11:-1].hex().upper())
    current_seconds = int.from_bytes(data[-9:-5], byteorder='big')
    current_nanoseconds = int.from_bytes(data[-5:-1], byteorder='big')
    current_timestamp = current_seconds + current_nanoseconds / (2 ** 32)
    return current_seconds, current_nanoseconds, current_timestamp


def parse_c2_timestamp(data):
    test_time_us = int(data[-4:-1].hex().upper(), 16)
    test_time_s = int(data[-5:-4].hex().upper(), 16)
    test_time_m = int(data[-6:-5].hex().upper(), 16)
    test_time_h = int(data[-7:-6].hex().upper(), 16)
    current_timestamp = (test_time_h * 3600 + test_time_m * 60 + test_time_s) + test_time_us / 1000000
    return test_time_h, test_time_m, test_time_s, test_time_us, current_timestamp


def parse_frame_info(data):
    frame_number = int.from_bytes(data[9:11], byteorder='big') # 扫描次数
    packet_number = data[11]  # 报文编号
    return frame_number, packet_number


def save_to_csv(frame_stats, radar_model, filename=None):
    if filename is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"radar_frame_stats_{radar_model}_{timestamp}.csv"
    os.makedirs(os.path.dirname(filename) if os.path.dirname(filename) else '.', exist_ok=True)
    with open(filename, 'w', newline='', encoding='utf-8') as csvfile:
        fieldnames = ['圈号', '总包数', '起始包号', '结束包号', '本圈最后一包时间戳',
                      '与上圈时间差(ms)', '本圈持续时间(ms)', '状态']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        for stat in frame_stats:
            csv_stat = stat.copy()
            if csv_stat['与上圈时间差(ms)'] is None:
                csv_stat['与上圈时间差(ms)'] = ''
            if csv_stat['本圈持续时间(ms)'] is None:
                csv_stat['本圈持续时间(ms)'] = ''
            writer.writerow(csv_stat)
    print(f"圈统计信息已保存到: {filename}")
    return filename


def print_frame_table(frame_stats):
    if not frame_stats:
        print("暂无圈统计数据")
        return
    print("\n" + "=" * 100)
    print("圈数据统计表格")
    print("=" * 100)
    print(f"{'圈号':<8} {'总包数':<8} {'起始包号':<10} {'结束包号':<10} {'与上圈时间差(ms)':<18} {'本圈时间(ms)':<15} {'状态':<10}")
    print("-" * 100)
    for stat in frame_stats:
        frame_num = stat['圈号']
        total_packets = stat['总包数']
        start_packet = stat['起始包号']
        end_packet = stat['结束包号']
        time_diff = stat['与上圈时间差(ms)']
        duration = stat['本圈持续时间(ms)']
        status = stat['状态']
        time_diff_str = f"{time_diff:.3f}" if time_diff is not None else "N/A"
        duration_str = f"{duration:.3f}" if duration is not None else "N/A"
        print(f"{frame_num:<8} {total_packets:<8} {start_packet:<10} {end_packet:<10} {time_diff_str:<18} {duration_str:<15} {status:<10}")
    print("=" * 100)


# =============================================================================
# 【通用核心】H1/C2 完全一样：根据帧头长度自动切包，解决所有粘包问题
# =============================================================================
def recv_radar_packet(sock, buffer):
    # 先读6字节获取长度
    while len(buffer) < 6:
        chunk = sock.recv(1024)
        if not chunk:
            return None, buffer
        buffer += chunk

    # 第5、6字节 = 整包长度（大端）
    frame_length = int.from_bytes(buffer[4:6], byteorder='big')

    # 读够完整一帧
    while len(buffer) < frame_length:
        chunk = sock.recv(1024)
        if not chunk:
            return None, buffer
        buffer += chunk

    # 切出完整包，剩余数据保留
    packet = buffer[:frame_length]
    buffer = buffer[frame_length:]
    return packet, buffer


def C200_time_cau():
    radar_model = get_radar_model()
    print(f"已选择: {radar_model}雷达")
    print("-" * 80)

    frame_data = defaultdict(dict)
    frame_stats = []
    last_frame_end_timestamp = None
    last_frame_number = None
    buffer = b''  # 全局缓冲，解决粘包

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect(('192.168.0.240', 2111))
    s.settimeout(5)
    time.sleep(0.2)

    # 发送开始指令  连续取数开关
    cmd_rawdata = '02 02 02 02 00 0A 02 31 01 46'
    s.send(bytes.fromhex(cmd_rawdata.replace(' ', '')))
    s.recv(1024)

    # 接收初始包
    data, buffer = recv_radar_packet(s, buffer)
    if not data:
        print("初始数据包接收失败")
        s.close()
        return

    frame_number, packet_number = parse_frame_info(data)
    if radar_model == 'H1':
        prev_seconds, prev_nanoseconds, prev_timestamp = parse_h1_timestamp(data)
    else:
        prev_h, prev_m, prev_s, prev_us, prev_timestamp = parse_c2_timestamp(data)

    frame_data[frame_number][packet_number] = {'timestamp': prev_timestamp}
    print(f"初始圈号:{frame_number} 包号:{packet_number} 包长度:{len(data)}B")
    print("开始稳定监测...")
    print("-" * 80)

    packet_count = 0
    consecutive_errors = 0

    try:
        while True:
            # 自动接收完整雷达包（H1/C2通用）
            data, buffer = recv_radar_packet(s, buffer)
            if not data:
                print("连接断开")
                break

            packet_count += 1
            pkt_len = len(data)
            current_frame_number, current_packet_number = parse_frame_info(data)

            # 时间戳解析（自动区分H1/C2）
            if radar_model == 'H1':
                current_seconds, current_nanoseconds, current_timestamp = parse_h1_timestamp(data)
                sec_diff = current_seconds - prev_seconds
                nsec_diff = current_nanoseconds - prev_nanoseconds
                if nsec_diff < 0:
                    sec_diff -= 1
                    nsec_diff += 2**32
                actual_interval = sec_diff + nsec_diff/(2**32)
                prev_seconds, prev_nanoseconds = current_seconds, current_nanoseconds
            else:
                current_h, current_m, current_s, current_us, current_timestamp = parse_c2_timestamp(data)
                actual_interval = current_timestamp - prev_timestamp
                prev_h, prev_m, prev_s, prev_us, prev_timestamp = current_h, current_m, current_s, current_us, current_timestamp

            # 打印正常间隔
            print(f"包[{packet_count}] 圈{current_frame_number} 包{current_packet_number} 长度:{pkt_len}B 间隔:{actual_interval*1000:.3f}ms")
            frame_data[current_frame_number][current_packet_number] = {'timestamp': current_timestamp}

            # 圈号切换 => 统计上一圈
            if last_frame_number is not None and current_frame_number != last_frame_number:
                print(f"\n🔁 新圈开始: {last_frame_number} -> {current_frame_number}")
                if last_frame_number in frame_data:
                    pkts = frame_data[last_frame_number]
                    first_pkt = min(pkts.keys())
                    last_pkt = max(pkts.keys())
                    t_start = pkts[first_pkt]['timestamp']
                    t_end = pkts[last_pkt]['timestamp']

                    duration_ms = (t_end - t_start) * 1000
                    time_diff_ms = (t_end - last_frame_end_timestamp) * 1000 if last_frame_end_timestamp else None
                    last_frame_end_timestamp = t_end

                    stat = {
                        '圈号': last_frame_number,
                        '总包数': len(pkts),
                        '起始包号': first_pkt,
                        '结束包号': last_pkt,
                        '本圈最后一包时间戳': t_end,
                        '与上圈时间差(ms)': time_diff_ms,
                        '本圈持续时间(ms)': duration_ms,
                        '状态': '完整' if len(pkts) >= 12 else '不完整'
                    }
                    frame_stats.append(stat)
                    print(f"✅ 圈{last_frame_number} 统计：{len(pkts)}包 持续:{duration_ms:.3f}ms")

            last_frame_number = current_frame_number

            # 异常检测
            if actual_interval < -0.001:
                consecutive_errors += 1
                print(f"❌ 时间戳回退 连续错误:{consecutive_errors}")
                if consecutive_errors >= 3:
                    break
                continue
            consecutive_errors = 0

            if actual_interval > 10:
                print("❌ 包间隔超过10秒，停止")
                break

    except KeyboardInterrupt:
        print("\n用户手动停止")
    finally:
        print("发送停止指令...")
        s.send(bytes.fromhex('02 02 02 02 00 0A 02 31 00 45'.replace(' ', '')))
        time.sleep(0.1)
        s.close()
        print("Socket 已关闭")

        if frame_stats:
            print_frame_table(frame_stats)
            save_to_csv(frame_stats, radar_model)
            print(f"\n共统计 {len(frame_stats)} 圈数据")
        print(f"处理总包数：{packet_count}")


def main():
    try:
        C200_time_cau()
    except Exception as e:
        print(f"程序异常：{e}")


if __name__ == '__main__':
    main()