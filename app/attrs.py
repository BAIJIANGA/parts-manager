# -*- coding: utf-8 -*-
"""元件属性(参数)的命名建议、归一化和显示格式。

属性本身是**自由**的:用户想叫什么就叫什么 —— 存在 `component.params` 那个 JSON
列里(见 db.py),加新属性不用改表结构、不用迁移。这个模块只做两件事:

1. **给建议**。按品类列一份常见的属性名当候选,但**绝不限制输入**。
   推断不出来的东西(光耦、传感器模块)必须能自己写,否则用户只能挑一个最接近的
   凑合着用,那等于把「它猜错了」换成「我被迫选了个不准确的」。这一点和导入复核
   窗口的处理保持一致。

2. **管显示**。收料清单和出库分配树的列宽是逐个量过的(check_layout.py),
   给每个属性各开一列的话属性一多就把两张表挤坏,所以**合成一列**显示:
   只列值(「50V · ±5%」),完整的名=值留给详情面板。

名字归一化只做「首尾空白 + 大小写」这一层:`耐压 ` 和 `耐压` 是同一个。
但 `耐压` 和 `耐压值` 是**两个** —— 那是用户真起了两个名字,不该替他合并,
猜错等于把人家的命名默默抹掉。
"""
from __future__ import annotations

from typing import Any

# 按品类给的建议属性名。只是候选,不影响能填什么。
SUGGESTIONS: dict[str, list[str]] = {
    "电阻": ["精度", "功率", "温度系数", "耐压"],
    "电容": ["耐压", "精度", "温度系数", "介质", "容差"],
    "电感": ["额定电流", "饱和电流", "直流电阻", "精度"],
    "磁珠": ["额定电流", "直流电阻", "阻抗@100MHz"],
    "二极管": ["耐压", "电流", "正向压降", "封装功耗"],
    "发光二极管": ["颜色", "波长", "亮度", "正向压降", "电流"],
    "三极管/MOS": ["耐压", "电流", "功率", "导通电阻", "阈值电压"],
    "芯片/IC": ["工作电压", "频率", "位数", "接口", "温度范围"],
    "连接器": ["间距", "位数", "额定电流", "耐压"],
    "晶振": ["频率", "精度", "负载电容", "工作温度"],
    "开关": ["额定电流", "耐压", "行程", "寿命"],
    "保险丝": ["额定电流", "耐压", "熔断速度"],
    "电位器": ["阻值", "精度", "功率", "圈数"],
    "继电器": ["线圈电压", "触点电流", "触点形式", "耐压"],
    "测试点": ["直径", "颜色"],
    "电池": ["电压", "容量", "尺寸", "化学体系"],
    "扬声器": ["阻抗", "功率", "尺寸"],
    "电机": ["电压", "转速", "扭矩", "电流"],
}

# 品类认不出来时兜底的建议。这几样几乎任何元件都用得上。
GENERIC = ["耐压", "精度", "功率", "电流", "温度系数"]

# 出入库那一列用哪个分隔符。用间隔号而不是逗号 —— 值里本来就可能带逗号。
SEP = " · "


def _preferred_order() -> list[str]:
    """显示顺序:先常见的关键参数,没排上的按名字排在后面。

    属性名是用户自己起的,直接按字典序会把「耐压」排到「精度」后面,读起来别扭
    (而且 `db.dump_params` 存的时候就是排过序的,字典序会原样透出来)。
    顺序取自 GENERIC + 各品类的建议表,去重并保持第一次出现的次序,所以
    加品类建议时不用额外维护一份顺序表。
    """
    out: list[str] = []
    for name in GENERIC + [n for lst in SUGGESTIONS.values() for n in lst]:
        if name not in out:
            out.append(name)
    return out


_PREFERRED = _preferred_order()


def _sort_key(item):
    """按 _PREFERRED 里的位次排;不在表里的排在最后,同组内按名字。"""
    name = item[0]
    try:
        return (_PREFERRED.index(name), "")
    except ValueError:
        return (len(_PREFERRED), name)


def suggest(category: Any, extra: Any = None) -> list[str]:
    """该品类的建议属性名 + 库里已经在用的名字。

    `extra` 是「这个品类下用户已经用过的属性名」(由 server.meta 现算出来的),
    排在建议前面 —— 用户自己的命名习惯比内置建议更该被优先看到。
    两边合并去重,顺序稳定。
    """
    out: list[str] = []
    seen: set[str] = set()

    def add(items):
        for it in items or []:
            key = norm(it)
            if key and key not in seen:
                seen.add(key)
                out.append(key)

    add(extra)
    add(SUGGESTIONS.get(str(category or "").strip()))
    add(GENERIC)
    return out


def norm(name: Any) -> str:
    """归一化属性名:去掉首尾空白,中间连续空白压成一个空格。

    只做这一层。大小写**不**在这里统一 —— 中文属性名没有大小写问题,
    而英文的「Vgs」和「VGS」到底哪个是用户想要的,我没资格替他决定。
    """
    text = str(name if name is not None else "")
    return " ".join(text.split())


def display_name(key: Any) -> str:
    """属性名怎么显示。归一化之后直接用;空名字返回空串。"""
    return norm(key)


def clean(params: Any) -> dict[str, str]:
    """把一份属性整理成 {归一化名: 值},丢掉空名字。

    值为空的属性**保留** —— 「这颗料的耐压还没填」和「这颗料没有耐压这个属性」
    是两件事,后者才是删掉那一行。值统一转成字符串并去掉首尾空白。
    """
    if not isinstance(params, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in params.items():
        key = norm(k)
        if not key:
            continue
        out[key] = str(v if v is not None else "").strip()
    return out


def fmt(params: Any) -> str:
    """合成出入库那一列要显示的一行文本:「50V · ±5%」。

    只列**非空**的值。名称为空或者值没填的属性不在这里占位,免得列里出现
    「· · ·」这种不知道在看什么的东西。一个值都没有时返回空串,表格里就是空白,
    而不是一个空的 `{}`。
    """
    vals = [v for _k, v in sorted(clean(params).items(), key=_sort_key) if v]
    return SEP.join(vals)


def fmt_full(params: Any) -> str:
    """完整的名=值列表,给详情面板用:「耐压=50V    精度=±5%」。

    没填值的属性也列出来(写成「耐压=」),因为「有这项但没填」本身是要看到的信息。
    """
    pairs = [f"{k}={v}" for k, v in sorted(clean(params).items(), key=_sort_key)]
    return "    ".join(pairs)
