"""
R2 雷达基础通信（控制面 HTTP + 数据面 TCP 扫描包）。

协议依据：``.cursor/skills/r2-lidar-protocol/R2雷达通讯协议说明.md``（表 4-5、STEP1～8、表 4-6）。
实现约束：
  - 控制指令使用 ``GET /cmd/<cmd_name>?...``，默认 ``HTTP/1.0``、端口 80；
  - 应答体为 JSON，且含 ``error_code`` / ``error_text``；
  - 扫描数据为独立 TCP，包头起始标志 ``0xA25C``、类型 ``0x0043``，端序文档未写明，
    业界常见为小端，本实现按 **小端** 解析并在字段注释中标明可验证方式。

本模块刻意只依赖标准库，便于在无 pip 环境直接运行。
"""

from __future__ import annotations

import http.client
import json
import select
import socket
import struct
import sys
import time
from dataclasses import dataclass
from typing import Any, Iterator
from urllib.parse import quote, urlencode


# ---------------------------------------------------------------------------
# 常量：与说明书表 4-6 / 示例 IP 对齐，便于全文搜索与单测断言
# ---------------------------------------------------------------------------

# 表 4-6：一包扫描数据的起始魔数与类型字段
R2_SCAN_MAGIC: int = 0xA25C
# 说明书示例类型 0x0043（ASCII ``'C'``）；部分实机固件为 0x0041（``'A'``），载荷布局相同，一并接受。
R2_SCAN_PACKET_TYPE: int = 0x0043
R2_SCAN_PACKET_TYPES_ACCEPTED: frozenset[int] = frozenset({0x0043, 0x0041})

# 表 4-6：无效距离编码（uint20 全 1）
R2_DISTANCE_INVALID: int = 0xFFFFF

# 文档示例默认设备地址（用户环境以实机为准）
R2_DEFAULT_HOST: str = "192.168.0.240"
R2_DEFAULT_CMD_PORT: int = 80

# 现场/产品约定：R2 整圈扫描 360° 对应点数（与 GUI 默认索引起止 0～3599、角分辨率 0.1°/index 一致）
R2_FULL_CIRCLE_POINT_COUNT: int = 3600

# 表 4-6「uint20 + uint12」在 **4 字节小端字** 内的具体排布，不同固件/机型可能不同。
# - ``dist20_amp12``：低 20 位距离、高 12 位幅度（说明书 OCR 常见解读）。
# - ``amp12_dist20``：低 12 位幅度、高 20 位距离（字段顺序对调）。
# - ``nibble20_12``：低 20 位由 b0|b1<<8|(b2&0x0F)<<16 组成，幅度为 (b2>>4)|(b3<<4)（半字节对齐排布）。
# - ``u16u16``：两个连续 uint16 LE，依次为距离、强度（各 16 位，强度再与 0xFFF 与以防越界）。
# 若日志里距离正常但反射率恒为 0，多为 **高 12 位在流中恒为 0**（固件未填或未开强度），可改本变量或抓单点 4 字节 Hex 对照。
R2_POINT_PACK_MODE: str = "dist20_amp12"


class R2ProtocolError(RuntimeError):
    """雷达返回 JSON 中 ``error_code != 0`` 或 HTTP 层异常时抛出。"""

    def __init__(self, message: str, *, error_code: int | None = None, payload: Any = None):
        super().__init__(message)
        self.error_code = error_code
        self.payload = payload


def http_json_error_code(data: dict[str, Any]) -> int:
    """
    读取 JSON 中的 ``error_code`` 为整数；缺省键视为 ``0``（成功）。

    非数字内容返回 ``-1``，调用方应按异常处理。
    """
    if "error_code" not in data:
        return 0
    try:
        return int(data["error_code"])
    except (TypeError, ValueError):
        return -1


def format_r2_http_json_error(http_path: str, data: dict[str, Any]) -> str:
    """
    将一次 HTTP JSON 业务失败格式化为可读多行串（含完整 JSON，便于对照设备返回）。

    约定：**仅** ``error_code == 0`` 为成功；例如
    ``{"error_code":120,"error_text":"Invalid handle or no handle provided"}``。
    """
    pretty = json.dumps(data, ensure_ascii=False, indent=2)
    return (
        "[R2 HTTP] 业务应答非正常：仅当 error_code==0 为成功；以下为设备返回原文。\n"
        f"GET {http_path}\n{pretty}"
    )


