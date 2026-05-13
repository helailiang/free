#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
R2 雷达（HTTP 控制 + ``start_scanoutput`` 连续 TCP 点云）+ Y100SC 连续步进测试（PySide6）。

与 ``h2_resolution_gui_test.py`` 功能对齐：转台步进、距离窗内多帧反射主峰中位数、邻 index±Δ 等。
雷达侧无 H2 表 4-2 单次指令；采用 ``r2_radar_client``：先 **HTTP** ``request_handle_tcp`` 取数据 TCP 端口，
再 **HTTP** ``start_scanoutput`` 成功后才连 TCP 收流；断开时 **HTTP** ``stop_scanoutput`` 再关 TCP。
「连续测试」在 step0 之后启动 **后台组圈线程**（仅步进阶段）：独占 TCP 不断 ``assemble_one_full_scan``，只保留最新一整圈，
每步探测通过 ``wait_newer_than`` 取新圈，避免与步内多次 ``optimized_single_measurement`` 抢读同一套接字。
整圈 **360° = 3600** 点（index ``0…3599``），与 ``R2_FULL_CIRCLE_POINT_COUNT`` 一致。

协议依据：``.cursor/skills/r2-lidar-protocol/R2雷达通讯协议说明.md``（STEP5～7、表 4-6）。

运行（在项目根或 ``R2`` 目录均可，需能解析到「码盘补偿」下的依赖模块）:
  python R2/r2_resolution_gui_test.py
  或: cd R2 && python r2_resolution_gui_test.py

导出：默认保存路径为仓库 ``R2/export/``（与协议代码同产品目录下的结果子文件夹），
首次导出前会自动创建；仍可在对话框中改存到其他位置。

