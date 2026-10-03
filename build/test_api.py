# -*- coding: utf-8 -*-
"""业务逻辑自检(不用开界面)。

用一个人为设计好的场景,把「现有 / 需求 / 缺口 / 在途 / 该买 / 能造几块」
全部算出**手算得出的期望值**,再逐个对。任何一处口径改动都会在这里立刻暴露。

场景(所有数字都是刻意凑的,便于手算):
    元件 A  10kΩ 电阻   安全库存 100,单价 0.01
    元件 B  1uF 电容    安全库存 0,  单价 0.05
    元件 C  10kΩ 替代料 安全库存 0    —— 是 A 的替代料
    元件 D  M3 螺丝     免点件
    元件 E  LED(红)    可选件

    项目「测试板」计划做 5 块:
      A 单块 10 个,无损耗          → 总需求 50
      B 单块 2 个,损耗 10%         → 总需求 ceil(2.2 × 5) = 11
      D 单块 4 个,免点件           → 总需求 20(不卡产能)
      E 单块 1 个,可选件           → 总需求 5 (不卡产能,单独统计)

    现有库存: A 30、C 5、B 3
      → A 这一行可用 30 + 5(替代料) = 35
      → A 行能支持 floor(35 / 10)   = 3 块
      → B 行能支持 floor(3 / 2.2)   = 1 块
      → 能造几块 = min(3, 1) = 1
"""
from __future__ import annotations

import io
import os
import sys

ROOT = sys.argv[1]
sys.path.insert(0, os.path.join(ROOT, "app"))

import bom       # noqa: E402
import db        # noqa: E402
import footprint  # noqa: E402  ← #10 标准封装识别
import server    # noqa: E402

CACHE = os.path.join(ROOT, "build", "cache")
DB = os.path.join(CACHE, "apitest.db")

out = io.StringIO()
def p(*a):
    out.write(" ".join(str(x) for x in a) + "\n")

FAILS = []
def check(label, got, want):
    ok = got == want
    if not ok:
        FAILS.append(f"{label}: 得到 {got!r},期望 {want!r}")
    p(f"  [{'OK ' if ok else 'FAIL'}] {label}: {got!r}" + ("" if ok else f"   期望 {want!r}"))

def close(label, got, want, tol=1e-6):
    ok = abs(float(got) - float(want)) <= tol
    if not ok:
        FAILS.append(f"{label}: 得到 {got!r},期望 {want!r}")
    p(f"  [{'OK ' if ok else 'FAIL'}] {label}: {got!r}" + ("" if ok else f"   期望 {want!r}"))


class _Match:
    def __init__(self, *groups):
        self._groups = groups

    def group(self, i):
        return self._groups[i - 1]


class Boom(Exception):
    pass


def call(fn, query=None, body=None, match=None, upload=None):
    """直接调后端 handler,把 ApiError 变成异常方便断言。"""
    ctx = server.Ctx(None, CON, dict(query or {}), dict(body or {}), upload)
    status, payload = fn(ctx, _Match(*match) if match else None)
    return status, payload


# --------------------------------------------------------------- 搭场景
for suffix in ("", "-journal", "-wal", "-shm"):
    f = DB + suffix
    if os.path.exists(f):
        os.remove(f)
CON = db.connect(DB)
db.init_db(CON)

def mkcomp(name, category, value, min_stock=0, price=0.0, package="0805"):
    _s, r = call(server.create_component, body={
        "name": name, "category": category, "value": value, "package": package,
        "min_stock": min_stock, "unit_price": price})
    return r["id"]

p("【1】建元件")
A = mkcomp("10kΩ R0603", "电阻", "10kΩ", min_stock=100, price=0.01, package="R0603")
B = mkcomp("1uF C0805", "电容", "1uF", min_stock=0, price=0.05, package="C0805")
C = mkcomp("10kΩ R0603 备用", "电阻", "10kΩ", min_stock=0, price=0.012, package="R0603")
D = mkcomp("M3x8 螺丝", "其他", "", min_stock=0, price=0.1)
E = mkcomp("LED 红 0805", "发光二极管", "GL0805UR01", min_stock=0, price=0.2)
check("建了 5 个元件", CON.execute("SELECT COUNT(*) FROM component").fetchone()[0], 5)
check("10kΩ 解析出数值 10000",
      CON.execute("SELECT value_num FROM component WHERE id=?", (A,)).fetchone()[0], 10000.0)
check("1uF 解析出数值 1e-6",
      CON.execute("SELECT value_num FROM component WHERE id=?", (B,)).fetchone()[0], 1e-06)
check("空 value 解析为 NULL",
      CON.execute("SELECT value_num FROM component WHERE id=?", (D,)).fetchone()[0], None)

p("\n【2】建项目(计划 5 块)与 BOM")
_s, r = call(server.create_project, body={"name": "测试板", "qty": 5})
PID = r["id"]

def mkline(cid, qty, **kw):
    _s, r = call(server.add_bom_line, body=dict(component_id=cid, required_qty=qty, **kw),
                 match=(str(PID),))
    return r["id"]

LA = mkline(A, 10)
LB = mkline(B, 2, attrition=10)
LD = mkline(D, 4, consumable=1)
LE = mkline(E, 1, optional=1)
check("BOM 有 4 行", CON.execute(
    "SELECT COUNT(*) FROM project_bom WHERE project_id=?", (PID,)).fetchone()[0], 4)

p("\n【3】加替代料:A 这一行可以用 C 顶")
_s, r = call(server.add_substitute, body={"component_id": C}, match=(str(LA),))
check("替代料已建立", CON.execute("SELECT COUNT(*) FROM bom_substitute").fetchone()[0], 1)

p("\n【4】入库: A 30、C 5、B 3")
for cid, qty in ((A, 30), (C, 5), (B, 3)):
    _s, r = call(server.stock_move, body={
        "kind": "IN", "component_id": cid, "qty": qty, "location": "未分类"})
    check(f"入库 {qty} 成功", r.get("ok", True), True)
check("A 现有 30", CON.execute(
    "SELECT SUM(qty) FROM stock WHERE component_id=?", (A,)).fetchone()[0], 30)

p("\n【5】元件的派生量(COMPONENT_SELECT 的口径)")
_s, comps = call(server.list_components, query={"limit": 0})
by_id = {c["id"]: c for c in comps["items"]}

a = by_id[A]
check("A 现有", a["on_hand"], 30)
check("A 需求 = 10 × 5", a["required"], 50)
check("A 缺口 = 50 − 30", a["deficit"], 20)
check("A 目标 = max(需求50, 安全库存100)", a["target"], 100)
check("A 该买 = 目标100 − 现有30 − 在途0", a["to_order"], 70)
check("A 状态(30 < 安全库存100)是偏低", a["stock_state"], "low")

b = by_id[B]
check("B 需求 = 2 × 5", b["required"], 10)
check("B 该买 = 10 − 3", b["to_order"], 7)

d = by_id[D]
check("D 需求 = 4 × 5(免点件也算需求)", d["required"], 20)
check("D 该买 = 20 − 0", d["to_order"], 20)
check("D 状态是缺货", d["stock_state"], "out")

c = by_id[C]
check("C 不在 BOM 里,需求为 0", c["required"], 0)
check("C 现有 5、没需求,该买 0", c["to_order"], 0)

p("\n【6】can_build:瓶颈决定产量")
rep = __import__("bom").build_report(CON, PID)
lines = {ln["component_id"]: ln for ln in rep["lines"]}
check("A 行总需求", lines[A]["need"], 50)
check("A 行可用 = 现有30 + 替代料5", lines[A]["available"], 35)
check("A 行缺口", lines[A]["gap"], 15)
check("A 行能支持 3 块", lines[A]["line_build"], 3)
check("B 行总需求 = ceil(2.2 × 5)", lines[B]["need"], 11)
check("B 行能支持 1 块", lines[B]["line_build"], 1)
check("D 是免点件,不卡产能", lines[D]["blocking"], False)
check("E 是可选件,不卡产能", lines[E]["blocking"], False)
check("能造几块 = min(3, 1)", rep["can_build"], 1)
check("缺料行数(只算非免点非可选)", rep["shortage_lines"], 2)
check("缺料总数 = 15+8+20+5", rep["shortage_qty"], 48)
check("可选件缺料单独统计", rep["optional_missing"], 1)
check("还没齐套", rep["ready"], False)

p("\n【7】项目列表带出能造几块")
_s, projs = call(server.list_projects)
check("1 个项目", len(projs["items"]), 1)
check("项目计划数量 5", projs["items"][0]["qty"], 5)
check("项目能造 1 块", projs["items"][0]["can_build"], 1)
check("项目缺 2 行", projs["items"][0]["shortage_lines"], 2)

p("\n【8】采购:在途要能抵扣「该买」")
_s, r = call(server.create_purchase, body={
    "component_id": A, "qty": 70, "status": "ordered", "supplier": "立创"})
PO1 = r["id"]
_s, comps = call(server.list_components, query={"limit": 0})
a = {c["id"]: c for c in comps["items"]}[A]
check("A 在途 70", a["on_order"], 70)
check("A 该买变成 0(100 − 30 − 70)", a["to_order"], 0)

p("\n【9】分批到货:收 30,剩下的还在途")
_s, r = call(server.receive_purchase, body={"qty": 30}, match=(str(PO1),))
check("已收 30", r["received"], 30)
check("还剩 40 未到", r["outstanding"], 40)
check("状态仍是已下单(没到齐)", r["status"], "ordered")
_s, comps = call(server.list_components, query={"limit": 0})
a = {c["id"]: c for c in comps["items"]}[A]
check("A 现有变成 60", a["on_hand"], 60)
check("A 在途变成 40", a["on_order"], 40)
check("A 该买 = max(0, 100 − 60 − 40)", a["to_order"], 0)
check("到货写了 1 条入库流水", CON.execute(
    "SELECT COUNT(*) FROM movement WHERE purchase_id=? AND kind='IN'", (PO1,)).fetchone()[0], 1)
check("流水挂上了采购单号", CON.execute(
    "SELECT purchase_id FROM movement WHERE kind='IN' ORDER BY id DESC LIMIT 1").fetchone()[0], PO1)

p("\n【10】收完剩下的 40,状态自动变成已到货")
_s, r = call(server.receive_purchase, body={}, match=(str(PO1),))
check("到齐", r["status"], "arrived")
check("未到数量 0", r["outstanding"], 0)
_s, comps = call(server.list_components, query={"limit": 0})
a = {c["id"]: c for c in comps["items"]}[A]
check("A 现有 100", a["on_hand"], 100)
check("A 在途 0(已到货不算在途)", a["on_order"], 0)
check("A 该买 0", a["to_order"], 0)

p("\n【11】该买清单")
_s, shop = call(server.shopping_list)
ids = {i["id"]: i for i in shop["items"]}
check("清单里有 D(缺 20)", ids.get(D, {}).get("buy_qty"), 20)
check("清单里有 B(缺 7)", ids.get(B, {}).get("buy_qty"), 7)
check("A 已经不缺,不在清单里", A in ids, False)
check("D 的理由说明了项目缺料", "项目缺料 20" in ids[D]["reasons"], True)

p("\n【12】一键把建议采购转成采购单")
want = [{"component_id": i["id"], "qty": i["buy_qty"]} for i in shop["items"]]
_s, r = call(server.create_purchase, body={"items": want, "status": "todo"})
check("按建议建了对应条数的采购单", r["count"], len(want))
_s, r = call(server.list_purchase, query={"status": "todo"})
check("都是「想买」状态", all(i["status_label"] == "想买" for i in r["items"]), True)
_s, comps = call(server.list_components, query={"limit": 0})
d = {c["id"]: c for c in comps["items"]}[D]
check("只是「想买」不算在途", d["on_order"], 0)
check("所以 D 该买仍是 20", d["to_order"], 20)

p("\n【13】层级仓位")
_s, r = call(server.create_location, body={"code": "A柜", "name": "A柜", "structural": 1})
cab = r["id"]
_s, r = call(server.create_location, body={"code": "A-01", "name": "01层", "parent_id": cab})
layer = r["id"]
_s, r = call(server.create_location, body={"code": "A-01-02", "name": "02格", "parent_id": layer})
cell = r["id"]
_s, locs = call(server.list_locations)
paths = {l["id"]: l["path"] for l in locs["items"]}
check("三层路径", paths[cell], "A柜 / 01层 / 02格")
check("A柜有 1 个子仓位", {l["id"]: l for l in locs["items"]}[cab]["children"], 1)

# 把一个元件挪到 02 格
_s, r = call(server.stock_move, body={
    "kind": "TRANSFER", "component_id": B, "qty": 2,
    "location": "未分类", "to_location": "A-01-02"})
check("移库成功", r.get("ok", True), True)
_s, r = call(server.location_contents, match=(str(cell),))
check("02 格里现在有 1 种料", r["kinds"], 1)
check("02 格里数量是 2", r["total_qty"], 2)
_s, r = call(server.location_contents, query={"cascade": 1}, match=(str(cab),))
check("A柜级联往下也能看到那 2 个", r["total_qty"], 2)
_s, r = call(server.location_contents, match=(str(cab),))
check("A柜本身不装东西(只有子仓位装)", r["total_qty"], 0)

p("\n【14】仓位删除要挡住")
try:
    call(server.delete_location, match=(str(cab),))
    check("有子仓位时删除被拒绝", False, True)
except server.ApiError as exc:
    check("有子仓位时删除被拒绝", "子仓位" in exc.message, True)

p("\n【15】总览")
_s, dash = call(server.dashboard)
check("元件种类 5", dash["kinds"], 5)
check("总数量 100 + 3 + 5", dash["total_qty"], 108)
check("1 个项目", len(dash["projects"]), 1)
check("项目能造 2 块(A 现有 100 支持 10 块,B 现有 3 支持 1 块)",
      dash["projects"][0]["can_build"], 1)
close("库存价值 = 100×0.01 + 3×0.05 + 5×0.012 + 0 + 0", dash["total_value"], 1.21)
check("有最近流水", len(dash["recent"]) > 0, True)

p("\n【16】改 BOM 行的损耗率,能造数跟着变")
_s, r = call(server.update_bom_line, body={"attrition": 0}, match=(str(LB),))
rep = __import__("bom").build_report(CON, PID)
lines = {ln["component_id"]: ln for ln in rep["lines"]}
check("B 行损耗清零后需求 = 2 × 5", lines[B]["need"], 10)
check("B 行能支持 floor(3 / 2) = 1 块", lines[B]["line_build"], 1)

