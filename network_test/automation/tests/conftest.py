"""
Pytest 公共夹具和报告钩子。

硬件测试必须显式传入 `--radar-config`，没有真实设备时用例会跳过，保证开发电脑仍可
运行离线自检。测试结束后自动输出 JSON/CSV/HTML 报告。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys
from typing import Any

import pytest

# 当用户直接运行 `pytest network_test/automation/tests` 时，Pytest 可能只把测试目录
# 放入 `sys.path`。这里显式加入仓库根目录，保证 `network_test.automation` 可导入。
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from network_test.automation.clients import create_radar_client
from network_test.automation.config import DeviceConfig, load_device_config
from network_test.automation.reports import CaseResult, ReportWriter, RunReport


def pytest_addoption(parser: pytest.Parser) -> None:
    """注册雷达自动化测试专用命令行参数。"""
    parser.addoption("--radar-config", action="store", default=None, help="C2/H1 设备 JSON 配置文件")
    parser.addoption(
        "--radar-report-dir",
        action="store",
        default=None,
        help="报告输出目录；不传时使用配置文件中的 report_dir",
    )


def pytest_configure(config: pytest.Config) -> None:
    """初始化报告缓存，并声明 integration 标记避免 Pytest 警告。"""
    config.addinivalue_line("markers", "integration: 需要真实 C2/H1 雷达设备的网络测试")
    config._c2h1_case_results = []  # type: ignore[attr-defined]
    config._c2h1_started_at = datetime.now().isoformat(timespec="seconds")  # type: ignore[attr-defined]


@pytest.fixture(scope="session")
def radar_config(pytestconfig: pytest.Config) -> DeviceConfig:
    """读取设备配置；没有配置时跳过硬件测试。"""
    config_path = pytestconfig.getoption("--radar-config")
    if not config_path:
        pytest.skip("未传入 --radar-config，跳过真实雷达网络测试")
    return load_device_config(config_path)


@pytest.fixture()
def radar_client(radar_config: DeviceConfig):
    """为每条用例创建独立客户端，避免半帧缓存或断线状态影响后续用例。"""
    client = create_radar_client(radar_config)
    try:
        yield client
    finally:
        client.close()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]):
    """收集每条用例 call 阶段结果，生成测试报告使用。"""
    outcome = yield
    report = outcome.get_result()
    if report.when != "call":
        return

    message = ""
    if report.failed:
        message = str(call.excinfo.value) if call.excinfo else "用例失败"
    elif report.skipped:
        message = "用例跳过"

    metrics = getattr(item, "_c2h1_metrics", {})
    item.config._c2h1_case_results.append(  # type: ignore[attr-defined]
        CaseResult(
            name=item.nodeid,
            outcome=report.outcome,
            duration_s=float(report.duration),
            message=message,
            metrics=metrics,
        )
    )


def attach_metrics(item: pytest.Item, metrics: dict[str, Any]) -> None:
    """测试用例调用该函数把协议统计指标附加到最终报告。"""
    item._c2h1_metrics = metrics  # type: ignore[attr-defined]


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """测试会话结束时生成报告；无配置的离线跳过场景不强制创建硬件报告。"""
    config_path = session.config.getoption("--radar-config")
    if not config_path:
        return

    radar_config = load_device_config(config_path)
    output_dir = session.config.getoption("--radar-report-dir") or radar_config.report_dir
    run_report = RunReport(
        title="C2/H1 Pytest 网络通信自动化测试",
        device_name=radar_config.name,
        model=radar_config.normalized_model,
        host=radar_config.host,
        started_at=session.config._c2h1_started_at,  # type: ignore[attr-defined]
        cases=session.config._c2h1_case_results,  # type: ignore[attr-defined]
        notes=[
            "报告由 Pytest 钩子自动生成。",
            f"pytest exitstatus={exitstatus}",
            f"生成时间={datetime.now().isoformat(timespec='seconds')}",
        ],
    )
    paths = ReportWriter(Path(output_dir)).write_all(run_report, prefix="pytest")
    session.config._c2h1_report_paths = paths  # type: ignore[attr-defined]


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter, exitstatus: int, config: pytest.Config) -> None:
    """在终端摘要中打印报告路径，方便测试人员快速打开 HTML。"""
    paths = getattr(config, "_c2h1_report_paths", None)
    if not paths:
        return
    terminalreporter.write_sep("-", "C2/H1 自动化报告")
    for kind, path in paths.items():
        terminalreporter.write_line(f"{kind}: {path}")
