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


def card_labels(card):
    """把一张大类卡片上所有 Label 的文字取出来(卡片是 tk.Frame)。"""
    out = []
    for w in card.winfo_children():
        if isinstance(w, tk.Label):
            out.append(w.cget("text"))
        elif isinstance(w, tk.Frame):
            out += [c.cget("text") for c in w.winfo_children() if isinstance(c, tk.Label)]
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
        con.commit()
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

        # ---- 首页固定列出 16 个标准大类:有没有库存都列出来,一眼看得到分类全貌
        check("首页固定列出全部 16 个标准大类,一个不少",
              [c for c in gui.CATEGORY_ORDER if c not in tab.cards], [])
        check("卡片数 = 标准大类数 + 库里另有品类的补充",
              len(tab.cards), len(tab._card_names))
        check("没有库存时停在首页", tab.view, "home")
        check("没有库存时明细表是空的", len(tab.tree.get_children()), 0)
        check("没有库存的大类卡片仍显示为可点(不是隐藏)",
              len(tab.cards), len(gui.CATEGORY_ORDER) > 0 and len(tab.cards))

        empty_cats = [n for n in tab._card_names if not tab._cat_data.get(n)]
        check("库存全 0,所以每张大类卡片都标着「暂无库存」",
              all("暂无库存" in "".join(card_labels(tab.cards[n])) for n in empty_cats), True)
        p(f"  库存全 0:16 张大类卡片都在,都标着「暂无库存」(不是隐藏)")

        # ---- 点进一个没货的大类:二级页面能打开,里面什么都没有
        probe = "磁珠"
        check("「磁珠」是标准大类但库里没有", probe in tab.cards and probe in empty_cats, True)
        tab.open_category(probe)
        app.update()
        check("点进没货的大类也能打开二级页面", tab.view, "cat")
        check("没货的大类二级页面里一行都不显示", len(tab.tree.get_children()), 0)
        check("没货的大类给出「暂无库存元件」", tab.cat_count.get(), "暂无库存元件")
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

        check("入库后 16 个大类卡片一张没少(还是全套,不是只剩有货的)",
              [c for c in gui.CATEGORY_ORDER if c not in tab.cards], [])
        check("入过货的大类不再标「暂无库存」",
              all("暂无库存" not in "".join(card_labels(tab.cards[c])) for c, _ in chosen), True)
        check("没入过货的大类仍然标「暂无库存」",
              "暂无库存" in "".join(card_labels(tab.cards[probe])), True)
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
        check("连刷 3 次,大类卡片一张都不少(入库后首页没变空白)",
              [c for c in gui.CATEGORY_ORDER if c not in tab.cards], [])
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
        # 这五列是这次重做的核心:我有什么 / 要多少 / 缺多少 / 在路上多少 / 该买多少
        for col in ("现有", "安全", "需求", "缺口", "在途", "该买"):
            check(f"二级表有「{col}」列", col in cols, True)
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
        check("记录表是平表,没有展开三角",
              "tree" not in str(st.tree.cget("show")), True)
        # 盘点/移库既不是入库也不是出库,只该出现在「流水」页
        kinds_shown = {str(st.tree.item(i, "values")[1]).replace("(已撤销)", "")
                       .replace("·撤销", "") for i in st.tree.get_children()}
        check("这里不会出现盘点 / 移库",
              kinds_shown <= {"入库", "出库", ""}, True)

        base_in = len(gui.call(app.con, server.list_movements,
                               query={"kind": "IN", "limit": "5000"}, quiet=True)["items"])
        check("入库流水显示的就是全部入库流水",
              len(st.tree.get_children()), base_in)
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
              len(st.tree.get_children()), none_in)
        # 【4】里那几次入库没挂项目,所以本来就该出现在「不指定项目」下
        check("「不指定项目」里确实有前面那几笔", none_in > 0, True)
        st.proj.set(st.ALL)
        st.reload()
        app.update()
        check("切回「全部项目」又都回来了",
              len(st.tree.get_children()), base_in)

        # ---- 出库流水是独立的一套
        st.set_action("OUT")
        app.update()
        check("出库页签记住自己是出库", st.action, "OUT")
        base_out = len(gui.call(app.con, server.list_movements,
                                query={"kind": "OUT", "limit": "5000"},
                                quiet=True)["items"])
        check("出库流水显示的是全部出库流水",
              len(st.tree.get_children()), base_out)
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
        check("在项目里开的入库单会落在这个项目名下",
              got[0]["note"], "项目里开的入库单")
        check("记录表立刻多一条", len(pr.pane_in.t_rec.get_children()), n_rec + 1)
        check("这边开的单也进了出入库页的入库流水",
              any(gui.call(app.con, server.list_movements, query={"kind": "IN"})
                  ["items"][k]["note"] == "项目里开的入库单" for k in range(3)), True)

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
        p(f"  [OK ] 流水页 {len(app.tab_move.tree.get_children())} 行")
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
            first = tab_mv.tree.get_children()[0]
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
                     for i in tab_mv.tree.get_children()}
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
                  len(st.tree.get_children()), 1)

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

        _ask27b = gui.ask_text
        gui.ask_text = lambda *a, **k: "三"
        try:
            rp.edit_qty(str(b27b))
            app.update()
        finally:
            gui.ask_text = _ask27b
        check("填了不是数字的东西,数量不变",
              int(rp.tree.item(str(b27b), "values")[col_of(rp, "qty")]), 5)

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

        # 收完货切到「元件出库」:刚收进来的那颗料必须已经出现在候选里。
        # 两张表看的是同一份库存,切过去还显示旧数字的话,用户会以为
        # 「我明明收了,怎么找不到」
        pr.sub.select(pr.pane_out)
        app.update()
        po27 = pr.pane_out.bom_form
        _ln27 = next(l for l in po27.lines if l["bom_id"] == b27a)
        check("切到出库页,刚收进来的那颗料就在候选里",
              c27a in [c["id"] for c in _ln27["candidates"]], True)
        check("而且候选上写的库存就是刚收的 7 个",
              next(c["on_hand"] for c in _ln27["candidates"]
                   if c["id"] == c27a), 7)
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
        check("父行上写着还差多少", "\u8fd8\u5dee 10" in pp.tree.item(parent, "text"), True)
        check("父行也带品类",
              pp.tree.item(parent, "values")[col_of(pp, "category")], "\u7535\u5bb9")
        check("子行默认没勾",
              pp.tree.item(pp.child_iid(b28, c28a),
                           "values")[col_of(pp, "pick")], gui.CHECK_OFF)

        # ------- 用户要的就是这一件事:0603 出 8 个之后,0805 那边自动变成还差 2
        pp.toggle(pp.child_iid(b28, c28a))
        app.update()
        check("勾上 0603 那颗,默认把它 8 个库存全出", pp.alloc[(b28, c28a)], 8)
        check("父行的「还需要」立刻从 10 变成 2",
              int(pp.tree.item(parent, "values")[col_of(pp, "left")]), 2)
        check("0805 那行的「还需要」也变成 2",
              int(pp.tree.item(pp.child_iid(b28, c28c),
                               "values")[col_of(pp, "left")]), 2)
        pp.toggle(pp.child_iid(b28, c28c))
        app.update()
        check("再勾 0805,默认正好补上剩下的 2 个", pp.alloc[(b28, c28c)], 2)
        check("父行的「还需要」归零",
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

        # 品类:可编辑下拉
        check("品类那个下拉能直接打字(认不出的品类必须能自己填)",
              "readonly" not in d30.cb_cat.state(), True)
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
        pr.sub.select(pr.pane_out)
        pr.pane_out.show_bom()
        app.update()
        po31 = pr.pane_out.bom_form
        po31.set_project(pid29)
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
        # (SELFTEST-A 的值是 470nF,但显示名是它自己那个名字)
        solo31 = [t for t in texts31 if "SELFTEST-A" in t]
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
        check("编辑窗口的品类下拉里能看到子类的全路径",
              "菜单测试类 / 下拉子类" in list(d35.cb_cat.cget("values")), True)
        check("打开时显示的就是全路径,不是大类名",
              d35.vars["category"].get(), "菜单测试类 / 下拉子类")
        check("顶层仍旧只显示光名字(不然一屏全是「电阻 / ...」)",
              "电阻" in list(d35.cb_cat.cget("values")), True)

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

        app.refresh_all()
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
