import socket
import threading
from OpenGL.GL import *
from multiprocessing import Process
import re
import time
import serial
import serial.tools.list_ports
from datetime import datetime
import math



# 读取转速角分辨率
cmd_rawdata = '02 02 02 02 00 09 00 1A 2B '
#设置扫描角度
cmd_rawdata_angle = '02 02 02 02 00 09 00 1C 2D'

# 十六进制与ascii码转换
class Converter(object):
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



def test(lidar_ip, timestamp_tolerance, pack_total, test_times, frame_length, precision_frame,lidar_frequency,lidar_res,lidar_scan_point_num,on_off_network,stop_threads,power_com):

    # 电源启动发送指令
    turn_on = bytes.fromhex('001000000005056009d0071924')
    # turn_on = '00100000000505xxyyaabbzzzz'
    # 电源发送指令2
    turn_off = bytes.fromhex('001000000005046009d00724E4')
    # turn_off = '00100000000504xxyyaabbzzzz'
    # 查询设备状态发送
    get_state = '02 02 02 02 00 09 02 0B 1E'
    # 查询设备状态正常返回的状态码
    recv_get_state = '02 02 02 02 00 0A 12 0B 01 30 '

    if on_off_network == 1:
        print(str(lidar_ip) + ':' + '开始测试...' + '共' + str(test_times) + '次通断电测试')
    else:
        print(str(lidar_ip) + ':' + '开始连续取数压力测试')

    now_times = 0
    if on_off_network == 1:
        # 获取所有可用串口
        comlist = serial.tools.list_ports.comports()
        # 修改COM口
        ser = serial.Serial('power_com', 9600, timeout=2)
    while now_times < test_times and stop_threads == 1:
        if on_off_network == 1:
            ser.write(turn_on)
            time.sleep(20)
            now_times += 1
        # -------------------------------连接雷达查询各种参数并赋值用于后续处理---------------------------------------------
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setblocking(0)
        s.settimeout(2)  # 设置超时为2秒
        s.connect((lidar_ip, 2111))
        time.sleep(1)
        # 先关闭连续取数
        s.send(bytes.fromhex('02 02 02 02 00 0A 02 31 00 45'.replace(' ', '')))
        time.sleep(0.1)
        s.recv(12760)
        # ————————————————————————————————————————————————————————————————————————————————
        begin_time = datetime.now()
        # 将当前日期和时间转换为时间戳（秒数），并取整数部分
        timestamp1 = int(begin_time.timestamp())
        while True:
            time.sleep(1)
            now_time = datetime.now()
            # 将当前日期和时间转换为时间戳（秒数），并取整数部分
            timestamp2 = int(now_time.timestamp())
            diff = timestamp2 - timestamp1
            if diff > 10:
                print("重启调速超时")
                exit()
            s.send(bytes.fromhex(get_state.replace(' ', '')))
            time.sleep(0.05)
            recv2 = s.recv(1024).hex().upper().replace(' ', '')
            if recv2 == recv_get_state.replace(' ', ''):
                break
        # ——————————————————————————————————————————————————————————————————————
        # 获取雷达转速角分辨率参数
        s.send(bytes.fromhex(cmd_rawdata.replace(' ', '')))
        try:
            c = str(s.recv(545).hex().upper())
        except socket.timeout:
            print(str(lidar_ip) + ':' + '获取雷达参数超时，请检查雷达是否连接正常')
            time.sleep(5)
            stop_threads = 0
            break
        if len(c) == 26:
            checksum = 0
            bytes_list = re.findall(r'\w{2}', c)
            for n in range(len(bytes_list)-1):
                checksum += int(bytes_list[n], 16)
            check = str(hex(checksum))[-2:].upper()
            if check != c[24:26]:
                print(str(lidar_ip) + ':' + 'Get scan config CheckSum error!')
                print(check)
                print(c)
        else:
            print(str(lidar_ip) + ':' + 'Get scan config error!Length error!')
            print(c)
            time.sleep(5)
            stop_threads = 0
            break
        # 角分辨率大小
        res = float(int(c[20:24],16))/10000
        if res != lidar_res:
            error_time = time.ctime()
            print(error_time)
            print(str(lidar_ip) + ':' + 'Get resolution error! The incorrect resolution is:' + str(res) + ' ,but the correct resolution is:' + str(lidar_res))
        # 转速
        scan_frequency = float(int(c[16:20],16))/100
        # if scan_frequency != lidar_frequency:
        #     error_time = time.ctime()
        #     print(error_time)
        #     print(str(lidar_ip) + ':' + 'Get frequency error! The incorrect frequency is:' + str(scan_frequency) + ' ,but the correct frequency is:' + str(scan_frequency))

        # 获取雷达扫描角度参数
        # s.send(bytes.fromhex(cmd_rawdata.replace(' ', '')))
        # try:
        #     c = str(s.recv(545).hex().upper())
        # except socket.timeout:
        #     print(str(lidar_ip) + ':' + '获取雷达参数超时，请检查雷达是否连接正常')
        #     stop_threads = 0
        #     break
        # if len(c) == 36:
        #     checksum = 0
        #     bytes_list = re.findall(r'\w{2}', c)
        #     for n in range(len(bytes_list) - 1):
        #         checksum += int(bytes_list[n], 16)
        #     check = str(hex(checksum))[-2:]
        #     if check != bytes_list[len(bytes_list) - 1]:
        #         print(str(lidar_ip) + ':' + 'Get scan angle CheckSum error!')
        # else:
        #     print(str(lidar_ip) + ':' + 'Get scan angle error!Length error!')
        #     print(c)
        #     stop_threads = 0
        #     break
        # # 一圈点数
        # start = (c[16:18] << 24 | c[18:20] << 16 | c[11] << 8 | c[12])/10000
        # stop = (c[13] << 24 | c[14] << 16 | c[15] << 8 | c[16])/10000
        all_angle = 270
        if all_angle == 360:
            samples_per_scan = int(all_angle/res)
        else:
            samples_per_scan = int(all_angle/res + 1)
        # print(str(lidar_ip) + ':' + 'res:', res, '    scan_frequency:', scan_frequency,'    samples_per_scan:', samples_per_scan )

        # C200连续取数
        cmd_start_getdata = '02 02 02 02 00 0A 02 31 01 46'
        # PF停止连续取数
        cmd_stop_getdata = '02 02 02 02 00 0A 02 31 00 45'
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setblocking(0)
        s.settimeout(2)  # 设置超时为2秒
        s.connect((lidar_ip, 2111))
        s.send(bytes.fromhex(cmd_start_getdata.replace(' ', '')))  # 发送取数指令
        s.recv(20)
        # print(str(lidar_ip) + ':' + '已开启连续取数！')

        # 需验证的参数
        data = ''
        new_pack_index = 0
        old_pack_index = 0
        new_scan_index = 0
        old_scan_index = -1
        new_pack_time_stamp = 0.0
        old_pack_time_stamp = 0.0
        new_recv_time_stamp = 0.0
        old_recv_time_stamp = 0.0
        word_dis = []
        error_dis_times = 0
        cycle_index = 0
        print('正在接收并处理数据')
        while True:
            if on_off_network == 1 and cycle_index >= 900:
                s.close()
                print(str(lidar_ip) + ':' + '第' + str(now_times) + '次通断电测试通过...')
                ser.write(turn_off)
                time.sleep(5)
                break
            try:
                recv_data = s.recv(545).hex().upper()
            except socket.timeout:
                error_time = time.ctime()
                print(error_time)
                print(str(lidar_ip) + ':' + '点云数据接收超时')
                time.sleep(5)
                stop_threads = 0
                break
            if len(recv_data) > 0:
                data += recv_data
            if len(data) > 12:
                # 检索数据包开始标志
                if data[:8] != '02020202':
                    error_time = time.ctime()
                    print(error_time)
                    print(str(lidar_ip) + ':' + '包头标志错误！')
                    stop_threads = 0
                    break
                # 获取本包数据长度++
                pack_length = int(data[8: 12], 16)
                if len(data) >= pack_length:
                    # print(len(data))
                    # 操作码验证
                    if data[12:14] != '12':
                        error_time = time.ctime()
                        print(error_time)
                        print(str(lidar_ip) + ':' + '操作码错误！')
                        stop_threads = 0
                        break
                    # 指令号验证
                    if data[14:16] != '32':
                        error_time = time.ctime()
                        print(error_time)
                        print(str(lidar_ip) + ':' + '指令号错误！')
                        stop_threads = 0
                        break
                    # 获取本包扫描次数和包序号
                    # print(data)
                    new_scan_index = int(data[18:22], 16)
                    new_pack_index = int(data[22:24], 16)

                    if new_pack_index == 0:
                        # 时间戳不对，则报错
                        if data[pack_length*2 - 22:pack_length*2 - 18] == 'CCCC':
                            second = int(data[pack_length*2 - 18:pack_length*2 - 16], 16) << 24 | int(data[pack_length*2 - 16:pack_length*2 - 14], 16) << 16 | \
                                     int(data[pack_length*2 - 14 :pack_length*2 -12], 16) << 8 | int(data[pack_length*2 - 12:pack_length*2 - 10], 16)
                            p_second = int(data[pack_length*2 - 10:pack_length*2 -8], 16) << 24 | int(data[pack_length*2 - 8:pack_length*2-6], 16) << 16 | \
                                       int(data[pack_length*2 - 6:pack_length*2 -4], 16) << 8 | int(data[pack_length*2 - 4 : pack_length*2 -2], 16)
                            new_pack_time_stamp = round(second + p_second * 232 / 1e12, 3)
                        elif data[pack_length*2 - 22:pack_length*2 - 18] == '07B2':
                            second = int(data[pack_length*2 - 16:pack_length*2 - 14],16) * 24 * 60 * 60 + int(data[pack_length*2 - 14:pack_length*2 - 12],16) * 60 * 60 + int(data[pack_length*2 - 12:pack_length*2 - 10],16) * 60 + int(data[pack_length*2 - 10 :pack_length*2 - 8],16)
                            p_second = int(data[pack_length*2 - 8:pack_length*2 - 6],16) << 16 | int(data[pack_length*2 - 6:pack_length*2 - 4],16) << 8 | int(data[pack_length*2 - 4:pack_length*2 -2],16)
                            # print(second)
                            # print(p_second)
                            new_pack_time_stamp = second + p_second/1000000

                        # 数据包时间戳验证
                        pack_time_diff = new_pack_time_stamp - old_pack_time_stamp
                        # if pack_time_diff != 0:
                        #     print(1/pack_time_diff)
                        if (pack_time_diff > ((1 + timestamp_tolerance) / scan_frequency) or pack_time_diff < ((1 - timestamp_tolerance) / scan_frequency) ) and old_pack_time_stamp != 0:
                            error_time = time.ctime()
                            print(error_time)
                            print(str(lidar_ip) + ':' + 'pack time diff error,pack_time_diff=' + str(pack_time_diff))
                            print(new_pack_time_stamp)
                            print(old_pack_time_stamp)
                            print('new_pack_index' + str(new_pack_index))
                            print('old_pack_index' + str(old_pack_index))
                            print('new_scan_index' + str(new_scan_index))
                            print('old_scan_index' + str(old_scan_index))
                            print(str(len(data)))
                            print(data[:pack_length*2])
                            time.sleep(5)
                            stop_threads = 0
                            break
                        old_pack_time_stamp = new_pack_time_stamp
                    # 数据包连续性验证
                    if new_pack_index == 0:
                        if old_pack_index != pack_total-1 and old_pack_index != 0:
                            error_time = time.ctime()
                            print(error_time)
                            print(str(lidar_ip) + ':' + '1pack index error')
                            print('1new_pack_index' + str(new_pack_index))
                            print('1old_pack_index' + str(old_pack_index))
                            print(str(len(data)))
                            print(data[:pack_length * 2])
                            time.sleep(5)
                            stop_threads = 0
                            break
                        if new_scan_index == 0 and old_scan_index != -1 :
                            if old_scan_index != 65535:
                                error_time = time.ctime()
                                print(error_time)
                                print(str(lidar_ip) + ':' + '0scan index error')
                                print('0new_scan_index' + str(new_scan_index))
                                print('0old_scan_index' + str(old_scan_index))
                                print(str(len(data)))
                                print(data[:pack_length * 2])
                                time.sleep(5)
                                stop_threads = 0
                                break
                        elif new_scan_index != 0 and old_scan_index != -1:
                            if new_scan_index != old_scan_index + 1 and old_scan_index != -1:
                                error_time = time.ctime()
                                print(error_time)
                                print(str(lidar_ip) + ':' + '1scan index error')
                                print('1new_scan_index' + str(new_scan_index))
                                print('1old_scan_index' + str(old_scan_index))
                                print(str(len(data)))
                                print(data[:pack_length * 2])
                                time.sleep(5)
                                stop_threads = 0
                                break
                    else:
                        if new_pack_index != old_pack_index + 1 and old_pack_index != 0:
                            error_time = time.ctime()
                            print(error_time)
                            print(str(lidar_ip) + ':' + 'pack index error')
                            print('new_pack_index' + str(new_pack_index))
                            print('old_pack_index' + str(old_pack_index))
                            print('new_scan_index' + str(new_scan_index))
                            print('old_scan_index' + str(old_scan_index))
                            print(str(len(data)))
                            print(data[:pack_length * 2])
                            time.sleep(5)
                            stop_threads = 0
                            break
                        if new_scan_index != old_scan_index and old_scan_index != -1:
                            error_time = time.ctime()
                            print(error_time)
                            print(str(lidar_ip) + ':' + 'scan index error')
                            print('new_pack_index' + str(new_pack_index))
                            print('old_pack_index' + str(old_pack_index))
                            print('new_scan_index' + str(new_scan_index))
                            print('old_scan_index' + str(old_scan_index))
                            print(str(len(data)))
                            print(data[:pack_length * 2])
                            time.sleep(5)
                            stop_threads = 0
                            break
                    old_scan_index = new_scan_index
                    old_pack_index = new_pack_index
                    # 扫描频率验证
                    # if int(data[24:28], 16) != scan_frequency * 100:
                    #     error_time = time.ctime()
                    #     print(error_time)
                    #     print(str(lidar_ip) + ':' + '扫描频率错误！')
                    #     print(data[:pack_length * 2])
                    #     time.sleep(5)
                    #     stop_threads = 0
                    #     break
                    # 角分辨率验证
                    if int(data[28:32], 16) != res * 10000:
                        error_time = time.ctime()
                        print(error_time)
                        print(str(lidar_ip) + ':' + '角分辨率错误！')
                        print(data[:pack_length * 2])
                        time.sleep(5)
                        stop_threads = 0
                        break
                    # 每圈采样点数验证
                    scan_point_num = int(data[32:36], 16)
                    if scan_point_num != samples_per_scan:
                        error_time = time.ctime()
                        print(error_time)
                        print(str(lidar_ip) + ':' + '每圈采样点数错误！')
                        print(scan_point_num)
                        print(samples_per_scan)
                        print(data[:pack_length * 2])
                        time.sleep(5)
                        stop_threads = 0
                        break
                    # 本包点数验证
                    pack_point_num = int(data[40:44], 16)
                    if pack_point_num * 4 + 33 != pack_length:
                        error_time = time.ctime()
                        print(error_time)
                        print(str(lidar_ip) + ':' + '本包点数与本包长度不符！')
                        print(data[:pack_length * 2])
                        time.sleep(5)
                        stop_threads = 0
                        break
                    # 首点索引判断

                    # 解析点云距离
                    if new_pack_index == 0:
                        cycle_index += 1
                        word_dis.clear()
                    point_data = data[44:pack_point_num*8 + 44]
                    word_tem = re.findall(r'\w{8}', point_data)
                    for word in word_tem:
                        word_a = int(word[:4], 16)
                        word_dis.append(word_a)
                    # 连续多帧距离全为零的验证
                    if pack_point_num + int(data[40:44], 16) == scan_point_num and len(word_dis) == scan_point_num:
                        if word_dis == [0 for _ in range(len(word_dis))]:
                            error_dis_times += 1
                        else:
                            error_dis_times = 0
                    if error_dis_times >= 2:
                        error_time = time.ctime()
                        print(error_time)
                        print(str(lidar_ip) + ':' + '连续多帧距离全为零！')
                        print(data[:pack_length * 2])
                        time.sleep(5)
                        stop_threads = 0
                        break
                    # 模拟精度框测试
                    if pack_point_num + int(data[36:40], 16) == scan_point_num and len(word_dis) == scan_point_num and precision_frame == 'true':
                        x_sum = []
                        y_sum = []
                        for i in range(len(word_dis)):
                            x = word_dis[i] * math.cos((res * i) / 360 * (2 * math.pi))
                            x_sum.append(x)
                            y = word_dis[i] * math.sin((res * i) / 360 * (2 * math.pi))
                            y_sum.append(y)
                        # print(x_sum)
                        # print(y_sum)
                        for i in range(len(word_dis)):
                            if 0 <= i < len(word_dis) / 4 or len(word_dis) * 3 / 4 <= i < len(word_dis):
                                dis_diff = abs(frame_length - abs(x_sum[i]))
                                if 30 < dis_diff <= 40:
                                    error_time = time.ctime()
                                    print(error_time)
                                    print('new_scan_index' + str(new_scan_index))
                                    print(str(lidar_ip) + ':' + '第' + str(i) + '个点距离误差超过30mm')
                                    print(data[: pack_length * 2])
                                    print(dis_diff)
                                    print(i)
                                elif dis_diff > 40:
                                    error_time = time.ctime()
                                    print(error_time)
                                    print('new_scan_index' + str(new_scan_index))
                                    print(str(lidar_ip) + ':' + '第' + str(i) + '个点距离误差超过40mm')
                                    print(data[:pack_length * 2])
                                    print(dis_diff)
                                    print(i)
                                    stop_threads = 0
                                    break
                            else:
                                dis_diff = abs(frame_length - abs(y_sum[i]))
                                if 30 < dis_diff <= 40:
                                    error_time = time.ctime()
                                    print(error_time)
                                    print('new_scan_index' + str(new_scan_index))
                                    print(str(lidar_ip) + ':' + '第' + str(i) + '个点距离误差超过30mm')
                                    print(data[: pack_length * 2])
                                    print(dis_diff)
                                    print(i)
                                elif dis_diff > 40:
                                    error_time = time.ctime()
                                    print(error_time)
                                    print('new_scan_index' + str(new_scan_index))
                                    print(str(lidar_ip) + ':' + '第' + str(i) + '个点距离误差超过40mm')
                                    print(data[:pack_length * 2])
                                    print(dis_diff)
                                    print(i)
                                    stop_threads = 0
                                    break
                    data = data[pack_length*2:]


