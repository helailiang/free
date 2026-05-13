# 在「码盘补偿」目录生成 R2 角分辨率 GUI 单文件 exe（依赖 uv + pyproject 的 build 组）。
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
uv sync --group build
uv run pyinstaller --noconfirm r2_resolution_gui_test.spec
Write-Host "输出: $PSScriptRoot\dist\r2_resolution_gui_test.exe"
