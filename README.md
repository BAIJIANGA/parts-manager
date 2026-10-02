# 元器件物料管理系统

一个跑在**本机浏览器**里的元器件库存 / 出入库 / 项目 BOM 管理系统。
专为「Altium Designer + 立创商城(LCSC)」的工作流设计:**立创编号(C-号)是元件的天然主键**。

**零第三方依赖** —— 只用 Python 标准库,不需要 pip、不需要 Node、不需要数据库服务。

---

## 快速开始

双击:

```
scripts\启动.bat
```

浏览器会自动打开 <http://127.0.0.1:8000/>。关闭那个黑色命令行窗口即停止服务。

> 服务只监听 `127.0.0.1`,**不对外网开放**,因此不需要任何防火墙规则,手机/其他电脑也访问不到。

### 启动脚本如何找 Python

按顺序尝试,用第一个能用的:

1. `venv\Scripts\python.exe`(项目自带)
2. `py -3`(Windows Python 启动器)
3. `python`(PATH 上的)

只要 **Python 3.10 或更高版本**即可。也可以用命令行直接跑:

```
venv\Scripts\python.exe app\server.py --port 8000
```

---

## 功能

### 1. 库存台账
- 元件增删改查,按 **名称 / 立创编号 / 厂家料号 / 厂家 / 封装 / 值** 模糊搜索
- 按品类、库存状态筛选;多种排序
- 库存状态自动标记:**充足 / 偏低(低于安全库存)/ 缺货**
- 点任意一行看详情:仓位分布 + 最近 50 条流水

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
│   ├── server.py          HTTP 服务 + JSON API(标准库 http.server)
│   ├── db.py              SQLite 建表、连接、工具
│   ├── bom.py             Altium BOM 解析与导入、缺料计算
│   ├── xlsx.py            纯标准库 .xlsx 读取器(zipfile + xml)
│   └── static/
│       ├── index.html
│       ├── app.js         前端逻辑(原生 JS,无构建)
│       └── style.css
├── data/
│   ├── parts.db           ← 全部数据就在这一个文件里
│   ├── backups/           每次启动自动快照(保留最近 20 份)
│   └── inbox/             导入过的 BOM 原文件留档
├── scripts/
│   └── 启动.bat
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

- **自动**:每次启动服务时,把 `data/parts.db` 快照到 `data/backups/parts_<时间戳>.db`,自动只留最近 20 份
- **手动**:直接复制 `data/parts.db` 即可(单文件数据库,不需要停止服务也能复制)
- **恢复**:把备份文件改名为 `data/parts.db` 覆盖过去,重启服务
- `data/` 已在 `.gitignore` 里,**数据库不会被提交到版本库**

---

## 常见问题

**端口被占用?**
换端口启动:`venv\Scripts\python.exe app\server.py --port 8010`

**页面打开是空白 / 一直在转?**
先确认命令行窗口里打印出了 `元器件物料管理系统已启动`。若报错,把错误贴出来。

**中文显示成乱码?**
启动脚本已设置 `chcp 65001` 和 `PYTHONUTF8=1`。若仍异常,检查系统区域设置中的「Beta: 使用 Unicode UTF-8 提供全球语言支持」。

**想换台电脑用?**
把整个 `parts-manager` 文件夹复制过去即可。若目标机的 `venv` 不可用,删掉 `venv` 文件夹,装个 Python 3.10+ 就行(零依赖)。

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

## API 一览

```
GET    /api/summary                       总览统计
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
