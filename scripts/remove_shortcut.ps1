# 删除桌面快捷方式
$desktop = [Environment]::GetFolderPath("Desktop")
$lnk = Join-Path $desktop "量化工作台.lnk"
if (Test-Path $lnk) { Remove-Item $lnk -Force }
