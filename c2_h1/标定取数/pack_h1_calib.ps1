# H1 标定取数 — 打包为 dist\H1CalibCapture.exe
#
# 解释器优先级：H1_CALIB_PYTHON > VIRTUAL_ENV > 本目录 .build_venv > PATH 的 python
# 若无法 import PyInstaller，会自动创建 .build_venv 并 pip install -r requirements-build.txt（适配 PEP 668）

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$venvLocal = Join-Path $PSScriptRoot ".build_venv"
$pyLocal = Join-Path $venvLocal "Scripts\python.exe"

if ($env:H1_CALIB_PYTHON) {
    $py = $env:H1_CALIB_PYTHON
} elseif ($env:VIRTUAL_ENV) {
    $py = Join-Path $env:VIRTUAL_ENV "Scripts\python.exe"
} elseif (Test-Path $pyLocal) {
    $py = $pyLocal
} else {
    $py = "python"
}

Write-Host "使用解释器: $py"
& $py -c "import sys; print(sys.executable)"
if ($LASTEXITCODE -ne 0) {
    throw "无法运行 Python。请安装 Python 或设置 H1_CALIB_PYTHON。"
}

& $py -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "无 PyInstaller，创建 .build_venv 并安装依赖…"
    & python -m venv $venvLocal
    $py = $pyLocal
    & $py -m pip install -U pip
    & $py -m pip install -r (Join-Path $PSScriptRoot "requirements-build.txt")
}

Write-Host "PyInstaller（首次可能需数分钟）…"
& $py -m PyInstaller --noconfirm (Join-Path $PSScriptRoot "h1_calib_capture.spec")
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller 失败。"
}

Write-Host "完成：dist\H1CalibCapture.exe"
