from __future__ import annotations

"""
Y100SC 串口示例：握手后按协议发送相对位移（步进）指令。

相对运动由 `Y100SCClient.move(axis, direction, distance)` 完成：distance 为步长（脉冲），
direction 为 '+' 或 '-'；本脚本通过命令行指定步长，方向默认 '+'。
"""

import argparse

from y100sc_client import Y100SCClient, Y100SCSerialConfig


def main() -> int:
    # 解析串口、轴、相对运动的步长与方向；步长对应设备协议中的位移量（非负整数）。
    ap = argparse.ArgumentParser(description="按 md 协议调用 Y100SC 串口指令（示例）")
    ap.add_argument("--port", default="COM6", help="串口号，例如 COM1/COM3")
    ap.add_argument("--timeout", type=float, default=0.5, help="读写超时秒数")
    ap.add_argument("--axis", default="X", choices=["X", "Y", "Z", "r", "t", "T"], help="测试轴")
    # 旋转/平移的相对步长：与设备文档中的脉冲步进一致；未指定时沿用原示例 1000。
    ap.add_argument(
        "--steps",
        type=int,
        default=1000,
        metavar="N",
        help="相对运动步长（脉冲，非负）；对应 move 的 distance",
    )
    # 正方向为协议中的 '+'，负方向为 '-'；默认 + 符合常见“正向转一圈/走一步”习惯。
    ap.add_argument(
        "--direction",
        default="+",
        choices=["+", "-"],
        help="运动方向：+ 或 -，默认 +",
    )
    args = ap.parse_args()

    if args.steps < 0:
        ap.error("--steps 必须为非负整数")

    cfg = Y100SCSerialConfig(port=args.port, timeout_s=args.timeout)
    with Y100SCClient(cfg) as dev:
        dev.handshake()
        print("handshake: OK")

        # sp = dev.query_speed()
        # print(f"speed: {sp}")
        #
        # # 查询X坐标
        # pos = dev.query_pos(args.axis)
        # print(f"pos({args.axis}): {pos}")
        #
        # hs = dev.query_home_status()
        # print(f"home_status(bits): {hs}  # 顺序见 md：T2 T1 R Z Y X")
        #
        # # 下面这些会让电移台实际运动/归零，默认不自动执行：下·
        # dev.set_speed(100)
        # hs = dev.query_home_status()
        # print(f"home_status(bits): {hs}  # 顺序见 md：T2 T1 R Z Y X")
        # dev.home(args.axis)
        # hs = dev.query_home_status()
        # print(f"home_status(bits): {hs}  # 顺序见 md：T2 T1 R Z Y X")
        # 相对位移：axis + direction + steps，到位后设备回 OK（见 Y100SCClient.move）。
        dev.move(args.axis, args.direction, args.steps)
        print(f"move: axis={args.axis} direction={args.direction} steps={args.steps}")
        # # # 查询X坐标
        # dev.home("X")
        # pos = dev.query_pos(args.axis)
        # print(f"pos({args.axis}): {pos}")
        # print(dev.stop().rstrip("\n"))
        # dev.stop()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