_s, r = call(server.update_bom_line, body={"required_qty": 1}, match=(str(LB),))
rep = __import__("bom").build_report(CON, PID)
lines = {ln["component_id"]: ln for ln in rep["lines"]}
check("B 单块用量降到 1 后能支持 3 块", lines[B]["line_build"], 3)
check("现在瓶颈是 A 的 10 块?不,A 现有 100÷10 = 10 块", lines[A]["line_build"], 10)
check("能造几块 = min(10, 3) = 3", rep["can_build"], 3)

p("\n【17】值区间 / 封装 / 单位 分面筛选")
# 专门造一串跨度大的阻值。关键是要证明区间比的是**数值**不是字符串 ——
# 按字符串比的话 "100k" < "10k",区间筛选就完全错了。
for v in ("100", "1k", "10k", "100k", "1M"):
    mkcomp(f"{v} 电阻", "电阻", f"{v}Ω", package="0603")

_s, r = call(server.list_components, query={"category": "电阻", "value_min": "500",
                                            "value_max": "2000"})
check("500~2000 之间只命中 1k(100 和 10k 都排除)",
      sorted({int(i["value_num"]) for i in r["items"]}), [1000])
p("  ↑ 若是按字符串比,\"100k\" 会落进这个区间")

_s, r = call(server.list_components, query={"category": "电阻", "value_min": "1000",
                                            "value_max": "100000"})
check("1k~100k 的取值集合",
      sorted({int(i["value_num"]) for i in r["items"]}), [1000, 10000, 100000])
check("1M 被上界排除",
      all(int(i["value_num"]) <= 100000 for i in r["items"]), True)

_s, r = call(server.list_components, query={"category": "电阻", "package": "0603"})
check("封装按「包含」匹配:5 个 0603 + 2 个 R0603 = 7", len(r["items"]), 7)
p("  ↑ 用 LIKE 而不是等号,所以搜 0603 也能命中 R0603 这种写法")
check("分面里列出当前范围真有的封装(含 R0603)",
      sorted(r["facets"]["packages"]), ["0603", "R0603"])
check("分面里的单位是 Ω", r["facets"]["units"], ["Ω"])
p("  ↑ 分面只列范围里真有的,不摆一堆选了也搜不到的空选项")

_s, r = call(server.list_components, query={"category": "电容", "unit": "F"})
check("按单位 F 筛出 1uF", len(r["items"]), 1)

_s, r = call(server.list_components,
             query={"category": "发光二极管", "value_min": "1"})
check("值不是数字的元件(丝印式的 GL0805UR01)不会被区间误纳",
      len(r["items"]), 0)

p("\n【18】按仓位盘点(实物清点)")
_s, drawer = call(server.create_location, body={"code": "测试抽屉", "name": "测试抽屉"})
LID = drawer["id"]
call(server.stock_move, body={"kind": "IN", "component_id": A, "qty": 7,
                              "location": "测试抽屉"})
_s, res = call(server.stocktake_location, body={"items": [
    {"component_id": A, "qty": 5},     # 账面 7,实盘 5 -> 有差异
    {"component_id": B, "qty": 0},     # 账面没有、实盘也没有 -> 不算差异
]}, match=(str(LID),))
check("数了 2 项", res["checked"], 2)
check("只有 1 处差异", res["changed"], 1)
check("对得上的不计入差异", res["unchanged"], 1)
check("差异的账面数如实报出", res["diffs"][0]["was"], 7)
check("差异的实盘数如实报出", res["diffs"][0]["now"], 5)
check("库存真的被盘成 5",
      CON.execute("SELECT qty FROM stock WHERE component_id=? AND location_id=?",
                  (A, LID)).fetchone()[0], 5)
mv = CON.execute("SELECT * FROM movement WHERE kind='ADJUST' ORDER BY id DESC "
                 "LIMIT 1").fetchone()
check("写了一条盘点流水", mv["kind"], "ADJUST")
check("流水里写清了「账面 → 实盘」", "账面 7" in mv["note"], True)
check("流水挂在被盘的那个仓位上", mv["location_id"], LID)

n_mv = CON.execute("SELECT COUNT(*) FROM movement").fetchone()[0]
_s, res = call(server.stocktake_location,
               body={"items": [{"component_id": A, "qty": 5}]}, match=(str(LID),))
check("完全一致时不留痕(否则流水会被几百条「没变」淹掉)",
      CON.execute("SELECT COUNT(*) FROM movement").fetchone()[0], n_mv)
check("但也如实报告没有差异", res["changed"], 0)

_s, cab2 = call(server.create_location, body={"code": "测试柜", "structural": 1})
try:
    call(server.stocktake_location, body={"items": []}, match=(str(cab2["id"]),))
    check("分层仓位拒绝盘点(它本身没有实物)", False, True)
except server.ApiError as exc:
    check("分层仓位拒绝盘点(它本身没有实物)", "分层" in exc.message, True)

for bad, why in (([{"component_id": A, "qty": -1}], "实盘负数"),
                 ([{"component_id": A, "qty": "abc"}], "实盘非整数")):
    try:
        call(server.stocktake_location, body={"items": bad}, match=(str(LID),))
        check(f"拒绝{why}", False, True)
    except server.ApiError:
        check(f"拒绝{why}", True, True)

p("\n【19】CSV 版 BOM 导入(KiCad / EasyEDA / 立创 / 中文表头)")


def write_csv(name, text, encoding="utf-8"):
    path = os.path.join(CACHE, name)
    with open(path, "wb") as f:
        f.write(text.encode(encoding))
    return path


def upload_of(path):
    with open(path, "rb") as f:
        return {"filename": os.path.basename(path), "data": f.read()}


# KiCad 的默认导出列名
kicad = write_csv("kicad_bom.csv",
                  "Reference,Value,Footprint,Quantity,MPN,Manufacturer,LCSC\n"
                  "R1 R2,10k,R_0603,2,RC0603FR-0710KL,Yageo,C98220\n"
                  "C1,100nF,C_0402,1,CL05B104KO5NNNC,Samsung,C1525\n"
                  "U1,STM32F103C8T6,LQFP-48,1,STM32F103C8T6,ST,C8734\n")
_s, rep = call(server.bom_import, body={"project_name": "KiCad 板"},
               upload=upload_of(kicad))
check("KiCad 的 CSV 导入了 3 行", rep["bom_lines"], 3)
check("位号用空格分隔也认得(R1 R2)", rep["rows"][0]["designators"], "R1,R2")
check("«LCSC» 这一列当成立创编号", rep["rows"][0]["lcsc_pn"], "C98220")
check("«MPN» 这一列当成厂家料号", rep["rows"][0]["mpn"], "RC0603FR-0710KL")
check("从位号 R 推断出品类是电阻", rep["rows"][0]["category"], "电阻")
check("从位号 U 推断出品类是芯片", rep["rows"][2]["category"], "芯片/IC")

# 立创 / EasyEDA 的列名 + GBK 编码(中文 Excel 另存为 CSV 就是 GBK)
lcsc = write_csv("lcsc_bom.csv",
                 "Quantity,Comment,Designator,Footprint,Manufacturer Part,"
                 "Manufacturer,Supplier Part\n"
                 "2,0.1uF,C3 C4,0603,CC0603KRX7R9BB104,Yageo,C1590\n",
                 encoding="gb18030")
_s, rep = call(server.bom_import, body={"project_name": "立创板"},
               upload=upload_of(lcsc))
check("立创列名的 CSV 也能导", rep["bom_lines"], 1)
check("«Supplier Part» 认成立创编号", rep["rows"][0]["lcsc_pn"], "C1590")
check("«Comment» 认成值",
      CON.execute("SELECT value FROM component WHERE lcsc_pn='C1590'").fetchone()[0],
      "0.1uF")

# 全中文表头 + GBK
zh = write_csv("zh_bom.csv",
               "序号,位号,数量,值,封装,厂家料号,备注\n"
               "1,\"R5,R6\",2,4.7kΩ,0603,RC0603FR-074K7L,手焊\n",
               encoding="gb18030")
_s, rep = call(server.bom_import, body={"project_name": "中文表头板"},
               upload=upload_of(zh))
check("中文表头认得出(位号/数量/值/封装/厂家料号)", rep["bom_lines"], 1)
check("GBK 编码没有乱码", rep["rows"][0]["mpn"], "RC0603FR-074K7L")
check("被引号包住的逗号位号拆得开", rep["rows"][0]["designators"], "R5,R6")
check("值 4.7kΩ 解析成了数值",
      CON.execute("SELECT value_num FROM component WHERE mpn=?",
                  ("RC0603FR-074K7L",)).fetchone()[0], 4700.0)

# 分号分隔(欧洲区域设置的 Excel 会这么存)
semi = write_csv("semi_bom.csv",
                 "Designator;Quantity;Comment;Footprint\nD1;1;LED 红;0805\n")
_s, rep = call(server.bom_import, body={"project_name": "分号板"},
               upload=upload_of(semi))
check("分号分隔的 CSV 也认得", rep["bom_lines"], 1)
check("分号版的值读对了", rep["rows"][0]["name"], "LED 红")
check("名字里不再拼封装(封装有自己的一列,拼进去是重复的)",
      CON.execute("SELECT package FROM component WHERE value='LED 红'"
                  ).fetchone()[0], "0805")
p("  ↑ 分隔符是按第一行里出现最多的那个猜的,不用手工改")

# 预览接口对 CSV 不该去找 sheet
_s, prev = call(server.bom_preview, upload=upload_of(kicad))
check("预览看到的行数和导入一致", prev["line_count"], 3)
check("CSV 没有工作表概念,sheets 为空", prev["sheets"], [])
check("预览会把解析告警带出来", isinstance(prev["warnings"], list), True)

try:
    call(server.bom_preview, upload={"filename": "bom.pdf", "data": b"x"})
    check("不认识的后缀被拒绝", False, True)
except server.ApiError as exc:
    check("不认识的后缀被拒绝,并说清支持哪些", ".csv" in exc.message, True)

bad = write_csv("bad.csv", "hello,world\n1,2\n")
try:
    call(server.bom_preview, upload=upload_of(bad))
    check("没有表头的 CSV 给出可读的报错", False, True)
except server.ApiError as exc:
    check("没有表头的 CSV 给出可读的报错", "表头" in exc.message, True)

p("\n【20】批量入库要用:「这句话对应库里的哪条料」")
_s, r = call(server.resolve_component, query={"q": "C98220"})
check("按立创编号精确命中", r["how"], "exact:lcsc_pn")
check("命中的就是那一条", r["match"]["lcsc_pn"], "C98220")

_s, r = call(server.resolve_component, query={"q": "rc0603fr-0710kl"})
check("按厂家料号精确命中,且大小写不敏感", r["how"], "exact:mpn")

mkcomp("独占匹配测试料 ZQX-777", "其他", "", package="独一无二封装")
_s, r = call(server.resolve_component, query={"q": "ZQX-777"})
check("只有一条模糊命中时才认它", r["how"], "unique")
check("认出来的就是那一条", r["match"]["name"], "独占匹配测试料 ZQX-777")

_s, r = call(server.resolve_component, query={"q": "0805"})
check("多匹配时不给答案(猜错比没猜到更糟)", r["match"], None)
check("how 标成 ambiguous", r["how"], "ambiguous")
check("但把候选带回去让人挑", len(r["candidates"]) > 1, True)
check("候选最多 8 条,不刷屏", len(r["candidates"]) <= 8, True)

_s, r = call(server.resolve_component, query={"q": "库里绝对没有的料 XYZ-404"})
check("完全没有就报 none(可以安全地新建)", r["how"], "none")
check("none 时 match 是 None", r["match"], None)

p("\n【21】撤销:写反向流水,不删记录")
_s, undo = call(server.create_component, body={
    "name": "撤销测试料", "category": "其他", "package": "SOT-23"})
U = undo["id"]


def u_at(loc="测试抽屉"):
    row = CON.execute("SELECT qty FROM stock WHERE component_id=? AND location_id="
                      "(SELECT id FROM location WHERE code=?)", (U, loc)).fetchone()
    return int(row["qty"]) if row else 0


_s, r = call(server.stock_move, body={"kind": "IN", "component_id": U, "qty": 10,
                                      "location": "测试抽屉"})
mv_in = r["movement_id"]
check("入库 10 个", u_at(), 10)

_s, r = call(server.void_movement, match=(str(mv_in),))
check("撤销返回新流水号", r["movement_id"] > mv_in, True)
check("库存回到 0", u_at(), 0)
check("撤销的是哪一笔如实报出", r["voided"], mv_in)

row = CON.execute("SELECT * FROM movement WHERE id=?", (mv_in,)).fetchone()
check("原记录被标成已撤销,但没被删", row["voided"], 1)
back = CON.execute("SELECT * FROM movement WHERE void_of=?", (mv_in,)).fetchone()
check("写了一条反向流水", back is not None, True)
check("反向流水的方向和原来相反", back["kind"], "OUT")
check("反向流水指向被撤销的那一笔", back["void_of"], mv_in)
check("反向流水的说明写清了撤销谁", f"撤销 #{mv_in}" in back["note"], True)
check("反向流水也记下了改动前的数量", back["qty_before"], 10)
check("原记录还在表里(账本只增不删)",
      CON.execute("SELECT COUNT(*) FROM movement WHERE id=?", (mv_in,)).fetchone()[0], 1)

try:
    call(server.void_movement, match=(str(mv_in),))
    check("同一笔不能撤销两次", False, True)
except server.ApiError as exc:
    check("同一笔不能撤销两次", "已经撤销过" in exc.message, True)

try:
    call(server.void_movement, match=(str(back["id"]),))
    check("撤销记录本身不能再被撤销", False, True)
except server.ApiError as exc:
    check("撤销记录本身不能再被撤销", "本身就是一条撤销" in exc.message, True)

_s, r = call(server.stock_move, body={"kind": "IN", "component_id": U, "qty": 4,
                                      "location": "测试抽屉"})
call(server.stock_move, body={"kind": "OUT", "component_id": U, "qty": 3,
                             "location": "测试抽屉"})
check("先入 4 再出 3,剩 1", u_at(), 1)
_s, last = call(server.last_movement)
check("最近一笔就是那条出库", last["movement"]["kind"], "OUT")
check("最近一笔带了中文动作名", last["movement"]["kind_label"], "出库")
call(server.void_movement, match=(str(last["movement"]["id"]),))
check("撤销出库后数量加回去", u_at(), 4)
_s, last = call(server.last_movement)
check("已撤销的那笔不再算「最近可撤销」", last["movement"]["kind"], "IN")
check("取到的正是那笔入库(数量 4)", last["movement"]["qty"], 4)

