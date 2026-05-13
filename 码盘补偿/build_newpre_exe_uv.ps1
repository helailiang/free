# 打包 newpre 角分辨率 GUI → dist\NewpreResolutionGui.exe
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
uv sync --group build
uv run pyinstaller --noconfirm newpre_resolution_gui.spec
Write-Host "输出: $PSScriptRoot\dist\NewpreResolutionGui.exe"
