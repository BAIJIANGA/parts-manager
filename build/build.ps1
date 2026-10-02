# 一键重建便携免安装桌面版。用法(在 parts-manager 目录下):
#     powershell -NoProfile -ExecutionPolicy Bypass -File build\build.ps1
#
# 产物只有一个,就是一个可以直接双击运行的目录:
#     dist\元器件物料管理\      ← 里面的 exe 双击即用;整个目录拷 U 盘就行
#
# 构建中间产物(内嵌 Python、编译出的启动器、自检输出)全部放 build\cache\,
# 不再往 dist\ 里堆东西,也不再打 zip —— dist 里那个目录本身就是解压好的成品。
$ErrorActionPreference = 'Stop'

$Root  = Split-Path -Parent $PSScriptRoot
$Build = $PSScriptRoot
$Cache = Join-Path $Build 'cache'
$Dist  = Join-Path $Root 'dist'
$Pkg   = Join-Path $Dist '元器件物料管理'
$Embed = Join-Path $Cache 'python-embed'
$Csc   = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'

$PY_VER = '3.12.10'
$PY_URL = "https://www.python.org/ftp/python/$PY_VER/python-$PY_VER-embed-amd64.zip"

function Step($n, $t) { Write-Host "[$n] $t" -ForegroundColor Cyan }
New-Item -ItemType Directory -Force -Path $Cache | Out-Null

# ---------------------------------------------------------------- 1
Step '1/6' '清理旧版遗留在 dist\ 里的中间产物'
# 早期版本把内嵌运行时、编译出的 exe、zip 也放在 dist\,和真正的产物混在一起,
# 一眼看不出哪个才是该双击的东西。这里只清【构建脚本自己产生过】的名字,
# 绝不碰用户自己解压出来的目录(那里面可能有正在运行的程序和数据库)。
foreach ($n in @('python-embed', '元器件物料管理.exe', 'LauncherDiag.exe',
                 'app.ico', 'selftest.db', 'gui_selftest.txt')) {
    $p = Join-Path $Dist $n
    if (Test-Path -LiteralPath $p) {
        Remove-Item -LiteralPath $p -Recurse -Force -EA SilentlyContinue
        Write-Host "  清掉 dist\$n" -ForegroundColor DarkGray
    }
}
Get-ChildItem $Dist -Filter '*.zip' -File -EA SilentlyContinue | ForEach-Object {
    Remove-Item -LiteralPath $_.FullName -Force -EA SilentlyContinue
    Write-Host "  清掉 dist\$($_.Name)" -ForegroundColor DarkGray
}

# ---------------------------------------------------------------- 2
Step '2/6' '检查编译器'
if (-not (Test-Path $Csc)) { throw "找不到 csc.exe: $Csc (.NET Framework 4 应随 Windows 自带)" }

# ---------------------------------------------------------------- 3
Step '3/6' '准备内嵌 Python 运行时,并注入 tkinter'
if (-not (Test-Path (Join-Path $Embed 'python.exe'))) {
    Write-Host "  下载 $PY_URL"
    # PowerShell 的 Invoke-WebRequest 在部分机器上抓不到 python.org(TLS 会被断),
    # 所以交给 Python 的 urllib 去下。
    $venvPy = Join-Path $Root 'venv\Scripts\python.exe'
    if (-not (Test-Path $venvPy)) { $venvPy = 'python' }
    $zip = Join-Path $Cache 'python-embed.zip'
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
} else { Write-Host '  已有,跳过下载' }

# --- tkinter:官方 embeddable 包不含它,而这是个原生窗口程序,必须有 ---
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
装一个官方 Python 3.12(安装时勾选 tcl/tk),或把已有 Python 的路径填进本脚本的候选列表。
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
# 瘦身:Tix 整个用不到,demos/tzdata/msgs 也不影响使用
foreach ($junk in 'tix8.4.3', 'demos', 'tzdata', 'msgs', 'nmake', 'dde1.4', 'reg1.3') {
    Get-ChildItem $tclRoot -Recurse -Directory -EA SilentlyContinue |
        Where-Object { $_.Name -eq $junk } |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -EA SilentlyContinue }
}
Write-Host ("  tcl 脚本库 {0:N2} MB" -f ((Get-ChildItem $tclRoot -Recurse -File | Measure-Object Length -Sum).Sum / 1MB))

