# C2/H1 网络通信自动化测试工具

本目录用于把《C2/H1 网络通信测试方案》落地为可执行的命令行自动化工具。第一版重点覆盖：

- TCP 基础连通和 H1 登录。
- HW-03 持续 ping、成功率和 RTT 统计。
- H1 单次数据请求、C2 配置查询。
- C2/H1 连续取数、帧解析、缺包率、解析错误统计。
- 应用层主动重连耗时验证。
- 长稳 runner、人工事件记录提示、JSON/CSV/HTML 报告。

## 使用前准备

1. 将测试电脑和雷达配置到同一网段。
2. 复制 `configs/h1.example.json` 或 `configs/c2.example.json`，修改 `host`、`port`、阈值和输出目录。
3. 配置文件支持 JSONC 写法，字段前的 `//` 中文注释会被加载器自动忽略。
4. 确认 H1/H2 命名按 H1E0-02A 协议处理；ROS 本轮暂不纳入。
5. 当前 H1 示例配置默认地址为 `192.168.1.86`。

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

HW-03 正式 ping/RTT 测试，默认使用配置中的 `ping.duration_s=604800`，也就是 7 天：

```bash
uv run python -m network_test.automation.runner --config network_test/automation/configs/h1.example.json --mode ping
```

如需先做 60 秒短测，可临时覆盖时长：

```bash
uv run python -m network_test.automation.runner --config network_test/automation/configs/h1.example.json --mode ping --duration-s 60
```

冒烟测试：

```bash
uv run python -m network_test.automation.runner --config network_test/automation/configs/h1.example.json --mode smoke
```

长稳测试示例：

```bash
uv run python -m network_test.automation.runner --config network_test/automation/configs/c2.example.json --mode stability --duration-s 28800 --window-s 60 --event-log 手工事件记录.txt
```

长稳 runner 会按窗口输出统计，并在报告汇总中给出：

- 总窗口数和失败窗口数。
- 总接收帧数、解析错误数、最大缺包率、最大数据间隔。
- 人工事件记录内容。
- 断线到恢复的估算耗时，以及是否超过 C2/H1 恢复阈值。

## 判定逻辑

- H1 默认恢复阈值：5 秒。
- C2 默认恢复阈值：10 秒。
- HW-03 ping 成功率默认不低于 99.9%。
- HW-03 局域网平均 RTT 建议小于 2ms。
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

## 现场验证闭环

完整现场执行建议见 `FIELD_VALIDATION.md`，测试实例实现明细见 `TEST_INSTANCE_IMPLEMENTATION.md`。推荐闭环为：

1. 先跑 HW-03 ping 短测确认基础网络和 RTT。
2. 再跑冒烟测试确认 IP、端口、协议命令。
3. 再跑 Pytest 核心用例确认连接、协议、取数、主动重连。
4. 然后跑短长稳，确认测试电脑和雷达可稳定运行。
5. 最后在长稳中加入拔网线、网络损伤、交换机重启等人工事件。
6. 归档配置文件、JSON/CSV/HTML 报告、人工事件记录和必要抓包。