# 盘点也要能撤销 —— 关键是 qty_before 存下来了
call(server.stocktake_location, body={"items": [{"component_id": U, "qty": 99}]},
     match=(str(LID),))
check("盘点成 99", u_at(), 99)
_s, last = call(server.last_movement)
adj = CON.execute("SELECT qty_before FROM movement WHERE id=?",
                  (last["movement"]["id"],)).fetchone()[0]
check("盘点流水里存了「原来多少」", adj, 4)
call(server.void_movement, match=(str(last["movement"]["id"]),))
check("撤销盘点后回到盘点前的数量", u_at(), 4)

# 移库也要能撤销
_s, drawer2 = call(server.create_location, body={"code": "测试抽屉2"})
LID2 = drawer2["id"]
_s, r = call(server.stock_move, body={"kind": "TRANSFER", "component_id": U, "qty": 3,
                                      "location": "测试抽屉", "to_location": "测试抽屉2"})
check("移走 3 个", (u_at(), u_at("测试抽屉2")), (1, 3))
call(server.void_movement, match=(str(r["movement_id"]),))
check("撤销移库后两边都还原", (u_at(), u_at("测试抽屉2")), (4, 0))

p("\n【22】撤销采购到货:得把采购单的已收数也退回去")
_s, uc = call(server.create_component, body={"name": "撤销采购测试料", "category": "其他"})
_s, po = call(server.create_purchase, body={"component_id": uc["id"], "qty": 20,
                                            "supplier": "测试供应商",
                                            "status": "ordered"})
po_id = po["id"]
_s, res = call(server.receive_purchase, body={"qty": 20, "location": "测试抽屉"},
               match=(str(po_id),))
check("收了 20 个", res["received"], 20)
check("收完状态变成已到货", CON.execute("SELECT status FROM purchase WHERE id=?",
                                        (po_id,)).fetchone()[0], "arrived")
mvid = CON.execute("SELECT id FROM movement WHERE purchase_id=? ORDER BY id DESC",
                   (po_id,)).fetchone()[0]
check("到货流水挂上了采购单号",
      CON.execute("SELECT purchase_id FROM movement WHERE id=?",
                  (mvid,)).fetchone()[0], po_id)
_s, r = call(server.void_movement, match=(str(mvid),))
check("撤销后采购单的已收数退回 0",
      CON.execute("SELECT received FROM purchase WHERE id=?", (po_id,)).fetchone()[0], 0)
check("状态退回「已下单」(货还在路上)",
      CON.execute("SELECT status FROM purchase WHERE id=?", (po_id,)).fetchone()[0],
      "ordered")
check("库存也退回去了",
      CON.execute("SELECT COALESCE(SUM(qty),0) FROM stock WHERE component_id=?",
                  (uc["id"],)).fetchone()[0], 0)

_s, _r = call(server.rebuild)
check("按流水重建后余额仍然对得上",
      CON.execute("SELECT COALESCE(SUM(qty),0) FROM stock WHERE component_id=?",
                  (U,)).fetchone()[0], 4)
check("撤销没有破坏「流水只增不改」:条数只增不减",
      CON.execute("SELECT COUNT(*) FROM movement WHERE void_of IS NOT NULL"
                  ).fetchone()[0] > 0, True)

p("\n【23】查重:找出反复导入 BOM 长出来的重复料")
_s, d1 = call(server.create_component, body={
    "name": "查重电阻甲", "category": "其他", "mpn": "DUP-MPN-001",
    "value": "1kΩ", "package": "0603"})
_s, d2 = call(server.create_component, body={
    "name": "查重电阻乙", "category": "其他", "mpn": "DUP-MPN-001",
    "value": "1kΩ", "package": "0603"})
d1, d2 = d1["id"], d2["id"]
for nm in ("查重同名料甲", "查重同名料乙"):
    call(server.create_component, body={"name": "查重同名料", "category": "其他",
                                        "value": "2k2Ω", "package": "0805"})
_s, d5 = call(server.create_component, body={
    "name": "查重值封装丙", "category": "其他", "value": "47uF", "package": "1206"})
_s, d6 = call(server.create_component, body={
    "name": "查重值封装丁", "category": "其他", "value": "47uF", "package": "1206"})
d5, d6 = d5["id"], d6["id"]

_s, dup = call(server.component_duplicates)
reasons = {g["reason"] for g in dup["groups"]}
check("能按厂家料号找出重复", "mpn" in reasons, True)
check("能按完全同名找出重复", "name" in reasons, True)
check("能按值 + 封装找出重复", "vf" in reasons, True)
check("按「有多确定」排:料号最前(它是硬证据)", dup["groups"][0]["reason"], "mpn")
check("多出来的条数如实统计", dup["extra"] >= 3, True)

g_mpn = [g for g in dup["groups"] if g["reason"] == "mpn" and g["key"] == "DUP-MPN-001"]
check("找到了这个料号的重复组", len(g_mpn), 1)
check("组里两条都在", sorted(i["id"] for i in g_mpn[0]["items"]), sorted([d1, d2]))
check("每条都带上现有库存,好判断哪条才是「正主」",
      "on_hand" in g_mpn[0]["items"][0], True)
check("组里给了一句「怎么判断」的提示", "基本可以确定" in g_mpn[0]["hint"], True)
g_vf = [g for g in dup["groups"] if g["reason"] == "vf" and g["key"] == "47uF / 1206"]
check("值+封装那档也找到了", len(g_vf), 1)
check("可疑档的提示让人自己确认,不替他拍板",
      "请自己确认" in g_vf[0]["hint"], True)

p("\n【24】合并:库存相加、BOM 行并成一条、采购单改指,但流水一条不动")
call(server.stock_move, body={"kind": "IN", "component_id": d1, "qty": 30,
                              "location": "测试抽屉"})
call(server.stock_move, body={"kind": "IN", "component_id": d2, "qty": 12,
                              "location": "测试抽屉"})
call(server.stock_move, body={"kind": "IN", "component_id": d2, "qty": 5,
                              "location": "测试抽屉2"})
_s, po2 = call(server.create_purchase, body={"component_id": d2, "qty": 7,
                                             "status": "todo"})
_s, proj_m = call(server.create_project, body={"name": "合并测试项目甲"})
_s, proj_n = call(server.create_project, body={"name": "合并测试项目乙"})
PM, PN = proj_m["id"], proj_n["id"]
CON.execute("INSERT INTO project_bom(project_id, component_id, required_qty, designators)"
            " VALUES(?,?,?,?)", (PM, d1, 2, "R1 R2"))
CON.execute("INSERT INTO project_bom(project_id, component_id, required_qty, designators)"
            " VALUES(?,?,?,?)", (PM, d2, 3, "R3"))
CON.execute("INSERT INTO project_bom(project_id, component_id, required_qty)"
            " VALUES(?,?,?)", (PN, d2, 5))
# 替代料也要跟着走:同一条 BOM 行上两条重复料的替代关系要合成一条
row_b = CON.execute("SELECT id FROM project_bom WHERE project_id=? AND component_id=?",
                    (PN, d2)).fetchone()[0]
CON.execute("INSERT INTO bom_substitute(bom_id, component_id) VALUES(?,?)", (row_b, d5))
CON.commit()
n_mv_before = CON.execute(
    "SELECT COUNT(*) FROM movement WHERE component_id IN (?,?)", (d1, d2)).fetchone()[0]
n_comp_before = CON.execute("SELECT COUNT(*) FROM component WHERE merged_into IS NULL"
                            ).fetchone()[0]


def q_at(cid, code):
    r = CON.execute("SELECT qty FROM stock WHERE component_id=? AND location_id="
                    "(SELECT id FROM location WHERE code=?)", (cid, code)).fetchone()
    return int(r["qty"]) if r else 0


_s, res = call(server.merge_components, body={"keep": d1, "drop": [d2]})
check("合并报告了并掉的 id", res["dropped"], [d2])
check("保留的那条还叫原来那个名字", res["keep"]["name"], "查重电阻甲")
check("同一仓位的库存相加(30 + 12)", q_at(d1, "测试抽屉"), 42)
check("别的仓位的库存整行挪过来", q_at(d1, "测试抽屉2"), 5)
check("被并的那条库存清零了", q_at(d2, "测试抽屉") + q_at(d2, "测试抽屉2"), 0)
check("被并的那条被标记、不是被删",
      CON.execute("SELECT merged_into FROM component WHERE id=?",
                  (d2,)).fetchone()[0], d1)
check("被并的那条还在表里(历史不能断)",
      CON.execute("SELECT COUNT(*) FROM component WHERE id=?", (d2,)).fetchone()[0], 1)
check("它自己的料号留着,方便以后追「这条原来是什么」",
      CON.execute("SELECT mpn FROM component WHERE id=?", (d2,)).fetchone()[0],
      "DUP-MPN-001")
check("元件总数少了一个",
      CON.execute("SELECT COUNT(*) FROM component WHERE merged_into IS NULL"
                  ).fetchone()[0], n_comp_before - 1)
_s, lst = call(server.list_components, query={"limit": "0"})
check("被并的那条不再出现在列表里",
      any(i["id"] == d2 for i in lst["items"]), False)
check("保留的那条还在列表里", any(i["id"] == d1 for i in lst["items"]), True)
_s, det = call(server.get_component, match=(str(d2),))
check("但按 id 直接查还查得到(能追回去)", det["id"], d2)

check("同一个项目里的两行 BOM 并成了一行",
      CON.execute("SELECT COUNT(*) FROM project_bom WHERE project_id=? AND component_id IN "
                  "(?,?)", (PM, d1, d2)).fetchone()[0], 1)
check("用量相加(2 + 3)",
      CON.execute("SELECT required_qty FROM project_bom WHERE project_id=? AND component_id=?",
                  (PM, d1)).fetchone()[0], 5)
check("位号拼起来了",
      CON.execute("SELECT designators FROM project_bom WHERE project_id=? AND component_id=?",
                  (PM, d1)).fetchone()[0], "R1 R2 R3")
check("另一个项目里只有一条,直接改指过来",
      CON.execute("SELECT component_id FROM project_bom WHERE project_id=?", (PN,)
                  ).fetchone()[0], d1)
check("BOM 里再没有指向被并那条的",
      CON.execute("SELECT COUNT(*) FROM project_bom WHERE component_id=?",
                  (d2,)).fetchone()[0], 0)
check("原来挂在被并那条上的替代料也改指过来了",
      CON.execute("SELECT COUNT(*) FROM bom_substitute WHERE bom_id=? AND component_id=?",
                  (row_b, d5)).fetchone()[0], 1)
check("采购单改指到保留的那条",
      CON.execute("SELECT component_id FROM purchase WHERE id=?",
                  (po2["id"],)).fetchone()[0], d1)
check("流水一条都没少(合并绝不能删历史)",
      CON.execute("SELECT COUNT(*) FROM movement WHERE component_id IN (?,?)",
                  (d1, d2)).fetchone()[0], n_mv_before)
check("被并那条名下的流水仍然挂在它身上(那是真实发生过的)",
      CON.execute("SELECT COUNT(*) FROM movement WHERE component_id=?",
                  (d2,)).fetchone()[0] > 0, True)

for bad_body, why, err in (
        ({"keep": d1, "drop": [d2]}, "已经被并过的不能再并一次", 409),
        ({"keep": d1, "drop": [d1]}, "不能把自己并进自己", 400),
        ({"keep": d1, "drop": []}, "没指定要并掉谁", 400),
        ({"keep": 999999, "drop": [d1]}, "保留的那条不存在", 404)):
    try:
        call(server.merge_components, body=bad_body)
        check(f"拒绝:{why}", False, True)
    except server.ApiError as exc:
        check(f"拒绝:{why}", exc.status, err)

_s, merged = call(server.merged_components)
check("已合并清单里有它", any(i["id"] == d2 for i in merged["items"]), True)
check("已合并清单会告诉你并到哪条去了",
      [i["keep_name"] for i in merged["items"] if i["id"] == d2][0], "查重电阻甲")

# 最要紧的一条:合并之后,再导入带这个料号的 BOM 必须挂到保留的那条上,
# 否则刚合掉的重复下一分钟就长回来了
items_p, _w = bom.rows_to_items([["Designator", "Quantity", "MPN"],
                                 ["R9", "1", "DUP-MPN-001"]])
bom.import_items(CON, items_p, "合并后再导入")
got = CON.execute(
    "SELECT b.component_id FROM project_bom b JOIN project p ON p.id=b.project_id "
    "WHERE p.name='合并后再导入'").fetchone()[0]
check("合并后重新导入,料挂到保留的那条上(不会又长回一条重复)", got, d1)

_s, dup2 = call(server.component_duplicates)
g2 = [g for g in dup2["groups"] if g["reason"] == "mpn" and g["key"] == "DUP-MPN-001"]
check("查重结果里那一组消失了", len(g2), 0)

# ---------------------------------------------------------------- 【25】
# 出入库页靠 list_projects 的 moves 决定给不给项目摆卡片。导入 BOM 会建项目,
# 但那一刻一件货都没动过 —— 摆出来等于把 BOM 当成一条出入库记录,是误导。
p("\n【25】只有真的发生过出入库的项目才算「有过出入库」")
_s, proj3 = call(server.create_project, body={"name": "没动过的项目"})
P3 = proj3["id"]
_s, c3 = call(server.create_component, body={"name": "统计流水测试料", "category": "其他"})
C3 = c3["id"]
_s, lp = call(server.list_projects)
row3 = [i for i in lp["items"] if i["id"] == P3][0]
check("刚建好、一次库都没动过的项目 moves = 0", row3["moves"], 0)
check("last_move_at 也是空的", row3["last_move_at"], None)

_s, mv3 = call(server.stock_move,
               body={"kind": "IN", "component_id": C3, "qty": 4, "project_id": P3})
_s, lp = call(server.list_projects)
row3 = [i for i in lp["items"] if i["id"] == P3][0]
check("真的入过一次库之后 moves = 1", row3["moves"], 1)
check("last_move_at 记下了时间", bool(row3["last_move_at"]), True)

_s, _ = call(server.void_movement, match=(str(mv3["movement_id"]),))
_s, lp = call(server.list_projects)
row3 = [i for i in lp["items"] if i["id"] == P3][0]
# 收进来又撤了 = 没动过。撤销补的那笔反向流水本身也不算,
# 否则「撤销一次」会把项目永远钉在「有过出入库」上
check("撤销掉唯一那笔之后 moves 回到 0", row3["moves"], 0)
check("反向流水不算一次出入库", CON.execute(
    "SELECT COUNT(*) FROM movement WHERE project_id=?", (P3,)).fetchone()[0], 2)

