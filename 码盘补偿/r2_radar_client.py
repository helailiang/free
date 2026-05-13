"""
R2 雷达：HTTP 控制 + ``start_scanoutput`` 后 TCP 连续扫描流，组「完整一圈」点云。

默认策略偏 **「最新一圈优先」**（可牺牲时延）：``stream_prep_mode="stop_tcp"`` 每次测量前重建数据 TCP；
``stream_accuracy_first`` 为 True 时在未显式传入 ``discard_stream_circles`` 的前提下抬高「丢整圈」下限；
``drain`` 模式则加大排空包数/时长并启用 **静默尾捕**，减少「排空瞬间又抵内核」的残留分包。

与 ``h2_radar_client.H2SingleScanRadar`` 对齐的对外方法：
``connect_radar`` / ``close`` / ``configure_scan_parameters`` /
``optimized_single_measurement``，供 ``R2/r2_resolution_gui_test.py`` 直接替换 H2 使用。

协议与组包：仓库 ``R2/r2_client.py``（表 4-6）；**连续取数顺序**（与现场一致）：

  1. **HTTP** ``request_handle_tcp`` → 得到数据 **TCP 端口** 与 ``handle``；
  2. **HTTP** ``start_scanoutput`` 且应答成功（``error_code==0``）后，**才**连接该 TCP 端口读点云；
  3. 不再取数时 **HTTP** ``stop_scanoutput``，再关闭数据 TCP（见 ``close``）。

  可选（``R2SingleScanRadar.stream_prep_mode``）：每次测量前 ``stop_scanoutput`` → 关数据 TCP →
  ``release_handle`` → 再 ``request_handle_tcp``（新端口、新 handle）→ ``start_scanoutput`` → 连新端口；
  比 ``drain`` 更「硬」地贴近实时；实机在仅 stop 时往往关闭旧口导致 WinError 10061，故必须重新要端口。

整圈点数：R2 扫描 **360°** 固定 **3600** 点（index ``0 … 3599``），与 ``R2_FULL_CIRCLE_POINT_COUNT``
及 GUI 默认角分辨率 ``0.1°/index`` 一致；**不**再使用 ``get_parameter(samples_per_scan)`` 作为一圈长度。

说明：
  - 数据 TCP 为持续推流；组一圈见 ``assemble_one_full_scan``：按 **圈号 ``scan_count``** 缓存多分包，
    仅在圈号 **uint16 严格 +1（含回绕）** 时合并上一圈返回；圈号异常跳动则丢弃缓存并重新同步。
    亦可在一圈内点索引提前填满时返回。
  - 接收期间周期性 ``feed_watchdog``（后台线程）；``close`` 时先 ``stop_scanoutput``，再关 TCP，再 ``release_handle``。
  - 角度：``angle_deg = start_angle_deg + index * angular_resolution_deg``（与 H2 GUI 字段对齐）。
"""

from __future__ import annotations

import sys
import socket

import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

# 与 h2_radar_client 相同：保证可 import ``R2`` 包（仓库根在 parents[1]）
def _import_root_dir() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parents[1]


_ROOT = _import_root_dir()
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from R2.r2_client import (  # noqa: E402
    R2ControlClient,
    R2ProtocolError,
    R2ScanHeader,
    R2_DISTANCE_INVALID,
    R2_FULL_CIRCLE_POINT_COUNT,
    connect_r2_data_tcp,
    drain_scan_tcp_complete_packets,
    iter_scan_points,
    open_scan_stream,
    parse_scan_packet,
    read_one_scan_packet,
    report_r2_http_json_error,
)

# 每次在 GUI/控制台打出「取一圈所用」时递增，与设备 ``scan_count`` 解耦；多线程下由锁保护。
_assemble_log_seq_lock = threading.Lock()
_assemble_log_seq: int = 0


def _slots_complete(slots: list[tuple[int, int] | None]) -> bool:
    """一圈是否已填满：每个索引位均有采样（非 ``None``）。"""
    return all(x is not None for x in slots)


def _apply_packet_to_slots(
    slots: list[tuple[int, int] | None],
    hdr: R2ScanHeader,
    point_bytes: bytes,
) -> None:
    """把单个分包内的点按 ``first_point_index + 本地序号`` 写入 ``slots``。"""
    first_idx = int(hdr.first_point_index)
    max_pts = int(hdr.points_in_packet)
    n_words = len(point_bytes) // 4
    n_take = min(max_pts, n_words)
    for local_i, (d_raw, amp) in enumerate(iter_scan_points(point_bytes[: n_take * 4])):
        if local_i >= n_take:
            break
        gidx = first_idx + local_i
        if 0 <= gidx < len(slots):
            if int(d_raw) == int(R2_DISTANCE_INVALID):
                if slots[gidx] is None:
                    slots[gidx] = (0, int(amp))
            else:
                slots[gidx] = (int(d_raw), int(amp))


def _uint16_forward_ring_delta(prev_ring: int, new_ring: int) -> int:
    """
    将 ``scan_count`` 视为 16 位无符号圈号，计算从 ``prev_ring`` 到 ``new_ring`` 的**正向**步进差。

    合法「下一圈」为差值 **1**（含 65535→0 溢出）。其它差值表示：丢包导致中间圈在 TCP 侧不可见、
    设备复位、或字段异常跳动；此时**不得**把上一圈未收齐的分包误当成整圈合并输出。
    """
    return (int(new_ring) - int(prev_ring)) & 0xFFFF


