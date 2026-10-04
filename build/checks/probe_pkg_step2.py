# -*- coding: utf-8 -*-
"""第二步的成品包实地验证:导入复核窗口 + 找相似窗口,用真实 BOM 和真实数据跑。

    cd dist\\元器件物料管理
    runtime\\python.exe ..\\..\\build\\cache\\probe_pkg_step2.py

全程只碰用户数据库的**副本**,跑完还要回头确认原库一个字节都没动。
"""
import io
import os
import shutil
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
# 包目录:认**当前工作目录**。打包脚本第 8 步会先把 cwd 切到刚组装出来的暂存包
# 再跑这里;要是照旧写死成 <仓库>\dist\元器件物料管理,第 8 步验的就成了"上一次
# 已经装好的那个包" —— 新包缺文件、少模块都看不出来。cwd 不像个包时才回退。
_CWD = os.getcwd()
if (os.path.isdir(os.path.join(_CWD, "app"))
        and os.path.isdir(os.path.join(_CWD, "runtime"))):
    PKG = os.path.abspath(_CWD)
else:
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


def snapshot(path):
    st = os.stat(path)
    with io.open(path, "rb") as f:
        return (st.st_size, st.st_mtime_ns, len(f.read()))


try:
    import db
    import gui
    import server

    print("模块导入 OK")
    before = snapshot(REAL)

    tmp = os.path.join(HERE, "_pkgprobe2.db")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(tmp + suffix):
            os.remove(tmp + suffix)
    shutil.copy2(REAL, tmp)
    con = db.connect(tmp)
    db.init_db(con)
    con.close()

    app = gui.App(tmp)
    app.update()
    app.update_idletasks()
    print("窗口: %d x %d" % (app.winfo_width(), app.winfo_height()))

    tabs = [app.nb.tab(i, "text").strip() for i in range(app.nb.index("end"))]
    ck("7 个页签还在", len(tabs), 7)
    app.nb.select(app.tab_proj)
    app.update()
    pr = app.tab_proj
    ck("项目页三个子页签还在",
       [pr.sub.tab(i, "text").strip() for i in range(pr.sub.index("end"))],
       ["BOM 明细", "元件入库", "元件出库"])

    # ---------------------------------------------------------- 真实 BOM 的预览
    print("\n--- 拿用户真实的 BOM 走一遍预览 ---")
    inbox = os.path.join(PKG, "data", "inbox")
    name = None
    if os.path.isdir(inbox):
        for f in sorted(os.listdir(inbox)):
            if f.lower().endswith((".xlsx", ".xlsm", ".csv")):
                name = f
                break
    ck("在 data\\inbox 里找到了真实 BOM", bool(name), True)
    if name:
        with open(os.path.join(inbox, name), "rb") as f:
            up = {"filename": name, "data": f.read()}
        prev = gui.call(app.con, server.bom_preview, upload=up)
        ck("预览成功", bool(prev), True)
        print("     %s:%d 行,总需求 %s" % (name, prev["line_count"], prev["total_qty"]))
        ck("每行都带了复核要用的行号",
           all(l["source_row"] is not None for l in prev["lines"]), True)
        ck("每行都带了把握等级",
           all(l["confidence"] in ("high", "low", "none") for l in prev["lines"]), True)
        ck("每行都带了中文把握说法",
           all(l["confidence_label"] for l in prev["lines"]), True)
        ck("每行都带了推断依据",
           all(l["reason"] for l in prev["lines"]), True)
        ck("need_review 和逐行数出来的对得上",
           prev["need_review"],
           sum(1 for l in prev["lines"] if l["confidence"] in ("low", "none")))
        ck("把品类选项一起给出来了", "电阻" in prev["categories"], True)
        got_hi = sum(1 for l in prev["lines"] if l["confidence"] == "high")
        print("     明确 %d 行 / 要确认 %d 行" % (got_hi, prev["need_review"]))
        from collections import Counter
        print("     品类分布:", dict(Counter(l["category"] for l in prev["lines"])))

        # ------------------------------------------------ 导入复核窗口
        print("\n--- 导入复核窗口 ---")
        box = {"n": 0, "name": None, "cats": None}
        d = gui.BomReviewDialog(app, app, prev, default_name="成品包体检",
                                on_confirm=lambda n, c: box.update(
                                    name=n, cats=c))
        d.update()
        d.update_idletasks()
        print("     窗口 %dx%d,表格列出 %d 行"
              % (d.winfo_width(), d.winfo_height(), len(d.tree.get_children())))
        ck("默认列出的行数 = 要确认的行数(拿得准的不打扰人)",
           len(d.tree.get_children()), prev["need_review"])
        ck("表格有 10 列", len(d.tree.cget("columns")), 10)
        d.only_review.set(False)
        d._render()
        d.update()
        ck("取消筛选能看到全部", len(d.tree.get_children()), prev["line_count"])
        d.only_review.set(True)
        d._render()
        d.update()

        kids = d.tree.get_children()
        if kids:
            d.tree.selection_set(kids)
            app.update()
            d.pick.set("电阻")
            d._apply_sel()
            app.update()
            ck("批量改之后列出来的是改过的品类",
               {d.tree.item(i, "values")[7] for i in kids}, {"电阻"})
            ck("改过的行标成人工指定",
               {d.tree.item(i, "values")[8] for i in kids}, {"人工指定"})
            d.name.set("成品包复核测试")
            d.ok()
            app.update()
            ck("确认后交回项目名", box["name"], "成品包复核测试")
            ck("只交回改过的行", len(box["cats"] or {}), len(kids))
            ck("交回的是「行号 -> 品类」",
               sorted(box["cats"] or {}), sorted(str(k) for k in kids))

            # 真的落库
            prev2 = gui.call(app.con, server.bom_preview, upload=up)
            rep = gui.call(app.con, server.bom_import,
                           body={"project_name": "成品包复核测试",
                                 "categories": {str(k): "钽电容" for k in
                                                [l["source_row"] for l in
                                                 prev2["lines"]]}},
                           upload=up)
            ck("按人工改的品类导入成功", bool(rep), True)
            cats = {r[0] for r in app.con.execute(
                "SELECT category FROM component WHERE id IN"
                " (SELECT component_id FROM project_bom WHERE project_id=?)",
                (rep["project_id"],))}
            ck("库里落的就是人工指定的品类", cats, {"钽电容"})
            print("     导入 %d 行,元件品类全部按人工指定落库"
                  % rep["bom_lines"])
        else:
            print("     (这个 BOM 每行都认得明确,复核窗口默认空着 —— 也算对)")

        # ------------------------------------------------ 找相似
        print("\n--- 找相似窗口 ---")
        line = next((l for l in prev["lines"] if l["value"] and l["package"]), None)
        if line:
            print("     拿这一行去找:值=%s 封装=%s" % (line["value"], line["package"]))
            picked = {"cid": None}
            # 勾上「只看有库存的」,和 BOM 明细那个入口的默认一致 ——
            # 这样挑中的料一定能落进出库列表,才验得到「真的选上了」
            sd = gui.SimilarDialog(app, app, value=line["value"],
                                   package=line["package"],
                                   on_pick=lambda c: picked.update(cid=c),
                                   in_stock_only=True)
            sd.update()
            sd.update_idletasks()
            print("     窗口 %dx%d,候选 %d 个"
                  % (sd.winfo_width(), sd.winfo_height(),
                     len(sd.tree.get_children())))
            print("     摘要:", sd.sum.get())
            _n = len(sd.tree.get_children())
            if _n:
                ck("找得到有库存的候选", True, True)
            else:
                # 用户库里那颗料现在没库存 —— 这时必须解释,不能只说「没有像的」
                ck("滤空时说清了「有,只是没库存」", "没库存" in sd.sum.get(), True)
                ck("并给了下一步(取消勾选 / 先入库)",
                   "取消勾选" in sd.sum.get(), True)
                sd.destroy()
                sd = gui.SimilarDialog(app, app, value=line["value"],
                                       package=line["package"],
                                       on_pick=lambda c: picked.update(cid=c),
                                       in_stock_only=False)
                sd.update()
                ck("取消勾选之后能看到没库存的候选",
                   len(sd.tree.get_children()) > 0, True)
            tops = [sd.tree.item(i, "values") for i in sd.tree.get_children()]
            ck("候选里每一项都写了像在哪里",
               all(t[7] for t in tops), True)
            ck("候选里每一项都写了把握", all(t[6] for t in tops), True)
            ck("把握只有那三种说法",
               {t[6] for t in tops}
               <= {"很可能是同一颗", "值对上了,封装要自己看", "只是有点像"}, True)
            scores = [sd._items[int(i)]["score"]
                      for i in sd.tree.get_children()]
            ck("按分数从高到低排", scores, sorted(scores, reverse=True))
            first = sd.tree.get_children()[0]
            sd.tree.selection_set(first)
            app.update()
            sd.pick()
            app.update()
            ck("挑中之后把元件号交回去", picked["cid"], int(first))

            # 挑中之后要真的落到开单区。分配树整条路第三步
            # (probe_pkg_step3.py)会走一遍,这里只验「入口没断」:
            # 要么挂到那条 BOM 需求的分配树上勾好了,要么退到「自由出库」
            # 把它选中 —— 但**绝不能悄悄什么都不做**。
            _bid = app.con.execute(
                "SELECT id FROM project_bom WHERE project_id=?"
                " ORDER BY id LIMIT 1", (pr._pid,)).fetchone()
            _bid = _bid[0] if _bid else None


            class _Box:
                def __init__(self):
                    self.infos = []

                def showinfo(self, title, msg, **kw):
                    self.infos.append((title, msg))


            _box = _Box()
            real_box = gui.messagebox
            gui.messagebox = _box
            try:
                if _bid:
                    pr._pick_similar_from_line(_bid, int(first))
                app.update()
            finally:
                gui.messagebox = real_box

            ck("自动切到元件出库子页签", pr.sub.index(pr.sub.select()), 2)
            _pp = pr.pane_out.bom_form
            _hit = _bid is not None and (_bid, int(first)) in _pp.alloc
            _sel = pr.pane_out.form._current_id()
            print("     挂到分配树上:", _hit, "| 自由开单区选中:", _sel,
                  "| 弹了提示:", len(_box.infos))
            ck("挑中的料要么挂上了、要么在自由开单区选中、要么弹提示说清原因",
               bool(_hit) or _sel is not None or bool(_box.infos), True)
            if _box.infos:
                _msg = _box.infos[0][1]
                print("     提示:", _msg.replace(chr(10), " / "))
                ck("提示里说清了原因", len(_msg) > 10, True)
        else:
            print("     (这个 BOM 里没有同时带值和封装的行,跳过找相似)")

    app.destroy()
    try:
        app.con.close()
    except Exception:  # noqa: BLE001
        pass
    os.remove(tmp)

    # ---------------------------------------------------------- 原库没被动过
    after = snapshot(REAL)
    ck("用户的真实数据库一个字节都没被动过", after, before)

    print("\n结果:" + ("成品包第二步 PASS" if not bad else f"FAIL {bad}"))
    sys.exit(1 if bad else 0)
except Exception:
    print("!! 失败:")
    traceback.print_exc()
    sys.exit(1)
