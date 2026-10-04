# -*- coding: utf-8 -*-
"""把会拦住自动化的模态对话框全部打桩。

**为什么必须有这个文件:**

验证脚本如果在真实桌面上弹出一个模态框(最典型的是「数量要填非负数」这种
校验提示),它就会**卡在那里等人点**,整个验证停住。用户明确要求:凡是在
工作区内能跑完的验证,一律全自动,不许让他手动操作 —— 他只该在「要动工作区
以外的东西」时才被打扰。

`build\\test_gui.py` 自己有一套打桩(几十处 `gui.messagebox = box`),所以它
一直是全自动的;出问题的是那些**临时写的探针** —— 即写即用,没人强制打桩。
与其指望每个写探针的人记得这件事,不如给一个现成的东西:

    import _stub_dialogs                # 和本文件同目录
    rec = _stub_dialogs.silence()       # 早点调,构造窗口之前
    ...                                 # 之后无论怎么点,都不会弹出要人点的框
    print(rec.infos)                    # 顺手还能断言「它到底提示过什么」

`silence()` 同时改两处,这是刻意的:
  * `tkinter.messagebox` 上的**模块属性** —— 管住那些 `from tkinter import
    messagebox` 的地方(直接改模块属性,不依赖调用方怎么 import);
  * `app.gui` 模块里引用到的 `messagebox` / `colorchooser` —— 管住界面代码
    自己持有的那份引用。
两处都改,才不会出现「我以为打桩了,它还是弹出来了」。

这不是测试专用的小把戏:**只要脚本会构造 Tk 窗口,就该先调它**,包括给成品包
做的实地探针。

------------------------------------------------------------------ 它挡不住什么

**这个模块只在当前进程里生效**,所以下面两件事它不是「打了桩就没事」,而是
**根本不许做**:

1. **另起一个进程去验证。** 比如 `subprocess` 起一个 pythonw、或直接运行
   `dist\\元器件物料管理\\元器件物料管理.exe` —— 那个新进程里没有这里的打桩,
   它的校验框会照样弹到用户桌面上,而且**卡在那里等人点**。要验界面就在
   **本进程**里 `import app.gui` 然后构造窗口;成品包只允许跑不碰界面的
   HTTP / 数据库层面的探针。
2. **`tk_popup` / `post` 弹出真菜单。** 打桩管不着它,而且 ttk 的弹出菜单会
   **留在用户屏幕上**直到被点掉。要验菜单就调「建菜单」那个函数,检查它的项、
   label 和 state(仓库自检里已经有这种写法,搜 `menu_labels`)。

一句话:**你能做的只有「本进程 + 打桩 + 不真弹」,做不到就别做,写进报告说明
哪一步跳过了、为什么。** 让用户去点一个框,是这套验证里最不该发生的事。
"""
import tkinter.messagebox as _tk_mb
import tkinter.simpledialog as _tk_sd

__all__ = ["Recorder", "silence"]

DEFAULT_COLOR = "#ff00ff"          # 洋红:占位色要丑得一眼认出来,别像真配色


class Recorder:
    """假装成人,但从不提问:该答「是」的答是,该提示的记下来。

    记下来是有用的 —— 「它到底弹过什么」经常正是断言想看的东西(比如「校验
    失败时有没有告诉用户」),只是不该让人去点。
    """

    def __init__(self, yes=True, values=None, color=DEFAULT_COLOR):
        self.yes = yes
        self.values = dict(values or {})
        self.color = color
        self.infos = []            # [(标题, 正文), ...]
        self.warnings = []
        self.errors = []
        self.asks = []             # 问过的问题(标题, 提示, 预设值)

    # ---------------------------------------------------------- 问是/否
    def askyesno(self, title=None, message=None, **kw):
        self.asks.append((title, message, None))
        return self.yes

    def askokcancel(self, title=None, message=None, **kw):
        self.asks.append((title, message, None))
        return self.yes

    def askretrycancel(self, title=None, message=None, **kw):
        self.asks.append((title, message, None))
        return self.yes

    def askyesnocancel(self, title=None, message=None, **kw):
        self.asks.append((title, message, None))
        return self.yes

    # ---------------------------------------------------------- 只提示
    def showinfo(self, title=None, message=None, **kw):
        self.infos.append((title, message))
        return "ok"

    def showwarning(self, title=None, message=None, **kw):
        self.warnings.append((title, message))
        return "ok"

    def showerror(self, title=None, message=None, **kw):
        self.errors.append((title, message))
        return "ok"

    # ---------------------------------------------------------- 问答式输入
    def askstring(self, title=None, prompt=None, initialvalue=None, **kw):
        self.asks.append((title, prompt, initialvalue))
        return self.values.get(title, initialvalue)

    def askinteger(self, title=None, prompt=None, initialvalue=None, **kw):
        self.asks.append((title, prompt, initialvalue))
        got = self.values.get(title, initialvalue)
        return None if got is None else int(got)

    def askfloat(self, title=None, prompt=None, initialvalue=None, **kw):
        self.asks.append((title, prompt, initialvalue))
        got = self.values.get(title, initialvalue)
        return None if got is None else float(got)


class _ColorChooser:
    """取色器:直接给一个固定颜色,不弹系统调色板。

    用户要的是「点色块就改色」的调色盘(见 issue #34),那种交互本来也不该
    每次都弹系统对话框 —— 探针里更不该。
    """

    def __init__(self, rec):
        self._rec = rec

    def askcolor(self, *a, **k):
        return _hex_to_rgb(self._rec.color), self._rec.color

    def Chooser(self, *a, **k):                    # noqa: N802 - 跟标准库同名
        return self.askcolor()


def _hex_to_rgb(s):
    s = (s or "").lstrip("#")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4)) if len(s) == 6 else None


def silence(gui=None, yes=True, values=None, color=DEFAULT_COLOR):
    """把对话框全部换成不会拦人的假货,返回记录器。

    gui 传 `app.gui` 模块(可选;传了就同时改它引用到的那份)。
    """
    rec = Recorder(yes=yes, values=values, color=color)

    # 1. 标准库模块属性:管住 `from tkinter import messagebox` 的各种写法
    for name in ("askyesno", "askokcancel", "askretrycancel", "askyesnocancel",
                 "showinfo", "showwarning", "showerror"):
        setattr(_tk_mb, name, getattr(rec, name))
    for name in ("askstring", "askinteger", "askfloat"):
        setattr(_tk_sd, name, getattr(rec, name))

    # 2. 界面模块自己持有的那份引用
    if gui is not None:
        try:
            gui.messagebox = rec
        except Exception:                          # noqa: BLE001
            pass
        try:
            gui.colorchooser = _ColorChooser(rec)
        except Exception:                          # noqa: BLE001
            pass
    return rec
