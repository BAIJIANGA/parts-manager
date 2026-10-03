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


def call(fn, query=None, body=None, match=None):
    """直接调后端 handler,把 ApiError 变成异常方便断言。"""
    ctx = server.Ctx(None, CON, dict(query or {}), dict(body or {}), None)
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
