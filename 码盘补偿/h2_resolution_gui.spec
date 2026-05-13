# -*- mode: python ; coding: utf-8 -*-
"""
在「码盘补偿」目录执行（推荐 uv）:
  uv sync --group build
  uv run pyinstaller --noconfirm h2_resolution_gui.spec

生成 dist\\H2ResolutionGui.exe（单文件、无控制台）。

说明：`libs` 在仓库根目录 `../libs`，不是 pip 包；datas 以 (libs 目录, "libs") 整树打入包内。
"""
from pathlib import Path

try:
    from PyInstaller.utils.hooks import collect_all

    _p6_datas, _p6_bins, _p6_hidden = collect_all("PySide6")
except Exception:  # noqa: BLE001 — 无 PySide6 时仍允许 pyi 解析本 spec
    _p6_datas, _p6_bins, _p6_hidden = [], [], []

_spec_dir = Path(SPECPATH)
_root = _spec_dir.parent
_libs_dir = _root / "libs"
# Analysis 的 datas 须为 (源路径, bundle 内相对目录) 二元组；Tree 的 TOC 三元组会触发 format 报错。
_libs_datas = [(str(_libs_dir), "libs")] if _libs_dir.is_dir() else []

block_cipher = None

a = Analysis(
    [str(_spec_dir / "h2_resolution_gui_test.py")],
    pathex=[str(_spec_dir), str(_root)],
    binaries=list(_p6_bins),
    datas=list(_p6_datas) + _libs_datas,
    hiddenimports=list(_p6_hidden)
    + [
        "h2_radar_client",
        "newpre_resolution_cli_test",
        "h1_radar_reader",
        "y100sc_client",
        "openpyxl",
        "openpyxl.cell._writer",
        "libs",
        "libs.protocols",
        "libs.protocols.h2_txt_parse",
        "libs.protocols.c3_common",
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
    name="H2ResolutionGui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
