from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Literal

import serial


Axis = Literal["X", "Y", "Z", "r", "t", "T"]
Sign = Literal["+", "-"]


class Y100SCError(RuntimeError):
    pass


@dataclass(frozen=True)
class Y100SCSerialConfig:
    port: str = "COM1"
    baudrate: int = 9600
    bytesize: int = 8
    parity: str = "N"
    stopbits: int = 1
    timeout_s: float = 0.3  # 文档：联络 200ms 内回 OK\n


class Y100SCClient:
    """
    基于 md 文档《Y100SC系列运动控制器控制指令及协议》的串口文本协议客户端。

    - 发送命令以 CR(0x0D) 结尾
    - 回包以 LF(0x0A) 结尾（如 OK\\n、X+123\\n、V 20\\n）
    """

    def __init__(self, config: Y100SCSerialConfig = Y100SCSerialConfig()) -> None:
        self._cfg = config
        self._ser: serial.Serial | None = None

    def open(self) -> None:
        if self._ser and self._ser.is_open:
            return
        self._ser = serial.Serial(
            port=self._cfg.port,
            baudrate=self._cfg.baudrate,
            bytesize=self._cfg.bytesize,
            parity=self._cfg.parity,
            stopbits=self._cfg.stopbits,
            timeout=self._cfg.timeout_s,
            write_timeout=self._cfg.timeout_s,
        )
        self._ser.reset_input_buffer()
        self._ser.reset_output_buffer()

    def close(self) -> None:
        if self._ser:
            try:
                self._ser.close()
            finally:
                self._ser = None

    def __enter__(self) -> "Y100SCClient":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _require_open(self) -> serial.Serial:
        if not self._ser or not self._ser.is_open:
            raise Y100SCError("串口未打开。请先调用 open() 或使用 with Y100SCClient(...).")
        return self._ser

    def _readline(self, *, allow_timeout: bool = False) -> str | None:
        ser = self._require_open()
        raw = ser.readline()  # 读到 \\n 或超时
        if not raw:
            if allow_timeout:
                return None
            raise Y100SCError("读取回包超时（未收到换行结尾的响应）。")
        try:
            return raw.decode("ascii", errors="strict")
        except UnicodeDecodeError as e:
            raise Y100SCError(f"回包不是 ASCII：{raw!r}") from e

    @staticmethod
    def _strip_echo(resp: str, cmd: str) -> str:
        """
        某些设备会把请求帧回显到 RX，例如：
        - 发送 '?V\\r'，回包可能为 '?V\\rV250\\n\\n'
        - 发送 '?V\\r\\n'，回包可能为 '?V\\r\\nV250\\n\\n'

        这里把最常见的回显前缀剥离掉，并返回剩余部分（不主动 rstrip）。
        """
        for prefix in (cmd + "\r\n", cmd + "\r", cmd + "\n"):
            if resp.startswith(prefix):
                return resp[len(prefix) :]
        return resp

    def _send_raw(self, cmd: str) -> None:
        ser = self._require_open()
        payload = (cmd + "\r").encode("ascii")
        ser.write(payload)
        ser.flush()

    def _wait_for_ok(self, cmd: str, timeout_s: float | None = None) -> None:
        """
        等待直到收到 OK（期间会忽略回显 cmd 和空行）。
        用于归零/运动等“完成后才回 OK”的指令。
        """
        deadline = time.monotonic() + (timeout_s if timeout_s is not None else self._cfg.timeout_s)
        last_raw: str | None = None

        # readline() 受 Serial.timeout 控制（可能远小于动作完成时间），这里用整体 deadline 兜底；
        # 读空时不报错，继续等待直到 deadline。
        while time.monotonic() < deadline:
            raw = self._readline(allow_timeout=True)
            if raw is None:
                continue
            last_raw = raw
            raw = self._strip_echo(raw, cmd)
            if raw.strip("\r\n") == "":
                continue
            if raw.strip("\r\n") == "OK":
                return
            # 某些实现会先回 'HX'/'X+1000' 之类确认收到，继续等待最终 OK
            # 若返回明确错误码（ERRx/ERRORx）则直接抛出
            tag = raw.strip("\r\n")
            if tag.startswith(("ERR", "ERROR")):
                raise Y100SCError(f"设备返回错误：{raw!r}")

        raise Y100SCError(f"等待 OK 超时，最后一行={last_raw!r}")

    def send(self, cmd: str) -> str:
        """
        发送一条命令（不需要自己加 \\r），并返回一行回包（包含末尾 \\n）。
        """
        self._send_raw(cmd)

        # 兼容：回显 + 真正响应 + 末尾多空行（\\n\\n）
        # 策略：最多读几行，跳过“纯回显/空行”，返回第一条有效响应行。
        last_raw: str | None = None
        for _ in range(6):
            raw = self._readline()
            last_raw = raw
            raw = self._strip_echo(raw, cmd)
            if raw.strip("\r\n") == "":
                continue
            return raw

        raise Y100SCError(f"未获取到有效回包（疑似仅回显/空行），最后一行={last_raw!r}")

    def handshake(self) -> None:
        """
        联络指令：发送 ?R\\r，期望回 OK\\n。
        """
        resp = self.send("?R")
        if resp.strip("\r\n") != "OK":
            raise Y100SCError(f"联络失败，回包={resp!r}")

    def query_speed(self) -> int:
        """
        速度查询：?V\\r -> 'V number\\n'
        """
        resp = self.send("?V").strip("\r\n")
        if not resp.startswith("V"):
            raise Y100SCError(f"速度查询回包格式异常：{resp!r}")
        # 兼容两种格式：
        # - 'V 250'
        # - 'V250'
        tail = resp[1:].strip()
        try:
            return int(tail)
        except ValueError as e:
            raise Y100SCError(f"速度数值解析失败：{resp!r}") from e

    def set_speed(self, speed: int) -> None:
        """
        速度设置：V<number>\\r -> OK\\n
        speed 范围 0..255
        """
        if not (0 <= speed <= 255):
            raise ValueError("speed 必须在 0..255")
        resp = self.send(f"V{speed}")
        if resp.strip("\r\n") != "OK":
            raise Y100SCError(f"速度设置失败，回包={resp!r}")

    def query_pos(self, axis: Axis) -> int:
        """
        坐标查询：?X/?Y/?Z/?r/?t/?T \\r -> 'X+number\\n' 或 'X-number\\n'
        返回带符号整数位置。
        """
        resp = self.send(f"?{axis}").strip("\r\n")
        if not resp or resp[0] != axis:
            raise Y100SCError(f"坐标查询回包格式异常：{resp!r}")
        try:
            return int(resp[1:])  # 包含 +/- 前缀
        except ValueError as e:
            raise Y100SCError(f"坐标值解析失败：{resp!r}") from e

    def home(self, axis: Axis, *, wait_timeout_s: float = 180.0) -> None:
        """
        归零是“完成后回 OK”的指令：
        - 先发送 H{axis}\\r，设备可能先回显/确认收到（如 'HX'）
        - 归零动作完成后再回 'OK'
        """
        cmd = f"H{axis}"
        self._send_raw(cmd)
        self._wait_for_ok(cmd, timeout_s=wait_timeout_s)

    def query_home_status(self) -> str:
        """
        归零状态：?H\\r -> 'H######\\n'
        返回 6 位状态字符串（不含 'H' 和换行），顺序见文档。
        """
        resp = self.send("?H").strip("\r\n")
        if not (resp.startswith("H") and len(resp) == 7):
            raise Y100SCError(f"归零状态回包格式异常：{resp!r}")
        bits = resp[1:]
        if any(c not in "01" for c in bits):
            raise Y100SCError(f"归零状态回包包含非法字符：{resp!r}")
        return bits

    def move(self, axis: Axis, direction: Sign, distance: int, *, wait_timeout_s: float = 180.0) -> None:
        """
        运行同样是“到位后回 OK”的指令：
        - 先发送 '{axis}{direction}{distance}\\r'，设备可能先回显/确认收到
        - 到达终点后回 'OK'
        """
        if direction not in ("+", "-"):
            raise ValueError("direction 只能是 '+' 或 '-'")
        if distance < 0:
            raise ValueError("distance 需为非负整数")
        cmd = f"{axis}{direction}{distance}"
        self._send_raw(cmd)
        self._wait_for_ok(cmd, timeout_s=wait_timeout_s)

    def stop(self) -> str:
        """
        停止：S\\r -> ERROR4\\n（文档描述）
        返回设备回包（含 \\n），供上层按需处理。
        """
        return self.send("S")

