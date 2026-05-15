# C2/H1 测试实例实现明细、实现过程与测试说明

本文档从《单线激光雷达（C2/H1）网络通信测试方案》第 4 章“详细测试用例”拆分而来，专门说明当前自动化代码如何落地测试实例、每个实例的简单实现过程，以及现场人员如何执行测试。

## 1. 当前测试实例覆盖关系

| 测试实例 | 对应方案用例 | 当前实现文件 | 自动化程度 | 核心判定 |
|---|---|---|---|---|
| HW-03 持续 ping 与 RTT 统计 | HW-03 | `network_test/automation/ping.py`、`network_test/automation/tests/test_connectivity.py`、`network_test/automation/runner.py` | 全自动 | 统计发送数、接收数、成功率、RTT 最小/平均/最大和抖动 |
| TCP 连接与 H1 登录 | PROT-02 | `network_test/automation/tests/test_connectivity.py` | 全自动 | TCP 能连接；H1 能完成登录；连接耗时记录到报告 |
| 基础协议查询 | PROT-01/02 | `network_test/automation/tests/test_protocol.py`、`clients/h1_client.py` | 全自动 | C2：0x1A 配置读有应答；H1：读 IP/网关/掩码/转速角分辨率并可解析 |
| 连续取数质量统计 | PROT-02、NET-01、REL-01 | `network_test/automation/tests/test_stream.py` | 全自动 | 能收到数据帧；解析错误为 0；缺包率不超过配置阈值 |
| 应用层主动重连 | NET-02 | `network_test/automation/tests/test_recovery.py` | 全自动 | 主动断开后重新连接耗时不超过 C2 10 秒、H1 5 秒 |
| 长稳窗口测试 | REL-01、NET-02、SYS-01 | `network_test/automation/runner.py` | 半自动 | 每个窗口输出帧数、圈数、缺包率、最大数据间隔和失败原因 |
| 人工事件对齐记录 | HW-01、HW-02、NET-02、NET-03、SYS-01 | `network_test/automation/FIELD_VALIDATION.md` | 半自动 | 记录拔网线、电源扰动、网络损伤等时间点，报告保留事件内容 |

## 2. 配置文件说明

配置文件位于 `network_test/automation/configs/`。当前示例文件使用 JSONC 写法，也就是普通 JSON 加 `//` 注释。程序会自动去掉注释后解析。

H1 当前可连接地址已配置为：

```json
{
  "model": "h1",
  "host": "192.168.1.86",
  "port": 2111
}
```

正式 HW-03 持续 ping 默认时长为 604800 秒，即 7 天：

```json
{
  "ping": {
    "duration_s": 604800.0,
    "pytest_duration_s": 10.0,
    "interval_s": 1.0,
    "timeout_ms": 1000,
    "packet_size": 32
  }
}
```

其中 `duration_s` 用于正式 runner 测试，`pytest_duration_s` 只用于快速回归，避免每次 Pytest 都阻塞 7 天。

## 3. 测试实例一：HW-03 持续 ping 与 RTT 统计

**测试目标：** 对齐方案 HW-03：配置雷达与测试机在同一子网，持续 ping，记录成功率、平均延迟和最大延迟。正式准入建议执行 7 天。

**简单实现过程：**

1. 读取配置中的 `host`、`ping.duration_s`、`ping.interval_s`、`ping.timeout_ms`、`ping.packet_size`。
2. Python 按固定间隔调用系统 `ping` 命令。
3. 解析 Windows 中文输出中的 `时间=1ms` 或英文输出中的 `time=1ms`。
4. 统计发送数、接收数、丢失数、成功率、RTT 最小值、RTT 平均值、RTT 最大值和抖动。
5. 与方案阈值比较：成功率不低于 99.9%，局域网平均 RTT 建议小于 2ms。

**快速样本测试：**

```bash
uv run pytest network_test/automation/tests/test_connectivity.py --radar-config network_test/automation/configs/h1.example.json
```

**正式 7 天测试：**

```bash
uv run python -m network_test.automation.runner --config network_test/automation/configs/h1.example.json --mode ping --duration-s 604800
```

**通过条件：**

- ping 成功率不低于配置中的 `ping_success_rate_min_percent`，默认 99.9%。
- 平均 RTT 小于 2ms。
- 报告中能看到 `rtt_avg_ms`、`rtt_max_ms`、`success_rate_percent`。

### 3.1 HW-03 报告字段解释

HW-03 报告中的 `stats` 字段可以按下面方式理解：

