# 技术规格

## 技术栈

- Python 3.12。
- Pytest 组织自动化用例。
- 标准库 `socket` 负责 TCP 通信。
- 标准库 `json`、`csv` 和手写 HTML 负责报告输出，第一版不额外引入报告模板依赖。

## 核心目录

- `network_test/automation/config.py`：设备配置、命令配置、阈值配置。
- `network_test/automation/clients/`：C2/H1 TCP 客户端。
- `network_test/automation/metrics.py`：连续取数统计。
- `network_test/automation/reports.py`：JSON、CSV、HTML 报告。
- `network_test/automation/runner.py`：冒烟测试、长稳测试和 Pytest 启动入口。
- `network_test/automation/tests/`：真实设备 Pytest 用例。

## 协议约定

- H1：依据 H1E0-02A 说明书第 4 章、表 4-2、4.2.1、4.2.22、4.2.23。
- C2：第一版依据 `c2_h1/压力测试/C200压力测试(连续取数+通断网).py` 中已使用命令。
- 默认端口：2111/TCP。
- 文本帧拆包：起始 `02 02 02 02`，随后 2 字节大端文本长度。

## 报告格式

- JSON 保留完整结构化指标。
- CSV 便于 Excel 查看用例结果。
- HTML 便于现场交付和非开发人员阅读。
