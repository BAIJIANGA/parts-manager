# -*- coding: utf-8 -*-
# 一键重建便携免安装桌面版。用法(在 parts-manager 目录下):
#     pwsh -File build\build.ps1
#
# 产物:
#     dist\元器件物料管理\            便携目录(双击里面的 exe 即可用)
#     dist\元器件物料管理_便携版.zip   可直接分发/拷贝到 U 盘
#
# 关键点:官方 embeddable 包【不含 tkinter】,而这个程序是原生窗口程序,
# 必须有 tkinter。所以下面第 3 步会从本机已装的 Python 里把 tkinter 的
# 原生部分(_tkinter.pyd + tcl/tk 的 dll + tcl 脚本库 + Lib\tkinter)搬过来。
$ErrorActionPreference = 'Stop'

$Root  = Split-Path -Parent $PSScriptRoot
$Dist  = Join-Path $Root 'dist'
$Build = $PSScriptRoot
$Pkg   = Join-Path $Dist '元器件物料管理'
$Embed = Join-Path $Dist 'python-embed'
$Csc   = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'

$PY_VER = '3.12.10'
$PY_URL = "https://www.python.org/ftp/python/$PY_VER/python-$PY_VER-embed-amd64.zip"

function Step($n, $t) { Write-Host "[$n] $t" -ForegroundColor Cyan }

# ---------------------------------------------------------------- 1
Step '1/7' '检查编译器'
if (-not (Test-Path $Csc)) { throw "找不到 csc.exe: $Csc (.NET Framework 4 应随 Windows 自带)" }

# ---------------------------------------------------------------- 2
Step '2/7' '准备内嵌 Python 运行时'
if (-not (Test-Path (Join-Path $Embed 'python.exe'))) {
    Write-Host "  下载 $PY_URL"
    # 注意:PowerShell 的 Invoke-WebRequest 在部分机器上抓不到 python.org
    # (TLS 握手会被断),所以交给 Python 的 urllib 去下。
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
print('  已解压到', out)
"@ $PY_URL $zip $Embed
    Remove-Item $zip -Force -EA SilentlyContinue
} else { Write-Host '  已有,跳过' }

# ---------------------------------------------------------------- 3
Step '3/7' '注入 tkinter(原生窗口的关键)'
$tkSrc = $null
foreach ($c in @(
        (Join-Path $env:USERPROFILE '.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe'),
        (Join-Path $Root 'venv\Scripts\python.exe'),
        'python')) {
    if ($c -eq 'python' -or (Test-Path $c)) {
        $base = & $c -c "import sys, tkinter; print(sys.base_prefix)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $base) { $tkSrc = $base.Trim(); break }
    }
}
if (-not $tkSrc) {
    throw @"
找不到带 tkinter 的 Python。
装一个官方 Python 3.12(安装时勾选 tcl/tk)即可,或者把已有 Python 的路径
填进这个脚本的候选列表。
"@
}
Write-Host "  tkinter 来源: $tkSrc"

$pydSrc = Join-Path $tkSrc 'DLLs\_tkinter.pyd'
if (-not (Test-Path $pydSrc)) { throw "在 $tkSrc\DLLs 里找不到 _tkinter.pyd" }
Copy-Item $pydSrc $Embed -Force

$tclDll = Get-ChildItem (Join-Path $tkSrc 'DLLs') -Filter 'tcl8*t.dll' -EA SilentlyContinue | Select-Object -First 1
$tkDll  = Get-ChildItem (Join-Path $tkSrc 'DLLs') -Filter 'tk8*t.dll'  -EA SilentlyContinue | Select-Object -First 1
if (-not $tclDll -or -not $tkDll) { throw "在 $tkSrc\DLLs 里找不到 tcl/tk 的 dll" }
Copy-Item $tclDll.FullName $Embed -Force
Copy-Item $tkDll.FullName $Embed -Force
Write-Host "  $($tclDll.Name) / $($tkDll.Name)"

$tclRoot = Join-Path $Embed 'tcl'
Remove-Item $tclRoot -Recurse -Force -EA SilentlyContinue
New-Item -ItemType Directory -Force -Path $tclRoot | Out-Null
foreach ($d in (Get-ChildItem (Join-Path $tkSrc 'tcl') -Directory -EA SilentlyContinue |
                Where-Object { $_.Name -match '^(tcl|tk)8' })) {
    Copy-Item $d.FullName (Join-Path $tclRoot $d.Name) -Recurse -Force
}
# 瘦身:Tix 整个用不到,demos/tzdata/msgs 也都不影响使用
foreach ($junk in 'tix8.4.3', 'demos', 'tzdata', 'msgs', 'nmake', 'dde1.4', 'reg1.3') {
    Get-ChildItem $tclRoot -Recurse -Directory -EA SilentlyContinue |
        Where-Object { $_.Name -eq $junk } |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -EA SilentlyContinue }
}
$tclMB = [math]::Round((Get-ChildItem $tclRoot -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 2)
Write-Host "  tcl 脚本库 $tclMB MB"

$libDir = Join-Path $Embed 'Lib'
New-Item -ItemType Directory -Force -Path $libDir | Out-Null
Remove-Item (Join-Path $libDir 'tkinter') -Recurse -Force -EA SilentlyContinue
Copy-Item (Join-Path $tkSrc 'Lib\tkinter') (Join-Path $libDir 'tkinter') -Recurse -Force
Get-ChildItem (Join-Path $libDir 'tkinter') -Recurse -Directory -Force -EA SilentlyContinue |
    Where-Object { $_.Name -eq '__pycache__' } |
    ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -EA SilentlyContinue }

