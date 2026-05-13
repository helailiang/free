import socket
import time


class RadarDataProcessor:
    def __init__(self, host='192.168.1.111', port=2111):
        self.host = host
        self.port = port
        self.socket = None
        self.header_size = 6
        self.block_size = 8
        self.target_packet_size = 23000
        self.debug = False

    def _debug_print(self, message):
        if self.debug:
            print(message)

    def connect_radar(self):
        """连接雷达 - 优化连接速度"""
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            # 优化socket设置
            self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)  # 禁用Nagle算法
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 128 * 1024)  # 增大接收缓冲区到128KB
            self.socket.settimeout(10)
            self.socket.connect((self.host, self.port))
            print(f" 雷达连接成功: {self.host}:{self.port}")
            return True
        except Exception as e:
            print(f" 连接失败: {e}")
            return False

    def send_command(self, command_hex='02 02 02 02 00 09 02 64 77'):
        """发送指令到雷达 - 优化发送速度"""
        try:
            cmd_rawdata = command_hex.replace(' ', '')
            command_bytes = bytes.fromhex(cmd_rawdata)
            self.socket.send(command_bytes)

            # 快速接收回复，设置短超时
            self.socket.settimeout(0.5)
            try:
                response = self.socket.recv(10)
            except socket.timeout:
                # 回复超时是正常的，继续执行
                pass
            return True
        except Exception as e:
            print(f"✗ 发送指令失败: {e}")
            return False

    def receive_radar_data_fast(self, required_total_bytes=None):
        """快速接收雷达数据 - 支持23000字节"""
        try:
            chunks = []
            total_size = 0
            start_time = time.perf_counter()
            last_data_time = None
            first_packet_timeout = 0.20
            idle_timeout = 0.03
            max_total_time = 0.45
            target_size = required_total_bytes or self.target_packet_size

            self.socket.settimeout(first_packet_timeout)

            while time.perf_counter() - start_time < max_total_time:
                try:
                    data = self.socket.recv(16384)
                    if data:
                        chunks.append(data)
                        total_size += len(data)
                        last_data_time = time.perf_counter()
                        self.socket.settimeout(idle_timeout)

                        if total_size >= target_size:
                            break
                    else:
                        break
                except socket.timeout:
                    if total_size == 0:
                        break
                    if last_data_time and (time.perf_counter() - last_data_time) >= idle_timeout:
                        break
                except BlockingIOError:
                    if total_size > 0:
                        break

            all_data = b"".join(chunks)
            self._debug_print(f"实际接收数据: {len(all_data)} 字节")
            return all_data
        except Exception as e:
            print(f" 接收数据失败: {e}")
            return None

    def hex2dec(self, string_num):
        """16进制数转10进制数函数"""
        return int(string_num.upper(), 16)

    def remove_header_fast(self, radar_data, header_size=6):
        """快速去掉头部"""
        if not radar_data or len(radar_data) <= header_size:
            return radar_data
        return radar_data[header_size:]

    def get_data_range_fast(self, radar_data, start_index, end_index):
        """快速处理数据范围"""
        if not radar_data:
            self._debug_print(" 无雷达数据可处理")
            return []

        if len(radar_data) <= self.header_size:
            self._debug_print(" 数据长度不足，无法去头")
            return []

        payload = memoryview(radar_data)[self.header_size:]
        block_count = len(payload) // self.block_size
        self._debug_print(f" 处理数据，总长度: {len(radar_data)} 字节")
        self._debug_print(f" 去掉头部后长度: {len(payload)} 字节")
        self._debug_print(f" 找到数据块数量: {block_count}")

        # 检查请求的索引范围是否在数据范围内
        if start_index >= block_count:
            print(f" 起始索引 {start_index} 超出数据范围 (0-{block_count - 1})")
            return []

        actual_end = min(end_index, block_count - 1)
        self._debug_print(f" 实际处理范围: {start_index}-{actual_end}")

        results = []

        for want_index in range(start_index, actual_end + 1):
            block_start = want_index * self.block_size
            block = payload[block_start:block_start + self.block_size]

            front_edge = int.from_bytes(block[0:2], byteorder='big', signed=False)
            back_edge = int.from_bytes(block[2:4], byteorder='big', signed=False)
            measured_distance = int.from_bytes(block[4:6], byteorder='big', signed=False)
            reflectivity = int.from_bytes(block[6:8], byteorder='big', signed=False)
            pulse_width = back_edge - front_edge

            results.append({
                'index': want_index,
                'measured_distance': measured_distance,
                'front_edge': front_edge,
                'back_edge': back_edge,
                'pulse_width': pulse_width,
                'reflectivity': reflectivity
            })

        self._debug_print(f" 成功解析: {len(results)} 个数据点")
        return results

    def has_consecutive_qualified_points(self, results, max_distance, consecutive_count=3):
        """
        检查是否有连续consecutive_count个点符合距离要求
        """
        if not results or len(results) < consecutive_count:
            return False

        consecutive_hits = 0
        previous_index = None

        for point in results:
            index_value = point['index']
            qualified = point['measured_distance'] != 0 and point['measured_distance'] < max_distance

            if qualified and (previous_index is None or index_value == previous_index + 1):
                consecutive_hits += 1
            elif qualified:
                consecutive_hits = 1
            else:
                consecutive_hits = 0

            if consecutive_hits >= consecutive_count:
                return True

            previous_index = index_value

        return False

    def fast_single_measurement(self, start_index, end_index, max_distance=None):
        """快速单次测量"""
        self._debug_print(f"🔍 开始单次测量，索引范围: {start_index}-{end_index}")

        # 发送指令
        if not self.send_command():
            self._debug_print(" 发送指令失败")
            return None

        # 接收数据
        required_total_bytes = self.header_size + ((end_index + 1) * self.block_size)
        radar_data = self.receive_radar_data_fast(required_total_bytes=required_total_bytes)
        if not radar_data:
            self._debug_print(" 接收数据失败")
            return None

        # 快速处理数据
        results = self.get_data_range_fast(radar_data, start_index, end_index)

        if not results:
            self._debug_print(" 数据处理失败")
            return None

        self._debug_print(f"原始数据点数: {len(results)}")

        # 应用过滤条件
        if max_distance:
            filtered_results = [
                r for r in results
                if r['measured_distance'] < max_distance and r['measured_distance'] > 10
            ]

            # 检查是否有连续3个符合要求的点
            has_consecutive = self.has_consecutive_qualified_points(results, max_distance, 3)
            self._debug_print(f"过滤后数据点数: {len(filtered_results)}")
            self._debug_print(f" 连续条件满足: {has_consecutive}")
        else:
            filtered_results = results
            has_consecutive = False
            self._debug_print(" 未启用距离过滤")

        return {
            'total_count': len(results),
            'filtered_count': len(filtered_results),
            'results': filtered_results,
            'has_consecutive_qualified': has_consecutive,  # 新增字段，表示是否有连续3个符合要求的点
            'consecutive_condition_met': has_consecutive  # 兼容性字段
        }

    def batch_measurement_fast(self, start_index, end_index, max_distance, iterations=100):
        """
        快速批量测量
        """
        print(f"\n 开始快速批量测量")
        print(f" 测量参数:")
        print(f"   索引范围: {start_index} - {end_index}")
        if max_distance:
            print(f"   过滤条件: 10 < 测量距离 < {max_distance}")
            print(f"   有效圈判定: 存在连续3个符合距离要求的点")
        print(f"   测量次数: {iterations} 次")
        print(f"   目标数据量: 23000 字节")
        print("=" * 60)

        all_measurements = []
        success_count = 0
        qualified_circles = 0  # 符合连续条件的圈数
        total_time = 0

        # 预热连接
        print(" 预热连接...")
        for _ in range(2):
            self.fast_single_measurement(start_index, end_index, max_distance)

        start_time = time.time()

        for i in range(iterations):
            iteration_start = time.time()

            try:
                result = self.fast_single_measurement(start_index, end_index, max_distance)

                if result and result['total_count'] > 0:
                    all_measurements.append({
                        'iteration': i + 1,
                        'total_count': result['total_count'],
                        'filtered_count': result['filtered_count'],
                        'results': result['results'],
                        'has_consecutive_qualified': result['has_consecutive_qualified']
                    })
                    success_count += 1

                    # 统计符合连续条件的圈数
                    if result['has_consecutive_qualified']:
                        qualified_circles += 1

                    iteration_time = time.time() - iteration_start
                    total_time += iteration_time

                    # 降低进度输出频率，避免控制台刷新成为瓶颈
                    if (i + 1) % 10 == 0 or i == 0:
                        ratio = result['filtered_count'] / result['total_count'] * 100
                        consecutive_status = "✅" if result['has_consecutive_qualified'] else "❌"
                        print(
                            f"📈 第 {i + 1:3d}/{iterations} 次 - 符合: {result['filtered_count']:3d}/{result['total_count']:3d} ({ratio:5.1f}%) - 连续: {consecutive_status} - 耗时: {iteration_time * 1000:.0f}ms")

            except Exception as e:
                print(f" 第 {i + 1} 次测量异常: {e}")
                continue

        total_elapsed = time.time() - start_time
        return all_measurements, success_count, qualified_circles, total_elapsed

    def print_fast_statistics(self, all_measurements, iterations, total_time, qualified_circles):
        """打印快速测量统计结果"""
        print("\n" + "=" * 70)
        print(" 快速批量测量统计结果")
        print("=" * 70)

        if not all_measurements:
            print(" 没有有效测量数据")
            return

        total_points_all = sum(m['total_count'] for m in all_measurements)
        filtered_points_all = sum(m['filtered_count'] for m in all_measurements)
        total_circles = len(all_measurements)

        overall_ratio = filtered_points_all / total_points_all * 100 if total_points_all > 0 else 0
        qualified_ratio = qualified_circles / total_circles * 100 if total_circles > 0 else 0

        print(f" 测量次数: {iterations} 次")
        print(f" 成功测量: {total_circles} 次")
        print(f" 有效圈数: {qualified_circles} 次 (连续3个点符合要求)")
        print(f"有效圈比例: {qualified_ratio:.2f}%")
        print(f"总数据点数: {total_points_all}")
        print(f" 符合条件点数: {filtered_points_all}")
        print(f" 总过滤比例: {overall_ratio:.2f}%")
        print(f"  总耗时: {total_time:.2f} 秒")
        print(f" 平均每次: {total_time / iterations * 1000:.1f} 毫秒")
        print(f" 测量频率: {iterations / total_time:.1f} 次/秒")

        # 显示每次测量的比例统计
        if all_measurements:
            ratios = [m['filtered_count'] / m['total_count'] * 100 for m in all_measurements if m['total_count'] > 0]
            if ratios:
                print(f"\n 详细统计:")
                print(f"   平均过滤比例: {sum(ratios) / len(ratios):.2f}%")
                print(f"   最小过滤比例: {min(ratios):.2f}%")
                print(f"   最大过滤比例: {max(ratios):.2f}%")

        print("=" * 70)

    def print_data_range_fast(self, results, start_index, end_index):
        """快速打印数据"""
        print(f"\n 数据输出 (索引 {start_index}-{end_index}):")
        print('索引-测量距离-前沿-后沿-脉宽-反射率')

        # 只显示前20个数据点，避免输出过多
        display_count = min(20, len(results))
        for result in results[:display_count]:
            print(
                f"{result['index']}-{result['measured_distance']}-{result['front_edge']}-{result['back_edge']}-{result['pulse_width']}-{result['reflectivity']}")

        if len(results) > display_count:
            print(f"... 还有 {len(results) - display_count} 个数据点未显示")

    def close(self):
        """关闭连接"""
        if self.socket:
            self.socket.close()


