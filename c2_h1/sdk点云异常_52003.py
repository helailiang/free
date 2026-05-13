#!/usr/bin/env python3
"""
C2/C3/H1 SDK UDP JSON：连续监测每圈点云的雷达状态 (lidar_state) 与点云状态 (cloud_error)。

与 readme 中 UDP 消息字段一致：lidar_state=1 正常，cloud_error=0 正常、1 点云异常（遮挡等场景可观察变化）。
"""
import argparse
import csv
import json
import os
import socket
import sys
import threading
import time
import re
from datetime import datetime
from typing import Optional

# ======================
# 配置区
# ======================
LIDARS = {
    # "Lidar01": 52001,
    "Lidar02": 52003,
}

STATE_TIMEOUT = 5  # 秒：超过该时间未收到数据，认为掉线

# ======================
# 全局状态
# ======================
lidar_status = {}
lock = threading.Lock()
_csv_lock = threading.Lock()
_csv_file = None
_csv_writer = None
_cli_args = None

# 运行会话：起止时间（脚本启动时设）与成功解析的每圈统计
_session_start: Optional[datetime] = None
_session_stats: dict = {
    "total": 0,
    "lidar_ok": 0,
    "lidar_bad": 0,
    "cloud_ok": 0,
    "cloud_bad": 0,
}
_exit_summary_printed = False


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(msg, *, file=sys.stdout):
    print(f"[{now()}] {msg}", flush=True, file=file)


def describe_lidar_state(v):
    if v == 1:
        return "雷达正常"
    return f"雷达故障(码={v})"


def describe_cloud_error(v):
    if v == 0:
        return "点云正常"
    if v == 1:
        return "点云异常"
    return f"点云状态未知(码={v})"


def open_csv_log(path: str) -> None:
    global _csv_file, _csv_writer
    write_header = (not os.path.exists(path)) or (os.path.getsize(path) == 0)
    _csv_file = open(path, "a", encoding="utf-8", newline="")
    _csv_writer = csv.writer(_csv_file)
    if write_header:
        _csv_writer.writerow(
            ["time_local", "Lidar_id", "frame_seq", "lidar_state", "cloud_error", "note"]
        )
    _csv_file.flush()


def append_csv_row(
        lidar_id: str,
        frame_seq,
        lidar_state: int,
        cloud_error: int,
        note: str,
) -> None:
    if _csv_writer is None:
        return
    with _csv_lock:
        _csv_writer.writerow(
            [now(), lidar_id, frame_seq, lidar_state, cloud_error, note]
        )
        if _csv_file:
            _csv_file.flush()


def to_int_field(x, default: int = -1) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return default


def should_print_per_scan_to_console(lidar_state: int, cloud_error: int) -> bool:
    if _cli_args is None:
        return True
    if _cli_args.no_per_scan:
        return False
    if _cli_args.abnormal_only:
        return (lidar_state != 1) or (cloud_error != 0)
    return True


def parse_and_update(data, addr):
    try:
        cleaned = clean_json_units(data)
        # print(cleaned)
        msg = json.loads(cleaned)
        # print(msg)
        lidar_id = msg.get("Lidar_id", "UNKNOWN")
        lidar_state = msg.get("lidar_state", -1)
        cloud_error = msg.get("cloud_error", -1)
        frame_seq = msg.get("frame_seq", "")
        time_stamp = msg.get("time_stamp", "")

        ls = to_int_field(lidar_state)
        ce = to_int_field(cloud_error)
        is_error = (ls != 1) or (ce != 0)
        s_lidar = describe_lidar_state(ls)
        s_cloud = describe_cloud_error(ce)
        note = f"{s_lidar};{s_cloud}"

        with lock:
            lidar_status[lidar_id] = {
                "error": is_error,
                "last_seen": time.time(),
                "lidar_state": ls,
                "cloud_error": ce,
                "frame_seq": frame_seq,
            }
            _session_stats["total"] += 1
            if ls == 1:
                _session_stats["lidar_ok"] += 1
            else:
                _session_stats["lidar_bad"] += 1
            if ce == 0:
                _session_stats["cloud_ok"] += 1
            else:
                _session_stats["cloud_bad"] += 1

        if should_print_per_scan_to_console(ls, ce):
            line = (
                f"{lidar_id} 圈序号={frame_seq} lidar_state={lidar_state} "
                f"cloud_error={cloud_error} | {s_lidar}；{s_cloud}"
            )
            if time_stamp != "":
                line = f"{line}  UTC_ts={time_stamp}"
            log(line)

        if _csv_writer is not None:
            append_csv_row(str(lidar_id), frame_seq, ls, ce, note)

    except Exception as e:
        log(f"[PARSE ERROR] {e}")


