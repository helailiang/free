# 用户流程与项目结构

## 用户流程

1. 测试人员确认雷达型号、IP、端口和供电网络环境。
2. 复制 `network_test/automation/configs/*.example.json` 并修改为真实设备配置。
3. 使用 Pytest 执行核心用例，或使用 runner 执行冒烟/长稳测试。
4. 打开 HTML 报告查看 PASS/FAIL、失败原因和关键指标。
5. 如有拔网线、IP 冲突、交换机重启、网络损伤等人工动作，同步记录事件时间点。

## 推荐命令

```bash
uv run pytest network_test/automation/tests --radar-config network_test/automation/configs/h1.example.json
uv run python -m network_test.automation.runner --config network_test/automation/configs/c2.example.json --mode smoke
uv run python -m network_test.automation.runner --config network_test/automation/configs/c2.example.json --mode stability --duration-s 28800 --window-s 60
```

## 项目结构

```text
network_test/automation/
├── clients/          # C2/H1 通信客户端
├── configs/          # 示例配置
├── tests/            # Pytest 用例
├── config.py         # 配置读取
├── metrics.py        # 指标统计
├── reports.py        # 报告输出
├── runner.py         # 命令行入口
└── README.md         # 使用说明
```
