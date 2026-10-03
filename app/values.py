# -*- coding: utf-8 -*-
"""把元件的标称值解析成数值,用来正确排序和按范围筛选。

为什么需要它:元件表里 value 是文本,`100kΩ` 按字典序排在 `10kΩ` **前面**,
`1uF` 和 `100nF` 也没法比大小。参考 InvenTree 的做法 —— 它给参数额外存一列
数值(data_numeric),那一列才是「按阻值筛 1k~100k」能用的原因。

支持的写法:
    10k      10000          10kΩ     10000(单位 Ω)
    4.7k     4700           4.7uF    4.7e-6(单位 F)
    100nF    1e-7           220uF    2.2e-4
    0Ω       0              1M       1e6
    10k3     10300(工程记法:k 后面的数字是小数部分)
    R47      0.47(R 当小数点用,电阻常见)

解析不出来的返回 (None, None) —— 比如厂家料号 `CH224K` 这种,它本来就不是标称值。
关键是把数字限定在开头:凡是**不以数字开头**的一律不认,这样 `SCT2450STER`、
`TYPE-C-SMD_...` 这类料号不会解析成乱七八糟的数。
"""
from __future__ import annotations

import re

# SI 前缀。大小写是有意义的:m 是毫,m 与 M 必须分开。
PREFIX = {
    "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6,
    "m": 1e-3,
    "k": 1e3, "K": 1e3,
    "M": 1e6, "G": 1e9, "T": 1e12,
    "R": 1.0, "r": 1.0,          # 4R7 = 4.7(电阻的欧姆位)
}

# 数字 + 可选前缀 + 可选工程记法尾数 + 剩下的是单位
_RE = re.compile(r"^([+-]?)(\d+)(?:\.(\d+))?([pnuµmkKMGT]|[Rr])?(\d*)([^\d]*)$")

# 单位只认这种短的形式;太长的说明这压根不是标称值
_UNIT_OK = re.compile(r"^[A-Za-zΩµ%][A-Za-zΩµ%]{0,3}$")


def parse_value(text):
    """'10kΩ' -> (10000.0, 'Ω');'SCT2450STER' -> (None, None)。"""
    if text is None:
        return None, None
    s = str(text).strip().replace(" ", "").replace("\u00a0", "")
    if not s:
        return None, None
    # ohm / OHM 都当 Ω,免得同一个东西两种单位
    s = re.sub(r"(?i)ohms?", "Ω", s)

    # R47 / r47:欧姆位省略整数部分,等价于 0.47(4R7 那条正则已经覆盖了)
    m0 = re.match(r"^([Rr])(\d+)$", s)
    if m0:
        return float(f"0.{m0.group(2)}"), None

    m = _RE.match(s)
    if not m:
        return None, None
    sign, int_part, frac, prefix, eng, unit = m.groups()

    if unit and not _UNIT_OK.match(unit):
        return None, None

    try:
        num = float(f"{int_part}.{frac}") if frac else float(int_part)
        # 工程记法:尾数数字是小数部分。10k3 -> 10.3k,R47 -> 0.47
        if eng:
            num += float(f"0.{eng}")
    except (ValueError, TypeError):
        return None, None

    num *= PREFIX.get(prefix, 1.0) if prefix else 1.0
    if sign == "-":
        num = -num
    return num, (unit or None)


def fmt_value(num, unit=None) -> str:
    """把数值还原成好看的写法:10000 -> '10k',4.7e-06 -> '4.7u'。"""
    if num is None:
        return ""
    for scale, sym in ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1.0, ""),
                       (1e-3, "m"), (1e-6, "u"), (1e-9, "n"), (1e-12, "p")):
        if abs(num) >= scale * 0.999:
            val = num / scale
            text = f"{val:.3f}".rstrip("0").rstrip(".")
            return f"{text}{sym}{unit or ''}"
    return f"{num}{unit or ''}"
