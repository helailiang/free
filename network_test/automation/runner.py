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
from network_test.automation.ping import PING_METRIC_EXPLANATIONS, run_ping_test
from network_test.automation.reports import CaseResult, ReportWriter, RunReport


def _case_result(
    name: str,
    outcome: str,
    started: float,
    message: str = "",
    metrics: dict | None = None,
    *,
    category: str = "general",
) -> CaseResult:
    """统一创建用例结果，避免每个流程重复计算耗时。"""
    return CaseResult(
        name=name,
        outcome=outcome,
        duration_s=max(0.0, time.monotonic() - started),
        message=message,
        category=category,
        metrics=metrics or {},
    )


def read_event_log_lines(event_log: str | None) -> list[str]:
    """
    读取人工事件记录。

    长稳测试中的拔网线、交换机重启、网络损伤仪注入等动作往往不能由脚本直接控制。
    这里把人工记录文件读入报告，测试后可将异常窗口与人工动作时间点对齐。
    """
    if not event_log:
        return []
    path = Path(event_log)
    if not path.exists():
        return [f"事件文件不存在：{event_log}"]
    lines = [line.strip() for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()]
    return [line for line in lines if line and not line.lstrip().startswith("#")]


def judge_stream_stats(config: DeviceConfig, stats_dict: dict) -> tuple[str, str, str]:
    """
    根据连续取数统计值给出 PASS/FAIL、失败分类和可读原因。

    判定顺序按现场排查优先级排列：先看有没有数据，再看解析是否失败，最后看缺包率。
    """
    frames = int(stats_dict.get("frames_received") or 0)
    parse_errors = int(stats_dict.get("parse_errors") or 0)
    loss_rate = float(stats_dict.get("loss_rate_percent") or 0.0)
    if frames <= 0:
        return "failed", "data_loss", "未收到连续取数数据帧"
    if parse_errors > 0:
        return "failed", "protocol_parse", f"存在 {parse_errors} 个解析失败帧"
    if loss_rate > config.stream_loss_limit_percent:
        return (
            "failed",
            "data_quality",
            f"缺包率 {loss_rate}% 超过阈值 {config.stream_loss_limit_percent}%",
        )
    return "passed", "data_quality", "连续取数正常"


def build_stability_summary(
    config: DeviceConfig,
    report: RunReport,
    *,
    duration_s: float,
    window_s: float,
    event_lines: list[str],
    recovery_events: list[dict],
) -> dict:
    """汇总长稳整轮指标，避免测试人员只看到大量窗口明细而难以判断总结果。"""
    stream_cases = [case for case in report.cases if case.name.startswith("stability_window_")]
    failed_cases = [case for case in stream_cases if case.outcome in {"failed", "error"}]
    metrics_list = [case.metrics for case in stream_cases if case.metrics]
    total_frames = sum(int(m.get("frames_received") or 0) for m in metrics_list)
    total_parse_errors = sum(int(m.get("parse_errors") or 0) for m in metrics_list)
    max_loss_rate = max((float(m.get("loss_rate_percent") or 0.0) for m in metrics_list), default=0.0)
    max_gap_s = max((float(m.get("max_inter_frame_gap_s") or 0.0) for m in metrics_list), default=0.0)
    slow_recovery_events = [
        item for item in recovery_events if float(item.get("outage_duration_s") or 0.0) > config.recovery_timeout_s
    ]

    return {
        "planned_duration_s": duration_s,
        "window_s": window_s,
        "window_count": len(stream_cases),
        "failed_window_count": len(failed_cases),
        "total_frames_received": total_frames,
        "total_parse_errors": total_parse_errors,
        "max_loss_rate_percent": round(max_loss_rate, 4),
        "max_inter_frame_gap_s": round(max_gap_s, 4),
        "loss_rate_limit_percent": config.stream_loss_limit_percent,
        "recovery_timeout_limit_s": config.recovery_timeout_s,
        "recovery_events": recovery_events,
        "slow_recovery_event_count": len(slow_recovery_events),
        "manual_event_count": len(event_lines),
        "manual_events": event_lines,
    }


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
        report.cases.append(_case_result("tcp_connect_and_login", "passed", started, category="connectivity"))
    except RadarClientError as exc:
        report.cases.append(_case_result("tcp_connect_and_login", "failed", started, str(exc), category="connectivity"))
        report.summary = {"failed_stage": "tcp_connect_and_login", "reason": str(exc)}
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
                category="protocol",
            )
        )
    except RadarClientError as exc:
        report.cases.append(_case_result("protocol_query", "failed", started, str(exc), category="protocol"))

    started = time.monotonic()
    try:
        client.start_streaming()
        stats = client.read_stream_stats(
            duration_s=float(config.stream.sample_duration_s),
            max_cycles=int(config.stream.sample_cycles),
        )
        client.stop_streaming()
        stats_dict = stats.to_dict()
        outcome, category, message = judge_stream_stats(config, stats_dict)
        report.cases.append(_case_result("stream_sample", outcome, started, message, stats_dict, category=category))
    except RadarClientError as exc:
        report.cases.append(_case_result("stream_sample", "failed", started, str(exc), category="data_stream"))
    finally:
        client.close()

    report.summary = {
        "case_count": len(report.cases),
        "passed": report.passed,
        "thresholds": {
            "stream_loss_limit_percent": config.stream_loss_limit_percent,
            "recovery_timeout_s": config.recovery_timeout_s,
        },
    }
    return report


