# C2/H1 网络通信自动化测试工具

本目录用于把《C2/H1 网络通信测试方案》落地为可执行的命令行自动化工具。第一版重点覆盖：

- TCP 基础连通和 H1 登录。
- H1 单次数据请求、C2 配置查询。
- C2/H1 连续取数、帧解析、缺包率、解析错误统计。
- 应用层主动重连耗时验证。
- 长稳 runner、人工事件记录提示、JSON/CSV/HTML 报告。

## 使用前准备

1. 将测试电脑和雷达配置到同一网段。
2. 复制 `configs/h1.example.json` 或 `configs/c2.example.json`，修改 `host`、`port`、阈值和输出目录。
3. 确认 H1/H2 命名按 H1E0-02A 协议处理；ROS 本轮暂不纳入。

## Pytest 自动化

```bash
uv run pytest network_test/automation/tests --radar-config network_test/automation/configs/h1.example.json
uv run pytest network_test/automation/tests --radar-config network_test/automation/configs/c2.example.json
```

执行后会在配置中的 `report_dir` 下生成：

- `pytest_*.json`：完整结构化结果。
- `pytest_*.csv`：用例级明细。
- `pytest_*.html`：现场可读摘要报告。

## 命令行 runner

冒烟测试：

```bash
uv run python -m network_test.automation.runner --config network_test/automation/configs/h1.example.json --mode smoke
```

长稳测试示例：

```bash
uv run python -m network_test.automation.runner --config network_test/automation/configs/c2.example.json --mode stability --duration-s 28800 --window-s 60 --event-log 手工事件记录.txt
```

## 判定逻辑

- H1 默认恢复阈值：5 秒。
- C2 默认恢复阈值：10 秒。
- H1 默认缺包率阈值：0.5%。
- C2 默认缺包率阈值：1.0%。
- 协议查询必须有非空应答。
- 连续取数必须收到数据帧且无解析错误。

## 现场事件记录建议

网络损伤仪、拔网线、交换机重启、IP 冲突、电源扰动等动作建议人工记录时间点，例如：

```text
2026-05-14 15:00:00 开始长稳
2026-05-14 15:10:00 拔掉雷达网线
2026-05-14 15:10:08 恢复雷达网线
2026-05-14 15:30:00 注入 1% 丢包 + 100ms 延迟
```

工具会把事件记录文件路径写入 HTML 报告，便于后续对齐异常窗口。
