# -*- coding: utf-8 -*-
"""第三步的成品包实地验证:按 BOM 收料清单 + 按 BOM 出库分配树。

    cd dist\\元器件物料管理
    runtime\\python.exe ..\\..\\build\\cache\\probe_pkg_step3.py

全程只碰用户数据库的**副本**,跑完还要回头确认原库一个字节都没动。
"""
import io
import os
import shutil
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.abspath(os.path.join(HERE, "..", "..", "dist", "元器件物料管理"))
REAL = os.path.join(PKG, "data", "parts.db")
print("包目录:", PKG)
print("当前工作目录:", os.getcwd())
assert os.path.isdir(PKG), PKG

sys.path.insert(0, os.path.join(PKG, "app"))
os.environ.setdefault("PYTHONUTF8", "1")

bad = []


def ck(label, got, want):
    ok = got == want
    if not ok:
        bad.append(label)
    print(f"  [{'OK ' if ok else 'FAIL'}] {label}: {got!r}"
          + ("" if ok else f"  (期望 {want!r})"))


def col_of(tree, title):
    """按表头文字找列号。

    绝不写死下标:给树加一列(比如「参数」)就会让后面所有列右移一位,
    写死的断言会**照样通过/失败,只是在看别的格子**。
    """
    cols = [tree.heading(c)["text"] for c in tree.cget("columns")]
    return cols.index(title)


def snapshot(path):
    st = os.stat(path)
    with io.open(path, "rb") as f:
        return (st.st_size, st.st_mtime_ns, len(f.read()))


def buttons_of(root):
    """收集某棵控件树里所有按钮的文字 —— 确认入口真的摆出来了,不是只在代码里。"""
    out = []

    def walk(w):
        for c in w.winfo_children():
            try:
                if isinstance(c, (gui.ttk.Button, gui.tk.Button)):
                    out.append(str(c.cget("text")))
            except gui.tk.TclError:
                pass
            walk(c)
    walk(root)
    return out


class FakeBox:
    """把弹窗顶掉:体检没法真人点「是」。"""

    answer = True

    def __init__(self):
        self.infos = []

    def askyesno(self, *a, **kw):
        return self.answer

    def showinfo(self, *a, **kw):
        self.infos.append(a)

    def showwarning(self, *a, **kw):
        self.infos.append(a)

    def showerror(self, *a, **kw):
        self.infos.append(a)