def get_user_input():
    """获取用户输入的参数"""
    # 获取索引范围
    while True:
        try:
            range_input = input("请输入要处理的索引范围 (格式: 开始-结束, 如 400-460): ").strip()
            if '-' in range_input:
                start_str, end_str = range_input.split('-')
                start_index = int(start_str.strip())
                end_index = int(end_str.strip())

                if start_index < 0 or end_index < 0:
                    print("❌ 索引不能为负数")
                    continue
                if start_index > end_index:
                    print("❌ 起始索引不能大于结束索引")
                    continue

                break
            else:
                print("❌ 请输入正确的格式，如: 400-460")
        except ValueError:
            print("❌ 请输入有效的数字")

    # 询问是否启用距离过滤
    use_filter = input("是否启用距离过滤? (y/n, 默认y): ").strip().lower() or 'y'
    max_distance = None

    if use_filter == 'y':
        while True:
            try:
                max_distance = int(
                    input("请输入最大测量距离 X (只显示 10 < 距离 < X 的数据, 默认500): ").strip() or "500")
                if max_distance <= 10:
                    print("❌ 距离值必须大于10")
                    continue
                break
            except ValueError:
                print("❌ 请输入有效的数字")

    # 获取测量次数
    while True:
        try:
            iterations = int(input("请输入测量次数 (默认50次): ").strip() or "50")
            if iterations <= 0:
                print("❌ 测量次数必须大于0")
                continue
            break
        except ValueError:
            print("❌ 请输入有效的数字")

    return start_index, end_index, max_distance, iterations