# ---------------------------------------------------------------- 【26】
# 出入库页的「找元件」列表靠这个筛选保持干净:导入 BOM 会凭空建出一堆库存为 0
# 的元件,它们在一个还没开始的出入库流程里全是噪音。
p("\n【26】moved 筛选:只要真的有过出入库的元件")


def live_moves(cid):
    return CON.execute("SELECT COUNT(*) FROM movement WHERE component_id=? "
                       "AND voided=0 AND void_of IS NULL", (cid,)).fetchone()[0]


_s, lc1 = call(server.list_components, query={"moved": "1", "limit": 999})
ids1 = {i["id"] for i in lc1["items"]}
_s, lc0 = call(server.list_components, query={"moved": "0", "limit": 999})
ids0 = {i["id"] for i in lc0["items"]}
_s, lca = call(server.list_components, query={"limit": 999})

check("刚建、一次都没动过的元件不在 moved=1 里", C3 in ids1, False)
check("它出现在 moved=0 里", C3 in ids0, True)
check("moved=1 返回的每一个都真的有流水",
      all(live_moves(i) > 0 for i in ids1), True)
check("moved=0 返回的每一个都真的没有流水",
      all(live_moves(i) == 0 for i in ids0), True)
check("moved=1 和 moved=0 不重不漏,合起来正好是全部元件",
      len(ids1) + len(ids0), len(lca["items"]))
check("两个集合没有交集", bool(ids1 & ids0), False)

_s, c4 = call(server.create_component, body={"name": "moved 筛选测试料", "category": "其他"})
C4 = c4["id"]
_s, lc1b = call(server.list_components, query={"moved": "1", "limit": 999})
check("新料建出来时也不在 moved=1 里", C4 in {i["id"] for i in lc1b["items"]}, False)
_s, _mv4 = call(server.stock_move, body={"kind": "IN", "component_id": C4, "qty": 2})
_s, lc1c = call(server.list_components, query={"moved": "1", "limit": 999})
check("入过一次库之后就进 moved=1 了", C4 in {i["id"] for i in lc1c["items"]}, True)
_s, _ = call(server.void_movement, match=(str(_mv4["movement_id"]),))
_s, lc1d = call(server.list_components, query={"moved": "1", "limit": 999})
check("撤销之后又退出 moved=1(等于没动过)",
      C4 in {i["id"] for i in lc1d["items"]}, False)

# lowstock(「只看缺货」那条路)也要认这个参数,否则一勾缺货筛选就破功
_s, ls1 = call(server.lowstock, query={"moved": "1"})
check("lowstock 也认 moved=1:返回的每一个都真的有流水",
      all(live_moves(i["id"]) > 0 for i in ls1["items"]), True)

# ---------------------------------------------------------------- 【27】
p("\n【27】BOM 品类推断:带依据、带把握,并且支持人工改写")


def cls(designators, fp="", val="", hint=""):
    return bom.classify(designators, fp, val, hint)


# 位号是最传统的判据
check("位号 R1 -> 电阻", cls(["R1"], "0603", "10kΩ")[0], "电阻")
check("位号有依据时把握算「明确」", cls(["R1"], "0603", "10kΩ")[1], "high")

# 这次新增的判据。导出的 BOM 常常连位号都没有,只剩值 ——
# 而值的单位本身就把品类说死了
check("没有位号,靠值 10kΩ 的单位认出电阻", cls([], "0603", "10kΩ")[0], "电阻")
check("值 100nF 的单位 F -> 电容", cls([], "0805", "100nF")[0], "电容")
check("值 4.7uH 的单位 H -> 电感", cls([], "0603", "4.7uH")[0], "电感")
check("靠值认出来也算明确依据,不是瞎猜", cls([], "0805", "100nF")[1], "high")
check("理由里写出了是哪个单位", "F" in cls([], "0805", "100nF")[2], True)
check("值 R47 里的 R 是欧姆位 -> 电阻", cls([], "", "R47")[0], "电阻")

# 认不出来就别装懂
check("值 10k(没有单位)不硬猜", cls([], "0603", "10k")[0], "其他")
check("认不出来时把握是 none", cls([], "0603", "10k")[1], "none")
check("料号形状的值不会被当成标称值", cls([], "", "CH224K")[0], "其他")

# 线索打架 -> 降级,并把冲突写出来让人拍板
_c, _f, _w = cls(["C1"], "0805", "10kΩ")
check("位号说电容、值说电阻时,仍给出一个结果(按位号)", _c, "电容")
check("但把握降级成「要确认」", _f, "low")
check("而且理由里把两条冲突都摆出来",
      ("矛盾" in _w) and ("电容" in _w) and ("电阻" in _w), True)

# 只有弱证据时同样要降级:封装像芯片,但认不出是什么片子
check("光凭 SOT-23 只能猜是芯片", cls([], "SOT-23-5", "")[0], "芯片/IC")
check("弱证据的把握是「要确认」", cls([], "SOT-23-5", "")[1], "low")

# BOM 自带品类列时最权威
check("BOM 自带品类时以它为准", cls([], "0603", "", "钽电容")[0], "钽电容")
check("并说明这个品类来自 BOM 本身", "BOM" in cls([], "0603", "", "钽电容")[2], True)

# 封装强特征压过位号 —— 旧版就有的行为,不能退化成跟着位号跑
check("U3 配接线端子封装 -> 连接器(封装压过位号)",
      cls(["U3"], "KF301-5.0-2P", "")[0], "连接器")
check("发光二极管不会被封装里的数字带成电感",
      cls([], "LED-0805", "")[0], "发光二极管")
check("SOD-123 没有位号也认得出是二极管", cls([], "SOD-123", "")[0], "二极管")

# 规则表产出的品类必须都能在下拉里选到,否则人看见一个选不回来的词
_reach = {c for _k, c in bom.CATEGORY_BY_PREFIX}
_reach |= {c for _k, c in bom.CATEGORY_BY_FOOTPRINT}
_reach |= {"其他", "芯片/IC", "电阻", "电容", "电感"}
check("规则表产出的品类都在 CATEGORIES 里", sorted(_reach - set(bom.CATEGORIES)), [])
check("品类清单没有重复项", len(bom.CATEGORIES), len(set(bom.CATEGORIES)))
_s, _m = call(server.meta)
check("界面拿到的品类选项也包含 BOM 会推断出来的那些",
      sorted(set(bom.CATEGORIES) - set(_m["categories"])), [])

# ---- 复核结果按行号回传,并且真的落库
rev = write_csv("review_bom.csv",
                "Designator,Quantity,Value,Footprint\n"
                "R1,1,10k,F1\n"
                "C1,1,100nF,F2\n"
                ",2,10k,F3\n")
_s, prev = call(server.bom_preview, upload=upload_of(rev))
check("预览把行数报出来", prev["line_count"], 3)
check("预览给了每行的行号(复核结果要按它回传)",
      all(l["source_row"] is not None for l in prev["lines"]), True)
check("预览给了每行的把握",
      [l["confidence"] for l in prev["lines"]], ["high", "high", "none"])
check("把握连中文说法一起给(界面不用自己再抄一份)",
      [l["confidence_label"] for l in prev["lines"]], ["明确", "明确", "认不出"])
check("预览把「有几行要人确认」统计出来了", prev["need_review"], 1)
check("预览把品类选项一起给出来(就一份清单)",
      sorted(set(bom.CATEGORIES) - set(prev["categories"])), [])
check("预览给了每行的依据,人才能判断该不该改",
      all(l["reason"] for l in prev["lines"]), True)
check("认不出来的那行,依据里说了为什么认不出",
      "没有" in prev["lines"][2]["reason"], True)

row_r = prev["lines"][0]["source_row"]
_s, rep = call(server.bom_import,
               body={"project_name": "人工复核板",
                     "categories": {str(row_r): "金属膜电阻"}},
               upload=upload_of(rev))
check("人工改过的品类按行号落库", rep["rows"][0]["category"], "金属膜电阻")
check("同一个值+封装,但行号没改的那行仍用推断结果",
      rep["rows"][1]["category"], "电容")
# 这里必须按 component_id 查,**不能按 name**:这块板里 10k/F1 和 10k/F3 值相同、
# 封装不同,名字都是 10k —— 名字本来就不唯一,拿它当键查会查到另一条去。
# 这恰好就是 issue #1 的成因:名字以前被当成身份用。
def cat_of(comp_id):
    row = CON.execute("SELECT category FROM component WHERE id=?", (comp_id,)).fetchone()
    return row[0] if row else None


check("元件表里存的也确实是改过的那个词",
      cat_of(rep["rows"][0]["component_id"]), "金属膜电阻")
check("人工改过之后不再显示成「认不出」",
      cat_of(rep["rows"][2]["component_id"]), "其他")

# 不改的时候行为不变:不传 categories 就全用推断结果
_s, rep2 = call(server.bom_import, body={"project_name": "不做复核板"},
                upload=upload_of(rev))
check("不传复核结果时照旧用推断结果", rep2["rows"][0]["category"], "电阻")

# ---- 元件**早就存在**时,人工改的品类也必须写进去
# 重新导入同一块板的改版 BOM 必然踩到这条:元件上一版就在库里了。
# 如果这里被 upsert 的「只补空字段,不覆盖已有值」默默丢掉,用户改完点确认、
# 界面提示导入成功、库里却还是老样子 —— 复核这一步就成了会骗人的摆设。
again = write_csv("review_again.csv",
                  "Designator,Quantity,Value,Footprint\n"
                  "R1,1,10k,F1\n")
_s, pa = call(server.bom_preview, upload=upload_of(again))
nm = pa["lines"][0]["name"]
r_a = pa["lines"][0]["source_row"]
check("名字就是值本身,后面不再跟封装", nm, "10k")
# 按身份键定位这一条 —— 库里叫 10k 的不止一条(F1 / F3 两个封装)
cid10k = CON.execute("SELECT id FROM component WHERE identity_key=?",
                     (bom.identity_key("10k", "F1", None, None),)).fetchone()[0]


def cat10k():
    return CON.execute("SELECT category FROM component WHERE id=?",
                       (cid10k,)).fetchone()[0]


cat_before = cat10k()
_s, _r = call(server.bom_import,
              body={"project_name": "人工复核板", "categories": {str(r_a): "合金电阻"}},
              upload=upload_of(again))
cat_after = cat10k()
check("元件已存在时,人工改的品类照样写进去了", cat_after, "合金电阻")
check("而且确实和改之前不一样(说明真的写下去了)", cat_before != cat_after, True)
check("导入报告里显示的也是人工指定的那个",
      _r["rows"][0]["category"], "合金电阻")

# 反过来:没人工复核的时候,不能拿猜的结果去覆盖库里已有的品类
_s, pb = call(server.bom_preview, upload=upload_of(again))
_s, _r2 = call(server.bom_import, body={"project_name": "人工复核板"},
               upload=upload_of(again))
check("没有人工复核时,推断结果不会覆盖已有的品类", cat10k(), "合金电阻")
check("推断出来的确实是另一个品类(电阻),它没被写进去",
      pb["lines"][0]["category"], "电阻")

# 但空着的品类还是该由推断补上 —— 老元件当初没填品类,不能一直空着
CON.execute("UPDATE component SET category='' WHERE id=?", (cid10k,))
_s, _r3 = call(server.bom_import, body={"project_name": "人工复核板"},
               upload=upload_of(again))
check("原来品类是空的,推断结果会补上", cat10k(), "电阻")

# ---------------------------------------------------------------- 【28】
p("\n【28】出库找料:按「值 + 封装」算相似,由人确认")


def mk_raw(name, value, package, category="其他", qty=0):
    _s, r = call(server.create_component, body={
        "name": name, "category": category, "value": value, "package": package})
    cid = r["id"]
    if qty:
        call(server.stock_move, body={"kind": "IN", "component_id": cid, "qty": qty})
    return cid


S_A = mk_raw("相似料 4k7 0603", "4.7kΩ", "0603", "电阻", 5)
S_B = mk_raw("相似料 4k7 0805", "4.7kΩ", "0805", "电阻", 9)
S_C = mk_raw("相似料 4700 0603", "4700", "0603", "其他")
S_D = mk_raw("相似料 100nF 0603", "100nF", "0603", "电容", 3)

_s, sim = call(server.components_similar,
               query={"value": "4.7k", "package": "0603"})
by_id = {i["id"]: i for i in sim["items"]}
check("最像的那一档排在最前面", sim["items"][0]["score"], 90)
check("最像的那个把握写「很可能是同一颗」",
      sim["items"][0]["verdict"], "很可能是同一颗")
check("并且说清像在哪里(值和封装都点出来)",
      sim["items"][0]["match"], "数值相同(写法不同)、封装相同")
check("写法更全的那一颗(4.7kΩ)同样在最高档", by_id[S_A]["score"], 90)
check("只写数值不写单位的(4700)也认得出来", by_id[S_C]["score"], 90)
check("封装不同的同类仍会列出来,但分数更低",
      by_id[S_B]["score"] < by_id[S_A]["score"], True)
check("封装不同时明确提醒封装要自己看",
      by_id[S_B]["verdict"], "值对上了,封装要自己看")
# 这条曾经是错的:100nF 0603 光靠封装凑巧一样就挤进了候选。
# 值才是主判据,值对不上的一律不算候选 —— 列出来纯属噪音。
check("值完全不同的不会因为封装凑巧一样就挤进来", S_D in by_id, False)

# 「单位和值都相同」只在两边都写了单位、而且真的一样时才说。
# 4.7k 和 4700 谁都没写单位,硬说「单位相同」是假话,还会把
# 写得最全的 4.7kΩ 压到后面去
_s, sim_u = call(server.components_similar,
                 query={"value": "4.7kΩ", "package": "0603"})
check("两边都写了同样的单位时才说「值和单位都相同」",
      sim_u["items"][0]["match"], "值和单位都相同、封装相同")
# 值给得全的时候,写得全的那颗要排在只写数值的前面
check("值给全时,写得同样全的那颗排在前面",
      sim_u["items"][0]["id"], S_A)
check("分数从高到低排",
      [i["score"] for i in sim["items"]],
      sorted((i["score"] for i in sim["items"]), reverse=True))
check("每一行都带着「像在哪里」的说明",
      all(i["match"] for i in sim["items"]), True)
check("接口把查询条件回显出来", sim["query"]["value"], "4.7k")
check("接口自带一句「要你自己确认」的说明", "确认" in sim["hint"], True)

