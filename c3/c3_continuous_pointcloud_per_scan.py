from __future__ import annotations

"""
C3 连续取数（TCP 52000）点云解析示例：按“每一圈(scan_cnt)”聚合并计算圈间时间差。

你将得到：
- 能下发“连续取数开/关（网口）”指令到 50000
- 能从 52000 持续接收 MSG 点云帧（msg_id=0x05），按 scan_cnt 聚合为一圈
- 每圈输出：scan_cnt、点数、时间戳（ns）、与上一圈的时间差（ms）

协议依据（`HOST-ARM通信协议.md`）：
- 1.1.2 Header：sof=0x5A；length 为小端 uint16；payload_type：CMD=0x00/ACK=0x01/MSG=0x02；
  seq_num；crc16 为 CRC16-MODBUS（对 Header 做校验）
- 1.1.4 Tail：crc32 为 Payload 校验（多项式 0x04C11DB7；实践等价 CRC32/IEEE；本工程按小端存储）
- 1.3.2.6 / 1.5：点云数据为 MSG，payload 中 msg_id=0x05，scan_cnt，data_type，time_type，timestamp(ns)，
  后跟 N 个点；点格式随 data_type 不同

注意：
- 点云一圈会被分成多包（协议 1.5）。本脚本按 scan_cnt 聚合多包为“一圈”。
- 每包里的 timestamp 代表“该包第一个点”的时间（协议 1.5）。本脚本以“每圈起始包(点云帧起始)”的 timestamp
  作为该圈时间戳；若没有起始标记，则退化为该圈看到的第一包 timestamp。
"""

import argparse
import socket
import struct
import time
from dataclasses import dataclass
from typing import Callable, Iterator


# ===== 时间戳解析（用于验证不同固件/时间源） =====
def parse_c3_timestamp(ts8: bytes) -> tuple[int, int, float] | None:
    """解析 H1/NTP 风格时间戳（大端）：seconds(uint32) + fraction(uint32, /2^32)。

    入参只需要传递“时间戳 8 字节”，函数内部再按 4 + 4 拆分。

    注意：这不是协议 1.5 里声明的 timestamp(ns)（该字段按协议是 uint64、单位 ns、且整体小端）。
    仅用于当实际抓包发现该字段实际为 H1/NTP 风格时间时做对照验证。
    """
    if len(ts8) != 8:
        return None
    current_seconds = int.from_bytes(ts8[4:8], byteorder="little", signed=False)
    current_fraction = int.from_bytes(ts8[0:4], byteorder="little", signed=False)
    current_timestamp = current_seconds + current_fraction / (2**32)
    return current_seconds, current_fraction, current_timestamp


def h1_timestamp_to_ns(h1: tuple[int, int, float] | None) -> int | None:
    if h1 is None:
        return None
    _sec, _frac, ts_s = h1
    return int(ts_s * 1_000_000_000.0)


# ===== 协议常量（依据 1.1.2 Header） =====
SOF = 0x5A
PAYLOAD_TYPE_CMD = 0x00
PAYLOAD_TYPE_ACK = 0x01
PAYLOAD_TYPE_MSG = 0x02

# Header 字段偏移（byte）
OFFSET_SOF = 0
OFFSET_LENGTH = 1  # uint16 little-endian
OFFSET_PAYLOAD_TYPE = 3
OFFSET_SEQ_NUM = 4
OFFSET_HEADER_CRC16 = 5  # uint16 little-endian
HEADER_LEN = 7  # sof(1)+length(2)+payload_type(1)+seq(1)+crc16(2)

# Tail（依据 1.1.4 Tail）
TAIL_LEN = 4  # crc32


# ===== 连续取数命令（协议附录示例） =====
# 连续取数开（网口）: 5A 0E 00 00 00 7E E5 02 00 01 EA 3D C2 8B
# 连续取数关:         5A 0E 00 00 00 7E E5 02 00 00 7C 0D C5 FC
START_STREAM_CMD = bytes.fromhex("5A 0E 00 00 00 7E E5 02 00 01 EA 3D C2 8B")
STOP_STREAM_CMD = bytes.fromhex("5A 0E 00 00 00 7E E5 02 00 00 7C 0D C5 FC")


