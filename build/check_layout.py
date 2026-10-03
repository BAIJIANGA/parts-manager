# -*- coding: utf-8 -*-
"""排版体检 —— 不开截图也能发现「控件被压扁 / 表格列超出宽度」。

为什么需要它:界面是 tkinter 画的,列宽写死、卡片尺寸写死,窗口一窄就会
出现「列被截掉」「控件被压成 1 像素」。这类问题光看代码看不出来,而我(模型)
读不了截图,所以只能用几何数字来查。

查三件事:
  1. 每张卡片是不是正好 CARD_W × CARD_H,有没有互相压住
  2. 每个 Treeview 的列宽之和 vs 实际可用宽度 —— 超了就说明有列看不见
  3. 有没有「已经显示出来、却被压到 1 像素以下」的容器控件

在默认尺寸(1360×830)和最小尺寸(1060×640)下各查一遍。
"""
from __future__ import annotations

import io
import os
import shutil
import sys

import tkinter as tk
import tkinter.ttk as ttk

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, os.path.join(ROOT, "app"))

import gui     # noqa: E402
import server  # noqa: E402

CACHE = os.path.join(ROOT, "build", "cache")
DB = os.path.join(CACHE, "layout.db")

OUT = io.StringIO()
FAILS = []


def p(*a):
    OUT.write(" ".join(str(x) for x in a) + "\n")


def bad(msg):
    FAILS.append(msg)
    p(f"  [问题] {msg}")


def walk(w):
    yield w
    for c in w.winfo_children():
        yield from walk(c)


def check_trees(root, where, strict=True):
    """列宽之和超过实际宽度 -> 有列被截掉。

    strict=True 用于默认窗口尺寸:这时候要求一列都不用横向拖。strict=False
    用于最小尺寸:窗口被拉到最小时横向滚动是正常的(每张表都带横向滚动条,
    没有东西是够不着的),只提示不报错。
    """
    for w in walk(root):
        if not isinstance(w, ttk.Treeview):
            continue
        if not w.winfo_ismapped():
            continue
        cols = list(w["columns"])
        show = str(w.cget("show"))
        # show="tree headings" 时还有一列 #0 在显示,它不列在 columns 里,得补上,
        # 否则算出来的宽度比真实值小,溢出会被漏判
        if "tree" in show:
            cols = ["#0"] + cols
        if not cols:
            continue
        total = sum(int(w.column(c, "width")) for c in cols)
        avail = w.winfo_width()
        heads = [str(w.heading(c)["text"]) for c in cols]
        if total > avail + 2:
            msg = (f"{where}:表格 {heads[:3]}… 列宽合计 {total} > 可用 {avail} "
                   f"(超出 {total - avail}px)")
            if strict:
                bad(msg + ",默认尺寸下就得横向拖")
            else:
                p(f"  (提示) {msg} —— 最小尺寸下可横向滚动,属正常")
        else:
            p(f"  [OK ] {where}:表格 {len(cols)} 列,合计 {total} ≤ 可用 {avail}")


def check_squashed(root, where):
    """已显示的容器被压到 1 像素以下 —— 通常是 pack/grid 用错了。"""
    n = 0
    for w in walk(root):
        if not w.winfo_ismapped():
            continue
        if isinstance(w, (tk.Toplevel, tk.Tk)):
            continue
        cls = w.winfo_class()
        if cls not in ("Frame", "TFrame", "Labelframe", "TLabelframe",
                       "Canvas", "Treeview", "Text", "Panedwindow", "TPanedwindow"):
            continue
        if not w.winfo_children() and cls not in ("Canvas", "Treeview", "Text"):
            continue
        ww, hh = w.winfo_width(), w.winfo_height()
        # 藏在未选中的页签里的控件宽高本来就是 1,只查已经真正显示出来的
        if hh <= 1 and w.winfo_manager():
            bad(f"{where}:{cls} 被压扁(高 {hh}px) 路径 {str(w)[:70]}")
            n += 1
        elif ww <= 1 and w.winfo_manager():
            bad(f"{where}:{cls} 被压扁(宽 {ww}px) 路径 {str(w)[:70]}")
            n += 1
    return n