依赖: PySide6、pyserial；可选 openpyxl；运行时会将项目根与 ``码盘补偿`` 加入 ``sys.path``。
"""
from __future__ import annotations

import sys
from pathlib import Path


def _bootstrap_sys_path_and_base() -> Path:
    """
    配置 ``sys.path`` 并返回「逻辑仓库根」目录。

    - **开发环境**：``__file__`` 在 ``R2/`` 或「码盘补偿」下时，``parents[1]`` 为仓库根；
      将仓库根与 ``码盘补偿`` 子目录插入 ``sys.path``，以便导入同目录下的
      ``r2_radar_client``、``newpre_resolution_cli_test``、``y100sc_client`` 以及包 ``R2``。
    - **PyInstaller 单文件/目录打包**：可执行体运行时模块在 ``sys._MEIPASS`` 下展开，
      依赖已由分析器打入该目录，仅需把 ``_MEIPASS`` 置于 ``sys.path`` 首部即可；
      此时不应再依赖磁盘上的「码盘补偿」相对路径。
    """
    if getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", None):
        base = Path(sys._MEIPASS)  # type: ignore[arg-type]
        s = str(base)
        if s not in sys.path:
            sys.path.insert(0, s)
        return base
    repo = Path(__file__).resolve().parents[1]
    ma = repo / "码盘补偿"
    for d in (repo, ma):
        t = str(d)
        if t not in sys.path:
            sys.path.insert(0, t)
    return repo


_bootstrap_sys_path_and_base()

import statistics
import threading
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any


def _r2_output_dir() -> Path:
    """
    角分辨率 GUI 导出文件的默认目录。

    - **开发**：仓库 ``R2/export/``（与 ``r2_client.py`` 同产品目录）。
    - **打包 exe**：在可执行文件所在目录下创建 ``R2_export``，避免写入只读解压区，
      且用户通常期望结果与 exe 同处便于拷贝。
    """
    if getattr(sys, "frozen", False) and getattr(sys, "executable", None):
        p = Path(sys.executable).resolve().parent / "R2_export"
    else:
        p = Path(__file__).resolve().parents[1] / "R2" / "export"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _r2_default_export_path(filename_stem: str) -> str:
    """生成 ``R2/export/<stem>_YYYYMMDD_HHMMSS>`` 默认路径（无扩展名，由对话框选择后缀）。"""
    return str(_r2_output_dir() / f"{filename_stem}_{time.strftime('%Y%m%d_%H%M%S')}")


from PySide6.QtCore import Qt, QThread, Signal, Slot
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from r2_radar_client import R2ProtocolError, R2SingleScanRadar, assemble_one_full_scan

# 在本文件内显式固定「可接受的测距包类型」：说明书 0x0043('C')；实机常见 0x0041('A')（探测失败原报错即此）。
# 解析使用 ``R2.r2_client.parse_scan_packet`` 读取本集合，运行期赋值即可生效。
import R2.r2_client as _r2_tcp_pkt  # noqa: E402

_r2_tcp_pkt.R2_SCAN_PACKET_TYPES_ACCEPTED = frozenset({0x0043, 0x0041})

# 点云「反射率」来自 TCP 每点 4 字节的幅度字段；若探测日志里反射率恒为 0，多为固件未写高 12 位
# 或位序与表 4-6 OCR 不一致。可在下面改 ``R2_POINT_PACK_MODE`` 后重连雷达试：
# ``dist20_amp12``(默认) / ``amp12_dist20`` / ``nibble20_12`` / ``u16u16`` — 详见 ``R2/r2_client.decode_r2_point_four_bytes``。
_r2_tcp_pkt.R2_POINT_PACK_MODE = "dist20_amp12"

from newpre_resolution_cli_test import (
    build_step_row,
    export_all_index_points,
    export_rows,
    fmt_rotate_cumulative,
    lookup_point_by_index,
    move_turntable,
)
from y100sc_client import Axis, Sign, Y100SCError

# 连续测试（R2SequenceThread）中，每帧「最高反射」候选点相对本步 targetIndex 允许的最大 index 半宽（含边界）。
# 即仅在 [target−N, target+N] 与距离窗的交集中取反射率最大点，避免全圈扫描时远端偶然高反干扰中位数。
CONTINUOUS_MAX_REFLECT_INDEX_NEIGHBOR_HALF_SPAN: int = 300


def _probe_prep_log_line(radar: R2SingleScanRadar | None) -> str:
    """
    根据 ``R2SingleScanRadar`` 上的 ``stream_prep_mode`` / ``stream_favor_latest_tcp`` 生成探测日志文案。

    说明：
      - ``stream_favor_latest_tcp`` 为 False 时，``optimized_single_measurement`` 会跳过 drain 与 stop_tcp，
        本函数在日志里显式写出，避免操作员误以为仍在「取最新」路径上。
      - 否则按 ``stream_prep_mode`` 区分 drain / stop_tcp / none 三种摘要。
    """
    if radar is None:
        return "测量前策略：默认（stop_tcp，见 r2_radar_client）"
    if not bool(getattr(radar, "stream_favor_latest_tcp", True)):
        return "测量前策略：已关闭 stream_favor_latest_tcp（不做 drain / stop_tcp 预清理）"
    prep = str(getattr(radar, "stream_prep_mode", "stop_tcp")).lower().strip()
    if prep == "stop_tcp":
        acc = bool(getattr(radar, "stream_accuracy_first", True))
        return (
            "测量前策略：stop+release+重新 request_handle_tcp（新数据口）；"
            + ("默认另至少丢 1 整圈再走业务圈（accuracy_first）" if acc else "丢圈数由 stream_discard_circles 决定")
        )
    if prep == "none":
        return "测量前策略：stream_prep_mode=none（不 drain、不停流；仍可按 discard_circles 丢整圈）"
    acc = bool(getattr(radar, "stream_accuracy_first", True))
    return (
        "测量前策略：drain（快读+静默尾捕）；"
        + ("默认另至少丢 2 整圈再走业务圈（accuracy_first）" if acc else "丢圈数由 stream_discard_circles 决定")
    )


def top_n_reflectivity_in_window(
    all_results: list[dict[str, Any]],
    min_mm: int,
    max_mm: int,
    n: int = 10,
    *,
    index_center: int | None = None,
    index_neighbor_half_span: int | None = None,
) -> list[dict[str, Any]]:
    """
    距离窗内按反射率降序、index 升序，取前 n 个点（每条为解析字典）。

    当给定 index_center 时，在距离窗筛选之后**再**按点云 index 收窄到
    [index_center − half_span, index_center + half_span]（闭区间）；half_span 缺省为
    CONTINUOUS_MAX_REFLECT_INDEX_NEIGHBOR_HALF_SPAN。用于连续步进测试，使「主峰」搜索
    与当前理论目标 index 对齐，而非在全圈所有 index 上取全局最大反射。
    """
    cand = [p for p in all_results if min_mm < int(p["measured_distance"]) < max_mm]
    if index_center is not None:
        half = (
            int(index_neighbor_half_span)
            if index_neighbor_half_span is not None
            else CONTINUOUS_MAX_REFLECT_INDEX_NEIGHBOR_HALF_SPAN
        )
        c = int(index_center)
        lo, hi = c - half, c + half
        cand = [p for p in cand if lo <= int(p["index"]) <= hi]
    cand.sort(key=lambda p: (-int(p["reflectivity"]), int(p["index"])))
    return cand[: max(0, n)]


def pick_max_reflectivity_index(
    all_results: list[dict[str, Any]],
    min_mm: int,
    max_mm: int,
    *,
    index_center: int | None = None,
    index_neighbor_half_span: int | None = None,
) -> int | None:
    """
    在距离窗内取反射率最大的点索引（同强度时取较小 index）。

    index_center 非空时与 top_n_reflectivity_in_window 相同：只在目标 index 邻域内比较反射率。
    """
    top = top_n_reflectivity_in_window(
        all_results,
        min_mm,
        max_mm,
        1,
        index_center=index_center,
        index_neighbor_half_span=index_neighbor_half_span,
    )
    if not top:
        return None
    return int(top[0]["index"])


def reflectivity_peak_and_subpeak(
    all_results: list[dict[str, Any]], min_mm: int, max_mm: int
) -> tuple[int, int, int | None] | None:
    """
    在距离窗内同时解析「主峰」与「次高反射」点（排序规则与 top_n_reflectivity_in_window 一致：
    反射率降序，同率时 index 升序）。

    起始索引仍取主峰 index（与 pick_max_reflectivity_index 一致）。主峰与次峰反射率差
    (r_top - r_second) 越大，通常表示主峰越孤立、后续多帧取中位索引时抖动越小，便于评估本次探测质量。

    Returns:
        (主峰 index, 主峰反射率 r_top, 次高反射率 r_second)；窗内无点时返回 None；
        窗内仅 1 个有效点时 r_second 为 None（不存在「另一条」次高点）。

    注意：供 R2ProbeReflectThread 使用，不在 index 轴上做「连续高反±index」收窄；独立探测每帧
    在整圈 ``0 … R2_FULL_CIRCLE_POINT_COUNT-1`` 上取数后再在距离窗内比较。连续测试的 index 邻域收窄仅作用于
    ``_median_max_reflect_probe`` 内对 ``pick_max_reflectivity_index`` 的调用。
    """
    top = top_n_reflectivity_in_window(all_results, min_mm, max_mm, 2)
    if not top:
        return None
    r1 = int(top[0]["reflectivity"])
    ix = int(top[0]["index"])
    if len(top) < 2:
        return (ix, r1, None)
    r2 = int(top[1]["reflectivity"])
    return (ix, r1, r2)


def probe_indices_display_and_stats(
    per_frame_max_indices: list[int], target_index: int, probe_median_index: int
) -> tuple[str, int, int, str, int]:
    """
    由每帧「距离窗内（及连续测试开启时 target±半宽 index 内）反射率最高」的 index 序列生成表格/导出字段：
    - 逗号分隔的各次索引（时间顺序）；
    - 该序列中出现次数的全局最大值（任一 index 的最多命中次数）；
    - targetIndex − 中位最高反射索引（有符号，便于看目标相对光斑中心偏哪侧）；
    - 序列中是否至少一次等于 targetIndex（是/否），以及 targetIndex 在该序列中的出现次数
      （与独立「探测最高反射」里对目标索引的统计一致，便于看逐次主峰是否常落在目标点上）。
    """
    seq = ",".join(str(x) for x in per_frame_max_indices)
    max_hit = max(Counter(per_frame_max_indices).values()) if per_frame_max_indices else 0
    delta = int(target_index) - int(probe_median_index)
    tgt = int(target_index)
    target_in_sequence_count = sum(1 for x in per_frame_max_indices if int(x) == tgt)
    sequence_contains_target = "是" if target_in_sequence_count > 0 else "否"
    return seq, max_hit, delta, sequence_contains_target, target_in_sequence_count


def compute_probe_peak_neighbor_row_fields(
    probe_peak_indices: list[int],
    probe_measurements: list[dict[str, Any]],
    index_delta: int,
    probe_median_index: int,
) -> tuple[dict[str, Any], list[str]]:
    """
    连续测试每一行：对「每帧距离窗内最高反射 index」为峰，在该帧点云上取 peak±index_delta 的
    measured_distance（mm），再对多帧分别得到左邻距、右邻距序列取**算术均值**写入表；
    左右距差列为「右邻距均值 − 左邻距均值」。index 列展示为「反射中位 index±Δ」（便于对照中位峰），
    与各帧实际 peak 可能略有不同。

    另给出左、右邻距各自的**样本标准差**（mm，字符串，有效样本不足 2 时为「—」），以及按帧顺序的
    **邻距明细串**（分号分隔，缺帧写「—」），供 Excel 导出列展示。

    index_delta<=0 或 probe 序列为空时：邻距相关字段为空字符串，无告警。

    Returns:
        可直接展开传入 build_step_row 的关键字参数字典；
        第二项为须并入该行 anomalies 的短语列表（如某帧缺左邻点）。
    """
    empty: dict[str, Any] = {
        "neighbor_index_offset": "",
        "neighbor_left_index": "",
        "neighbor_left_distance_mm": "",
        "neighbor_right_index": "",
        "neighbor_right_distance_mm": "",
        "neighbor_lr_distance_diff_mm": "",
        "neighbor_left_std_mm": "",
        "neighbor_right_std_mm": "",
        "neighbor_left_distances_detail_mm": "",
        "neighbor_right_distances_detail_mm": "",
    }
    if index_delta <= 0 or not probe_peak_indices:
        return empty, []
    if len(probe_peak_indices) != len(probe_measurements):
        raise ValueError(
            "compute_probe_peak_neighbor_row_fields：peak 序列与 measurement 序列长度须一致"
        )
    d = int(index_delta)
    med = int(probe_median_index)
    lix_disp = med - d
    rix_disp = med + d
    left_mm_list: list[int] = []
    right_mm_list: list[int] = []
    left_detail_parts: list[str] = []
    right_detail_parts: list[str] = []
    miss_left = 0
    miss_right = 0
    for peak, m in zip(probe_peak_indices, probe_measurements):
        all_r = m.get("all_results") or []
        pk = int(peak)
        pl = lookup_point_by_index(all_r, pk - d)
        pr = lookup_point_by_index(all_r, pk + d)
        if pl is None:
            miss_left += 1
            left_detail_parts.append("—")
        else:
            lv = int(pl["measured_distance"])
            left_mm_list.append(lv)
            left_detail_parts.append(str(lv))
        if pr is None:
            miss_right += 1
            right_detail_parts.append("—")
        else:
            rv = int(pr["measured_distance"])
            right_mm_list.append(rv)
            right_detail_parts.append(str(rv))
    warn: list[str] = []
    if miss_left:
        warn.append(f"主峰左邻缺{miss_left}帧")
    if miss_right:
        warn.append(f"主峰右邻缺{miss_right}帧")

    l_mean: int | str = round(statistics.mean(left_mm_list)) if left_mm_list else ""
    r_mean: int | str = round(statistics.mean(right_mm_list)) if right_mm_list else ""
    diff_lr: int | str = ""
    if isinstance(l_mean, int) and isinstance(r_mean, int):
        diff_lr = int(r_mean) - int(l_mean)

    def _side_std_mm(vals: list[int]) -> str:
        # 至少 2 个样本点才能计算与样本均值对应的 stdev（与界面「σ」预期一致）。
        if len(vals) >= 2:
            return f"{statistics.stdev(vals):.2f}"
        return "—"

    left_std_s = _side_std_mm(left_mm_list)
    right_std_s = _side_std_mm(right_mm_list)
    left_detail_s = ";".join(left_detail_parts) if left_detail_parts else ""
    right_detail_s = ";".join(right_detail_parts) if right_detail_parts else ""

    return {
        "neighbor_index_offset": d,
        "neighbor_left_index": lix_disp,
        "neighbor_left_distance_mm": l_mean,
        "neighbor_right_index": rix_disp,
        "neighbor_right_distance_mm": r_mean,
        "neighbor_lr_distance_diff_mm": diff_lr,
        "neighbor_left_std_mm": left_std_s,
        "neighbor_right_std_mm": right_std_s,
        "neighbor_left_distances_detail_mm": left_detail_s,
        "neighbor_right_distances_detail_mm": right_detail_s,
    }, warn


def _neighbor_mean_std_line(label: str, vals: list[int]) -> str:
    """邻距统计单行：均值、标准差与 n；单点时不报 σ。"""
    n = len(vals)
    if n == 0:
        return ""
    mu = statistics.mean(vals)
    if n >= 2:
        sig = statistics.stdev(vals)
        return f"{label}均值 {mu:.1f} mm，标准差 {sig:.1f} mm（n={n}）"
    return f"{label}均值 {mu:.1f} mm，标准差 —（n={n}）"


def neighbor_stats_plain(rows: list[dict[str, Any]]) -> str:
    """
    左/右邻距及左右距差的汇总短语，用中文分号连接；无邻距数据时返回空串。
    供 compose_h2_final_conclusion 复用（邻距段用中文分号连接）。
    """
    lefts: list[int] = []
    rights: list[int] = []
    diffs: list[int] = []
    for r in rows:
        off = r.get("neighbor_index_offset", "")
        if off == "" or (isinstance(off, int) and off <= 0):
            continue
        lm = r.get("neighbor_left_distance_mm", "")
        rm = r.get("neighbor_right_distance_mm", "")
        df = r.get("neighbor_lr_distance_diff_mm", "")
        if isinstance(lm, int):
            lefts.append(lm)
        if isinstance(rm, int):
            rights.append(rm)
        if isinstance(df, int):
            diffs.append(df)
    if not lefts and not rights:
        return ""
    parts: list[str] = []
    if lefts:
        parts.append(_neighbor_mean_std_line("左邻距", lefts))
    if rights:
        parts.append(_neighbor_mean_std_line("右邻距", rights))
    if diffs:
        parts.append(f"左右距差(右−左)均值 {statistics.mean(diffs):.1f} mm（n={len(diffs)}）")
    return "；".join(parts)


def target_index_range_text(rows: list[dict[str, Any]]) -> str:
    """各步 target_index 的最小～最大，用于最终结论首段。"""
    if not rows:
        return "目标索引范围 —"
    idxs = [int(r["target_index"]) for r in rows]
    mn, mx = min(idxs), max(idxs)
    return f"目标索引范围 {mn}～{mx}"


def radar_angle_rotation_range_text(rows: list[dict[str, Any]]) -> str:
    """
    各步目标点 `radar_angle_deg`（与导出列「雷达角(°)」同源）的最小～最大，
    表示本次会话中目标点在点云里的雷达角跨度。
    """
    if not rows:
        return "雷达角旋转角度范围 —"
    angs = [float(r.get("radar_angle_deg", r["angle_deg"])) for r in rows]
    mn, mx = min(angs), max(angs)
    return f"雷达角旋转角度范围 {mn:+.1f}°～{mx:+.1f}°"


def probe_match_rate_plain(rows: list[dict[str, Any]]) -> str:
    """反射中位与 target 一致率一句，无 leading「 | 」。"""
    flagged = [r for r in rows if r.get("probe_match_target") in ("是", "否")]
    if not flagged:
        return "反射中位=target：无数据"
    ok = sum(1 for r in flagged if r.get("probe_match_target") == "是")
    n = len(flagged)
    return f"反射中位=target 一致 {ok}/{n} = {ok / n:.2%}"


def compose_h2_final_conclusion(rows: list[dict[str, Any]]) -> str:
    """
    连续测试单行最终结论（界面 / 日志 / Excel 仅此一行，无「汇总」明细；R2/H2 共用列结构）：
    目标索引范围 a～b |雷达角旋转角度范围 c°～d° |（三空格）反射中位一致率 |（单空格）邻距分号段。
    「|雷达角」前无空格，与约定示例一致。
    """
    tix = target_index_range_text(rows)
    rag = radar_angle_rotation_range_text(rows)
    probe = probe_match_rate_plain(rows)
    neigh = neighbor_stats_plain(rows)
    head = f"{tix} |{rag}"
    out = f"{head} |   {probe}"
    if neigh:
        out += f" | {neigh}"
    return out


TABLE_HEADERS = [
    "step",
    "转台角度",
    "targetIndex",
    "最高反射中位索引",
    "中位=target",
    "各次最高反射索引",
    "序列含目标",
    "目标在序列中次数",
    "最多命中次数",
    "目标−中位Δ",
    "index偏移",
    "雷达测得角(°)",
    "理论角度(°)",
    "角度误差(°)",
    "距离(m)",
    "强度",
    "邻indexΔ",
    "左邻index",
    "左邻距mm",
    "左邻距σ(mm)",
    "右邻index",
    "右邻距mm",
    "右邻距σ(mm)",
    "左右距差mm",
    "异常",
]


@dataclass
class R2GuiConfig:
    radar_ip: str
    # R2：HTTP 命令端口（默认 80），与 H2 的 TCP 2111 不同。
    radar_port: int
    index_start: int
    index_end: int
    min_distance_mm: int
    max_distance_mm: int
    angular_res_deg: float
    step_angle_deg: float
    start_index: int
    com: str
    baud: int
    axis: Axis
    direction: Sign
    pulses_per_step: int
    settle_ms: int
    steps: int
    scan_start_deg: float
    # 每步转台到位后：在距离窗内连续采集此帧数；每帧最高反射 index 的选取规则见 continuous_probe_index_half_span。
    per_step_probe_repeat: int
    # 相对 targetIndex 在点云 index 轴上左右各偏移若干 index 取邻点测距（mm）；0 表示不取邻点。
    neighbor_index_delta: int
    # 连续测试每帧「最高反射」候选：在距离窗内再限制为 [target−N, target+N]（闭区间，N 为本字段）。
    # N≤0 时不做 index 收窄，在距离窗内对全索引起候选（与独立探测的全窗语义一致，仅用于连续步进）。
    continuous_probe_index_half_span: int


class UserStopSequence(Exception):
    """用户在步进或每步反射探测循环中请求停止（不视为采集失败）。"""


class R2GuiCircleProducer:
    """
    仅「连续测试」步进阶段使用：后台线程独占数据 TCP，循环 ``assemble_one_full_scan``，
    内存中只保留**最新一整圈** ``(index, mm, amp)``；无人消费时新圈覆盖旧圈。

    消费侧通过 ``wait_newer_than(seq, timeout)`` 阻塞直到 ``seq`` 增大，再经
    ``R2SingleScanRadar.measurement_from_full_triples`` 做索引起止与距离窗裁剪。

    生命周期：由 ``R2SequenceThread`` 在 step0 完成后 ``start()``，步进循环 ``finally`` 中 ``stop_and_join()``。
    """

    def __init__(self, radar: R2SingleScanRadar) -> None:
        self._radar = radar
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._seq: int = 0
        self._latest: list[tuple[int, int, int]] | None = None
        self._fatal: str | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.stop_and_join()
        self._stop = threading.Event()
        self._fatal = None
        self._seq = 0
        self._latest = None
        self._thread = threading.Thread(target=self._run_loop, name="R2GuiCircleProducer", daemon=True)
        self._thread.start()

    def stop_and_join(self, join_s: float = 6.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=float(join_s))
            self._thread = None

    def current_seq(self) -> int:
        with self._lock:
            return int(self._seq)

    def wait_newer_than(self, seq: int, timeout_s: float) -> tuple[list[tuple[int, int, int]], int]:
        deadline = time.monotonic() + float(timeout_s)
        with self._cv:
            while int(self._seq) <= int(seq):
                if self._fatal is not None:
                    raise RuntimeError(self._fatal)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"等待新一圈超时（>{float(timeout_s):.1f}s）；当前 seq={self._seq} 需要 > {seq}"
                    )
                self._cv.wait(timeout=min(0.5, remaining))
            assert self._latest is not None
            return list(self._latest), int(self._seq)

    def _run_loop(self) -> None:
        n_pts = int(getattr(self._radar, "_points_per_circle", int(_r2_tcp_pkt.R2_FULL_CIRCLE_POINT_COUNT)))
        while not self._stop.is_set():
            sock = self._radar.socket
            if sock is None:
                with self._cv:
                    self._fatal = "数据 TCP 未连接"
                    self._cv.notify_all()
                return
            try:
                triples = assemble_one_full_scan(
                    sock,
                    n_pts,
                    deadline_s=float(self._radar.full_scan_deadline_s),
                    wait_packet_index_one=bool(self._radar.assemble_wait_packet_index_one),
                    scan_log=None,
                )
            except (R2ProtocolError, OSError) as exc:
                if self._stop.is_set():
                    return
                with self._cv:
                    self._fatal = str(exc)
                    self._cv.notify_all()
                return
            with self._cv:
                self._latest = list(triples)
                self._seq += 1
                self._cv.notify_all()


class R2SequenceThread(QThread):
    progress = Signal(int, int, object)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        cfg: R2GuiConfig,
        external_radar: R2SingleScanRadar | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._external_radar = external_radar
        self._stop = False

    def stop(self) -> None:
        self._stop = True
        self.requestInterruption()

    def _sleep_interruptible(self, total_s: float, chunk_s: float = 0.05) -> bool:
        """分段睡眠以便尽快响应停止；若用户停止返回 False。"""
        if total_s <= 0:
            return not (self._stop or self.isInterruptionRequested())
        deadline = time.monotonic() + total_s
        while time.monotonic() < deadline:
            if self._stop or self.isInterruptionRequested():
                return False
            remaining = deadline - time.monotonic()
            time.sleep(max(0.0, min(chunk_s, remaining)))
        return True

    def _continuous_fetch_measurement(
        self,
        cfg: R2GuiConfig,
        radar: R2SingleScanRadar,
        producer: R2GuiCircleProducer,
        seq_gate: list[int],
        fail_hint: str,
    ) -> dict[str, Any]:
        timeout_s = max(45.0, float(radar.full_scan_deadline_s) * 3.0)
        triples, new_seq = producer.wait_newer_than(int(seq_gate[0]), timeout_s)
        seq_gate[0] = int(new_seq)
        m = radar.measurement_from_full_triples(
            triples,
            cfg.index_start,
            cfg.index_end,
            cfg.max_distance_mm,
            emit_peak_log=False,
        )
        if not m:
            raise RuntimeError(radar.last_error or fail_hint)
        return m

    def _median_max_reflect_probe(
        self,
        cfg: R2GuiConfig,
        radar: R2SingleScanRadar,
        fail_prefix: str,
        seed_measurement: dict[str, Any] | None,
        reflectivity_index_center: int,
        *,
        circle_producer: R2GuiCircleProducer | None = None,
        seq_gate: list[int] | None = None,
    ) -> tuple[int, list[int], dict[str, Any], list[dict[str, Any]]]:
        """
        在 (min_distance_mm, max_distance_mm) 距离窗内，每帧取反射率最高的 index（同率取较小 index），
        得到若干次采样后取中位数。若 seed_measurement 非空，则首点用该帧点云，其余再采 per_step_probe_repeat-1 帧。

        与独立按钮「探测最高反射索引」的差异：当 cfg.continuous_probe_index_half_span>0 时，此处仅在
        本步理论 targetIndex（reflectivity_index_center）±该半宽的 index 闭区间内参与「谁反射率最高」
        的比较，避免全圈远处高反误选；半宽≤0 时不在 index 轴收窄。按钮探测始终对全 index + 距离窗。

        Returns:
            (反射中位 index, 各帧主峰 index 列表, 最后一帧 measurement 字典, 与各帧主峰顺序一致的 measurement 列表)
            最后一项供邻距按「各帧主峰±Δ」取距后做均值/标准差。
        """
        indices: list[int] = []
        probe_measurements: list[dict[str, Any]] = []
        last_m: dict[str, Any] = {}
        center = int(reflectivity_index_center)
        hs = int(cfg.continuous_probe_index_half_span)
        restrict_index = hs > 0
        if seed_measurement is not None:
            if self._stop or self.isInterruptionRequested():
                raise UserStopSequence
            all0 = seed_measurement.get("all_results") or []
            ix0 = pick_max_reflectivity_index(
                all0,
                cfg.min_distance_mm,
                cfg.max_distance_mm,
                **(
                    {"index_center": center, "index_neighbor_half_span": hs}
                    if restrict_index
                    else {}
                ),
            )
            if ix0 is None:
                if restrict_index:
                    raise RuntimeError(
                        f"{fail_prefix}：在 targetIndex={center}±{hs} 与距离窗的交集内无有效点"
                    )
                raise RuntimeError(f"{fail_prefix}：距离窗内无有效点")
            indices.append(ix0)
            probe_measurements.append(seed_measurement)
            last_m = seed_measurement
            need_more = cfg.per_step_probe_repeat - 1
        else:
            need_more = cfg.per_step_probe_repeat

        for j in range(need_more):
            if self._stop or self.isInterruptionRequested():
                raise UserStopSequence
            if circle_producer is not None and seq_gate is not None:
                m = self._continuous_fetch_measurement(
                    cfg,
                    radar,
                    circle_producer,
                    seq_gate,
                    f"{fail_prefix}：第{len(indices) + 1}次探测采集失败",
                )
            else:
                m = radar.optimized_single_measurement(
                    cfg.index_start, cfg.index_end, cfg.max_distance_mm
                )
                if not m:
                    raise RuntimeError(
                        radar.last_error or f"{fail_prefix}：第{len(indices) + 1}次探测采集失败"
                    )
            all_r = m.get("all_results") or []
            ix = pick_max_reflectivity_index(
                all_r,
                cfg.min_distance_mm,
                cfg.max_distance_mm,
                **(
                    {"index_center": center, "index_neighbor_half_span": hs}
                    if restrict_index
                    else {}
                ),
            )
            if ix is None:
                if restrict_index:
                    raise RuntimeError(
                        f"{fail_prefix}：第{len(indices) + 1}次在 targetIndex={center}±{hs} 与距离窗交集内无有效点"
                    )
                raise RuntimeError(
                    f"{fail_prefix}：第{len(indices) + 1}次距离窗内无有效点"
                )
            indices.append(ix)
            probe_measurements.append(m)
            last_m = m
            if j < need_more - 1 and not self._sleep_interruptible(0.12):
                raise UserStopSequence
        med = int(statistics.median(indices))
        return med, indices, last_m, probe_measurements

    def run(self) -> None:
        cfg = self._cfg
        # 单连接雷达：若主界面已连同一 IP/端口，必须复用，不可再 connect 第二次。
        if self._external_radar is not None:
            radar = self._external_radar
            own_socket = False
        else:
            radar = R2SingleScanRadar(host=cfg.radar_ip, cmd_port=int(cfg.radar_port))
            own_socket = True
        # R2 无 H2 登录帧；仅将 GUI 角分辨率/起始角写入客户端，用于由 index 推算 angle_deg。
        radar.configure_scan_parameters(
            angular_resolution_deg=cfg.angular_res_deg,
            start_angle_deg=cfg.scan_start_deg,
        )
        if own_socket and not radar.connect_radar():
            self.failed.emit(radar.last_error or "雷达连接失败")
            return
        # 与现场机械/坐标约定一致：转台 UI 选「+」时，目标点在点云 index 上表现为向负侧累进
        # （init_idx - k）；选「-」时表现为向正侧累进（init_idx + k）。硬件 move_turntable 仍按 UI 方向发脉冲。
        sign = -1 if cfg.direction == "+" else 1
        rows: list[dict[str, Any]] = []

        try:
            self._run_sequence(cfg, radar, sign, rows)
        except Y100SCError as e:
            self.failed.emit(f"转台/控制器：{e}")
        except (OSError, ValueError, RuntimeError) as e:
            self.failed.emit(str(e))
        except Exception as e:
            self.failed.emit(f"未预期错误：{e!s}")
        finally:
            if own_socket:
                radar.close()

    def _run_sequence(
        self,
        cfg: R2GuiConfig,
        radar: R2SingleScanRadar,
        sign: int,
        rows: list[dict[str, Any]],
    ) -> None:
        m0 = radar.optimized_single_measurement(
            cfg.index_start, cfg.index_end, cfg.max_distance_mm
        )
        if not m0:
            self.failed.emit(radar.last_error or "step0：采集失败")
            return
        init_idx = int(cfg.start_index)
        all0 = m0.get("all_results") or []
        p0 = lookup_point_by_index(all0, init_idx)
        if p0 is None:
            self.failed.emit(
                f"step0：点云中无起始索引 index={init_idx}（检查索引起止是否包含该 index）"
            )
            return
        init_mm = int(p0["measured_distance"])
        init_i = int(p0["reflectivity"])

        try:
            med0, ind0, _lm0, pm0 = self._median_max_reflect_probe(
                cfg, radar, "step0", m0, init_idx
            )
        except UserStopSequence:
            self.finished_ok.emit(rows)
            return
        except RuntimeError as e:
            self.failed.emit(str(e))
            return
        match0 = "是" if med0 == init_idx else "否"
        seq0, hit0, d_tgt_med0, seq_tgt0, seq_tgt_cnt0 = probe_indices_display_and_stats(
            ind0, init_idx, med0
        )

        nb0, nb_warn0 = compute_probe_peak_neighbor_row_fields(
            ind0, pm0, cfg.neighbor_index_delta, med0
        )
        an0 = "；".join(nb_warn0) if nb_warn0 else ""

        ts0 = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        row0 = build_step_row(
            step=0,
            rotate_label="0°",
            rotate_deg=0.0,
            target_index=init_idx,
            index_start=init_idx,
            index_end=init_idx,
            delta_index=0,
            measured_angle_deg=0.0,
            theory_angle_deg=0.0,
            error_deg=0.0,
            timestamp=ts0,
            anomalies=an0,
            raw=p0,
            probe_median_index=med0,
            probe_match_target=match0,
            probe_indices_sequence=seq0,
            probe_sequence_contains_target=seq_tgt0,
            probe_sequence_target_hit_count=seq_tgt_cnt0,
            probe_max_hit_count=hit0,
            target_minus_probe_median=d_tgt_med0,
            **nb0,
        )
        rows.append(row0)
        self.progress.emit(0, cfg.steps, row0)

        settle_s = cfg.settle_ms / 1000.0
        producer: R2GuiCircleProducer | None = None
        try:
            if cfg.steps >= 1:
                producer = R2GuiCircleProducer(radar)
                producer.start()
            for k in range(1, cfg.steps + 1):
                if self._stop or self.isInterruptionRequested():
                    break
                move_turntable(cfg.com, cfg.baud, cfg.axis, cfg.direction, cfg.pulses_per_step)
                if not self._sleep_interruptible(settle_s):
                    break
                # 连续测试：转台脉冲发完且 UI「到位等待」结束后，再留 50 ms 让机构振动略衰减再采整圈。
                if not self._sleep_interruptible(0.05):
                    break

                theory_idx = init_idx + sign * k
                theory_deg = sign * k * cfg.step_angle_deg

                seq_gate = [producer.current_seq()]
                try:
                    med_k, probe_indices, last_m, pm_k = self._median_max_reflect_probe(
                        cfg,
                        radar,
                        f"step{k}",
                        None,
                        theory_idx,
                        circle_producer=producer,
                        seq_gate=seq_gate,
                    )
                except UserStopSequence:
                    break
                except (RuntimeError, TimeoutError) as e:
                    self.failed.emit(str(e))
                    return
                match_k = "是" if med_k == theory_idx else "否"
                seq_k, hit_k, d_tgt_med_k, seq_tgt_k, seq_tgt_cnt_k = probe_indices_display_and_stats(
                    probe_indices, theory_idx, med_k
                )

                all_target_frame = last_m.get("all_results") or []
                pk = lookup_point_by_index(all_target_frame, theory_idx)
                if pk is None:
                    seq_gate[0] = producer.current_seq()
                    try:
                        m_fix = self._continuous_fetch_measurement(
                            cfg,
                            radar,
                            producer,
                            seq_gate,
                            f"step{k}：补采后仍无法取 target 点",
                        )
                    except (RuntimeError, TimeoutError) as e:
                        self.failed.emit(str(e))
                        return
                    all_target_frame = m_fix.get("all_results") or []
                    pk = lookup_point_by_index(all_target_frame, theory_idx)
                    if pk is None:
                        self.failed.emit(
                            f"step{k}：点云中无 index={theory_idx}（请扩大索引起止以覆盖起始索引±步数）"
                        )
                        return
                cur_idx = theory_idx
                d_idx = cur_idx - init_idx
                measured_deg = d_idx * cfg.angular_res_deg
                err_deg = measured_deg - theory_deg

                anomalies: list[str] = []
                dm = int(pk["measured_distance"])
                if abs(dm - init_mm) > max(500, int(0.05 * max(init_mm, 1))):
                    anomalies.append("距离突变")
                di = int(pk["reflectivity"])
                if init_i > 0 and abs(di - init_i) > max(80, int(0.5 * init_i)):
                    anomalies.append("强度异常")

                nbk, nb_warn_k = compute_probe_peak_neighbor_row_fields(
                    probe_indices, pm_k, cfg.neighbor_index_delta, med_k
                )
                anomalies.extend(nb_warn_k)

                ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
                row = build_step_row(
                    step=k,
                    rotate_label=fmt_rotate_cumulative(theory_deg),
                    rotate_deg=float(theory_deg),
                    target_index=cur_idx,
                    index_start="",
                    index_end="",
                    delta_index=int(d_idx),
                    measured_angle_deg=float(measured_deg),
                    theory_angle_deg=float(theory_deg),
                    error_deg=float(err_deg),
                    timestamp=ts,
                    anomalies="；".join(anomalies),
                    raw=pk,
                    probe_median_index=med_k,
                    probe_match_target=match_k,
                    probe_indices_sequence=seq_k,
                    probe_sequence_contains_target=seq_tgt_k,
                    probe_sequence_target_hit_count=seq_tgt_cnt_k,
                    probe_max_hit_count=hit_k,
                    target_minus_probe_median=d_tgt_med_k,
                    **nbk,
                )
                rows.append(row)
                self.progress.emit(k, cfg.steps, row)
        finally:
            if producer is not None:
                producer.stop_and_join()

        self.finished_ok.emit(rows)


class R2ProbeReflectThread(QThread):
    """
    独立按钮「探测最高反射索引」：连续若干帧采集，每帧在 **整圈** index（0…3599）上取点云，
    仅在 **距离窗** 内比较反射率取主峰（**不使用**界面「索引起/止」收窄，也 **不使用**「连续高反±index」），
    再对各帧主峰 index 取中位数写入起始索引。

    每帧额外记录「主峰反射率 − 次高反射率」差额（窗内唯一点时无次高），探测结束后汇总打印，
    便于判断本次光斑主峰是否足够突出（差额越大通常越利于后续连续步进的中位索引稳定）。

    另将「点击探测前」主界面起始索引视为目标索引，与各次主峰 index 序列比对，统计是否命中及出现次数
    （与连续测试表中 targetIndex 与逐帧最高反射 index 的对比语义一致，便于预判 step0 一致率）。
    """

    detail_log = Signal(str)
    # 第三参：各次探测的 (r_top - r_second)，无次高点时为 None（与窗内唯一点对应）。
    # 第四、五参：探测前记录的目标索引、该值在 indices 序列中的出现次数。
    finished_ok = Signal(int, object, object, int, int)
    failed = Signal(str)

    def __init__(
        self,
        radar: R2SingleScanRadar,
        max_distance_mm: int,
        min_distance_mm: int,
        repeat: int,
        angular_res_deg: float,
        scan_start_deg: float,
        probe_target_index: int,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._radar = radar
        self._max_distance_mm = max_distance_mm
        self._min_distance_mm = min_distance_mm
        self._repeat = max(1, repeat)
        self._angular_res_deg = angular_res_deg
        self._scan_start_deg = float(scan_start_deg)
        # 探测开始前主界面「起始索引」框的值，用于与各次采纳的 index 序列做包含/计数统计（不在线程内改 UI）。
        self._probe_target_index = int(probe_target_index)

    def run(self) -> None:
        self._radar.configure_scan_parameters(
            angular_resolution_deg=self._angular_res_deg,
            start_angle_deg=self._scan_start_deg,
        )
        indices: list[int] = []
        # 每帧主峰相对次峰的反射率差；None 表示该帧窗内只有一个有效点，无「次高」可比。
        per_shot_gaps: list[int | None] = []
        full_hi = int(_r2_tcp_pkt.R2_FULL_CIRCLE_POINT_COUNT) - 1
        for i in range(self._repeat):
            m = self._radar.optimized_single_measurement(0, full_hi, self._max_distance_mm)
            if not m:
                self.failed.emit(self._radar.last_error or f"探测第{i + 1}次：采集失败")
                return
            all_r = m.get("all_results") or []
            peak_info = reflectivity_peak_and_subpeak(
                all_r, self._min_distance_mm, self._max_distance_mm
            )
            if peak_info is None:
                self.failed.emit(f"探测第{i + 1}次：距离窗内无有效点")
                return
            ix, r_top, r_second = peak_info
            gap: int | None = (r_top - r_second) if r_second is not None else None
            per_shot_gaps.append(gap)
            top10 = top_n_reflectivity_in_window(
                all_r, self._min_distance_mm, self._max_distance_mm, 10
            )
            lines = [
                f"  #{j + 1:2d}  index={int(p['index']):4d}  反射率={int(p['reflectivity']):5d}  "
                f"距离={int(p['measured_distance'])}mm  angle={float(p['angle_deg']):.2f}°"
                for j, p in enumerate(top10)
            ]
            if not lines:
                lines = ["  (距离窗内无点)"]
            n_show = len(top10)
            # 将「主峰 − 次峰」差额写进单次日志，便于对照 top10 列表人工核对主峰是否足够尖。
            if gap is not None:
                gap_line = (
                    f"\n  本帧：最高反射率={r_top}，次之={r_second}，"
                    f"差额（最高−次之）={gap}"
                )
            else:
                gap_line = f"\n  本帧：最高反射率={r_top}，窗内仅 1 点，无「次之」可比"
            self.detail_log.emit(
                f"探测第 {i + 1}/{self._repeat} 次 — 整圈 index 0…{full_hi}，距离窗内按反射率从高到低列出（最多 10 条，"
                f"实际 {n_show} 条；#1 反射率最高，往下应递减或持平，勿从下往上读成递增）：\n"
                + "\n".join(lines)
                + gap_line
                + f"\n  → 本轮采纳 index={ix}（窗内反射率全局最高；同率取较小 index）"
            )
            indices.append(ix)
            time.sleep(0.12)
        med = int(statistics.median(indices))
        # 统计「目标索引」在逐次主峰 index 列表中的命中次数（可大于 1，表示多帧主峰均落在目标上）。
        tgt = self._probe_target_index
        target_hit_count = sum(1 for x in indices if int(x) == tgt)
        self.finished_ok.emit(med, indices, per_shot_gaps, tgt, target_hit_count)


class R2ResolutionMainWindow(QMainWindow):
    """主窗：``radar_scan_log`` 供 ``R2SingleScanRadar`` 在工作线程里经 Signal 安全写界面日志。"""

    radar_scan_log = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("R2 连续点云 角分辨率测试（PySide6）")
        self.resize(1180, 760)
        self._worker: R2SequenceThread | None = None
        self._probe_thread: R2ProbeReflectThread | None = None
        self._session_rows: list[dict[str, Any]] = []

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        gb_radar = QGroupBox("雷达")
        g_r = QGridLayout(gb_radar)
        self.leRadarIp = QLineEdit("192.168.0.240")
        self.leRadarPort = QLineEdit("80")
        self.btnRadarConnect = QPushButton("连接")
        self.btnRadarDisconnect = QPushButton("断开")
        self.lblRadarState = QLabel("雷达：未连接")
        self.dsbAngularRes = QDoubleSpinBox()
        self.dsbAngularRes.setRange(0.001, 10.0)
        self.dsbAngularRes.setDecimals(3)
        self.dsbAngularRes.setSingleStep(0.01)
        self.dsbAngularRes.setValue(0.1)
        self.dsbStepAngle = QDoubleSpinBox()
        self.dsbStepAngle.setRange(0.001, 10.0)
        self.dsbStepAngle.setDecimals(3)
        self.dsbStepAngle.setSingleStep(0.01)
        self.dsbStepAngle.setValue(0.1)
        g_r.addWidget(QLabel("IP"), 0, 0)
        g_r.addWidget(self.leRadarIp, 0, 1)
        g_r.addWidget(QLabel("端口"), 0, 2)
        g_r.addWidget(self.leRadarPort, 0, 3)
        g_r.addWidget(self.btnRadarConnect, 0, 4)
        g_r.addWidget(self.btnRadarDisconnect, 0, 5)
        g_r.addWidget(self.lblRadarState, 1, 0, 1, 4)
        g_r.addWidget(QLabel("角分辨率(°/index)"), 1, 4)
        g_r.addWidget(self.dsbAngularRes, 1, 5)
        g_r.addWidget(QLabel("每步理论角(°)"), 2, 4)
        g_r.addWidget(self.dsbStepAngle, 2, 5)
        self.sbIdx0 = QSpinBox()
        self.sbIdx0.setRange(0, 100_000)
        self.sbIdx1 = QSpinBox()
        self.sbIdx1.setRange(0, 100_000)
        self.sbIdx1.setValue(3599)
        self.sbMinMm = QSpinBox()
        self.sbMinMm.setRange(0, 50000)
        self.sbMinMm.setValue(10)
        self.sbMaxMm = QSpinBox()
        self.sbMaxMm.setRange(20, 50000)
        self.sbMaxMm.setValue(12000)
        g_r.addWidget(QLabel("索引起"), 2, 0)
        g_r.addWidget(self.sbIdx0, 2, 1)
        g_r.addWidget(QLabel("索引止"), 2, 2)
        g_r.addWidget(self.sbIdx1, 2, 3)
        g_r.addWidget(QLabel("距离(mm)"), 3, 0)
        g_r.addWidget(self.sbMinMm, 3, 1)
        g_r.addWidget(QLabel("～"), 3, 2)
        g_r.addWidget(self.sbMaxMm, 3, 3)
        g_r.addWidget(QLabel("扫描起始角(°)"), 4, 0)
        self.dsbScanStart = QDoubleSpinBox()
        self.dsbScanStart.setRange(-180.0, 180.0)
        self.dsbScanStart.setDecimals(3)
        self.dsbScanStart.setValue(0.0)
        g_r.addWidget(self.dsbScanStart, 4, 1)
        hint_r2 = QLabel(
            "R2：HTTP 先 request_handle_tcp 取端口 → start_scanoutput 成功后再连 TCP；"
            "停用 HTTP stop_scanoutput。整圈 360°=3600 点（0.1°/index）"
        )
        hint_r2.setStyleSheet("color: gray;")
        g_r.addWidget(hint_r2, 5, 0, 1, 6)
        root.addWidget(gb_radar)

        gb_tt = QGroupBox("Y100SC 转台")
        g_t = QGridLayout(gb_tt)
        self.leCom = QLineEdit("COM6")
        self.sbBaud = QSpinBox()
        self.sbBaud.setRange(1200, 921600)
        self.sbBaud.setValue(9600)
        self.cmbAxis = QComboBox()
        for a in ("X", "Y", "Z", "r", "t", "T"):
            self.cmbAxis.addItem(a)
        self.cmbDir = QComboBox()
        self.cmbDir.addItems(["+", "-"])
        self.sbPulses = QSpinBox()
        self.sbPulses.setRange(1, 10000)
        self.sbPulses.setValue(40)
        self.sbSettleMs = QSpinBox()
        self.sbSettleMs.setRange(0, 60000)
        self.sbSettleMs.setValue(300)
        g_t.addWidget(QLabel("串口"), 0, 0)
        g_t.addWidget(self.leCom, 0, 1)
        g_t.addWidget(QLabel("波特率"), 0, 2)
        g_t.addWidget(self.sbBaud, 0, 3)
        g_t.addWidget(QLabel("轴"), 0, 4)
        g_t.addWidget(self.cmbAxis, 0, 5)
        g_t.addWidget(QLabel("方向"), 1, 0)
        g_t.addWidget(self.cmbDir, 1, 1)
        g_t.addWidget(QLabel("每步脉冲"), 1, 2)
        g_t.addWidget(self.sbPulses, 1, 3)
        g_t.addWidget(QLabel("到位等待(ms)"), 1, 4)
        g_t.addWidget(self.sbSettleMs, 1, 5)
        root.addWidget(gb_tt)

        gb_run = QGroupBox(
            "连续测试（起始索引=step0；转台每步后按 起始索引±步数 取点云；可先探测填索引）"
        )
        vb_run = QVBoxLayout(gb_run)
        row_idx = QHBoxLayout()
        self.sbStartIndex = QSpinBox()
        self.sbStartIndex.setRange(0, 100_000)
        self.sbStartIndex.setValue(1009)
        self.sbProbeRepeat = QSpinBox()
        self.sbProbeRepeat.setRange(1, 30)
        self.sbProbeRepeat.setValue(3)
        self.sbProbeRepeat.setToolTip(
            "「探测最高反射索引」：连续采集此次数，每帧取距离窗内反射率最高 index，多帧取中位数填入起始索引；"
            "每帧日志与探测结束汇总中会给出「最高反射率 − 次之」差额（差额越大主峰越突出）；"
            "结束时会统计点击探测前「起始索引」是否出现在各次主峰 index 序列中及出现次数。\n"
            "「连续测试」：每步转台到位后同样按此次数做反射探测；每帧「最高反射」默认在当步 targetIndex±"
            "「连续高反±index」与距离窗交集中选取；该半宽设为 0 时不在 index 轴收窄。表中写中位索引及是否与 "
            "targetIndex 一致；结束后统计一致占有率。"
        )
        self.btnProbeMaxReflect = QPushButton("探测最高反射索引")
        self.btnProbeMaxReflect.setEnabled(False)
        self.lblProbeResult = QLabel("探测：未执行")
        self.lblProbeResult.setMinimumWidth(200)
        row_idx.addWidget(QLabel("起始索引"))
        row_idx.addWidget(self.sbStartIndex)
        row_idx.addWidget(QLabel("探测次数"))
        row_idx.addWidget(self.sbProbeRepeat)
        self.sbContinuousReflectHalfSpan = QSpinBox()
        self.sbContinuousReflectHalfSpan.setRange(0, 10000)
        self.sbContinuousReflectHalfSpan.setValue(CONTINUOUS_MAX_REFLECT_INDEX_NEIGHBOR_HALF_SPAN)
        self.sbContinuousReflectHalfSpan.setToolTip(
            "仅「连续测试」每步多帧反射探测：每帧在距离窗内取最高反射 index 时，是否再限制在 "
            "当步 targetIndex±本值（index 闭区间）。0 表示不限制 index（全窗候选，与旧行为一致）；"
            "大于 0 时忽略 target 两侧更远 index 上的高反，避免误采。"
        )
        row_idx.addWidget(QLabel("连续高反±index"))
        row_idx.addWidget(self.sbContinuousReflectHalfSpan)
        row_idx.addWidget(self.btnProbeMaxReflect)
        row_idx.addWidget(self.lblProbeResult, stretch=1)
        vb_run.addLayout(row_idx)

        row_run = QHBoxLayout()
        self.sbSteps = QSpinBox()
        self.sbSteps.setRange(1, 2000)
        self.sbSteps.setValue(50)
        self.btnStart = QPushButton("开始连续测试")
        self.btnStop = QPushButton("停止")
        self.btnStop.setEnabled(False)
        self.btnExport = QPushButton("导出表格")
        self.btnExportAllIndices = QPushButton("导出索引窗全点云")
        self.btnExportAllIndices.setToolTip(
            "立即采一整圈 R2 点云并导出索引窗内 angle/距离/前沿(0)/后沿(0)/反射率"
        )
        self.btnExportAllIndices.setEnabled(False)
        row_run.addWidget(QLabel("步数 N"))
        row_run.addWidget(self.sbSteps)
        self.sbNeighborIdxDelta = QSpinBox()
        self.sbNeighborIdxDelta.setRange(0, 2000)
        self.sbNeighborIdxDelta.setValue(30)
        self.sbNeighborIdxDelta.setToolTip(
            "连续测试每一行：对每帧「距离窗内最高反射 index」为峰，在该帧点云上读 peak−Δ 与 peak+Δ 的 "
            "measured_distance（mm）；表内左/右邻距为各帧该距离的**均值**；GUI 分列显示左、右邻距"
            "标准差（mm）。Excel 导出含各帧邻距分号列表。index 列展示为中位峰±Δ。Δ=0 表示关闭。"
        )
        row_run.addWidget(QLabel("邻index±Δ"))
        row_run.addWidget(self.sbNeighborIdxDelta)
        row_run.addWidget(self.btnStart)
        row_run.addWidget(self.btnStop)
        row_run.addWidget(self.btnExport)
        row_run.addWidget(self.btnExportAllIndices)
        row_run.addStretch()
        vb_run.addLayout(row_run)

        self.lblStats = QLabel("—")
        self.lblStats.setTextInteractionFlags(
            self.lblStats.textInteractionFlags() | Qt.TextInteractionFlag.TextSelectableByMouse
        )
        vb_run.addWidget(self.lblStats)
        root.addWidget(gb_run)

        self.table = QTableWidget(0, len(TABLE_HEADERS))
        self.table.setHorizontalHeaderLabels(TABLE_HEADERS)
        # 使用 Interactive 列宽 + 横向滚动条，避免 Stretch 占满视口导致无法左右查看多列。
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        _th = self.table.horizontalHeader()
        _th.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        _th.setStretchLastSection(False)
        root.addWidget(self.table, stretch=1)

        self.teLog = QPlainTextEdit()
        self.teLog.setReadOnly(True)
        self.teLog.setMinimumHeight(160)
        root.addWidget(self.teLog)

        self.btnRadarConnect.clicked.connect(self._on_connect)
        self.btnRadarDisconnect.clicked.connect(self._on_disconnect)
        self.btnProbeMaxReflect.clicked.connect(self._on_probe_max_reflect)
        self.btnStart.clicked.connect(self._on_start)
        self.btnStop.clicked.connect(self._on_stop)
        self.btnExport.clicked.connect(self._on_export)
        self.btnExportAllIndices.clicked.connect(self._on_export_all_indices)
        self.sbIdx0.valueChanged.connect(self._sync_start_index_bounds)
        self.sbIdx1.valueChanged.connect(self._sync_start_index_bounds)
        self.radar_scan_log.connect(self._on_radar_scan_log)

        self._radar: R2SingleScanRadar | None = None
        self.statusBar().showMessage("就绪")
        self._sync_start_index_bounds()

    @Slot(str)
    def _on_radar_scan_log(self, msg: str) -> None:
        """组圈/本圈统计等由 ``r2_radar_client`` 在工作线程发出，经 Signal 在主线程写入日志。"""
        self._log(msg)

    def closeEvent(self, event: Any) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.stop()
            self._worker.wait(4000)
        if self._probe_thread and self._probe_thread.isRunning():
            self._probe_thread.wait(3000)
        if self._radar:
            self._radar.close()
            self._radar = None
        event.accept()

    def _log(self, s: str) -> None:
        self.teLog.appendPlainText(time.strftime("[%H:%M:%S] ") + s)
        cur = self.teLog.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        self.teLog.setTextCursor(cur)
        self.teLog.ensureCursorVisible()

    def _refresh_run_buttons(self) -> None:
        w_busy = self._worker is not None and self._worker.isRunning()
        p_busy = self._probe_thread is not None and self._probe_thread.isRunning()
        self.btnStart.setEnabled(not w_busy and not p_busy)
        self.btnProbeMaxReflect.setEnabled(self._radar is not None and not w_busy and not p_busy)
        self.btnExportAllIndices.setEnabled(self._radar is not None and not w_busy and not p_busy)
        self.sbProbeRepeat.setEnabled(not p_busy)
        self.sbStartIndex.setEnabled(not w_busy and not p_busy)
        self.sbContinuousReflectHalfSpan.setEnabled(not w_busy and not p_busy)

    def _set_busy(self, busy: bool) -> None:
        self.btnExport.setEnabled(not busy)
        self.btnStop.setEnabled(busy)
        self.btnRadarConnect.setEnabled(not busy)
        self._refresh_run_buttons()

    def _validate_range(self) -> bool:
        if self.sbIdx0.value() > self.sbIdx1.value():
            QMessageBox.warning(self, "参数", "索引起不能大于索引止")
            return False
        if self.sbMinMm.value() >= self.sbMaxMm.value():
            QMessageBox.warning(self, "参数", "最小距离须小于最大距离")
            return False
        return True

    def _sync_r2_radar_from_ui(self) -> bool:
        """将界面角分辨率、扫描起始角写入 R2 客户端（用于 index→angle_deg，与 H2 GUI 语义一致）。"""
        if not self._radar:
            return False
        self._radar.configure_scan_parameters(
            angular_resolution_deg=float(self.dsbAngularRes.value()),
            start_angle_deg=float(self.dsbScanStart.value()),
        )
        return True

    @Slot()
    def _on_connect(self) -> None:
        if self._radar:
            self._log("雷达已连接")
            return
        try:
            port = int(self.leRadarPort.text().strip())
        except ValueError:
            QMessageBox.warning(self, "参数", "端口须为整数")
            return
        self._radar = R2SingleScanRadar(host=self.leRadarIp.text().strip(), cmd_port=port)
        # 默认「最新一圈优先」（r2_radar_client：stop_tcp + accuracy_first + 多整圈丢弃）；若需提速可示例：
        # self._radar.stream_prep_mode = "drain"; self._radar.stream_accuracy_first = False; self._radar.stream_discard_circles_before_sample = 0
        if not self._sync_r2_radar_from_ui():
            self._radar = None
            return
        if not self._radar.connect_radar():
            err = self._radar.last_error or "未知错误"
            self._radar = None
            # ``connect_radar`` 失败时 ``r2_client`` 已对非 0 的 error_code 打印 JSON；此处写入 GUI 日志便于留存。
            self._log("雷达连接失败（仅 error_code==0 为成功；详情见控制台 stderr 或下文本）:\n" + err)
            QMessageBox.warning(self, "连接失败", err)
            self.lblRadarState.setText("雷达：未连接")
            return
        self.lblRadarState.setText(f"雷达：已连接 {self.leRadarIp.text()}:{port}（R2 HTTP+数据TCP）")
        # 将 ``r2_radar_client`` 里 ``[R2] 取一圈…`` / ``总点数…`` 等行投递到本窗日志（工作线程里 emit 亦安全）。
        self._radar.scan_log = self.radar_scan_log.emit
        self._log(
            "R2：已按顺序 request_handle_tcp → start_scanoutput → 数据 TCP；"
            "后台 feed_watchdog；断开时将 HTTP stop_scanoutput 后关 TCP。"
        )
        self._refresh_run_buttons()

    @Slot()
    def _on_disconnect(self) -> None:
        if self._radar:
            self._radar.close()
            self._radar = None
        self.lblRadarState.setText("雷达：未连接")
        self._log("雷达已断开。")
        if not (self._worker and self._worker.isRunning()):
            self._set_busy(False)
        self._refresh_run_buttons()

    def _sync_start_index_bounds(self) -> None:
        lo = int(self.sbIdx0.value())
        hi = int(self.sbIdx1.value())
        self.sbStartIndex.setMinimum(lo)
        self.sbStartIndex.setMaximum(hi)
        if self.sbStartIndex.value() < lo:
            self.sbStartIndex.setValue(lo)
        if self.sbStartIndex.value() > hi:
            self.sbStartIndex.setValue(hi)

    def _gather_config(self) -> R2GuiConfig | None:
        if not self._validate_range():
            return None
        try:
            rport = int(self.leRadarPort.text().strip())
        except ValueError:
            QMessageBox.warning(self, "参数", "端口须为整数")
            return None
        axis: Axis = self.cmbAxis.currentText()  # type: ignore[assignment]
        direction: Sign = self.cmbDir.currentText()  # type: ignore[assignment]
        self._sync_start_index_bounds()
        return R2GuiConfig(
            radar_ip=self.leRadarIp.text().strip(),
            radar_port=rport,
            index_start=int(self.sbIdx0.value()),
            index_end=int(self.sbIdx1.value()),
            min_distance_mm=int(self.sbMinMm.value()),
            max_distance_mm=int(self.sbMaxMm.value()),
            angular_res_deg=float(self.dsbAngularRes.value()),
            step_angle_deg=float(self.dsbStepAngle.value()),
            start_index=int(self.sbStartIndex.value()),
            com=self.leCom.text().strip(),
            baud=int(self.sbBaud.value()),
            axis=axis,
            direction=direction,
            pulses_per_step=int(self.sbPulses.value()),
            settle_ms=int(self.sbSettleMs.value()),
            steps=int(self.sbSteps.value()),
            scan_start_deg=float(self.dsbScanStart.value()),
            per_step_probe_repeat=max(1, int(self.sbProbeRepeat.value())),
            neighbor_index_delta=int(self.sbNeighborIdxDelta.value()),
            continuous_probe_index_half_span=int(self.sbContinuousReflectHalfSpan.value()),
        )

    def _radar_matches_ui(self, cfg: R2GuiConfig) -> bool:
        r = self._radar
        if r is None or r.socket is None:
            return False
        return r.host == cfg.radar_ip and int(r.port) == int(cfg.radar_port)

    @Slot()
    def _on_probe_max_reflect(self) -> None:
        if not self._radar:
            QMessageBox.warning(self, "探测", "请先连接雷达。")
            return
        if not self._validate_range():
            return
        self._sync_start_index_bounds()
        if self._probe_thread and self._probe_thread.isRunning():
            return
        if not self._sync_r2_radar_from_ui():
            return
        # 在覆盖起始索引之前记下当前「起始索引」作为目标，用于与各次探测得到的最高反射 index 序列比对。
        probe_target_index = int(self.sbStartIndex.value())
        self._probe_thread = R2ProbeReflectThread(
            self._radar,
            int(self.sbMaxMm.value()),
            int(self.sbMinMm.value()),
            int(self.sbProbeRepeat.value()),
            float(self.dsbAngularRes.value()),
            float(self.dsbScanStart.value()),
            probe_target_index,
            self,
        )
        self._probe_thread.detail_log.connect(self._log)
        self._probe_thread.finished_ok.connect(self._on_probe_finished_ok)
        self._probe_thread.failed.connect(self._on_probe_failed)
        self._probe_thread.finished.connect(self._on_probe_thread_finished)
        self._refresh_run_buttons()
        self._log(
            f"开始探测最高反射索引：次数={self.sbProbeRepeat.value()}，"
            f"整圈 index 0…{int(_r2_tcp_pkt.R2_FULL_CIRCLE_POINT_COUNT) - 1}（与索引起止/连续高反±index 无关）；"
            f"{_probe_prep_log_line(self._radar)}，"
            f"距离窗 ({self.sbMinMm.value()},{self.sbMaxMm.value()}) mm，"
            f"目标索引（当前起始索引）={probe_target_index}"
        )
        self._probe_thread.start()

    @Slot(int, object, object, int, int)
    def _on_probe_finished_ok(
        self,
        median_idx: int,
        indices: object,
        per_shot_gaps: object,
        probe_target_index: int,
        target_hit_count: int,
    ) -> None:
        assert isinstance(indices, list)
        assert isinstance(per_shot_gaps, list)
        self.sbStartIndex.setValue(median_idx)
        # 将各次「最高−次之」差额格式化，None 表示该次窗内唯一点。
        gap_parts: list[str] = []
        numeric_gaps: list[int] = []
        for k, g in enumerate(per_shot_gaps):
            if isinstance(g, int):
                gap_parts.append(str(g))
                numeric_gaps.append(g)
            else:
                gap_parts.append("—")
        gaps_joined = "[" + ", ".join(gap_parts) + "]"
        contains_tgt = "是" if target_hit_count > 0 else "否"
        tgt_line = (
            f"目标索引={probe_target_index}，序列是否包含：{contains_tgt}，"
            f"包含次数={target_hit_count}/{len(indices)}"
        )
        self.lblProbeResult.setText(
            f"中位数索引={median_idx}  各次={indices}  各次差额={gaps_joined}  {tgt_line}"
        )
        self._log(f"探测完成：中位数索引={median_idx}，各次索引={indices}")
        self._log(f"探测完成：{tgt_line}")
        self._log(
            f"探测完成：各次「最高反射率 − 次之反射率」差额（同单位）={gaps_joined}"
        )
        if numeric_gaps:
            self._log(
                "探测完成：有效差额统计（含次高点的次数）— "
                f"最小={min(numeric_gaps)}，中位数={int(statistics.median(numeric_gaps))}，"
                f"最大={max(numeric_gaps)}（差额越大通常主峰越突出，连续测试越稳）"
            )
        elif per_shot_gaps:
            self._log(
                "探测完成：各次窗内均只有 1 个有效点，无法计算「最高−次之」差额；请检查距离窗或点云。"
            )

    @Slot(str)
    def _on_probe_failed(self, msg: str) -> None:
        QMessageBox.warning(self, "探测最高反射", msg)
        self._log(f"探测失败：{msg}")

    @Slot()
    def _on_probe_thread_finished(self) -> None:
        self._probe_thread = None
        self._refresh_run_buttons()

    @Slot()
    def _on_start(self) -> None:
        cfg = self._gather_config()
        if not cfg:
            return
        external: R2SingleScanRadar | None = None
        if self._radar_matches_ui(cfg):
            if not self._sync_r2_radar_from_ui():
                return
            external = self._radar
        elif self._radar is not None:
            # 已连接但与当前 IP/端口不一致：先断开，由线程内新建连接
            self._radar.close()
            self._radar = None
            self.lblRadarState.setText("雷达：未连接")
            self._log("已断开旧连接（与当前 IP/端口不一致），测试中将新建连接。")

        self.table.setRowCount(0)
        self._session_rows.clear()
        self._set_busy(True)
        self._worker = R2SequenceThread(cfg, external, self)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished_ok.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._on_worker_thread_finished)
        self._worker.start()
        self._log(
            f"开始连续测试：起始索引={cfg.start_index}，步数 N={cfg.steps}，方向={cfg.direction}，"
            f"每步理论角={cfg.step_angle_deg}°，每步反射探测次数={cfg.per_step_probe_repeat}，"
            f"连续高反±index={cfg.continuous_probe_index_half_span}"
            f"（≤0 为全 index 候选），目标邻index±Δ={cfg.neighbor_index_delta}"
        )

    def _append_row(self, row: dict[str, Any]) -> None:
        i = self.table.rowCount()
        self.table.insertRow(i)
        self.table.setItem(i, 0, QTableWidgetItem(str(row["step"])))
        self.table.setItem(i, 1, QTableWidgetItem(str(row["rotate_label"])))
        self.table.setItem(i, 2, QTableWidgetItem(str(row["target_index"])))
        self.table.setItem(i, 3, QTableWidgetItem(str(row.get("probe_median_index", ""))))
        self.table.setItem(i, 4, QTableWidgetItem(str(row.get("probe_match_target", ""))))
        self.table.setItem(i, 5, QTableWidgetItem(str(row.get("probe_indices_sequence", ""))))
        self.table.setItem(
            i, 6, QTableWidgetItem(str(row.get("probe_sequence_contains_target", "")))
        )
        self.table.setItem(
            i, 7, QTableWidgetItem(str(row.get("probe_sequence_target_hit_count", "")))
        )
        self.table.setItem(i, 8, QTableWidgetItem(str(row.get("probe_max_hit_count", ""))))
        self.table.setItem(i, 9, QTableWidgetItem(str(row.get("target_minus_probe_median", ""))))
        self.table.setItem(i, 10, QTableWidgetItem(str(row["delta_index"])))
        self.table.setItem(i, 11, QTableWidgetItem(f"{row['measured_angle_deg']:.4f}"))
        self.table.setItem(i, 12, QTableWidgetItem(f"{row['theory_angle_deg']:+.4f}"))
        self.table.setItem(i, 13, QTableWidgetItem(f"{row['error_deg']:+.4f}"))
        self.table.setItem(i, 14, QTableWidgetItem(f"{row['distance_m']:.3f}"))
        self.table.setItem(i, 15, QTableWidgetItem(str(row["intensity"])))
        self.table.setItem(i, 16, QTableWidgetItem(str(row.get("neighbor_index_offset", ""))))
        self.table.setItem(i, 17, QTableWidgetItem(str(row.get("neighbor_left_index", ""))))
        self.table.setItem(i, 18, QTableWidgetItem(str(row.get("neighbor_left_distance_mm", ""))))
        self.table.setItem(i, 19, QTableWidgetItem(str(row.get("neighbor_left_std_mm", ""))))
        self.table.setItem(i, 20, QTableWidgetItem(str(row.get("neighbor_right_index", ""))))
        self.table.setItem(i, 21, QTableWidgetItem(str(row.get("neighbor_right_distance_mm", ""))))
        self.table.setItem(i, 22, QTableWidgetItem(str(row.get("neighbor_right_std_mm", ""))))
        self.table.setItem(i, 23, QTableWidgetItem(str(row.get("neighbor_lr_distance_diff_mm", ""))))
        self.table.setItem(i, 24, QTableWidgetItem(row.get("anomalies") or ""))
        last_item = self.table.item(i, 0)
        if last_item is not None:
            self.table.scrollToItem(last_item, QAbstractItemView.ScrollHint.PositionAtBottom)

    @Slot(int, int, object)
    def _on_progress(self, cur: int, tot: int, row: object) -> None:
        assert isinstance(row, dict)
        self._session_rows.append(row)
        self._append_row(row)
        self._log(
            f"step {row['step']}/{tot}: idx={row['target_index']} Δ={row['delta_index']} "
            f"err={row['error_deg']:+.4f}° | 反射中位={row.get('probe_median_index', '')} "
            f"一致={row.get('probe_match_target', '')} "
            f"目标−中位Δ={row.get('target_minus_probe_median', '')} "
            f"序列含目标={row.get('probe_sequence_contains_target', '')} "
            f"目标在序列中次数={row.get('probe_sequence_target_hit_count', '')} "
            f"最多命中={row.get('probe_max_hit_count', '')} "
            f"左邻mm={row.get('neighbor_left_distance_mm', '')} "
            f"右邻mm={row.get('neighbor_right_distance_mm', '')}"
        )

    @Slot(object)
    def _on_finished(self, rows: object) -> None:
        self._set_busy(False)
        assert isinstance(rows, list)
        self._session_rows = list(rows)
        conclusion = compose_h2_final_conclusion(self._session_rows)
        self.lblStats.setText(conclusion)
        self._log(conclusion)
        self._log("测试结束。")
        self.statusBar().showMessage("完成")

    @Slot(str)
    def _on_failed(self, msg: str) -> None:
        self._set_busy(False)
        QMessageBox.warning(self, "连续测试", msg)
        self._log("失败：" + msg)
        if self._session_rows:
            conclusion = compose_h2_final_conclusion(self._session_rows)
            self.lblStats.setText(conclusion)

    @Slot()
    def _on_worker_thread_finished(self) -> None:
        """线程结束时清空引用；若未收到 finished_ok/failed（异常路径），此处恢复按钮。"""
        self._worker = None
        if not self.btnStart.isEnabled():
            self._set_busy(False)
        self._refresh_run_buttons()

    @Slot()
    def _on_stop(self) -> None:
        if self._worker:
            self._worker.stop()
            self._log("已请求停止…")

    @Slot()
    def _on_export_all_indices(self) -> None:
        if not self._radar:
            QMessageBox.warning(self, "导出", "请先连接雷达。")
            return
        if not self._validate_range():
            return
        path, _filt = QFileDialog.getSaveFileName(
            self,
            "导出索引窗内全部点",
            _r2_default_export_path("索引窗点云"),
            "Excel (*.xlsx);;CSV (*.csv)",
        )
        if not path:
            return
        if not self._sync_r2_radar_from_ui():
            return
        m = self._radar.optimized_single_measurement(
            int(self.sbIdx0.value()),
            int(self.sbIdx1.value()),
            int(self.sbMaxMm.value()),
        )
        if not m:
            QMessageBox.warning(self, "导出", self._radar.last_error or "采集失败")
            return
        all_rows = m.get("all_results") or []
        if not all_rows:
            QMessageBox.information(self, "导出", "无点云数据")
            return
        try:
            export_all_index_points(all_rows, Path(path))
        except SystemExit as e:
            QMessageBox.warning(self, "导出", str(e))
            return
        except OSError as e:
            QMessageBox.warning(self, "导出", str(e))
            return
        self._log(
            f"已导出索引窗全点云：索引起 {self.sbIdx0.value()}～索引止 {self.sbIdx1.value()}，"
            f"共 {len(all_rows)} 点 → {path}"
        )

    @Slot()
    def _on_export(self) -> None:
        if not self._session_rows:
            QMessageBox.information(self, "导出", "暂无数据")
            return
        path, filt = QFileDialog.getSaveFileName(
            self,
            "导出",
            _r2_default_export_path("r2_resolution_gui"),
            "Excel (*.xlsx);;CSV (*.csv)",
        )
        if not path:
            return
        conclusion = compose_h2_final_conclusion(self._session_rows)
        try:
            export_rows(self._session_rows, Path(path), "", conclusion)
        except SystemExit as e:
            QMessageBox.warning(self, "导出", str(e))
            return
        except OSError as e:
            QMessageBox.warning(self, "导出", str(e))
            return
        self._log(f"已导出：{path}")


def main() -> int:
    app = QApplication(sys.argv)
    w = R2ResolutionMainWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
