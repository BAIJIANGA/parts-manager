# -*- coding: utf-8 -*-
"""元器件物料管理 —— 桌面版(Tkinter)。

特点:
  * 纯本地原生窗口,不开端口、不连网络、不用浏览器
  * 零第三方依赖,只用 Python 标准库(含 tkinter)
  * 业务逻辑**直接复用 server.py 里的处理函数** —— 那些函数只依赖一个合成出来的
    Ctx(query/body/upload/con),完全不碰 HTTP,所以桌面版和网页版的语义逐字一致,
    不存在「两套实现慢慢走偏」的问题。

启动:  pythonw app/gui.py         (无控制台窗口)
       python  app/gui.py         (带控制台,方便看报错)
"""
from __future__ import annotations

import csv
import os
import re
import sqlite3
import sys
import traceback
import tkinter as tk
# colorchooser 是标准库自带的取色器(系统调色板),不引第三方库 ——
# 「Excel 那种改颜色」要的就是一个能点着选的色盘
from tkinter import colorchooser, filedialog, messagebox, ttk

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import attrs       # noqa: E402  ← 属性名的建议、归一化和显示格式
import bom         # noqa: E402  ← 事后重算「品类是不是猜的」,见 bom_guess
import db          # noqa: E402
import footprint   # noqa: E402  ← 标准封装识别:C0805 / R0603 / L0402 归到同一个尺寸
import server      # noqa: E402  ← 复用全部业务逻辑
import values      # noqa: E402  ← 值区间筛选要把 1k 这种写法解析成数值

DEFAULT_DB = os.path.join(server.PROJECT_ROOT, "data", "parts.db")
ICON_PATH = os.path.join(server.PROJECT_ROOT, "app", "static", "app.ico")
BACKUP_DIR = os.path.join(server.PROJECT_ROOT, "data", "backups")


def pkg_groups(pkgs):
    """把一堆封装写法按尺寸归并:[(显示标签, 筛选用值, 原始写法列表)]。

    归类规则只有 footprint 一套 —— 菜单、筛选、身份键必须用同一套,否则会出现
    「菜单里合成一个、匹配时算两个」这种自相矛盾。C0805 和 0805 点哪一个都该
    看到同一批料;1005 和 0402 也是(同一个 0402,两种叫法)。

    显示标签用**能认出尺寸时的尺寸**(认不出就原样),所以按钮上看到的是 0805
    这种短标签,而不是被截断的一长串。用户存进去的字一个都不改。
    """
    out, at = [], {}
    for p in pkgs:
        p = str(p or "").strip()
        if not p:
            continue
        key = footprint.canon(p)
        if key not in at:
            at[key] = {"label": footprint.label(p) or p, "key": key, "raws": []}
            out.append(at[key])
            at[key]["raws"].append(p)
        else:
            at[key]["raws"].append(p)
    return [(g["label"], g["key"], g["raws"]) for g in out]

KIND_LABEL = {"IN": "入库", "OUT": "出库", "ADJUST": "盘点", "TRANSFER": "移库"}
# 查重的三档依据。前两档是硬证据,第三档只是可疑 —— 界面上要能看出这个区别,
# 免得把「值封装一样」也当成「肯定是同一个东西」直接合掉。
REASON_LABEL = {"mpn": "料号相同", "name": "名称相同", "vf": "值+封装相同"}
STATE_LABEL = dict(server.STATE_LABEL)   # 跟后端共用一份口径,别各写一份
FONT = ("Microsoft YaHei UI", 9)


def readable_fg(bg):
    """在这块底色上该用黑字还是白字(#34 的行调色盘)。

    色块按钮**自己就显示当前颜色**,底色可能是深蓝也可能是白 —— 文字色写死黑的话,
    深底上会糊成一团,那就白折腾了。按感知亮度(0.299R + 0.587G + 0.114B)分个档够用,
    不必上 WCAG 那套对比度公式。认不出的颜色返回空串 = 让 Tk 用默认字色。
    """
    s = str(bg or "").strip().lstrip("#")
    if len(s) != 6:
        return ""
    try:
        r, g, b = (int(s[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return ""
    return "#ffffff" if (0.299 * r + 0.587 * g + 0.114 * b) < 140 else "#000000"


# --------------------------------------------------------------------- 复用后端

class _Match:
    """顶替正则匹配对象,让带路径参数的 handler 也能直接调用。

    注意 group(1) 对应的是**第一个捕获组**,而正则里 group(0) 是整个匹配串,
    所以这里要减 1 —— 所有 handler 都只用 m.group(1)。
    """

    def __init__(self, *groups):
        self._groups = groups

    def group(self, i):
        return self._groups[i - 1]


def make_ctx(con, query=None, body=None, upload=None):
    return server.Ctx(None, con, dict(query or {}), dict(body or {}), upload)


def call(con, fn, query=None, body=None, upload=None, match=None, parent=None, quiet=False):
    """调用一个后端 handler,统一处理 ApiError。

    成功返回 payload(dict);失败弹提示并返回 None。
    """
    ctx = make_ctx(con, query, body, upload)
    try:
        _status, payload = fn(ctx, _Match(*match) if match else None)
        return payload
    except server.ApiError as exc:
        if not quiet:
            messagebox.showwarning("操作未完成", exc.message, parent=parent)
        return None
    except sqlite3.IntegrityError as exc:
        if not quiet:
            messagebox.showwarning("数据冲突", f"违反唯一约束:{exc}", parent=parent)
        return None
    except Exception as exc:  # noqa: BLE001 —— 界面层要兜住一切,不能白屏
        if not quiet:
            messagebox.showerror("出错了", f"{type(exc).__name__}: {exc}", parent=parent)
        traceback.print_exc()
        return None


# --------------------------------------------------------------------- 小工具

def clear_tree(tree):
    children = tree.get_children()
    if children:
        tree.delete(*children)


# 手动做出的入库/出库,在备注前面盖这个章。流水表没有 source 列,手动单笔的
# project_id / bom_id / purchase_id 全是空的,事后只能靠「三个 id 都为空」反推,
# 说不清这条到底是手动做的、还是别的什么没关联的动作。
MANUAL_TAG = "手动"


def manual_note(body):
    """给手动入口产生的流水盖上来源章(已经盖过就不重复盖)。"""
    note = str(body.get("note") or "").strip()
    if note.startswith(MANUAL_TAG):
        return body
    body["note"] = f"{MANUAL_TAG} {note}".strip() if note else MANUAL_TAG
    return body


def col_index(columns, key):
    """按列名找列号。

    撤销时要读某一笔流水的单元格来写确认文案。写死 values[3] 的话,以后给
    表加一列(比如 #30 把「项目」列换成组行)就全部串位 —— 串位之后不少断言
    **还是通过的**,只是它验的已经不是原来那件事了,那比直接失败更糟。
    """
    return [c[0] for c in columns].index(key)


def make_tree(parent, columns, height=14, show="headings"):
    """columns: [(key, 标题, 宽度[, 是否伸缩]), ...]  返回 (frame, tree)

    默认 show="headings" —— 不显示 #0 树列,也就没有展开三角。这个界面里
    大多数列表都是平的:分类用卡片进二级页,列表本身不需要折叠。

    「按 BOM 出库」和「流水」这几张表例外,它们传 show="tree headings":
    一条 BOM 需求下面要挂几颗能凑它的库存料,一个项目下面要挂它那些流水,
    必须能折叠,否则会摊成一张看不出层次的几十行大表(见 #30)。

    **对齐(#27):表头和单元格一律居中。** 列定义里历史上第 4 项是「这一列的
    对齐方式」,调用点写了 w / center / e 各不相同的值,于是同一张表里左中右
    混排。用户明确要求「所有的都要居中」,所以这一项从 #27 起被忽略 —— 老调用
    点还带着它(是字符串),新写的列定义第 4 项留空即可。

    第 4 项腾出来之后顺便当伸缩开关用:给 bool 就是「这一列是否随窗口伸缩」,
    不写就按老规矩(文字类的 name / note 才伸缩)。列宽和 stretch 的行为完全
    没变 —— 这次只动对齐。
    """
    frame = ttk.Frame(parent)
    keys = [c[0] for c in columns]
    tree = ttk.Treeview(frame, columns=keys, height=height, show=show)
    for col in columns:
        # 第 3 项之后只取宽度:对齐不再按列给,一律居中
        key, title, width = col[:3]
        if len(col) > 4:
            # 老写法:(键, 标题, 宽度, 对齐, 是否伸缩)
            stretch = col[4]
        elif len(col) == 4 and isinstance(col[3], bool):
            # 新写法:(键, 标题, 宽度, 是否伸缩)
            stretch = col[3]
        else:
            stretch = key in ("name", "note")
        # 表头也要显式居中:ttk 的表头默认跟随主题,不写死的话换个主题就偏了
        tree.heading(key, text=title, anchor="center")
        tree.column(key, width=width, anchor="center", stretch=stretch)
    vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    hsb = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
    tree.grid(row=0, column=0, sticky="nsew")
    vsb.grid(row=0, column=1, sticky="ns")
    hsb.grid(row=1, column=0, sticky="ew")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    return frame, tree


def ask_text(parent, title, prompt, initial=""):
    dlg = _TextDialog(parent, title, prompt, initial)
    parent.wait_window(dlg)
    return dlg.result


class _TextDialog(tk.Toplevel):
    def __init__(self, parent, title, prompt, initial=""):
        super().__init__(parent)
        self.title(title)
        self.resizable(False, False)
        self.result = None
        self.transient(parent)
        ttk.Label(self, text=prompt).grid(row=0, column=0, columnspan=2, padx=12, pady=(12, 4), sticky="w")
        self.var = tk.StringVar(value=initial)
        entry = ttk.Entry(self, textvariable=self.var, width=44)
        entry.grid(row=1, column=0, columnspan=2, padx=12, sticky="ew")
        entry.focus_set()
        entry.select_range(0, "end")
        btns = ttk.Frame(self)
        btns.grid(row=2, column=0, columnspan=2, pady=12)
        ttk.Button(btns, text="确定", command=self._ok, width=10).grid(row=0, column=0, padx=4)
        ttk.Button(btns, text="取消", command=self.destroy, width=10).grid(row=0, column=1, padx=4)
        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()

    def _ok(self):
        self.result = self.var.get().strip()
        self.destroy()


# --------------------------------------------------------------------- 主窗口

class App(tk.Tk):
    def __init__(self, db_path):
        super().__init__()
        self.db_path = db_path
        self.con = db.connect(db_path)
        db.init_db(self.con)
        # 老库升上来时 value_num 还是空的,补一次,排序/筛选才立刻可用
        db.backfill_values(self.con)

        self.title("元器件物料管理")
        self.geometry("1360x830")
        self.minsize(1060, 640)
        self._setup_style()
        self._set_icon()
        self._build_menu()

        self.status = tk.StringVar(value="就绪")
        bar = ttk.Frame(self, relief="groove", padding=(8, 4))
        bar.pack(side="bottom", fill="x")
        ttk.Label(bar, textvariable=self.status).pack(side="left")

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=6, pady=(6, 0))

        # 页签顺序按使用动线排:看全局 → 我有什么 → 动库存 → 要做什么 → 要买什么
        #              → 东西放哪 → 流水账
        self.tab_dash = DashboardTab(self.nb, self)
        self.tab_comp = ComponentsTab(self.nb, self)
        self.tab_stock = StockTab(self.nb, self)
        self.tab_proj = ProjectsTab(self.nb, self)
        self.tab_purchase = PurchaseTab(self.nb, self)
        self.tab_loc = LocationsTab(self.nb, self)
        self.tab_move = MovementsTab(self.nb, self)
        self._tabs = {"dash": self.tab_dash, "comp": self.tab_comp, "stock": self.tab_stock,
                      "proj": self.tab_proj, "purchase": self.tab_purchase,
                      "loc": self.tab_loc, "move": self.tab_move}
        for tab, label in ((self.tab_dash, "  总览  "),
                           (self.tab_comp, "  库存  "),
                           (self.tab_stock, "  出入库  "),
                           (self.tab_proj, "  项目 BOM  "),
                           (self.tab_purchase, "  采购  "),
                           (self.tab_loc, "  仓位  "),
                           (self.tab_move, "  流水  ")):
            self.nb.add(tab, text=label)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.refresh_all()

    # ---------------------------------------------------------- 外观

    def _setup_style(self):
        try:
            ttk.Style().theme_use("vista")
        except tk.TclError:
            pass
        st = ttk.Style()
        st.configure(".", font=FONT)
        st.configure("Treeview", font=FONT, rowheight=23)
        st.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
        st.configure("Big.TLabel", font=("Microsoft YaHei UI", 11, "bold"))
        st.configure("H1.TLabel", font=("Microsoft YaHei UI", 15, "bold"))
        st.configure("Dim.TLabel", foreground="#666")

    def _set_icon(self):
        for p in (ICON_PATH, os.path.join(BASE_DIR, "static", "app.ico")):
            if os.path.isfile(p):
                try:
                    self.iconbitmap(p)
                    return
                except tk.TclError:
                    pass

    def _build_menu(self):
        menubar = tk.Menu(self)

        m_file = tk.Menu(menubar, tearoff=0)
        m_file.add_command(label="备份数据库…", command=self.backup_now)
        m_file.add_command(label="打开数据目录", command=self.open_data_dir)
        m_file.add_separator()
        m_file.add_command(label="退出", command=self._on_close)
        menubar.add_cascade(label="文件", menu=m_file)

        m_tool = tk.Menu(menubar, tearoff=0)
        m_tool.add_command(label="⚡ 快速入库…", accelerator="Ctrl+I", command=self.quick_in)
        m_tool.add_command(label="📋 批量入库…", accelerator="Ctrl+B", command=self.batch_in)
        # 出库和入库共用同一个粘贴框:一堆料一次领走,不用一颗一颗点右键
        m_tool.add_command(label="📤 批量出库…", accelerator="Ctrl+Shift+B",
                           command=lambda: self.batch_in(out=True))
        m_tool.add_separator()
        m_tool.add_command(label="↶ 撤销上一次出入库", accelerator="Ctrl+Z",
                           command=self.undo_last)
        m_tool.add_command(label="🔍 查重与合并…", command=self.dedupe)
        m_tool.add_separator()
        m_tool.add_command(label="按流水重建库存余额(校验)", command=self.rebuild_stock)
        m_tool.add_command(label="导出元件清单 CSV…", command=self.export_components)
        m_tool.add_separator()
        m_tool.add_command(label="刷新全部", accelerator="F5", command=self.refresh_all)
        menubar.add_cascade(label="工具", menu=m_tool)

        m_help = tk.Menu(menubar, tearoff=0)
        m_help.add_command(label="关于", command=self.about)
        menubar.add_cascade(label="帮助", menu=m_help)

        self.config(menu=menubar)
        # 收货时手上可能还拿着袋子,让快捷键能一键到位
        self.bind_all("<Control-i>", lambda _e: self.quick_in())
        self.bind_all("<Control-b>", lambda _e: self.batch_in())
        self.bind_all("<Control-B>", lambda _e: self.batch_in(out=True))
        self.bind_all("<Control-z>", lambda _e: self.undo_last())
        self.bind_all("<F5>", lambda _e: self.refresh_all())

    def quick_in(self):
        dlg = QuickInDialog(self, self)
        self.wait_window(dlg)
        if dlg.done:
            self.refresh_all()

    def batch_in(self, out=False):
        """批量入库/出库。out=True 打开的是出库那一版(同一套粘贴框)。"""
        dlg = BatchInDialog(self, self, kind="OUT" if out else "IN")
        self.wait_window(dlg)
        if dlg.done:
            self.refresh_all()

    def dedupe(self):
        dlg = DedupeDialog(self, self)
        self.wait_window(dlg)

    def undo_last(self):
        """撤销最近一笔。录错了当场按 Ctrl+Z 就能退回去 ——
        正因为「收货即录入」,录错才反而更容易发生,所以得留一条退路。"""
        w = self.focus_get()
        # 正在文本框里打字时,Ctrl+Z 归文本框自己管(它有自己的撤销),
        # bind_all 是全局的,不挡一下连批量入库的输入框都会弹这个窗
        if isinstance(w, (tk.Text, ttk.Entry, tk.Entry)):
            try:
                w.event_generate("<<Undo>>")
            except tk.TclError:
                pass
            return
        got = call(self.con, server.last_movement, quiet=True) or {}
        mv = got.get("movement")
        if not mv:
            messagebox.showinfo("没有可撤销的", "流水里没有还能撤销的记录。", parent=self)
            return
        where = mv.get("location_code") or "—"
        if mv.get("to_location_code"):
            where += f" → {mv['to_location_code']}"
        label = (f"{mv['kind_label']}  {mv['component_name']}  {mv['qty']} 个  {where}"
                 f"\n{(mv.get('created_at') or '')[:19]}")
        if mv.get("note"):
            label += f"\n{mv['note']}"
        undo_movement(self, self, mv["id"], label)

    # ---------------------------------------------------------- 数据

    def refresh_all(self):
        for tab in self._tabs.values():
            try:
                tab.reload()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
        self.refresh_status()

    def goto_tab(self, name):
        """切到某个页签并刷新它。总览页的「去采购页」这类跳转用它。"""
        tab = self._tabs.get(name)
        if tab is None:
            return
        try:
            self.nb.select(tab)
            tab.reload()
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    def refresh_status(self):
        # 有临时消息在显示时不要覆盖它。这条判断是必要的:set_status 之后通常还会
        # refresh_all(),而 refresh_all 结尾就是 refresh_status —— 不挡一下的话
        # 「已入库:xxx × 3」「批量入库完成」这类刚给用户的反馈会立刻被统计数字顶掉,
        # 用户根本来不及看见。_status_hold 归 0 后由定时器把统计数字放回来。
        if getattr(self, "_status_hold", 0):
            return
        s = call(self.con, server.summary, quiet=True)
        if not s:
            self.status.set("统计读取失败")
            return
        self.status.set(
            f"元件 {s['components']} 种   总库存 {s['total_qty']} 个   "
            f"缺货 {s['out']} 种   缺料 {s['low']} 种   项目 {s['projects']} 个"
        )

    def set_status(self, text, seconds=4):
        """在状态栏留一句话,seconds 秒后自动换回统计数字。"""
        self._status_hold = getattr(self, "_status_hold", 0) + 1
        hold = self._status_hold
        self.status.set(text)
        self.after(int(seconds * 1000), lambda: self._status_release(hold))

    def _status_release(self, hold):
        # 期间又来了更新的临时消息,就让那一条的定时器负责恢复
        if hold != getattr(self, "_status_hold", 0):
            return
        self._status_hold = 0
        self.refresh_status()

    # ---------------------------------------------------------- 动作

    def backup_now(self):
        import shutil
        import datetime as _dt
        os.makedirs(BACKUP_DIR, exist_ok=True)
        stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = os.path.join(BACKUP_DIR, f"parts_{stamp}.db")
        try:
            self.con.commit()
            shutil.copy2(self.db_path, dest)
        except OSError as exc:
            messagebox.showerror("备份失败", str(exc), parent=self)
            return
        messagebox.showinfo("备份完成", f"已保存到:\n{dest}", parent=self)

    def open_data_dir(self):
        folder = os.path.dirname(self.db_path)
        os.makedirs(folder, exist_ok=True)
        try:
            os.startfile(folder)  # noqa: S606 —— Windows 专用,打开资源管理器
        except OSError as exc:
            messagebox.showerror("打不开", str(exc), parent=self)

    def rebuild_stock(self):
        if not messagebox.askyesno(
                "重建库存余额",
                "将按出入库流水的时间顺序重新计算所有库存余额。\n\n"
                "流水是原始记录,余额是它的计算结果 —— 这个操作只是重算,不会改流水。\n"
                "结束后会报告重算前后的差异(差异应为 0)。\n\n继续吗?", parent=self):
            return
        rep = call(self.con, server.rebuild, parent=self)
        if not rep:
            return
        n = rep["diff_count"]
        if n == 0:
            messagebox.showinfo("校验通过", "重算结果与现状完全一致,差异 0 处。", parent=self)
        else:
            detail = "\n".join(
                f"  元件 {d['component_id']} 仓位 {d['location_id']}:{d['before']} → {d['after']}"
                for d in rep["differences"][:20])
            messagebox.showwarning("发现差异",
                                   f"共 {n} 处与现状不一致,已按流水修正:\n\n{detail}", parent=self)
        self.refresh_all()

    def export_components(self):
        data = call(self.con, server.list_components, parent=self)
        if not data:
            return
        path = filedialog.asksaveasfilename(
            parent=self, title="导出元件清单", defaultextension=".csv",
            initialfile="元器件清单.csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        cols = [("name", "名称"), ("lcsc_pn", "商品编号"), ("mpn", "厂家料号"),
                ("manufacturer", "厂家"), ("category", "品类"), ("package", "封装"),
                ("value", "值"), ("on_hand", "库存"), ("min_stock", "最低库存"),
                ("unit", "单位"), ("note", "备注")]
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow([c[1] for c in cols])
                for it in data["items"]:
                    w.writerow([it.get(c[0], "") for c in cols])
        except OSError as exc:
            messagebox.showerror("导出失败", str(exc), parent=self)
            return
        messagebox.showinfo("导出完成", f"共 {len(data['items'])} 条:\n{path}", parent=self)

    def about(self):
        messagebox.showinfo(
            "关于",
            "元器件物料管理系统 —— 桌面版\n\n"
            "本地原生窗口,不开端口、不连网络。\n"
            "数据全部存放在 data/parts.db 这一个文件里。\n\n"
            f"数据库:{self.db_path}\n"
            f"Python:{sys.version.split()[0]}",
            parent=self)

    def _on_close(self):
        try:
            self.con.commit()
            self.con.close()
        except Exception:  # noqa: BLE001
            pass
        self.destroy()


# --------------------------------------------------------------------- 卡片区

# 大类卡片上的字母标识与配色。字母沿用电子行业画原理图时的位号习惯
# (R / C / L / J / Y …),比抽象图标更容易一眼认出来;颜色只用来区分,
# 不承载别的含义。
CATEGORY_STYLE = {
    "电阻":       ("R",   "#2d6cdf"),
    "电容":       ("C",   "#c0392b"),
    "电感":       ("L",   "#8e44ad"),
    "磁珠":       ("FB",  "#16a085"),
    "二极管":     ("D",   "#d35400"),
    "三极管/MOS": ("Q",   "#27ae60"),
    "发光二极管": ("LED", "#e67e22"),
    "芯片/IC":    ("IC",  "#34495e"),
    "连接器":     ("J",   "#7f8c8d"),
    "晶振":       ("Y",   "#2980b9"),
    "开关":       ("SW",  "#c2185b"),
    "传感器":     ("S",   "#00838f"),
    "保险丝":     ("F",   "#f39c12"),
    "电位器":     ("RP",  "#5d4037"),
    "继电器":     ("K",   "#455a64"),
    "其他":       ("…",   "#607d8b"),
}
UNCATEGORIZED = "未分类"
# 卡片上的字母/颜色只是一张**显示用**的偏好表(见 _cat_rank):常见元件类排前面,
# 自定义品类按名字排后面。它**不再是卡片名单** —— 首页列哪几张卡片完全由库里的
# 品类树决定。以前那张写死的 16 个标准大类名单会画出「库里根本没有对应品类行」
# 的空卡片,而那种卡片点「删除…」删不掉(没有任何一行可删),正是 issue #32 里
# 用户说的「有东西删不掉」。品类属于用户,名单不许写死。
CATEGORY_ORDER = list(CATEGORY_STYLE)
DEFAULT_CAT_STYLE = ("•", "#7f8c8d")

HOME_BG = "#eef1f5"          # 卡片区的浅灰底,衬托白卡片
CARD_BG = "#ffffff"
CARD_BG_HOVER = "#e8f1ff"
CARD_EDGE = "#d5dbe3"
CARD_EDGE_HOVER = "#2d6cdf"
CARD_W = 220                 # 卡片尺寸;一行放几张按窗口宽度算,不写死
CARD_H = 88
CARD_GAP = 16
DIM_BADGE = "#b9c0c9"        # 空的大类:标识和文字都变灰,一眼看出没东西
DIM_TITLE = "#9aa3af"
PROJECT_COLORS = ["#3d6b9e", "#0b6e4f", "#8e5a2b", "#6a4c93",
                  "#a33b5b", "#2b7a78", "#7a5c2b", "#4a5568"]


class CardBoard(ttk.Frame):
    """一格一格的卡片区,可滚动、按宽度自动决定一行摆几张。

    库存首页的大类和出入库的项目页都用它,所以那张「卡片尺寸必须等于设定值」
    的坑只需要在这里躲一次。
    """

    def __init__(self, parent, on_pick, on_menu=None):
        super().__init__(parent)
        self.on_pick = on_pick
        # 右键回调(可选)。库存菜单用它做「就地增删这一级」;
        # 项目页不给,那边右键有自己的意思。
        self.on_menu = on_menu
        self.cards = {}          # key -> 卡片控件
        self._seq = []           # 显示顺序,重排时用
        self._cols = 0

        wrap = tk.Frame(self, bg=HOME_BG, highlightbackground=CARD_EDGE,
                        highlightthickness=1)
        wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(wrap, bg=HOME_BG, highlightthickness=0)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.area = tk.Frame(self.canvas, bg=HOME_BG)
        self._win = self.canvas.create_window((0, 0), window=self.area, anchor="nw")
        self.area.bind("<Configure>", lambda _e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self._on_resize)

    def _on_resize(self, event):
        self.canvas.itemconfigure(self._win, width=event.width)
        self.reflow()

    def render(self, specs, empty_text="没有可显示的内容。"):
        """specs: [(key, 标题, 字母标识, 颜色, 副标题, 是否置灰), ...]"""
        for w in self.area.winfo_children():
            w.destroy()
        self.cards = {}
        # 这一句是必须的:上面刚把旧卡片 destroy() 掉,列表里留的还是那些死控件,
        # 重排时对它们调 .grid() 会抛 TclError("bad window path name"),
        # 把整个 reload() 打断 —— 表现就是入库之后首页一片空白。
        self._seq = []
        self._cols = 0
        if not specs:
            tk.Label(self.area, bg=HOME_BG, fg="#98a2b3", justify="left",
                     font=("Microsoft YaHei UI", 10), text=empty_text
                     ).grid(row=0, column=0, sticky="w", padx=28, pady=40)
            return
        for key, title, glyph, color, sub, dim in specs:
            card = self._make_card(key, title, glyph, color, sub, dim)
            self.cards[key] = card
            self._seq.append(card)
        self.reflow()

    def reflow(self):
        """按当前宽度决定一行摆几张卡片 —— 窗口拉宽就多摆几张,不留一大片空白。"""
        if not self._seq:
            return
        avail = self.canvas.winfo_width()
        if avail <= 1:
            return
        cols = max(1, (avail - CARD_GAP) // (CARD_W + CARD_GAP))
        if cols == self._cols:
            return
        self._cols = cols
        for i, card in enumerate(self._seq):
            if not card.winfo_exists():     # 兜底:死控件跳过,不要让整次刷新陪葬
                continue
            card.grid(row=i // cols, column=i % cols,
                      padx=CARD_GAP // 2, pady=CARD_GAP // 2, sticky="nw")

    def _make_card(self, key, title, glyph, color, sub, dim):
        badge_bg = DIM_BADGE if dim else color
        title_fg = DIM_TITLE if dim else "#1f2937"
        card = tk.Frame(self.area, bg=CARD_BG, highlightbackground=CARD_EDGE,
                        highlightthickness=1, cursor="hand2",
                        width=CARD_W, height=CARD_H)
        # 子控件是用 pack 摆的,必须关掉 pack_propagate —— 关 grid_propagate 没用,
        # 卡片会被内容撑成 130x72 而不是设定的尺寸。
        card.pack_propagate(False)
        card.grid_propagate(False)

        badge = tk.Label(card, text=glyph, bg=badge_bg, fg="white", width=4, height=2,
                         font=("Microsoft YaHei UI", 10, "bold"))
        badge.pack(side="left", padx=(14, 12))

        body = tk.Frame(card, bg=CARD_BG)
        body.pack(side="left", fill="both", expand=True)
        lbl = tk.Label(body, text=title, bg=CARD_BG, fg=title_fg, anchor="w",
                       font=("Microsoft YaHei UI", 12, "bold"))
        lbl.pack(anchor="w", pady=(18, 0))
        hint = tk.Label(body, text=sub, bg=CARD_BG, fg="#98a2b3", anchor="w",
                        font=("Microsoft YaHei UI", 8))
        hint.pack(anchor="w")

        # 子控件会吃掉点击,所以逐个绑;悬停时整张卡片一起变色
        parts = (card, badge, body, lbl, hint)
        for w in parts:
            w.bind("<Button-1>", lambda _e, k=key: self.on_pick(k))
            if self.on_menu is not None:
                # 右键 = 改这一级(加子类 / 改名 / 删除)。左键照旧往下钻,
                # 多一个右键不改变原来的手感。
                w.bind("<Button-3>", lambda e, k=key: self.on_menu(k, e))
            w.bind("<Enter>", lambda _e, ws=parts: self._tint(ws, CARD_BG_HOVER))
            w.bind("<Leave>", lambda _e, ws=parts: self._tint(ws, CARD_BG))
        card.bind("<Enter>", lambda _e: card.configure(highlightbackground=CARD_EDGE_HOVER),
                  add="+")
        card.bind("<Leave>", lambda _e: card.configure(highlightbackground=CARD_EDGE),
                  add="+")
        return card

    @staticmethod
    def _tint(parts, bg):
        """整张卡片换底色;badge(parts[1])保持自己的颜色不动。"""
        for i, w in enumerate(parts):
            if i == 1:
                continue
            try:
                w.configure(bg=bg)
            except tk.TclError:
                pass


# --------------------------------------------------------------------- 元件库存

class ComponentsTab(ttk.Frame):
    """元件库存。

    首页固定列出 16 个标准大类(电阻 / 电容 / 电感 …),点一张卡片进入**独立的
    二级页面**看具体型号 —— 不是树形展开,首页也没有那个「加号」。

    但二级页面里**只列有库存的元件**:库存为 0 的元件在这一页不出现,点进去就是
    空的。因为导入 BOM 只是把「这块板子要用到什么」记下来,东西还没买回来,那属于
    「要买什么」,不是「已经有什么」。库存在「出入库」里入进来之后才会显示。

    项目视角也不在这里 —— 项目 BOM 回答的是「这批料是给谁配的」,和库存是两本账,
    混在一棵树里只会让人分不清。项目在「项目 BOM」页里单独看。
    """

    def __init__(self, parent, app: App):
        super().__init__(parent, padding=8)
        self.app = app
        self.con = app.con

        self._all = []            # 有库存的元件(全量)
        self._cat_data = {}       # 大类名 -> 有库存的元件
        self._rows = {}           # 元件 id -> 行数据
        self._card_names = []     # 首页要列的大类
        self.view = "home"        # home / cat / search
        self.current_category = None

        self.holder = ttk.Frame(self)
        self.holder.pack(fill="both", expand=True)
        self.page_home = ttk.Frame(self.holder)
        self.page_cat = ttk.Frame(self.holder)
        self.page_pick = ttk.Frame(self.holder)     # 中间页:选下一级子类

        # 面包屑放在 holder **上面**,三个页面共用同一条 —— 切页时它不该跟着消失,
        # 而且它是「我在树的哪一层」唯一持续可见的答案。
        self.crumb_bar = ttk.Frame(self)
        self.crumb = []            # [{"kind":"cat","id":..,"name":..}, {"kind":"pkg",...}]
        self._cat_flat = {}        # 品类 id -> 节点(带 children / own / total / path)
        self._uncat_ids = set()    # 树上「未分类」那一支的 id(见 _compute_uncat_ids)
        self._pick_items = []      # 中间页当前列出的子类
        self._own_only = False     # 列表只显示「直接挂在这一级」的元件(中间页那张「本级」)
        self.pkg_chips = {}        # 封装芯片,自检要数它们

        self._build_home()
        self._build_cat()
        self._build_pick()
        # 列设置:None = 默认(基础列 + 自动挑两个属性);列表 = 用户自己安排好的
        # 顺序,元素形如 "value" 或 "@耐压"
        self._want_cols = None
        self._slot_attr = {}       # a1..a12 -> 属性名
        # 用户给行配的颜色:元件 id 的字符串 -> {"fg": 颜色|None, "bg": 颜色|None}。
        # 用元件 id 当键(不是行号、不是表格 iid):换页面、换筛选之后还是同一行。
        self._row_colors = {}

        # 这三样得在 _load_cols() **之前**备好:它是把文件里的设置写进这几个
        # 状态位的。原来这几行排在 _load_cols() 之后,读回来的设置转头就被
        # None 覆盖 —— 「列设置能记住」其实只在同一次会话里成立,重开软件全丢。
        self._load_cols()          # 上次勾的列设置,得在建第一屏之前读到
        self.go_home()

    # ---- 给自检用的快捷入口
    @property
    def cards(self):
        return self.board.cards

    @property
    def card_area(self):
        return self.board.area

    # ------------------------------------------------------ 页面:大类卡片

    def _build_home(self):
        head = ttk.Frame(self.page_home)
        head.pack(fill="x", pady=(0, 8))
        ttk.Label(head, text="元件库存", style="H1.TLabel").pack(side="left")
        self.count = tk.StringVar()
        ttk.Label(head, textvariable=self.count, style="Dim.TLabel").pack(side="left", padx=12)
        # 「快速入库」放在最显眼的位置:收货那一刻就该把东西记下来,
        # 而不是等攒了一堆之后再补录 —— 补录是这类系统最常见的死法
        ttk.Button(head, text="⚡ 快速入库", command=self.quick_in).pack(side="right")
        ttk.Button(head, text="📋 批量入库", command=self.batch_in).pack(side="right", padx=6)
        ttk.Button(head, text="🔍 查重", command=self.app.dedupe).pack(side="right", padx=6)
        # 首页也能直接加大类 —— 「不要只在一级菜单上面做品类管理」的意思是
        # 每一级都能就地改,不是要把首页那个入口拿掉
        ttk.Button(head, text="＋ 新增大类", command=self.add_root_cat).pack(
            side="right", padx=6)
        # 品类管理放在首页顶栏:想看整棵树、想把某一支挪个位置时用它
        ttk.Button(head, text="🗂 品类管理", command=self.manage_categories).pack(
            side="right", padx=6)
        ttk.Button(head, text="＋ 新增元件", command=self.add).pack(side="right", padx=6)
        ttk.Button(head, text="刷新", command=self.reload).pack(side="right", padx=6)

        sbox = ttk.Frame(self.page_home)
        sbox.pack(fill="x", pady=(0, 10))
        ttk.Label(sbox, text="搜索").pack(side="left", padx=(0, 4))
        self.q = tk.StringVar()
        self.ent_q = ttk.Entry(sbox, textvariable=self.q, width=30)
        self.ent_q.pack(side="left")
        self.ent_q.bind("<Return>", lambda _e: self.open_search())
        ttk.Button(sbox, text="搜索", command=self.open_search).pack(side="left", padx=6)
        ttk.Label(sbox, text="名称 / 商品编号 / 料号 / 封装 / 值 / 丝印 / 参数 / 备注",
                  style="Dim.TLabel").pack(side="left", padx=6)

        self.board = CardBoard(self.page_home, self.open_category, self._cat_menu)
        self.board.pack(fill="both", expand=True)

    # ------------------------------------------------------ 页面:某个大类

    def _build_cat(self):
        # 显示零库存:默认开。刚加的料库存是 0,不显示的话用户会以为没加进去(#22)。
        # 必须在这里建:__init__ 是先调 _build_cat、后设其余状态位的
        self.zero_stock = tk.BooleanVar(value=True)
        head = ttk.Frame(self.page_cat)
        head.pack(fill="x", pady=(0, 8))
        # 上一层而不是直接回首页:人在第三层的时候,想退的是第二层
        ttk.Button(head, text="← 上一层", command=self.go_back, width=9).pack(
            side="left", padx=(0, 10))
        self.cat_badge = tk.Label(head, text="", bg="#7f8c8d", fg="white", width=4,
                                  font=("Microsoft YaHei UI", 10, "bold"))
        self.cat_badge.pack(side="left", padx=(0, 8))
        self.cat_title = tk.StringVar()
        ttk.Label(head, textvariable=self.cat_title, style="H1.TLabel").pack(side="left")
        self.cat_count = tk.StringVar()
        ttk.Label(head, textvariable=self.cat_count, style="Dim.TLabel").pack(side="left",
                                                                             padx=12)
        ttk.Button(head, text="＋ 新增元件", command=self.add).pack(side="right")
        ttk.Button(head, text="编辑", command=self.edit).pack(side="right", padx=6)
        ttk.Button(head, text="删除", command=self.delete).pack(side="right")
        # 人已经站在这一级上了,加子类不该逼他退回首页再找位置
        ttk.Button(head, text="＋ 新建子类", command=self.add_sub_here).pack(
            side="right", padx=6)
        ttk.Button(head, text="列…", command=self.pick_columns).pack(
            side="right")

        # ---- 行调色盘(#34)。为什么另起一排,而不是塞进 head 那一排:
        # head 那排已经有「← 上一层 / 类别 / 标题 / 计数 / ＋新增元件 / 编辑 /
        # 删除 / ＋新建子类 / 列…」,再加三个按钮在小窗口下会把标题挤没;
        # 而这一排是**每次刷色都要点**的东西,得离表格近、看得见。
        # 用户原话(第二次很明确了):「表格颜色改成和 excel 那种差不多的,
        # 选中一行上面添加调色盘之类的进行颜色修改」—— 所以摆在表格正上方。
        self.pal_bar = ttk.Frame(self.page_cat)
        self.pal_bar.pack(fill="x", pady=(0, 4))
        _palrow = ttk.Frame(self.pal_bar)
        _palrow.pack(fill="x")
        ttk.Label(_palrow, text="行配色", style="Dim.TLabel").pack(side="left",
                                                                  padx=(0, 6))
        # 为什么用 tk.Button 而不是 ttk.Button:ttk 按钮的外观归主题管,
        # configure(background=…) 在 Windows 主题下**不生效** —— 而这里的要点正是
        # 「按钮自身显示当前颜色」,底色必须说了算。
        self.btn_fg = tk.Button(_palrow, text="字体颜色", width=9, relief="groove",
                                bd=2, command=lambda: self.show_palette("fg"))
        self.btn_fg.pack(side="left")
        self.btn_bg = tk.Button(_palrow, text="填充颜色", width=9, relief="groove",
                                bd=2, command=lambda: self.show_palette("bg"))
        self.btn_bg.pack(side="left", padx=(4, 8))
        # 没上色时按钮该长什么样:把 Tk 给的默认底色/字色记下来,同步时用它还原
        self._btn_face = self.btn_fg.cget("background")
        self._btn_fg_face = self.btn_fg.cget("foreground")
        self.btn_clear = ttk.Button(_palrow, text="清除配色",
                                    command=self.clear_row_colors)
        self.btn_clear.pack(side="left")
        # 用户抱怨的死结是「看不出到底生效没有」,所以当前状态写在明面上
        self.pal_now = tk.StringVar(
            value="未选中行:先选一行(可 Ctrl / Shift 多选)再点色块")
        ttk.Label(_palrow, textvariable=self.pal_now, style="Dim.TLabel").pack(
            side="left", padx=10)
        # 能力边界对用户说清楚:这是整行配色,不是一格一格刷(见 _row_tags)
        ttk.Label(_palrow, text="(ttk 表格只能整行配色,刷不了单个单元格)",
                  style="Dim.TLabel").pack(side="left")
        # 色板本体:**默认不 pack**(收起)。常驻一排色块会把下面「显示零库存 /
        # 值区间」那排挤下去,而刷色是偶尔做的事;点「字体颜色 / 填充颜色」才摊开。
        self.pal_panel = ttk.Frame(self.pal_bar)
        self.pal_target = None       # 当前摊开的是给哪一项选色:"fg" / "bg" / None
        self.pal_swatches = {}       # 颜色 -> 那一格色块按钮(自检直接 invoke 它)
        self._sync_color_buttons()

        # 封装这一级(菜单的第三级)。做成一行可点的芯片,而不是又一页:
        # 同一个大类里封装通常只有两三种,为它单开一页会把「看一眼料」变成三次点击。
        self.pkg_bar = ttk.Frame(self.page_cat)
        self.pkg_bar.pack(fill="x", pady=(0, 4))

        # 筛选栏。回答的是「我这个大类里到底有没有某一档的东西」——
        # 比如「0805 的电阻里有没有 1k~10k 的」。比的是解析出来的数值,不是字符串。
        bar = ttk.Frame(self.page_cat)
        bar.pack(fill="x", pady=(0, 6))
        # 默认勾上:加完料一眼就能看见它(库存 0 也一样);只看有货的可以自己取消
        ttk.Checkbutton(bar, text="显示零库存", variable=self.zero_stock,
                        command=self.reload).pack(side="right")
        ttk.Label(bar, text="值").pack(side="left")
        self.f_min = tk.StringVar()
        self.f_max = tk.StringVar()
        for var, tip in ((self.f_min, "≥"), (self.f_max, "≤")):
            ttk.Label(bar, text=tip).pack(side="left", padx=(6, 2))
            e = ttk.Entry(bar, textvariable=var, width=8)
            e.pack(side="left")
            e.bind("<Return>", lambda _e: self.load_category())
        ttk.Label(bar, text="单位").pack(side="left", padx=(10, 4))
        self.f_unit = tk.StringVar(value=self.ALL)
        self.cmb_unit = ttk.Combobox(bar, textvariable=self.f_unit, width=6,
                                     values=[self.ALL], state="readonly")
        self.cmb_unit.pack(side="left")
        self.cmb_unit.bind("<<ComboboxSelected>>", lambda _e: self.load_category())
        ttk.Label(bar, text="封装").pack(side="left", padx=(10, 4))
        self.f_pkg = tk.StringVar(value=self.ALL)
        self.cmb_pkg = ttk.Combobox(bar, textvariable=self.f_pkg, width=14,
                                    values=[self.ALL], state="readonly")
        self.cmb_pkg.pack(side="left")
        self.cmb_pkg.bind("<<ComboboxSelected>>", lambda _e: self.load_category())
        ttk.Button(bar, text="清除筛选", command=self.clear_filters).pack(side="left", padx=10)
        self.f_hint = tk.StringVar()
        ttk.Label(bar, textvariable=self.f_hint, foreground="#b9770e").pack(side="left")
        ttk.Label(bar, text="  (值可以写 1k / 10kΩ / 0.1uF)", style="Dim.TLabel").pack(
            side="left")

        # issue #40:按「库存够不够」筛。范围**永远是当前所在层级的整棵子树** ——
        # 不用自己算:列表页把 category_id 交给后端,它自己会展开整棵子树,
        # 所以在「陶瓷电容」按就出陶瓷电容以下全部,在「陶瓷电容/0603」按就只出 0603 的。
        # 另起一排而不是塞进上面那条:那条已经挤满了(值/单位/封装/清除/两段提示),
        # 再塞会把它们挤成 1px,排版自检也会报。
        self.stock_filter = ""       # "" / "out"(库存=0)/ "low"(低于安全库存)
        sbar = ttk.Frame(self.page_cat)
        sbar.pack(fill="x", pady=(0, 6))
        ttk.Label(sbar, text="库存筛选").pack(side="left")
        ttk.Button(sbar, text="库存=0", width=8,
                   command=lambda: self.set_stock_filter("out")).pack(
            side="left", padx=(6, 2))
        ttk.Button(sbar, text="低于安全库存", width=13,
                   command=lambda: self.set_stock_filter("low")).pack(side="left", padx=2)
        ttk.Label(sbar, text="(没设安全库存的不算)", style="Dim.TLabel").pack(
            side="left", padx=(6, 0))
        self.f_stock = tk.StringVar()
        ttk.Label(sbar, textvariable=self.f_stock, foreground="#b9770e").pack(
            side="left", padx=8)

        pane = ttk.Panedwindow(self.page_cat, orient="vertical")
        pane.pack(fill="both", expand=True)

        top = ttk.Frame(pane)
        # 这里是平表,不是树 —— 没有 #0 列,也就没有展开三角
        frame, self.tree = make_tree(top, [
            ("name", "名称", 190, "w", True),
            ("lcsc_pn", "商品编号", 88, "center"),
            ("mpn", "厂家料号", 112, "w", True),
            # 厂家换成丝印:按「哪些字段真会被填」的统计,厂家只有 4/8 的表会记,
            # 而丝印是拆机料唯一能用来找回身份的东西,应该一眼看得到
            ("marking", "丝印", 78, "center"),
            ("package", "封装", 88, "w", True),
            ("value", "值", 68, "w"),
            # 属性槽:标题在 _apply_attr_cols() 里按这一页实际有的属性动态填。
            # 定义 4 个空槽、靠 displaycolumns 决定露几个 —— 换列不用重建整张表,
            # 属性名是用户自己起的(耐压、精度、Vgs…),列根本没法写死。
            # 属性槽:定义 12 个空槽,靠 displaycolumns 决定露哪几个、按什么顺序 ——
            # 用户要「想显示什么就显示什么、顺序能调、数量还能加」,写死几列满足不了。
            ("a1", "", 74, "center"),
            ("a2", "", 74, "center"),
            ("a3", "", 74, "center"),
            ("a4", "", 74, "center"),
            ("a5", "", 74, "center"),
            ("a6", "", 74, "center"),
            ("a7", "", 74, "center"),
            ("a8", "", 74, "center"),
            ("a9", "", 74, "center"),
            ("a10", "", 74, "center"),
            ("a11", "", 74, "center"),
            ("a12", "", 74, "center"),
            ("on_hand", "现有", 55, "e"),
            ("min_stock", "安全", 50, "e"),
            # 需求/缺口/在途/该买 是**项目 BOM 的缺料口径**,不是库存本身的事,
            # 摆在库存页只会让人以为"我缺料了"。它们在项目页和 BOM 复核里都有,
            # 这里整列去掉,腾出来的宽度留给真正的库存信息和后面的属性列。
            ("state", "状态", 55, "center"),
            ("note", "备注", 130, "w", True),
        ], height=14)
        frame.pack(fill="both", expand=True)
        pane.add(top, weight=3)

        self.tree.tag_configure("out", background="#ffe3e3")
        self.tree.tag_configure("low", background="#fff6dd")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", self._on_double)
        # 多选:把一整排元件挪到同一个新品类下,是这一页最常见的批量操作
        self.tree.configure(selectmode="extended")

        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="入库", command=lambda: self._quick_move("IN"))
        self.menu.add_command(label="出库", command=lambda: self._quick_move("OUT"))
        self.menu.add_command(label="盘点", command=lambda: self._quick_move("ADJUST"))
        self.menu.add_command(label="移库", command=lambda: self._quick_move("TRANSFER"))
        self.menu.add_separator()
        # 行配色(issue #28):选中一行或多行 → 文字颜色 / 背景色 / 清除。
        # 摆在「挪到品类 / 编辑 / 删除」**前面**的理由:用户第一次抱怨的就是
        # 「你也没有添加」—— 那三项原来吊在菜单最末尾(第 10/11/12 项,前面还有
        # 两条分隔线),在一张长菜单里等于没有。现在主入口是表格上方那排色块
        # (见 pal_bar),这三项退成「右键就在手边」的第二条路:两条路走的是
        # **同一个** pick_row_color(连「设完取消选中」都一样),不会两套行为。
        # 「背景色…」和工具条上的「填充颜色」是同一件事,只是沿用菜单里的老叫法,
        # 免得改名字把用惯右键的人弄糊涂。
        # 注意这是**整行**上色:ttk 连单元格做不到,连整列也不行(见 _row_tags)。
        self.menu.add_command(label="文字颜色…", command=lambda: self.pick_row_color("fg"))
        self.menu.add_command(label="背景色…", command=lambda: self.pick_row_color("bg"))
        # issue #36:选中的**格子**单独刷底色。和上面两条(刷整行)分开,互不干扰 ——
        # 行色是 tag,格子色是覆盖色块,两套东西。
        self.menu.add_separator()
        self.menu.add_command(label="选中格子:底色…", command=self.pick_cell_color)
        self.menu.add_command(label="选中格子:清掉底色", command=self.clear_cell_color)
        self.menu.add_command(label="清除自定义行配色", command=self.clear_row_colors)
        self.menu.add_separator()
        self.menu.add_command(label="挪到品类…", command=self.move_to_category)
        self.menu.add_command(label="编辑…", command=self.edit)
        self.menu.add_command(label="删除", command=self.delete)
        self.tree.bind("<Button-3>", self._popup)

        # ---- 在列标题上直接按住横向拖 = 换列(#33)
        # 用户原话:「拖动你只是改了**列管理里面**的拖动,我需要你在**元件显示栏**
        # 那里加入拖动交换」。上一轮(#26)那套拖动是「列…」窗口内部的,必须先开窗;
        # 这一套就架在表头本身上,不用开任何窗口 —— 同一个交互模式,只是搬到了表格上。
        # 为什么三个事件都先判 identify_region(x, y) == "heading" 才决定吃不吃:
        # 同一张表上还挂着行点击选中(<<TreeviewSelect>>)、双击进编辑、右键出菜单,
        # 两列之间那条分隔条上还要拖列宽 —— 这里只认领「按在标题文字上」的那一下,
        # 其余区域一律原样放行(见 _hdr_drag_begin 里的注释)。
        self._hdr_src = None       # 正被拖的是屏幕上第几列(None = 没在拖)
        self._hdr_slot = None      # 松手会插到第几格(和「列…」里 _drag_slot 同一个意思)
        # 落点提示线:一条 2px 的竖线压在表头上,平时 place_forget(),拖着才露脸。
        # 和「列…」窗口里那条横线是同一个思路 —— 独立小控件比给某一列换底色靠谱:
        # 表头底色在 Windows 主题下常被主题自己盖掉,而且说不清是插在它前面还是后面。
        self.hdr_line = tk.Frame(self.tree.master, width=2, bg="#2f6fd0",
                                 bd=0, highlightthickness=0)
        # issue #35:拖动换列要"跟手" —— 这两个是拖动过程中的状态:
        # _hdr_after 是「边缘自动滚动」的定时器 id,_hdr_last_x 是最后一次鼠标横坐标
        # (自动滚动之后没有新的 motion 事件,要靠它把提示线重新贴到落点上)
        self._hdr_after = None
        self._hdr_last_x = None
        # issue #36:Excel 式单元格选中。**只画 4 条边框,中间一律空着** ——
        # 铺满的覆盖层会吃掉 <Button-1>/<Double-1>/<Button-3>,双击进编辑和右键菜单当场就废。
        # 下面这几个 bind 也**只画框、绝不返回 "break"**,行上原来那一套照旧走。
        self._sel_cells = set()          # {(行 iid, "#列号")}
        self._sel_anchor = None          # 框选的起点,(行 iid, "#列号")
        # issue #36 的另一半:单格底色。每个有底色的格子是一个覆盖色块,所以
        # **数量要省着用**(只给真刷过色的格子造);而且每个色块都必须把
        # Button-1 / Double-1 / Button-3 转发回表格,否则双击进编辑、右键菜单会失效。
        self._cell_colors = {}           # {(行 iid, "#列号"): "#rrggbb"}
        self._cell_patches = {}          # {(行 iid, "#列号"): tk.Frame}
        self._sel_frames = [tk.Frame(self.tree, background="#1f6feb")
                            for _i in range(4)]      # 上/下/左/右
        self.tree.bind("<ButtonPress-1>", self._sel_click, add="+")
        self.tree.bind("<Shift-ButtonPress-1>", self._sel_click, add="+")
        self.tree.bind("<Control-ButtonPress-1>", self._sel_click, add="+")
        self.tree.bind("<B1-Motion>", self._sel_drag, add="+")
        # 改窗口大小、换列顺序、reload 之后框会跑偏,统一重画一次
        self.tree.bind("<Configure>", lambda _e: self._sel_paint(), add="+")
        self.tree.bind("<ButtonPress-1>", self._hdr_drag_begin, add="+")
        self.tree.bind("<B1-Motion>", self._hdr_drag_motion, add="+")
        self.tree.bind("<ButtonRelease-1>", self._hdr_drag_end, add="+")

        bottom = ttk.Frame(pane)
        left = ttk.LabelFrame(bottom, text="仓位分布", padding=6)
        left.pack(side="left", fill="both", expand=True, padx=(0, 4))
        f1, self.t_stock = make_tree(left, [
            ("code", "仓位", 130, "w"), ("qty", "数量", 80, "e")], height=6)
        f1.pack(fill="both", expand=True)

        right = ttk.LabelFrame(bottom, text="最近流水", padding=6)
        right.pack(side="left", fill="both", expand=True, padx=(4, 0))
        f2, self.t_hist = make_tree(right, [
            ("created_at", "时间", 140, "center"),
            ("kind", "动作", 60, "center"),
            ("qty", "数量", 55, "e"),
            ("location_code", "仓位", 90, "w"),
            ("project_name", "项目", 110, "w"),
            ("note", "备注", 180, "w")], height=6)
        f2.pack(fill="both", expand=True)
        pane.add(bottom, weight=2)

    # ------------------------------------------------------ 页面:选下一级

    def _build_pick(self):
        head = ttk.Frame(self.page_pick)
        head.pack(fill="x", pady=(0, 8))
        ttk.Button(head, text="← 返回", command=self.go_back, width=9).pack(
            side="left", padx=(0, 10))
        self.pick_title = tk.StringVar()
        ttk.Label(head, textvariable=self.pick_title, style="H1.TLabel").pack(side="left")
        self.pick_count = tk.StringVar()
        ttk.Label(head, textvariable=self.pick_count, style="Dim.TLabel").pack(
            side="left", padx=12)
        # 就地加子类:人已经站在这一级上了,不该逼他先打开整棵树再找位置
        ttk.Button(head, text="＋ 加子类", command=self.add_here).pack(
            side="right", padx=6)
        ttk.Button(head, text="🗂 品类管理", command=self.manage_categories).pack(side="right")
        ttk.Label(self.page_pick,
                  text="这一级是品类树里的子类。加新子类点右上角「＋ 加子类」,"
                       "改名或删除在卡片上点右键。只动这一级,不会牵连上面的层级。",
                  style="Dim.TLabel").pack(anchor="w", pady=(0, 6))
        self.pick_board = CardBoard(self.page_pick, self._pick_one, self._cat_menu)
        self.pick_board.pack(fill="both", expand=True)

    def _load_cat_tree(self):
        """把品类树拉下来,存成 id -> 节点。菜单的每一层都从它来。"""
        data = call(self.con, server.list_categories, quiet=True) or {}
        self._cat_flat = {}
        for n in data.get("flat") or []:
            self._cat_flat[n["id"]] = n
        # 树上真有一个叫「未分类」的顶层行时,那一支的 id 也一起备好 ——
        # 「有没有品类」这件事每次都要问它,不能每问一次就重扫一遍整棵树
        self._uncat_ids = self._compute_uncat_ids()
        return data

    def _compute_uncat_ids(self):
        """树上那些**真叫「未分类」的顶层品类行**和它整棵子树的 id。

        为什么要认这一支:老数据(或老版本的后端)删掉顶层大类时会把料挂到一个
        自动建出来的「未分类」品类行下面。那些料在树上是有位置的,可它们和
        「真的没有品类」的料是同一个意思,首页那张卡片理应把它们一起收着。
        没有这一行就返回空集 —— 这时「未分类」= category_id 为空。
        """
        root = None
        for n in self._cat_flat.values():
            if (n.get("parent_id") is None
                    and (n.get("name") or "").strip() == UNCATEGORIZED):
                root = n["id"]
        if root is None:
            return set()
        ids = {root}
        # 品类树只有几层,反复扫到不再增长为止:几行代码,比引一个递归好懂
        while True:
            more = {n["id"] for n in self._cat_flat.values()
                    if n.get("parent_id") in ids and n["id"] not in ids}
            if not more:
                return ids
            ids |= more

    def _is_uncat(self, item):
        """这颗元件该不该算「没有品类」—— 首页那张「未分类」卡片收的就是这些。

        **判定只看 category_id,不看 category 文本**,两种组合都算:
          * `category_id` 对不上品类树里的任何节点(为 NULL,或指着已经被删掉的
            节点)—— 这是后端删掉顶层大类之后留下的形态:category_id=NULL +
            文本 ''。按字段组合说就是「id 空」这一条,文本空不空都不影响判断;
          * `category_id` 落在那个**真叫「未分类」的顶层品类行**那一支下面 —— 老
            数据/老版本后端删大类时会把料挂到自动建出来的「未分类」行下面。

        为什么不用文本判断:文本是显示用的冗余标签(老数据里会出现「文本写着
        「其他」、id 却是空」这种组合),拿它判断会把有品类的料误判成没品类的,
        反过来也会。而「有没有挂在树上」只有 id 说得准 —— 对不上树就是真的没位置。
        """
        cid = item.get("category_id")
        return cid not in self._cat_flat or cid in getattr(self, "_uncat_ids", set())

    def _uncat_items(self, items):
        """从一批元件里挑出「没有品类」的那些。"""
        return [it for it in items if self._is_uncat(it)]

    def _last_cat(self):
        for c in reversed(self.crumb):
            if c["kind"] == "cat":
                return c
        return None

    def _render_crumb(self):
        """面包屑:点哪一级就退回哪一级。"""
        for w in self.crumb_bar.winfo_children():
            w.destroy()
        if not self.crumb:
            self.crumb_bar.pack_forget()
            return
        self.crumb_bar.pack(fill="x", pady=(0, 6), before=self.holder)
        ttk.Button(self.crumb_bar, text="🏠 全部库存", width=12,
                   command=self.go_home).pack(side="left", padx=(0, 2))
        for i, c in enumerate(self.crumb):
            ttk.Label(self.crumb_bar, text="›", style="Dim.TLabel").pack(side="left", padx=1)
            if i == len(self.crumb) - 1:
                # 当前这一级不是按钮 —— 点它没有去处,做成按钮只会让人白点一次
                ttk.Label(self.crumb_bar, text=c["name"],
                          font=("Microsoft YaHei UI", 9, "bold")).pack(side="left", padx=2)
            else:
                ttk.Button(self.crumb_bar, text=c["name"], width=max(4, len(c["name"]) + 2),
                           command=lambda n=i: self.crumb_to(n)).pack(side="left", padx=2)

    def crumb_to(self, i):
        self.crumb = self.crumb[:i + 1]
        # 「本级」那一格被点掉了,就真的回到上一级去,不能还停在「只看本级」的列表里
        self._own_only = any(c["kind"] == "own" for c in self.crumb)
        if not any(c["kind"] == "pkg" for c in self.crumb):
            self.f_pkg.set(self.ALL)
        self._descend()

    def go_back(self):
        if len(self.crumb) <= 1:
            self.go_home()
            return
        self.crumb_to(len(self.crumb) - 2)

    def _pick_one(self, key):
        """点中间页的一张卡片。key 是品类 id;特殊的 "self" 是那张「本级」。"""
        if key == "self":
            # 本级:这一级已经在路径里了,所以只把列表切成「只要挂在这一个节点上的」
            self._own_only = True
            self.load_category()
            return
        node = self._cat_flat.get(int(key))
        if node is None:
            # 这一级被删了(卡片是上一屏画的)。不能静默重画了事 ——
            # 静默就是这个 issue 的病根,至少给一句话
            self.app.set_status("这个品类已经不在品类树里了,已经刷新", 6)
            self.reload()
            return
        if (self.crumb and self.crumb[-1]["kind"] == "cat"
                and self.crumb[-1].get("id") == node["id"]):
            # 同一个节点已经站在路径末尾了,再点一次不该又加一段
            self._descend()
            return
        self._own_only = False
        self.crumb.append({"kind": "cat", "id": node["id"], "name": node["name"],
                           "path": node["path"]})
        self._descend()

    def pick_package(self, pkg):
        """点封装的芯片 —— 这是最后一级之前的那一步。"""
        self.f_pkg.set(pkg or self.ALL)
        self.crumb = [c for c in self.crumb if c["kind"] != "pkg"]
        if pkg:
            self.crumb.append({"kind": "pkg", "name": pkg})
        self.load_category()

    def _show_pick(self, node):
        self.view = "pick"
        self._pick_items = list(node.get("children") or [])
        specs = []
        # 「本级」:大类下面既有细分出来的子类、又有还没细分的料,是很常见的摆法。
        # 少了这张卡片,那些料就永远进不去 —— 点一个「有子类的品类」只会看到子类。
        #
        # 判断**摆不摆**这张卡片的是 `own`(直接挂在这个节点下的元件数,**不看库存**),
        # 不是 `own_stocked`。原来的写法只认有库存的,于是"这个节点有子类、本级又直接挂了
        # 一批 0 库存的料"时,那批料在菜单上**没有任何入口**,只能靠搜索 ——
        # 这正是 issue #40 说的"物料不要自动消失"。
        # 卡片上的**数字**仍然只用有库存的(点进去看到的就是那些),和子类卡片口径一致。
        own = int(node.get("own") or 0)
        own_stocked = int(node.get("own_stocked") or 0)
        if own:
            glyph, color = CATEGORY_STYLE.get(node["name"], DEFAULT_CAT_STYLE)
            specs.append(("self", "本级(挂在这里的)", glyph, color,
                          f"{own_stocked} 种在库" if own_stocked else "暂无库存", False))
        stocked = 0
        for ch in self._pick_items:
            glyph, color = CATEGORY_STYLE.get(ch["name"], DEFAULT_CAT_STYLE)
            # 只数**有库存**的:卡片写的数字必须和点进去看到的一致,
            # 不然零库存的品类也喊「N 种在库」,点进去一行都没有(虚假库存)
            n = int(ch.get("total_stocked") or 0)
            stocked += n
            specs.append((str(ch["id"]), ch["name"], glyph, color,
                          f"{n} 种在库" if n else "暂无库存", not n))
        self.pick_title.set(node["name"])
        total = stocked + own_stocked
        self.pick_count.set(f"{len(self._pick_items)} 个子类"
                            + (f",共 {total} 种在库" if total else ""))
        self.pick_board.render(specs, empty_text="这个品类下面还没有子类。")
        self._render_crumb()
        self._swap(self.page_pick)

    def _descend(self):
        """按当前路径决定该显示哪一页。"""
        node = self._last_cat()
        if node is None:
            self.go_home()
            return
        live = self._cat_flat.get(node.get("id"))
        if live is not None and (live.get("children") or []):
            # 有子类就必须先选子类 —— 但只在这里跳,没子类的大类直接进列表,
            # 不让用户点一个只有一项的页面
            self._show_pick(live)
            return
        self.load_category()

    # ------------------------------------------------------ 页面切换

    def _swap(self, page):
        """三个页面**只能有一个**留在屏幕上。

        这里原来只收起了 home 和 cat,漏了 page_pick —— 于是从「选子类」那一页进到
        列表页时,两页同时铺着、上下各占一块(用户说的「上下分页」);而且 pick 页
        还在屏幕上,里面的卡片照样能点,一点面包屑就多一段(「C0805 › C0805 › C0805」);
        「返回」也只是改了路径、页面还是叠着,看着像没反应。
        三个症状同一个根因,就是这一行。
        """
        for p in (self.page_home, self.page_cat, self.page_pick):
            p.pack_forget()
        page.pack(fill="both", expand=True)

    def go_home(self):
        self.view = "home"
        self.current_category = None
        self.crumb = []
        self._own_only = False
        self._render_crumb()
        self._swap(self.page_home)

    def open_category(self, name):
        """点一张大类卡片。"""
        self.current_category = name
        self._load_cat_tree()
        node = None
        for n in self._cat_flat.values():
            if n["parent_id"] is None and n["name"] == name:
                node = n
                break
        self.view = "cat"
        self._own_only = False
        self.clear_filters(redraw=False)
        if node is None:
            # 品类表里没有这个大类。理论上不该发生(卡片就是从树上来的),
            # 但真发生了也不能让用户点着一张卡片却什么都没发生 ——
            # 退化成老行为:按文本筛。
            self.crumb = [{"kind": "cat", "id": None, "name": name, "path": name}]
            self.load_category()
            return
        self.crumb = [{"kind": "cat", "id": node["id"], "name": node["name"],
                       "path": node["path"]}]
        self._descend()

    def manage_categories(self):
        """开品类管理窗口;关掉之后菜单要按新的树重画。

        这个窗口用来**看整棵树**(哪一支在哪个位置、下面各有多少料)。
        日常的「加一个 / 改个名 / 删掉」不必开它 —— 在菜单里就地做,
        人本来就在那一页上。
        """
        dlg = CategoryManagerDialog(self, self.app)
        self.wait_window(dlg)
        self.reload()

    # ------------------------------------------------------ 就地改这一级

    def _menu_node(self, key):
        """右键那张卡片对应树上哪个节点。

        首页的卡片 key 是大类**名字**,中间页的 key 是节点 id —— 两个页面
        共用同一个回调,所以按当前视图分辨。

        首页的卡片现在**全部来自品类树**(名单就是库里真有的顶层品类 + 有需要
        才算出来的「未分类」),所以名字一定对得上一行。唯一对不上的是「未分类」
        那张:它不是品类行,是"库里有多少料没有品类"算出来的(见 reload)。
        真找不到时返回 None,由调用方给一句人话 —— 静默返回正是 issue #29 里
        「点了没反应」的根源。以前这里会给写死的名单现造一个虚拟节点(id 为 None),
        那套东西随着写死名单一起没了。
        """
        if self.view == "pick":
            if str(key) == "self":
                # 中间页那张「本级」卡片绑的是同一个右键回调。它对应的就是**当前
                # 这一级**(不是"找不到节点"),所以按当前路径末端的品类节点算 ——
                # 不然会给用户来一句莫名其妙的「这一级已经不在品类树里了」。
                here = self._last_cat()
                if here is None:
                    return None
                return self._cat_flat.get(here.get("id")) or here
            try:
                return self._cat_flat.get(int(key))
            except (TypeError, ValueError):
                return None
        for n in self._cat_flat.values():
            if n["parent_id"] is None and n["name"] == key:
                return n
        return None

    def _cat_menu(self, key, event):
        """卡片右键菜单:只动这一级。"""
        menu = self._build_cat_menu(key)
        if menu is None:
            return
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _build_cat_menu(self, key):
        """把这张卡片该有的右键菜单建出来(不弹出来)。

        单独拆出来是为了让自检能直接看菜单里有哪些项 —— tk_popup 会真把菜单
        挂到屏幕上等人点,自检里没法点它,只能看它长什么样。
        """
        self._load_cat_tree()          # 数量、子类这些得是新的
        node = self._menu_node(key)
        if node is None or node.get("id") is None:
            if str(key).strip() == UNCATEGORIZED:
                # 「未分类」不是品类行,没有可改名、可删的层级。这里明说一句:
                # 它是按「库里真有没有品类的料」算出来的,把料归了类就自己消失。
                self.app.set_status(
                    f"「{UNCATEGORIZED}」不是品类,它是按「库里真有没有品类的料」算出来的:"
                    f"把这些料归了类,这张卡片自己就消失", 8)
            else:
                # 别处刚把这一级删了。**不许静默返回** —— 静默返回正是 issue #29 里
                # 「点了没反应」的根源,至少给一句话。
                self.app.set_status(f"「{key}」这一级已经不在品类树里了,已经刷新", 6)
                self.reload()
            return None
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="＋ 在这下面加子品类…",
                         command=lambda: self.add_child_cat(node))
        menu.add_command(label="改名…", command=lambda: self.rename_cat(node))
        menu.add_command(label="删除…", command=lambda: self.delete_cat(node))
        if node["parent_id"] is None:
            menu.add_separator()
            menu.add_command(label="＋ 再加一个顶级品类…", command=self.add_root_cat)
        return menu

    def add_here(self):
        """在当前这一页下面加子品类。首页没有「当前页」,那就是加顶级。"""
        self._load_cat_tree()
        node = self._last_cat() if self.view == "pick" else None
        if node is None:
            self.add_root_cat()
        else:
            self.add_child_cat(node)

    def add_root_cat(self):
        name = ask_text(self, "新增大类", "新的大类叫什么?", "")
        if not name:
            return
        res = call(self.con, server.create_category, parent=self, body={"name": name})
        if res is None:
            return
        self.app.set_status(f"加好了大类「{name}」", 5)
        self.reload()

    def add_child_cat(self, node):
        """在某个品类下面新建子品类。加完菜单立刻多一张卡片。

        卡片一定对应库里真有的一行(名单就是从品类树来的,见 reload),所以这里
        直接用这一级的 id 挂子类;id 为空只可能是防御路径(菜单不会给出这种项)。
        """
        if node.get("id") is None:
            self.app.set_status(
                f"「{(node or {}).get('name')}」在品类树里没有对应的一行,加不了子品类", 6)
            return
        name = ask_text(self, "新增子品类",
                        f"挂在「{node['path']}」下面的新品类叫什么?", "")
        if not name:
            return
        res = call(self.con, server.create_category, parent=self,
                   body={"name": name, "parent_id": node["id"]})
        if res is None:
            return
        self.app.set_status(f"加好了「{node['path']} / {name}」", 5)
        self.reload()

    def add_sub_here(self):
        """在**当前这一级**下面新建子类。

        列表页原来只有「＋ 新增元件 / 编辑 / 删除」,想加子类得退回库存首页 ——
        而人明明已经站在这一级上了。用户的原话:「我想要增加封装 R0805,
        把这个一排的元件放下面去,就没有这个按键,只能跑到库存首页去新建」。
        所以这里不只建类:建完顺手问一句要不要把**选中的那些元件**挪进去。
        """
        node = self._last_cat()
        if node is None or node.get("id") is None:
            # 「未分类」这一级不是一个品类行,它下面不存在"子类"这回事(料要自己
            # 归到某个品类里去)。给一句话再回首页 —— 悄没声地弹回首页,用户只会
            # 以为按钮坏了,那正是这类毛病最招人烦的地方。
            if node is not None:
                self.app.set_status(
                    f"「{node.get('name')}」不是品类,加不了子类;"
                    "把料归到某个品类里就行", 8)
            self.go_home()
            return
        self._load_cat_tree()
        live = self._cat_flat.get(node["id"])
        if live is None:
            # 不许静默返回:点了「新建子类」却什么都没发生,用户只能反复点
            self.app.set_status(f"「{node.get('name')}」已经不在品类树里了,已经刷新", 6)
            self.reload()
            return
        picked = self.selected_ids()
        name = ask_text(self, "新增子品类",
                        f"挂在「{live['path']}」下面的新品类叫什么?", "")
        if not name:
            return
        res = call(self.con, server.create_category, parent=self,
                   body={"name": name, "parent_id": live["id"]})
        if res is None:
            return
        self.app.set_status(f"加好了「{live['path']} / {name}」", 5)
        new_id = (res or {}).get("id")
        if picked and new_id:
            if messagebox.askyesno(
                    "顺手挪一下?",
                    f"把选中的 {len(picked)} 颗元件挪到「{live['path']} / {name}」下面吗?\n\n"
                    "只改它们挂在哪个品类下,库存和流水都不动。", parent=self):
                self._move_ids_to(picked, int(new_id), f"{live['path']} / {name}")
        self.reload()

    def move_to_category(self):
        """把选中的元件一次性挪到某个品类路径下。"""
        picked = self.selected_ids()
        if not picked:
            return
        here = self._last_cat()
        dlg = CategoryPickerDialog(self, self.app,
                                  cat_id=(here or {}).get("id"))
        self.wait_window(dlg)
        target, path = dlg.result
        if target is None:
            return
        self._move_ids_to(picked, int(target), path)
        self.reload()

    def _move_ids_to(self, ids, target_id, path):
        """批量改归属。只动 category / category_id,不碰库存和流水。"""
        done = 0
        for cid in ids:
            res = call(self.con, server.update_component, parent=self,
                       match=(str(cid),), body={"category_id": int(target_id)})
            if res is not None:
                done += 1
        self.app.set_status(f"{done} 颗元件挪到了「{path}」", 6)
        return done

    def rename_cat(self, node):
        name = ask_text(self, "改品类名", "新的名字:", node["name"])
        if not name or name == node["name"]:
            return
        res = call(self.con, server.update_category, parent=self,
                   match=(str(node["id"]),), body={"name": name})
        if res is None:
            return
        n = int((res or {}).get("renamed_components") or 0)
        self.app.set_status(f"改好了。{n} 个元件的品类跟着更新了。", 5)
        self.reload()

    def delete_cat(self, node):
        """删掉这一级。**下游的东西一件都不丢**:子类接到上一级,元件跟着走。

        两类落点,措辞必须分开(用户点「删除」时最怕的是「连子类和料一起没了」,
        而这里一件都不删):
          * 删的不是顶层 -> 子类和元件都接到上一级;
          * 删的是顶层大类 -> 它没有上一级,下面的子类各自成为顶层,直接挂在它
            下面的元件变成**没有品类**(后端把 category_id 清空、文本清空),
            首页那张「未分类」卡片会把它们收着,可以在那里重新归类。
        """
        if node.get("id") is None:
            # 防御:菜单只对品类树上的行给「删除…」,这张卡片进不来这条路
            self.app.set_status(
                f"「{(node or {}).get('name')}」在品类树里没有对应的一行,没有可删的东西", 6)
            return
        top = node.get("parent_id") is None
        kids = node.get("children") or []
        n = int(node.get("total") or 0)
        msg = f"删掉「{node['path']}」?"
        if kids:
            if top:
                # 顶层的下一级没有「上一级」可接 —— 它们各自升成顶层,名字不变
                msg += (f"\n\n它下面的 {len(kids)} 个子品类会各自升成顶层品类"
                        f"(自己原来的名字就是大类名),**一个都不会删**。")
            else:
                msg += (f"\n\n它下面的 {len(kids)} 个子品类会接到上一级,"
                        f"**一个都不会删**。")
        if n:
            if top:
                msg += (f"\n\n挂在它(含子类)下面的 {n} 个元件会变成「没有品类」,"
                        f"**不会被删除**,之后在首页「{UNCATEGORIZED}」里找得到、"
                        f"能重新归类。")
            else:
                msg += (f"\n\n挂在它(含子类)下面的 {n} 个元件会被挪到上一级,"
                        f"**不会被删除**。")
        if top and (node.get("name") or "").strip() == UNCATEGORIZED:
            # 这一行就叫「未分类」-> 里面的料还是「没有品类」,那张卡片自然会再出现。
            # 先说清楚,免得用户又觉得「删不掉」来回抱怨 —— 它不是品类,是算出来的。
            msg += (f"\n\n注意:「{UNCATEGORIZED}」这张卡片是按「库里真有没有品类的"
                    f"料」算出来的,只要还有这样的料它就会一直在;把料归了类它就消失。")
        if not messagebox.askyesno("确认删除", msg, parent=self):
            return
        res = call(self.con, server.delete_category, parent=self,
                   match=(str(node["id"]),))
        if res is None:
            # 后端拒了(或被取消)—— 说一句,别让用户以为删掉了
            self.app.set_status(f"「{node['path']}」没删掉,数据一点没动", 6)
            return
        moved = int(res.get("moved_components") or 0)
        kids_moved = int(res.get("moved_children") or 0)
        # to 是空串 = 没有上一级(删的就是顶层大类)—— 这时措辞必须是「没有品类了」,
        # 不能再说「挪到「未分类」」:后端已经不建那一行了,那不是它的去处
        to = str(res.get("to") or "").strip()
        if to:
            self.app.set_status(
                f"删掉了「{node['path']}」。{moved} 颗料挪到了上一级「{to}」,"
                f"{kids_moved} 个子品类也接到了上一级,一个都没丢。", 8)
        else:
            self.app.set_status(
                f"删掉了「{node['path']}」。{moved} 颗料现在没有品类了,"
                f"可以在「{UNCATEGORIZED}」里重新归类;"
                f"{kids_moved} 个子品类升成了顶层,一个都没丢。", 8)
        self.reload()

    # ------------------------------------------------------ 二级页的筛选

    ALL = "(全部)"

    def _facet_value(self, var):
        v = var.get().strip()
        return "" if v in ("", self.ALL) else v

    def _set_facet(self, combo, options, var):
        """下拉里放「(全部)」+ 当前范围里真有的值,并保留当前选中项。

        选项来自后端按当前范围算出的分面,所以不会摆一堆选了也搜不到的空选项。
        """
        cur = var.get()
        opts = [self.ALL] + list(options)
        if cur and cur not in opts:
            opts.append(cur)
        combo.configure(values=opts)

    def set_stock_filter(self, mode):
        """按库存档位筛**当前所在层级的整棵子树**(issue #40)。再点同一个按钮 = 取消。

        范围不用自己算:列表页把 `category_id` 交给后端,后端自己会展开整棵子树。
        点「库存=0」时顺手把「显示零库存」打开 —— 外层条件是 AND,`stocked=1` 与
        `state=out` 互斥,不打开那个开关就**永远是空表**(口径见 build\\test_api.py【43】)。
        """
        mode = "" if self.stock_filter == mode else mode
        self.stock_filter = mode
        if mode == "out" and not self.zero_stock.get():
            self.zero_stock.set(True)
            self.f_stock.set("已顺手打开「显示零库存」—— 否则这张表永远是空的")
        else:
            self.f_stock.set({"": "", "out": "只看库存=0 的",
                              "low": "只看低于安全库存的"}.get(mode, ""))
        # 列表页(有「当前层级」)走 load_category;首页只有卡片,那边 load_category
        # 会直接返回(view 不是 cat),所以再刷一次总览,让卡片上的数字也跟着筛。
        self.load_category()
        if self.view != "cat":
            self.reload()

    def clear_filters(self, redraw=True):
        self.f_min.set("")
        self.f_max.set("")
        self.f_unit.set(self.ALL)
        self.f_pkg.set(self.ALL)
        self.f_hint.set("")
        if redraw and self.view == "cat":
            self.load_category()

    def _filter_summary(self):
        bits = []
        if self.f_min.get().strip():
            bits.append(f"值 ≥ {self.f_min.get().strip()}")
        if self.f_max.get().strip():
            bits.append(f"值 ≤ {self.f_max.get().strip()}")
        if self._facet_value(self.f_unit):
            bits.append(f"单位 {self._facet_value(self.f_unit)}")
        if self._facet_value(self.f_pkg):
            bits.append(f"封装 {self._facet_value(self.f_pkg)}")
        return "筛选:" + "  ".join(bits) if bits else ""

    def load_category(self):
        """按当前筛选重新向后端要这个大类在库的元件。

        为什么不在内存里筛:值区间必须按解析出的数值比(value_num)。
        在内存里拿字符串比,「100k」会被当成比「10k」小,区间就完全不对了。
        分面选项(封装/单位)也是后端按当前范围算的,只列真有的。
        """
        node = self._last_cat()
        if node is None and self.current_category:
            node = {"id": None, "name": self.current_category}
        if node is None:
            return
        # 进列表页就是 cat 模式。以前这一句写在 open_category 里,因为那时只有
        # 它一条路会走到这儿;现在中间页也会,漏在这儿 refresh 之后就会按错的
        # 模式去恢复路径(页面是列表页,view 却还写着 pick)。
        self.view = "cat"
        name = node["name"]
        # 面包屑里的「封装」这一级要和实际筛选一致:点芯片和直接改下拉框是两条路,
        # 但走过的路必须是同一条 —— 否则面包屑会写着 0603、表里却是全部
        pkg_now = self._facet_value(self.f_pkg)
        self.crumb = [c for c in self.crumb if c["kind"] not in ("pkg", "own")]
        if self._own_only:
            self.crumb.append({"kind": "own", "name": "本级"})
        if pkg_now:
            self.crumb.append({"kind": "pkg", "name": pkg_now})
        # 按 category_id 筛**整棵子树**:挂在子类下的元件文本仍然写着大类名,
        # 只按文本筛的话,「挂在子类」和「直接挂在顶层」根本分不出来
        # 「显示零库存」开着就**不传** stocked —— 传了新加的料(库存 0)会被 SQL
        # 直接滤掉:加成功了却看不见,比报错还让人困惑(issue #22)
        # 「未分类」这一级收的是**对不上品类树**的元件(见 _is_uncat):
        #   * 新后端删掉顶层大类之后,把它们清成 category_id 为空、文本为空;
        #   * 老数据里则可能挂在一个真叫「未分类」的品类行下面。
        # 两种形态都得能列出来,而后端没有「没有品类」这个筛选项(那个文件不归
        # 这边改),所以这一级不传品类条件、取回来自己挑 —— 一个本地小库,
        # limit=0 一次就取完了。别的品类照旧把 category_id 交给 SQL 筛整棵子树。
        uncat = (name == UNCATEGORIZED)
        if uncat:
            # 这一级的判定要用树上**最新**的 id(见 _is_uncat):拿上一屏的旧树来挑,
            # 刚建出来的品类下面的料会被误算成「没有品类」(卡片和列表对不上)
            self._load_cat_tree()
        query = {"limit": "0", "sort": "value"}
        # issue #40:`stocked=1` 与 `state=out` 是 AND 关系,同时传必然是空表 ——
        # 要「库存=0」的时候就不能再传 stocked(那时上面那个开关也已经被自动打开)
        if not self.zero_stock.get() and self.stock_filter != "out":
            query["stocked"] = "1"
        if self.stock_filter:
            query["state"] = self.stock_filter
        if uncat:
            pass
        elif node["id"] is None:
            query["category"] = name
        else:
            query["category_id"] = str(node["id"])
            if self._own_only:
                # 只要直接挂在这一个节点上的,不含子孙
                query["own"] = "1"
        pkg, unit = self._facet_value(self.f_pkg), self._facet_value(self.f_unit)
        if pkg:
            query["package"] = pkg
        if unit:
            query["unit"] = unit
        for key, var in (("value_min", self.f_min), ("value_max", self.f_max)):
            text = var.get().strip()
            if not text:
                continue
            num, _u = values.parse_value(text)
            if num is None:
                try:
                    num = float(text)          # 也允许直接写纯数字
                except ValueError:
                    self.f_hint.set(f"「{text}」看不懂,值请写 1k / 10kΩ / 0.1uF 这种")
                    return
            query[key] = str(num)

        data = call(self.con, server.list_components, query=query, quiet=True) or {}
        rows = data.get("items") or []
        facets = data.get("facets") or {}
        if uncat:
            rows = self._uncat_items(rows)
            # 分面也得按挑出来的这批料算。用整库的分面,下拉里会摆一堆在「未分类」
            # 里根本选不到东西的封装/单位 —— 选了就是空表,看着像数据丢了。
            facets = {
                "packages": sorted({(it.get("package") or "").strip() for it in rows
                                    if (it.get("package") or "").strip()}),
                "units": sorted({(it.get("value_unit") or "").strip() for it in rows
                                 if (it.get("value_unit") or "").strip()}),
            }
        # 下拉列归并后的尺寸,和那一排按钮同一套值 —— 两条路筛出不同结果的话,
        # 用户只会更糊涂(按钮上写着 0805,下拉里却没有 0805 这一项)。
        self._set_facet(self.cmb_pkg,
                        [g[1] for g in pkg_groups(facets.get("packages") or [])],
                        self.f_pkg)
        self._set_facet(self.cmb_unit, facets.get("units") or [], self.f_unit)
        self._render_pkg_chips(facets.get("packages") or [])

        # 图标和颜色跟着**大类**走,子类沿用父类的 —— 否则同一支下面的
        # 每个子类一个颜色,反而看不出它们是一家
        root_name = self.crumb[0]["name"] if self.crumb else name
        glyph, color = CATEGORY_STYLE.get(root_name, DEFAULT_CAT_STYLE)
        self.cat_badge.configure(text=glyph, bg=color if rows else DIM_BADGE)
        self.cat_title.set(name)
        summary = self._filter_summary()
        if rows:
            self.cat_count.set(f"共 {len(rows)} 种" + (f"   {summary}" if summary else ""))
            self.f_hint.set("")
        else:
            # 空态要分清「这个大类本来就没货」和「是你筛掉了」
            self.cat_count.set(summary if summary else "暂无库存元件")
        self._render_crumb()
        self._swap(self.page_cat)
        self._fill(rows)

    def _render_pkg_chips(self, pkgs):
        """封装这一级的菜单。选中的那个用 ● 标出来 —— ttk 按钮没有「按下」态,
        而「我现在筛的是哪个封装」必须一眼看得到,不能只靠下拉框里那一行小字。"""
        for w in self.pkg_bar.winfo_children():
            w.destroy()
        self.pkg_chips = {}
        ttk.Label(self.pkg_bar, text="封装", style="Dim.TLabel").pack(side="left",
                                                                    padx=(0, 6))
        cur = self._facet_value(self.f_pkg)
        # 先按尺寸归并:C0805 / 0805 / SMD0805 是同一个位置,摆成三个按钮
        # 只会让人挑花眼,而且点哪一个都只筛出一部分 —— 看着像漏了数据。
        groups = pkg_groups(pkgs)
        if len(groups) > 14:
            # 封装特别多的时候不给芯片了:一行几十个按钮比下拉框还难找
            ttk.Label(self.pkg_bar, text=f"{len(groups)} 种封装,用右边的下拉框筛",
                      style="Dim.TLabel").pack(side="left")
            self.pkg_chips["__many__"] = None
            return

        def chip(label, value):
            active = (cur == value)
            btn = ttk.Button(self.pkg_bar, text=("● " if active else "   ") + label,
                             width=max(6, len(label) + 5),
                             command=lambda: self.pick_package(value))
            btn.pack(side="left", padx=1)
            self.pkg_chips[value] = btn

        chip("全部", "")
        for label, key, _raws in groups:
            chip(label, key)

    def open_search(self):
        kw = self.q.get().strip()
        if not kw:
            self.go_home()
            return
        self.view = "search"
        self.current_category = None
        # 走后端搜,不是内存里比字符串 —— 丝印、参数 JSON 只有后端才搜得到,
        # 而拆机料恰恰只能靠丝印找回来
        data = call(self.con, server.list_components,
                    query={"q": kw, "limit": "0", "sort": "category"}, quiet=True) or {}
        hits = data.get("items") or []
        self.cat_badge.configure(text="⌕", bg="#546e7a")
        self.cat_title.set(f"搜索:{kw}")
        self.cat_count.set(f"找到 {len(hits)} 种" if hits else "没找到")
        self._swap(self.page_cat)
        self._fill(hits)

    def _fill(self, rows):
        # 属性列要按这一页**真有值**的属性来定,所以必须在插行之前算出来
        self._apply_cols(rows)
        clear_tree(self.tree)
        self._rows = {}
        for it in rows:
            self._rows[it["id"]] = it
            self._insert_component(it)

    # ------------------------------------------------------ 数据

    def reload(self):
        # 刷新前记住选中的那一行:重建表格会让 Tk 发 <<TreeviewSelect>>,
        # 那一刻 selected_id() 是空的,右下角「仓位分布 / 最近流水」会被
        # 一并清掉 —— 用户报的「出入库流水没显示」其实是这么来的(流水写进去了)。
        keep = self.selected_id()
        # stocked=1:库存为 0 的元件在 SQL 层就被滤掉,二级页面自然只剩有货的
        # 总览 / 二级页**永远**只看有货的:导入 BOM 会留下一堆库存 0 的料,
        # 放开就会把真正到货的那些淹掉。要「看得见零库存」去叶子列表页
        # (那里默认开着,见 load_category)
        # issue #40:库存筛选生效时改用 state(它本身就是"够不够"的口径);
        # 那时候不能再叠 stocked —— state=out 与 stocked=1 互斥,叠了就是空表。
        _q = ({"sort": "category", "state": self.stock_filter}
              if self.stock_filter else {"stocked": "1", "sort": "category"})
        data = call(self.con, server.list_components,
                    query=_q, quiet=True)
        if data is None:
            return
        self._all = data["items"]

        # 先把品类树拉新,再决定「哪颗料算在哪张卡片下」:后者要用树上的 id
        # (见 _card_name_of / _is_uncat)。拿上一次的旧树来算,刚建出来的品类下面
        # 的料会被算成「没有品类」—— 卡片的库存数和「未分类」卡片跟着一起错。
        tree_data = self._load_cat_tree()

        self._cat_data = {}
        for it in self._all:
            self._cat_data.setdefault(self._card_name_of(it), []).append(it)

        # 首页卡片名单 = 品类树里的**顶层品类**,库里有几个就几张,一个不多一个不少。
        # 排序交给 _cat_rank(常见元件类排前面),不再有写死的 16 个名字 —— 写死就会
        # 画出库里没有对应行的空卡片,那种卡片点「删除…」删不掉(issue #32)。
        tops = sorted({(n.get("name") or "").strip() for n in self._cat_flat.values()
                       if n.get("parent_id") is None and (n.get("name") or "").strip()},
                      key=self._cat_rank)
        names = list(tops)
        # 「未分类」是**数据驱动**的:库里真有没有品类的元件,才摆这张卡片;一件
        # 都没有它就不存在 —— 这正是「未分类也能删干净」的实现方式(它不是一行品类,
        # 是一个算出来的入口,删不掉是因为没得删)。两个口径一起看:
        #   * 后端 list_categories 的 loose:整库有多少元件的 category_id 是空的
        #     (含零库存的料,不然卡片会随着库存变化忽隐忽现);
        #   * 首页这屏里对不上品类树的元件:兜住老数据里 id 指着一个已经被删掉的节点。
        # 后端哪天不报 loose 了,后一条仍然兜得住(取最大值,不互相覆盖)。
        loose = 0
        if isinstance(tree_data, dict):
            try:
                loose = int(tree_data.get("loose") or 0)
            except (TypeError, ValueError):
                loose = 0
        loose = max(loose, len(self._uncat_items(self._all)))
        if loose and UNCATEGORIZED not in names:
            names.append(UNCATEGORIZED)
        self._card_names = names

        self._render_cards()
        n = len(self._all)
        msg = f"{n} 种在库元件" if n else "还没有元件入库"
        self.count.set(msg)

        if self.view in ("cat", "pick"):
            if self.current_category in self._card_names:
                self._reopen_path()
            else:
                self.go_home()
        elif self.view == "search":
            if self.q.get().strip():
                self.open_search()
            else:
                self.go_home()

        # 选中设回去,Tk 会重新触发 _on_select,两张表跟着恢复
        self._restore_selection(keep)

    def _card_name_of(self, item):
        """这颗元件算在首页哪张卡片下。

        对不上品类树的(见 _is_uncat)一律算「未分类」:它可能还带着一个老的文本
        标签,但树上既然没有位置,首页上就只有「未分类」这张卡片装得下它 ——
        按文本分的话它会落进一张根本不存在的卡片里,人就再也找不到它了。
        """
        if self._is_uncat(item):
            return UNCATEGORIZED
        # 文本写的是**顶层大类名**(见 server._retag_category_subtree),而卡片
        # 就是顶层品类,所以直接对上
        return (item.get("category") or "").strip() or UNCATEGORIZED

    def _reopen_path(self):
        """刷新之后把用户留在原来那一层,而不是弹回首页。

        为什么不能直接 open_category(current_category):那只回到大类那一级,
        用户明明在看「电容 / 无极性陶瓷电容 / 0603」,刷一下(比如刚入完库)
        就被弹回「电容」,得重新点两次 —— 这是最招人烦的一类小毛病。
        """
        path = list(self.crumb)
        self._load_cat_tree()
        self.crumb = []
        for c in path:
            if c["kind"] == "cat":
                live = self._cat_flat.get(c.get("id"))
                if live is None:
                    # 树上没有这一级了。两种可能,不能一律把用户赶回首页:
                    #   * 「未分类」本来就不在树上(它是算出来的),留着这一层;
                    #   * 真被删掉了 -> 停在上一个还在的层级。
                    if (c.get("name") or "").strip() == UNCATEGORIZED:
                        self.crumb.append({"kind": "cat", "id": None,
                                           "name": UNCATEGORIZED,
                                           "path": UNCATEGORIZED})
                        continue
                    break
                self.crumb.append({"kind": "cat", "id": live["id"],
                                   "name": live["name"], "path": live["path"]})
            else:
                self.crumb.append(dict(c))
        if not self.crumb:
            self.go_home()
            return
        if not any(c["kind"] == "pkg" for c in self.crumb):
            self.f_pkg.set(self.ALL)
        self._descend()

    def _render_cards(self):
        specs = []
        for name in self._card_names:
            glyph, color = CATEGORY_STYLE.get(name, DEFAULT_CAT_STYLE)
            has = bool(self._cat_data.get(name))
            specs.append((name, name, glyph, color,
                          "点开查看 →" if has else "暂无库存", not has))
        # 这里只有一种情况会用到空态文字:库里一个品类都没有(卡片名单就是从品类树
        # 来的)。那就别再说「还没有元件入库」—— 那句话会让人以为货丢了,而他要做的
        # 其实是建一个品类(品类完全由他自己定,见 add_root_cat)。
        self.board.render(specs, empty_text=(
            "还没有品类。点上面的「＋ 新增大类」建一个 —— 品类完全由你自己定,"
            "库里真有的品类才会出现在这里。"))

    def _row_tags(self, cid, state=None):
        """这一行该挂哪些 tag —— 用户配的颜色优先(issue #28 / #34)。

        **先说清两条硬限制**(#34 里逐条实测过,别照旧注释想当然):
          * **单元格做不到**:`ttk.Treeview.item()` 只认
            `image / open / tags / text / values`,根本没有「某一个格子」这一层;
          * **连整列也做不到**:`tree.column(c, background=…)` 直接
            `TclError: unknown option "-background"` —— `column` 只管宽度、对齐
            这些小选项,没有任何配色项(旧注释写的「tag 是整行的、column 是整列的」
            是错的)。想给整列上色只能给每一列**每一行**都打 tag,那还是按行。
        所以能做的只有**整行**(tag),这里做的就是按行刷色(可以选中多行一起设)。
        要真做到 Excel 那样选中一格刷一格,只能自己用 Canvas 画一张表 ——
        那是把整个界面重写一遍,不值当。界面上也是按这个口径写死的
        (见 pal_bar 那排「ttk 表格只能整行配色」)。

        背景色和文字色分开存:只设了文字色的行,背景照旧走原来的规则色。

        内置那两条规则色(out 缺货红底 / low 缺料黄底)也是靠 tag 配背景的。
        (#38 之后后端 `stock_state` 只有 ok / low / out 三档,旧的那档 'short'
        已无任何来源,所以这里不用为它留分支、也没给它配过颜色。)
        同一行有多个 tag 都配了同一个选项时,**谁生效由 Tk 说了算,而且不是
        「行上 tags 列表的先后」** —— 实测(6 种创建顺序 + 把抢色的 tag 换成
        第 4 个)规则是**谁先 tag_configure 谁赢,`item(..., tags=[…])` 里
        写的顺序完全不起作用**(`build/test_gui.py` 的 effective_bg 就是按这条
        口径写的)。内置那两条在启动时就 configure 了,比后面才建的自定义 tag
        更早,所以不写死就会变成「有时红有时黄」。这里**不赌优先级**:用户明确
        设过背景色的就以用户为准(那就不再挂那条只配背景色的状态 tag),没设过
        背景色的才留着状态 tag。一条规则只有一个来源,不打架。
        """
        custom = self._row_colors.get(str(cid)) or {}
        fg, bg = custom.get("fg"), custom.get("bg")
        tags = []
        if fg or bg:
            tag = f"u{cid}"           # 一行一个 tag,各配各的颜色
            # 空串 = 这一项不设置(Tk 认这个写法),所以只设文字色时背景不受影响
            self.tree.tag_configure(tag, foreground=fg or "", background=bg or "")
            tags.append(tag)
        if state and not bg:
            tags.append(state)
        return tuple(tags)

    def _insert_component(self, it):
        # 顺序必须和 _build_cat 里的列定义一致 —— 这里是位置参数,不是按名字填的
        self.tree.insert("", "end", iid=str(it["id"]), values=(
            it.get("name") or "", it.get("lcsc_pn") or "", it.get("mpn") or "",
            it.get("marking") or "", it.get("package") or "",
            it.get("value") or "",
            # 每个槽按 _slot_attr 找自己那一列是哪个属性;元件没填这个属性就留空。
            # 空槽也要补 "",位置参数一个都不能少
            *[attrs.clean(it.get("params")).get(self._slot_attr.get(s, ""), "")
              for s in self.ATTR_SLOTS],
            it.get("on_hand") or 0, it.get("min_stock") or 0,
            STATE_LABEL.get(it.get("stock_state"), ""), it.get("note") or ""),
            # 用户配的行颜色按**元件 id**取,所以在哪一页、筛成什么样,
            # 同一颗料都是同一个颜色
            tags=self._row_tags(it.get("id"), it.get("stock_state")))

    def _cat_rank(self, name):
        """大类的显示顺序:常见元件类排前面,认不出来的按名称排在后面。"""
        try:
            return (0, CATEGORY_ORDER.index(name))
        except ValueError:
            return (1, name)

    def selected_id(self):
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def _attr_order(self):
        """属性列的先后:常用参数在前,其余按用户表里出现的顺序。

        顺序直接取自 attrs 的建议表(和出入库那一列的排列用的是同一套口径),
        不另外维护一份 —— 加品类建议时这里自动跟上。
        """
        out = []
        for name in list(attrs.GENERIC) + [n for lst in attrs.SUGGESTIONS.values()
                                          for n in lst]:
            if name not in out:
                out.append(name)
        return out

    def _auto_attr_cols(self, rows):
        """自动挑:这一页里**真有值**的属性,常用参数优先,默认最多 2 个。

        用户要的是「电容的耐压、精度直接就显示出来,不用点开」。列太宽会挤掉
        库存那些数字,所以默认只露两个;想多看几个去「列…」里勾。
        """
        seen = {}
        for it in rows:
            for name, val in attrs.clean(it.get("params")).items():
                if val:
                    seen[name] = seen.get(name, 0) + 1
        if not seen:
            return []
        order = {n: i for i, n in enumerate(self._attr_order())}
        ranked = sorted(seen, key=lambda n: (order.get(n, 999), -seen[n], n))
        return ranked[:2]

    # ------------------------------------------------------------ 列管理

    # 属性列的槽位。定义 12 个空槽、靠 displaycolumns 决定露哪几个、按什么顺序。
    ATTR_SLOTS = ["a%d" % _i for _i in range(1, 13)]

    def _base_cols(self):
        """表格里除属性槽以外的基础列(按定义顺序)。"""
        return [c for c in (str(x) for x in self.tree["columns"])
                if c not in self.ATTR_SLOTS]

    def _base_labels(self):
        """基础列的显示名 —— 直接取表头,省得再维护一份对照表。"""
        return {c: str(self.tree.heading(c, "text")) for c in self._base_cols()}

    def _default_cols(self, rows):
        """默认列:基础列按原顺序,再接上自动挑出来的属性列。"""
        return list(self._base_cols()) + ["@" + n for n in self._auto_attr_cols(rows)]

    def _apply_cols(self, rows):
        """摆好这一页要显示的列:**显示哪些、按什么顺序**都听用户的。"""
        specs = self._want_cols
        if specs is None:
            specs = self._default_cols(rows)
        base = self._base_cols()
        self._slot_attr = {}
        shown, slot_i = [], 0
        for spec in specs:
            spec = str(spec)
            key = spec[1:] if spec.startswith("@") else spec
            if key in base:
                shown.append(key)
            elif slot_i < len(self.ATTR_SLOTS):
                slot = self.ATTR_SLOTS[slot_i]
                self.tree.heading(slot, text=key)
                self._slot_attr[slot] = key
                shown.append(slot)
                slot_i += 1
        for slot in self.ATTR_SLOTS[slot_i:]:
            self.tree.heading(slot, text="")
        if not shown:                    # 一列都不勾,别给他一张空表
            shown = base[:1]
        try:
            self.tree.configure(displaycolumns=shown)
        except tk.TclError:
            pass

    def _attr_pool(self):
        """「列…」里能勾的属性名:常用建议 + 这一页出现过的。"""
        names = []
        for name in self._attr_order():
            if name not in names:
                names.append(name)
        for it in self._rows.values():
            for name, val in attrs.clean(it.get("params")).items():
                if val and name not in names:
                    names.append(name)
        return names[:20]

    def _col_cfg_path(self):
        """列设置(以及行配色)存哪。

        放在**数据库文件旁边** —— 那个目录一定可写(程序就在那儿跑),
        整个文件夹搬走时设置也跟着走,不会丢在别处的用户目录里。
        几个设置共用这一个文件、共用同一套读写:另起一套存储迟早会出现
        「盖子开了底没盖」那种半保存状态。
        """
        try:
            row = self.con.execute("PRAGMA database_list").fetchone()
            path = row[2] if row else ""
        except sqlite3.Error:
            path = ""
        if not path:
            return None
        return os.path.join(os.path.dirname(path), "ui_columns.json")

    def _save_cols(self):
        path = self._col_cfg_path()
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(db.dump_params({
                    "cols": self._want_cols,
                    "zero_stock": bool(self.zero_stock.get()),
                    # 行配色:键是元件 id 的字符串(JSON 的键只能是字符串),
                    # 值形如 {"fg": "#rrggbb", "bg": null}
                    "row_colors": self._row_colors,
                }))
        except OSError:
            pass              # 存不下就只影响"下次记住",不该弹错误打断人

    def _load_cols(self):
        path = self._col_cfg_path()
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                cfg = db.parse_params(f.read())
        except (OSError, ValueError):
            return
        if not isinstance(cfg, dict):     # 文件被人改坏了也当没有,别打断启动
            return
        # ---- 行配色。坏了 / 类型不对就当没有,不能因为一个配置文件启动不了
        raw_colors = cfg.get("row_colors")
        if isinstance(raw_colors, dict):
            colors = {}
            for key, val in raw_colors.items():
                if not isinstance(val, dict):
                    continue
                fg = val.get("fg") or None
                bg = val.get("bg") or None
                if fg or bg:
                    colors[str(key)] = {"fg": fg, "bg": bg}
            self._row_colors = colors
        if "zero_stock" in cfg:
            self.zero_stock.set(bool(cfg.get("zero_stock")))
        want = cfg.get("cols")
        if isinstance(want, list):
            self._want_cols = [str(x) for x in want]
            return
        # 兼容上一版存下来的 {"attrs": ["耐压", ...]}
        old = cfg.get("attrs")
        if isinstance(old, list):
            self._want_cols = ["@" + str(n) for n in old]

    def pick_columns(self):
        """自己勾要显示哪几个属性列(一个都不勾 = 恢复自动挑)。"""
        pool = self._attr_pool()
        if not pool:
            messagebox.showinfo(
                "还没有属性",
                "库里还没有任何元件填过自定义属性。\n\n"
                "在元件编辑窗口的「其他属性」里加上「耐压」「精度」这类名字,"
                "它们就会出现在这个列表里。", parent=self)
            return
        base = self._base_labels()
        cur = self._want_cols
        if cur is None:
            cur = self._default_cols(list(self._rows.values()))
        dlg = ColumnPickDialog(self, cur, pool, base, self._default_cols([]))
        self.wait_window(dlg)
        if not dlg.done:
            return
        self._want_cols = dlg.result
        self._save_cols()
        self.reload()
        self.app.set_status("列设置已保存,下次打开还是这样", 5)

    # ------------------------------------------------ 在表头上拖动换列(#33)

    def _screen_cols(self):
        """屏幕上从左到右的列,对应列设置清单里的哪几项(对不上就返回 None)。

        _apply_cols 把 "@耐压" 这类属性列塞进 a1..a12 槽里,所以屏幕上看到的列名
        是槽名(a1、a2…),序号和清单里的项对不上。拖动要改的必须是**唯一那份
        顺序出处**(_want_cols),所以每次都得先把屏幕列翻回清单项。

        屏幕列和清单项必须**一一对应**才敢按屏幕位置改清单顺序:数量对不上
        (加了 12 个以上的属性列、清单里有重复项等等)就返回 None 放弃这次拖动 ——
        宁可不响应,也不能顺手把某一列的显示/隐藏一起改了(#33 只许改顺序)。
        """
        keys = [str(c) for c in self.tree["displaycolumns"]]
        specs = self._want_cols
        if specs is None:
            # 没动过列设置时,屏幕上的就是「基础列 + 自动挑的属性列」这一份默认顺序
            specs = self._default_cols(list(self._rows.values()))
        specs = [str(s) for s in specs]
        base = self._base_cols()
        shown, slot_i = [], 0
        for spec in specs:
            key = spec[1:] if spec.startswith("@") else spec
            if key in base:
                shown.append(spec)
            elif slot_i < len(self.ATTR_SLOTS):
                shown.append(spec)
                slot_i += 1
        if len(shown) != len(specs) or len(keys) != len(specs):
            return None
        return specs

    # ---------------- issue #36:Excel 式单元格选中 ----------------
    # 这一版的边界(写清楚,免得被当成 bug):
    #   * 能选任意一格;按住 Shift/Ctrl 点,或者按住左键拖,就从起点扩成矩形(跨行跨列)。
    #   * **只画边框**:中间不铺任何控件 —— 铺了就会吃掉双击/右键,那两样不能退化。
    #   * 单格**底色**还没做(那是 #36 的另一半:色块必须转发事件,不能直接盖上去)。
    def _sel_cols(self):
        """屏幕上现有的列号:#1..#N(identify_column 给的就是这种)。"""
        return ["#%d" % i for i in range(1, len(self._screen_cols() or []) + 1)]

    def _sel_rows(self):
        """表格里的行顺序(摊平的),用来算"框选跨了哪几行"。"""
        out = []

        def _walk(parent=""):
            for iid in self.tree.get_children(parent):
                out.append(str(iid))
                _walk(str(iid))

        _walk()
        return out

    def _sel_cell_at(self, event):
        iid = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        if not iid or not col or col == "#0":
            return None
        return (str(iid), str(col))

    def _sel_clear(self):
        self._sel_cells = set()
        self._sel_anchor = None
        self._sel_paint()

    def _sel_extend(self, cur):
        """从锚点扩到当前格,凑成矩形 —— 这就是「多行多列框选」。"""
        if self._sel_anchor is None:
            self._sel_anchor, self._sel_cells = cur, {cur}
            return
        rows, cols = self._sel_rows(), self._sel_cols()
        ar, ac = self._sel_anchor
        cr, cc = cur
        if ar not in rows or cr not in rows or ac not in cols or cc not in cols:
            # 表格刚重建过,锚点已经不在这一屏了:当成本次是新起点
            self._sel_anchor, self._sel_cells = cur, {cur}
            return
        i0, i1 = sorted((rows.index(ar), rows.index(cr)))
        j0, j1 = sorted((cols.index(ac), cols.index(cc)))
        self._sel_cells = {(rows[i], cols[j])
                           for i in range(i0, i1 + 1)
                           for j in range(j0, j1 + 1)}

    def _sel_paint(self):
        """把 4 条边框摆到选中矩形的四边。中间**不铺任何东西**。

        这是 issue #36 的要害:铺满的覆盖层会把 <Button-1>/<Double-1>/<Button-3> 全吃掉,
        双击进编辑、右键菜单会当场失效 —— 所以中间那块必须留给 Treeview 自己接。
        """
        for f in self._sel_frames:
            f.place_forget()
        # 色块和边框要一起同步:先撤边框,再重铺色块,最后画新边框(边框在色块上面)
        self._paint_cell_colors()
        if not self._sel_cells:
            return
        rows, cols = self._sel_rows(), self._sel_cols()
        picked = [(r, c) for (r, c) in self._sel_cells if r in rows and c in cols]
        if not picked:
            return
        ri = [rows.index(r) for r, _c in picked]
        ci = [cols.index(c) for _r, c in picked]
        try:
            bx = self.tree.bbox(rows[min(ri)], cols[min(ci)])
            bx2 = self.tree.bbox(rows[max(ri)], cols[max(ci)])
        except tk.TclError:
            return
        # 任何一格滚出可视区(bbox 返回空串)就整体不画 —— 免得画出半截框、或者画错位置
        if not bx or not bx2:
            return
        x0, y0 = bx[0], bx[1]
        x1, y1 = bx2[0] + bx2[2], bx2[1] + bx2[3]
        w, h = max(2, x1 - x0), max(2, y1 - y0)
        top, bottom, left, right = self._sel_frames
        top.place(x=x0, y=y0, width=w, height=2)
        bottom.place(x=x0, y=y1 - 2, width=w, height=2)
        left.place(x=x0, y=y0, width=2, height=h)
        right.place(x=x1 - 2, y=y0, width=2, height=h)
        for f in self._sel_frames:
            f.lift()

    def _sel_click(self, event):
        """单击选一格;按住 Shift/Ctrl 就是从上一次的位置扩成矩形。

        **不返回 "break"**:行上的选中、双击进编辑、右键菜单都要照旧走。
        """
        cur = self._sel_cell_at(event)
        if cur is None:
            self._sel_clear()
            return None
        if event.state & 0x0005:          # Shift(0x1)/Control(0x4):扩选
            self._sel_extend(cur)
        else:
            self._sel_anchor, self._sel_cells = cur, {cur}
        self._sel_paint()
        return None

    def _sel_drag(self, event):
        """按住左键拖 = 从锚点框选(跨行跨列)。同样不吞事件。"""
        if self._sel_anchor is None:
            return None
        cur = self._sel_cell_at(event)
        if cur is None:
            return None
        self._sel_extend(cur)
        self._sel_paint()
        return None

    # ---- 单格底色(#36 的另一半):覆盖色块 + 事件转发 ----
    def set_cell_color(self, bg, cells=None):
        """给选中的格子刷底色(bg=None 表示清掉)。"""
        for rc in (self._sel_cells if cells is None else cells):
            if bg:
                self._cell_colors[(rc[0], rc[1])] = str(bg)
            else:
                self._cell_colors.pop((rc[0], rc[1]), None)
        self._sel_paint()

    def _cell_patch(self, rc):
        """取(没有就造)某一格的覆盖色块,并把鼠标事件转发回表格。"""
        fr = self._cell_patches.get(rc)
        if fr is None:
            fr = tk.Frame(self.tree, borderwidth=0, highlightthickness=0)
            for _name in ("<Button-1>", "<Double-1>", "<Button-3>",
                          "<B1-Motion>", "<ButtonRelease-1>"):
                fr.bind(_name,
                        lambda e, _f=fr, _n=_name: self._forward_to_tree(_f, _n, e))
            self._cell_patches[rc] = fr
        return fr

    def _forward_to_tree(self, fr, name, event):
        """把覆盖色块上的鼠标事件转发回表格本体。

        issue #36 的要害就是这一句:覆盖层会吃掉事件,不转发的话双击进编辑、
        右键菜单、拖动框选会全部失效。坐标必须换算 —— 覆盖层是 tree 的子控件,
        `winfo_x/y` 就是它在 tree 里的位置。
        """
        try:
            self.tree.event_generate(name, x=fr.winfo_x() + event.x,
                                     y=fr.winfo_y() + event.y)
        except tk.TclError:
            pass
        return "break"          # 覆盖层自己不要再处理一遍

    def _paint_cell_colors(self):
        """把有底色的格子铺上色块;滚出可视区 / 已经不在这一屏的一律撤掉。"""
        rows, cols = self._sel_rows(), self._sel_cols()
        for rc, fr in list(self._cell_patches.items()):
            item, col = rc
            if rc not in self._cell_colors or item not in rows or col not in cols:
                fr.place_forget()
                continue
            try:
                b = self.tree.bbox(item, col)
            except tk.TclError:
                b = None
            if not b:                       # 滚出可视区了:撤掉,别画到错的地方
                fr.place_forget()
                continue
            fr.configure(background=self._cell_colors[rc])
            fr.place(x=b[0], y=b[1], width=b[2], height=b[3])
            fr.lower()                      # 压到选中边框下面,边框才看得见

    def pick_cell_color(self):
        """右键菜单:给选中的格子挑一个底色。"""
        if not self._sel_cells:
            try:
                self.app.set_status("先点一格,或者按住 Shift / 拖动框选几格,再刷底色", 6)
            except AttributeError:
                pass
            return "break"
        cur = self._cell_colors.get(next(iter(self._sel_cells))) or "#ffe3e3"
        try:
            _rgb, hexv = colorchooser.askcolor(color=cur, parent=self)
        except tk.TclError:
            return "break"
        if hexv:
            self.set_cell_color(hexv)
        return "break"

    def clear_cell_color(self):
        """右键菜单:把选中格子的底色清掉(行色是另一套,不动)。"""
        self.set_cell_color(None)
        return "break"

    def _hdr_bounds(self):
        """屏幕上每一列的左右边界(像素),给横向拖动算落点用。

        优先用 bbox 量:那是真实渲染出来的位置,列被拉伸过、被横向滚动过都算得准。
        表里一行都没有时 bbox 返回空,退回按列宽累加(减掉横向滚动量)—— 那时
        反正没有行可看,落点差几个像素只是看着,不影响插到第几格。
        """
        keys = [str(c) for c in self.tree["displaycolumns"]]
        spans = []
        kids = self.tree.get_children()
        if kids:
            for i in range(len(keys)):
                # 用 "#N" 而不是列名:displaycolumns 里的第 N 个显示列就是 #N,
                # 不受这张表有没有 #0 树列的影响
                bb = self.tree.bbox(kids[0], "#%d" % (i + 1))
                if not bb:
                    spans = []
                    break
                spans.append((bb[0], bb[0] + bb[2]))
        if not spans and keys:
            widths = [int(self.tree.column(k, "width")) for k in keys]
            try:
                off = int(self.tree.xview()[0] * sum(widths))
            except (tk.TclError, ValueError, TypeError):
                off = 0
            x = -off
            for w in widths:
                spans.append((x, x + w))
                x += w
        return keys, spans

    def _hdr_slot_at(self, x, spans):
        """x 处松手会插到第几格(0..列数),以及提示线该画在哪个像素上。

        过了一列的中线就算插到它后面 —— 和「列…」窗口里上下拖时「过了一半就
        换位」是同一个手感,不用另学一套。线画在**列的边界**上,一眼看得出会
        插到哪两列之间。
        """
        for i, (x0, x1) in enumerate(spans):
            if x < (x0 + x1) // 2:
                return i, x0
        return len(spans), spans[-1][1]

    def _hdr_height(self):
        """表头那一行有多高(提示线画多长)。

        ttk 没有「表头的 bbox」这种接口,只能拿第一行数据的上沿当表头的下沿。
        一行都没有时退回一个经验值 —— 那种情况下线只画在空表上,长短只是看着,
        不参与落点判定。
        """
        kids = self.tree.get_children()
        if kids:
            bb = self.tree.bbox(kids[0])
            if bb:
                return max(8, bb[1])
        return 25

    def _hdr_drag_begin(self, event):
        """按在**列标题**上才开始:准备拖这一列。

        只有 identify_region == "heading" 才算我们的 —— 行上按下那一套(选中、
        双击、右键)一点都不能碰,两列之间那条分隔条上也要照旧能拖列宽。所以
        按在别处时这里什么都不记、返回 None,事件继续往下走,该谁处理谁处理。
        """
        self._hdr_src = self._hdr_slot = None
        try:
            if self.tree.identify_region(event.x, event.y) != "heading":
                return None
        except tk.TclError:
            return None
        if self._screen_cols() is None:      # 屏幕列和列设置对不上,这次不起拖
            return None
        keys, spans = self._hdr_bounds()
        if not keys or len(keys) != len(spans):
            return None
        for i, (x0, x1) in enumerate(spans):
            if x0 <= event.x < x1:
                self._hdr_src = i
                self._hdr_last_x = event.x
                # issue #35:跟手反馈 —— 一按住标题鼠标就变成左右箭头。
                # 没有这个的时候,拖的时候只有那条细细的提示线,连"我正在拖"都不明显。
                try:
                    self.tree.config(cursor="sb_h_double_arrow")
                except tk.TclError:
                    pass
                return "break"               # 表头上的按下由我们接管
        return None                          # 落在最后一列右边的空处:不起拖

    def _hdr_drag_motion(self, event):
        """拖着的时候算落点、画提示线。没在拖就返回 None —— 行上的拖动照旧。"""
        if self._hdr_src is None:
            return None
        self._hdr_last_x = event.x
        self._hdr_arm_scroll()               # issue #35:顶到左右边缘就开始自动滚
        return self._hdr_place(event.x)

    def _hdr_place(self, x):
        """按鼠标横坐标 x 算落点并画提示线(自动滚动之后也要用它重画)。"""
        keys, spans = self._hdr_bounds()
        if not spans or len(keys) != len(spans):
            return "break"
        slot, x = self._hdr_slot_at(x, spans)
        src = self._hdr_src
        # slot 是「插到老顺序的第几格之前」;把自己抽走之后,身后的下标都要往前挪
        # 一格,所以 slot == src 和 slot == src+1 都是「原地放下」—— 那就别画线,
        # 免得提示一次不会发生的换位(手抖一两像素不算拖动,和单击一样)。
        if slot in (src, src + 1):
            self._hdr_slot = None
            self.hdr_line.place_forget()
        else:
            self._hdr_slot = slot
            self.hdr_line.place(x=max(0, x - 1), y=0, width=2,
                                height=self._hdr_height())
            self.hdr_line.lift()
        return "break"

    def _hdr_arm_scroll(self):
        """鼠标贴近表格左/右边缘时启动「边缘自动滚动」(issue #35)。

        为什么用定时器、而不是在 motion 里滚一次就完:鼠标**贴着边不动**的时候
        不会再有 motion 事件,只滚一次就停住了 —— 而"想换到看不见的那一列"
        恰恰就是这种"顶到边上不动"的姿势。鼠标离开边缘就停(_hdr_scroll_tick 判断),
        松手也会停(见 _hdr_drag_end)。
        """
        if self._hdr_after is not None:
            return                           # 已经在滚了,别叠第二个定时器
        x, w = self._hdr_last_x, self.tree.winfo_width()
        if x is None or w <= 1:
            return
        if x <= 28 or x >= w - 28:
            self._hdr_after = self.tree.after(40, self._hdr_scroll_tick)

    def _hdr_scroll_tick(self):
        """自动滚动的一格:还在拖、鼠标还贴着边就继续滚,否则停。"""
        self._hdr_after = None
        x = self._hdr_last_x
        if self._hdr_src is None or x is None:
            return                           # 已经松手了
        w = self.tree.winfo_width()
        step = -1 if x <= 28 else (1 if x >= w - 28 else 0)
        if not step:
            return                           # 鼠标离开边缘:停,等下一次 motion 再起
        before = self.tree.xview()
        self.tree.xview_scroll(step, "units")
        if self.tree.xview() == before:
            return                           # 滚到头了,别再空转
        self._hdr_place(x)                   # 滚完把提示线重新贴到落点上
        self._hdr_after = self.tree.after(40, self._hdr_scroll_tick)

    def _hdr_drag_end(self, _event=None):
        """松手:真拖了就写回新顺序并存盘;只是在标题上点了一下就什么都不改。"""
        # issue #35:收拖必须把自动滚动停掉、鼠标形状还原 —— 漏掉任何一样,
        # 后面整张表都会一直自己滚 / 鼠标一直卡在左右箭头
        if self._hdr_after is not None:
            try:
                self.tree.after_cancel(self._hdr_after)
            except Exception:  # noqa: BLE001
                pass
            self._hdr_after = None
        self._hdr_last_x = None
        try:
            self.tree.config(cursor="")
        except tk.TclError:
            pass
        src, slot = self._hdr_src, self._hdr_slot
        self._hdr_src = self._hdr_slot = None
        self.hdr_line.place_forget()
        if src is None:
            return None                      # 不是我们接管的,原样放行
        if slot is None:
            return "break"                   # 按下又原地松开:顺序一点没动
        if slot > src:
            slot -= 1                        # 抽走自己,身后的下标往前挪一格
        if slot == src:
            return "break"
        specs = self._screen_cols()
        if specs is None or src >= len(specs) or slot > len(specs):
            return "break"
        specs.insert(slot, specs.pop(src))
        # 顺序只有 _want_cols 这一份出处(见 _apply_cols)。写回它、存盘、重画。
        # 必须 reload 而不是只 configure(displaycolumns=...):属性列是按顺序占
        # a1..a12 槽的,顺序一变,槽和属性的对应关系跟着变 —— 只改显示顺序的话,
        # 「耐压」的标题下会摆着「精度」的值。reload 会把行按新的槽重新插一遍。
        self._want_cols = specs
        self._save_cols()                    # 和「列…」共用同一份 ui_columns.json
        self.reload()
        self.app.set_status("列顺序已保存,下次打开还是这样", 5)
        return "break"                       # 这一下是我们接管的,得吞掉

    def _restore_selection(self, cid):
        """刷新之后把选中还回去。

        重建表格会触发 <<TreeviewSelect>>,而那次回调里 selected_id() 是 None ——
        _on_select 一进来就清空「仓位分布 / 最近流水」,于是这两张表每次刷新都变空白,
        看起来像出入库没记流水。把选中设回去,Tk 会再触发一次 _on_select,两张表回来。
        """
        if not cid or not self.tree.exists(str(cid)):
            return
        self.tree.selection_set(str(cid))
        self.tree.see(str(cid))

    def selected_ids(self):
        """选中的全部元件 id。表格是多选的 —— 批量挪品类靠它。"""
        out = []
        for iid in self.tree.selection():
            try:
                out.append(int(iid))
            except (TypeError, ValueError):
                continue
        return out

    # ------------------------------------------------------ 选中与右键

    def _on_double(self, event):
        if self.tree.identify_row(event.y):
            self.edit()

    def _on_select(self, _event=None):
        # 工具条那两个色块跟着选中行走(#34):按钮自身显示当前选中行的配色。
        # 放在最前面,是因为「一行都没选」那条提前 return 的路上也得把按钮还原。
        self._sync_color_buttons()
        cid = self.selected_id()
        clear_tree(self.t_stock)
        clear_tree(self.t_hist)
        if not cid:
            return
        det = call(self.con, server.get_component, match=(cid,), quiet=True)
        if not det:
            return
        for lot in det.get("stock_by_location") or []:
            self.t_stock.insert("", "end", values=(lot["code"], lot["qty"]))
        for mv in det.get("movements") or []:
            self.t_hist.insert("", "end", values=(
                (mv.get("created_at") or "")[:19], KIND_LABEL.get(mv.get("kind"), mv.get("kind")),
                mv.get("qty"), mv.get("location_code") or "",
                mv.get("project_name") or "", mv.get("note") or ""))

    def _popup(self, event):
        row = self.tree.identify_row(event.y)
        if not row:
            return
        # 只在「右键的这一行**不在**选中集合里」时才切换选中(#34 修的真 bug)。
        # 原来这里无条件 selection_set(row):用户 Ctrl 选中 8/10/7 三行,对着中间
        # 那行一点右键,选中集合就被顶成只剩 ('10',) —— 说明书里承诺的「选中多行
        # 一起设色」用鼠标根本走不通(上一轮自检直接调 pick_row_color 绕过了
        # _popup,所以一直没查出来)。右键**已在选中集合里**的行不该动选中,
        # 这也是 Windows 资源管理器 / Excel 的规矩。
        if row not in self.tree.selection():
            self.tree.selection_set(row)
        self.menu.tk_popup(event.x_root, event.y_root)

    # ------------------------------------------------------ 动作

    def _quick_move(self, kind):
        cid = self.selected_id()
        if not cid:
            messagebox.showinfo("提示", "请先选中一个元件。", parent=self)
            return
        dlg = MoveDialog(self, self.app, cid, kind)
        self.app.wait_window(dlg)
        if dlg.done:
            self.app.refresh_all()

    def add(self):
        # 把「我正站在哪一级」带进弹窗 —— 不带的话新料会被后端兜底丢到根级「其他」,
        # 和用户所在的位置毫无关系(用户报的正是这个)。
        # 只认 view=="cat":搜索页复用同一张表,但 crumb 里还留着上一次钻取的路径,
        # 照带就会把料挂到上一次那个品类上。
        node = self._last_cat() if self.view == "cat" else None
        dlg = ComponentDialog(self, self.app, None, cat_id=(node or {}).get("id"))
        self.app.wait_window(dlg)
        if dlg.done:
            self.app.refresh_all()
            # 加完就把人送到那颗料上:开关关着就替他打开,否则他面对的还是一张
            # 看不见新料的表(issue #22)
            new_id = getattr(dlg, "new_id", None)
            if new_id:
                if not self.zero_stock.get():
                    self.zero_stock.set(True)
                    self._save_cols()
                self.reload()
                self._restore_selection(new_id)
                try:
                    self.tree.see(str(new_id))
                except tk.TclError:
                    pass
                self.app.set_status("已加好,并定位到刚新增的这一颗", 6)

    def quick_in(self):
        dlg = QuickInDialog(self, self.app)
        self.app.wait_window(dlg)
        if dlg.done:
            self.app.refresh_all()

    def batch_in(self, out=False):
        """库存页的批量入库/出库都走这里。"""
        dlg = BatchInDialog(self, self.app)
        self.app.wait_window(dlg)
        if dlg.done:
            self.app.refresh_all()

    def edit(self):
        cid = self.selected_id()
        if not cid:
            messagebox.showinfo("提示", "请先选中一个元件。", parent=self)
            return
        dlg = ComponentDialog(self, self.app, cid)
        self.app.wait_window(dlg)
        if dlg.done:
            self.app.refresh_all()

    def delete(self):
        cid = self.selected_id()
        if not cid:
            # 不许静默返回:右键菜单里点了「删除」却没反应,用户只会再点几次
            self.app.set_status("请先选中要删的元件(也可以按住 Ctrl / Shift 多选)", 5)
            return
        item = self._rows.get(cid) or {}
        name = item.get("name") or f"#{cid}"
        if not messagebox.askyesno("删除元件", f"确定删除「{name}」吗?\n\n此操作不可撤销。", parent=self):
            return
        res = call(self.con, server.delete_component, query={}, match=(cid,), parent=self, quiet=True)
        if res is None:
            if messagebox.askyesno(
                    "该元件被 BOM 引用",
                    "删除失败:可能被项目 BOM 引用。\n\n"
                    "强行删除会同时移除它在各项目 BOM 里的行(库存流水会保留)。\n继续吗?", parent=self):
                res = call(self.con, server.delete_component, query={"force": "1"},
                           match=(cid,), parent=self, quiet=True)
                if res is None:
                    self.app.set_status(f"「{name}」没删掉,数据一点没动", 6)
                    return
            else:
                self.app.set_status(f"「{name}」没删,数据一点没动", 5)
                return
        self.app.set_status(f"删掉了元件「{name}」", 5)
        self.app.refresh_all()

    # ------------------------------------------------------ 行配色(#28 / #34)

    # 调色盘上的常用色(issue #34)。挑法:前面几个是能当**字色**用的深色
    # (黑 / 红 / 橙 / 黄褐 / 绿 / 蓝 / 紫 / 灰),后面几个浅的当**填充**色,
    # 最后那个 #fff6dd 和内置的「偏低黄底」是同一个值 —— 手动染过色的人想再
    # 染回一模一样的黄,不用去开系统调色板对色号。要做成常量而不是各写一处:
    # 色板、自检、以后想加「最近用过的颜色」都得认同一份。
    PALETTE = ("#000000", "#c0392b", "#e67e22", "#b7950b", "#1e8449",
               "#1f618d", "#6c3483", "#7f8c8d", "#ffffff", "#ffe3e3",
               "#fff6dd")

    def show_palette(self, which):
        """点「字体颜色 / 填充颜色」→ 在下面摊开一排色块(issue #34)。

        为什么把色板**摊在工具条里**,而不是弹一个下拉菜单 / 对话框:
          * 用户要的是「像 Excel 那样点一格就改色」,每点一次都弹一个系统调色板
            正好是最烦人的那种交互;
          * 弹出菜单要走 `tk_popup` / grab,而这套东西的自检里**不许真弹菜单**
            (见 build/checks/_stub_dialogs.py)—— 摊在这一页里既好验也不拦人。
        """
        self.pal_target = which
        for w in self.pal_panel.winfo_children():
            w.destroy()          # 重开时只重建色块,免得越点越多
        name = "字体颜色" if which == "fg" else "填充颜色(即背景色)"
        ttk.Label(self.pal_panel, text=f"{name}:", style="Dim.TLabel").pack(
            side="left", padx=(6, 4))
        self.pal_swatches = {}
        for color in self.PALETTE:
            sw = tk.Button(self.pal_panel, width=3, height=1, bg=color,
                           activebackground=color, relief="ridge", bd=1,
                           # 色块上不写字:写上去就把颜色本身挡住了,而这里卖的
                           # 就是颜色。具体色号看右边那行状态说明。
                           command=lambda c=color: self.pick_row_color(
                               self.pal_target, c))
            sw.pack(side="left", padx=1, pady=1)
            self.pal_swatches[color] = sw
        ttk.Button(self.pal_panel, text="更多颜色…",
                   command=lambda: self.pick_row_color(self.pal_target)).pack(
            side="left", padx=(8, 4))
        ttk.Button(self.pal_panel, text="收起", command=self.hide_palette).pack(
            side="left")
        self.pal_panel.pack(fill="x", pady=(2, 0))
        self.app.set_status(
            f"点一格色块,就给选中的行设{name}(可 Ctrl / Shift 多选)", 6)

    def hide_palette(self):
        """收起色板。

        用 pack_forget 而不是 destroy:收起只是「先别占地方」,下次点开还得用,
        反复新建再加销毁没有好处。
        """
        self.pal_target = None
        self.pal_panel.pack_forget()

    def pick_row_color(self, which, picked=None):
        """给选中的行设文字色 / 填充色(which = "fg" / "bg")。

        工具条色块和右键菜单**走的是同一条路**,这是刻意的:色块直接把颜色当
        picked 传进来,右键那两个菜单项不传,那就弹标准库的 colorchooser
        (系统调色板,零依赖)兜底。取色返回 ((r,g,b), "#rrggbb");用户点取消时
        是 (None, None),那就什么都不改。
        """
        ids = self.selected_ids()
        if not ids:
            self.app.set_status("先选中要上色的行(按住 Ctrl / Shift 可以选多行)", 5)
            return
        if picked is None:
            title = "选文字颜色" if which == "fg" else "选填充颜色(背景色)"
            _rgb, picked = colorchooser.askcolor(parent=self, title=title)
            if not picked:
                return                   # 用户取消了,不改任何东西
        self._apply_row_color(which, str(picked), ids)

    def _apply_row_color(self, which, color, ids):
        """真正落盘 + 落到屏幕上的那一步 —— 两个入口共用,不许各写一套。

        上完色**顺手取消这几行的选中**,这是 #34 里最要紧的一处:右键那一行本身
        就是选中行,而 ttk 的选中态蓝色高亮会把整行自定义色**整个盖住**(像素实测:
        选中时自定义色 0 像素 / 高亮蓝 12939 像素;取消选中才看得到 12841 像素)。
        不取消的话,用户点完颜色屏幕上一点变化都没有,当然认为「你根本没做」。
        取舍认了:想接着设另一种颜色(比如设完字色再设填充)得重新选一次;
        换来的是「点完立刻看得见」。状态栏也照实说一句,免得人以为还要做别的。
        """
        for cid in ids:
            slot = self._row_colors.setdefault(str(cid), {"fg": None, "bg": None})
            slot[which] = color
        self._save_cols()
        self._recolor_rows(ids)
        self._deselect(ids)
        self._sync_color_buttons()
        what = "字体颜色" if which == "fg" else "填充颜色"
        self.app.set_status(
            f"{len(ids)} 行的{what}设成了 {color}(已取消选中,现在就能看到)", 6)

    def _deselect(self, ids):
        """把这几个 iid 从选中集合里摘掉(别的行不受影响)。"""
        live = [str(c) for c in ids if self.tree.exists(str(c))]
        if live:
            self.tree.selection_remove(*live)

    def _sync_color_buttons(self):
        """把两个色块按钮刷成**当前选中行**的颜色(按钮自身显示当前颜色)。

        没选中行时还原成系统默认底色:色块上的颜色只说一件事 ——「你现在选中的
        这一行是什么颜色」。留着上一次点过的颜色会让人以为当前行已经是那个色。
        """
        if not hasattr(self, "btn_fg") or not hasattr(self, "tree"):
            # 建这一排的时候表格还没建出来(_build_cat 是先铺工具条、后建树的),
            # 那一刻没有选中行可取 —— 交给后面第一次 <<TreeviewSelect>> 去刷。
            # 少了这一句,启动时直接 AttributeError。
            return
        ids = self.selected_ids()
        colors = getattr(self, "_row_colors", None) or {}
        # 多选时按**第一行**显示:多行颜色不一样的话没法用一个色块表达,
        # 状态栏那行会写清「选中 N 行」
        custom = (colors.get(str(ids[0])) or {}) if ids else {}
        for which, btn, name in (("fg", self.btn_fg, "字体颜色"),
                                 ("bg", self.btn_bg, "填充颜色")):
            color = custom.get(which)
            try:
                if color:
                    # 按钮底色 = 这一行的颜色;文字色按亮度反着来,深色底上才看得见
                    btn.configure(bg=color, activebackground=color,
                                  fg=readable_fg(color), text=name)
                else:
                    # 还原成系统默认外观。字色必须用**建按钮时记下的那个默认值**:
                    # configure(fg="") 不是「恢复默认」,Tk 会直接
                    # `TclError: unknown color name ""`。
                    btn.configure(bg=self._btn_face, activebackground=self._btn_face,
                                  fg=self._btn_fg_face, text=name)
            except tk.TclError:
                # 这两个按钮纯装饰:万一某个颜色字符串 Tk 不认,绝不能把
                # 「选中行 → 填流水/仓位」那条路带崩 —— _on_select 是 Tk 回调,
                # 里面抛异常只会被 Tk 吞掉,表现成右下角两张表莫名空白(踩过)。
                pass
        if not ids:
            self.pal_now.set("未选中行:先选一行(可 Ctrl / Shift 多选)再点色块")
        else:
            got = colors.get(str(ids[0])) or {}
            self.pal_now.set(
                f"选中 {len(ids)} 行 · 字体 {got.get('fg') or '默认'} · "
                f"填充 {got.get('bg') or '默认(规则色)'}")

    def clear_row_colors(self):
        """把选中行的自定义配色清掉,回到默认(含内置的规则色)。

        和上色一样,清完也取消选中 —— 不取消的话规则色同样被选中高亮盖着,
        用户会以为「清了个寂寞」。
        """
        ids = self.selected_ids()
        if not ids:
            self.app.set_status("先选中要清除配色的行", 5)
            return
        n = sum(1 for cid in ids if self._row_colors.pop(str(cid), None))
        self._save_cols()
        self._recolor_rows(ids)
        self._deselect(ids)
        self._sync_color_buttons()
        if n:
            self.app.set_status(f"{n} 行的自定义配色清掉了,回到默认", 5)
        else:
            self.app.set_status("选中的行本来就没有自定义配色", 4)

    def _recolor_rows(self, ids):
        """就地换掉这几行的 tag。

        故意**不整表重建**:重建会顺手把选中、右边「仓位分布 / 最近流水」一起
        清掉,刷个颜色还把用户挑好的行弄丢是最招人烦的。
        内置规则色跟着改回来,靠的还是 _row_tags 那一套(用户没设背景就还给
        规则色)。
        """
        for cid in ids:
            iid = str(cid)
            if not self.tree.exists(iid):
                continue
            it = self._rows.get(cid) or {}
            self.tree.item(iid, tags=self._row_tags(cid, it.get("stock_state")))


# --------------------------------------------------------------------- 出入库

def category_options(con):
    """品类下拉的选项。统一从后端 meta 拿,免得界面上再维护第二份清单。"""
    meta = call(con, server.meta, quiet=True) or {}
    return list(meta.get("categories") or []) or ["其他"]


class ColumnPickDialog(tk.Toplevel):
    """列管理:显示哪些列、按什么顺序。

    用户的原话:「重要的东西显示在前面,不要的就用户自己隐藏掉」「显示的顺序也要
    可调,我想把耐压放在前面我就放在前面」。所以这里是一张**能勾、能挪**的清单,
    而不是一排写死的复选框。

    挪有两种办法,都得留着:
      * ↑ ↓ 按钮 —— 一格一格挪,一次只动一位,手不离开键盘也能用;
      * 按住一行上下拖 —— 直接拖到目标位置(见 _drag_* 那几个方法)。

    #26 加拖动时特意**没有动** ↑↓:一次挪好几位的场景拖动快得多,而「就差一位」
    的时候按钮比精确拖动省事(也不容易拖过头),两个入口各有各的用处。
    """

    def __init__(self, parent, current, pool, base, default):
        super().__init__(parent)
        self.title("显示哪些列、按什么顺序")
        self.transient(parent)
        self.resizable(False, False)
        self.done = False
        self.result = None
        self._base = dict(base)
        self._default = list(default)
        self._shown = [str(s) for s in current]
        # 拖动的状态。_drag_i 是「按下的那一行」在 self.items 里的下标,
        # _drag_slot 是「松手会插到第几格」(0..len(items))。松手前不动
        # self.items —— 拖动过程中表里行的 iid 就是下标,先把表改了的话
        # 下标全乱,后面每一次 motion 都得重新找自己拖的是哪一行。
        self._drag_i = None
        self._drag_slot = None

        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body,
                  text="双击一行 = 显示 / 隐藏;按住一行上下拖 = 调顺序;"
                       "也可以用 ↑ ↓ 一格一格挪(越靠上,表格里越靠左):",
                  style="Dim.TLabel").pack(anchor="w", pady=(0, 6))

        wrap = ttk.Frame(body)
        wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(wrap, columns=("mark", "name", "where"),
                                 show="headings", height=14, selectmode="browse")
        # 对齐一律居中(#27)。这里原来是「显示」居中、「列名」左对齐、「来源」居中,
        # 一张表里就混着两种;列定义里那个对齐位不再需要了。
        for key, title, wdt in (("mark", "显示", 46),
                                ("name", "列名", 170),
                                ("where", "来源", 70)):
            self.tree.heading(key, text=title, anchor="center")
            self.tree.column(key, width=wdt, anchor="center", stretch=(key == "name"))
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        # 拖动时那条插入位置指示线。做成一条独立的小控件压在表格上,而不是给
        # 目标行换底色:换底色说不清是插在它上面还是下面,而拖到列表末尾时
        # 根本没有「目标行」可以高亮 —— 那种「拖到最后一位」恰恰是最常见的操作。
        # 它平时 place_forget(),只在拖动中露脸。
        self.line = tk.Frame(wrap, height=2, bg="#2f6fd0", bd=0, highlightthickness=0)
        self.tree.bind("<Double-1>", self._toggle)
        self.tree.bind("<space>", self._toggle)
        self.tree.bind("<ButtonPress-1>", self._drag_begin)
        self.tree.bind("<B1-Motion>", self._drag_motion)
        self.tree.bind("<ButtonRelease-1>", self._drag_end)

        # 清单 = 已显示的(按用户顺序)在前,其余基础列 / 属性列排后面
        rest = self._default + ["@" + n for n in pool]
        self.items = []
        for spec in self._shown + [s for s in rest if s not in self._shown]:
            if spec not in self.items:
                self.items.append(spec)
        self._refresh()

        bar = ttk.Frame(body)
        bar.pack(fill="x", pady=(8, 0))
        ttk.Button(bar, text="↑ 上移", command=lambda: self._move(-1)).pack(side="left")
        ttk.Button(bar, text="↓ 下移", command=lambda: self._move(1)).pack(side="left", padx=4)
        ttk.Button(bar, text="全选", command=lambda: self._all(True)).pack(side="left", padx=(12, 0))
        ttk.Button(bar, text="全不选", command=lambda: self._all(False)).pack(side="left", padx=4)
        ttk.Button(bar, text="恢复默认", command=self._reset).pack(side="left", padx=(12, 0))

        add = ttk.Frame(body)
        add.pack(fill="x", pady=(8, 0))
        ttk.Label(add, text="再加一列属性:").pack(side="left")
        self.v_new = tk.StringVar()
        ent = ttk.Entry(add, textvariable=self.v_new, width=16)
        ent.pack(side="left", padx=6)
        ent.bind("<Return>", lambda _e: self._add())
        ttk.Button(add, text="加进去", command=self._add).pack(side="left")
        ttk.Label(add, text="(元件没填这个属性,那一格就是空的)",
                  style="Dim.TLabel").pack(side="left", padx=8)

        foot = ttk.Frame(self, padding=(12, 0, 12, 12))
        foot.pack(fill="x")
        ttk.Button(foot, text="确定", command=self._ok).pack(side="right")
        ttk.Button(foot, text="取消", command=self.destroy).pack(side="right", padx=6)
        self.bind("<Escape>", lambda _e: self.destroy())
        self.tree.focus_set()

    # ---------------------------------------------------------- 内部
    def _label(self, spec):
        spec = str(spec)
        if spec.startswith("@"):
            return spec[1:]
        return self._base.get(spec) or spec

    def _refresh(self):
        keep = self.tree.selection()
        keep = keep[0] if keep else None
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        for i, spec in enumerate(self.items):
            self.tree.insert("", "end", iid=str(i), values=(
                "✔" if spec in self._shown else "",
                self._label(spec),
                "属性" if str(spec).startswith("@") else "元件字段"))
        kids = self.tree.get_children()
        if keep and self.tree.exists(keep):
            self.tree.selection_set(keep)
            self.tree.see(keep)
        elif kids:
            self.tree.selection_set(kids[0])

    def _sel(self):
        sel = self.tree.selection()
        if not sel:
            return None
        try:
            return int(sel[0])
        except (TypeError, ValueError):
            return None

    def _toggle(self, _event=None):
        i = self._sel()
        if i is None:
            return
        spec = self.items[i]
        if spec in self._shown:
            self._shown.remove(spec)
        else:
            self._shown.append(spec)
        self._synced_shown()
        self._refresh()

    def _move(self, d):
        i = self._sel()
        if i is None:
            return
        j = i + d
        if not 0 <= j < len(self.items):
            return
        self.items[i], self.items[j] = self.items[j], self.items[i]
        self._synced_shown()
        self._refresh()
        self.tree.selection_set(str(j))
        self.tree.see(str(j))

    # ---------------------------------------------------------- 拖动调序(#26)

    def _drag_begin(self, event):
        """按下一行 = 准备拖它。此刻只记「拖的是哪一行」,不动清单。"""
        iid = self.tree.identify_row(event.y)
        self._drag_slot = None
        self._drag_i = int(iid) if str(iid).isdigit() else None
        if self._drag_i is not None:
            # 按下的行先选中:拖着的时候一眼看得出拖的是哪一行
            self.tree.selection_set(iid)

    def _drag_motion(self, event):
        if self._drag_i is None:
            return
        # 拖到框子上沿 / 下沿就自动滚一屏 —— 属性列可以加到十几个,14 行的框
        # 装不下,不滚的话远处的行根本拖不到。
        h = self.tree.winfo_height()
        if event.y < 14:
            self.tree.yview_scroll(-1, "units")
        elif event.y > h - 14:
            self.tree.yview_scroll(1, "units")
        slot, y = self._slot_at(event.y)
        if slot != self._drag_slot:
            self._drag_slot = slot
            self._show_line(y)

    def _drag_end(self, _event=None):
        src, slot = self._drag_i, self._drag_slot
        self._drag_i = self._drag_slot = None
        self.line.place_forget()
        if src is None or slot is None:
            return          # 只是点了一下,没拖
        # slot 是「插到老清单的第几格之前」。把自己抽走之后,它后面的下标都会
        # 往前挪一格,所以目标在身后时要减一 —— 不减的话往下拖会少走一位。
        if slot > src:
            slot -= 1
        if slot == src:
            return          # 原地放下(包括拖回自己头上),什么都不用做
        spec = self.items.pop(src)
        self.items.insert(slot, spec)
        # 勾选状态跟「哪些被勾上」走,不跟位置走:拖动只改顺序,原来勾着的
        # 拖完还得勾着(#26 的硬要求)。顺便把 _shown 也按新顺序摆好 ——
        # 它的顺序不影响确定后的结果(结果按 items 的顺序取,见 _ok),
        # 但两份清单顺序一致,以后读代码的人不用再去分辨哪份说了算。
        self._synced_shown()
        self._refresh()
        self.tree.selection_set(str(slot))
        self.tree.see(str(slot))

    def _slot_at(self, y):
        """y 处松手会插到第几格(0..len(items)),以及指示线该画在多高。

        按「行的上半 / 下半」决定插在它前面还是后面,和大多数列表一样:
        光标落在上半就是插到这一行前面,落在下半就是插到它后面。这样一条
        指示线就能把落点说清楚,不会出现「拖到某一行上,到底插前还是插后」。
        """
        iid = self.tree.identify_row(y)
        if str(iid).isdigit():
            k = int(iid)
            bb = self.tree.bbox(iid)
            if bb:
                top, height = bb[1], bb[3]
                if y >= top + height // 2:
                    return k + 1, top + height
                return k, top
            return k, None
        # 落在行与行之间的空白(或表格下沿的空白)上:靠上就是最前,靠下就是最后
        kids = self.tree.get_children()
        if not kids:
            return 0, None
        top_bb = self.tree.bbox(kids[0])
        end_bb = self.tree.bbox(kids[-1])
        if top_bb and y < top_bb[1]:
            return 0, top_bb[1]
        if end_bb:
            return len(self.items), end_bb[1] + end_bb[3]
        return len(self.items), None

    def _show_line(self, y):
        if y is None:
            self.line.place_forget()
            return
        width = self.tree.winfo_width()
        self.line.place(x=0, y=max(0, y - 1), width=width, height=2)
        self.line.lift()

    def _synced_shown(self):
        """把 _shown 按 items 的顺序重摆一遍(只动顺序,不动勾没勾)。"""
        self._shown = [s for s in self.items if s in self._shown]

    def _all(self, state):
        self._shown = list(self.items) if state else []
        self._refresh()

    def _reset(self):
        self._shown = list(self._default)
        rest = self._default + [s for s in self.items if s not in self._default]
        self.items = []
        for spec in rest:
            if spec not in self.items:
                self.items.append(spec)
        self._refresh()

    def _add(self):
        name = attrs.norm(self.v_new.get())
        if not name:
            return
        spec = "@" + name
        if spec not in self.items:
            self.items.append(spec)
        if spec not in self._shown:
            self._shown.append(spec)
        self.v_new.set("")
        self._refresh()
        self.tree.selection_set(str(self.items.index(spec)))
        self.tree.see(str(self.items.index(spec)))

    def _ok(self):
        # 结果按 self.items 的顺序取 —— 它就是清单上从上到下的那个可见顺序,
        # 所以拖动改的是 items,确定之后表格那一列序就跟着变。_shown 只回答
        # 「勾没勾」(以及一键全选/恢复默认时的集合),不要拿它的顺序当列序:
        # 它是「先勾谁谁在前」的追加顺序,和用户眼睛看到的顺序不是一回事。
        picked = [s for s in self.items if s in self._shown]
        self.result = picked or None      # 一列都不勾 = 恢复默认
        self.done = True
        self.destroy()

class CategoryStepBox(ttk.Frame):
    """一级一个框的品类选择器:选完上一级才出下一级,没有了就停。

    单独做成一个控件,是因为有三处要用:独立的「选品类」窗口、BOM 行上
    「这一行算哪一类」那个窗口(它还得额外允许直接打字)、还有自检。

    **每个框里只有这一级的名字**。以前是一个装着全路径的长下拉:C0805 那种
    一长串,框里只看得见结尾那几个字,用户根本不知道自己在选哪一支 ——
    而选错品类的代价是这颗料以后按品类找不到。

    每一级右边都能「＋ 新建…」:挑到一半发现「钽电容」这一档还没有,当场加上,
    不用关掉窗口跑去品类管理里加完再回来重挑。新建的节点挂在这一级选中的
    那个节点下面 —— 用户点的位置就是他想要的位置。
    """

    LEVELS = ["大类", "二级", "三级", "四级", "五级", "六级"]

    def __init__(self, parent, con, on_change=None):
        super().__init__(parent)
        self.con = con
        self.on_change = on_change
        self._items, self._by_id = [], {}
        self._rows, self._sel = [], {}
        self._busy = False
        self.fetch()
        self.seed(None)

    # ---------------------------------------------------------------- 数据
    def fetch(self):
        data = call(self.con, server.list_categories, quiet=True) or {}
        self._items = list(data.get("items") or [])
        # flat 和 items 里是同一批 dict(后端就建了一份),children 已经挂好了
        self._by_id = {n["id"]: n for n in (data.get("flat") or [])}

    def kids(self, pid):
        """某一级的候选。名字不叫 children 是因为 Tkinter 的控件自己有个
        `children` 实例属性(子控件字典),同名的方法会被它遮住。"""
        if pid is None:
            return self._items
        node = self._by_id.get(pid)
        return list(node.get("children") or []) if node else []

    # ---------------------------------------------------------------- 铺框
    def reset(self):
        """清空重铺。品类在别处改过之后(比如刚在品类管理里加过)要重来一次。"""
        self.fetch()
        self.seed(None)

    def seed(self, cat_id):
        """按已有的归属把框铺好,让人一眼看出它在树的哪一层。"""
        self._busy = True            # 铺初始状态时别让选择事件再往下铺一级
        try:
            for w in self.winfo_children():
                w.destroy()
            self._rows, self._sel = [], {}
            chain, node = [], self._by_id.get(cat_id) if cat_id else None
            while node is not None:          # 从自己往上数祖先
                chain.append(node)
                node = self._by_id.get(node["parent_id"])
            chain.reverse()                  # 再从上往下铺
            pid = None
            for n in chain:
                self._grow(pid, preselect=n["id"])
                pid = n["id"]
            if not chain:
                self._grow(None)
            elif self.kids(chain[-1]["id"]):
                # 它下面还有子类,再铺一级空的 —— 想往里选就能直接选
                self._grow(chain[-1]["id"])
        finally:
            self._busy = False
        self._notify()

    def _grow(self, pid, preselect=None):
        level = len(self._rows)
        kids = self.kids(pid)
        frame = ttk.Frame(self)
        frame.pack(fill="x", pady=2)
        name = self.LEVELS[level] if level < len(self.LEVELS) else f"{level + 1}级"
        ttk.Label(frame, text=f"选{name}", width=7, style="Dim.TLabel").pack(side="left")
        var = tk.StringVar()
        cb = ttk.Combobox(frame, textvariable=var, width=20, state="readonly",
                          values=[n["name"] for n in kids])
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda _e, lv=level: self._on_pick(lv))
        ttk.Button(frame, text="＋ 新建…", width=9,
                   command=lambda lv=level: self.new_here(lv)).pack(side="left", padx=(6, 0))
        self._rows.append((frame, cb, var, pid))
        self._sel[level] = None
        if preselect is not None:
            for i, n in enumerate(kids):
                if n["id"] == preselect:
                    cb.current(i)
                    self._sel[level] = n
                    break
        return level

    def _on_pick(self, level):
        if self._busy:
            return
        _f, cb, _v, pid = self._rows[level]
        kids = self.kids(pid)
        idx = cb.current()
        if idx < 0 or idx >= len(kids):
            return
        node = kids[idx]
        self._sel[level] = node
        # 改了上一级,底下几级原来的选择就没意义了,一律清掉重铺
        for lv in [k for k in self._sel if k > level]:
            del self._sel[lv]
        while len(self._rows) > level + 1:
            f, _c, _v2, _p = self._rows.pop()
            f.destroy()
        if node.get("children"):
            self._grow(node["id"])
        self._notify()

    # ---------------------------------------------------------------- 取值
    def current_node(self):
        """选到的最深那个节点。「确定」确定的就是它。"""
        for lv in sorted(self._sel, reverse=True):
            if self._sel.get(lv) is not None:
                return self._sel[lv]
        return None

    def _notify(self):
        if self.on_change:
            self.on_change(self.current_node())

    def new_here(self, level):
        """在这一级新建一个节点,挂在上一级选中的那个节点下面。

        比如选了大类「电容」、二级「陶瓷贴片电容」,然后在三级那一行点新建,
        新节点就挂在「陶瓷贴片电容」下面 —— 用户点的位置就是他想要的位置。
        """
        if level >= len(self._rows):
            return
        pid = self._rows[level][3]
        parent = self._by_id.get(pid) if pid else None
        if level > 0 and parent is None:
            messagebox.showinfo("提示", "先把上一级选好,才知道新品类挂在哪儿。",
                                parent=self)
            return
        where = f"「{parent['path']}」下面" if parent else "顶层"
        name = ask_text(self, "新建品类", f"在{where}新建一个,叫什么?", "")
        if not name:
            return
        res = call(self.con, server.create_category, parent=self, body={
            "name": name, "parent_id": parent["id"] if parent else None})
        if res is None:
            return
        new_id = int((res or {}).get("id") or 0)
        # 不整棵重铺 —— 重铺会把人已经选好的上级路径丢掉;只把新节点接进
        # 这一级的候选里并选中它,接着往下挑
        self.fetch()
        _f, cb, _v, _pid = self._rows[level]
        kids = self.kids(pid)
        cb.configure(values=[n["name"] for n in kids])
        for i, n in enumerate(kids):
            if n["id"] == new_id:
                cb.current(i)
                break
        # 显式再调一次:_on_pick 是幂等的(先清下面几级再铺一级),
        # 所以不管 current() 自己有没有触发过,结果都一样
        self._on_pick(level)


class CategoryDialog(tk.Toplevel):
    """改某一行的品类:既能一级一级挑,也能直接打字。

    双击 BOM 行的品类、导入 BOM 前复核品类,走的都是这里。

    为什么两级都要留着:「一级一级挑」解决的是「选项太长看不清选的是啥」,
    而「直接打字」解决的是「推断不出来的品类得能写进去」(光耦、传感器模块)。
    只留前者会让用户只能挑一个最接近的凑合 —— 那等于把「猜错了」换成
    「被迫选了个不准确的」。
    """

    def __init__(self, parent, app: App, current="", options=None):
        super().__init__(parent)
        self.title("改品类")
        self.transient(parent.winfo_toplevel())
        self.resizable(False, False)
        self.value = None
        opts = list(options or []) or ["其他"]
        cur = str(current or "").strip()
        if cur and cur not in opts:
            opts = [cur] + opts       # 库里已有的词也要能选回来
        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="这一行算哪一类?").grid(row=0, column=0, sticky="w")
        self.var = tk.StringVar(value=cur or opts[0])
        # 故意**不**设成 readonly:推断不出来的品类必须能自己打进去。
        # 这个控件要**先建**:自检里「小窗口也能打字」数的是第一个 Combobox。
        cb = ttk.Combobox(body, textvariable=self.var, values=opts, width=24)
        cb.grid(row=1, column=0, sticky="w", pady=(6, 6))
        ttk.Label(body, text="或者一级一级往下选(选完大类才出二级):",
                  style="Dim.TLabel").grid(row=2, column=0, sticky="w")
        # hint 必须建在 step 前面:CategoryStepBox 一构造就会回调一次 _on_step
        # (把初始的归属报出来),那时候写 hint 就会 AttributeError。
        self.hint = tk.StringVar()
        ttk.Label(body, textvariable=self.hint, style="Dim.TLabel",
                  wraplength=330, justify="left").grid(row=4, column=0, sticky="w",
                                                       pady=(6, 0))
        self.step = CategoryStepBox(body, app.con, on_change=self._on_step)
        self.step.grid(row=3, column=0, sticky="w", pady=(4, 0))
        # 这个品类树上有对应节点的话,按它的归属预铺好
        cid0 = None
        for n in self.step._by_id.values():
            if n["path"] == cur:
                cid0 = n["id"]
                break
        if cid0 is None:
            leaf = cur.split(" / ")[-1]
            for n in self.step._by_id.values():
                if leaf and n["name"] == leaf:
                    cid0 = n["id"]
                    break
        if cid0:
            self.step.seed(cid0)
        btns = ttk.Frame(body)
        btns.grid(row=5, column=0, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="取消", command=self.destroy, width=10).pack(side="right")
        ttk.Button(btns, text="确定", command=self.ok, width=10).pack(side="right", padx=6)
        self.bind("<Return>", lambda _e: self.ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()
        cb.focus_set()

    def _on_step(self, node):
        """在树上选到某个节点:把「这一行算哪一类」填成它那一支的大类名。

        BOM 行上的品类记的是**大类**(component.category 存的也是大类名,
        按品类分组 / 筛选的 SQL 全靠这一列),所以在树上选到多深,这一栏填的
        都是最顶层那一个;同时把全路径写出来,让人知道自己刚才选的是哪一支。
        """
        if node is None:
            self.hint.set("")
            return
        root = node["path"].split(" / ")[0]
        self.var.set(root)
        self.hint.set(f"选中的是:{node['path']}\n"
                      f"(BOM 行的品类记大类,所以这一栏填「{root}」)")

    def ok(self):
        self.value = self.var.get().strip() or "其他"
        self.destroy()


def ask_category(parent, app: App, current="", options=None):
    d = CategoryDialog(parent, app, current, options)
    parent.winfo_toplevel().wait_window(d)
    return d.value


class CategoryPickerDialog(tk.Toplevel):
    """选品类:一级一级往下选,「确定」确定的是当前最深那个节点。

    库存明细面板和元件编辑窗口用它。结果放在 result 里:
      * (节点 id, 全路径)  —— 选了树上某个节点
      * (None, "")         —— 点了「清成品类」(只有 allow_clear 时才有这个按钮)
    """

    def __init__(self, parent, app: App, cat_id=None, allow_clear=False):
        super().__init__(parent)
        self.app = app
        self.con = app.con
        self.result = None
        self.title("选品类")
        self.transient(parent)
        self.resizable(False, False)
        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="一级一级往下选。这一级选完,下一级才会出来。",
                  style="Dim.TLabel").pack(anchor="w", pady=(0, 8))
        self.path_var = tk.StringVar()
        self.step = CategoryStepBox(body, self.con, on_change=self._on_step)
        self.step.pack(anchor="w")
        ttk.Label(body, textvariable=self.path_var, foreground="#1f6feb",
                  wraplength=340, justify="left").pack(anchor="w", pady=(10, 0))
        bar = ttk.Frame(body)
        bar.pack(fill="x", pady=(12, 0))
        ttk.Button(bar, text="确定", width=10, command=self.ok).pack(side="right")
        ttk.Button(bar, text="取消", width=10,
                   command=self.destroy).pack(side="right", padx=6)
        if allow_clear:
            ttk.Button(bar, text="清成品类",
                       command=self.clear_choice).pack(side="left")
        self.bind("<Return>", lambda _e: self.ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()
        if cat_id:
            self.step.seed(cat_id)
        self.update_idletasks()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        self.geometry(f"+{px + 40}+{py + 40}")

    def _on_step(self, node):
        self.path_var.set(f"选中:{node['path']}" if node else "还没选 —— 至少选一个大类。")

    def ok(self):
        node = self.step.current_node()
        if node is None:
            messagebox.showinfo("提示", "先选一个大类。", parent=self)
            return
        self.result = (node["id"], node["path"])
        self.destroy()

    def clear_choice(self):
        self.result = (None, "")
        self.destroy()


class BomReviewDialog(tk.Toplevel):
    """导入 BOM 之前,先让人把品类过一遍。

    为什么要这一步:品类是「按品类找料」「按值筛选」的基础,而推断难免出错 ——
    导出格式五花八门,缺位号、写错封装、把料号填进值那一列,都会让推断跑偏。

    但**不能让人从头看一遍**:他会直接点确定,那这一步就白加了。所以这里:
      · 默认只列「要确认」的行(线索打架的、只有弱证据的、认不出的)
      · 每一行都写着推断依据,一眼能看出该不该改
      · 选中的多行可以一次改掉,不用一行一行点
    想从头看就把筛选关掉。
    """

    COLS = [
        ("row", "行", 42, "e"),
        ("name", "名称", 148, "w", True),
        ("lcsc_pn", "商品编号", 82, "center"),
        ("value", "值", 68, "w"),
        ("package", "封装", 78, "w"),
        ("designators", "位号", 108, "w", True),
        ("qty", "数量", 44, "e"),
        ("category", "品类", 84, "center"),
        ("conf", "把握", 58, "center"),
        ("reason", "推断依据", 236, "w", True),
    ]

    def __init__(self, parent, app: App, preview: dict, default_name="", on_confirm=None):
        super().__init__(parent)
        self.title("导入 BOM 前核对品类")
        self.transient(parent.winfo_toplevel())
        self.geometry("1220x700")
        self.minsize(1000, 520)
        self.app = app
        self.con = app.con
        self.on_confirm = on_confirm
        self._lines = list(preview.get("lines") or [])
        self._cats = {}                       # source_row -> 人工改过的品类
        # 后端给的是「推断得出来的品类」;再并上用户库里已经在用的,
        # 免得他自己用惯的词在这里选不到
        opts = list(preview.get("categories") or [])
        for c in category_options(app.con):
            if c not in opts:
                opts.append(c)
        self._opts = opts or ["其他"]

        body = ttk.Frame(self, padding=10)
        body.pack(fill="both", expand=True)

        head = ttk.Frame(body)
        head.pack(fill="x")
        ttk.Label(head, text=f"{preview.get('filename') or 'BOM'} —— "
                             f"{len(self._lines)} 行,"
                             f"总需求 {preview.get('total_qty') or 0} 个",
                  style="Big.TLabel").pack(side="left")
        need = int(preview.get("need_review") or 0)
        ttk.Label(head,
                  text=(f"其中 {need} 行的品类要你确认" if need else "品类都认得明确"),
                  foreground=("#c0392b" if need else "#1e8449")).pack(side="right")

        nrow = ttk.Frame(body)
        nrow.pack(fill="x", pady=(8, 2))
        ttk.Label(nrow, text="项目名称").pack(side="left", padx=(0, 6))
        self.name = tk.StringVar(value=default_name)
        ttk.Entry(nrow, textvariable=self.name, width=32).pack(side="left")
        ttk.Label(nrow, text="(BOM 行挂到这个项目下;同名项目会被整份覆盖)",
                  style="Dim.TLabel").pack(side="left", padx=8)

        bar = ttk.Frame(body)
        bar.pack(fill="x", pady=(4, 4))
        self.only_review = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text="只看要确认的", variable=self.only_review,
                        command=self._render).pack(side="left")
        ttk.Label(bar, text="把选中的改成").pack(side="left", padx=(14, 4))
        self.pick = tk.StringVar(value=self._opts[0])
        # 可打字:下拉是建议,认不出的新品类要能自己填
        ttk.Combobox(bar, textvariable=self.pick, values=self._opts, width=16).pack(
            side="left")
        ttk.Button(bar, text="应用", command=self._apply_sel).pack(side="left", padx=6)
        ttk.Label(bar, text="双击一行能单独改;下拉也能直接打字;"
                            "Ctrl / Shift 可以多选",
                  style="Dim.TLabel").pack(side="left", padx=6)

        f, self.tree = make_tree(body, self.COLS, height=15)
        f.pack(fill="both", expand=True)
        self.tree.configure(selectmode="extended")
        self.tree.tag_configure("review", background="#fff6dd")
        self.tree.tag_configure("fixed", background="#e8f6ec")
        self.tree.bind("<Double-1>", lambda _e: self._edit_one())

        warn = preview.get("warnings") or []
        if warn:
            ttk.Label(body, text=("解析时有 %d 条提醒:" % len(warn))
                                + " / ".join(warn[:3]) + (" …" if len(warn) > 3 else ""),
                      style="Dim.TLabel", wraplength=1120,
                      justify="left").pack(anchor="w", pady=(6, 0))

        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(8, 0))
        self.sum = tk.StringVar()
        ttk.Label(btns, textvariable=self.sum, style="Dim.TLabel").pack(side="left")
        ttk.Button(btns, text="取消", command=self.destroy, width=12).pack(side="right")
        ttk.Button(btns, text="确认导入", command=self.ok, width=14).pack(side="right", padx=8)

        self._render()
        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()

    # ------------------------------------------------------------ 内部

    def _cat_of(self, line):
        return self._cats.get(line.get("source_row")) or line.get("category") or "其他"

    def _render(self):
        keep = set(self.tree.selection())
        clear_tree(self.tree)
        shown = 0
        for line in self._lines:
            conf = line.get("confidence") or "none"
            fixed = line.get("source_row") in self._cats
            if self.only_review.get() and conf == "high" and not fixed:
                continue
            shown += 1
            tags = ("fixed",) if fixed else (("review",) if conf != "high" else ())
            self.tree.insert("", "end", iid=str(line.get("source_row")), values=(
                line.get("source_row"), line.get("name") or "",
                line.get("lcsc_pn") or "", line.get("value") or "",
                line.get("package") or "", line.get("designators") or "",
                line.get("qty") or 0, self._cat_of(line),
                "人工指定" if fixed else (line.get("confidence_label") or ""),
                "由人指定" if fixed else (line.get("reason") or "")), tags=tags)
        n_review = sum(1 for l in self._lines
                       if (l.get("confidence") or "none") != "high"
                       or l.get("source_row") in self._cats)
        self.sum.set(f"显示 {shown} / {len(self._lines)} 行;要过一眼的 {n_review} 行。"
                     f"导入后还能在库存页随时改品类。")
        back = [i for i in keep if self.tree.exists(i)]
        if back:
            self.tree.selection_set(back)

    def _apply_sel(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "先在下面选中要改的行(按住 Ctrl / Shift 可以多选)。",
                                parent=self)
            return
        cat = self.pick.get().strip() or "其他"
        for iid in sel:
            self._cats[int(iid)] = cat
        self._render()
        self.app.set_status(f"已把 {len(sel)} 行的品类改成「{cat}」")

    def _edit_one(self):
        sel = self.tree.selection()
        if len(sel) != 1:
            return
        row = int(sel[0])
        line = next((l for l in self._lines if l.get("source_row") == row), None)
        if line is None:
            return
        got = ask_category(self, self.app, self._cat_of(line), self._opts)
        if got:
            self._cats[row] = got
            self._render()

    def ok(self):
        name = self.name.get().strip()
        if not name:
            messagebox.showinfo("提示", "项目名称不能空。", parent=self)
            return
        # JSON 的键是字符串,后端两种都认,这里统一成字符串省得扯皮
        cats = {str(k): v for k, v in self._cats.items()}
        self.destroy()
        if self.on_confirm:
            self.on_confirm(name, cats)


class SimilarDialog(tk.Toplevel):
    """按「值 + 封装」在库存里找相似元件,由人确认。

    为什么要有它:导出的 BOM 常常缺料号、缺位号,只剩值和封装,而且同一样东西
    不同工具写法还不一样(4.7k / 4.7kΩ / 4700;C0805 / 0805 / C-0805)。
    严格相等找不到,人就得自己在几百行里翻。

    这里给出候选和「像在哪里」,但**不替人认定是同一颗料** ——
    出库记错料,比多花两秒确认严重得多。
    """

    COLS = [
        ("name", "元件", 186, "w", True),
        ("lcsc_pn", "商品编号", 86, "center"),
        ("value", "值", 74, "w"),
        ("package", "封装", 82, "w"),
        ("category", "品类", 78, "w"),
        ("on_hand", "库存", 50, "e"),
        ("verdict", "把握", 148, "center"),
        ("match", "像在哪里", 186, "w", True),
    ]

    def __init__(self, parent, app: App, *, value="", package="", category="",
                 on_pick=None, in_stock_only=True):
        super().__init__(parent)
        self.title("按「值 + 封装」找相似元件")
        self.transient(parent.winfo_toplevel())
        self.geometry("1040x640")
        self.minsize(900, 480)
        self.app = app
        self.con = app.con
        self.on_pick = on_pick
        self._items = {}
        self._opts = category_options(app.con)

        body = ttk.Frame(self, padding=10)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="按「值 + 封装」在库存里找相似",
                  style="Big.TLabel").pack(anchor="w")

        bar = ttk.Frame(body)
        bar.pack(fill="x", pady=(8, 2))
        ttk.Label(bar, text="值").pack(side="left", padx=(0, 4))
        self.v_value = tk.StringVar(value=value or "")
        ttk.Entry(bar, textvariable=self.v_value, width=15).pack(side="left")
        ttk.Label(bar, text="封装").pack(side="left", padx=(10, 4))
        self.v_pkg = tk.StringVar(value=package or "")
        ttk.Entry(bar, textvariable=self.v_pkg, width=15).pack(side="left")
        ttk.Label(bar, text="品类").pack(side="left", padx=(10, 4))
        self.v_cat = tk.StringVar(value=category or "")
        ttk.Combobox(bar, textvariable=self.v_cat, values=[""] + self._opts, width=13,
                     state="readonly").pack(side="left")
        self.stocked = tk.BooleanVar(value=bool(in_stock_only))
        ttk.Checkbutton(bar, text="只看有库存的", variable=self.stocked,
                        command=self.reload).pack(side="left", padx=10)
        ttk.Button(bar, text="找相似", command=self.reload).pack(side="left")

        ttk.Label(body, text="相似度只说明「像」,是不是同一颗料要你自己确认。",
                  style="Dim.TLabel").pack(anchor="w", pady=(0, 4))

        f, self.tree = make_tree(body, self.COLS, height=13)
        f.pack(fill="both", expand=True)
        self.tree.tag_configure("strong", background="#e8f6ec")
        self.tree.tag_configure("weak", foreground="#8d99a6")
        self.tree.bind("<Double-1>", lambda _e: self.pick())

        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(8, 0))
        self.sum = tk.StringVar()
        ttk.Label(btns, textvariable=self.sum, style="Dim.TLabel",
                  wraplength=640, justify="left").pack(side="left")
        ttk.Button(btns, text="关闭", command=self.destroy, width=12).pack(side="right")
        if on_pick:
            ttk.Button(btns, text="选中它去开单", command=self.pick,
                       width=16).pack(side="right", padx=8)

        self.bind("<Return>", lambda _e: self.reload())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.reload()
        self.grab_set()

    def reload(self):
        q = {"value": self.v_value.get().strip(), "package": self.v_pkg.get().strip(),
             "category": self.v_cat.get().strip(), "limit": "20"}
        if self.stocked.get():
            q["stocked"] = "1"
        data = call(self.con, server.components_similar, query=q, quiet=True) or {}
        items = data.get("items") or []
        clear_tree(self.tree)
        self._items = {}
        for it in items:
            score = int(it.get("score") or 0)
            tags = ("strong",) if score >= 90 else (("weak",) if score < 50 else ())
            self.tree.insert("", "end", iid=str(it["id"]), values=(
                it.get("name") or "", it.get("lcsc_pn") or "", it.get("value") or "",
                it.get("package") or "", it.get("category") or "",
                it.get("on_hand") or 0, it.get("verdict") or "",
                it.get("match") or ""), tags=tags)
            self._items[it["id"]] = it
        if items:
            top = items[0]
            self.sum.set(f"{len(items)} 个候选;最像的是「{top.get('name')}」"
                         f"({top.get('match')})。")
        elif not (q["value"] or q["package"]):
            self.sum.set("值或封装至少填一个 —— 两个都空就没有比对的依据。")
        elif self.stocked.get():
            # 被「只看有库存的」滤空了。这时说「没有像的」是错的 ——
            # 用户会得出「库里没有这颗料」的结论,而事实可能只是现在没库存,
            # 那要去买、还是要先入库,是完全不同的下一步。所以再查一次,说清楚。
            alt = dict(q)
            alt.pop("stocked", None)
            others = (call(self.con, server.components_similar, query=alt, quiet=True)
                      or {}).get("items") or []
            if others:
                self.sum.set(f"有 {len(others)} 个像的,但都没库存(被「只看有库存的」"
                             f"滤掉了)。取消勾选就能看到;要出库的话得先入库。")
            else:
                self.sum.set("没有像的 —— 库里可能压根没有这颗料。"
                             "可以只填值、或把封装去掉再试一次。")
        else:
            self.sum.set("没有像的。可以只填值、或把封装去掉再试一次。")

    def pick(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "先选中一颗料。", parent=self)
            return
        cid = int(sel[0])
        if self.on_pick:
            self.destroy()
            self.on_pick(cid)


class MoveForm(ttk.Frame):
    """一个方向的开单区:左边找元件,右边填单。

    这块原本长在「出入库」页上。搬到项目里,是因为入库/出库从来不是孤立的动作 ——
    它总是「给某个项目收料 / 发料」,而做这个决定要看的正是这张 BOM 还缺什么。
    出入库页因此只剩查账的职责。
    """

    def __init__(self, parent, app: App, action: str, on_done=None):
        super().__init__(parent)
        self.app = app
        self.con = app.con
        self.action = action
        self.project_id = None
        self.on_done = on_done
        self._items = {}
        self._only_low = False

        left = ttk.Frame(self)
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))
        right = ttk.LabelFrame(self, text=f"{self.label}开单", padding=10)
        right.pack(side="left", fill="y")

        sbar = ttk.Frame(left)
        sbar.pack(fill="x", pady=(0, 4))
        ttk.Label(sbar, text="找元件").pack(side="left", padx=(0, 4))
        self.q = tk.StringVar()
        ent = ttk.Entry(sbar, textvariable=self.q, width=20)
        ent.pack(side="left")
        ent.bind("<KeyRelease>", lambda _e: self.reload())
        ttk.Button(sbar, text="只看缺货", command=self.only_low).pack(side="left", padx=6)
        # 出库默认只列有库存的:没有的东西发不出去,列出来纯属干扰。
        # 入库反过来必须看得到全部,否则刚导入 BOM 的新料永远收不进来 ——
        # 入库的前提正是它现在库存为 0。
        self.only_stocked = tk.BooleanVar(value=(action == "OUT"))
        ttk.Checkbutton(sbar, text="只列有库存的", variable=self.only_stocked,
                        command=self.reload).pack(side="left")
        # 手上这颗料的写法和库里不一致时(0402 写成 0602、4.7k 写成 4.7kΩ),
        # 靠搜索是找不到的,得按值+封装算相似
        ttk.Button(sbar, text="找相似…", command=self.similar).pack(side="left", padx=(6, 0))

        self.list_hint = tk.StringVar()
        ttk.Label(left, textvariable=self.list_hint, style="Dim.TLabel",
                  wraplength=520, justify="left").pack(anchor="w", pady=(0, 4))

        f, self.tree = make_tree(left, [
            ("name", "名称", 175, "w", True),
            ("lcsc_pn", "商品编号", 86, "center"),
            ("package", "封装", 88, "w"),
            ("on_hand", "库存", 54, "e"),
            ("state", "状态", 54, "center")], height=10)
        f.pack(fill="both", expand=True)
        self.tree.tag_configure("out", background="#ffe3e3")
        self.tree.tag_configure("low", background="#fff6dd")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        self.target = tk.StringVar(value="（左侧选一个元件）")
        ttk.Label(right, textvariable=self.target, style="Big.TLabel",
                  wraplength=230).grid(row=0, column=0, columnspan=2,
                                       pady=(0, 8), sticky="w")
        ttk.Label(right, text="数量").grid(row=1, column=0, sticky="e", padx=(0, 6), pady=3)
        self.qty = tk.StringVar()
        ttk.Entry(right, textvariable=self.qty, width=12).grid(row=1, column=1, sticky="w")
        ttk.Label(right, text="仓位").grid(row=2, column=0, sticky="e", padx=(0, 6), pady=3)
        self.loc = tk.StringVar(value="未分类")
        self.cb_loc = ttk.Combobox(right, textvariable=self.loc, width=14)
        self.cb_loc.grid(row=2, column=1, sticky="w")
        ttk.Label(right, text="操作人").grid(row=3, column=0, sticky="e", padx=(0, 6), pady=3)
        self.who = tk.StringVar(value="本地用户")
        ttk.Entry(right, textvariable=self.who, width=14).grid(row=3, column=1, sticky="w")
        ttk.Label(right, text="备注").grid(row=4, column=0, sticky="e", padx=(0, 6), pady=3)
        self.note = tk.StringVar()
        ttk.Entry(right, textvariable=self.note, width=20).grid(row=4, column=1, sticky="w")

        self.hint = tk.StringVar()
        ttk.Label(right, textvariable=self.hint, style="Dim.TLabel",
                  wraplength=230, justify="left").grid(
            row=5, column=0, columnspan=2, pady=(6, 0), sticky="w")
        ttk.Button(right, text=f"确认{self.label}", command=self.submit,
                   width=14).grid(row=6, column=0, columnspan=2, pady=10)

    @property
    def label(self):
        return KIND_LABEL.get(self.action, self.action)

    # ------------------------------------------------------ 上下文

    def set_project(self, pid):
        self.project_id = pid or None
        if self.action == "OUT":
            self.hint.set("出库:数量填出库数量,库存不足会被拒绝。")
        else:
            self.hint.set("入库:数量填入库数量。提交后记在这个项目名下,并按时间排在下面。")
        self.load_locations()

    def load_locations(self):
        meta = call(self.con, server.meta, quiet=True) or {}
        codes = [l["code"] for l in meta.get("locations") or []]
        self.cb_loc.configure(values=codes)
        if not self.loc.get() and codes:
            self.loc.set(codes[0])

    # ------------------------------------------------------ 数据

    def reload(self):
        keep = self._current_id()      # 刷之前选中的是谁,刷完要还选回去
        query = {"limit": 500}
        kw = self.q.get().strip()
        if kw:
            query["q"] = kw
        if self.only_stocked.get():
            query["stocked"] = "1"
        if self._only_low:
            data = call(self.con, server.lowstock, query=query, quiet=True)
        else:
            data = call(self.con, server.list_components, query=query, quiet=True)
        if data is None:
            return
        clear_tree(self.tree)
        self._items = {}
        for it in data["items"]:
            self.tree.insert("", "end", iid=str(it["id"]), values=(
                it.get("name") or "", it.get("lcsc_pn") or "", it.get("package") or "",
                it.get("on_hand") or 0, STATE_LABEL.get(it.get("stock_state"), "")),
                tags=(it.get("stock_state") or "",))
            self._items[it["id"]] = it
        n = len(self._items)
        tips = []
        if self.only_stocked.get():
            tips.append("只列有库存的")
        if self._only_low:
            tips.append("只看缺货")
        self.list_hint.set(f"{n} 个元件" + (f"({'、'.join(tips)})" if tips else ""))
        if n == 0 and tips:
            self.list_hint.set(f"当前条件下没有元件({'、'.join(tips)})。"
                               f"取消勾选就能看到全部。")
        # 开完单 app.refresh_all() 会走到这里。不把选中还回去的话,左侧会变成
        # 没选中任何元件的状态,下一次开单得重新点一遍 —— 连续入库很难用。
        if keep and keep in self._items:      # _items 的键是 int,别拿 str 去比
            self.tree.selection_set(str(keep))
            self.tree.see(str(keep))

    def only_low(self):
        self._only_low = not self._only_low
        self.reload()

    def similar(self):
        """按值+封装找相似。没选中元件时就把搜索框里打的字当值用。"""
        cid = self._current_id()
        it = self._items.get(cid) if cid else None
        SimilarDialog(
            self, self.app,
            value=(it or {}).get("value") or self.q.get().strip(),
            package=(it or {}).get("package") or "",
            category=(it or {}).get("category") or "",
            on_pick=self.select_by_id,
            # 出库时没库存的候选帮不上忙;入库时反过来,只想要有库存的是自相矛盾
            in_stock_only=(self.action == "OUT"))

    def select_by_id(self, cid) -> bool:
        """把某个元件选中。从「找相似」里挑中之后走这里,好让人接着填数量提交。

        要先清掉搜索框、取消「只看缺货」:挑中的那颗料很可能正因为被筛选挡着
        而不在列表里,不放开就选不上,用户会觉得「点了没反应」。

        返回**有没有真的选上**。没选上时调用方得说话 —— 静默不做事最糟。
        """
        self.q.set("")
        self._only_low = False
        self.reload()
        key = str(cid)
        if not self.tree.exists(key):
            return False
        self.tree.selection_set(key)
        self.tree.see(key)
        self.tree.focus(key)
        return True

    def _on_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        it = self._items.get(int(sel[0])) or {}
        self.target.set(f"{it.get('name')}\n现有 {it.get('on_hand') or 0} "
                        f"{it.get('unit') or '个'}")

    def _current_id(self):
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    # ------------------------------------------------------ 动作

    def submit(self):
        if not self.project_id:
            messagebox.showinfo("提示", "先在左边选一个项目,入库/出库都要记在项目名下。",
                                parent=self)
            return
        cid = self._current_id()
        if not cid:
            messagebox.showinfo("提示", "请先在左边选中一个元件。", parent=self)
            return
        raw = self.qty.get().strip()
        if not raw.isdigit():
            messagebox.showinfo("提示", "数量要填非负整数。", parent=self)
            return
        body = {"kind": self.action, "component_id": cid, "qty": int(raw),
                "location": self.loc.get().strip() or "未分类",
                "operator": self.who.get().strip() or "本地用户",
                "note": self.note.get().strip(),
                "project_id": self.project_id}
        manual_note(body)          # 手动开的单:流水里标明来源
        res = call(self.con, server.stock_move, body=body, parent=self)
        if res is None:
            return
        self.qty.set("")
        self.note.set("")
        self.app.set_status(
            f"{self.label}完成:该仓位现有 {res['qty_at_location']},"
            f"总库存 {res['on_hand']}")
        if self.on_done:
            self.on_done()
        self.app.refresh_all()


# 勾选框。Treeview 没有真的 checkbox,用这两个字符顶上 —— 零依赖,
# 点一下切换,手感跟真勾选框一样,而且导出/截图里也看得见状态。
CHECK_ON, CHECK_OFF = "☑", "☐"


def still_open(line) -> bool:
    """这条 BOM 需求还有没有「没消化掉」的量 —— 收料页和出库页共用这一个判断(#31)。

    一件事只有一份口径:**一条需求全收完或全发完之后,两边都不该再列它**。
    从前这个减法只做了「已发料」那一半,于是刚收进来的货在出库页上还能再发
    一遍(用户原话:「全部元件入库,元件出库那边就不要再显示它们」)。所以这里
    减的是后端算好的 remaining(= 需求 − 已发料 − 已入库),不再由界面自己减。

    remaining **缺失**时当作「还有」:宁可多列一行(那一行本来就是能收能发的),
    也不要因为后端少回一个字段就把整张表清空 —— 那看起来像 BOM 数据丢了。
    """
    rem = line.get("remaining")
    return True if rem is None else int(rem) > 0


class BomPaneBase(ttk.Frame):
    """按 BOM 开单的两块共用的壳:筛选、仓位/经手人/备注、提交按钮。

    收料和发料在这几件事上一模一样(同一个项目上下文、同一套落库参数),
    分开写的话迟早只有一边记得「提交前先挡一下没选项目」。
    """

    #: 子类覆盖 —— 提交按钮上的字
    SUBMIT = "提交"
    #: 子类覆盖 —— 表格列定义
    COLS = []
    #: 子类覆盖 —— 表格里有没有可勾选的行
    HAS_CHECK = True

    def __init__(self, parent, app: App, action: str, on_done=None):
        super().__init__(parent)
        self.app = app
        self.con = app.con
        self.action = action
        self.on_done = on_done
        self.project_id = None
        self.lines = []
        # 被后端挡掉的「已经做完」的行数(#31)。两个面板都拿它写一句提示,
        # 否则用户只看到行数变少,会以为 BOM 数据丢了
        self.hidden_done = 0
        self._idx = {}          # (bom_id, component_id) -> 候选元件,提交时要用名字
        # 勾选状态在基类里就初始化好:筛选框的 trace 可能在任何一次 reload
        # 之前就触发 render,那时候子类的属性还不存在
        self.alloc = {}         # (bom_id, component_id) -> 本次数量(出库)
        self.picked = set()     # 勾上的 bom_id(入库)
        self.qty = {}           # bom_id -> 本次数量(入库)
        self._open = set()      # 展开了的 bom_id

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 4))
        ttk.Label(bar, text="筛").pack(side="left")
        self.q = tk.StringVar()
        self.q.trace_add("write", lambda *_: self.render())
        ttk.Entry(bar, textvariable=self.q, width=16).pack(side="left", padx=(2, 6))
        ttk.Button(bar, text="全选", command=lambda: self.check_all(True)).pack(side="left")
        ttk.Button(bar, text="全不选", command=lambda: self.check_all(False)
                   ).pack(side="left", padx=4)
        self.extra_tools(bar)
        self.hint = tk.StringVar()
        ttk.Label(bar, textvariable=self.hint, style="Dim.TLabel").pack(side="right")

        frame, self.tree = self.build_tree()
        frame.pack(fill="both", expand=True)

        # 只认「选」那一列的点击,别处照旧走 Treeview 自己的行为 ——
        # 否则展开箭头、拖列宽都会被这里吃掉
        self.tree.bind("<Button-1>", self.on_click)
        self.tree.bind("<space>", self.on_space)
        self.tree.bind("<Double-1>", self.on_double)
        self.setup_tree()

        foot = ttk.Frame(self)
        foot.pack(fill="x", pady=(4, 0))
        ttk.Label(foot, text="仓位").pack(side="left")
        self.loc = tk.StringVar()
        self.cb_loc = ttk.Combobox(foot, textvariable=self.loc, width=13, state="readonly")
        self.cb_loc.pack(side="left", padx=(2, 8))
        ttk.Label(foot, text="经手人").pack(side="left")
        self.who = tk.StringVar(value="本地用户")
        ttk.Entry(foot, textvariable=self.who, width=9).pack(side="left", padx=(2, 8))
        ttk.Label(foot, text="备注").pack(side="left")
        self.note = tk.StringVar()
        ttk.Entry(foot, textvariable=self.note, width=14).pack(side="left", padx=(2, 8))
        self.btn_go = ttk.Button(foot, text=self.SUBMIT, command=self.submit)
        self.btn_go.pack(side="right")
        self.load_locations()

    # ------------------------------------------------------------ 子类接口

    def extra_tools(self, bar):
        """子类往工具栏上再加自己的按钮。"""

    def setup_tree(self):
        """表格建好、默认绑定都装好之后,子类再挂自己的东西(右键菜单之类)。

        放在这里而不是 __init__ 里的原因:子类要拿到 self.tree 才能挂,
        而 self.tree 是 build_tree() 建出来的。
        """

    def build_tree(self):
        raise NotImplementedError

    def render(self):
        raise NotImplementedError

    def submit(self):
        raise NotImplementedError

    def check_all(self, on):
        raise NotImplementedError

    # ------------------------------------------------------------ 公共

    def load_locations(self):
        meta = call(self.con, server.meta, quiet=True) or {}
        codes = [l["code"] for l in meta.get("locations") or []]
        self.cb_loc.configure(values=codes)
        if not self.loc.get() and codes:
            self.loc.set(codes[0])

    def set_project(self, pid):
        self.project_id = pid or None
        if not self.project_id:
            self.lines = []
            clear_tree(self.tree)
            self.hint.set("先在左边选一个项目。")
            return
        self.reload()

    def reload(self):
        raise NotImplementedError

    # ---- 勾选

    def on_space(self, _event):
        sel = self.tree.selection()
        if sel:
            self.toggle(sel[0])
        return "break"

    def on_click(self, event):
        """点「选」那一列 = 切换勾选。别的列一律不拦。"""
        if self.tree.identify_region(event.x, event.y) != "cell":
            return None
        if self.tree.identify_column(event.x) != "#1":
            return None
        row = self.tree.identify_row(event.y)
        if not row or not self.is_checkable(row):
            return None
        self.toggle(row)
        return "break"

    def is_checkable(self, iid) -> bool:
        return True

    def toggle(self, iid):
        raise NotImplementedError

    def on_double(self, event):
        """双击改数量。空实现留给子类,但得拦住 Treeview 的默认展开切换 ——
        双击子行时展开/收起父行会让人以为「点了没反应」。"""
        row = self.tree.identify_row(event.y)
        if row and self.is_checkable(row):
            self.edit_qty(row)
            return "break"
        return None

    def edit_qty(self, iid):
        raise NotImplementedError

    def moved_text(self, n: int, total: int, unit: str = "个") -> str:
        return f"勾了 {n} 行,合计 {total} {unit}"


class CategoryManagerDialog(tk.Toplevel):
    """整棵品类树:加、改名、挪上级、删。

    这里用来看**全貌**,日常的「加一个 / 改个名 / 删掉」在库存菜单里就地就能做
    (卡片右键),不必开这个窗口。

    删的时候**绝不删元件,也不连坐子类** —— 子类接到上一级、元件挪到上一级,
    只有被点的那个节点消失。用户要的是「整理货架」,不是「连货一起扔」,
    所以确认框里会把这两件事都写清楚。
    """

    COLS = [("name", "品类", 170, "w", True),
            ("own", "直接挂", 62, "e", False),
            ("total", "含子类", 62, "e", False),
            ("path", "全路径", 240, "w", True)]

    def __init__(self, parent, app: App):
        super().__init__(parent)
        self.app = app
        self.con = app.con
        self.title("品类管理")
        self.transient(parent)
        self.geometry("660x460")
        self._flat = {}

        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="品类是「按品类找料」和库存菜单的基础。加几级就有几级菜单。",
                  style="Dim.TLabel").pack(anchor="w", pady=(0, 6))
        frame, self.tree = make_tree(body, self.COLS, height=12, show="tree headings")
        # #0 树列不在 columns 里,make_tree 管不到它,得单独改成居中(#27)
        self.tree.column("#0", width=180, anchor="center", stretch=True)
        frame.pack(fill="both", expand=True)

        bar = ttk.Frame(body)
        bar.pack(fill="x", pady=(8, 0))
        ttk.Button(bar, text="＋ 子品类", command=self.add_child).pack(side="left")
        ttk.Button(bar, text="＋ 顶级品类", command=self.add_root).pack(side="left", padx=6)
        ttk.Button(bar, text="改名…", command=self.rename).pack(side="left", padx=6)
        ttk.Button(bar, text="删除", command=self.remove).pack(side="left", padx=6)
        ttk.Button(bar, text="关闭", command=self.destroy).pack(side="right")

        self.hint = tk.StringVar()
        ttk.Label(body, textvariable=self.hint, foreground="#b9770e",
                  wraplength=600, justify="left").pack(anchor="w", pady=(6, 0))
        self.reload()

    def _call(self, fn, body=None, match=None):
        return call(self.con, fn, body=body, match=match, parent=self)

    def reload(self):
        data = call(self.con, server.list_categories, quiet=True) or {}
        self._flat = {n["id"]: n for n in (data.get("flat") or [])}
        clear_tree(self.tree)

        def put(node, parent_iid=""):
            self.tree.insert(parent_iid, "end", iid=str(node["id"]), text=node["name"],
                             open=True,
                             values=(node["name"], node["own"], node["total"], node["path"]))
            for ch in node.get("children") or []:
                put(ch, str(node["id"]))

        for n in data.get("items") or []:
            put(n)
        loose = int(data.get("loose") or 0)
        self.hint.set(f"共 {len(self._flat)} 个品类节点。" +
                      (f"另有 {loose} 个元件还没挂品类,它们不在菜单里 —— "
                       f"在元件列表里能看到,编辑一下就能挂上。" if loose else ""))

    def selected(self):
        sel = self.tree.selection()
        if not sel:
            return None
        return self._flat.get(int(sel[0]))

    def add_root(self):
        name = ask_text(self, "加顶级品类", "新品类叫什么?", "")
        if not name:
            return
        if self._call(server.create_category, body={"name": name}) is not None:
            self.hint.set(f"加好了「{name}」。")
            self.reload()

    def add_child(self):
        node = self.selected()
        if node is None:
            self.hint.set("先在上面选中一个品类,再给它加子品类。")
            return
        name = ask_text(self, "加子品类",
                        f"挂在「{node['path']}」下面的新子品类叫什么?", "")
        if not name:
            return
        if self._call(server.create_category,
                      body={"name": name, "parent_id": node["id"]}) is not None:
            self.hint.set(f"加好了「{node['path']} / {name}」。")
            self.reload()

    def rename(self):
        node = self.selected()
        if node is None:
            self.hint.set("先选中要改名的品类。")
            return
        name = ask_text(self, "改品类名", "新的名字:", node["name"])
        if not name or name == node["name"]:
            return
        res = self._call(server.update_category, match=(node["id"],), body={"name": name})
        if res is not None:
            self.hint.set(f"改好了。{res.get('renamed_components') or 0} 个元件的品类跟着更新了。")
            self.reload()

    def remove(self):
        node = self.selected()
        if node is None:
            self.hint.set("先选中要删的品类。")
            return
        # 落点得按「是不是顶层」分开写:顶层没有上一级 —— 它下面的子类各自升成
        # 顶层,元件则变成**没有品类**(后端把 category_id 清空)。一律说成
        # 「挪到「未分类」」是**错的**:后端不建那一行,那句话会让人以为料被搬进
        # 了一个叫「未分类」的品类里(#32 之前的老说法;卡片右键那条 delete_cat
        # 已经是「没有品类」了,同一个动作在两个入口说法必须一致)。
        top = node.get("parent_id") is None
        n = int(node.get("total") or 0)
        msg = f"删掉「{node['path']}」?"
        kids = node.get("children") or []
        if kids:
            if top:
                # 顶层的下一级没有「上一级」可接 —— 它们各自升成顶层,名字不变
                msg += (f"\n\n它下面的 {len(kids)} 个子品类会各自升成顶层品类"
                        f"(自己原来的名字就是大类名),**一个都不会删**。")
            else:
                msg += (f"\n\n它下面的 {len(kids)} 个子品类会接到上一级品类,"
                        f"**一个都不会删**。")
        if n:
            # 这两句必须写出来:用户点「删除」时最怕的就是
            # 「连子类和料一起没了」,而这里一件都不删
            if top:
                msg += (f"\n\n挂在它(含子类)下面的 {n} 个元件会变成「没有品类」,"
                        f"**不会被删除**,之后在首页「{UNCATEGORIZED}」里找得到、"
                        f"能重新归类。")
            else:
                msg += (f"\n\n挂在它(含子类)下面的 {n} 个元件会被挪到上一级品类,"
                        f"**不会被删除**。")
        if not messagebox.askyesno("确认删除", msg, parent=self):
            return
        res = self._call(server.delete_category, match=(node["id"],))
        if res is not None:
            moved = int(res.get("moved_components") or 0)
            kids_moved = int(res.get("moved_children") or 0)
            # to 是空串 = 没有上一级 = 删的是顶层。这时既不能说成「挪到了「」」,
            # 也不能再说「挪到「未分类」」—— 那些料就是没有品类了(和 delete_cat
            # 那句一字不差地对应上)。
            to = str(res.get("to") or "").strip()
            if to:
                self.hint.set(f"删掉了。{moved} 个元件挪到了上一级「{to}」,"
                              f"{kids_moved} 个子品类接到了上一级,一个都没丢。")
            else:
                self.hint.set(f"删掉了。{moved} 个元件现在没有品类了,"
                              f"可以在「{UNCATEGORIZED}」里重新归类;"
                              f"{kids_moved} 个子品类升成了顶层,一个都没丢。")
            self.reload()


class BomReceivePane(BomPaneBase):
    """按 BOM 收料:一箱货到了,勾掉收到了的,一次全收进来。

    数量默认填 **这条需求还没入库的数量**(remaining) —— 这是「按 BOM 收货」
    该有的默认值。让人每行自己算「还差几个」是在把库房的账推给记性,而记性会出错;
    这条需求已经收了多少库里记着(received_qty),界面自己减得出来。

    只列**还没做完**的行(见 #31):一条需求全收完或者全发完之后,它就不该再
    出现在这里 —— 否则用户会以为「上个 BOM 还能再收一遍」。挡掉了几条会在
    提示里写出来,免得看起来像数据丢了。

    品类单独占一列,是因为收料时最容易出错的恰恰是「这个看起来像电阻的
    东西到底是不是电阻」:值、封装都对不上时,品类是最后一道人工检查。
    """

    SUBMIT = "✓ 勾选的全部入库"
    ONE = "✓ 只入库选中这一行"

    # 列宽账(排版体检逐个量过):非拉伸列的合计必须小于最小窗口下的可用宽度
    # (1060 宽时是 629),拉伸的「名称」才有地方可缩。加「参数」这一列之前
    # 非拉伸合计是 540,得给参数留出 100 —— 办法是从几个本来就被截断的列里
    # 各让几像素,而不是让新列把这张表撑破(超了就得横向拖才能看全)。
    # #31 又加了「已入库 / 还没动」两列:同样是从既有列里各让几像素,合计仍是 770,
    # 没有把表撑宽 —— 这两个数正是「这条需求收到哪一步了」,不给列就没地方看。
    COLS = [
        ("pick", "选", 34, "center", False),
        ("name", "名称", 150, "w", True),
        ("category", "品类", 66, "w", False),
        ("value", "值", 54, "w", False),
        ("package", "封装", 68, "w", False),
        # 属性合成一列显示(只放值,如「50V · ±5%」)。不给每个属性各开一列 ——
        # 属性是用户自己加的,加几个都不该把这张表撑破。
        ("params", "参数", 88, "w", False),
        ("designators", "位号", 66, "w", False),
        ("need", "BOM需求", 50, "e", False),
        # 「已入库」是这条需求已经收进来多少,「还没动」是还剩多少没被收或发消化掉。
        # 两个都摆出来,用户才不用拿总需求去减 —— 而那条减法正是出错的源头。
        ("received", "已入库", 50, "e", False),
        ("left", "还没动", 50, "e", False),
        ("on_hand", "现有", 38, "e", False),
        ("qty", "本次入库", 56, "e", False),
    ]

    def extra_tools(self, bar):
        # 批量按钮的字面意思是「全部」,而货常常只到了一部分。给一个字面意思就是
        # 「只有这一行」的入口,人就不用靠「先清干净别的勾」来求安心了。
        self.btn_one = ttk.Button(bar, text=self.ONE, command=self.receive_selected)
        self.btn_one.pack(side="left", padx=(6, 0))

    def build_tree(self):
        f, t = make_tree(self, self.COLS, height=13)
        t.tag_configure("done", foreground="#1e7a34")
        t.tag_configure("short", background="#fff8e6")
        return f, t

    def setup_tree(self):
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.tree.bind("<Button-3>", self.on_right_click)
        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label=self.ONE, command=self.receive_selected)
        self.menu.add_command(label="改本次入库数量…", command=self.edit_selected)
        self.menu.add_separator()
        self.menu.add_command(label="勾选这一行", command=lambda: self.set_checked(True))
        self.menu.add_command(label="取消勾选", command=lambda: self.set_checked(False))

    def on_select(self, _event=None):
        """选中的是哪一行,直接写在按钮上 —— 免得点下去才发现选的是隔壁那行。"""
        bid = self.selected_bid()
        l = self._idx.get(bid) if bid is not None else None
        if not l:
            self.btn_one.configure(text=self.ONE)
        else:
            nm = str(l.get("name") or "")[:12]
            pkg = str(l.get("package") or "")
            self.btn_one.configure(text=f"✓ 只入库「{nm}{' ' + pkg if pkg else ''}」")

    def on_right_click(self, event):
        row = self.tree.identify_row(event.y)
        if row and self.is_checkable(row):
            self.tree.selection_set(row)
            self.menu.tk_popup(event.x_root, event.y_root)
            self.menu.grab_release()
        return "break"

    def selected_bid(self):
        """当前选中的那一行对应的 bom_id;没选、或选中的不是数据行就是 None。"""
        sel = self.tree.selection()
        if not sel or not self.is_checkable(sel[0]):
            return None
        return int(sel[0])

    def edit_selected(self):
        bid = self.selected_bid()
        if bid is not None:
            self.edit_qty(str(bid))

    def set_checked(self, on):
        bid = self.selected_bid()
        if bid is None:
            return
        if on:
            self.picked.add(bid)
        else:
            self.picked.discard(bid)
        self.render()

    def receive_selected(self):
        bid = self.selected_bid()
        if bid is None:
            messagebox.showinfo(
                "提示", "先在表里点一行 —— 「只入库这一行」得知道是哪一行。\n"
                        "(要一次收多行,就在「选」那一列把它们勾上,再点右下角那个按钮。)",
                parent=self)
            return
        self.move_rows([bid], single=True)

    def reload(self):
        # pending=1 只是让后端顺手少算点(它只回「还没做完」的行,连候选料
        # 都不用去凑),**界面这一层照样自己筛一遍**:滤一遍放在别处,迟早
        # 只改一处 —— #31 这一版就是只在发料那本账上减,收进来的货在出库页
        # 还能再出一遍。两处用同一条 still_open,谁漏改都还有另一层接住。
        data = call(self.con, server.project_bom, match=(str(self.project_id),),
                    query={"pending": "1"}, quiet=True)
        if data is None:
            return
        lines = list(data.get("lines") or [])
        rows = [l for l in lines if still_open(l)]
        self.lines = rows
        # 藏掉几条要说一句:只看到行数变少,人第一反应是「我的 BOM 呢」。
        # 后端已经挡掉的那批不在 lines 里,所以两家分别数、相加不会重复计数。
        # .get 兜底:后端那个字段还没落地时也不能崩。
        self.hidden_done = (int(data.get("hidden_done") or 0)
                            + len(lines) - len(rows))
        bids = {l["bom_id"] for l in self.lines}
        # 数量默认取「还没入库的那部分」(remaining),不再取 BOM 总需求:
        # 从前默认总需求是因为「还差几个」得人自己算,现在这条需求已经收了多少
        # 库里记着(received_qty),再让人拿总需求去减,等于明知故问。
        # 已经手改过的保留 —— 刷新往往是别处顺手触发的,把用户填好的数换掉,
        # 他会以为自己刚才看错了。
        keep = self.qty
        self.qty = {}
        for l in self.lines:
            bid = l["bom_id"]
            # remaining 万一没回(后端那个字段还没落地),退回「需求 − 已入库」再退
            # 到总需求。默认填 0 的话,点「一键入库」会被判成「没什么可收的」——
            # 而那正是他刚挑出来的行。
            rem = l.get("remaining")
            if rem is None:
                rem = max(0, int(l.get("need") or 0)
                          - int(l.get("received_qty") or 0))
            self.qty[bid] = int(keep.get(bid, rem))
        self.picked &= bids
        self.render()

    def render(self):
        # 重画会清掉选中行,所以先记住。render() 大多是被「勾选一行」「改数量」
        # 触发的 —— 不接回来的话,刚勾完选中就没了,接着点「只入库这一行」
        # 会被问「先在表里点一行」,看起来像上一步操作把界面弄坏了。
        keep = self.tree.selection()
        clear_tree(self.tree)
        self._idx = {}
        kw = self.q.get().strip().lower()
        shown = 0
        for l in self.lines:
            if kw and kw not in " ".join(
                    str(l.get(k) or "") for k in
                    ("name", "category", "value", "package", "designators",
                     "params")).lower():
                continue
            shown += 1
            bid = l["bom_id"]
            self._idx[bid] = l
            on = bid in self.picked
            self.tree.insert("", "end", iid=str(bid), values=(
                CHECK_ON if on else CHECK_OFF, l.get("name") or "",
                l.get("category") or "未分类", l.get("value") or "",
                l.get("package") or "", attrs.fmt(l.get("params")),
                l.get("designators") or "",
                l.get("need") or 0, l.get("received_qty") or 0,
                l.get("remaining") or 0, l.get("on_hand") or 0,
                self.qty.get(bid, 0)),
                tags=("done" if l.get("gap") == 0 else "short",))
        # 把选中接回来(只接还在的),并让按钮上的字跟着更新
        for iid in keep:
            if self.tree.exists(str(iid)):
                self.tree.selection_set(str(iid))
                break
        self.on_select()
        n = len(self.picked)
        total = sum(int(self.qty.get(b, 0) or 0) for b in self.picked)
        # 藏掉的行要说一句:只看到行数变少,人第一反应是「我的 BOM 呢」
        tail = f";{self.hidden_done} 条已经做完,不再列出" if self.hidden_done else ""
        self.hint.set(f"显示 {shown} / {len(self.lines)} 行{tail};"
                      + self.moved_text(n, total))
        self.btn_go.configure(
            text=self.SUBMIT + (f"({n} 行)" if n else ""))

    def is_checkable(self, iid):
        # Treeview 的 iid 一律是字符串,而 _idx 是按 int 的 bom_id 索引的 ——
        # 直接 `iid in self._idx` 永远为假,点上去一点反应都没有
        return str(iid).isdigit() and int(iid) in self._idx

    def check_all(self, on):
        for l in self.lines:
            bid = l["bom_id"]
            if on:
                self.picked.add(bid)
            else:
                self.picked.discard(bid)
        self.render()

    def toggle(self, iid):
        bid = int(iid)
        if bid in self.picked:
            self.picked.discard(bid)
        else:
            self.picked.add(bid)
        self.render()

    def edit_qty(self, iid):
        bid = int(iid)
        l = self._idx.get(bid)
        if not l:
            return
        raw = ask_text(self, "本次入库数量",
                       f"「{l.get('name')}」这次入库多少?\n"
                       f"(BOM 需求 {l.get('need')},"
                       f"已经入库 {l.get('received_qty') or 0},"
                       f"还剩 {l.get('remaining') or 0} 个没动)",
                       str(self.qty.get(bid, 0)))
        if raw is None:
            return
        raw = raw.strip()
        if not raw.isdigit():
            messagebox.showinfo("提示", "数量要填非负整数。", parent=self)
            return
        self.qty[bid] = int(raw)
        self.picked.add(bid)        # 改了数量就是想收它,顺手勾上
        self.render()

    def submit(self):
        """批量:把勾上的行一起收进来。"""
        if not self.project_id:
            messagebox.showinfo("提示", "先在左边选一个项目。", parent=self)
            return
        self.move_rows([b for b in self.qty if b in self.picked])

    def move_rows(self, bids, single=False):
        """把这几行按「本次入库」收进来。批量入库和单个入库**共用这一条路**。

        必须共用:两条路要是各写一份确认和落库,迟早只有一条记得挡「数量是 0」
        「没选仓位」「这个项目还没选」,而这种分叉恰恰会在收料的时候出事。
        差别只在措辞(「这一行」还是「勾选的 N 行」)。
        """
        if not self.project_id:
            messagebox.showinfo("提示", "先在左边选一个项目。", parent=self)
            return
        rows = [b for b in bids if b in self._idx and int(self.qty.get(b, 0) or 0) > 0]
        if not rows:
            messagebox.showinfo(
                "提示",
                "这一行的「本次入库」是 0,没什么可收的。\n双击那一行可以改数量。"
                if single else
                "还没有勾选要入库的行。\n"
                "在「选」那一列点一下就能勾上;数量默认是这条需求**还没入库**的数,\n"
                "双击一行可以改。想在哪儿收哪一行,就选中它用「只入库这一行」。",
                parent=self)
            return
        # 名字撞车是常态(100nF 0603 和 100nF 0805 都叫 100nF),确认框里带上封装,
        # 否则人看到两行一模一样的「100nF」根本分不清自己收的是哪颗
        def label(b):
            l = self._idx[b]
            pkg = str(l.get("package") or "")
            return f"{l.get('name') or ''}{' ' + pkg if pkg else ''}"

        items = [{"component_id": self._idx[b]["component_id"],
                  "qty": int(self.qty.get(b, 0) or 0),
                  "bom_id": b, "note": self.note.get().strip()} for b in rows]
        total = sum(i["qty"] for i in items)
        what = "这一行" if single else f"勾选的 {len(items)} 行"
        if not messagebox.askyesno(
                "确认入库",
                f"要把{what}、共 {total} 个收进来吗?\n\n"
                + "\n".join(f"  {label(i['bom_id'])}  ×{i['qty']}" for i in items[:10])
                + ("\n  …" if len(items) > 10 else ""), parent=self):
            return
        res = call(self.con, server.stock_batch,
                   body={"kind": "IN", "items": items,
                         "location": self.loc.get().strip() or "未分类",
                         "operator": self.who.get().strip() or "本地用户",
                         "project_id": self.project_id},
                   parent=self)
        if res is None:
            return
        got, bad = len(res.get("done") or []), res.get("failed") or []
        # 只清掉这次真收了的行。原来是一把全清 —— 单个入库时那会把别的勾也抹掉,
        # 正要接着收第二行的人得重新勾一遍
        for b in rows:
            self.picked.discard(b)
        self.note.set("")
        if bad:
            messagebox.showwarning(
                "入库结果",
                f"成功 {got} 行 / {res.get('total_qty') or 0} 个。\n\n没成的:\n"
                + "\n".join(f"  {b.get('name') or b.get('component_id')}:"
                            f"{b.get('reason')}" for b in bad[:8]), parent=self)
        else:
            self.app.set_status(
                f"按 BOM 入库完成:{got} 行 / {res.get('total_qty') or 0} 个", 8)
        if self.on_done:
            self.on_done()


class BomPickPane(BomPaneBase):
    """按 BOM 发料:一条需求可以由几颗不同的库存料凑齐。

    库里 0603 有 8 个、0805 有 2 个,而 BOM 要 10 个 —— 这是常态不是例外,
    导出的 BOM 常常连封装都不写全。所以这里不能给一张「库存元件」的平表
    让人自己心算,得把每条 BOM 需求摊开、把能凑它的料挂在下面:勾一颗、
    填个数,父行上的「还能出库」立刻跟着减,下一颗该出几个一眼就能看出来。

    只列**还能出**的需求(见 #31):一条需求全发完或者全收完之后,它就不该
    再出现在这里 —— 否则用户会以为「上个 BOM 还能再出一次」。挡掉了几条会在
    提示里写出来。
    """

    SUBMIT = "✓ 按这个分配出库"

    # 参数列和收料清单同宽同位置:两张表是同一件事的两半,列对不齐最难认。
    # 非拉伸合计 34+76+68+86+104+50+68+58 = 544,小于最小窗口下的 629,
    # 「像在哪儿」和树列这两根拉伸列还有余量可让。
    COLS = [
        ("pick", "选", 34, "center", False),
        ("category", "品类", 76, "w", False),
        ("value", "值", 68, "w", False),
        ("package", "封装", 86, "w", False),
        ("params", "参数", 104, "w", False),
        ("on_hand", "库存", 50, "e", False),
        ("qty", "本次出库", 68, "e", False),
        # 表头写「还能出库」而不是「还需要」:这个数现在减掉的是**两本账**
        # (已经发出去的 + 已经收进来的),说「还需要」会让人以为只扣了发料(#31)
        ("left", "还能出库", 58, "e", False),
        ("match", "像在哪儿", 110, "w", True),
    ]

    def build_tree(self):
        f, t = make_tree(self, self.COLS, height=13, show="tree headings")
        t.heading("#0", text="BOM 需求 ↓ 能凑它的库存料", anchor="center")
        # 列宽账(和左边项目列表在同一个 Panedwindow 里,请求宽度涨了左边就被挤):
        # 非拉伸 440 + 参数 104 + 树列 + 「像在哪儿」要 <= 加列之前的 804,
        # 否则连左边那张只剩 2px 余量的项目表都会一起超。150 + 110 正好补齐。
        # #0 树列不在 COLS 里,make_tree 管不到它,单独改成居中(#27)
        t.column("#0", width=150, anchor="center", stretch=True)
        t.tag_configure("line", background="#eef4fb")
        t.tag_configure("covered", foreground="#1e7a34")
        # 封装和这条需求一模一样的候选:能直接装上去,发错货的重灾区就在
        # 「值一样、封装不一样」那两颗之间,所以单独给个颜色
        t.tag_configure("fp", foreground="#0b6bcb")
        t.tag_configure("own", foreground="#1e7a34")
        t.tag_configure("empty", foreground="#999")
        # issue #39:领不到料的行 —— 需求行还有剩余却一个候选都凑不出来,或者候选自己库存是 0。
        # 底色和库存页的「缺货」用同一个值;#39 的要害是**替换**而不是叠加:
        # 多个 tag 都配了 background 时,Tk 的规则是"谁先 tag_configure 谁赢",
        # 与 item(tags=[...]) 的顺序无关 —— 直接叠一个红会被先注册的 line(淡蓝底)压掉。
        t.tag_configure("short", background="#ffe3e3")
        return f, t

    def extra_tools(self, bar):
        ttk.Button(bar, text="自动配齐", command=self.auto_fill).pack(side="left", padx=4)
        ttk.Button(bar, text="展开全部", command=lambda: self.set_open_all(True)
                   ).pack(side="left")
        ttk.Button(bar, text="合上全部", command=lambda: self.set_open_all(False)
                   ).pack(side="left", padx=4)

    def reload(self):
        data = call(self.con, server.project_pick_plan,
                    match=(str(self.project_id),), quiet=True)
        if data is None:
            return
        lines = list(data.get("lines") or [])
        # 后端(pick_plan)已经挡过一遍,界面按同一条 still_open 再挡一遍 ——
        # 收料页和出库页要的是同一个「还欠着吗」的答案,两处都挡、两家分别数,
        # 谁漏改都还有另一层接住(#31 漏的就是「只改了一边」)
        self.lines = [l for l in lines if still_open(l)]
        self.hidden_done = (int(data.get("hidden_done") or 0)
                            + len(lines) - len(self.lines))
        # 分配是用户一个一个勾出来的,所以能留就留:候选还在、库存还够的
        # 留着并按新库存收窄;候选没了(那颗料被并掉、或库存归零)就丢掉。
        # 开完单时 submit 已经先清过 alloc 了,所以这里不会把发出去的勾又捡回来。
        keep = self.alloc
        self.alloc = {}
        for l in self.lines:
            bid = l["bom_id"]
            for c in l.get("candidates") or []:
                q = int(keep.get((bid, c["id"]), 0) or 0)
                if q > 0:
                    q = min(q, int(c.get("on_hand") or 0))
                    if q > 0:
                        self.alloc[(bid, c["id"])] = q
        self._open = {l["bom_id"] for l in self.lines if l.get("remaining")}
        self.render()

    # ------------------------------------------------------------ 计算

    def used(self, bid) -> int:
        return sum(int(q) for (b, _c), q in self.alloc.items() if b == bid)

    def remain(self, bid) -> int:
        l = next((x for x in self.lines if x["bom_id"] == bid), None)
        if not l:
            return 0
        return max(0, int(l.get("remaining") or 0) - self.used(bid))

    def default_qty(self, bid, cand) -> int:
        """勾上一颗料时默认出几个:够补这一行的缺口就填缺口,不够就全出。

        缺口已经补满时兜底给 1 —— 那是「我还想多发几个备用」,让人自己改,
        不该悄悄变成 0(勾了却出 0 个最让人困惑)。
        """
        gap = self.remain(bid)
        stock = int(cand.get("on_hand") or 0)
        if gap <= 0:
            return min(stock, 1)
        return min(stock, gap)

    # ------------------------------------------------------------ 渲染

    def render(self):
        clear_tree(self.tree)
        self._idx = {}
        kw = self.q.get().strip().lower()
        shown = 0
        # 显示名现在只放值,所以同值的两条需求(100nF 0603 / 100nF 0805)在这棵树里
        # 会长得一模一样 —— 它们本来是不同的两条。只在真撞车时才把封装补进树列:
        # 平时封装已经单独占一列,不必重复。补的是显示层的兜底,不参与任何匹配。
        dup = {}
        for l in self.lines:
            nm = str(l.get("name") or "")
            dup[nm] = dup.get(nm, 0) + 1
        for l in self.lines:
            bid = l["bom_id"]
            if kw and kw not in " ".join(
                    str(l.get(k) or "") for k in
                    ("name", "category", "value", "package", "designators",
                     "params")).lower():
                continue
            shown += 1
            rem, used = self.remain(bid), self.used(bid)
            need = int(l.get("need") or 0)
            mark = "✓ 齐了" if rem == 0 else f"还能出 {rem}"
            nm = str(l.get("name") or "")
            pkg = str(l.get("package") or "").strip()
            # 只在同名出现不止一次时补封装,而且是补在树列上当作区分用的后缀
            text = f"{nm}{' ' + pkg if pkg and dup.get(nm, 0) > 1 else ''}" \
                   f"  ×{need}   [{mark}]"
            # issue #39:这一行**领不到料**就标红。rem == 0 是"这次已经配齐";
            # 真领不到的是「还有剩余(rem > 0)却一个候选都没有」——
            # 候选列表只列有库存的料,所以"没有候选"就等于"库里凑不出来"。
            if rem > 0 and not (l.get("candidates") or []):
                tags = ["short"]
            else:
                tags = ["line"]
                if rem == 0 and need:
                    tags.append("covered")
            self.tree.insert("", "end", iid=str(bid), text=text, open=(bid in self._open),
                             values=("", l.get("category") or "未分类",
                                     l.get("value") or "", l.get("package") or "",
                                     attrs.fmt(l.get("params")),
                                     l.get("available") or 0, used, rem, ""),
                             tags=tuple(tags))
            cands = l.get("candidates") or []
            if not cands:
                # 没有候选不是「没数据」,是「这颗料库里一个都没有」——
                # 得说出来,否则展开是空的会让人以为界面坏了。
                # 第一个参数是父行 id:挂在需求下面,不然它会变成一条跟
                # 需求平级的孤立行,看着像另一条 BOM。
                self.tree.insert(str(bid), "end", iid=f"{bid}:none", text="",
                                 values=("", "", "", "", "", "", "", rem,
                                         "库存里没有能凑它的料,得先入库或设替代料"),
                                 tags=("short",))
                continue
            for c in cands:
                key = (bid, c["id"])
                self._idx[key] = c
                on = key in self.alloc
                # issue #39:候选自己库存是 0 也算"领不到"。现在候选被"只列有库存的"挡着,
                # 基本触发不到,但规则要在。同样走**替换**,保持"一行最多一个配背景色的 tag"。
                if not int(c.get("on_hand") or 0):
                    tags = ("short",)
                else:
                    tags = ("own",) if c.get("own") else ()
                    if c.get("fp_match"):
                        tags += ("fp",)
                self.tree.insert(str(bid), "end", iid=self.child_iid(bid, c["id"]),
                                 text="", values=(
                                     CHECK_ON if on else CHECK_OFF,
                                     c.get("category") or "未分类",
                                     c.get("value") or "", c.get("package") or "",
                                     # 出库时最需要区分的正是「值封装一样、耐压不同」的两颗料 ——
                                     # 候选料是 component_row() 来的,本来就带 params
                                     attrs.fmt(c.get("params")),
                                     c.get("on_hand") or 0,
                                     self.alloc.get(key, ""), rem,
                                     c.get("match") or ""),
                                 tags=tags)
        n = len(self.alloc)
        total = sum(int(q) for q in self.alloc.values())
        # 有几条需求会被这次出库补满,是这一屏最该看到的结论
        done = sum(1 for l in self.lines
                   if l.get("remaining") and self.remain(l["bom_id"]) == 0)
        self.hint.set(f"显示 {shown} / {len(self.lines)} 条需求;"
                      + self.moved_text(n, total) + (f",补齐 {done} 条" if done else "")
                      + (f";{self.hidden_done} 条已经做完,不再列出"
                         if self.hidden_done else ""))
        self.btn_go.configure(text=self.SUBMIT + (f"({n} 行)" if n else ""))

    @staticmethod
    def child_iid(bid, cid):
        return f"{bid}:{cid}"

    def set_open_all(self, on):
        for l in self.lines:
            bid = l["bom_id"]
            if on:
                self._open.add(bid)
            else:
                self._open.discard(bid)
        self.render()

    # ------------------------------------------------------------ 勾选

    def is_checkable(self, iid):
        """iid 形如「BOM行号:元件号」才是可勾选的子行。

        「5:none」那种占位提示行会被这里挡掉 —— 它不是真的候选,
        勾不了,双击也不该弹出改数量的框。
        """
        key = self.key_of(iid)
        return key is not None and key in self._idx

    @staticmethod
    def key_of(iid):
        parts = str(iid).split(":", 1)
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            return None
        return int(parts[0]), int(parts[1])

    def toggle(self, iid):
        bid_s, cid_s = str(iid).split(":", 1)
        bid, cid = int(bid_s), int(cid_s)
        key = (bid, cid)
        if key in self.alloc:
            del self.alloc[key]
        else:
            cand = self._idx.get(key)
            if not cand:
                return
            self.alloc[key] = self.default_qty(bid, cand)
        self._open.add(bid)             # 勾了就把这一行留着展开,好接着看
        self.render()
        if self.tree.exists(str(iid)):
            self.tree.see(str(iid))

    def edit_qty(self, iid):
        bid_s, cid_s = str(iid).split(":", 1)
        bid, cid = int(bid_s), int(cid_s)
        key = (bid, cid)
        cand = self._idx.get(key) or {}
        if key not in self.alloc:
            messagebox.showinfo("提示", "先在最左边「选」那一列点一下勾上它。",
                                parent=self)
            return
        stock = int(cand.get("on_hand") or 0)
        raw = ask_text(self, "本次出库数量",
                       f"「{cand.get('name')}」这次出多少个?(库存 {stock},"
                       f"这条 BOM 还能出 {self.remain(bid)})",
                       str(self.alloc.get(key, 0)))
        if raw is None:
            return
        raw = raw.strip()
        if not raw.isdigit():
            messagebox.showinfo("提示", "数量要填非负整数。", parent=self)
            return
        n = int(raw)
        if n > stock:
            messagebox.showinfo("提示",
                                f"库存只有 {stock} 个,发不了 {n} 个。"
                                        f"(要发更多得先入库。)", parent=self)
            return
        self.alloc[key] = n
        self.render()

    def auto_fill(self):
        """按相似度从高到低,把每条需求拿现有库存凑一遍 —— 只填勾选,不提交。

        凑不齐的地方会留着,让人看见「这条还差 3 个,库里真没有」。
        """
        self.alloc = {}
        for l in self.lines:
            bid = l["bom_id"]
            for c in l.get("candidates") or []:
                if self.remain(bid) <= 0:
                    break
                take = min(int(c.get("on_hand") or 0), self.remain(bid))
                if take > 0:
                    self.alloc[(bid, c["id"])] = take
        self._open = {b for (b, _c) in self.alloc}
        self.render()

    def check_all(self, on):
        if not on:
            self.alloc = {}
        else:
            self.auto_fill()
            return
        self.render()

    def check_for(self, bid, cid) -> bool:
        """把「这条需求用这颗料顶」勾上(从 BOM 明细的「找相似」跳过来时用)。"""
        key = (bid, cid)
        if key not in self._idx:
            return False
        if key not in self.alloc:
            self.alloc[key] = self.default_qty(bid, self._idx[key])
        self._open.add(bid)
        self.render()
        iid = self.child_iid(bid, cid)
        if self.tree.exists(iid):
            self.tree.selection_set(iid)
            self.tree.see(iid)
            self.tree.focus(iid)
        return True

    # ------------------------------------------------------------ 提交

    def submit(self):
        if not self.project_id:
            messagebox.showinfo("提示", "先在左边选一个项目。", parent=self)
            return
        items = [{"bom_id": b, "component_id": c, "qty": int(q)}
                 for (b, c), q in sorted(self.alloc.items()) if int(q) > 0]
        if not items:
            messagebox.showinfo(
                "提示", "还没有勾选要出库的料。\n"
                        "在「选」那一列点一下就能勾上,或者点「自动配齐」\n"
                        "让它按相似度先配一遍。", parent=self)
            return
        total = sum(i["qty"] for i in items)
        # 出库是扣库存,点错了要一条条撤销 —— 值得把要动的东西先念一遍
        preview = "\n".join(
            f"  {self._idx[(i['bom_id'], i['component_id'])].get('name')}"
            f"  ×{i['qty']}" for i in items[:10])
        if not messagebox.askyesno(
                "确认出库",
                f"要出 {len(items)} 行、共 {total} 个吗?\n\n{preview}"
                + ("\n  …" if len(items) > 10 else ""), parent=self):
            return
        res = call(self.con, server.pick_for_project,
                   match=(str(self.project_id),),
                   body={"items": items,
                         "location": self.loc.get().strip() or "未分类",
                         "operator": self.who.get().strip() or "本地用户",
                         "note": self.note.get().strip()},
                   parent=self)
        if res is None:
            return
        got, bad = len(res.get("picked") or []), res.get("failed") or []
        self.alloc = {}
        self.note.set("")
        if bad:
            messagebox.showwarning(
                "出库结果",
                f"成功 {got} 行。\n\n没成的:\n"
                + "\n".join(f"  {b.get('reason')}" for b in bad[:8]), parent=self)
        else:
            self.app.set_status(f"按 BOM 出库完成:{got} 行 / {total} 个", 8)
        if self.on_done:
            self.on_done()


class MovePane(ttk.Frame):
    """一个方向的开单 + 这个项目在这个方向的记录。

    记录就留在开单区下面,因为「刚收的这笔进去没有」是紧接着要确认的事。
    让人切到别的页去核对,录错的那一笔就会一直错下去。
    """

    def __init__(self, parent, app: App, action: str):
        super().__init__(parent, padding=6)
        self.app = app
        self.con = app.con
        self.action = action
        self.project_id = None
        self._loaded = False

        pane = ttk.Panedwindow(self, orient="vertical")
        pane.pack(fill="both", expand=True)

        # 两种开单方式。默认「按 BOM」—— 因为记录本来就挂在项目名下,
        # 而项目就该照 BOM 收发货;「自由」那条路是留给不在 BOM 上的东西的
        # (螺丝、锡、随手补的料),它不该是主路径,但也不能没有。
        top = ttk.Frame(pane)
        bar = ttk.Frame(top)
        bar.pack(fill="x", pady=(0, 4))
        self.mode = tk.StringVar(value="bom")
        ttk.Radiobutton(bar, text=" 按 BOM 收料 " if action == "IN" else " 按 BOM 出库 ",
                        value="bom", variable=self.mode, style="Toolbutton",
                        command=self._sync_mode).pack(side="left")
        ttk.Radiobutton(bar, text=" 自由入库 " if action == "IN" else " 自由出库 ",
                        value="free", variable=self.mode, style="Toolbutton",
                        command=self._sync_mode).pack(side="left", padx=4)
        self.mode_hint = tk.StringVar()
        ttk.Label(bar, textvariable=self.mode_hint, style="Dim.TLabel"
                  ).pack(side="left", padx=8)

        self.work = ttk.Frame(top)
        self.work.pack(fill="both", expand=True)
        cls = BomReceivePane if action == "IN" else BomPickPane
        self.bom_form = cls(self.work, app, action, on_done=self._after_move)
        self.form = MoveForm(self.work, app, action, on_done=self._after_move)
        pane.add(top, weight=3)

        box = ttk.LabelFrame(pane, text=f"{KIND_LABEL.get(action, action)}记录"
                                        f"(按时间倒序)", padding=6)
        head = ttk.Frame(box)
        head.pack(fill="x")
        self.rec_hint = tk.StringVar()
        ttk.Label(head, textvariable=self.rec_hint, style="Dim.TLabel").pack(side="left")
        ttk.Button(head, text="↶ 撤销选中的记录", command=self.undo).pack(side="right")
        f, self.t_rec = make_tree(box, [
            ("created_at", "时间", 140, "center"),
            ("component_name", "元件", 170, "w", True),
            ("lcsc_pn", "商品编号", 86, "center"),
            ("qty", "数量", 52, "e"),
            ("location_code", "仓位", 84, "w"),
            ("operator", "操作人", 76, "center"),
            ("note", "备注", 165, "w", True)], height=6)
        f.pack(fill="both", expand=True)
        # 撤销过的淡掉但不隐藏 —— 历史要看得见,只是别再当它是有效的
        self.t_rec.tag_configure("voided", foreground="#95a5a6")
        self.t_rec.tag_configure("reversal", foreground="#2471a3")
        pane.add(box, weight=2)
        self._sync_mode()

    # ---------------------------------------------------------- 模式

    def _sync_mode(self):
        if self.mode.get() == "bom":
            self.form.pack_forget()
            self.bom_form.pack(fill="both", expand=True)
            self.mode_hint.set(
                # #31 之后这两句都得改口径:入库默认填的是「还没收的数」(不是总需求),
                # 出库页那个数减掉的是「已收 + 已发」两本账,再叫「还需要」会让人
                # 以为只扣了发料
                "勾选 + 一键入库,数量默认取这条需求还没收的数,品类就在表里。"
                if self.action == "IN" else
                "一条 BOM 需求可以由几颗库存料凑齐;勾一颗、填个数,还能出几个会跟着减。")
            # 已经为这个项目装过就不再刷。出库那边的勾选是**人手一个个勾出来
            # 的**,来回切一下模式就清空,等于把刚做的工作扔掉
            if (self.bom_form.project_id != self.project_id
                    or not getattr(self.bom_form, "lines", None)):
                self.bom_form.set_project(self.project_id)
        else:
            self.bom_form.pack_forget()
            self.form.pack(fill="both", expand=True)
            self.mode_hint.set("不在 BOM 上的东西从这儿开单。")
            if self.project_id:
                self.form.reload()

    def show_bom(self):
        self.mode.set("bom")
        self._sync_mode()

    def show_free(self):
        self.mode.set("free")
        self._sync_mode()

    def set_project(self, pid):
        """换项目:上下文、两张开单表、记录表一起更新。

        几件事必须一起做。只设上下文不刷元件列表的话,开单区左边是空的,
        看起来像「库里没有元件」—— 那是这一页最不能出的错。

        **同一个项目重复调等于什么都不做。** 这不是省一次查询那么简单:
        子页签一切换就会走到这里,而「按 BOM 出库」上挂的分配是用户一个勾
        一个勾点出来的,每切一次就清空一次的话,他刚勾好的东西会莫名消失。
        真要重刷(刚开完单、库存变了)走 refresh_panes()。
        """
        pid = pid or None
        if pid == self.project_id and self._loaded:
            return
        self.project_id = pid
        self._loaded = True
        self.form.set_project(self.project_id)
        self.bom_form.set_project(self.project_id)
        if self.mode.get() == "free":
            self.form.reload()
        self.load_records()

    def reload(self):
        self.refresh_panes()

    def refresh_panes(self):
        """强制按当前项目重刷两张开单表和记录表。

        开完单必须刷:那两张表上都写着「现有」和「还差」,不刷的话屏幕上
        还是改动前的数字,而下一次开单正是照着它填的。
        """
        self._loaded = False
        self.set_project(self.project_id)

    def _after_move(self):
        """刚开完一笔单:记录表要刷,当前那张开单表也要刷。

        只刷记录表的话,开单表里还写着改动前的库存 —— 而那正是下一次开单
        要照着填的数字。
        """
        self.load_records()
        if self.mode.get() == "bom":
            if self.bom_form.project_id:
                self.bom_form.reload()
        elif self.project_id:
            self.form.reload()

    def load_records(self):
        clear_tree(self.t_rec)
        if not self.project_id:
            self.rec_hint.set("先在左边选一个项目。")
            return
        data = call(self.con, server.list_movements,
                    query={"kind": self.action, "project_id": str(self.project_id),
                           "limit": 500}, quiet=True) or {}
        items = data.get("items") or []
        for mv in items:
            if mv.get("voided"):
                tags = ("voided",)
            elif mv.get("void_of"):
                tags = ("reversal",)
            else:
                tags = ()
            self.t_rec.insert("", "end", iid=str(mv["id"]), values=(
                (mv.get("created_at") or "")[:19], mv.get("component_name") or "",
                mv.get("lcsc_pn") or "", mv.get("qty") or 0,
                mv.get("location_code") or "", mv.get("operator") or "",
                mv.get("note") or ""), tags=tags)
        live = [mv for mv in items if not mv.get("voided")]
        total = sum(int(mv.get("qty") or 0) for mv in live)
        self.rec_hint.set(f"{len(items)} 条,有效合计 {total} 个" if items
                          else "还没有记录")

    def undo(self):
        sel = self.t_rec.selection()
        if not sel:
            messagebox.showinfo("提示", "先点一行要撤销的记录。", parent=self)
            return
        vals = self.t_rec.item(sel[0], "values")
        label = (f"{KIND_LABEL.get(self.action, '')}  {vals[1]}  {vals[3]} 个  "
                 f"{vals[4]}\n{vals[0]}")
        if vals[6]:
            label += f"\n{vals[6]}"
        undo_movement(self, self.app, int(sel[0]), label)


# --------------------------------------------------------------- 流水按项目折叠

#: 没挂项目的流水(日常补货 / 领用)归到这一组。用户的话是「单独归成一组」。
NONE_GROUP = "(不开项目)"


class MovementGroups:
    """把「按项目折叠的流水表」这套行为收在一处(#30)。

    「流水」页(全部动作)和「出入库」页的入库流水 / 出库流水,原本都是同一张
    把所有流水按时间倒序平铺的长表:不同项目的料混在一起,项目一多就没法看。
    用户原话是「不然看着太乱了,一直展开在那边」—— 所以按项目分组,而且
    **默认收着**,想看哪个项目再点开。

    为什么抽成一个类、而不是三个页签各写一份:折叠 / 展开、组行的样子、
    「撤销」必须跳过组行 —— 这几件事在三处必须一模一样,不然用户在「流水」页
    学会的动作,到「入库流水」页就不灵了。

    折叠状态存在这个对象里(也就是页签对象里),刷新、撤销、切页签都不会把
    用户收好的组又弹开;关掉程序才忘掉 —— 用户要的就是「别每次刷新都弹回全展开」。
    """

    #: 组行 iid 的前缀。流水行的 iid 就是流水 id(纯数字),两者不会撞。
    PREFIX = "g"

    def __init__(self, tree, columns):
        self.tree = tree
        self.columns = list(columns)
        #: 展开着的组键。空集 = 全收着,这就是默认状态
        self.expanded = set()
        #: [(组键, 组行 iid, 组名, [该组的流水 id...]), ...],按组第一次出现的先后
        self.groups = []
        self._blank = ("",) * len(self.columns)
        # 组行要有别于流水行的样子 —— 没有底色的话它读起来就是一行内容空了大半的
        # 流水,「点它能展开」这件事根本看不出来
        tree.tag_configure("pgroup", background="#e6eef9", foreground="#1f3a5f")
        # 鼠标点击那条路在 _on_click 里记账;键盘(空格 / 回车)和直接点小三角
        # 由 ttk 自己处理,我们接不到 —— 那种情况靠重建前的 _harvest() 兜住。
        tree.bind("<Button-1>", self._on_click, add="+")

    # ---------------------------------------------------------------- 查
    @staticmethod
    def key_of(mv):
        """把一笔流水归到哪一组。没挂项目的那些全归 0 号组。"""
        # 项目 id 从 1 开始(自增主键),所以 0 当「不开项目」这个哨兵不会跟谁撞
        return mv.get("project_id") or 0

    @staticmethod
    def name_of(mv):
        return mv.get("project_name") or NONE_GROUP

    def is_group(self, iid):
        return str(iid).startswith(self.PREFIX)

    def group_count(self):
        return len(self.groups)

    def row_ids(self):
        """表里**真正的流水行**(不含组行)的 iid,按显示顺序。

        撤销、断言都走这个:组行不是任何一笔账,拿它去撤销只会补出一笔
        莫名其妙的流水。组行收着的时候它的子行其实没显示出来,但它们在表里
        就是存在的,所以这里一律算上。
        """
        out = []
        for _key, _gid, _name, ids in self.groups:
            out += ids
        return out

    # ---------------------------------------------------------------- 画
    def _harvest(self):
        """重建之前,先把树上真实的展开状态收回来。

        用户不一定走我们那个点击处理:键盘的空格 / 回车、或者直接点组名前面
        那个小三角,都是 ttk 自己处理的。与其去追每一种交互(漏一个的表现就是
        「我明明收起来了,一刷新又弹开」),不如在重建前以树上真实的 -open 为准。
        """
        for key, gid, _name, _ids in self.groups:
            if not self.tree.exists(gid):
                continue
            if self.tree.item(gid, "open"):
                self.expanded.add(key)
            else:
                self.expanded.discard(key)

    def render(self, items, values_of, tags_of):
        """items 是 list_movements 那一份;values_of / tags_of 由页签自己给。

        三个页签的列不一样(「流水」页还有动作全称和「移到」,入库流水没有),
        分组逻辑却是同一套,所以单元格的内容让调用方算,这里只管层次。
        """
        self._harvest()
        clear_tree(self.tree)
        self.groups = []
        order, bucket = [], {}
        for mv in items:
            key = self.key_of(mv)
            if key not in bucket:
                bucket[key] = []
                order.append(key)
            bucket[key].append(mv)
        for key in order:
            rows = bucket[key]
            gid = f"{self.PREFIX}{key}"
            # 流水 id 统一存成字符串:它就是树里的 iid,tree.insert 收进去、
            # tree.get_children 吐出来都是字符串,别在这里一会儿 int 一会儿 str
            self.groups.append((key, gid, self.name_of(rows[0]),
                                [str(mv["id"]) for mv in rows]))
            self._put_group(gid, key, self.name_of(rows[0]), len(rows))
            for mv in rows:
                self.tree.insert(gid, "end", iid=str(mv["id"]),
                                 values=values_of(mv), tags=tags_of(mv))

    def _put_group(self, gid, key, name, n):
        open_ = key in self.expanded
        self.tree.insert("", "end", iid=gid, open=open_, tags=("pgroup",),
                         text=f"{'▼' if open_ else '▶'} {name}({n} 笔)",
                         values=self._blank)

    def _relabel(self, key):
        """展开 / 收起之后更新组行上的箭头。笔数不用重算,它没变。"""
        for k, gid, name, ids in self.groups:
            if k == key:
                open_ = k in self.expanded
                self.tree.item(gid, open=open_,
                               text=f"{'▼' if open_ else '▶'} {name}({len(ids)} 笔)")
                return

    # ---------------------------------------------------------------- 折叠
    def toggle(self, iid):
        key = self._key_of_iid(iid)
        if key is None:
            return False
        if key in self.expanded:
            self.expanded.discard(key)
        else:
            self.expanded.add(key)
        self._relabel(key)
        return True

    def _key_of_iid(self, iid):
        for key, gid, _name, _ids in self.groups:
            if gid == str(iid):
                return key
        return None

    def _on_click(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid or not self.is_group(iid):
            return None
        self.toggle(iid)
        # 吃掉这次点击。不挡的话 ttk 自己的 Press 绑定还会再切一次(点了等于
        # 没点),而且组行会被选中 —— 「撤销」只该针对具体某一笔流水,不该有
        # 「正好选中了组行」这种状态。键盘上下键仍然能把选中挪到组行上,
        # 那种情况由 undo() 里那道判断兜住。
        return "break"


class StockTab(ttk.Frame):
    """出入库 —— 只查账,不开单。

    「出入库」到底该是什么?结论是**流水**:它回答的是「什么时候进了什么、
    什么时候出了什么」。而真正的收料/发料动作天生属于某个项目,所以在
    「项目 BOM」页里做(见 MovePane)—— 那边才有这张 BOM 缺什么可看。

    拆成【入库流水】和【出库流水】两页,是因为看这两件事的时机不同:
    收料时对着入库流水核对这批来了没有,发料时对着出库流水核对这块板领齐没有。

    盘点 / 移库既不是进也不是出,所以只出现在「流水」页,不在这里。
    """

    ACTIONS = (("IN", "入库流水"), ("OUT", "出库流水"))
    ALL, NONE_PROJ = "全部项目", "不指定项目"

    # 列定义:(键, 标题, 宽度[, 是否随窗口伸缩])。对齐不在这里写 —— 一律居中(#27)。
    # 有两个地方按列名取列号(填行、撤销时读文案),所以列顺序改了这里一起改就够。
    COLS = [
        ("created_at", "时间", 145),
        ("kind", "动作", 96),
        ("component_name", "元件", 190, True),
        ("lcsc_pn", "商品编号", 88),
        ("qty", "数量", 52),
        ("location_code", "仓位", 86),
        ("operator", "操作人", 76),
        ("note", "备注", 190, True),
    ]

    def __init__(self, parent, app: App):
        super().__init__(parent, padding=8)
        self.app = app
        self.con = app.con
        self.action = "IN"
        self._projects = []

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 6))
        self.btn = {}
        for val, label in self.ACTIONS:
            b = ttk.Button(bar, text=label, width=11,
                           command=lambda v=val: self.set_action(v))
            b.pack(side="left", padx=(0, 6))
            self.btn[val] = b

        ttk.Label(bar, text="项目").pack(side="left", padx=(14, 4))
        self.proj = tk.StringVar(value=self.ALL)
        self.cb_proj = ttk.Combobox(bar, textvariable=self.proj, width=20,
                                    state="readonly", values=[self.ALL])
        self.cb_proj.pack(side="left")
        self.cb_proj.bind("<<ComboboxSelected>>", lambda _e: self.reload())

        ttk.Label(bar, text="条数").pack(side="left", padx=(14, 4))
        self.limit = tk.StringVar(value="300")
        ttk.Combobox(bar, textvariable=self.limit, width=6, state="readonly",
                     values=["100", "300", "1000", "5000"]).pack(side="left")
        ttk.Button(bar, text="刷新", command=self.reload).pack(side="left", padx=6)
        ttk.Button(bar, text="↶ 撤销选中的记录", command=self.undo).pack(side="right")

        self.summary = tk.StringVar()
        ttk.Label(self, textvariable=self.summary, style="Dim.TLabel",
                  justify="left").pack(anchor="w", pady=(0, 4))

        f, self.tree = make_tree(self, self.COLS, height=20, show="tree headings")
        # #0 树列放项目名 —— 这一页现在按项目分组(#30),项目名属于「组」这一层,
        # 不再是每一行的内容,所以原来那列 130px 的「项目」就撤掉了:组行上已经
        # 写着是哪个项目,每行再重复一遍既占宽度又吵。
        self.tree.heading("#0", text="项目", anchor="center")
        self.tree.column("#0", width=150, anchor="center", stretch=False)
        f.pack(fill="both", expand=True)
        # 撤销过的淡掉但不隐藏 —— 历史要看得见,只是别再当它是有效的
        self.tree.tag_configure("voided", foreground="#95a5a6")
        self.tree.tag_configure("reversal", foreground="#2471a3")
        self.groups = MovementGroups(self.tree, self.COLS)

        self.set_action("IN")

    def set_action(self, val):
        self.action = val
        for k, b in self.btn.items():
            b.state(["pressed"] if k == val else ["!pressed"])
        self.reload()

    def reload(self):
        data = call(self.con, server.list_projects, quiet=True) or {}
        self._projects = data.get("items") or []
        # 下拉里只放「选了一定看得到东西」的项目。导入 BOM 会建出一堆一次库都没
        # 出入过的项目,全列进来的话每挑一个都是一张空表 —— 那才是这一页真正的噪音。
        # 关键是这里不会造成死路:第一单在「项目 BOM」页里开,那边列的是全部项目。
        key = "moves_in" if self.action == "IN" else "moves_out"
        live = [p for p in self._projects if p.get(key)]
        idle = len(self._projects) - len(live)
        names = [self.ALL, self.NONE_PROJ] + [self._proj_name(p) for p in live]
        self.cb_proj.configure(values=names)
        if self.proj.get() not in names:
            self.proj.set(self.ALL)

        query = {"kind": self.action, "limit": self.limit.get()}
        pick = self.proj.get()
        if pick == self.NONE_PROJ:
            # 「不挂项目」的日常补货/领用:project_id 为空的那种流水
            query["project"] = "none"
        elif pick != self.ALL:
            hit = [p for p in live if self._proj_name(p) == pick]
            if hit:
                query["project_id"] = str(hit[0]["id"])
        data = call(self.con, server.list_movements, query=query, quiet=True) or {}
        items = data.get("items") or []

        def values_of(mv):
            # 「动作」列不是多余的:撤销一笔出库补的是入库、撤销一笔入库补的是出库,
            # 所以这一页里混着正常流水和反向流水,得一眼分得清。
            kind_txt = KIND_LABEL.get(mv.get("kind"), mv.get("kind"))
            if mv.get("voided"):
                kind_txt += "(已撤销)"
            elif mv.get("void_of"):
                kind_txt += "·撤销"
            return ((mv.get("created_at") or "")[:19], kind_txt,
                    mv.get("component_name") or "", mv.get("lcsc_pn") or "",
                    mv.get("qty") or 0, mv.get("location_code") or "",
                    mv.get("operator") or "", mv.get("note") or "")

        def tags_of(mv):
            if mv.get("voided"):
                return ("voided",)
            if mv.get("void_of"):
                return ("reversal",)
            return ()

        self.groups.render(items, values_of, tags_of)
        voided = sum(1 for mv in items if mv.get("voided"))
        total = sum(int(mv.get("qty") or 0) for mv in items if not mv.get("voided"))
        label = dict(self.ACTIONS)[self.action]
        hidden = (f"另有 {idle} 个项目还没有{'入库' if self.action == 'IN' else '出库'}"
                  f"记录,没列进下拉。" if idle else "")
        # 摘要里报一句分了多少组:分组之后一进来只看得到几条组行,不说明的话
        # 会有人以为「我的流水怎么只剩这么几条了」
        self.summary.set(
            f"{label} {len(items)} 条" + (f"(其中已撤销 {voided} 条)" if voided else "")
            + f",按项目分 {self.groups.group_count()} 组,有效合计 {total} 个。"
            + "点项目那一行可以展开 / 收起(默认收着)。"
            + hidden
            + "　这一页只查账 —— 真正的入库/出库在「项目 BOM」页里做,"
              + "盘点/移库在「流水」页.")

    @staticmethod
    def _proj_name(p):
        return p.get("name") or f"项目 {p['id']}"

    def undo(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "先点一行要撤销的记录。", parent=self)
            return
        iid = str(sel[0])
        if self.groups.is_group(iid):
            # 组行是「哪个项目」那一层,不是任何一笔账。拿它去撤销只会补出一笔
            # 莫名其妙的流水,所以挡住并说清下一步怎么做。
            messagebox.showinfo("提示", "这是一行项目分组,不是某一笔流水。\n"
                                        "点开它,选中下面具体的那一笔再撤销。",
                                parent=self)
            return
        vals = self.tree.item(iid, "values")
        label = (f"{vals[col_index(self.COLS, 'kind')]}  "
                 f"{vals[col_index(self.COLS, 'component_name')]}  "
                 f"{vals[col_index(self.COLS, 'qty')]} 个  "
                 f"{vals[col_index(self.COLS, 'location_code')]}\n"
                 f"{vals[col_index(self.COLS, 'created_at')]}")
        note = vals[col_index(self.COLS, "note")]
        if note:
            label += f"\n{note}"
        undo_movement(self, self.app, int(iid), label)


# --------------------------------------------------------------------- 项目 BOM

class LineDetail(ttk.Frame):
    """BOM 明细右边那块详情面板:点一行就看它。

    为什么是一块常驻面板,而不是再开一个弹窗:BOM 明细本身就是「一行一行核对」的
    界面,核对时反复要看的正是「这颗料库里到底是什么、还剩多少」。弹窗会盖住那张表,
    关掉又得重新找回到哪一行了 —— 常驻面板才对得上这个动作。

    品类做成**可编辑**下拉(不是只读),因为品类本来就靠人判断:光耦、传感器模块
    这类东西推不出来,只能自己填。只给一个固定清单的话,用户只能挑一个最接近的
    凑合,等于把「它猜错了」换成「我被迫选了个不准确的」。
    """

    #: 需求那一段的字段:界面标签 → build_report() 里 line 的键
    NEED_FIELDS = [
        ("单块用量", "per_board"), ("损耗", "attrition"), ("固定损耗", "setup_qty"),
        ("需求", "need"), ("已发料", "placed_qty"), ("已入库", "received_qty"),
        # 不叫「还需要」:这个数减掉的是「已发料 + 已入库」两本账,叫「还需要」
        # 会让人以为只扣了发出去的那部分(#31)。收料面板上那一列也叫「还没动」
        ("还没动", "remaining"), ("现有", "on_hand"), ("替代", "sub_qty"),
        ("缺口", "gap"),
    ]

    def __init__(self, parent, app: App):
        super().__init__(parent, padding=(8, 0, 0, 0))
        self.app = app
        self.con = app.con
        self.line = None
        self.comp = None
        self._cats = []
        self._cat_by_id = {}     # 品类 id -> 显示用的那一行(全路径)
        self._cat_map = {}       # 显示用的那一行 -> 品类 id
        self._picked_id = None   # 逐级选择器选中了树上哪个节点
        self._picked_label = ""  # 上面那个节点显示成什么(用来判断用户改没改)

        req = ttk.LabelFrame(self, text="这条需求", padding=5)
        req.pack(fill="x")
        # 一行两对,不是三对 —— 三对时这个框请求 248px,是整个右侧最宽的一个,
        # 会被 Panedwindow 拿去当最小宽度,把左边的项目列表一起挤窄。
        # 一行两对只请求约 174px,十个字段排五行,反而更好读。
        self.v = {}
        for i, (label, key) in enumerate(self.NEED_FIELDS):
            row, col = divmod(i, 2)
            ttk.Label(req, text=label, style="Dim.TLabel").grid(
                row=row, column=col * 2, sticky="e", padx=(0, 3), pady=1)
            var = tk.StringVar(value="—")
            self.v[key] = var
            ttk.Label(req, textvariable=var).grid(
                row=row, column=col * 2 + 1, sticky="w", padx=(0, 14), pady=1)

        box = ttk.LabelFrame(self, text="这颗元件", padding=5)
        box.pack(fill="x", pady=(8, 0))
        self.v2 = {}
        rows = [("值", "value"), ("封装", "package"), ("商品编号", "lcsc_pn"),
                ("厂家料号", "mpn"),
                # 用户自定义的属性(耐压 / 精度 / 功率 …)。收料清单那一列只放得下
                # 值,完整的名=值在这里看 —— 属性填了总得有地方看全。
                ("属性", "params"),
                ("现有库存", "on_hand_total"), ("所在仓位", "locs")]
        for i, (label, key) in enumerate(rows):
            ttk.Label(box, text=label, style="Dim.TLabel").grid(
                row=i, column=0, sticky="e", padx=(0, 4), pady=1)
            var = tk.StringVar(value="—")
            self.v2[key] = var
            # wraplength 直接决定这块面板的请求宽度 —— 它就是被 Panedwindow
            # 拿去算最小宽度的那一项,调大会把左边两张表一起挤窄
            ttk.Label(box, textvariable=var, wraplength=132, justify="left").grid(
                row=i, column=1, sticky="w", pady=1)

        # 品类:一级一级选出来的,不再是一个写着全路径的长下拉 ——
        # 路径一长,框里只看得见结尾,根本不知道选的是哪一支。
        # 认不出来的品类也不必担心:选择器里每一级都能「＋ 新建…」。
        r = len(rows)
        ttk.Label(box, text="品类", style="Dim.TLabel").grid(
            row=r, column=0, sticky="e", padx=(0, 4), pady=(4, 1))
        self.cat = tk.StringVar()
        self.cb_cat = ttk.Entry(box, textvariable=self.cat, width=17, state="readonly")
        self.cb_cat.grid(row=r, column=1, sticky="w", pady=(4, 1))
        ttk.Button(box, text="换…", width=6, command=self.pick_category).grid(
            row=r + 1, column=1, sticky="w", pady=(3, 0))
        box.columnconfigure(1, weight=1)

        self.tip = tk.StringVar()
        ttk.Label(self, textvariable=self.tip, style="Dim.TLabel",
                  wraplength=170, justify="left").pack(fill="x", pady=(6, 0))
        self.clear()

    # ------------------------------------------------------------ 取值

    def category_options(self):
        """品类候选 = 内置清单 + 库里已经在用的 + **树上每个节点的全路径**。

        最后一项是给子类用的:只列顶层名的话,用户自己加过的「无极性陶瓷电容」
        在这一栏里根本看不见,只能靠记忆打一个一字不差的名字。

        故意不缓存:品类是随时会加的,缓存住的话刚加的子类要重启才出现。
        这是本地进程内的一次查询(不走网络),值得。
        """
        meta = call(self.con, server.meta, quiet=True) or {}
        opts = list(meta.get("categories") or [])
        self._cat_by_id = {}
        for n in meta.get("category_paths") or []:
            # 顶层显示光名字(和以前一样,免得下拉里一堆「电阻 / ...」),
            # 子类显示全路径 —— 同名子类挂在不同大类下时,只有路径分得清
            label = n["path"] if n.get("parent_id") else n["name"]
            self._cat_by_id[n["id"]] = label
            if label not in opts:
                opts.append(label)
        self._cat_map = {v: k for k, v in self._cat_by_id.items()}
        self._cats = opts
        return self._cats

    def clear(self):
        self.line = None
        self.comp = None
        for var in self.v.values():
            var.set("—")
        for var in self.v2.values():
            var.set("—")
        self.cat.set("")
        self._picked_id = None
        self._picked_label = ""
        self.tip.set("先在左边点一行物料,这里就显示它的属性。")

    def show(self, line):
        """把一行物料摊开在这块面板上。line 就是 project_bom 报告里的一行。"""
        if not line:
            self.clear()
            return
        self.line = line
        for key, var in self.v.items():
            raw = line.get(key)
            if key == "attrition":
                var.set(f"{float(raw or 0):g}%")
            elif raw is None or raw == "":
                var.set("—")
            else:
                var.set(str(raw))
        # 这颗料从库里取全 —— 报告里只有「这个项目要用的那几个数」,
        # 库存分布、料号这些得看元件本身
        cid = line.get("component_id")
        comp = call(self.con, server.get_component, match=(str(cid),), quiet=True) if cid else None
        self.comp = comp or {}
        for key in ("value", "package", "lcsc_pn", "mpn"):
            self.v2[key].set(str(self.comp.get(key) or "—"))
        self.v2["params"].set(attrs.fmt_full(self.comp.get("params")) or "—")
        stock = self.comp.get("stock_by_location") or []
        unit = self.comp.get("unit") or "个"
        total = sum(int(s.get("qty") or 0) for s in stock)
        self.v2["on_hand_total"].set(f"{total} {unit}" if total else f"0 {unit}")
        self.v2["locs"].set("  ".join(f"{s.get('code')}:{s.get('qty')}" for s in stock)
                            or "库里一颗都没有")
        # 候选现查(品类随时会加,缓存住的话刚加的子类要重启才出现)
        self.category_options()
        # 挂在子类下的元件要显示全路径,不能只显示大类名 ——
        # 否则这块面板看着像「它就在大类下」,和库存菜单里看到的不是一回事
        cur_id = self.comp.get("category_id")
        self._picked_id = cur_id if cur_id in self._cat_by_id else None
        self._picked_label = self._cat_by_id.get(cur_id, "") if self._picked_id else ""
        self.cat.set(self._picked_label or str(self.comp.get("category") or ""))
        self.tip.set("品类点「换…」一级一级改。")

    # ------------------------------------------------------------ 存

    def pick_category(self):
        """逐级选品类。选完立刻生效,不用再点一次保存。"""
        if not self.comp:
            messagebox.showinfo("提示", "先在左边点一行物料。", parent=self)
            return
        dlg = CategoryPickerDialog(self, self.app, self.comp.get("category_id"))
        self.wait_window(dlg)
        if not dlg.result:
            return
        cid, path = dlg.result
        if cid is None:
            return
        self._picked_id = cid
        self._picked_label = path
        self.cat.set(path)
        self.save_category()

    def save_category(self):
        if not self.comp:
            messagebox.showinfo("提示", "先在左边点一行物料。", parent=self)
            return
        want = (self.cat.get() or "").strip()
        if not want:
            messagebox.showinfo(
                "提示", "品类不能是空的。\n认不出来的话,填一个最接近的也比空着强 —— "
                        "空着以后搜不到它。", parent=self)
            return
        cur_label = (self._cat_by_id.get(self.comp.get("category_id"))
                     or str(self.comp.get("category") or ""))
        if want == cur_label:
            self.app.set_status("品类没变,不用保存")
            return
        body = {"category": want}
        cat_id = self._picked_id if (want and want == self._picked_label) else None
        if cat_id is None:
            cat_id = self._cat_map.get(want)
        if cat_id:
            # 选的是子类就挂到子类上,不是挂到同名的大类上
            body["category_id"] = cat_id
            # category 这一列永远写「顶层大类名」,和 category_id 指向的一支一致 ——
            # 两列不许互相矛盾,按品类分组的 SQL 全靠文本那一列
            body["category"] = want.split(" / ")[0] if " / " in want else want
        res = call(self.con, server.update_component,
                   match=(str(self.comp["id"]),), body=body, parent=self)
        if res is None:
            return
        old = self.comp.get("category") or "未分类"
        self.comp["category"] = body["category"]
        if self.line is not None:
            self.line["category"] = body["category"]
        self.app.set_status(
            f"「{self.comp.get('name') or ''}」的品类:{old} → {want}", 6)
        # 品类会出现在 BOM 明细、入库清单、出库分配树上,三处都得跟着走
        self.app.refresh_all()


# 「标记」列里给用户看的那两个字,做成常量:自检要按它断言,免得两边各写一份字面量
GUESS_FLAG = "待核"


def bom_guess(line: dict) -> tuple[bool, str]:
    """**事后**判断一条 BOM 需求的品类是不是「猜的」。返回 (要不要核, 一句话依据)。

    背景(issue #37):导入不再拦一道「核对品类」了,那「哪几行是猜的」就得换个
    地方说 —— 落库之后再让人一眼看到。可 BOM 解析出来的 `category_confidence` /
    `category_reason` **不落库**:`project_bom` 只存 component_id / required_qty /
    designators,品类记在 component 上。所以导入之后界面手上只剩「位号 / 值 /
    封装 / 库里存着的品类」,只能拿它们重跑一遍 `bom.classify`。

    两条判据,任意一条成立就值得人自己看一眼:

      * `confidence` 是 low / none —— 线索本身定不下来(认不出,或者互相打架);
      * 重算出来的品类**和库里存的不一样** —— 这行的品类跟它自己的位号/值/封装
        对不上,该核。少了这一条会漏掉一类行:hint 那一票在导入时可能把结论
        从 high 拉到 low,重算没那一票又会算回 high(见下面「局限」)。

    ------------------------------------------------------------------ 局限
    重算**只能传 `hint=""`**。`classify` 的第 4 个参数 hint 来自 BOM 文件自己的
    「品类 / 分类 / 类别」列(见 `bom.HEADER_ALIASES`),而**那一列不落库**,事后
    拿不回来。所以对「自带品类列的 BOM」,这里的判决可能和导入那一刻不一致,
    两个方向都会发生:

      * 这一行**只**靠品类列站住(位号/值/封装全无线索):导入时 high,重算
        none —— **多标**一次「待核」。这行本来也没别的证据,核一下不亏。
      * 品类列和位号/值打架、导入时已判 low:重算少了品类列那一票,可能反倒
        算成 high。靠上面第二条判据兜:结论变了,就说明这行的品类**只**在那一
        列上有支撑,同样该看。真正会漏的是「hint 和别的线索打架、重算却算出
        **同一个**品类」那种行 —— 概率小,但不是零。

    还有一件事得说清:这个标记**不是**「导入时算错了」的意思,而是「这一行的
    品类没法用行内线索确认,值得你自己看一眼」。所以「复核品类…」里人亲手改过
    的行也可能照样被标上 —— 库里只存品类值,没存「这是人定的」,而人改的往往
    正是线索本来就定不下来的那些行(那正是人去改它的原因)。多标不丢数据,
    比漏标好。

    一句话:这是个**提示**,不是结论。宁可多标几行,也不装作知道。
    """
    stored = str(line.get("category") or "").strip()
    designators = bom.split_designators(str(line.get("designators") or ""))
    cat, conf, why = bom.classify(designators, line.get("package") or "",
                                  line.get("value") or "")
    if conf in (bom.CONF_LOW, bom.CONF_NONE):
        return True, why
    if stored and stored != cat:
        return True, (f"{why}；但库里这行的品类是「{stored}」 —— 导入时 BOM 自带"
                      f"的那一列品类没落库,重算少了它这一票,所以对不上")
    return False, ""


class ProjectsTab(ttk.Frame):
    def __init__(self, parent, app: App):
        super().__init__(parent, padding=8)
        self.app = app
        self.con = app.con
        self._pid = None
        self._lines = {}
        self._guess = {}          # bom_id -> 品类是不是猜的(见 bom_guess)
        self.report = {}

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Button(bar, text="📥 导入 BOM (.xlsx / .csv)",
                   command=self.import_bom).pack(side="left")
        ttk.Button(bar, text="计划数量…", command=self.set_qty).pack(side="left", padx=6)
        ttk.Button(bar, text="按 BOM 领料", command=self.pick).pack(side="left")
        ttk.Button(bar, text="缺料转采购", command=self.to_purchase).pack(side="left", padx=6)
        ttk.Button(bar, text="导出缺料 CSV", command=self.export_shortage).pack(side="left")
        ttk.Button(bar, text="新建空项目", command=self.new_project).pack(side="left", padx=6)
        ttk.Button(bar, text="删除项目", command=self.delete_project).pack(side="left")
        ttk.Button(bar, text="刷新", command=self.reload).pack(side="left", padx=6)

        pane = ttk.Panedwindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True)

        left = ttk.LabelFrame(pane, text="项目(「能造」= 在最缺的那一行上能装出几块)", padding=6)
        # 左右分栏的列宽要一起算:两边的列宽之和必须塞得进默认窗口,
        # 否则一打开就要横向拖动。build\check_layout.py 会盯着这件事。
        f, self.t_proj = make_tree(left, [
            ("name", "项目", 150, "w", True),
            ("qty", "计划", 45, "e"),
            ("can_build", "能造", 45, "e"),
            ("bom_lines", "料号", 45, "e"),
            ("shortage_lines", "缺料行", 52, "e"),
            ("state", "状态", 55, "center")], height=22)
        f.pack(fill="both", expand=True)
        self.t_proj.tag_configure("ok", foreground="#1e8449")
        self.t_proj.tag_configure("bad", foreground="#c0392b")
        self.t_proj.bind("<<TreeviewSelect>>", self._on_pick_project)
        pane.add(left, weight=1)

        right = ttk.Frame(pane)
        head = ttk.Frame(right)
        head.pack(fill="x", pady=(0, 4))
        self.title = tk.StringVar(value="（左侧选一个项目）")
        ttk.Label(head, textvariable=self.title, style="Big.TLabel").pack(side="left")
        self.shortage = tk.StringVar()
        self.lbl_shortage = ttk.Label(head, textvariable=self.shortage, foreground="#c0392b")
        self.lbl_shortage.pack(side="right")

        # 「这张 BOM 还缺什么」和「给它收料 / 发料」是同一件事的前后两步,
        # 放在一个项目的三个子页签里,不用在页面之间来回跳。
        self.sub = ttk.Notebook(right)
        self.sub.pack(fill="both", expand=True)

        detail = ttk.Frame(self.sub, padding=6)
        tools = ttk.Frame(detail)
        tools.pack(fill="x", pady=(0, 4))
        ttk.Button(tools, text="编辑选中行…", command=self.edit_line).pack(side="left")
        ttk.Button(tools, text="替代料…", command=self.substitutes).pack(side="left", padx=6)
        ttk.Button(tools, text="添加料行…", command=self.add_line).pack(side="left")
        ttk.Button(tools, text="移除选中行", command=self.remove_line).pack(side="left", padx=6)
        # BOM 上这一行和库里那颗料的写法对不上时用这个 ——
        # 导出的 BOM 常常只剩值和封装,严格相等是找不到的
        ttk.Button(tools, text="找相似库存…", command=self.similar_for_line).pack(side="left")
        # 导入之后**主动**想连品类一起过一遍才走这个(issue #37)。它不再是导入的
        # 必经步骤:主路(📥 导入 BOM)选完文件就落库,复核窗口只在这里出现。
        ttk.Button(tools, text="复核品类…", command=self.review_categories).pack(
            side="left", padx=6)
        ttk.Label(tools, text="双击一行可直接改用量/损耗/可选/免点。",
                  style="Dim.TLabel").pack(side="left", padx=8)

        # 树 + 右侧详情面板。分栏只加在这一个子页签内部,三个子页签的整体结构不动 ——
        # 那三页的可用宽度是逐个量过的(check_layout.py),整体改结构会把它们挤坏
        split = ttk.Panedwindow(detail, orient="horizontal")
        split.pack(fill="both", expand=True)

        bom_side = ttk.Frame(split)
        # 列宽合计 640:这一页右边多了详情面板,原本 842 的账放不下了。
        # 让出来的是「名称」和「位号」这两个拉伸列 —— 它们本来就会吃掉剩余空间,
        # 收窄它们的基准宽度不损失信息;详情面板里能看到完整的名字。
        # issue #37 之后「标记」列多担了一件事:品类是猜的行要在这里写「待核」。
        # 40px 装不下(「可选 替代2 待核」会互相挤掉),所以加宽到 56,这 16px 从
        # 「位号」出 —— 它还是拉伸列,1360 宽的窗口下拿到的是「基准宽 + 余量」,
        # 基准小一点只是余量多一点,而且合计仍然是 640(check_layout.py 盯着这个数)。
        f2, self.t_bom = make_tree(bom_side, [
            ("name", "名称", 104, "w", True),
            ("lcsc_pn", "商品编号", 62, "center"),
            ("value", "值", 50, "w"),
            ("package", "封装", 58, "w"),
            ("per_board", "单块", 36, "e"),
            ("attrition", "损耗", 36, "e"),
            ("need", "需求", 40, "e"),
            ("on_hand", "现有", 38, "e"),
            ("sub_qty", "替代", 36, "e"),
            ("gap", "缺口", 40, "e"),
            ("flag", "标记", 56, "center"),
            ("designators", "位号", 84, "w", True)], height=16)
        f2.pack(fill="both", expand=True)
        self.t_bom.tag_configure("short", background="#ffe3e3")
        self.t_bom.tag_configure("done", foreground="#888")
        self.t_bom.bind("<Double-1>", lambda _e: self.edit_line())
        # 单击就出详情。BOM 明细的动作本来就是「一行一行看过去」,
        # 要求双击才给看等于把最常做的事藏起来
        self.t_bom.bind("<<TreeviewSelect>>", self._on_bom_select)
        split.add(bom_side, weight=3)

        self.detail = LineDetail(split, app)
        split.add(self.detail, weight=2)
        self.sub.add(detail, text="  BOM 明细  ")

        self.pane_in = MovePane(self.sub, app, "IN")
        self.pane_out = MovePane(self.sub, app, "OUT")
        self.sub.add(self.pane_in, text="  元件入库  ")
        self.sub.add(self.pane_out, text="  元件出库  ")
        self.sub.bind("<<NotebookTabChanged>>", self._on_sub_change)
        pane.add(right, weight=3)

    def reload(self):
        data = call(self.con, server.list_projects, quiet=True)
        if data is None:
            return
        clear_tree(self.t_proj)
        for p in data["items"]:
            ok = p["ready"]
            self.t_proj.insert("", "end", iid=str(p["id"]), values=(
                p.get("name") or "", p.get("qty") or 1, p.get("can_build") or 0,
                p.get("bom_lines") or 0, p.get("shortage_lines") or "",
                "料齐" if ok else "缺料"), tags=("ok" if ok else "bad",))
        if self._pid and self._has_project(str(self._pid)):
            self.t_proj.selection_set(str(self._pid))
        elif self.t_proj.get_children():
            self.t_proj.selection_set(self.t_proj.get_children()[0])
            self._on_pick_project()
        else:
            # 一个项目都没有:两个方向的开单区要明确说「先在左边选一个项目」,
            # 而不是留着上一次的项目 id 继续往旧项目里记账。
            # 只把 _pid 置空是不够的 —— 见 _clear_bom()。
            self._pid = None
            self._clear_bom()
        # 会走到这里,往往是「刚开完单 / 刚撤销了一笔」—— 开单表上的「现有」
        # 和记录表都得跟着走。不能指望选中事件:选中的项目没变时它不发。
        self._sync_panes(force=True)

    def _clear_bom(self):
        """把「当前项目」这一摊显示全部清空。

        删掉最后一个项目之后走这里。数据库那一层是干净的:`project_bom` 声明了
        `ON DELETE CASCADE`,`db.connect()` 里也有 `PRAGMA foreign_keys = ON`,
        实测删完项目 `project_bom` 一行不剩。残留全在界面上 ——

        `t_bom` 里的行只在 `load_bom()` 里被 `clear_tree` 掉,而 `load_bom()`
        第一句就是 `if not self._pid: return`:没有项目的时候它压根不会被调用,
        于是表格、标题、缺料标签、详情面板会一起停在已删项目的内容上。
        用户看到的就是「项目删了,BOM 明细没删」。
        """
        clear_tree(self.t_bom)
        self._lines = {}
        self._guess = {}
        self.report = {}
        self.title.set("（左侧选一个项目）")
        self.shortage.set("")
        self.lbl_shortage.configure(foreground="#c0392b")
        self.detail.clear()

    def _has_project(self, iid):
        try:
            self.t_proj.item(iid)
            return True
        except tk.TclError:
            return False

    def _on_sub_change(self, _event=None):
        """切子页签:把「要显示出来」的那一页刷一遍。

        为什么必须刷:入库和出库看的是同一份库存。在「元件入库」里收完货
        切到「元件出库」,如果这张表还是切走之前的数,刚收的那颗料就不会
        出现在候选里 —— 用户会以为「我明明收了,怎么找不到」。

        只刷这一张:另一张是藏着的,刷了也没人看,而它上面可能挂着人勾了
        一半的分配。刷不会把手填的数抹掉 —— 保命措施在各自 reload() 里,
        见那两处的注释。
        """
        self._sync_panes()
        try:
            cur = self.sub.index(self.sub.select())
        except Exception:  # noqa: BLE001
            return
        for p in (getattr(self, "pane_in", None), getattr(self, "pane_out", None)):
            if p is not None and self.sub.index(p) == cur:
                p.refresh_panes()

    def _on_pick_project(self, _event=None):
        sel = self.t_proj.selection()
        if not sel:
            return
        self._pid = int(sel[0])
        self.load_bom()
        self._sync_panes()

    def _on_bom_select(self, _event=None):
        """BOM 明细里换了一行 → 右边详情跟着换。"""
        sel = self.t_bom.selection()
        if not sel:
            self.detail.clear()
            return
        self.detail.show(self._lines.get(int(sel[0])))
        # 「标记」列只有两个字,依据得说出来 —— 只说「待核」等于让人重头猜一遍
        why = self._guess.get(int(sel[0]))
        if why:
            self.app.set_status(f"这一行的品类是猜的:{why}", 8)

    def similar_for_line(self):
        """拿 BOM 明细里选中那一行的值+封装,去库存里找相似。"""
        sel = self.t_bom.selection()
        if not sel:
            messagebox.showinfo("提示", "先在 BOM 明细里选一行。", parent=self)
            return
        bid = int(sel[0])          # BOM 明细的 iid 就是 bom_id
        vals = self.t_bom.item(sel[0], "values")
        # 列序:名称 / 商品编号 / 值 / 封装 / 单块 / 损耗 / 需求 / 现有 / 替代 / 缺口 / 标记 / 位号
        value, package = str(vals[2] or ""), str(vals[3] or "")
        if not (value or package):
            messagebox.showinfo(
                "提示", "这一行既没有值也没有封装,没有能比对的东西。\n"
                        "可以先在「编辑选中行…」里补上。", parent=self)
            return
        # bom_id 一起带进回调:挑中的料要挂到**这一条**需求上,
        # 只传元件号的话,挂到哪条需求就成了猜的
        SimilarDialog(self, self.app, value=value, package=package,
                      on_pick=lambda cid: self._pick_similar_from_line(bid, cid),
                      in_stock_only=True)

    def _pick_similar_from_line(self, bid, cid):
        """挑中之后切到「元件出库」,并优先把「这条需求用这颗料顶」勾上。

        找相似的目的十有八九就是**这条 BOM 需求拿这颗料凑**,所以直接挂到
        那条需求的分配树上、勾好、按缺口把数量填好。让用户再回一张平表里
        自己找一遍,等于把他刚做完的判断丢掉。

        挂不上(这颗料不是这条需求的候选,比如它现在根本没有库存)就退回
        「自由出库」把它选中。两条路都走不通时必须说话 —— 悄悄切个页什么都
        不做,用户只会以为按钮坏了。
        """
        if not self._pid:
            return
        self.sub.select(self.pane_out)
        self.app.update_idletasks()
        po = self.pane_out
        po.show_bom()
        if po.bom_form.check_for(bid, cid):
            self.app.set_status("已挂到这条 BOM 需求上,确认出库就行")
            return
        po.show_free()
        if po.form.select_by_id(cid):
            self.app.set_status("已选中,填数量就能出库")
            return
        # 找相似时如果没勾「只看有库存的」,挑中的料可能现在就是没库存,
        # 而出库列表默认不列没库存的
        messagebox.showinfo(
            "提示",
            "这颗料现在没有库存,出库列表里默认不列它。\n"
            "先在「元件入库」里把它收进来,或者取消勾选「只列有库存的」。",
            parent=self)

    def _sync_panes(self, force=False):
        """把「现在选的是哪个项目」同步给两个方向的开单区。

        没有项目就没有上下文:入库/出库都要记在项目名下,否则「这批料是为谁收的」
        就丢了。项目页自己的子页签在没选项目时会写明「先在左边选一个项目」。

        force=True 表示「数据变了,按当前项目重刷一遍」。撤销、开单之后必须
        走这条:set_project 对同一个项目是幂等的,光靠它刷不到记录表。
        """
        for p in (getattr(self, "pane_in", None), getattr(self, "pane_out", None)):
            if p is not None:
                # 先按「现在到底选着哪个项目」对齐,再决定要不要重刷,顺序不能反。
                # refresh_panes() 是拿 pane **自己记着的** project_id 去重刷的,
                # 项目被删掉之后那个 id 已经陈旧 —— 只调它会把一个不存在的项目
                # 又刷回屏幕上,收料清单和分配树因此一直留着旧内容。
                # set_project 对同一个项目是幂等的(见 MovePane.set_project),
                # 项目没变时它直接返回,所以这条路径不会白重建一遍开单表。
                p.set_project(self._pid)
                if force:
                    p.refresh_panes()

    def load_bom(self):
        if not self._pid:
            return
        # 重画之前先记住选中的是哪条需求 —— 品类一改就会全量刷新,
        # 不记住的话面板会跳回「先在左边点一行物料」,人要重新找一遍刚才那行
        keep = None
        sel = self.t_bom.selection()
        if sel and str(sel[0]).isdigit():
            keep = int(sel[0])
        rep = call(self.con, server.project_bom, match=(self._pid,), quiet=True)
        clear_tree(self.t_bom)
        self._lines = {}
        self._guess = {}
        self.report = rep or {}
        if not rep:
            self.detail.clear()
            return
        proj = rep.get("project") or {}
        self.title.set(f"{proj.get('name') or ''}   计划 {rep['boards']} 块   "
                       f"能造 {rep['can_build']} 块")
        for line in rep.get("lines") or []:
            self._lines[line["bom_id"]] = line
            flags = []
            # 品类是猜的,就在「标记」列写「待核」—— 导入不再拦一道复核了(issue
            # #37),「哪几行得自己看一眼」只能落在表里。放在最前面:这一列很窄,
            # 后面还可能跟着「可选 / 免点 / 替代N」,最要紧的一条得先看见。
            need_check, why = bom_guess(line)
            if need_check:
                self._guess[line["bom_id"]] = why
                flags.append(GUESS_FLAG)
            if line.get("optional"):
                flags.append("可选")
            if line.get("consumable"):
                flags.append("免点")
            if line["substitutes"]:
                flags.append(f"替代{len(line['substitutes'])}")
            self.t_bom.insert("", "end", iid=str(line["bom_id"]), values=(
                line.get("name") or "", line.get("lcsc_pn") or "",
                line.get("value") or "", line.get("package") or "",
                line.get("per_board") or 0,
                f"{line['attrition']:g}%" if line.get("attrition") else "",
                line.get("need") or 0, line.get("on_hand") or 0,
                line.get("sub_qty") or "", line.get("gap") or "",
                " ".join(flags), line.get("designators") or ""),
                tags=("short",) if line.get("gap") else ("done",))

        # 把选中和详情面板接回来
        if keep is not None and self.t_bom.exists(str(keep)):
            self.t_bom.selection_set(str(keep))
            self.detail.show(self._lines.get(keep))
        else:
            self.detail.clear()

        parts = []
        if rep["shortage_lines"]:
            parts.append(f"缺料 {rep['shortage_lines']} 种 / {rep['shortage_qty']} 个")
        if rep.get("optional_missing"):
            parts.append(f"另有 {rep['optional_missing']} 个可选件缺")
        if rep["shortage_value"]:
            parts.append(f"补料约 {rep['shortage_value']:.2f} 元")
        self.shortage.set("   ".join(parts) if parts else "✓ 料齐,可以开工")
        self.lbl_shortage.configure(foreground="#c0392b" if parts else "#27ae60")

    # ------------------------------------------------------ BOM 行操作

    def _sel_line(self):
        sel = self.t_bom.selection()
        if not sel:
            messagebox.showinfo("提示", "先在右边选一行料。", parent=self)
            return None
        return self._lines.get(int(sel[0]))

    def edit_line(self):
        line = self._sel_line()
        if not line:
            return
        dlg = BomLineDialog(self, self.app, line)
        self.wait_window(dlg)
        if dlg.done:
            self.app.refresh_all()

    def substitutes(self):
        line = self._sel_line()
        if not line:
            return
        dlg = SubstituteDialog(self, self.app, line)
        self.wait_window(dlg)
        if dlg.done:
            self.app.refresh_all()

    def add_line(self):
        if not self._pid:
            messagebox.showinfo("提示", "先选一个项目。", parent=self)
            return
        picker = ComponentPicker(self, self.app, "选一个元件加进 BOM")
        self.wait_window(picker)
        if not picker.result:
            return
        text = ask_text(self, "添加料行", "单块用量:", "1")
        if not text:
            return
        try:
            qty = int(text)
        except ValueError:
            messagebox.showinfo("提示", "用量要填整数。", parent=self)
            return
        if call(self.con, server.add_bom_line,
                body={"component_id": picker.result, "required_qty": qty},
                match=(self._pid,), parent=self):
            self.app.refresh_all()

    def remove_line(self):
        line = self._sel_line()
        if not line:
            return
        if not messagebox.askyesno("移除料行",
                                   f"把「{line['name']}」从这个项目的 BOM 里去掉?", parent=self):
            return
        if call(self.con, server.delete_bom_line, match=(line["bom_id"],), parent=self):
            self.app.refresh_all()

    def set_qty(self):
        if not self._pid:
            messagebox.showinfo("提示", "先选一个项目。", parent=self)
            return
        cur = (self.report or {}).get("boards") or 1
        text = ask_text(self, "计划数量", "这个项目计划做几块?", str(cur))
        if text is None:
            return
        try:
            qty = int(text)
        except ValueError:
            messagebox.showinfo("提示", "要填整数。", parent=self)
            return
        if qty < 1:
            messagebox.showinfo("提示", "至少做 1 块。", parent=self)
            return
        if call(self.con, server.update_project, body={"qty": qty},
                match=(self._pid,), parent=self):
            self.app.refresh_all()

    def to_purchase(self):
        rep = self.report or {}
        if not self._pid or not rep.get("lines"):
            messagebox.showinfo("提示", "先选一个有 BOM 的项目。", parent=self)
            return
        rows = [l for l in rep["lines"] if l["to_order"] > 0]
        if not rows:
            messagebox.showinfo("提示", "这个项目不缺料。", parent=self)
            return
        if not messagebox.askyesno(
                "缺料转采购",
                f"把 {len(rows)} 项缺料加进采购单吗?\n\n"
                "只加**还差的部分**:已经在途的不会再加一遍。\n"
                "会先记成「想买」,下过单之后到采购页标成「已下单」。", parent=self):
            return
        items = [{"component_id": l["component_id"], "qty": l["to_order"],
                  "project_id": self._pid} for l in rows]
        if call(self.con, server.create_purchase,
                body={"items": items, "status": "todo"}, parent=self):
            self.app.refresh_all()
            self.app.goto_tab("purchase")

    # ------------------------------------------------------ 动作

    def import_bom(self):
        """选一个 BOM 文件,**直接导进去** —— 不再拦一道「核对品类」。

        issue #37,用户原话:「导入 BOM 不要显示什么需要确认的这种东西,用户会
        自己确认一遍,你的算法不一定准确。」

        从前这里是:先 POST /api/bom/preview,再弹一个模态的 BomReviewDialog,
        不点「确认导入」就一个字节都不落库。品类推断只是**辅助**,那个窗口却把
        它变成了**门禁** —— 后端从来没这个要求(`bom_import` 的 categories 是
        可选参数),网页版(/api/bom/import)也一直是直接导。所以现在:

          * 项目名默认取文件名(和网页版一致),**不额外问一次名字**;
          * 落库、切到新项目、报结果,由 _do_import 一手做完;
          * 「哪几行品类是猜的」改成**事后**在 BOM 明细的「标记」列写「待核」
            (见 bom_guess / load_bom),人自己回头核;
          * 想连品类一起过一遍的,用明细工具栏的「复核品类…」—— 那是用户主动
            要的,不再是必经之路。
        """
        path = filedialog.askopenfilename(
            parent=self, title="选择 BOM 文件",
            filetypes=[("BOM 文件", "*.xlsx *.xlsm *.csv *.tsv"),
                       ("Excel 工作簿", "*.xlsx *.xlsm"),
                       ("CSV(KiCad / EasyEDA / 立创)", "*.csv *.tsv"),
                       ("全部文件", "*.*")])
        if not path:
            return
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as exc:
            messagebox.showerror("读不了文件", str(exc), parent=self)
            return

        upload = {"filename": os.path.basename(path), "data": data}
        name = os.path.splitext(os.path.basename(path))[0]
        self._do_import(upload, name)

    def review_categories(self):
        """导入**之后**主动复核品类:选文件 → 看推断 → 改完按改的整份重导。

        这就是原来那条「导入前核对」的路,只是从**必经**改成**按需**:复核窗口
        还在(BomReviewDialog),但只有用户点了这个按钮才会出现。

        为什么要重选文件:落库走的是 bom_import(它按文件重新解析、整份覆盖同名
        项目),项目自己并不留 BOM 文件路径,所以复核必须拿到源文件。复核的是
        **当前项目**:项目名默认填当前项目名,改完的品类跟着文件一起重导,覆盖的
        就是这个项目(replace_existing 默认开),`self._pid` 自然还是它。

        项目名空着时 BomReviewDialog 会挡住(它自己校验),这里不用重复。
        """
        if not self._pid:
            messagebox.showinfo("提示", "先在左边选一个要复核的项目。", parent=self)
            return
        path = filedialog.askopenfilename(
            parent=self, title="选择这个项目对应的 BOM(复核完按你改的品类整份重导)",
            filetypes=[("BOM 文件", "*.xlsx *.xlsm *.csv *.tsv"),
                       ("全部文件", "*.*")])
        if not path:
            return
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as exc:
            messagebox.showerror("读不了文件", str(exc), parent=self)
            return

        upload = {"filename": os.path.basename(path), "data": data}
        preview = call(self.con, server.bom_preview, upload=upload, parent=self)
        if not preview:
            return
        # 默认顶上当前项目名,复核完覆盖的就是它。用户想改名也可以直接改这个框。
        default_name = (self.report.get("project") or {}).get("name") or \
            os.path.splitext(os.path.basename(path))[0]
        BomReviewDialog(self, self.app, preview, default_name=default_name,
                        on_confirm=lambda name, cats: self._do_import(
                            upload, name, cats))

    def _do_import(self, upload, name, categories=None):
        """把文件真的导进去,然后把结果**说清楚**。

        categories 是「复核品类…」带回来的人工结果({行号: 品类});从
        「📥 导入 BOM」直接进来时是 None,品类全走推断。
        """
        rep = call(self.con, server.bom_import,
                   body={"project_name": name, "categories": categories},
                   upload=upload, parent=self)
        if rep is None:
            return
        warn = rep.get("warnings") or []
        # 报告里的字段名是 bom_lines;这里曾经写成 line_count,导入成功后必然 KeyError。
        # 用 get 兜一下,免得以后再改字段名又炸一次。
        lines = rep.get("bom_lines", rep.get("line_count", 0))
        # 先把「切到新项目」做完(SET self._pid + 全量刷新),再弹完成提示:
        #   * 提示是模态的,弹在前面等于让人对着旧表点确定;
        #   * load_bom() 顺手算出了「哪几行品类是猜的」(self._guess),那个条数
        #     要写进提示里,先刷新就不用为它再查一次后端。
        self._pid = rep["project_id"]
        self.app.refresh_all()
        guess = len(self._guess)
        msg = (f"{lines} 行已导入,请核对\n"
               f"项目:{rep['project_name']}\n"
               f"新建/复用元件 {rep.get('components_created', 0)} 个\n"
               f"总需求 {rep.get('total_qty', 0)}\n"
               f"警告 {len(warn)} 条")
        if categories:
            msg += f"\n人工改过品类的 {len(categories)} 行已按你改的落库"
        if guess:
            msg += (f"\n其中 {guess} 行的品类是**猜的**:明细表「标记」列写了"
                    f"「{GUESS_FLAG}」,点中那一行右边会说推断依据,品类可以直接改")
        if warn:
            msg += "\n\n" + "\n".join(f"· {w}" for w in warn[:10])
        messagebox.showinfo("导入完成", msg, parent=self)

    def new_project(self):
        name = ask_text(self, "新建项目", "项目名称:", "")
        if not name:
            return
        res = call(self.con, server.create_project, body={"name": name}, parent=self)
        if res:
            self._pid = res["id"]
            self.app.refresh_all()

    def delete_project(self):
        if not self._pid:
            return
        name = self.title.get() or f"#{self._pid}"
        if not messagebox.askyesno("删除项目",
                                   f"确定删除项目「{name}」吗?\n\nBOM 一起删除,库存流水保留。",
                                   parent=self):
            return
        if call(self.con, server.delete_project, match=(self._pid,), parent=self):
            self._pid = None
            self.app.refresh_all()

    def pick(self):
        if not self._pid:
            messagebox.showinfo("提示", "请先选一个项目。", parent=self)
            return
        if not self.t_bom.get_children():
            messagebox.showinfo("提示", "这个项目还没有 BOM。", parent=self)
            return
        if not messagebox.askyesno(
                "按 BOM 领料",
                "将对整个 BOM 批量出库(只领还缺的部分)。\n"
                "库存不足的料号会被跳过并列明原因,不会中断其他料号。\n\n继续吗?", parent=self):
            return
        rep = call(self.con, server.pick_for_project, match=(self._pid,),
                   body={}, parent=self)
        if rep is None:
            return
        failed = rep.get("failed") or []
        msg = f"成功出库 {len(rep.get('picked') or [])} 个料号。"
        if failed:
            msg += f"\n\n以下 {len(failed)} 个没领成:\n" + "\n".join(
                f"· 元件 {f['component_id']} 需 {f['qty']}:{f['reason']}" for f in failed[:12])
        messagebox.showinfo("领料结果", msg, parent=self)
        self.app.refresh_all()

    def export_shortage(self):
        if not self._pid or not self._lines:
            messagebox.showinfo("提示", "先选一个有 BOM 的项目。", parent=self)
            return
        rows = [l for l in self._lines.values() if l["gap"] > 0]
        if not rows:
            messagebox.showinfo("提示", "这个项目没有缺料。", parent=self)
            return
        path = filedialog.asksaveasfilename(
            parent=self, title="导出缺料清单", defaultextension=".csv",
            initialfile=f"缺料_{self.title.get()}.csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        cols = [("name", "名称"), ("lcsc_pn", "商品编号"), ("mpn", "厂家料号"),
                ("manufacturer", "厂家"), ("package", "封装"), ("value", "值"),
                ("required_qty", "需求"), ("placed_qty", "已领"),
                # 已入库和已发料分开两列:导出去对账的人一眼看得出这条需求
                # 是「收进来了」还是「已经发给板子了」(#31)
                ("received_qty", "已入库"), ("on_hand", "库存"),
                ("gap", "缺口"), ("designators", "位号")]
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow([c[1] for c in cols])
                for r in rows:
                    w.writerow([r.get(c[0], "") for c in cols])
        except OSError as exc:
            messagebox.showerror("导出失败", str(exc), parent=self)
            return
        messagebox.showinfo("导出完成", f"{len(rows)} 行缺料:\n{path}", parent=self)


# --------------------------------------------------------------------- 流水

def undo_movement(parent, app: App, mid: int, label=None) -> bool:
    """撤销一笔流水。

    撤销是这里唯一一个「会改动已经记下的历史」的操作,所以先把这一笔原样摆出来
    问一遍 —— 不看清楚就点确定,正是最容易出事的地方。

    实现上是补一笔反向流水、把原记录标成「已撤销」,不删记录:账本必须还能按
    流水重建,删了就查不出这一笔到底怎么了。
    """
    if not messagebox.askyesno(
            "撤销这一次操作",
            f"{label or ('流水 #' + str(mid))}\n\n"
            "撤销不会删掉记录,而是补一笔反向流水,原记录标成「已撤销」。\n"
            "确定要撤销吗?", parent=parent):
        return False
    res = call(app.con, server.void_movement, match=(str(mid),), parent=parent)
    if res is None:
        return False
    app.set_status(f"已撤销 #{mid};「{res['name']}」现在 {res['on_hand']} 个", 8)
    app.refresh_all()
    return True


class MovementsTab(ttk.Frame):
    """流水 —— 所有动作的账本,按项目折叠(#30)。

    原本这里把所有流水按时间倒序平铺成一张长表:不同项目的料混在一起,
    项目一多就完全看不出谁是谁的。现在每个项目一个可折叠的组行,
    组行上写着项目名和笔数,**默认全收着** —— 用户原话「不然看着太乱了,
    一直展开在那边」。
    """

    # 列定义:(键, 标题, 宽度[, 是否随窗口伸缩])。对齐一律居中(#27)。
    COLS = [
        ("created_at", "时间", 145),
        ("kind", "动作", 100),
        ("component_name", "元件", 175),
        ("lcsc_pn", "商品编号", 88),
        ("qty", "数量", 55),
        ("location_code", "仓位", 90),
        ("to_location_code", "移到", 90),
        ("operator", "操作人", 80),
        ("note", "备注", 200, True),
    ]

    def __init__(self, parent, app: App):
        super().__init__(parent, padding=8)
        self.app = app
        self.con = app.con

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Label(bar, text="动作").pack(side="left", padx=(0, 4))
        self.kind = tk.StringVar(value="全部")
        cb = ttk.Combobox(bar, textvariable=self.kind, width=8, state="readonly",
                          values=["全部"] + list(KIND_LABEL.values()))
        cb.pack(side="left", padx=(0, 10))
        cb.bind("<<ComboboxSelected>>", lambda _e: self.reload())

        ttk.Label(bar, text="条数").pack(side="left", padx=(0, 4))
        self.limit = tk.StringVar(value="300")
        ttk.Combobox(bar, textvariable=self.limit, width=6, state="readonly",
                     values=["100", "300", "1000", "5000"]).pack(side="left", padx=(0, 10))
        ttk.Button(bar, text="刷新", command=self.reload).pack(side="left")
        # 折叠是这次新加的,界面上得有一句话告诉用户「组行是可以点的」——
        # 光靠一个 ▶ 小三角,不一定有人会去点它
        ttk.Label(bar, text="点项目那一行可以展开 / 收起(默认收着)",
                  style="Dim.TLabel").pack(side="left", padx=10)
        ttk.Button(bar, text="↶ 撤销选中的记录", command=self.undo).pack(side="right")

        f, self.tree = make_tree(self, self.COLS, height=22, show="tree headings")
        # #0 树列放项目名。原来那列 120px 的「项目」撤掉了:分组之后项目名就是
        # 组行的名字,每一行再重复一遍既占宽度又吵(和「出入库」页一个道理)。
        self.tree.heading("#0", text="项目", anchor="center")
        self.tree.column("#0", width=150, anchor="center", stretch=False)
        f.pack(fill="both", expand=True)
        # 撤销过的记录淡掉但不隐藏 —— 历史要看得见,只是别再当它是有效的
        self.tree.tag_configure("voided", foreground="#95a5a6")
        self.tree.tag_configure("reversal", foreground="#2471a3")
        self.groups = MovementGroups(self.tree, self.COLS)

    def reload(self):
        query = {"limit": self.limit.get()}
        label = self.kind.get()
        if label != "全部":
            query["kind"] = {v: k for k, v in KIND_LABEL.items()}[label]
        data = call(self.con, server.list_movements, query=query, quiet=True)
        if data is None:
            return

        def values_of(mv):
            kind_txt = KIND_LABEL.get(mv.get("kind"), mv.get("kind"))
            if mv.get("voided"):
                kind_txt += "(已撤销)"
            elif mv.get("void_of"):
                kind_txt += "·撤销"
            return ((mv.get("created_at") or "")[:19], kind_txt,
                    mv.get("component_name") or "", mv.get("lcsc_pn") or "",
                    mv.get("qty") or 0, mv.get("location_code") or "",
                    mv.get("to_location_code") or "", mv.get("operator") or "",
                    mv.get("note") or "")

        def tags_of(mv):
            if mv.get("voided"):
                return ("voided",)
            if mv.get("void_of"):
                return ("reversal",)
            return ()

        self.groups.render(data["items"], values_of, tags_of)

    def undo(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "先点一行要撤销的记录。", parent=self)
            return
        iid = str(sel[0])
        if self.groups.is_group(iid):
            # 组行不是任何一笔账 —— 撤销是这套东西里唯一会改动已记下历史的操作,
            # 更不能让它落在一个「一行文字」上
            messagebox.showinfo("提示", "这是一行项目分组,不是某一笔流水。\n"
                                        "点开它,选中下面具体的那一笔再撤销。",
                                parent=self)
            return
        vals = self.tree.item(iid, "values")
        label = (f"{vals[col_index(self.COLS, 'kind')]}  "
                 f"{vals[col_index(self.COLS, 'component_name')]}  "
                 f"{vals[col_index(self.COLS, 'qty')]} 个  "
                 f"{vals[col_index(self.COLS, 'location_code')]}\n"
                 f"{vals[col_index(self.COLS, 'created_at')]}")
        note = vals[col_index(self.COLS, "note")]
        if note:
            label += f"\n{note}"
        undo_movement(self, self.app, int(iid), label)


# --------------------------------------------------------------------- 仓位

class LocationsTab(ttk.Frame):
    """仓位:柜 → 层 → 格 的层级结构。

    真实的料柜就是这个形状,平铺一层根本表达不了「A柜第 2 层左边那格」。
    标成「分层」的仓位只用来分层、自己不装东西(比如「A柜」和「02层」),
    末端的格子才装料。选中任意一层会把下面所有子仓位的料一起统计出来。
    """

    def __init__(self, parent, app: App):
        super().__init__(parent, padding=8)
        self.app = app
        self.con = app.con
        self._sid = None
        self._rows = {}

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Button(bar, text="＋ 新建顶层仓位", command=self.add_top).pack(side="left")
        ttk.Button(bar, text="＋ 加子仓位", command=self.add_child).pack(side="left", padx=6)
        ttk.Button(bar, text="编辑…", command=self.edit).pack(side="left")
        ttk.Button(bar, text="删除", command=self.delete).pack(side="left", padx=6)
        # 实物清点:拿着一箱料挨个核对。放在这里是因为这事总是「一个抽屉一个抽屉」地做,
        # 而不是「一个元件一个元件」地做。
        ttk.Button(bar, text="📋 盘点这个仓位…",
                   command=self.stocktake).pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="刷新", command=self.reload).pack(side="left")
        ttk.Label(bar, text="入/出库时写一个不存在的仓位编码,会自动建一个顶层仓位。",
                  style="Dim.TLabel").pack(side="left", padx=12)

        pane = ttk.Panedwindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True)

        left = ttk.LabelFrame(pane, text="仓位结构", padding=6)
        frame = ttk.Frame(left)
        cols = (("kinds", "存放元件", 80), ("qty", "数量", 70),
                ("structural", "类型", 60), ("note", "备注", 150))
        # 这一处必须用 show="tree headings"(要 #0 树列来显示层级),
        # 而 make_tree 是按平表写的,所以这里自己搭。
        tree = ttk.Treeview(frame, columns=[c[0] for c in cols], height=22,
                            show="tree headings")
        tree.heading("#0", text="仓位", anchor="center")
        tree.column("#0", width=200, anchor="center", stretch=False)
        for key, title, width in cols:
            # 对齐一律居中,见 make_tree 上面那段说明(#27)
            tree.heading(key, text=title, anchor="center")
            tree.column(key, width=width, anchor="center", stretch=(key == "note"))
        vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        frame.pack(fill="both", expand=True)
        self.tree = tree
        self.tree.bind("<<TreeviewSelect>>", self._on_pick)
        pane.add(left, weight=2)

        right = ttk.Frame(pane)
        head = ttk.Frame(right)
        head.pack(fill="x")
        self.title = tk.StringVar(value="（左侧选一个仓位）")
        ttk.Label(head, textvariable=self.title, style="Big.TLabel").pack(side="left")
        self.summary = tk.StringVar()
        ttk.Label(right, textvariable=self.summary, foreground="#666").pack(anchor="w", pady=(2, 6))
        f2, self.t_contents = make_tree(right, [
            ("name", "名称", 180, "w", True),
            ("lcsc_pn", "商品编号", 90, "center"),
            ("value", "值", 75, "w"),
            ("package", "封装", 95, "w"),
            ("qty", "数量", 60, "e"),
            ("location_code", "实际仓位", 120, "w"),
        ], height=20)
        f2.pack(fill="both", expand=True)
        pane.add(right, weight=3)

    # ------------------------------------------------------ 数据

    def reload(self):
        data = call(self.con, server.list_locations, quiet=True)
        if data is None:
            return
        items = data["items"]
        self._rows = {i["id"]: i for i in items}
        kids = {}
        for it in items:
            kids.setdefault(it["parent_id"], []).append(it)
        for group in kids.values():
            group.sort(key=lambda x: x["code"])

        clear_tree(self.tree)
        # 递归插:按 parent_id 归组,天然就是树
        def walk(parent_id, iid):
            for it in kids.get(parent_id, []):
                self.tree.insert(iid, "end", iid=str(it["id"]),
                                 text=it["name"] or it["code"],
                                 values=(it["kinds"] or "", it["qty"] or "",
                                         "分层" if it["structural"] else "存货",
                                         it["note"] or ""),
                                 open=True)
                walk(it["id"], str(it["id"]))

        walk(None, "")
        if self._sid is not None and self._has(str(self._sid)):
            self.tree.selection_set(str(self._sid))
        elif self.tree.get_children(""):
            self.tree.selection_set(self.tree.get_children("")[0])
            self._on_pick()

    def _has(self, iid):
        try:
            self.tree.item(iid)
            return True
        except tk.TclError:
            return False

    def _on_pick(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        self._sid = int(sel[0])
        row = self._rows.get(self._sid) or {}
        self.title.set(row.get("path") or "")
        # 选中分层仓位时级联统计,把子仓位里的东西一起算进来
        rep = call(self.con, server.location_contents,
                   query={"cascade": "1" if row.get("structural") else ""},
                   match=(self._sid,), quiet=True)
        clear_tree(self.t_contents)
        if not rep:
            return
        for it in rep["items"]:
            self.t_contents.insert("", "end", values=(
                it["name"], it["lcsc_pn"] or "", it["value"] or "", it["package"] or "",
                it["qty"], it["location_code"]))
        scope = "含子仓位" if row.get("structural") else "仅本仓位"
        self.summary.set(f"{scope}:{rep['kinds']} 种 / {rep['total_qty']} 个"
                         + (f"   估值 {rep['value']:.2f} 元" if rep["value"] else ""))

    # ------------------------------------------------------ 动作

    def add_top(self):
        self._new(parent_id=None)

    def add_child(self):
        if self._sid is None:
            messagebox.showinfo("提示", "先在左边选一个仓位。", parent=self)
            return
        self._new(parent_id=self._sid)

    def _new(self, parent_id):
        dlg = LocationDialog(self, self.app, None, parent_id=parent_id)
        self.wait_window(dlg)
        if dlg.done:
            self.app.refresh_all()

    def edit(self):
        if self._sid is None:
            return
        dlg = LocationDialog(self, self.app, self._sid)
        self.wait_window(dlg)
        if dlg.done:
            self.app.refresh_all()

    def stocktake(self):
        if self._sid is None:
            messagebox.showinfo("提示", "先在左边选一个要清的仓位。", parent=self)
            return
        dlg = StocktakeDialog(self, self.app, self._sid)
        self.wait_window(dlg)
        if dlg.done:
            self.app.refresh_all()

    def delete(self):
        if self._sid is None:
            return
        name = self.title.get() or f"#{self._sid}"
        if not messagebox.askyesno("删除仓位", f"确定删除「{name}」吗?", parent=self):
            return
        if call(self.con, server.delete_location, match=(self._sid,), parent=self):
            self._sid = None
            self.app.refresh_all()


class LocationDialog(tk.Toplevel):
    """新建 / 编辑仓位。可以指定上级仓位,以及是否「只用来分层」。"""

    TOP = "(顶层)"

    def __init__(self, parent, app: App, lid, parent_id=None):
        super().__init__(parent)
        self.app = app
        self.con = app.con
        self.lid = lid
        self.done = False

        existing = {}
        if lid:
            row = self.con.execute("SELECT * FROM location WHERE id=?", (lid,)).fetchone()
            if row:
                existing = db.row_to_dict(row)
        if parent_id is None:
            parent_id = existing.get("parent_id")

        self.title("编辑仓位" if lid else "新建仓位")
        self.transient(parent)
        self.resizable(False, False)
        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)

        meta = call(self.con, server.meta, quiet=True) or {}
        # 不能把自己当自己的上级,所以先剔除自己
        self._loc_map = {l["path"]: l["id"] for l in (meta.get("locations") or [])
                         if l["id"] != lid}
        cur = self.TOP
        if parent_id:
            for path, i in self._loc_map.items():
                if i == parent_id:
                    cur = path
                    break

        self.v_code = tk.StringVar(value=str(existing.get("code") or ""))
        self.v_name = tk.StringVar(value=str(existing.get("name") or ""))
        self.v_parent = tk.StringVar(value=cur)
        self.v_struct = tk.BooleanVar(value=bool(existing.get("structural")))
        self.v_note = tk.StringVar(value=str(existing.get("note") or ""))

        rows = [("仓位编码 *", self.v_code, None),
                ("显示名称", self.v_name, None),
                ("上级仓位", self.v_parent, [self.TOP] + sorted(self._loc_map))]
        for i, (label, var, values) in enumerate(rows):
            ttk.Label(body, text=label).grid(row=i, column=0, sticky="e", padx=(0, 8), pady=4)
            if values is not None:
                w = ttk.Combobox(body, textvariable=var, width=30, values=values)
                w.state(["readonly"])
            else:
                w = ttk.Entry(body, textvariable=var, width=32)
            w.grid(row=i, column=1, sticky="w", pady=4)

        ttk.Checkbutton(body, text="只用来分层(自己不装东西,例如「A柜」「02层」)",
                        variable=self.v_struct).grid(row=3, column=1, sticky="w", pady=4)
        ttk.Label(body, text="备注").grid(row=4, column=0, sticky="e", padx=(0, 8), pady=4)
        ttk.Entry(body, textvariable=self.v_note, width=32).grid(row=4, column=1, sticky="w", pady=4)
        ttk.Label(body, text="编码要唯一;建议用 A-01-02 这种能看出层级的写法。",
                  style="Dim.TLabel").grid(row=5, column=1, sticky="w")

        btns = ttk.Frame(body)
        btns.grid(row=6, column=0, columnspan=2, pady=(10, 0))
        ttk.Button(btns, text="保存", command=self.save, width=12).grid(row=0, column=0, padx=4)
        ttk.Button(btns, text="取消", command=self.destroy, width=12).grid(row=0, column=1, padx=4)
        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()
        self.v_code.set(self.v_code.get())
        self.focus_set()

    def save(self):
        code = self.v_code.get().strip()
        if not code:
            messagebox.showinfo("提示", "仓位编码必填。", parent=self)
            return
        path = self.v_parent.get()
        body = {"code": code, "name": self.v_name.get().strip() or code,
                "structural": self.v_struct.get(), "note": self.v_note.get().strip(),
                "parent_id": self._loc_map.get(path)}
        if self.lid:
            res = call(self.con, server.update_location, body=body, match=(self.lid,), parent=self)
        else:
            res = call(self.con, server.create_location, body=body, parent=self)
        if res is None:
            return
        self.done = True
        self.destroy()


# --------------------------------------------------------------------- 盘点


class StocktakeDialog(tk.Toplevel):
    """盘点一个仓位 —— 拿着一箱料挨个核对。

    要点是**用键盘走一遍**:输入实数、回车,自动跳到下一个还没数的,
    全程不用碰鼠标。所以焦点一直留在下面那个输入框,而不是表上。

    对得上的行不写流水(否则流水会被几百条「没变」淹掉,真出事时反而查不出来),
    只有差异才记账。「账面有但现在根本没数到」也算差异 —— 只要那一行你填了 0。
    """

    def __init__(self, parent, app, location_id):
        super().__init__(parent)
        self.app = app
        self.con = app.con
        self.lid = location_id
        self.done = False
        self.counts = {}
        self._by_id = {}

        data = call(self.con, server.location_contents, match=(location_id,),
                    parent=self) or {}
        self.loc = data.get("location") or {}
        self.items = data.get("items") or []
        self._by_id = {int(i["id"]): i for i in self.items}

        if self.loc.get("structural"):
            messagebox.showinfo(
                "提示",
                f"「{self.loc.get('code')}」是分层仓位,本身不装东西,没有实物可数。\n"
                f"请盘点它下面的具体仓位。", parent=parent)
            self.destroy()
            return

        self.title("盘点仓位")
        self.transient(parent)
        self.geometry("860x580")
        body = ttk.Frame(self, padding=10)
        body.pack(fill="both", expand=True)

        head = ttk.Frame(body)
        head.pack(fill="x")
        ttk.Label(head, text=f"盘点「{data.get('path') or self.loc.get('code') or ''}」",
                  style="Big.TLabel").pack(side="left")
        self.summary = tk.StringVar()
        ttk.Label(head, textvariable=self.summary, style="Dim.TLabel").pack(side="left",
                                                                           padx=12)
        ttk.Label(head, text="只把和账面不一样的行写成盘点流水",
                  style="Dim.TLabel").pack(side="right")

        frame, self.tree = make_tree(body, [
            ("name", "名称", 205, "w", True),
            ("lcsc_pn", "商品编号", 92, "center"),
            ("value", "值", 68, "w"),
            ("package", "封装", 82, "w"),
            ("was", "账面", 58, "e"),
            ("now", "实盘", 58, "e"),
            ("diff", "差", 58, "e")], height=15)
        frame.pack(fill="both", expand=True, pady=(8, 6))
        self.tree.tag_configure("diff", background="#fdecea", foreground="#a93226")
        self.tree.tag_configure("same", foreground="#7f8c8d")
        self.tree.bind("<Double-1>", lambda _e: self.ent.focus_set())

        for it in self.items:
            cid = int(it["id"])
            self.tree.insert("", "end", iid=str(cid), values=(
                it.get("name") or "", it.get("lcsc_pn") or "", it.get("value") or "",
                it.get("package") or "", int(it["qty"]), "", ""))
        for it in self.items:
            self._render_row(int(it["id"]))

        foot = ttk.Frame(body)
        foot.pack(fill="x")
        ttk.Label(foot, text="现在数", style="Big.TLabel").pack(side="left", padx=(0, 6))
        self.v_qty = tk.StringVar()
        self.ent = ttk.Entry(foot, textvariable=self.v_qty, width=9,
                             font=("Microsoft YaHei UI", 13))
        self.ent.pack(side="left")
        self.ent.bind("<Return>", lambda _e: self.apply())
        self.cur = tk.StringVar()
        ttk.Label(foot, textvariable=self.cur, foreground="#1a5276").pack(side="left",
                                                                         padx=12)
        ttk.Button(foot, text="保存盘点结果", command=self.save).pack(side="right")
        ttk.Button(foot, text="全部重来", command=self.reset).pack(side="right", padx=6)

        self.hint = tk.StringVar()
        ttk.Label(body, textvariable=self.hint, foreground="#b9770e").pack(anchor="w",
                                                                          pady=(6, 0))
        ttk.Label(body, text="填实数,回车 → 自动跳到下一个;数不到就填 0。"
                             "没数的行保持原样不动。",
                  style="Dim.TLabel").pack(anchor="w")

        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()
        kids = self.tree.get_children()
        if kids:
            self.tree.selection_set(kids[0])
        self.ent.focus_set()
        self._summary()
        self._show_current()

    # ---------------------------------------------------------- 内部

    def _render_row(self, cid):
        it = self._by_id[cid]
        was = int(it["qty"])
        now = self.counts.get(cid)
        if now is None:
            diff, tags = "", ()
        elif now == was:
            diff, tags = "一致", ("same",)
        else:
            diff, tags = f"{now - was:+d}", ("diff",)
        self.tree.item(str(cid), values=(
            it.get("name") or "", it.get("lcsc_pn") or "", it.get("value") or "",
            it.get("package") or "", was, "" if now is None else now, diff), tags=tags)

    def _summary(self):
        n, total = len(self.counts), len(self.items)
        diffs = sum(1 for k, v in self.counts.items() if v != int(self._by_id[k]["qty"]))
        bits = [f"已数 {n} / {total} 种"]
        if diffs:
            bits.append(f"{diffs} 处对不上")
        if n < total:
            bits.append(f"还有 {total - n} 种没数")
        self.summary.set("   ".join(bits))

    def _show_current(self):
        sel = self.tree.selection()
        if not sel:
            self.cur.set("")
            return
        it = self._by_id.get(int(sel[0]))
        if it:
            self.cur.set(f"← {it['name']}(账面 {int(it['qty'])})")

    def apply(self):
        sel = self.tree.selection()
        kids = list(self.tree.get_children())
        if not sel or not kids:
            return
        iid = sel[0]
        idx = kids.index(iid)
        raw = self.v_qty.get().strip()
        if raw != "":
            try:
                qty = int(raw)
            except ValueError:
                self.hint.set("实盘数要填整数;数不到就填 0。")
                return
            if qty < 0:
                self.hint.set("实盘数不能是负数。")
                return
            self.counts[int(iid)] = qty
            self._render_row(int(iid))
            self.hint.set("")
        self.v_qty.set("")
        # 跳到「下一个还没数的」;后面没有了就绕回前面找
        order = list(range(idx + 1, len(kids))) + list(range(0, idx))
        nxt = next((kids[j] for j in order if int(kids[j]) not in self.counts), None)
        if nxt is None:
            if len(self.counts) >= len(kids):
                self.hint.set("这一格全数完了,点「保存盘点结果」。")
            nxt = iid
        self.tree.selection_set(nxt)
        self.tree.see(nxt)
        self.ent.focus_set()
        self._summary()
        self._show_current()

    def reset(self):
        self.counts.clear()
        for cid in self._by_id:
            self._render_row(cid)
        kids = self.tree.get_children()
        if kids:
            self.tree.selection_set(kids[0])
            self.tree.see(kids[0])
        self.v_qty.set("")
        self.hint.set("")
        self._summary()
        self._show_current()
        self.ent.focus_set()

    def save(self):
        if not self.counts:
            messagebox.showinfo("提示", "还没数任何一项。", parent=self)
            return
        if len(self.counts) < len(self.items) and not messagebox.askyesno(
                "只数了一部分",
                f"这一格有 {len(self.items)} 种,你只数了 {len(self.counts)} 种。\n"
                f"没数到的保持原样不动。\n\n确定就这样保存吗?", parent=self):
            return
        items = [{"component_id": cid, "qty": q}
                 for cid, q in sorted(self.counts.items())]
        res = call(self.con, server.stocktake_location, body={"items": items},
                   match=(self.lid,), parent=self)
        if res is None:
            return
        self.done = True
        self.app.set_status(
            f"盘点完成:{res['location']} 数了 {res['checked']} 种,"
            f"{res['changed']} 处差异已记账", 6)
        msg = f"数了 {res['checked']} 种。\n\n"
        if res["changed"]:
            msg += f"有 {res['changed']} 处和账面不一样,已经写成盘点流水:\n\n"
            msg += "\n".join(f"  {d['name']}:账面 {d['was']} → 实盘 {d['now']}"
                             for d in res["diffs"][:12])
            if len(res["diffs"]) > 12:
                msg += f"\n  … 还有 {len(res['diffs']) - 12} 处"
        else:
            msg += "全部和账面一致,没有差异,所以没写流水。"
        messagebox.showinfo("盘点结果", msg, parent=self)
        self.destroy()


LINE_QTY_RE = re.compile(r"[x×*]\s*(\d+)\s*$", re.IGNORECASE)


def parse_batch_line(line):
    """把人写的一行拆成 (描述, 数量)。

    数量的写法:
      * 「10k 0603 x50」—— x / × / * 后面跟数字,最明确
      * 直接从表格里复制粘贴时(有制表符),最后一列是纯数字就当数量
    故意不把「行尾裸数字」当数量:那样「100nF 0805」会被读成数量 805。
    拿不准的一律按 1 算,反正预览表里可以改。
    """
    text = (line or "").strip()
    if not text or text.startswith("#"):
        return None
    qty = 1
    if "\t" in text:
        parts = [p.strip() for p in text.split("\t") if p.strip()]
        if len(parts) >= 2 and parts[-1].isdigit():
            qty = int(parts[-1])
            parts = parts[:-1]
        text = " ".join(parts)
    else:
        m = LINE_QTY_RE.search(text)
        if m:
            qty = int(m.group(1))
            text = text[:m.start()].strip()
    if not text:
        return None
    return text, max(qty, 1)


class BatchInDialog(tk.Toplevel):
    """批量入库 —— 收到一整箱货、或者对着采购单一次性录进来。

    流程是「先解析、再预览、最后才入库」:先把每行对到库里的一条元件上给你看,
    你确认(或者改数量、改匹配)之后再落库。批量操作最怕的就是「猜错了还悄悄
    记进去」——几十条料进错地方,事后极难发现,所以宁可多停一步。
    """

    def __init__(self, parent, app: App, kind="IN"):
        # IN = 批量入库,OUT = 批量出库(同一套粘贴框,服务端两条路都支持)
        # 必须放在最前面:下面的标题、大标题都要按它来定
        self.kind = kind
        super().__init__(parent)
        self.app = app
        self.con = app.con
        self.done = False
        self.rows = []

        self.title("批量入库 —— 粘贴一整张单子" if self.kind == "IN"
                   else "批量出库 —— 粘贴一整张单子")
        self.transient(parent)
        self.geometry("960x700")
        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text=("批量入库" if self.kind == "IN" else "批量出库"),
                  style="Big.TLabel").pack(anchor="w")
        ttk.Label(body, text="一行一个料,数量写成 x50;也可以直接从表格里"
                             "复制粘贴(最后一列是数字就当数量)。",
                  style="Dim.TLabel").pack(anchor="w", pady=(0, 6))

        box = ttk.Frame(body)
        box.pack(fill="x")
        self.txt = tk.Text(box, height=8, wrap="none",
                           font=("Consolas", 10))
        self.txt.pack(side="left", fill="x", expand=True)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        sb.pack(side="left", fill="y")
        self.txt.insert("1.0",
                        "10k 0603 x50\n"
                        "100nF 0805 x20\n"
                        "STM32F103C8T6 x2\n")

        ctl = ttk.Frame(body)
        ctl.pack(fill="x", pady=6)
        ttk.Button(ctl, text="解析预览", command=self.parse).pack(side="left")
        ttk.Label(ctl, text="   放进").pack(side="left")
        meta = call(self.con, server.meta, quiet=True) or {}
        self._loc_paths = {l["id"]: l["path"] for l in (meta.get("locations") or [])}
        self.v_loc = tk.StringVar()
        ttk.Combobox(ctl, textvariable=self.v_loc, width=20,
                     values=[l["path"] for l in (meta.get("locations") or [])]).pack(
            side="left", padx=4)
        ttk.Label(ctl, text="(留空 = 各料自己的默认仓位)", style="Dim.TLabel").pack(
            side="left")
        ttk.Button(ctl, text="清空", command=lambda: self.txt.delete("1.0", "end")).pack(
            side="right")

        f, self.tree = make_tree(body, [
            ("line", "输入", 185, "w", True),
            ("match", "匹配到", 215, "w", True),
            ("on_hand", "现有", 52, "e"),
            ("qty", "数量", 52, "e"),
            ("note", "说明", 165, "w", True)], height=12)
        f.pack(fill="both", expand=True)
        self.tree.tag_configure("new", foreground="#1e8449")
        self.tree.tag_configure("bad", background="#fdecea", foreground="#a93226")
        self.tree.tag_configure("many", background="#fef5e7", foreground="#b9770e")
        self.tree.bind("<Double-1>", lambda _e: self.pick_match())

        edit = ttk.Frame(body)
        edit.pack(fill="x", pady=(6, 0))
        ttk.Label(edit, text="改选中行的数量").pack(side="left")
        self.v_qty = tk.StringVar()
        self.ent = ttk.Entry(edit, textvariable=self.v_qty, width=8)
        self.ent.pack(side="left", padx=4)
        self.ent.bind("<Return>", lambda _e: self.set_qty())
        ttk.Button(edit, text="改", command=self.set_qty).pack(side="left")
        ttk.Button(edit, text="换一个匹配…", command=self.pick_match).pack(side="left",
                                                                          padx=8)
        ttk.Button(edit, text="全部入库", command=self.commit).pack(side="right")
        self.hint = tk.StringVar()
        ttk.Label(body, textvariable=self.hint, foreground="#b9770e").pack(anchor="w",
                                                                          pady=(6, 0))
        ttk.Label(body, text="双击一行可以手动指定它对应哪个元件。"
                             "「多项匹配」的行不会入库,除非你先指定。",
                  style="Dim.TLabel").pack(anchor="w")

        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()
        self.txt.focus_set()

    # ---------------------------------------------------------- 解析

    def parse(self):
        lines = self.txt.get("1.0", "end").splitlines()
        self.rows = []
        for raw in lines:
            parsed = parse_batch_line(raw)
            if not parsed:
                continue
            text, qty = parsed
            res = call(self.con, server.resolve_component, query={"q": text},
                       quiet=True) or {}
            self.rows.append({
                "text": text, "qty": qty,
                "match": res.get("match"), "how": res.get("how") or "none",
                "candidates": res.get("candidates") or [],
            })
        self.render()

    def render(self):
        clear_tree(self.tree)
        for i, r in enumerate(self.rows):
            m = r["match"]
            if m:
                matched = f"{m['name']}"
                note = "精确命中" if str(r["how"]).startswith("exact") else "唯一命中"
                tag = ()
            elif r["how"] == "ambiguous":
                matched = "?"
                note = f"{len(r['candidates'])} 项匹配,请手动指定"
                tag = ("many",)
            else:
                matched = f"(新建) {r['text']}"
                note = "库里没有,入库时按这个名字新建"
                tag = ("new",)
            self.tree.insert("", "end", iid=str(i), values=(
                r["text"], matched, (m or {}).get("on_hand") if m else "",
                r["qty"], note), tags=tag)
        todo = sum(1 for r in self.rows if r["match"] or r["how"] == "none")
        blocked = sum(1 for r in self.rows if r["how"] == "ambiguous")
        self.hint.set(f"解析出 {len(self.rows)} 行,可入库 {todo} 行"
                      + (f",{blocked} 行要多项匹配需要你指定" if blocked else "")
                      if self.rows else "还没有内容,先粘点东西进来再点「解析预览」。")

    # ---------------------------------------------------------- 编辑

    def _sel(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "先点一行。", parent=self)
            return None
        return self.rows[int(sel[0])]

    def set_qty(self):
        r = self._sel()
        if r is None:
            return
        try:
            qty = int(self.v_qty.get() or 0)
        except ValueError:
            messagebox.showinfo("提示", "数量要填整数。", parent=self)
            return
        if qty <= 0:
            messagebox.showinfo("提示", "数量要大于 0。", parent=self)
            return
        r["qty"] = qty
        self.v_qty.set("")
        self.render()
        self.hint.set(f"「{r['text']}」数量改成 {qty}")

    def pick_match(self):
        r = self._sel()
        if r is None:
            return
        pk = ComponentPicker(self, self.app, f"给「{r['text']}」指定元件", initial=r["text"])
        self.wait_window(pk)
        if not pk.result:
            return
        got = call(self.con, server.get_component, match=(str(pk.result),), quiet=True) or {}
        # get_component 返回的可能是 {component: …} 也可能是元件本身,两种都兜住
        comp = got.get("component") or got.get("item") or got
        if not comp or not comp.get("id"):
            comp = call(self.con, server.resolve_component,
                        query={"q": str(pk.result)}, quiet=True)
            comp = (comp or {}).get("match")
        if comp and comp.get("id"):
            r["match"] = comp
            r["how"] = "manual"
            self.render()
            self.hint.set(f"「{r['text']}」已指定为「{comp['name']}」")

    # ---------------------------------------------------------- 入库

    def commit(self):
        if not self.rows:
            messagebox.showinfo("提示", "先点「解析预览」。", parent=self)
            return
        blocked = [r for r in self.rows if not r["match"] and r["how"] == "ambiguous"]
        ready = [r for r in self.rows if r["match"] or r["how"] == "none"]
        if blocked and not messagebox.askyesno(
                "还有没指定的",
                f"有 {len(blocked)} 行是多匹配,还没指定对应哪个元件,这次会跳过。\n\n"
                + "\n".join(f"  {r['text']}({len(r['candidates'])} 项匹配)"
                            for r in blocked[:8])
                + "\n\n继续入库其余的 " + f"{len(ready)} 行吗?", parent=self):
            return
        if not ready:
            messagebox.showinfo("提示", "没有可入库的行。", parent=self)
            return

        loc = self.v_loc.get().strip()
        created = stocked = 0
        total = 0
        failed = []
        for r in ready:
            cid = (r["match"] or {}).get("id")
            if not cid:
                made = call(self.con, server.create_component,
                            body={"name": r["text"], "category": "未分类"},
                            parent=self)
                if not made:
                    failed.append(r["text"])
                    continue
                cid = made["id"]
                created += 1
            body = {"kind": self.kind, "component_id": cid, "qty": r["qty"]}
            manual_note(body)      # 手动批量:流水里标明来源
            if loc:
                body["location"] = loc
            if call(self.con, server.stock_move, body=body, parent=self) is None:
                failed.append(r["text"])
                continue
            stocked += 1
            total += r["qty"]
        self.done = True
        self.app.set_status(
            f"批量{'入库' if self.kind == 'IN' else '出库'}完成:"
            f"{stocked} 种 / {total} 个(其中新建 {created} 个元件)", 8)
        msg = (f"入库 {stocked} 种,共 {total} 个。\n"
               + (f"其中新建了 {created} 个元件(只填了名称,归到「未分类」)。\n"
                  if created else ""))
        if blocked:
            msg += f"\n跳过 {len(blocked)} 行(多匹配未指定)。\n"
        if failed:
            msg += "\n失败:" + "、".join(failed[:8])
        messagebox.showinfo("批量入库" if self.kind == "IN" else "批量出库",
                            msg, parent=self)
        self.app.refresh_all()
        if not failed:
            self.destroy()


class DedupeDialog(tk.Toplevel):
    """查重与合并 —— 收拾反复导入 BOM 长出来的重复料。

    重复料是这类工具最难躲开的数据腐烂:同一颗电阻,一期 BOM 带着商品编号,
    另一期只有值和封装,于是库里长出两条,库存还分散记在两处。它不会自己好,
    只会越来越难收拾 —— 所以得有个地方能定期扫一遍。

    合并**不删除**被并掉的那条,只标一下「并到谁那儿去了」:它名下挂着真实发生过的
    收发货流水,而流水是 ON DELETE CASCADE,直接删元件会把历史一起带走。
    标一下既能让列表干净,又保住了来龙去脉,合错了也查得回来。
    """

    def __init__(self, parent, app: App):
        super().__init__(parent)
        self.app = app
        self.con = app.con
        self.done = False
        self.groups = []

        self.title("查重与合并")
        self.transient(parent)
        self.geometry("1060x660")
        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)

        head = ttk.Frame(body)
        head.pack(fill="x")
        ttk.Label(head, text="查重与合并", style="Big.TLabel").pack(side="left")
        ttk.Button(head, text="重新扫描", command=self.scan).pack(side="right")
        ttk.Button(head, text="已合并的元件…", command=self.show_merged).pack(
            side="right", padx=6)
        ttk.Label(body, text="左边按「有多确定」排:料号相同是硬证据,"
                             "「值 + 封装相同」只是可疑,要你自己确认。",
                  style="Dim.TLabel").pack(anchor="w", pady=(0, 6))

        pan = ttk.PanedWindow(body, orient="horizontal")
        pan.pack(fill="both", expand=True)

        left = ttk.Frame(pan, padding=(0, 0, 8, 0))
        f1, self.t_groups = make_tree(left, [
            ("reason", "依据", 78, "w"),
            ("key", "相同点", 138, "w", True),
            ("n", "条数", 42, "e")], height=13)
        f1.pack(fill="both", expand=True)
        self.t_groups.bind("<<TreeviewSelect>>", lambda _e: self.show_group())
        pan.add(left, weight=1)

        right = ttk.Frame(pan)
        ttk.Label(right, text="点一行选中「要保留的那条」,再把其余的并过来").pack(anchor="w")
        f2, self.t_items = make_tree(right, [
            ("name", "名称", 165, "w", True),
            ("lcsc_pn", "商品编号", 86, "center"),
            ("mpn", "厂家料号", 108, "w"),
            ("value", "值", 60, "w"),
            ("package", "封装", 70, "w"),
            ("on_hand", "现有", 46, "e"),
            ("where", "在哪些项目里用", 120, "w", True)], height=13)
        f2.pack(fill="both", expand=True)
        self.t_items.bind("<Double-1>", lambda _e: self.merge())
        pan.add(right, weight=2)

        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(8, 0))
        self.hint = tk.StringVar()
        ttk.Label(btns, textvariable=self.hint, foreground="#b9770e").pack(side="left")
        ttk.Button(btns, text="关闭", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="把其余的并到选中的这一条", command=self.merge).pack(
            side="right", padx=6)

        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()
        self.scan()

    # ---------------------------------------------------------- 扫描

    def scan(self):
        data = call(self.con, server.component_duplicates, quiet=True) or {}
        self.groups = data.get("groups") or []
        clear_tree(self.t_groups)
        for i, g in enumerate(self.groups):
            self.t_groups.insert("", "end", iid=str(i), values=(
                REASON_LABEL.get(g["reason"], g["reason"]), g["key"], len(g["items"])))
        if self.groups:
            self.hint.set(f"发现 {len(self.groups)} 组疑似重复,"
                          f"合并后能少 {data.get('extra', 0)} 条")
            self.t_groups.selection_set("0")
            self.show_group()
        else:
            self.hint.set("没有发现重复,挺好的。")
            clear_tree(self.t_items)

    def show_group(self):
        sel = self.t_groups.selection()
        clear_tree(self.t_items)
        if not sel:
            return
        g = self.groups[int(sel[0])]
        # 每条重复料都查一下它被哪些项目用到 —— 这决定并哪一条更省事
        for it in g["items"]:
            used = [r["name"] for r in self.con.execute(
                """SELECT DISTINCT p.name FROM project_bom b
                   JOIN project p ON p.id = b.project_id
                   WHERE b.component_id=? ORDER BY p.name""", (it["id"],))]
            self.t_items.insert("", "end", iid=str(it["id"]), values=(
                it["name"], it.get("lcsc_pn") or "", it.get("mpn") or "",
                it.get("value") or "", it.get("package") or "",
                it.get("on_hand") or 0, "、".join(used) or "—"))
        first = self.t_items.get_children()
        if first:
            self.t_items.selection_set(first[0])
        self.hint.set(g["hint"])

    # ---------------------------------------------------------- 合并

    def merge(self):
        sel = self.t_items.selection()
        if not sel:
            messagebox.showinfo("提示", "先在右边点一行,选「要保留的那一条」。",
                                parent=self)
            return
        keep = int(sel[0])
        drop = [int(i) for i in self.t_items.get_children() if int(i) != keep]
        if not drop:
            messagebox.showinfo("提示", "这一组只有一条,没什么可并的。", parent=self)
            return
        kname = self.t_items.item(sel[0], "values")[0]
        names = "、".join(self.t_items.item(str(d), "values")[0] for d in drop)
        if not messagebox.askyesno(
                "合并元件",
                f"保留:「{kname}」\n并入:「{names}」\n\n"
                "会这样处理:\n"
                "  · 各仓位的库存相加到保留的那条上\n"
                "  · BOM 里指向被并那些的行改指过来(同一个项目里会并成一行)\n"
                "  · 采购单、替代料一起改指\n"
                "  · **出入库流水一条不动**(那是真实发生过的,删了账就重建不出来)\n"
                "  · 被并的那条不删,只标记「已并入」,列表里不再出现\n\n"
                "确定合并吗?", parent=self):
            return
        res = call(self.con, server.merge_components,
                   body={"keep": keep, "drop": drop}, parent=self)
        if res is None:
            return
        self.done = True
        self.app.set_status(f"已把 {len(drop)} 条重复料并入「{kname}」", 8)
        self.scan()
        self.app.refresh_all()

    def show_merged(self):
        """已经并掉的那些。留着这个清单是为了「当初并到哪儿去了」能查回来。"""
        data = call(self.con, server.merged_components, quiet=True) or {}
        items = data.get("items") or []
        if not items:
            messagebox.showinfo("已合并的元件", "还没有合并过任何元件。", parent=self)
            return
        lines = [f"  {i['name']}  →  {i['keep_name'] or '(已不存在)'}"
                 for i in items[:40]]
        more = f"\n…… 另有 {len(items) - 40} 条" if len(items) > 40 else ""
        messagebox.showinfo("已合并的元件",
                            f"共 {len(items)} 条:\n\n" + "\n".join(lines) + more,
                            parent=self)


# --------------------------------------------------------------------- 总览


class DashboardTab(ttk.Frame):
    """总览。一屏回答三个问题:我有什么、要做什么、该买什么。"""

    SPECS = [("kinds", "元件种类"), ("total_qty", "库存总数"), ("total_value", "库存估值"),
             ("buy_kinds", "该买种类"), ("buy_amount", "该买金额"), ("on_order_qty", "在途数量"),
             ("out_kinds", "缺货种类"), ("low_kinds", "低于安全库存")]

    def __init__(self, parent, app: App):
        super().__init__(parent, padding=8)
        self.app = app
        self.con = app.con

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Label(bar, text="总览", style="H1.TLabel").pack(side="left")
        ttk.Button(bar, text="刷新", command=self.reload).pack(side="right")
        ttk.Button(bar, text="去采购页 →",
                   command=lambda: self.app.goto_tab("purchase")).pack(side="right", padx=6)
        ttk.Button(bar, text="去项目页 →",
                   command=lambda: self.app.goto_tab("proj")).pack(side="right")

        cards = ttk.Frame(self)
        cards.pack(fill="x", pady=(0, 10))
        self.vals = {}
        for i, (key, label) in enumerate(self.SPECS):
            cell = tk.Frame(cards, bg=CARD_BG, highlightbackground=CARD_EDGE,
                            highlightthickness=1)
            cell.grid(row=0, column=i, padx=3, sticky="nsew")
            cards.columnconfigure(i, weight=1, uniform="stat")
            var = tk.StringVar(value="–")
            tk.Label(cell, textvariable=var, bg=CARD_BG, fg="#1f2937",
                     font=("Microsoft YaHei UI", 15, "bold")).pack(padx=10, pady=(9, 0))
            tk.Label(cell, text=label, bg=CARD_BG, fg="#98a2b3",
                     font=("Microsoft YaHei UI", 8)).pack(padx=10, pady=(0, 9))
            self.vals[key] = var

        pane = ttk.Panedwindow(self, orient="vertical")
        pane.pack(fill="both", expand=True)

        top = ttk.LabelFrame(pane, text="项目进度(能造几块由最缺的那一行决定)", padding=6)
        f1, self.t_proj = make_tree(top, [
            ("name", "项目", 210, "w", True),
            ("qty", "计划做", 70, "e"),
            ("can_build", "能造", 70, "e"),
            ("bom_lines", "料号数", 70, "e"),
            ("shortage_lines", "缺料行", 70, "e"),
            ("shortage_value", "缺料估值", 90, "e"),
            ("state", "状态", 90, "center")], height=8)
        f1.pack(fill="both", expand=True)
        self.t_proj.tag_configure("ok", foreground="#1e8449")
        self.t_proj.tag_configure("bad", foreground="#c0392b")
        self.t_proj.bind("<Double-1>", lambda _e: self.app.goto_tab("proj"))
        pane.add(top, weight=1)

        bottom = ttk.LabelFrame(pane, text="最近流水", padding=6)
        f2, self.t_recent = make_tree(bottom, [
            ("created_at", "时间", 145, "center"),
            ("kind", "动作", 60, "center"),
            ("component_name", "元件", 220, "w", True),
            ("qty", "数量", 60, "e"),
            ("location_code", "仓位", 100, "w"),
            ("project_name", "项目", 120, "w"),
            ("note", "备注", 200, "w", True)], height=10)
        f2.pack(fill="both", expand=True)
        self.t_recent.tag_configure("IN", foreground="#1e8449")
        self.t_recent.tag_configure("OUT", foreground="#c0392b")
        pane.add(bottom, weight=1)

    def reload(self):
        d = call(self.con, server.dashboard, quiet=True)
        if d is None:
            return
        for key, _label in self.SPECS:
            v = d.get(key)
            if key in ("total_value", "buy_amount"):
                self.vals[key].set(f"{float(v or 0):.2f}")
            else:
                self.vals[key].set(str(v if v is not None else 0))
        # 该买和缺货用红色标出来,一眼能看到
        for key in ("buy_kinds", "out_kinds"):
            try:
                self.vals[key].set(self.vals[key].get())
            except tk.TclError:
                pass
        clear_tree(self.t_proj)
        for p in d.get("projects") or []:
            ok = p["ready"]
            self.t_proj.insert("", "end", values=(
                p["name"], p["qty"], p["can_build"], p["bom_lines"],
                p["shortage_lines"] or "", f"{p['shortage_value']:.2f}" if p["shortage_value"] else "",
                "料齐" if ok else "缺料"), tags=("ok" if ok else "bad",))
        clear_tree(self.t_recent)
        for m in d.get("recent") or []:
            self.t_recent.insert("", "end", values=(
                m["created_at"], KIND_LABEL.get(m["kind"], m["kind"]),
                f"{m['component_name']}" + (f" ({m['value']})" if m.get("value") else ""),
                m["qty"], m["location_code"] or "", m["project_name"] or "",
                m["note"] or ""), tags=(m["kind"],))


# --------------------------------------------------------------------- 采购


class PurchaseTab(ttk.Frame):
    """采购。上面是「该买什么」(算出来的),下面是「采购单」(你真正下的单)。

    在途 = 已下单未到货。采购建议会把在途扣掉,所以同一个东西不会重复建议。
    """

    def __init__(self, parent, app: App):
        super().__init__(parent, padding=8)
        self.app = app
        self.con = app.con
        self._buy = {}

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Label(bar, text="采购", style="H1.TLabel").pack(side="left")
        ttk.Button(bar, text="刷新", command=self.reload).pack(side="right")
        ttk.Button(bar, text="一键全部加入采购单", command=self.add_all).pack(side="right", padx=6)
        ttk.Button(bar, text="把选中的加入采购单", command=self.add_selected).pack(side="right")

        pane = ttk.Panedwindow(self, orient="vertical")
        pane.pack(fill="both", expand=True)

        top = ttk.LabelFrame(
            pane, text="该买什么(= 目标库存 − 现有 − 在途;目标 = max(项目需求, 安全库存))",
            padding=6)
        head = ttk.Frame(top)
        head.pack(fill="x")
        self.buy_sum = tk.StringVar()
        ttk.Label(head, textvariable=self.buy_sum, foreground="#c0392b").pack(side="left")
        f1, self.t_buy = make_tree(top, [
            ("name", "名称", 210, "w", True),
            ("category", "品类", 80, "w"),
            ("value", "值", 75, "w"),
            ("package", "封装", 95, "w"),
            ("on_hand", "现有", 55, "e"),
            ("min_stock", "安全", 55, "e"),
            ("required", "需求", 55, "e"),
            ("on_order", "在途", 55, "e"),
            ("buy_qty", "该买", 60, "e"),
            ("unit_price", "单价", 60, "e"),
            ("amount", "金额", 70, "e"),
            ("supplier", "供应商", 90, "w"),
            ("reasons", "原因", 200, "w", True)], height=11)
        f1.pack(fill="both", expand=True)
        self.t_buy.tag_configure("urgent", background="#ffe3e3")
        pane.add(top, weight=3)

        bottom = ttk.LabelFrame(pane, text="采购单(「想买」不算在途,「已下单」才算)", padding=6)
        head2 = ttk.Frame(bottom)
        head2.pack(fill="x")
        ttk.Button(head2, text="标记为已下单", command=lambda: self.set_status("ordered")).pack(side="left")
        ttk.Button(head2, text="到货入库…", command=self.receive).pack(side="left", padx=6)
        ttk.Button(head2, text="取消订单", command=lambda: self.set_status("cancel")).pack(side="left")
        ttk.Button(head2, text="删除", command=self.delete).pack(side="left", padx=6)
        self.po_sum = tk.StringVar()
        ttk.Label(head2, textvariable=self.po_sum, style="Dim.TLabel").pack(side="right")
        f2, self.t_po = make_tree(bottom, [
            ("status_label", "状态", 70, "center"),
            ("name", "名称", 210, "w", True),
            ("value", "值", 75, "w"),
            ("qty", "订购", 60, "e"),
            ("received", "已收", 60, "e"),
            ("outstanding", "未到", 60, "e"),
            ("unit_price", "单价", 60, "e"),
            ("amount", "金额", 75, "e"),
            ("supplier", "供应商", 100, "w"),
            ("created_at", "建立时间", 140, "center"),
            ("note", "备注", 140, "w", True)], height=9)
        f2.pack(fill="both", expand=True)
        self.t_po.tag_configure("ordered", background="#e8f1ff")
        self.t_po.tag_configure("arrived", foreground="#888")
        self.t_po.tag_configure("cancel", foreground="#bbb")
        pane.add(bottom, weight=2)

    # ------------------------------------------------------ 数据

    def reload(self):
        shop = call(self.con, server.shopping_list, quiet=True)
        if shop is not None:
            clear_tree(self.t_buy)
            self._buy = {}
            for it in shop["items"]:
                self._buy[str(it["id"])] = it
                self.t_buy.insert("", "end", iid=str(it["id"]), values=(
                    it["name"], it.get("category") or "", it.get("value") or "",
                    it.get("package") or "", it.get("on_hand") or 0,
                    it.get("min_stock") or "", it.get("required") or "",
                    it.get("on_order") or "", it["buy_qty"], f"{it['unit_price']:.3f}",
                    f"{it['amount']:.2f}", it.get("supplier") or "",
                    " / ".join(it.get("reasons") or [])),
                    tags=("urgent",) if it.get("deficit") else ())
            self.buy_sum.set(
                f"{shop['total']} 种要买,共 {shop['total_qty']} 个,"
                f"约 {shop['total_amount']:.2f} 元"
                + (f"(另有 {shop['on_order_qty']} 个在途)" if shop["on_order_qty"] else ""))

        data = call(self.con, server.list_purchase, quiet=True)
        if data is None:
            return
        clear_tree(self.t_po)
        for it in data["items"]:
            self.t_po.insert("", "end", iid=str(it["id"]), values=(
                it["status_label"], it["name"], it.get("value") or "", it["qty"],
                it.get("received") or 0, it.get("outstanding") or 0,
                f"{float(it['unit_price'] or 0):.3f}", f"{float(it['amount'] or 0):.2f}",
                it.get("supplier") or "", it.get("created_at") or "",
                it.get("note") or ""), tags=(it["status"],))
        todo = [i for i in data["items"] if i["status"] == "todo"]
        ordered = [i for i in data["items"] if i["status"] == "ordered"]
        self.po_sum.set(f"想买 {len(todo)} 单 / 已下单 {len(ordered)} 单")

    # ------------------------------------------------------ 动作

    def _selected_ids(self, tree):
        return [int(i) for i in tree.selection()]

    def add_selected(self):
        ids = self._selected_ids(self.t_buy)
        if not ids:
            messagebox.showinfo("提示", "先在上面选几行要买的料。", parent=self)
            return
        items = [{"component_id": i, "qty": self._buy[str(i)]["buy_qty"]} for i in ids]
        if call(self.con, server.create_purchase,
                body={"items": items, "status": "todo"}, parent=self):
            self.app.refresh_all()
            self.app.set_status(f"已把 {len(items)} 项加入采购单(状态:想买)")

    def add_all(self):
        if not self._buy:
            messagebox.showinfo("提示", "现在没有要买的东西。", parent=self)
            return
        items = [{"component_id": int(k), "qty": v["buy_qty"]} for k, v in self._buy.items()]
        n = len(items)
        if not messagebox.askyesno("一键采购",
                                   f"把这 {n} 项全部加入采购单吗?\n\n"
                                   "会先记成「想买」,确认下过单之后再点「标记为已下单」。",
                                   parent=self):
            return
        if call(self.con, server.create_purchase,
                body={"items": items, "status": "todo"}, parent=self):
            self.app.refresh_all()
            self.app.set_status(f"已加入 {n} 项采购单")

    def set_status(self, status):
        ids = self._selected_ids(self.t_po)
        if not ids:
            messagebox.showinfo("提示", "先在下面选采购单。", parent=self)
            return
        for pid in ids:
            if call(self.con, server.update_purchase, body={"status": status},
                    match=(pid,), parent=self, quiet=len(ids) > 1) is None:
                return
        self.app.refresh_all()
        self.app.set_status(f"{len(ids)} 单已改成「{server.PURCHASE_STATUS[status]}」")

    def receive(self):
        ids = self._selected_ids(self.t_po)
        if not ids:
            messagebox.showinfo("提示", "先在下面选采购单。", parent=self)
            return
        pid = ids[0]
        dlg = ReceiveDialog(self, self.app, pid)
        self.wait_window(dlg)
        if dlg.done:
            self.app.refresh_all()

    def delete(self):
        ids = self._selected_ids(self.t_po)
        if not ids:
            return
        if not messagebox.askyesno("删除采购单", f"确定删除这 {len(ids)} 条采购单吗?", parent=self):
            return
        for pid in ids:
            call(self.con, server.delete_purchase, match=(pid,), parent=self, quiet=True)
        self.app.refresh_all()


class ReceiveDialog(tk.Toplevel):
    """到货入库。可以只收一部分,剩下的继续算在途。"""

    def __init__(self, parent, app: App, pid):
        super().__init__(parent)
        self.app = app
        self.con = app.con
        self.pid = pid
        self.done = False

        row = self.con.execute(
            """SELECT pu.*, c.name, c.value, c.package, c.unit,
                      (pu.qty - pu.received) AS outstanding
                 FROM purchase pu JOIN component c ON c.id = pu.component_id
                WHERE pu.id = ?""", (pid,)).fetchone()
        self.row = db.row_to_dict(row) if row else {}
        left = int(self.row.get("outstanding") or 0)

        self.title("到货入库")
        self.transient(parent)
        self.resizable(False, False)
        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)

        desc = (f"{self.row.get('name') or ''}"
                + (f"  ({self.row.get('value')})" if self.row.get("value") else ""))
        ttk.Label(body, text=desc, style="Big.TLabel").pack(anchor="w")
        ttk.Label(body, text=f"订购 {self.row.get('qty')}   已收 {self.row.get('received')}   "
                             f"未到 {left}",
                  style="Dim.TLabel").pack(anchor="w", pady=(2, 10))

        grid = ttk.Frame(body)
        grid.pack(fill="x")
        ttk.Label(grid, text="本次到货数量").grid(row=0, column=0, sticky="e", padx=(0, 8), pady=4)
        self.v_qty = tk.StringVar(value=str(left))
        ttk.Entry(grid, textvariable=self.v_qty, width=16).grid(row=0, column=1, sticky="w", pady=4)

        ttk.Label(grid, text="放进仓位").grid(row=1, column=0, sticky="e", padx=(0, 8), pady=4)
        meta = call(self.con, server.meta, quiet=True) or {}
        paths = [l["path"] for l in (meta.get("locations") or [])]
        self.v_loc = tk.StringVar(value="")
        w = ttk.Combobox(grid, textvariable=self.v_loc, width=24, values=paths)
        w.grid(row=1, column=1, sticky="w", pady=4)
        ttk.Label(grid, text="留空就放这个元件的默认仓位(没设就用「未分类」)。",
                  style="Dim.TLabel").grid(row=2, column=1, sticky="w")

        btns = ttk.Frame(body)
        btns.pack(pady=(14, 0))
        ttk.Button(btns, text="全部入库" if left else "确认", command=lambda: self.do(left)
                   ).grid(row=0, column=0, padx=4)
        ttk.Button(btns, text="按填写数量入库", command=lambda: self.do(None)).grid(row=0, column=1, padx=4)
        ttk.Button(btns, text="取消", command=self.destroy).grid(row=0, column=2, padx=4)
        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()
        self.focus_set()

    def do(self, qty):
        body = {}
        if qty is not None:
            body["qty"] = qty
        else:
            try:
                body["qty"] = int(self.v_qty.get() or 0)
            except ValueError:
                messagebox.showinfo("提示", "数量要填整数。", parent=self)
                return
            if body["qty"] <= 0:
                messagebox.showinfo("提示", "数量要大于 0。", parent=self)
                return
        loc = self.v_loc.get().strip()
        if loc:
            body["location"] = loc
        res = call(self.con, server.receive_purchase, body=body, match=(self.pid,), parent=self)
        if res is None:
            return
        self.done = True
        msg = f"入库 {body['qty']} 个。"
        msg += f"还剩 {res['outstanding']} 个在途。" if res["outstanding"] else "这一单已全部到货。"
        self.app.set_status(msg, 6)
        self.destroy()


# --------------------------------------------------------------------- 弹窗


class BomLineDialog(tk.Toplevel):
    """改一行 BOM:单块用量、损耗率、固定损耗、可选/免点、位号、备注。

    损耗率和固定损耗看着像多余的字段,但它们决定「能造几块」算得准不准:
    贴片会有报废,试产有固定损耗,不算进去算出来的可造数会偏乐观。
    """

    def __init__(self, parent, app: App, line):
        super().__init__(parent)
        self.app = app
        self.con = app.con
        self.line = line
        self.done = False

        self.title("编辑 BOM 行")
        self.transient(parent)
        self.resizable(False, False)
        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text=line.get("name") or "", style="Big.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w")
        sub = " / ".join(x for x in (line.get("value"), line.get("package"),
                                     line.get("lcsc_pn")) if x)
        ttk.Label(body, text=sub, style="Dim.TLabel").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(0, 10))

        self.v = {
            "per_board": tk.StringVar(value=str(line.get("per_board") or 1)),
            "attrition": tk.StringVar(value=f"{line.get('attrition') or 0:g}"),
            "setup_qty": tk.StringVar(value=str(line.get("setup_qty") or 0)),
            "designators": tk.StringVar(value=line.get("designators") or ""),
            "note": tk.StringVar(value=line.get("note") or ""),
        }
        self.v_opt = tk.BooleanVar(value=bool(line.get("optional")))
        self.v_con = tk.BooleanVar(value=bool(line.get("consumable")))

        for i, (label, key) in enumerate([("单块用量", "per_board"),
                                          ("损耗率 %", "attrition"),
                                          ("固定损耗", "setup_qty"),
                                          ("位号", "designators"),
                                          ("备注", "note")]):
            ttk.Label(body, text=label).grid(row=i + 2, column=0, sticky="e", padx=(0, 8), pady=4)
            ttk.Entry(body, textvariable=self.v[key], width=32).grid(
                row=i + 2, column=1, sticky="w", pady=4)

        ttk.Checkbutton(body, text="可选件(缺了也不影响「能造几块」)",
                        variable=self.v_opt).grid(row=7, column=1, sticky="w", pady=2)
        ttk.Checkbutton(body, text="免点件(螺丝/锡这类,算需求但不卡产能)",
                        variable=self.v_con).grid(row=8, column=1, sticky="w", pady=2)
        ttk.Label(body, text="损耗和固定损耗都会算进总需求,也影响能造几块。",
                  style="Dim.TLabel").grid(row=9, column=1, sticky="w", pady=(4, 0))

        btns = ttk.Frame(body)
        btns.grid(row=10, column=0, columnspan=2, pady=(12, 0))
        ttk.Button(btns, text="保存", command=self.save, width=12).grid(row=0, column=0, padx=4)
        ttk.Button(btns, text="取消", command=self.destroy, width=12).grid(row=0, column=1, padx=4)
        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()
        self.focus_set()

    def save(self):
        try:
            per = int(self.v["per_board"].get() or 0)
            setup = int(self.v["setup_qty"].get() or 0)
            att = float(self.v["attrition"].get() or 0)
        except ValueError:
            messagebox.showinfo("提示", "用量和固定损耗填整数,损耗率填数字。", parent=self)
            return
        if per < 0 or setup < 0 or att < 0:
            messagebox.showinfo("提示", "都不能是负数。", parent=self)
            return
        body = {"required_qty": per, "setup_qty": setup, "attrition": att,
                "optional": self.v_opt.get(), "consumable": self.v_con.get(),
                "designators": self.v["designators"].get().strip(),
                "note": self.v["note"].get().strip()}
        if call(self.con, server.update_bom_line, body=body,
                match=(self.line["bom_id"],), parent=self):
            self.done = True
            self.destroy()


class SubstituteDialog(tk.Toplevel):
    """管理一行的替代料。替代料的库存会算进这一行的可用量。"""

    def __init__(self, parent, app: App, line):
        super().__init__(parent)
        self.app = app
        self.con = app.con
        self.line = line
        self.done = False

        self.title("替代料")
        self.transient(parent)
        self.geometry("640x430")
        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text=f"「{line.get('name') or ''}」可以用这些料顶替",
                  style="Big.TLabel").pack(anchor="w")
        self.sum = tk.StringVar()
        ttk.Label(body, textvariable=self.sum, style="Dim.TLabel").pack(anchor="w", pady=(2, 8))

        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(0, 6))
        ttk.Button(btns, text="＋ 添加替代料", command=self.add).pack(side="left")
        ttk.Button(btns, text="移除选中", command=self.remove).pack(side="left", padx=6)

        f, self.tree = make_tree(body, [
            ("name", "名称", 190, "w", True),
            ("lcsc_pn", "商品编号", 95, "center"),
            ("value", "值", 75, "w"),
            ("package", "封装", 95, "w"),
            ("on_hand", "现有", 60, "e")], height=12)
        f.pack(fill="both", expand=True)
        ttk.Button(body, text="关闭", command=self.destroy, width=12).pack(pady=(10, 0))

        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()
        self.reload()

    def reload(self):
        rep = call(self.con, server.list_substitutes, match=(self.line["bom_id"],), quiet=True)
        clear_tree(self.tree)
        if not rep:
            return
        for it in rep["items"]:
            self.tree.insert("", "end", iid=str(it["id"]), values=(
                it["name"], it["lcsc_pn"] or "", it["value"] or "",
                it["package"] or "", it["on_hand"]))
        self.sum.set(f"{rep['total']} 个替代料,合计现有 {rep['on_hand']} 个 —— "
                     "这些数量会算进这一行的可用量。")

    def add(self):
        picker = ComponentPicker(self, self.app, "选一个替代料")
        self.wait_window(picker)
        if not picker.result:
            return
        if call(self.con, server.add_substitute, body={"component_id": picker.result},
                match=(self.line["bom_id"],), parent=self):
            self.done = True
            self.reload()

    def remove(self):
        sel = self.tree.selection()
        if not sel:
            return
        for sid in sel:
            call(self.con, server.delete_substitute, match=(int(sid),), parent=self, quiet=True)
        self.done = True
        self.reload()


class ComponentPicker(tk.Toplevel):
    """从库里挑一个元件。搜索支持名称 / 商品编号 / 厂家料号 / 值 / 封装。"""

    def __init__(self, parent, app: App, title="选一个元件", initial=None):
        super().__init__(parent)
        self.app = app
        self.con = app.con
        self.result = None

        self.title(title)
        self.transient(parent)
        self.geometry("800x520")
        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)

        sbox = ttk.Frame(body)
        sbox.pack(fill="x", pady=(0, 6))
        ttk.Label(sbox, text="搜索").pack(side="left", padx=(0, 4))
        self.q = tk.StringVar(value=initial or "")
        ent = ttk.Entry(sbox, textvariable=self.q, width=36)
        ent.pack(side="left")
        ent.bind("<Return>", lambda _e: self.reload())
        ttk.Button(sbox, text="查", command=self.reload).pack(side="left", padx=4)

        f, self.tree = make_tree(body, [
            ("name", "名称", 210, "w", True),
            ("lcsc_pn", "商品编号", 95, "center"),
            ("value", "值", 75, "w"),
            ("package", "封装", 100, "w"),
            ("on_hand", "现有", 60, "e"),
            ("category", "品类", 85, "w")], height=13)
        f.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda _e: self.pick())

        btns = ttk.Frame(body)
        btns.pack(pady=(10, 0))
        ttk.Button(btns, text="选这个", command=self.pick, width=12).grid(row=0, column=0, padx=4)
        ttk.Button(btns, text="取消", command=self.destroy, width=12).grid(row=0, column=1, padx=4)

        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()
        ent.focus_set()
        self.reload()

    def reload(self):
        data = call(self.con, server.list_components,
                    query={"q": self.q.get().strip(), "limit": "500"}, quiet=True)
        clear_tree(self.tree)
        if not data:
            return
        for it in data["items"]:
            self.tree.insert("", "end", iid=str(it["id"]), values=(
                it["name"], it.get("lcsc_pn") or "", it.get("value") or "",
                it.get("package") or "", it.get("on_hand") or 0,
                it.get("category") or ""))

    def pick(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "先选一个元件。", parent=self)
            return
        self.result = int(sel[0])
        self.destroy()


class QuickInDialog(tk.Toplevel):
    """快速入库 —— 收货那一刻用的。

    为什么要单独做这么一个窗口:社区里弃用这类系统最常见的原因就是「每用一次料都得
    去改数据库,人成了它的奴隶」。所以录入必须发生在**刚拆开快递、袋子还在手上**的
    那十几秒里,而且步骤越少越好。

    这个窗口只问三件事:是什么、几个、放哪。
      * 搜得到 → 回车入库
      * 搜不到 → 回车当场新建一条最小记录(只填名称,类别归到「未分类」),
        别的信息以后有空再补 —— 绝不拦着你「先把东西记下来」
    入库成功后窗口不关,光标跳回搜索框,可以连着录下一袋。这就是「收货即录入」。
    """

    def __init__(self, parent, app: App, project_id=None):
        super().__init__(parent)
        self.app = app
        self.con = app.con
        self.project_id = project_id
        self.done = False
        self._hits = {}

        self.title("快速入库 —— 收货就用它")
        self.transient(parent)
        self.geometry("800x560")
        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text="快速入库", style="Big.TLabel").pack(anchor="w")
        ttk.Label(body, text="搜名称 / 商品编号 / 厂家料号 / 值 / 封装 / 丝印;"
                             "搜不到就直接回车新建。",
                  style="Dim.TLabel").pack(anchor="w", pady=(0, 8))

        top = ttk.Frame(body)
        top.pack(fill="x")
        self.q = tk.StringVar()
        self.ent_q = ttk.Entry(top, textvariable=self.q, width=34,
                               font=("Microsoft YaHei UI", 12))
        self.ent_q.grid(row=0, column=0, sticky="w")
        self.ent_q.bind("<KeyRelease>", lambda _e: self.reload())
        self.ent_q.bind("<Return>", lambda _e: self.commit())
        ttk.Button(top, text="查", command=self.reload).grid(row=0, column=1, padx=4)

        ttk.Label(top, text="  数量").grid(row=0, column=2, sticky="e")
        self.v_qty = tk.StringVar(value="1")
        self.ent_qty = ttk.Entry(top, textvariable=self.v_qty, width=6)
        self.ent_qty.grid(row=0, column=3, padx=4)
        self.ent_qty.bind("<Return>", lambda _e: self.commit())

        ttk.Label(top, text="  放进").grid(row=0, column=4, sticky="e")
        meta = call(self.con, server.meta, quiet=True) or {}
        paths = [l["path"] for l in (meta.get("locations") or [])]
        self._loc_paths = {l["id"]: l["path"] for l in (meta.get("locations") or [])}
        self.v_loc = tk.StringVar()
        self.cmb_loc = ttk.Combobox(top, textvariable=self.v_loc, width=20, values=paths)
        self.cmb_loc.grid(row=0, column=5, padx=4)
        self.cmb_loc.bind("<Return>", lambda _e: self.commit())
        ttk.Label(top, text="(留空 = 元件的默认仓位,没设就用「未分类」)",
                  style="Dim.TLabel").grid(row=0, column=6, sticky="w")

        self.hint = tk.StringVar()
        ttk.Label(body, textvariable=self.hint, foreground="#b9770e").pack(
            anchor="w", pady=(6, 4))

        f, self.tree = make_tree(body, [
            ("name", "名称", 215, "w", True),
            ("lcsc_pn", "商品编号", 88, "center"),
            ("value", "值", 68, "w"),
            ("package", "封装", 88, "w"),
            ("marking", "丝印", 70, "center"),
            ("on_hand", "现有", 55, "e"),
            ("default_loc", "默认仓位", 120, "w")], height=13)
        f.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda _e: self.commit())

        btns = ttk.Frame(body)
        btns.pack(pady=(10, 0))
        ttk.Button(btns, text="入库 (回车)", command=self.commit,
                   width=16).grid(row=0, column=0, padx=4)
        ttk.Button(btns, text="关闭", command=self.destroy, width=12).grid(row=0, column=1, padx=4)

        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()
        self.ent_q.focus_set()
        self.reload()

    def reload(self):
        text = self.q.get().strip()
        data = call(self.con, server.list_components,
                    query={"q": text, "limit": "200",
                           "sort": "category" if not text else "updated"}, quiet=True)
        clear_tree(self.tree)
        self._hits = {}
        for it in (data or {}).get("items") or []:
            self._hits[str(it["id"])] = it
            loc = self._loc_paths.get(it.get("default_loc_id")) or ""
            self.tree.insert("", "end", iid=str(it["id"]), values=(
                it.get("name") or "", it.get("lcsc_pn") or "", it.get("value") or "",
                it.get("package") or "", it.get("marking") or "",
                it.get("on_hand") or 0, loc))
        kids = self.tree.get_children()
        if kids:
            self.tree.selection_set(kids[0])

        # 搜不到就说清楚「回车会新建」,免得用户以为卡住了
        if text and not kids:
            self.hint.set(f"库里没有「{text}」 —— 直接按回车,就用这个名字新建一条记录"
                          f"(品类算「未分类」,以后再补封装/编号)。")
        elif text and len(kids) == 1:
            self.hint.set(f"命中 1 条,回车入库。")
        elif text:
            self.hint.set(f"命中 {len(kids)} 条,↑↓ 选一条再回车。")
        else:
            self.hint.set("")

    def _resolve_target(self):
        """返回 (component_id, 是否新建)。"""
        sel = self.tree.selection()
        text = self.q.get().strip()
        if sel and (not text or sel[0] in self._hits):
            return int(sel[0]), False
        if not text:
            messagebox.showinfo("提示", "先搜一下,或者直接输入新元件的名称。", parent=self)
            return None, False
        return text, True

    def commit(self):
        target, is_new = self._resolve_target()
        if target is None:
            return
        try:
            qty = int(self.v_qty.get() or 0)
        except ValueError:
            messagebox.showinfo("提示", "数量要填整数。", parent=self)
            return
        if qty <= 0:
            messagebox.showinfo("提示", "数量要大于 0。", parent=self)
            return

        body = {"kind": "IN", "qty": qty}
        if is_new:
            created = call(self.con, server.create_component,
                           body={"name": target, "category": "未分类"}, parent=self)
            if not created:
                return
            body["component_id"] = created["id"]
        else:
            body["component_id"] = target
        loc = self.v_loc.get().strip()
        if loc:
            body["location"] = loc
        if self.project_id:
            body["project_id"] = self.project_id
        manual_note(body)          # 快速入库:流水里标明来源
        if call(self.con, server.stock_move, body=body, parent=self) is None:
            return

        name = target if is_new else (self._hits[str(target)].get("name") or target)
        self.done = True
        self.app.refresh_all()
        self.app.set_status(f"已入库:{name} × {qty}" + ("(新建)" if is_new else ""), 5)
        # 不收工,接着录下一袋
        self.q.set("")
        self.v_qty.set("1")
        self.reload()
        self.ent_q.focus_set()


class ComponentDialog(tk.Toplevel):
    """新增 / 编辑元件。

    安全库存、建议补货量、参考单价都不是摆设:安全库存参与「该买多少」的计算,
    单价用来估库存价值和采购金额。默认仓位让「采购到货」不用每次手选位置。
    """

    NO_LOC = "(不指定)"
    FIELDS = [
        ("name", "名称 *", None),
        ("lcsc_pn", "商品编号", None),
        ("mpn", "厂家料号", None),
        ("manufacturer", "厂家", None),
        ("category", "品类", "category"),
        ("package", "封装", None),
        ("value", "值", None),
        # 丝印/顶标。拆下来的料上只有这么几个字母,SOT-23 的丝印根本不是料号,
        # 所以这个字段必须能搜 —— 「这是什么芯片」是社区里最高频的求助。
        ("marking", "丝印 / 顶标", None),
        ("unit", "单位", None),
        ("min_stock", "安全库存", None),
        ("reorder_qty", "建议补货量(0=自动)", None),
        ("unit_price", "参考单价", None),
        ("supplier", "供应商", "supplier"),
        ("default_loc_id", "默认仓位", "loc"),
        ("datasheet_url", "数据手册链接", None),
        ("product_url", "商品链接", None),
    ]

    def __init__(self, parent, app: App, cid, cat_id=None):
        super().__init__(parent)
        self.app = app
        self.con = app.con
        self.cid = cid
        # 新建时「默认挂到我现在站的这一级」——由 ComponentsTab.add() 传进来
        self.cat_id = cat_id
        self.done = False
        self.vars = {}
        self._loc_map = {}          # 显示路径 -> location id

        self.title("编辑元件" if cid else "新增元件")
        self.transient(parent)
        self.resizable(False, False)

        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)

        existing = {}
        if cid:
            existing = call(self.con, server.get_component, match=(cid,), quiet=True) or {}

        meta = call(self.con, server.meta, quiet=True) or {}
        self._meta = meta          # 属性名候选要用(见 attr_options)
        self.cb_cat = None         # 品类下拉框,拿到引用才能挂刷新

        # 品类下拉的候选:顶层名(含内置建议) + 树上每个节点的全路径。
        # 只给顶层名的话,用户自己加过的子类在这儿是看不见的 —— 加了个寂寞。
        self._cat_by_id = {}
        for n in meta.get("category_paths") or []:
            # 顶层显示光名字(不然一屏全是「电阻 / ...」),子类显示全路径 ——
            # 同名子类挂在不同大类下时,只有路径分得清
            self._cat_by_id[n["id"]] = (n["path"] if n.get("parent_id") else n["name"])
        self._cat_map = {v: k for k, v in self._cat_by_id.items()}
        # 逐级选择器选中的是树上哪个节点。保存时优先用它 ——
        # 光靠文本认不出「同名子类」,会把料挂到错的那一支上
        self._picked_cat_id = None
        self._picked_cat_label = ""
        locs = meta.get("locations") or []
        for loc in locs:
            self._loc_map[loc["path"]] = loc["id"]
        cur_loc = self.NO_LOC
        if existing.get("default_loc_id"):
            for path, lid in self._loc_map.items():
                if lid == existing["default_loc_id"]:
                    cur_loc = path
                    break
        self._cur_loc = cur_loc

        sup = [r[0] for r in self.con.execute(
            "SELECT DISTINCT supplier FROM component WHERE supplier IS NOT NULL "
            "AND supplier <> '' ORDER BY supplier")]

        for i, (key, label, kind) in enumerate(self.FIELDS):
            ttk.Label(body, text=label).grid(row=i, column=0, sticky="e", padx=(0, 8), pady=3)
            if key == "default_loc_id":
                var = tk.StringVar(value=cur_loc)
                w = ttk.Combobox(body, textvariable=var, width=32,
                                 values=[self.NO_LOC] + sorted(self._loc_map))
                w.state(["readonly"])
            elif kind == "category":
                # 编辑已有元件:显示它在树上的全路径。只显示大类名的话,
                # 打开窗口什么都不动直接保存,子类归属就被冲回大类了。
                cur_cat = str(existing.get(key) or "")
                cid_cur = existing.get("category_id")
                if cid_cur and cid_cur in self._cat_by_id:
                    cur_cat = self._cat_by_id[cid_cur]
                elif not cid and cat_id in self._cat_by_id:
                    # 新建:预填成用户当前所在的那一级(全路径),保存时就落在这一级
                    cid_cur = cat_id
                    cur_cat = self._cat_by_id[cat_id]
                var = tk.StringVar(value=cur_cat)
                self._picked_cat_id = cid_cur if cid_cur in self._cat_by_id else None
                self._picked_cat_label = cur_cat if self._picked_cat_id else ""
                # 只读展示 + 「换…」:一级一级选,不再给一条装着全路径的长下拉
                w = ttk.Frame(body)
                self.cb_cat = ttk.Entry(w, textvariable=var, width=26,
                                        state="readonly")
                self.cb_cat.pack(side="left")
                ttk.Button(w, text="换…", width=5,
                           command=self.pick_category).pack(side="left", padx=(4, 0))
            elif kind == "supplier":
                var = tk.StringVar(value=str(existing.get(key) or ""))
                w = ttk.Combobox(body, textvariable=var, width=32, values=sup)
            else:
                var = tk.StringVar(value="" if existing.get(key) is None
                                   else str(existing.get(key, "")))
                w = ttk.Entry(body, textvariable=var, width=34)
            self.vars[key] = var
            w.grid(row=i, column=1, sticky="w", pady=3)
            if kind == "category" and self.cb_cat is None:
                self.cb_cat = w


        r = len(self.FIELDS)

        # 属性(耐压 / 精度 / 功率 / 额定电流 / 饱和电流 …)。用户自己起名字,
        # 想加什么加什么。存成 component.params 的 JSON,所以加新属性不用改表结构。
        #
        # 这里以前是一个 tk.Text,要用户背「一行一个,写成 耐压=50V」这个语法:
        # 键名打错一个字不报错,只是静默多出一条属性;想删掉其中一条也只能整块
        # 重打。现在是一张「名称 / 值」的小表,一行一个属性。
        ttk.Label(body, text="属性").grid(row=r, column=0, sticky="ne", padx=(0, 8), pady=3)
        self.attr_box = ttk.Frame(body)
        self.attr_box.grid(row=r, column=1, sticky="w", pady=3)
        self.attr_rows = []
        head = ttk.Frame(self.attr_box)
        head.pack(fill="x")
        ttk.Label(head, text="名称", width=12, style="Dim.TLabel").pack(side="left")
        ttk.Label(head, text="值", width=15, style="Dim.TLabel").pack(side="left", padx=(4, 0))
        # 「＋ 加一个属性」先摆上,后面每加一行都插到它前面 —— 顺序才稳定
        self.attr_btn = ttk.Button(self.attr_box, text="＋ 加一个属性", command=self.attr_add)
        self.attr_btn.pack(anchor="w", pady=(2, 0))
        saved = existing.get("params")
        for _k, _v in (saved.items() if isinstance(saved, dict) else []):
            self.attr_add(_k, _v)
        if not self.attr_rows:
            self.attr_add()            # 新元件也先给一行,免得人要找「怎么加」
        # 品类一改,属性名的候选跟着换(电感的「饱和电流」不该出现在电阻下面)。
        # 现在品类是「只读框 + 换…」选出来的,所以刷新挂在 pick_category 里 ——
        # 别再往 cb_cat 上绑 <<ComboboxSelected>>:Entry 不会发这个事件。
        ttk.Label(body, text="比如 耐压=50V、精度=±5%。名称能自己填,候选按品类给。",
                  style="Dim.TLabel").grid(row=r + 1, column=1, sticky="w")

        ttk.Label(body, text="备注").grid(row=r + 2, column=0, sticky="ne", padx=(0, 8), pady=3)
        self.note = tk.Text(body, width=34, height=3, font=FONT)
        self.note.grid(row=r + 2, column=1, sticky="w", pady=3)
        self.note.insert("1.0", existing.get("note") or "")

        ttk.Label(body, text="商品编号、厂家料号都可以留空 —— 系统按「值 + 封装」自己认。",
                  style="Dim.TLabel").grid(row=r + 3, column=1, sticky="w", pady=(0, 6))

        btns = ttk.Frame(body)
        btns.grid(row=r + 4, column=0, columnspan=2, pady=(6, 0))
        ttk.Button(btns, text="保存", command=self.save, width=12).grid(row=0, column=0, padx=4)
        ttk.Button(btns, text="取消", command=self.destroy, width=12).grid(row=0, column=1, padx=4)

        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()
        self.focus_set()
        self._center(parent)

    def _center(self, parent):
        self.update_idletasks()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        w, h = self.winfo_width(), self.winfo_height()
        self.geometry(f"+{px + (pw - w) // 2}+{py + max(0, (ph - h) // 4)}")

    # ------------------------------------------------------------ 属性小表

    def attr_options(self):
        """属性名候选:库里这个品类下已经在用的 + 内置建议。

        只是**候选**,不是白名单 —— 下拉框是可编辑的,光耦的「温度范围」、
        传感器的「量程」这类推不出来的名字必须能自己写。只给固定清单的话,
        用户只能挑一个最接近的凑合,等于把「它猜错了」换成「我被迫选了个不准确的」。
        """
        meta = getattr(self, "_meta", None) or {}
        cat = (self.vars["category"].get() or "").strip() if "category" in self.vars else ""
        by_cat = meta.get("attrs_by_category") or {}
        extra = list(by_cat.get(cat) or [])
        if not cat:
            # 品类还没填:把整库用过的属性名都端上来,下拉不该是空的
            extra = list(meta.get("attrs_all") or [])
        return attrs.suggest(cat, extra)

    def attr_add(self, name="", value=""):
        """加一行属性。名称是可编辑下拉。"""
        row = ttk.Frame(self.attr_box)
        row.pack(fill="x", pady=1, before=self.attr_btn)
        var_n = tk.StringVar(value=attrs.display_name(name))
        var_v = tk.StringVar(value="" if value is None else str(value))
        cb = ttk.Combobox(row, textvariable=var_n, width=12, values=self.attr_options())
        cb.pack(side="left")
        ttk.Entry(row, textvariable=var_v, width=15, font=FONT).pack(side="left", padx=(4, 0))
        ttk.Button(row, text="✕", width=3,
                   command=lambda r=row: self.attr_del(r)).pack(side="left", padx=(4, 0))
        self.attr_rows.append((row, cb, var_n, var_v))

    def attr_del(self, row):
        """删掉一行。控件和它那两个变量一起丢掉,免得 _parse_params 读到幽灵行。"""
        self.attr_rows = [t for t in self.attr_rows if t[0] is not row]
        row.destroy()

    def attr_refresh(self):
        """品类改了,属性名候选重算一遍。"""
        opts = self.attr_options()
        for _row, cb, _vn, _vv in self.attr_rows:
            cb.configure(values=opts)

    def _parse_params(self):
        """把小表读成 {名称: 值}。

        * 名称空着的那一行直接丢掉 —— 点「＋ 加一个属性」多出来一行又没填,
          不该存成一条空名字的属性
        * 名称相同的行,后面的覆盖前面的
        * **值空着也留着**:「这颗料的耐压还没填」是要记住的状态,
          和「这颗料没有耐压这个属性」是两件事,后者才是把那一行删掉
        * 名称按 attrs.norm 归一化后再合并,`耐压 ` 和 `耐压` 算同一个名字
        """
        out = {}
        for _row, _cb, var_n, var_v in self.attr_rows:
            key = attrs.norm(var_n.get())
            if not key:
                continue
            out[key] = var_v.get().strip()
        return out

    def pick_category(self):
        """逐级选品类。选完把显示改成全路径,具体挂哪个节点在 save 里定。"""
        dlg = CategoryPickerDialog(self, self.app, self._picked_cat_id)
        self.wait_window(dlg)
        if not dlg.result:
            return
        cid, path = dlg.result
        if cid is None:
            return
        self._picked_cat_id = cid
        self._picked_cat_label = path
        self.vars["category"].set(path)
        # 品类变了,属性名的候选跟着换(耐压/精度这些是按品类推的)
        self.attr_refresh()

    def save(self):
        body = {k: v.get().strip() for k, v in self.vars.items()}

        if not body.get("name"):
            messagebox.showinfo("提示", "名称必填。", parent=self)
            return
        for key, label, _kind in self.FIELDS:
            if key in ("min_stock", "reorder_qty"):
                try:
                    body[key] = int(body.get(key) or 0)
                except ValueError:
                    messagebox.showinfo("提示", f"{label} 要填整数。", parent=self)
                    return
            elif key == "unit_price":
                try:
                    body[key] = float(body.get(key) or 0)
                except ValueError:
                    messagebox.showinfo("提示", "参考单价要填数字。", parent=self)
                    return

        loc_id = self._loc_map.get(body.pop("default_loc_id", ""), None)
        body["default_loc_id"] = loc_id
        body["params"] = self._parse_params()
        body["note"] = self.note.get("1.0", "end").strip()
        # 下拉里选的是树上哪个节点就挂到哪个节点。手打的词(_cat_map 里没有)
        # 仍旧只发名字,由后端按名字找/建顶层节点 —— 这条路必须留着:
        # 认不出来的品类(光耦、传感器模块)得能直接填。
        cat_text = (body.get("category") or "").strip()
        cat_id = self._picked_cat_id if (cat_text and cat_text == self._picked_cat_label) \
            else None
        if cat_id is None:
            cat_id = self._cat_map.get(cat_text)
        if cat_id:
            body["category_id"] = cat_id
            # category 写顶层大类名,和 category_id 指向的一支保持一致
            body["category"] = cat_text.split(" / ")[0] if " / " in cat_text else cat_text

        if self.cid:
            res = call(self.con, server.update_component, body=body, match=(self.cid,), parent=self)
        else:
            res = call(self.con, server.create_component, body=body, parent=self)
        if res is None:
            return
        # 记住这颗料的 id,好让列表页刷完直接选中它(issue #22)
        self.new_id = self.cid or (res or {}).get("id")
        self.done = True
        self.destroy()


class MoveDialog(tk.Toplevel):
    """从元件列表右键直接开单。"""

    def __init__(self, parent, app: App, cid, kind):
        super().__init__(parent)
        self.app = app
        self.con = app.con
        self.cid = cid
        self.done = False

        comp = call(self.con, server.get_component, match=(cid,), quiet=True) or {}
        self.title(f"{KIND_LABEL.get(kind, kind)} —— {comp.get('name') or cid}")
        self.transient(parent)
        self.resizable(False, False)

        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text=f"{comp.get('name')}\n现有 {comp.get('on_hand') or 0} "
                             f"{comp.get('unit') or '个'}",
                  style="Big.TLabel").grid(row=0, column=0, columnspan=2, pady=(0, 10), sticky="w")

        ttk.Label(body, text="动作").grid(row=1, column=0, sticky="e", padx=(0, 8), pady=4)
        self.kind = tk.StringVar(value=kind)
        kf = ttk.Frame(body)
        kf.grid(row=1, column=1, sticky="w")
        for i, (val, label) in enumerate(KIND_LABEL.items()):
            ttk.Radiobutton(kf, text=label, value=val, variable=self.kind,
                            command=self._sync).grid(row=0, column=i)

        ttk.Label(body, text="数量").grid(row=2, column=0, sticky="e", padx=(0, 8), pady=4)
        self.qty = tk.StringVar()
        ttk.Entry(body, textvariable=self.qty, width=16).grid(row=2, column=1, sticky="w")

        meta = call(self.con, server.meta, quiet=True) or {}
        codes = [l["code"] for l in meta.get("locations") or []]

        ttk.Label(body, text="仓位").grid(row=3, column=0, sticky="e", padx=(0, 8), pady=4)
        self.loc = tk.StringVar(value="未分类")
        ttk.Combobox(body, textvariable=self.loc, width=13, values=codes).grid(
            row=3, column=1, sticky="w")

        self.lbl_to = ttk.Label(body, text="移到")
        self.lbl_to.grid(row=4, column=0, sticky="e", padx=(0, 8), pady=4)
        self.to_loc = tk.StringVar(value=codes[0] if codes else "未分类")
        self.cb_to = ttk.Combobox(body, textvariable=self.to_loc, width=13, values=codes)
        self.cb_to.grid(row=4, column=1, sticky="w")

        ttk.Label(body, text="备注").grid(row=5, column=0, sticky="e", padx=(0, 8), pady=4)
        self.note = tk.StringVar()
        ttk.Entry(body, textvariable=self.note, width=28).grid(row=5, column=1, sticky="w")

        btns = ttk.Frame(body)
        btns.grid(row=6, column=0, columnspan=2, pady=(12, 0))
        ttk.Button(btns, text="提交", command=self.submit, width=12).grid(row=0, column=0, padx=4)
        ttk.Button(btns, text="取消", command=self.destroy, width=12).grid(row=0, column=1, padx=4)

        self.bind("<Escape>", lambda _e: self.destroy())
        self._sync()
        self.grab_set()

    def _sync(self):
        if self.kind.get() == "TRANSFER":
            self.lbl_to.grid()
            self.cb_to.grid()
        else:
            self.lbl_to.grid_remove()
            self.cb_to.grid_remove()

    def submit(self):
        raw = self.qty.get().strip()
        if not raw.isdigit():
            messagebox.showinfo("提示", "数量要填非负整数。", parent=self)
            return
        body = {"kind": self.kind.get(), "component_id": self.cid, "qty": int(raw),
                "location": self.loc.get().strip() or "未分类",
                "note": self.note.get().strip()}
        if self.kind.get() == "TRANSFER":
            body["to_location"] = self.to_loc.get().strip()
        manual_note(body)          # 库存页右键的入库/出库/盘点/移库都是手动的
        if call(self.con, server.stock_move, body=body, parent=self) is not None:
            self.done = True
            self.destroy()


# --------------------------------------------------------------------- 入口

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    db_path = argv[0] if argv else DEFAULT_DB

    # 启动前先整库快照一份,和网页版一致
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        made = server.backup_db(db_path)
        if made:
            print(f"[备份] 已创建 {os.path.basename(made)}")
    except Exception:  # noqa: BLE001 —— 备份失败不该拦住启动
        traceback.print_exc()

    app = App(db_path)
    # 先把窗口真正画出来,再写一行日志:这样 data\gui.log 里能直接看到
    # 「窗口有没有成功显示」,以后排障不用猜。
    app.update()
    try:
        print(f"[就绪] 窗口已显示 {app.winfo_width()}x{app.winfo_height()},"
              f"可见={bool(app.winfo_viewable())};{app.status.get()}")
    except Exception:  # noqa: BLE001 —— 没有 stdout 时(直接跑 pythonw)忽略即可
        pass
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
