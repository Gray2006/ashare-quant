# 创建桌面快捷方式(由 install.bat 调用)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$target = Join-Path $root "start.bat"
$desktop = [Environment]::GetFolderPath("Desktop")
$lnk = Join-Path $desktop "量化工作台.lnk"
$shell = New-Object -ComObject WScript.Shell
$s = $shell.CreateShortcut($lnk)
$s.TargetPath = $target
$s.WorkingDirectory = $root
$s.IconLocation = "$env:SystemRoot\System32\imageres.dll,109"
$s.Description = "A 股公开行情与技术分析"
$s.Save()
Write-Output "shortcut: $lnk"