def run_ping_report(config: DeviceConfig, *, duration_s: float | None = None) -> RunReport:
    """
    执行 HW-03 正式 ping/RTT 测试。

    与 TCP 连通不同，HW-03 要求长时间 ping 并统计成功率、平均 RTT、最大 RTT。
    正式测试建议使用配置默认 604800 秒，也就是 7 天。
    """
    report = RunReport(
        title="C2/H1 HW-03 持续 ping 与 RTT 测试",
        device_name=config.name,
        model=config.normalized_model,
        host=config.host,
        started_at=datetime.now().isoformat(timespec="seconds"),
        notes=[
            "该模式对齐 HW-03：持续 ping，统计成功率、RTT 均值、RTT 最大值和抖动。",
            "正式准入建议 duration_s=604800，即 7 天。",
        ],
    )
    started = time.monotonic()
    stats = run_ping_test(config, duration_s=duration_s)
    stats_dict = stats.to_dict()
    success_rate = float(stats.success_rate_percent)
    avg_rtt = stats.rtt_avg_ms
    outcome = "passed"
    message = "HW-03 ping 成功率和 RTT 统计正常"
    if success_rate < config.thresholds.ping_success_rate_min_percent:
        outcome = "failed"
        message = (
            f"ping 成功率 {success_rate}% 低于阈值 "
            f"{config.thresholds.ping_success_rate_min_percent}%"
        )
    elif avg_rtt is None:
        outcome = "failed"
        message = "未统计到有效 RTT"
    elif avg_rtt >= 2.0:
        outcome = "failed"
        message = f"平均 RTT {avg_rtt}ms 不满足局域网 <2ms 建议标准"

    report.cases.append(
        _case_result("hw03_ping_rtt", outcome, started, message, stats_dict, category="connectivity")
    )
    report.summary = {
        "hw03_requirement": "持续 ping，记录成功率、平均 RTT、最大 RTT；正式测试建议 7 天。",
        "ping_success_rate_min_percent": config.thresholds.ping_success_rate_min_percent,
        "rtt_avg_limit_ms": 2.0,
        "how_to_read": [
            "先看 success_rate_percent：应不低于 ping_success_rate_min_percent。",
            "再看 rtt_avg_ms：局域网平均 RTT 建议小于 rtt_avg_limit_ms。",
            "再看 rtt_max_ms 和 jitter_ms：用于判断是否存在偶发卡顿或延迟波动。",
            "最后看 errors：如果非空，说明脚本调用系统 ping 命令时出现异常。",
        ],
        "metric_explanations": PING_METRIC_EXPLANATIONS,
        "stats": stats_dict,
    }
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
    event_lines = read_event_log_lines(event_log)
    if event_log:
        report.notes.append(f"人工事件记录文件：{event_log}")
        report.notes.append(f"已读取人工事件 {len(event_lines)} 条。")

    end_at = time.monotonic() + max(1.0, duration_s)
    window_index = 0
    client = create_radar_client(config)
    outage_started_at: float | None = None
    recovery_events: list[dict] = []

    while time.monotonic() < end_at:
        started = time.monotonic()
        window_index += 1
        try:
            client.connect()
            client.start_streaming()
            stats = client.read_stream_stats(duration_s=min(window_s, max(0.1, end_at - time.monotonic())))
            client.stop_streaming()
            stats_dict = stats.to_dict()
            outcome, category, message = judge_stream_stats(config, stats_dict)
            if outcome == "passed" and outage_started_at is not None:
                outage_duration = time.monotonic() - outage_started_at
                recovery_events.append(
                    {
                        "recovered_at_window": window_index,
                        "outage_duration_s": round(outage_duration, 4),
                        "within_threshold": outage_duration <= config.recovery_timeout_s,
                    }
                )
                outage_started_at = None
            report.cases.append(
                _case_result(
                    f"stability_window_{window_index}",
                    outcome,
                    started,
                    message,
                    stats_dict,
                    category=category,
                )
            )
        except RadarClientError as exc:
            if outage_started_at is None:
                outage_started_at = time.monotonic()
            report.cases.append(
                _case_result(
                    f"stability_window_{window_index}",
                    "failed",
                    started,
                    str(exc),
                    {"recovery_timeout_s": config.recovery_timeout_s},
                    category="connectivity",
                )
            )
            time.sleep(min(2.0, config.recovery_timeout_s))
        finally:
            client.close()

    if outage_started_at is not None:
        outage_duration = time.monotonic() - outage_started_at
        recovery_events.append(
            {
                "recovered_at_window": None,
                "outage_duration_s": round(outage_duration, 4),
                "within_threshold": False,
                "note": "测试结束前仍未恢复",
            }
        )

    report.summary = build_stability_summary(
        config,
        report,
        duration_s=duration_s,
        window_s=window_s,
        event_lines=event_lines,
        recovery_events=recovery_events,
    )
    if report.summary["slow_recovery_event_count"] > 0:
        report.cases.append(
            CaseResult(
                name="stability_recovery_summary",
                outcome="failed",
                duration_s=0.0,
                message="存在超过恢复阈值的断线/恢复事件",
                category="recovery",
                metrics={"recovery_events": recovery_events},
            )
        )
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
    parser.add_argument("--mode", choices=["smoke", "ping", "stability", "pytest"], default="smoke", help="测试模式")
    parser.add_argument(
        "--duration-s",
        type=float,
        default=None,
        help="测试总时长，单位秒；ping 模式不传时使用配置中的 7 天 duration_s，长稳不传时默认 3600 秒",
    )
    parser.add_argument("--window-s", type=float, default=60.0, help="长稳单窗口取数时长，单位秒")
    parser.add_argument("--event-log", default=None, help="人工事件记录文件路径，可选")
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    config = load_device_config(config_path)
    report_dir = Path(config.report_dir)

    if args.mode == "pytest":
        return run_pytest(config_path, report_dir)

    if args.mode == "ping":
        report = run_ping_report(config, duration_s=float(args.duration_s) if args.duration_s is not None else None)
    elif args.mode == "stability":
        stability_duration_s = float(args.duration_s) if args.duration_s is not None else 3600.0
        report = run_stability(
            config,
            duration_s=stability_duration_s,
            window_s=float(args.window_s),
            event_log=args.event_log,
        )
    else:
        report = run_smoke(config)

    paths = ReportWriter(report_dir).write_all(report, prefix=args.mode)
    for kind, path in paths.items():
        print(f"{kind}: {path}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
