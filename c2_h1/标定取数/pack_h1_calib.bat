@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem 解释器优先级：
rem   1) H1_CALIB_PYTHON
rem   2) VIRTUAL_ENV\Scripts\python.exe
rem   3) 本目录 .build_venv（若已存在，例如上次自动创建）
rem   4) PATH 上的 python
rem 若当前解释器无法 import PyInstaller，则自动创建 .build_venv 并安装依赖（适配 PEP 668 / uv 托管环境）

set "VENV_LOCAL=%~dp0.build_venv"

if defined H1_CALIB_PYTHON (
  set "PY=%H1_CALIB_PYTHON%"
) else if defined VIRTUAL_ENV (
  set "PY=%VIRTUAL_ENV%\Scripts\python.exe"
) else if exist "%VENV_LOCAL%\Scripts\python.exe" (
  set "PY=%VENV_LOCAL%\Scripts\python.exe"
) else (
  set "PY=python"
)

echo 使用解释器: %PY%
"%PY%" -c "import sys; print(sys.executable)" 2>nul
if errorlevel 1 (
  echo 未找到可用的 Python。请安装 Python 3.10+ 或设置 H1_CALIB_PYTHON。
  exit /b 1
)

"%PY%" -c "import PyInstaller" 2>nul
if errorlevel 1 (
  echo 当前解释器无 PyInstaller，正在创建本目录 .build_venv 并安装依赖…
  python -m venv "%VENV_LOCAL%"
  if errorlevel 1 (
    echo 创建 .build_venv 失败。
    exit /b 1
  )
  set "PY=%VENV_LOCAL%\Scripts\python.exe"
  "%PY%" -m pip install -U pip
  "%PY%" -m pip install -r "%~dp0requirements-build.txt"
  if errorlevel 1 exit /b 1
)

echo 打包 …
"%PY%" -m PyInstaller --noconfirm h1_calib_capture.spec
if errorlevel 1 (
  echo 打包失败。
  exit /b 1
)
echo 完成：dist\H1CalibCapture.exe
exit /b 0
