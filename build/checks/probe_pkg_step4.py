# -*- coding: utf-8 -*-
"""第四步的成品包实地验证:单个入库(#2)、BOM 明细详情面板与改品类(#3)、
名字与身份键分家(#1)。

    cd dist\\元器件物料管理
    runtime\\python.exe ..\\..\\build\\cache\\probe_pkg_step4.py

和第三步一样,全程只碰用户数据库的**副本**,跑完回头确认原库一个字节没动。
不一样的是这一步刻意**直接用用户自己的那个项目和那张 BOM** 来验 ——
合成数据能过不代表他那 19 行能过。
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


def stock_of(con, cid):
    return con.execute("SELECT COALESCE(SUM(qty),0) FROM stock WHERE component_id=?",
                       (cid,)).fetchone()[0]


try:
    import bom
    import db
    import gui
    import server

    print("模块导入 OK")
    before = snapshot(REAL)

    # ---------------------------------------------------- 新版到底在不在包里
    print("\n--- 成品包里是不是新版 ---")
    ck("界面里有详情面板这个类", hasattr(gui, "LineDetail"), True)
    ck("收料面板有「只入库这一行」这个说法",
       hasattr(gui.BomReceivePane, "ONE"), True)
    ck("收料面板有单收那个方法",
       hasattr(gui.BomReceivePane, "receive_selected"), True)
    ck("收料面板有右键菜单那些入口",
       hasattr(gui.BomReceivePane, "set_checked"), True)

    txt = io.open(os.path.join(PKG, "使用说明.txt"), encoding="utf-8-sig").read()
    ck("说明书里写了单个入库", "只入库选中这一行" in txt, True)
    # 详情面板能改品类这件事,说明书**还在讲**(第十一批只是跟着界面换了说法:
    # 品类那格不再是个能直接打字的框 + 「保存品类」,而是只读展示 + 「换…」一级一级选)。
    # 原先这里写死找「保存品类」这四个字 —— 那个按钮已经不存在了,所以这条断言
    # 无论说明书写得多清楚都会红。改成「在详情面板那一段里讲到改品类」这个口径:
    # 段落还在、段里还要出现「品类」和「换…」,少一样就红,不靠某个会过期的按钮名。
    _i = txt.find("右边就会出来这块料的详情")
    _sec = txt[_i:_i + 700] if _i >= 0 else ""
    ck("说明书里有「点一行出详情」那一段", bool(_sec), True)
    ck("说明书里写了详情面板能改品类",
       ("品类" in _sec) and ("换…" in _sec), True)

    # ---------------------------------------------------- #1 名字 vs 身份键
    print("\n--- #1 名字归名字,封装归封装 ---")
    ck("显示名只放值,后面不再拼封装",
       bom.build_name("100nF", None, None, "电容"), "100nF")
    ck("值和封装都空时才退到品类",
       bom.build_name("", None, None, "电容"), "电容")
    ck("同值不同封装算出两个不同的身份键(原来这里会并成一条)",
       bom.identity_key("100nF", "0603", None, None)
       != bom.identity_key("100nF", "0805", None, None), True)
    ck("封装写法不同但归一化后是同一颗",
       bom.identity_key("100nF", "C-0603", None, None),
       bom.identity_key("100nF", "c0603", None, None))
    ck("什么都判断不出来时给空串,不是某个光秃秃的前缀",
       bom.identity_key("", "", None, None), "")

    tmp = os.path.join(HERE, "_pkgprobe4.db")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(tmp + suffix):
            os.remove(tmp + suffix)
    shutil.copy2(REAL, tmp)

    con = db.connect(tmp)
    db.init_db(con)
    cols = {r[1] for r in con.execute("PRAGMA table_info(component)")}
    ck("旧库升上来之后 component.identity_key 补上了", "identity_key" in cols, True)
    n_keyless = con.execute(
        "SELECT COUNT(*) FROM component WHERE merged_into IS NULL"
        " AND COALESCE(identity_key,'')=''"
        " AND (COALESCE(value,'')<>'' OR COALESCE(package,'')<>''"
        "      OR COALESCE(mpn,'')<>'' OR COALESCE(lcsc_pn,'')<>'')").fetchone()[0]
    ck("凡是知道点什么的元件都补上了身份键", n_keyless, 0)
    n_leftover = con.execute(
        "SELECT COUNT(*) FROM component WHERE merged_into IS NULL"
        " AND (COALESCE(value,'')<>'' OR COALESCE(package,'')<>'')"
        " AND name = TRIM(COALESCE(value,'') || ' ' || COALESCE(package,''))"
    ).fetchone()[0]
    ck("没有哪个元件的名字还等于「值 空格 封装」(迁移真的跑过了)", n_leftover, 0)
    con.close()

    app = gui.App(tmp)
    app.update()
    app.update_idletasks()
    app.nb.select(app.tab_proj)
    app.update()
    pr = app.tab_proj
    pid = pr._pid
    ck("用户那个项目还在,而且默认选中了", bool(pid), True)
    if not pid:
        raise SystemExit("没有项目就没法验这一页,先手动建一个")

    print("\n--- 用户这张 BOM 上的名字长什么样 ---")
    for l in app.con.execute(
            "SELECT c.name, c.value, c.package, c.identity_key FROM project_bom b"
            " JOIN component c ON c.id=b.component_id WHERE b.project_id=?"
            " ORDER BY b.id LIMIT 5", (pid,)).fetchall():
        print(f"      name={l['name']!r}  value={l['value']!r}"
              f"  package={l['package']!r}  key={l['identity_key']!r}")

    # ================================================== #3 BOM 明细详情面板
    print("\n--- #3 BOM 明细:点一行右边出详情 ---")
    pr.sub.select(0)
    app.update()
    app.update_idletasks()
    ck("BOM 明细右边摆了一块详情面板",
       isinstance(getattr(pr, "detail", None), gui.LineDetail), True)
    d = pr.detail
    ck("还没点行时,面板叫人「先在左边点一行物料」",
       "先在左边点一行" in d.tip.get(), True)
    ck("面板真的画出来了(没有被挤成 1 像素)",
       d.winfo_ismapped() and d.winfo_width() > 150, True)
    print(f"     详情面板宽 {d.winfo_width()} px")

    kids = pr.t_bom.get_children()
    ck("用户的 BOM 明细有行", len(kids) > 0, True)
    bid = int(kids[0])
    pr.t_bom.selection_set(str(bid))
    app.update()
    ck("点了一行之后面板就认下了这条需求", d.line.get("bom_id"), bid)
    ck("面板说出了这条需求的单块用量",
       d.v["per_board"].get(), str(next(l for l in pr._lines.values()
                                        if l["bom_id"] == bid)["per_board"]))
    ck("面板取到了这颗元件", bool(d.comp.get("id")), True)
    ck("面板写着这颗料的值或封装(用户数据里总该有一个)",
       (d.v2["value"].get() != "—") or (d.v2["package"].get() != "—"), True)
    ck("面板写着现有多少", "个" in d.v2["on_hand_total"].get(), True)
    ck("面板写着在哪个仓位(或者明说一颗都没有)",
       (":" in d.v2["locs"].get()) or ("一颗都没有" in d.v2["locs"].get()), True)

    # 品类那格:第十一批起是**只读展示 + 「换…」逐级选**,不再是个能随便打字的框
    # (免得随手敲一个对不上品类树的名字出来)。所以这里断言的是**当前意图**:
    # 格子只读、面板自己写着改它要点「换…」。
    # 显示的内容取自品类树,挂在子类下就是**全路径** —— 只写顶层大类名的话,
    # 这块面板看着像「它就在大类下」,和库存菜单里看到的不是一回事。
    ck("品类那格是只读的(改它得走「换…」)",
       "readonly" in d.cb_cat.state(), True)
    ck("面板自己写着「品类点「换…」一级一级改」", "换…" in d.tip.get(), True)
    _node_label = d._cat_by_id.get(d.comp.get("category_id") or -1)
    ck("品类显示的就是品类树里的那一条(子类给全路径)",
       d.cat.get(), _node_label or (d.comp.get("category") or ""))
    ck("挂在子类下时是全路径(带「 / 」),不是拿顶层名冒充",
       (" / " in d.cat.get()) if (_node_label and " / " in _node_label)
       else (d.cat.get() == (d.comp.get("category") or "")), True)
    opts = d.category_options()
    ck("候选里带着用户库里已经在用的品类", bool(opts), True)
    print("     品类候选前几个:", opts[:6])

    cid = d.comp["id"]
    old_cat = d.comp.get("category") or ""
    d.cat.set("成品包体检品类")
    app.update()
    d.save_category()
    app.update()
    ck("改完立刻写进了库",
       app.con.execute("SELECT category FROM component WHERE id=?",
                       (cid,)).fetchone()[0], "成品包体检品类")
    ck("BOM 明细那一行也跟着变了",
       app.con.execute("SELECT c.category FROM project_bom b"
                       " JOIN component c ON c.id=b.component_id WHERE b.id=?",
                       (bid,)).fetchone()[0], "成品包体检品类")
    ck("面板自己也是新值", d.comp.get("category"), "成品包体检品类")
    ck("全量刷新之后面板还停在这条需求上,没跳回空白",
       d.line.get("bom_id"), bid)

    _rb = gui.messagebox
    fb = FakeBox()
    gui.messagebox = fb
    try:
        d.cat.set("   ")
        d.save_category()
        app.update()
    finally:
        gui.messagebox = _rb
    ck("品类不让填空(空着以后按品类搜不到它)", len(fb.infos) >= 1, True)
    ck("而且没真写进去",
       app.con.execute("SELECT category FROM component WHERE id=?",
                       (cid,)).fetchone()[0], "成品包体检品类")

    # 改回去,别把用户的品类留成体检的痕迹
    d.cat.set(old_cat)
    if not old_cat:
        d.cat.set("未分类")
    d.save_category()
    app.update()
    ck("改回原样了",
       app.con.execute("SELECT category FROM component WHERE id=?",
                       (cid,)).fetchone()[0], old_cat or "未分类")

    # ================================================== #2 只入库这一行
    print("\n--- #2 收料:只入库选中这一行 ---")
    pin = pr.pane_in
    pin.show_bom()
    pr.sub.select(pin)
    app.update()
    pin.set_project(pid)
    app.update()
    rp = pin.bom_form
    ck("工具栏上有「只入库这一行」这个按钮",
       gui.BomReceivePane.ONE in str(rp.btn_one.cget("text")), True)

    lines = rp.lines
    ck("用户的 BOM 至少有两行才验得出来「只收一行」", len(lines) >= 2, True)
    b0, c0 = lines[0]["bom_id"], lines[0]["component_id"]
    b1, c1 = lines[1]["bom_id"], lines[1]["component_id"]
    s0, s1 = stock_of(app.con, c0), stock_of(app.con, c1)

    # 先勾上第二行,再单收第一行 —— 第二行的勾必须留着
    rp.picked.add(b0)
    rp.picked.add(b1)
    rp.render()
    app.update()
    rp.tree.selection_set(str(b0))
    app.update()
    ck("选中之后按钮上写着选的是哪一行",
       bool(str(lines[0].get("name") or "")) and
       str(lines[0].get("name"))[:8] in str(rp.btn_one.cget("text")), True)
    ck("这时候它认得选中行", rp.selected_bid(), b0)

    rp.qty[b0] = 1
    _rb = gui.messagebox
    fb = FakeBox()
    gui.messagebox = fb
    try:
        rp.receive_selected()
        app.update()
    finally:
        gui.messagebox = _rb
    ck("动手前先念了一遍要收什么", len(fb.asks) >= 1, True)
    ck("选中那一行的库存真的多了 1", stock_of(app.con, c0), s0 + 1)
    ck("另一行一颗都没动", stock_of(app.con, c1), s1)
    ck("另一行的勾还在(单收不会顺手把别的勾清掉)", b1 in rp.picked, True)
    ck("收过的那一行的勾自己掉了", b0 in rp.picked, False)
    ck("流水记在这个项目名下",
       app.con.execute("SELECT COUNT(*) FROM movement WHERE project_id=? AND kind='IN'"
                       " AND bom_id=?", (pid, b0)).fetchone()[0] >= 1, True)

    # 右键菜单那几条入口确实挂上了
    labels = [rp.menu.entrycget(i, "label")
              for i in range(rp.menu.index("end") + 1)
              if rp.menu.type(i) == "command"]
    ck("右键菜单里有「只入库这一行」",
       any(gui.BomReceivePane.ONE in x for x in labels), True)
    ck("右键菜单里有勾选 / 取消勾选",
       any("勾选" in x for x in labels), True)

    # 批量那条路照旧
    rp.set_checked(True)
    app.update()
    ck("「勾选这一行」把它勾上了", b1 in rp.picked, True)
    _rb = gui.messagebox
    gui.messagebox = FakeBox()
    try:
        rp.submit()
        app.update()
    finally:
        gui.messagebox = _rb
    ck("批量入库还能用", stock_of(app.con, c1), s1 + int(lines[1]["need"]))

    app.destroy()
    try:
        app.con.close()
    except Exception:  # noqa: BLE001
        pass
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(tmp + suffix):
            os.remove(tmp + suffix)

    after = snapshot(REAL)
    ck("用户的真实数据库一个字节都没被动过", after, before)

    print("\n结果:" + ("成品包第四步 PASS" if not bad else f"FAIL {bad}"))
    sys.exit(1 if bad else 0)
except Exception:
    print("!! 失败:")
    traceback.print_exc()
    sys.exit(1)
