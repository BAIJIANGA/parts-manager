# 元器件物料管理系统

一个**本地原生窗口**的元器件库存 / 出入库 / 项目 BOM 管理程序。
专为「Altium Designer + 立创商城(LCSC)」的工作流设计:**立创编号(C-号)是元件的天然主键**。

**零第三方依赖** —— 只用 Python 标准库(含 tkinter),不需要 pip、不需要 Node、
不需要数据库服务、**不开端口、不连网络**。

---

## 快速开始

双击 `dist\元器件物料管理\元器件物料管理.exe` 即可。

**不需要装 Python,不需要装任何东西,不弹命令行窗口,也不打开浏览器。**
整个文件夹拷到 U 盘或别的电脑照样能跑 —— 因为内嵌了 Python 运行时(`runtime\`)。

界面是原生 Tkinter 窗口,五个标签页:元件库存 / 出入库 / 项目 BOM / 流水 / 仓位。
关掉窗口进程就退出,不残留后台。

> 程序**完全不碰网络**:没有任何监听端口,没有 HTTP 服务,不需要防火墙规则。

### 桌面版是怎么复用后端逻辑的

`app/gui.py` **不重写任何业务逻辑**,而是直接调用 `app/server.py` 里的路由处理函数。
能这么做的原因:`route` 装饰器原样返回函数,而 `Ctx` 只读 `query / body / upload / con`,
完全不依赖 HTTP。

```python
ctx = server.Ctx(None, con, {"state": "out"}, {}, None)   # 合成一个请求上下文
status, payload = server.list_components(ctx, None)       # 直接当普通函数调用
```

所以桌面版和(仍然保留的)网页版**语义逐字一致**,不存在两套实现慢慢走偏的问题。

---

## 从源码跑(开发用)

```
venv\Scripts\python.exe app\gui.py        # 桌面版(机器上要有带 tkinter 的 Python)
venv\Scripts\python.exe app\server.py     # 网页版(可选,仅对照用)
```

只要 **Python 3.10 或更高版本**,零第三方依赖。

### 自检

```
python build\test_gui.py
```

在 `data\parts.db` 的**副本**上跑:把整个窗口、5 个标签页、3 个弹窗真的构造并渲染一遍,
再跑一遍代表性的数据操作(增改删、入库/出库/盘点/移库、超额出库、按流水重建校验)。
结果写到 `dist\gui_selftest.txt`。**不会动你的真实数据。**

### 重新打包便携版

```
powershell -NoProfile -ExecutionPolicy Bypass -File build\build.ps1
```

产出 `dist\元器件物料管理\`(便携目录)和 `dist\元器件物料管理_便携版.zip`(可分发)。

> 必须带 `-ExecutionPolicy Bypass`,否则 Windows 会拒绝执行
> (`running scripts is disabled on this system`)。

三个容易踩的坑,脚本里都已经处理掉了:

- **官方 embeddable 包不含 tkinter**,而这个程序是原生窗口程序,必须有它。
  脚本会从本机已装的 Python 里把 `_tkinter.pyd`、`tcl86t.dll` / `tk86t.dll`、
  `tcl\` 脚本库和 `Lib\tkinter` 搬进内嵌运行时,并把 `Lib` 写进 `python312._pth`。
  装完还会**当场 import tkinter 开一次窗口**做验证,不通过就直接报错终止。
- **启动器 `build\Launcher.cs` 用 Windows 自带的 `csc.exe` 编译**,
  不依赖 Visual Studio,也不需要 .NET SDK。
- **脚本本身必须是 UTF-8 with BOM**。Windows PowerShell 5.1 会把没有 BOM 的 UTF-8
  当 ANSI 读,中文一乱就变成语法错误。

---

## 功能

### 1. 库存台账(首页)

**一级只显示大类**(电阻 / 电容 / 电感 / 发光二极管 / 芯片 IC / 连接器 …),
点开三角才进二级看具体型号 —— 所以首页永远只有几行,不会被几百个型号一次铺满。
默认全部折叠。

导入过 BOM 的项目会额外挂一个以项目号命名的一级节点(如 `【项目】Board1_PCB1`),
该项目的元件挂在它下面。**同一个元件会同时出现在「它的大类」和「所属项目」下面**,
这是故意的两个视角:大类回答「我有什么」,项目回答「这批料是给谁配的」。

- 大类顺序按常见元件类排(电阻 / 电容 / 电感 排最前),库里新出现的品类排在后面
- 一级节点不带数量;工具栏右侧统一显示「共 N 种元件 / M 个大类」
- 「全部展开 / 全部折叠」一键切换;展开状态在刷新后会被记住
- 按 **名称 / 立创编号 / 厂家料号 / 厂家 / 封装 / 值 / 品类** 模糊搜索
- 按大类、库存状态筛选;多种排序(排序作用于大类内部的型号)
- 库存状态自动标记:**充足 / 偏低(低于安全库存)/ 缺货**,缺货行整行标红
- 选中任意元件看详情:仓位分布 + 最近 50 条流水
- 右键元件可直接开单(入库 / 出库 / 盘点 / 移库);双击大类展开或收起

### 2. 出入库
四种动作,全部**事务化**并留痕:

| 动作 | 含义 | 数量填什么 |
|---|---|---|
| **入库 IN** | 增加库存 | 入库数量 |
| **出库 OUT** | 减少库存 | 出库数量 |
| **盘点 ADJUST** | 修正为实际清点值 | **实际数到的数量** |
| **移库 TRANSFER** | 仓位间移动 | 移动数量 |

- 禁止负库存(超量出库会被拒绝并提示当前数量)
- 仓位管理:随时增删仓位(如 `A-01-02` 表示 A柜-01层-02格)
- 低库存 / 缺货预警清单

### 3. 项目 BOM
- **导入 Altium 导出的 .xlsx** → 自动建元件 + 展开位号 + 生成项目 BOM
- **缺料清单** = 需求 − 现有库存,实时联动(入库后立刻重算)
- **按 BOM 领料**:一键批量出库,库存不足的料号会被跳过并列明原因
- 导出缺料清单 CSV(带 BOM 头,Excel 直接打开不乱码)

### 4. 流水
所有出入库记录,含时间、动作、数量、仓位、项目、操作人。

---

## 目录结构

```
parts-manager/
├── app/
│   ├── gui.py             ★ 桌面版界面(原生 tkinter),程序入口
│   ├── server.py          业务逻辑 + JSON API(网页版用;桌面版直接复用它的处理函数)
│   ├── db.py              SQLite 建表、连接、工具
│   ├── bom.py             Altium BOM 解析与导入、缺料计算
│   ├── xlsx.py            纯标准库 .xlsx 读取器(zipfile + xml)
│   └── static/
│       ├── index.html
│       ├── app.js         前端逻辑(原生 JS,无构建)
│       └── style.css
├── data/
│   ├── parts.db           ← 全部数据就在这一个文件里
│   ├── backups/           每次启动 / 每次删除前自动快照(保留最近 20 份)
│   └── inbox/             导入过的 BOM 原文件留档
├── build/                 ← 便携版构建源
│   ├── Launcher.cs        启动器(编译成 exe)
│   ├── make_icon.py       图标生成
│   ├── build.ps1          一键打包
│   ├── test_gui.py        自检(逻辑 + 界面构造,不动真实数据)
│   └── 使用说明.txt        随包一起发的说明
├── dist/                  构建产物(不进版本库)
│   ├── python-embed/      内嵌 Python 3.12 运行时(已注入 tkinter)
│   └── 元器件物料管理/      ← 最终便携目录,双击里面的 exe
├── scripts/
│   └── 启动.bat           旧网页版的开发启动脚本(桌面版不需要它)
├── venv/                  项目自带 Python 环境
├── .gitignore
└── README.md
```

---

## 数据模型

```
component     元件主数据(lcsc_pn 唯一,缺失时回退 mpn)
location      仓位
stock         库存余额 = 元件 × 仓位
movement      出入库流水(只增不改)
project       项目
project_bom   项目 BOM = 项目 × 元件 × 需求数量 + 位号
```

设计要点:

- **余额与流水分离**。流水是原始记录,余额是物化结果。设 `POST /api/rebuild` 可按流水顺序重放并重建余额,用来校验两者是否一致(实测差异恒为 0)。
- **仓位相关参数放 JSON**(`component.params`),比如 `{"耐压":"50V","精度":"±1%"}`,避免为每个品类加列。

---

## 导入 BOM 的要求

支持 Altium Designer 直接导出的 `.xlsx`,列名**不区分大小写、自动识别中英文**。

必需两列(缺了会明确报错):

| 规范字段 | 可接受的表头写法 |
|---|---|
| 位号 | `Designator` / `Designators` / `位号` / `元件标号` |
| 数量 | `Quantity` / `Qty` / `数量` / `用量` |

可选列:

| 规范字段 | 可接受的表头写法 |
|---|---|
| 厂家料号 | `Manufacturer Part` / `MPN` / `厂家料号` |
| 厂家 | `Manufacturer` / `厂家` / `制造商` |
| 立创编号 | `Supplier Part` / `供应商料号` / `立创编号` |
| 供应商 | `Supplier` / `供应商` |
| 注释 / 值 / 封装 | `Comment` / `Value` / `Footprint` / `注释` / `值` / `封装` |

导入时的行为:

- 立创编号优先做识别键;**没有立创编号就回退用厂家料号**
- 已有元件只**补空字段**,不覆盖你手工改过的值
- 位号数与数量不一致会给出警告,以数量为准
- 品类按 **封装特征 → 位号前缀** 自动推断(如 `USB1` / `CONN-*` → 连接器,`RES-ADJ` → 电位器)

> 已用你桌面上 `BOM_Board1_PCB1_2026-10-01.xlsx` 实测:20 行读取完整、
> 19 个元件、位号 1~19 齐全、总用量 39、**0 条警告**。

---

## 备份与恢复

- **自动**:每次启动程序时,以及每次**删除类操作之前**,把 `data/parts.db` 快照到
  `data/backups/parts_<时间戳>.db`,只留最近 20 份
- **手动**:直接复制 `data/parts.db`(单文件数据库,程序开着也能复制),或点「文件 → 备份数据库」
- **恢复**:关掉程序,把备份文件改名为 `data/parts.db` 覆盖过去,再打开
- `data/` 已在 `.gitignore` 里,**数据库不会被提交到版本库**

---

## 常见问题

**双击 exe 没反应 / 弹了个报错框?**
打开 `data\gui.log` 看最后几十行,原因基本都写在里面。日志里若**没有**
「`[就绪] 窗口已显示`」这一行,说明窗口根本没起来,那几行 traceback 就是答案。

**提示"程序已经在运行了"?**
同一时刻只允许开一个窗口(避免两个窗口改同一个数据库)。到任务栏找那个窗口,
或先在任务管理器里结束 `pythonw.exe`。

**关掉窗口后任务管理器里还有 python.exe?**
正常不会。若有,确认不是在跑**旧版网页版**(`app\server.py`)——那一版靠心跳超时退出。

**中文显示成乱码?**
桌面版窗口用系统字体,不涉及编码;启动器给子进程设了 `PYTHONUTF8=1`,
所以 `data\gui.log` 也是 UTF-8。若记事本打开日志像乱码,换 VS Code 打开即可。

**想换台电脑用?**
把 `dist\元器件物料管理\` 整个文件夹拷过去,双击 exe 即可,**目标机什么都不用装**。
exe 必须和 `runtime\`、`app\` 两个文件夹放在一起,单独拷 exe 是跑不起来的。
(从源码跑才需要目标机有 Python 3.10+,且必须带 tkinter。)

**`data/tmp/` 里有个删不掉的空目录?**
那是最初在 DSH 沙箱里试装 Python 包时留下的痕迹(受限进程建的目录带保护性权限项)。与本系统无关,系统不使用该目录,忽略即可。

---

## 设计说明

### 为什么零第三方依赖?

原计划用 FastAPI + Uvicorn,但实测发现:本机环境下 `pip` 无法工作——
CPython 的 `tempfile.mkdtemp()` 在 Windows 上会创建**不继承父目录权限**的目录,
而 DSH 沙箱的写入许可是靠继承传递的,于是 pip 建临时目录这一步必然被拒。

改用纯标准库后:不需要 pip、不需要虚拟环境装包、不需要任何权限放宽,
而且顺带得到一个能跑在**任何 Python 3.10+** 上的自包含程序。

### `xlsx.py` 为什么自己写?

为了不依赖 `openpyxl`。xlsx 本质是一个 zip 包着若干 XML,
BOM 这种规整表格只需要读共享字符串表和单元格值,约 150 行足够。
已与 `openpyxl` 逐值比对,结果一致(且更干净:不会多出一个全空的尾行)。

---

## 处理函数一览

下表是 `app/server.py` 里注册的路由。**桌面版不经过 HTTP**,而是合成一个 `Ctx`
直接调用这些函数(路径参数则用 `gui._Match` 顶替正则匹配对象);网页版才把它们挂到 `/api/*`。

```
GET    /api/summary    ← 桌面版就是 server.summary(ctx, None)
GET    /api/components?q=&category=&state=&sort=&limit=&offset=
POST   /api/components                    新建元件
GET    /api/components/{id}               元件详情(含仓位分布与流水)
PUT    /api/components/{id}               更新元件
DELETE /api/components/{id}?force=1       删除元件
GET    /api/meta                          品类/封装/厂家/仓位选项
POST   /api/locations                     新建仓位
DELETE /api/locations/{id}                删除仓位
POST   /api/stock/move                    入库/出库/盘点/移库
GET    /api/movements?component_id=&project_id=&kind=&limit=
GET    /api/lowstock                      低库存与缺货
POST   /api/rebuild                       按流水重建余额并比对
GET    /api/projects                      项目列表
POST   /api/projects                      新建空项目
DELETE /api/projects/{id}                 删除项目
GET    /api/projects/{id}/bom             项目 BOM + 缺料清单
POST   /api/projects/{id}/pick            按 BOM 批量领料
POST   /api/bom/preview                   上传 BOM 预览(不落库)
POST   /api/bom/import                    上传 BOM 导入
```