def u16_le(buf: bytes | bytearray, offset: int) -> int:
    return int.from_bytes(buf[offset : offset + 2], "little", signed=False)


def crc16_modbus(data: bytes) -> int:
    """CRC16-MODBUS（用于 Header 校验），依据协议 1.1.2。"""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = ((crc >> 1) ^ 0xA001) if (crc & 1) else (crc >> 1)
    return crc & 0xFFFF


def crc32_ieee(data: bytes) -> int:
    """CRC32/IEEE（用于 Payload 校验），与协议 1.1.4 的多项式 0x04C11DB7 对应。"""
    # zlib.crc32 在 Python 标准库中是 CRC32/IEEE 的常用实现
    import zlib

    return zlib.crc32(data) & 0xFFFFFFFF


@dataclass(frozen=True)
class C3Frame:
    length: int
    payload_type: int
    seq_num: int
    payload: bytes


def parse_c3_frame(frame: bytes, *, verify_crc: bool = True) -> C3Frame | None:
    """解析单个完整帧（已经按 length 切割完成）。"""
    if len(frame) < HEADER_LEN + TAIL_LEN:
        return None
    if frame[OFFSET_SOF] != SOF:
        return None

    length = u16_le(frame, OFFSET_LENGTH)
    if length != len(frame):
        return None

    payload_type = frame[OFFSET_PAYLOAD_TYPE]
    seq_num = frame[OFFSET_SEQ_NUM] # 帧序号

    if verify_crc:
        header_wo_crc = frame[:OFFSET_HEADER_CRC16]  # sof..seq_num（5 字节）
        crc16_expected = u16_le(frame, OFFSET_HEADER_CRC16)
        crc16_actual = crc16_modbus(header_wo_crc)
        if crc16_actual != crc16_expected:
            return None

    payload = frame[HEADER_LEN:-TAIL_LEN]

    if verify_crc:
        crc32_expected = int.from_bytes(frame[-TAIL_LEN:], "little", signed=False)
        crc32_actual = crc32_ieee(payload)
        if crc32_actual != crc32_expected:
            return None

    return C3Frame(length=length, payload_type=payload_type, seq_num=seq_num, payload=payload)


def build_c3_frame(payload_type: int, seq_num: int, payload: bytes) -> bytes:
    """按协议 1.1.2/1.1.4 组一个完整帧（含 CRC16/CRC32，小端存储）。"""
    if not (0 <= payload_type <= 0xFF and 0 <= seq_num <= 0xFF):
        raise ValueError("payload_type/seq_num must be uint8")

    length = HEADER_LEN + len(payload) + TAIL_LEN
    if length > 1024:
        raise ValueError("frame too large (>1024 bytes)")

    header_wo_crc = bytes(
        [
            SOF,
            length & 0xFF,
            (length >> 8) & 0xFF,
            payload_type & 0xFF,
            seq_num & 0xFF,
        ]
    )
    crc16 = crc16_modbus(header_wo_crc).to_bytes(2, "little", signed=False)
    crc32 = crc32_ieee(payload).to_bytes(4, "little", signed=False)
    return header_wo_crc + crc16 + payload + crc32