# ._pth 决定 import 去哪找 —— 必须把 Lib 加进去,tkinter 才 import 得到
@"
python312.zip
.
Lib

# Uncomment to run site.main() automatically
#import site
"@ | Set-Content -LiteralPath (Join-Path $Embed 'python312._pth') -Encoding ASCII

$probe = & (Join-Path $Embed 'python.exe') -c "import tkinter; r=tkinter.Tk(); r.withdraw(); r.destroy(); print('tkinter', tkinter.TkVersion)" 2>&1
if ($LASTEXITCODE -ne 0) { throw "内嵌运行时里 tkinter 不可用:`n$probe" }
Write-Host "  $probe   <- 运行时自检通过" -ForegroundColor Green

# ---------------------------------------------------------------- 4
Step '4/7' '生成图标'
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

# ---------------------------------------------------------------- 5
Step '5/7' '编译启动器'
$iconArg = @()
if (Test-Path (Join-Path $Build 'app.ico')) { $iconArg = @("/win32icon:$(Join-Path $Build 'app.ico')") }
& $Csc /nologo /target:winexe /r:System.Windows.Forms.dll @iconArg `
       /out:"$(Join-Path $Dist '元器件物料管理.exe')" (Join-Path $Build 'Launcher.cs')
if ($LASTEXITCODE -ne 0) { throw 'csc 编译失败' }

# ---------------------------------------------------------------- 6
Step '6/7' '组装便携目录'
if (Test-Path $Pkg) { Remove-Item $Pkg -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Pkg | Out-Null
Copy-Item (Join-Path $Dist '元器件物料管理.exe') $Pkg -Force
Copy-Item $Embed (Join-Path $Pkg 'runtime') -Recurse -Force
Copy-Item (Join-Path $Root 'app') (Join-Path $Pkg 'app') -Recurse -Force
# 清掉字节码和空目录,免得打进包里
Get-ChildItem $Pkg -Recurse -Directory -Force |
    Where-Object { $_.Name -eq '__pycache__' -or $_.Name -eq 'routers' } |
    ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -EA SilentlyContinue }
# 窗口图标:gui.py 从 app\static\app.ico 找它
$ico = Join-Path $Build 'app.ico'
if (Test-Path $ico) {
    New-Item -ItemType Directory -Force -Path (Join-Path $Pkg 'app\static') | Out-Null
    Copy-Item $ico (Join-Path $Pkg 'app\static\app.ico') -Force
}
# 便携版是纯桌面程序,网页版的静态资源不带 —— 否则有人双击 index.html,
# 又会在浏览器里看到那个「白屏」。
foreach ($web in 'index.html', 'app.js', 'style.css') {
    Remove-Item (Join-Path $Pkg "app\static\$web") -Force -EA SilentlyContinue
}
New-Item -ItemType Directory -Force -Path (Join-Path $Pkg 'data') | Out-Null
$db = Join-Path $Root 'data\parts.db'
if (Test-Path $db) { Copy-Item $db (Join-Path $Pkg 'data\parts.db') -Force }
Copy-Item (Join-Path $Build '使用说明.txt') $Pkg -Force -EA SilentlyContinue

# ---------------------------------------------------------------- 7
Step '7/7' '打包 zip'
$zip = Join-Path $Dist '元器件物料管理_便携版.zip'
Remove-Item $zip -Force -EA SilentlyContinue
Compress-Archive -Path $Pkg -DestinationPath $zip -CompressionLevel Optimal

$total = (Get-ChildItem $Pkg -Recurse -File | Measure-Object Length -Sum).Sum
Write-Host ''
Write-Host ("完成: 便携目录 {0:N2} MB,zip {1:N2} MB" -f ($total / 1MB), ((Get-Item $zip).Length / 1MB)) -ForegroundColor Green
Write-Host "  $Pkg"
Write-Host "  $zip"
Write-Host ''
Write-Host '自检(不碰你的数据,会复制一份数据库):  python build\test_gui.py' -ForegroundColor DarkGray



