"""
连续取数原始帧落盘。

抓取文件采用 JSON Lines：一行一个完整 TCP 应用层帧。这样现场可以直接用文本工具查看，
后续也能按 `frame_hex` 还原原始 bytes。
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import re
from typing import TextIO

from network_test.automation.metrics import PacketInfo


def _safe_path_part(value: str) -> str:
    """把 IP、型号等字符串压成适合文件名的片段。"""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return cleaned.strip("._") or "unknown"


class RawFrameCapture:
    """把完整应用层帧写成 JSONL 文件。"""

    def __init__(
        self,
        output_dir: str | Path,
        *,
        model: str,
        host: str,
        max_frames: int = 0,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        safe_model = _safe_path_part(model)
        safe_host = _safe_path_part(host.replace(":", "_"))
        self.path = self.output_dir / f"stream_raw_{safe_model}_{safe_host}_{stamp}.jsonl"
        self.max_frames = max(0, int(max_frames))
        self.frames_written = 0
        self.truncated = False
        self._file: TextIO | None = self.path.open("w", encoding="utf-8", newline="\n")

    def close(self) -> None:
        """关闭输出文件。"""
        if self._file is not None:
            self._file.close()
            self._file = None

    def write_frame(
        self,
        *,
        frame_index: int,
        received_at_s: float,
        frame: bytes,
        packet: PacketInfo | None,
    ) -> None:
        """写入一条完整帧；达到 max_frames 后静默停止并标记截断。"""
        if self._file is None:
            return
        if self.max_frames and self.frames_written >= self.max_frames:
            self.truncated = True
            return

        payload = {
            "frame_index": int(frame_index),
            "received_at_s": round(float(received_at_s), 6),
            "raw_length": len(frame),
            "parsed": packet is not None,
            "packet": asdict(packet) if packet is not None else None,
            "frame_hex": frame.hex(" ").upper(),
        }
        self._file.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        self._file.write("\n")
        self.frames_written += 1

    def __enter__(self) -> "RawFrameCapture":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()
