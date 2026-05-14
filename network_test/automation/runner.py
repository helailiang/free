"""
C2/H1 网络通信自动化命令行入口。

常用方式：
1. 冒烟测试：`python -m network_test.automation.runner --config network_test/automation/configs/h1.example.json --mode smoke`
2. 长稳测试：`python -m network_test.automation.runner --config ... --mode stability --duration-s 3600`
3. Pytest：`python -m network_test.automation.runner --config ... --mode pytest`
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import subprocess
import sys
import time

from network_test.automation.clients import create_radar_client
from network_test.automation.clients.base import RadarClientError
from network_test.automation.config import DeviceConfig, load_device_config
from network_test.automation.reports import CaseResult, ReportWriter, RunReport


def _case_result(name: str, outcome: str, started: float, message: str = "", metrics: dict | None = None) -> CaseResult:
    """统一创建用例结果，避免每个流程重复计算耗时。"""
    return CaseResult(
        name=name,
        outcome=outcome,
        duration_s=max(0.0, time.monotonic() - started),
        message=message,
        metrics=metrics or {},
    )


def run_smoke(config: DeviceConfig) -> RunReport:
    """
    执行短流程冒烟测试：连接、配置/协议查询、短时间连续取数。

    该模式适合开发阶段和现场开测前验证线缆、IP、端口、协议命令是否正确。
    """
    report = RunReport(
        title="C2/H1 网络通信冒烟测试",
        device_name=config.name,
        model=config.normalized_model,
        host=config.host,
        started_at=datetime.now().isoformat(timespec="seconds"),
        notes=["H1/H2 命名已统一按 H1 协议处理；ROS 本轮暂不纳入。"],
    )

    client = create_radar_client(config)
    started = time.monotonic()
    try:
        client.connect()
        report.cases.append(_case_result("tcp_connect_and_login", "passed", started))
    except RadarClientError as exc:
        report.cases.append(_case_result("tcp_connect_and_login", "failed", started, str(exc)))
        return report

    started = time.monotonic()
    try:
        reply = client.query_config()
        report.cases.append(
            _case_result(
                "protocol_query",
                "passed" if reply else "failed",
                started,
                "收到协议应答" if reply else "协议查询无应答",
                {"reply_bytes": len(reply), "reply_hex_prefix": reply[:32].hex(" ").upper()},
            )
        )
    except RadarClientError as exc:
        report.cases.append(_case_result("protocol_query", "failed", started, str(exc)))

    started = time.monotonic()
    try:
        client.start_streaming()
        stats = client.read_stream_stats(
            duration_s=float(config.stream.sample_duration_s),
            max_cycles=int(config.stream.sample_cycles),
        )
        client.stop_streaming()
        outcome = "passed"
        message = "连续取数正常"
        if stats.frames_received <= 0:
            outcome = "failed"
            message = "未收到连续取数数据帧"
        elif stats.loss_rate_percent > config.stream_loss_limit_percent:
            outcome = "failed"
            message = f"缺包率 {stats.loss_rate_percent}% 超过阈值 {config.stream_loss_limit_percent}%"
        report.cases.append(_case_result("stream_sample", outcome, started, message, stats.to_dict()))
    except RadarClientError as exc:
        report.cases.append(_case_result("stream_sample", "failed", started, str(exc)))
    finally:
        client.close()

    return report


def run_stability(config: DeviceConfig, *, duration_s: float, window_s: float, event_log: str | None) -> RunReport:
    """
    执行长稳窗口测试。

    长稳不一次性保存所有点云，只按窗口累计指标；如果现场有拔网线、交换机重启等人工动作，
    可把事件记录文件路径传入报告 notes，便于测试后人工对齐时间线。
    """
    report = RunReport(
        title="C2/H1 网络通信长稳测试",
        device_name=config.name,
        model=config.normalized_model,
        host=config.host,
        started_at=datetime.now().isoformat(timespec="seconds"),
        notes=[
            f"计划运行 {duration_s:.1f}s，窗口 {window_s:.1f}s。",
            "人工网络损伤、通断网和电源动作请同步记录时间点。",
        ],
    )
    if event_log:
        report.notes.append(f"人工事件记录文件：{event_log}")

    end_at = time.monotonic() + max(1.0, duration_s)
    window_index = 0
    client = create_radar_client(config)

    while time.monotonic() < end_at:
        started = time.monotonic()
        window_index += 1
        try:
            client.connect()
            client.start_streaming()
            stats = client.read_stream_stats(duration_s=min(window_s, max(0.1, end_at - time.monotonic())))
            client.stop_streaming()
            outcome = "passed" if stats.frames_received > 0 else "failed"
            message = "窗口取数正常" if outcome == "passed" else "窗口内未收到数据"
            report.cases.append(_case_result(f"stability_window_{window_index}", outcome, started, message, stats.to_dict()))
        except RadarClientError as exc:
            report.cases.append(_case_result(f"stability_window_{window_index}", "failed", started, str(exc)))
            time.sleep(min(2.0, config.recovery_timeout_s))
        finally:
            client.close()

    return report


def run_pytest(config_path: Path, report_dir: Path) -> int:
    """通过子进程启动 Pytest，保证命令行 runner 和直接 pytest 使用同一套用例。"""
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "network_test/automation/tests",
        "--radar-config",
        str(config_path),
        "--radar-report-dir",
        str(report_dir),
    ]
    return subprocess.run(cmd, check=False).returncode


def main(argv: list[str] | None = None) -> int:
    """解析命令行参数并执行指定模式。"""
    parser = argparse.ArgumentParser(description="C2/H1 单线激光雷达网络通信自动化测试")
    parser.add_argument("--config", required=True, help="设备 JSON 配置路径")
    parser.add_argument("--mode", choices=["smoke", "stability", "pytest"], default="smoke", help="测试模式")
    parser.add_argument("--duration-s", type=float, default=3600.0, help="长稳总时长，单位秒")
    parser.add_argument("--window-s", type=float, default=60.0, help="长稳单窗口取数时长，单位秒")
    parser.add_argument("--event-log", default=None, help="人工事件记录文件路径，可选")
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    config = load_device_config(config_path)
    report_dir = Path(config.report_dir)

    if args.mode == "pytest":
        return run_pytest(config_path, report_dir)

    if args.mode == "stability":
        report = run_stability(config, duration_s=float(args.duration_s), window_s=float(args.window_s), event_log=args.event_log)
    else:
        report = run_smoke(config)

    paths = ReportWriter(report_dir).write_all(report, prefix=args.mode)
    for kind, path in paths.items():
        print(f"{kind}: {path}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