# 千分位/单位写法归一化:0.1uF 与 100nF 是同一个值
_s, sim4 = call(server.components_similar, query={"value": "100nF"})
ids4 = {i["id"] for i in sim4["items"]}
check("只给值也能找", S_D in ids4, True)
row_u = CON.execute("SELECT id FROM component WHERE value='0.1uF'"
                    " AND merged_into IS NULL").fetchone()
if row_u:
    check("0.1uF 被认出和 100nF 是同一个值(只是单位写法不同)",
          row_u[0] in ids4, True)

# 只看有库存 —— 出库时没库存的候选帮不上忙
S_E = mk_raw("相似料 4k7 0603 无库存", "4.7kΩ", "0603", "电阻")
_s, sim2 = call(server.components_similar,
                query={"value": "4.7kΩ", "package": "0603", "stocked": "1"})
ids2 = {i["id"] for i in sim2["items"]}
check("勾了「只看有库存的」,没库存的就不出现", S_E in ids2, False)
check("有库存的还在", S_A in ids2, True)
_s, sim3 = call(server.components_similar, query={"value": "4.7kΩ", "package": "0603"})
ids3 = {i["id"] for i in sim3["items"]}
check("不勾的话没库存的也列(入库时可能正想找它)", S_E in ids3, True)
_top = sim3["items"][0]["score"]
_same = [i["on_hand"] > 0 for i in sim3["items"] if i["score"] == _top]
check("分数一样时有库存的排在前面(没库存的帮不上出库)",
      _same, sorted(_same, reverse=True))

# 只给封装时把握只能是「封装一样,值还没对上」—— 不能让人以为这就是那颗料,
# 但也不该只说「有点像」:封装一样至少说明它装得上去。
_s, sim5 = call(server.components_similar, query={"package": "0603"})
check("只给封装时也找得到东西", len(sim5["items"]) > 0, True)
check("但把握只说「封装一样,值还没对上」",
      {i["verdict"] for i in sim5["items"]}, {"封装一样,值还没对上"})
_s, sim6 = call(server.components_similar, query={"package": "R_0603"})
check("封装归一化后 C0805 / c-0805 / 0805 这类写法能对上",
      any(i["id"] == S_A for i in sim6["items"]), True)
check("两个条件都空时什么都不给(没有比对的依据)",
      len(call(server.components_similar, query={})[1]["items"]), 0)
check("limit 参数管用",
      len(call(server.components_similar, query={"package": "0603", "limit": "1"})[1]["items"]), 1)

p("\n【29】出库分配方案:一条 BOM 需求,由几颗库存料来凑")
P29 = call(server.create_project, body={"name": "SELFTEST-分配方案"})[1]["id"]
C_BOM = mk_raw("分配-BOM\u6307\u5b9a\u768468nF", "68nF", "0603", "\u7535\u5bb9")
C_A = mk_raw("分配-68nF-0603", "68nF", "0603", "\u7535\u5bb9", 8)
C_C = mk_raw("分配-68nF-0805", "68nF", "0805", "\u7535\u5bb9", 2)
B29 = call(server.add_bom_line, match=(P29,),
           body={"component_id": C_BOM, "required_qty": 10})[1]["id"]

_s, plan = call(server.project_pick_plan, match=(P29,))
check("接口认得这个项目", plan["project_id"], P29)
line = next(l for l in plan["lines"] if l["bom_id"] == B29)
check("这一行要 10 个", line["need"], 10)
check("还需要 10 个(还没发过料)", line["remaining"], 10)
check("候选正好是那两颗有库存的",
      sorted(c["id"] for c in line["candidates"]), sorted([C_A, C_C]))
check("值+封装都对上的排最前面", line["candidates"][0]["id"], C_A)
check("封装不同的排后面,但也在(它就是用来凑剩下的那 2 个)",
      line["candidates"][1]["id"], C_C)
check("第一颗的把握是「很可能是同一颗」",
      line["candidates"][0]["verdict"], "\u5f88\u53ef\u80fd\u662f\u540c\u4e00\u9897")
check("第二颗会提醒你封装要自己看",
      line["candidates"][1]["verdict"], "\u503c\u5bf9\u4e0a\u4e86,\u5c01\u88c5\u8981\u81ea\u5df1\u770b")
check("BOM 自己那颗没库存,所以不进候选",
      C_BOM in [c["id"] for c in line["candidates"]], False)
check("说了这一屏该怎么用", "凑齐" in plan["hint"], True)

# BOM 自己指定的那颗只要还有库存,必须排最前面 —— 相似度再高也只是「像」
call(server.stock_move, body={"kind": "IN", "component_id": C_BOM, "qty": 5})
_s, plan2 = call(server.project_pick_plan, match=(P29,))
line2 = next(l for l in plan2["lines"] if l["bom_id"] == B29)
check("BOM 自己那颗有库存时排最前面", line2["candidates"][0]["id"], C_BOM)
check("而且标出它就是 BOM 本行指定的料", line2["candidates"][0]["own"], True)

# 替代料:用户设它就是为了「这颗不够时拿那颗顶」,值可能完全不同,
# 只按相似度会漏掉,所以必须单独放进来
C_SUB = mk_raw("分配-\u66ff\u4ee3\u6599", "82nF", "1206", "\u7535\u5bb9", 3)
call(server.add_substitute, match=(B29,), body={"component_id": C_SUB})
_s, plan3 = call(server.project_pick_plan, match=(P29,))
line3 = next(l for l in plan3["lines"] if l["bom_id"] == B29)
check("登记过的替代料也进候选(哪怕值和封装都不一样)",
      C_SUB in [c["id"] for c in line3["candidates"]], True)
check("标明它是替代料,不是靠相似度猜来的",
      next(c["substitute"] for c in line3["candidates"] if c["id"] == C_SUB), True)

# 库里一颗都没有的需求:方案里照样要出现,只是候选是空的 ——
# 从方案里悄悄抹掉的话,用户会以为「这条需求不存在」
C_NONE = mk_raw("分配-\u5e93\u91cc\u6ca1\u6709\u7684", "XF-9999", "NOPE999",
                "SELFTEST-\u65e0\u5e93\u5b58\u7c7b")
B_none = call(server.add_bom_line, match=(P29,),
              body={"component_id": C_NONE, "required_qty": 3})[1]["id"]
_s, plan4 = call(server.project_pick_plan, match=(P29,))
line4 = next(l for l in plan4["lines"] if l["bom_id"] == B_none)
check("库里一颗都凑不出的需求,候选是空的", line4["candidates"], [])
check("但它照样出现在方案里(不能因为没库存就不列)",
      line4["remaining"], 3)

p("\n【30】批量开单:逐行给结果,一行坏不拖累别的行")
C31 = mk_raw("批量-\u5165\u5e93\u6599", "1k\u03a9", "0801", "\u7535\u9631")
C32 = mk_raw("批量-\u51fa\u5e93\u6599", "2k\u03a9", "0802", "\u7535\u9631", 5)
_s, r = call(server.stock_batch, body={"kind": "IN", "items": [
    {"component_id": C31, "qty": 3},
    {"component_id": 999999, "qty": 1},
    {"component_id": C32, "qty": 2},
]})
check("批量入库成功两行", len(r["done"]), 2)
check("失败一行", len(r["failed"]), 1)
check("失败那行说清楚了为什么", "不存在" in r["failed"][0]["reason"], True)
check("成功的那些照样落库",
      CON.execute("SELECT COALESCE(SUM(qty),0) FROM stock WHERE component_id=?",
                  (C31,)).fetchone()[0], 3)
check("总数量只算成功的那几行", r["total_qty"], 5)
check("有一行没成,ok 就得是 False(界面要如实说)", r["ok"], False)

# 出库:目标仓位不够时从别的仓位凑,而且要把「已发料」记到对应的 BOM 需求上
C33 = mk_raw("批量-\u5206\u6563\u5728\u4e24\u5730", "3k\u03a9", "0803", "\u7535\u9631")
call(server.stock_move, body={"kind": "IN", "component_id": C33, "qty": 1,
                              "location": "\u672a\u5206\u7c7b"})
call(server.stock_move, body={"kind": "IN", "component_id": C33, "qty": 4,
                              "location": "SELFTEST-L2"})
P30 = call(server.create_project, body={"name": "SELFTEST-批量出库"})[1]["id"]
B30 = call(server.add_bom_line, match=(P30,),
           body={"component_id": C33, "required_qty": 4})[1]["id"]
_s, outs = call(server.pick_for_project, match=(P30,), body={"items": [
    {"bom_id": B30, "component_id": C33, "qty": 4, "location": "\u672a\u5206\u7c7b"}]})
check("按 BOM 出库整体成功", outs["ok"], True)
check("目标仓位只有 1 个,剩下的自动从别的仓位凑",
      CON.execute("SELECT COALESCE(SUM(qty),0) FROM stock WHERE component_id=?",
                  (C33,)).fetchone()[0], 1)
check("凑的过程拆成了两条流水(仓位是真的动了)",
      len(outs["picked"][0]["movement_ids"]), 2)
check("已发料记到那条 BOM 需求上,不是记到元件上",
      CON.execute("SELECT placed_qty FROM project_bom WHERE id=?",
                  (B30,)).fetchone()[0], 4)
# 注意区别:「缺口 gap」说的是**库里还差多少**(发料不会让库存回来),
# 「还需要 remaining」才是**还要发多少**。发完这 4 个,后者归零、前者照旧。
_s, rep30 = call(server.project_bom, match=(P30,))
l30 = next(l for l in rep30["lines"] if l["bom_id"] == B30)
check("发完之后「还需要」归零", l30["remaining"], 0)
check("但库里的缺口照旧在(发给板子不等于补回库存):需求 4 减掉还剩的 1 个", l30["gap"], 3)

# 入库也带 bom_id:留下的只是「这批货是为哪条需求买的」,不能算成已发料
_s, inb = call(server.stock_batch, body={"kind": "IN", "items": [
    {"component_id": C33, "qty": 4, "bom_id": B30}]})
check("批量入库也留下了来路(为哪条需求买的)",
      CON.execute("SELECT COUNT(*) FROM movement WHERE bom_id=? AND kind='IN'",
                  (B30,)).fetchone()[0], 1)
check("但入库**不算**已发料 —— 货进来不等于发给板子了",
      CON.execute("SELECT placed_qty FROM project_bom WHERE id=?",
                  (B30,)).fetchone()[0], 4)

M30 = outs["picked"][0]["movement_ids"][0]
call(server.void_movement, match=(M30,), body={"operator": "SELFTEST"})
check("撤销一笔出库,已发料跟着退回去",
      CON.execute("SELECT placed_qty FROM project_bom WHERE id=?",
                  (B30,)).fetchone()[0], 3)
check("撤销补的反向流水也记着那条需求",
      CON.execute("SELECT COUNT(*) FROM movement WHERE bom_id=?", (B30,)
                  ).fetchone()[0] >= 4, True)

# ---------------------------------------------------------------- 【31】
p("\n【31】元件身份和显示名拆开了:名字只是名字,匹配另有一套")
# 以前显示名是「值 + 空格 + 封装」,而 find_component() 在没有立创编号 / 厂家料号时
# **就是拿这个名字找同一颗料的** —— 名字同时兼任身份键和显示名。于是「100nF 就是
# 100nF,别带封装」这种纯粹的显示要求,一改就会连带改掉匹配行为:100nF 0603 和
# 100nF 0805 算成同一个名字,下次导入把两颗不同的料静默并成一条,BOM 需求和库存
# 全错,而且不报错、界面上看不出来。这一节就是把这件事钉住。

# ---- 身份键本身
check("同一个值 + 同一个封装 → 同一个键",
      bom.identity_key("100nF", "0603", None, None),
      bom.identity_key("100nF", "0603", None, None))
check("★ 值相同但封装不同 → 必须是两个键(整件事的核心)",
      bom.identity_key("100nF", "0603", None, None)
      != bom.identity_key("100nF", "0805", None, None), True)
check("封装写法不同但归一化后一样 → 还是同一个键",
      bom.identity_key("100nF", "C-0603", None, None),
      bom.identity_key("100nF", "c0603", None, None))
check("立创编号优先,且大小写不敏感",
      bom.identity_key("100nF", "0603", None, "c12345"),
      bom.identity_key("999uF", "9999", None, "C12345"))
check("没有立创编号时厂家料号顶上",
      bom.identity_key("100nF", "0603", "MPN-X", None),
      bom.identity_key("999uF", "9999", "MPN-X", None))
check("值和封装都没有,才退回按名字",
      bom.identity_key(None, None, None, None, "手写的名字"), "nm:手写的名字")
check("什么都判断不出来时给空串 —— 宁可多一条重复,也不要乱并",
      bom.identity_key(None, None, None, None), "")

# ---- 显示名只放值
check("显示名就是值本身,不带封装", bom.build_name("1uF", "MPN1", "C1", "电容"), "1uF")
check("值为空才退回厂家料号", bom.build_name(None, "MPN1", "C1", "电容"), "MPN1")
check("料号也没有就退回立创编号", bom.build_name(None, None, "C1", "电容"), "C1")
check("都没有就退回品类(名字不能为空,name 是 NOT NULL)",
      bom.build_name(None, None, None, "电容"), "电容")

# ---- 同一颗料、两个封装:反复导入必须始终是两条
ident = write_csv("ident_bom.csv",
                  "Designator,Quantity,Value,Footprint\n"
                  "C1,10,9.09k,SELFTEST-A\n"
                  "C2,2,9.09k,SELFTEST-B\n")
_s, ri = call(server.bom_import, body={"project_name": "身份键板"},
              upload=upload_of(ident))
check("两个封装不同的 9.09k 是两条 BOM 行", ri["bom_lines"], 2)
check("值相同、封装不同 → 两条元件,没有并成一条",
      CON.execute("SELECT COUNT(*) FROM component WHERE value='9.09k'"
                  " AND package IN ('SELFTEST-A','SELFTEST-B')").fetchone()[0], 2)
check("两条的名字都是 9.09k(名字只放值)",
      sorted(r[0] for r in CON.execute(
          "SELECT name FROM component WHERE value='9.09k'"
          " AND package IN ('SELFTEST-A','SELFTEST-B')")), ["9.09k", "9.09k"])
check("封装各自留着,靠它区分",
      sorted(r[0] for r in CON.execute(
          "SELECT package FROM component WHERE value='9.09k'"
          " AND package IN ('SELFTEST-A','SELFTEST-B')")),
      ["SELFTEST-A", "SELFTEST-B"])

