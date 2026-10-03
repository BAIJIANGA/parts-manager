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

import db        # noqa: E402
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
check("分号版的值读对了", rep["rows"][0]["name"], "LED 红 0805")
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
