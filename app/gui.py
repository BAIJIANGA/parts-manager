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
from tkinter import filedialog, messagebox, ttk

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import db          # noqa: E402
import server      # noqa: E402  ← 复用全部业务逻辑
import values      # noqa: E402  ← 值区间筛选要把 1k 这种写法解析成数值

DEFAULT_DB = os.path.join(server.PROJECT_ROOT, "data", "parts.db")
ICON_PATH = os.path.join(server.PROJECT_ROOT, "app", "static", "app.ico")
BACKUP_DIR = os.path.join(server.PROJECT_ROOT, "data", "backups")

KIND_LABEL = {"IN": "入库", "OUT": "出库", "ADJUST": "盘点", "TRANSFER": "移库"}
# 查重的三档依据。前两档是硬证据,第三档只是可疑 —— 界面上要能看出这个区别,
# 免得把「值封装一样」也当成「肯定是同一个东西」直接合掉。
REASON_LABEL = {"mpn": "料号相同", "name": "名称相同", "vf": "值+封装相同"}
STATE_LABEL = dict(server.STATE_LABEL)   # 跟后端共用一份口径,别各写一份
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


def make_tree(parent, columns, height=14, show="headings"):
    """columns: [(key, 标题, 宽度, 对齐), ...]  返回 (frame, tree)

    默认 show="headings" —— 不显示 #0 树列,也就没有展开三角。这个界面里
    绝大多数列表都是平的:分类用卡片进二级页,列表本身不需要折叠。

    只有「按 BOM 出库」那张表例外,它传 show="tree headings":一条 BOM 需求
    下面要挂几颗能凑它的库存料,必须能折叠,否则 19 行需求 × 每行几颗候选
    会摊成一张看不出层次的几十行大表。
    """
    frame = ttk.Frame(parent)
    keys = [c[0] for c in columns]
    tree = ttk.Treeview(frame, columns=keys, height=height, show=show)
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
        self.bind_all("<Control-z>", lambda _e: self.undo_last())
        self.bind_all("<F5>", lambda _e: self.refresh_all())

    def quick_in(self):
        dlg = QuickInDialog(self, self)
        self.wait_window(dlg)
        if dlg.done:
            self.refresh_all()

    def batch_in(self):
        dlg = BatchInDialog(self, self)
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
            f"缺货 {s['out']} 种   偏低 {s['low']} 种   项目 {s['projects']} 个"
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
        # 「快速入库」放在最显眼的位置:收货那一刻就该把东西记下来,
        # 而不是等攒了一堆之后再补录 —— 补录是这类系统最常见的死法
        ttk.Button(head, text="⚡ 快速入库", command=self.quick_in).pack(side="right")
        ttk.Button(head, text="📋 批量入库", command=self.batch_in).pack(side="right", padx=6)
        ttk.Button(head, text="🔍 查重", command=self.app.dedupe).pack(side="right", padx=6)
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
        ttk.Label(sbox, text="名称 / 立创编号 / 料号 / 封装 / 值 / 丝印 / 参数 / 备注",
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

        # 筛选栏。回答的是「我这个大类里到底有没有某一档的东西」——
        # 比如「0805 的电阻里有没有 1k~10k 的」。比的是解析出来的数值,不是字符串。
        bar = ttk.Frame(self.page_cat)
        bar.pack(fill="x", pady=(0, 6))
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

        pane = ttk.Panedwindow(self.page_cat, orient="vertical")
        pane.pack(fill="both", expand=True)

        top = ttk.Frame(pane)
        # 这里是平表,不是树 —— 没有 #0 列,也就没有展开三角
        frame, self.tree = make_tree(top, [
            ("name", "名称", 190, "w", True),
            ("lcsc_pn", "立创编号", 88, "center"),
            ("mpn", "厂家料号", 112, "w", True),
            # 厂家换成丝印:按「哪些字段真会被填」的统计,厂家只有 4/8 的表会记,
            # 而丝印是拆机料唯一能用来找回身份的东西,应该一眼看得到
            ("marking", "丝印", 78, "center"),
            ("package", "封装", 88, "w", True),
            ("value", "值", 68, "w"),
            ("on_hand", "现有", 55, "e"),
            ("min_stock", "安全", 50, "e"),
            ("required", "需求", 55, "e"),
            ("deficit", "缺口", 55, "e"),
            ("on_order", "在途", 55, "e"),
            ("to_order", "该买", 55, "e"),
            ("state", "状态", 55, "center"),
            ("note", "备注", 130, "w", True),
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
        self.clear_filters(redraw=False)
        self.load_category()

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
        name = self.current_category
        if not name:
            return
        query = {"category": name, "stocked": "1", "limit": "0", "sort": "value"}
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
        self._set_facet(self.cmb_pkg, facets.get("packages") or [], self.f_pkg)
        self._set_facet(self.cmb_unit, facets.get("units") or [], self.f_unit)

        glyph, color = CATEGORY_STYLE.get(name, DEFAULT_CAT_STYLE)
        self.cat_badge.configure(text=glyph, bg=color if rows else DIM_BADGE)
        self.cat_title.set(name)
        summary = self._filter_summary()
        if rows:
            self.cat_count.set(f"共 {len(rows)} 种" + (f"   {summary}" if summary else ""))
            self.f_hint.set("")
        else:
            # 空态要分清「这个大类本来就没货」和「是你筛掉了」
            self.cat_count.set(summary if summary else "暂无库存元件")
        self._swap(self.page_cat)
        self._fill(rows)

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
        # 需求/缺口/在途/该买 为 0 时留空 —— 一列 0 会把真正要注意的数字淹掉
        self.tree.insert("", "end", iid=str(it["id"]), values=(
            it.get("name") or "", it.get("lcsc_pn") or "", it.get("mpn") or "",
            it.get("marking") or "", it.get("package") or "",
            it.get("value") or "", it.get("on_hand") or 0, it.get("min_stock") or 0,
            it.get("required") or "", it.get("deficit") or "",
            it.get("on_order") or "", it.get("to_order") or "",
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

    def quick_in(self):
        dlg = QuickInDialog(self, self.app)
        self.app.wait_window(dlg)
        if dlg.done:
            self.app.refresh_all()

    def batch_in(self):
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

def category_options(con):
    """品类下拉的选项。统一从后端 meta 拿,免得界面上再维护第二份清单。"""
    meta = call(con, server.meta, quiet=True) or {}
    return list(meta.get("categories") or []) or ["其他"]


class CategoryDialog(tk.Toplevel):
    """挑一个品类。双击 BOM 行单独改的时候用。"""

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
        # 故意**不**设成 readonly:下拉只是建议。推断不出来的品类(光耦、
        # 传感器模块……)必须能自己打进去,否则用户只能挑一个最接近的凑合 ——
        # 那等于把「猜错了」换成「被迫选了个不准确的」
        cb = ttk.Combobox(body, textvariable=self.var, values=opts, width=24)
        cb.grid(row=1, column=0, sticky="w", pady=(6, 14))
        btns = ttk.Frame(body)
        btns.grid(row=2, column=0, sticky="e")
        ttk.Button(btns, text="取消", command=self.destroy, width=10).pack(side="right")
        ttk.Button(btns, text="确定", command=self.ok, width=10).pack(side="right", padx=6)
        self.bind("<Return>", lambda _e: self.ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()
        cb.focus_set()

    def ok(self):
        self.value = self.var.get().strip() or "其他"
        self.destroy()


def ask_category(parent, app: App, current="", options=None):
    d = CategoryDialog(parent, app, current, options)
    parent.winfo_toplevel().wait_window(d)
    return d.value


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
        ("lcsc_pn", "立创编号", 82, "center"),
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
        ("lcsc_pn", "立创编号", 86, "center"),
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
            ("lcsc_pn", "立创编号", 86, "center"),
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


class BomReceivePane(BomPaneBase):
    """按 BOM 收料:一箱货到了,勾掉收到了的,一次全收进来。

    数量默认填 **BOM 的总需求** —— 这是「按 BOM 收货」该有的默认值。让人
    每行自己算「还差几个」是在把库房的账推给记性,而记性会出错。

    品类单独占一列,是因为收料时最容易出错的恰恰是「这个看起来像电阻的
    东西到底是不是电阻」:值、封装都对不上时,品类是最后一道人工检查。
    """

    SUBMIT = "✓ 勾选的全部入库"

    COLS = [
        ("pick", "选", 34, "center", False),
        ("name", "名称", 190, "w", True),
        ("category", "品类", 82, "w", False),
        ("value", "值", 68, "w", False),
        ("package", "封装", 88, "w", False),
        ("designators", "位号", 92, "w", False),
        ("need", "BOM需求", 62, "e", False),
        ("on_hand", "现有", 48, "e", False),
        ("qty", "本次入库", 66, "e", False),
    ]

    def build_tree(self):
        f, t = make_tree(self, self.COLS, height=13)
        t.tag_configure("done", foreground="#1e7a34")
        t.tag_configure("short", background="#fff8e6")
        return f, t

    def reload(self):
        data = call(self.con, server.project_bom, match=(str(self.project_id),),
                    quiet=True)
        if data is None:
            return
        self.lines = list(data.get("lines") or [])
        bids = {l["bom_id"] for l in self.lines}
        # 数量默认取 BOM 总需求;已经手改过的保留。刷新往往是别处顺手触发的,
        # 把用户填好的数换回默认值,他会以为自己刚才看错了
        keep = self.qty
        self.qty = {}
        for l in self.lines:
            bid = l["bom_id"]
            self.qty[bid] = int(keep.get(bid, l["need"]))
        self.picked &= bids
        self.render()

    def render(self):
        clear_tree(self.tree)
        self._idx = {}
        kw = self.q.get().strip().lower()
        shown = 0
        for l in self.lines:
            if kw and kw not in " ".join(
                    str(l.get(k) or "") for k in
                    ("name", "category", "value", "package", "designators")).lower():
                continue
            shown += 1
            bid = l["bom_id"]
            self._idx[bid] = l
            on = bid in self.picked
            self.tree.insert("", "end", iid=str(bid), values=(
                CHECK_ON if on else CHECK_OFF, l.get("name") or "",
                l.get("category") or "未分类", l.get("value") or "",
                l.get("package") or "", l.get("designators") or "",
                l.get("need") or 0, l.get("on_hand") or 0,
                self.qty.get(bid, 0)),
                tags=("done" if l.get("gap") == 0 else "short",))
        n = len(self.picked)
        total = sum(int(self.qty.get(b, 0) or 0) for b in self.picked)
        self.hint.set(f"显示 {shown} / {len(self.lines)} 行;" + self.moved_text(n, total))
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
                       f"「{l.get('name')}」这次入库多少?(BOM 需求 {l.get('need')})",
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
        if not self.project_id:
            messagebox.showinfo("提示", "先在左边选一个项目。", parent=self)
            return
        items = [{"component_id": self._idx[b]["component_id"], "qty": int(q),
                  "bom_id": b, "note": self.note.get().strip()}
                 for b, q in self.qty.items()
                 if b in self.picked and int(q or 0) > 0 and b in self._idx]
        if not items:
            messagebox.showinfo(
                "提示", "还没有勾选要入库的行。\n"
                        "在「选」那一列点一下就能勾上;数量默认是 BOM 需求,\n"
                        "双击一行可以改。", parent=self)
            return
        total = sum(i["qty"] for i in items)
        if not messagebox.askyesno(
                "确认入库",
                f"要把勾选的 {len(items)} 行、共 {total} 个收进来吗?\n\n"
                + "\n".join(f"  {self._idx[i['bom_id']].get('name')}  ×{i['qty']}"
                            for i in items[:10])
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
        self.picked.clear()
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
    填个数,父行上的「还需要」立刻跟着减,下一颗该出几个一眼就能看出来。
    """

    SUBMIT = "✓ 按这个分配出库"

    COLS = [
        ("pick", "选", 34, "center", False),
        ("category", "品类", 76, "w", False),
        ("value", "值", 68, "w", False),
        ("package", "封装", 86, "w", False),
        ("on_hand", "库存", 50, "e", False),
        ("qty", "本次出库", 68, "e", False),
        ("left", "还需要", 58, "e", False),
        ("match", "像在哪儿", 168, "w", True),
    ]

    def build_tree(self):
        f, t = make_tree(self, self.COLS, height=13, show="tree headings")
        t.heading("#0", text="BOM 需求 ↓ 能凑它的库存料")
        t.column("#0", width=196, anchor="w", stretch=True)
        t.tag_configure("line", background="#eef4fb")
        t.tag_configure("covered", foreground="#1e7a34")
        t.tag_configure("own", foreground="#1e7a34")
        t.tag_configure("empty", foreground="#999")
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
        self.lines = list(data.get("lines") or [])
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
        for l in self.lines:
            bid = l["bom_id"]
            if kw and kw not in " ".join(
                    str(l.get(k) or "") for k in
                    ("name", "category", "value", "package", "designators")).lower():
                continue
            shown += 1
            rem, used = self.remain(bid), self.used(bid)
            need = int(l.get("need") or 0)
            mark = "✓ 齐了" if rem == 0 else f"还差 {rem}"
            text = f"{l.get('name') or ''}  ×{need}   [{mark}]"
            tags = ["line"]
            if rem == 0 and need:
                tags.append("covered")
            self.tree.insert("", "end", iid=str(bid), text=text, open=(bid in self._open),
                             values=("", l.get("category") or "未分类",
                                     l.get("value") or "", l.get("package") or "",
                                     l.get("available") or 0, used, rem, ""),
                             tags=tuple(tags))
            cands = l.get("candidates") or []
            if not cands:
                # 没有候选不是「没数据」,是「这颗料库里一个都没有」——
                # 得说出来,否则展开是空的会让人以为界面坏了。
                # 第一个参数是父行 id:挂在需求下面,不然它会变成一条跟
                # 需求平级的孤立行,看着像另一条 BOM。
                self.tree.insert(str(bid), "end", iid=f"{bid}:none", text="",
                                 values=("", "", "", "", "", "", rem,
                                         "库存里没有能凑它的料,得先入库或设替代料"),
                                 tags=("empty",))
                continue
            for c in cands:
                key = (bid, c["id"])
                self._idx[key] = c
                on = key in self.alloc
                tags = ("own",) if c.get("own") else ()
                self.tree.insert(str(bid), "end", iid=self.child_iid(bid, c["id"]),
                                 text="", values=(
                                     CHECK_ON if on else CHECK_OFF,
                                     c.get("category") or "未分类",
                                     c.get("value") or "", c.get("package") or "",
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
                      + self.moved_text(n, total) + (f",补齐 {done} 条" if done else ""))
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
                       f"这条 BOM 还需要 {self.remain(bid)})",
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
            ("lcsc_pn", "立创编号", 86, "center"),
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
                "勾选 + 一键入库,数量默认取 BOM 需求,品类就在表里。"
                if self.action == "IN" else
                "一条 BOM 需求可以由几颗库存料凑齐;勾一颗、填个数,还需要几个会跟着减。")
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

        f, self.tree = make_tree(self, [
            ("created_at", "时间", 145, "center"),
            ("kind", "动作", 96, "center"),
            ("component_name", "元件", 190, "w", True),
            ("lcsc_pn", "立创编号", 88, "center"),
            ("qty", "数量", 52, "e"),
            ("location_code", "仓位", 86, "w"),
            ("project_name", "项目", 130, "w", True),
            ("operator", "操作人", 76, "center"),
            ("note", "备注", 190, "w", True)], height=20)
        f.pack(fill="both", expand=True)
        # 撤销过的淡掉但不隐藏 —— 历史要看得见,只是别再当它是有效的
        self.tree.tag_configure("voided", foreground="#95a5a6")
        self.tree.tag_configure("reversal", foreground="#2471a3")

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
        clear_tree(self.tree)
        for mv in items:
            # 「动作」列不是多余的:撤销一笔出库补的是入库、撤销一笔入库补的是出库,
            # 所以这一页里混着正常流水和反向流水,得一眼分得清。
            kind_txt = KIND_LABEL.get(mv.get("kind"), mv.get("kind"))
            if mv.get("voided"):
                kind_txt += "(已撤销)"
                tags = ("voided",)
            elif mv.get("void_of"):
                kind_txt += "·撤销"
                tags = ("reversal",)
            else:
                tags = ()
            self.tree.insert("", "end", iid=str(mv["id"]), values=(
                (mv.get("created_at") or "")[:19], kind_txt,
                mv.get("component_name") or "",
                mv.get("lcsc_pn") or "", mv.get("qty") or 0,
                mv.get("location_code") or "", mv.get("project_name") or "—",
                mv.get("operator") or "", mv.get("note") or ""), tags=tags)
        voided = sum(1 for mv in items if mv.get("voided"))
        total = sum(int(mv.get("qty") or 0) for mv in items if not mv.get("voided"))
        label = dict(self.ACTIONS)[self.action]
        hidden = (f"另有 {idle} 个项目还没有{'入库' if self.action == 'IN' else '出库'}"
                  f"记录,没列进下拉。" if idle else "")
        self.summary.set(
            f"{label} {len(items)} 条" + (f"(其中已撤销 {voided} 条)" if voided else "")
            + f",有效合计 {total} 个。" + hidden
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
        vals = self.tree.item(sel[0], "values")
        label = (f"{vals[1]}  {vals[2]}  {vals[4]} 个  {vals[5]}\n{vals[0]}")
        if vals[8]:
            label += f"\n{vals[8]}"
        undo_movement(self, self.app, int(sel[0]), label)


# --------------------------------------------------------------------- 项目 BOM

class ProjectsTab(ttk.Frame):
    def __init__(self, parent, app: App):
        super().__init__(parent, padding=8)
        self.app = app
        self.con = app.con
        self._pid = None
        self._lines = {}
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
        ttk.Label(tools, text="双击一行可直接改用量/损耗/可选/免点。",
                  style="Dim.TLabel").pack(side="left", padx=8)

        f2, self.t_bom = make_tree(detail, [
            ("name", "名称", 150, "w", True),
            ("lcsc_pn", "立创编号", 80, "center"),
            ("value", "值", 62, "w"),
            ("package", "封装", 80, "w"),
            ("per_board", "单块", 42, "e"),
            ("attrition", "损耗", 44, "e"),
            ("need", "需求", 48, "e"),
            ("on_hand", "现有", 48, "e"),
            ("sub_qty", "替代", 44, "e"),
            ("gap", "缺口", 48, "e"),
            ("flag", "标记", 56, "center"),
            ("designators", "位号", 140, "w", True)], height=16)
        f2.pack(fill="both", expand=True)
        self.t_bom.tag_configure("short", background="#ffe3e3")
        self.t_bom.tag_configure("done", foreground="#888")
        self.t_bom.bind("<Double-1>", lambda _e: self.edit_line())
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
            # 而不是留着上一次的项目 id 继续往旧项目里记账
            self._pid = None
        # 会走到这里,往往是「刚开完单 / 刚撤销了一笔」—— 开单表上的「现有」
        # 和记录表都得跟着走。不能指望选中事件:选中的项目没变时它不发。
        self._sync_panes(force=True)

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

    def similar_for_line(self):
        """拿 BOM 明细里选中那一行的值+封装,去库存里找相似。"""
        sel = self.t_bom.selection()
        if not sel:
            messagebox.showinfo("提示", "先在 BOM 明细里选一行。", parent=self)
            return
        bid = int(sel[0])          # BOM 明细的 iid 就是 bom_id
        vals = self.t_bom.item(sel[0], "values")
        # 列序:名称 / 立创编号 / 值 / 封装 / 单块 / 损耗 / 需求 / 现有 / 替代 / 缺口 / 标记 / 位号
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
                if force:
                    p.refresh_panes()
                else:
                    p.set_project(self._pid)

    def load_bom(self):
        if not self._pid:
            return
        rep = call(self.con, server.project_bom, match=(self._pid,), quiet=True)
        clear_tree(self.t_bom)
        self._lines = {}
        self.report = rep or {}
        if not rep:
            return
        proj = rep.get("project") or {}
        self.title.set(f"{proj.get('name') or ''}   计划 {rep['boards']} 块   "
                       f"能造 {rep['can_build']} 块")
        for line in rep.get("lines") or []:
            self._lines[line["bom_id"]] = line
            flags = []
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
        # 先解析一遍再让人确认。直接导进去再改就晚了:那一刻品类已经落库,
        # 而且人根本不知道哪些行是猜的。
        preview = call(self.con, server.bom_preview, upload=upload, parent=self)
        if not preview:
            return
        default_name = os.path.splitext(os.path.basename(path))[0]
        BomReviewDialog(self, self.app, preview, default_name=default_name,
                        on_confirm=lambda name, cats: self._import_confirmed(
                            upload, name, cats))

    def _import_confirmed(self, upload, name, categories):
        """复核完了,带着人工改过的品类真正落库。"""
        rep = call(self.con, server.bom_import,
                   body={"project_name": name, "categories": categories},
                   upload=upload, parent=self)
        if rep is None:
            return
        warn = rep.get("warnings") or []
        # 报告里的字段名是 bom_lines;这里曾经写成 line_count,导入成功后必然 KeyError。
        # 用 get 兜一下,免得以后再改字段名又炸一次。
        lines = rep.get("bom_lines", rep.get("line_count", 0))
        msg = (f"项目:{rep['project_name']}\n"
               f"BOM {lines} 行,新建/复用元件 {rep.get('components_created', 0)} 个\n"
               f"总需求 {rep.get('total_qty', 0)}\n"
               f"警告 {len(warn)} 条")
        if categories:
            msg += f"\n人工改过品类的 {len(categories)} 行已按你改的落库"
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
        ttk.Button(bar, text="↶ 撤销选中的记录", command=self.undo).pack(side="right")

        f, self.tree = make_tree(self, [
            ("created_at", "时间", 145, "center"),
            ("kind", "动作", 100, "center"),
            ("component_name", "元件", 175, "w"),
            ("lcsc_pn", "立创编号", 88, "center"),
            ("qty", "数量", 55, "e"),
            ("location_code", "仓位", 90, "w"),
            ("to_location_code", "移到", 90, "w"),
            ("project_name", "项目", 120, "w"),
            ("operator", "操作人", 80, "center"),
            ("note", "备注", 200, "w")], height=22)
        f.pack(fill="both", expand=True)
        # 撤销过的记录淡掉但不隐藏 —— 历史要看得见,只是别再当它是有效的
        self.tree.tag_configure("voided", foreground="#95a5a6")
        self.tree.tag_configure("reversal", foreground="#2471a3")

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
            kind_txt = KIND_LABEL.get(mv.get("kind"), mv.get("kind"))
            if mv.get("voided"):
                kind_txt += "(已撤销)"
                tags = ("voided",)
            elif mv.get("void_of"):
                kind_txt += "·撤销"
                tags = ("reversal",)
            else:
                tags = ()
            self.tree.insert("", "end", iid=str(mv["id"]), values=(
                (mv.get("created_at") or "")[:19], kind_txt,
                mv.get("component_name") or "", mv.get("lcsc_pn") or "",
                mv.get("qty") or 0, mv.get("location_code") or "",
                mv.get("to_location_code") or "", mv.get("project_name") or "",
                mv.get("operator") or "", mv.get("note") or ""), tags=tags)

    def undo(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "先点一行要撤销的记录。", parent=self)
            return
        mid = int(sel[0])
        vals = self.tree.item(sel[0], "values")
        label = f"{vals[1]}  {vals[2]}  {vals[4]} 个  {vals[5]}\n{vals[0]}"
        if vals[9]:
            label += f"\n{vals[9]}"
        undo_movement(self, self.app, mid, label)


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
        cols = (("kinds", "存放元件", 80, "e"), ("qty", "数量", 70, "e"),
                ("structural", "类型", 60, "center"), ("note", "备注", 150, "w"))
        # 这一处必须用 show="tree headings"(要 #0 树列来显示层级),
        # 而 make_tree 是按平表写的,所以这里自己搭。
        tree = ttk.Treeview(frame, columns=[c[0] for c in cols], height=22,
                            show="tree headings")
        tree.heading("#0", text="仓位")
        tree.column("#0", width=200, anchor="w", stretch=False)
        for key, title, width, anchor in cols:
            tree.heading(key, text=title)
            tree.column(key, width=width, anchor=anchor, stretch=(key == "note"))
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
            ("lcsc_pn", "立创编号", 90, "center"),
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
            ("lcsc_pn", "立创编号", 92, "center"),
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

    def __init__(self, parent, app: App):
        super().__init__(parent)
        self.app = app
        self.con = app.con
        self.done = False
        self.rows = []

        self.title("批量入库 —— 粘贴一整张单子")
        self.transient(parent)
        self.geometry("960x700")
        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text="批量入库", style="Big.TLabel").pack(anchor="w")
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
            body = {"kind": "IN", "component_id": cid, "qty": r["qty"]}
            if loc:
                body["location"] = loc
            if call(self.con, server.stock_move, body=body, parent=self) is None:
                failed.append(r["text"])
                continue
            stocked += 1
            total += r["qty"]
        self.done = True
        self.app.set_status(
            f"批量入库完成:{stocked} 种 / {total} 个(其中新建 {created} 个元件)", 8)
        msg = (f"入库 {stocked} 种,共 {total} 个。\n"
               + (f"其中新建了 {created} 个元件(只填了名称,归到「未分类」)。\n"
                  if created else ""))
        if blocked:
            msg += f"\n跳过 {len(blocked)} 行(多匹配未指定)。\n"
        if failed:
            msg += "\n失败:" + "、".join(failed[:8])
        messagebox.showinfo("批量入库", msg, parent=self)
        self.app.refresh_all()
        if not failed:
            self.destroy()


class DedupeDialog(tk.Toplevel):
    """查重与合并 —— 收拾反复导入 BOM 长出来的重复料。

    重复料是这类工具最难躲开的数据腐烂:同一颗电阻,一期 BOM 带着立创编号,
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
            ("lcsc_pn", "立创编号", 86, "center"),
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
            ("lcsc_pn", "立创编号", 95, "center"),
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
    """从库里挑一个元件。搜索支持名称 / 立创编号 / 厂家料号 / 值 / 封装。"""

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
            ("lcsc_pn", "立创编号", 95, "center"),
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
        ttk.Label(body, text="搜名称 / 立创编号 / 厂家料号 / 值 / 封装 / 丝印;"
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
            ("lcsc_pn", "立创编号", 88, "center"),
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
        ("lcsc_pn", "立创编号", None),
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

    def __init__(self, parent, app: App, cid):
        super().__init__(parent)
        self.app = app
        self.con = app.con
        self.cid = cid
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
                var = tk.StringVar(value=str(existing.get(key) or ""))
                w = ttk.Combobox(body, textvariable=var, width=32,
                                 values=list(meta.get("categories") or []))
            elif kind == "supplier":
                var = tk.StringVar(value=str(existing.get(key) or ""))
                w = ttk.Combobox(body, textvariable=var, width=32, values=sup)
            else:
                var = tk.StringVar(value="" if existing.get(key) is None
                                   else str(existing.get(key, "")))
                w = ttk.Entry(body, textvariable=var, width=34)
            self.vars[key] = var
            w.grid(row=i, column=1, sticky="w", pady=3)

        r = len(self.FIELDS)

        # 品类参数(耐压 / 精度 / 功率 …)。存成 JSON,所以加新参数不用改表结构。
        ttk.Label(body, text="参数").grid(row=r, column=0, sticky="ne", padx=(0, 8), pady=3)
        self.params = tk.Text(body, width=34, height=4, font=FONT)
        self.params.grid(row=r, column=1, sticky="w", pady=3)
        saved = existing.get("params") or {}
        if isinstance(saved, dict):
            self.params.insert("1.0", "\n".join(f"{k}={v}" for k, v in saved.items()))
        ttk.Label(body, text="一行一个,写成  耐压=50V ,没参数就留空。",
                  style="Dim.TLabel").grid(row=r + 1, column=1, sticky="w")

        ttk.Label(body, text="备注").grid(row=r + 2, column=0, sticky="ne", padx=(0, 8), pady=3)
        self.note = tk.Text(body, width=34, height=3, font=FONT)
        self.note.grid(row=r + 2, column=1, sticky="w", pady=3)
        self.note.insert("1.0", existing.get("note") or "")

        ttk.Label(body, text="立创编号是识别主键;留空则用厂家料号。",
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

    def _parse_params(self):
        out = {}
        for line in self.params.get("1.0", "end").splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k:
                out[k] = v
        return out

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