# ★ 再导一遍 —— 这才是真会出事的地方:老代码在这里把 B 并进 A
_s, ri2 = call(server.bom_import, body={"project_name": "身份键板"},
               upload=upload_of(ident))
check("★ 同一份 BOM 再导一遍,仍然只有两条元件(没被静默合并)",
      CON.execute("SELECT COUNT(*) FROM component WHERE value='9.09k'").fetchone()[0], 2)
check("再导一遍,两条 BOM 行都还在", ri2["bom_lines"], 2)
check("两条需求各自挂在自己的元件上,没有指到同一个",
      CON.execute("SELECT COUNT(DISTINCT component_id) FROM project_bom b"
                  " JOIN component c ON c.id = b.component_id"
                  " WHERE c.value='9.09k'").fetchone()[0], 2)

# ---- 老数据迁移:只改能确定是自动生成的名字
old_id = CON.execute(
    "INSERT INTO component(name, category, value, package) VALUES(?,?,?,?)",
    ("8.08k 0603", "电阻", "8.08k", "0603")).lastrowid
CON.commit()
db.backfill_identity(CON)
check("迁移给没算过键的老行补上了身份键",
      CON.execute("SELECT identity_key FROM component WHERE id=?",
                  (old_id,)).fetchone()[0],
      bom.identity_key("8.08k", "0603", None, None))
check("「值 封装」式的旧名字被修成只有值",
      CON.execute("SELECT name FROM component WHERE id=?", (old_id,)).fetchone()[0],
      "8.08k")

hand_id = CON.execute(
    "INSERT INTO component(name, category, value, package) VALUES(?,?,?,?)",
    ("我自己起的名", "电阻", "7.07k", "0603")).lastrowid
CON.commit()
db.backfill_identity(CON)
check("人手起的名字一律不动 —— 猜错等于把人家的命名默默抹掉",
      CON.execute("SELECT name FROM component WHERE id=?", (hand_id,)).fetchone()[0],
      "我自己起的名")
check("但它的身份键照样补上", CON.execute(
    "SELECT identity_key FROM component WHERE id=?", (hand_id,)).fetchone()[0],
    bom.identity_key("7.07k", "0603", None, None))

# ---- 迁移是幂等的:再跑一遍不该再动任何东西
again_keys, again_names = db.backfill_identity(CON)
check("迁移跑第二遍不再改动任何东西(幂等)", (again_keys, again_names), (0, 0))

# ---- 改了身份字段,键必须立刻跟着变
call(server.update_component, match=(old_id,), body={"package": "0805"})
check("改了封装,身份键立刻跟着变(否则下次导入又认不出它)",
      CON.execute("SELECT identity_key FROM component WHERE id=?",
                  (old_id,)).fetchone()[0],
      bom.identity_key("8.08k", "0805", None, None))
check("改完再导一次同一颗料,不会多长一条",
      CON.execute("SELECT COUNT(*) FROM component WHERE value='8.08k'").fetchone()[0], 1)


p("\n【32】品类:能加能改能删、能搭层级,而且删品类绝不删元件")

# 全新库起来时,内置品类就该建好了 —— 否则用户得先在一个空列表里
# 自己敲十几个品类出来,才轮得到「挂元件」这件事
_s, _t = call(server.list_categories)
_seeded = [r["name"] for r in _t["flat"] if r["parent_id"] is None]
check("内置品类在库里就建好了", set(bom.CATEGORIES) <= set(_seeded), True)
check("树同时给出顶层和扁平两份(items 画菜单、flat 查路径)",
      (len(_t["items"]) > 0, len(_t["flat"]) >= len(_t["items"])), (True, True))


def cat_node(path):
    """按全路径找节点。路径拼法由后端负责,自检这边只按结果找。"""
    _s2, t2 = call(server.list_categories)
    for r in t2["flat"]:
        if r["path"] == path:
            return r
    return None


def cat_ev(path):
    _s2, t2 = call(server.list_categories)
    for r in t2["flat"]:
        if r["path"] == path:
            return r
    return {}


# ---- 加一个二级品类
cap = cat_node("电容")
check("「电容」在树上(内置顶层节点)", cap is not None, True)
_s, sub = call(server.create_category,
               body={"name": "无极性陶瓷电容", "parent_id": cap["id"]})
check("能加子品类", cat_node("电容 / 无极性陶瓷电容") is not None, True)
check("加子品类不动顶层", cat_node("电容")["parent_id"], None)

# ---- 把元件挂到子类上:文本必须还是「电容」
_s, c1 = call(server.create_component, body={
    "name": "SELFTEST-CAT-A", "category_id": sub["id"], "value": "1uF", "package": "0603"})
_s, c2 = call(server.create_component, body={
    "name": "SELFTEST-CAT-B", "category_id": sub["id"], "value": "2.2uF", "package": "0603"})
check("挂到子类下,文本仍然是大类(按品类分组的地方一行都不用改)",
      CON.execute("SELECT category FROM component WHERE id=?", (c1["id"],)).fetchone()[0],
      "电容")
check("而 category_id 指的就是那个子类节点",
      CON.execute("SELECT category_id FROM component WHERE id=?", (c1["id"],)).fetchone()[0],
      sub["id"])
check("子类下的元件数会统计上来", cat_ev("电容 / 无极性陶瓷电容")["own"], 2)
check("父类的 total 含子孙(删之前要靠它告诉用户会牵连多少)",
      cat_ev("电容")["total"],
      cat_ev("电容")["own"] + sum(c["total"] for c in cat_ev("电容")["children"]))
check("但父类的 own 只算直接挂在它下面的",
      cat_ev("电容")["own"] < cat_ev("电容")["total"], True)

# ---- meta 要把全路径发出来,不然界面上的下拉选不到子类
_s, mt = call(server.meta)
check("meta 里带上了品类全路径",
      any(p["path"] == "电容 / 无极性陶瓷电容" for p in mt.get("category_paths") or []),
      True)
check("顶层品类也在 categories 那份清单里(兼容老界面)", "电容" in mt["categories"], True)

# ---- 改名:子类改名不动文本,顶层改名要动
_s, _r = call(server.update_category, match=(sub["id"],),
              body={"name": "陶瓷电容(无极性)"})
check("子类改名后路径跟着变",
      cat_node("电容 / 陶瓷电容(无极性)") is not None, True)
check("子类改名不影响元件的文本(大类没变)",
      CON.execute("SELECT category FROM component WHERE id=?", (c1["id"],)).fetchone()[0],
      "电容")
_s, _r = call(server.update_category, match=(cap["id"],),
              body={"name": "电容(改名测试)"})
check("顶层改名要报出影响了几个元件", _r["renamed_components"] >= 2, True)
check("顶层改名:底下元件的文本跟着变(否则分组就和树对不上了)",
      CON.execute("SELECT category FROM component WHERE id=?", (c1["id"],)).fetchone()[0],
      "电容(改名测试)")
check("连挂在子类下的那颗也跟着变(改的是整棵子树)",
      CON.execute("SELECT category FROM component WHERE id=?", (c2["id"],)).fetchone()[0],
      "电容(改名测试)")
# 改回来,别影响后面的断言
call(server.update_category, match=(cap["id"],), body={"name": "电容"})
check("改回来之后文本也回到「电容」",
      CON.execute("SELECT category FROM component WHERE id=?", (c1["id"],)).fetchone()[0],
      "电容")

# ---- 防呆:同层重名、挪到自己下面
try:
    call(server.create_category, body={"name": "电容"})
    _dup = "没报错"
except server.ApiError as e:
    _dup = e.status
check("同一层里重名会被挡住(否则用户会看到两个一模一样的节点)", _dup, 400)
try:
    call(server.create_category, body={"name": "陶瓷电容(无极性)", "parent_id": cap["id"]})
    _dup2 = "没报错"
except server.ApiError as e:
    _dup2 = e.status
check("同一层里重名(子类)也会被挡住", _dup2, 400)
try:
    call(server.update_category, match=(cap["id"],), body={"parent_id": sub["id"]})
    _cyc = "没报错"
except server.ApiError as e:
    _cyc = e.status
check("不能把品类挪进自己的子孙里(否则这棵树成环,谁也走不到顶)", _cyc, 400)

# ---- 删子品类:元件往上挪,一颗不丢
_n_before = CON.execute("SELECT COUNT(*) FROM component").fetchone()[0]
_s, _d = call(server.delete_category, match=(sub["id"],))
check("删子品类时报告挪走了几个元件", _d["moved_components"], 2)
check("挪到哪儿也说得明明白白", _d["to"], "电容")
check("一个元件都没被删 —— 删品类是「整理货架」,不是「扔东西」",
      CON.execute("SELECT COUNT(*) FROM component").fetchone()[0], _n_before)
check("它们落到了父类下面",
      CON.execute("SELECT category_id FROM component WHERE id=?", (c1["id"],)).fetchone()[0],
      cap["id"])
check("子类确实没了", cat_node("电容 / 陶瓷电容(无极性)"), None)

# ---- 填一个没见过的品类名:品类表里自动多一行
_s, c3 = call(server.create_component, body={
    "name": "SELFTEST-CAT-C", "category": "用户自己敲的品类", "value": "x", "package": "0603"})
check("敲一个没见过的品类名,树上就多一个顶层节点",
      cat_node("用户自己敲的品类") is not None, True)
check("而且元件就挂在这一行上(不是挂了个空)",
      CON.execute("SELECT category_id FROM component WHERE id=?", (c3["id"],)).fetchone()[0],
      cat_node("用户自己敲的品类")["id"])

# ---- 删顶层品类:落到「未分类」,元件照样不丢
_s, _d2 = call(server.delete_category, match=(cat_node("用户自己敲的品类")["id"],))
check("删顶层品类时,底下的料落到「未分类」", _d2["to"], "未分类")
check("元件还在", CON.execute(
    "SELECT COUNT(*) FROM component WHERE id=?", (c3["id"],)).fetchone()[0], 1)
check("它的文本也跟着变成未分类", CON.execute(
    "SELECT category FROM component WHERE id=?", (c3["id"],)).fetchone()[0], "未分类")
check("未分类这一行是兜底,建出来了", cat_node("未分类") is not None, True)

# ---- 野数据(没挂品类的元件)要能被发现,也要能被对账修好
_loose_id = CON.execute(
    "INSERT INTO component(name, category, value, package) VALUES(?,?,?,?)",
    ("野数据", "其他", "0", "0603")).lastrowid
CON.commit()
_s, _t4 = call(server.list_categories)
check("没挂品类的元件会被单独报数 —— 否则用户会在菜单里找不到它却不知道为什么",
      _t4["loose"] >= 1, True)
db.reconcile_categories(CON)
_s, _t5 = call(server.list_categories)
check("启动对账会把野数据挂回去", _t5["loose"], 0)


p("\n【33】封装索引:C0805 / R0603 / L0402 认得出来,同尺寸算同一个")

# ---- 纯函数:标准写法、公制写法、少一位的常见写法,都归到一个尺寸
check("C0805 认成 0805", footprint.size("C0805"), "0805")
check("R0603 认成 0603", footprint.size("R0603"), "0603")
check("L0402 认成 0402", footprint.size("L0402"), "0402")
check("公制 1005 就是 0402(同一颗,两种叫法)", footprint.size("1005"), "0402")
check("公制 1608 就是 0603", footprint.size("1608"), "0603")
check("写 805 也认(少一位的常见写法)", footprint.size("805"), "0805")
check("SMD0805 / 0805_1.6x0.8 都能挖出 0805",
      [footprint.size("SMD0805"), footprint.size("0805_1.6x0.8")], ["0805", "0805"])
check("认不出尺寸的就不认(footprint 只认标准封装)",
      footprint.size("DIP-8"), "")
check("但字面归一化照样管用(和 bom.norm_package 一致)",
      footprint.canon("DIP-8"), "DIP8")
check("0603 和 0201 不是一回事", footprint.similar("0603", "0201"), False)
check("DIP-8 和 DIP-16 也不是一回事", footprint.similar("DIP-8", "DIP-16"), False)
check("0805 和 C0805 是一回事", footprint.similar("0805", "C0805"), True)
check("1005 和 0402 是一回事", footprint.similar("1005", "0402"), True)

# ---- 落库:package_key 由后端维护,用户什么都不用做
_s, _c33 = call(server.create_component, body={
    "name": "封装索引料", "value": "10uF", "package": "C0805", "category": "电容"})
_r33 = CON.execute("SELECT package, package_key FROM component WHERE id=?",
                   (_c33["id"],)).fetchone()
check("用户写的封装原样存着(显示不改他的字)", _r33["package"], "C0805")
check("索引列记下了尺寸", _r33["package_key"], "0805")
_s, _u33 = call(server.update_component, match=(str(_c33["id"]),),
                body={"package": "R0603"})
check("改封装时索引列跟着重算",
      CON.execute("SELECT package_key FROM component WHERE id=?",
                  (_c33["id"],)).fetchone()[0], "0603")
call(server.update_component, match=(str(_c33["id"]),), body={"package": "C0805"})

# ---- 按封装筛:写 C0805 的和写 0805 的,点一下该一起出来
_s, _p33 = call(server.list_components, query={"package": "0805", "limit": "0"})
check("按 0805 筛,写 C0805 的那颗也出来",
      _c33["id"] in {i["id"] for i in _p33["items"]}, True)
_s, _pa = call(server.list_components, query={"package": "1005", "limit": "0"})
_s, _pb = call(server.list_components, query={"package": "0402", "limit": "0"})
check("按 1005 筛等价于按 0402 筛(用户心里它们是一个)",
      {i["id"] for i in _pa["items"]} - {i["id"] for i in _pb["items"]}, set())

# ---- 搜索也认尺寸
_s, _k33 = call(server.list_components, query={"q": "C0805", "limit": "0"})
check("按关键字搜 C0805 搜得到", _c33["id"] in {i["id"] for i in _k33["items"]}, True)
_s, _k33b = call(server.list_components, query={"q": "1005", "limit": "0"})
_bad33 = [i["id"] for i in _k33b["items"]
          if (CON.execute("SELECT package_key FROM component WHERE id=?",
                          (i["id"],)).fetchone()[0] or "") != "0402"]
check("搜 1005 把 0402 那批都带出来(没有漏网)", _bad33, [])

# ---- 相似候选:同尺寸的带 fp_match,而且写在「像在哪儿」里
_s, _s33 = call(server.components_similar,
                query={"value": "10uF", "package": "0805"})
