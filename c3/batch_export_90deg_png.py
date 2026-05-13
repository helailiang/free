"""
批量生成与 long_text_H2_pointcloud_gui.py「90°距离走势图」一致的 PNG 文件（matplotlib Agg，等价于导出图表截图）。

用法:
  python batch_export_90deg_png.py "E:\\mywork\\产品学习\\c3\\WL2026431738"
  python batch_export_90deg_png.py "上述路径" --out-dir "E:\\...\\WL2026431738_plots"
  python batch_export_90deg_png.py "路径" --reference-mm 200 --tolerance 0.2   # 全体固定参考距离，覆盖自动解析
  .\.venv\Scripts\python.exe c3\batch_export_90deg_png.py "E:\mywork\产品学习\c3\WL2026431750"
  .\.venv\Scripts\python.exe c3\batch_export_90deg_png.py "E:\mywork\产品学习\c3\WL2026431750" --out-dir "E:\mywork\产品学习\c3\WL2026431750_plots"
参考距离默认从文件名自动解析：匹配「数字+m」表示米（如 25m.txt → 25000mm），取文件名中最后一处匹配。
排除 mm（毫米写法）：例如 300mm 不会产生误匹配。可加 --no-auto-reference 关闭自动解析。

说明: 不 import Tk GUI 模块，统计逻辑与 long_text_H2_pointcloud_gui.py 中常量、stats_by_scan_cnt 保持一致。
"""

from __future__ import annotations

import argparse
import math
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import matplotlib

matplotlib.use("Agg")

from matplotlib.figure import Figure

from libs.protocols.c3_common import circular_angle_diff_deg
from libs.protocols.h2_txt_parse import flatten_points, parse_h2_txt_frames

# 与 long_text_H2_pointcloud_gui.py 保持一致
H2_SCAN_START_DEG = -45.0
SCAN_STATS_MAX_SCANS = 1000

# 文件名中的距离：`25m` / `_39m` 表示米，换算为 mm；`(?!m)` 避免把 `300mm` 当成 `300m`
_REF_METERS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*m(?!m)", re.IGNORECASE)


def reference_mm_from_filename(path: Path) -> Optional[float]:
    """
    从文件名（不含扩展名）解析「数字+m」米制距离，转为毫米。
    例：25m.txt -> 25000；50%_39m.txt -> 39000（取最后一处匹配）。
    """
    stem = path.stem
    matches = list(_REF_METERS_RE.finditer(stem))
    if not matches:
        return None
    meters = float(matches[-1].group(1))
    return meters * 1000.0


def parse_txt(file_path: str, **kwargs):
    return parse_h2_txt_frames(file_path, scan_start_deg=H2_SCAN_START_DEG, **kwargs)


def calc_stats(values: List[float]) -> Dict:
    if not values:
        return {
            "count": 0,
            "mean": None,
            "variance": None,
            "std_dev": None,
            "min": None,
            "max": None,
        }

    n = len(values)
    mean = sum(values) / n
    variance = sum((x - mean) ** 2 for x in values) / n
    std_dev = math.sqrt(variance)

    return {
        "count": n,
        "mean": mean,
        "variance": variance,
        "std_dev": std_dev,
        "min": min(values),
        "max": max(values),
    }


def stats_by_scan_cnt(points: List[Dict], target_angle_deg: float, tolerance_deg: float) -> List[Dict]:
    groups = {}

    for p in points:
        if circular_angle_diff_deg(p["angle_deg"], target_angle_deg) <= tolerance_deg:
            key = p["scan_cnt"]
            groups.setdefault(key, []).append(p["r_mm"])

    sorted_keys = sorted(groups.keys())
    if len(sorted_keys) > SCAN_STATS_MAX_SCANS:
        sorted_keys = sorted_keys[:SCAN_STATS_MAX_SCANS]

    results = []
    for scan_cnt in sorted_keys:
        values = groups[scan_cnt]
        s = calc_stats(values)
        results.append({
            "scan_cnt": scan_cnt,
            "count": s["count"],
            "mean_r_mm": s["mean"],
            "variance_r": s["variance"],
            "std_r": s["std_dev"],
            "min_r_mm": s["min"],
            "max_r_mm": s["max"],
        })

    return results


