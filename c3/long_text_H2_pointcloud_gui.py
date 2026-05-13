import re
import struct
import math
import csv
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from typing import List, Dict, Optional, Tuple
import threading
import os
from datetime import datetime

from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from libs.protocols.c3_common import circular_angle_diff_deg
from libs.protocols.h2_txt_parse import (
    filter_points_by_angle,
    flatten_points,
    parse_h2_txt_frames,
)

# H2：若设备经 4.2.16 修改扫描范围，与说明书默认 -45°~225° 不一致时请修改此处
H2_SCAN_START_DEG = -45.0
# 按扫描计数分组统计时，最多纳入的不同 scan_cnt 个数（超过则仅取其中前 1000 次）
SCAN_STATS_MAX_SCANS = 1000


def parse_txt(file_path: str, **kwargs):
    return parse_h2_txt_frames(
        file_path,
        scan_start_deg=H2_SCAN_START_DEG,
        **kwargs,
    )

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


def save_csv(rows: List[Dict], csv_path: str):
    if not rows:
        print(f"{csv_path}: 无数据")
        return

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def stats_by_scan_cnt(points: List[Dict], target_angle_deg: float, tolerance_deg: float) -> List[Dict]:
    """
    按 scan_cnt 分组统计指定角度附近的 r。
    若不同 scan_cnt 超过 SCAN_STATS_MAX_SCANS，则按 scan_cnt 升序仅取前 SCAN_STATS_MAX_SCANS 个扫描计数参与统计。
    """
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


def analyze_single_file(file_path: str, angles: List[float], tolerance: float,
                        reference_distances: List[float] = None) -> Dict:
    """
    分析单个文件，返回各角度的统计结果
    reference_distances: 参考距离列表，用于计算精度误差
    """
    try:
        parsed_frames = parse_txt(file_path)
        all_points = flatten_points(parsed_frames)

        results = {
            "file_name": os.path.basename(file_path),
            "file_path": file_path,
            "total_frames": len(parsed_frames),
            "total_points": len(all_points),
            "angle_results": []
        }

        for i, angle in enumerate(angles):
            selected = filter_points_by_angle(all_points, angle, tolerance)
            r_values = [p["r_mm"] for p in selected]
            stats = calc_stats(r_values)

            # 计算精度误差（如果有参考距离）
            accuracy_error = None
            if reference_distances and i < len(reference_distances) and reference_distances[i] is not None and stats[
                'mean'] is not None:
                accuracy_error = stats['mean'] - reference_distances[i]

            results["angle_results"].append({
                "angle": angle,
                "count": stats["count"],
                "mean": stats["mean"],
                "std_dev": stats["std_dev"],
                "min": stats["min"],
                "max": stats["max"],
                "accuracy_error": accuracy_error
            })

        return results
    except Exception as e:
        return {
            "file_name": os.path.basename(file_path),
            "file_path": file_path,
            "error": str(e),
            "angle_results": []
        }


def get_folder_info(file_path: str, root_folder: str = ""):
    """
    获取文件夹信息
    如果没有根目录，子目录就为根目录，少打印一个文件夹名
    """
    full_path = os.path.dirname(file_path)
    path_parts = full_path.split(os.sep)

    root_name = ""
    sub_name = ""

    if root_folder and os.path.exists(root_folder):
        # 有根目录的情况
        try:
            rel_path = os.path.relpath(full_path, root_folder)
            if rel_path == '.':
                # 文件就在根目录下
                root_name = os.path.basename(root_folder)
                sub_name = ""  # 子文件夹为空
            else:
                path_components = rel_path.split(os.sep)
                root_name = os.path.basename(root_folder)
                sub_name = path_components[0] if path_components else ""
        except:
            # 相对路径计算失败，使用绝对路径
            if len(path_parts) >= 2:
                root_name = path_parts[-2]
                sub_name = path_parts[-1] if len(path_parts) > 1 else ""
    else:
        # 没有根目录，子目录就为根目录
        if len(path_parts) >= 1:
            root_name = path_parts[-1]  # 最后一级文件夹作为根目录
            sub_name = ""  # 子文件夹为空

    return root_name, sub_name


