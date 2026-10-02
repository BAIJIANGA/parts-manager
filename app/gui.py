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
import sqlite3
import sys
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import db          # noqa: E402
import server      # noqa: E402  ← 复用全部业务逻辑

DEFAULT_DB = os.path.join(server.PROJECT_ROOT, "data", "parts.db")
ICON_PATH = os.path.join(server.PROJECT_ROOT, "app", "static", "app.ico")
BACKUP_DIR = os.path.join(server.PROJECT_ROOT, "data", "backups")

KIND_LABEL = {"IN": "入库", "OUT": "出库", "ADJUST": "盘点", "TRANSFER": "移库"}
STATE_LABEL = {"ok": "充足", "low": "偏低", "out": "缺货"}
FONT = ("Microsoft YaHei UI", 9)


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


def make_tree(parent, columns, height=14):
    """columns: [(key, 标题, 宽度, 对齐), ...]  返回 (frame, tree)

    一律 show="headings" —— 不显示 #0 树列,也就没有展开三角。这个界面里
    所有列表都是平的:分类用卡片进二级页,列表本身不需要折叠。
    """
    frame = ttk.Frame(parent)
    keys = [c[0] for c in columns]
    tree = ttk.Treeview(frame, columns=keys, height=height, show="headings")
    for col in columns:
        key, title, width, anchor = col[:4]
        # 第 5 个元素可以指定这一列是否跟着窗口伸缩;不写就按老规矩(文字类才伸缩)
        stretch = col[4] if len(col) > 4 else (key in ("name", "note"))
        tree.heading(key, text=title)
        tree.column(key, width=width, anchor=anchor, stretch=stretch)
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

        self.tab_comp = ComponentsTab(self.nb, self)
        self.tab_stock = StockTab(self.nb, self)
        self.tab_proj = ProjectsTab(self.nb, self)
        self.tab_move = MovementsTab(self.nb, self)
        self.tab_loc = LocationsTab(self.nb, self)
        for tab, label in ((self.tab_comp, "  元件库存  "),
                           (self.tab_stock, "  出入库  "),
                           (self.tab_proj, "  项目 BOM  "),
                           (self.tab_move, "  流水  "),
                           (self.tab_loc, "  仓位  ")):
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
        m_tool.add_command(label="按流水重建库存余额(校验)", command=self.rebuild_stock)
        m_tool.add_command(label="导出元件清单 CSV…", command=self.export_components)
        m_tool.add_separator()
        m_tool.add_command(label="刷新全部", command=self.refresh_all)
        menubar.add_cascade(label="工具", menu=m_tool)

        m_help = tk.Menu(menubar, tearoff=0)
        m_help.add_command(label="关于", command=self.about)
        menubar.add_cascade(label="帮助", menu=m_help)

        self.config(menu=menubar)

    # ---------------------------------------------------------- 数据

    def refresh_all(self):
        for tab in (self.tab_comp, self.tab_stock, self.tab_proj, self.tab_move, self.tab_loc):
            try:
                tab.reload()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
        self.refresh_status()

    def refresh_status(self):
        s = call(self.con, server.summary, quiet=True)
        if not s:
            self.status.set("统计读取失败")
            return
        self.status.set(
            f"元件 {s['components']} 种   总库存 {s['total_qty']} 个   "
            f"缺货 {s['out']} 种   偏低 {s['low']} 种   项目 {s['projects']} 个"
        )

    def set_status(self, text, seconds=4):
        self.status.set(text)
        self.after(seconds * 1000, self.refresh_status)

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
        cols = [("name", "名称"), ("lcsc_pn", "立创编号"), ("mpn", "厂家料号"),
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
# 首页固定列出这 16 个标准大类,库里有货没货都列出来 —— 一眼能看到分类全貌,
# 点进去没有再说没有。「未分类」和自定义品类只有在库里真出现时才补在后面。
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

    def __init__(self, parent, on_pick):
        super().__init__(parent)
        self.on_pick = on_pick
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

        self._build_home()
        self._build_cat()
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
        ttk.Button(head, text="＋ 新增元件", command=self.add).pack(side="right")
        ttk.Button(head, text="刷新", command=self.reload).pack(side="right", padx=6)

        sbox = ttk.Frame(self.page_home)
        sbox.pack(fill="x", pady=(0, 10))
        ttk.Label(sbox, text="搜索").pack(side="left", padx=(0, 4))
        self.q = tk.StringVar()
        ent = ttk.Entry(sbox, textvariable=self.q, width=30)
        ent.pack(side="left")
        ent.bind("<Return>", lambda _e: self.open_search())
        ttk.Button(sbox, text="搜索", command=self.open_search).pack(side="left", padx=6)
        ttk.Label(sbox, text="名称 / 立创编号 / 料号 / 封装 / 值",
                  style="Dim.TLabel").pack(side="left", padx=6)

        self.board = CardBoard(self.page_home, self.open_category)
        self.board.pack(fill="both", expand=True)

    # ------------------------------------------------------ 页面:某个大类

    def _build_cat(self):
        head = ttk.Frame(self.page_cat)
        head.pack(fill="x", pady=(0, 8))
        ttk.Button(head, text="← 返回", command=self.go_home, width=9).pack(side="left",
                                                                         padx=(0, 10))
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

        pane = ttk.Panedwindow(self.page_cat, orient="vertical")
        pane.pack(fill="both", expand=True)

        top = ttk.Frame(pane)
        # 这里是平表,不是树 —— 没有 #0 列,也就没有展开三角
        frame, self.tree = make_tree(top, [
            ("name", "名称", 200, "w", True),
            ("lcsc_pn", "立创编号", 95, "center"),
            ("mpn", "厂家料号", 130, "w", True),
            ("manufacturer", "厂家", 110, "w"),
            ("package", "封装", 100, "w", True),
            ("value", "值", 80, "w"),
            ("on_hand", "库存", 60, "e"),
            ("min_stock", "最低", 55, "e"),
            ("state", "状态", 60, "center"),
            ("note", "备注", 170, "w", True),
        ], height=14)
        frame.pack(fill="both", expand=True)
        pane.add(top, weight=3)

        self.tree.tag_configure("out", background="#ffe3e3")
        self.tree.tag_configure("low", background="#fff6dd")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", self._on_double)

        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="入库", command=lambda: self._quick_move("IN"))
        self.menu.add_command(label="出库", command=lambda: self._quick_move("OUT"))
        self.menu.add_command(label="盘点", command=lambda: self._quick_move("ADJUST"))
        self.menu.add_command(label="移库", command=lambda: self._quick_move("TRANSFER"))
        self.menu.add_separator()
        self.menu.add_command(label="编辑…", command=self.edit)
        self.menu.add_command(label="删除", command=self.delete)
        self.tree.bind("<Button-3>", self._popup)

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

    # ------------------------------------------------------ 页面切换

    def _swap(self, page):
        self.page_home.pack_forget()
        self.page_cat.pack_forget()
        page.pack(fill="both", expand=True)

    def go_home(self):
        self.view = "home"
        self.current_category = None
        self._swap(self.page_home)

    def open_category(self, name):
        self.view = "cat"
        self.current_category = name
        glyph, color = CATEGORY_STYLE.get(name, DEFAULT_CAT_STYLE)
        rows = self._cat_data.get(name, [])
        self.cat_badge.configure(text=glyph, bg=color if rows else DIM_BADGE)
        self.cat_title.set(name)
        self.cat_count.set(f"共 {len(rows)} 种" if rows else "暂无库存元件")
        self._swap(self.page_cat)
        self._fill(rows)

    def open_search(self):
        kw = self.q.get().strip()
        if not kw:
            self.go_home()
            return
        self.view = "search"
        self.current_category = None
        hits = [it for it in self._all if self._match(it, kw)]
        self.cat_badge.configure(text="⌕", bg="#546e7a")
        self.cat_title.set(f"搜索:{kw}")
        self.cat_count.set(f"找到 {len(hits)} 种" if hits else "没找到")
        self._swap(self.page_cat)
        self._fill(hits)

    @staticmethod
    def _match(it, kw):
        low = kw.lower()
        for key in ("name", "lcsc_pn", "mpn", "manufacturer", "value",
                    "package", "category", "note"):
            if low in str(it.get(key) or "").lower():
                return True
        return False

    def _fill(self, rows):
        clear_tree(self.tree)
        self._rows = {}
        for it in rows:
            self._rows[it["id"]] = it
            self._insert_component(it)

    # ------------------------------------------------------ 数据

    def reload(self):
        # stocked=1:库存为 0 的元件在 SQL 层就被滤掉,二级页面自然只剩有货的
        data = call(self.con, server.list_components,
                    query={"stocked": "1", "sort": "category"}, quiet=True)
        if data is None:
            return
        self._all = data["items"]

        self._cat_data = {}
        for it in self._all:
            name = (it.get("category") or "").strip() or UNCATEGORIZED
            self._cat_data.setdefault(name, []).append(it)

        # 首页固定列出 16 个标准大类;库里另有自定义品类或未分类的,补在后面
        meta = call(self.con, server.meta, quiet=True) or {}
        real = [c for c in ((meta.get("filters") or {}).get("categories") or []) if c]
        names = list(CATEGORY_ORDER)
        names += sorted(c for c in real if c not in CATEGORY_ORDER)
        if any(not c.strip() for c in real):
            names.append(UNCATEGORIZED)
        self._card_names = names

        self._render_cards()
        n = len(self._all)
        self.count.set(f"{n} 种在库元件" if n else "还没有元件入库")

        if self.view == "cat":
            if self.current_category in self._card_names:
                self.open_category(self.current_category)
            else:
                self.go_home()
        elif self.view == "search":
            if self.q.get().strip():
                self.open_search()
            else:
                self.go_home()

    def _render_cards(self):
        specs = []
        for name in self._card_names:
            glyph, color = CATEGORY_STYLE.get(name, DEFAULT_CAT_STYLE)
            has = bool(self._cat_data.get(name))
            specs.append((name, name, glyph, color,
                          "点开查看 →" if has else "暂无库存", not has))
        self.board.render(specs, empty_text="还没有元件入库。")

    def _insert_component(self, it):
        self.tree.insert("", "end", iid=str(it["id"]), values=(
            it.get("name") or "", it.get("lcsc_pn") or "", it.get("mpn") or "",
            it.get("manufacturer") or "", it.get("package") or "",
            it.get("value") or "", it.get("on_hand") or 0, it.get("min_stock") or 0,
            STATE_LABEL.get(it.get("stock_state"), ""), it.get("note") or ""),
            tags=(it.get("stock_state") or "",))

    def _cat_rank(self, name):
        """大类的显示顺序:常见元件类排前面,认不出来的按名称排在后面。"""
        try:
            return (0, CATEGORY_ORDER.index(name))
        except ValueError:
            return (1, name)

    def selected_id(self):
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    # ------------------------------------------------------ 选中与右键

    def _on_double(self, event):
        if self.tree.identify_row(event.y):
            self.edit()

    def _on_select(self, _event=None):
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
        dlg = ComponentDialog(self, self.app, None)
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
                    return
            else:
                return
        self.app.refresh_all()


