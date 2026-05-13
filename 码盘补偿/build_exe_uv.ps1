# 在「码盘补偿」目录生成单文件 exe（含上级目录 libs/，勿用 --collect-all libs）
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
uv sync --group build
uv run pyinstaller --noconfirm h2_resolution_gui.spec
Write-Host "输出: $PSScriptRoot\dist\H2ResolutionGui.exe"