def iter_frames_from_stream(
    sock: socket.socket,
    *,
    verify_crc: bool = True,
    recv_chunk: int = 4096,
    debug: bool = False,
    on_drop: Callable[[bytes], None] | None = None,
) -> Iterator[C3Frame]:
    """从 TCP 字节流中按 SOF/length 切帧并迭代输出。"""
    buffer = bytearray()
    total_bytes = 0
    while True:
        try:
            chunk = sock.recv(recv_chunk)
        except socket.timeout:
            # 让上层还能周期性输出 debug（或响应 Ctrl+C），而不是在这里异常退出
            continue
        except OSError:
            return
        if not chunk:
            return
        total_bytes += len(chunk)
        buffer.extend(chunk)

        while True:
            sof_pos = buffer.find(bytes([SOF]))
            if sof_pos < 0:
                # 没有 SOF，缓存过大则清掉，避免无限增长
                if len(buffer) > 1024 * 1024:
                    buffer.clear()
                break

            if sof_pos > 0:
                del buffer[:sof_pos]

            if len(buffer) < OFFSET_LENGTH + 2:
                break

            frame_len = u16_le(buffer, OFFSET_LENGTH)
            # 依据协议 1.1：一帧最大 1024 bytes（文档写明）
            if frame_len < HEADER_LEN + TAIL_LEN or frame_len > 1024:
                del buffer[0]
                continue

            if len(buffer) < frame_len:
                break

            raw = bytes(buffer[:frame_len])
            del buffer[:frame_len]

            parsed = parse_c3_frame(raw, verify_crc=verify_crc)
            if parsed is not None:
                yield parsed
            elif debug:
                # CRC 或长度不通过时，至少把首部打印出来便于对齐协议
                head = raw[: min(len(raw), 32)].hex(" ")
                print(f"[DROP] 丢弃帧（解析/CRC失败）| 帧长(len)={len(raw)} 头部预览(head)={head}")
                if on_drop is not None:
                    on_drop(raw)


@dataclass
class PointXYZ:
    x_mm: int
    y_mm: int
    z_mm: int
    reflectivity: int
    tag: int


@dataclass
class ScanAggregate:
    scan_cnt: int
    time_type: int
    timestamp_ns: int | None  # 以“起始包”为准；缺失则为第一包
    points: list[PointXYZ]
    first_seq_num: int | None
    last_seq_num: int | None


def parse_point_cloud_msg_payload(payload: bytes) -> dict | None:
    """解析 MSG 点云 payload（msg_id=0x05），并返回结构化字段。

    依据协议 1.5（点云数据）：
    msg_id(0) / scan_cnt(1) / data_type(2) / time_type(3) / timestamp(4..11) / points(12..)
    """
    if len(payload) < 1:
        return None
    msg_id = payload[0]
    if msg_id != 0x05:
        return None

    # 严格按 1.5：最小头部为 1+1+1+1+8
    if len(payload) < 1 + 1 + 1 + 1 + 8:
        return None

    scan_cnt = payload[1]
    data_type = payload[2]
    time_type = payload[3]
    timestamp_ns = int.from_bytes(payload[4:12], "little", signed=False)
    points_blob = payload[12:]

    # 额外：对“同一段 8 字节”按 H1/NTP 风格（大端 sec+frac）再解一次，用于对照验证
    ts8 = payload[4:12]
    h1 = parse_c3_timestamp(ts8)
    timestamp_h1_ns = h1_timestamp_to_ns(h1)

    return {
        "layout": "v1.5",
        "msg_id": msg_id,
        "scan_cnt": scan_cnt,
        "data_type": data_type,
        "time_type": time_type,
        "timestamp_ns": timestamp_ns,
        "timestamp_h1_ns": timestamp_h1_ns,
        "timestamp_h1_dbg": ts8.hex().upper(),
        "points_blob": points_blob,
    }


def parse_points_xyz(points_blob: bytes) -> list[PointXYZ]:
    """解析直角坐标点云（data_type=0x01 时的点格式），依据协议 1.5。

    每点：
    - x int32
    - y int32
    - z int32
    - reflectivity uint16
    - tag uint8
    合计 4+4+4+2+1=15 字节/点
    """
    point_size = 15
    if len(points_blob) < point_size:
        return []
    n = len(points_blob) // point_size
    out: list[PointXYZ] = []
    offset = 0
    for _ in range(n):
        x, y, z, refl = struct.unpack_from("<iiiH", points_blob, offset)
        tag = points_blob[offset + 14]
        out.append(PointXYZ(x_mm=x, y_mm=y, z_mm=z, reflectivity=refl, tag=tag))
        offset += point_size
    return out


