"""
连续取数与数据完整性测试。

该用例对应 PROT-02、NET-01 中的应用层数据连续性统计。第一版按配置中的圈数或时长
读取数据，重点检查是否有帧、解析错误和缺包率是否超过阈值。
"""

from __future__ import annotations

import pytest

from network_test.automation.clients.base import RadarClientError
from network_test.automation.config import DeviceConfig
from network_test.automation.metrics import StreamStats

from .conftest import attach_metrics

# 与 `StreamStats` / `metrics.py` 字段一一对应，便于现场对照报告与断言失败信息。
STREAM_SAMPLE_METRIC_EXPLANATIONS: dict[str, str] = {
    "model": "设备型号（c2/h1），与配置一致。",
    "host": "被测雷达 IP。",
    "started_at_s": "统计窗口开始时刻（time.monotonic 秒），非墙钟。",
    "ended_at_s": "统计窗口结束时刻；与 started 之差 ≈ 采样时长。",
    "frames_received": "成功解析并计入统计的 TCP 应用层帧总数。",
    "scans_seen": "观测到的不重复扫描圈号（scan_id）个数。",
    "completed_scans": "包号齐全（每圈收满 expected_packets_per_scan）的完整圈数。",
    "loss_evaluated_scans": "参与缺包率统计的圈数；窗口首尾被时间截断的不完整圈会跳过。",
    "boundary_partial_scans_ignored": "因采样窗口边界截断而未纳入缺包率统计的不完整圈数。",
    "points_received": "各帧点数之和，粗看吞吐；不单独校验每点正确性。",
    "parse_errors": "无法按协议解析的帧数；>0 时查粘包/校验/固件。",
    "duplicate_packets": "同一 (圈号, 包号) 重复到达次数。",
    "missing_packets": "在可判定圈内按每圈应有包数推算的缺包总数。",
    "expected_packets": "理论应收包数 = 参与缺包率统计的圈数 × 每圈应有包数。",
    "loss_rate_percent": "缺包率(%) = missing_packets / expected_packets × 100；与准入阈值比对。",
    "frame_rate_hz": "平均帧率 = frames_received / 窗口秒数。",
    "max_inter_frame_gap_s": "相邻两帧接收时间间隔的最大值（秒）。",
    "reconnect_count": "本窗口内记录的重连次数（本短测通常为 0）。",
    "longest_data_gap_s": "与 max_inter_frame_gap_s 同源，报告字段兼容用。",
    "notes": "附加说明字符串列表。",
    "stream_loss_limit_percent_applied": "本用例实际使用的缺包率准入阈值（%），来自配置 thresholds。",
    "sample_cycles_applied": "本测传入 read_stream_stats 的 max_cycles：收满多少完整圈后停止。",
    "sample_duration_s_applied": "本测采样时长上限（秒）；与 sample_cycles 先满足其一即停止收数。",
    "window_duration_s": "本窗口有效统计时长（秒）= ended_at_s - started_at_s，用于折算频率。",
    "implied_scan_rate_hz": "推算扫描频率（圈/秒）= frame_rate_hz ÷ expected_packets_per_scan；每圈一包序列为 1 圈。",
    "completed_scan_rate_hz": "完整圈频率（圈/秒）= completed_scans ÷ window_duration_s；仅统计包号齐全的圈。",
    "expected_scan_rate_hz": "配置期望扫描频率（Hz），来自 stream.expected_frame_rate_hz。",
    "scan_rate_vs_expected_error_percent": "|implied_scan_rate_hz - expected_scan_rate_hz| / 期望 × 100；与 frame_rate 容差比对。",
    "frame_rate_tolerance_percent_applied": "本用例扫描频率相对误差上限（%），来自 thresholds.frame_rate_tolerance_percent。",
    "sample_cycles_met": "是否已收满 sample_cycles 个完整圈（completed_scans ≥ 配置值）。",
    "sample_duration_met": "是否已跑满 sample_duration_s 时长上限（window_duration_s ≥ 配置×0.98）。",
}


