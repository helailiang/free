"""
自动化测试报告生成。

报告分三层输出：
1. JSON：保留完整结构，便于后续平台或脚本二次分析。
2. CSV：便于测试人员用 Excel 快速筛选失败项。
3. HTML：便于现场交付时直接打开查看摘要。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import csv
from datetime import datetime
import html
import json
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class CaseResult:
    """单条 Pytest 用例或 runner 步骤的报告记录。"""

    name: str
    outcome: str
    duration_s: float
    message: str = ""
    category: str = "general"
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转换为 JSON/CSV 可处理的基础字典。"""
        return asdict(self)


@dataclass(slots=True)
class RunReport:
    """一次测试运行的完整摘要。"""

    title: str
    device_name: str
    model: str
    host: str
    started_at: str
    cases: list[CaseResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        """只要存在 failed/error 即视为整轮失败，skipped 不影响硬件未连接时的离线自检。"""
        return all(case.outcome not in {"failed", "error"} for case in self.cases)

    def outcome_counts(self) -> dict[str, int]:
        """按结果汇总数量，现场人员可先看整体 PASS/FAIL 分布。"""
        counts: dict[str, int] = {}
        for case in self.cases:
            counts[case.outcome] = counts.get(case.outcome, 0) + 1
        return counts

    def failure_categories(self) -> dict[str, int]:
        """按失败分类汇总，帮助判断问题更像链路、协议、数据质量还是恢复超时。"""
        categories: dict[str, int] = {}
        for case in self.cases:
            if case.outcome not in {"failed", "error"}:
                continue
            categories[case.category] = categories.get(case.category, 0) + 1
        return categories

    def to_dict(self) -> dict[str, Any]:
        """转换为带总结果的字典结构。"""
        return {
            "title": self.title,
            "device_name": self.device_name,
            "model": self.model,
            "host": self.host,
            "started_at": self.started_at,
            "passed": self.passed,
            "outcome_counts": self.outcome_counts(),
            "failure_categories": self.failure_categories(),
            "summary": self.summary,
            "notes": self.notes,
            "cases": [case.to_dict() for case in self.cases],
        }


class ReportWriter:
    """把 `RunReport` 输出为 JSON、CSV、HTML 三种格式。"""

    def __init__(self, output_dir: str | Path) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def write_all(self, report: RunReport, *, prefix: str = "c2h1_network_test") -> dict[str, Path]:
        """写入三种报告并返回路径，方便命令行打印或 CI 收集。"""
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = f"{prefix}_{report.model}_{stamp}"
        json_path = self.output_dir / f"{base}.json"
        csv_path = self.output_dir / f"{base}.csv"
        html_path = self.output_dir / f"{base}.html"
        self.write_json(report, json_path)
        self.write_csv(report, csv_path)
        self.write_html(report, html_path)
        return {"json": json_path, "csv": csv_path, "html": html_path}

    def write_json(self, report: RunReport, path: Path) -> None:
        """写入完整 JSON，保留指标嵌套结构。"""
        path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    def write_csv(self, report: RunReport, path: Path) -> None:
        """写入用例级 CSV，嵌套指标以 JSON 字符串放入 metrics 列。"""
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["name", "outcome", "category", "duration_s", "message", "metrics"],
            )
            writer.writeheader()
            for case in report.cases:
                writer.writerow(
                    {
                        "name": case.name,
                        "outcome": case.outcome,
                        "category": case.category,
                        "duration_s": round(case.duration_s, 4),
                        "message": case.message,
                        "metrics": json.dumps(case.metrics, ensure_ascii=False),
                    }
                )

    def write_html(self, report: RunReport, path: Path) -> None:
        """写入无外部依赖 HTML，便于离线测试电脑直接打开。"""
        rows = []
        for case in report.cases:
            rows.append(
                "<tr>"
                f"<td>{html.escape(case.name)}</td>"
                f"<td>{html.escape(case.outcome)}</td>"
                f"<td>{html.escape(case.category)}</td>"
                f"<td>{case.duration_s:.3f}</td>"
                f"<td>{html.escape(case.message)}</td>"
                f"<td><pre>{html.escape(json.dumps(case.metrics, ensure_ascii=False, indent=2))}</pre></td>"
                "</tr>"
            )

        notes = "".join(f"<li>{html.escape(note)}</li>" for note in report.notes)
        summary = html.escape(json.dumps(report.summary, ensure_ascii=False, indent=2))
        outcome_counts = html.escape(json.dumps(report.outcome_counts(), ensure_ascii=False))
        failure_categories = html.escape(json.dumps(report.failure_categories(), ensure_ascii=False))
        body = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{html.escape(report.title)}</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; line-height: 1.5; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #ccc; padding: 8px; vertical-align: top; }}
    th {{ background: #f3f3f3; }}
    pre {{ white-space: pre-wrap; margin: 0; }}
  </style>
</head>
<body>
  <h1>{html.escape(report.title)}</h1>
  <p>设备：{html.escape(report.device_name)} / {html.escape(report.model)} / {html.escape(report.host)}</p>
  <p>开始时间：{html.escape(report.started_at)}，总结果：{"PASS" if report.passed else "FAIL"}</p>
  <p>结果计数：{outcome_counts}</p>
  <p>失败分类：{failure_categories}</p>
  <h2>汇总指标</h2>
  <pre>{summary}</pre>
  <h2>备注</h2>
  <ul>{notes}</ul>
  <h2>用例结果</h2>
  <table>
    <thead><tr><th>用例</th><th>结果</th><th>分类</th><th>耗时(s)</th><th>信息</th><th>指标</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</body>
</html>
"""
        path.write_text(body, encoding="utf-8")