def normalize_c3_angle_deg(angle_deg: float) -> float:
    """与 `M盲区测试C3.py` 保持一致的角度归一化逻辑。"""
    if angle_deg > 270.0:
        return angle_deg - 360.0
    return angle_deg


@dataclass
class PointPolar2D:
    """极坐标二维点（data_type=0x03），依据协议 1.5 的“极坐标数据格式”。"""

    r_mm: int
    angle_deg: float
    reflectivity: int
    tag: int
    seq_num: int
    scan_cnt: int
    timestamp_ns: int
    frame_index: int
    point_index: int


def parse_points_polar_2d(points_blob: bytes) -> list[tuple[int, int, int, int]]:
    """解析极坐标二维点（data_type=0x03）。

    依据协议 `HOST-ARM通信协议.md` 的 `1.5 点云数据 -> 极坐标数据格式`：
    - r uint16（mm）
    - angle uint16（0..36000, 单位 0.01°）
    - reflectivity uint16
    - tag uint8
    合计 2+2+2+1=7 字节/点

    返回 (r_mm, angle_raw_u16, reflectivity, tag) 的列表。
    """
    point_size = 7
    if len(points_blob) < point_size:
        return []
    n = len(points_blob) // point_size
    out: list[tuple[int, int, int, int]] = []
    offset = 0
    for _ in range(n):
        r_mm, angle_raw, refl = struct.unpack_from("<HHH", points_blob, offset)
        tag = points_blob[offset + 6]
        out.append((r_mm, angle_raw, refl, tag))
        offset += point_size
    return out


