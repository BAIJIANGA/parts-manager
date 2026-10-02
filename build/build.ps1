# -*- coding: utf-8 -*-
# 一键重建便携免安装版。用法(在 parts-manager 目录下):
#     pwsh -File build\build.ps1
#
# 产物:
#     dist\元器件物料管理\            便携目录(双击里面的 exe 即可用)
#     dist\元器件物料管理_便携版.zip   可直接分发/拷贝到 U 盘
$ErrorActionPreference = 'Stop'

$Root  = Split-Path -Parent $PSScriptRoot
$Dist  = Join-Path $Root 'dist'
$Build = $PSScriptRoot
$Pkg   = Join-Path $Dist '元器件物料管理'
$Embed = Join-Path $Dist 'python-embed'
$Csc   = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'

$PY_VER = '3.12.10'
$PY_URL = "https://www.python.org/ftp/python/$PY_VER/python-$PY_VER-embed-amd64.zip"

Write-Host '[1/6] 检查编译器' -ForegroundColor Cyan
if (-not (Test-Path $Csc)) { throw "找不到 csc.exe: $Csc (.NET Framework 4 应随 Windows 自带)" }

Write-Host '[2/6] 准备内嵌 Python' -ForegroundColor Cyan
if (-not (Test-Path (Join-Path $Embed 'python.exe'))) {
    Write-Host "  下载 $PY_URL"
    # 注意:PowerShell 的 Invoke-WebRequest 在本机下不来(python.org 的 TLS 握手会断),
    # 所以用项目自带的 venv Python 去抓。
    $venvPy = Join-Path $Root 'venv\Scripts\python.exe'
    if (-not (Test-Path $venvPy)) { $venvPy = 'python' }
    $zip = Join-Path $Dist 'python-embed.zip'
    & $venvPy -c @"
import urllib.request, zipfile, os, sys
url, dest, out = sys.argv[1], sys.argv[2], sys.argv[3]
req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
with urllib.request.urlopen(req, timeout=180) as r, open(dest, 'wb') as f:
    f.write(r.read())
os.makedirs(out, exist_ok=True)
with zipfile.ZipFile(dest) as z: z.extractall(out)
print('  extracted to', out)
"@ $PY_URL $zip $Embed
    Remove-Item $zip -Force -EA SilentlyContinue
} else { Write-Host '  已有,跳过' }

Write-Host '[3/6] 生成图标' -ForegroundColor Cyan
$pillowPy = $null
foreach ($c in @(
    (Join-Path $env:USERPROFILE '.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe'),
    (Join-Path $Root 'venv\Scripts\python.exe'))) {
    if (Test-Path $c) {
        & $c -c 'import PIL' 2>$null
        if ($LASTEXITCODE -eq 0) { $pillowPy = $c; break }
    }
}
if ($pillowPy) { & $pillowPy (Join-Path $Build 'make_icon.py') (Join-Path $Build 'app.ico') }
else { Write-Host '  没有带 Pillow 的 Python,跳过图标(不影响功能)' -ForegroundColor Yellow }

Write-Host '[4/6] 编译启动器' -ForegroundColor Cyan
$iconArg = if (Test-Path (Join-Path $Build 'app.ico')) { "/win32icon:$(Join-Path $Build 'app.ico')" } else { '' }
& $Csc /nologo /target:winexe /r:System.Windows.Forms.dll $iconArg `
       /out:"$(Join-Path $Dist '元器件物料管理.exe')" (Join-Path $Build 'Launcher.cs')
if ($LASTEXITCODE -ne 0) { throw 'csc 编译失败' }

Write-Host '[5/6] 组装便携目录' -ForegroundColor Cyan
if (Test-Path $Pkg) { Remove-Item $Pkg -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Pkg | Out-Null
Copy-Item (Join-Path $Dist '元器件物料管理.exe') $Pkg -Force
Copy-Item $Embed (Join-Path $Pkg 'runtime') -Recurse -Force
Copy-Item (Join-Path $Root 'app') (Join-Path $Pkg 'app') -Recurse -Force
Get-ChildItem (Join-Path $Pkg 'app') -Recurse -Directory -Filter __pycache__ |
    Remove-Item -Recurse -Force -EA SilentlyContinue
New-Item -ItemType Directory -Force -Path (Join-Path $Pkg 'data') | Out-Null
$db = Join-Path $Root 'data\parts.db'
if (Test-Path $db) { Copy-Item $db (Join-Path $Pkg 'data\parts.db') -Force }
Copy-Item (Join-Path $Build '使用说明.txt') $Pkg -Force -EA SilentlyContinue

Write-Host '[6/6] 打包 zip' -ForegroundColor Cyan
$zip = Join-Path $Dist '元器件物料管理_便携版.zip'
Remove-Item $zip -Force -EA SilentlyContinue
Compress-Archive -Path $Pkg -DestinationPath $zip -CompressionLevel Optimal

$total = (Get-ChildItem $Pkg -Recurse -File | Measure-Object Length -Sum).Sum
Write-Host ''
Write-Host ("完成: 便携目录 {0:N2} MB,zip {1:N2} MB" -f ($total/1MB), ((Get-Item $zip).Length/1MB)) -ForegroundColor Green
Write-Host "  $Pkg"
Write-Host "  $zip"