def build_figure(
    file_path: str,
    *,
    tolerance: float,
    reference_mm: Optional[float],
    target_deg: float = 90.0,
) -> Tuple[Optional[Figure], Optional[str]]:
    """
    与 GUI show_90deg_distance_plot 中绘图逻辑对齐。
    返回 (Figure, None) 成功；(None, error_reason) 失败。
    """
    parsed_frames = parse_txt(file_path)
    all_points = flatten_points(parsed_frames)
    per_scan = stats_by_scan_cnt(all_points, target_deg, tolerance)
    if not per_scan:
        return None, "no_points_at_angle"

    scan_cnts = [row["scan_cnt"] for row in per_scan]
    means = [row["mean_r_mm"] for row in per_scan]

    overall_mean = sum(means) / len(means)
    ms = calc_stats(means)
    std_across_scans = ms["std_dev"] if ms["std_dev"] is not None else 0.0

    meas_mm = reference_mm
    if meas_mm is not None:
        system_err = abs(overall_mean - meas_mm)
        actual_err = system_err + std_across_scans
    else:
        system_err = None
        actual_err = None

    matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans", "sans-serif"]
    matplotlib.rcParams["axes.unicode_minus"] = False

    fig = Figure(figsize=(9.2, 6.0), dpi=100)
    ax = fig.add_subplot(111)
    ax.plot(scan_cnts, means, color="#2563eb", linewidth=1.2, marker=".", markersize=4)
    ax.set_xlabel("扫描计数 scan_cnt")
    ax.set_ylabel("平均距离 (mm)")
    ax.set_title(f"{target_deg:g}° ±{tolerance}° 平均距离随扫描计数变化")
    ax.grid(True, linestyle="--", alpha=0.35)

    if system_err is not None:
        line1 = f"1. 系统误差（平均距离−测量距离）: {system_err:.3f} mm"
        line3 = f"3. 实际误差（系统误差+标准差）: {actual_err:.3f} mm"
    else:
        line1 = "1. 系统误差（平均距离−测量距离）: —（无参考距离：文件名未解析到 Nm 或未指定 --reference-mm）"
        line3 = "3. 实际误差（系统误差+标准差）: —"
    line2 = f"2. 标准差距离: {std_across_scans:.3f} mm"
    anno = "\n".join([line1, line2, line3])

    fig.tight_layout(rect=[0, 0, 1, 0.88])
    fig.text(
        0.5,
        0.91,
        anno,
        ha="center",
        va="center",
        fontsize=8.5,
        transform=fig.transFigure,
        bbox=dict(boxstyle="round,pad=0.45", facecolor="wheat", edgecolor="gray", alpha=0.92),
    )

    base = os.path.basename(file_path)
    fig.text(0.5, 0.03, f"数据文件: {base}", ha="center", va="bottom", fontsize=8.5, transform=fig.transFigure)

    return fig, None


def collect_txt_files(root: Path, recurse: bool) -> List[Path]:
    if recurse:
        return sorted({p for p in root.rglob("*.txt")})
    return sorted(root.glob("*.txt"))


def main() -> int:
    ap = argparse.ArgumentParser(description="批量导出 90° 距离走势图 PNG")
    ap.add_argument("input_dir", type=Path, help="包含 txt 点云文件的目录")
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="PNG 输出目录（默认：输入目录下的子目录 90deg_trend_png）",
    )
    ap.add_argument("--tolerance", type=float, default=0.2, help="角度容差（度），与 GUI 默认一致")
    ap.add_argument(
        "--reference-mm",
        type=float,
        default=None,
        help="全体文件共用参考距离 (mm)，指定则覆盖按文件名自动解析",
    )
    ap.add_argument(
        "--no-auto-reference",
        action="store_true",
        help="禁用从文件名解析 Nm→mm，且不填参考距离（除非仍指定了 --reference-mm）",
    )
    ap.add_argument("--no-recurse", action="store_true", help="仅扫描输入目录一层，不递归子文件夹")
    args = ap.parse_args()

    root = args.input_dir.resolve()
    if not root.is_dir():
        print(f"目录不存在或不是文件夹: {root}", file=sys.stderr)
        return 2

    out_dir = (args.out_dir or (root / "90deg_trend_png")).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    txts = collect_txt_files(root, recurse=not args.no_recurse)
    if not txts:
        print(f"未找到 .txt 文件: {root}")
        return 1

    ok, fail = 0, 0
    for i, fp in enumerate(txts, 1):
        try:
            rel = fp.relative_to(root)
        except ValueError:
            rel = Path(fp.name)
        safe_stem = str(rel).replace(os.sep, "_")
        if safe_stem.lower().endswith(".txt"):
            safe_stem = safe_stem[:-4]
        png_path = out_dir / f"{safe_stem}_90deg.png"
        try:
            ref_mm: Optional[float] = args.reference_mm
            ref_note = ""
            if ref_mm is None and not args.no_auto_reference:
                ref_mm = reference_mm_from_filename(fp)
                if ref_mm is not None:
                    ref_note = f" ref={ref_mm:g}mm(auto)"
            elif ref_mm is not None:
                ref_note = f" ref={ref_mm:g}mm(fixed)"
            fig, err = build_figure(str(fp), tolerance=args.tolerance, reference_mm=ref_mm)
            if fig is None:
                print(f"[{i}/{len(txts)}] SKIP {fp} ({err})")
                fail += 1
                continue
            fig.savefig(png_path, dpi=120, bbox_inches="tight")
            print(f"[{i}/{len(txts)}] OK{ref_note} -> {png_path}")
            ok += 1
        except Exception as e:
            print(f"[{i}/{len(txts)}] FAIL {fp}: {e}", file=sys.stderr)
            fail += 1

    print(f"完成: 成功 {ok}, 跳过/失败 {fail}, 输出目录 {out_dir}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