def run(
    host: str,
    *,
    cmd_port: int = 50000,
    data_port: int = 52000,
    verify_crc: bool = True,
    max_scans: int = 0,
    connect_timeout_s: float = 3.0,
    debug: bool = False,
    timestamp_source: str = "payload_ns",
) -> None:
    # 1) 命令端口：下发“连续取数开”
    cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    cmd_sock.settimeout(connect_timeout_s)
    cmd_sock.connect((host, cmd_port))

    # 2) 数据端口：接收点云 MSG
    data_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    data_sock.settimeout(1.0)
    data_sock.connect((host, data_port))

    # 让运行过程“可视化”：一启动就打印连接信息，避免“没输出不知道在干嘛”
    print(
        "[INFO] 连接成功 | "
        f"雷达IP(host)={host} 命令端口(cmd_port)={cmd_port} 点云端口(data_port)={data_port} "
        f"校验CRC(verify_crc)={verify_crc}"
    )

    cmd_sock.sendall(START_STREAM_CMD)
    try:
        ack = cmd_sock.recv(1024)  # ACK 可按需解析；此处只确保链路有回包
        if debug:
            print(f"[ACK] 连续取数开(start_stream) 回包长度={len(ack)}B 头部预览={ack[:32].hex(' ')}")
    except socket.timeout:
        print("[WARN] 连续取数开(start_stream) 超时未收到ACK（继续接收点云）")

    # ===== 运行态变量说明（便于读代码）=====
    # scan_cnt：协议 1.5 定义的“点云帧计数”，每扫描一圈 +1（uint8，0..255 循环）
    # data_type：点云数据类型（协议 1.5）；本脚本主要解析 0x03：极坐标二维
    # time_type/timestamp_ns：协议 1.5 的时间戳类型与时间戳（ns）；每包 timestamp 表示“该包第一个点”的时间
    #
    # 聚合策略（参考 `M盲区测试C3.py`）：
    # - 同一圈(scan_cnt 相同)会被拆成多包，先暂存到 current_points
    # - scan_cnt 变化：认为上一圈结束，输出统计，并清空开始新一圈

    # 参考 `M盲区测试C3.py`：按 scan_cnt 聚合为“单圈”，并计算相邻两圈 timestamp 差值
    current_scan_cnt: int | None = None
    current_points: list[PointPolar2D] = []
    current_seq_nums: list[int] = []
    current_frame_count = 0
    current_timestamp_ns: int | None = None
    current_time_type: int | None = None
    frame_index = 0

    previous_scan_cnt: int | None = None
    previous_timestamp_ns: int | None = None
    completed = 0

    seen_frames = 0
    seen_msg05 = 0
    started_host_ns = time.time_ns()
    last_status_print_ns = started_host_ns

    # ===== debug 统计：用于区分“过滤导致跳号” vs “解析失败/丢帧” =====
    dropped_parse_fail = 0
    skipped_payload_type_non_msg = 0
    skipped_msg_parse_failed = 0
    skipped_msg_id_non_05 = 0
    skipped_data_type_other = 0
    skipped_points_empty = 0

    def on_drop_frame(_raw: bytes) -> None:
        nonlocal dropped_parse_fail
        dropped_parse_fail += 1

    def maybe_print_debug_stats(force: bool = False) -> None:
        if not debug:
            return
        if not force and (seen_frames % 500 != 0):
            return
        print(
            "[DBG-STAT] 过滤/丢帧统计 | "
            f"已收帧数(frames)={seen_frames} 解析失败丢弃(drop_parse_fail)={dropped_parse_fail} "
            f"非MSG跳过(skip_non_msg)={skipped_payload_type_non_msg} "
            f"点云payload解析失败(skip_parse_failed)={skipped_msg_parse_failed} "
            f"msg_id!=0x05跳过(skip_msgid!=05)={skipped_msg_id_non_05} "
            f"data_type非0x03跳过(skip_data_type_other)={skipped_data_type_other} "
            f"点解析为空(skip_points_empty)={skipped_points_empty}"
        )

    def maybe_print_status() -> None:
        """每隔一段时间打印一次运行状态，避免长期无输出。"""
        nonlocal last_status_print_ns
        now = time.time_ns()
        if now - last_status_print_ns < 2_000_000_000:  # 2s
            return
        last_status_print_ns = now
        elapsed_s = (now - started_host_ns) / 1_000_000_000.0
        print(
            f"[STAT] 运行状态 | 运行时长={elapsed_s:6.1f}s "
            f"已收帧数(frames)={seen_frames} 点云消息(msg_id=0x05)={seen_msg05} "
            f"当前圈号(scan_cnt)={current_scan_cnt if current_scan_cnt is not None else '-'} "
            f"本圈包数(packets)={current_frame_count} 本圈点数(points)={len(current_points)}"
        )

    try:
        for frame in iter_frames_from_stream(
            data_sock,
            verify_crc=verify_crc,

            debug=debug,
            on_drop=on_drop_frame if debug else None,
        ):
            seen_frames += 1
            if not debug:
                maybe_print_status()
            if debug : # and (seen_frames <= 5 or seen_frames % 200 == 0)
                print(
                    f"[FRAME] 帧信息 | 序号=#{seen_frames} "
                    f"负载类型(payload_type)=0x{frame.payload_type:02X} "
                    f"帧序号(seq_num)={frame.seq_num} 负载长度(payload_len)={len(frame.payload)}B"
                )
            if frame.payload_type != PAYLOAD_TYPE_MSG:
                skipped_payload_type_non_msg += 1
                continue

            msg = parse_point_cloud_msg_payload(frame.payload)
            if msg is None:
                skipped_msg_parse_failed += 1
                continue
            seen_msg05 += 1
            if debug and (seen_msg05 <= 5 or seen_msg05 % 200 == 0):
                print(
                    f"[MSG05] 点云消息(msg_id=0x05) | 序号=#{seen_msg05} "
                    f"解析布局(layout)={msg['layout']} 圈号(scan_cnt)={msg['scan_cnt']} "
                    f"点云类型(data_type)=0x{msg['data_type']:02X} "
                    f"点数据长度(points_blob_len)={len(msg['points_blob'])}B"
                )
            maybe_print_debug_stats()

            scan_cnt = int(msg["scan_cnt"])
            data_type = int(msg["data_type"])
            time_type = int(msg["time_type"])
            # 时间戳选择：
            # - payload_ns：协议 1.5 声明的 timestamp(uint64, ns, 小端)
            # - h1：按你提供的 H1/NTP 风格从 payload 尾部解析（大端，sec+fraction/2^32），再换算成 ns
            if timestamp_source == "h1":
                timestamp_ns = msg.get("timestamp_h1_ns")
                if debug and msg.get("timestamp_h1_dbg") is not None:
                    print(f"[TS-H1] payload[-11:-1]={msg['timestamp_h1_dbg']} ts_h1_ns={timestamp_ns}")
            else:
                timestamp_ns = int(msg["timestamp_ns"])
            # 参考 `M盲区测试C3.py`：主要消费 data_type=0x03（极坐标二维）点云
            if data_type != 0x03:
                if debug:
                    print(
                        f"[SKIP] 跳过点云包 | 点云类型(data_type)=0x{data_type:02X} "
                        f"（当前脚本仅解析 0x03：极坐标二维）"
                    )
                skipped_data_type_other += 1
                continue

            raw_points = parse_points_polar_2d(msg["points_blob"])
            if not raw_points:
                skipped_points_empty += 1
                continue

            # scan_cnt 发生变化：上一圈结束，输出并清空
            if current_scan_cnt is None:
                current_scan_cnt = scan_cnt
            elif scan_cnt != current_scan_cnt:
                if current_scan_cnt is not None and current_points and current_timestamp_ns is not None:
                    # 与 `M盲区测试C3.py` 一致：按 angle/seq/点在包内序号排序
                    current_points.sort(
                        key=lambda p: (
                            p.angle_deg,
                            p.seq_num,
                            p.point_index,
                        )
                    )

                    angle_min = current_points[0].angle_deg
                    angle_max = current_points[-1].angle_deg
                    dist_min = min(p.r_mm for p in current_points)
                    dist_max = max(p.r_mm for p in current_points)
                    # 估计“中间缺了多少 seq”（mod256）：
                    # - observed_span：首包 seq 到末包 seq 的跨度（按 uint8 循环）
                    # - expected_span：若每包 seq 连续递增，则跨度应为 packets-1
                    # - missing_est：两者差值（<0 则视为 0）
                    seq_missing_est = None
                    seq_discont_count = None
                    seq_missing_sum_est = None
                    seq_dup_count = None
                    if current_seq_nums:
                        first_seq = current_seq_nums[0]
                        last_seq = current_seq_nums[-1]
                        observed_span = (last_seq - first_seq) % 256
                        expected_span = max(0, current_frame_count - 1)
                        seq_missing_est = max(0, observed_span - expected_span)

                        # 更直观的“是否连续”诊断（按接收顺序逐包检查）：
                        # step = (curr - prev) % 256
                        # - step==1：连续
                        # - step==0：重复包/重复 seq
                        # - step>1：中间缺包（或乱序导致的看似缺包）
                        discont = 0
                        missing_sum = 0
                        dup = 0
                        for prev, curr in zip(current_seq_nums, current_seq_nums[1:]):
                            step = (curr - prev) % 256
                            if step == 1:
                                continue
                            discont += 1
                            if step == 0:
                                dup += 1
                            else:
                                missing_sum += max(0, step - 1)
                        seq_discont_count = discont
                        seq_missing_sum_est = missing_sum
                        seq_dup_count = dup
                    delta_ms = (
                        (current_timestamp_ns - previous_timestamp_ns) / 1_000_000.0
                        if previous_timestamp_ns is not None
                        else 0.0
                    )
                    print(
                        f"[SCAN] 单圈完成 | 圈号(scan_cnt)={current_scan_cnt:3d} "
                        f"包数(packets)={current_frame_count:3d} 点数(points)={len(current_points):6d} "
                        f"角度范围(angle)={angle_min:7.2f}°..{angle_max:7.2f}° "
                        f"距离范围(r)={dist_min:5d}..{dist_max:5d}mm "
                        f"时间戳类型(time_type)={current_time_type} 时间戳(ts_ns)={current_timestamp_ns} "
                        f"圈间时间差(delta_ms)={delta_ms:9.3f}ms "
                        f"帧序号范围(seq)={current_seq_nums[0] if current_seq_nums else None}"
                        f"->{current_seq_nums[-1] if current_seq_nums else None} "
                        f"缺包估计(seq_missing_est)={seq_missing_est} "
                        f"不连续次数(seq_discont)={seq_discont_count} "
                        f"缺口累加估计(seq_missing_sum_est)={seq_missing_sum_est} "
                        f"重复次数(seq_dup)={seq_dup_count}"
                    )

                    previous_scan_cnt = current_scan_cnt
                    previous_timestamp_ns = current_timestamp_ns
                    completed += 1
                    if max_scans and completed >= max_scans:
                        break

                # 开始新一圈
                current_scan_cnt = scan_cnt
                current_points = [] # 点数信息
                current_seq_nums = []  # 帧数列表
                current_frame_count = 0
                current_timestamp_ns = None
                current_time_type = None

            # 聚合当前包
            current_frame_count += 1
            current_seq_nums.append(frame.seq_num)
            # 以“本圈看到的最新 timestamp”刷新；若你希望严格用“起始包”的 timestamp，后续我也可以改成只在起始包赋值
            current_timestamp_ns = int(timestamp_ns) if timestamp_ns is not None else None
            current_time_type = time_type

            for idx_in_packet, (r_mm, angle_raw, refl, tag) in enumerate(raw_points):
                angle_deg = normalize_c3_angle_deg(angle_raw / 100.0)  # 0.01° -> °
                current_points.append(
                    PointPolar2D(
                        r_mm=r_mm,
                        angle_deg=angle_deg,
                        reflectivity=refl,
                        tag=tag,
                        seq_num=frame.seq_num,
                        scan_cnt=scan_cnt,
                        timestamp_ns=timestamp_ns,
                        frame_index=frame_index,
                        point_index=idx_in_packet,
                    )
                )

            frame_index += 1

    finally:
        maybe_print_debug_stats(force=True)
        # 关闭连续取数
        try:
            cmd_sock.sendall(STOP_STREAM_CMD)
            try:
                ack2 = cmd_sock.recv(1024)
                if debug:
                    print(f"[ACK] 连续取数关(stop_stream) 回包长度={len(ack2)}B 头部预览={ack2[:32].hex(' ')}")
            except socket.timeout:
                pass
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


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="C3 连续点云按圈聚合与圈间时间差统计")
    p.add_argument("--host", default="192.168.1.112", help="C3 雷达 IP")
    p.add_argument("--cmd-port", type=int, default=50000, help="命令端口（协议 1.2：50000/TCP）")
    p.add_argument("--data-port", type=int, default=52000, help="点云端口（协议 1.2：52000/TCP）")
    p.add_argument("--no-crc", action="store_true", help="不校验 CRC16/CRC32（调试用）")
    # 默认改为 0：持续运行直到手动停止（或显式指定 --max-scans）
    p.add_argument("--max-scans", type=int, default=0, help="最多输出多少圈（0 表示无限）")
    p.add_argument("--debug", action="store_true", help="打印帧/消息解析调试信息")
    p.add_argument(
        "--timestamp-source",
        choices=["payload_ns", "h1"],
        default="h1",
        help="圈间 delta 的时间戳来源：payload_ns=协议1.5的uint64(ns,小端)；h1=按parse_c3_timestamp(=H1/NTP:sec+frac/2^32,大端)解析payload中timestamp的8字节后换算ns",
    )
    return p


def main() -> None:
    args = build_argparser().parse_args()
    run(
        args.host,
        cmd_port=args.cmd_port,
        data_port=args.data_port,
        verify_crc=not args.no_crc,
        max_scans=args.max_scans,
        debug=args.debug,
        timestamp_source="h1",#args.timestamp_source,
    )


if __name__ == "__main__":
    main()

