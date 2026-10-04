# -*- coding: utf-8 -*-
"""在打包目录里实地跑一遍新版界面:账本两张表 + 项目页三个子页签。

用包里那个 python.exe 直接跑(启动器是 winexe,出错时只弹对话框,拿不到 traceback)。
执行时的工作目录必须是包目录,这样相对路径和真实启动一致。

    cd dist\\元器件物料管理
    runtime\\python.exe ..\\..\\build\\cache\\probe_pkg_step1.py
"""
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
    print(f"  [{'OK ' if ok else 'FAIL'}] {label}: {got!r}" + ("" if ok else f" (期望 {want!r})"))


try:
    import db
    import gui
    import server

    print("模块导入 OK")

    real = os.path.join(PKG, "data", "parts.db")
    tmp = os.path.join(HERE, "_pkgprobe1.db")
    # 绝不碰用户数据:复制一份再跑
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(tmp + suffix):
            os.remove(tmp + suffix)
    shutil.copy2(real, tmp)

    con = db.connect(tmp)
    db.init_db(con)
    con.close()

    app = gui.App(tmp)
    app.update()
    app.update_idletasks()
    print("窗口: %d x %d" % (app.winfo_width(), app.winfo_height()))
    tabs = [app.nb.tab(i, "text").strip() for i in range(app.nb.index("end"))]
    print("页签:", tabs)
    ck("7 个页签都在", len(tabs), 7)
    ck("出入库页签还在", "出入库" in tabs, True)
    ck("流水页签还在(盘点/移库要有地方看)", "流水" in tabs, True)

    # ---------------------------------------------------------- 出入库
    print("\n--- 出入库:只读账本 ---")
    app.nb.select(app.tab_stock)
    app.update()
    st = app.tab_stock
    ck("两个方向", sorted(st.btn), ["IN", "OUT"])
    ck("没有开单区了", hasattr(st, "submit"), False)
    ck("入库流水行数 = 库里全部入库流水",
       len(st.tree.get_children()),
       len(server.list_movements(gui.make_ctx(app.con, query={"kind": "IN", "limit": "5000"}),
                                 gui._Match())[1]["items"]))
    print("  摘要:", st.summary.get())
    ck("摘要说明了只查账", "只查账" in st.summary.get(), True)
    ck("项目下拉有「全部项目」", st.ALL in st.cb_proj.cget("values"), True)
    ck("项目下拉有「不指定项目」", st.NONE_PROJ in st.cb_proj.cget("values"), True)
    st.set_action("OUT")
    app.update()
    print("  出库流水 %d 行 / 摘要: %s" % (len(st.tree.get_children()), st.summary.get()))
    st.set_action("IN")
    app.update()

    # ---------------------------------------------------------- 项目 BOM
    print("\n--- 项目 BOM:三个子页签 ---")
    app.nb.select(app.tab_proj)
    app.update()
    pr = app.tab_proj
    subs = [pr.sub.tab(i, "text").strip() for i in range(pr.sub.index("end"))]
    ck("三个子页签", subs, ["BOM 明细", "元件入库", "元件出库"])
    n_pick = len(pr.t_proj.get_children())
    print("  项目数:", n_pick)
    if n_pick:
        pr.t_proj.selection_set(pr.t_proj.get_children()[0])
        app.update()
        app.update_idletasks()
        ck("选项目后入库区拿到该项目", pr.pane_in.project_id, int(pr.t_proj.get_children()[0]))
        # 两个方向现在默认都是「按 BOM」,底下那张旧的自由开单表要先切出来才是活的
        ck("入库/出库默认都走「按 BOM」",
           (pr.pane_in.mode.get(), pr.pane_out.mode.get()), ("bom", "bom"))
        pr.pane_in.show_free()
        pr.pane_out.show_free()
        app.update()
        ck("切到自由之后入库区元件列表非空",
           len(pr.pane_in.form.tree.get_children()) > 0, True)
        ck("入库默认列全部", pr.pane_in.form.only_stocked.get(), False)
        ck("出库默认只列有库存的", pr.pane_out.form.only_stocked.get(), True)
        ck("入库记录表是平表", "tree" not in str(pr.pane_in.t_rec.cget("show")), True)
        print("  入库可选元件 %d 个 / 出库可选 %d 个 / 入库记录 %d 条" % (
            len(pr.pane_in.form.tree.get_children()),
            len(pr.pane_out.form.tree.get_children()),
            len(pr.pane_in.t_rec.get_children())))
        for k in ("pane_in", "pane_out"):
            pr.sub.select(getattr(pr, k))
            app.update()
            app.update_idletasks()
            w = getattr(pr, k)
            print("  %s 显示 %dx%d" % (k, w.winfo_width(), w.winfo_height()))
            ck(f"{k} 没有被压扁", w.winfo_width() > 200, True)
        pr.sub.select(0)
        app.update()
    else:
        print("  (库里没有项目,跳过子页签内容检查)")

    app.destroy()
    try:
        app.con.close()
    except Exception:  # noqa: BLE001
        pass
    os.remove(tmp)
    print("\n结果:" + ("成品包新版界面 PASS" if not bad else f"FAIL {bad}"))
except Exception:
    print("!! 失败:")
    traceback.print_exc()
    sys.exit(1)
