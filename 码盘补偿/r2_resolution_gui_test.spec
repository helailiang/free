# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller：从「码盘补偿/r2_resolution_gui_test.py」生成 R2 角分辨率 GUI 单文件 exe。

``pathex`` 含仓库根与「码盘补偿」，便于解析 ``R2`` 包与平铺模块；Qt 由官方 hook 随 import 收集。
"""

from pathlib import Path

_REPO_ROOT = Path(SPECPATH).resolve().parent

datas = []
binaries = []
hiddenimports = [
    "shiboken6",
    "serial",
    "serial.tools",
    "R2",
    "R2.r2_client",
    "r2_radar_client",
    "newpre_resolution_cli_test",
    "y100sc_client",
    "openpyxl",
    "openpyxl.cell._writer",
]

a = Analysis(
    ["r2_resolution_gui_test.py"],
    pathex=[str(_REPO_ROOT), str(_REPO_ROOT / "码盘补偿")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="r2_resolution_gui_test",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
