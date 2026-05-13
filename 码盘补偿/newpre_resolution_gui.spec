# -*- mode: python ; coding: utf-8 -*-
"""
在「码盘补偿」目录执行:
  uv sync --group build
  uv run pyinstaller --noconfirm newpre_resolution_gui.spec

生成 dist\\NewpreResolutionGui.exe（单文件、无控制台）。
"""
from pathlib import Path

try:
    from PyInstaller.utils.hooks import collect_all

    _p6_datas, _p6_bins, _p6_hidden = collect_all("PySide6")
except Exception:  # noqa: BLE001
    _p6_datas, _p6_bins, _p6_hidden = [], [], []

_spec_dir = Path(SPECPATH)
_root = _spec_dir.parent

block_cipher = None

a = Analysis(
    [str(_spec_dir / "newpre_resolution_gui_test.py")],
    pathex=[str(_spec_dir), str(_root)],
    binaries=list(_p6_bins),
    datas=list(_p6_datas),
    hiddenimports=list(_p6_hidden)
    + [
        "h1_radar_reader",
        "newpre_resolution_cli_test",
        "y100sc_client",
        "openpyxl",
        "openpyxl.cell._writer",
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
    name="NewpreResolutionGui",
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