def main():
    print(" 雷达数据快速批量测量程序")
    print(" 支持23000字节数据接收")
    print("=" * 50)

    # 配置参数
    HOST = '192.168.0.240'
    PORT = 2111

    # 获取用户输入
    print("\n 请设置测量参数")
    start_index, end_index, max_distance, iterations = get_user_input()

    print(f"\n 测量设置:")
    print(f"   索引范围: {start_index} - {end_index}")
    if max_distance:
        print(f"   过滤条件: 10 < 测量距离 < {max_distance}")
        print(f"   有效圈判定: 存在连续3个符合距离要求的点")
    else:
        print(f"   过滤条件: 无")
    print(f"   测量次数: {iterations} 次")
    print(f"   目标数据: 23000 字节")
    print(f"    使用快速模式")

    # 创建处理器
    radar = RadarDataProcessor(HOST, PORT)

    try:
        # 连接雷达
        print("\n1. 连接雷达...")
        if not radar.connect_radar():
            print("❌ 程序终止")
            return

        # 开始快速批量测量
        print("\n2. 开始快速批量测量...")

        all_measurements, success_count, qualified_circles, total_time = radar.batch_measurement_fast(
            start_index, end_index, max_distance, iterations
        )

        # 打印统计结果
        radar.print_fast_statistics(all_measurements, iterations, total_time, qualified_circles)

        # 显示最后一次测量的数据（限制数量）
        if all_measurements:
            last_measurement = all_measurements[-1]
            if last_measurement['results']:
                print(f"\n📋 最后一次测量数据 (前20个点):")
                radar.print_data_range_fast(last_measurement['results'], start_index, end_index)

    except KeyboardInterrupt:
        print("\n️ 用户中断程序")
    except Exception as e:
        print(f"\n 程序执行出错: {e}")
    finally:
        radar.close()
        print("\n 程序结束")


if __name__ == '__main__':
    main()