| 字段 | 含义 | 怎么看 |
|---|---|---|
| `host` | 被测雷达 IP 地址 | 确认是不是当前要测的设备，例如 `192.168.1.86` |
| `planned_duration_s` | 计划测试时长，单位秒 | 10 表示短测 10 秒；604800 表示 7 天 |
| `interval_s` | 两次 ping 之间的间隔，单位秒 | 1.0 表示约每秒 ping 一次 |
| `timeout_ms` | 单次 ping 等待应答的最长时间，单位毫秒 | 超过该时间还没回包，就算这次 ping 丢包 |
| `packet_size` | ping 负载大小，单位字节 | 用于固定每个 ICMP 包的数据长度 |
| `sent` | 已发送 ping 包数量 | 数量越大，统计越可信；7 天测试会非常大 |
| `received` | 已收到应答的 ping 包数量 | 应尽量接近 `sent` |
| `lost` | 未收到应答的 ping 包数量 | 理想情况为 0 |
| `success_rate_percent` | ping 成功率 | 默认要求不低于 99.9%；100 表示本次无丢包 |
| `rtt_min_ms` | 最小 RTT，单位毫秒 | 最快一次网络往返耗时 |
| `rtt_avg_ms` | 平均 RTT，单位毫秒 | 局域网建议小于 2ms，是最重要的延迟指标 |
| `rtt_max_ms` | 最大 RTT，单位毫秒 | 用于观察偶发卡顿；如果远大于平均值，需要看交换机、网卡或网络干扰 |
| `jitter_ms` | RTT 抖动，单位毫秒 | 越小越稳定；0 表示本次每次 RTT 基本一致 |
| `started_at_s` | 程序内部单调时钟开始时间 | 不是北京时间，只用于计算持续时长 |
| `ended_at_s` | 程序内部单调时钟结束时间 | `ended_at_s - started_at_s` 约等于实际测试耗时 |
| `errors` | 脚本执行系统 ping 命令时的异常 | 空列表表示脚本执行层面没有异常 |

以 10 秒短测结果为例：

```json
{
  "sent": 113,
  "received": 113,
  "lost": 0,
  "success_rate_percent": 100.0,
  "rtt_avg_ms": 1.0,
  "rtt_max_ms": 1.0,
  "jitter_ms": 0.0
}
```

这表示本次短测发出 113 个 ping 包，113 个都有应答，没有丢包；平均 RTT 为 1ms，满足小于 2ms 的建议标准；最大 RTT 也是 1ms，抖动为 0，说明这段时间网络非常稳定。

## 4. 测试实例二：TCP 连接与 H1 登录

**测试目标：** 验证应用层协议入口是否可用。该测试不替代 HW-03 ping，而是 PROT-02 前置检查。

**简单实现过程：**

1. 读取配置中的 `host`、`port`、`connect_timeout_s`。
2. 创建 TCP socket，连接雷达默认端口 2111。
3. 如果型号为 H1，则按 H1E0-02A 说明书第 4 章表 4-2 和 4.2.1 组登录帧。
4. 检查 H1 登录应答中是否包含成功特征 `12 01 01`。
5. 把连接耗时、型号、IP、端口写入报告。

**如何测试：**

```bash
uv run pytest network_test/automation/tests/test_connectivity.py --radar-config network_test/automation/configs/h1.example.json
uv run pytest network_test/automation/tests/test_connectivity.py --radar-config network_test/automation/configs/c2.example.json
```

**通过条件：**

- C2：TCP 连接成功。
- H1：TCP 连接成功，并且登录应答判断成功。
- 失败时报告中应能看到连接超时、拒绝连接、登录失败等明确原因。

## 5. 测试实例三：基础协议查询

**测试目标：** 验证 PROT-02 中“发送标准指令并验证响应格式”的基础能力。

**简单实现过程：**

1. C2 发送配置查询命令 `02 02 02 02 00 09 00 1A 2B`。
2. H1 登录后依次读 IP（0x10）、网关（0x12）、子网掩码（0x14）、转速/角分辨率（0x1A），见 `clients/h1_client.py`。
3. 原始应答在 `clients/h1_param_parse.py` 中按表 4-2 应答操作码 `0x12` 切帧并解析。
4. 报告写入 `ip`、`gateway`、`subnet_mask`、`spin_hz`、`angle_resolution_deg` 及 `raw_lengths`。

**如何测试：**

```bash
uv run pytest network_test/automation/tests/test_protocol.py --radar-config network_test/automation/configs/h1.example.json
uv run pytest network_test/automation/tests/test_protocol.py --radar-config network_test/automation/configs/c2.example.json
```

**通过条件：**

