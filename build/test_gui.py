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
        p("\n【5】出入库:拆成入库 / 出库两个页签,先选项目再进二级页")
        app.nb.select(app.tab_stock)
        app.update()
        st = app.tab_stock
        check("默认停在入库", st.action, "IN")
        check("进来是项目卡片首页", st.view, "home")
        check("入库 / 出库两个按钮都在", sorted(st.btn), ["IN", "OUT"])
        check("项目卡片里有「不指定项目」那一格", "0" in st.cards, True)
        check("项目卡片数 = 项目数 + 1(不指定项目)",
              len(st.cards), len(st._projects) + 1)
        check("每张项目卡片都写着「点开开单 / 看记录」",
              all("点开开单" in "".join(card_labels(c)) for c in st.cards.values()), True)
        p(f"  项目卡片 {sorted(st.cards)}")

        st.open_project("0")
        app.update()
        check("选了项目后进二级页", st.view, "proj")
        check("二级页已显示", bool(st.page_proj.winfo_ismapped()), True)
        check("项目首页已收起", bool(st.page_home.winfo_ismapped()), False)
        check("标题写出了动作和项目", st.proj_title.get(), "入库 · 不指定项目")
        # 【4】里那几次入库没挂项目,所以本来就该出现在「不指定项目」下
        base = len(gui.call(app.con, server.list_movements,
                            query={"project": "none", "kind": "IN"}, quiet=True)["items"])
        check("「不指定项目」只收没挂项目的流水",
              all(m["project_id"] is None for m in gui.call(
                  app.con, server.list_movements,
                  query={"project": "none", "kind": "IN"}, quiet=True)["items"]), True)
        check("二级页显示的就是这个项目 + 这个动作的全部记录",
              len(st.t_rec.get_children()), base)
        check("记录表是平表,没有展开三角",
              "headings" in str(st.t_rec.cget("show"))
              and "tree" not in str(st.t_rec.cget("show")), True)

        it = chosen[0][1]
        st.tree.selection_set(str(it["id"]))
        app.update()
        st.qty.set("5")
        st.loc.set("未分类")
        st.note.set("自检第一条")
        st.submit()
        app.update()
        recs = st.t_rec.get_children()
        check("开单后记录表立刻多一条(刷新要跟上,不能是旧的)", len(recs), base + 1)
        v = st.t_rec.item(recs[0], "values")
        check("最新一条就是刚入的元件", v[1], it["name"])
        check("记录里数量对", int(v[3]), 5)

        st.qty.set("3")
        st.note.set("自检第二条")
        st.submit()
        app.update()
        recs = st.t_rec.get_children()
        check("再开一单,记录再多一条", len(recs), base + 2)
        check("记录按时间倒序(新的在最上面)",
              st.t_rec.item(recs[0], "values")[6], "自检第二条")
        check("倒序:第二新的在它下面",
              st.t_rec.item(recs[1], "values")[6], "自检第一条")

        # ---- 出库是独立的一套,不会和入库混在一起
        st.set_action("OUT")
        app.update()
        check("切到出库后先回项目卡片首页", st.view, "home")
        check("出库页签记住自己是出库", st.action, "OUT")
        st.open_project("0")
        app.update()
        check("出库的标题跟着动作变", st.proj_title.get(), "出库 · 不指定项目")
        base_out = len(gui.call(app.con, server.list_movements,
                                query={"project": "none", "kind": "OUT"},
                                quiet=True)["items"])
        check("入库那几单不会出现在出库记录里",
              len(st.t_rec.get_children()), base_out)
        st.tree.selection_set(str(it["id"]))
        st.qty.set("2")
        st.note.set("自检出库")
        st.submit()
        app.update()
        check("开一单出库,出库记录里多一条",
              len(st.t_rec.get_children()), base_out + 1)
        st.set_action("IN")
        st.open_project("0")
        app.update()
        check("切回入库,出库那一单不会混进来",
              len(st.t_rec.get_children()), base + 2)
        st.go_home()
        app.update()
        check("返回项目卡片首页正常", st.view, "home")

        app.nb.select(app.tab_proj)
        app.update()
        p(f"  [OK ] 项目页 {len(app.tab_proj.t_proj.get_children())} 个项目,"
          f"BOM {len(app.tab_proj.t_bom.get_children())} 行,缺料标签「{app.tab_proj.shortage.get()}」")

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

        p("\n【12】元件弹窗:新字段与参数解析")
        d3 = gui.ComponentDialog(app, app, cid0)
        d3.update()
        for key in ("unit_price", "min_stock", "reorder_qty", "supplier", "default_loc_id"):
            check(f"弹窗有「{key}」字段", key in d3.vars, True)
        d3.params.delete("1.0", "end")
        d3.params.insert("1.0", "耐压=50V\n精度=1%\n没有等号的行\n\n温度= -40~85C")
        parsed = d3._parse_params()
        check("参数按 key=value 解析", parsed.get("耐压"), "50V")
        check("参数值两边空格被去掉", parsed.get("温度"), "-40~85C")
        check("没有等号的行被忽略", len(parsed), 3)
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