def udp_listener(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", port))

    log(f"Listening UDP {port}")

    while True:
        data, addr = sock.recvfrom(65535)
        # 先打印原始数据
        # print(f"Raw data: {data}")
        # print(f"Raw data string: {data.decode('utf-8', errors='ignore')}")
        parse_and_update(data, addr)


def clean_json_units(data):
    """清理JSON中的单位符号"""
    if isinstance(data, bytes):
        data_str = data.decode("utf-8", errors="ignore")
    else:
        data_str = data
    # 处理各种单位
    units = [
        (r'°', ''),  # 角度符号
        (r'hz', '', re.IGNORECASE),  # 频率
        (r'm/s', ''),  # 速度
        (r'km/h', ''),  # 速度
        (r'mm', ''),  # 长度
        (r'cm', ''),  # 长度
        (r'm', ''),  # 长度
    ]

    for pattern, replacement, *flags in units:
        flag = flags[0] if flags else 0
        if flag:
            data_str = re.sub(pattern, replacement, data_str, flags=flag)
        else:
            data_str = re.sub(pattern, replacement, data_str)

    # 清理数值和单位之间的空格：-45 ° -> -45
    data_str = re.sub(r'(-?\d+(?:\.\d+)?)\s+([a-zA-Z°]+)', r'\1', data_str)

    return data_str


def monitor():
    last_state = None

    while True:
        time.sleep(1)

        with lock:
            current = {}
            now_ts = time.time()

            for lidar, _ in LIDARS.items():
                info = lidar_status.get(lidar)

                if not info:
                    current[lidar] = "NO_DATA"
                    continue

                if now_ts - info["last_seen"] > STATE_TIMEOUT:
                    current[lidar] = "TIMEOUT"
                else:
                    current[lidar] = "ERROR" if info["error"] else "OK"

        # 聚合判断（断连 / 多雷达汇总），状态变化时打印
        all_error = all(v == "ERROR" for v in current.values())

        state_str = f"{current} => ALL_ERROR={all_error}"

        if state_str != last_state:
            log(f"[汇总] {state_str}")
            last_state = state_str


def parse_args():
    p = argparse.ArgumentParser(
        description="每圈输出 lidar_state / cloud_error，并汇总在线与异常（遮挡试验用）。"
    )
    p.add_argument(
        "--abnormal-only",
        action="store_true",
        help="仅当雷达非 1 或点云非 0 时打印该圈；正常圈不刷控制台（仍写 CSV 若开启）。",
    )
    p.add_argument(
        "--log-csv",
        metavar="FILE",
        default=None,
        help="将每圈记录追加到 CSV（与控制台过滤独立：默认仍写每圈；可配合重定向自管）。",
    )
    p.add_argument(
        "--no-per-scan",
        action="store_true",
        help="不逐圈打印，仅保留 [汇总] 与断连检测（类旧版行为）。",
    )
    return p.parse_args()


def _close_csv() -> None:
    global _csv_file, _csv_writer
    with _csv_lock:
        if _csv_file is not None:
            try:
                _csv_file.close()
            except OSError:
                pass
            _csv_file = None
            _csv_writer = None


def print_exit_summary() -> None:
    """脚本结束时打印：开始/结束时间、总圈数、雷达与点云正常/异常次数。"""
    global _exit_summary_printed
    if _exit_summary_printed:
        return
    _exit_summary_printed = True

    end = datetime.now()
    start = _session_start
    s = _session_stats

    lines = [
        "======== 运行结束汇总 ========",
    ]
    if start is not None:
        start_s = start.strftime("%Y-%m-%d %H:%M:%S")
        end_s = end.strftime("%Y-%m-%d %H:%M:%S")
        sec = (end - start).total_seconds()
        lines.append(f"  开始时间: {start_s}")
        lines.append(f"  结束时间: {end_s}")
        lines.append(f"  运行时长: {sec:.1f} 秒")
    else:
        lines.append("  （无会话开始时间）")
    lines.append(f"  总圈数(成功解析的 UDP 包): {s['total']}")
    lines.append(
        f"  雷达状态  正常(lidar_state=1): {s['lidar_ok']}  "
        f"异常(≠1): {s['lidar_bad']}"
    )
    lines.append(
        f"  点云状态  正常(cloud_error=0): {s['cloud_ok']}  "
        f"异常(≠0): {s['cloud_bad']}"
    )
    lines.append("============================")
    for line in lines:
        log(line)


def main():
    global _cli_args, _session_start, _session_stats, _exit_summary_printed
    _exit_summary_printed = False
    _session_stats = {
        "total": 0,
        "lidar_ok": 0,
        "lidar_bad": 0,
        "cloud_ok": 0,
        "cloud_bad": 0,
    }
    _session_start = datetime.now()
    _cli_args = parse_args()
    if _cli_args.log_csv:
        try:
            open_csv_log(_cli_args.log_csv)
        except OSError as e:
            log(f"无法打开 CSV: {e}", file=sys.stderr)
            sys.exit(1)

    log(
        "每圈状态监测："
        f"逐圈控制台={not _cli_args.no_per_scan}"
        f"；仅异常到控制台={_cli_args.abnormal_only}"
        f"；CSV={_cli_args.log_csv or '关'}"
    )

    for lidar, port in LIDARS.items():
        t = threading.Thread(target=udp_listener, args=(port,), daemon=True)
        t.start()

    try:
        monitor()
    except KeyboardInterrupt:
        log("收到退出信号 (Ctrl+C)，结束运行。")
    finally:
        print_exit_summary()
        _close_csv()


if __name__ == "__main__":
    main()
