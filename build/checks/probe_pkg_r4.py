# -*- coding: utf-8 -*-
"""第四轮的成品包实地验证:自定义属性(#5)、品类树与四级库存菜单(#6/#7)、
删项目要连明细一起清掉(#4)。

    cd dist\\元器件物料管理
    runtime\\python.exe ..\\..\\build\\checks\\probe_pkg_r4.py

讲究和前面几步一样,而且这一步**尤其**要注意:第四轮的核心是「老库要能升上来」,
所以这里刻意直接用包里那份**真实**数据库的副本跑一遍,而不是造一个干净的库 ——
干净的新库建表当然成功,什么都证明不了。

库里有没有项目都无所谓(用户的包里正好没有,那正是 #4 那个 bug 的场景),
需要就现造一个。全程只碰副本,跑完回头确认原库一个字节都没动。
"""
import io
import os
import shutil
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.abspath(os.path.join(HERE, "..", "..", "dist", "元器件物料管理"))
CACHE = os.path.abspath(os.path.join(HERE, "..", "cache"))
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


def snapshot(path):
    st = os.stat(path)
    with io.open(path, "rb") as f:
        return (st.st_size, st.st_mtime_ns, len(f.read()))


def cols_of(tree):
    return [tree.heading(c)["text"] for c in tree.cget("columns")]


def col_of(tree, title):
    """按表头找列号。写死下标的话,加一列就会让断言去看别的格子。"""
    return cols_of(tree).index(title)


def cell_text(tree):
    """把这棵树(含子行)所有格子的文字拼起来,给「总有某一格写着 50V」用。"""
    out = []
    for i in tree.get_children():
        out.extend(str(v) for v in tree.item(i, "values"))
        for ch in tree.get_children(i):
            out.extend(str(v) for v in tree.item(ch, "values"))
    return out


class FakeBox:
    """把弹窗顶掉,并且记下问过什么。"""

    answer = True

    def __init__(self):
        self.infos, self.warns, self.asks = [], [], []

    def askyesno(self, *a, **kw):
        self.asks.append(a)
        return self.answer

    def showinfo(self, *a, **kw):
        self.infos.append(a)

    def showwarning(self, *a, **kw):
        self.warns.append(a)

    def showerror(self, *a, **kw):
        self.warns.append(a)