def _print_assembled_circle_packets(
    ring: int,
    packets: dict[int, tuple[R2ScanHeader, bytes]],
    log: Callable[[str], None],
) -> None:
    """
    组满一圈并即将返回点云时，输出本圈所用的 **圈号** + **每个分包的点数明细**（便于核对
    "雷达一圈由多少包组成 / 各包覆盖哪段 index / 是否有包丢失"）。

    输出包含两部分（同一次 ``log`` 调用、用换行连接，GUI/控制台均可读）：

      1. **概览行**：``组圈日志序号 / 圈号(scan_count) / 共 N 包 / 合计 M 点``。
         ``合计 M`` = ``sum(hdr.points_in_packet for hdr in packets.values())``，
         应等于设备 ``samples_per_scan``；若 ``M < samples_per_scan`` 表明该圈在 TCP 侧
         有包缺失，``_merge_circle_packets_to_triples`` 会用 ``(0, 0)`` 占位补齐。

      2. **明细行**：按 ``packet_index`` 升序列出每包的 ``pkt#K:first=F:pts=P``——
         ``K`` = 该圈内第 K 个分包，``F`` = 本包内**第一个点**的全圈 ``index``（来自包头
         ``first_point_index``），``P`` = 本包点数（来自包头 ``points_in_packet``）。
         相邻两包的 ``F`` 之差应等于前一包的 ``P``（连续无缝覆盖全圈 index）；若出现跳跃
         或重叠，配合上方 ``合计 M`` 即可定位异常段。

    ``log`` 由 ``assemble_one_full_scan(..., scan_log=...)`` 传入，可为雷达实例的
    ``_emit_scan_log``（同时打印到控制台与 GUI 日志面板）。

    为何日志里的「圈号」会**反复相同**（看起来像恒定值）：
      - 打印的是包头里的 ``scan_count``（uint16 设备侧圈计数），不是本软件自增的序号。
      - 每次 **数据 TCP 会话重建**（如方案 3 的 ``release_handle`` + ``request_handle_tcp``/
        建连）后，雷达固件常把该计数 **从 0 或较小值重新累加**，因此连续多次测量若都刚重连完
        再组圈，会多次看到 ``0、1、2…`` 里**同一数字**，属正常现象，**不代表**点云未更新。
      - 此时以本函数前缀的 **组圈日志序号**（进程内单调递增）区分各次独立闭合的整圈。
    """
    global _assemble_log_seq
    with _assemble_log_seq_lock:
        _assemble_log_seq += 1
        log_n = int(_assemble_log_seq)
    pkt_idxs = sorted(packets.keys())
    pkt_n = len(pkt_idxs)
    breakdown_parts: list[str] = []
    total_pts = 0
    for pi in pkt_idxs:
        hdr, _payload = packets[pi]
        first_idx = int(hdr.first_point_index)
        n_pts = int(hdr.points_in_packet)
        breakdown_parts.append(f"pkt#{pi}:first={first_idx}:pts={n_pts}")
        total_pts += n_pts
    breakdown_s = ", ".join(breakdown_parts)
    log(
        f"[R2] 取一圈所用：组圈日志序号={log_n}，圈号(scan_count)={int(ring) & 0xFFFF}，"
        f"共 {pkt_n} 包，合计 {total_pts} 点。\n"
        f"  分包明细（pkt#K:first=F:pts=P）= [{breakdown_s}]\n"
        "  说明：scan_count 为设备 16 位字段，可能与节拍/重连叠加而重复；同一组圈日志序号不会重复，"
        "重复 scan_count 仍可能是不同圈点云。"
    )


def _peak_reflectivity_point(points: list[dict[str, Any]]) -> tuple[int, int] | None:
    """
    在点列表中取反射率最大点；同反射率时取较小 index（与 GUI ``top_n_reflectivity_in_window`` 规则一致）。
    """
    if not points:
        return None
    best = max(points, key=lambda p: (int(p["reflectivity"]), -int(p["index"])))
    return int(best["index"]), int(best["reflectivity"])


def _warn_if_packets_ring_mismatch(
    packets: dict[int, tuple[R2ScanHeader, bytes]],
    expected_ring: int,
    slog: Callable[[str], None],
) -> None:
    """
    防御性校验：``packets`` 中每个包头里的 ``scan_count`` 应与 ``expected_ring`` 一致。

    正常 TCP 顺序下，仅当 ``ring == current_ring`` 时才会写入 ``packets``，此处应恒成立。
    若不一致，多为包头字段异常或极端重排，仅告警不中断合并（避免现场完全停摆）。
    """
    er = int(expected_ring) & 0xFFFF
    bad_pix: list[int] = []
    for pix, (h, _) in packets.items():
        if (int(h.scan_count) & 0xFFFF) != er:
            bad_pix.append(int(pix))
    if bad_pix:
        slog(
            f"[R2] 警告：缓存圈号={er}，但下列包号对应包头 scan_count 不一致（前 20 个）: {bad_pix[:20]}"
        )


def _merge_circle_packets_to_triples(
    packets: dict[int, tuple[R2ScanHeader, bytes]],
    n_points: int,
) -> list[tuple[int, int, int]]:
    """
    将同一 ``scan_count`` 下已收齐的 **多个分包**（按 ``packet_index`` 排序）合并为 ``n_points`` 条点。

    同一 ``packet_index`` 若重复出现，后到的覆盖先到（抗重传）。未覆盖到的索引补 ``(0,0)``。
    """
    slots: list[tuple[int, int] | None] = [None] * int(n_points)
    for _pidx in sorted(packets.keys()):
        h, pb = packets[_pidx]
        _apply_packet_to_slots(slots, h, pb)
    out: list[tuple[int, int, int]] = []
    for i in range(int(n_points)):
        cell = slots[i]
        if cell is None:
            out.append((i, 0, 0))
        else:
            out.append((i, int(cell[0]), int(cell[1])))
    return out