# --------------------------------------------------------------------- 出入库

class StockTab(ttk.Frame):
    """出入库 —— 拆成「入库」和「出库」两个页签。

    这两件事方向相反、看的东西也不一样,挤在一个单选框里容易点错。每一页都是:

        先选项目(卡片)  →  进这个项目的二级页,在里面开单,并看这个项目
                            在这个动作下的全部记录(按时间倒序)

    项目是这批料的去向,所以按项目分开看最自然;「不指定项目」那一格用来记
    不带项目的日常补货。盘点 / 移库不在这两个页签里,用下面那个按钮开,
    它们本来也不是「进 / 出」这种方向性的动作。
    """

    ACTIONS = (("IN", "入库"), ("OUT", "出库"))
    NO_PROJECT = "不指定项目"

    def __init__(self, parent, app: App):
        super().__init__(parent, padding=8)
        self.app = app
        self.con = app.con
        self._items = {}
        self._projects = []
        self.action = "IN"
        self.project_id = None      # None = 停在项目首页;0 = 不指定项目;>0 = 项目 id
        self.view = "home"

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 8))
        self.btn = {}
        for val, label in self.ACTIONS:
            b = ttk.Button(bar, text=label, width=10,
                           command=lambda v=val: self.set_action(v))
            b.pack(side="left", padx=(0, 6))
            self.btn[val] = b
        self.head = tk.StringVar()
        ttk.Label(bar, textvariable=self.head, style="Dim.TLabel").pack(side="left", padx=12)
        ttk.Button(bar, text="盘点 / 移库…", command=self.other_move).pack(side="right")
        ttk.Button(bar, text="刷新", command=self.reload).pack(side="right", padx=6)

        self.holder = ttk.Frame(self)
        self.holder.pack(fill="both", expand=True)
        self.page_home = ttk.Frame(self.holder)
        self.page_proj = ttk.Frame(self.holder)

        self._build_home()
        self._build_proj()
        self.set_action("IN")

    @property
    def cards(self):
        return self.board.cards

    # ------------------------------------------------------ 顶部动作切换

    def set_action(self, val):
        self.action = val
        for k, b in self.btn.items():
            b.state(["pressed"] if k == val else ["!pressed"])
        self.go_home()
        self.reload()

    def _act_label(self):
        return dict(self.ACTIONS)[self.action]

    # ------------------------------------------------------ 页面:项目卡片

    def _build_home(self):
        self.board = CardBoard(self.page_home, self.open_project)
        self.board.pack(fill="both", expand=True)

    def open_project(self, key):
        self.view = "proj"
        self.project_id = int(key)
        if self.project_id:
            proj = next((p for p in self._projects if p["id"] == self.project_id), {})
            name = proj.get("name") or f"项目 {self.project_id}"
        else:
            name = self.NO_PROJECT
        self.proj_title.set(f"{self._act_label()} · {name}")
        self._swap(self.page_proj)
        self._load_records()
        self.reload()

    def go_home(self):
        self.view = "home"
        self.project_id = None
        self._swap(self.page_home)

    def _swap(self, page):
        self.page_home.pack_forget()
        self.page_proj.pack_forget()
        page.pack(fill="both", expand=True)

    def _render_cards(self):
        specs = []
        for i, p in enumerate(self._projects):
            name = p.get("name") or f"项目 {p['id']}"
            specs.append((str(p["id"]), name, (name.strip()[:1] or "P").upper(),
                          PROJECT_COLORS[i % len(PROJECT_COLORS)],
                          "点开开单 / 看记录", False))
        specs.append(("0", self.NO_PROJECT, "—", "#8d99a6",
                      "点开开单 / 看记录", False))
        self.board.render(specs, empty_text="还没有项目。先到「项目 BOM」页导入一个。")

    # ------------------------------------------------------ 页面:某个项目

    def _build_proj(self):
        head = ttk.Frame(self.page_proj)
        head.pack(fill="x", pady=(0, 8))
        ttk.Button(head, text="← 返回", command=self.go_home, width=9).pack(side="left",
                                                                         padx=(0, 10))
        self.proj_title = tk.StringVar()
        ttk.Label(head, textvariable=self.proj_title, style="H1.TLabel").pack(side="left")
        self.proj_info = tk.StringVar()
        ttk.Label(head, textvariable=self.proj_info, style="Dim.TLabel").pack(side="left",
                                                                             padx=12)

        pane = ttk.Panedwindow(self.page_proj, orient="vertical")
        pane.pack(fill="both", expand=True)

        # ---- 上半:开单(左挑元件,右填单)
        top = ttk.Frame(pane)
        left = ttk.Frame(top)
        left.pack(side="left", fill="both", expand=True, padx=(0, 8))

        sbar = ttk.Frame(left)
        sbar.pack(fill="x", pady=(0, 6))
        ttk.Label(sbar, text="找元件").pack(side="left", padx=(0, 4))
        self.q = tk.StringVar()
        ent = ttk.Entry(sbar, textvariable=self.q, width=24)
        ent.pack(side="left")
        ent.bind("<KeyRelease>", lambda _e: self._load_components())
        ttk.Button(sbar, text="只看缺货", command=self.only_low).pack(side="left", padx=6)

        f, self.tree = make_tree(left, [
            ("name", "名称", 190, "w", True),
            ("lcsc_pn", "立创编号", 90, "center"),
            ("package", "封装", 100, "w"),
            ("on_hand", "库存", 60, "e"),
            ("state", "状态", 60, "center")], height=12)
        f.pack(fill="both", expand=True)
        self.tree.tag_configure("out", background="#ffe3e3")
        self.tree.tag_configure("low", background="#fff6dd")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        right = ttk.LabelFrame(top, text="开单", padding=10)
        right.pack(side="left", fill="y")
        self.target = tk.StringVar(value="（左侧选一个元件）")
        ttk.Label(right, textvariable=self.target, style="Big.TLabel",
                  wraplength=260).grid(row=0, column=0, columnspan=2, pady=(0, 10), sticky="w")

        ttk.Label(right, text="数量").grid(row=1, column=0, sticky="e", padx=(0, 6), pady=3)
        self.qty = tk.StringVar()
        ttk.Entry(right, textvariable=self.qty, width=14).grid(row=1, column=1, sticky="w")

        ttk.Label(right, text="仓位").grid(row=2, column=0, sticky="e", padx=(0, 6), pady=3)
        self.loc = tk.StringVar(value="未分类")
        self.cb_loc = ttk.Combobox(right, textvariable=self.loc, width=12)
        self.cb_loc.grid(row=2, column=1, sticky="w")

        # 移库才用得到,平时藏起来
        self.lbl_to = ttk.Label(right, text="移到")
        self.lbl_to.grid(row=3, column=0, sticky="e", padx=(0, 6), pady=3)
        self.to_loc = tk.StringVar()
        self.cb_to = ttk.Combobox(right, textvariable=self.to_loc, width=12)
        self.cb_to.grid(row=3, column=1, sticky="w")

        ttk.Label(right, text="操作人").grid(row=4, column=0, sticky="e", padx=(0, 6), pady=3)
        self.who = tk.StringVar(value="本地用户")
        ttk.Entry(right, textvariable=self.who, width=14).grid(row=4, column=1, sticky="w")

        ttk.Label(right, text="备注").grid(row=5, column=0, sticky="e", padx=(0, 6), pady=3)
        self.note = tk.StringVar()
        ttk.Entry(right, textvariable=self.note, width=22).grid(row=5, column=1,
                                                               columnspan=2, sticky="w")

        self.hint = tk.StringVar()
        ttk.Label(right, textvariable=self.hint, style="Dim.TLabel",
                  wraplength=260, justify="left").grid(
            row=6, column=0, columnspan=2, pady=(6, 0), sticky="w")
        ttk.Button(right, text="提交", command=self.submit, width=16).grid(
            row=7, column=0, columnspan=2, pady=12)
        pane.add(top, weight=3)

        # ---- 下半:这个项目在这个动作下的记录,按时间倒序
        box = ttk.LabelFrame(pane, text=f"{self._act_label()}记录(按时间倒序)", padding=6)
        f2, self.t_rec = make_tree(box, [
            ("created_at", "时间", 150, "center"),
            ("component_name", "元件", 200, "w", True),
            ("lcsc_pn", "立创编号", 95, "center"),
            ("qty", "数量", 60, "e"),
            ("location_code", "仓位", 90, "w"),
            ("operator", "操作人", 90, "w"),
            ("note", "备注", 200, "w", True)], height=7)
        f2.pack(fill="both", expand=True)
        self.box_rec = box
        pane.add(box, weight=2)

    # ------------------------------------------------------ 数据

    def reload(self):
        meta = call(self.con, server.meta, quiet=True) or {}
        codes = [l["code"] for l in meta.get("locations") or []]
        self.cb_loc.configure(values=codes)
        self.cb_to.configure(values=codes or ["未分类"])
        if not self.to_loc.get() and codes:
            self.to_loc.set(codes[0])

        projects = call(self.con, server.list_projects, quiet=True) or {}
        self._projects = projects.get("items") or []

        self._render_cards()
        self._load_components()
        self._sync_hint()
        if self.view == "proj":
            self.head.set(f"{self._act_label()}:下面是这个项目的开单区和记录")
            self.box_rec.configure(text=f"{self._act_label()}记录(按时间倒序)")
            # 开完单 app.refresh_all() 会走到这里 —— 必须重读记录表,
            # 否则提交成功了但下面那半张表还是旧的
            self._load_records()
        else:
            self.head.set(f"{self._act_label()}:先选一个项目,进去开单并看记录")

    def _load_components(self):
        keep = self._current_id()      # 刷之前选中的是谁,刷完要还选回去
        query = {"limit": 500}
        if self.q.get().strip():
            query["q"] = self.q.get().strip()
        if getattr(self, "_only_low", False):
            data = call(self.con, server.lowstock, quiet=True)
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
        # 开完单 app.refresh_all() 会走到这里。不把选中还回去的话,左侧会变成
        # 没选中任何元件的状态,下一次开单得重新点一遍 —— 连续入库很难用。
        if keep and keep in self._items:      # _items 的键是 int,别拿 str 去比
            self.tree.selection_set(str(keep))
            self.tree.see(str(keep))

    def _load_records(self):
        clear_tree(self.t_rec)
        query = {"kind": self.action, "limit": 500}
        if self.project_id:
            query["project_id"] = str(self.project_id)
        else:
            query["project"] = "none"
        data = call(self.con, server.list_movements, query=query, quiet=True) or {}
        items = data.get("items") or []
        for mv in items:
            self.t_rec.insert("", "end", values=(
                (mv.get("created_at") or "")[:19], mv.get("component_name") or "",
                mv.get("lcsc_pn") or "", mv.get("qty") or 0,
                mv.get("location_code") or "", mv.get("operator") or "",
                mv.get("note") or ""))
        self.proj_info.set(f"{len(items)} 条记录" if items else "还没有记录")

    def only_low(self):
        self._only_low = not getattr(self, "_only_low", False)
        self._load_components()

    def _on_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        it = self._items.get(int(sel[0])) or {}
        self.target.set(f"{it.get('name')}\n现有 {it.get('on_hand') or 0} {it.get('unit') or '个'}")

    def _current_id(self):
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def _sync_hint(self):
        if self.action == "IN":
            self.hint.set("入库:数量填入库数量。提交后记在这个项目名下,并按时间排在下面。")
        else:
            self.hint.set("出库:数量填出库数量,库存不足会被拒绝。")

    def _sync(self):        # 兼容老代码的调用
        self._sync_hint()

    # ------------------------------------------------------ 动作

    def submit(self):
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
                "note": self.note.get().strip()}
        if self.project_id:
            body["project_id"] = self.project_id
        res = call(self.con, server.stock_move, body=body, parent=self)
        if res is None:
            return
        self.qty.set("")
        self.note.set("")
        self.app.set_status(
            f"{self._act_label()}完成:该仓位现有 {res['qty_at_location']},"
            f"总库存 {res['on_hand']}")
        self.app.refresh_all()

    def other_move(self):
        """盘点 / 移库:不是「进 / 出」这种方向性动作,单独开弹窗做。"""
        cid = self._current_id()
        if not cid:
            messagebox.showinfo("提示", "请先在左边选中一个元件,再选盘点或移库。",
                                parent=self)
            return
        dlg = MoveDialog(self, self.app, cid, "ADJUST")
        self.app.wait_window(dlg)
        if dlg.done:
            self.app.refresh_all()