try:
    import attrs
    import bom
    import db
    import gui
    import server

    print("模块导入 OK")
    before = snapshot(REAL)
    app = None

    def call(fn, body=None, match=None):
        """和桌面版一样:合成 Ctx 直接调处理函数,不经过 HTTP。"""
        ctx = gui.make_ctx(app.con, body=body)
        return fn(ctx, gui._Match(*match) if match else None)

    # ------------------------------------------------ 包里是不是新版
    print("\n--- 成品包里是不是第四轮的新版 ---")
    ck("有自定义属性这个模块", hasattr(attrs, "fmt"), True)
    ck("有品类管理窗口", hasattr(gui, "CategoryManagerDialog"), True)
    ck("库存页有「品类管理」入口",
       hasattr(gui.ComponentsTab, "manage_categories"), True)
    ck("库存页有四级菜单那一套", hasattr(gui.ComponentsTab, "_descend"), True)
    ck("品类下拉会发 category_id",
       "category_id" in io.open(os.path.join(PKG, "app", "gui.py"),
                                encoding="utf-8").read(), True)
    txt = io.open(os.path.join(PKG, "使用说明.txt"), encoding="utf-8-sig").read()
    ck("说明书里写了四级菜单", "大类 → 子类 → 封装 → 具体元件" in txt, True)
    ck("说明书里写了自定义属性", "自己定义属性" in txt, True)
    ck("说明书里写了品类管理", "品类管理" in txt, True)

    # ------------------------------------------------ 老库迁移(这一步的关键)
    print("\n--- 老库升上来:品类表要自己建出来 ---")
    tmp = os.path.join(CACHE, "_pkgprobe_r4.db")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(tmp + suffix):
            os.remove(tmp + suffix)
    shutil.copy2(REAL, tmp)

    raw = db.connect(tmp)
    n_comp = raw.execute("SELECT COUNT(*) FROM component").fetchone()[0]
    n_stock = raw.execute("SELECT COUNT(*) FROM stock").fetchone()[0]
    raw.close()
    print(f"      迁移前:元件 {n_comp} 个,库存行 {n_stock} 行")

    con = db.connect(tmp)
    db.init_db(con)
    n_cat = con.execute("SELECT COUNT(*) FROM category").fetchone()[0]
    roots = [r["name"] for r in con.execute(
        "SELECT name FROM category WHERE parent_id IS NULL").fetchall()]
    ck("迁移之后 category 表建出来了", n_cat >= 19, True)
    ck("内置那 19 个顶层品类一个不少",
       [c for c in bom.CATEGORIES if c not in roots], [])
    ck("元件一个都没少", con.execute("SELECT COUNT(*) FROM component").fetchone()[0],
       n_comp)
    ck("库存行一个都没少", con.execute("SELECT COUNT(*) FROM stock").fetchone()[0],
       n_stock)
    ck("凡是原来有品类的元件都挂上了节点",
       con.execute("""SELECT COUNT(*) FROM component
                      WHERE category_id IS NULL AND COALESCE(category,'') <> ''"""
                   ).fetchone()[0], 0)
    # 文本列必须等于它所在节点的**顶层祖先**名 —— 这是整个套路的基石
    ck("文本列和 id 列没有一处自相矛盾",
       con.execute(
           """WITH RECURSIVE up(id, parent_id, name, root) AS (
                  SELECT id, parent_id, name, name FROM category WHERE parent_id IS NULL
                  UNION ALL
                  SELECT c.id, c.parent_id, c.name, up.root
                    FROM category c JOIN up ON c.parent_id = up.id)
              SELECT COUNT(*) FROM component c JOIN up ON up.id = c.category_id
               WHERE up.root <> c.category"""
       ).fetchone()[0], 0)
    db.init_db(con)
    ck("再跑一遍迁移结果不变(用户每次启动都会走一遍)",
       con.execute("SELECT COUNT(*) FROM category").fetchone()[0], n_cat)
    con.close()

    # ------------------------------------------------ #5 属性
    print("\n--- #5 自己定义属性 ---")
    ck("属性按品类给候选", "耐压" in attrs.suggest("电容"), True)
    ck("电感给的是电流类候选", "饱和电流" in attrs.suggest("电感"), True)
    ck("合成一行的写法", attrs.fmt({"耐压": "50V", "精度": "±5%"}), "50V · ±5%")
    ck("空属性给空串,不是光秃秃的分隔符", attrs.fmt({}), "")
    ck("全名写法给面板用", attrs.fmt_full({"耐压": "50V"}), "耐压=50V")
    ck("归一化不动普通名字", attrs.norm("耐压"), "耐压")

    # ------------------------------------------------ #6/#7 界面
    print("\n--- #6/#7 四级菜单与品类管理(用真实库的副本) ---")
    app = gui.App(tmp)
    app.update()
    app.update_idletasks()
    app.nb.select(app.tab_comp)
    app.update()
    tab = app.tab_comp

    ck("库存首页照旧列出全部标准大类",
       [c for c in gui.CATEGORY_ORDER if c not in tab.cards], [])

    top = call(server.create_category, body={"name": "成品包菜单测试"})[1]
    sub = call(server.create_category,
               body={"name": "陶瓷子类", "parent_id": top["id"]})[1]
    c1 = call(server.create_component, body={
        "name": "成品包子类料", "category_id": sub["id"], "value": "1uF",
        "package": "0603"})[1]
    call(server.stock_move, body={"kind": "IN", "component_id": c1["id"], "qty": 5,
                                  "location": "未分类"})
    got1 = call(server.get_component, match=(str(c1["id"]),))[1]
    ck("元件挂在子类节点上", got1["category_id"], sub["id"])
    ck("文本列仍旧只写顶层大类名", got1["category"], "成品包菜单测试")

    app.refresh_all()
    app.update()
    ck("用户自建的顶层品类也有卡片", "成品包菜单测试" in tab.cards, True)

    tab.open_category("成品包菜单测试")
    app.update()
    ck("有子类的大类先让你选子类(不是把子类混在一张表里)", tab.view, "pick")
    ck("中间页真的画出来了", bool(tab.page_pick.winfo_ismapped()), True)
    ck("面包屑只有一级", [c["name"] for c in tab.crumb], ["成品包菜单测试"])
    ck("中间页上是那个子类", [c["name"] for c in tab._pick_items], ["陶瓷子类"])

    tab._pick_one(str(sub["id"]))
    app.update()
    ck("选了子类之后进列表页", tab.view, "cat")
    ck("面包屑两级", [c["name"] for c in tab.crumb], ["成品包菜单测试", "陶瓷子类"])
    k_nm = col_of(tab.tree, "名称")
    ck("列表里只有这个子类的料",
       [tab.tree.item(i, "values")[k_nm] for i in tab.tree.get_children()],
       ["成品包子类料"])
    ck("库存列表里没有「参数」列(14 列已经只剩 2px,塞不下)",
       "参数" in cols_of(tab.tree), False)

    ck("封装这一级的按钮列出来了", ("0603" in tab.pkg_chips, "" in tab.pkg_chips),
       (True, True))
    tab.pick_package("0805")
    app.update()
    ck("点一个这个子类里没有的封装:表清空,页面不报错",
       len(tab.tree.get_children()), 0)
    tab.pick_package("")
    app.update()
    ck("点回「全部」又有了", len(tab.tree.get_children()), 1)
    tab.crumb_to(0)
    app.update()
    ck("点面包屑的大类退回选子类那一层", tab.view, "pick")

    # ------------------------------------------------ 属性真的能存能显示
    print("\n--- 属性在成品包里存得进、显示得出 ---")
    ck("保存属性成功",
       call(server.update_component, body={"params": {"耐压": "50V", "精度": "±5%"}},
            match=(str(c1["id"]),))[0], 200)

    d = gui.ComponentDialog(app, app, c1["id"])
    d.update()
    got_attrs = set()
    for _row, _cb, var_n, var_v in d.attr_rows:
        got_attrs.add(var_n.get())
        got_attrs.add(var_v.get())
    ck("元件编辑窗口里能改回那两个属性",
       {"耐压", "50V", "精度", "±5%"} <= got_attrs, True)
    ck("品类那格是只读的,改它得点「换…」(不再是能随便打字的框)",
       "readonly" in d.cb_cat.state(), True)
    ck("打开时显示的就是全路径(不然一保存子类归属就被冲掉)",
       d.vars["category"].get(), "成品包菜单测试 / 陶瓷子类")

    # #9:选品类改成一级一级弹 —— 框里只放这一级的名字,不放「大类 / 子类」长路径
    pick = gui.CategoryPickerDialog(app, app, d._picked_cat_id)
    pick.update()
    lv1 = list(pick.step._rows[0][1].cget("values"))
    ck("逐级选:第一级只有大类,没有「大类 / 子类」拼起来的长路径",
       [v for v in lv1 if " / " in v], [])
    ck("第一级里选中的就是这颗料的大类", pick.step._rows[0][1].get(),
       "成品包菜单测试")
    ck("有子类才会长出第二级", len(pick.step._rows), 2)
    ck("第二级里就是那个子类", pick.step._rows[1][1].get(), "陶瓷子类")
    ck("顺着选到底拿到的是子类那个节点,不是大类",
       pick.step.current_node()["name"], "陶瓷子类")
    pick.destroy()
    d.destroy()

    # 收料/出库这两屏才是用户点名要看属性的地方 —— 现造一个项目和一条需求
    pj = call(server.create_project, body={"name": "成品包属性项目"})[1]
    call(server.add_bom_line, body={"component_id": c1["id"], "required_qty": 50},
         match=(str(pj["id"]),))
    app.refresh_all()
    app.update()
    t_proj = app.tab_proj.t_proj
    hit = [i for i in t_proj.get_children()
           if "成品包属性项目" in str(t_proj.item(i, "values"))]
    ck("项目出现在左边列表里", len(hit), 1)
    t_proj.selection_set(hit[0])
    app.update()
    app.update_idletasks()
    ck("左边选中了它", app.tab_proj._pid, pj["id"])

    recv = app.tab_proj.pane_in.bom_form.tree
    ck("收料清单里有「参数」列", "参数" in cols_of(recv), True)
    ck("收料清单里那颗料的属性显示出来了",
       "50V · ±5%" in cell_text(recv), True)

    pick = app.tab_proj.pane_out.bom_form.tree
    ck("出库分配里有「参数」列", "参数" in cols_of(pick), True)
    ck("出库分配的候选行上也显示出来了",
       any("50V" in v for v in cell_text(pick)), True)

    det = app.tab_proj.detail
    det.show(app.tab_proj.pane_out.bom_form.lines[0])
    app.update()
    ck("BOM 明细详情面板写的是全名",
       det.v2["params"].get(), "耐压=50V    精度=±5%")
    det.clear()

    # ------------------------------------------------ 品类管理窗口
    print("\n--- 品类管理窗口 ---")
    dlg = gui.CategoryManagerDialog(app, app)
    dlg.update()
    ck("窗口里列出了树", top["id"] in dlg._flat, True)
    ck("子类挂在父类下面", dlg.tree.parent(str(sub["id"])), str(top["id"]))
    ck("全路径拼好了", dlg._flat[sub["id"]]["path"], "成品包菜单测试 / 陶瓷子类")
    ck("含子类的元件数统计出来了", int(dlg._flat[top["id"]]["total"]) >= 1, True)

    real_box, real_ask = gui.messagebox, gui.ask_text
    box = FakeBox()
    gui.messagebox = box
    gui.ask_text = lambda *a, **kw: "成品包新品类"
    try:
        dlg.add_root()
        dlg.update()
    finally:
        gui.ask_text = real_ask
    ck("窗口里能加顶级品类",
       "成品包新品类" in [n["path"] for n in dlg._flat.values()], True)

    gui.messagebox = box
    try:
        dlg.tree.selection_set(str(sub["id"]))
        dlg.remove()
        dlg.update()
    finally:
        gui.messagebox = real_box
    ck("删品类之前问了一句", len(box.asks) >= 1, True)
    ck("而且说清了元件不会被删",
       "不会被删除" in " ".join(str(a) for a in box.asks[-1]), True)
    left = app.con.execute("SELECT category, category_id FROM component WHERE id=?",
                           (c1["id"],)).fetchone()
    ck("删品类之后元件还在", left is not None, True)
    ck("而且挪到了大类下面(不是被删,也不是变成未分类)",
       (left["category"], left["category_id"]), ("成品包菜单测试", top["id"]))
    ck("子类节点真的没了", sub["id"] in dlg._flat, False)
    dlg.destroy()
    app.update()

    # ------------------------------------------------ #4 删项目要连明细一起清
    print("\n--- #4 删掉项目之后明细也要跟着走 ---")
    box2 = FakeBox()
    gui.messagebox = box2
    try:
        app.tab_proj.delete_project()
        app.update()
    finally:
        gui.messagebox = real_box
    tt = app.tab_proj
    ck("删项目之后明细一条不剩(#4 的原始毛病)",
       app.con.execute("SELECT COUNT(*) FROM project_bom WHERE project_id=?",
                       (pj["id"],)).fetchone()[0], 0)
    ck("项目列表里也没有它了",
       [i for i in t_proj.get_children()
        if "成品包属性项目" in str(t_proj.item(i, "values"))], [])
    # 这份库是用户自己的,可能本来就有他的项目 —— 有的话,删完会自动切到那个项目上,
    # 那是对的行为。所以分两种情形断言,但都不能出现「已删项目」的痕迹。
    _rest = app.con.execute("SELECT id FROM project ORDER BY id").fetchall()
    if not _rest:
        ck("库里没别的项目时,明细表一张干净的表", len(tt.t_bom.get_children()), 0)
        ck("收料清单也跟着空了", len(recv.get_children()), 0)
        ck("标题回到「选一个项目」,没留着那个已经不存在的项目",
           "选一个项目" in tt.title.get(), True)
    else:
        _ids = [r["id"] for r in _rest]
        ck("删掉之后切到了库里还存在的项目(没赖在已删的那个上)",
           tt._pid in _ids, True)
        ck("标题不再写着那个已经删掉的项目",
           "成品包属性项目" in tt.title.get(), False)
        ck("明细表里那个项目一行都不剩",
           [str(tt.t_bom.item(i, "values")) for i in tt.t_bom.get_children()
            if "成品包属性项目" in str(tt.t_bom.item(i, "values"))], [])
        ck("标题上的数字是还存在的那个项目",
           tt.title.get() != "", True)
        print(f"       (库里另有 {len(_ids)} 个项目,删完自动切到了 id={tt._pid})")

    app.destroy()
    app = None
except Exception:
    traceback.print_exc()
    bad.append("探针自己抛异常了")

# ---------------------------------------------------- 收尾与原库校验
# 临时库可能还被 sqlite 的连接占着(Windows 上删不掉),删不掉不算失败 ——
# 它在 build/cache/ 下,本来就不进版本库
for suffix in ("", "-wal", "-shm"):
    p = os.path.join(CACHE, "_pkgprobe_r4.db" + suffix)
    try:
        if os.path.exists(p):
            os.remove(p)
    except OSError:
        pass
after = snapshot(REAL)
ck("用户真实数据库一个字节都没被动过", after, before)

print()
if bad:
    print("结果:FAIL", bad)
    sys.exit(1)
print("结果:成品包第四轮 PASS —— 老库能升上来,四级菜单/自定义属性/品类管理都能用")
