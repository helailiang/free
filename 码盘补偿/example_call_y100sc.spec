# -*- mode: python ; coding: utf-8 -*-
"""
在「码盘补偿」目录执行（与仓库其它 spec 一致，推荐 uv）:

  uv sync --group build
  uv run pyinstaller --noconfirm example_call_y100sc.spec

生成 dist\\example_call_y100sc.exe（单文件、保留控制台，便于 --help 与串口输出）。

说明：入口仅依赖标准库 + pyserial；显式加入 serial.win32 相关子模块，避免 Windows onefile 下动态导入遗漏。
"""
from pathlib import Path

_spec_dir = Path(SPECPATH)
_root = _spec_dir.parent

block_cipher = None

a = Analysis(
    [str(_spec_dir / "example_call_y100sc.py")],
    pathex=[str(_spec_dir), str(_root)],
    binaries=[],
    datas=[],
    hiddenimports=[
        "y100sc_client",
        "serial",
        "serial.serialutil",
        "serial.serialwin32",
        "serial.win32",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="example_call_y100sc",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