# --------------------------------------------------------------------- 项目 BOM

class ProjectsTab(ttk.Frame):
    def __init__(self, parent, app: App):
        super().__init__(parent, padding=8)
        self.app = app
        self.con = app.con
        self._pid = None
        self._lines = {}

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Button(bar, text="📥 导入 BOM (Altium .xlsx)", command=self.import_bom).pack(side="left")
        ttk.Button(bar, text="按 BOM 领料", command=self.pick).pack(side="left", padx=6)
        ttk.Button(bar, text="导出缺料 CSV", command=self.export_shortage).pack(side="left")
        ttk.Button(bar, text="新建空项目", command=self.new_project).pack(side="left", padx=6)
        ttk.Button(bar, text="删除项目", command=self.delete_project).pack(side="left")
        ttk.Button(bar, text="刷新", command=self.reload).pack(side="left", padx=6)

        pane = ttk.Panedwindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True)

        left = ttk.LabelFrame(pane, text="项目", padding=6)
        f, self.t_proj = make_tree(left, [
            ("name", "项目", 190, "w"),
            ("bom_lines", "料号", 55, "e"),
            ("required_qty", "总需求", 65, "e")], height=20)
        f.pack(fill="both", expand=True)
        self.t_proj.bind("<<TreeviewSelect>>", self._on_pick_project)
        pane.add(left, weight=1)

        right = ttk.Frame(pane)
        head = ttk.Frame(right)
        head.pack(fill="x", pady=(0, 6))
        self.title = tk.StringVar(value="（左侧选一个项目）")
        ttk.Label(head, textvariable=self.title, style="Big.TLabel").pack(side="left")
        self.shortage = tk.StringVar()
        self.lbl_shortage = ttk.Label(head, textvariable=self.shortage, foreground="#c0392b")
        self.lbl_shortage.pack(side="right")

        f2, self.t_bom = make_tree(right, [
            ("name", "名称", 190, "w"),
            ("lcsc_pn", "立创编号", 90, "center"),
            ("mpn", "厂家料号", 120, "w"),
            ("package", "封装", 95, "w"),
            ("value", "值", 80, "w"),
            ("required_qty", "需求", 55, "e"),
            ("placed_qty", "已领", 55, "e"),
            ("on_hand", "库存", 55, "e"),
            ("gap", "缺口", 55, "e"),
            ("designators", "位号", 200, "w")], height=20)
        f2.pack(fill="both", expand=True)
        self.t_bom.tag_configure("short", background="#ffe3e3")
        self.t_bom.tag_configure("done", foreground="#888")
        pane.add(right, weight=3)

    def reload(self):
        data = call(self.con, server.list_projects, quiet=True)
        if data is None:
            return
        clear_tree(self.t_proj)
        for p in data["items"]:
            self.t_proj.insert("", "end", iid=str(p["id"]),
                               values=(p.get("name") or "", p.get("bom_lines") or 0,
                                       p.get("required_qty") or 0))
        if self._pid and str(self._pid) in self.t_proj.get_children():
            self.t_proj.selection_set(str(self._pid))
        elif self.t_proj.get_children():
            first = self.t_proj.get_children()[0]
            self.t_proj.selection_set(first)
            self._on_pick_project()

    def _on_pick_project(self, _event=None):
        sel = self.t_proj.selection()
        if not sel:
            return
        self._pid = int(sel[0])
        self.load_bom()

    def load_bom(self):
        if not self._pid:
            return
        rep = call(self.con, server.project_bom, match=(self._pid,), quiet=True)
        clear_tree(self.t_bom)
        self._lines = {}
        if not rep:
            return
        proj = rep.get("project") or {}
        self.title.set(proj.get("name") or "")
        total_gap = 0
        short_kinds = 0
        for line in rep.get("lines") or []:
            gap = (line.get("required_qty") or 0) - (line.get("placed_qty") or 0) \
                  - (line.get("on_hand") or 0)
            gap = max(0, gap)
            if gap:
                total_gap += gap
                short_kinds += 1
            self._lines[line["component_id"]] = {**line, "gap": gap}
            self.t_bom.insert("", "end", iid=str(line["component_id"]), values=(
                line.get("name") or "", line.get("lcsc_pn") or "", line.get("mpn") or "",
                line.get("package") or "", line.get("value") or "",
                line.get("required_qty") or 0, line.get("placed_qty") or 0,
                line.get("on_hand") or 0, gap or "",
                line.get("designators") or ""),
                tags=("short",) if gap else ("done",))
        if short_kinds:
            self.shortage.set(f"缺料 {short_kinds} 种 / {total_gap} 个")
        else:
            self.shortage.set("✓ 料齐")
        self.lbl_shortage.configure(foreground="#c0392b" if short_kinds else "#27ae60")

    # ------------------------------------------------------ 动作

    def import_bom(self):
        path = filedialog.askopenfilename(
            parent=self, title="选择 Altium 导出的 BOM",
            filetypes=[("Excel 工作簿", "*.xlsx *.xlsm"), ("全部文件", "*.*")])
        if not path:
            return
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as exc:
            messagebox.showerror("读不了文件", str(exc), parent=self)
            return

        default_name = os.path.splitext(os.path.basename(path))[0]
        name = ask_text(self, "导入 BOM", "项目名称(留空则用文件名):", default_name)
        if name is None:
            return
        name = name or default_name

        upload = {"filename": os.path.basename(path), "data": data}
        rep = call(self.con, server.bom_import, body={"project_name": name}, upload=upload,
                   parent=self)
        if rep is None:
            return
        warn = rep.get("warnings") or []
        msg = (f"项目:{rep['project_name']}\n"
               f"BOM {rep['line_count']} 行,新建/复用元件 {rep.get('components_created', 0)} 个\n"
               f"总需求 {rep.get('total_qty', 0)}\n"
               f"警告 {len(warn)} 条")
        if warn:
            msg += "\n\n" + "\n".join(f"· {w}" for w in warn[:10])
        messagebox.showinfo("导入完成", msg, parent=self)
        self._pid = rep["project_id"]
        self.app.refresh_all()

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
        cols = [("name", "名称"), ("lcsc_pn", "立创编号"), ("mpn", "厂家料号"),
                ("manufacturer", "厂家"), ("package", "封装"), ("value", "值"),
                ("required_qty", "需求"), ("placed_qty", "已领"), ("on_hand", "库存"),
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

class MovementsTab(ttk.Frame):
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

        f, self.tree = make_tree(self, [
            ("created_at", "时间", 145, "center"),
            ("kind", "动作", 60, "center"),
            ("component_name", "元件", 180, "w"),
            ("lcsc_pn", "立创编号", 90, "center"),
            ("qty", "数量", 60, "e"),
            ("location_code", "仓位", 95, "w"),
            ("to_location_code", "移到", 95, "w"),
            ("project_name", "项目", 130, "w"),
            ("operator", "操作人", 85, "center"),
            ("note", "备注", 220, "w")], height=22)
        f.pack(fill="both", expand=True)

    def reload(self):
        query = {"limit": self.limit.get()}
        label = self.kind.get()
        if label != "全部":
            query["kind"] = {v: k for k, v in KIND_LABEL.items()}[label]
        data = call(self.con, server.list_movements, query=query, quiet=True)
        if data is None:
            return
        clear_tree(self.tree)
        for mv in data["items"]:
            self.tree.insert("", "end", values=(
                (mv.get("created_at") or "")[:19],
                KIND_LABEL.get(mv.get("kind"), mv.get("kind")),
                mv.get("component_name") or "", mv.get("lcsc_pn") or "",
                mv.get("qty") or 0, mv.get("location_code") or "",
                mv.get("to_location_code") or "", mv.get("project_name") or "",
                mv.get("operator") or "", mv.get("note") or ""))


# --------------------------------------------------------------------- 仓位

class LocationsTab(ttk.Frame):
    def __init__(self, parent, app: App):
        super().__init__(parent, padding=8)
        self.app = app
        self.con = app.con

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 6))
        ttk.Button(bar, text="＋ 新建仓位", command=self.add).pack(side="left")
        ttk.Button(bar, text="删除仓位", command=self.delete).pack(side="left", padx=6)
        ttk.Button(bar, text="刷新", command=self.reload).pack(side="left")
        ttk.Label(bar, text="仓位编码随意,例如 A-01-02 表示 A柜-01层-02格;"
                            "填单时写一个不存在的编码会自动创建。",
                  style="Dim.TLabel").pack(side="left", padx=12)

        f, self.tree = make_tree(self, [
            ("code", "仓位编码", 200, "w"),
            ("kinds", "存放元件数", 100, "e"),
            ("qty", "库存合计", 100, "e"),
            ("note", "备注", 380, "w")], height=22)
        f.pack(fill="both", expand=True)

    def reload(self):
        rows = self.con.execute(
            """SELECT l.id, l.code, l.note,
                      (SELECT COUNT(*) FROM stock s WHERE s.location_id=l.id AND s.qty<>0) AS kinds,
                      (SELECT COALESCE(SUM(s.qty),0) FROM stock s WHERE s.location_id=l.id) AS qty
               FROM location l ORDER BY l.code""").fetchall()
        clear_tree(self.tree)
        for r in rows:
            self.tree.insert("", "end", iid=str(r["id"]),
                             values=(r["code"], r["kinds"], r["qty"], r["note"] or ""))

    def add(self):
        code = ask_text(self, "新建仓位", "仓位编码:", "")
        if not code:
            return
        if call(self.con, server.create_location, body={"code": code}, parent=self):
            self.app.refresh_all()

    def delete(self):
        sel = self.tree.selection()
        if not sel:
            return
        vals = self.tree.item(sel[0], "values")
        if not messagebox.askyesno("删除仓位", f"确定删除仓位「{vals[0]}」吗?", parent=self):
            return
        if call(self.con, server.delete_location, match=(int(sel[0]),), parent=self):
            self.app.refresh_all()


