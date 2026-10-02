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

        for i, name in enumerate(["元件库存", "出入库", "项目BOM", "流水", "仓位"]):
            app.nb.select(i)
            app.update()
            app.update_idletasks()
            p(f"  [OK ] 标签页「{name}」渲染正常")

        app.nb.select(0)
        app.update()
        tab = app.tab_comp

        # ---- 库存全为 0:首页应该是空的。导入 BOM 只是记下「这块板子要用到什么」,
        #      东西还没买回来,那不算库存,不该出现在这一页。
        check("没有库存时首页一张卡片都没有", tab.cards, {})
        check("没有库存时停在首页", tab.view, "home")
        check("没有库存时给出空状态提示", len(tab.card_area.winfo_children()), 1)
        check("没有库存时明细表是空的", len(tab.tree.get_children()), 0)
        p(f"  库存全 0:卡片 {len(tab.cards)} 张,只显示空状态提示")

        # ---- 入一点货,首页才该长出对应的大类卡片
        p("\n【4】入库之后,该大类才出现在首页")
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

        want_cards = sorted({c for c, _ in chosen})
        check("入库后首页出现对应的大类卡片", sorted(tab.cards), want_cards)
        check("卡片上只有名称,不带数量",
              all(not any(ch.isdigit() for ch in t)
                  for c in tab.cards.values() for t in card_labels(c)), True)
        check("首页不显示任何项目", any("项目" in k for k in tab.cards), False)
        check("在库元件就是入过货的那几个",
              sorted(x["id"] for x in tab._all), sorted({it["id"] for _, it in chosen}))
        check("在库元件库存都 > 0", all(x["on_hand"] > 0 for x in tab._all), True)
        # 卡片尺寸必须就是设定值 —— 曾经因为子控件是 pack 的、却只关了
        # grid_propagate,卡片被内容撑成 130x72,这里看住它
        check("卡片尺寸就是设定值,没被内容撑变",
              {(c.winfo_width(), c.winfo_height()) for c in tab.cards.values()},
              {(gui.CARD_W, gui.CARD_H)})
        check("有元件被库存过滤掉(证明过滤真的生效)",
              len(items) - len(tab._all) > 0, True)
        p(f"  入库 {len(chosen)} 种后:卡片 {want_cards},在库 {len(tab._all)} 种,"
          f"其余 {len(items) - len(tab._all)} 种(库存 0)不显示")

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
        k_qty = cols.index("库存")
        check("二级表里每行库存都 > 0",
              all(float(tab.tree.item(k, "values")[k_qty]) > 0
                  for k in tab.tree.get_children()), True)
        show = str(tab.tree.cget("show"))
        check("二级是平表,没有展开三角",
              "headings" in show and "tree" not in show, True)
        p(f"  点开「{cat0}」-> {n0} 个型号(该大类在库的全在这里),页面切换正常")
        for k in tab.tree.get_children():
            v = tab.tree.item(k, "values")
            p(f"      {v[0]}  库存 {v[k_qty]}  {v[cols.index('状态')]}")

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
        p(f"  搜索「{cat0}」-> {len(tab.tree.get_children())} 条")
        tab.q.set("")
        tab.go_home()
        app.update()

        app.nb.select(1)
        app.update()
        for k in ("TRANSFER", "ADJUST", "IN", "OUT"):
            app.tab_stock.kind.set(k)
            app.tab_stock._sync()
            app.update()
        p("  [OK ] 出入库页四种动作切换正常")

        app.nb.select(2)
        app.update()
        p(f"  [OK ] 项目页 {len(app.tab_proj.t_proj.get_children())} 个项目,"
          f"BOM {len(app.tab_proj.t_bom.get_children())} 行,缺料标签「{app.tab_proj.shortage.get()}」")

        app.nb.select(3)
        app.update()
        p(f"  [OK ] 流水页 {len(app.tab_move.tree.get_children())} 行")
        app.nb.select(4)
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
        p("  [OK ] 出入库弹窗")
        mv.destroy()

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