def _sample_stop_conditions_met(
    stats: StreamStats, radar_config: DeviceConfig
) -> tuple[bool, bool, float]:
    """
    判断 read_stream_stats 的两种正常结束条件是否至少满足其一。

    与 `clients/base.py` 一致：收满 sample_cycles 个完整圈，或跑满 sample_duration_s 时长上限。
    仅当两者都未满足时视为异常提前结束（断流、空连接等）。
    """
    window_s = max(0.0, float(stats.ended_at_s - stats.started_at_s))
    cycles_ok = stats.completed_scans >= int(radar_config.stream.sample_cycles)
    # 与 deadline 对齐，留约 2% 容差抵消循环末尾计时误差。
    duration_ok = window_s >= float(radar_config.stream.sample_duration_s) * 0.98
    return cycles_ok, duration_ok, window_s


def _stream_frequency_metrics(stats: StreamStats, radar_config: DeviceConfig) -> dict[str, float]:
    """
    由连续取数统计推导扫描频率指标，并给出与配置期望的相对误差。

    H1/C2 连续流按「每圈 expected_packets_per_scan 个 TCP 帧」折算圈速；
    与 `stream.expected_frame_rate_hz`（典型 30Hz）对照，使用 `frame_rate_tolerance_percent` 作准入。
    """
    # 统计窗口时长，避免除零；与 StreamMetrics.finish 中 duration 定义一致。
    window_duration_s = max(0.001, float(stats.ended_at_s - stats.started_at_s))
    epp = max(0, int(radar_config.stream.expected_packets_per_scan))
    implied_scan_rate_hz = (
        round(float(stats.frame_rate_hz) / float(epp), 4) if epp > 0 else 0.0
    )
    completed_scan_rate_hz = round(float(stats.completed_scans) / window_duration_s, 4)
    expected_scan_rate_hz = float(radar_config.stream.expected_frame_rate_hz)
    if expected_scan_rate_hz > 0:
        scan_rate_vs_expected_error_percent = round(
            abs(implied_scan_rate_hz - expected_scan_rate_hz) / expected_scan_rate_hz * 100.0,
            4,
        )
    else:
        scan_rate_vs_expected_error_percent = 0.0
    return {
        "window_duration_s": round(window_duration_s, 4),
        "implied_scan_rate_hz": implied_scan_rate_hz,
        "completed_scan_rate_hz": completed_scan_rate_hz,
        "expected_scan_rate_hz": expected_scan_rate_hz,
        "scan_rate_vs_expected_error_percent": scan_rate_vs_expected_error_percent,
        "frame_rate_tolerance_percent_applied": float(radar_config.thresholds.frame_rate_tolerance_percent),
    }


def _stream_stats_report_payload(
    stats: StreamStats,
    *,
    radar_config: DeviceConfig,
    stream_loss_limit_percent: float,
    sample_cycles: int,
    sample_duration_s: float,
) -> dict[str, object]:
    """合并实测值、频率推导、指标说明与本次测试配置，供 attach_metrics 写入 JSON/HTML。"""
    freq = _stream_frequency_metrics(stats, radar_config)
    cycles_ok, duration_ok, window_s = _sample_stop_conditions_met(stats, radar_config)
    return {
        **stats.to_dict(),
        **freq,
        "sample_cycles_met": cycles_ok,
        "sample_duration_met": duration_ok,
        "metric_explanations": STREAM_SAMPLE_METRIC_EXPLANATIONS,
        "stream_loss_limit_percent_applied": stream_loss_limit_percent,
        "sample_cycles_applied": sample_cycles,
        "sample_duration_s_applied": sample_duration_s,
    }