def save_summary_report(results_list: List[Dict], output_path: str, angles: List[float],
                        root_folder: str = "", reference_distances: List[float] = None):
    """
    保存汇总报告到txt文件
    格式: 根目录文件夹名 + 子文件夹名 + 文件名 + 角度 + 精度误差 + 平均距离 + 标准差
    """
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("激光雷达数据分析汇总报告\n")
        f.write("=" * 150 + "\n")
        f.write(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        if root_folder:
            f.write(f"根目录: {root_folder}\n")
        f.write(f"分析角度: {', '.join([f'{a}°' for a in angles])}\n")
        if reference_distances:
            f.write(f"参考距离: {', '.join([f'{d}mm' if d else 'N/A' for d in reference_distances])}\n")
        f.write(f"容差: {tolerance_value}°\n" if 'tolerance_value' in globals() else "容差: 0.2°\n")
        f.write("=" * 150 + "\n\n")

        if len(angles) == 1:
            # 单角度格式
            header = "{:<30} {:<30} {:<30} {:<10} {:<15} {:<15} {:<15}".format(
                "文件夹", "子文件夹", "文件名", "角度(°)", "精度误差(mm)", "平均距离(mm)", "标准差"
            )
            f.write(header + "\n")
            f.write("-" * 150 + "\n")

            for result in results_list:
                root_name, sub_name = get_folder_info(result["file_path"], root_folder)
                file_name = result["file_name"]

                if "error" in result:
                    line = "{:<30} {:<30} {:<30} 解析错误: {}".format(
                        root_name[:28] + "..." if len(root_name) > 28 else root_name,
                        sub_name[:28] + "..." if len(sub_name) > 28 else sub_name,
                        file_name[:28] + "..." if len(file_name) > 28 else file_name,
                        result['error']
                    )
                    f.write(line + "\n\n")
                else:
                    angle_result = result["angle_results"][0]
                    mean_val = f"{angle_result['mean']:.2f}" if angle_result['mean'] is not None else "N/A"
                    std_val = f"{angle_result['std_dev']:.2f}" if angle_result['std_dev'] is not None else "N/A"
                    error_val = f"{angle_result['accuracy_error']:.2f}" if angle_result.get(
                        'accuracy_error') is not None else "N/A"

                    line = "{:<30} {:<30} {:<30} {:<10} {:<15} {:<15} {:<15}".format(
                        root_name[:28] + "..." if len(root_name) > 28 else root_name,
                        sub_name[:28] + "..." if len(sub_name) > 28 else sub_name,
                        file_name[:28] + "..." if len(file_name) > 28 else file_name,
                        f"{angle_result['angle']:.1f}",
                        error_val,
                        mean_val,
                        std_val
                    )
                    f.write(line + "\n\n")
        else:
            # 三角度格式 - 保持原有的分列显示，增加精度误差列
            header = "{:<30} {:<30} {:<30} {:<15} {:<15} {:<15} {:<15} {:<15} {:<15} {:<15} {:<15}".format(
                "文件夹", "子文件夹", "文件名",
                "角度1均值", "角度1误差", "角度1标准差",
                "角度2均值", "角度2误差", "角度2标准差",
                "角度3均值", "角度3误差", "角度3标准差"
            )
            f.write(header + "\n")
            f.write("-" * 180 + "\n")

            for result in results_list:
                root_name, sub_name = get_folder_info(result["file_path"], root_folder)
                file_name = result["file_name"]

                if "error" in result:
                    line = "{:<30} {:<30} {:<30} 解析错误: {}".format(
                        root_name[:28] + "..." if len(root_name) > 28 else root_name,
                        sub_name[:28] + "..." if len(sub_name) > 28 else sub_name,
                        file_name[:28] + "..." if len(file_name) > 28 else file_name,
                        result['error']
                    )
                    f.write(line + "\n\n")
                else:
                    values = [root_name, sub_name, file_name]

                    for angle_result in result["angle_results"]:
                        mean_val = f"{angle_result['mean']:.2f}" if angle_result['mean'] is not None else "N/A"
                        error_val = f"{angle_result['accuracy_error']:.2f}" if angle_result.get(
                            'accuracy_error') is not None else "N/A"
                        std_val = f"{angle_result['std_dev']:.2f}" if angle_result['std_dev'] is not None else "N/A"
                        values.extend([mean_val, error_val, std_val])

                    # 补齐到12列
                    while len(values) < 12:
                        values.append("")

                    line = "{:<30} {:<30} {:<30} {:<15} {:<15} {:<15} {:<15} {:<15} {:<15} {:<15} {:<15}".format(
                        values[0][:28] + "..." if len(values[0]) > 28 else values[0],
                        values[1][:28] + "..." if len(values[1]) > 28 else values[1],
                        values[2][:28] + "..." if len(values[2]) > 28 else values[2],
                        values[3], values[4], values[5],
                        values[6], values[7], values[8],
                        values[9], values[10]
                    )
                    f.write(line + "\n\n")

        # 添加统计摘要
        f.write("\n" + "=" * 150 + "\n")
        f.write("统计摘要\n")
        f.write("=" * 150 + "\n")

        for i, angle in enumerate(angles):
            valid_results = [r for r in results_list if "error" not in r and r["angle_results"][i]["mean"] is not None]
            if valid_results:
                means = [r["angle_results"][i]["mean"] for r in valid_results]
                avg_mean = sum(means) / len(means)

                # 计算平均精度误差
                errors = [r["angle_results"][i].get("accuracy_error") for r in valid_results
                          if r["angle_results"][i].get("accuracy_error") is not None]
                avg_error = sum(errors) / len(errors) if errors else None

                f.write(f"\n角度 {angle}°:\n")
                f.write(f"  有效文件数: {len(valid_results)}\n")
                f.write(f"  平均距离平均值: {avg_mean:.2f} mm\n")
                if avg_error is not None:
                    f.write(f"  平均精度误差: {avg_error:.2f} mm\n")


class LidarAnalyzerGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("激光雷达点云数据分析工具 (C2/H1 文本协议) - 批量处理")
        self.root.geometry("1200x850")

        # 设置样式
        style = ttk.Style()
        style.theme_use('clam')

        # 变量
        self.folder_path = tk.StringVar()
        self.tolerance = tk.DoubleVar(value=0.2)
        self.angle_mode = tk.StringVar(value="3")  # "1" 或 "3"

        # 三个角度变量
        self.angle_vars = [
            tk.DoubleVar(value=0.0),
            tk.DoubleVar(value=90.0),
            tk.DoubleVar(value=180.0)
        ]

        # 参考距离变量（用于计算精度误差）
        self.reference_vars = [
            tk.DoubleVar(value=200.0),  # 默认参考距离200mm
            tk.DoubleVar(value=200.0),
            tk.DoubleVar(value=200.0)
        ]
        self.use_reference = tk.BooleanVar(value=False)  # 是否使用参考距离

        self.batch_results = []  # 存储批量处理结果
        self.current_file_results = []  # 存储当前文件的分析结果（用于单文件模式）
        self.all_points: List[Dict] = []  # 单文件最近一次解析的扁平点（用于导出 / 图表）
        self.parsed_frames: List = []

        # 存储界面元素的字典（使用索引作为键）
        self.stats_frames = {}  # 使用索引作为键
        self.stats_trees = {}  # 使用索引作为键
        self.scan_trees = {}  # 使用索引作为键

        self.create_widgets()

    def create_widgets(self):
        # 主框架
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 文件/文件夹选择区域
        path_frame = ttk.LabelFrame(main_frame, text="选择处理目标", padding="10")
        path_frame.pack(fill=tk.X, pady=5)

        # 单文件模式
        file_frame = ttk.Frame(path_frame)
        file_frame.pack(fill=tk.X, pady=2)
        ttk.Label(file_frame, text="单文件:").pack(side=tk.LEFT)
        self.file_path = tk.StringVar()
        ttk.Entry(file_frame, textvariable=self.file_path, width=60).pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)
        ttk.Button(file_frame, text="浏览文件...", command=self.select_file).pack(side=tk.LEFT, padx=2)

        # 文件夹模式
        folder_frame = ttk.Frame(path_frame)
        folder_frame.pack(fill=tk.X, pady=2)
        ttk.Label(folder_frame, text="批量处理:").pack(side=tk.LEFT)
        ttk.Entry(folder_frame, textvariable=self.folder_path, width=60).pack(side=tk.LEFT, padx=5, fill=tk.X,
                                                                              expand=True)
        ttk.Button(folder_frame, text="浏览文件夹...", command=self.select_folder).pack(side=tk.LEFT, padx=2)

        # 参数设置区域
        param_frame = ttk.LabelFrame(main_frame, text="分析参数设置", padding="10")
        param_frame.pack(fill=tk.X, pady=5)

        # 角度模式选择
        mode_frame = ttk.Frame(param_frame)
        mode_frame.grid(row=0, column=0, columnspan=8, sticky=tk.W, pady=5)

        ttk.Label(mode_frame, text="角度模式:").pack(side=tk.LEFT, padx=5)
        ttk.Radiobutton(mode_frame, text="单角度", variable=self.angle_mode, value="1",
                        command=self.toggle_angle_mode).pack(side=tk.LEFT, padx=10)
        ttk.Radiobutton(mode_frame, text="三角度", variable=self.angle_mode, value="3",
                        command=self.toggle_angle_mode).pack(side=tk.LEFT, padx=10)

        # 参考距离选项
        ttk.Checkbutton(mode_frame, text="使用参考距离计算精度误差",
                        variable=self.use_reference, command=self.toggle_reference).pack(side=tk.LEFT, padx=20)

        # 角度和参考距离输入区域
        self.input_frame = ttk.Frame(param_frame)
        self.input_frame.grid(row=1, column=0, columnspan=8, sticky=tk.W, pady=5)

        # 创建角度和参考距离输入框
        self.create_input_fields()

        # 容差
        ttk.Label(param_frame, text="容差(度):").grid(row=2, column=0, sticky=tk.W, padx=5, pady=5)
        ttk.Entry(param_frame, textvariable=self.tolerance, width=10).grid(row=2, column=1, padx=5, pady=5, sticky=tk.W)
        ttk.Label(param_frame, text="(所有角度使用相同容差)", foreground="gray").grid(row=2, column=2, sticky=tk.W,
                                                                                      padx=5)

        # 按钮区域
        button_frame = ttk.Frame(main_frame)
        button_frame.pack(fill=tk.X, pady=10)

        ttk.Button(button_frame, text="分析单文件", command=self.start_single_analysis, width=12).pack(side=tk.LEFT,
                                                                                                       padx=2)
        ttk.Button(button_frame, text="批量分析", command=self.start_batch_analysis, width=12).pack(side=tk.LEFT,
                                                                                                    padx=2)
        ttk.Button(button_frame, text="导出汇总报告", command=self.export_summary_report, width=12).pack(side=tk.LEFT,
                                                                                                         padx=2)
        ttk.Button(button_frame, text="导出详细数据", command=self.export_all, width=12).pack(side=tk.LEFT, padx=2)
        ttk.Button(button_frame, text="清除结果", command=self.clear_results, width=12).pack(side=tk.LEFT, padx=2)
        ttk.Button(
            button_frame,
            text="90°距离走势图",
            command=self.show_90deg_distance_plot,
            width=14,
        ).pack(side=tk.LEFT, padx=6)

        # 进度条
        self.progress = ttk.Progressbar(main_frame, mode='indeterminate')
        self.progress.pack(fill=tk.X, pady=5)

        # 结果显示区域
        result_frame = ttk.LabelFrame(main_frame, text="处理结果", padding="5")
        result_frame.pack(fill=tk.BOTH, expand=True, pady=5)

        # 使用Notebook分页
        self.notebook = ttk.Notebook(result_frame)
        self.notebook.pack(fill=tk.BOTH, expand=True)

        # 批量结果页
        batch_frame = ttk.Frame(self.notebook)
        self.notebook.add(batch_frame, text="批量处理结果")

        # 创建Treeview显示批量处理结果
        self.batch_columns = []
        self.batch_tree = None
        self.create_batch_tree(batch_frame)

        # 为每个角度创建统计结果页（用于单文件分析）
        for i in range(3):
            frame = ttk.Frame(self.notebook)
            self.notebook.add(frame, text=f"角度{i + 1}统计")

            # 创建Treeview
            columns = ('参数', '数值')
            tree = ttk.Treeview(frame, columns=columns, show='tree headings', height=10)
            tree.heading('参数', text='参数')
            tree.heading('数值', text='数值')
            tree.column('参数', width=150)
            tree.column('数值', width=150)

            scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=tree.yview)
            tree.configure(yscrollcommand=scroll.set)

            tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            scroll.pack(side=tk.RIGHT, fill=tk.Y)

            self.stats_frames[i] = frame
            self.stats_trees[i] = tree

        # 扫描统计页（多角度合并）
        scan_frame = ttk.Frame(self.notebook)
        self.notebook.add(scan_frame, text="扫描统计(合并)")

        # 为每个角度创建独立的扫描统计列
        self.scan_notebook = ttk.Notebook(scan_frame)
        self.scan_notebook.pack(fill=tk.BOTH, expand=True)

        for i in range(3):
            angle_frame = ttk.Frame(self.scan_notebook)
            self.scan_notebook.add(angle_frame, text=f"角度{i + 1}")

            columns2 = ('scan_cnt', 'count', 'mean_r_mm', 'std_r', 'min_r_mm', 'max_r_mm')
            tree = ttk.Treeview(angle_frame, columns=columns2, show='headings', height=15)

            tree.heading('scan_cnt', text='扫描计数')
            tree.heading('count', text='点数')
            tree.heading('mean_r_mm', text='平均距离(mm)')
            tree.heading('std_r', text='标准差')
            tree.heading('min_r_mm', text='最小距离(mm)')
            tree.heading('max_r_mm', text='最大距离(mm)')

            for col in columns2:
                tree.column(col, width=100)

            scroll = ttk.Scrollbar(angle_frame, orient=tk.VERTICAL, command=tree.yview)
            tree.configure(yscrollcommand=scroll.set)

            tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            scroll.pack(side=tk.RIGHT, fill=tk.Y)

            self.scan_trees[i] = tree

        # 日志页
        log_frame = ttk.Frame(self.notebook)
        self.notebook.add(log_frame, text="运行日志")

        self.log_text = scrolledtext.ScrolledText(log_frame, height=15, width=80)
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # 状态栏
        self.status_bar = ttk.Label(main_frame, text="就绪", relief=tk.SUNKEN, anchor=tk.W)
        self.status_bar.pack(fill=tk.X, pady=5)

        # 初始化
        self.toggle_angle_mode()
        self.toggle_reference()

    def create_input_fields(self):
        """创建角度和参考距离输入字段"""
        # 清除现有内容
        for widget in self.input_frame.winfo_children():
            widget.destroy()

        mode = self.angle_mode.get()

        if mode == "1":
            # 单角度模式
            ttk.Label(self.input_frame, text="角度1(度):").pack(side=tk.LEFT, padx=5)
            ttk.Entry(self.input_frame, textvariable=self.angle_vars[0], width=8).pack(side=tk.LEFT, padx=5)

            if self.use_reference.get():
                ttk.Label(self.input_frame, text="参考距离1(mm):").pack(side=tk.LEFT, padx=5)
                ttk.Entry(self.input_frame, textvariable=self.reference_vars[0], width=8).pack(side=tk.LEFT, padx=5)
        else:
            # 三角度模式
            for i in range(3):
                ttk.Label(self.input_frame, text=f"角度{i + 1}(度):").pack(side=tk.LEFT, padx=2)
                ttk.Entry(self.input_frame, textvariable=self.angle_vars[i], width=6).pack(side=tk.LEFT, padx=2)

                if self.use_reference.get():
                    ttk.Label(self.input_frame, text=f"参考{i + 1}(mm):").pack(side=tk.LEFT, padx=2)
                    ttk.Entry(self.input_frame, textvariable=self.reference_vars[i], width=6).pack(side=tk.LEFT, padx=2)

                if i < 2:
                    ttk.Label(self.input_frame, text="  ").pack(side=tk.LEFT)

    def toggle_angle_mode(self):
        """切换角度模式"""
        self.create_input_fields()

        # 更新批量结果页的列
        self.update_batch_columns()

    def toggle_reference(self):
        """切换参考距离显示"""
        self.create_input_fields()

    def create_batch_tree(self, parent):
        """创建批量结果树的列"""
        mode = self.angle_mode.get()

        if mode == "1":
            # 单角度模式
            columns = ('文件夹', '子文件夹', '文件名', '角度', '精度误差', '均值(mm)', '标准差')
            self.batch_tree = ttk.Treeview(parent, columns=columns, show='headings', height=10)

            self.batch_tree.heading('文件夹', text='文件夹')
            self.batch_tree.heading('子文件夹', text='子文件夹')
            self.batch_tree.heading('文件名', text='文件名')
            self.batch_tree.heading('角度', text='角度(°)')
            self.batch_tree.heading('精度误差', text='精度误差(mm)')
            self.batch_tree.heading('均值(mm)', text='均值(mm)')
            self.batch_tree.heading('标准差', text='标准差')

            for col in columns:
                self.batch_tree.column(col, width=120)
        else:
            # 三角度模式 - 保持原有的分列显示，增加精度误差列
            columns = ('文件夹', '子文件夹', '文件名',
                       '角度1均值', '角度1误差', '角度1标准差',
                       '角度2均值', '角度2误差', '角度2标准差',
                       '角度3均值', '角度3误差', '角度3标准差')
            self.batch_tree = ttk.Treeview(parent, columns=columns, show='headings', height=10)

            self.batch_tree.heading('文件夹', text='文件夹')
            self.batch_tree.heading('子文件夹', text='子文件夹')
            self.batch_tree.heading('文件名', text='文件名')
            self.batch_tree.heading('角度1均值', text='角度1均值(mm)')
            self.batch_tree.heading('角度1误差', text='角度1误差(mm)')
            self.batch_tree.heading('角度1标准差', text='角度1标准差')
            self.batch_tree.heading('角度2均值', text='角度2均值(mm)')
            self.batch_tree.heading('角度2误差', text='角度2误差(mm)')
            self.batch_tree.heading('角度2标准差', text='角度2标准差')
            self.batch_tree.heading('角度3均值', text='角度3均值(mm)')
            self.batch_tree.heading('角度3误差', text='角度3误差(mm)')
            self.batch_tree.heading('角度3标准差', text='角度3标准差')

            self.batch_tree.column('文件夹', width=150)
            self.batch_tree.column('子文件夹', width=150)
            self.batch_tree.column('文件名', width=150)
            self.batch_tree.column('角度1均值', width=100)
            self.batch_tree.column('角度1误差', width=100)
            self.batch_tree.column('角度1标准差', width=100)
            self.batch_tree.column('角度2均值', width=100)
            self.batch_tree.column('角度2误差', width=100)
            self.batch_tree.column('角度2标准差', width=100)
            self.batch_tree.column('角度3均值', width=100)
            self.batch_tree.column('角度3误差', width=100)
            self.batch_tree.column('角度3标准差', width=100)

        scroll = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=self.batch_tree.yview)
        self.batch_tree.configure(yscrollcommand=scroll.set)

        self.batch_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

    def update_batch_columns(self):
        """更新批量结果页的列显示"""
        if hasattr(self, 'batch_tree') and self.batch_tree:
            # 销毁旧的tree
            self.batch_tree.master.destroy()

            # 重新创建
            batch_frame = ttk.Frame(self.notebook)
            self.notebook.insert(0, batch_frame, text="批量处理结果")
            self.create_batch_tree(batch_frame)

            # 如果有数据，重新显示
            if self.batch_results:
                self.update_batch_display()

    def log(self, message: str):
        """添加日志信息"""
        self.log_text.insert(tk.END, f"{message}\n")
        self.log_text.see(tk.END)
        self.root.update_idletasks()

    def select_file(self):
        """选择单个文件"""
        filename = filedialog.askopenfilename(
            title="选择数据文件",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")]
        )
        if filename:
            self.file_path.set(filename)
            self.log(f"已选择文件: {filename}")

    def select_folder(self):
        """选择文件夹"""
        folder = filedialog.askdirectory(title="选择包含txt文件的文件夹")
        if folder:
            self.folder_path.set(folder)
            self.log(f"已选择文件夹: {folder}")

    def get_active_angles(self):
        """获取当前激活的角度列表"""
        mode = self.angle_mode.get()
        if mode == "1":
            return [self.angle_vars[0].get()]
        else:
            return [var.get() for var in self.angle_vars]

    def get_reference_distances(self):
        """获取参考距离列表"""
        if self.use_reference.get():
            mode = self.angle_mode.get()
            if mode == "1":
                return [self.reference_vars[0].get()]
            else:
                return [var.get() for var in self.reference_vars]
        return None

    def get_reference_mm_for_angle(self, target_deg: float) -> Optional[float]:
        """
        与 target_deg 最接近的界面角度档所对应的参考距离（测量距离）；未勾选参考距离时为 None。
        三角度模式下按与 target_deg 最接近的一档选取；单角度模式使用角度1的参考距离。
        """
        if not self.use_reference.get():
            return None
        mode = self.angle_mode.get()
        if mode == "1":
            return float(self.reference_vars[0].get())
        best_i = min(range(3), key=lambda i: abs(float(self.angle_vars[i].get()) - target_deg))
        return float(self.reference_vars[best_i].get())

    def start_single_analysis(self):
        """开始单文件分析"""
        if not self.file_path.get():
            messagebox.showwarning("警告", "请先选择数据文件！")
            return

        thread = threading.Thread(target=self.analyze_single_file)
        thread.daemon = True
        thread.start()

    def start_batch_analysis(self):
        """开始批量分析"""
        if not self.folder_path.get():
            messagebox.showwarning("警告", "请先选择文件夹！")
            return

        thread = threading.Thread(target=self.analyze_batch)
        thread.daemon = True
        thread.start()

    def analyze_single_file(self):
        """分析单个文件"""
        try:
            self.progress.start()
            self.status_bar.config(text="正在分析单文件...")
            self.root.update_idletasks()

            file_path = self.file_path.get()
            angles = self.get_active_angles()
            tolerance = self.tolerance.get()
            reference_distances = self.get_reference_distances()

            self.log("=" * 50)
            self.log(f"开始分析文件: {file_path}")
            if reference_distances:
                self.log(f"参考距离: {reference_distances}")

            # 分析文件
            result = analyze_single_file(file_path, angles, tolerance, reference_distances)
            self.current_file_results = [result]

            if "error" in result:
                self.log(f"文件解析失败: {result['error']}")
                self.all_points = []
                self.parsed_frames = []
            else:
                self.log(f"解析完成: 帧数={result['total_frames']}, 总点数={result['total_points']}")

                # 更新角度统计显示
                for i, angle_result in enumerate(result["angle_results"]):
                    stats = {
                        "count": angle_result["count"],
                        "mean": angle_result["mean"],
                        "std_dev": angle_result["std_dev"],
                        "min": angle_result["min"],
                        "max": angle_result["max"],
                        "accuracy_error": angle_result.get("accuracy_error")
                    }
                    self.update_stats_display(i, stats, angle_result["angle"], tolerance)

                    if angle_result.get("accuracy_error") is not None:
                        self.log(f"角度{angle_result['angle']}° 精度误差: {angle_result['accuracy_error']:.2f} mm")

                # 更新扫描统计（并缓存点云供导出 / 90° 走势图）
                parsed_frames = parse_txt(file_path)
                all_points = flatten_points(parsed_frames)
                self.parsed_frames = parsed_frames
                self.all_points = all_points
                for i, angle in enumerate(angles):
                    if i < 3:  # 最多更新3个角度
                        per_scan = stats_by_scan_cnt(all_points, angle, tolerance)
                        self.update_scan_display(i, per_scan)

            self.status_bar.config(text="单文件分析完成")

        except Exception as e:
            self.log(f"错误: {str(e)}")
            messagebox.showerror("错误", f"分析过程中发生错误:\n{str(e)}")
            self.status_bar.config(text="分析失败")
        finally:
            self.progress.stop()

    def analyze_batch(self):
        """批量分析文件夹下所有txt文件"""
        try:
            self.progress.start()
            self.status_bar.config(text="正在批量分析...")
            self.root.update_idletasks()

            folder = self.folder_path.get()
            angles = self.get_active_angles()
            tolerance = self.tolerance.get()
            reference_distances = self.get_reference_distances()

            # 获取所有txt文件
            txt_files = []
            for root, dirs, files in os.walk(folder):
                for file in files:
                    if file.lower().endswith('.txt'):
                        txt_files.append(os.path.join(root, file))

            self.log("=" * 50)
            self.log(f"开始批量分析文件夹: {folder}")
            self.log(f"找到 {len(txt_files)} 个txt文件")
            if reference_distances:
                self.log(f"参考距离: {reference_distances}")

            self.batch_results = []
            total_files = len(txt_files)

            for i, file_path in enumerate(txt_files, 1):
                self.log(f"处理文件 [{i}/{total_files}]: {os.path.basename(file_path)}")

                result = analyze_single_file(file_path, angles, tolerance, reference_distances)
                self.batch_results.append(result)

                # 更新批量结果显示
                self.update_batch_display()

            self.log(f"\n批量分析完成，成功处理 {len([r for r in self.batch_results if 'error' not in r])} 个文件")
            self.status_bar.config(text="批量分析完成")

        except Exception as e:
            self.log(f"批量分析错误: {str(e)}")
            messagebox.showerror("错误", f"批量分析过程中发生错误:\n{str(e)}")
            self.status_bar.config(text="批量分析失败")
        finally:
            self.progress.stop()

    def update_batch_display(self):
        """更新批量结果显示"""
        # 清空现有内容
        for item in self.batch_tree.get_children():
            self.batch_tree.delete(item)

        mode = self.angle_mode.get()
        root_folder = self.folder_path.get() if self.folder_path.get() else ""

        # 添加结果
        for result in self.batch_results:
            if "error" in result:
                root_name, sub_name = get_folder_info(result["file_path"], root_folder)
                if mode == "1":
                    self.batch_tree.insert('', 'end', values=(
                        root_name,
                        sub_name,
                        result["file_name"],
                        "错误",
                        result["error"],
                        "",
                        ""
                    ))
                else:
                    self.batch_tree.insert('', 'end', values=(
                        root_name,
                        sub_name,
                        result["file_name"],
                        "错误",
                        result["error"],
                        "",
                        "",
                        "",
                        "",
                        "",
                        "",
                        ""
                    ))
            else:
                root_name, sub_name = get_folder_info(result["file_path"], root_folder)

                if mode == "1":
                    # 单角度格式
                    angle_result = result["angle_results"][0]
                    mean_val = f"{angle_result['mean']:.2f}" if angle_result['mean'] is not None else "N/A"
                    std_val = f"{angle_result['std_dev']:.2f}" if angle_result['std_dev'] is not None else "N/A"
                    error_val = f"{angle_result.get('accuracy_error', 0):.2f}" if angle_result.get(
                        'accuracy_error') is not None else "N/A"

                    self.batch_tree.insert('', 'end', values=(
                        root_name,
                        sub_name,
                        result["file_name"],
                        f"{angle_result['angle']:.1f}",
                        error_val,
                        mean_val,
                        std_val
                    ))
                else:
                    # 三角度格式 - 保持分列显示，增加精度误差
                    values = [root_name, sub_name, result["file_name"]]

                    for angle_result in result["angle_results"]:
                        mean_val = f"{angle_result['mean']:.2f}" if angle_result['mean'] is not None else "N/A"
                        error_val = f"{angle_result.get('accuracy_error', 0):.2f}" if angle_result.get(
                            'accuracy_error') is not None else "N/A"
                        std_val = f"{angle_result['std_dev']:.2f}" if angle_result['std_dev'] is not None else "N/A"
                        values.extend([mean_val, error_val, std_val])

                    # 补齐到12列
                    while len(values) < 12:
                        values.append("")

                    self.batch_tree.insert('', 'end', values=values)

    def update_stats_display(self, index: int, stats: Dict, angle: float, tolerance: float):
        """更新统计结果显示"""
        tree = self.stats_trees[index]

        # 清空现有内容
        for item in tree.get_children():
            tree.delete(item)

        # 添加统计结果
        tree.insert('', 'end', values=('目标角度', f"{angle}°"))
        tree.insert('', 'end', values=('容差', f"{tolerance}°"))
        tree.insert('', 'end', values=('命中点数', stats['count']))

        mean_val = f"{stats['mean']:.2f}" if stats['mean'] is not None else 'N/A'
        tree.insert('', 'end', values=('平均距离(mm)', mean_val))

        if stats.get('accuracy_error') is not None:
            error_val = f"{stats['accuracy_error']:.2f}"
            tree.insert('', 'end', values=('精度误差(mm)', error_val))

        var_val = f"{stats.get('variance', 0):.2f}" if stats.get('variance') is not None else 'N/A'
        tree.insert('', 'end', values=('方差', var_val))

        std_val = f"{stats['std_dev']:.2f}" if stats['std_dev'] is not None else 'N/A'
        tree.insert('', 'end', values=('标准差', std_val))

        min_val = f"{stats['min']:.2f}" if stats['min'] is not None else 'N/A'
        tree.insert('', 'end', values=('最小距离(mm)', min_val))

        max_val = f"{stats['max']:.2f}" if stats['max'] is not None else 'N/A'
        tree.insert('', 'end', values=('最大距离(mm)', max_val))

    def update_scan_display(self, index: int, scan_stats: List[Dict]):
        """更新扫描统计显示"""
        tree = self.scan_trees[index]

        # 清空现有内容
        for item in tree.get_children():
            tree.delete(item)

        # 添加扫描统计
        for stat in scan_stats:
            tree.insert('', 'end', values=(
                stat['scan_cnt'],
                stat['count'],
                f"{stat['mean_r_mm']:.2f}" if stat['mean_r_mm'] is not None else 'N/A',
                f"{stat['std_r']:.2f}" if stat['std_r'] is not None else 'N/A',
                f"{stat['min_r_mm']:.2f}" if stat['min_r_mm'] is not None else 'N/A',
                f"{stat['max_r_mm']:.2f}" if stat['max_r_mm'] is not None else 'N/A'
            ))

    def show_90deg_distance_plot(self):
        """弹出窗口：展示 90°（±当前容差）条件下，各扫描周期平均距离随 scan_cnt 的变化。"""
        file_path = self.file_path.get().strip()
        if not file_path or not os.path.isfile(file_path):
            messagebox.showwarning("警告", "请先选择有效的 txt 数据文件。")
            return

        try:
            import matplotlib

            matplotlib.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans", "sans-serif"]
            matplotlib.rcParams["axes.unicode_minus"] = False
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
            from matplotlib.figure import Figure
        except ImportError:
            messagebox.showerror(
                "缺少依赖",
                "绘制图表需要安装 matplotlib：\npip install matplotlib",
            )
            return

        target_deg = 90.0
        tolerance = self.tolerance.get()

        try:
            parsed_frames = parse_txt(file_path)
            all_points = flatten_points(parsed_frames)
            per_scan = stats_by_scan_cnt(all_points, target_deg, tolerance)
        except Exception as e:
            messagebox.showerror("错误", f"解析或统计失败:\n{e}")
            return

        if not per_scan:
            messagebox.showinfo(
                "提示",
                f"在 {target_deg:g}° ± {tolerance}° 容差下无命中点，无法绘制走势图。\n"
                "可适当增大容差或确认数据中包含该角度。",
            )
            return

        scan_cnts = [row["scan_cnt"] for row in per_scan]
        means = [row["mean_r_mm"] for row in per_scan]

        # 平均距离（各 scan 平均距离的均值）、跨扫描标准差、系统/实际误差（需参考距离）
        overall_mean = sum(means) / len(means)
        ms = calc_stats(means)
        std_across_scans = ms["std_dev"] if ms["std_dev"] is not None else 0.0
        meas_mm = self.get_reference_mm_for_angle(target_deg)
        if meas_mm is not None:
            system_err = abs(overall_mean - meas_mm)
            actual_err = system_err + std_across_scans
        else:
            system_err = None
            actual_err = None

        win = tk.Toplevel(self.root)
        win.title("90° 距离走势图")
        win.geometry("920x620")

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
            line1 = "1. 系统误差（平均距离−测量距离）: —（请勾选「使用参考距离」并填写测量距离）"
            line3 = "3. 实际误差（系统误差+标准差）: —"
        line2 = f"2. 标准差距离: {std_across_scans:.3f} mm"
        anno = "\n".join([line1, line2, line3])
        # 标注放在图表上方图外区域，避免遮挡曲线
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

        canvas = FigureCanvasTkAgg(fig, master=win)
        canvas.draw()
        canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        base = os.path.basename(file_path)
        ttk.Label(win, text=f"数据文件: {base}", anchor=tk.W).pack(fill=tk.X, padx=8, pady=(6, 0))

    def export_summary_report(self):
        """导出汇总报告到txt文件"""
        if not self.batch_results and not self.current_file_results:
            messagebox.showwarning("警告", "没有数据可导出，请先进行分析！")
            return

        try:
            # 选择保存文件
            save_path = filedialog.asksaveasfilename(
                title="保存汇总报告",
                defaultextension=".txt",
                filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")]
            )
            if not save_path:
                return

            self.status_bar.config(text="正在导出汇总报告...")

            results_to_export = self.batch_results if self.batch_results else self.current_file_results
            angles = self.get_active_angles()
            root_folder = self.folder_path.get() if self.folder_path.get() else ""
            reference_distances = self.get_reference_distances()

            # 设置全局变量供报告函数使用
            global tolerance_value
            tolerance_value = self.tolerance.get()

            # 保存汇总报告
            save_summary_report(results_to_export, save_path, angles, root_folder, reference_distances)

            self.log(f"已导出汇总报告: {save_path}")
            messagebox.showinfo("成功", f"汇总报告已成功导出到:\n{save_path}")
            self.status_bar.config(text="导出完成")

        except Exception as e:
            self.log(f"导出错误: {str(e)}")
            messagebox.showerror("错误", f"导出失败:\n{str(e)}")
            self.status_bar.config(text="导出失败")

    def export_all(self):
        """导出所有详细数据到CSV"""
        if not hasattr(self, 'all_points') and not self.batch_results:
            messagebox.showwarning("警告", "没有数据可导出，请先进行分析！")
            return

        try:
            # 选择保存目录
            save_dir = filedialog.askdirectory(title="选择保存目录")
            if not save_dir:
                return

            self.status_bar.config(text="正在导出详细数据...")

            angles = self.get_active_angles()
            tolerance = self.tolerance.get()

            if self.batch_results:
                # 批量模式：为每个文件导出数据
                for result in self.batch_results:
                    if "error" in result:
                        continue

                    file_name = os.path.splitext(result["file_name"])[0]
                    file_dir = os.path.join(save_dir, file_name)
                    os.makedirs(file_dir, exist_ok=True)

                    # 重新解析文件以获取详细点数据
                    parsed_frames = parse_txt(result["file_path"])
                    all_points = flatten_points(parsed_frames)

                    # 导出全部点
                    all_points_path = os.path.join(file_dir, "all_points.csv")
                    save_csv(all_points, all_points_path)

                    # 为每个角度导出数据
                    for i, angle in enumerate(angles):
                        selected = filter_points_by_angle(all_points, angle, tolerance)
                        selected_path = os.path.join(file_dir, f"angle{i + 1}_{angle}deg_selected.csv")
                        save_csv(selected, selected_path)

                        per_scan = stats_by_scan_cnt(all_points, angle, tolerance)
                        scan_path = os.path.join(file_dir, f"angle{i + 1}_{angle}deg_stats.csv")
                        save_csv(per_scan, scan_path)

                    self.log(f"已导出文件 {result['file_name']} 的详细数据")
            else:
                # 单文件模式
                if hasattr(self, 'all_points') and self.all_points:
                    file_name = os.path.splitext(os.path.basename(self.file_path.get()))[0]
                    file_dir = os.path.join(save_dir, file_name)
                    os.makedirs(file_dir, exist_ok=True)

                    # 导出全部点
                    all_points_path = os.path.join(file_dir, "all_points.csv")
                    save_csv(self.all_points, all_points_path)

                    # 为每个角度导出数据
                    for i, angle in enumerate(angles):
                        selected = filter_points_by_angle(self.all_points, angle, tolerance)
                        selected_path = os.path.join(file_dir, f"angle{i + 1}_{angle}deg_selected.csv")
                        save_csv(selected, selected_path)

                        per_scan = stats_by_scan_cnt(self.all_points, angle, tolerance)
                        scan_path = os.path.join(file_dir, f"angle{i + 1}_{angle}deg_stats.csv")
                        save_csv(per_scan, scan_path)

                    self.log(f"已导出文件 {file_name} 的详细数据")

            messagebox.showinfo("成功", f"详细数据已成功导出到:\n{save_dir}")
            self.status_bar.config(text="导出完成")

        except Exception as e:
            self.log(f"导出错误: {str(e)}")
            messagebox.showerror("错误", f"导出失败:\n{str(e)}")
            self.status_bar.config(text="导出失败")

    def clear_results(self):
        """清除所有结果"""
        # 清除批量结果显示
        if hasattr(self, 'batch_tree'):
            for item in self.batch_tree.get_children():
                self.batch_tree.delete(item)

        # 清除所有统计树的显示
        for tree in self.stats_trees.values():
            for item in tree.get_children():
                tree.delete(item)

        # 清除所有扫描统计树的显示
        for tree in self.scan_trees.values():
            for item in tree.get_children():
                tree.delete(item)

        self.log_text.delete(1.0, tk.END)
        self.batch_results = []
        self.current_file_results = []
        self.parsed_frames = []
        self.all_points = []
        self.status_bar.config(text="已清除")
        self.log("已清除所有结果")


def main():
    root = tk.Tk()
    app = LidarAnalyzerGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()