if __name__ == "__main__":
    main_frequency = float(input('请输入转速:'))
    main_res = float(input('请输入角分辨率:'))
    main_scan_num = int(input('请输入扫描点数:'))
    # main_frequency = 15
    # main_res = float(0.3333)
    # main_scan_num = 811
    main_precision_frame = input('是否需要精度框测试，需要输入:true;不需要输入:false,或不输入')
    # main_precision_frame = 'false'
    main_frame_length = 0
    # main_pack_total = 7
    # main_test_times = 5000
    # main_timestamp_tolerance = 0.2
    if main_precision_frame == 'true':
        main_frame_length = int(input('请输入精度框边长（/mm）:'))
        main_frame_length = main_frame_length/2
    main_pack_total = int(input('请输入每圈包数:'))
    main_timestamp_tolerance = float(input('请输入包内时间戳富余量（一般为0.2）:'))
    main_ip = input('请输入IP,多个用"、"分割:')
    main_on_off_network = int(input('需要通断网压力测试输入1;需要连续取数压力测试输入0:'))
    main_test_times = 1
    if main_on_off_network == 1:
        main_test_times = int(input('请输入通断电测试的次数(一般2000次):'))
    # main_ip = '192.168.1.111'
    power_com = 'COM3'
    main_ip_list = main_ip.split("、")
    print(main_ip_list)
    # 设置子线程并启动
    threading_list = []
    stop_threads_dict = {}
    for i in main_ip_list:
        stop_threads_dict[i] = 1

    for i in range(len(main_ip_list)):
        threading_list.append(threading.Thread(target=test, args=(main_ip_list[i], main_timestamp_tolerance, main_pack_total, main_test_times, main_frame_length, main_precision_frame,main_frequency,main_res,main_scan_num,main_on_off_network,stop_threads_dict[main_ip_list[i]],power_com)))

    for i in threading_list:
        i.start()

    for i in threading_list:
        i.join()

    input("所有线程均已结束，可截图保存后按任意键退出...")