def assemble_one_full_scan(
    sock: socket.socket,
    points_per_circle: int,
    *,
    deadline_s: float = 0.5,
    per_read_timeout_s: float = 1.2,
    wait_packet_index_one: bool = True,
    scan_log: Callable[[str], None] | None = None,
) -> list[tuple[int, int, int]]:
    """
    从已打开的 R2 数据 TCP 套接字读取二进制包，拼出 **完整一圈** 点云（``points_per_circle`` 点）。

    现场逻辑（与设备分包行为一致）：

    - **``scan_count``（包头）**：**圈号**，在**同一条数据 TCP 会话内**每扫完一圈通常递增（uint16 溢出后从 0 继续）。
      若每次测量前执行 ``stop_tcp`` 等导致 **TCP 重连/句柄重建**，固件侧计数器常会 **复位**，日志里圈号可能
      反复出现相同数值，这不表示点云未刷新，仅表示协议层计数重新从低值开始。
    - **``packet_index``**：**当前圈内的第几个分包**（通常从 1 递增；同一圈 ``scan_count`` 不变）。
    - 一圈点云由 **多个分包** 组成；**同一圈号** 收到的所有分包拼起来才是该圈数据。
    - **一圈结束判定（主路径）**：读到 **下一圈** 且圈号相对当前缓存为 **严格 +1（uint16 含回绕）**，
      才认定 **上一圈** 已在流中结束，对 **缓存中的上一圈** 合并返回。
    - **圈号异常跳动**（例如 1227 后直接 1292）：**不**闭合上一圈；丢弃未完成缓存并重新同步，
      避免把缺分包的一圈当整圈导致索引窗内点数锐减、最高反射误判。
    - **同一圈包号统计**：仅在 ``hdr.scan_count == current_ring`` 时写入 ``packets``，故日志里的包号
      均对应当前攒圈同一圈号；合并前再做包头 ``scan_count`` 一致性校验，不一致则打 ``[R2] 警告``。
    - **提前完成（辅路径）**：若尚未等到圈号切换，但 ``0 … points_per_circle-1`` 索引已全部写入，
      也可立即返回（兼容单包一圈或极快收齐）。

    对齐：若 ``wait_packet_index_one`` 为真，则丢弃数据直到遇到 ``packet_index == 1`` 再开始缓存，
    避免从半圈中间开始把两圈拼在一起；不要求 ``first_point_index == 0``（实机未必为 0）。

    Args:
        sock: 已连接的数据 TCP。
        points_per_circle: 一圈点数（如 3600）。
        deadline_s: 最长等待；一圈分包多时应适当加大。
        per_read_timeout_s: 单次 ``recv`` 超时，过大拖慢响应，过小易误判超时。
        wait_packet_index_one: 是否必须等到 ``packet_index==1`` 才开始组圈。
        scan_log: 每成功组满一圈及摘要时调用；缺省为 ``print``。GUI 可传入 ``radar._emit_scan_log``。

    Returns:
        ``[(index, dist_mm, amp), ...]``，长度 ``points_per_circle``。
    """
    if points_per_circle <= 0:
        raise R2ProtocolError(f"非法 points_per_circle={points_per_circle}")

    slog: Callable[[str], None] = scan_log if scan_log is not None else (lambda m: print(m, flush=True))

    t_end = time.monotonic() + float(deadline_s)
    # 当前正在攒的圈号；packets: 该圈内 packet_index -> (header, payload)
    current_ring: int | None = None
    packets: dict[int, tuple[R2ScanHeader, bytes]] = {}
    # 辅路径：同一圈分包持续写入，用于在「圈号未翻转」前提前凑满一圈点数
    slots_fast: list[tuple[int, int] | None] = [None] * int(points_per_circle)

    while time.monotonic() < t_end:
        sock.settimeout(float(per_read_timeout_s))
        try:
            raw = read_one_scan_packet(sock)
        except (R2ProtocolError, OSError) as exc:
            if time.monotonic() >= t_end:
                raise R2ProtocolError(f"组一圈超时: {exc}") from exc
            continue

        hdr, point_bytes, _ = parse_scan_packet(raw)
        ring = int(hdr.scan_count) & 0xFFFF
        pix = int(hdr.packet_index) & 0xFFFF
        # 尚未开始攒圈：可选仅当 packet_index==1 时入场，与设备「圈内第 1 包」对齐
        if current_ring is None:
            if wait_packet_index_one and pix != 1:
                continue
            current_ring = ring
            packets = {pix: (hdr, point_bytes)}
            slots_fast = [None] * int(points_per_circle)
            _apply_packet_to_slots(slots_fast, hdr, point_bytes)
            continue

        if ring == current_ring:
            packets[pix] = (hdr, point_bytes)
            _apply_packet_to_slots(slots_fast, hdr, point_bytes)
            if _slots_complete(slots_fast):
                assert current_ring is not None
                _warn_if_packets_ring_mismatch(packets, int(current_ring), slog)
                _print_assembled_circle_packets(int(current_ring), packets, slog)
                return [
                    (i, int(slots_fast[i][0]), int(slots_fast[i][1]))  # type: ignore[index]
                    for i in range(len(slots_fast))
                ]
            continue

        # ---------- ring != current_ring：仅当圈号为「上一圈 +1」时才闭合；否则视为流异常 ----------
        ring_delta = _uint16_forward_ring_delta(int(current_ring), int(ring))
        if ring_delta != 1:
            # 非顺序递增：不能把当前 packets 当完整一圈输出；丢弃并从可锚定位置重新攒圈。
            print(
                "[R2] assemble_one_full_scan: 圈号非顺序递增 "
                f"(缓存圈={current_ring} 新包圈={ring} Δ={ring_delta} 包号={pix})，"
                "丢弃未完成分包并重新同步",
                file=sys.stderr,
                flush=True,
            )
            if wait_packet_index_one and int(pix) != 1:
                current_ring = None
                packets = {}
                slots_fast = [None] * int(points_per_circle)
                continue
            current_ring = int(ring)
            packets = {int(pix): (hdr, point_bytes)}
            slots_fast = [None] * int(points_per_circle)
            _apply_packet_to_slots(slots_fast, hdr, point_bytes)
            continue
        # closed_ring 就是上一圈的圈号
        closed_ring = int(current_ring)
        _warn_if_packets_ring_mismatch(packets, closed_ring, slog)
        triples = _merge_circle_packets_to_triples(packets, points_per_circle)
        _print_assembled_circle_packets(closed_ring, packets, slog)
        # 当前包属于新圈（已验证为 strict +1），切换状态；本函数只返回上一圈。
        current_ring = int(ring)
        packets = {int(pix): (hdr, point_bytes)}
        slots_fast = [None] * int(points_per_circle)
        _apply_packet_to_slots(slots_fast, hdr, point_bytes)
        return triples

    raise R2ProtocolError(
        f"{deadline_s:.1f}s 内未收到「下一圈」以闭合当前圈（最后圈号={current_ring}，"
        f"已缓存分包数={len(packets)}）；可增大 deadline_s 或检查数据口/喂狗。"
    )


