# 富锐单线激光雷达 C2/C3/H1 SDK

## 1.简介

本 SDK 适用于 Ubuntu 系统，无需依赖第三方库，可实现同时连接两台富锐 C2/C3/H1 单线激光雷达，并通过 UDP 广播输出雷达相关信息（包含雷达标识、时间戳、扫描圈序号、雷达状态、点云状态等核心数据）。
核心特性
*支持两台雷达独立连接、数据解析，互不干扰；
*UDP 广播输出标准化 JSON 格式数据，易对接上层应用；
*时间戳基于 UTC（1970.1.1），精确到毫秒级；
*自动重连机制，断连后自动重试，保障稳定性；
*无需 ROS 环境，纯 C++ 原生实现，轻量化部署。

## 2.环境要求

硬件要求
运行设备：x86/ARM 架构 Ubuntu 主机（如 Ubuntu 18.04/20.04/22.04）
雷达设备：2 台富锐 C2/C3/H1单线激光雷达（以太网 / 串口连接均可）；
网络：雷达与主机需在同一局域网（以太网连接时）。
软件要求
编译工具：GCC/G++ (5.4 及以上)、CMake (3.10 及以上)；
系统依赖：net-tools、socat（可选，用于测试 UDP 消息）；
依赖安装命令：
sudo apt update && sudo apt install -y gcc g++ cmake net-tools socat

## 3.快速使用流程

### 3.1 代码部署

1)将 SDK 源码解压 / 克隆至 Ubuntu 主机，目录结构如下：
CN-sdk/
├── config/                # 雷达配置文件目录
│   ├── free_lidar_Lidar01.json  # 雷达1配置文件
│   └── free_lidar_Lidar02.json  # 雷达2配置文件
├── free_lidar/            # SDK核心头文件目录
│   ├── free_lidar_node.h  # 核心类头文件
│   └── ...（其他驱动头文件）
├── free_lidar_node.cpp    # SDK核心实现文件
└── CMakeLists.txt         # 编译配置文件
2)修改雷达配置文件（关键）：
分别编辑 config/free_lidar_Lidar01.json 和 config/free_lidar_Lidar02.json，填写对应雷达的参数：
{
 "frame_id": "Lidar01",            // 雷达标识（自定义，如Lidar01/Lidar02）
 "scanner_ip": "192.168.1.101",    // 雷达1/2的IP地址
 "scan_frequency": "15",           // 扫描频率（15Hz）
 "scan_resolution": "3333",        // 扫描分辨率(0.3333°)
 "start_angle": "-45",             // 起始角度（°）
 "stop_angle": "225",              // 终止角度（°）
 "filter_switch": "1",             // 滤波开关（1=开启，0=关闭）
 "cluster_num": "3",               // 聚类数量
 "broad_filter_num": "5",          // 滤波宽度
 "NOR_switch": "1",                // 归一化开关（h1需开启）
 "is_reverse_postion": "false"     // 点云位置反转（true/false）
}

{
  "frame_id": "scan",  
  "scanner_ip": "192.168.192.100",  
  "scan_frequency": 30,
  "scan_resolution": 1000,
  "start_angle": -45,
  "stop_angle": 225,
  "offset_angle": -90,
  "filter_switch": 1,
  "cluster_num": 3,
  "broad_filter_num": 10,
  "NOR_switch": 0,
  "is_reverse_position": false,
  "topic_name": "/scan"
}

### 3.2 编译代码

在 SDK 根目录执行以下命令编译：
cd ~/CH-sdk  # 进入SDK根目录
rm -rf build && mkdir build && cd build  # 清理/创建编译目录
cmake ..  # 生成编译配置
cmake --build . -- -j$(nproc)  # 编译（-j后接CPU核心数，加速编译）

### 3.3 运行程序

进入程序目录

cd ~/CH-sdk

运行可执行文件

./build/free_lidar_node

运行日志说明
启动成功：输出 [UDP-Lidar01] 初始化成功...、[UDP-Lidar02] 初始化成功...；
连接成功：输出 [Lidar01] Connecting to 192.168.1.101 ... OK；
数据推送：持续输出 [UDP-Lidar01] 广播推送成功，数据长度: xx bytes...，表示雷达数据已通过 UDP 广播输出。

### 3.4 验证 UDP 消息

新开终端，分别侦听两台雷达的 UDP 广播端口，验证数据输出：

侦听雷达1（广播端口52001）

socat -u UDP4-RECVFROM:52001,broadcast,reuseaddr,fork - | ts '[Lidar01 %Y-%m-%d %H:%M:%S]'

