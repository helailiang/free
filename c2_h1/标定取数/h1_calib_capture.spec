# -*- mode: python ; coding: utf-8 -*-
"""
H1 标定取数 GUI — PyInstaller 单文件打包配置。

在本目录（与 h1标定取数.py 同级）执行：

  双击 **pack_h1_calib.bat** 或执行 **.\\pack_h1_calib.ps1**。

  脚本会依次尝试：``H1_CALIB_PYTHON``、``VIRTUAL_ENV``、本目录已有 **.build_venv**、PATH 的 ``python``；
  若缺少 PyInstaller，会自动创建 **.build_venv** 并 ``pip install -r requirements-build.txt``（适合 PEP 668 / uv 环境）。

  首次打包可能较慢（PySide6 collect_all 体积大），请等待至生成 ``dist\\H1CalibCapture.exe``。

产物：dist\\H1CalibCapture.exe（无控制台窗口）。

说明：通过 collect_all("PySide6") 打入 Qt 插件与二进制依赖，避免运行时报缺 DLL。
"""
from pathlib import Path

try:
    from PyInstaller.utils.hooks import collect_all

    _p6_datas, _p6_bins, _p6_hidden = collect_all("PySide6")
except Exception:  # noqa: BLE001 — 仅解析 spec 时无 PySide6 也可通过
    _p6_datas, _p6_bins, _p6_hidden = [], [], []

_spec_dir = Path(SPECPATH)
_entry = _spec_dir / "h1标定取数.py"

block_cipher = None

a = Analysis(
    [str(_entry)],
    pathex=[str(_spec_dir)],
    binaries=list(_p6_bins),
    datas=list(_p6_datas),
    hiddenimports=list(_p6_hidden),
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
    name="H1CalibCapture",
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
