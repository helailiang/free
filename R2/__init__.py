"""
R2 雷达基础通信包。

本目录实现依据仓库技能 ``r2-lidar-protocol`` 及同技能目录下的
``R2雷达通讯协议说明.md``：控制面为 HTTP（默认端口 80），数据面为独立 TCP
端口上的表 4-6 二进制扫描包。
"""

from .r2_client import (
    R2ControlClient,
    R2ProtocolError,
    R2ScanHeader,
    R2_FULL_CIRCLE_POINT_COUNT,
    R2_POINT_PACK_MODE,
    R2_SCAN_PACKET_TYPES_ACCEPTED,
    configure_r2_data_socket,
    connect_r2_data_tcp,
    decode_r2_point_four_bytes,
    format_r2_http_json_error,
    http_json_error_code,
    report_r2_http_json_error,
    iter_scan_points,
    open_scan_stream,
    parse_scan_packet,
    read_one_scan_packet,
)

__all__ = [
    "R2ControlClient",
    "R2ProtocolError",
    "R2ScanHeader",
    "R2_FULL_CIRCLE_POINT_COUNT",
    "R2_POINT_PACK_MODE",
    "R2_SCAN_PACKET_TYPES_ACCEPTED",
    "configure_r2_data_socket",
    "connect_r2_data_tcp",
    "decode_r2_point_four_bytes",
    "format_r2_http_json_error",
    "http_json_error_code",
    "report_r2_http_json_error",
    "iter_scan_points",
    "open_scan_stream",
    "parse_scan_packet",
    "read_one_scan_packet",
]