- C2：配置读有非空应答。
- H1：至少一条读命令有原始应答，且至少解析出一项（如 IP 或角分辨率）。
- 点云活性（4.2.22）单独用 `H1RadarClient.probe_protocol()`，不替代参数读。
- 若无应答，检查 IP、端口、上电、网段、H1 登录密码。

## 6. 测试实例四：连续取数质量统计

**测试目标：** 验证 PROT-02 的数据格式与数据连续性，同时作为 NET-01、REL-01 的基础统计能力。

**简单实现过程：**

1. 连接雷达并完成必要初始化。
2. 发送启动连续取数命令 `02 02 02 02 00 0A 02 31 01 46`。
3. 按 `02 02 02 02 + 2 字节大端长度` 从 TCP 字节流中切出完整帧。
4. H1 优先使用 `libs.protocols.h2_txt_parse.parse_h2_pointcloud_frame` 解析点云帧。
5. C2 第一版使用现有 C200 脚本里的圈号和包号字段位置做连续性统计。
6. 按圈号、包号统计已收帧数、已见圈数、完整圈数、缺包率、解析错误数、最大数据间隔。
7. 发送停止连续取数命令 `02 02 02 02 00 0A 02 31 00 45`。

**如何测试：**

```bash
uv run pytest network_test/automation/tests/test_stream.py --radar-config network_test/automation/configs/h1.example.json
uv run pytest network_test/automation/tests/test_stream.py --radar-config network_test/automation/configs/c2.example.json
```

**通过条件：**

- 能收到连续数据帧。
- 解析错误数为 0。
- H1 缺包率不超过 0.5%。
- C2 缺包率不超过 1.0%。

## 7. 测试实例五：应用层主动重连

**测试目标：** 验证 NET-02 中异常恢复能力的第一层：上位机主动断开后重新连接是否快速恢复。该测试不替代真实拔网线和交换机重启测试，但可以作为自动化回归用例。

**简单实现过程：**

1. 第一次创建客户端并连接雷达。
2. 主动关闭 socket，模拟上位机连接断开。
3. 立即创建第二个客户端重新连接。
4. 记录第二次连接耗时。
5. 与型号阈值比较：C2 不超过 10 秒，H1 不超过 5 秒。

**如何测试：**

```bash
uv run pytest network_test/automation/tests/test_recovery.py --radar-config network_test/automation/configs/h1.example.json
uv run pytest network_test/automation/tests/test_recovery.py --radar-config network_test/automation/configs/c2.example.json
```

**通过条件：**

- 应用层主动重连耗时小于对应型号阈值。
- 如果失败，优先排查设备是否只允许单连接、上一次连接是否未释放、雷达固件是否需要更长会话清理时间。

## 8. 测试实例六：长稳与人工事件记录

**测试目标：** 覆盖 REL-01 长期稳定性，并为 HW-01、HW-02、NET-02、NET-03、SYS-01 这类需要人工动作或外部设备的测试提供统一记录方式。

**简单实现过程：**

1. 使用 runner 按固定窗口运行，例如每 60 秒作为一个窗口。
2. 每个窗口内连接雷达、启动连续取数、统计数据质量、停止取数并关闭连接。
3. 窗口之间如果出现连接失败或数据中断，报告记录失败原因。
4. 测试人员在外部文本文件中记录人工事件，例如拔网线、恢复网线、交换机重启、电源纹波、网络损伤参数。
5. runner 把事件记录内容写入 HTML/JSON 报告，测试后人工对齐异常时间点。

**如何测试：**

```bash
uv run python -m network_test.automation.runner --config network_test/automation/configs/h1.example.json --mode stability --duration-s 28800 --window-s 60 --event-log 手工事件记录.txt
uv run python -m network_test.automation.runner --config network_test/automation/configs/c2.example.json --mode stability --duration-s 28800 --window-s 60 --event-log 手工事件记录.txt
```

**通过条件：**

- 长稳期间无程序崩溃。
- 每个窗口尽量保持有数据帧输出。
- 人工断网恢复后，C2 应在 10 秒内恢复，H1 应在 5 秒内恢复。
- 数据缺包率、最大数据间隔、重连次数应满足第 2 章准入标准。

## 9. 报告输出与结果判读

执行 Pytest 或 runner 后，会在配置文件中的 `report_dir` 目录生成三类报告：

| 报告类型 | 用途 | 重点查看内容 |
|---|---|---|
| JSON | 机器可读，便于后续平台接入 | 每个用例的完整指标、失败原因、原始统计字段 |
| CSV | 便于 Excel 筛选 | 用例名称、PASS/FAIL、耗时、失败信息 |
| HTML | 现场交付和人工审核 | 总结果、设备信息、每条用例结论、关键指标摘要 |