新开终端，侦听雷达2（广播端口52003）

socat -u UDP4-RECVFROM:52003,broadcast,reuseaddr,fork - | ts '[Lidar02 %Y-%m-%d %H:%M:%S]'

## 4.UDP 消息格式说明

### 4.1 消息结构

雷达输出的 UDP 消息为 JSON 格式，字段说明如下：
{
"frame_id":"scan",		//帧名称（自定义标识）
"Lidar_id":"Lidar01",		//雷达序号
"device_type":"H100",		//雷达型号
"time_stamp":14797.309,		//UTC时间戳，单位s，精确到ms
"frame_seq":48700,		//扫描圈序号，0～65535循环
"lidar_state":1,		//雷达状态，1为正常，其他为故障码
"cloud_error":0,		//点云状态，0为正常，1为异常
"angle_min":-45°,		//起始角度
"angle_max":225°,		//终止角度
"resolution":0.100000°,		//角分辨率
"frequency":30hz,		//扫描频率
"ranges":[749,751,...],		//距离数组，1=1mm，数组第一个值对应起始角度，最后一个值对应终止角度
"intensities":[749,751,...]	//反射率数组，数组第一个值对应起始角度，最后一个值对应终止角度
}

### 4.2 端口说明

| 雷达标识 | 本地绑定端口 | UDP 广播目标端口 | 侦听命令                                                |
| -------- | ------------ | ---------------- | ------------------------------------------------------- |
| Lidar01  | 52000        | 52001            | socat -u UDP4-RECVFROM:52001,broadcast,reuseaddr,fork - |
| Lidar02  | 52002        | 52003            | socat -u UDP4-RECVFROM:52003,broadcast,reuseaddr,fork - |

## 5. 常见问题排查

### 5.1 编译报错：找不到头文件

- 检查 `free_lidar/` 目录下是否存在 `free_lidar_node.h`、`lidar_driver.h` 等核心头文件；
- 检查 `CMakeLists.txt` 中是否正确包含头文件目录。

### 5.2 运行报错：连接雷达失败

- 检查雷达 IP 是否正确，主机与雷达是否能 ping 通（`ping 192.168.1.101`）；
- 以太网连接时，检查雷达端口 2111 是否开放（`telnet 192.168.1.101 2111`）；
- 串口连接时，检查串口权限（`sudo chmod 777 /dev/ttyUSB0`）。

### 5.3 无 UDP 消息输出

- 检查防火墙是否放行 UDP 端口（`sudo ufw allow 52001/udp && sudo ufw allow 52003/udp`）；
- 用 `tcpdump` 验证数据是否发送：`sudo tcpdump -i any udp port 52001`；
- 确认雷达已正常输出扫描数据（可通过雷达厂商工具验证）。

### 5.4 程序异常退出

- 检查雷达是否断连，程序内置自动重连机制，断连后会每 5 秒重试；
- 查看系统日志（`dmesg`），排查内存 / 线程异常；
- 确保两台雷达的本地绑定端口（52000/52002）未被占用（`netstat -tulpn | grep 52000`）。

## 6. 扩展说明

### 6.1 增加更多雷达

若需连接超过两台雷达，仅需修改以下两处：

1. `free_lidar_node.cpp` 中 `init_all_udp_sockets()` 函数，新增雷达 UDP 初始化：

// 新增雷达3（ID: Lidar03，本地端口52004，目标端口52005） 

if (!init_single_udp_socket(2, "Lidar03")) {

```
return false;
```

 }

2.main 函数中新增雷达实例，读取对应配置文件（`free_lidar_Lidar03.json`）并连接。

### 6.2 修改 UDP 广播地址

默认广播地址为 `255.255.255.255`（全网广播），如需修改为网段广播（如 `192.168.1.255`），修改 `udp_push_json` 函数中以下代码：

// 原代码 
dest_addr.sin_addr.s_addr = htonl(INADDR_BROADCAST); 
// 修改为网段广播（示例） 
dest_addr.sin_addr.s_addr = inet_addr("192.168.1.255");

## 7. 注意事项

1. 雷达参数（扫描频率、角度等）需与雷达实际配置一致，否则可能解析数据异常；
2. 运行程序时需确保足够的权限；
3. 长时间运行建议通过 `nohup` 后台启动：`nohup ./free_lidar_node > Lidar.log 2>&1 &`；
4. 程序退出时按 `Ctrl+C`，会自动关闭 UDP 连接和雷达连接，避免资源泄漏。
