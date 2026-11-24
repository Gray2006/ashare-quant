# 量化工作台 - 在线安装脚本
# 由 install-online.bat 下载并执行:从 GitHub 拉最新 release 源码,
# 解压到目标目录,然后调用 install.bat 建虚拟环境、装依赖、建快捷方式。
# 参数 -Target 可覆盖安装目录(默认 %USERPROFILE%\ashare-quant),测试用。

param(
    [string]$Target = "",
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"
$Repo = "Gray2006/ashare-quant"

if (-not $Target) {
    $Target = Join-Path $env:USERPROFILE "ashare-quant"
}

function Fail($msg) {
    Write-Host ""
    Write-Host "[X] $msg" -ForegroundColor Red
    exit 1
}

# 已经装过就不覆盖:data/config.json 在里面,覆盖会丢账本
if (Test-Path (Join-Path $Target "run.py")) {
    Write-Host ""
    Write-Host "[!] 已经装过:$Target" -ForegroundColor Yellow
    Write-Host "    要升级请打开程序 -> 设置 -> 检查更新"
    Write-Host ""
    exit 2
}

# 1. 查最新 release
Write-Host ""
Write-Host "[1/3] 查 GitHub 最新版本 ..."
$headers = @{ "User-Agent" = "ashare-quant-installer" }
try {
    $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/latest" `
        -Headers $headers -TimeoutSec 30
} catch {
    Fail "连不上 GitHub:$($_.Exception.Message)"
}
$tag = [string]$rel.tag_name
if (-not $tag) {
    Fail "没拿到版本号,可能仓库还没有 release"
}
Write-Host "      最新版本 $tag"

# 2. 下载源码 zip
$zip = Join-Path $env:TEMP ("ashare-quant-" + $tag + ".zip")
$url = [string]$rel.zipball_url
if (-not $url) {
    $url = "https://github.com/$Repo/archive/refs/tags/$tag.zip"
}
Write-Host "[2/3] 下载 $tag ..."
try {
    Invoke-WebRequest -Uri $url -OutFile $zip -Headers $headers -TimeoutSec 300 -UseBasicParsing
} catch {
    Fail "下载失败:$($_.Exception.Message)"
}
if (-not (Test-Path $zip) -or (Get-Item $zip).Length -lt 1024) {
    Fail "下载到的文件不完整"
}

# 3. 解压并放到目标目录
$tmp = Join-Path $env:TEMP "ashare-quant-src"
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "[3/3] 解压并安装到 $Target ..."
try {
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
} catch {
    Fail "解压失败:$($_.Exception.Message)"
}
$root = Get-ChildItem $tmp -Directory | Select-Object -First 1
if (-not $root -or -not (Test-Path (Join-Path $root.FullName "run.py"))) {
    Fail "压缩包里没有 run.py,这个 tag 不是桌面版源码"
}
New-Item -ItemType Directory -Force -Path (Split-Path $Target -Parent) | Out-Null
if (Test-Path $Target) {
    Remove-Item $Target -Recurse -Force
}
Move-Item $root.FullName $Target
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item $zip -Force -ErrorAction SilentlyContinue

# 4. 交给 install.bat 建环境装依赖
if ($SkipInstall) {
    Write-Host ""
    Write-Host "代码已就位(-SkipInstall,跳过依赖安装):$Target" -ForegroundColor Green
    exit 0
}
Write-Host ""
Write-Host "代码已就位,开始安装依赖 ..." -ForegroundColor Green
Set-Location $Target
& cmd.exe /c "install.bat"
if ($LASTEXITCODE -ne 0) {
    Fail "install.bat 返回失败($LASTEXITCODE),按上面的提示排查"
}
exit 0
