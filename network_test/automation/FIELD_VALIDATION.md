# C2/H1 自动化测试现场验证闭环

本文档用于指导测试人员把自动化脚本跑成一轮可交付、可复查的现场验证结果。它不是替代测试方案，而是把“准备、执行、记录、判定、归档”串成闭环。

## 1. 验证前检查

执行前请确认以下项目：

- 被测设备型号：C2 或 H1。项目中历史 H2 命名统一按 H1 协议处理。
- 测试电脑与雷达在同一网段，能 ping 通雷达 IP。
- H1 当前示例配置地址为 `192.168.1.86`，如现场设备不同需先修改 `host`。
- 雷达供电稳定，网线、交换机端口和测试电脑网卡状态正常。
- 已复制并修改配置文件，例如 `configs/h1.example.json` 或 `configs/c2.example.json`。
- 配置文件支持 JSONC 注释，字段说明可保留，不需要删除。
- 已确认 `host`、`port`、连续取数命令、停止取数命令和阈值符合当前样机固件。
- 如要做拔网线、网络损伤、交换机重启或电源扰动，已准备人工事件记录文件。

## 2. 推荐执行顺序

建议按风险从低到高执行：

1. HW-03 ping 短测：确认基础网络成功率和 RTT 统计可用。
2. 冒烟测试：确认 IP、端口、H1 登录和基础协议查询。
3. Pytest 核心用例：执行连接、协议查询、连续取数、主动重连。
4. 短长稳：先跑 30 分钟到 2 小时，观察脚本和设备是否稳定。
5. 人工扰动：在长稳过程中执行拔网线、恢复网线、交换机重启、网络损伤等动作。
6. 正式测试：按项目要求执行 7 天 ping，以及 8 小时、24 小时或 72 小时长稳。

## 3. 命令示例

H1 冒烟测试：

```bash
uv run python -m network_test.automation.runner --config network_test/automation/configs/h1.example.json --mode smoke
```

H1 HW-03 正式 7 天 ping：

```bash
uv run python -m network_test.automation.runner --config network_test/automation/configs/h1.example.json --mode ping
```

C2 Pytest 核心用例：

```bash
uv run pytest network_test/automation/tests --radar-config network_test/automation/configs/c2.example.json
```

C2 8 小时长稳：

```bash
uv run python -m network_test.automation.runner --config network_test/automation/configs/c2.example.json --mode stability --duration-s 28800 --window-s 60 --event-log network_test/automation/manual_events.txt
```

## 4. 人工事件记录模板

建议把人工动作记录为纯文本，每行一个事件：

```text
2026-05-14 15:00:00 开始长稳测试
2026-05-14 15:10:00 拔掉雷达网线
2026-05-14 15:10:08 恢复雷达网线
2026-05-14 15:30:00 注入 1% 丢包 + 100ms 延迟
2026-05-14 16:00:00 重启交换机
2026-05-14 16:00:06 交换机恢复
```

runner 会把事件文件内容写入 JSON/HTML 报告的 `summary.manual_events` 中。测试后如果出现数据中断、恢复超时或缺包率上升，应优先对齐这些人工事件时间点。

## 5. 报告判读

每次执行会输出三类报告：

- JSON：完整结构化结果，适合归档和二次分析。
- CSV：用例级明细，适合用 Excel 过滤失败项。
- HTML：现场审核最直观，优先打开查看。

HTML 中重点看：

- 总结果：`PASS` 或 `FAIL`。
- 结果计数：通过、失败、跳过各有多少项。
- 失败分类：`connectivity`、`protocol`、`data_quality`、`data_loss`、`recovery` 等。
- HW-03 指标：`success_rate_percent`、`rtt_avg_ms`、`rtt_max_ms`、`jitter_ms`。
- 汇总指标：总帧数、失败窗口数、最大缺包率、最大数据间隔、恢复事件和人工事件。
- 用例结果：每个窗口或每条 Pytest 用例的详细指标。

## 6. 通过条件建议

第一版建议按以下条件判定：

- 冒烟测试全部 PASS。
- Pytest 核心用例全部 PASS。
- HW-03 ping 成功率不低于 99.9%，平均 RTT 小于 2ms。
- 长稳期间程序无崩溃。
- H1 缺包率不超过 0.5%，C2 缺包率不超过 1.0%。
- H1 断线恢复不超过 5 秒，C2 断线恢复不超过 10 秒。
- 报告中不存在 `protocol_parse`、`data_loss`、`recovery` 分类失败。

如果用于量产准入，可把缺包率阈值进一步收紧为接近 0，并延长长稳时间。

## 7. 归档清单

一轮现场验证结束后，建议归档：

- 使用的配置文件。
- JSON、CSV、HTML 报告。
- 人工事件记录文件。
- 如有失败，附 Wireshark 抓包、交换机端口统计、测试现场照片或视频。
- 失败原因分析与复测结论。