try:
    import db
    import gui
    import server

    print("模块导入 OK")
    before = snapshot(REAL)

    # ---------------------------------------------------------- 新版到底在不在
    print("\n--- 成品包里是不是新版 ---")
    for name in ("BomPaneBase", "BomReceivePane", "BomPickPane"):
        ck(f"界面里有 {name}", hasattr(gui, name), True)
    pats = {p.pattern for _m, p, _f in server.ROUTES}
    ck("路由里有批量开单", "^/api/stock/batch$" in pats, True)
    ck("路由里有出库分配方案", r"^/api/projects/(\d+)/pick_plan$" in pats, True)

    txt = io.open(os.path.join(PKG, "使用说明.txt"), encoding="utf-8-sig").read()
    ck("说明书里写了「按 BOM 收料」", "按 BOM 收料" in txt, True)
    ck("说明书里写了「按 BOM 出库」", "按 BOM 出库" in txt, True)
    ck("说明书里保留了「自由」那条路", "自由" in txt, True)

    tmp = os.path.join(HERE, "_pkgprobe3.db")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(tmp + suffix):
            os.remove(tmp + suffix)
    shutil.copy2(REAL, tmp)

    con = db.connect(tmp)
    db.init_db(con)
    cols = {r[1] for r in con.execute("PRAGMA table_info(movement)")}
    ck("旧库升上来之后 movement.bom_id 补上了", "bom_id" in cols, True)
    con.close()

    app = gui.App(tmp)
    app.update()
    app.update_idletasks()
    print("窗口: %d x %d" % (app.winfo_width(), app.winfo_height()))
    app.nb.select(app.tab_proj)
    app.update()
    pr = app.tab_proj
    pid = pr._pid
    ck("用户那个项目还在,而且默认选中了", bool(pid), True)
    if not pid:
        raise SystemExit("没有项目就没法验这一页,先手动建一个")

    # ================================================== ① 按 BOM 收料
    print("\n--- ① 按 BOM 收料清单 ---")
    pin = pr.pane_in
    ck("入库默认就是「按 BOM」", pin.mode.get(), "bom")
    ck("默认摆出来的是收料清单", isinstance(pin.bom_form, gui.BomReceivePane), True)
    rp = pin.bom_form
    cols = [rp.tree.heading(c)["text"] for c in rp.tree.cget("columns")]
    print("     列:", " / ".join(cols))
    ck("最前面是「选」那一列", cols[0], "选")
    ck("有「品类」这一列(值封装对不上时靠它兜底)", "品类" in cols, True)
    ck("「品类」就排在名称旁边,不用横向拖才看得到",
       cols.index("品类") <= 2, True)
    ck("列数和 BOM 需求行数对得上",
       len(rp.tree.get_children()), len(rp.lines))
    ck("数量默认填的就是 BOM 的总需求",
       [rp.qty[l["bom_id"]] for l in rp.lines],
       [int(l["need"]) for l in rp.lines])
    ck("一上来一个都没勾", len(rp.picked), 0)
    btns = buttons_of(pin)
    ck("有「勾选的全部入库」这个按钮",
       any("勾选的全部入库" in b for b in btns), True)
    ck("全选 / 全不选都在",
       any("全选" in b for b in btns) and any("全不选" in b for b in btns), True)

    rp.check_all(True)
    app.update()
    ck("一键全勾勾满了", len(rp.picked), len(rp.lines))
    rp.check_all(False)
    app.update()
    ck("再点一次全不选", len(rp.picked), 0)

    # 真收一行进去,而且这一行必须是 BOM 第一行
    r0 = rp.lines[0]
    bid0, cid0 = r0["bom_id"], r0["component_id"]
    before_stock = app.con.execute(
        "SELECT COALESCE(SUM(qty),0) FROM stock WHERE component_id=?",
        (cid0,)).fetchone()[0]
    rp.toggle(str(bid0))
    rp.qty[bid0] = 2
    app.update()
    ck("勾上了那一条", bid0 in rp.picked, True)
    _rb = gui.messagebox
    gui.messagebox = FakeBox()
    try:
        rp.submit()
        app.update()
    finally:
        gui.messagebox = _rb
    ck("库存真的多了 2",
       app.con.execute("SELECT COALESCE(SUM(qty),0) FROM stock WHERE component_id=?",
                       (cid0,)).fetchone()[0], before_stock + 2)
    ck("流水记在这个项目名下",
       app.con.execute("SELECT COUNT(*) FROM movement WHERE project_id=? AND kind='IN'",
                       (pid,)).fetchone()[0] >= 1, True)
    ck("入库**没有**把「已发料」记上(货进库不等于发给板子了)",
       app.con.execute("SELECT COALESCE(SUM(placed_qty),0) FROM project_bom"
                       " WHERE id=?", (bid0,)).fetchone()[0], 0)
    ck("交完之后勾选清空", len(rp.picked), 0)

    # ================================================== ② 按 BOM 出库
    print("\n--- ② 按 BOM 出库分配树 ---")
    pout = pr.pane_out
    # 像真人一样点一下「元件出库」那个页签 —— 刚收进来的货要能被看见,
    # 靠的就是切页时刷一遍(不然还显示切走之前的库存)
    pr.sub.select(pout)
    app.update()
    ck("出库默认也是「按 BOM」", pout.mode.get(), "bom")
    ck("默认摆出来的是分配树", isinstance(pout.bom_form, gui.BomPickPane), True)
    pp = pout.bom_form
    ck("这张表是树(有展开箭头那一列)", "tree" in str(pp.tree.cget("show")), True)
    ck("树列的标题说明它是什么",
       "BOM 需求" in str(pp.tree.heading("#0")["text"]), True)
    ck("每条 BOM 需求都是一个顶层行",
       [pp.tree.parent(str(l["bom_id"])) for l in pp.lines], [""] * len(pp.lines))
    ck("需求一条都没漏", len(pp.tree.get_children()), len(pp.lines))
    btns = buttons_of(pr.pane_out)
    ck("有「按这个分配出库」这个按钮",
       any("按这个分配出库" in b for b in btns), True)
    ck("有「自动配齐」", any("自动配齐" in b for b in btns), True)

    # 刚收进来 2 个的那条需求,现在自己那颗料应该有库存了
    ln = next(l for l in pp.lines if l["bom_id"] == bid0)
    c0 = next((c for c in ln["candidates"] if c["id"] == cid0), None)
    ck("刚收的料出现在候选里", bool(c0), True)
    ck("而且排在候选最前面(BOM 自己指定的料优先)", ln["candidates"][0]["id"], cid0)
    ck("它被标成「BOM 本行指定的料」", ln["candidates"][0]["own"], True)
    ck("候选带着把握说法", bool(ln["candidates"][0]["verdict"]), True)
    ck("候选带着「像在哪里」", bool(ln["candidates"][0]["match"]), True)

    want = pp.default_qty(bid0, c0)
    ck("默认数量 = min(这颗的库存, 还差多少)", want, min(int(c0["on_hand"]),
                                                       int(ln["remaining"])))
    rem0 = pp.remain(bid0)
    child = pp.child_iid(bid0, cid0)
    ck("这一行的子行确实挂在那条需求下面",
       pp.tree.parent(child), str(bid0))
    pp.toggle(child)
    app.update()
    ck("勾上之后填好了数量", pp.alloc.get((bid0, cid0)), want)
    ck("父行的「还需要」立刻跟着减", pp.remain(bid0), rem0 - want)
    _vals = pp.tree.item(str(bid0), "values")
    # Treeview 里的单元格一律是字符串
    ck("父行格子里写着还差多少",
       int(_vals[col_of(pp.tree, "还需要")]), pp.remain(bid0))

    _rb = gui.messagebox
    gui.messagebox = FakeBox()
    try:
        pp.submit()
        app.update()
    finally:
        gui.messagebox = _rb
    ck("出库之后扣到了库存",
       app.con.execute("SELECT COALESCE(SUM(qty),0) FROM stock WHERE component_id=?",
                       (cid0,)).fetchone()[0], before_stock + 2 - want)
    ck("「已发料」记在那条 BOM 需求上",
       app.con.execute("SELECT placed_qty FROM project_bom WHERE id=?",
                       (bid0,)).fetchone()[0], want)
    ck("流水记住了自己顶的是哪条需求",
       app.con.execute("SELECT COUNT(*) FROM movement WHERE bom_id=? AND kind='OUT'",
                       (bid0,)).fetchone()[0], 1)
    ck("交完之后分配清空", len(pp.alloc), 0)

    mid = app.con.execute("SELECT MAX(id) FROM movement WHERE bom_id=? AND kind='OUT'",
                          (bid0,)).fetchone()[0]
    gui.call(app.con, server.void_movement, match=(mid,),
             body={"operator": "成品包体检"})
    ck("撤销一笔之后「已发料」退回 0",
       app.con.execute("SELECT placed_qty FROM project_bom WHERE id=?",
                       (bid0,)).fetchone()[0], 0)
    ck("撤销补的反向流水也记着那条需求",
       app.con.execute("SELECT COUNT(*) FROM movement WHERE bom_id=?",
                       (bid0,)).fetchone()[0] >= 2, True)

    # ================================================== ③ 自由那种方式
    print("\n--- ③ 自由入库 / 自由出库还能用 ---")
    pin.show_free()
    pr.sub.select(pin)
    app.update()
    app.update_idletasks()
    ck("入库切到自由了", pin.mode.get(), "free")
    ck("自由那张表显示出来了", bool(pin.form.winfo_ismapped()), True)
    ck("自由入库默认列全部元件(要收的料现在库存就是 0)",
       pin.form.only_stocked.get(), False)
    pout.show_free()
    app.update()
    ck("出库切到自由了", pout.mode.get(), "free")
    ck("自由出库默认只列有库存的", pout.form.only_stocked.get(), True)
    pin.show_bom()
    pout.show_bom()
    app.update()
    ck("切回来还是按 BOM", (pin.mode.get(), pout.mode.get()), ("bom", "bom"))

    app.destroy()
    try:
        app.con.close()
    except Exception:  # noqa: BLE001
        pass
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(tmp + suffix):
            os.remove(tmp + suffix)

    # ---------------------------------------------------------- 原库没被动过
    after = snapshot(REAL)
    ck("用户的真实数据库一个字节都没被动过", after, before)

    print("\n结果:" + ("成品包第三步 PASS" if not bad else f"FAIL {bad}"))
    sys.exit(1 if bad else 0)
except Exception:
    print("!! 失败:")
    traceback.print_exc()
    sys.exit(1)