def report_r2_http_json_error(http_path: str, data: dict[str, Any]) -> None:
    """若 ``error_code != 0``，向 **stderr** 打印 ``format_r2_http_json_error``；成功则不打印。"""
    if http_json_error_code(data) == 0:
        return
    print(format_r2_http_json_error(http_path, data), file=sys.stderr, flush=True)


def _require_success_json(data: dict[str, Any], *, http_path: str = "") -> dict[str, Any]:
    """
    校验 R2 HTTP JSON：``error_code == 0`` 才返回 ``data``。

    失败时先 **打印** 完整说明与 JSON 到 stderr，再抛 ``R2ProtocolError``（消息与打印一致，便于 GUI 展示）。
    """
    code = http_json_error_code(data)
    if code != 0:
        path = http_path or "(路径未记录)"
        msg = format_r2_http_json_error(path, data)
        print(msg, file=sys.stderr, flush=True)
        raise R2ProtocolError(msg, error_code=code, payload=data)
    return data


def _parse_json_body(raw: bytes) -> dict[str, Any]:
    """
    从 HTTP 响应字节流中取出 JSON 对象。

    设备应答形态为 ``HTTP/1.0 200 OK`` + 头域 + 空行 + JSON（见说明书示例）。
    ``http.client`` 已将 body 单独给出；此处仍兼容 body 前若含少量空白/杂字节的情况，
    通过首次 ``{`` 与末次 ``}`` 截取，降低对固件微小差异的敏感度。
    """
    text = raw.decode("utf-8", errors="replace").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0 or end <= start:
        raise R2ProtocolError(f"响应中未找到 JSON 对象: {text[:200]!r}")
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise R2ProtocolError(f"JSON 解析失败: {exc}; 片段={text[start : start + 120]!r}") from exc