def _print_stream_sample_legend(
    report_row: dict[str, object], *, loss_limit: float, sample_cycles: int, sample_duration_s: float
) -> None:
    """在 Pytest 控制台打印指标值 + 简要说明（需 `pytest -s` 才可见标准输出）。"""
    lines = [
        "",
        "========== 连续取数短测指标（test_stream_sample_quality）==========",
    ]
    for key, tip in STREAM_SAMPLE_METRIC_EXPLANATIONS.items():
        val = report_row.get(key, "")
        lines.append(f"  {key} = {val!r}")
        lines.append(f"      → {tip}")
    lines.append(f"  [准入] loss_rate_percent 应 ≤ {loss_limit}%（h1/c2 见 thresholds）")
    lines.append(
        f"  [准入] 阵停：sample_cycles_met 或 sample_duration_met 至少其一为 true"
        f"（见指标列）"
    )
    lines.append(
        f"  [准入] 扫描频率相对误差 ≤ {report_row.get('frame_rate_tolerance_percent_applied')}%"
        f"（期望 {report_row.get('expected_scan_rate_hz')} Hz）"
    )
    lines.append(
        f"  [本测] sample_cycles={sample_cycles}、sample_duration_s={sample_duration_s}，先满足其一即停"
    )
    lines.append("================================================================")
    lines.append("")
    print("\n".join(lines))


@pytest.mark.integration
def test_stream_sample_quality(radar_client, radar_config, request: pytest.FixtureRequest) -> None:
    """短时间连续取数：帧/缺包统计 + 扫描频率推算与期望对比（expected_frame_rate_hz、容差）。"""
    try:
        radar_client.connect()
        radar_client.start_streaming()
        stats = radar_client.read_stream_stats(
            duration_s=float(radar_config.stream.sample_duration_s),
            max_cycles=int(radar_config.stream.sample_cycles),
        )
        radar_client.stop_streaming()
    except RadarClientError as exc:
        pytest.fail(f"连续取数失败: {exc}")

    metrics = _stream_stats_report_payload(
        stats,
        radar_config=radar_config,
        stream_loss_limit_percent=float(radar_config.stream_loss_limit_percent),
        sample_cycles=int(radar_config.stream.sample_cycles),
        sample_duration_s=float(radar_config.stream.sample_duration_s),
    )
    row_for_print = {k: v for k, v in metrics.items() if k != "metric_explanations"}
    attach_metrics(request.node, metrics)
    _print_stream_sample_legend(
        row_for_print,
        loss_limit=float(radar_config.stream_loss_limit_percent),
        sample_cycles=int(radar_config.stream.sample_cycles),
        sample_duration_s=float(radar_config.stream.sample_duration_s),
    )

    assert stats.frames_received > 0, "未收到连续取数数据帧"
    assert stats.parse_errors == 0, f"存在 {stats.parse_errors} 个解析失败帧"
    cycles_ok, duration_ok, window_s = _sample_stop_conditions_met(stats, radar_config)
    assert cycles_ok or duration_ok, (
        f"连续取数异常提前结束：既未收满 {radar_config.stream.sample_cycles} 圈完整数据"
        f"（completed_scans={stats.completed_scans}），也未跑满 "
        f"{radar_config.stream.sample_duration_s}s 时长上限（window_duration_s={window_s:.3f}s）"
    )
    assert stats.loss_rate_percent <= radar_config.stream_loss_limit_percent, (
        f"缺包率 {stats.loss_rate_percent}% 超过阈值 {radar_config.stream_loss_limit_percent}%"
    )
    exp_hz = float(radar_config.stream.expected_frame_rate_hz)
    if exp_hz > 0:
        err = float(row_for_print.get("scan_rate_vs_expected_error_percent", 0.0))
        tol = float(radar_config.thresholds.frame_rate_tolerance_percent)
        assert err <= tol, (
            f"扫描频率相对误差 {err}% 超过容差 {tol}% "
            f"（implied_scan_rate_hz={row_for_print['implied_scan_rate_hz']}, 期望 {exp_hz} Hz）"
        )
