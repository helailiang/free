#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
H2（H1E0-02A 文本协议）单次取数 + 一圈点数统计。

依据《H1E0-02A 产品说明书（V2.0）》表 4-2、4.2.1 登录、4.2.22 请求单次数据
（指令号 0x30；应答操作码 0x12；点云分包，参数 6 为一帧总点数、参数 8 为本包点数）。

与 newpre_resolution_gui_test.py 并列；不修改该文件。本脚本走 H2 单次点云，
不经 H1 标定二进制取数（02 64 77）。

仓库根目录需在 sys.path 中以便导入 libs.protocols.h2_txt_parse。

运行（在「码盘补偿」目录或项目根）:
  python h2_single_scan_pointcount.py
  python h2_single_scan_pointcount.py --ip 192.168.1.111 --export-csv out.csv

依赖: 标准库；协议实现见同目录 h2_radar_client.py（含 libs）。
"""
from __future__ import annotations

import argparse
import csv
import socket
import sys
import time
from pathlib import Path
from typing import Any

# 码盘补偿/ -> 项目根
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from h2_radar_client import (  # noqa: E402
    H2_SINGLE_SCAN_REQUEST,
    build_login_frame,
    drain_socket,
    merge_h2_points,
    recv_pointcloud_after_single_request,
)


def merge_points(packets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return merge_h2_points(packets)


def main() -> int:
    p = argparse.ArgumentParser(description="H2 单次取数（4.2.22）并统计一圈点数")
    p.add_argument("--ip", default="192.168.1.111", help="雷达 IP（表 4-1 默认网段可改）")
    p.add_argument("--port", type=int, default=2111, help="TCP 端口，默认 2111")
    p.add_argument(
        "--login-frame-hex",
        default="",
        help="覆盖默认登录整帧 hex；为空则用 --permission/--password-hex 组帧",
    )
    p.add_argument(
        "--permission",
        type=lambda x: int(x, 0),
        default=0x03,
        help="登录权限字节（说明书示例 0x03）",
    )
    p.add_argument(
        "--password-hex",
        default="F4724744",
        help="登录密码 4 字节 hex，默认说明书示例",
    )
    p.add_argument("--scan-start-deg", type=float, default=-45.0, help="扫描起始角（与点索引→角度一致）")
    p.add_argument("--idle-s", type=float, default=0.2, help="无新数据判空闲秒数")
    p.add_argument("--max-wait-s", type=float, default=3.0, help="最长接收窗口秒")
    p.add_argument("--export-csv", default="", help="可选：合并后的点写入 CSV（索引,角度,距离mm,反射率）")
    args = p.parse_args()

    if args.login_frame_hex.strip():
        login_frame = bytes.fromhex(args.login_frame_hex.replace(" ", ""))
    else:
        pwd = bytes.fromhex(args.password_hex.replace(" ", ""))
        if len(pwd) != 4:
            print("--password-hex 须为 8 个 hex 字符（4 字节）", file=sys.stderr)
            return 1
        login_frame = build_login_frame(int(args.permission), pwd)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.settimeout(5.0)
    try:
        sock.connect((args.ip, int(args.port)))
    except OSError as e:
        print(f"连接失败: {e}", file=sys.stderr)
        return 1

    try:
        drain_socket(sock)
        sock.sendall(login_frame)
        login_resp = sock.recv(4096)
        if not login_resp:
            print("登录无应答", file=sys.stderr)
            return 1
        # 4.2.1 成功应答含 12 01 01（不严格校验整帧，避免粘包）
        if b"\x12\x01\x01" not in login_resp:
            print(f"登录可能失败，首包 hex 前缀: {login_resp[:32].hex()}", file=sys.stderr)
        drain_socket(sock)

        sock.sendall(H2_SINGLE_SCAN_REQUEST)
        carry = bytearray()
        packets = recv_pointcloud_after_single_request(
            sock,
            carry,
            idle_s=float(args.idle_s),
            max_total_s=float(args.max_wait_s),
            scan_start_deg=float(args.scan_start_deg),
        )
    finally:
        try:
            sock.close()
        except OSError:
            pass

    if not packets:
        print("未收到 4.2.22 点云应答（指令 0x30）。", file=sys.stderr)
        if carry:
            print(f"缓冲残留 {len(carry)} 字节: {bytes(carry)[:64].hex()}…", file=sys.stderr)
        return 2

    meta0 = packets[0]
    circle_n = int(meta0["h2_points_per_circle"])
    angle_res = float(meta0["h2_angle_resolution_deg"])
    scan_cnt = int(meta0["scan_cnt"])
    sum_packet_n = sum(int(x["point_count"]) for x in packets)
    merged = merge_points(packets)
    idxs = [int(x["point_index"]) for x in merged]
    idx_min = min(idxs) if idxs else None
    idx_max = max(idxs) if idxs else None

    print("=== H2 单次取数（4.2.22）一圈统计 ===")
    print(f"依据: 说明书表 4-2、4.2.22（请求/应答指令号 0x30；应答操作码 0x12）")
    print(f"雷达: {args.ip}:{args.port}")
    print(f"扫描序号 scan_cnt: {scan_cnt}")
    print(f"应答头「一帧数据点云数」参数 6: {circle_n}")
    print(f"应答头角分辨率×10000 参数 5: {angle_res} °/点")
    print(f"收到点云分包数: {len(packets)}")
    print(f"各包点数之和 ΣN(参数8): {sum_packet_n}")
    print(f"合并后实际点数: {len(merged)}")
    if idx_min is not None:
        print(f"合并点全局索引范围: {idx_min} … {idx_max}（跨度 {idx_max - idx_min + 1}）")
    if sum_packet_n != circle_n:
        print(f"提示: ΣN 与头字段「一帧点云数」不一致（可能未收全或设备分包与头不同步）。")
    if len(merged) != circle_n:
        print(f"提示: 合并点数与头字段「一帧点云数」不一致。")

    out_csv = args.export_csv.strip()
    if out_csv:
        path = Path(out_csv)
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["point_index", "angle_deg", "distance_mm", "reflectivity", "packet_num"])
            for pkt in packets:
                pn = int(pkt.get("h2_packet_num", 0))
                for pt in pkt.get("points") or []:
                    w.writerow(
                        [
                            int(pt["point_index"]),
                            f"{float(pt['angle_deg']):.6f}",
                            int(pt["r_mm"]),
                            int(pt["reflectivity"]),
                            pn,
                        ]
                    )
        print(f"已导出 CSV: {path.resolve()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