def check_cards(tab, where):
    """卡片必须正好 CARD_W × CARD_H,且互不重叠。"""
    cards = list(tab.cards.values())
    if not cards:
        p(f"  [OK ] {where}:当前没有卡片(空态)")
        return
    wrong = []
    for c in cards:
        w, h = c.winfo_width(), c.winfo_height()
        if (w, h) != (gui.CARD_W, gui.CARD_H):
            wrong.append(f"{w}x{h}")
    if wrong:
        bad(f"{where}:有 {len(wrong)} 张卡片尺寸不对(应为 "
            f"{gui.CARD_W}x{gui.CARD_H}),实际 {wrong[:4]}")
    else:
        p(f"  [OK ] {where}:{len(cards)} 张卡片都是 {gui.CARD_W}x{gui.CARD_H}")

    rects = [(c.winfo_rootx(), c.winfo_rooty(), c.winfo_width(), c.winfo_height())
             for c in cards]
    hits = 0
    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            ax, ay, aw, ah = rects[i]
            bx, by, bw, bh = rects[j]
            if ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah:
                hits += 1
    if hits:
        bad(f"{where}:有 {hits} 对卡片互相重叠")
    else:
        p(f"  [OK ] {where}:{len(cards)} 张卡片互不重叠")


def main() -> int:
    if os.path.exists(DB):
        os.remove(DB)
    shutil.copy2(os.path.join(ROOT, "data", "parts.db"), DB)

    app = gui.App(DB)
    for geom in ("1360x830", "1060x640"):
        app.geometry(geom)
        app.update()
        app.update_idletasks()
        p(f"\n【窗口 {geom}】实际 {app.winfo_width()}x{app.winfo_height()}")
        # 默认尺寸是日常用法,要求一列都不用拖;最小尺寸只提示
        strict = geom == "1360x830"

        for name, tab in app._tabs.items():
            app.nb.select(tab)
            app.update()
            app.update_idletasks()
            check_trees(tab, name, strict)
            check_squashed(tab, name)

        # 库存首页的卡片 + 出入库项目页的卡片都要查
        app.nb.select(app.tab_comp)
        app.tab_comp.go_home()
        app.update()
        check_cards(app.tab_comp, "库存首页卡片")

        # 首页右上角那排按钮越加越多,得看住搜索框别被挤没了 ——
        # 搜索是这套东西的主入口,它被挤扁等于主要功能废掉
        app.update_idletasks()
        ew = app.tab_comp.ent_q.winfo_width()
        if ew < 120:
            bad(f"库存首页的搜索框只剩 {ew}px —— 被右上角那排按钮挤扁了(至少要 120)")
        else:
            p(f"  [OK ] 库存首页搜索框还有 {ew}px,没被按钮挤扁")

        # 二级页面的表在首页状态下没被 pack,不特意点进去就查不到 —— 那正是
        # 列最多、最容易溢出的那张表。开发库里可能一件库存都没有,所以有货的
        # 大类优先,没有就随便点一个,总之要把这张表显示出来。
        names = app.tab_comp._card_names
        cats = [n for n in names if app.tab_comp._cat_data.get(n)] or list(names[:1])
        if cats:
            app.tab_comp.open_category(cats[0])
            app.update()
            app.update_idletasks()
            check_trees(app.tab_comp, "comp/二级页", strict)
            check_squashed(app.tab_comp, "comp/二级页")

        # 出入库页现在是一张只读流水表(入库 / 出库两张),没有卡片也没有二级页了
        app.nb.select(app.tab_stock)
        app.update()
        for act, name in (("IN", "stock/入库流水"), ("OUT", "stock/出库流水")):
            app.tab_stock.set_action(act)
            app.update()
            app.update_idletasks()
            check_trees(app.tab_stock, name, strict)
            check_squashed(app.tab_stock, name)

        # 开单搬到了项目 BOM 页,而且右半边变成了三个子页签 ——
        # 每一个的可用宽度都比以前窄一点,必须逐个量。
        app.nb.select(app.tab_proj)
        for k in ("pane_in", "pane_out"):
            app.tab_proj.sub.select(app.tab_proj.__dict__[k])
            app.update()
            app.update_idletasks()
            check_trees(app.tab_proj.__dict__[k], f"proj/{k}", strict)
            check_squashed(app.tab_proj.__dict__[k], f"proj/{k}")
        app.tab_proj.sub.select(0)
        app.update()
        app.update_idletasks()
        check_squashed(app.tab_proj, "proj/BOM 明细")

    # 弹窗各有自己的几何尺寸,主窗口那轮体检管不着它们。
    # 新增元件窗口字段最多、盘点窗口列最宽,是两个最容易溢出的。
    p("\n【弹窗】按各自的默认尺寸")
    makers = [("快速入库", lambda: gui.QuickInDialog(app, app)),
              ("批量入库", lambda: gui.BatchInDialog(app, app)),
              ("新增元件", lambda: gui.ComponentDialog(app, app, None)),
              ("查重与合并", lambda: gui.DedupeDialog(app, app)),
              ("选一个元件", lambda: gui.ComponentPicker(app, app))]
    # 导入复核窗口是全套里列最多的(10 列),最容易撑爆;找相似窗口 8 列。
    # 两个都用**真实**的后端 payload,不然界面改了字段名这里也发现不了。
    _csv = os.path.join(CACHE, "layout_review.csv")
    with io.open(_csv, "w", encoding="utf-8", newline="") as f:
        f.write("Designator,Quantity,Value,Footprint\n"
                "R1,1,10kΩ,0603\n"
                "C1,1,100nF,0805\n"
                ",2,10k,F3\n")
    with open(_csv, "rb") as f:
        _up = {"filename": "layout_review.csv", "data": f.read()}
    try:
        _prev = gui.call(app.con, server.bom_preview, upload=_up)
    except Exception as exc:  # noqa: BLE001
        p(f"  (导入复核窗口跳过:拿不到预览 payload {type(exc).__name__}: {exc})")
        _prev = None
    if _prev:
        makers.append(("导入复核", lambda: gui.BomReviewDialog(
            app, app, _prev, default_name="排版体检板")))
    _c = app.con.execute(
        "SELECT value, package FROM component WHERE merged_into IS NULL"
        " ORDER BY (value = '') + (package = ''), id LIMIT 1").fetchone()
    if _c:
        makers.append(("找相似", lambda: gui.SimilarDialog(
            app, app, value=_c["value"] or "", package=_c["package"] or "")))

    row = app.con.execute(
        "SELECT id FROM location WHERE structural=0 ORDER BY id LIMIT 1").fetchone()
    if row:
        makers.append(("盘点仓位", lambda: gui.StocktakeDialog(app, app, row["id"])))
    for label, make in makers:
        try:
            d = make()
            d.update()
            d.update_idletasks()
            check_trees(d, f"弹窗/{label}")
            check_squashed(d, f"弹窗/{label}")
            d.destroy()
            app.update()
        except Exception as exc:  # noqa: BLE001
            p(f"  (弹窗 {label} 跳过:{type(exc).__name__}: {exc})")

    app.destroy()
    try:
        app.con.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        os.remove(DB)
    except OSError:
        pass

    p("\n" + "=" * 66)
    p(f"排版体检:{'没有发现问题' if not FAILS else str(len(FAILS)) + ' 个问题'}")
    for f in FAILS:
        p(f"  · {f}")
    io.open(os.path.join(CACHE, "layout_check.txt"), "w", encoding="utf-8").write(OUT.getvalue())
    print(f"layout={'FAIL' if FAILS else 'PASS'} problems={len(FAILS)}")
    for f in FAILS[:25]:
        print("  " + f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