_fp33 = [i for i in _s33["items"] if i["id"] == _c33["id"]]
check("同尺寸的候选被认出来了", bool(_fp33) and bool(_fp33[0]["fp_match"]), True)
check("而且说清了为什么一样(把尺寸写出来)",
      any("封装一样(0805)" in i["match"] for i in _fp33), True)
check("同尺寸的排在没有 fp_match 的前面",
      [bool(i["fp_match"]) for i in _s33["items"]],
      sorted([bool(i["fp_match"]) for i in _s33["items"]], reverse=True))


p("\n【34】删品类只动这一级:子类接到上一级,元件挪上去,一件都不删")


def _tree34(tag):
    """建一棵 顶层 -> 中间 -> 叶子 的树,每一级各挂一颗料。"""
    top = call(server.create_category, body={"name": f"删测{tag}-顶层"})[1]["id"]
    mid = call(server.create_category,
               body={"name": f"删测{tag}-中间", "parent_id": top})[1]["id"]
    leaf = call(server.create_category,
                body={"name": f"删测{tag}-叶子", "parent_id": mid})[1]["id"]
    ids = {"top": top, "mid": mid, "leaf": leaf}
    for key in ("top", "mid", "leaf"):
        ids["c_" + key] = call(server.create_component, body={
            "name": f"删测{tag}-{key}", "category_id": ids[key],
            "value": "1k", "package": "0603"})[1]["id"]
    return ids


# ---- 删中间层:叶子接到顶层,只有这一个节点消失
T = _tree34("A")
_st34, _r34 = call(server.delete_category, match=(str(T["mid"]),))
check("删中间层:报出挪走了几个元件", _r34["moved_components"], 1)
check("删中间层:报出几个子类接到了上一级", _r34["moved_children"], 1)
check("只删掉这一个节点", _r34["deleted_nodes"], 1)
check("叶子接到顶层去了",
      CON.execute("SELECT parent_id FROM category WHERE id=?",
                  (T["leaf"],)).fetchone()[0], T["top"])
check("挂在中间层的那颗料挪到了顶层",
      CON.execute("SELECT category_id FROM component WHERE id=?",
                  (T["c_mid"],)).fetchone()[0], T["top"])
check("另外两颗料一动没动",
      [CON.execute("SELECT category_id FROM component WHERE id=?",
                   (T["c_top"],)).fetchone()[0],
       CON.execute("SELECT category_id FROM component WHERE id=?",
                   (T["c_leaf"],)).fetchone()[0]], [T["top"], T["leaf"]])
check("中间那个节点确实没了",
      CON.execute("SELECT COUNT(*) AS n FROM category WHERE id=?",
                  (T["mid"],)).fetchone()["n"], 0)
check("路径少了一层(叶子上面直接是顶层)",
      db.category_path(CON, T["leaf"]), "删测A-顶层 / 删测A-叶子")
check("挪上去那颗料的文本列仍旧等于顶层名字",
      CON.execute("SELECT category FROM component WHERE id=?",
                  (T["c_mid"],)).fetchone()[0], "删测A-顶层")

# ---- 删顶层:下级各自成为顶层,元件的文本必须跟着换(否则写着一个不存在的大类)
T2 = _tree34("B")
_st34b, _r34b = call(server.delete_category, match=(str(T2["top"]),))
check("删顶层:直接挂在它下面的料落到未分类",
      CON.execute("SELECT category FROM component WHERE id=?",
                  (T2["c_top"],)).fetchone()[0], "未分类")
check("中间层自己当了顶层",
      CON.execute("SELECT parent_id FROM category WHERE id=?",
                  (T2["mid"],)).fetchone()[0], None)
check("中间层那颗料的文本换成了新的顶层名",
      CON.execute("SELECT category FROM component WHERE id=?",
                  (T2["c_mid"],)).fetchone()[0], "删测B-中间")
check("整支下面的料(含叶子)文本都跟着换了",
      CON.execute("SELECT category FROM component WHERE id=?",
                  (T2["c_leaf"],)).fetchone()[0], "删测B-中间")

# ---- 撞名:下级接到上一级时遇到同名节点 -> 并进去,既不报错也不丢东西
T3 = _tree34("C")
_twin34 = call(server.create_category,
               body={"name": "删测C-叶子", "parent_id": T3["top"]})[1]["id"]
_st34c, _r34c = call(server.delete_category, match=(str(T3["mid"]),))
check("撞名时并进去,不报错", _st34c, 200)
check("并进去之后同名节点只剩一个",
      CON.execute("SELECT COUNT(*) AS n FROM category WHERE name=? AND parent_id=?",
                  ("删测C-叶子", T3["top"])).fetchone()["n"], 1)
check("叶子那颗料挪到了那个已经存在的同名节点上",
      CON.execute("SELECT category_id FROM component WHERE id=?",
                  (T3["c_leaf"],)).fetchone()[0], _twin34)

# ---- 绝不连坐:删一整支行,一颗元件都不许少
T4 = _tree34("D")
_before34 = CON.execute("SELECT COUNT(*) AS n FROM component "
                        "WHERE merged_into IS NULL").fetchone()["n"]
call(server.delete_category, match=(str(T4["top"]),))
_after34 = CON.execute("SELECT COUNT(*) AS n FROM component "
                       "WHERE merged_into IS NULL").fetchone()["n"]
check("删顶层一整支,元件一颗都没少", _after34, _before34)
check("整支的节点都还在(只是层级变了)",
      CON.execute("SELECT COUNT(*) AS n FROM category WHERE id IN (?,?,?)",
                  (T4["top"], T4["mid"], T4["leaf"])).fetchone()["n"], 2)

p("\n【35】本级入口 + 「在库」口径:卡片写的数字必须和点进去看到的一致")

# ---- own=1:只要**直接挂在这一个节点上**的元件,不含子孙。
# 大类下面既有细分出来的子类、又有还没细分的料,是很常见的摆法 ——
# 少了这个入口,那些料永远看不到(用户报的「电容里一颗电容没看到」)。
_s, _t35 = call(server.list_categories)
_cap35 = [n for n in _t35["flat"] if n["name"] == "电容" and n["parent_id"] is None]
check("库里有顶层「电容」", len(_cap35), 1)
_c35 = _cap35[0]
_s, _sub35 = call(server.list_components,
                  query={"category_id": str(_c35["id"]), "limit": "0"})
_s, _own35 = call(server.list_components,
                  query={"category_id": str(_c35["id"]), "own": "1", "limit": "0"})
check("本级列表里确实有料(否则这个入口就是白加的)", len(_own35["items"]) > 0, True)
check("own=1 不含子孙:每一行的 category_id 就是这一个节点",
      all(it["category_id"] == _c35["id"] for it in _own35["items"]), True)
check("own=1 是整棵子树的子集",
      len(_own35["items"]) <= len(_sub35["items"]), True)

# ---- 「N 种在库」必须和点进去看到的一致:只数有库存的。
# 原来卡片数的是元件条数、列表按 stocked=1 过滤,于是零库存的品类也喊「在库」,
# 点进去一行都没有(用户报的「虚假库存」)。
check("每个节点都带一套「有库存」口径",
      all(("own_stocked" in n and "total_stocked" in n) for n in _t35["flat"]), True)
check("有库存的数不会超过元件总数",
      all(n["own_stocked"] <= n["own"] and n["total_stocked"] <= n["total"]
          for n in _t35["flat"]), True)
check("本级「在库」的数字 = 本级里真有库存的条数",
      _c35["own_stocked"],
      len([i for i in _own35["items"] if (i.get("on_hand") or 0) > 0]))
check("子树「在库」也不少于本级的",
      _c35["total_stocked"] >= _c35["own_stocked"], True)

# ---- 封装压过 BOM 文件里抄错的「分类」列(用户报的「电阻被分到电容里」)
check("文件写「电容」但封装是 R0603 → 按封装算成电阻",
      bom.classify(["R8"], "R0603", "0Ω", "电容")[0], "电阻")
check("理由要写明是以封装为准",
      "以封装为准" in bom.classify(["R8"], "R0603", "0Ω", "电容")[2], True)
check("本来就一致的不能被误伤",
      bom.classify(["C1"], "C0805", "100nF", "电容")[0], "电容")
check("子类名和封装同族也算一致(贴片陶瓷电容 ⊃ 电容)",
      bom.classify(["C2"], "C0805", "1uF", "贴片陶瓷电容")[0], "贴片陶瓷电容")
check("兜底的「其他」不推翻", bom.classify(["R1"], "R0603", "1kΩ", "其他")[0], "其他")
check("封装认不出类别时不推翻",
      bom.classify(["U1"], "SOT-23-5", "", "电容")[0], "电容")

# ---- 全库扫描:文本和树上顶层名字必须处处一致

p("\n【37】按仓位**路径**入库,不能新建幽灵仓位")

# 快速入库/批量入库的下拉给的是路径(「A柜 / 01层 / 02格」)。后端以前只认 id/code,
# 认不出来就 INSERT 一个新顶层仓位 —— 货记到那儿去了,仓位表也被污染。
# 测试库可能是上次跑剩的:同名仓位先清掉,免得撞 location.code 的唯一约束
CON.execute("DELETE FROM location WHERE code IN ('测试柜R7', '01层R7', '02格R7')")
CON.commit()
_root37 = CON.execute(
    "INSERT INTO location(code, parent_id) VALUES('测试柜R7', NULL)").lastrowid
_mid37 = CON.execute(
    "INSERT INTO location(code, parent_id) VALUES('01层R7', ?)",
    (_root37,)).lastrowid
_leaf37 = CON.execute(
    "INSERT INTO location(code, parent_id) VALUES('02格R7', ?)",
    (_mid37,)).lastrowid
CON.commit()
_n_before37 = CON.execute("SELECT COUNT(*) AS n FROM location").fetchone()["n"]
_cid37 = call(server.list_components, query={"limit": "1"})[1]["items"][0]["id"]
_s37, _r37 = call(server.stock_move,
                  body={"kind": "IN", "component_id": _cid37, "qty": 1,
                        "location": "测试柜R7 / 01层R7 / 02格R7"})
check("按路径入库成功", _s37, 200)
check("流水落在真正的那个仓位 id 上",
      CON.execute("SELECT location_id AS l FROM movement WHERE id=?",
                  (_r37["movement_id"],)).fetchone()["l"], _leaf37)
check("仓位表没有多出任何一行(没造出幽灵仓位)",
      CON.execute("SELECT COUNT(*) AS n FROM location").fetchone()["n"],
      _n_before37)
# 手打一个真没有的仓位名,仍旧要能新建(这条路必须留着)
_n_before37b = CON.execute("SELECT COUNT(*) AS n FROM location").fetchone()["n"]
_s37b, _r37b = call(server.stock_move,
                    body={"kind": "IN", "component_id": _cid37, "qty": 1,
                          "location": "手打的新格子"})
check("手打新仓位名仍旧能新建", _s37b, 200)
check("确实多了一个仓位",
      CON.execute("SELECT COUNT(*) AS n FROM location").fetchone()["n"],
      _n_before37b + 1)

p("\n【38】手动批量出库(不依赖 BOM)")

# 菜单里的「📤 批量出库」走的就是这条路:同一批料一次领走,逐条写流水并盖「手动」章。
_cid38 = call(server.list_components, query={"limit": "1"})[1]["items"][0]["id"]
call(server.stock_move, body={"kind": "IN", "component_id": _cid38, "qty": 5,
                              "location": "未分类", "note": "手动 自检备料"})
_n38 = CON.execute("SELECT COUNT(*) AS n FROM movement").fetchone()["n"]
_q38 = CON.execute("SELECT COALESCE(SUM(qty),0) AS q FROM stock WHERE component_id=?",
                   (_cid38,)).fetchone()["q"]
_s38, _r38 = call(server.stock_batch,
                  body={"kind": "OUT",
                        "items": [{"component_id": _cid38, "qty": 2}],
                        "location": "未分类", "note": "手动 批量出库"})
check("批量出库成功", _s38, 200)
check("流水多了一条",
      CON.execute("SELECT COUNT(*) AS n FROM movement").fetchone()["n"], _n38 + 1)
check("最新那条是出库",
      CON.execute("SELECT kind AS k FROM movement ORDER BY id DESC LIMIT 1"
                  ).fetchone()["k"], "OUT")
check("库存相应减少",
      CON.execute("SELECT COALESCE(SUM(qty),0) AS q FROM stock WHERE component_id=?",
                  (_cid38,)).fetchone()["q"], _q38 - 2)
check("这条流水盖了「手动」章",
      CON.execute("SELECT note AS n FROM movement ORDER BY id DESC LIMIT 1"
                  ).fetchone()["n"], "手动 批量出库")

p("\n【36】批量挪品类:只改归属,不碰库存和流水")

# 用户的原话:「我想要增加封装 R0805,把这个一排的元件放下面去」——
# 界面上是多选 + 「挪到品类…」,后端这条路就是 update_component(category_id=...)。
_s, _t36 = call(server.list_categories)
_t36 = [n for n in _t36["flat"] if n["name"] == "电容"]
check("有可用的目标品类", len(_t36) >= 1, True)
_s, _new36 = call(server.create_category,
                  body={"name": "R0805 测试", "parent_id": _t36[0]["id"]})
check("新建子类拿到了 id", bool((_new36 or {}).get("id")), True)
_nid36 = (_new36 or {}).get("id")
_s, _src36 = call(server.list_components, query={"limit": "3"})
_targets36 = _src36["items"][:3]
check("库里有可挪的元件", len(_targets36) > 0, True)
_before36 = {i["id"]: (i.get("on_hand") or 0) for i in _targets36}
_moved36 = 0
for _i in _targets36:
    _s2, _r2 = call(server.update_component, match=(str(_i["id"]),),
                    body={"category_id": _nid36})
    if _s2 in (200, 201):
        _moved36 += 1
check("每一颗都挪成功了", _moved36, len(_targets36))
check("category_id 指向新节点",
      [CON.execute("SELECT category_id AS c FROM component WHERE id=?",
                   (_i["id"],)).fetchone()["c"] for _i in _targets36],
      [_nid36] * len(_targets36))
# category 存的一直是**顶层**大类名(和 category_id 指向那一支保持一致),
# 叶子归属只由 category_id 决定 —— 所以这里期望的是顶层名,不是新节点自己的名字
check("品类文本跟新节点那一支的顶层名一致",
      [CON.execute("SELECT category AS c FROM component WHERE id=?",
                   (_i["id"],)).fetchone()["c"] for _i in _targets36],
      ["电容"] * len(_targets36))
