"""
H1 / C2 / C3 时间戳与圈统计统一脚本（输出形式对齐 `c2_h1/H1时间戳测试通用版本.py`）。

- H1、C2：沿用通用版 TCP 收包（第 5–6 字节大端为整包长度）、`parse_h1_timestamp` / `parse_c2_timestamp`、
  圈号/包号来自 `parse_frame_info`（与通用版一致）。
- C3：复用同目录 `c3_continuous_pointcloud_per_scan.py` 的 HOST-ARM 切帧与点云 payload 解析；
  时间戳取点云 1.5 头中 8 字节 `payload[4:12]`，用其中的 `parse_c3_timestamp`（与当前 C3 脚本一致）。

圈统计字段、终端表格与 CSV 列名与 H1 通用版保持一致。
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import socket
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


# ----- 动态加载同目录 C3 解析模块（避免工作目录影响 import） -----
def _load_c3_scan_module() -> Any:
    here = Path(__file__).resolve().parent
    path = here / "c3_continuous_pointcloud_per_scan.py"
    spec = importlib.util.spec_from_file_location("c3_continuous_pointcloud_per_scan", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 C3 模块: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# =============================================================================
# H1 / C2：与 `H1时间戳测试通用版本.py` 对齐
# =============================================================================


def parse_h1_timestamp(data: bytes, *, print_hex: bool = False) -> tuple[int, int, float]:
    if print_hex:
        print("===========" + data[-11:-1].hex().upper())
    current_seconds = int.from_bytes(data[-9:-5], byteorder="big")
    current_nanoseconds = int.from_bytes(data[-5:-1], byteorder="big")
    current_timestamp = current_seconds + current_nanoseconds / (2**32)
    return current_seconds, current_nanoseconds, current_timestamp


def parse_c2_timestamp(data: bytes) -> tuple[int, int, int, int, float]:
    test_time_us = int(data[-4:-1].hex().upper(), 16)
    test_time_s = int(data[-5:-4].hex().upper(), 16)
    test_time_m = int(data[-6:-5].hex().upper(), 16)
    test_time_h = int(data[-7:-6].hex().upper(), 16)
    current_timestamp = (test_time_h * 3600 + test_time_m * 60 + test_time_s) + test_time_us / 1_000_000
    return test_time_h, test_time_m, test_time_s, test_time_us, current_timestamp


def parse_frame_info_h1c2(data: bytes) -> tuple[int, int]:
    frame_number = int.from_bytes(data[9:11], byteorder="big")
    packet_number = data[11]
    return frame_number, packet_number


def save_to_csv(frame_stats: list[dict], radar_model: str, filename: str | None = None) -> str:
    import csv

    if filename is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"radar_frame_stats_{radar_model}_{timestamp}.csv"
    os.makedirs(os.path.dirname(filename) if os.path.dirname(filename) else ".", exist_ok=True)
    with open(filename, "w", newline="", encoding="utf-8") as csvfile:
        fieldnames = [
            "圈号",
            "总包数",
            "起始包号",
            "结束包号",
            "本圈最后一包时间戳",
            "与上圈时间差(ms)",
            "本圈持续时间(ms)",
            "状态",
        ]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        for stat in frame_stats:
            csv_stat = stat.copy()
            if csv_stat["与上圈时间差(ms)"] is None:
                csv_stat["与上圈时间差(ms)"] = ""
            if csv_stat["本圈持续时间(ms)"] is None:
                csv_stat["本圈持续时间(ms)"] = ""
            writer.writerow(csv_stat)
    print(f"圈统计信息已保存到: {filename}")
    return filename


def print_frame_table(frame_stats: list[dict]) -> None:
    if not frame_stats:
        print("暂无圈统计数据")
        return
    print("\n" + "=" * 100)
    print("圈数据统计表格")
    print("=" * 100)
    print(
        f"{'圈号':<8} {'总包数':<8} {'起始包号':<10} {'结束包号':<10} "
        f"{'与上圈时间差(ms)':<18} {'本圈时间(ms)':<15} {'状态':<10}"
    )
    print("-" * 100)
    for stat in frame_stats:
        frame_num = stat["圈号"]
        total_packets = stat["总包数"]
        start_packet = stat["起始包号"]
        end_packet = stat["结束包号"]
        time_diff = stat["与上圈时间差(ms)"]
        duration = stat["本圈持续时间(ms)"]
        status = stat["状态"]
        time_diff_str = f"{time_diff:.3f}" if time_diff is not None else "N/A"
        duration_str = f"{duration:.3f}" if duration is not None else "N/A"
        print(
            f"{frame_num:<8} {total_packets:<8} {start_packet:<10} {end_packet:<10} "
            f"{time_diff_str:<18} {duration_str:<15} {status:<10}"
        )
    print("=" * 100)


def recv_radar_packet(sock: socket.socket, buffer: bytearray) -> tuple[bytes | None, bytearray]:
    while len(buffer) < 6:
        chunk = sock.recv(1024)
        if not chunk:
            return None, buffer
        buffer += chunk

    frame_length = int.from_bytes(buffer[4:6], byteorder="big")

    while len(buffer) < frame_length:
        chunk = sock.recv(1024)
        if not chunk:
            return None, buffer
        buffer += chunk

    packet = bytes(buffer[:frame_length])
    del buffer[:frame_length]
    return packet, buffer


def run_h1_or_c2(
    *,
    radar_model: str,
    host: str,
    port: int,
    print_hex: bool,
) -> None:
    frame_data: defaultdict[int, dict] = defaultdict(dict)
    frame_stats: list[dict] = []
    last_frame_end_timestamp: float | None = None
    last_frame_number: int | None = None
    buffer = bytearray()

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect((host, port))
    s.settimeout(5)
    time.sleep(0.2)

    start_hex = "02 02 02 02 00 0A 02 31 01 46"
    stop_hex = "02 02 02 02 00 0A 02 31 00 45"
    s.send(bytes.fromhex(start_hex.replace(" ", "")))
    try:
        s.recv(1024)
    except OSError:
        pass

    data, buffer = recv_radar_packet(s, buffer)
    if not data:
        print("初始数据包接收失败")
        s.close()
        return

    frame_number, packet_number = parse_frame_info_h1c2(data)
    if radar_model == "H1":
        prev_seconds, prev_nanoseconds, prev_timestamp = parse_h1_timestamp(data, print_hex=print_hex)
        prev_h = prev_m = prev_s = prev_us = None  # type: ignore[assignment]
    else:
        prev_h, prev_m, prev_s, prev_us, prev_timestamp = parse_c2_timestamp(data)
        prev_seconds = prev_nanoseconds = None  # type: ignore[assignment]

    frame_data[frame_number][packet_number] = {"timestamp": prev_timestamp}
    print(f"初始圈号:{frame_number} 包号:{packet_number} 包长度:{len(data)}B")
    print("开始稳定监测...")
    print("-" * 80)

    packet_count = 0
    consecutive_errors = 0

    try:
        while True:
            data, buffer = recv_radar_packet(s, buffer)
            if not data:
                print("连接断开")
                break

            packet_count += 1
            pkt_len = len(data)
            current_frame_number, current_packet_number = parse_frame_info_h1c2(data)

            if radar_model == "H1":
                current_seconds, current_nanoseconds, current_timestamp = parse_h1_timestamp(
                    data, print_hex=print_hex
                )
                sec_diff = current_seconds - int(prev_seconds)  # type: ignore[arg-type]
                nsec_diff = current_nanoseconds - int(prev_nanoseconds)  # type: ignore[arg-type]
                if nsec_diff < 0:
                    sec_diff -= 1
                    nsec_diff += 2**32
                actual_interval = sec_diff + nsec_diff / (2**32)
                prev_seconds, prev_nanoseconds = current_seconds, current_nanoseconds
            else:
                current_h, current_m, current_s, current_us, current_timestamp = parse_c2_timestamp(data)
                actual_interval = current_timestamp - float(prev_timestamp)
                prev_h, prev_m, prev_s, prev_us = current_h, current_m, current_s, current_us
                prev_timestamp = current_timestamp

            print(
                f"包[{packet_count}] 圈{current_frame_number} 包{current_packet_number} "
                f"长度:{pkt_len}B 间隔:{actual_interval * 1000:.3f}ms"
            )
            frame_data[current_frame_number][current_packet_number] = {"timestamp": current_timestamp}

            if last_frame_number is not None and current_frame_number != last_frame_number:
                print(f"\n🔁 新圈开始: {last_frame_number} -> {current_frame_number}")
                if last_frame_number in frame_data:
                    pkts = frame_data[last_frame_number]
                    first_pkt = min(pkts.keys())
                    last_pkt = max(pkts.keys())
                    t_start = pkts[first_pkt]["timestamp"]
                    t_end = pkts[last_pkt]["timestamp"]

                    duration_ms = (t_end - t_start) * 1000
                    time_diff_ms = (t_end - last_frame_end_timestamp) * 1000 if last_frame_end_timestamp else None
                    last_frame_end_timestamp = t_end

                    stat = {
                        "圈号": last_frame_number,
                        "总包数": len(pkts),
                        "起始包号": first_pkt,
                        "结束包号": last_pkt,
                        "本圈最后一包时间戳": t_end,
                        "与上圈时间差(ms)": time_diff_ms,
                        "本圈持续时间(ms)": duration_ms,
                        "状态": "完整" if len(pkts) >= 12 else "不完整",
                    }
                    frame_stats.append(stat)
                    print(f"✅ 圈{last_frame_number} 统计：{len(pkts)}包 持续:{duration_ms:.3f}ms")

            last_frame_number = current_frame_number

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
        try:
            s.send(bytes.fromhex(stop_hex.replace(" ", "")))
        except OSError:
            pass
        time.sleep(0.1)
        s.close()
        print("Socket 已关闭")

        if frame_stats:
            print_frame_table(frame_stats)
            save_to_csv(frame_stats, radar_model)
            print(f"\n共统计 {len(frame_stats)} 圈数据")
        print(f"处理总包数：{packet_count}")


def run_c3(
    *,
    host: str,
    cmd_port: int,
    data_port: int,
    verify_crc: bool,
    print_ts_hex: bool,
) -> None:
    c3 = _load_c3_scan_module()
    START_STREAM_CMD = c3.START_STREAM_CMD
    STOP_STREAM_CMD = c3.STOP_STREAM_CMD
    iter_frames_from_stream = c3.iter_frames_from_stream
    PAYLOAD_TYPE_MSG = c3.PAYLOAD_TYPE_MSG
    parse_point_cloud_msg_payload = c3.parse_point_cloud_msg_payload
    parse_c3_timestamp = c3.parse_c3_timestamp

    frame_data: defaultdict[int, dict] = defaultdict(dict)
    frame_stats: list[dict] = []
    last_frame_end_timestamp: float | None = None
    last_frame_number: int | None = None

    cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    cmd_sock.settimeout(3.0)
    cmd_sock.connect((host, cmd_port))

    data_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    data_sock.settimeout(1.0)
    data_sock.connect((host, data_port))

    print(
        f"[INFO] C3 已连接 | host={host} cmd_port={cmd_port} data_port={data_port} verify_crc={verify_crc}"
    )
    cmd_sock.sendall(START_STREAM_CMD)
    try:
        cmd_sock.recv(1024)
    except OSError:
        pass

    prev_timestamp: float | None = None
    packet_count = 0
    consecutive_errors = 0

    try:
        for frame in iter_frames_from_stream(data_sock, verify_crc=verify_crc):
            if frame.payload_type != PAYLOAD_TYPE_MSG:
                continue
            msg = parse_point_cloud_msg_payload(frame.payload)
            if msg is None:
                continue

            scan_cnt = int(msg["scan_cnt"])
            packet_number = int(frame.seq_num)
            ts8 = bytes(frame.payload[4:12])
            parsed = parse_c3_timestamp(ts8)
            if parsed is None:
                continue
            _a, _b, current_timestamp = parsed

            if print_ts_hex:
                print("===========" + ts8.hex().upper())

            packet_count += 1
            pkt_len = int(frame.length)

            if prev_timestamp is None:
                actual_interval = 0.0
            else:
                actual_interval = float(current_timestamp) - float(prev_timestamp)
            prev_timestamp = float(current_timestamp)

            print(
                f"包[{packet_count}] 圈{scan_cnt} 包{packet_number} "
                f"长度:{pkt_len}B 间隔:{actual_interval * 1000:.3f}ms"
            )
            frame_data[scan_cnt][packet_number] = {"timestamp": current_timestamp}

            if last_frame_number is not None and scan_cnt != last_frame_number:
                print(f"\n🔁 新圈开始: {last_frame_number} -> {scan_cnt}")
                if last_frame_number in frame_data:
                    pkts = frame_data[last_frame_number]
                    first_pkt = min(pkts.keys())
                    last_pkt = max(pkts.keys())
                    t_start = pkts[first_pkt]["timestamp"]
                    t_end = pkts[last_pkt]["timestamp"]

                    duration_ms = (t_end - t_start) * 1000
                    time_diff_ms = (t_end - last_frame_end_timestamp) * 1000 if last_frame_end_timestamp else None
                    last_frame_end_timestamp = t_end

                    stat = {
                        "圈号": last_frame_number,
                        "总包数": len(pkts),
                        "起始包号": first_pkt,
                        "结束包号": last_pkt,
                        "本圈最后一包时间戳": t_end,
                        "与上圈时间差(ms)": time_diff_ms,
                        "本圈持续时间(ms)": duration_ms,
                        "状态": "完整" if len(pkts) >= 12 else "不完整",
                    }
                    frame_stats.append(stat)
                    print(f"✅ 圈{last_frame_number} 统计：{len(pkts)}包 持续:{duration_ms:.3f}ms")

            last_frame_number = scan_cnt

            if packet_count > 1:
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
        try:
            cmd_sock.sendall(STOP_STREAM_CMD)
        except OSError:
            pass
        try:
            data_sock.close()
        except OSError:
            pass
        try:
            cmd_sock.close()
        except OSError:
            pass

        if frame_stats:
            print_frame_table(frame_stats)
            save_to_csv(frame_stats, "C3")
            print(f"\n共统计 {len(frame_stats)} 圈数据")
        print(f"处理总包数：{packet_count}")


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="H1/C2/C3 时间戳统一测试（输出对齐 H1 通用版）")
    p.add_argument("--radar", choices=["H1", "C2", "C3"], required=True, help="雷达类型")
    p.add_argument("--host", default="192.168.0.240", help="雷达 IP（C3 可改；H1/C2 默认与通用版一致）")
    p.add_argument("--port", type=int, default=2111, help="H1/C2 TCP 端口（默认 2111）")
    p.add_argument("--cmd-port", type=int, default=50000, help="C3 命令端口")
    p.add_argument("--data-port", type=int, default=52000, help="C3 点云端口")
    p.add_argument("--no-crc", action="store_true", help="C3：不校验 CRC")
    p.add_argument(
        "--print-ts-hex",
        action="store_true",
        help="打印时间戳相关 hex（H1：等同通用版 payload[-11:-1)；C3：打印 8 字节 ts hex）",
    )
    return p


def main() -> None:
    args = build_argparser().parse_args()
    try:
        if args.radar in ("H1", "C2"):
            run_h1_or_c2(
                radar_model=args.radar,
                host=args.host,
                port=args.port,
                print_hex=args.print_ts_hex,
            )
        else:
            run_c3(
                host=args.host,
                cmd_port=args.cmd_port,
                data_port=args.data_port,
                verify_crc=not args.no_crc,
                print_ts_hex=args.print_ts_hex,
            )
    except Exception as e:
        print(f"程序异常：{e}")


if __name__ == "__main__":
    main()
