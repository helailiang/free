"""
C2/H1 单线激光雷达网络通信自动化测试包。

本包把原测试方案中的人工步骤拆成可复用的 Python 模块：配置读取、设备通信、
指标统计、报告生成和 Pytest 用例。现场测试人员可以先用命令行跑核心通信和长稳，
后续再按需要把这些能力封装成 GUI。
"""

from .config import DeviceConfig, load_device_config

__all__ = ["DeviceConfig", "load_device_config"]
