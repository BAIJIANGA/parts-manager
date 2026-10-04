# -*- coding: utf-8 -*-
"""桌面版自检 —— 不开窗口也能给界面和数据层做一次完整体检。

用法(在 parts-manager 目录下):
    python build\\test_gui.py

做法:
  * 先把真实数据库复制一份到 dist/selftest.db,所有操作只动副本,**不碰你的数据**
  * 把整个窗口、5 个标签页、3 个弹窗都真的构造出来并渲染(能抓出控件参数写错、
    布局冲突这类只在运行时才暴露的问题)
  * 跑一遍代表性的数据操作(增/改/删元件、入库/盘点/移库、超额出库、重建校验)
  * 结果写到 build/cache/gui_selftest.txt(UTF-8,避免控制台编码把中文弄乱)

退出码 0 = 全部通过。
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import tkinter as tk
import traceback

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
APP = os.path.join(ROOT, "app")
sys.path.insert(0, APP)

import db        # noqa: E402
import gui       # noqa: E402
import server    # noqa: E402


# ---------------------------------------------------------------------------
# 全局兜底打桩 —— 必须在**所有段落之前**装好(下面任何一段漏桩都不会弹出去)
# ---------------------------------------------------------------------------
# **为什么非要有这一道(别觉得多余就删掉):**
# 各段落自己有几十处 `gui.messagebox = boxNN`,但每一处只管住它自己那几行代码。
# 段落一多,总有一段会走到校验分支却没打桩 —— 实测就漏过一次:【27】「填了不是
# 数字的东西」那一段只桩了 `gui.ask_text`,于是 `BomReceivePane.edit_qty` 里的
# `messagebox.showinfo("提示", "数量要填非负整数。")` 直接弹到**用户桌面**上,
# 模态框卡在那里等人点,整个自检跟着停住。用户已经为「弹框」抱怨过三次,所以
# 这里不再指望每一段都记得打桩:顶部先兜一道,之后无论哪一段漏了,最多是这条
# 提示被 FALLBACK 记下来,绝不会弹到桌面上。
# 段落自己的 `gui.messagebox = boxNN` **照旧保留** —— 它们要断言「提示了什么」,
# 而它们的还原是 `gui.messagebox = _mbNN`(段落开始时捕获的那一份,正是这一道
# 兜底),所以还原之后仍然落在兜底上,不会把兜底冲掉。文件末尾另有三句断言看住
# 这件事:跑完所有段落,兜底(和取色器兜底)必须还在,而且不该有任何一条提示
# 从兜底漏过去 —— 漏过去就说明那一段忘了打桩。
sys.path.insert(0, os.path.join(ROOT, "build", "checks"))
import _stub_dialogs                                     # noqa: E402
FALLBACK = _stub_dialogs.silence(gui)                    # messagebox + colorchooser
FALLBACK_CHOOSER = gui.colorchooser

REAL_DB = os.path.join(ROOT, "data", "parts.db")
# 自检的临时库和结果都放 build\cache\ —— dist\ 只放成品,不往里丢别的东西
CACHE = os.path.join(ROOT, "build", "cache")
TEST_DB = os.path.join(CACHE, "selftest.db")
RESULT = os.path.join(CACHE, "gui_selftest.txt")

OUT = io.StringIO()
FAILS: list[str] = []


def p(*a):
    OUT.write(" ".join(str(x) for x in a) + "\n")


def check(label, got, want):
    ok = got == want
    if not ok:
        FAILS.append(f"{label}: 得到 {got!r},期望 {want!r}")
    p(f"  [{'OK ' if ok else 'FAIL'}] {label}: {got!r}" + ("" if ok else f"  期望 {want!r}"))
    return ok


def buttons_of(root):
    """收集一棵控件树里所有按钮的文字。用来确认某个入口真的摆出来了。"""
    out = []

    def walk(w):
        for c in w.winfo_children():
            try:
                if isinstance(c, (gui.ttk.Button, tk.Button)):
                    out.append(str(c.cget("text")))
            except tk.TclError:
                pass
            walk(c)
    walk(root)
    return out


def col_of(pane, key):
    """按列名找列号。

    收料清单和出库分配树的列是会变的(加「参数」那一列就是这样)。断言里写死
    `values[8]` 的话,加一列就全部串位 —— 而串位之后**断言往往还是「通过」的**,
    只是它验的已经不是原来那件事了,那比直接失败更糟。所以这两张表一律按列名取。
    列名找不到时直接抛异常,不静默算出一个错的列号。
    """
    return [c[0] for c in pane.COLS].index(key)


def all_rows(tree, parent=""):
    """把一棵 Treeview 里所有行摊平(组行的子行也算在内)。

    流水按项目折叠之后(#30),`tree.get_children()` 拿到的是**组行**,
    真正的流水行挂在它们下面而且默认收着。断言里直接数 get_children() 的话,
    数的就不是流水 —— 而那种断言往往还是「通过」的,只是它验的已经不是
    原来那件事了,那比直接失败更糟。
    """
    out = []
    for iid in tree.get_children(parent):
        out.append(iid)
        out += all_rows(tree, iid)
    return out


def want_cards(tab, con):
    """此刻首页**应该**有的卡片名单(#32 的契约),外加「库里到底有没有没品类的料」。

    issue #32 把首页卡片改成了**完全数据驱动**:卡片 = 库里真有的顶层品类,
    外加一张**算出来的**「未分类」—— 它只在库里真有没有品类的元件时才出现
    (为的是「删掉一个顶层大类之后,那些料不会变得找不到」)。
    所以「卡片 == 顶层品类,一个不多一个不少」这种老写法在库里有没品类的料时
    必然是假的;而把「未分类」从期望名单里直接划掉,又等于把这条新契约的看门狗
    摘了 —— 哪天它变回一张常驻卡片,断言反而不会红。两个方向都要咬住,口径就
    写在这一处,别再各节自己拼一遍(拼出来的迟早不一致)。

    顶层品类走后端 list_categories,**不是** tab._cat_flat:拿界面自己那份缓存
    当期望,就验不出「缓存和库里对不上」。
    「有没有没品类的料」用界面自己那把尺子(tab._uncat_items -> _is_uncat:看
    category_id 对不对得上品类树,不看元件上那行旧文本),而且数**全库** ——
    零库存的料也算「库里有这种料」,这张卡片不该随库存忽隐忽现。

    返回 (顶层品类名(排序后), 期望的卡片名集合, 库里有没有没品类的料)。
    """
    tree = gui.call(con, server.list_categories, quiet=True) or {}
    tops = sorted((n.get("name") or "").strip() for n in (tree.get("flat") or [])
                  if n.get("parent_id") is None and (n.get("name") or "").strip())
    items = gui.call(con, server.list_components, query={"limit": 0}, quiet=True) or {}
    uncat = bool(tab._uncat_items(items.get("items") or []))
    want = set(tops) | ({gui.UNCATEGORIZED} if uncat else set())
    return tops, want, uncat


def treeviews(root):
    """收集一棵控件树里所有的 Treeview —— 用来一次性体检「所有类表格」。"""
    out = []

    def walk(w):
        for c in w.winfo_children():
            if isinstance(c, gui.ttk.Treeview):
                out.append(c)
            walk(c)
    walk(root)
    return out


def off_center(tree):
    """这张表里**没居中**的列(单元格或表头),返回 [(列, 实际值), ...]。

    列宽和 stretch 不在这里查 —— #27 只动对齐,专门另有断言看住列宽没被顺手改掉。
    """
    cols = [str(c) for c in tree["columns"]]
    # #0 树列不列在 columns 里,show 里带 tree 时它才显示
    if "tree" in str(tree.cget("show")):
        cols = ["#0"] + cols
    bad = []
    for c in cols:
        cell = str(tree.column(c, "anchor"))
        if cell != "center":
            bad.append((c, cell))
        head = str(tree.heading(c, "anchor"))
        if head != "center":
            bad.append((f"{c} 表头", head))
    return bad


def card_labels(card):
    """把一张大类卡片上所有 Label 的文字取出来(卡片是 tk.Frame)。"""
    out = []
    for w in card.winfo_children():
        if isinstance(w, tk.Label):
            out.append(w.cget("text"))
        elif isinstance(w, tk.Frame):
            out += [c.cget("text") for c in w.winfo_children() if isinstance(c, tk.Label)]
    return out


def color_str(raw):
    """把 Tk 取回来的颜色变成 '#rrggbb' 字符串。

    Tk 返回的是个「颜色对象」,repr 形如 <color object: '#fff6dd'>;拿它跟字符串
    直接比会因为类型不同而**假失败** —— 而这种失败最容易被误读成「功能没生效」,
    所以统一的在这里抠出 #rrggbb 那一段。
    """
    txt = str(raw)
    if "#" in txt:
        return "#" + txt.split("#", 1)[1][:6].lower()
    return txt


def effective_bg(tree, iid):
    """这一行**实际**会用的背景色。

    **先把「谁赢」这条口径写对(#34 实测改的,旧注释是错的):** 一行可以挂好几个
    tag,同一个选项(背景色)谁生效由 Tk 说了算,**不是**「行上 tags 列表里的先后」
    —— 6 种创建顺序 + 把抢色的 tag 换成第 4 个做下来,规则都是**谁先 tag_configure
    谁赢,`item(..., tags=[…])` 里写的顺序完全不起作用**。内置那两条规则色(out /
    low)是在建表时就 configure 的,比后面才建的自定义 tag 更早,所以真让两个 tag
    同时管背景色,赢的是规则色。

    那这里为什么还能这么取?因为代码里**一行最多只有一个 tag 配背景色**
    (_row_tags 里用户设了背景色就**不再挂**规则 tag),这条口径在这前提下才是
    屏幕真刷出来的那个颜色 —— 【45】那一节有一条断言专门钉住这个前提。
    取法:按 tags 列表走,遇到第一个配了背景色的就用它(背景色是空串 = 这个 tag
    没设置这一项,跳过)。要是以后真让一行挂两个配背景色的 tag,这个函数就得
    改成别的办法(比如截屏比像素),别照着现在这段推。
    """
    for tag in tree.item(iid, "tags"):
        raw = tree.tag_configure(str(tag), "background")
        if str(raw):
            return color_str(raw)
    return ""


def menu_labels(menu):
    """一张菜单里所有命令项的 (文字, 状态),分隔线不算。"""
    out = []
    end = menu.index("end")
    for i in range(0 if end is None else end + 1):
        if str(menu.type(i)) == "command":
            out.append((str(menu.entrycget(i, "label")),
                        str(menu.entrycget(i, "state"))))
    return out


def main() -> int:
    p("=" * 70)
    p("元器件物料管理系统 —— 桌面版自检")
    p("=" * 70)

    if not os.path.isfile(REAL_DB):
        p(f"找不到数据库:{REAL_DB}")
        p("(先随便用一次程序,让它自己建库)")
        return 2
    os.makedirs(os.path.dirname(TEST_DB), exist_ok=True)
    shutil.copy2(REAL_DB, TEST_DB)
    p(f"已复制数据库副本 -> {TEST_DB}\n")

    real_count = None
    try:
        # ---------------------------------------------------------- 数据层
        p("【1】数据层(界面调用的就是这些函数)")
        con = db.connect(TEST_DB)
        db.init_db(con)

        def ctx(**kw):
            return gui.make_ctx(con, **kw)

        M = gui._Match

        s = server.summary(ctx(), None)[1]
        real_count = s["components"]
        p(f"  元件 {s['components']} 种,项目 {s['projects']} 个,"
          f"缺货 {s['out']} 种,偏低 {s['low']} 种")

        d = server.list_components(ctx(query={"sort": "category"}), None)[1]
        check("元件列表条数", d["total"], s["components"])
        check("列表字段完整", all(k in d["items"][0] for k in
                                  ("name", "on_hand", "stock_state", "lcsc_pn")), True)

        check("缺货筛选条数", server.list_components(ctx(query={"state": "out"}), None)[1]["total"],
              s["out"])

        m = server.meta(ctx(), None)[1]
        p(f"  品类 {len(m['categories'])} 个,仓位 {len(m['locations'])} 个")
        check("meta 有仓位字段", "code" in (m["locations"][0] if m["locations"] else {"code": "x"}), True)

        # 用「库里真实存在」的品类名去搜,验证「品类也参与搜索」确实生效
        real_cats = (m.get("filters") or {}).get("categories") or []
        probe = next((c[:2] for c in real_cats if c and len(c) >= 2), None)
        if probe:
            dq = server.list_components(ctx(query={"q": probe}), None)[1]
            p(f"  按品类「{probe}」搜索命中 {dq['total']} 条")
            check("按品类搜索可用", dq["total"] > 0, True)

        pl = server.list_projects(ctx(), None)[1]
        p(f"  项目 {len(pl['items'])} 个")
        if pl["items"]:
            pid = pl["items"][0]["id"]
            rep = server.project_bom(ctx(), M(pid))[1]
            p(f"  项目「{rep['project']['name']}」BOM {len(rep['lines'])} 行,"
              f"缺料 {rep.get('shortage_lines')} 种 / {rep.get('shortage_qty')} 个")
            check("BOM 行数与项目列表一致", len(rep["lines"]), pl["items"][0]["bom_lines"])
            check("BOM 有缺口字段", "shortage_qty" in rep, True)

        # ---------------------------------------------------------- 增删改与出入库
        p("\n【2】增删改与出入库(在副本上做,做完删干净)")
        NAME = "SELFTEST-\u81ea\u68c0"
        cid = server.create_component(
            ctx(body={"name": NAME, "min_stock": 5, "category": "SELFTEST"}), None)[1]["id"]
        got = server.get_component(ctx(), M(cid))[1]
        check("新建后能读回中文名", got["name"], NAME)
        check("新建默认缺货", got["stock_state"], "out")

        r = server.stock_move(ctx(body={"kind": "IN", "component_id": cid, "qty": 10,
                                        "location": "\u672a\u5206\u7c7b"}), None)[1]
        check("入库 10", (r["qty_at_location"], r["on_hand"]), (10, 10))

        r = server.stock_move(ctx(body={"kind": "OUT", "component_id": cid, "qty": 4,
                                        "location": "\u672a\u5206\u7c7b"}), None)[1]
        check("出库 4", (r["qty_at_location"], r["on_hand"]), (6, 6))

        r = server.stock_move(ctx(body={"kind": "ADJUST", "component_id": cid, "qty": 3,
                                        "location": "\u672a\u5206\u7c7b"}), None)[1]
        check("盘点为 3(绝对数,不是差值)", (r["qty_at_location"], r["on_hand"]), (3, 3))

        r = server.stock_move(ctx(body={"kind": "TRANSFER", "component_id": cid, "qty": 2,
                                        "location": "\u672a\u5206\u7c7b",
                                        "to_location": "SELFTEST-LOC"}), None)[1]
        check("移库 2 后目标仓位", r["qty_at_location"], 2)
        check("移库后总量不变", r["on_hand"], 3)

        try:
            server.stock_move(ctx(body={"kind": "OUT", "component_id": cid, "qty": 9999,
                                        "location": "SELFTEST-LOC"}), None)
            FAILS.append("超额出库没有被拒绝")
            p("  [FAIL] 超额出库没有被拒绝")
        except server.ApiError as exc:
            p(f"  [OK ] 超额出库被拒绝:{exc.message}")

        kinds = [x["kind"] for x in server.list_movements(
            ctx(query={"component_id": str(cid)}), None)[1]["items"]]
        check("流水条数", len(kinds), 4)
        check("流水顺序(倒序)", kinds, ["TRANSFER", "ADJUST", "OUT", "IN"])

        server.update_component(ctx(body={"min_stock": 99}), M(cid))
        check("改最低库存", server.get_component(ctx(), M(cid))[1]["min_stock"], 99)

        check("按流水重建余额差异为 0", server.rebuild(ctx(), None)[1]["diff_count"], 0)

        server.delete_component(ctx(query={"force": "1"}), M(cid))
        con.execute("DELETE FROM location WHERE code='SELFTEST-LOC'")
        # ---- 收尾:上面那颗料是用 category="SELFTEST"(一个**名字**)建的,后端按
        #      名字 ensured 了一行同名品类;料删了,那一行还留着 —— 它会以「库里
        #      真有的一行」的身份在首页占一张没有元件的空卡片,后面每一节「卡片
        #      名单对账」都得把它算进去、还得单独删它。测试自己造出来的东西,用完
        #      就收掉(照上面删 SELFTEST-LOC 那个仓位的写法)。
        #      只收自己造的那一种形态:**没挂在任何元件上**的空行。真挂着料的
        #      SELFTEST(脏库里可能真有)不算测试垃圾,不动它。
        _sid0 = con.execute(
            "SELECT id FROM category WHERE parent_id IS NULL AND name='SELFTEST' "
            "AND (SELECT COUNT(*) FROM component WHERE category_id=category.id)=0"
        ).fetchone()
        if _sid0 is not None:
            server.delete_category(ctx(), M(str(_sid0["id"])))
        con.commit()
        check("收尾:SELFTEST 这个测试品类没留在库里(后面的段落不受影响)",
              con.execute("SELECT COUNT(*) FROM category WHERE name='SELFTEST' "
                          "AND (SELECT COUNT(*) FROM component "
                          "     WHERE category_id=category.id)=0").fetchone()[0], 0)
        check("清理后元件数复原", server.summary(ctx(), None)[1]["components"], real_count)
        con.close()

        # ---------------------------------------------------------- 界面
        p("\n【3】界面构造(真的把窗口建出来并渲染)")
        app = gui.App(TEST_DB)
        app.update()
        p(f"  窗口 {app.winfo_width()}x{app.winfo_height()},状态栏:{app.status.get()}")

        wanted = ["总览", "库存", "出入库", "项目 BOM", "采购", "仓位", "流水"]
        check("页签数量和名字都对", app.nb.index("end"), len(wanted))
        for i, name in enumerate(wanted):
            app.nb.select(i)
            app.update()
            app.update_idletasks()
            check(f"标签页「{name}」的标题", app.nb.tab(i, "text").strip(), name)
        p(f"  [OK ] {len(wanted)} 个标签页全部渲染正常")

        # 后面的断言都针对库存页 —— 必须真的切到它,否则控件不算被显示出来
        app.nb.select(app.tab_comp)
        app.update()
        tab = app.tab_comp

        # ---- 首页卡片名单**完全数据驱动**(#32):卡片 = 库里真有的顶层品类,
        # 外加一张**算出来的**「未分类」—— 它只在库里真有没有品类的元件时才出现。
        # 写死的那 16 个标准大类名不见得在库里都有行,画出来就是一张没有对应行的
        # 空卡片 —— 用户点它右键的「删除…」删不掉(没有任何一行可删),这正是
        # 「有东西删不掉」的根。
        # 要断言的是**三件事**,少一件这条契约就没人看着了:
        #   1) 库里真有的顶层品类,首页一张卡片都不许少(少一张 = 那类料没入口);
        #   2) 写死名单里、库里却没有对应行的名字,一张卡片都不许画(空卡片不许回来);
        #   3) 「未分类」这张数据驱动的卡片:**有**没品类的料时才许有,**一件都没有
        #      时不许有**。两个方向都要咬住 —— 只把它从期望名单里划掉等于把这条
        #      新契约的看门狗摘了(哪天它变回一张常驻卡片,断言反而不会红)。
        _tops3, _want3, _uncat3 = want_cards(tab, app.con)
        _tops3s = set(_tops3)
        check("库里真有的顶层品类,首页一张卡片都不少",
              [c for c in _tops3 if c not in tab.cards], [])
        check("首页卡片名单 = 库里真有的顶层品类(+ 真有没品类的料时才多一张「未分类」)",
              sorted(tab.cards), sorted(_want3))
        check("「未分类」卡片跟「库里真有没品类的元件」一一对应(不是常驻卡片)",
              gui.UNCATEGORIZED in tab.cards,
              bool(_uncat3) or gui.UNCATEGORIZED in _tops3s)
        check("写死名单里但库里没有对应行的名字不再画成卡片",
              [c for c in gui.CATEGORY_ORDER if c not in _tops3s and c in tab.cards], [])
        check("卡片数 = 名单数", len(tab.cards), len(tab._card_names))
        check("没有库存时停在首页", tab.view, "home")
        check("没有库存时明细表是空的", len(tab.tree.get_children()), 0)
        # 这一节(以及下面【4】)所有「暂无库存」的断言都建立在「副本库里本来就是
        # 0 库存」这个前提上 —— tab._all 就是有库存的那一批。前提不成立时就在这里
        # 报出来,而不是让后面几条报出一串看不出原因的错误。
        check("前提:副本库里本来一件库存都没有", len(tab._all), 0)

        empty_cats = [n for n in tab._card_names if not tab._cat_data.get(n)]
        check("库存全 0,所以每张大类卡片都标着「暂无库存」",
              all("暂无库存" in "".join(card_labels(tab.cards[n])) for n in empty_cats), True)
        check("而且真有「没库存」的卡片在(上一条不是句废话)", len(empty_cats) > 0, True)
        p(f"  库存全 0:{len(tab.cards)} 张卡片都在(名单来自品类树),都标着「暂无库存」")

        # ---- 点进一个「库里没有料」的大类:二级页面能打开,里面什么都没有
        # 挑哪一张也不能写死:以前这里写死挑「磁珠」—— 那时卡片名单是写死的,
        # 写死的名字一定在;现在名单只列库里真有的,再假定某个名字一定有卡片,
        # 验的就不是「名单对不对」了。
        # 而且「没货」≠「没料」:「显示零库存」默认是**开着**的(新加的料库存 0 也
        # 得看得见,issue #22),所以二级页面不是只列有库存的。这里分两张卡片,各验
        # 各的:
        #   * 库里一件料都没有的大类 -> 页面必须是空的、写着「暂无库存元件」;
        #   * 有料、只是没入过库的大类 -> 页面按「显示零库存」开关给结果。
        # 上一版拿「不在 tab._cat_data 里」(= 没有**有库存的**料)当「空页」验:
        # 干净库里挑中的恰好是一件料都没有的那张(三极管/MOS)所以碰对了,而库里
        # 真有零库存的料时(脏库)挑中的是 SELFTEST —— 它下面挂着 1 颗没入过库的
        # 料,页面当然有 1 行,那两条断言当场就红。这不是应用回归,是断言把「没货」
        # 读成了「没料」。
        _all3 = (gui.call(app.con, server.list_components, query={"limit": 0},
                          quiet=True) or {}).get("items") or []
        _with3 = {tab._card_name_of(it) for it in _all3}
        # 「未分类」那张卡片不进这两个挑选:它可能是**算出来的**入口(库里没有
        # 对应行),二级页走的是另一套判定(_is_uncat),这一节不验它(【42】专门验)。
        _cand3 = [n for n in _tops3 if n in tab.cards and n != gui.UNCATEGORIZED]
        probe = next((n for n in _cand3 if n not in _with3), None)
        _zero3 = next((n for n in _cand3 if n in _with3 and n not in tab._cat_data), None)
        check("挑得出一张「卡片在、库里真有行、一件料都没有」的大类来点",
              (probe in tab.cards, probe in _tops3s, probe not in _with3),
              (True, True, True))
        if probe is not None:
            tab.open_category(probe)
            app.update()
            check("点进空的大类也能打开二级页面", tab.view, "cat")
            check("库里一件料都没有的大类,二级页面一行都不显示",
                  len(tab.tree.get_children()), 0)
            check("空的大类给出「暂无库存元件」", tab.cat_count.get(), "暂无库存元件")
            tab.go_home()
            app.update()
        check("挑得出一张「卡片在、库里有料、只是都没入过库」的大类来点",
              (_zero3 in tab.cards, _zero3 not in tab._cat_data),
              (True, True))
        if _zero3 is not None:
            _node3 = next(n for n in tab._cat_flat.values()
                          if n["parent_id"] is None and n["name"] == _zero3)
            _n3 = len((gui.call(app.con, server.list_components,
                                query={"limit": 0, "category_id": str(_node3["id"])},
                                quiet=True) or {}).get("items") or [])
            check("这个大类库里真有料(下面两条不是句废话)", _n3 > 0, True)
            _vis3 = bool(tab.zero_stock.get())      # 「显示零库存」此刻开着没有
            tab.open_category(_zero3)
            app.update()
            check("有料没库存的大类点进去:零库存能看见就列出来,关着就空着(口径跟着开关走)",
                  (len(tab.tree.get_children()), tab.cat_count.get()),
                  (_n3, f"共 {_n3} 种") if _vis3 else (0, "暂无库存元件"))
            tab.go_home()
            app.update()

        # ---- 入一点货,对应的大类才从「暂无库存」变成有东西
        p("\n【4】入库之后对应大类才有东西,而且反复刷新不能把卡片弄丢")
        items = gui.call(app.con, server.list_components,
                         query={"sort": "category"}, quiet=True)["items"]
        by_cat = {}
        for it in items:
            by_cat.setdefault((it.get("category") or "").strip() or "未分类", []).append(it)
        chosen = []
        for c in sorted(by_cat, key=tab._cat_rank)[:2]:
            chosen += [(c, it) for it in by_cat[c][:2]]
        for _cat, it in chosen:
            gui.call(app.con, server.stock_move,
                     body={"kind": "IN", "component_id": it["id"], "qty": 7,
                           "location": "未分类"}, quiet=True)
        app.refresh_all()
        app.update()

        _tops4, _want4, _uncat4 = want_cards(tab, app.con)
        _tops4s = set(_tops4)
        check("入库后卡片一张没少(还是库里真有的那些,不是只剩有货的)",
              [c for c in _tops4 if c not in tab.cards], [])
        check("入库后也没多出卡片来(「未分类」按库里有没有没品类的料算)",
              [c for c in tab.cards if c not in _want4], [])
        check("写死名单里但库里没有对应行的名字照样不出卡片(空卡片没有了)",
              [c for c in gui.CATEGORY_ORDER if c not in _tops4s and c in tab.cards], [])
        # 入过货的那几个大类必须不再标「暂无库存」。按**卡片名**取,不按元件上那行
        # 文本 —— 卡片名走 _card_name_of(对不上品类树的一律算「未分类」),脏数据里
        # 文本和卡片名会对不上。
        _chosen4 = sorted({tab._card_name_of(it) for _c, it in chosen})
        check("入过货的大类不再标「暂无库存」",
              (all(c in tab.cards for c in _chosen4),
               [c for c in _chosen4 if "暂无库存" in "".join(card_labels(tab.cards[c]))]),
              (True, []))
        # 没入过货的卡片照旧标「暂无库存」—— 按当下的卡片名单**全量**对一遍,
        # 而不是盯着上面挑出来的那一个:那是入库**之前**挑的快照,刷新之后卡片
        # 名单本来就可能变(未分类会现身/消失),拿旧快照当 key 去索引新的
        # tab.cards 正是上一版崩在 KeyError 的那条路。
        _empty4 = [n for n in tab._card_names if n in tab.cards and not tab._cat_data.get(n)]
        check("没入过货的卡片照旧标着「暂无库存」",
              [n for n in _empty4 if "暂无库存" not in "".join(card_labels(tab.cards[n]))], [])
        check("而且真有没入过货的卡片在(上一条不是句废话)", len(_empty4) > 0, True)
        check("在库元件就是入过货的那几个",
              sorted(x["id"] for x in tab._all), sorted({it["id"] for _, it in chosen}))
        check("在库元件库存都 > 0", all(x["on_hand"] > 0 for x in tab._all), True)
        check("有元件被库存过滤掉(证明 SQL 层过滤真的生效)",
              len(items) - len(tab._all) > 0, True)
        # 卡片尺寸必须就是设定值 —— 曾经因为子控件是 pack 的、却只关了
        # grid_propagate,卡片被内容撑成 130x72,这里看住它
        check("卡片尺寸就是设定值,没被内容撑变",
              {(c.winfo_width(), c.winfo_height()) for c in tab.cards.values()},
              {(gui.CARD_W, gui.CARD_H)})

        # ---- 关键回归:每次刷新都会把卡片 destroy 再重建,如果重排时列表里还留着
        #      上一步销毁的控件,对它们调 .grid() 会抛 TclError("bad window path
        #      name"),整次 reload 中断 —— 表现就是「入库之后首页一片空白」。
        for _ in range(3):
            app.refresh_all()
            app.update()
        # 刷 3 次之后重新取一次事实再对(不要拿刷新前的快照来比:那正是「半新状态」
        # 式的假红/假通过 —— 每一轮刷新都可能让「未分类」那张卡片现身或消失)。
        _tops4b, _want4b, _uncat4b = want_cards(tab, app.con)
        check("连刷 3 次,卡片一张都不少(入库后首页没变空白)",
              [c for c in _tops4b if c not in tab.cards], [])
        check("连刷 3 次之后也没多出卡片来(名单还是那一套)",
              [c for c in tab.cards if c not in _want4b], [])
        check("连刷 3 次后卡片真的显示在界面上",
              all(c.winfo_ismapped() for c in tab.cards.values()), True)
        check("重排列表里没有残留的死控件", len(tab.board._seq), len(tab.cards))
        check("重排列表里的控件全都还活着",
              all(c.winfo_exists() for c in tab.board._seq), True)
        pos = [(c.grid_info().get("row"), c.grid_info().get("column"))
               for c in tab.cards.values()]
        check("每张卡片占的格子都不一样,没有互相盖住", len(set(pos)), len(pos))
        p(f"  连刷 3 次后 {len(tab.cards)} 张卡片仍全部可见,占 {len(set(pos))} 个格子")

        # ---- 点卡片 -> 独立的二级页面(不是树形展开)
        cat0, first = chosen[0]
        n0 = len(tab._cat_data[cat0])
        # 二级页的表格和首页卡片是同一个列表控件;卡片数的是**有货的**,
        # 所以这里先关掉「显示零库存」,让两者的口径一致(#22 把这个开关
        # 交给了用户,默认开着是为了「加完料就看得见」)
        tab.zero_stock.set(False)
        tab.open_category(cat0)
        app.update()
        check("点卡片后切到二级页面", tab.view, "cat")
        check("二级页面记住了在看哪个大类", tab.current_category, cat0)
        check("二级页面已显示出来", bool(tab.page_cat.winfo_ismapped()), True)
        check("首页已收起", bool(tab.page_home.winfo_ismapped()), False)
        check("二级表行数 = 该大类在库元件数", len(tab.tree.get_children()), n0)
        cols = [tab.tree.heading(c)["text"] for c in tab.tree["columns"]]
        k_qty = cols.index("现有")
        check("二级表里每行库存都 > 0",
              all(float(tab.tree.item(k, "values")[k_qty]) > 0
                  for k in tab.tree.get_children()), True)
        # 库存页只讲库存:我有多少 / 安全线是多少
        for col in ("现有", "安全"):
            check(f"二级表有「{col}」列", col in cols, True)
        # 需求/缺口/在途/该买是**项目 BOM 的缺料口径**,摆在库存页会让人以为"我缺料了"。
        # 项目页和 BOM 复核里都有,这里整列去掉(用户明确要求)。
        for col in ("需求", "缺口", "在途", "该买"):
            check(f"二级表没有「{col}」列(缺料信息属于项目页)", col in cols, False)
        check("元件表格是多选的(批量挪品类靠它)",
              "extended" in str(tab.tree.cget("selectmode")), True)
        check("列表页有就地加子类的方法",
              callable(getattr(tab, "add_sub_here", None)), True)
        check("列表页有批量挪品类的方法",
              callable(getattr(tab, "move_to_category", None)), True)
        show = str(tab.tree.cget("show"))
        check("二级是平表,没有展开三角",
              "headings" in show and "tree" not in show, True)
        p(f"  点开「{cat0}」-> {n0} 个型号(该大类在库的全在这里),页面切换正常")
        for k in tab.tree.get_children():
            v = tab.tree.item(k, "values")
            p(f"      {v[0]}  现有 {v[k_qty]}  {v[cols.index('状态')]}")

        rows = tab.tree.get_children()
        tab.tree.selection_set(rows[0])
        app.update()
        check("选中元件能取到 id", isinstance(tab.selected_id(), int), True)
        p(f"  [OK ] 选中元件后详情加载,仓位分布 {len(tab.t_stock.get_children())} 行,"
          f"流水 {len(tab.t_hist.get_children())} 行")

        tab.go_home()
        app.update()
        check("返回后回到首页", tab.view, "home")
        check("返回后首页重新显示", bool(tab.page_home.winfo_ismapped()), True)
        check("返回后二级页面收起", bool(tab.page_cat.winfo_ismapped()), False)

        # ---- 搜索也是独立页面
        tab.q.set(cat0)
        tab.open_search()
        app.update()
        check("搜索切到独立页面", tab.view, "search")
        check("搜索结果非空", len(tab.tree.get_children()) > 0, True)
        tab.q.set("")
        tab.go_home()
        app.update()

        # ---------------------------------------------------------- 出入库
        p("\n【5】出入库:只查账,拆成入库流水 / 出库流水")
        app.nb.select(app.tab_stock)
        app.update()
        st = app.tab_stock
        check("默认停在入库流水", st.action, "IN")
        check("入库流水 / 出库流水两个按钮都在", sorted(st.btn), ["IN", "OUT"])
        # 职责重排:这一页现在只是账本,开单和元件列表都搬去「项目 BOM」页了
        check("这一页已经没有开单区(操作搬去项目 BOM 页了)",
              hasattr(st, "submit"), False)
        check("这一页也没有元件列表(所以不会再被导入 BOM 的料塞满)",
              hasattr(st, "tree") and "headings" in str(st.tree.cget("show")), True)
        # #30 之后这张表改成按项目折叠了:多一个 #0 组列和展开三角。
        # 「带 #0 树列」和「有展开三角」本来就是一回事,所以这里反过来验。
        check("记录表按项目折叠(#30):有 #0 组列和展开三角",
              "tree" in str(st.tree.cget("show")), True)
        # 盘点/移库既不是入库也不是出库,只该出现在「流水」页。
        # 组行的「动作」格是空的,所以在 all_rows 里也算「不属于盘点/移库」。
        kinds_shown = {str(st.tree.item(i, "values")[1]).replace("(已撤销)", "")
                       .replace("·撤销", "") for i in all_rows(st.tree)}
        check("这里不会出现盘点 / 移库",
              kinds_shown <= {"入库", "出库", ""}, True)

        base_in = len(gui.call(app.con, server.list_movements,
                               query={"kind": "IN", "limit": "5000"}, quiet=True)["items"])
        check("入库流水显示的就是全部入库流水",
              len(st.groups.row_ids()), base_in)
        check("摘要写出了条数和合计", "入库流水" in st.summary.get(), True)
        check("摘要里说明了这一页只查账", "只查账" in st.summary.get(), True)
        check("摘要给出了真正的操作在哪做",
              "项目 BOM" in st.summary.get(), True)

        # ---- 项目筛选
        check("项目筛选有「全部项目」和「不指定项目」",
              st.ALL in st.cb_proj.cget("values")
              and st.NONE_PROJ in st.cb_proj.cget("values"), True)
        st.proj.set(st.NONE_PROJ)
        st.reload()
        app.update()
        none_in = len(gui.call(app.con, server.list_movements,
                               query={"kind": "IN", "project": "none",
                                      "limit": "5000"}, quiet=True)["items"])
        check("选「不指定项目」只剩没挂项目的流水",
              len(st.groups.row_ids()), none_in)
        # 【4】里那几次入库没挂项目,所以本来就该出现在「不指定项目」下
        check("「不指定项目」里确实有前面那几笔", none_in > 0, True)
        st.proj.set(st.ALL)
        st.reload()
        app.update()
        check("切回「全部项目」又都回来了",
              len(st.groups.row_ids()), base_in)

        # ---- 出库流水是独立的一套
        st.set_action("OUT")
        app.update()
        check("出库页签记住自己是出库", st.action, "OUT")
        base_out = len(gui.call(app.con, server.list_movements,
                                query={"kind": "OUT", "limit": "5000"},
                                quiet=True)["items"])
        check("出库流水显示的是全部出库流水",
              len(st.groups.row_ids()), base_out)
        check("入库那几单不会出现在出库流水里", base_out < base_in, True)
        st.set_action("IN")
        app.update()
        p(f"  入库流水 {base_in} 条 / 出库流水 {base_out} 条")

        # ---- 真正的入库/出库操作现在在「项目 BOM」页里
        p("\n【5.1】入库 / 出库的操作搬到了项目 BOM 页")
        app.nb.select(app.tab_proj)
        app.update()
        pr = app.tab_proj
        check("项目页右半边分成了三个子页签", pr.sub.index("end"), 3)
        tabs = [pr.sub.tab(i, "text").strip() for i in range(pr.sub.index("end"))]
        check("三个子页签是 BOM 明细 / 元件入库 / 元件出库",
              tabs, ["BOM 明细", "元件入库", "元件出库"])
        check("元件入库的开单区是入库方向", pr.pane_in.action, "IN")
        check("元件出库的开单区是出库方向", pr.pane_out.action, "OUT")
        # 入库必须看得见全部,否则刚导入 BOM 的新料永远收不进来(它库存就是 0);
        # 出库反过来,没有的东西发不出去,列出来纯属干扰
        check("入库方向默认列出全部元件",
              pr.pane_in.form.only_stocked.get(), False)
        check("出库方向默认只列有库存的",
              pr.pane_out.form.only_stocked.get(), True)
        # 两个方向现在各有两种开单方式,默认是「按 BOM」。下面这一段测的是
        # 「自由」那条路(不在 BOM 上的东西),所以先切过去;按 BOM 那两块
        # 单独在【27】【28】里测
        check("两个方向默认都按 BOM 开单",
              (pr.pane_in.mode.get(), pr.pane_out.mode.get()), ("bom", "bom"))
        check("入库区默认那块是按 BOM 收料清单",
              isinstance(pr.pane_in.bom_form, gui.BomReceivePane), True)
        check("出库区默认那块是按 BOM 分配树",
              isinstance(pr.pane_out.bom_form, gui.BomPickPane), True)
        pr.pane_in.show_free()
        pr.pane_out.show_free()
        app.update()
        p(f"  [OK ] 项目页 {len(pr.t_proj.get_children())} 个项目,"
          f"BOM {len(pr.t_bom.get_children())} 行,缺料标签「{pr.shortage.get()}」")

        pid0 = int(pr.t_proj.get_children()[0])
        pr.t_proj.selection_set(str(pid0))
        app.update()
        check("选中项目后入库区记下了这个项目", pr.pane_in.project_id, pid0)
        check("选中项目后出库区也记下了", pr.pane_out.project_id, pid0)

        it = chosen[0][1]
        f_in = pr.pane_in.form
        f_in.tree.selection_set(str(it["id"]))
        app.update()
        f_in.qty.set("6")
        f_in.note.set("项目里开的入库单")
        n_rec = len(pr.pane_in.t_rec.get_children())
        f_in.submit()
        app.update()
        got = gui.call(app.con, server.list_movements,
                       query={"project_id": str(pid0), "kind": "IN"})["items"]
        # 手动开的单会在备注前面盖「手动」章(#21):流水表没有 source 列,不盖章就
        # 分不清这条是手动做的、还是别的什么没关联的项目动作
        check("在项目里开的入库单会落在这个项目名下(并盖了「手动」来源章)",
              got[0]["note"], "手动 项目里开的入库单")
        check("盖的是前缀,不是把用户写的备注吃掉",
              got[0]["note"].endswith("项目里开的入库单"), True)
        check("记录表立刻多一条", len(pr.pane_in.t_rec.get_children()), n_rec + 1)
        check("这边开的单也进了出入库页的入库流水",
              any(gui.call(app.con, server.list_movements, query={"kind": "IN"})
                  ["items"][k]["note"] == "手动 项目里开的入库单"
                  for k in range(3)), True)

        # 没选项目时不许开单 —— 记在谁名下都不清楚
        f_in.set_project(None)
        f_in.qty.set("1")
        _mb, _infos = gui.messagebox, []
        class _Box:
            answer = True
            def showinfo(self, *a, **k):
                _infos.append(a)
            def showwarning(self, *a, **k):
                _infos.append(a)
            def askyesno(self, *a, **k):
                return True
        box5a = _Box()
        gui.messagebox = box5a
        try:
            f_in.submit()
            check("没选项目时开单会被挡住并说明原因",
                  bool(_infos) and "项目" in str(_infos[0][1]), True)
        finally:
            gui.messagebox = _mb
        f_in.set_project(pid0)

        # 撤销也要能在项目页里做
        mid = pr.pane_in.t_rec.get_children()[0]
        gui.messagebox = box5a
        try:
            pr.pane_in.t_rec.selection_set(mid)
            pr.pane_in.undo()
            app.update()
            check("在项目页撤销后那一条标成了已撤销",
                  "voided" in str(pr.pane_in.t_rec.item(mid, "tags")), True)
        finally:
            gui.messagebox = _mb
        check("撤销补的反向流水出现在出入库页的出库流水里",
              any("撤销" in str(gui.call(app.con, server.list_movements,
                                        query={"kind": "OUT"})["items"][k]["note"])
                  for k in range(3)), True)

        pr.pane_in.show_bom()
        pr.pane_out.show_bom()
        app.update()

        app.nb.select(app.tab_move)
        app.update()
        p(f"  [OK ] 流水页 {len(app.tab_move.groups.row_ids())} 行 /"
          f" {app.tab_move.groups.group_count()} 个项目分组")
        app.nb.select(app.tab_loc)
        app.update()
        p(f"  [OK ] 仓位页 {len(app.tab_loc.tree.get_children())} 行")

        d = gui.ComponentDialog(app, app, None)
        d.update()
        p(f"  [OK ] 新增元件弹窗 {len(d.vars)} 个字段")
        d.destroy()
        cid0 = chosen[0][1]["id"]
        d2 = gui.ComponentDialog(app, app, cid0)
        d2.update()
        check("编辑弹窗回填名称", bool(d2.vars["name"].get()), True)
        d2.destroy()
        mv = gui.MoveDialog(app, app, cid0, "IN")
        mv.update()
        p("  [OK ] 出入库弹窗(盘点 / 移库也从这里走)")
        mv.destroy()

        # ============================================ 这次重做的部分
        app_con = app.con

        def actx(**kw):
            return gui.make_ctx(app_con, **kw)

        def API(fn, query=None, body=None, match=None):
            return fn(actx(query=query, body=body), M(*match) if match else None)[1]

        p("\n【8】总览页")
        app.nb.select(app.tab_dash)
        app.update()
        dash = app.tab_dash
        check("8 个统计数字都填上了",
              [k for k, v in dash.vals.items() if v.get() in ("", "–")], [])
        check("总览列出了项目进度", len(dash.t_proj.get_children()), 1)
        check("总览列出了最近流水", len(dash.t_recent.get_children()) > 0, True)
        pv = dash.t_proj.item(dash.t_proj.get_children()[0], "values")
        check("项目行带「能造」数量(第 3 列)", pv[2] != "", True)
        p("  " + " / ".join(f"{k}={v.get()}" for k, v in dash.vals.items()))

        p("\n【9】采购页:该买 → 下单 → 到货")
        app.nb.select(app.tab_purchase)
        app.update()
        buy = app.tab_purchase
        check("该买清单里有缺料的料号", len(buy.t_buy.get_children()) > 0, True)
        check("原因列说清了为什么该买",
              any("项目缺料" in str(buy.t_buy.item(k, "values")[12])
                  for k in buy.t_buy.get_children()), True)
        check("汇总文字非空", bool(buy.buy_sum.get()), True)

        picked = buy.t_buy.get_children()[:3]
        buy.t_buy.selection_set(picked)
        app.update()
        target = int(picked[0])
        n_before = len(buy.t_po.get_children())
        buy.add_selected()
        app.update()
        check("加入采购单后下面多了 3 条", len(buy.t_po.get_children()), n_before + 3)

        by = {i["id"]: i for i in API(server.list_components, query={"limit": "0"})["items"]}
        check("只是「想买」不算在途", by[target]["on_order"], 0)
        check("所以该买数量没被抵掉", by[target]["to_order"] > 0, True)

        # 采购单列表按状态再按 id 倒序排,所以第一行不是刚为 picked[0] 建的那单,
        # 得按元件号反查出来,不能想当然取 get_children()[0]。
        po_id = app_con.execute(
            "SELECT id FROM purchase WHERE component_id=? ORDER BY id DESC LIMIT 1",
            (target,)).fetchone()[0]
        buy.t_po.selection_set(str(po_id))
        app.update()
        buy.set_status("ordered")
        app.update()
        check("标记已下单后状态列变成「已下单」",
              buy.t_po.item(str(po_id), "values")[0], "已下单")
        by = {i["id"]: i for i in API(server.list_components, query={"limit": "0"})["items"]}
        check("已下单的数量算进在途了", by[target]["on_order"] > 0, True)

        rd = gui.ReceiveDialog(app, app, po_id)
        rd.update()
        rd.v_qty.set("1")
        rd.do(None)
        app.update()
        rec = app_con.execute("SELECT received FROM purchase WHERE id=?",
                              (po_id,)).fetchone()[0]
        check("到货入库把已收数量记成 1", rec, 1)
        check("到货写了一条带采购单号的入库流水", app_con.execute(
            "SELECT COUNT(*) FROM movement WHERE purchase_id=? AND kind='IN'",
            (po_id,)).fetchone()[0], 1)

        p("\n【10】层级仓位:柜 → 层 → 格")
        loc = app.tab_loc
        app.nb.select(loc)
        app.update()
        chain, parent = [], None
        for code, name, structural in (("A柜", "A柜", True), ("A-01", "01层", True),
                                       ("A-01-02", "02格", False)):
            b = {"code": code, "name": name, "structural": structural}
            if parent:
                b["parent_id"] = parent
            parent = API(server.create_location, body=b)["id"]
            chain.append(parent)
        loc.reload()
        app.update()
        check("02格的上级是 01层", loc.tree.parent(str(chain[2])), str(chain[1]))
        check("01层的上级是 A柜", loc.tree.parent(str(chain[1])), str(chain[0]))
        check("A柜标成「分层」", loc.tree.item(str(chain[0]), "values")[2], "分层")
        check("02格标成「存货」", loc.tree.item(str(chain[2]), "values")[2], "存货")

        try:
            API(server.stock_move, body={"kind": "IN", "component_id": cid0,
                                        "qty": 5, "location": "A柜"})
            check("往分层仓位直接放东西会被挡住", False, True)
        except server.ApiError as exc:
            check("往分层仓位直接放东西会被挡住", "分层仓位" in exc.message, True)

        API(server.stock_move, body={"kind": "IN", "component_id": cid0,
                                     "qty": 7, "location": "A-01-02"})
        loc.reload()
        loc.tree.selection_set(str(chain[0]))
        loc._on_pick()
        app.update()
        check("选中 A柜 能级联看到子仓位里的料",
              len(loc.t_contents.get_children()) > 0, True)
        check("说明里标明了是级联统计", "含子仓位" in loc.summary.get(), True)
        loc.tree.selection_set(str(chain[2]))
        loc._on_pick()
        app.update()
        check("选中 02格 能看到放进去的 7 个",
              any(float(loc.t_contents.item(k, "values")[4]) == 7
                  for k in loc.t_contents.get_children()), True)

        p("\n【11】项目页:计划数量 / 能造几块 / BOM 行编辑")
        proj = app.tab_proj
        app.nb.select(proj)
        proj.reload()
        app.update()
        kids = proj.t_proj.get_children()
        check("项目列表 1 行", len(kids), 1)
        pv = proj.t_proj.item(kids[0], "values")
        check("项目行第 2 列是「计划」数量", pv[1], "1")
        check("标题里写了计划与能造",
              "计划" in proj.title.get() and "能造" in proj.title.get(), True)
        check("BOM 表 12 列(含单块/损耗/替代/标记)", len(proj.t_bom["columns"]), 12)

        bid0 = int(proj.t_bom.get_children()[0])
        line0 = proj._lines[bid0]
        bd = gui.BomLineDialog(app, proj, line0)
        bd.update()
        check("BOM 行弹窗回填了单块用量", bd.v["per_board"].get(), str(line0["per_board"]))
        bd.v["attrition"].set("5")
        bd.save()
        proj.reload()
        app.update()
        check("损耗率改成 5% 真的存下来了", proj._lines[bid0]["attrition"], 5.0)
        check("弹窗已标记完成", bd.done, True)

        sd = gui.SubstituteDialog(app, proj, proj._lines[bid0])
        sd.update()
        n_sub = len(sd.tree.get_children())
        # 必须挑一个**真有库存**的元件当替代料,否则 sub_qty 本来就是 0,
        # 测不出「替代料库存计入可用量」这件事
        cands = [i for i in API(server.list_components, query={"limit": "0"})["items"]
                 if i["on_hand"] > 0 and i["id"] != proj._lines[bid0]["component_id"]]
        check("库里有带库存的元件可以拿来当替代料", len(cands) > 0, True)
        API(server.add_substitute, body={"component_id": cands[0]["id"]}, match=(bid0,))
        sd.reload()
        app.update()
        check("加了替代料后弹窗里多一行", len(sd.tree.get_children()), n_sub + 1)
        check("替代料汇总里会说明计入可用量", "可用量" in sd.sum.get(), True)
        sd.destroy()
        rep_after = API(server.project_bom, match=(proj._pid,))
        line_after = [l for l in rep_after["lines"] if l["bom_id"] == bid0][0]
        check("替代料的库存真的被算进这一行的可用量", line_after["sub_qty"] > 0, True)
        check("可用量 = 本件现有 + 替代料现有",
              line_after["available"], line_after["on_hand"] + line_after["sub_qty"])

        p("\n【12】元件弹窗:新字段与属性小表")
        d3 = gui.ComponentDialog(app, app, cid0)
        d3.update()
        for key in ("unit_price", "min_stock", "reorder_qty", "supplier", "default_loc_id"):
            check(f"弹窗有「{key}」字段", key in d3.vars, True)

        # 属性以前是一个 tk.Text,要用户背「一行一个,写成 耐压=50V」这个语法:
        # 键名打错一个字不报错,只是静默多出一条属性,想删一条也只能整块重打。
        # 现在是一张「名称 / 值」的小表。
        check("属性是结构化的行,不再是一个要背语法的文本框",
              hasattr(d3, "attr_rows"), True)
        check("没把 tk.Text 那个老控件留在身上", hasattr(d3, "params"), False)
        n0 = len(d3.attr_rows)
        d3.attr_add("耐压", "50V")
        d3.attr_add("温度", "-40~85C")
        d3.attr_add("", "点了加号又没填名称的一行")
        app.update()
        check("能加行", len(d3.attr_rows), n0 + 3)
        parsed = d3._parse_params()
        check("按「名称 / 值」解析", parsed.get("耐压"), "50V")
        check("值两边空格被去掉", parsed.get("温度"), "-40~85C")
        check("名称空着的那一行被丢掉(点错了不该存成一条空名字的属性)",
              "" in parsed, False)
        d3.attr_del(d3.attr_rows[-1][0])
        app.update()
        check("能删行", len(d3.attr_rows), n0 + 2)
        check("删掉之后解析里也没有它", d3._parse_params().get("耐压"), "50V")
        d3.attr_add("  耐压  ", "100V")
        check("名称两边多打了空格,归一化后算同一个名字(不该变成两条)",
              (d3._parse_params().get("耐压"), len([k for k in d3._parse_params()
                                                    if "耐压" in k])), ("100V", 1))
        check("空值也留着 ——「还没填」和「没这个属性」是两件事",
              "空值测试" not in d3._parse_params(), True)
        d3.attr_add("空值测试", "")
        check("值空着仍然出现在解析结果里",
              "空值测试" in d3._parse_params(), True)
        check("属性名有候选", len(d3.attr_options()) > 0, True)
        d3.vars["category"].set("电感")
        opts = d3.attr_options()
        check("换了品类,候选跟着换(电感能看到饱和电流)", "饱和电流" in opts, True)
        check("只是个建议,不是白名单 —— 输入框仍然能自己打字",
              (isinstance(d3.attr_rows[0][1], gui.ttk.Combobox)
               and "readonly" not in d3.attr_rows[0][1].state()), True)
        d3.destroy()

        p("\n【13】元件选择器")
        pk = gui.ComponentPicker(app, app, "测试")
        pk.update()
        total_c = len(pk.tree.get_children())
        check("选择器列出了元件", total_c > 0, True)
        pk.q.set("10k")
        pk.reload()
        app.update()
        check("搜索能过滤掉不相关的", 0 < len(pk.tree.get_children()) < total_c, True)
        pk.tree.selection_set(pk.tree.get_children()[0])
        pk.pick()
        check("选中后返回元件 id", isinstance(pk.result, int), True)

        p("\n【14】丝印 / 参数都要能搜到 —— 拆机料靠这个认回来")
        mk = API(server.create_component, body={
            "name": "拆机 SOT-23-5 未知芯片", "category": "芯片 IC",
            "marking": "CX4R", "package": "SOT-23-5",
            "params": {"来源": "salvage-bin-7"}})
        mk_id = mk["id"]
        got = API(server.list_components, query={"q": "CX4R"})
        check("按丝印搜得到", [i["id"] for i in got["items"]], [mk_id])
        check("丝印真的存下来了", got["items"][0]["marking"], "CX4R")
        API(server.update_component, body={"marking": "KGMK"}, match=(mk_id,))
        check("改了丝印后按新丝印搜得到",
              [i["id"] for i in API(server.list_components, query={"q": "KGMK"})["items"]],
              [mk_id])
        check("旧丝印搜不到了",
              len(API(server.list_components, query={"q": "CX4R"})["items"]), 0)
        check("参数(JSON)里的值也能搜到(只可能由 params 命中)",
              [i["id"] for i in
               API(server.list_components, query={"q": "salvage-bin-7"})["items"]],
              [mk_id])

        p("\n【15】快速入库 —— 收货那一刻的录入")
        app.nb.select(app.tab_comp)
        app.update()
        qi = gui.QuickInDialog(app, app)
        qi.update()
        stock_of = lambda cid: app_con.execute(  # noqa: E731
            "SELECT COALESCE(SUM(qty),0) FROM stock WHERE component_id=?", (cid,)
        ).fetchone()[0]

        qi.q.set("10k")
        qi.reload()
        app.update()
        check("输入关键字后列出了候选", len(qi.tree.get_children()) > 0, True)
        pick = int(qi.tree.get_children()[0])
        qi.tree.selection_set(str(pick))
        qi.v_qty.set("3")
        before = stock_of(pick)
        qi.commit()
        app.update()
        check("搜到已有的,回车直接入库 3 个", stock_of(pick), before + 3)
        check("入库后窗口不关(可以接着录下一袋)", bool(qi.winfo_exists()), True)
        check("入库后搜索框自动清空", qi.q.get(), "")
        check("入库后数量回到 1", qi.v_qty.get(), "1")

        n_before = app_con.execute("SELECT COUNT(*) FROM component").fetchone()[0]
        qi.q.set("ZZTOP-999-瞎写的")
        qi.reload()
        app.update()
        check("库里没有时提示「会新建」", "新建" in qi.hint.get(), True)
        qi.v_qty.set("2")
        qi.commit()
        app.update()
        check("搜不到时当场新建了一条元件",
              app_con.execute("SELECT COUNT(*) FROM component").fetchone()[0], n_before + 1)
        newrow = app_con.execute(
            "SELECT id, category FROM component WHERE name=?",
            ("ZZTOP-999-瞎写的",)).fetchone()
        check("新建的先归到「未分类」,不拦着你先记下来", newrow["category"], "未分类")
        check("新建的同时就入库了 2 个", stock_of(newrow["id"]), 2)
        qi.destroy()

        p("\n【16】快捷键")
        check("Ctrl+I 绑上了快速入库", bool(app.bind_all("<Control-i>")), True)
        check("F5 绑上了刷新全部", bool(app.bind_all("<F5>")), True)

        p("\n【17】二级页的值区间 / 封装筛选")
        app.nb.select(app.tab_comp)
        app.update()
        tab.open_category(cat0)
        app.update()
        cols = [tab.tree.heading(c)["text"] for c in tab.tree["columns"]]
        k_val = cols.index("值")
        # 用一组跨数量级的阻值:按字符串比的话 "100k" < "10k",区间就全错了
        made = []
        for v, pkg in (("100", "0603"), ("1k", "0603"), ("10k", "0603"),
                       ("100k", "0805"), ("1M", "0805")):
            c = API(server.create_component, body={
                "name": f"筛选用 {v}Ω", "category": "筛选测试类",
                "value": f"{v}Ω", "package": pkg})
            API(server.stock_move, body={"kind": "IN", "component_id": c["id"],
                                         "qty": 10})
            made.append(c["id"])
        app.refresh_all()
        tab.open_category("筛选测试类")
        app.update()
        check("5 个筛选样例都在", len(tab.tree.get_children()), 5)

        tab.f_min.set("500")
        tab.f_max.set("2000")
        tab.load_category()
        app.update()
        check("值 500~2000 只剩 1k 那一个", len(tab.tree.get_children()), 1)
        check("剩下的确实是 1kΩ",
              tab.tree.item(tab.tree.get_children()[0], "values")[k_val], "1kΩ")
        p("  ↑ 按字符串比的话 100k 会落进这个区间")

        tab.f_min.set("1k")
        tab.f_max.set("100k")
        tab.load_category()
        app.update()
        check("也认工程记号:1k~100k 命中 3 个",
              len(tab.tree.get_children()), 3)
        check("筛选状态写在了计数旁边", "值 ≥ 1k" in tab.cat_count.get(), True)

        tab.clear_filters()
        app.update()
        check("清除筛选后恢复 5 个", len(tab.tree.get_children()), 5)
        check("单位下拉里只有 Ω(范围里真有的)",
              list(tab.cmb_unit.cget("values")), [tab.ALL, "Ω"])
        check("封装下拉里是 0603 / 0805",
              sorted(tab.cmb_pkg.cget("values")[1:]), ["0603", "0805"])

        tab.f_pkg.set("0805")
        tab.load_category()
        app.update()
        check("按封装筛出 2 个 0805", len(tab.tree.get_children()), 2)
        check("筛选时封装下拉仍然列着 0603(不会被自己筛掉)",
              "0603" in list(tab.cmb_pkg.cget("values")), True)
        p("  ↑ 分面按「范围」算而不是按「结果」算,否则选定一个封装后别的就消失了")

        tab.f_min.set("瞎写")
        tab.load_category()
        app.update()
        check("值写错了给提示,而不是把表清空",
              "看不懂" in tab.f_hint.get(), True)
        check("值写错时表不动(还是那 2 行)",
              len(tab.tree.get_children()), 2)

        tab.clear_filters()
        app.update()
        tab.go_home()
        app.update()
        check("返回首页后筛选被清掉", tab.f_min.get(), "")

        p("\n【18】按仓位盘点(实物清点,键盘走一遍)")

        class FakeBox:
            """把弹窗换成记账本 —— 顺便保证自检永远不会被一个模态框卡死。"""

            def __init__(self):
                self.infos, self.warns, self.asks, self.answer = [], [], [], True

            def _fire(self, box, title, msg, **_kw):
                box.append((title, msg))
                return True

            def showinfo(self, title, msg, **kw):
                return self._fire(self.infos, title, msg, **kw)

            def showwarning(self, title, msg, **kw):
                return self._fire(self.warns, title, msg, **kw)

            def showerror(self, title, msg, **kw):
                return self._fire(self.warns, title, msg, **kw)

            def askyesno(self, title, msg, **kw):
                self.asks.append((title, msg))
                return self.answer

        real_box = gui.messagebox
        box = FakeBox()
        gui.messagebox = box
        try:
            dloc = API(server.create_location,
                       body={"code": "盘点抽屉", "name": "盘点抽屉"})
            API(server.stock_move, body={"kind": "IN", "component_id": made[0],
                                         "qty": 9, "location": "盘点抽屉"})
            API(server.stock_move, body={"kind": "IN", "component_id": made[1],
                                         "qty": 4, "location": "盘点抽屉"})
            app.refresh_all()
            app.nb.select(app.tab_loc)
            app.update()
            loc_tab = app.tab_loc
            loc_tab.reload()
            app.update()

            def find_iid(tree, node=""):
                for i in tree.get_children(node):
                    if str(tree.item(i, "text")) == "盘点抽屉":
                        return i
                    hit = find_iid(tree, i)
                    if hit:
                        return hit
                return None

            node = find_iid(loc_tab.tree)
            check("新仓位出现在仓位树上", node is not None, True)
            loc_tab.tree.selection_set(node)
            app.update()
            check("选中仓位后记住了它", loc_tab._sid, dloc["id"])
            check("右边的内容表列出了这个抽屉里的 2 种",
                  len(loc_tab.t_contents.get_children()), 2)

            sk = gui.StocktakeDialog(loc_tab, app, dloc["id"])
            sk.update()
            check("盘点窗列出了账面有的 2 种", len(sk.tree.get_children()), 2)
            check("还没数时计数是 0 / 2", "已数 0 / 2 种" in sk.summary.get(), True)

            first, second = sk.tree.get_children()
            sk.tree.selection_set(first)
            sk.v_qty.set("7")
            sk.apply()
            sk.update()
            check("填完就记下", sk.counts.get(int(first)), 7)
            check("差异行打上红标", "diff" in sk.tree.item(first, "tags"), True)
            check("焦点自动跳到下一个还没数的", sk.tree.selection()[0], second)
            check("输入框自动清空,接着数下一个", sk.v_qty.get(), "")
            check("计数变成 1 / 2", "已数 1 / 2 种" in sk.summary.get(), True)

            sk.tree.selection_set(second)
            sk.v_qty.set("0")
            sk.apply()
            sk.update()
            check("数不到就填 0(账面有、实物没有也是差异)", sk.counts.get(int(second)), 0)
            check("全数完后给出提示", "全数完" in sk.hint.get(), True)
            check("计数变成 2 / 2", "已数 2 / 2 种" in sk.summary.get(), True)

            sk.tree.selection_set(second)
            sk.v_qty.set("x")
            sk.apply()
            check("非整数被挡下并提示", "整数" in sk.hint.get(), True)
            check("挡下时不会写脏数据", sk.counts.get(int(second)), 0)

            sk.save()
            check("保存后窗口自动关闭", bool(sk.winfo_exists()), False)
            check("给出了盘点结果", len(box.infos) > 0, True)
            check("结果里报出了 2 处差异", "2 处" in box.infos[-1][1], True)

            got = {r["component_id"]: r["qty"] for r in app_con.execute(
                "SELECT component_id, qty FROM stock WHERE location_id=?",
                (dloc["id"],)).fetchall()}
            check("第一个被盘成 7", got.get(made[0]), 7)
            check("第二个被盘成 0(行保留,只是数量归零)", got.get(made[1]), 0)
            check("正好写了 2 条盘点流水",
                  app_con.execute("SELECT COUNT(*) FROM movement "
                                  "WHERE kind='ADJUST' AND location_id=?",
                                  (dloc["id"],)).fetchone()[0], 2)
            check("没数到的种类不会被动到(IN 流水还在)",
                  app_con.execute("SELECT COUNT(*) FROM movement "
                                  "WHERE kind='IN' AND location_id=?",
                                  (dloc["id"],)).fetchone()[0], 2)

            # 分层仓位本身没有实物可数
            scab = API(server.create_location,
                       body={"code": "盘点柜", "structural": 1})
            sk2 = gui.StocktakeDialog(loc_tab, app, scab["id"])
            sk2.update()
            check("分层仓位直接拒绝,并说清原因", "分层" in box.infos[-1][1], True)
            sk2.destroy()
        finally:
            gui.messagebox = real_box

        p("\n【19】批量入库:一行怎么拆")
        check("x50 是数量", gui.parse_batch_line("10k 0603 x50"), ("10k 0603", 50))
        check("×20 / *20 也认", gui.parse_batch_line("100nF ×20"), ("100nF", 20))
        check("没写数量就是 1", gui.parse_batch_line("STM32F103C8T6"), ("STM32F103C8T6", 1))
        check("行尾裸数字不当数量 —— 否则「100nF 0805」会被读成 805",
              gui.parse_batch_line("100nF 0805"), ("100nF 0805", 1))
        check("表格里复制粘贴(制表符)时最后一列数字当数量",
              gui.parse_batch_line("10k\t0603\t50"), ("10k 0603", 50))
        check("# 开头的行当注释忽略", gui.parse_batch_line("# 下面开始"), None)
        check("空行忽略", gui.parse_batch_line("   "), None)

        p("\n【20】批量入库:先解析预览,确认后才落库")
        box2 = FakeBox()
        gui.messagebox = box2
        try:
            for v in ("2k2", "2k4"):
                API(server.create_component, body={
                    "name": f"批量测试 {v}", "category": "批量测试类",
                    "value": f"{v}Ω", "package": "0805"})
            app.refresh_all()

            def on_hand_of(name):
                row = app_con.execute("SELECT id FROM component WHERE name=?",
                                      (name,)).fetchone()
                return app_con.execute(
                    "SELECT COALESCE(SUM(qty),0) FROM stock WHERE component_id=?",
                    (row["id"],)).fetchone()[0]

            bd = gui.BatchInDialog(app, app)
            bd.update()
            bd.txt.delete("1.0", "end")
            bd.txt.insert("1.0",
                          "筛选用 1kΩ x5\n"
                          "没有的料 ZZZ-404 x2\n"
                          "0805 x1\n"
                          "批量测试 2k2 x4\n")
            bd.parse()
            bd.update()
            check("解析出 4 行", len(bd.rows), 4)
            check("x5 被读成数量 5", bd.rows[0]["qty"], 5)
            check("整名精确命中已有的元件",
                  str(bd.rows[0]["how"]).startswith("exact"), True)
            check("库里没有的那行标成「将新建」", bd.rows[1]["how"], "none")
            check("一行对上多项时不给答案", bd.rows[2]["how"], "ambiguous")
            check("多匹配的行在预览里标黄提醒", "many" in bd.tree.item("2", "tags"), True)
            check("新建的行标绿", "new" in bd.tree.item("1", "tags"), True)
            check("4 行都列在预览表里", len(bd.tree.get_children()), 4)

            bd.tree.selection_set("2")
            bd.v_qty.set("9")
            bd.set_qty()
            check("能在预览里直接改数量", bd.rows[2]["qty"], 9)
            check("改完输入框清空", bd.v_qty.get(), "")

            before = on_hand_of("筛选用 1kΩ")
            box2.answer = True
            bd.commit()
            bd.update()
            check("命中已有元件的入库了 5 个", on_hand_of("筛选用 1kΩ"), before + 5)
            check("库里没有的当场新建",
                  app_con.execute("SELECT COUNT(*) FROM component WHERE name=?",
                                  ("没有的料 ZZZ-404",)).fetchone()[0], 1)
            check("新建的也入库了 2 个", on_hand_of("没有的料 ZZZ-404"), 2)
            check("精确命中的第二条也入库了 4 个", on_hand_of("批量测试 2k2"), 4)
            check("多匹配那行被跳过,没有瞎猜着入库",
                  app_con.execute("SELECT COUNT(*) FROM component WHERE name='0805'"
                                  ).fetchone()[0], 0)
            check("结果里说了跳过了几行", "跳过" in box2.infos[-1][1], True)
            check("结果里说了新建了几个", "新建" in box2.infos[-1][1], True)
            check("批量入库标记了 done,主界面会刷新", bd.done, True)
            check("全部成功时窗口自动关闭", bool(bd.winfo_exists()), False)

            check("Ctrl+B 绑上了批量入库", bool(app.bind_all("<Control-b>")), True)
        finally:
            gui.messagebox = real_box

        p("\n【21】撤销上一次出入库(录错了当场退回去)")
        box3 = FakeBox()
        gui.messagebox = box3
        try:
            c = API(server.create_component, body={"name": "撤销界面测试料",
                                                   "category": "其他"})
            cid = c["id"]

            def stock_of():
                return app_con.execute(
                    "SELECT COALESCE(SUM(qty),0) FROM stock WHERE component_id=?",
                    (cid,)).fetchone()[0]

            API(server.stock_move, body={"kind": "IN", "component_id": cid, "qty": 12})
            app.refresh_all()
            app.update()
            tab_mv = app.tab_move
            app.nb.select(tab_mv)
            tab_mv.reload()
            app.update()
            first = tab_mv.groups.row_ids()[0]
            check("流水页列出了刚入库的那条",
                  tab_mv.tree.item(first, "values")[1], "入库")

            app.focus_set()
            app.update()
            box3.asks.clear()
            app.undo_last()
            app.update()
            check("撤销前先问一遍 —— 不问就动手太危险", len(box3.asks), 1)
            check("问的时候把这一笔原样摆出来了",
                  "撤销界面测试料" in box3.asks[0][1], True)
            check("并说清是补反向流水、不是删记录",
                  "反向流水" in box3.asks[0][1], True)
            check("撤销后库存归零", stock_of(), 0)
            check("原记录还在,只是标成已撤销",
                  app_con.execute("SELECT voided FROM movement WHERE component_id=? "
                                  "ORDER BY id LIMIT 1", (cid,)).fetchone()[0], 1)

            tab_mv.reload()
            app.update()
            shown = {tab_mv.tree.item(i, "values")[1]: tab_mv.tree.item(i, "tags")
                     for i in tab_mv.groups.row_ids()}
            check("流水页把已撤销的那笔标出来",
                  any("已撤销" in k for k in shown), True)
            check("已撤销的行淡显而不是隐藏(历史要看得见)",
                  any("voided" in t for t in shown.values()), True)
            check("反向流水本身也在流水页里",
                  any("·撤销" in k for k in shown), True)

            box3.answer = False
            API(server.stock_move, body={"kind": "IN", "component_id": cid, "qty": 3})
            app.focus_set()
            app.update()
            app.undo_last()
            check("在确认框里点「否」就真的什么都不做", stock_of(), 3)

            box3.answer = True
            API(server.stock_move, body={"kind": "OUT", "component_id": cid, "qty": 3})
            app.focus_set()
            app.update()
            app.undo_last()
            app.update()
            check("撤销出库后数量加回来", stock_of(), 3)
            check("撤销完给了状态栏提示", "已撤销" in app.status.get(), True)
            check("Ctrl+Z 绑上了撤销", bool(app.bind_all("<Control-z>")), True)

            box3.answer = False
            app.focus_set()
            app.update()
            app.undo_last()
            check("再点否还是不动", stock_of(), 3)
        finally:
            gui.messagebox = real_box

        p("\n【22】查重与合并")
        box4 = FakeBox()
        gui.messagebox = box4
        try:
            e1 = API(server.create_component, body={
                "name": "界面查重甲", "category": "其他", "mpn": "GUI-DUP-1",
                "value": "3k3Ω", "package": "0603"})["id"]
            e2 = API(server.create_component, body={
                "name": "界面查重乙", "category": "其他", "mpn": "GUI-DUP-1",
                "value": "3k3Ω", "package": "0603"})["id"]
            API(server.stock_move, body={"kind": "IN", "component_id": e1, "qty": 5})
            API(server.stock_move, body={"kind": "IN", "component_id": e2, "qty": 8})
            app.refresh_all()

            dd = gui.DedupeDialog(app, app)
            dd.update()
            row = [i for i in dd.t_groups.get_children()
                   if dd.t_groups.item(i, "values")[1] == "GUI-DUP-1"]
            check("扫出了这一组重复", len(row), 1)
            check("依据显示成中文而不是 mpn 这种内部码",
                  dd.t_groups.item(row[0], "values")[0], "料号相同")
            dd.t_groups.selection_set(row[0])
            dd.show_group()
            dd.update()
            check("右边列出了这一组的成员", len(dd.t_items.get_children()), 2)
            check("成员表带上了各自的现有库存,好判断哪条是正主",
                  sorted(int(dd.t_items.item(i, "values")[5])
                         for i in dd.t_items.get_children()), [5, 8])

            keep_iid = [i for i in dd.t_items.get_children()
                        if int(dd.t_items.item(i, "values")[5]) == 8][0]
            drop_iid = [i for i in dd.t_items.get_children() if i != keep_iid][0]
            dd.t_items.selection_set(keep_iid)
            box4.asks.clear()
            dd.merge()
            dd.update()
            check("合并前把要保留和要并掉的名字都摆出来问一遍", len(box4.asks), 1)
            check("确认框里明说了流水一条不动",
                  "流水一条不动" in box4.asks[0][1], True)
            check("也说了被并的那条不删",
                  "不删" in box4.asks[0][1], True)
            check("库存相加到保留的那条(5 + 8)",
                  app_con.execute("SELECT COALESCE(SUM(qty),0) FROM stock "
                                  "WHERE component_id=?", (int(keep_iid),)).fetchone()[0], 13)
            check("被并的那条标上 merged_into",
                  app_con.execute("SELECT merged_into FROM component WHERE id=?",
                                  (int(drop_iid),)).fetchone()[0], int(keep_iid))
            check("合并后自动重扫,那一组不见了",
                  any(dd.t_groups.item(i, "values")[1] == "GUI-DUP-1"
                      for i in dd.t_groups.get_children()), False)
            check("合并标记了 done,主界面会刷新", dd.done, True)
            check("合并完给了状态栏提示", "已把" in app.status.get(), True)

            box4.infos.clear()
            dd.show_merged()
            check("「已合并的元件…」能查回并到哪儿去了",
                  "界面查重甲" in box4.infos[-1][1], True)
            dd.destroy()
        finally:
            gui.messagebox = real_box

        p("\n【23】出入库页的项目下拉只列真的有记录的项目")
        box5 = FakeBox()
        gui.messagebox = box5
        try:
            pid = API(server.create_project, body={"name": "界面收发货项目", "qty": 1})["id"]
            cid = API(server.create_component, body={
                "name": "界面收发货料", "category": "其他",
                "value": "2k2Ω", "package": "0603"})["id"]
            app.refresh_all()
            app.nb.select(app.tab_stock)
            st = app.tab_stock
            st.set_action("IN")
            app.update()

            nm = "界面收发货项目"
            # 导入 BOM 会建出一堆一次库都没出入过的项目。全列进下拉的话,
            # 每挑一个都是一张空表 —— 那才是这一页真正的噪音来源。
            check("刚建好、一次库都没动过的项目不进下拉",
                  nm in st.cb_proj.cget("values"), False)
            check("摘要里说明了有几个被藏起来",
                  "没列进下拉" in st.summary.get(), True)
            check("「全部项目」永远在(否则看不到全部流水)",
                  st.ALL in st.cb_proj.cget("values"), True)
            check("「不指定项目」也永远在",
                  st.NONE_PROJ in st.cb_proj.cget("values"), True)

            # 在「项目 BOM」页里真的收一次货。这一页默认是「按 BOM 收料」清单,
            # 按 BOM 那两块在【27】【28】里单独测,这里测的是「自由入库」那条路
            pr = app.tab_proj
            pr.t_proj.selection_set(str(pid))
            app.update()
            pr.pane_in.show_free()
            app.update()
            check("选中项目后开单区左边有元件可选(不能是空列表)",
                  len(pr.pane_in.form.tree.get_children()) > 0, True)
            pr.pane_in.form.tree.selection_set(str(cid))
            app.update()
            pr.pane_in.form.qty.set("3")
            pr.pane_in.form.submit()
            app.update()

            st.reload()
            app.update()
            check("真的入过库之后,项目就进下拉了",
                  nm in st.cb_proj.cget("values"), True)
            st.proj.set(nm)
            st.reload()
            app.update()
            check("选中它看得到这个项目的入库流水,不是空表",
                  len(st.groups.row_ids()), 1)

            st.set_action("OUT")
            app.update()
            check("出库流水里,还没出过库的项目依然不列",
                  nm in st.cb_proj.cget("values"), False)

            # 撤销掉唯一那笔 —— 等于没动过,又该从下拉里退出
            mid = API(server.list_movements,
                      query={"project_id": str(pid), "kind": "IN"})["items"][0]["id"]
            API(server.void_movement, match=(str(mid),))
            st.set_action("IN")
            app.update()
            check("撤销掉唯一那笔之后,项目又退出入库下拉",
                  nm in st.cb_proj.cget("values"), False)
            # 下拉藏起来不等于删除 —— 项目还在「项目 BOM」页的列表里,
            # 所以第一单永远开得出来,不会变成死路
            check("项目本身还在「项目 BOM」页的列表里(藏起来不是删掉)",
                  any(p["id"] == pid for p in st._projects), True)
            check("在项目 BOM 页里仍然选得到它",
                  str(pid) in pr.t_proj.get_children(), True)
        finally:
            gui.messagebox = real_box

        # ---------------------------------------------------------- 第二步
        p("\n【24】BOM 导入前核对品类:只列要确认的,能逐行改也能批量改")
        cats = gui.category_options(app.con)
        prev = {
            "filename": "自检板.xlsx", "total_qty": 12, "need_review": 2,
            "categories": cats,
            "warnings": ["第 5 行:位号数(1)与数量(2)不一致,按数量为准"],
            "lines": [
                {"source_row": 2, "name": "10kΩ 0603", "lcsc_pn": "C1", "value": "10kΩ",
                 "package": "0603", "designators": "R1", "qty": 1,
                 "category": "电阻", "confidence": "high",
                 "confidence_label": "明确", "reason": "位号 R 开头,按惯例是电阻"},
                {"source_row": 3, "name": "10k 0603", "lcsc_pn": None, "value": "10k",
                 "package": "0603", "designators": "", "qty": 1,
                 "category": "其他", "confidence": "none",
                 "confidence_label": "认不出",
                 "reason": "位号、值、封装里都没有能认出品类的线索"},
                {"source_row": 4, "name": "10kΩ 0805", "lcsc_pn": None, "value": "10kΩ",
                 "package": "0805", "designators": "C9", "qty": 10,
                 "category": "电容", "confidence": "low",
                 "confidence_label": "要确认",
                 "reason": "位号 C 开头,按惯例是电容；值 10kΩ 的单位是 Ω,Ω 只能是电阻。"
                           "这几条线索互相矛盾,请人工确认"},
            ],
        }
        got = {}
        dlg = gui.BomReviewDialog(app, app, prev, default_name="自检板",
                                  on_confirm=lambda n, c: got.update(name=n, cats=c))
        dlg.update()
        # 让人从头看一遍是不现实的,他只会直接点确定 —— 所以默认只摆要确认的
        check("默认只列要确认的行", len(dlg.tree.get_children()), 2)
        check("行号就是回传用的键(解析会跳行,按位置对会错位)",
              sorted(int(i) for i in dlg.tree.get_children()), [3, 4])
        check("要确认的行有底色提醒",
              "review" in str(dlg.tree.item("4", "tags")), True)
        check("表格里写出了推断依据,人才能判断该不该改",
              "矛盾" in dlg.tree.item("4", "values")[9], True)
        check("把握用的是后端给的中文说法(界面不再抄一份)",
              dlg.tree.item("4", "values")[8], "要确认")
        check("汇总写清了显示几行、要过几行",
              "显示 2 / 3 行" in dlg.sum.get() and "要过一眼的 2 行" in dlg.sum.get(), True)

        dlg.only_review.set(False)
        dlg._render()
        dlg.update()
        check("关掉筛选能看到全部", len(dlg.tree.get_children()), 3)
        check("认得出明确的那行也在", "2" in dlg.tree.get_children(), True)
        dlg.only_review.set(True)
        dlg._render()
        dlg.update()

        # 批量改:选中的多行一次改掉,不用一行一行点
        dlg.tree.selection_set(["3", "4"])
        app.update()
        dlg.pick.set("电阻")
        dlg._apply_sel()
        app.update()
        check("批量改完两行都是电阻",
              [dlg.tree.item(i, "values")[7] for i in dlg.tree.get_children()],
              ["电阻", "电阻"])
        check("改过的行标成人工指定",
              [dlg.tree.item(i, "values")[8] for i in dlg.tree.get_children()],
              ["人工指定", "人工指定"])
        check("改过的行换成另一种底色",
              "fixed" in str(dlg.tree.item("3", "tags")), True)
        check("改过的行依据也改成「由人指定」",
              dlg.tree.item("3", "values")[9], "由人指定")

        # 逐行改。ask_category 会 wait_window 把测试卡死,所以换掉它
        real_ask = gui.ask_category
        gui.ask_category = lambda *a, **k: "钽电容"
        try:
            dlg.tree.selection_set(["4"])
            app.update()
            dlg._edit_one()
            app.update()
            check("双击一行能单独改品类", dlg.tree.item("4", "values")[7], "钽电容")
        finally:
            gui.ask_category = real_ask

        # 项目名空着不让过
        box24 = FakeBox()
        gui.messagebox = box24
        try:
            dlg.name.set("")
            dlg.ok()
            app.update()
            check("项目名空着会被挡住", bool(box24.infos), True)
            check("并说明原因", "名称" in str(box24.infos[0][1]), True)
            check("挡住了就不会关掉对话框", bool(dlg.winfo_exists()), True)
        finally:
            gui.messagebox = real_box

        # 下拉必须能直接打字:推断不出来的品类(比如光耦)不能只让人凑合选一个
        def _combos(w, out):
            for c in w.winfo_children():
                if isinstance(c, gui.ttk.Combobox):
                    out.append(c)
                _combos(c, out)
            return out

        _all = _combos(dlg, [])
        check("复核窗口的下拉不是只读的(能自己打字填新品类)",
              all("readonly" not in str(c.cget("state")) for c in _all), True)
        dlg.tree.selection_set(["3"])
        app.update()
        dlg.pick.set("光耦")
        dlg._apply_sel()
        app.update()
        check("自己打进去的品类能被采用",
              dlg.tree.item("3", "values")[7], "光耦")

        dlg.name.set("自检复核板")
        dlg.ok()
        app.update()
        check("确认后把项目名交回去", got.get("name"), "自检复核板")
        check("只把人工改过的行交回去,没改的不动",
              sorted(got.get("cats") or {}), ["3", "4"])
        _dlg3 = gui.CategoryDialog(app, app, "电容", ["电阻", "电容"])
        _dlg3.update()
        check("单独改品类的小窗口也能打字",
              "readonly" not in str(_combos(_dlg3, [])[0].cget("state")), True)
        _dlg3.var.set("钽电容")
        _dlg3.ok()
        check("打进去的新品类能带回来", _dlg3.value, "钽电容")
        check("交回去的是「行号 -> 品类」",
              (got.get("cats") or {}).get("4"), "钽电容")
        check("自己打进去的新品类也一起交回去",
              (got.get("cats") or {}).get("3"), "光耦")
        check("确认后对话框自己关掉", bool(dlg.winfo_exists()), False)

        p("\n【25】出库找相似:候选排好序,挑中就能接着开单")
        sdlg = gui.SimilarDialog(app, app, value="10kΩ", package="0603",
                                 on_pick=lambda cid: got.update(picked=cid),
                                 in_stock_only=True)
        sdlg.update()
        check("找到了候选", len(sdlg.tree.get_children()) > 0, True)
        check("输入框用传进来的值预填好", sdlg.v_value.get(), "10kΩ")
        check("封装也预填好", sdlg.v_pkg.get(), "0603")
        check("默认勾着「只看有库存的」", sdlg.stocked.get(), True)
        check("汇总里点出了最像的是哪个", "最像" in sdlg.sum.get(), True)
        check("有回调时摆出「选中它去开单」",
              any("去开单" in t for t in buttons_of(sdlg)), True)
        check("表格里写着「像在哪里」",
              any("相同" in dlg2 for dlg2 in
                  [sdlg.tree.item(i, "values")[7] for i in sdlg.tree.get_children()]), True)

        first = sdlg.tree.get_children()[0]
        sdlg.tree.selection_set(first)
        app.update()
        sdlg.pick()
        app.update()
        check("挑中之后把元件号交回去", got.get("picked"), int(first))
        check("挑完对话框自己关掉", bool(sdlg.winfo_exists()), False)

        # 被「只看有库存的」滤空时,必须说清「有,只是没库存」——
        # 说成「没有这颗料」会把用户引去做完全不同的下一步
        _nostock = app.con.execute(
            "SELECT c.value, c.package FROM component c WHERE c.merged_into IS NULL"
            " AND c.value <> '' AND NOT EXISTS (SELECT 1 FROM stock s"
            " WHERE s.component_id=c.id AND s.qty > 0) LIMIT 1").fetchone()
        if _nostock:
            sdlg3 = gui.SimilarDialog(app, app, value=_nostock["value"],
                                      package=_nostock["package"], on_pick=None,
                                      in_stock_only=True)
            sdlg3.update()
            check("没库存的料不会出现在「只看有库存的」列表里",
                  len(sdlg3.tree.get_children()), 0)
            check("但要说明「有,只是都被滤掉了」,不能让人以为没有这颗料",
                  "没库存" in sdlg3.sum.get(), True)
            check("并指出下一步怎么办(取消勾选 / 先入库)",
                  "取消勾选" in sdlg3.sum.get(), True)
            sdlg3.destroy()

        sdlg2 = gui.SimilarDialog(app, app, value="", package="", on_pick=None)
        sdlg2.update()
        check("没给值也没给封装时没有候选", len(sdlg2.tree.get_children()), 0)
        check("并说明缺的是比对依据", "依据" in sdlg2.sum.get(), True)
        check("没有回调时不摆「选中它去开单」",
              any("去开单" in t for t in buttons_of(sdlg2)), False)
        sdlg2.destroy()

        p("\n【26】找相似的两个入口接在了该在的地方")
        app.nb.select(app.tab_proj)
        app.update()
        pr = app.tab_proj
        check("BOM 明具有「找相似库存…」按钮",
              any("找相似库存" in t for t in buttons_of(pr)), True)
        check("开单区有「找相似…」按钮",
              any("找相似" in t for t in buttons_of(pr.pane_in.form)), True)

        # 拿 BOM 明细里选中那一行的值+封装去库存里找
        line = None
        for iid in pr.t_bom.get_children():
            v = pr.t_bom.item(iid, "values")
            if v[2] or v[3]:
                line = iid
                break
        seen = {}
        real_sim = gui.SimilarDialog
        gui.SimilarDialog = lambda parent, a, **kw: seen.update(kw)
        try:
            if line:
                pr.t_bom.selection_set(line)
                app.update()
                pr.similar_for_line()
                check("拿的是 BOM 那一行的值/封装",
                      bool(seen.get("value") or seen.get("package")), True)
                check("出库方向默认只看有库存的", seen.get("in_stock_only"), True)
                check("接了回调,挑中之后能接着开单", bool(seen.get("on_pick")), True)

            # 开单区那个按钮:入库方向刻意**不**勾只看有库存的。
            # 「找相似…」在「自由入库」那块上,所以先切过去
            pr.pane_in.show_free()
            app.update()
            f_in = pr.pane_in.form
            if f_in._items:
                # 挑一颗有值的料,不然这条断言等于没测到「预填」
                cid = next((k for k, v in f_in._items.items() if v.get("value")),
                           next(iter(f_in._items)))
                f_in.tree.selection_set(str(cid))
                app.update()
                seen.clear()
                f_in.similar()
                check("用选中那颗料的值去预填",
                      (seen.get("value") or ""),
                      (f_in._items[cid].get("value") or ""))
                check("封装也一起带过去(值+封装才是判据)",
                      (seen.get("package") or ""),
                      (f_in._items[cid].get("package") or ""))
                check("入库方向不勾「只看有库存的」(要收的料现在库存就是 0)",
                      seen.get("in_stock_only"), False)
        finally:
            gui.SimilarDialog = real_sim

        # 挑中之后切到出库子页签。库里这颗料要是那条 BOM 需求的候选,就挂在
        # 分配树上勾好;不是候选(比如根本没库存)就退回「自由出库」选中它 ——
        # 这两条路都不能「点了没反应」
        pr.pane_out.show_free()
        app.update()
        out_form = pr.pane_out.form
        out_ids = list(out_form._items)
        if out_ids:
            cid = out_ids[0]
            _bom0 = pr.t_bom.get_children()
            bid_for = int(_bom0[0]) if _bom0 else 1
            pr._pick_similar_from_line(bid_for, cid)
            app.update()
            check("挑中之后自动切到「元件出库」子页签",
                  pr.sub.index(pr.sub.select()), 2)
            check("并且在那个列表里选中了它", out_form._current_id(), cid)
            # 选中的料可能正被搜索词挡在外面 —— 不放开筛选就点不上,
            # 用户会以为「点了没反应」
            out_form.q.set("绝不可能匹配的词 zzz")
            out_form.reload()
            app.update()
            check("搜索词把它挡住了", str(cid) in out_form.tree.get_children(), False)
            check("select_by_id 会放开筛选把它选出来",
                  out_form.select_by_id(cid), True)
            app.update()
            check("而且真的成了当前选中", out_form._current_id(), cid)
            # 没库存的料不在出库列表里,这时必须返回 False 让调用方说话,
            # 而不是静默什么都不发生
            _no_stock = app.con.execute(
                "SELECT c.id FROM component c WHERE c.merged_into IS NULL"
                " AND NOT EXISTS (SELECT 1 FROM stock s WHERE s.component_id=c.id"
                " AND s.qty > 0) LIMIT 1").fetchone()
            if _no_stock:
                check("没库存的料选不上,返回 False 让调用方去解释",
                      out_form.select_by_id(_no_stock[0]), False)
            out_form.q.set("")
            out_form.reload()
            app.update()


        # ==================================================== 按 BOM 收料清单
        p("\n【27】按 BOM 收料:品类看得见、能勾选、一键入库")
        pid27 = API(server.create_project, body={"name": "SELFTEST-收料", "qty": 1})["id"]
        c27a = API(server.create_component, body={
            "name": "SELFTEST-收料电容", "category": "电容",
            "value": "1uF", "package": "0603"})["id"]
        c27b = API(server.create_component, body={
            "name": "SELFTEST-收料电阻", "category": "电阻",
            "value": "10k\u03a9", "package": "0603"})["id"]
        b27a = API(server.add_bom_line, match=(pid27,),
                   body={"component_id": c27a, "required_qty": 7})["id"]
        b27b = API(server.add_bom_line, match=(pid27,),
                   body={"component_id": c27b, "required_qty": 4})["id"]

        app.refresh_all()
        app.update()
        pr.t_proj.selection_set(str(pid27))
        app.nb.select(app.tab_proj)
        pr.sub.select(pr.pane_in)
        pr.pane_in.show_bom()
        app.update()
        rp = pr.pane_in.bom_form
        rp.set_project(pid27)
        app.update()

        check("收料清单按 BOM 展开", len(rp.lines), 2)
        cols = [rp.tree.heading(k, "text") for k in rp.tree["columns"]]
        check("表里有「品类」这一列(收料时最容易搞错的就是这个)",
              "品类" in cols, True)
        check("勾选框那一列排在最前面", cols[0], "选")
        rows = {int(i): rp.tree.item(i, "values") for i in rp.tree.get_children()}
        check("每行都写着品类",
              sorted(r[col_of(rp, "category")] for r in rows.values()),
              ["\u7535\u5bb9", "\u7535\u963b"])
        check("数量默认取 BOM 需求,不用自己填",
              sorted(int(r[col_of(rp, "qty")]) for r in rows.values()), [4, 7])
        check("默认一个都没勾(勾了才是收到了)", len(rp.picked), 0)
        check("每条最前面都是空框",
              sorted(set(r[col_of(rp, "pick")] for r in rows.values())),
              [gui.CHECK_OFF])

        # 走真实的点击路径,而不是直接调 toggle —— 「点不上」正是要防的毛病
        rp.tree.see(str(b27a))
        rp.tree.update_idletasks()
        bbox = rp.tree.bbox(str(b27a), "#1")
        check("那个勾选框真的画在屏幕上了", bool(bbox), True)
        if bbox:
            class _Ev:
                pass
            ev = _Ev()
            ev.x, ev.y = bbox[0] + bbox[2] // 2, bbox[1] + bbox[3] // 2
            rp.on_click(ev)
            app.update()
        check("点「选」那一格就勾上了", b27a in rp.picked, True)
        check("勾上之后格子里是打钩",
              rp.tree.item(str(b27a), "values")[col_of(rp, "pick")], gui.CHECK_ON)
        rp.on_click(ev)
        app.update()
        check("再点一下取消勾选", b27a in rp.picked, False)
        rp.on_click(ev)
        app.update()
        check("再点回来又勾上了", b27a in rp.picked, True)

        # 双击改数量
        _ask27 = gui.ask_text
        gui.ask_text = lambda *a, **k: "5"
        try:
            rp.edit_qty(str(b27b))
            app.update()
        finally:
            gui.ask_text = _ask27
        check("双击能改这一行的数量",
              int(rp.tree.item(str(b27b), "values")[col_of(rp, "qty")]), 5)
        check("改了数量就顺手勾上(不然改了也不算)", b27b in rp.picked, True)

        # 校验失败时 gui.py 会 showinfo(「数量要填非负整数。」)—— 这一处**必须**
        # 自己桩住:从前它只桩了 ask_text,那条提示就弹到用户桌面上等人点,把
        # 整个自检卡住了(顶部那道兜底是保险,不是让段落偷懒的借口:段落自己桩住
        # 才断言得出「它到底有没有告诉用户」,而不是「有没有弹出去」)。
        _mb27b = gui.messagebox
        box27b = FakeBox()
        gui.messagebox = box27b
        _ask27b = gui.ask_text
        gui.ask_text = lambda *a, **k: "三"
        try:
            rp.edit_qty(str(b27b))
            app.update()
        finally:
            gui.ask_text = _ask27b
            gui.messagebox = _mb27b
        check("填了不是数字的东西,数量不变",
              int(rp.tree.item(str(b27b), "values")[col_of(rp, "qty")]), 5)
        check("而且不是默默不理,明说「要填非负整数」",
              any("非负整数" in str(m) for _t, m in box27b.infos), True)

        rp.check_all(True)
        app.update()
        check("「全选」把两行都勾上", sorted(rp.picked), sorted([b27a, b27b]))
        check("摘要里写着勾了几行、共几个", "2 \u884c" in rp.hint.get(), True)

        _mb27 = gui.messagebox
        box27 = FakeBox()
        gui.messagebox = box27
        try:
            rp.submit()
            app.update()
        finally:
            gui.messagebox = _mb27
        check("提交前先把要收的东西念了一遍", bool(box27.asks), True)
        for _cid27, _want27 in ((c27a, 7), (c27b, 5)):
            check(f"一键入库把 {_want27} 个收进来了",
                  app_con.execute("SELECT COALESCE(SUM(qty),0) FROM stock "
                                  "WHERE component_id=?", (_cid27,)).fetchone()[0], _want27)
        check("两笔都记在这个项目名下",
              app_con.execute("SELECT COUNT(*) FROM movement WHERE project_id=? "
                              "AND kind='IN' AND voided=0", (pid27,)).fetchone()[0], 2)
        check("流水还记得它是为哪条 BOM 需求收的",
              app_con.execute("SELECT COUNT(*) FROM movement WHERE bom_id=?",
                              (b27a,)).fetchone()[0], 1)
        check("收完把勾清空,免得再点一次又收一遍", len(rp.picked), 0)
        check("入库**不该**动「已发料」(货进来不等于发给板子了)",
              app_con.execute("SELECT COALESCE(SUM(placed_qty),0) FROM project_bom "
                              "WHERE project_id=?", (pid27,)).fetchone()[0], 0)

        # ------- #31:全收完的需求不许再出现在「元件出库」页上 -------
        # 收 7 个那条收了 7 个、收 4 个那条收了 5 个 —— 两条都已经做完。
        # 从前它们照样列在出库页上,用户看着像「上个 BOM 还能再出一次」。
        # 收进来的 7 个也必须看得出「已入库 7、没动 0」,别让人自己去减。
        rp.set_project(pid27)
        app.update()
        _rows27 = {int(i): rp.tree.item(i, "values")
                   for i in rp.tree.get_children()}
        check("收完之后收料页自己也不再列这两条(收完就是收完了)",
              _rows27, {})
        check("收料页也说得出挡掉了几条",
              "2 条已经做完,不再列出" in rp.hint.get(), True)

        pr.sub.select(pr.pane_out)
        app.update()
        po27 = pr.pane_out.bom_form
        check("★ 全部收完的两条需求,出库页一条都不再列",
              [l["bom_id"] for l in po27.lines], [])
        check("★ 后端如实报了挡掉几条,界面才说得出那句提示",
              po27.hidden_done, 2)
        check("提示里原话写着「2 条已经做完,不再列出」",
              "2 条已经做完,不再列出" in po27.hint.get(), True)

        # 只收了一半的那条必须还在,而且「还能出库」是减掉已入库之后的数 ——
        # 不减的话收了 4 个还说「还能出 10 个」,等于把这 4 个再发一遍。
        # 同一个项目里「一个元件只能有一行 BOM」(表上有唯一约束),所以另开一颗
        # 值/封装都一样的料来演这条需求
        c27c = API(server.create_component, body={
            "name": "SELFTEST-\u6536\u6599\u7535\u5bb9-\u534a\u6536", "category": "\u7535\u5bb9",
            "value": "1uF", "package": "0603"})["id"]
        b27c = API(server.add_bom_line, match=(pid27,),
                   body={"component_id": c27c, "required_qty": 10})["id"]
        API(server.stock_batch, body={"kind": "IN", "items": [
            {"component_id": c27c, "qty": 4, "bom_id": b27c}]})
        po27.set_project(pid27)
        app.update()
        _ln27 = next(l for l in po27.lines if l["bom_id"] == b27c)
        check("只收了一半的那条照样列在出库页",
              [l["bom_id"] for l in po27.lines], [b27c])
        check("★ 已入库 4 个,「还能出库」就只剩 6 个(不是 10 个)",
              _ln27["remaining"], 6)
        check("父行上写着「还能出 6」,不是「还差 10」",
              "\u8fd8\u80fd\u51fa 6" in po27.tree.item(str(b27c), "text"), True)
        check("收进来的货就在候选里(两张表看的是同一份库存)",
              c27c in [c["id"] for c in _ln27["candidates"]], True)
        check("而且候选上写的库存就是刚收的 4 个",
              next(c["on_hand"] for c in _ln27["candidates"]
                   if c["id"] == c27c), 4)

        # 收料页那一侧:这条需求要 10、已入库 4、没动 6,三个数都看得见
        pr.sub.select(pr.pane_in)
        app.update()
        rp.set_project(pid27)
        app.update()
        _v27 = rp.tree.item(str(b27c), "values")
        check("收料页把「已入库 4」摆出来了",
              int(_v27[col_of(rp, "received")]), 4)
        check("收料页把「还没动 6」也摆出来了(不用自己拿需求去减)",
              int(_v27[col_of(rp, "left")]), 6)
        check("本次入库的默认数就是「还没收的那 6 个」",
              int(_v27[col_of(rp, "qty")]), 6)
        pr.sub.select(0)
        app.update()

        # ==================================================== 按 BOM 出库分配树
        p("\n【28】按 BOM 出库:一条需求由几颗库存料凑齐")
        pid28 = API(server.create_project, body={"name": "SELFTEST-分配", "qty": 1})["id"]
        c28bom = API(server.create_component, body={
            "name": "SELFTEST-BOM\u6307\u5b9a\u7684\u7535\u5bb9", "category": "\u7535\u5bb9",
            "value": "68nF", "package": "0603"})["id"]
        b28 = API(server.add_bom_line, match=(pid28,),
                  body={"component_id": c28bom, "required_qty": 10})["id"]
        c28a = API(server.create_component, body={
            "name": "SELFTEST-68nF-0603-\u5e93\u5b58", "category": "\u7535\u5bb9",
            "value": "68nF", "package": "0603"})["id"]
        c28c = API(server.create_component, body={
            "name": "SELFTEST-68nF-0805-\u5e93\u5b58", "category": "\u7535\u5bb9",
            "value": "68nF", "package": "0805"})["id"]
        # 品类也用一个库里没有的:不然「品类相同」那一分会让别的料混进候选,
        # 而这条要测的恰恰是「一颗都凑不出来」
        c28none = API(server.create_component, body={
            "name": "SELFTEST-\u5e93\u91cc\u6ca1\u6709\u7684\u6599",
            "category": "SELFTEST-\u65e0\u5e93\u5b58\u7c7b", "value": "XF-9999",
            "package": "NOPE999"})["id"]
        b28none = API(server.add_bom_line, match=(pid28,),
                      body={"component_id": c28none, "required_qty": 3})["id"]
        # 库里:0603 有 8 个、0805 有 2 个 —— 正是「一条需求两颗料凑」的场景
        API(server.stock_move, body={"kind": "IN", "component_id": c28a, "qty": 8,
                                     "location": "\u672a\u5206\u7c7b"})
        API(server.stock_move, body={"kind": "IN", "component_id": c28c, "qty": 2,
                                     "location": "\u672a\u5206\u7c7b"})

        app.refresh_all()
        app.update()
        pr.t_proj.selection_set(str(pid28))
        app.update()
        pp = pr.pane_out.bom_form
        pr.pane_out.show_bom()
        pp.set_project(pid28)
        app.update()

        plan = API(server.project_pick_plan, match=(pid28,))
        line28 = next(l for l in plan["lines"] if l["bom_id"] == b28)
        check("分配方案里这条需求要 10 个", line28["need"], 10)
        check("还需要 10 个", line28["remaining"], 10)
        check("能凑它的库存料有两颗",
              sorted(c["id"] for c in line28["candidates"]), sorted([c28a, c28c]))
        check("值+封装都对上的那颗排在最前面",
              line28["candidates"][0]["id"], c28a)
        check("封装不同的那颗也列出来了(它正是用来凑剩下的)",
              line28["candidates"][1]["id"], c28c)

        parent = str(b28)
        check("这条需求成了树上的一行(父行)", pp.tree.exists(parent), True)
        check("展开箭头下面挂着两颗库存料",
              sorted(pp.tree.get_children(parent)),
              sorted([pp.child_iid(b28, c28a), pp.child_iid(b28, c28c)]))
        # 父行的文案从「还差 N」改成「还能出 N」(#31):这个数现在减掉的是
        # 「已发料 + 已入库」两本账,再叫「还差」会让人以为只扣了发出去的那部分。
        # 列名仍然是 left,断言按值取照样看得住这个数
        check("父行上写着「还能出」多少",
              "\u8fd8\u80fd\u51fa 10" in pp.tree.item(parent, "text"), True)
        check("父行也带品类",
              pp.tree.item(parent, "values")[col_of(pp, "category")], "\u7535\u5bb9")
        check("子行默认没勾",
              pp.tree.item(pp.child_iid(b28, c28a),
                           "values")[col_of(pp, "pick")], gui.CHECK_OFF)

        # ------- 用户要的就是这一件事:0603 出 8 个之后,0805 那边自动变成还差 2
        pp.toggle(pp.child_iid(b28, c28a))
        app.update()
        check("勾上 0603 那颗,默认把它 8 个库存全出", pp.alloc[(b28, c28a)], 8)
        check("父行的「还能出库」立刻从 10 变成 2",
              int(pp.tree.item(parent, "values")[col_of(pp, "left")]), 2)
        check("0805 那行的「还能出库」也变成 2",
              int(pp.tree.item(pp.child_iid(b28, c28c),
                               "values")[col_of(pp, "left")]), 2)
        pp.toggle(pp.child_iid(b28, c28c))
        app.update()
        check("再勾 0805,默认正好补上剩下的 2 个", pp.alloc[(b28, c28c)], 2)
        check("父行的「还能出库」归零",
              int(pp.tree.item(parent, "values")[col_of(pp, "left")]), 0)
        check("父行上说「齐了」", "\u9f50\u4e86" in pp.tree.item(parent, "text"), True)

        _mb28 = gui.messagebox
        box28 = FakeBox()
        gui.messagebox = box28
        try:
            pp.submit()
            app.update()
        finally:
            gui.messagebox = _mb28
        check("出库前把要发的东西念了一遍", bool(box28.asks), True)
        for _cid28 in (c28a, c28c):
            check("那一颗的库存被扣光了",
                  app_con.execute("SELECT COALESCE(SUM(qty),0) FROM stock "
                                  "WHERE component_id=?", (_cid28,)).fetchone()[0], 0)
        check("两笔流水都记在这个项目名下",
              app_con.execute("SELECT COUNT(*) FROM movement WHERE project_id=? "
                              "AND kind='OUT' AND voided=0", (pid28,)).fetchone()[0], 2)
        check("两笔流水都记住了自己是顶哪条 BOM 需求",
              app_con.execute("SELECT COUNT(*) FROM movement WHERE bom_id=?",
                              (b28,)).fetchone()[0], 2)
        check("这条 BOM 需求的「已发料」记成了 10",
              app_con.execute("SELECT placed_qty FROM project_bom WHERE id=?",
                              (b28,)).fetchone()[0], 10)
        check("出完勾选清空", len(pp.alloc), 0)

        # 反过来也要成立(#31 要的是**对称**):全部出库完的那条,收料页也不该再列
        # 它 —— 否则用户会以为已经发给板子的料还能再收一遍。库里一颗都没有的那条
        # (b28none)一颗都没动过,必须照样在:用来确认「挡掉一条」不是「整张表空掉」。
        pr.sub.select(pr.pane_in)
        pr.pane_in.show_bom()
        app.update()
        rp28 = pr.pane_in.bom_form
        rp28.set_project(pid28)
        app.update()
        check("全部出库完的 b28,收料页也不再列它(两个面板对称)",
              rp28.tree.exists(str(b28)), False)
        check("但一颗都没动过的那条照样在(不是整张表空掉)",
              rp28.tree.exists(str(b28none)), True)
        check("收料页也说得出挡掉了几条",
              "1 条已经做完,不再列出" in rp28.hint.get(), True)
        pr.sub.select(pr.pane_out)
        pr.pane_out.show_bom()
        app.update()

        # 撤销一笔出库,已发料要退回去 —— 不退的话界面会说「还差 0 个」,
        # 而东西其实已经还回架上了
        mid28 = app_con.execute(
            "SELECT id FROM movement WHERE bom_id=? AND kind='OUT' AND voided=0"
            " ORDER BY id DESC LIMIT 1", (b28,)).fetchone()[0]
        API(server.void_movement, match=(mid28,), body={"operator": "SELFTEST"})
        check("撤销一笔出库后,已发料退回到 8",
              app_con.execute("SELECT placed_qty FROM project_bom WHERE id=?",
                              (b28,)).fetchone()[0], 8)
        check("撤销补的反向流水也记着那条 BOM 需求",
              app_con.execute("SELECT COUNT(*) FROM movement WHERE bom_id=?",
                              (b28,)).fetchone()[0], 3)

        # 库里一颗都没有的那条:展开不能是空的,空白会让人以为界面坏了
        pp.set_project(pid28)
        app.update()
        check("库里一颗都没有的那条,展开写着「没有能凑它的料」",
              pp.tree.exists(f"{b28none}:none"), True)
        check("那种行勾不上", pp.is_checkable(f"{b28none}:none"), False)
        check("不是这条需求的候选时勾不上,让调用方去解释",
              pp.check_for(b28none, c28a), False)

        # 自动配齐:按相似度先配一遍,但绝不自动提交
        API(server.stock_move, body={"kind": "IN", "component_id": c28a, "qty": 20,
                                     "location": "\u672a\u5206\u7c7b"})
        pp.set_project(pid28)
        app.update()
        n_out = app_con.execute("SELECT COUNT(*) FROM movement WHERE project_id=? "
                                "AND kind='OUT'", (pid28,)).fetchone()[0]
        want_fill = next(l["remaining"] for l in pp.lines if l["bom_id"] == b28)
        pp.auto_fill()
        app.update()
        check("自动配齐把缺口填满,填的正好是还差的那个数",
              pp.alloc.get((b28, c28a)), want_fill)
        check("填完之后这条需求就不缺了", pp.remain(b28), 0)
        check("它只是填勾选,不会自己提交",
              app_con.execute("SELECT COUNT(*) FROM movement WHERE project_id=? "
                              "AND kind='OUT'", (pid28,)).fetchone()[0], n_out)

        # BOM 自己指定的那颗料只要还有库存,就必须排在最前面 ——
        # 它才是 BOM 本来要的东西,相似度再高也只是「像」
        API(server.stock_move, body={"kind": "IN", "component_id": c28bom, "qty": 5,
                                     "location": "\u672a\u5206\u7c7b"})
        pp.set_project(pid28)
        app.update()
        line28b = next(l for l in pp.lines if l["bom_id"] == b28)
        check("BOM 自己那颗料有库存时排在最前面",
              line28b["candidates"][0]["id"], c28bom)
        check("它被标成「BOM 本行指定的料」", line28b["candidates"][0]["own"], True)

        # 从 BOM 明细的「找相似库存…」挑一颗:要自动切到出库页,并挂到这条
        # 需求的分配树上勾好 —— 而不是丢回一个平表让人自己再找一遍
        pr.t_bom.selection_set(str(b28))
        app.update()
        pp.alloc = {}
        pr._pick_similar_from_line(b28, c28bom)
        app.update()
        check("从 BOM 明细挑中的料会自动切到「元件出库」子页签",
              pr.sub.index(pr.sub.select()), 2)
        check("并且按相似度挂在这条需求的分配树上勾好了",
              (b28, c28bom) in pp.alloc, True)

        pr.pane_in.show_bom()
        pr.pane_out.show_bom()
        app.update()

        # ==================================================== 收料的单个入库
        p("\n【29】收料:除了一键全部入库,还要能只入库这一行")
        pid29 = API(server.create_project, body={"name": "SELFTEST-单收", "qty": 1})["id"]
        # 名字取短的:工具栏按钮只显示名字的前 12 个字,
        # 名字太长的话「按钮上写着选的是哪一行」这条断言就变成断言截断了
        c29a = API(server.create_component, body={
            "name": "SELFTEST-A", "category": "电容",
            "value": "470nF", "package": "0603"})["id"]
        c29b = API(server.create_component, body={
            "name": "SELFTEST-B", "category": "电阻",
            "value": "22k\u03a9", "package": "0603"})["id"]
        b29a = API(server.add_bom_line, match=(pid29,),
                   body={"component_id": c29a, "required_qty": 6})["id"]
        b29b = API(server.add_bom_line, match=(pid29,),
                   body={"component_id": c29b, "required_qty": 3})["id"]

        app.refresh_all()
        app.update()
        pr.t_proj.selection_set(str(pid29))
        app.nb.select(app.tab_proj)
        pr.sub.select(pr.pane_in)
        pr.pane_in.show_bom()
        app.update()
        rp29 = pr.pane_in.bom_form
        rp29.set_project(pid29)
        app.update()

        check("收料区有「只入库这一行」这个入口", hasattr(rp29, "btn_one"), True)
        check("按钮上写着它是干什么的", rp29.ONE in rp29.btn_one.cget("text"), True)
        check("没选中任何行时,不知道要收哪一行", rp29.selected_bid(), None)

        def stock29(cid):
            return app_con.execute(
                "SELECT COALESCE(SUM(qty),0) FROM stock WHERE component_id=?",
                (cid,)).fetchone()[0]

        _mb29 = gui.messagebox
        box29 = FakeBox()
        gui.messagebox = box29
        try:
            # 一行都没选就点:要说清楚,而且**什么都不能动**
            rp29.receive_selected()
            app.update()
        finally:
            gui.messagebox = _mb29
        check("没选中就点,会解释而不是默默什么都不做", len(box29.infos) >= 1, True)
        check("而且没有误收任何东西", (stock29(c29a), stock29(c29b)), (0, 0))

        rp29.tree.selection_set(str(b29a))
        app.update()
        check("选中之后按钮上写着选的是哪一行(免得点下去才发现选错行)",
              "SELFTEST-A" in rp29.btn_one.cget("text"), True)

        # 先勾上另一行,再单收选中那一行 —— 别的勾绝不能被顺手清掉
        rp29.picked.add(b29b)
        rp29.render()
        app.update()
        box29b = FakeBox()
        _mb29b = gui.messagebox
        gui.messagebox = box29b
        try:
            rp29.receive_selected()
            app.update()
        finally:
            gui.messagebox = _mb29b
        check("单收之前先把要收的东西念了一遍", bool(box29b.asks), True)
        check("单个入库只收了选中那一行", stock29(c29a), 6)
        check("另一行一颗都没动", stock29(c29b), 0)
        check("别的勾没被顺手清掉(否则接着收第二行得重新勾一遍)",
              b29b in rp29.picked, True)
        check("收过的那行自己取消了勾(免得再点一次又收一遍)",
              b29a in rp29.picked, False)

        # 右键菜单:同一个动作要有第二条入口
        items29 = [rp29.menu.entrycget(i, "label")
                   for i in range(rp29.menu.index("end") + 1)
                   if rp29.menu.type(i) == "command"]
        check("右键菜单里有「只入库这一行」", rp29.ONE in items29, True)
        check("也有「改本次入库数量」", any("改本次入库数量" in x for x in items29), True)
        check("也有勾选 / 取消勾选",
              any("勾选这一行" in x for x in items29)
              and any("取消勾选" in x for x in items29), True)

        rp29.tree.selection_set(str(b29b))
        app.update()
        rp29.set_checked(True)
        app.update()
        check("右键菜单那个「勾选这一行」真的勾上了", b29b in rp29.picked, True)

        # 批量那条路照旧能用,而且和单收共用同一条 move_rows
        box29c = FakeBox()
        _mb29c = gui.messagebox
        gui.messagebox = box29c
        try:
            rp29.submit()
            app.update()
        finally:
            gui.messagebox = _mb29c
        check("批量入库还能用,收的是剩下那一行", stock29(c29b), 3)
        check("批量也照样先确认一遍", bool(box29c.asks), True)
        check("批量走的就是单收那条路(同一个函数)",
              "move_rows" in type(rp29).submit.__code__.co_names
              or "move_rows" in type(rp29).__dict__.keys(), True)

        # 名字撞车时确认框里必须还分得清 —— 这一条把 #1 和 #2 接上了
        c29d = API(server.create_component, body={
            "name": "877nF", "category": "电容",
            "value": "877nF", "package": "0603"})["id"]
        c29e = API(server.create_component, body={
            "name": "877nF", "category": "电容",
            "value": "877nF", "package": "0805"})["id"]
        b29d = API(server.add_bom_line, match=(pid29,),
                   body={"component_id": c29d, "required_qty": 1})["id"]
        b29e = API(server.add_bom_line, match=(pid29,),
                   body={"component_id": c29e, "required_qty": 1})["id"]
        rp29.set_project(pid29)
        app.update()
        rp29.tree.selection_set(str(b29d))
        app.update()
        box29d = FakeBox()
        _mb29d = gui.messagebox
        gui.messagebox = box29d
        try:
            rp29.receive_selected()
            app.update()
        finally:
            gui.messagebox = _mb29d
        ask29 = box29d.asks[0][1] if box29d.asks else ""
        check("名字撞车时,确认框里连封装一起写出来(否则分不清收的是哪颗)",
              "0603" in ask29, True)

        # ==================================================== BOM 明细详情面板
        p("\n【30】点 BOM 明细的一行,右边出详情,品类能直接下拉改")
        pr.sub.select(0)
        app.update()
        d30 = pr.detail
        check("BOM 明细右边真有一块详情面板", isinstance(d30, gui.LineDetail), True)
        check("还没选行时面板明说「先在左边点一行物料」",
              "先在左边点一行" in d30.tip.get(), True)

        pr.t_bom.selection_set(str(b29a))
        app.update()
        check("选中一行之后,面板记下了是哪条需求", d30.line.get("bom_id"), b29a)
        check("面板上写着这颗料的值", d30.v2["value"].get(), "470nF")
        check("写着封装", d30.v2["package"].get(), "0603")
        check("写着这一条需求的单块用量", d30.v["per_board"].get(), "6")

        # 品类:只读展示 + 「换…」,一级一级选(不再是一个装着全路径的长下拉)
        check("品类那一栏是只读的,靠「换…」按钮去逐级选",
              "readonly" in d30.cb_cat.state(), True)
        check("下拉里带着库里已经在用的品类", "电容" in d30.category_options(), True)
        check("也带着内置清单(库里还没有的那些)", len(d30.category_options()) > 3, True)
        check("当前显示的就是这颗料的品类", d30.cat.get(), "电容")

        d30.cat.set("SELFTEST-新品类")
        d30.save_category()
        app.update()
        check("改完立刻落库",
              app_con.execute("SELECT category FROM component WHERE id=?",
                              (c29a,)).fetchone()[0], "SELFTEST-新品类")
        check("面板自己也跟着变了", d30.comp.get("category"), "SELFTEST-新品类")
        check("BOM 明细那一行跟着变了",
              app_con.execute("SELECT c.category FROM project_bom b"
                              " JOIN component c ON c.id=b.component_id"
                              " WHERE b.id=?", (b29a,)).fetchone()[0], "SELFTEST-新品类")
        check("全量刷新之后面板还停在这条需求上(没跳回空白)",
              d30.line.get("bom_id"), b29a)

        box30 = FakeBox()
        _mb30 = gui.messagebox
        gui.messagebox = box30
        try:
            d30.cat.set("   ")
            d30.save_category()
            app.update()
        finally:
            gui.messagebox = _mb30
        check("品类不让填空(空着以后搜不到它)", len(box30.infos) >= 1, True)
        check("而且没真写进去",
              app_con.execute("SELECT category FROM component WHERE id=?",
                              (c29a,)).fetchone()[0], "SELFTEST-新品类")

        check("面板上写着现在有多少", "个" in d30.v2["on_hand_total"].get(), True)
        locs30 = d30.v2["locs"].get()
        check("写着分布在哪儿,没有就明说「库里一颗都没有」",
              (":" in locs30) or ("一颗都没有" in locs30), True)

        # ============================================ 同值不同封装要分得清
        p("\n【31】同值不同封装的两条需求,在分配树里不能长得一模一样")
        # 这一段**必须自带数据**。原来它借用【29】留下的 pid29:可 #31 之后
        # 「收完 / 发完」的需求两个面板都不再列出,而【29】恰好把 pid29 那两条
        # 877nF 都收完了 —— 收完就是收完了,表上本来就不该再有它们。拿剩饭来验
        # 「两条要分得清」,验到的只会是「还剩几条」,于是几条断言全红。
        # 所以另开一个项目,三条需求全是刚建、一颗都没动过(remaining 都 > 0),
        # 这样验的才是撞名那件事本身。
        pid31 = API(server.create_project,
                    body={"name": "SELFTEST-撞名", "qty": 1})["id"]
        c31a = API(server.create_component, body={
            "name": "877nF", "category": "电容",
            "value": "877nF", "package": "0603"})["id"]
        c31b = API(server.create_component, body={
            "name": "877nF", "category": "电容",
            "value": "877nF", "package": "0805"})["id"]
        c31c = API(server.create_component, body={
            "name": "SELFTEST-A31", "category": "电容",
            "value": "470nF", "package": "0603"})["id"]
        for _c31 in (c31a, c31b, c31c):
            API(server.add_bom_line, match=(pid31,),
                body={"component_id": _c31, "required_qty": 1})
        app.refresh_all()
        app.update()
        pr.t_proj.selection_set(str(pid31))
        app.update()
        pr.sub.select(pr.pane_out)
        pr.pane_out.show_bom()
        app.update()
        po31 = pr.pane_out.bom_form
        po31.set_project(pid31)
        app.update()
        texts31 = [po31.tree.item(i, "text") for i in po31.tree.get_children()]
        same31 = [t for t in texts31 if "877nF" in t]
        check("两条 877nF 的需求都在树里", len(same31), 2)
        check("它们不是同一条文案(撞车时必须把封装补进树列,否则扫一眼分不清)",
              len(set(same31)), 2)
        check("补进去的正是封装",
              any("0603" in x for x in same31)
              and any("0805" in x for x in same31), True)
        # 不撞车的那种不能也被补上 —— 否则等于把封装又加回名字了
        # (SELFTEST-A31 的值也是 470nF,但在这一页上它的名字只有它自己一个)
        solo31 = [t for t in texts31 if "SELFTEST-A31" in t]
        check("没撞车的那条不带封装(封装有单独的列,不重复)",
              bool(solo31) and "0603" not in solo31[0], True)

        p("\n【32】删掉最后一个项目之后,屏幕上的 BOM 明细不能留着")
        # 实测过:数据库那一层一直是好的 —— project_bom.project_id 声明了
        # ON DELETE CASCADE,db.connect() 里也有 PRAGMA foreign_keys = ON,
        # 删掉项目之后 project_bom 一行不剩。残影全在界面:
        # load_bom() 第一句就是 `if not self._pid: return`,没有项目的时候它
        # 压根不会被调用,于是表格、标题、缺料标签、详情面板一起停在旧内容上,
        # 用户看到的就是「项目删了,BOM 明细没删」。
        # 先把库里其它项目清掉,好造出「删的正好是最后一个」这个情形。
        for _row in list(app_con.execute("SELECT id FROM project").fetchall()):
            API(server.delete_project, match=(_row[0],))
        pid32 = API(server.create_project,
                    body={"name": "SELFTEST-删最后", "qty": 2})["id"]
        c32 = API(server.create_component, body={
            "name": "SELFTEST-C32", "category": "电容",
            "value": "330nF", "package": "0805"})["id"]
        API(server.add_bom_line, match=(pid32,),
            body={"component_id": c32, "required_qty": 5})
        app.refresh_all()
        app.update()
        app.nb.select(app.tab_proj)
        pr.t_proj.selection_set(str(pid32))
        app.update()
        check("删除前:这一页确实有内容(不然下面的断言等于没测)",
              len(pr.t_bom.get_children()) > 0, True)
        check("删除前:选中的就是这个项目", pr._pid, pid32)

        _mb32 = gui.messagebox
        box32 = FakeBox()
        gui.messagebox = box32
        try:
            pr.delete_project()
            app.update()
        finally:
            gui.messagebox = _mb32

        check("删之前问过确认(不能默默删)", len(box32.asks), 1)
        check("数据库里这个项目没了",
              app_con.execute("SELECT COUNT(*) FROM project WHERE id=?",
                              (pid32,)).fetchone()[0], 0)
        check("数据库里它的 BOM 明细也没了(CASCADE 本来就好的)",
              app_con.execute("SELECT COUNT(*) FROM project_bom WHERE project_id=?",
                              (pid32,)).fetchone()[0], 0)
        check("★ 界面上 BOM 明细表也清空了(以前这里会留着已删项目的行)",
              len(pr.t_bom.get_children()), 0)
        check("★ 内部那份 _lines 也清了", len(pr._lines), 0)
        check("标题不再写着已删项目的名字",
              pr.title.get(), "（左侧选一个项目）")
        check("缺料标签清空", pr.shortage.get(), "")
        check("入库区不再挂着已删项目", pr.pane_in.project_id, None)
        check("出库区不再挂着已删项目", pr.pane_out.project_id, None)
        check("入库收料清单清空", len(pr.pane_in.bom_form.lines), 0)
        check("出库分配树清空", len(pr.pane_out.bom_form.lines), 0)
        check("详情面板回到「先点一行」", "先在左边点一行" in pr.detail.tip.get(), True)
        check("全库没有指向已删项目的孤儿 BOM 行",
              app_con.execute("SELECT COUNT(*) FROM project_bom b LEFT JOIN project p"
                              " ON p.id=b.project_id WHERE p.id IS NULL").fetchone()[0], 0)
        check("删项目不该把元件一起带走",
              app_con.execute("SELECT COUNT(*) FROM component WHERE id=?",
                              (c32,)).fetchone()[0], 1)

        p("\n【33】用户自定义的属性:录入 -> 落库 -> 在入库/出库都看得见")
        # 属性名和值都由用户定(电容的耐压/精度、电阻的精度/功率、电感的额定/饱和电流…),
        # 存法是 component.params 那个 JSON 列。这里要测的不是「能存」——
        # 那本来就能存 —— 而是「填完之后在收料清单和出库分配树上真的看得见」。
        pid33 = API(server.create_project,
                    body={"name": "SELFTEST-属性", "qty": 1})["id"]
        cid33 = API(server.create_component, body={
            "name": "SELFTEST-C33", "category": "电容", "value": "100nF",
            "package": "0603", "params": {"耐压": "50V", "精度": "±5%"}})["id"]
        cid33b = API(server.create_component, body={
            "name": "SELFTEST-C33B", "category": "电阻", "value": "10k",
            "package": "0603"})["id"]          # 故意一个属性都不填
        b33 = API(server.add_bom_line, match=(pid33,),
                  body={"component_id": cid33, "required_qty": 4})["id"]
        b33b = API(server.add_bom_line, match=(pid33,),
                   body={"component_id": cid33b, "required_qty": 2})["id"]
        loc33 = API(server.create_location,
                    body={"code": "属性抽屉", "name": "属性抽屉"})["id"]
        API(server.stock_move, body={"kind": "IN", "component_id": cid33,
                                     "qty": 20, "location": "属性抽屉"})
        app.refresh_all()
        app.update()

        app.nb.select(app.tab_proj)
        pr.t_proj.selection_set(str(pid33))
        pr.sub.select(pr.pane_in)
        pr.pane_in.show_bom()
        app.update()
        rp33 = pr.pane_in.bom_form
        rp33.set_project(pid33)
        app.update()
        i_p = col_of(rp33, "params")
        got_p = rp33.tree.item(str(b33), "values")[i_p]
        check("收料清单里能看到这颗电容的属性", got_p, "50V · ±5%")
        check("没填属性的那颗料是空白,不是一个空的 {}",
              rp33.tree.item(str(b33b), "values")[i_p], "")
        check("搜属性值也能筛出来(仓库里认料就靠这个)",
              (rp33.q.set("50V"), rp33.render(), len(rp33.tree.get_children()))[-1], 1)
        rp33.q.set("")
        rp33.render()
        app.update()

        # 出库那一边:分配树里的候选料要能看到属性 ——
        # 值封装都一样、耐压不同的两颗料,正是这里最容易发错货
        pr.sub.select(pr.pane_out)
        pr.pane_out.show_bom()
        app.update()
        pp33 = pr.pane_out.bom_form
        pp33.set_project(pid33)
        app.update()
        child33 = pp33.child_iid(b33, cid33)
        check("分配树里有这颗料", pp33.tree.exists(child33), True)
        j_p = col_of(pp33, "params")
        check("分配树的候选行也显示属性",
              pp33.tree.item(child33, "values")[j_p], "50V · ±5%")
        check("需求那一行(父行)也带上属性",
              pp33.tree.item(str(b33), "values")[j_p], "50V · ±5%")
        child33b = pp33.child_iid(b33b, cid33b)
        if pp33.tree.exists(child33b):
            check("没属性的候选行是空白", pp33.tree.item(child33b, "values")[j_p], "")

        # 属性名要能按品类建议出来(候选,不是白名单)
        mt33 = API(server.meta)
        check("meta 说出了库里在用的属性名",
              "耐压" in (mt33.get("attrs_all") or []), True)
        check("而且是按品类归的(电容下面的属性名)",
              "耐压" in (mt33.get("attrs_by_category") or {}).get("电容", []), True)
        check("内置建议表也发了(电容 -> 耐压)",
              "耐压" in ((mt33.get("attrs_builtin") or {}).get("电容") or []), True)

        # 弹窗:读得回来、改得动、存得住
        d33 = gui.ComponentDialog(app, app, cid33)
        d33.update()
        names33 = [t[2].get() for t in d33.attr_rows]
        check("弹窗把已有的属性读成了行", sorted(names33), ["精度", "耐压"])
        check("值也读回来了", d33._parse_params().get("耐压"), "50V")
        for _r, _cb, vn, vv in d33.attr_rows:
            if vn.get() == "耐压":
                vv.set("100V")
        d33.attr_add("温度系数", "X7R")
        d33.save()
        app.update()
        app.refresh_all()
        app.update()
        got33 = API(server.get_component, match=(cid33,))
        check("改过的属性值存下来了", got33["params"].get("耐压"), "100V")
        check("新加的属性也存下来了", got33["params"].get("温度系数"), "X7R")
        check("没动的那条还在", got33["params"].get("精度"), "±5%")

        rp33.set_project(pid33)
        app.update()
        # 顺序不是字典序,是 attrs 里那份「重要度」顺序(耐压/精度 在 GENERIC 里,
        # 排在 温度系数 前面)—— 用户扫一眼先看到的是耐压和精度,那才是关键参数
        check("改完再回收料清单,显示的是新值(顺序按重要度,不是字典序)",
              rp33.tree.item(str(b33), "values")[i_p], "100V · ±5% · X7R")
        check("而字典序下 温度系数 会跑到前面 —— 那正是我们不想要的",
              "100V · X7R" not in rp33.tree.item(str(b33), "values")[i_p], True)

        p("\n【34】库存四级菜单:大类 -> 子类(可多级)-> 封装 -> 元件")

        # 搭一棵两层的品类树:菜单测试类 -> {子A, 子B}
        top34 = API(server.create_category, body={"name": "菜单测试类"})["id"]
        subA = API(server.create_category,
                   body={"name": "子A", "parent_id": top34})["id"]
        subB = API(server.create_category,
                   body={"name": "子B", "parent_id": top34})["id"]
        made34 = []
        for nm, cid34, pkg in (("菜单A-0603", subA, "0603"), ("菜单A-0805", subA, "0805"),
                               ("菜单B-0603", subB, "0603")):
            row34 = API(server.create_component, body={
                "name": nm, "category_id": cid34, "value": nm, "package": pkg})
            API(server.stock_move, body={"kind": "IN", "component_id": row34["id"],
                                         "qty": 5, "location": "未分类"})
            made34.append(row34["id"])
        # 一个大类,刻意一个子类都不加
        API(server.create_category, body={"name": "无子类测试"})
        lone34 = API(server.create_component, body={
            "name": "无子类料", "category": "无子类测试", "value": "x",
            "package": "SOT-23"})
        API(server.stock_move, body={"kind": "IN", "component_id": lone34["id"],
                                     "qty": 3, "location": "未分类"})
        app.refresh_all()
        app.update()
        app.nb.select(app.tab_comp)
        app.update()

        cols34 = [tab.tree.heading(c)["text"] for c in tab.tree["columns"]]
        k_nm34 = cols34.index("名称")

        def names34():
            return sorted(tab.tree.item(i, "values")[k_nm34]
                          for i in tab.tree.get_children())

        check("用户自建的顶层品类也出现在首页卡片上(加完总得有入口)",
              "菜单测试类" in tab.cards, True)

        # ---- 有子类 -> 先进中间页选子类
        gui.clear_tree(tab.tree)
        tab.open_category("菜单测试类")
        app.update()
        check("有子类的大类先让你选子类,不把几个子类混在一张表里", tab.view, "pick")
        p("DBG31 nb_current=%s comp_frame=%s comp_mapped=%s holder_mapped=%s "
          "pick_mgr=%r pick_mapped=%s app_mapped=%s"
          % (app.nb.index("current"), str(app.tab_comp),
             app.tab_comp.winfo_ismapped(), tab.holder.winfo_ismapped(),
             tab.page_pick.winfo_manager(), tab.page_pick.winfo_ismapped(),
             app.winfo_ismapped()))
        check("中间页真的显示出来了", bool(tab.page_pick.winfo_ismapped()), True)
        check("列表页收起来了", bool(tab.page_cat.winfo_ismapped()), False)
        check("这时候明细表是空的(不是偷偷把全部堆出来)",
              len(tab.tree.get_children()), 0)
        check("中间页上就是这个大类的 2 个子类",
              sorted(tab._pick_items and [c["name"] for c in tab._pick_items]),
              ["子A", "子B"])
        check("面包屑这时只有一级", [c["name"] for c in tab.crumb], ["菜单测试类"])
        check("中间页的卡片键就是子类 id",
              sorted(tab.pick_board.cards), sorted([str(subA), str(subB)]))

        # ---- 选子类 -> 进列表页,而且只显示这一支的料
        tab._pick_one(str(subA))
        app.update()
        check("选了子类之后进列表页", tab.view, "cat")
        check("只显示这个子类下的料,不是整个大类混在一起",
              names34(), ["菜单A-0603", "菜单A-0805"])
        check("面包屑变成两级(大类 / 子类)",
              [c["name"] for c in tab.crumb], ["菜单测试类", "子A"])
        check("标题是当前这一级,图标配色跟大类走(不然一家子看不出是一家)",
              tab.cat_title.get(), "子A")

        # ---- 封装这一级(菜单第三级)
        check("封装这一级的芯片列出来了(含「全部」)",
              sorted(k for k in tab.pkg_chips if k != "__many__"), ["", "0603", "0805"])
        tab.pick_package("0603")
        app.update()
        check("点了 0603 的芯片之后只剩 0603 的料", names34(), ["菜单A-0603"])
        check("面包屑补上了封装这一级",
              [(c["kind"], c["name"]) for c in tab.crumb],
              [("cat", "菜单测试类"), ("cat", "子A"), ("pkg", "0603")])
        check("选中的芯片用 ● 标出来(ttk 按钮没有按下态,得自己标)",
              tab.pkg_chips["0603"].cget("text").startswith("●"), True)
        check("没选的芯片没有 ●",
              tab.pkg_chips["0805"].cget("text").startswith("●"), False)

        # ---- 面包屑可以点着往回走
        tab.crumb_to(1)
        app.update()
        check("点面包屑的「子A」退到子类这一层,封装筛选被清掉",
              (tab.view, tab._facet_value(tab.f_pkg)), ("cat", ""))
        check("退回之后两级的料都回来了", names34(), ["菜单A-0603", "菜单A-0805"])
        tab.crumb_to(0)
        app.update()
        check("点面包屑的大类退回选子类那一层", tab.view, "pick")

        # ---- 没有子类就不多这一页
        tab.go_home()
        app.update()
        tab.open_category("无子类测试")
        app.update()
        check("没有子类的大类跳过中间页,直接进列表", tab.view, "cat")
        check("而且面包屑只有一级 —— 没有凭空多出来的第二级",
              [c["name"] for c in tab.crumb], ["无子类测试"])
        check("它自己的料正常显示", names34(), ["无子类料"])

        # ---- 钻到第三级之后刷新,要留在原地
        tab.go_home()
        app.update()
        tab.open_category("菜单测试类")
        app.update()
        tab._pick_one(str(subA))
        app.update()
        tab.pick_package("0805")
        app.update()
        check("先钻到第三级", [c["name"] for c in tab.crumb], ["菜单测试类", "子A", "0805"])
        app.refresh_all()
        app.update()
        check("刷新之后还留在原来那三级 —— 不弹回首页",
              (tab.view, [c["name"] for c in tab.crumb]),
              ("cat", ["菜单测试类", "子A", "0805"]))
        check("表里还是筛过的那一行", names34(), ["菜单A-0805"])

        # ---- 品类管理窗口
        dlg34 = gui.CategoryManagerDialog(app, app)
        dlg34.update()
        # _flat 的键是 int(id)。这里写 str 的话永远 False —— 断言会「失败」
        # 还好,怕的是写成「期望 False」的那条:它会通过,但什么都没验到。
        check("品类管理窗口把树列出来了", top34 in dlg34._flat, True)
        check("窗口里的树是分层的(子类挂在父类下面)",
              dlg34.tree.parent(str(subA)), str(top34))
        check("每个节点的全路径拼好了", dlg34._flat[subA]["path"], "菜单测试类 / 子A")
        check("含子类的元件数统计出来了(删之前要靠它告诉用户会牵连多少)",
              int(dlg34._flat[top34]["total"]) >= 2, True)

        _ask34 = gui.ask_text
        gui.ask_text = lambda *a, **k: "菜单测试类(新增)"
        try:
            dlg34.add_root()
            dlg34.update()
        finally:
            gui.ask_text = _ask34
        check("窗口里能加顶级品类",
              "菜单测试类(新增)" in [n["path"] for n in dlg34._flat.values()], True)

        _ask34b = gui.ask_text
        gui.ask_text = lambda *a, **k: "子C"
        try:
            dlg34.tree.selection_set(str(subB))
            dlg34.add_child()
            dlg34.update()
        finally:
            gui.ask_text = _ask34b
        check("窗口里能给选中的节点加子品类",
              "菜单测试类 / 子B / 子C" in [n["path"] for n in dlg34._flat.values()], True)

        # ---- 删品类:元件一个都不能少
        n_before34 = API(server.list_components, query={"limit": "0"})["total"]
        _mb34 = gui.messagebox
        asked34 = {}

        class _Box34:
            @staticmethod
            def askyesno(title, msg, **kw):
                asked34["title"] = title
                asked34["msg"] = msg
                return True

        gui.messagebox = _Box34
        try:
            dlg34.tree.selection_set(str(subA))
            dlg34.remove()
            dlg34.update()
        finally:
            gui.messagebox = _mb34
        check("删之前问了一句", asked34.get("title"), "确认删除")
        check("而且说清了元件不会被删(用户最怕的就是这个)",
              "不会被删除" in asked34.get("msg", ""), True)
        check("还说了有几个元件会被挪走", "2 个元件" in asked34.get("msg", ""), True)
        check("删完之后元件一个都没少",
              API(server.list_components, query={"limit": "0"})["total"], n_before34)
        left34 = API(server.list_components, query={"limit": "0"})["items"]
        moved34 = [c for c in left34 if c["id"] in made34[:2]]
        check("原来挂在「子A」下面的两颗料挪到了大类下面",
              sorted(c["category"] for c in moved34), ["菜单测试类", "菜单测试类"])
        check("子A这一级真的没了", subA in dlg34._flat, False)
        dlg34.destroy()
        app.update()

        # ---- 正在看的那一级被删掉:刷新要退到一个还存在的层级,而不是空页
        app.refresh_all()
        app.update()
        check("被删掉的那一级不会把菜单卡住(自动退回还存在的层级)",
              (tab.view, [c["name"] for c in tab.crumb]), ("pick", ["菜单测试类"]))
        check("而且还能继续选剩下的子类",
              sorted(c["name"] for c in tab._pick_items), ["子B"])
        check("首页卡片没被这通增删弄丢", "菜单测试类" in tab.cards, True)
        tab.go_home()
        app.update()

        p("\n【35】品类下拉要认得子类(而且别把子类归属冲掉)")

        sub35 = API(server.create_category,
                    body={"name": "下拉子类", "parent_id": top34})["id"]
        c35 = API(server.create_component, body={
            "name": "下拉测试料", "category_id": sub35, "value": "1uF",
            "package": "0603"})
        got35 = API(server.get_component, match=(c35["id"],))
        check("元件记下了它挂在树上哪个节点", got35["category_id"], sub35)
        check("而文本列仍旧写着顶层大类名(按品类分组的 SQL 全靠这一列)",
              got35["category"], "菜单测试类")

        d35 = gui.ComponentDialog(app, app, c35["id"])
        d35.update()
        check("编辑窗口记住了它挂在树上哪个节点(逐级选择器靠它预铺)",
              d35._picked_cat_id, sub35)
        check("打开时显示的就是全路径,不是大类名",
              d35.vars["category"].get(), "菜单测试类 / 下拉子类")

        # 逐级选:一级一个框,框里**只有这一级的名字**
        pk35 = gui.CategoryPickerDialog(d35, app, sub35)
        pk35.update()
        check("逐级选择窗口按现有归属铺好了两级(大类 + 二级)",
              len(pk35.step._rows), 2)
        check("第一级里只有顶层名字,没有「大类 / 子类」这种长路径",
              [v for v in pk35.step._rows[0][1].cget("values") if " / " in v], [])
        check("第二级里只有这一级的名字",
              "下拉子类" in list(pk35.step._rows[1][1].cget("values")), True)
        check("当前选中的就是它挂着的那一支",
              pk35.step.current_node()["id"], sub35)
        pk35.ok()
        check("确定交回节点 id 和全路径",
              pk35.result, (sub35, "菜单测试类 / 下拉子类"))
        check("确定之后窗口自己关掉", bool(pk35.winfo_exists()), False)

        # 用户要的核心:选完大类才出二级,没有二级就不弹
        pk35b = gui.CategoryPickerDialog(d35, app)
        pk35b.update()
        check("还没选的时候只有一级框", len(pk35b.step._rows), 1)
        _cb0 = pk35b.step._rows[0][1]
        _v0 = list(_cb0.cget("values"))
        check("第一级列的就是顶层大类", "菜单测试类" in _v0, True)
        _cb0.current(_v0.index("菜单测试类"))
        pk35b.step._on_pick(0)
        pk35b.update()
        check("选完大类才出二级", len(pk35b.step._rows), 2)
        check("二级里是它下面的子类",
              "下拉子类" in list(pk35b.step._rows[1][1].cget("values")), True)
        pk35b.destroy()

        # 关键回归:什么都不动直接保存,子类归属不能被冲回大类
        d35.save()
        app.update()
        check("不动品类直接保存,仍然挂在子类上",
              API(server.get_component, match=(c35["id"],))["category_id"], sub35)

        d35b = gui.ComponentDialog(app, app, c35["id"])
        d35b.update()
        d35b.vars["category"].set("菜单测试类 / 子B")
        d35b.save()
        app.update()
        got35b = API(server.get_component, match=(c35["id"],))
        check("下拉里换成另一个子类,保存后真的挪过去了", got35b["category_id"], subB)
        check("文本列跟着更新成大类的名字", got35b["category"], "菜单测试类")

        d35c = gui.ComponentDialog(app, app, c35["id"])
        d35c.update()
        d35c.vars["category"].set("下拉手打新类")
        d35c.save()
        app.update()
        got35c = API(server.get_component, match=(c35["id"],))
        check("认不出来的品类仍然能自己打字填(这条路不能被堵死)",
              got35c["category"], "下拉手打新类")
        flat35 = API(server.list_categories)["flat"]
        check("而且是建成顶层节点,不是挂到谁下面",
              [n["parent_id"] for n in flat35 if n["name"] == "下拉手打新类"], [None])

        # BOM 明细那块面板也要认得子类
        det35 = pr.detail
        det35.show({"component_id": c35["id"], "bom_id": None})
        app.update()
        check("BOM 明细面板的品类下拉也列出子类全路径",
              "菜单测试类 / 子B" in det35.category_options(), True)
        det35.cat.set("菜单测试类 / 下拉子类")
        det35.save_category()
        app.update()
        check("面板上把品类改成子类,真的挂到了那个子类上(不是同名的大类)",
              API(server.get_component, match=(c35["id"],))["category_id"], sub35)
        check("面板上回填的是子类全路径,不是大类名",
              (det35.show({"component_id": c35["id"], "bom_id": None}),
               det35.cat.get())[1], "菜单测试类 / 下拉子类")
        det35.clear()
        app.update()


        p("\n【36】库存菜单里就地改这一级 + 封装按钮按尺寸归并")

        tab36 = app.tab_comp
        app.nb.select(tab36)
        app.update()
        a36 = API(server.create_category, body={"name": "就地大类"})["id"]
        b36 = API(server.create_category, body={"name": "中间层", "parent_id": a36})["id"]
        c36 = API(server.create_category, body={"name": "叶子层", "parent_id": b36})["id"]
        m36 = API(server.create_component, body={
            "name": "就地测试料", "category_id": c36, "value": "4k7",
            "package": "0603"})["id"]

        # ---- 加子类:就在这一级上加,不打开品类管理窗口
        _ask36 = gui.ask_text
        gui.ask_text = lambda *a, **k: "钽电容"
        try:
            tab36._load_cat_tree()
            tab36.add_child_cat(tab36._cat_flat[b36])
            app.update()
        finally:
            gui.ask_text = _ask36
        _flat36 = API(server.list_categories)["flat"]
        check("就地加子类生效了", "钽电容" in [n["name"] for n in _flat36], True)
        check("新加的挂在原来那一级下面(不是又建了个顶层)",
              [n["parent_id"] for n in _flat36 if n["name"] == "钽电容"], [b36])

        # ---- 改名:只改这一级,元件的归属不动
        gui.ask_text = lambda *a, **k: "钽电容-改名"
        try:
            tab36._load_cat_tree()
            tab36.rename_cat(tab36._cat_flat[b36])
            app.update()
        finally:
            gui.ask_text = _ask36
        check("就地改名生效了",
              "钽电容-改名" in [n["name"] for n in API(server.list_categories)["flat"]],
              True)
        check("改名不动元件挂在哪儿",
              API(server.get_component, match=(m36,))["category_id"], c36)

        # ---- 删除:确认框必须写明「子类接到上一级、元件不会被删」
        _box36 = FakeBox()
        _mb36 = gui.messagebox
        gui.messagebox = _box36
        try:
            tab36._load_cat_tree()
            tab36.delete_cat(tab36._cat_flat[b36])
            app.update()
        finally:
            gui.messagebox = _mb36
        _msgs36 = " ".join(m for _t, m in _box36.asks)
        check("删除前会问一句", len(_box36.asks) >= 1, True)
        check("确认框里写明子类会接到上一级", "接到" in _msgs36, True)
        check("确认框里写明元件不会被删", "不会被删除" in _msgs36, True)
        check("删完叶子接到了大类上",
              [n["parent_id"] for n in API(server.list_categories)["flat"]
               if n["id"] == c36], [a36])
        check("料一动没动,还挂在原来那个叶子节点上(叶子只是接到了大类下面)",
              API(server.get_component, match=(m36,))["category_id"], c36)
        check("但它的大类文本换成了新的顶层名(否则写着已经不存在的大类)",
              API(server.get_component, match=(m36,))["category"], "就地大类")

        # ---- 卡片右键:就地增删改的入口
        _card36 = next(iter(tab36.board.cards.values()), None)
        check("首页卡片绑了右键菜单(就地改这一级)",
              bool(_card36 is not None and _card36.bind("<Button-3>")), True)
        check("中间页的卡片也绑了右键", tab36.pick_board.on_menu is not None, True)

        # ---- 逐级选择器里当场新建(挑到一半发现没有这一档)
        step36 = gui.CategoryStepBox(tab36, app.con)
        app.update()
        _cb36 = step36._rows[0][1]
        _v36 = list(_cb36.cget("values"))
        _cb36.current(_v36.index("就地大类"))
        step36._on_pick(0)
        app.update()
        check("选完大类才出二级", len(step36._rows), 2)
        gui.ask_text = lambda *a, **k: "当场新建的子类"
        try:
            step36.new_here(1)
            app.update()
        finally:
            gui.ask_text = _ask36
        _flat36b = API(server.list_categories)["flat"]
        check("在二级那一行能当场新建",
              "当场新建的子类" in [n["name"] for n in _flat36b], True)
        check("新节点挂在刚选中的大类下面",
              [n["parent_id"] for n in _flat36b if n["name"] == "当场新建的子类"],
              [a36])
        check("新建完自动选中它,接着就能往下选",
              step36.current_node()["name"], "当场新建的子类")
        step36.destroy()

        # ---- 封装按钮按尺寸归并:C0805 和 0805 只出一个按钮
        check("同尺寸的不同写法合成一个按钮",
              [g[1] for g in gui.pkg_groups(["C0805", "0805", "R0603"])],
              ["0805", "0603"])
        check("按钮上写的是尺寸本身,不是一长串",
              [g[0] for g in gui.pkg_groups(["C0805", "0805"])], ["0805"])
        check("认不出尺寸的不会被吃掉,按钮上原样写着用户那个词",
              [(g[0], g[1]) for g in gui.pkg_groups(["DIP-8"])],
              [("DIP-8", "DIP8")])
        tab36._render_pkg_chips(["C0805", "0805", "0603"])
        app.update()
        check("封装按钮只出 0805 和 0603 两个",
              sorted(k for k in tab36.pkg_chips if k), ["0603", "0805"])
        check("「全部」那个按钮还在", "" in tab36.pkg_chips, True)

        p("\n【37】钻取:三个页面互斥、面包屑不重复、有子类的品类能进本级")
        tab37 = app.tab_comp
        app.nb.select(tab37)
        app.update()

        def _npages(t):
            return len([1 for w in (t.page_home, t.page_cat, t.page_pick)
                        if w.winfo_ismapped()])

        def _crumb(t):
            return [c["name"] for c in t.crumb]

        # 测试库里不一定正好有「有子类、本级又有货」的品类 —— 自己造一个。
        # 不造的话下面几条会被跳过,跳过的断言等于没测。
        _host37 = next((n for n in tab37._cat_flat.values()
                        if n["parent_id"] is None
                        and int(n.get("own_stocked") or 0) > 0), None)
        if _host37 is None:
            p("  [!!] 测试库里没有「本级有货」的顶层品类,【37】没法测 —— 这不该发生")
        else:
            app.con.execute("INSERT INTO category(name, parent_id, sort) VALUES (?,?,?)",
                            ("测试子类", _host37["id"], 99))
            app.con.commit()
            tab37._load_cat_tree()
            tab37.open_category(_host37["name"])
            app.update()
            check("点进有子类的品类,屏幕上只有一页(以前会和列表页上下叠着)",
                  _npages(tab37), 1)
            check("有子类又有本级的品类,中间页给出了「本级」入口",
                  "self" in tab37.pick_board.cards, True)
            # #29:中间页那张「本级」卡片也绑着同一个右键回调。它就是**当前这一级**,
            # 不许被当成「找不到节点」而弹一句「这一级已经不在品类树里了」
            _sm37 = tab37._build_cat_menu("self")
            check("「本级」那张卡片的右键也有菜单(不是当成找不到节点)",
                  bool(_sm37) and any(l.startswith("＋ 在这下面加子品类")
                                      for l, _s in menu_labels(_sm37)), True)
            _kid = [k for k in tab37.pick_board.cards
                    if k.isdigit()
                    and (tab37._cat_flat.get(int(k)) or {}).get("name") == "测试子类"]
            check("刚造出来的子类出现在这一页上", len(_kid), 1)
            tab37._pick_one(_kid[0])
            app.update()
            _want = _crumb(tab37)          # 第一次点本来就该往下走一层
            check("点进子类,面包屑多了一层", len(_want) >= 2, True)
            for _ in range(3):
                tab37._pick_one(_kid[0])
                app.update()
            check("连点同一张卡片,面包屑不会越点越长", _crumb(tab37), _want)
            check("连点之后屏幕上仍然只有一页", _npages(tab37), 1)
            tab37.go_back()
            app.update()
            check("从子类退回上一层就回到中间页",
                  (_npages(tab37), "self" in tab37.pick_board.cards), (1, True))
            # 「显示零库存」默认开着,而 own_stocked 数的是**有货**的元件;
            # 这一条要保持它原本「只看有货」的语义,先把开关关掉(#22)
            tab37.zero_stock.set(False)
            tab37._pick_one("self")
            app.update()
            check("进本级后面包屑多一格「本级」", _crumb(tab37)[-1:], ["本级"])
            check("本级是列表页,而且屏幕上只有一页", _npages(tab37), 1)
            _rows37 = [str(tab37.tree.item(i, "values"))
                       for i in tab37.tree.get_children()]
            check("本级列表里只有挂在这一级的料(不含子类里的)",
                  len(_rows37), int(_host37.get("own_stocked") or 0))
        tab37.go_home()
        app.update()
        check("回到首页后面包屑清空", _crumb(tab37), [])

        app.refresh_all()
        p("\n【39】元件列表直接显示自定义属性(耐压、精度),列还能自己勾")
        
        # 用户要的是「电容的耐压精度直接就显示出来,不用点开」。属性名是他自己起的,
        # 所以列不写死:按这一页**真有值**的属性自动挑,也能在「列…」里自己勾。
        t39 = app.tab_comp
        check("自动挑:常用参数优先、最多两个",
              t39._auto_attr_cols([
                  {"params": {"功率": "0.1W", "耐压": "50V", "精度": "±5%"}}]),
              ["耐压", "精度"])
        check("一个属性都没填时,一列都不占",
              t39._auto_attr_cols([{"params": {}}, {"params": None}]), [])
        
        # 默认值要在**干净**环境里看:上一轮自检留下的 ui_columns.json 会把
        # 开关带过来,那测的就不是默认值了
        _cfg39clean = t39._col_cfg_path()
        if _cfg39clean and os.path.exists(_cfg39clean):
            os.remove(_cfg39clean)
        _t39fresh = gui.ComponentsTab(app.nb, app)
        check("新开的列表页默认就显示零库存(加完料一眼看得见)",
              _t39fresh.zero_stock.get(), True)
        _t39fresh.destroy()

        t39._want_cols = ["@耐压", "@精度"]
        t39._apply_cols([])
        _heads39 = [str(t39.tree.heading("a%d" % k, "text")) for k in (1, 2, 3, 4)]
        check("勾上的属性变成了列表的列标题", _heads39[:2], ["耐压", "精度"])
        _shown39 = [str(c) for c in t39.tree["displaycolumns"]]
        check("显示的是那两个槽", _shown39[-2:], ["a1", "a2"])
        check("没勾的空槽不显示", "a3" in _shown39, False)

        # 顺序可调 —— 用户原话「我想把耐压放在前面我就放在前面」
        t39._want_cols = ["@耐压", "name"]
        t39._apply_cols([])
        check("耐压能被摆到最前面",
              [str(c) for c in t39.tree["displaycolumns"]][:2], ["a1", "name"])
        check("摆到前面那格的标题就是耐压", t39.tree.heading("a1", "text"), "耐压")

        # 数量可加:不止两个
        t39._want_cols = ["@耐压", "@精度", "@容差", "name"]
        t39._apply_cols([])
        check("属性列能加到三个",
              [str(c) for c in t39.tree["displaycolumns"]][:3], ["a1", "a2", "a3"])

        # 不重要的列能藏掉
        t39._want_cols = ["name", "on_hand"]
        t39._apply_cols([])
        check("安全/状态/备注这些列能被用户藏掉",
              [c for c in ("state", "note") if c in t39.tree["displaycolumns"]], [])

        # 值要真的落到那一行上;没填的属性留空(不串位、也不是 None)
        t39._want_cols = ["@耐压", "@精度", "@容差"]
        t39._apply_cols([])
        t39._insert_component({"id": 990739, "name": "自检用", "package": "C0805",
                               "value": "100nF",
                               "params": {"耐压": "50V", "精度": "±5%"}})
        _vals39 = [str(v) for v in t39.tree.item("990739", "values")]
        check("行里带着耐压的值", "50V" in _vals39, True)
        check("行里带着精度的值", "±5%" in _vals39, True)
        check("元件没填的属性那一格就是空的", _vals39[2], "")
        t39.tree.delete("990739")

        # 设置要能记住(存在数据库旁边的 ui_columns.json,顺序和开关一起)
        _cfg39 = t39._col_cfg_path()
        t39._want_cols = ["@耐压", "name"]
        t39.zero_stock.set(False)
        t39._save_cols()
        check("列设置写到了数据库旁边", bool(_cfg39) and os.path.exists(_cfg39), True)
        t39._want_cols = None
        t39.zero_stock.set(True)
        t39._load_cols()
        check("下次打开能原样读回来(含顺序)", t39._want_cols, ["@耐压", "name"])
        check("「显示零库存」也跟着记住", t39.zero_stock.get(), False)

        # 列管理窗口本身:能勾、能调序、能加属性
        _dlg39 = gui.ColumnPickDialog(t39, ["name", "on_hand"], t39._attr_pool(),
                                      t39._base_labels(), t39._default_cols([]))
        app.update()
        check("列管理窗口列出了候选列", len(_dlg39.items) >= 2, True)
        check("取消一列", (_dlg39._shown.remove("name") or True)
              if "name" in _dlg39._shown else False, True)
        _dlg39._shown.append("name")
        _first39 = _dlg39.items[0]
        _dlg39._sel = lambda: 0                    # 选中第一行再往下挪
        _dlg39._move(1)
        check("↓ 能把一列往下挪", _dlg39.items[1], _first39)
        _dlg39.v_new.set("容差")
        _dlg39._add()
        check("能自己加一列属性名", "@容差" in _dlg39._shown, True)
        _dlg39.destroy()

        # ---- #26 按住一行上下拖就能调顺序(↑↓ 按钮必须留着,上面那两行就是它)
        p("\n【39.1】列管理:拖动调换列顺序")
        _t26 = app.tab_comp

        class _Ev26:
            """拖动那几个方法只认 event.y,给个够用的假事件就行。

            真去 event_generate 的话,得先等窗口被窗口管理器映射出来才拿得到
            行坐标(拿不到 bbox 时落点会退化成「插到最后」),自检会变成时好时坏。
            """
            def __init__(self, y):
                self.y = y

        def _row_y(dlg, i, after=False):
            """第 i 行里一个「算在下半 / 上半」的 y —— 上半个行插到它前面,
            下半个行插到它后面,和用户拖动时看到的那条指示线一致。"""
            bb = dlg.tree.bbox(str(i))
            if bb:
                return bb[1] + (bb[3] - 3 if after else 3)
            # 极端情况下(窗口还没映射出来)按行高硬算,落点是一样的
            return 25 + (i + (1 if after else 0)) * 20 + (6 if after else -6)

        def _drag(dlg, src, dst, after=False):
            dlg._drag_begin(_Ev26(_row_y(dlg, src)))
            dlg._drag_motion(_Ev26(_row_y(dlg, dst, after=after)))
            dlg._drag_end()

        def _dlg26_new(items, shown):
            """开一个列管理窗口,并把清单换成指定的那三行。

            用真实那些候选列的话,期望值会被当前那套列设置牵着走,
            断言就变成在看别的东西了。
            """
            d = gui.ColumnPickDialog(_t26, ["name", "on_hand"], _t26._attr_pool(),
                                     _t26._base_labels(), _t26._default_cols([]))
            app.update()
            d.items = list(items)
            d._shown = list(shown)
            d._refresh()
            app.update()
            return d

        _dlg26 = _dlg26_new(["A", "B", "C"], ["A", "C"])
        check("↑ ↓ 两个按钮还在(#26 不许弄坏它)",
              [t for t in buttons_of(_dlg26) if t.startswith(("↑", "↓"))],
              ["↑ 上移", "↓ 下移"])
        check("三个拖动事件都接上了",
              [bool(_dlg26.tree.bind(e)) for e in ("<ButtonPress-1>", "<B1-Motion>",
                                                   "<ButtonRelease-1>")],
              [True, True, True])

        _shown26 = sorted(_dlg26._shown)
        _drag(_dlg26, 2, 0)          # 把最后一行拖到最上面
        check("往上拖:被拖的那行插到了目标位置", _dlg26.items, ["C", "A", "B"])
        check("拖动只改顺序,原来勾着的一行都没被改掉",
              sorted(_dlg26._shown), _shown26)
        _dlg26._ok()
        check("拖完确定,结果(也就是表格的列序)就是拖后的顺序",
              _dlg26.result, ["C", "A"])

        # 往下拖,顺便看插入位置提示
        _dlg26b = _dlg26_new(["A", "B", "C"], ["A", "C"])
        _dlg26b._drag_begin(_Ev26(_row_y(_dlg26b, 0)))
        _dlg26b._drag_motion(_Ev26(_row_y(_dlg26b, 1, after=True)))
        check("拖到哪就记下了会插到第几格(这一处是插到第二行后面)",
              _dlg26b._drag_slot, 2)
        p("DBG31 dlg_mapped=%s tree_h=%s children=%s bboxes=%s slot_at68=%s"
          % (_dlg26b.winfo_ismapped(), _dlg26b.tree.winfo_height(),
             _dlg26b.tree.get_children(),
             [_dlg26b.tree.bbox(i) for i in _dlg26b.tree.get_children()],
             _dlg26b._slot_at(68)))
        check("拖动过程中有可见的插入位置提示(那条指示线画出来了)",
              bool(_dlg26b.line.place_info()), True)
        _dlg26b._drag_end()
        check("松手之后提示收起来", bool(_dlg26b.line.place_info()), False)
        check("往下拖:落到目标行的后面", _dlg26b.items, ["B", "A", "C"])
        _dlg26b.destroy()

        # 没勾的那行拖进勾着的行中间:它本来就不出现在表格里,所以可见的那几列
        # 顺序一个都不该动 —— 这就是「拖了但确定后没变」的合理情形
        _dlg26c = _dlg26_new(["A", "B", "C"], ["A", "C"])   # B 是没勾的
        _drag(_dlg26c, 1, 0)                                # 把 B 拖到最上面
        check("把没勾的行往上拖,它自己在候选清单里确实挪了位置",
              _dlg26c.items, ["B", "A", "C"])
        _dlg26c._ok()
        check("但它没被勾上,所以确定后那几列的顺序一点没变",
              _dlg26c.result, ["A", "C"])

        # #22 的原始场景:库存为 0 的新料确实存在(以前在 SQL 层就被滤掉)
        _dlg22 = gui.ComponentDialog(t39, app, None)
        _dlg22.vars["name"].set("自检-零库存新料")
        _dlg22.save()
        app.update()
        check("新增弹窗回传了新料的 id", bool(getattr(_dlg22, "new_id", None)), True)
        check("新料真的落库了",
              int(app.con.execute("SELECT COUNT(*) FROM component WHERE name=?",
                                  ("自检-零库存新料",)).fetchone()[0]), 1)
        check("新料库存是 0(所以「只看有货」时它根本不会出现)",
              float(app.con.execute(
                  "SELECT COALESCE(SUM(qty),0) FROM stock WHERE component_id=?",
                  (_dlg22.new_id,)).fetchone()[0]), 0.0)

        # 开关真的在管 SQL:同一级,开着比关着多出「零库存」的那些
        _node39 = t39._last_cat()
        if _node39:
            t39.zero_stock.set(True)
            t39.load_category(_node39)
            _on39 = len(t39.tree.get_children())
            t39.zero_stock.set(False)
            t39.load_category(_node39)
            _off39 = len(t39.tree.get_children())
            check("开关开着时能多看到零库存的料(以前在 SQL 层就被滤掉)",
                  _on39 > _off39, True)
            p("  这一级:开着 %d 条 / 关掉 %d 条" % (_on39, _off39))

        t39._want_cols = None          # 恢复出厂:自动挑
        t39.zero_stock.set(True)
        t39._save_cols()
        t39._apply_cols([])
        
        p("\n【38】刷新不能把选中和「最近流水」刷没")
        
        # 重建表格会让 Tk 发 <<TreeviewSelect>>,那一刻 selected_id() 是空的,
        # _on_select 于是把右下角「仓位分布 / 最近流水」清空 —— 用户看到的
        # 「出入库流水没显示」就是这么来的(流水其实写进去了)。
        t38 = app.tab_comp
        app.nb.select(t38)
        app.update()
        _kids38 = t38.tree.get_children()
        if _kids38:
            t38.tree.selection_set(_kids38[0])
            app.update()
            _hist38 = len(t38.t_hist.get_children())
            _stock38 = len(t38.t_stock.get_children())
            check("选中的行确实填了流水/仓位",
                  _hist38 > 0 or _stock38 > 0, True)
            t38.reload()
            app.update()
            check("reload 之后选中还在",
                  tuple(t38.tree.selection()), (_kids38[0],))
            check("reload 之后「最近流水」没被清空",
                  len(t38.t_hist.get_children()), _hist38)
            check("reload 之后「仓位分布」没被清空",
                  len(t38.t_stock.get_children()), _stock38)
        else:
            p("  (这一页没有行,跳过)")

        # ---------------------------------------------------------- #27 / #30
        p("\n【40】所有类表格的单元格和表头都居中(#27)")
        # 主窗口那几张表 + 几张手搭的表全查一遍:对齐以前是每个调用点自己传的,
        # 于是同一张表里「值」左对齐、「数量」右对齐混着。这里按整棵控件树查,
        # 漏掉哪一处都会露出来(不只是 make_tree 建的那几张)。
        _dlg40 = gui.ColumnPickDialog(app.tab_comp, ["name"], app.tab_comp._attr_pool(),
                                      app.tab_comp._base_labels(),
                                      app.tab_comp._default_cols([]))
        app.update()
        _dialogs40 = [("列管理", _dlg40)]
        _cat40 = None
        try:
            _cat40 = gui.CategoryManagerDialog(app, app)
            app.update()
            _dialogs40.append(("品类管理", _cat40))
        except Exception as exc:  # noqa: BLE001
            p(f"  (品类管理窗口跳过:{type(exc).__name__}: {exc})")
        try:
            app.nb.select(app.tab_comp)
            app.update()
            _bad40 = []
            for _where40, _root40 in [("主窗口", app)] + _dialogs40:
                for _t40 in treeviews(_root40):
                    for _col40, _anch40 in off_center(_t40):
                        _bad40.append(f"{_where40}:{_col40}={_anch40}")
            check("每一列(含 #0 树列)的单元格和表头都居中", _bad40, [])
        finally:
            if _cat40 is not None:
                _cat40.destroy()
        # 手搭的那几张表再点名查一次:它们不过 make_tree,最容易漏
        check("列管理窗口那张手搭的表(原来「列名」是左对齐的)也居中了",
              off_center(_dlg40.tree), [])
        _dlg40.destroy()
        app.update()
        app.nb.select(app.tab_loc)
        app.update()
        check("仓位页那张手搭的树(#0 原来是左对齐)也居中了",
              off_center(app.tab_loc.tree), [])
        # 只改对齐:列宽和「是否随窗口伸缩」必须原样。
        # 不伸缩的那几列宽度就是列定义里写的数;伸缩列(备注)会被 Tk 拿去填满
        # 剩下的宽度,所以它只要求「不小于」定义值 —— 拿它跟定义值比是比错了东西。
        check("不伸缩的列,宽度没被顺手改掉(#27 只动对齐)",
              [int(app.tab_move.tree.column(c[0], "width"))
               for c in gui.MovementsTab.COLS if not c[3:]],
              [c[2] for c in gui.MovementsTab.COLS if not c[3:]])
        check("伸缩列还是从定义宽度开始长大(备注)",
              int(app.tab_move.tree.column("note", "width")) >= 200, True)
        check("stretch 也没被顺手改掉(备注伸缩、数量不伸缩)",
              [bool(int(app.tab_move.tree.column(c, "stretch"))) for c in ("note", "qty")],
              [True, False])

        p("\n【41】流水按项目折叠(#30)")
        _t41 = app.tab_move
        # 【32】那一节把库里的项目全删了(流水的 project_id 被外键置空),于是到这里
        # 只剩一个「(不开项目)」组。只有一个组的话,「按项目分组」到底管不管用
        # 根本看不出来,「别的组没被带着一起展开」那种断言也会变成空的 ——
        # 先造两个项目、各自记一笔。
        _pid41a = API(server.create_project, body={"name": "折叠测试甲", "qty": 1})["id"]
        _pid41b = API(server.create_project, body={"name": "折叠测试乙", "qty": 1})["id"]
        API(server.stock_move, body={"kind": "IN", "component_id": made[0], "qty": 2,
                                     "project_id": _pid41a})
        API(server.stock_move, body={"kind": "IN", "component_id": made[1], "qty": 1,
                                     "project_id": _pid41b})
        app.nb.select(_t41)
        _t41.kind.set("全部")
        _t41.reload()
        app.update()
        _g41 = _t41.groups
        _names41 = [_n for _k, _g, _n, _i in _g41.groups]
        check("流水表按项目分了组(不是一张平铺的长表)",
              _g41.group_count() >= 3, True)
        check("一个项目一个组,组名就是项目名",
              [n for n in ("折叠测试甲", "折叠测试乙") if n in _names41],
              ["折叠测试甲", "折叠测试乙"])
        check("没挂项目的流水单独归成一组(日常补货/领用)",
              gui.NONE_GROUP in _names41, True)
        check("组行上写着项目名 + 该组笔数",
              all(str(_t41.tree.item(_g, "text")).endswith(" 笔)")
                  and _n in str(_t41.tree.item(_g, "text"))
                  for _k, _g, _n, _i in _g41.groups), True)
        check("默认全收着(用户原话:一直展开在那边太乱)",
              all(not _t41.tree.item(_g, "open") for _k, _g, _n, _i in _g41.groups), True)
        check("每一笔流水都挂在某个组下面,组行本身不是流水",
              all(_t41.tree.parent(i) for i in _g41.row_ids()), True)
        check("流水行都是纯数字 iid(撤销要拿它去撤销)",
              all(str(i).isdigit() for i in _g41.row_ids()), True)
        # 收着的时候组里的流水确实没显示出来(identify_row 是「屏幕上看得见什么」)
        _kept_rows41 = set(_g41.row_ids())
        _visible41 = {i for i in all_rows(_t41.tree)
                      if i in _kept_rows41 and _t41.tree.bbox(i)}
        check("收着的组里那些流水行不在屏幕上", _visible41, set())

        _k41, _gid41, _n41, _ids41 = _g41.groups[0]
        _real_identify41 = _t41.tree.identify_row
        try:
            # 不靠真实几何:直接让 identify_row 说「你点的就是这一行」
            _t41.tree.identify_row = lambda _y: _gid41
            _ret41 = _g41._on_click(_Ev26(0))
        finally:
            _t41.tree.identify_row = _real_identify41
        check("点一下组行就展开,而且这次点击被吃掉了(免得 Tk 再切一次变成点了没点)",
              _ret41, "break")
        check("展开之后组里的流水就显示出来了", _t41.tree.item(_gid41, "open"), 1)
        check("箭头换成「已展开」的样子",
              str(_t41.tree.item(_gid41, "text")).startswith("▼"), True)
        # 折叠状态是记在页签上的,所以刷新/撤销不会把用户收好的组又弹开
        _t41.reload()
        app.update()
        check("刷新之后这一组还开着(折叠状态在当前会话里记住)",
              _t41.tree.item(_gid41, "open"), 1)
        check("别的组没被带着一起展开(有不止一个组,这条才不是空话)",
              [bool(_t41.tree.item(_g, "open"))
               for _k, _g, _n, _i in _g41.groups if _g != _gid41],
              [False] * (len(_g41.groups) - 1))
        check("这一条断言确实覆盖到了别的组", len(_g41.groups) >= 2, True)

        # 键盘空格 / 直接点组名前面那个小三角是 ttk 自己切的,我们收不到事件 ——
        # 直接改树上真实的 -open(等于 ttk 替用户切了)再刷新,这个状态也得留住,
        # 否则表现就是「我明明收起来了,一刷新又弹开」
        _other41 = [_g for _k, _g, _n, _i in _g41.groups if _g != _gid41][0]
        _t41.tree.item(_other41, open=1)
        _t41.reload()
        app.update()
        check("从 ttk 那条路展开的组,刷新之后同样是开着的",
              _t41.tree.item(_other41, "open"), 1)
        _t41.tree.item(_other41, open=0)
        _t41.reload()
        app.update()
        check("从 ttk 那条路收起来的组,刷新之后同样是收着的",
              _t41.tree.item(_other41, "open"), 0)

        _g41.toggle(_gid41)
        check("再点一下收起来", _t41.tree.item(_gid41, "open"), 0)

        # 撤销只针对具体某一笔流水:组行不能被当成流水去撤销(#30 的硬要求)
        _box41 = FakeBox()
        _voided41 = app_con.execute(
            "SELECT COUNT(*) FROM movement WHERE voided=1").fetchone()[0]
        gui.messagebox = _box41
        try:
            _t41.tree.selection_set(_gid41)
            _t41.undo()
            app.update()
            check("选中组行时撤销被挡住,并且说清了该怎么办",
                  bool(_box41.infos) and "分组" in _box41.infos[0][1], True)
            check("一笔流水都没被动到",
                  app_con.execute("SELECT COUNT(*) FROM movement WHERE voided=1"
                                  ).fetchone()[0], _voided41)
            check("也没弹「确定要撤销吗」那种确认框", _box41.asks, [])
        finally:
            gui.messagebox = real_box
        _mid41 = _g41.row_ids()[0]
        _t41.tree.selection_set(str(_mid41))
        _box41b = FakeBox()
        _box41b.answer = False
        gui.messagebox = _box41b
        try:
            _t41.undo()
            app.update()
            check("选中真的那一笔时,撤销照旧走到「先问一遍」",
                  len(_box41b.asks), 1)
        finally:
            gui.messagebox = real_box

        # 动作类型下拉这个筛选还得能用:筛完还是分着组的
        _t41.kind.set("入库")
        _t41.reload()
        app.update()
        # 「撤销」补的是反向流水,「入库」这一档里会混着「入库·撤销」——
        # 和上面那个 kinds_shown 一样,后缀要摘掉再比
        _kinds41 = {str(_t41.tree.item(i, "values")[
            gui.col_index(gui.MovementsTab.COLS, "kind")]).replace("(已撤销)", "")
            .replace("·撤销", "")
            for i in _g41.row_ids()}
        check("动作筛选还能用(筛完仍然是按项目分组的)",
              _kinds41, {"入库"})
        check("筛选之后组还在", _g41.group_count() > 0, True)
        _t41.kind.set("全部")
        _t41.reload()
        app.update()

        # 出入库页的入库流水 / 出库流水是同一类问题,一并处理掉了
        app.nb.select(app.tab_stock)
        app.update()
        _st41 = app.tab_stock
        _st41.set_action("IN")
        app.update()
        check("出入库页的入库流水也按项目折叠了(默认也收着)",
              _st41.groups.group_count() >= 2
              and all(not _st41.tree.item(_g, "open")
                      for _k, _g, _n, _i in _st41.groups.groups), True)
        check("出入库页的组行上也有项目名和笔数",
              all(" 笔)" in str(_st41.tree.item(_g, "text"))
                  for _k, _g, _n, _i in _st41.groups.groups), True)
        check("出入库页也把这两个项目分成了一组一组",
              [n for n in ("折叠测试甲", "折叠测试乙")
               if n in [_n for _k, _g, _n, _i in _st41.groups.groups]],
              ["折叠测试甲", "折叠测试乙"])
        check("出入库页也能列出全部流水行(收着也数得到)",
              len(_st41.groups.row_ids()) > 0, True)
        _gid41b = _st41.groups.groups[0][1]
        _st41.tree.selection_set(_gid41b)
        _box41c = FakeBox()
        gui.messagebox = _box41c
        try:
            _st41.undo()
            app.update()
            check("出入库页选中组行时撤销同样被挡住",
                  bool(_box41c.infos) and "分组" in _box41c.infos[0][1], True)
        finally:
            gui.messagebox = real_box
        _st41.set_action("IN")
        app.update()

        # ---------------------------------------------------------- #29 / #32
        p("\n【42】#32 品类是用户自己的:卡片只列库里真有的,每一个都删得掉,删了不回来")

        _t42 = app.tab_comp
        app.nb.select(_t42)
        app.update()
        # 这一节会自己造「配过色」的状态,而且**会写进数据库旁边的 ui_columns.json**
        # —— 开始之前先清干净,免得上一节(或上一次运行)留下的设置把期望值带偏。
        # 那正是「断言假通过 / 假失败」最常见的来源。
        _cfg42 = _t42._col_cfg_path()
        if _cfg42 and os.path.exists(_cfg42):
            os.remove(_cfg42)
        _t42._row_colors = {}
        _t42._want_cols = None
        _t42.zero_stock.set(True)
        app.refresh_all()
        app.update()

        # ---- 造 3 个自定义品类。首页卡片名单必须**恰好**等于库里真有的顶层品类
        #      (+ 真有没品类的料时才多出来的那张「未分类」):自己建的必须在,
        #      写死名单里但库里没有对应行的**不许**画出来。
        _mb42 = gui.messagebox          # 这一节用的「确认框」都拿它当还原点
        _mine42 = [API(server.create_category, body={"name": n})["id"]
                   for n in ("自检-自定义甲", "自检-自定义乙", "自检-自定义丙")]
        _t42.reload()
        app.update()
        _tops42, _want42, _uncat42 = want_cards(_t42, app.con)
        check("自己新建的 3 个品类都在首页卡片里",
              [n for n in _mine42 if _t42._cat_flat[n]["name"] not in _t42.cards], [])
        check("首页卡片名单 = 库里真有的顶层品类(+ 真有没品类的料时才多一张「未分类」)",
              sorted(_t42.cards), sorted(_want42))
        check("写死名单里但库里没有对应行的名字不再画成卡片(空卡片没了)",
              [c for c in gui.CATEGORY_ORDER
               if c not in set(_tops42) and c in _t42.cards], [])
        check("每张卡片都能在品类树里找到对应的一行(不然它就是删不掉的空卡片)",
              [c for c in _t42.cards
               if c != gui.UNCATEGORIZED and c not in set(_tops42)], [])

        # ---- 上面那条不能是句废话:必须**真的**有一个「写在标准名单里、库里却没有
        #      对应行」的名字。用户那个库里的「传感器」就是这么来的 —— 一张永远
        #      删不掉的空卡片。库里没有这种名字时,当场按真实入口删掉一个造出来。
        _ghost42 = next((c for c in gui.CATEGORY_ORDER if c not in set(_tops42)), None)
        if _ghost42 is None:
            _ghost42 = gui.CATEGORY_ORDER[0]
            _kill42 = next(n for n in _t42._cat_flat.values()
                           if n["parent_id"] is None and n["name"] == _ghost42)
            _box42g = FakeBox()
            gui.messagebox = _box42g
            try:
                _t42.delete_cat(_kill42)
                app.update()
            finally:
                gui.messagebox = _mb42
        check("造出了那个状态:名字写在标准名单里,库里真没有对应的一行",
              (_ghost42 in gui.CATEGORY_ORDER,
               _ghost42 in {n["name"] for n in API(server.list_categories)["flat"]}),
              (True, False))
        check("所以它没有卡片(以前这里是一张点「删除」也没用的空卡片)",
              _ghost42 in _t42.cards, False)
        # 上面可能真删了一个(删掉顶层会把它的料变成「没有品类」,那张「未分类」
        # 卡片跟着现身),名单要重新对一遍 —— 而且必须用**当下**的事实重算,
        # 不能拿删除前的快照减一减:那算出来的是「没删干净的错觉」。
        _tops42, _want42, _uncat42 = want_cards(_t42, app.con)
        check("重新对账:卡片名单 = 库里真有的顶层品类(+ 可能的「未分类」)",
              sorted(_t42.cards), sorted(_want42))
        p(f"  库里没有对应行的标准名单名字:{[c for c in gui.CATEGORY_ORDER if c not in set(_tops42)]}"
          f"(它们一张卡片都不画)")

        # ---- 卡片右键:有对应行的卡片必须给出「改名 / 删除」,而且**不许置灰**
        #      (用户说的「有东西删不掉」就是从置灰那两项来的)
        _menu42 = _t42._build_cat_menu(_tops42[0])
        _items42 = menu_labels(_menu42)
        _cmds42 = [l for l, _s in _items42]
        check("卡片菜单里有「删除…」", any(l.startswith("删除") for l in _cmds42), True)
        check("删除这一项不是置灰的(库里真有这一行,删得掉)",
              [s for l, s in _items42 if l.startswith("删除")], ["normal"])
        check("菜单里不再有「库里还没有这一级」那种防御话术",
              [l for l in _cmds42 if "库里还没有这一级" in l], [])
        check("菜单里也没有「恢复删掉过的标准大类」这个入口了(整套隐藏名单拆掉了)",
              [l for l in _cmds42 if "恢复" in l], [])

        # 真的弹一次。tk_popup 会真把菜单挂到屏幕上等人点,自检里点不了,所以只把
        # 它换掉,确认 _cat_menu 真的走到「弹」这一步(而不是从某个 return 回来)
        _pop42, _popped42 = tk.Menu.tk_popup, []
        _ev42 = type("_Ev42", (), {"x_root": 0, "y_root": 0})()
        tk.Menu.tk_popup = lambda self, x, y: _popped42.append((x, y))
        try:
            _t42._cat_menu(_tops42[0], _ev42)
        finally:
            tk.Menu.tk_popup = _pop42
        check("对着卡片右键,菜单真的弹出来了", len(_popped42), 1)

        # ================================================================ 未分类
        # 「未分类」必须**数据驱动**:库里真有多少"没有品类的料",它才在;一件都没有
        # 它就不在 —— 这就是「未分类也要能删干净」在界面上的落地(它不是一行品类,
        # 是一个算出来的入口)。老数据里可能真有一行叫「未分类」的品类(老版本删大
        # 类时自动建的),那它就是一行普通品类、一张普通卡片 —— 先按真实入口把它
        # 删掉,把这一节的状态清成"新后端的样子"。
        _t42._load_cat_tree()
        _legacy42 = next((n for n in _t42._cat_flat.values()
                          if n["parent_id"] is None
                          and (n["name"] or "").strip() == gui.UNCATEGORIZED), None)
        if _legacy42 is not None:
            _box42a = FakeBox()
            gui.messagebox = _box42a
            try:
                _t42.delete_cat(_legacy42)
                app.update()
            finally:
                gui.messagebox = _mb42
            check("老数据里那行「未分类」就是一行普通品类,一样删得掉",
                  gui.UNCATEGORIZED in {n["name"] for n in
                                        API(server.list_categories)["flat"]
                                        if n["parent_id"] is None}, False)

        # 造一颗**没有品类**的料。走的是用户那条真实的路:建一个大类、把料挂在它
        # 下面、入库,再把那个大类删掉 —— 后端会把料的 category_id 清空。不是我们
        # 直接去改库,所以验的是界面在那条路上的表现。
        _c42 = API(server.create_category, body={"name": "自检-待归类"})["id"]
        _p42 = API(server.create_component, body={
            "name": "自检-没有品类的料", "category_id": _c42,
            "value": "10k", "package": "0603"})["id"]
        API(server.stock_move, body={"kind": "IN", "component_id": _p42,
                                     "qty": 3, "location": "未分类"})
        _t42._load_cat_tree()
        _box42b = FakeBox()
        gui.messagebox = _box42b
        try:
            _t42.delete_cat(_t42._cat_flat[_c42])
            app.update()
        finally:
            gui.messagebox = _mb42
        check("顶层大类删掉之后,料一颗没少,只是「没有品类」了",
              API(server.get_component, match=(_p42,))["category_id"], None)
        check("首页这时出现「未分类」卡片(库里真有没有品类的料)",
              gui.UNCATEGORIZED in _t42.cards, True)

        # 卡片出现还不够 —— 必须**点得进去、列得出它**
        _t42.open_category(gui.UNCATEGORIZED)
        app.update()
        check("点「未分类」能进二级页面", _t42.view, "cat")
        check("标题写着「未分类」", _t42.cat_title.get(), gui.UNCATEGORIZED)
        check("「未分类」列表里列得出那颗没有品类的料",
              _p42 in [int(i) for i in _t42.tree.get_children()], True)
        # 刷新不许把用户从这一级踢回首页:它不在品类树上,路径恢复要认得它
        app.refresh_all()
        app.update()
        check("刷新之后还留在「未分类」这一级(没被踢回首页)", _t42.view, "cat")
        check("刷新之后那颗料还在列表里",
              _p42 in [int(i) for i in _t42.tree.get_children()], True)

        # 把这批料**全部**归到一个真实品类里 -> 卡片自己消失(= 未分类也能删干净)
        _c42t = API(server.create_category, body={"name": "自检-归类目标"})["id"]
        _loose42 = [it["id"] for it in API(server.list_components,
                                           query={"limit": "0"})["items"]
                    if _t42._is_uncat(it)]
        check("这一节造的那颗料确实属于「没有品类」的那批",
              _p42 in _loose42, True)
        _t42._move_ids_to(_loose42, _c42t, "自检-归类目标")
        _t42.reload()
        app.update()
        check("所有料都归了类之后,「未分类」卡片自己消失(它也能删干净)",
              gui.UNCATEGORIZED in _t42.cards, False)
        # 首页那张卡片的「有没有货」也得跟着对:它用的是同一次刷新里**新的**品类树
        # (拿上一次的旧树算,刚建的品类下面的料会被算进「未分类」,卡片数就错了)
        check("首页「自检-归类目标」这张卡片也数得到那颗料(卡片不是拿旧树算的)",
              _p42 in [it["id"] for it in _t42._cat_data.get("自检-归类目标", [])],
              True)
        _t42.open_category("自检-归类目标")
        app.update()
        check("归类之后那颗料出现在「自检-归类目标」这一级里(点得进去、列得出来)",
              _p42 in [int(i) for i in _t42.tree.get_children()], True)

        # ================================================================ 措辞
        # 删完给用户看的就是那一句,必须和新语义对上(后端返回的 to 为空串 = 没有
        # 上一级 = 删的是顶层,界面就是按这个信号分的):
        #   * 删子级 -> 「N 颗料挪到了上一级 X」;
        #   * 删顶层 -> 「N 颗料现在没有品类了,可以在「未分类」里重新归类」。
        _top42 = API(server.create_category, body={"name": "自检-措辞顶层"})["id"]
        _kid42 = API(server.create_category,
                     body={"name": "自检-措辞子级", "parent_id": _top42})["id"]
        _k42 = API(server.create_component, body={
            "name": "自检-措辞料", "category_id": _kid42, "value": "1k"})["id"]
        API(server.stock_move, body={"kind": "IN", "component_id": _k42, "qty": 2,
                                     "location": "未分类"})
        _t42._load_cat_tree()
        gui.messagebox = FakeBox()
        try:
            _t42.delete_cat(_t42._cat_flat[_kid42])
            app.update()
        finally:
            gui.messagebox = _mb42
        _st42 = app.status.get()
        check("删子级:反馈写明「N 颗料挪到了上一级 X」",
              "1 颗料挪到了上一级「自检-措辞顶层」" in _st42, True)
        check("删子级:不会说成「没有品类」(那是删顶层才有的落点)",
              "没有品类" in _st42, False)
        check("而且子级里那颗料真的挪到了上一级",
              API(server.get_component, match=(_k42,))["category_id"], _top42)

        _t42._load_cat_tree()
        gui.messagebox = FakeBox()
        try:
            _t42.delete_cat(_t42._cat_flat[_top42])
            app.update()
        finally:
            gui.messagebox = _mb42
        _st42b = app.status.get()
        check("删顶层:反馈写明「N 颗料现在没有品类了,可以在「未分类」里重新归类」",
              ("1 颗料现在没有品类了" in _st42b
               and f"「{gui.UNCATEGORIZED}」里重新归类" in _st42b), True)
        check("删顶层:不会再出现老措辞「挪到「未分类」」(后端已经不建那一行了)",
              "挪到「未分类」" in _st42b, False)
        check("料还在,只是没有品类了",
              API(server.get_component, match=(_k42,))["category_id"], None)

        # ================================================================ 删得掉
        # 每一张卡片都必须**真的删得掉**(用户原话:「库存主界面一定要能够被删掉」)。
        # 走真实的删除入口(delete_cat —— 卡片右键菜单里那一条用的就是它),一张一张
        # 删,每删一张都断言:卡片消失 + 库里那一行真没了。
        _t42._load_cat_tree()
        check("这时首页每张卡片都对应库里真有一行(「未分类」除外,它是算出来的)",
              sorted(c for c in _t42.cards if c != gui.UNCATEGORIZED),
              sorted(n["name"] for n in _t42._cat_flat.values()
                     if n["parent_id"] is None))
        # 收尾对账要用「循环开始前库里真有哪些顶层」,当场取一份。**不能**用上面
        # 别处取的旧快照:这一节前面又建又删了好几个品类,快照早就过期了。
        _tops42c = [n["name"] for n in API(server.list_categories)["flat"]
                    if n["parent_id"] is None]
        _gone42 = []
        for _i42 in range(200):                  # 上限只是防死循环,不是预期次数
            _t42._load_cat_tree()
            # 以**库**为准决定这一轮删谁:_load_cat_tree() 走的是 quiet=True,拉取
            # 失败时 _cat_flat 会被清空 —— 把「一次没拉到」读成「已经删干净了」,
            # 这一节后面的收尾断言就会全部假通过(实测那次偶发红就是这么露头的)。
            _rows42 = [n["name"] for n in API(server.list_categories)["flat"]
                       if n["parent_id"] is None]
            if not _rows42:
                break
            _nm42 = _rows42[0]
            _node42 = next((n for n in _t42._cat_flat.values()
                            if n["parent_id"] is None and n["name"] == _nm42), None)
            if _node42 is None:
                # 界面的品类树和库对不上。先**重拉一次**再下结论:_load_cat_tree()
                # 的 quiet=True 会把一次拉取失败吞成空树,空一次不等于库里没有这一行。
                _t42._load_cat_tree()
                _node42 = next((n for n in _t42._cat_flat.values()
                                if n["parent_id"] is None and n["name"] == _nm42), None)
            check(f"要删的「{_nm42}」在首页卡片名单里(有行才有卡片)",
                  _nm42 in _t42.cards, True)
            if _node42 is None:
                # 重拉之后还是没有:树和库真不一致。绝不能拿 None 去删:delete_cat
                # 会直接抛 AttributeError,整节崩掉、后面的收尾全不跑,测试数据全
                # 留在副本里。
                check(f"界面的品类树上也找得到「{_nm42}」这一行(树和库一致)", False, True)
                continue
            _box42c = FakeBox()
            gui.messagebox = _box42c
            try:
                _t42.delete_cat(_node42)
                app.update()
            finally:
                gui.messagebox = _mb42
            check(f"删「{_nm42}」之前问了一句", len(_box42c.asks) >= 1, True)
            # 删完把**卡片**也刷一遍再断言。delete_cat 末尾会 reload(),但它有几条
            # 提前 return 的路(后端拒了 / 没确认),那时卡片还停在删之前 —— 只刷
            # 品类树(_load_cat_tree)不刷卡片,断言看的就是半新状态。
            _t42.reload()
            app.update()
            check(f"删完「{_nm42}」,卡片立刻不在名单里", _nm42 in _t42.cards, False)
            check(f"删完「{_nm42}」,库里那一行也真没了",
                  _nm42 in {n["name"] for n in API(server.list_categories)["flat"]
                            if n["parent_id"] is None}, False)
            _gone42.append(_nm42)
        # 收尾对账一律问**当下**的事实,不拿循环前的快照做集合运算:循环中途删掉一个
        # 带子类的顶层,子类会被提升成新的顶层(名字变了、多出来的还要再删),名单每
        # 一轮都在动,「旧快照 - 删过的名字」减出来的是「没删干净的错觉」。
        _left42 = [n["name"] for n in API(server.list_categories)["flat"]
                   if n["parent_id"] is None]
        check("循环开始前库里确实有一批顶层品类(下面两条不是句废话)",
              len(_tops42c) > 0, True)
        check("库里原来那批顶层品类 + 自己建的,一张不剩全删掉了(不是只删得掉一张)",
              _left42, [])
        check("而且是**一个个**删过来的:循环前见到的每个顶层名字都真删过一遍",
              sorted(n for n in _tops42c if n not in set(_gone42)), [])
        check("品类删光之后,首页上不再有「品类卡片」这种东西",
              [c for c in _t42.cards if c != gui.UNCATEGORIZED], [])
        if gui.UNCATEGORIZED in _t42.cards:
            check("「未分类」卡片给不出「删除」菜单(它不是品类行,没得删)",
                  _t42._build_cat_menu(gui.UNCATEGORIZED), None)
            check("而且状态栏说清了它是怎么来的(不是点了没反应)",
                  gui.UNCATEGORIZED in app.status.get(), True)

        # ---- 「删了立刻又回来」的根因是后端一启动就按标准名单把缺失的品类行播种回去。
        #      这里**真的再跑一次初始化**(和软件启动是同一条路:App.__init__ 里就是
        #      db.init_db),再看那些被删掉的标准大类有没有自己长回来。
        _seeded42 = set(db.category_seed())
        # 哪些标准大类现在真没了 —— 直接拿「当下库里还剩什么」算,不走 _gone42 的
        # 账本(账本只在上面那条「一个个删过来」的断言里用,不做收尾判据)。
        _std42 = sorted(n for n in _seeded42 if n not in set(_left42))
        check("这一轮真的删掉了标准名单里的大类(不是只删了自定义品类,验了个寂寞)",
              len(_std42) > 0, True)
        db.init_db(app.con)
        app.refresh_all()
        app.update()
        check("再跑一次初始化(等于重启软件),被删掉的标准大类一个都没长回来",
              sorted(n for n in _std42 if n in _t42.cards), [])
        check("库里也没有被重新播种回来",
              sorted(n for n in _std42
                     if n in {x["name"] for x in API(server.list_categories)["flat"]}),
              [])
        _tops42b = {n["name"] for n in API(server.list_categories)["flat"]
                    if n["parent_id"] is None}
        check("初始化之后卡片名单还是「库里真有的顶层品类 + 可能的未分类」,写死的没回来",
              [c for c in _t42.cards
               if c != gui.UNCATEGORIZED and c not in _tops42b], [])

        # ---- 设置文件里不再有「删掉过的大类」这种东西;那个恢复入口也不存在了
        _t42._save_cols()
        with open(_t42._col_cfg_path(), encoding="utf-8") as _f42:
            _raw42 = _f42.read()
        check("ui_columns.json 里不再写 hidden_cats(整套机制拆掉了)",
              "hidden_cats" in _raw42, False)
        check("同一份文件里原来的键没被挤掉", "cols" in _raw42, True)
        check("实例上也没有 _hidden_cats / restore_hidden_cats 了(死代码清干净)",
              (hasattr(_t42, "_hidden_cats"),
               hasattr(_t42, "restore_hidden_cats")), (False, False))

        # ---- 「＋ 新增大类」建出来的品类必须立刻有卡片、而且立刻删得掉:用户要的是
        #      「绝对的自定义自由化」—— 他自己加的品类和内置的完全平起平坐。
        _ask42b = gui.ask_text
        gui.ask_text = lambda *a, **k: "自检-新增大类"
        try:
            _t42.add_root_cat()
            app.update()
        finally:
            gui.ask_text = _ask42b
        check("用「＋ 新增大类」建的品类立刻出现在首页卡片里",
              "自检-新增大类" in _t42.cards, True)
        check("它和别的品类一样能删(菜单里那一项不是置灰的)",
              [s for l, s in menu_labels(_t42._build_cat_menu("自检-新增大类"))
               if l.startswith("删除")], ["normal"])
        _t42._load_cat_tree()
        _new42 = next((n for n in _t42._cat_flat.values()
                       if n["parent_id"] is None and n["name"] == "自检-新增大类"), None)
        _box42d = FakeBox()
        gui.messagebox = _box42d
        try:
            _t42.delete_cat(_new42)
            app.update()
        finally:
            gui.messagebox = _mb42
        check("自己建的品类也删得掉,删完卡片立刻消失、库里也没了",
              ("自检-新增大类" in _t42.cards,
               "自检-新增大类" in {n["name"] for n in API(server.list_categories)["flat"]}),
              (False, False))

        p("\n【43】#28 库存表按行配色:能设、能记住、能清除,而且压过内置规则色")

        # 造两行「内置规则色会生效」的料:库存 5 / 4,安全线 50 -> 都是 low(黄底)
        _cat43 = API(server.create_category, body={"name": "行配色测试类"})["id"]
        _c43 = API(server.create_component, body={
            "name": "自检-黄底", "category_id": _cat43, "value": "1k",
            "package": "0603", "min_stock": 50})["id"]
        API(server.stock_move, body={"kind": "IN", "component_id": _c43, "qty": 5,
                                     "location": "未分类"})
        _c43b = API(server.create_component, body={
            "name": "自检-再一行", "category_id": _cat43, "value": "2k",
            "package": "0603", "min_stock": 50})["id"]
        API(server.stock_move, body={"kind": "IN", "component_id": _c43b, "qty": 4,
                                     "location": "未分类"})
        _t43 = app.tab_comp
        _t43.zero_stock.set(True)
        _t43.open_category("行配色测试类")
        app.update()
        _i43 = str(_c43)
        check("这一页有那两行", len(_t43.tree.get_children()), 2)
        check("内置规则色给这一行挂了 low(偏低黄底)",
              (_t43.tree.exists(_i43), "low" in _t43.tree.item(_i43, "tags")),
              (True, True))
        check("内置 low 的确是黄底(下面拿它当对照)",
              color_str(_t43.tree.tag_configure("low", "background")), "#fff6dd")

        _lbl43 = [l for l, _s in menu_labels(_t43.menu)]
        check("右键菜单里有「文字颜色…」/「背景色…」/「清除自定义行配色」",
              [l for l in ("文字颜色…", "背景色…", "清除自定义行配色") if l in _lbl43],
              ["文字颜色…", "背景色…", "清除自定义行配色"])
        check("而且和「入库 / 删除」它们并列在同一张菜单里",
              ("入库" in _lbl43 and "删除" in _lbl43), True)

        # ---- 文字色。取色对话框用标准库的 colorchooser,自检里换成假的
        _real43 = gui.colorchooser.askcolor
        gui.colorchooser.askcolor = lambda *a, **k: ((0, 0, 255), "#0000ff")
        try:
            _t43.tree.selection_set(_i43)
            _t43.pick_row_color("fg")
        finally:
            gui.colorchooser.askcolor = _real43
        _tags43 = _t43.tree.item(_i43, "tags")
        check("只设文字色:这一行挂上了自己的配色 tag",
              any(str(t).startswith("u") for t in _tags43), True)
        check("文字色真的配在那一个 tag 上",
              color_str(_t43.tree.tag_configure("u" + _i43, "foreground")), "#0000ff")
        check("只设文字色时**不碰背景**:内置的 low 还挂在这一行上",
              "low" in _tags43, True)
        check("那个自定义 tag 没配背景色(空串 = 不设置这一项)",
              color_str(_t43.tree.tag_configure("u" + _i43, "background")), "")

        # ---- 背景色:必须压过内置规则色
        # #34 之后上色会**取消选中**(不然蓝色高亮把整行颜色盖住,用户以为没生效),
        # 所以每设一次都得重新选一次 —— 这也正是那个取舍的另一半,自检里照实走。
        gui.colorchooser.askcolor = lambda *a, **k: ((255, 255, 0), "#ffff00")
        try:
            _t43.tree.selection_set(_i43)
            _t43.pick_row_color("bg")
        finally:
            gui.colorchooser.askcolor = _real43
        check("设了背景色之后内置的 low 让位(两个 tag 抢同一个选项只会时红时黄)",
              "low" in _t43.tree.item(_i43, "tags"), False)
        check("这一行实际生效的背景色 = 用户配的那个(压过了内置黄底)",
              effective_bg(_t43.tree, _i43), "#ffff00")
        check("没配过的行还是内置的黄底(规则色没被一起改掉)",
              effective_bg(_t43.tree, str(_c43b)), "#fff6dd")

        # ---- 多选一起设:和 Excel 里选一片一起刷是一个意思
        gui.colorchooser.askcolor = lambda *a, **k: ((255, 0, 0), "#ff0000")
        try:
            _t43.tree.selection_set((_i43, str(_c43b)))
            _t43.pick_row_color("fg")
        finally:
            gui.colorchooser.askcolor = _real43
        check("选了两行就两行一起设",
              [color_str(_t43.tree.tag_configure("u" + str(_x), "foreground"))
               for _x in (_c43, _c43b)], ["#ff0000", "#ff0000"])

        # ---- 取色对话框点取消:什么都不改(不能取消了还刷上色)
        # 这里必须**先选行**:上一句多选上色已经把那两行的选中取消了,不选就
        # 走不到取色那一步(变成「没选行」的早退),这条断言会**假通过** ——
        # 验的已经不是「点取消」这件事了。
        _t43.tree.selection_set(_i43)
        _before43 = {k: dict(v) for k, v in _t43._row_colors.items()}
        gui.colorchooser.askcolor = lambda *a, **k: (None, None)
        try:
            _t43.pick_row_color("bg")
        finally:
            gui.colorchooser.askcolor = _real43
        check("点取消,配色一点没动", _t43._row_colors, _before43)

        # ---- 一行都没选:给提示,不静默
        _t43.tree.selection_remove(*_t43.tree.selection())
        _t43.pick_row_color("fg")
        check("一行都没选时给一句提示(不是点了没反应)",
              "选中" in app.status.get(), True)

        # ---- 持久化:重开页面 = 重开软件,颜色还在
        with open(_t43._col_cfg_path(), encoding="utf-8") as _f43:
            _raw43 = _f43.read()
        check("行配色写进了数据库旁边的 ui_columns.json",
              ("row_colors" in _raw43 and "#ffff00" in _raw43), True)
        _t43b = gui.ComponentsTab(app.nb, app)
        _t43b.reload()
        _t43b.open_category("行配色测试类")
        app.update()
        check("重开之后这一行的背景色还是用户配的",
              effective_bg(_t43b.tree, _i43), "#ffff00")
        check("键用的是元件 id,所以换页面 / 换筛选还是同一行",
              (_t43b._row_colors.get(_i43) or {}).get("bg"), "#ffff00")
        _t43b.destroy()
        app.update()

        # ---- 清除:回到默认,内置规则色跟着回来
        _t43.tree.selection_set(_i43)
        _t43.clear_row_colors()
        app.update()
        check("清除之后这一行不再挂自定义 tag",
              [str(t) for t in _t43.tree.item(_i43, "tags")
               if str(t).startswith("u")], [])
        check("清除之后内置的 low 回来了(规则色照旧)",
              effective_bg(_t43.tree, _i43), "#fff6dd")
        check("保存的数据里也删掉了",
              _i43 in _t43._row_colors, False)

        # ---- 元件行右键菜单里的「删除」:点了要真删掉,而且有交代
        _tmp43 = API(server.create_component, body={
            "name": "自检-右键删除", "category_id": _cat43, "value": "3k",
            "package": "0603"})["id"]
        API(server.stock_move, body={"kind": "IN", "component_id": _tmp43, "qty": 2,
                                     "location": "未分类"})
        _t43.reload()
        app.update()
        _i43c = str(_tmp43)
        check("新料出现在这一页(下面才点得到它)", _t43.tree.exists(_i43c), True)
        _box43 = FakeBox()
        _mb43 = gui.messagebox
        gui.messagebox = _box43
        try:
            _t43.tree.selection_set(_i43c)
            # 走的是菜单上那一条,不是直接调方法 —— 入口没接上也要能查出来
            _t43.menu.invoke(_t43.menu.index("删除"))
            app.update()
        finally:
            gui.messagebox = _mb43
        check("右键菜单里点「删除」会先问一句", len(_box43.asks) >= 1, True)
        check("元件真的没了",
              int(app_con.execute("SELECT COUNT(*) FROM component WHERE id=?",
                                  (_tmp43,)).fetchone()[0]), 0)
        check("而且状态栏说了删掉的是哪一颗",
              "自检-右键删除" in app.status.get(), True)

        # ---- 收尾:这一节造的状态不能留给下一次运行。
        # ui_columns.json 就在数据库旁边,下一次运行**会真的读它** —— 留着
        # 「某一行是黄底」的话,下一轮自检里行 tag 的期望值就全变了,那种失败最难查。
        # (「删掉过的标准大类」那套机制已经整个拆掉了,这里不再有它的收尾。)
        _t43._row_colors.clear()
        _t43._want_cols = None
        _t43.zero_stock.set(True)
        _t43._save_cols()
        with open(_t43._col_cfg_path(), encoding="utf-8") as _f43b:
            _raw43b = _f43b.read()
        check("收尾:行配色和「删掉过的大类」都没留在设置文件里(下一轮自检不受影响)",
              ("hidden_cats" not in _raw43b and "#ffff00" not in _raw43b
               and "#ff0000" not in _raw43b), True)

        p("\n【44】#33 在库存表的**列标题**上直接按住拖 = 换列(不用开「列…」窗口)")

        # 用户原话:「拖动你只是改了**列管理里面**的拖动,我需要你在**元件显示栏**
        # 那里加入拖动交换」。所以这一节验的是:焦点在**表格自己的表头**上,一次
        # 「按下 → 横向拖 → 松手」就换列,全程不碰「列…」那个窗口。
        _t44 = app.tab_comp
        _t44.zero_stock.set(True)
        _t44._want_cols = ["name", "on_hand", "lcsc_pn"]
        _t44.open_category("行配色测试类")      # 让 _fill 按上面这份列设置把行摆好
        app.update()
        _seq44 = ["name", "on_hand", "lcsc_pn"]
        check("这一页按列设置摆好了三列(下面拖的就是它)",
              [str(c) for c in _t44.tree["displaycolumns"]], _seq44)
        check("表里有两行,落点才量得出来", len(_t44.tree.get_children()) >= 2, True)

        check("三个拖动事件都接在库存表上(不是只接了「列…」里那张)",
              [bool(_t44.tree.bind(e)) for e in ("<ButtonPress-1>", "<B1-Motion>",
                                                 "<ButtonRelease-1>")],
              [True, True, True])
        check("落点提示线是这张表上的独立小控件,平时不出现",
              bool(_t44.hdr_line.place_info()), False)

        class _Ev44:
            """表头拖动那几个方法只认 event.x / event.y(顺手真问一次
            identify_region)。event_generate 造出来的事件也得先等窗口映射出来才
            量得准坐标,自检会时好时坏 —— 和 #26 那边一样,给个够用的假事件,
            坐标从 bbox 现算,就是人眼看到的那几个像素。"""

            def __init__(self, x, y):
                self.x, self.y = x, y

        def _hdr_y(tab):
            """表头那一行里一个稳妥的 y。表头高约 25,第一行数据从 25 起。"""
            kids = tab.tree.get_children()
            bb = tab.tree.bbox(kids[0]) if kids else None
            top = bb[1] if bb else 25
            return max(2, min(top // 2, top - 2))

        def _col_x(tab, i):
            """第 i 个显示列(= #(i+1))横向中间那一点的 x。"""
            bb = tab.tree.bbox(tab.tree.get_children()[0], "#%d" % (i + 1))
            return bb[0] + bb[2] // 2

        _y44 = _hdr_y(_t44)
        check("下面按的那个 y 真的落在表头区域里(不然验的就是行,不是标题)",
              _t44.tree.identify_region(_col_x(_t44, 0), _y44), "heading")

        # ---- 把第三列(lcsc_pn)拖到最前面:一次「按下 → 拖 → 松手」
        _t44._hdr_drag_begin(_Ev44(_col_x(_t44, 2), _y44))
        check("按在标题上:认下了这次拖动,拖的是第三列", _t44._hdr_src, 2)
        _t44._hdr_drag_motion(_Ev44(1, _y44))       # 拖到最左边那条边界上
        check("拖到最左边:记下了会插到第 0 格", _t44._hdr_slot, 0)
        check("拖动过程中那条落点提示线真的画出来了",
              bool(_t44.hdr_line.place_info()), True)
        check("提示线立在第 0 格的边界上(不是随便画在哪儿)",
              int(_t44.hdr_line.place_info()["x"]) <= 1, True)
        _t44._hdr_drag_end()
        check("松手之后提示线收起来", bool(_t44.hdr_line.place_info()), False)
        _new44 = ["lcsc_pn", "name", "on_hand"]
        check("列顺序真的换了(看屏幕上摆的那几个,不是只看内部变量)",
              [str(c) for c in _t44.tree["displaycolumns"]], _new44)
        check("列设置那份清单(顺序的唯一出处)也跟着换了", _t44._want_cols, _new44)

        # ---- 存盘:和「列…」共用 ui_columns.json 里的 cols
        _cfg44 = _t44._col_cfg_path()
        with open(_cfg44, encoding="utf-8") as _f44:
            _raw44 = _f44.read()
        check("ui_columns.json 里的 cols 顺序和屏幕上一致",
              list(db.parse_params(_raw44).get("cols") or []), _new44)

        # ---- 重开一个列表页(等于重开软件:tabs 都是新建的,__init__ 里会读文件)
        _t44b = gui.ComponentsTab(app.nb, app)
        check("重开之后读回来的列设置就是拖后的顺序", _t44b._want_cols, _new44)
        _t44b.reload()
        _t44b.open_category("行配色测试类")
        app.update()
        check("重开之后屏幕上摆的还是这个顺序",
              [str(c) for c in _t44b.tree["displaycolumns"]], _new44)
        _t44b.destroy()
        app.update()

        # ---- 边界 1:在标题上按一下、原地松手,什么都不该改
        _t44._hdr_drag_begin(_Ev44(_col_x(_t44, 0), _y44))
        _t44._hdr_drag_end()
        check("标题上单击(不拖):列顺序一点没动",
              [str(c) for c in _t44.tree["displaycolumns"]], _new44)
        check("而且没留下「正在拖」的状态",
              (_t44._hdr_src, _t44._hdr_slot), (None, None))
        check("单击不拖的时候也不画提示线",
              bool(_t44.hdr_line.place_info()), False)

        # ---- 边界 2:在**行区域**按下并横向拖,不该换列
        _rowbb44 = _t44.tree.bbox(_t44.tree.get_children()[0])
        _cell44 = (_rowbb44[0] + 5, _rowbb44[1] + 5)
        check("下面按的那一点真的在行区域里",
              _t44.tree.identify_region(*_cell44) in ("cell", "tree"), True)
        _t44._hdr_drag_begin(_Ev44(*_cell44))
        check("在行上按下:不认领这次拖动(状态没被记下)", _t44._hdr_src, None)
        _t44._hdr_drag_motion(_Ev44(1, _cell44[1]))
        _t44._hdr_drag_end()
        check("在行上横向拖也不换列",
              [str(c) for c in _t44.tree["displaycolumns"]], _new44)
        check("行区域拖动不会冒出提示线",
              bool(_t44.hdr_line.place_info()), False)

        # ---- 边界 3:普通行点击 / 双击 / 右键不能被这套拖动弄坏。
        # 这里用真事件走一遍点击和右键(证明确实是 Tk 自己那套在干活),但**不**
        # 合成真的双击:双击会走 edit() -> ComponentDialog -> wait_window,那是
        # 一个模态等待循环,自检会卡在那儿等人点(用户已经为弹框抱怨过三次)。
        # 所以双击只直接调那一个回调,并临时把 edit 换成一个记录器 —— 既不弹窗,
        # 又确实验到了「双击一行会走到编辑」。
        _rows44 = _t44.tree.get_children()
        _t44.tree.selection_remove(*_t44.tree.selection())
        _t44.tree.event_generate("<ButtonPress-1>", x=_cell44[0], y=_cell44[1])
        _t44.tree.event_generate("<ButtonRelease-1>", x=_cell44[0], y=_cell44[1])
        app.update()
        check("普通行点击照样能选中(事件没被表头那套吞掉)",
              list(_t44.tree.selection()), [_rows44[0]])

        _hits44 = []
        _edit44 = _t44.edit
        _t44.edit = lambda: _hits44.append(1)
        try:
            _t44._on_double(_Ev44(_cell44[0], _cell44[1]))
        finally:
            _t44.edit = _edit44
        check("双击一行仍然进编辑(这条绑定没被挡掉)", _hits44, [1])

        class _Menu44:
            """假的右键菜单:只记下 tk_popup 被调过,**绝不真弹一个菜单到桌面上**。"""

            def __init__(self):
                self.popped = []

            def tk_popup(self, x, y):
                self.popped.append((x, y))

        _menu44 = _t44.menu
        _fake44 = _Menu44()
        _t44.menu = _fake44
        try:
            _bb44b = _t44.tree.bbox(_rows44[1])
            _t44.tree.event_generate("<Button-3>", x=_bb44b[0] + 5, y=_bb44b[1] + 5)
            app.update()
        finally:
            _t44.menu = _menu44
        check("在行上按右键仍然会弹菜单(换成了假菜单,真菜单不会弹到桌面上)",
              len(_fake44.popped), 1)
        check("右键那一下还是会把行选上(原来的行为)", list(_t44.tree.selection()),
              [_rows44[1]])
        _lbl44 = [l for l, _s in menu_labels(_t44.menu)]
        check("右键菜单本身照样构建得出来(条目还在)",
              ("入库" in _lbl44 and "删除" in _lbl44), True)

        # ---- 属性列也在能拖的范围里,而且换位之后**标题和值要一起走**。
        # 属性列是按顺序占 a1..a12 槽的:只改 displaycolumns 的话,「耐压」的标题
        # 底下会摆着「精度」的值(两列都是 a1/a2,看键名根本看不出来串了)——
        # 这正是拖完必须 reload 重插一遍行、而不是只 configure 的原因。
        _c44p = API(server.create_component, body={
            "name": "自检-换列属性", "category_id": _cat43, "value": "4k7",
            "package": "0603", "params": {"耐压": "50V", "精度": "±5%"}})["id"]
        API(server.stock_move, body={"kind": "IN", "component_id": _c44p, "qty": 7,
                                     "location": "未分类"})
        _t44._want_cols = ["name", "@耐压", "@精度"]
        _t44.open_category("行配色测试类")      # _fill 按上面这份列设置把行摆好
        app.update()
        _row44p = str(_c44p)

        def _headed(tab, iid):
            """{表头文字: 这一行在这一格的值}。

            按**表头**取,不按列键取 —— 属性列换了位之后键名还是 a1/a2,
            只有表头文字能把「哪一列是什么」说清楚。
            """
            out = {}
            for key in [str(c) for c in tab.tree["displaycolumns"]]:
                out[str(tab.tree.heading(key, "text"))] = tab.tree.set(iid, key)
            return out

        _before44p = _headed(_t44, _row44p)
        check("换列之前:「耐压」表头下是 50V、「精度」下是 ±5%",
              (_before44p.get("耐压"), _before44p.get("精度")), ("50V", "±5%"))

        _spans44 = _t44._hdr_bounds()[1]
        _t44._hdr_drag_begin(_Ev44(_col_x(_t44, 1), _y44))      # 第 2 列 = 耐压
        _t44._hdr_drag_motion(_Ev44(_spans44[-1][1] + 30, _y44))  # 拖到最后一列右边
        check("往右拖:记下了会插到最后一格(身后那位要往前挪一格,不然少走一位)",
              _t44._hdr_slot, 3)
        _t44._hdr_drag_end()
        _heads44 = [str(_t44.tree.heading(k, "text"))
                    for k in [str(c) for c in _t44.tree["displaycolumns"]]]
        check("往右拖:屏幕上表头的顺序真的变了", _heads44, ["名称", "精度", "耐压"])
        check("列设置清单也对上了(耐压挪到了精度后面)",
              _t44._want_cols, ["name", "@精度", "@耐压"])
        _after44p = _headed(_t44, _row44p)
        check("换位之后「耐压」表头下还是 50V(值和标题一起走,没串到别的槽上)",
              (_after44p.get("耐压"), _after44p.get("精度")), ("50V", "±5%"))

        # ---- 再用**真事件**从 Tk 那条路上完整走一遍:按下表头 → 横向拖 → 松手。
        # 上面几次是直接调回调(坐标是真的,identify_region 也是真的),但「事件到底
        # 送不送得到那几个绑定上」没验 —— 这一段就把最后这一环补上。
        _t44.tree.event_generate("<ButtonPress-1>", x=_col_x(_t44, 2), y=_y44)
        _t44.tree.event_generate("<B1-Motion>", x=2, y=_y44)
        check("真事件:按下表头再横向拖,提示线真的出来了",
              bool(_t44.hdr_line.place_info()), True)
        _t44.tree.event_generate("<ButtonRelease-1>", x=2, y=_y44)
        app.update()
        _heads44b = [str(_t44.tree.heading(k, "text"))
                     for k in [str(c) for c in _t44.tree["displaycolumns"]]]
        check("真事件走一遍:松手就换了列(拖到最左边)", _heads44b,
              ["耐压", "名称", "精度"])
        check("真事件那条路也存了盘", _t44._want_cols, ["@耐压", "name", "@精度"])

        # ---- 「列…」窗口里那套拖动和 ↑↓ 必须留着(两条路都要能用)
        _dlg44 = gui.ColumnPickDialog(_t44, ["name", "on_hand"], _t44._attr_pool(),
                                      _t44._base_labels(), _t44._default_cols([]))
        app.update()
        check("列管理窗里的 ↑ ↓ 按钮还在(#33 没把它顶掉)",
              [t for t in buttons_of(_dlg44) if t.startswith(("↑", "↓"))],
              ["↑ 上移", "↓ 下移"])
        _dlg44.items = ["A", "B", "C"]
        _dlg44._refresh()
        app.update()
        _rowdlg44 = _dlg44.tree.bbox("0")
        _ydlg44 = (_rowdlg44[1] + 3) if _rowdlg44 else 28

        class _EvDlg44:
            def __init__(self, y):
                self.y = y

        _dlg44._drag_begin(_EvDlg44(_ydlg44))
        _dlg44._drag_motion(_EvDlg44(_ydlg44 + 40))
        _dlg44._drag_end()
        check("列管理窗里的拖动也照旧能调顺序", _dlg44.items, ["B", "A", "C"])
        _dlg44.destroy()

        # ---- 收尾:这一节改过列顺序,而 ui_columns.json 就在数据库旁边,下一轮
        # 自检启动时**会真的读它** —— 留着「lcsc_pn 排在第一位」的话,下一轮所有
        # 跟列序、列标题有关的期望值都会跟着变,那种失败最难查。
        _t44._want_cols = None
        _t44.zero_stock.set(True)
        _t44._save_cols()
        with open(_t44._col_cfg_path(), encoding="utf-8") as _f44b:
            _raw44b = _f44b.read()
        check("收尾:列顺序没留在设置文件里(下一轮自检不受影响)",
              db.parse_params(_raw44b).get("cols"), None)

        p("\n【45】#34 表格上方那条行调色盘:点色块就上色、多选一起改、改完立刻看得见")

        # 用户原话(第二次说得很明确了):「表格颜色改成和 excel 那种差不多的,选中
        # 一行上面添加调色盘之类的进行颜色修改」。这一节验的就是这个形态:表格上方
        # 一排色块,点一下给选中的行上色,**全程不开系统调色板**;顺带把 _popup
        # 那个「右键把多选顶掉」的老 bug 一起钉住。
        _t45 = app.tab_comp
        _t45.zero_stock.set(True)
        _t45.open_category("行配色测试类")
        app.update()
        _i45, _i45b, _i45c = str(_c43), str(_c43b), str(_c44p)
        check("这一节要用的三行都在表里",
              all(_t45.tree.exists(x) for x in (_i45, _i45b, _i45c)), True)

        # ---- 工具条上真的有那两个色块按钮 + 清除(这一排是表格正上方那一条)
        check("表格上方那排有「字体颜色 / 填充颜色」两个色块按钮和「清除配色」",
              buttons_of(_t45.pal_bar), ["字体颜色", "填充颜色", "清除配色"])
        check("色板默认是收着的(不白占表格的地方)", _t45.pal_panel.winfo_manager(), "")

        # ---- 点「字体颜色」:色板摊在**这一页里**,不是弹窗
        _t45.btn_fg.invoke()
        app.update()
        check("点一下「字体颜色」就摊开一排色块(不用弹模态框)",
              len(_t45.pal_swatches), len(_t45.PALETTE))
        check("色板是摊在这一页里的,不是弹出去的小窗",
              str(_t45.pal_panel.winfo_manager()), "pack")
        check("末尾有「更多颜色…」兜底(走系统取色器)+「收起」",
              [t for t in buttons_of(_t45.pal_panel) if t], ["更多颜色…", "收起"])
        check("每一格色块的颜色就取 PALETTE 那一份(不再另写一处)",
              [color_str(_t45.pal_swatches[c].cget("bg")) for c in _t45.PALETTE],
              [color_str(c) for c in _t45.PALETTE])

        # ---- 用工具条给选中行上色,而且**不碰系统取色器**
        # 这里把 askcolor 换成一个「被调用就记一笔、然后返回取消」的壳:点色块本来
        # 就不该走它。留真货在那儿的话,真调起来会把系统调色板弹到用户桌面上。
        _real45 = gui.colorchooser.askcolor
        _ask45 = []
        gui.colorchooser.askcolor = lambda *a, **k: (_ask45.append(1), (None, None))[1]
        try:
            _t45.tree.selection_set(_i45)
            app.update()
            check("选中这一行:两个色块按钮显示的是这一行的现状(还没配过 = 默认底)",
                  (color_str(_t45.btn_fg.cget("bg")), color_str(_t45.btn_bg.cget("bg"))),
                  (color_str(_t45._btn_face), color_str(_t45._btn_face)))
            check("状态说明也写清了「这一行还没配过色」",
                  "默认" in _t45.pal_now.get(), True)

            _t45.btn_bg.invoke()                    # 换成「填充颜色」的色板
            app.update()
            _t45.pal_swatches["#ffe3e3"].invoke()   # 点其中一格
            app.update()
        finally:
            gui.colorchooser.askcolor = _real45
        check("点色块上色,全程没打开系统取色器(要的就是这个)", _ask45, [])
        check("工具条上色走的还是同一套路径:这一行挂上了自己的配色 tag",
              any(str(t).startswith("u") for t in _t45.tree.item(_i45, "tags")), True)
        check("屏幕上这一行的填充色真的变成刚点的那一格",
              effective_bg(_t45.tree, _i45), "#ffe3e3")
        check("也落盘了(ui_columns.json 就在数据库旁边)",
              "#ffe3e3" in open(_t45._col_cfg_path(), encoding="utf-8").read(), True)

        # ---- 改完立刻看得见(#34 的取舍就摆在这儿)
        check("上完色这一行的选中被取消了(不然看到的还是高亮蓝,像素实测过)",
              list(_t45.tree.selection()), [])
        check("设过填充色的行不再挂内置 low(用户设的压过规则色)",
              "low" in _t45.tree.item(_i45, "tags"), False)
        check("没动过的行内置黄底还在(规则色没被连坐)",
              effective_bg(_t45.tree, _i45b), "#fff6dd")
        _t45.tree.selection_set(_i45)
        app.update()
        check("再选中这一行,「填充颜色」按钮的底色就是这一行的色(跟着选中行走)",
              color_str(_t45.btn_bg.cget("bg")), "#ffe3e3")
        check("状态说明里也写明了当前这一行的配色",
              "#ffe3e3" in _t45.pal_now.get(), True)

        # ---- 多选一起改(#34 查出来的真 bug:_popup 一进来就 selection_set(row),
        # 把 Ctrl/Shift 选的其他行整个顶掉 —— 说明书里承诺的「多选一起设」用鼠标
        # 根本走不通。上一轮自检是直接调 pick_row_color 绕过了 _popup,所以没查出来)
        _t45.tree.selection_set((_i45, _i45b, _i45c))
        app.update()

        class _Menu45:
            """假的右键菜单:只记下 tk_popup 被调过,**绝不真弹一个菜单到桌面上**。"""

            def __init__(self):
                self.popped = []

            def tk_popup(self, x, y):
                self.popped.append((x, y))

        class _Ev45:
            """右键事件:只需要 _popup 用到的 y(行坐标)。"""

            def __init__(self, y):
                self.y = y
                self.x_root = 0
                self.y_root = 0

        _bb45 = _t45.tree.bbox(_i45b)
        _y45row = (_bb45[1] + 3) if _bb45 else 26
        _menu45 = _t45.menu
        _fake45 = _Menu45()
        _t45.menu = _fake45
        try:
            _t45._popup(_Ev45(_y45row))
            app.update()
        finally:
            _t45.menu = _menu45
        check("对着其中一行右键,菜单照样弹(假菜单记到了)", len(_fake45.popped), 1)
        check("右键的那一行**本来就在选中集合里** → 三行的选中一个都没被顶掉",
              sorted(_t45.tree.selection()), sorted((_i45, _i45b, _i45c)))

        _t45.btn_fg.invoke()
        app.update()
        _t45.pal_swatches["#1f618d"].invoke()
        app.update()
        check("多选三行,点一格色块三行一起上字色",
              [color_str(_t45.tree.tag_configure("u" + str(_x), "foreground"))
               for _x in (_c43, _c43b, _c44p)], ["#1f618d", "#1f618d", "#1f618d"])
        check("多选的这三行也一起取消选中(每一行都立刻看得见)",
              list(_t45.tree.selection()), [])
        check("只设了字体色 → 填充照旧是内置的规则色(内置黄底没丢)",
              effective_bg(_t45.tree, _i45b), "#fff6dd")

        # ---- 右键一行**不在**选中集合里的行:照旧切到它(老行为不能丢)
        _t45.tree.selection_set(_i45)
        app.update()
        _bb45b = _t45.tree.bbox(_i45b)
        _y45rowb = (_bb45b[1] + 3) if _bb45b else 26
        _menu45 = _t45.menu
        _fake45b = _Menu45()
        _t45.menu = _fake45b
        try:
            _t45._popup(_Ev45(_y45rowb))
            app.update()
        finally:
            _t45.menu = _menu45
        check("右键一行不在选中集合里的行,照旧切到它(旧行为还在)",
              list(_t45.tree.selection()), [_i45b])

        # ---- 清除:回内置规则色
        _t45.tree.selection_set((_i45, _i45b, _i45c))
        app.update()
        _t45.btn_clear.invoke()
        app.update()
        check("「清除配色」把三行的自定义色清掉了",
              [_x in _t45._row_colors for _x in (_i45, _i45b, _i45c)],
              [False, False, False])
        check("清除之后内置的偏低黄底回来了(第三行没填安全库存,本来就没有规则色)",
              [effective_bg(_t45.tree, _x) for _x in (_i45, _i45b, _i45c)],
              ["#fff6dd", "#fff6dd", ""])
        check("清除之后也取消选中(不然规则色同样被高亮盖着)",
              list(_t45.tree.selection()), [])

        # ---- 右键菜单那三项**保留**了(工具条是主入口,右键退成第二个入口);
        #      留就必须和工具条表现一致 —— 所以这里走菜单里那一条真点一遍
        _lbl45 = [l for l, _s in menu_labels(_t45.menu)]
        check("右键菜单里那三项还在",
              [l for l in ("文字颜色…", "背景色…", "清除自定义行配色") if l in _lbl45],
              ["文字颜色…", "背景色…", "清除自定义行配色"])
        check("它们已经不在菜单最末尾了(原来吊在第 10/11/12 项上,够不着)",
              _lbl45.index("文字颜色…") < _lbl45.index("删除"), True)
        _idx45 = next(i for i in range(_t45.menu.index("end") + 1)
                      if str(_t45.menu.type(i)) == "command"
                      and str(_t45.menu.entrycget(i, "label")) == "背景色…")
        _t45.tree.selection_set(_i45)
        app.update()
        gui.colorchooser.askcolor = lambda *a, **k: ((0, 128, 0), "#008000")
        try:
            _t45.menu.invoke(_idx45)        # 走菜单里那一条,不是直接调方法
            app.update()
        finally:
            gui.colorchooser.askcolor = _real45
        check("右键那一条和工具条**表现一致**:一样上色、一样取消选中",
              (effective_bg(_t45.tree, _i45), list(_t45.tree.selection())),
              ("#008000", []))

        # ---- 把两条「能力边界」在真表上钉住(#34 要求把说法改对,不是改完就算)
        _col45 = ""
        try:
            _t45.tree.column("name", background="#ffffff")
            _t45.tree.column("name", background="")     # 万一真被接受,也立刻收回
        except tk.TclError as exc:
            _col45 = str(exc)
        check("ttk 连**整列**都不支持上色(column 没有 background 这个选项)",
              ("background" in _col45 and "unknown option" in _col45), True)
        # effective_bg 是按「行上 tags 列表里第一个配了背景色的 tag」取的。这条口径
        # **只有在「一行最多只有一个 tag 配背景色」时才等于屏幕真刷出来的那个颜色**:
        # 谁赢由 Tk 说了算,而实测不是 tags 列表的先后(6 种创建顺序 + 把抢色的 tag
        # 换成第 4 个,规则都是「谁先 tag_configure 谁赢」)。所以要钉的是这个前提,
        # 不是假装能算出 Tk 的选择。
        _bg_tags45 = {}
        for _x in (_i45, _i45b, _i45c):
            _bg_tags45[_x] = len([t for t in _t45.tree.item(_x, "tags")
                                  if str(_t45.tree.tag_configure(str(t), "background"))])
        # 两个都要看:没有一个行超过 1(前提成立),而且真有一行是 1
        # (不然这条断言就成了「全是 0 也通过」的废话)
        check("每一行最多只有一个 tag 配了背景色(effective_bg 那条口径的前提)",
              (all(n <= 1 for n in _bg_tags45.values()), max(_bg_tags45.values())),
              (True, 1))

        # ---- 收尾:ui_columns.json 就在数据库旁边,下一轮自检**会真的读它**,
        #      留着「某一行是绿底」的话下一轮的期望值就全变了
        _t45.hide_palette()
        _t45._row_colors.clear()
        _t45._want_cols = None
        _t45.zero_stock.set(True)
        _t45._save_cols()
        with open(_t45._col_cfg_path(), encoding="utf-8") as _f45:
            _raw45 = _f45.read()
        check("收尾:#34 刷的那些色没留在设置文件里(下一轮自检不受影响)",
              [x for x in ("#ffe3e3", "#1f618d", "#008000") if x in _raw45], [])
        check("收尾:色板也收起来了", _t45.pal_panel.winfo_manager(), "")

        app.update()
        p("  [OK ] 全量刷新")
        app.destroy()
        # destroy() 不会关数据库连接 —— 不显式关掉,临时库就一直被占着删不掉
        try:
            app.con.close()
        except Exception:  # noqa: BLE001
            pass
        p("\n界面已正常关闭(连接与窗口都释放)")

    except Exception:  # noqa: BLE001
        FAILS.append("抛出异常")
        p("\n!! 自检崩了:")
        p(traceback.format_exc())
    finally:
        try:
            os.remove(TEST_DB)
        except OSError:
            pass

    # ---- 顶部那道兜底自己也得交账(见文件开头的说明)----
    # 1) 段落级的打桩还原用的是「本段开始时捕获的那一份」,也就是顶部这道兜底。
    #    要是哪一段把它还原成了真的 tkinter.messagebox,后面任意一段漏桩就又会
    #    弹到用户桌面上。所以跑完必须确认兜底还在。
    check("跑完所有段落,对话框兜底还在(没被哪一段还原成真的)",
          gui.messagebox is FALLBACK, True)
    check("跑完所有段落,取色器兜底也还在",
          gui.colorchooser is FALLBACK_CHOOSER, True)
    # 2) 一条提示都不该**漏过**兜底。漏过去说明那一段忘了打桩,而兜底对「是/否」
    #    一律答「是」—— 那一段的断言很可能是建立在一个没人确认过的前提上。
    #    所以第三种情况也要报出来,而不是让它悄悄过去。
    check("全程没有一条提示漏过兜底(有就说明某一段忘了打桩)",
          (list(FALLBACK.infos), list(FALLBACK.warnings), list(FALLBACK.errors)),
          ([], [], []))

    p("\n" + "=" * 70)
    if FAILS:
        p(f"结果:失败 {len(FAILS)} 项")
        for f in FAILS:
            p(f"  · {f}")
    else:
        p("结果:全部通过")
    p("=" * 70)

    os.makedirs(os.path.dirname(RESULT), exist_ok=True)
    with open(RESULT, "w", encoding="utf-8") as f:
        f.write(OUT.getvalue())

    text = OUT.getvalue()
    try:
        print(text)
    except UnicodeEncodeError:
        # 控制台是 GBK 时,中文会编不出来 —— 只报结论,细节看结果文件
        print(f"自检完成:失败 {len(FAILS)} 项。详情见 {RESULT}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