结果判读建议：

1. 先看 HTML 总结果是否 PASS。
2. 若 FAIL，先看失败分类：`connectivity`、`protocol`、`data_quality`、`data_loss`、`recovery`。
3. 对 HW-03，重点看 `success_rate_percent`、`rtt_avg_ms`、`rtt_max_ms`。
4. 对连续取数，重点看 `frames_received`、`parse_errors`、`loss_rate_percent`、`max_inter_frame_gap_s`。
5. 如果失败发生在人工事件之后，应对照 `event-log` 文件确认是否属于预期扰动。

## 10. 现场执行顺序建议

建议按风险从低到高执行：

1. 只接一台雷达，先运行 HW-03 ping 短样本。
2. 运行 TCP 连接与基础协议查询。
3. 运行 5 圈或 10 秒连续取数短测。
4. 运行应用层主动重连测试。
5. 运行 30 分钟到 2 小时短长稳。
6. 加入人工拔网线、交换机重启、IP 冲突、背景流量等扰动。
7. 最后再执行 7 天 ping，以及 8 小时、24 小时、72 小时长稳。

高风险测试（协议模糊测试、广播风暴、固件升级压力循环）不建议直接在量产样机或客户现场执行，应先在可恢复的实验设备上验证。

## 11. 如何添加新的 H1 指令测试

后续要在自动化里增加「读/写某参数」或新冒烟项，按下面四步做即可（不必改 runner 核心逻辑）。

### 步骤 1：在配置文件增加命令 Hex

编辑 `network_test/automation/configs/h1.example.json` 的 `commands` 段，增加字段，例如：

```json
"read_version_hex": "02 02 02 02 00 09 02 0C 1F"
```

同时在 `network_test/automation/config.py` 的 `CommandConfig` 里增加同名默认值（否则 `from_dict` 会忽略未知键）。Hex 来源：说明书 4.2.x 或现场工具（如 `c2_h1/C225指令测试.py`）。

### 步骤 2：在客户端封装发送（可选）

- **只测「有应答」**：在测试里直接  
  `radar_client.read_parameter("read_version_hex")`  
  （`H1RadarClient` 已提供，需先 `connect()` 完成登录）。
- **需要专用方法**：在 `h1_client.py` 增加  
  `def read_version(self) -> bytes: return self.read_parameter("read_version_hex")`  
  无参数读也可用 `build_h1_read_frame(0x0C)` 组帧，但推荐配置化以便现场改 Hex 不重打包。

写命令（操作码 `0x01`）需按说明书拼参数并用 `checksum8` 算校验和，可参考 C225 的 `set_ip` / `set_freq`；建议单独 `build_h1_write_frame` 或配置完整写帧 Hex。

### 步骤 3：解析应答（需要断言数值时）

在 `clients/h1_param_parse.py` 增加解析函数，例如：

```python
def parse_version_string(raw: bytes) -> str | None:
    frame = find_reply_frame(raw, 0x0C)
    ...
```

若多条读命令要一起上报，可扩展 `H1NetworkParams` 字段，或在 `read_network_params()` 里追加调用。

### 步骤 4：增加 Pytest 用例

在 `tests/` 下新建或扩展 `test_protocol.py`，例如：

```python
@pytest.mark.integration
def test_h1_read_version(radar_client, radar_config):
    if radar_config.normalized_model != "h1":
        pytest.skip("仅 H1")
    radar_client.connect()
    raw = radar_client.read_parameter("read_version_hex")
    assert raw
```

用 `attach_metrics(request.node, {...})` 把解析结果写入 JSON 报告。运行：

```bash
uv run pytest network_test/automation/tests/test_protocol.py -k version --radar-config network_test/automation/configs/h1.example.json
```

### 冒烟 runner 是否自动包含？

`runner --mode smoke` 的 `protocol_query` 步骤目前只调用 `read_network_params()`。新指令若需进冒烟，在 `read_network_params()` 中增加读取与解析，或单独加 `runner` 的 case 名称。

### 相关文件速查

| 文件 | 作用 |
|---|---|
| `config.py` / `configs/h1.example.json` | 命令 Hex 与阈值 |
| `clients/h1_client.py` | 登录、发读命令、`read_network_params()` |
| `clients/h1_param_parse.py` | 应答切帧与字段解析 |
| `tests/test_protocol.py` | PROT 类 Pytest 用例 |
| `runner.py` | 现场一键冒烟 |