class R2SingleScanRadar:
    """
    R2 连续流场景下的「单次测量」= 从 TCP 流中截取**完整一圈**并裁剪索引窗。

    ``connect_radar`` 会：HTTP 探活 → ``request_handle_tcp`` → **成功** ``start_scanoutput`` →
    再建数据 TCP；并启动看门狗线程。之后每次 ``optimized_single_measurement`` 默认只读 TCP；若
    ``stream_prep_mode="stop_tcp"``，则在每次测量前走 ``_pause_data_tcp_and_reopen``（含 ``release_handle``
    与重新 ``request_handle_tcp``，以匹配实机数据口在 stop 后常变更的行为）。若更关心速度，可改为
    ``"drain"`` 或把 ``stream_accuracy_first`` 设为 False 并减小 ``stream_discard_circles_before_sample``。
    """

    def __init__(
        self,
        host: str = "192.168.0.240",
        cmd_port: int = 80,
        connect_timeout: float = 5.0,
    ) -> None:
        self.host = str(host)
        self.cmd_port = int(cmd_port)
        self.connect_timeout = float(connect_timeout)
        self.last_error = ""
        self.angular_resolution_deg: float = 0.1
        self.start_angle_deg: float = -45.0
        self._ctrl: R2ControlClient | None = None
        self._data_sock: socket.socket | None = None
        # 当前数据 TCP 端口（``open_scan_stream`` / ``stop_tcp`` 重配后会更新）。
        self._data_tcp_port: int = 0
        self._handle: str = ""
        # 整圈点数：默认按 GUI 假设值（``R2_FULL_CIRCLE_POINT_COUNT``=3600，对应 0.1°/index）。
        # 真实值由 ``connect_radar`` / ``restart_data_stream`` 内部的
        # ``_detect_points_per_circle_from_sock`` 从首包包头 ``samples_per_scan`` 读出后覆盖；
        # 不同固件可能配置为 1800（0.2°/index）等其他值，避免硬编码引发的"只组到一半"问题。
        self._points_per_circle: int = int(R2_FULL_CIRCLE_POINT_COUNT)
        # 设备真实扫描参数（来自包头）：连接成功后由 ``_detect_points_per_circle_from_sock`` 写入。
        # 0.0 表示尚未探测/探测失败；GUI 侧 ``_sync_ui_from_device_params`` 会基于这些字段
        # 自动校正 angular_resolution_deg / start_angle_deg / 索引上限等控件。
        self._device_angular_resolution_deg: float = 0.0
        self._device_start_angle_deg: float = 0.0
        self._device_scan_fov_deg: float = 0.0
        self._watch_stop = threading.Event()
        self._watch_thread: threading.Thread | None = None
        # 与 ``_pause_data_tcp_and_reopen`` 互斥：避免喂狗在 ``release_handle`` 之后仍携带已失效的 handle。
        self._stream_ctl_lock = threading.Lock()
        # 组一圈的最长等待；高速转台场景可适当加大。
        # 一圈多分包时须等圈号翻转，默认略长；仍可在实例上改 ``full_scan_deadline_s``。
        self.full_scan_deadline_s: float = 15.0
        # 为 True 时仅当 ``packet_index==1`` 才开始攒圈；若入场后一直等不到 1，可改为 False 从任意分包开始。
        self.assemble_wait_packet_index_one: bool = True
        # 可选：由 GUI 注入（如 ``Qt.Signal.emit``），便于在工作线程取数时把组圈摘要写到界面日志。
        self.scan_log: Callable[[str], None] | None = None
        # 每次测量前在正式组圈之外，先 **完整组装并丢弃** 若干圈（不返回给 GUI），用于甩掉排队中的旧点云。
        # 默认 1；与 ``stream_accuracy_first`` 的「下限抬升」叠加后，``drain`` 模式下实际至少丢 2 圈（见测量逻辑）。
        self.stream_discard_circles_before_sample: int = 5
        # 为 True 时：每次测量前按 ``stream_prep_mode`` 做「贴近实时」准备（见 ``optimized_single_measurement``）。
        self.stream_favor_latest_tcp: bool = True
        # 为 True（默认）：在未显式传入 ``discard_stream_circles`` 时，按策略抬高丢整圈下限（stop_tcp≥1，drain≥2），
        # 以时间换「用于判读/导出的那一圈」更贴近当前时刻；追求极致帧率时置 False。
        self.stream_accuracy_first: bool = True
        # 测量前 TCP 侧准备策略（在 ``stream_favor_latest_tcp`` 为 True 时生效）：
        #   - ``"drain"``：保持连接，用 ``drain_scan_tcp_complete_packets``（含静默尾捕）消费到队列沿；
        #   - ``"stop_tcp"``：stop → 关 TCP → ``release_handle`` → ``request_handle_tcp`` → start → 连 **新** 端口；
        #   - ``"none"``：跳过 drain/stop（仍可按 ``stream_discard_circles_before_sample`` 丢整圈）。
        self.stream_prep_mode: str = "drain"
        # ``drain_scan_tcp_complete_packets`` 上限（极端积压时仍可再调大）。
        self.drain_tcp_max_packets: int = 100_000
        self.drain_tcp_max_wall_s: float = 20.0
        # 静默尾捕：在「零超时已不可读」之后追加若干次短阻塞 select，减少尾包残留（仅在本轮已读过≥1 包时启用）。
        self.drain_tcp_tail_quiet_polls: int = 20
        self.drain_tcp_tail_quiet_poll_s: float = 0.05

    def _emit_scan_log(self, msg: str) -> None:
        """控制台始终打印一行；若 ``self.scan_log`` 已绑定（如主窗 Signal），则同步转发给 GUI。"""
        print(msg, flush=True)
        if self.scan_log is not None:
            try:
                self.scan_log(msg)
            except Exception:
                pass

    @property
    def socket(self) -> Any:
        """与 H2 兼容：主界面用 ``radar.socket is not None`` 判断已连接；R2 为数据 TCP。"""
        return self._data_sock

    @property
    def port(self) -> int:
        """与 H2 的 ``radar.port`` 对齐，用于判断 UI IP/端口是否一致（R2 为 HTTP 命令端口）。"""
        return int(self.cmd_port)

    @property
    def points_per_circle(self) -> int:
        """
        设备实测的整圈点数（来自包头 ``samples_per_scan``，由 ``_detect_points_per_circle_from_sock`` 写入）。

        - 未连接 / 探测失败时回退到默认 ``R2_FULL_CIRCLE_POINT_COUNT``。
        - GUI 侧 "探测最高反索引" 等需要"全圈范围"的逻辑应使用本属性，而非硬编码 3600，
          否则 0.2°/index 设备会因半圈空 slot 被当作 ``(0,0)`` 而在末尾形成 1800 个伪点。
        """
        return int(self._points_per_circle) if int(self._points_per_circle) > 0 else int(R2_FULL_CIRCLE_POINT_COUNT)

    @property
    def device_angular_resolution_deg(self) -> float:
        """设备实测角分辨率（``angle_step_centi_deg / 100``）。0.0 表示尚未探测。"""
        return float(self._device_angular_resolution_deg)

    @property
    def device_start_angle_deg(self) -> float:
        """设备实测起始角（``first_angle_centi_deg / 100``，含符号）。0.0 表示尚未探测。"""
        return float(self._device_start_angle_deg)

    @property
    def device_scan_fov_deg(self) -> float:
        """设备实测扫描视场角（``points_per_circle × angular_resolution_deg``）。0.0 表示尚未探测。"""
        return float(self._device_scan_fov_deg)

    def _detect_points_per_circle_from_sock(self) -> int:
        """
        从当前 ``self._data_sock`` 上 **窥探一包** 数据，解析包头，得到设备真实的：
          - ``samples_per_scan``        → ``self._points_per_circle``
          - ``angle_step_centi_deg``    → ``self._device_angular_resolution_deg`` (deg)
          - ``first_angle_centi_deg``   → ``self._device_start_angle_deg``        (deg)
          - 二者相乘                    → ``self._device_scan_fov_deg``           (deg)

        不依赖 ``get_parameter(samples_per_scan)`` HTTP 调用——直接取数据流首包，**不会**与
        固件返回值口径不一致；此包不会丢弃，``read_one_scan_packet`` 已把它从套接字读出，
        但下一次 ``assemble_one_full_scan`` 进入循环后第一次 ``read`` 仍会读到下一个完整包，
        所以仅消耗"一包"数据即可。

        失败时返回当前已设的 ``_points_per_circle``（不修改 device_* 字段），并打日志告警。
        """
        sock = self._data_sock
        if sock is None:
            self._emit_scan_log("[R2] 探测设备扫描参数失败：数据 TCP 未建立。")
            return int(self._points_per_circle)
        try:
            # 与 ``assemble_one_full_scan`` 内部用法保持一致：``read_one_scan_packet`` 只返回
            # **整包字节流**（含表 4-6 的固定头 + 可变长 ScanData），随后必须经
            # ``parse_scan_packet`` 解出 ``(R2ScanHeader, point_bytes, ...)``。
            # 之前误写成 2-tuple 解包，会触发 "too many values to unpack (expected 2)"。
            raw_pkt = read_one_scan_packet(sock)
            hdr, _point_bytes, _tail = parse_scan_packet(raw_pkt)
        except Exception as exc:  # noqa: BLE001 — 探测失败不应中断连接流程
            self._emit_scan_log(f"[R2] 探测设备扫描参数失败：读包异常 {exc!r}。")
            return int(self._points_per_circle)

        samples = int(hdr.samples_per_scan)
        # 注意：``R2ScanHeader`` 里这两个字段名虽叫 ``_centi_deg``（百分之一度），但 R2 表 4-6 明文规定
        # 单位是 **1/10000°**（万分之一度）。例如 0.2°/index 设备的包头会给 angle_step=2000、
        # 0.1°/index 设备给 1000；若按 1/100 解算会把 0.2° 错算成 20°。命名是上游
        # ``r2_client.parse_scan_packet`` 的历史遗留，此处只按协议规定的真实单位解换。
        step_raw = int(hdr.angle_step_centi_deg)      # 实为 1/10000 度
        first_raw = int(hdr.first_angle_centi_deg)    # 实为 1/10000 度
        # 异常防护：固件偶尔返回 0，回落到 GUI 默认值，避免下游除零 / 死循环。
        if samples <= 0 or step_raw <= 0:
            self._emit_scan_log(
                f"[R2] 探测到非法包头：samples_per_scan={samples}, "
                f"angle_step_raw={step_raw}（1/10000°），"
                f"放弃覆盖，沿用默认 points_per_circle={int(self._points_per_circle)}。"
            )
            return int(self._points_per_circle)

        # 1/10000° → °：除以 10000.0
        step_deg = step_raw / 10000.0
        first_deg = first_raw / 10000.0
        fov_deg = samples * step_deg
        gui_step = float(self.angular_resolution_deg)
        gui_start = float(self.start_angle_deg)
        # 1% 容差比较（避免 0.1 与 0.10001 触发误报）
        step_match = abs(step_deg - gui_step) <= max(1e-6, gui_step * 0.01)
        start_match = abs(first_deg - gui_start) <= 0.05  # 起始角差 <0.05° 视为一致
        # 一次性把 4 个核心参数 + 与 GUI 的差异打到日志（控制台 + GUI 面板都看得到）。
        self._emit_scan_log(
            "[R2] 设备包头扫描参数："
            f"samples_per_scan={samples}（每圈点数）, "
            f"angle_step={step_deg:.4f}°/index, "
            f"first_angle={first_deg:+.4f}°, "
            f"FOV={fov_deg:.2f}°；"
            f"GUI 当前：角分辨率={gui_step:.4f}°，起始角={gui_start:+.4f}° → "
            f"分辨率{'一致' if step_match else '不一致(将由 GUI 自动同步)'}, "
            f"起始角{'一致' if start_match else '不一致(将由 GUI 自动同步)'}。"
        )
        if not step_match:
            theo_3600 = abs(0.1 - step_deg) <= 1e-6
            theo_1800 = abs(0.2 - step_deg) <= 1e-6
            if theo_3600 or theo_1800:
                self._emit_scan_log(
                    f"[R2] 提示：角分辨率 {step_deg:.4f}° 对应每圈 {samples} 点"
                    f"（0.1°→3600，0.2°→1800）；硬编码 3600 时 0.2° 设备会出现"
                    "「合计 1800 点 / 总点数 3600」差值，由组圈使用 device.samples_per_scan 修复。"
                )

        self._device_angular_resolution_deg = float(step_deg)
        self._device_start_angle_deg = float(first_deg)
        self._device_scan_fov_deg = float(fov_deg)
        self._points_per_circle = int(samples)
        return int(samples)

    def configure_scan_parameters(
        self,
        angular_resolution_deg: float | None = None,
        scan_angle_range_deg: float | None = None,
        start_angle_deg: float | None = None,
    ) -> None:
        if angular_resolution_deg is not None and angular_resolution_deg > 0:
            self.angular_resolution_deg = float(angular_resolution_deg)
        if start_angle_deg is not None:
            self.start_angle_deg = float(start_angle_deg)
        _ = scan_angle_range_deg

    def _watchdog_loop(self) -> None:
        while not self._watch_stop.is_set():
            ctrl = self._ctrl
            handle = self._handle
            if ctrl and handle:
                # 非阻塞抢锁：``_pause_data_tcp_and_reopen`` 持锁期间会 ``release_handle``/换 handle，
                # 若此时仍用旧 handle 喂狗，设备侧易返回非法句柄；宁可跳过一轮。
                if self._stream_ctl_lock.acquire(blocking=False):
                    try:
                        ctrl.feed_watchdog(handle)
                    except R2ProtocolError:
                        # ``feed_watchdog`` 内 ``_get_ok`` 已在 stderr 打印完整 JSON 并抛错，此处不再重复输出。
                        pass
                    except Exception as exc:
                        print(f"[R2] feed_watchdog 请求异常: {exc!r}", file=sys.stderr, flush=True)
                    finally:
                        self._stream_ctl_lock.release()
            self._watch_stop.wait(0.25)

    def connect_radar(self) -> bool:
        self.last_error = ""
        self.close()
        try:
            ctrl = R2ControlClient(self.host, cmd_port=self.cmd_port, timeout=self.connect_timeout)
            # 可选探活；失败则后续 request_handle_tcp 也会失败。
            ctrl.get_protocol_info()
            self._points_per_circle = int(R2_FULL_CIRCLE_POINT_COUNT)

            # 顺序由 ``open_scan_stream`` 保证：request_handle_tcp → start_scanoutput（成功）→ TCP connect。
            sock, handle, ctrl2, data_port = open_scan_stream(self.host, cmd=ctrl, timeout=self.connect_timeout)
            assert ctrl2 is ctrl

            self._watch_stop.clear()
            self._watch_thread = threading.Thread(target=self._watchdog_loop, name="R2Watchdog", daemon=True)
            self._watch_thread.start()

            self._ctrl = ctrl
            self._data_sock = sock
            self._handle = handle
            self._data_tcp_port = int(data_port)
            # 数据 TCP 已建好，立即从首包探测设备真实扫描参数（samples_per_scan / angle_step / first_angle），
            # 并把 ``_points_per_circle`` + ``_device_*`` 字段刷新；探测 **必须** 在 watchdog 启动后、
            # 任何 GUI 取数前完成，否则首次组圈仍会按硬编码 3600 走，重复出现"合计 X 点 / 总点数 3600"。
            self._detect_points_per_circle_from_sock()
            return True
        except Exception as exc:  # noqa: BLE001 — 连接失败需吞并转成 last_error
            self.last_error = str(exc)
            self.close()
            return False

    def close(self) -> None:
        """停止看门狗 → HTTP ``stop_scanoutput`` → 关闭数据 TCP → HTTP ``release_handle``。"""
        self._watch_stop.set()
        if self._watch_thread is not None:
            self._watch_thread.join(timeout=2.0)
            self._watch_thread = None

        ctrl, handle, sock = self._ctrl, self._handle, self._data_sock
        # 先 HTTP 停流，再关套接字。容错路径不抛错，但若 ``error_code!=0`` 须打印完整 JSON（与现场约定一致）。
        if ctrl and handle:
            safe = quote(handle, safe="")
            path_stop = f"/cmd/stop_scanoutput?handle={safe}"
            try:
                report_r2_http_json_error(path_stop, ctrl.get_json_raw(path_stop))
            except Exception as exc:
                print(f"[R2] stop_scanoutput 请求异常: {exc!r}", file=sys.stderr, flush=True)
        if sock:
            try:
                sock.close()
            finally:
                self._data_sock = None
        if ctrl and handle:
            safe = quote(handle, safe="")
            path_rel = f"/cmd/release_handle?handle={safe}"
            try:
                report_r2_http_json_error(path_rel, ctrl.get_json_raw(path_rel))
            except Exception as exc:
                print(f"[R2] release_handle 请求异常: {exc!r}", file=sys.stderr, flush=True)
        self._ctrl = None
        self._handle = ""
        self._data_tcp_port = 0

    def _pause_data_tcp_and_reopen(self) -> None:
        """
        在一次测量开始前，用「停流 → 关本机 TCP → 释放句柄 → 重新要端口 → 再开流 → 连新口」裁掉旧字节。

        为何不能 ``stop`` 后仍连 **旧** ``_data_tcp_port``：
          - 多台实机在 ``stop_scanoutput`` 后会撤掉数据口监听，旧端口 ``connect`` 直接 ``WinError 10061``
           （目标积极拒绝）；说明书 STEP5→6 也要求 **先** 拿到端口与 handle，**再** ``start`` 后去连。
        因此本函数在关套接字后走 ``release_handle`` + ``request_handle_tcp``，用 **新** ``handle`` 与 **新**
        ``port`` 再建数据 TCP；与初次 ``open_scan_stream`` 等价，只是复用已有 ``R2ControlClient``。

        与 ``drain`` 的取舍：
          - 本路径 HTTP 更多、还要换 handle，**延迟**更高；固件异常时失败面也略大，不需要「硬清空」时
            请用 ``stream_prep_mode=\"drain\"``。

        Raises:
            ``R2ProtocolError`` / ``OSError``：由 ``optimized_single_measurement`` 统一捕获并写入 ``last_error``。
        """
        ctrl = self._ctrl
        handle = self._handle
        sock = self._data_sock
        if ctrl is None or not handle:
            raise R2ProtocolError("数据流重置失败：缺少 HTTP 客户端或 handle")
        safe = quote(handle, safe="")
        path_stop = f"/cmd/stop_scanoutput?handle={safe}"
        # 与 ``close`` 一致：先停设备侧推流，再关本机套接字。
        report_r2_http_json_error(path_stop, ctrl.get_json_raw(path_stop))
        if sock is not None:
            try:
                sock.close()
            finally:
                self._data_sock = None

        with self._stream_ctl_lock:
            ctrl.release_handle(handle)
            new_port, new_handle = ctrl.request_handle_tcp()
            ctrl.start_scanoutput(new_handle)
            try:
                s = connect_r2_data_tcp(self.host, int(new_port), timeout=float(self.connect_timeout))
            except OSError:
                safe2 = quote(new_handle, safe="")
                path_stop2 = f"/cmd/stop_scanoutput?handle={safe2}"
                try:
                    report_r2_http_json_error(path_stop2, ctrl.get_json_raw(path_stop2))
                except Exception as exc:
                    print(f"[R2] stop_scanoutput（重连失败后清理）异常: {exc!r}", file=sys.stderr, flush=True)
                raise
            self._handle = new_handle
            self._data_tcp_port = int(new_port)
            self._data_sock = s

    # ──────────────────────────────────────────────────────────────────────────
    # 方案 3（停取 → 移台 → 清缓 → 起取）专用 public API
    # ──────────────────────────────────────────────────────────────────────────
    # GUI 在每个步进周期前调 ``stop_data_stream()``，把设备推流关掉、本机 TCP 关掉
    # （但保留 HTTP handle 与 R2ControlClient，供下一次 ``restart_data_stream`` 复用），
    # 再驱动转台运动 + 等待沉降；之后调 ``restart_data_stream()`` 重开数据通道。
    # 与 ``_pause_data_tcp_and_reopen`` 的差异：后者是 ``optimized_single_measurement``
    # **内部** 在测量前的一次性操作，外层无法把转台运动夹在 stop 与 restart 之间；本对
    # public API 把这两步显式暴露给 GUI 调度。

    def stop_data_stream(self) -> None:
        """
        关停数据推流：HTTP ``stop_scanoutput`` + 关本机数据 TCP 套接字。

        - 不调 ``release_handle``：保留当前 handle 与 ``R2ControlClient``，便于
          ``restart_data_stream`` 用相同会话上下文重新拉起；GUI 取消 / 关闭时仍
          应走 ``close()``，那里会显式 release。
        - 不抛异常：方案 3 主流程关心的是"确保没有点云在排队"，HTTP / 套接字侧任何
          异常都打日志吞掉，下一步 ``restart_data_stream`` 会重新建链。
        """
        ctrl = self._ctrl
        handle = self._handle
        if ctrl is None or not handle:
            self._emit_scan_log("[R2] stop_data_stream 跳过：未连接或 handle 为空。")
            return
        safe = quote(handle, safe="")
        path_stop = f"/cmd/stop_scanoutput?handle={safe}"
        try:
            report_r2_http_json_error(path_stop, ctrl.get_json_raw(path_stop))
        except Exception as exc:  # noqa: BLE001 — 关流失败也要继续关 TCP
            self._emit_scan_log(f"[R2] stop_scanoutput 异常（继续关闭 TCP）：{exc!r}")
        sock = self._data_sock
        if sock is not None:
            try:
                sock.close()
            finally:
                self._data_sock = None
        self._emit_scan_log("[R2] 数据流已停止（HTTP stop_scanoutput + 本机 TCP 关闭）。")

    def restart_data_stream(self) -> None:
        """
        重新拉起数据推流：``release_handle``（如残留）→ ``request_handle_tcp`` 拿新端口
        与 handle → ``start_scanoutput`` → 连接新 TCP 端口 → ``_detect_points_per_circle_from_sock``。

        与初次 ``connect_radar`` 的差异：复用现有 ``R2ControlClient`` 与 watchdog 线程，
        不必走 ``get_protocol_info`` / 启动新 watchdog；其它步骤等价。

        探测包头是必需的：每次重连都可能遇到固件参数变化（如使用方临时改了 ``angle_step``），
        ``_points_per_circle`` / ``device_*`` 字段必须刷新，否则 GUI 自动同步会用旧值。

        Raises:
            ``R2ProtocolError`` / ``OSError``：调用方需把异常显示到 GUI 并允许中断序列。
        """
        ctrl = self._ctrl
        if ctrl is None:
            raise R2ProtocolError("restart_data_stream 失败：未连接（self._ctrl is None）")
        # 旧 handle 残留时先尝试 release（不致命）；新流程必须用全新 handle 与端口。
        old_handle = self._handle
        if old_handle:
            try:
                ctrl.release_handle(old_handle)
            except Exception as exc:  # noqa: BLE001
                self._emit_scan_log(f"[R2] release_handle（旧 handle）异常（忽略）：{exc!r}")
            self._handle = ""
        with self._stream_ctl_lock:
            new_port, new_handle = ctrl.request_handle_tcp()
            ctrl.start_scanoutput(new_handle)
            try:
                s = connect_r2_data_tcp(self.host, int(new_port), timeout=float(self.connect_timeout))
            except OSError:
                safe2 = quote(new_handle, safe="")
                path_stop2 = f"/cmd/stop_scanoutput?handle={safe2}"
                try:
                    report_r2_http_json_error(path_stop2, ctrl.get_json_raw(path_stop2))
                except Exception as exc:
                    print(f"[R2] stop_scanoutput（重连失败后清理）异常: {exc!r}", file=sys.stderr, flush=True)
                raise
            self._handle = new_handle
            self._data_tcp_port = int(new_port)
            self._data_sock = s
        self._emit_scan_log(
            f"[R2] 数据流已重启：新端口={int(self._data_tcp_port)}，新 handle={self._handle[:8]}…"
        )
        self._detect_points_per_circle_from_sock()

    def _merged_to_gui_style(self, triples: list[tuple[int, int, int]]) -> list[dict[str, Any]]:
        """R2 (index, mm, amp) → 与 H2/h1 一致的 ``all_results`` 字典列表。"""
        st = float(self.start_angle_deg)
        ang = float(self.angular_resolution_deg)
        out: list[dict[str, Any]] = []
        for idx, r_mm, refl in triples:
            out.append(
                {
                    "index": int(idx),
                    "angle_deg": st + int(idx) * ang,
                    "measured_distance": int(r_mm),
                    "front_edge": 0,
                    "back_edge": 0,
                    "reflectivity": int(refl),
                }
            )
        return out

    def measurement_from_full_triples(
        self,
        triples: list[tuple[int, int, int]],
        start_index: int,
        end_index: int,
        max_distance: int | None = None,
        *,
        emit_peak_log: bool = True,
    ) -> dict[str, Any] | None:
        """
        将已组好的一整圈 ``(index, mm, amp)`` 转为与 ``optimized_single_measurement`` 相同结构的测量字典。

        供 **GUI 连续测试后台组圈线程** 使用：TCP 仅由该线程读，主流程不再调用 ``assemble_one_full_scan``，
        避免与 ``optimized_single_measurement`` 抢同一套接字。
        """
        all_full = self._merged_to_gui_style(triples)
        lo, hi = int(start_index), int(end_index)
        all_results = [p for p in all_full if lo <= int(p["index"]) <= hi]
        if not all_results:
            self.last_error = (
                f"索引窗 [{lo},{hi}] 内无点（本圈索引约 {all_full[0]['index']}～{all_full[-1]['index']}）"
            )
            return None

        if max_distance is not None:
            filtered_results = [r for r in all_results if 10 < int(r["measured_distance"]) < int(max_distance)]
            has_consecutive = False
            consecutive_points: list[dict[str, Any]] = []
            out_results = filtered_results
        else:
            filtered_results = all_results
            has_consecutive = False
            consecutive_points = []
            out_results = all_results

        peak = _peak_reflectivity_point(filtered_results)
        if emit_peak_log:
            if peak is not None:
                pix, pr = peak
                self._emit_scan_log(
                    f"[R2] 总点数：{len(all_full)}，过滤后剩余点数：{len(filtered_results)}，"
                    f"最高反index： {pix}，最高反： {pr}"
                )
            else:
                self._emit_scan_log(
                    f"[R2] 总点数：{len(all_full)}，过滤后剩余点数：{len(filtered_results)}，"
                    "最高反index： —，最高反： —"
                )

        return {
            "total_count": len(all_results),
            "filtered_count": len(filtered_results),
            "results": out_results,
            "all_results": all_results,
            "has_consecutive_qualified": has_consecutive,
            "consecutive_points": consecutive_points,
            "start_index": start_index,
            "end_index": end_index,
            "angular_resolution_deg": self.angular_resolution_deg,
            "scan_angle_range_deg": 360.0,
            "start_angle_deg": self.start_angle_deg,
            "expected_point_count": int(self._points_per_circle),
        }

    def optimized_single_measurement(
        self,
        start_index: int,
        end_index: int,
        max_distance: int | None = None,
        *,
        discard_stream_circles: int | None = None,
        favor_latest_stream: bool | None = None,
    ) -> dict[str, Any] | None:
        if self._data_sock is None:
            self.last_error = "雷达未连接"
            return None
        do_favor = (
            bool(self.stream_favor_latest_tcp)
            if favor_latest_stream is None
            else bool(favor_latest_stream)
        )
        prep = str(self.stream_prep_mode).lower().strip()
        n_disc = (
            int(self.stream_discard_circles_before_sample)
            if discard_stream_circles is None
            else int(discard_stream_circles)
        )
        n_disc = max(0, n_disc)
        # 未显式覆盖丢圈数时，用「下限」保证用于业务的那一圈前面有足够多的纯丢弃整圈（时间换新鲜度）。
        if discard_stream_circles is None and bool(self.stream_accuracy_first) and do_favor:
            if prep == "stop_tcp":
                n_disc = max(n_disc, 1)
            elif prep == "drain":
                n_disc = max(n_disc, 2)
        try:
            # TCP 无法绕过内核接收队列；``drain`` 用快读 + 静默尾捕；``stop_tcp`` 则换套接字 + 新端口硬切流。
            if do_favor:
                if prep == "stop_tcp":
                    self._pause_data_tcp_and_reopen()
                    self._emit_scan_log(
                        "[R2] 测量前已 stop+release 并重新 request_handle_tcp/start，"
                        "已接上新数据 TCP 端口（丢弃旧连接的内核缓冲）"
                    )
                elif prep == "none":
                    pass
                else:
                    if prep != "drain":
                        self._emit_scan_log(
                            f"[R2] 未知 stream_prep_mode={self.stream_prep_mode!r}，按 drain 处理"
                        )
                    n_fast = drain_scan_tcp_complete_packets(
                        self._data_sock,
                        max_packets=int(self.drain_tcp_max_packets),
                        max_wall_s=float(self.drain_tcp_max_wall_s),
                        tail_quiet_polls=int(self.drain_tcp_tail_quiet_polls),
                        tail_quiet_poll_s=float(self.drain_tcp_tail_quiet_poll_s),
                        tail_only_after_reads=True,
                    )
                    if n_fast > 0:
                        self._emit_scan_log(
                            f"[R2] 已排空 TCP 已到达队列：丢弃完整扫描包 {n_fast} 个，下一圈取最新到达数据"
                        )
            # 再按「整圈」丢弃若干次：与 ``stream_accuracy_first`` 下限叠加后，stop_tcp 至少 1 圈、drain 至少 2 圈（在未显式传 ``discard_stream_circles`` 时）。
            if n_disc > 0:
                self._emit_scan_log(
                    f"[R2] 测量前再丢弃 TCP 流中 {n_disc} 整圈缓冲（避免沿用排队中的旧点云）"
                )
            for _ in range(n_disc):
                assemble_one_full_scan(
                    self._data_sock,
                    self._points_per_circle,
                    deadline_s=float(self.full_scan_deadline_s),
                    wait_packet_index_one=bool(self.assemble_wait_packet_index_one),
                    scan_log=None,
                )
            triples = assemble_one_full_scan(
                self._data_sock,
                self._points_per_circle,
                deadline_s=float(self.full_scan_deadline_s),
                wait_packet_index_one=bool(self.assemble_wait_packet_index_one),
                scan_log=self._emit_scan_log,
            )
        except (R2ProtocolError, OSError) as exc:
            self.last_error = str(exc)
            return None

        return self.measurement_from_full_triples(
            triples, start_index, end_index, max_distance, emit_peak_log=True
        )