# --------------------------------------------------------------------- 弹窗

class ComponentDialog(tk.Toplevel):
    """新增 / 编辑元件。"""

    FIELDS = [
        ("name", "名称 *", 34),
        ("lcsc_pn", "立创编号", 34),
        ("mpn", "厂家料号", 34),
        ("manufacturer", "厂家", 34),
        ("category", "品类", 34),
        ("package", "封装", 34),
        ("value", "值", 34),
        ("unit", "单位", 34),
        ("min_stock", "最低库存", 34),
        ("datasheet_url", "数据手册链接", 34),
        ("product_url", "商品链接", 34),
    ]

    def __init__(self, parent, app: App, cid):
        super().__init__(parent)
        self.app = app
        self.con = app.con
        self.cid = cid
        self.done = False
        self.vars = {}

        self.title("编辑元件" if cid else "新增元件")
        self.transient(parent)
        self.resizable(False, False)

        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)

        existing = {}
        if cid:
            existing = call(self.con, server.get_component, match=(cid,), quiet=True) or {}

        for i, (key, label, width) in enumerate(self.FIELDS):
            ttk.Label(body, text=label).grid(row=i, column=0, sticky="e", padx=(0, 8), pady=3)
            var = tk.StringVar(value="" if existing.get(key) is None else str(existing.get(key, "")))
            self.vars[key] = var
            if key == "category":
                meta = call(self.con, server.meta, quiet=True) or {}
                w = ttk.Combobox(body, textvariable=var, width=width - 2,
                                 values=list(meta.get("categories") or []))
            else:
                w = ttk.Entry(body, textvariable=var, width=width)
            w.grid(row=i, column=1, sticky="w", pady=3)

        r = len(self.FIELDS)
        ttk.Label(body, text="备注").grid(row=r, column=0, sticky="ne", padx=(0, 8), pady=3)
        self.note = tk.Text(body, width=34, height=4, font=FONT)
        self.note.grid(row=r, column=1, sticky="w", pady=3)
        self.note.insert("1.0", existing.get("note") or "")

        ttk.Label(body, text="立创编号是识别主键;留空则用厂家料号。",
                  style="Dim.TLabel").grid(row=r + 1, column=1, sticky="w", pady=(0, 6))

        btns = ttk.Frame(body)
        btns.grid(row=r + 2, column=0, columnspan=2, pady=(6, 0))
        ttk.Button(btns, text="保存", command=self.save, width=12).grid(row=0, column=0, padx=4)
        ttk.Button(btns, text="取消", command=self.destroy, width=12).grid(row=0, column=1, padx=4)

        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()
        self.vars["name"].get()  # 触发一下,避免首个输入框未聚焦时的怪现象
        self.focus_set()
        self._center(parent)

    def _center(self, parent):
        self.update_idletasks()
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
        w, h = self.winfo_width(), self.winfo_height()
        self.geometry(f"+{px + (pw - w) // 2}+{py + (ph - h) // 3}")

    def save(self):
        body = {k: v.get().strip() for k, v in self.vars.items()}
        body["note"] = self.note.get("1.0", "end").strip()
        if not body.get("name"):
            messagebox.showinfo("提示", "名称必填。", parent=self)
            return
        try:
            body["min_stock"] = int(body.get("min_stock") or 0)
        except ValueError:
            messagebox.showinfo("提示", "最低库存要填整数。", parent=self)
            return

        if self.cid:
            res = call(self.con, server.update_component, body=body, match=(self.cid,), parent=self)
        else:
            res = call(self.con, server.create_component, body=body, parent=self)
        if res is None:
            return
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