check("新节点确实在「电容」这一支下面",
      CON.execute("SELECT COUNT(*) AS n FROM category WHERE id=? AND name='R0805 测试'",
                  (_nid36,)).fetchone()["n"], 1)
check("挪品类不动库存",
      [CON.execute("SELECT COALESCE(SUM(qty),0) AS q FROM stock WHERE component_id=?",
                   (_i["id"],)).fetchone()["q"] for _i in _targets36],
      [_before36[_i["id"]] for _i in _targets36])
# 挪回原位,别把后面的扫描搞乱
for _i in _targets36:
    call(server.update_component, match=(str(_i["id"]),),
         body={"category_id": _i["category_id"]}) if _i.get("category_id") else None


_mism = 0
for _r in CON.execute("SELECT id, category, category_id FROM component "
                      "WHERE category_id IS NOT NULL"):
    _root = db.category_root(CON, _r["category_id"])
    if _root is not None and (_root["name"] or "") != (_r["category"] or ""):
        _mism += 1
check("全库扫描:没有元件的品类文本和它在树上的顶层名字对不上", _mism, 0)


p("\n【39】BOM 导入按封装落到子类:电阻 + C0603 也要进 R0603(#25)")

# 用户的原话:
#   「我导入 bom 表你需要把 bom 里面的电阻电感电容按照品类名字分好,**不要只搞一个
#    电阻**,电阻下面是有子类的。比如封装是 R0603 你就分到 R0603 这里面;如果 bom
#    里面是 0603,你识别到了他是一个电阻,你就往电阻里面找最相似的放;比如 bom 表上
#    是电阻、封装写的 0603 而不是 R0603,你也要往 R0603 放。**就算写的是 C0603 你也
#    要往 R0603 放。**」
#
# 这个自检库刚开始只有顶层品类(种子就是 bom.CATEGORIES),所以先按用户真实库的样子
# 搭出子类(他那边的形状是:电阻 -> R0603/R0805,电容 -> 贴片陶瓷电容 -> C0603/C0805,
# 电感**没有**子类)。电感这一支故意不建:规则明确「不新建节点」,没有就留在根级。


def _ensure_cat(name, parent_id):
    """按 (名字, 父节点) 找/建一个子类。已有就复用,别撞唯一约束。"""
    _s2, t2 = call(server.list_categories)
    for r in t2["flat"]:
        if r["name"] == name and r["parent_id"] == parent_id:
            return r["id"]
    _s2, node = call(server.create_category, body={"name": name, "parent_id": parent_id})
    return node["id"]


_cat_r = cat_node("电阻")["id"]
_cat_c = cat_node("电容")["id"]
_cat_l = cat_node("电感")["id"]
_r0603 = _ensure_cat("R0603", _cat_r)
_r0805 = _ensure_cat("R0805", _cat_r)
_c_mlcc = _ensure_cat("贴片陶瓷电容", _cat_c)
_c0603 = _ensure_cat("C0603", _c_mlcc)     # 注意:子类在**两层**下面,只找一层是找不到的
_c0805 = _ensure_cat("C0805", _c_mlcc)
check("子类搭好了:电阻支两个、电容支两层",
      (db.category_path(CON, _r0603), db.category_path(CON, _c0603)),
      ("电阻 / R0603", "电容 / 贴片陶瓷电容 / C0603"))
check("电感这一支本来就(故意)没有子类", cat_node("电感 / L0603"), None)


def _mpn_node(mpn):
    r = CON.execute("SELECT category_id FROM component WHERE mpn=?", (mpn,)).fetchone()
    return None if r is None else r["category_id"]


def _mpn_text(mpn):
    return CON.execute("SELECT category FROM component WHERE mpn=?", (mpn,)).fetchone()[0]


# ---- 带品类列的 BOM:界面复核对话框看到的就是这份(preview -> import)
bom_cat = write_csv("issue25_cat.csv",
                    "Designator,Comment,Footprint,Quantity,MPN,Category\n"
                    "R1,10k,R0603,1,ST39-A1,电阻\n"
                    "R2,20k,0603,1,ST39-A2,电阻\n"
                    "R3,30k,C0603,1,ST39-A3,电阻\n"
                    "R4,40k,R0805,1,ST39-A4,电阻\n"
                    "C1,100nF,C0603,1,ST39-A5,电容\n"
                    "L1,10uH,L0603,1,ST39-A6,电感\n"
                    "R5,50k,SOT-23,1,ST39-A7,电阻\n")
# ---- 不带品类列的 BOM:只有位号 + 值 + 封装,品类全靠推断
bom_nocat = write_csv("issue25_nocat.csv",
                      "Designator,Comment,Footprint,Quantity,MPN\n"
                      "R11,10k,R0603,1,ST39-B1\n"
                      "R12,20k,0603,1,ST39-B2\n"
                      "R13,30k,C0603,1,ST39-B3\n"
                      "R14,40k,R0805,1,ST39-B4\n"
                      "C11,100nF,C0603,1,ST39-B5\n"
                      "L11,10uH,L0603,1,ST39-B6\n"
                      "C12,100nF,C0805,1,ST39-B7\n")

_ncat0 = CON.execute("SELECT COUNT(*) AS n FROM category").fetchone()["n"]
_s, _prev39 = call(server.bom_preview, upload=upload_of(bom_cat))
check("预览把 7 行都解析出来了(走的是界面真正用的那条路)",
      _prev39["line_count"], 7)
_s, _repA = call(server.bom_import, body={"project_name": "ISSUE25-带品类列"},
                 upload=upload_of(bom_cat))
_s, _repB = call(server.bom_import, body={"project_name": "ISSUE25-不带品类列"},
                 upload=upload_of(bom_nocat))
check("两份 BOM 各 7 行", (_repA["bom_lines"], _repB["bom_lines"]), (7, 7))
check("导入报告里没有「有 N 颗没找到子类」这类提醒(用户说不用提醒他)",
      [_repA["warnings"], _repB["warnings"]], [[], []])
check("导入没有新建任何品类节点(不硬造 L0603 之类)",
      CON.execute("SELECT COUNT(*) AS n FROM category").fetchone()["n"], _ncat0)

# 逐行对落点。带品类列的这 7 行覆盖了用户点名的三种写法 + 认不出尺寸的兜底
for _mpn, _want, _label in (
        ("ST39-A1", _r0603, "电阻 + R0603"),
        ("ST39-A2", _r0603, "电阻 + 0603(缺前缀)"),
        ("ST39-A3", _r0603, "电阻 + C0603(用户点名:写错字母也要进 R0603)"),
        ("ST39-A4", _r0805, "电阻 + R0805"),
        ("ST39-A5", _c0603, "电容 + C0603(电容支的子类在两层下面)"),
        ("ST39-A6", _cat_l, "电感 + L0603(电感支没有子类 -> 老实留在根级)"),
        ("ST39-A7", _cat_r, "电阻 + SOT-23(认不出尺寸 -> 留在电阻根级)"),
        ("ST39-B1", _r0603, "无品类列:位号 R + R0603"),
        ("ST39-B2", _r0603, "无品类列:位号 R + 0603"),
        ("ST39-B3", _r0603, "无品类列:位号 R + C0603"),
        ("ST39-B4", _r0805, "无品类列:位号 R + R0805"),
        ("ST39-B5", _c0603, "无品类列:位号 C + C0603"),
        ("ST39-B6", _cat_l, "无品类列:位号 L + L0603(没有子类,留根级)"),
        ("ST39-B7", _c0805, "无品类列:位号 C + C0805")):
    check(f"{_label} -> {db.category_path(CON, _want)}", _mpn_node(_mpn), _want)

check("落到子类之后,品类文本仍然是大类名(按品类分组的地方一行都不用改)",
      [_mpn_text(m) for m in ("ST39-A1", "ST39-A3", "ST39-A5", "ST39-A6")],
      ["电阻", "电阻", "电容", "电感"])

# 用户点名的那一条:封装写的是 C0603,但这一行是电阻 —— 必须判成电阻,进电阻支
check("「电阻 + C0603 + 品类列写电阻」判成电阻(不被那个 C 带到电容支)",
      bom.classify(["R3"], "C0603", "30k", "电阻")[0], "电阻")
check("同一行写成别的位号也一样(判据是这一行的类型,不是字母)",
      bom.classify(["R9"], "C0603", "30k", "电阻")[0], "电阻")
check("不带品类列时靠位号也判成电阻",
      bom.classify(["R3"], "C0603", "30k", "")[0], "电阻")
# 上一版为「文件写电容、封装 R0603」立的规矩不能被这次改动推翻
check("旧的仍然成立:文件写「电容」但封装 R0603、位号 R8 → 按封装算成电阻",
      bom.classify(["R8"], "R0603", "0Ω", "电容")[0], "电阻")
check("而且理由里还是写着以封装为准",
      "以封装为准" in bom.classify(["R8"], "R0603", "0Ω", "电容")[2], True)

# ---- order-independence:同一批行倒过来放,落点必须一模一样。
# 以前那个 bug 的症状就是「把某一行挪到最前面,结果就变了」(判反之后先建了另一边
# 的节点,后面跟着错)。
bom_rev = write_csv("issue25_rev.csv",
                    "Designator,Comment,Footprint,Quantity,MPN,Category\n"
                    "R5,50k,SOT-23,1,ST39-C7,电阻\n"
                    "L1,10uH,L0603,1,ST39-C6,电感\n"
                    "C1,100nF,C0603,1,ST39-C5,电容\n"
                    "R4,40k,R0805,1,ST39-C4,电阻\n"
                    "R3,30k,C0603,1,ST39-C3,电阻\n"
                    "R2,20k,0603,1,ST39-C2,电阻\n"
                    "R1,10k,R0603,1,ST39-C1,电阻\n")
_s, _repC = call(server.bom_import, body={"project_name": "ISSUE25-倒着放"},
                 upload=upload_of(bom_rev))
check("同一份 BOM 倒着放,7 行落点与正着放完全一致(结果与行序无关)",
      [_mpn_node("ST39-C%d" % i) for i in (1, 2, 3, 4, 5, 6, 7)],
      [_mpn_node("ST39-A%d" % i) for i in (1, 2, 3, 4, 5, 6, 7)])

# ---- package_key 在**插入时**就写好,不留 NULL 等下次启动
check("导入时就算好了 package_key(那段时间按尺寸筛料才不会漏)",
      [r["k"] for r in CON.execute(
          "SELECT package_key AS k FROM component WHERE mpn LIKE 'ST39-%' ORDER BY id")],
      [footprint.canon(r["p"]) for r in CON.execute(
          "SELECT package AS p FROM component WHERE mpn LIKE 'ST39-%' ORDER BY id")])

# ---- 兜底:启动时 reconcile_categories() 把堆在**根级**的料自己归位
check("A(10kΩ R0603)现在堆在「电阻」根级 —— 这是本次要修的起点",
      CON.execute("SELECT category_id AS c FROM component WHERE id=?",
                  (A,)).fetchone()["c"], _cat_r)
_n_recon = db.reconcile_categories(CON)
check("启动对账确实挪了东西(都堆在根级的那批)", _n_recon > 0, True)
check("堆在根级的电阻 A 按封装归到了 电阻 / R0603",
      CON.execute("SELECT category_id AS c FROM component WHERE id=?",
                  (A,)).fetchone()["c"], _r0603)
check("堆在根级的电容 B 按封装归到了 电容 / 贴片陶瓷电容 / C0805",
      CON.execute("SELECT category_id AS c FROM component WHERE id=?",
                  (B,)).fetchone()["c"], _c0805)
check("「其他」这一支没有子类,螺丝就留在根级(认不出相似的就不乱挂)",
      CON.execute("SELECT category_id AS c FROM component WHERE id=?",
                  (D,)).fetchone()["c"], cat_node("其他")["id"])
check("发光二极管这一支也没有子类,LED 同样留在根级",
      CON.execute("SELECT category_id AS c FROM component WHERE id=?",
                  (E,)).fetchone()["c"], cat_node("发光二极管")["id"])

# ---- 已经挂在**子类**上的料:reconcile 一根都不许动(那是用户自己放的)
_sub_keep = {m: _mpn_node(m) for m in ("ST39-A5", "ST39-A6", "ST39-B7", "ST39-C3")}
db.reconcile_categories(CON)
check("已挂子类的料再跑一次对账也不会被挪走",
      {m: _mpn_node(m) for m in _sub_keep}, _sub_keep)
check("再跑一次是幂等的(一颗都不再动)", db.reconcile_categories(CON), 0)

# ---- 人工指定过的品类不许被自动归位覆盖。
# 复核对话框里人把这一行改成「电容」,它就该待在电容根级 —— 哪怕封装是 R0603
# (电阻支有同尺寸的 R0603 节点),规则也不许"顺手"把它塞进去。
bom_h = write_csv("issue25_human.csv",
                  "Designator,Comment,Footprint,Quantity,MPN,Category\n"
                  "R21,60k,R0603,1,ST39-H1,电阻\n")
_s, _prevH = call(server.bom_preview, upload=upload_of(bom_h))
_rowH = _prevH["lines"][0]["source_row"]
check("人没改的时候这一行本来是电阻", _prevH["lines"][0]["category"], "电阻")
_s, _repH = call(server.bom_import,
                 body={"project_name": "ISSUE25-人工指定",
                       "categories": {str(_rowH): "电容"}},
                 upload=upload_of(bom_h))
check("人工指定的品类落库了", _mpn_text("ST39-H1"), "电容")
check("人工指定的行留在电容根级,没被自动塞进 C0603/R0603",
      _mpn_node("ST39-H1"), _cat_c)

# ---- 最后再扫一遍全库:文本和树上顶层名字必须仍然处处一致
_mism39 = 0
for _r in CON.execute("SELECT id, category, category_id FROM component "
                      "WHERE category_id IS NOT NULL"):
    _root = db.category_root(CON, _r["category_id"])
    if _root is not None and (_root["name"] or "") != (_r["category"] or ""):
        _mism39 += 1
check("全库扫描(第 39 节之后):文本和树上顶层名字仍然处处一致", _mism39, 0)

CON.close()
p("\n" + "=" * 62)
p(f"结果:{'全部通过' if not FAILS else '失败 ' + str(len(FAILS)) + ' 项'}")
for f in FAILS:
    p(f"  · {f}")
io.open(os.path.join(CACHE, "api_selftest.txt"), "w", encoding="utf-8").write(out.getvalue())
print(f"assertions={'FAIL' if FAILS else 'PASS'} failures={len(FAILS)}")
for f in FAILS:
    print("  " + f)
sys.exit(1 if FAILS else 0)