$libDir = Join-Path $Embed 'Lib'
New-Item -ItemType Directory -Force -Path $libDir | Out-Null
Remove-Item (Join-Path $libDir 'tkinter') -Recurse -Force -EA SilentlyContinue
Copy-Item (Join-Path $tkSrc 'Lib\tkinter') (Join-Path $libDir 'tkinter') -Recurse -Force

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
Step '4/6' '生成图标'
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
Step '5/6' '编译启动器'
$iconArg = @()
if (Test-Path (Join-Path $Build 'app.ico')) { $iconArg = @("/win32icon:$(Join-Path $Build 'app.ico')") }
& $Csc /nologo /target:winexe /r:System.Windows.Forms.dll @iconArg `
       /out:"$(Join-Path $Cache '元器件物料管理.exe')" (Join-Path $Build 'Launcher.cs')
if ($LASTEXITCODE -ne 0) { throw 'csc 编译失败' }

# ---------------------------------------------------------------- 6
Step '6/6' '组装 dist\元器件物料管理'
# 程序正从目标目录运行的话,重建会删一半卡在占用的文件上,留下一个残缺目录 —— 先拦住
$busy = Get-Process -Name '元器件物料管理', 'pythonw' -EA SilentlyContinue |
        Where-Object { $_.Path -and $_.Path -like "$Pkg\*" }
if ($busy) {
    throw "程序正在从 $Pkg 运行(PID $($busy.Id -join ', '))。请先关掉那个窗口,再重新打包。"
}
# data\ 里是库存、BOM、流水 —— 先挪出来再放回去。绝不能因为重新打包,
# 就把你往运行的包里录进去的数据删掉。
$dataKeep = Join-Path $Cache '_pkgdata'
if (Test-Path $dataKeep) { Remove-Item $dataKeep -Recurse -Force }
if (Test-Path (Join-Path $Pkg 'data')) { Move-Item (Join-Path $Pkg 'data') $dataKeep -Force }
if (Test-Path $Pkg) { Remove-Item $Pkg -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Pkg | Out-Null
Copy-Item (Join-Path $Cache '元器件物料管理.exe') $Pkg -Force
Copy-Item $Embed (Join-Path $Pkg 'runtime') -Recurse -Force
Copy-Item (Join-Path $Root 'app') (Join-Path $Pkg 'app') -Recurse -Force

# 清掉字节码和空目录(注意要放在上面那次 import tkinter 之后,否则 __pycache__ 又生出来)
Get-ChildItem $Pkg -Recurse -Directory -Force |
    Where-Object { $_.Name -eq '__pycache__' -or $_.Name -eq 'routers' } |
    ForEach-Object { Remove-Item -LiteralPath $_.FullName -Recurse -Force -EA SilentlyContinue }

# 窗口图标:gui.py 从 app\static\app.ico 找它
$ico = Join-Path $Build 'app.ico'
if (Test-Path $ico) {
    New-Item -ItemType Directory -Force -Path (Join-Path $Pkg 'app\static') | Out-Null
    Copy-Item $ico (Join-Path $Pkg 'app\static\app.ico') -Force
}
# 便携版是纯桌面程序,网页版的静态资源不带 —— 否则有人双击 index.html 又看到「白屏」
foreach ($web in 'index.html', 'app.js', 'style.css') {
    Remove-Item (Join-Path $Pkg "app\static\$web") -Force -EA SilentlyContinue
}

if (Test-Path $dataKeep) {
    Move-Item $dataKeep (Join-Path $Pkg 'data') -Force
    Write-Host '  保留包里原有的数据库(没有覆盖你录进去的数据)' -ForegroundColor Yellow
} else {
    New-Item -ItemType Directory -Force -Path (Join-Path $Pkg 'data') | Out-Null
    $devDb = Join-Path $Root 'data\parts.db'
    if (Test-Path $devDb) {
        Copy-Item $devDb (Join-Path $Pkg 'data\parts.db') -Force
        Write-Host '  已放入当前数据库,打开就能看到已有的元件'
    }
}
Copy-Item (Join-Path $Build '使用说明.txt') $Pkg -Force -EA SilentlyContinue

# ---------------------------------------------------------------- 汇报
$files = Get-ChildItem $Pkg -Recurse -File
Write-Host ''
Write-Host ("完成:{0:N2} MB,{1} 个文件" -f (($files | Measure-Object Length -Sum).Sum / 1MB), $files.Count) -ForegroundColor Green
Write-Host "  双击这个就能用: $(Join-Path $Pkg '元器件物料管理.exe')" -ForegroundColor Green
$stray = Get-ChildItem $Dist | Where-Object { $_.FullName -ne $Pkg }
if ($stray) {
    Write-Host '  注意:dist\ 里还有别的东西(不是本脚本产生的,没动它们):' -ForegroundColor Yellow
    $stray | ForEach-Object { Write-Host "    $($_.Name)" -ForegroundColor Yellow }
}
Write-Host ''
Write-Host '自检(不碰你的数据,会复制一份数据库):  python build\test_gui.py' -ForegroundColor DarkGray