class R2ControlClient:
    """
    R2 控制面客户端：通过 HTTP GET 调用 ``/cmd/<cmd_name>``。

    典型业务流程（与说明书 STEP 一致）：
      1. ``get_protocol_info`` / ``list_parameters`` 探活与枚举；
      2. ``get_parameter`` 读配置；
      3. ``request_handle_tcp`` 取数据口端口与 ``handle``；
      4. ``start_scanoutput`` 通知设备开始向该端口推流；
      5. 另开 TCP 连接至数据端口读二进制包（见 ``read_one_scan_packet``）；
      6. 周期性 ``feed_watchdog``，避免会话被设备回收（说明书 STEP7）。

    注意：说明书注明样机阶段部分写参数/命令可能不可用，调用 ``set_parameter`` 等应做好异常处理。
    """

    def __init__(
        self,
        host: str = R2_DEFAULT_HOST,
        *,
        cmd_port: int = R2_DEFAULT_CMD_PORT,
        timeout: float = 5.0,
    ) -> None:
        # 保存连接参数；每次请求新建 ``HTTPConnection``，避免长连接在雷达侧 ``Connection: close`` 下的半开状态。
        self.host = host
        self.cmd_port = int(cmd_port)
        self.timeout = float(timeout)

    def _get(self, path: str) -> dict[str, Any]:
        """
        发送单次 GET 并解析 JSON。

        ``path`` 必须以 ``/cmd/`` 开头且含查询串（若无需参数，部分命令仍带 ``?``，与文档示例一致）。
        """
        conn = http.client.HTTPConnection(self.host, self.cmd_port, timeout=self.timeout)
        try:
            # 显式关闭连接与 HTTP/1.0 行为对齐说明书；部分固件对 Host 头敏感，一并带上。
            conn.request(
                "GET",
                path,
                headers={"Host": self.host, "Connection": "close", "Accept": "application/json,*/*"},
            )
            resp = conn.getresponse()
            body = resp.read()
            if resp.status != 200:
                raise R2ProtocolError(f"HTTP 状态异常: {resp.status} {resp.reason!r}, body={body[:200]!r}")
            return _parse_json_body(body)
        finally:
            conn.close()

    def _get_ok(self, path: str) -> dict[str, Any]:
        """GET 并解析 JSON，且 **强制** ``error_code==0``。"""
        return _require_success_json(self._get(path), http_path=path)

    def get_json_raw(self, path: str) -> dict[str, Any]:
        """
        GET 并解析 JSON，**不**校验 ``error_code``。

        用于 ``stop_scanoutput`` / ``release_handle`` 等容错清理；若需提示用户请对返回值调用
        ``report_r2_http_json_error(path, data)``。
        """
        return self._get(path)

    def get_protocol_info(self) -> dict[str, Any]:
        """STEP1：查询协议名、版本及可用 ``commands`` 列表。"""
        return self._get_ok("/cmd/get_protocol_info")

    def list_parameters(self) -> dict[str, Any]:
        """STEP2：查询可读写的参数名列表（样机阶段可能仅支持读）。"""
        return self._get_ok("/cmd/list_parameters")

    def get_parameter(self, *names: str) -> dict[str, Any]:
        """
        STEP4：按名称读取一个或多个参数。

        多个参数名使用说明书约定的英文分号 ``;`` 连接，例如
        ``list=scan_frequency;scan_frequency_measured``。
        """
        if not names:
            raise ValueError("至少传入一个参数名")
        # 分号在部分 HTTP 栈中需编码；说明书示例为明文分号，这里先明文，若遇兼容问题可改为 %3B。
        q = urlencode({"list": ";".join(names)})
        return self._get_ok(f"/cmd/get_parameter?{q}")

    def set_parameter(self, mapping: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        """
        STEP3：写参数（说明书注明样机阶段可能不支持）。

        ``mapping`` 与关键字参数合并后序列化为查询串；值为 ``str`` / ``int`` / ``float`` / ``bool``。
        """
        merged: dict[str, Any] = {}
        if mapping:
            merged.update(mapping)
        merged.update(kwargs)
        if not merged:
            raise ValueError("无参数可写")
        q = urlencode({str(k): str(v) for k, v in merged.items()})
        return self._get_ok(f"/cmd/set_parameter?{q}")

    def request_handle_tcp(self) -> tuple[int, str]:
        """
        STEP5：申请 TCP 数据通道，返回 ``(port, handle)``。

        ``handle`` 需原样传给 ``start_scanoutput`` / ``feed_watchdog``；若含非 URL 安全字符则编码。
        """
        data = self._get_ok("/cmd/request_handle_tcp?packet_type=C&start_angle=-1800000")
        port = int(data["port"])
        handle = str(data["handle"])
        return port, handle

    def start_scanoutput(self, handle: str) -> dict[str, Any]:
        """STEP6：携带 ``handle`` 请求设备开始向数据 TCP 端口推送扫描数据。"""
        safe = quote(handle, safe="")
        return self._get_ok(f"/cmd/start_scanoutput?handle={safe}")

    def feed_watchdog(self, handle: str) -> dict[str, Any]:
        """STEP7：会话保活；在接收数据循环旁路周期性调用。"""
        safe = quote(handle, safe="")
        return self._get_ok(f"/cmd/feed_watchdog?handle={safe}")

    def stop_scanoutput(self, handle: str) -> dict[str, Any]:
        """说明书命令列表中的停止输出（样机阶段可能不可用）；接口先按规范暴露。"""
        safe = quote(handle, safe="")
        return self._get_ok(f"/cmd/stop_scanoutput?handle={safe}")

    def release_handle(self, handle: str) -> dict[str, Any]:
        """释放数据句柄（样机阶段可能不可用）。"""
        safe = quote(handle, safe="")
        return self._get_ok(f"/cmd/release_handle?handle={safe}")


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """从流式套接字读取恰好 ``n`` 字节，避免半包导致结构体错位。"""
    chunks: list[bytes] = []
    remaining = n
    while remaining > 0:
        block = sock.recv(remaining)
        if not block:
            raise R2ProtocolError(f"对端关闭或 EOF，仍缺 {remaining} 字节")
        chunks.append(block)
        remaining -= len(block)
    return b"".join(chunks)


@dataclass
class R2ScanHeader:
    """
    表 4-6 扫描包头的已解析子集（按小端、固定字段布局解出）。

    说明：OCR 版表格在「状态标志位 / 扫描频率 / 每圈采样数」等行存在串列风险；
    本结构按说明书字段语义与常见嵌入式排列推导；若与实机不符，请以 ``raw_header`` 十六进制比对并调整 ``HEADER_STRUCT``。
    """

    magic: int
    packet_type: int
    total_length: int
    header_length: int
    scan_count: int
    packet_index: int
    status_flags: int
    scan_frequency_milli_hz: int
    samples_per_scan: int
    points_in_packet: int
    first_point_index: int
    first_angle_centi_deg: int
    angle_step_centi_deg: int
    raw_header: bytes


# 从第 10 字节起固定解析 66 字节，对应：扫描圈号、包序号、两路 ntp64、状态、频率、
# 每圈采样数、本包点数、第一点序号、第一点角度、角步进、两 uint32 保留、两 ntp64 保留。
# 若固件增加字段，会体现在 ``header_length`` 增大；此时 ``raw_header`` 仍完整保留供分析。
_HEADER_SUFFIX_FMT: str = "<HHQQIIHHHiiIIQQ"
_HEADER_SUFFIX_SIZE: int = struct.calcsize(_HEADER_SUFFIX_FMT)


def parse_scan_packet(packet: bytes) -> tuple[R2ScanHeader, bytes, bytes | None]:
    """
    将一整包 TCP 扫描数据拆成 ``(头信息, 点云载荷字节, 可选校验和 4 字节)``。

    点云载荷为 ``uint20 + uint12`` 紧排。说明书称包尾可有 **optional** 4 字节校验；
    与点云同为 4 字节对齐时无法仅从长度可靠区分，故默认 **整段 ``body`` 均作为点云区** 返回。
    若实机确认末尾恒为校验和，可在上层调用 ``iter_scan_points`` 前自行 ``body = body[:-4]``。
    """
    if len(packet) < 10:
        raise R2ProtocolError(f"包过短: len={len(packet)}")
    magic, packet_type, total_length, header_length = struct.unpack_from("<HHIH", packet, 0)
    if magic != R2_SCAN_MAGIC:
        raise R2ProtocolError(f"魔数不匹配: 期望 0x{R2_SCAN_MAGIC:04X}, 实际 0x{magic:04X}")
    if int(packet_type) not in R2_SCAN_PACKET_TYPES_ACCEPTED:
        exp = ", ".join(f"0x{t:04X}" for t in sorted(R2_SCAN_PACKET_TYPES_ACCEPTED))
        raise R2ProtocolError(f"类型不匹配: 接受 {{{exp}}}，实际 0x{int(packet_type) & 0xFFFF:04X}")
    if total_length > len(packet):
        raise R2ProtocolError(f"声明总长 {total_length} 大于缓冲区 {len(packet)}")
    if header_length > total_length or header_length < 10:
        raise R2ProtocolError(f"异常 header_length={header_length}, total_length={total_length}")

    tail = packet[:total_length]
    body = tail[header_length:]

    checksum_bytes: bytes | None = None
    point_bytes = body

    suffix = tail[10:header_length]
    if len(suffix) < _HEADER_SUFFIX_SIZE:
        # 头短于推导布局：仅填已解析的基础字段，其余置 0，避免误解析。
        hdr = R2ScanHeader(
            magic=magic,
            packet_type=packet_type,
            total_length=total_length,
            header_length=header_length,
            scan_count=0,
            packet_index=0,
            status_flags=0,
            scan_frequency_milli_hz=0,
            samples_per_scan=0,
            points_in_packet=max(0, len(point_bytes) // 4),
            first_point_index=0,
            first_angle_centi_deg=0,
            angle_step_centi_deg=0,
            raw_header=tail[:header_length],
        )
        return hdr, point_bytes, checksum_bytes

    (
        scan_count,
        packet_index,
        _ntp_internal,
        _ntp_sync,
        status_flags,
        scan_frequency_milli_hz,
        samples_per_scan,
        points_in_packet,
        first_point_index,
        first_angle_centi_deg,
        angle_step_centi_deg,
        _r1,
        _r2,
        _r3,
        _r4,
    ) = struct.unpack_from(_HEADER_SUFFIX_FMT, tail, 10)

    hdr = R2ScanHeader(
        magic=magic,
        packet_type=packet_type,
        total_length=total_length,
        header_length=header_length,
        scan_count=int(scan_count) & 0xFFFF,
        packet_index=int(packet_index) & 0xFFFF,
        status_flags=int(status_flags) & 0xFFFFFFFF,
        scan_frequency_milli_hz=int(scan_frequency_milli_hz) & 0xFFFFFFFF,
        samples_per_scan=int(samples_per_scan) & 0xFFFF,
        points_in_packet=int(points_in_packet) & 0xFFFF,
        first_point_index=int(first_point_index) & 0xFFFF,
        first_angle_centi_deg=int(first_angle_centi_deg),
        angle_step_centi_deg=int(angle_step_centi_deg),
        raw_header=tail[:header_length],
    )
    return hdr, point_bytes, checksum_bytes


def decode_r2_point_four_bytes(b4: bytes) -> tuple[int, int]:
    """
    将单个测距点的 **4 字节原始小端载荷** 解成 ``(distance_mm, amplitude)``。

    说明：表 4-6 仅写「uint20 distance + uint12 amplitude」，未画明在 32bit 字内的位序；
    默认 ``dist20_amp12`` 与多数「低距高幅」实现一致。若现场 **距离合理而幅度恒 0**，
    说明 ``(uint32>>20)&0xFFF`` 在实机上常为 0：要么固件未写强度，要么应换 ``R2_POINT_PACK_MODE``
    （见模块级常量说明）。GUI 的「反射率」列即本函数返回的 ``amplitude``（与 H2 列名对齐）。
    """
    if len(b4) != 4:
        raise R2ProtocolError(f"单点须 4 字节，实际 len={len(b4)}")
    mode = str(R2_POINT_PACK_MODE).strip().lower()

    if mode in ("amp12_dist20", "rev", "swapped"):
        w = int.from_bytes(b4, "little", signed=False)
        dist = (w >> 12) & R2_DISTANCE_INVALID
        amp = w & 0xFFF
        return dist, amp

    if mode in ("nibble20_12", "nibble"):
        b0, b1, b2, b3 = b4[0], b4[1], b4[2], b4[3]
        dist = int(b0 | (b1 << 8) | ((b2 & 0x0F) << 16)) & R2_DISTANCE_INVALID
        amp = int(((b2 >> 4) | (b3 << 4)) & 0xFFF)
        return dist, amp

    if mode in ("u16u16", "hh", "two_u16"):
        d16, a16 = struct.unpack_from("<HH", b4, 0)
        dist = int(d16) & R2_DISTANCE_INVALID
        amp = int(a16) & 0xFFFF
        return dist, amp

    # 默认：dist20_amp12 — 与表 4-6「先距后幅」的常见小端解读一致
    w = int.from_bytes(b4, "little", signed=False)
    dist = w & R2_DISTANCE_INVALID
    amp = (w >> 20) & 0xFFF
    return dist, amp


def iter_scan_points(point_payload: bytes) -> Iterator[tuple[int, int]]:
    """
    迭代解析测距点 ``(distance_mm, amplitude)``。

    实际拆法由 ``decode_r2_point_four_bytes`` / ``R2_POINT_PACK_MODE`` 决定；默认与表 4-6 的
    uint20+uint12 小端「低 20 距、高 12 幅」一致。``distance_mm == 0xFFFFF`` 为无效距离。
    """
    if len(point_payload) % 4 != 0:
        raise R2ProtocolError(f"点云区长度非 4 倍数: len={len(point_payload)}")
    for i in range(0, len(point_payload), 4):
        yield decode_r2_point_four_bytes(point_payload[i : i + 4])


def read_one_scan_packet(sock: socket.socket) -> bytes:
    """
    从已连接的数据 TCP 套接字读取 **完整一包** 表 4-6 数据。

    表 4-6 前 10 字节为 ``magic + type + total_length + header_length``（小端），
    必须先读满 10 字节再按 ``total_length`` 读齐整包，否则会错把 ``header_length`` 的高字节并入长度域。
    """
    prefix = _recv_exact(sock, 10)
    magic, packet_type, total_length, _hdr_len = struct.unpack_from("<HHIH", prefix, 0)
    if magic != R2_SCAN_MAGIC:
        raise R2ProtocolError(f"同步丢失: 魔数 0x{magic:04X} != 0x{R2_SCAN_MAGIC:04X}")
    if total_length < 10:
        raise R2ProtocolError(f"非法 total_length={total_length}")
    rest = _recv_exact(sock, total_length - 10)
    return prefix + rest


def drain_scan_tcp_complete_packets(
    sock: socket.socket,
    *,
    max_packets: int = 8000,
    max_wall_s: float = 1.5,
    per_packet_timeout_s: float = 1.5,
    tail_quiet_polls: int = 0,
    tail_quiet_poll_s: float = 0.04,
    tail_only_after_reads: bool = True,
) -> int:
    """
    尽量排空数据 TCP 上「当前已到达内核」的完整扫描包，使后续组圈更接近 **此刻** 的雷达输出。

    说明：
      - **无法**让数据不经过 OS 接收缓冲；TCP 语义即如此。本函数做的是：在保持连接的前提下，
        连续读取并丢弃 **完整** 表 4-6 包，直到 ``select`` 表明暂无更多立即可读数据（或达到上限）。
      - 仅在 ``select`` 返回可读后才 ``read_one_scan_packet``，避免在「仅半包到达」时用短超时读导致
        半包滞留、后续组圈错位。
      - 若设备推流极快、积压超过 ``max_packets`` / ``max_wall_s``，可能仍残留少量旧数据；
        可再依赖 ``R2SingleScanRadar.stream_discard_circles_before_sample`` 额外丢整圈。
      - **静默尾捕**（``tail_quiet_polls`` > 0）：在「零超时 select 已不可读」之后，用短阻塞 ``select``
        再轮询若干次，吸收「刚排空瞬间又抵内核」的尾包；若 ``tail_only_after_reads`` 为 True 且本轮
        尚未读过任何完整包，则跳过尾捕，避免在**空缓冲**连接上无谓等待。

    Returns:
        成功丢弃的完整包个数。
    """
    old_to = sock.gettimeout()
    n = 0
    deadline = time.monotonic() + float(max_wall_s)
    tail_polls = max(0, int(tail_quiet_polls))
    tail_poll_s = max(0.0, float(tail_quiet_poll_s))
    try:
        sock.settimeout(float(per_packet_timeout_s))
        while True:
            # 阶段 A：零超时「能读多少读多少」，尽快追到队列实时沿。
            while n < int(max_packets) and time.monotonic() < deadline:
                r, _, _ = select.select([sock], [], [], 0.0)
                if not r:
                    break
                read_one_scan_packet(sock)
                n += 1
            if tail_polls <= 0:
                return n
            # 若连接上本来就无积压，不要为「尾捕」白白阻塞（常见于 stop_tcp 刚重连后的空队列）。
            if tail_only_after_reads and n == 0:
                return n
            # 阶段 B：短阻塞轮询，吃掉排空瞬间又到达的少量整包；若捕到则回到阶段 A 继续快读。
            got_tail = False
            for _ in range(tail_polls):
                if n >= int(max_packets) or time.monotonic() >= deadline:
                    return n
                r2, _, _ = select.select([sock], [], [], tail_poll_s)
                if r2:
                    read_one_scan_packet(sock)
                    n += 1
                    got_tail = True
                    break
            if not got_tail:
                return n
    finally:
        sock.settimeout(old_to)


def configure_r2_data_socket(sock: socket.socket) -> None:
    """
    对 R2 **数据面** TCP 套接字应用与 ``open_scan_stream`` / ``connect_r2_data_tcp`` 一致的选项。

    意图：
      - ``TCP_NODELAY``：减少 Nagle 对小包组圈的额外延迟；
      - ``SO_RCVBUF``：在系统允许时略放大接收缓冲，减轻极端推流下内核队列溢出风险；
        若 ``setsockopt`` 被拒（部分主机策略），静默忽略即可，不影响基本读流。
    """
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 256 * 1024)
    except OSError:
        pass


def connect_r2_data_tcp(host: str, port: int, *, timeout: float = 5.0) -> socket.socket:
    """
    建立到 ``(host, port)`` 的 R2 数据 TCP，并套用 ``configure_r2_data_socket``。

    与 ``open_scan_stream`` 中「成功 ``start_scanoutput`` 之后」的建连步骤一致。
    注意：许多实机在 ``stop_scanoutput`` / ``release_handle`` 后会关闭原监听端口；若需再次收流，
    应重新 ``request_handle_tcp`` 取 **新** 端口（及新 ``handle``），勿假定旧端口仍可连。
    """
    s = socket.create_connection((host, int(port)), timeout=float(timeout))
    configure_r2_data_socket(s)
    return s


def open_scan_stream(
    host: str,
    *,
    cmd: R2ControlClient | None = None,
    timeout: float = 5.0,
) -> tuple[socket.socket, str, R2ControlClient, int]:
    """
    按产品流程 **严格顺序** 打开连续点云数据通道（STEP5 → STEP6 → 再连 TCP）：

    1. **HTTP** ``request_handle_tcp``：取得动态 **数据 TCP 端口** 与 ``handle``；
    2. **HTTP** ``start_scanoutput``：仅当 JSON 应答成功（``error_code==0``，由
       ``start_scanoutput`` 内部 ``_require_success_json`` 保证）后，本函数才继续；
    3. **TCP** ``connect_r2_data_tcp``：连接步骤 1 返回的端口读取表 4-6 包。

    不再取数时须由调用方通过 **HTTP** ``stop_scanoutput`` 停止推流（见 ``R2SingleScanRadar.close``），
    再关闭数据套接字；顺序错误可能导致无数据或会话异常。

    返回 ``(data_socket, handle, ctrl, data_port)``：多返回 ``data_port`` 便于 GUI/日志展示；若上层在
    ``stop_tcp`` 类流程中已 ``release_handle`` 并再次 ``request_handle_tcp``，须改用 **新** 返回的端口与 handle。
    """
    ctrl = cmd or R2ControlClient(host, timeout=timeout)
    data_port, handle = ctrl.request_handle_tcp()
    # ``start_scanoutput`` 失败会抛 ``R2ProtocolError``，此时不得连接数据 TCP（与现场协议一致）。
    ctrl.start_scanoutput(handle)
    try:
        s = connect_r2_data_tcp(host, data_port, timeout=timeout)
    except OSError:
        # 已下发 start 但 TCP 未连上：尽量 HTTP 停流，避免雷达侧长时间空推（不抛业务错，仅打印非 0）。
        try:
            p = f"/cmd/stop_scanoutput?handle={quote(handle, safe='')}"
            report_r2_http_json_error(p, ctrl.get_json_raw(p))
        except Exception as exc:
            print(f"[R2] TCP 连接失败后清理 stop_scanoutput 异常: {exc!r}", file=sys.stderr, flush=True)
        raise
    return s, handle, ctrl, int(data_port)


if __name__ == "__main__":
    # 最小烟测：对默认 IP 调用 get_protocol_info；无设备时会打印连接错误，便于现场排查。
    target = sys.argv[1] if len(sys.argv) > 1 else R2_DEFAULT_HOST
    print(f"R2 探活: GET http://{target}/cmd/get_protocol_info")
    try:
        cli = R2ControlClient(target)
        print("获取协议")
        info = cli.get_protocol_info()
        print(json.dumps(info, ensure_ascii=False, indent=2))

        print("获取tcp参数")
        info1 = cli.request_handle_tcp()
        print(json.dumps(info1, ensure_ascii=False, indent=2))
        port,handler = info1
        print("开启tcp参数", port)
        info2 = cli.stop_scanoutput(handle=handler)
        print(json.dumps(info2, ensure_ascii=False, indent=2))
        print("关闭tcp参数")
        info3 = cli.stop_scanoutput(handle=handler)
        print(json.dumps(info3, ensure_ascii=False, indent=2))
    except Exception as exc:  # noqa: BLE001 — 命令行烟测需要打印任意异常栈因
        print(f"失败: {exc}", file=sys.stderr)
        sys.exit(1)
