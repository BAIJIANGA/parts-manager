# -*- coding: utf-8 -*-
"""标准封装识别:把 C0805 / R0603 / L0402 / 0805 / 1005 认成同一个「尺寸」。

为什么值得单独做成一个模块:立创的 BOM 导出会写成 C0805(电容)、R0603(电阻)、
L0402(电感),前面那个字母是**器件类别**,不是封装的一部分。同一个 0805,
一条记录写 0805、另一条写 C0805 —— 不认的话就是两种封装:

  * 库存页的封装按钮分成两个,筛选一次只看得到一半;
  * 身份键(identity_key 的 vp: 那一段)也算成两个,下次导入 BOM 会把同一颗料
    并成两条,或者干脆匹配不上;
  * 电容电感一多(几百上千条),按封装找料只能全表逐行比字符串,慢。

三种等价关系都认:
  1. **类别前缀**  C0805 == 0805 == R0805(物理尺寸一样;能不能互换看的是尺寸,
     不是它当初被画成电容还是电阻)
  2. **分隔符**    C-0805 == C0805 == C 0805 == C_0805
  3. **公制/英制** 1005(公制)== 0402(英制),1608/0603,2012/0805,3216/1206 …

第 3 条有个必须写下来的坑:公制 0603 等于英制 0201,公制 0402 等于英制 01005。
但中文语境里说「0603」「0402」**指的都是英制** —— 按公制去解释会让全库的
0603 电阻统统变成 0201。所以只映射那批**不会和英制名字撞车**的公制代号
(1005 / 1608 / 2012 / 3216 / 3225 / 4532 / 5025 / 6432)。

认不出来的一律退回「去分隔符 + 大写」—— 和 bom.norm_package 完全一致,
所以 DIP-8 / SOT-23 / TYPE-C 这些非标准封装的匹配行为和以前一模一样,不会有回归。
"""
from __future__ import annotations

import re

# 英制尺寸(中文语境下说「0603」指的就是这个)
SIZES = ("0201", "0402", "0603", "0805", "1206", "1210", "1812", "2010", "2512")

# 公制 -> 英制。只收不会和英制名字撞车的那些,理由见文件头。
METRIC_TO_IMPERIAL = {
    "1005": "0402",
    "1608": "0603",
    "2012": "0805",
    "3216": "1206",
    "3225": "1210",
    "4532": "1812",
    "5025": "2010",
    "6432": "2512",
}

# 封装开头的类别代号 -> 品类。长的必须排在前面(RES 要压过 R)。
# 这些只是**弱证据**:封装叫 R0603 不等于它一定是电阻(有人拿 0603 封装画保险丝),
# 所以 bom.classify 只在别的线索全都认不出来时才用它。
KIND_CATEGORY = {
    "RES": "电阻", "CAP": "电容", "IND": "电感",
    "LED": "发光二极管", "FB": "磁珠", "XTAL": "晶振", "CRYSTAL": "晶振",
    "CONN": "连接器", "HDR": "连接器", "FUSE": "保险丝", "SW": "开关",
    "C": "电容", "R": "电阻", "L": "电感", "D": "二极管",
    "Q": "三极管/MOS", "U": "芯片/IC", "Y": "晶振", "X": "晶振",
    "J": "连接器", "P": "连接器", "F": "保险丝", "K": "继电器", "M": "电机",
}

# 按长度降序,保证 RES 先于 R、LED 先于 L 被匹配到
_KINDS_BY_LEN = sorted(KIND_CATEGORY, key=len, reverse=True)

_SEP_RE = re.compile(r"[\s\-_/]+")
_NUM_RE = re.compile(r"\d{3,4}")


def norm(text) -> str:
    """去分隔符 + 大写。和 bom.norm_package 同一套规则(那里转发到这里)。"""
    return _SEP_RE.sub("", str(text or "").upper())


def _find_size(text: str) -> tuple[str, int]:
    """在归一化后的串里找尺寸。返回 (尺寸, 它在串里的位置);没有返回 ('', -1)。

    逐段找 3~4 位数字:3 位的左边补 0(805 -> 0805),再看是不是已知尺寸。
    `0805_1.6x0.8` 这种带尺寸说明的写法也能认出 0805(先撞上的是它)。
    """
    for m in _NUM_RE.finditer(text):
        tok = m.group(0).zfill(4)
        if tok in SIZES:
            return tok, m.start()
        if tok in METRIC_TO_IMPERIAL:
            return METRIC_TO_IMPERIAL[tok], m.start()
    return "", -1


def _find_kind(head: str) -> str:
    for code in _KINDS_BY_LEN:
        if head.startswith(code):
            return code
    return ""


def parse(text) -> dict:
    """拆成 {raw, norm, kind, size, known}。

    size 是归并后的尺寸(公制已折算成英制),认不出来是空串 —— 这时候
    kind 也不再往下猜,因为连尺寸都没有的串(比如 `SMD`)说明不了任何事。
    """
    raw = str(text or "").strip()
    up = norm(raw)
    size, at = _find_size(up)
    # 类别代号只在尺寸**前面**那一段里找:C0805 的 C 是类别,而 CAPC0805 的
    # CAPC 整个都是前缀。尺寸后面那截(X7R 之类)不是类别。
    head = up[:at] if at >= 0 else up
    kind = _find_kind(head) if size else ""
    return {"raw": raw, "norm": up, "kind": kind, "size": size, "known": bool(size)}


def size(text) -> str:
    """归并后的尺寸,认不出来返回空串。"""
    return _find_size(norm(text))[0]


def kind(text) -> str:
    """类别代号(C / R / L / CAP / RES …),认不出来返回空串。"""
    return parse(text)["kind"]


def category(text) -> str:
    """按封装的类别代号猜品类。认不出来返回空串(= 不表态,别硬猜)。"""
    return KIND_CATEGORY.get(kind(text), "")


def canon(text) -> str:
    """归并键:能认出尺寸就给尺寸,否则退回「去分隔符 + 大写」。

    这个函数是封装这件事的**唯一**判断标准 —— 身份键、相似度、分组、筛选、
    搜索全用它,免得几处各写一份规则然后慢慢漂移。
    """
    got = size(text)
    return got if got else norm(text)


# 给人看/给代码用的短名字:这个就是存进 component.package_key 的那个值。
# (别叫 norm_package —— bom 里那个同名函数只去分隔符,不认尺寸,两件事。)
package_key = canon


def similar(a, b) -> bool:
    """两个封装是不是同一个尺寸。空的不算相似(宁可说不像,也别乱认)。"""
    ka, kb = canon(a), canon(b)
    return bool(ka) and ka == kb


def label(text) -> str:
    """界面上显示的写法:认得出就给归并后的尺寸,认不出就原样显示。

    注意**不**改写用户存进去的字(他说过「保留原样显示」)—— 这只是给
    分组按钮、候选列表这种地方用的短标签。
    """
    raw = str(text or "").strip()
    return size(raw) or raw
