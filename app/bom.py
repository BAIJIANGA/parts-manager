# -*- coding: utf-8 -*-
"""BOM 解析与导入(xlsx / csv 都走这里)。

流程:读文件 -> 定位表头 -> 按列名映射 -> 归一化每一行 -> upsert 元件 -> 建项目 BOM。

xlsx 和 csv 只差「怎么把文件变成二维表格」这一步,后面完全共用一套行解析,
所以两种格式的识别规则、告警、品类推断不会各走各的。

表头兼容 Altium / KiCad / EasyEDA / 立创 的常见写法,不区分大小写、忽略多余空格。
没有立创编号时回退用厂家料号(MPN)作为识别键。
"""
from __future__ import annotations

import csv
import io
import math
import os
import re
from typing import Any, Iterable

import db
import values
import xlsx

# 规范字段 -> 可接受的表头写法。
# 要同时认下这几家的默认导出,否则用户得先手工改成我们要的列名才导得进来:
#   Altium   Designator / Comment / Footprint / Quantity / Manufacturer Part
#   KiCad    Reference / Value / Footprint / Quantity / MPN / Manufacturer / LCSC
#   EasyEDA  Designator / Comment / Footprint / Quantity / Manufacturer Part
#   立创 BOM Quantity / Comment / Designator / Footprint / Manufacturer Part / Supplier Part
HEADER_ALIASES: dict[str, list[str]] = {
    "no": ["no.", "no", "序号", "#", "item", "index"],
    "quantity": ["quantity", "qty", "qty.", "数量", "用量", "amount", "pcs", "数量(pcs)"],
    "comment": ["comment", "注释", "描述", "description", "remark", "备注", "note"],
    "designator": ["designator", "designators", "位号", "元件标号", "元件位号",
                   "reference", "references", "refdes", "ref", "refs",
                   "元件编号", "标号", "reference designator"],
    "footprint": ["footprint", "封装", "pcb footprint", "package", "case"],
    "value": ["value", "值", "参数值", "标称值", "val", "nominal"],
    "mpn": ["manufacturer part", "manufacturer part number", "manufacturer part no",
            "manufacturer p/n", "mpn", "mpn/part number", "part number", "part no",
            "partno", "厂家料号", "制造商料号", "厂商料号", "厂家型号", "厂商型号"],
    "manufacturer": ["manufacturer", "厂家", "制造商", "厂商", "mfr", "brand"],
    # 有些导出会自带品类列。它比任何推断都权威,所以认出来就直接用。
    # 故意不收 "type"/"kind":那两列常常写的是 SMD / 直插 / 贴片,不是品类。
    "category": ["category", "品类", "分类", "类别", "元件类别", "component category"],
    "supplier_pn": ["supplier part", "supplier part number", "supplier part no",
                    "supplier part #", "供应商料号", "供应商编号", "立创编号",
                    "立创料号", "商品编号", "lcsc", "lcsc part", "lcsc part number",
                    "lcsc part no", "lcsc part #", "lcsc part number/商品编号"],
    "supplier": ["supplier", "供应商", "供货商", "vendor"],
}

# 位号前缀 -> 品类(按优先顺序匹配,长的必须在前)
CATEGORY_BY_PREFIX: list[tuple[str, str]] = [
    ("USB", "连接器"),
    ("LED", "发光二极管"),
    ("FB", "磁珠"),
    ("CN", "连接器"),
    ("SW", "开关"),
    ("TP", "测试点"),
    ("RV", "电位器"),
    ("LS", "扬声器"),
    ("BT", "电池"),
    ("C", "电容"),
    ("R", "电阻"),
    ("L", "电感"),
    ("D", "二极管"),
    ("Q", "三极管/MOS"),
    ("U", "芯片/IC"),
    ("J", "连接器"),
    ("P", "连接器"),
    ("Y", "晶振"),
    ("X", "晶振"),
    ("F", "保险丝"),
    ("K", "继电器"),
    ("M", "电机"),
]

# 封装名里的强特征 -> 品类。优先于位号前缀,用来纠正「用 U3 标接线端子」这类
# 不规范但真实存在的画法。
CATEGORY_BY_FOOTPRINT: list[tuple[tuple[str, ...], str]] = [
    (("RES-ADJ", "POTENTIOMETER", "TRIM", "POT_"), "电位器"),
    # 发光二极管要排在别的前面:LED 封装名里常带 0805/0603 这类数字,
    # 落到最后那条「R/C/L 开头 + 数字」的弱规则上会被认成电感
    (("LED_", "LED-", "LED0805", "LED0603", "LED1206"), "发光二极管"),
    # 二极管封装。SOD / DO-214 是很明确的二极管外形,不会和别的撞。
    # 故意不收 TO-220 / TO-252:那里面既有 MOS 也有稳压块,认不准就别装懂
    (("SOD-", "SOD123", "SOD323", "SOD523", "DO-214", "DO-41", "DO-35"), "二极管"),
    # 接线端子的常见型号族(KF301 / KF128 / KF2EDG …)。国产 BOM 里很常见,
    # 而且这类料经常被位号写成 U 或 J,不认封装就会跟着位号跑偏
    (("KF301", "KF128", "KF2EDG", "KF350", "KF7.62", "DG301", "HT396"), "连接器"),
    (("TYPE-C", "TYPEC", "USB", "MICRO-USB", "MINI-USB"), "连接器"),
    (("CONN", "HEADER", "SOCKET", "TERMINAL", "PLUG", "JACK", "PINHD", "HDR"), "连接器"),
    (("XTAL", "CRYSTAL", "OSC", "RESONATOR"), "晶振"),
    (("FUSE", "PTC"), "保险丝"),
    (("SWITCH", "SW_", "TACT", "BUTTON"), "开关"),
    (("RELAY",), "继电器"),
    (("TESTPOINT", "TP_"), "测试点"),
]

DESIGNATOR_RE = re.compile(r"^([A-Za-z]+)")

# 芯片常见封装。它只说明「长得像芯片」,识别不出具体是什么片子,所以算弱证据。
CHIP_FOOTPRINTS = ("SOT", "QFN", "QFP", "BGA", "SOP", "SOIC", "DFN", "LQFP", "TSSOP")

# 值的单位 -> 品类。导出的 BOM 常常位号、料号都不全,但「值」几乎总在,
# 而值的单位本身就限定了它是什么:Ω 只能是电阻,F 只能是电容,H 只能是电感。
VALUE_UNIT_CATEGORY = {"Ω": "电阻", "F": "电容", "H": "电感"}

# 品类全集。它同时是导入复核对话框里那个下拉框的选项来源 ——
# 只有一处定义,免得上游认出来的品类在界面上反而选不到。
# 顺序就是下拉框里的顺序:常用的排前面。
CATEGORIES = [
    "电阻", "电容", "电感", "磁珠", "二极管", "发光二极管", "三极管/MOS",
    "芯片/IC", "连接器", "晶振", "开关", "保险丝", "电位器", "继电器",
    "测试点", "电池", "扬声器", "电机", "其他",
]

# 置信度常量。界面按它决定哪些行要标红催人看一眼。
CONF_HIGH, CONF_LOW, CONF_NONE, CONF_HUMAN = "high", "low", "none", "human"
CONF_LABEL = {CONF_HIGH: "明确", CONF_LOW: "要确认", CONF_NONE: "认不出",
              CONF_HUMAN: "人工指定"}


def value_category(value: Any) -> tuple[str | None, str]:
    """从「值」的单位判品类。返回 (品类, 理由);判不出来时品类是 None。

    这是这次新增的判据。位号可以被省掉、料号可以被省掉,但「值」几乎总在,
    而且 10kΩ / 100nF / 4.7uH 里的那个单位已经把品类说死了。
    """
    text = str(value or "").strip()
    if not text:
        return None, ""
    num, unit = values.parse_value(text)
    if num is None:
        return None, ""
    if unit in VALUE_UNIT_CATEGORY:
        cat = VALUE_UNIT_CATEGORY[unit]
        return cat, f"值 {text} 的单位是 {unit},{unit} 只能是{cat}"
    # 4R7 / R47:R 是欧姆位(当小数点用),不是单位,但足以说明这是电阻
    if re.search(r"[Rr]", text):
        return "电阻", f"值 {text} 里的 R 是欧姆位,所以是电阻"
    return None, ""


def classify(designators: Iterable[str] | None = None, footprint: Any = "",
             value: Any = "", hint: Any = "") -> tuple[str, str, str]:
    """判品类,并给出**依据**和**把握有多大**。返回 (品类, 置信度, 理由)。

    置信度:
      high  有明确依据(封装关键词 / 位号前缀 / 值的单位 / BOM 里写的品类),
            而且所有线索互相印证
      low   线索之间**打架**,或者只有「封装长得像芯片」这类弱证据 ——
            这两种情况人工必须看一眼
      none  一个可用的线索都没有,归到「其他」
      human 导入时人手动指定的

    为什么返回理由而不是只返回一个词:这是给人工复核看的。只说「我猜是电容」
    没法判断该不该改,说「值 100nF 的单位是 F」才能一眼看出对不对。

    为什么线索打架要降级而不是硬选一个:那份 BOM 自己就矛盾,比如位号写着 C1
    而值是 10kΩ。这种只能是数据错了,该由人拍板,程序不该装作知道。

    优先级(数字越小越权威)刻意保持和旧版一致:封装里的强特征(比如
    RES-ADJ→电位器、TYPE-C→连接器)要压过位号,因为「用 U3 标接线端子」
    这类不规范的画法真实存在,而封装的写法通常更接近事实。
    """
    votes: list[tuple[int, str, str]] = []      # (优先级, 品类, 理由)

    text_hint = str(hint or "").strip()
    if text_hint:
        votes.append((0, text_hint, f"BOM 里本来就写着品类「{text_hint}」"))

    fp = str(footprint or "").upper()
    for keys, cat in CATEGORY_BY_FOOTPRINT:
        if any(k in fp for k in keys):
            votes.append((1, cat, f"封装 {footprint} 里的关键词是{cat}的特征"))
            break

    for d in designators or []:
        m = DESIGNATOR_RE.match(str(d).strip())
        if not m:
            continue
        prefix = m.group(1).upper()
        for pfx, cat in CATEGORY_BY_PREFIX:
            if prefix.startswith(pfx):
                votes.append((2, cat, f"位号 {prefix} 开头,按惯例是{cat}"))
                break
        break

    vcat, vwhy = value_category(value)
    if vcat:
        votes.append((3, vcat, vwhy))

    if not any(v[0] <= 3 for v in votes):       # 还没有明确依据,试试弱证据
        if any(k in fp for k in CHIP_FOOTPRINTS):
            votes.append((4, "芯片/IC", f"封装 {footprint} 是芯片常见的封装(弱证据)"))
        elif fp[:1] in ("R", "C", "L") and any(ch.isdigit() for ch in fp):
            cat = {"R": "电阻", "C": "电容", "L": "电感"}[fp[0]]
            votes.append((4, cat, f"封装 {footprint} 以 {fp[0]} 开头且带数字(弱证据)"))

    if not votes:
        return "其他", CONF_NONE, "位号、值、封装里都没有能认出品类的线索"

    votes.sort(key=lambda v: v[0])
    strong = [v for v in votes if v[0] <= 3]
    pick = (strong or votes)[0]
    if len({v[1] for v in votes}) == 1:
        # 一条线索都没有对不上的,才敢说「明确」
        return pick[1], (CONF_HIGH if strong else CONF_LOW), pick[2]
    why = "；".join(v[2] for v in votes) + "。这几条线索互相矛盾,请人工确认"
    return pick[1], CONF_LOW, why


def _norm(s: Any) -> str:
    """表头归一化:转小写、去首尾空白、压缩内部空格。"""
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s).strip().lower())


def split_designators(raw: Any) -> list[str]:
    """'C1,C2,C8' -> ['C1','C2','C8'];兼容中文逗号、顿号、分号与空格。

    空格也是分隔符 —— KiCad 导出的位号就长这样:「R1 R2 R3」。
    """
    if raw is None:
        return []
    text = str(raw).replace("，", ",").replace("、", ",").replace(";", ",").replace("；", ",")
    text = re.sub(r"\s+", ",", text)
    return [d.strip() for d in text.split(",") if d.strip()]


def guess_category(designators: Iterable[str], footprint: str = "", value: str = "") -> str:
    """只要品类名时用这个(老调用点)。判据、置信度和理由见 classify。"""
    return classify(designators, footprint, value)[0]


def find_header(rows: list[list[Any]]) -> tuple[int, dict[str, int]]:
    """在前若干行里找表头行。返回 (行索引, {规范字段: 列索引})。"""
    lookup = {_norm(alias): field for field, aliases in HEADER_ALIASES.items() for alias in aliases}
    best = (-1, {})
    for i, row in enumerate(rows[:15]):  # 表头通常在最前面
        cols: dict[str, int] = {}
        for ci, cell in enumerate(row):
            field = lookup.get(_norm(cell))
            if field and field not in cols:
                cols[field] = ci
        # 至少要认出「位号」和「数量」才算表头
        if "designator" in cols and "quantity" in cols and len(cols) > len(best[1]):
            best = (i, cols)
    if best[0] < 0:
        raise ValueError("没找到 BOM 表头行:至少需要 'Designator'(位号)与 'Quantity'(数量)两列")
    return best


def _cell(row: list[Any], cols: dict[str, int], field: str) -> Any:
    idx = cols.get(field)
    if idx is None or idx >= len(row):
        return None
    val = row[idx]
    if isinstance(val, str):
        val = val.strip()
        return val or None
    return val


def _to_int(val: Any) -> int | None:
    if val is None:
        return None
    try:
        return int(float(str(val).replace(",", "").strip()))
    except (ValueError, TypeError):
        return None


# 「身份」和「显示名」是两件事。以前它们挤在 name 一个字段里,后来出了个很难查的 bug:
# 显示名是 `值 + 空格 + 封装`(1uF 0805),而 find_component() 在没有立创编号 / 厂家
# 料号时**就是拿这个名字找同一颗料的**。也就是说,只要动了显示规则,匹配行为跟着变 ——
# 有人要求「100nF 就是 100nF,别带封装」,一改,100nF 0603 和 100nF 0805 就算成同一个
# 名字,下次导入把两颗不同的料静默并成一条,BOM 需求和库存全错,而且不报错、看不出来。
#
# 所以身份单独一个键,按优先级取:立创编号 > 厂家料号 > 值 + 封装。名字随便怎么改都
# 不影响匹配。


def norm_package(text) -> str:
    """封装归一化:C0805 / c0805 / C-0805 归成同一个键。

    这个函数原来只在 server.py 里(给相似度打分用的)。身份键也得用同一套规则 ——
    两处各写一份迟早会漂移,所以挪到这里当成唯一的实现,server 那边改成转发。
    """
    return re.sub(r"[\s\-_/]+", "", str(text or "").upper())


def _ident_part(text) -> str:
    """归一化身份键里的一段:去首尾空白、内部连续空白压成一个、统一大写。"""
    if text is None:
        return ""
    return re.sub(r"\s+", " ", str(text).strip()).upper()


def identity_key(value: str | None, package: str | None, mpn: str | None,
                 lcsc: str | None, name: str | None = None) -> str:
    """算一颗料的匹配键。同一颗料反复导入必须算出同一个键。

    优先级不能随手换 —— 立创编号是唯一的,厂家料号次之,两者都没有才退回「值 + 封装」:

    - 立创编号 / 厂家料号:单独就足够定性,和封装无关(同一个编号只对应一种封装)
    - 值和封装都进键:少任何一个,100nF 0603 和 100nF 0805 就会撞车
    - 连值和封装都没有(手填的、只有名字的元件):退回按名字 —— 这时候名字就是它
      唯一的身份,只能用它

    返回空串表示「什么都判断不出来」,调用方会退化成不匹配(宁可多一条重复,
    也不要乱并)。
    """
    if lcsc and str(lcsc).strip():
        return "lcsc:" + str(lcsc).strip().upper()
    if mpn and str(mpn).strip():
        return "mpn:" + str(mpn).strip()
    val = _ident_part(value)
    pkg = norm_package(package)
    if val or pkg:
        return f"vp:{val}|{pkg}"
    nm = _ident_part(name)
    if nm:
        return "nm:" + nm
    # 什么信息都没有。这里**必须**返回空串,不能返回 "nm:" 这种光秃秃的前缀 ——
    # 那会让所有「什么都没有」的元件共用一个键,一导入就整批并成一条,
    # 正是拆出身份键要避免的事。空串会让调用方退化成不匹配(宁可多一条重复)。
    return ""


def build_name(value: str | None, mpn: str | None,
               lcsc: str | None, category: str) -> str:
    """拼一个人能读的显示名。**只放值,不带封装。**

    封装有自己单独一列,再拼进名字里是重复的,列表看着也脏。
    值为空时退回厂家料号 / 立创编号 / 品类,保证名字永远不为空(name 是 NOT NULL)。
    """
    if value and str(value).strip():
        return str(value).strip()
    return mpn or lcsc or category


def parse_workbook(path: str, sheet_name: str | None = None) -> tuple[list[dict], list[str]]:
    """解析 xlsx/xlsm,返回 (条目列表, 警告列表)。"""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"文件不存在:{path}")

    rows = xlsx.read_sheet(path, sheet_name=sheet_name)
    if not rows:
        raise ValueError("工作表是空的")
    return rows_to_items(rows)


# CSV 常见编码。中文 Excel 另存为 CSV 往往是 GBK,而 KiCad / 立创导出的是带 BOM 的
# UTF-8 —— 两个都得认,不能让用户为了导入先去转一次编码。
CSV_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "gbk", "big5", "latin-1")


def read_csv_rows(path: str) -> list[list[str]]:
    """读 CSV 成二维表。表头定位、列名映射跟 xlsx 完全共用。"""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"文件不存在:{path}")
    raw = open(path, "rb").read()
    if not raw.strip():
        raise ValueError("文件是空的")
    text = None
    for enc in CSV_ENCODINGS:
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("这个 CSV 的编码认不出来")

    # 分隔符按第一行里出现最多的那个猜。欧洲区域设置下 Excel 会用分号,
    # 有些工具导出的是制表符 —— 全都当 CSV 处理,不让用户去改。
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    delim = ","
    best = 0
    for cand in (",", ";", "\t", "|"):
        n = first.count(cand)
        if n > best:
            best, delim = n, cand
    return [list(r) for r in csv.reader(io.StringIO(text), delimiter=delim)]


def parse_csv(path: str) -> tuple[list[dict], list[str]]:
    rows = [r for r in read_csv_rows(path) if any(str(c).strip() for c in r)]
    if not rows:
        raise ValueError("CSV 里没有内容")
    return rows_to_items(rows)


def parse_any(path: str, sheet_name: str | None = None) -> tuple[list[dict], list[str]]:
    """按扩展名分派。xlsx / xlsm 走 Excel,其余按 CSV 处理。"""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm"):
        return parse_workbook(path, sheet_name=sheet_name)
    if ext in (".csv", ".txt", ".tsv"):
        return parse_csv(path)
    # 后缀不认识时两种都试一遍,总比直接拒了强
    try:
        return parse_workbook(path, sheet_name=sheet_name)
    except Exception:
        return parse_csv(path)


def rows_to_items(rows: list[list[Any]]) -> tuple[list[dict], list[str]]:
    """二维表 -> 条目列表。xlsx 和 csv 共用这一套,规则不会两边跑偏。"""
    hdr_idx, cols = find_header(rows)
    warnings: list[str] = []
    items: list[dict] = []
    seen: set[str] = set()

    for offset, row in enumerate(rows[hdr_idx + 1:], start=hdr_idx + 2):
        designators = split_designators(_cell(row, cols, "designator"))
        qty = _to_int(_cell(row, cols, "quantity"))
        # 数量缺失时用位号个数兜底
        if qty is None and designators:
            qty = len(designators)

        lcsc_raw = _cell(row, cols, "supplier_pn")
        lcsc = str(lcsc_raw).strip() if lcsc_raw else None
        mpn_raw = _cell(row, cols, "mpn")
        mpn = str(mpn_raw).strip() if mpn_raw else None

        # 整行空白就跳过
        if not any([designators, qty, lcsc, mpn, _cell(row, cols, "value")]):
            continue

        comment = _cell(row, cols, "comment")
        value = _cell(row, cols, "value") or comment
        footprint = _cell(row, cols, "footprint")

        # 识别键:立创编号 > 厂家料号 > 值+封装。
        # 不能因为「没有料号」就把整行丢掉 —— KiCad 和 EasyEDA 的默认导出
        # 常常只有 Value + Footprint,这种行照样是能导进来的。
        if lcsc:
            key = lcsc.upper()
        elif mpn:
            key = f"MPN:{mpn}".upper()
        elif value or footprint:
            key = f"VF:{value or ''}|{footprint or ''}".upper()
        else:
            warnings.append(f"第 {offset} 行:既没有料号也没有值和封装,没法识别,已跳过")
            continue

        if key in seen:
            warnings.append(f"第 {offset} 行:{key} 重复出现,已合并处理")
        seen.add(key)

        if designators and qty is not None and len(designators) != qty:
            warnings.append(
                f"第 {offset} 行:{key} 位号数({len(designators)})与数量({qty})不一致,按数量为准"
            )
            qty = qty if qty is not None else len(designators)

        category, cat_conf, cat_why = classify(
            designators, str(footprint or ""), str(value or ""),
            _cell(row, cols, "category") or "")

        items.append({
            "lcsc_pn": lcsc.upper() if lcsc else None,
            "mpn": mpn,
            "manufacturer": _cell(row, cols, "manufacturer"),
            "name": build_name(str(value) if value else None,
                               mpn, lcsc, category),
            "category": category,
            "category_confidence": cat_conf,
            "category_reason": cat_why,
            "value": str(value) if value else None,
            "package": str(footprint) if footprint else None,
            "designators": designators,
            "qty": int(qty or 0),
            "source_row": offset,
        })

    if not items:
        raise ValueError("没有解析到任何元件行")
    return items, warnings


def find_component(con, item: dict):
    """找这颗料在库里对应的行,没有就返回 None。

    按 identity_key 找,**不再按 name 找**。名字是给人看的,用户随时可能要求改显示
    规则;拿它当键用,等于改一次显示就偷偷改一次匹配行为(见 identity_key 上面的注释)。

    始终跳过 merged_into 不为空的行:那些是合并掉的重复料,已经不在列表里了,
    再往它们身上挂 BOM 行等于把刚合掉的重复又长回来。
    """
    key = identity_key(item.get("value"), item.get("package"),
                       item.get("mpn"), item.get("lcsc_pn"), item.get("name"))
    if key:
        row = con.execute(
            "SELECT * FROM component WHERE identity_key=? AND merged_into IS NULL"
            " ORDER BY id LIMIT 1", (key,)).fetchone()
        if row:
            return row
    # 兜底:老库里可能还留着没算过键的行(迁移没跑完就不该发生,但真发生的话
    # lcsc_pn 上的 UNIQUE 约束会让 INSERT 直接报错,比多查一次严重得多)。
    lcsc = item.get("lcsc_pn")
    if lcsc:
        return con.execute(
            "SELECT * FROM component WHERE lcsc_pn=? AND merged_into IS NULL",
            (lcsc,)).fetchone()
    return None


def upsert_component(con, item: dict) -> tuple[int, bool]:
    """插入或补全元件。返回 (元件 id, 是否新建)。只补空字段,不覆盖已有值。

    **一个例外:导入前人工指定的品类,哪怕原来已经有品类也要写进去。**
    推断出来的品类只是新料的默认值,猜错也就猜错了;人工改过的是决定。
    如果这条被「不覆盖已有值」默默丢掉,用户改完点确认、界面提示导入成功、
    库里却还是老样子 —— 那复核这一步就成了摆设,而且是会骗人的那种。

    重新导入同一块板的改版 BOM 时必然踩到这条:元件早就在库里了,
    不复核前改的品类等于白改。
    """
    existing = find_component(con, item)
    human = item.get("category_confidence") == CONF_HUMAN
    if existing:
        cid = existing["id"]
        updates, args = [], []
        for field in ("lcsc_pn", "mpn", "manufacturer", "value", "package", "name"):
            if not existing[field] and item.get(field):
                updates.append(f"{field}=?")
                args.append(item[field])
        # 品类单独处理:空着的补上;人工复核改过的直接覆盖
        want_cat = item.get("category")
        if want_cat and (human or not existing["category"]) \
                and want_cat != existing["category"]:
            updates.append("category=?")
            args.append(want_cat)
        if updates:
            updates.append("updated_at=?")
            args.extend([db.now(), cid])
            con.execute(f"UPDATE component SET {', '.join(updates)} WHERE id=?", args)
            # 身份字段可能刚被补上(比如原来没厂家料号,这次导进来了),键得跟着重算 ——
            # 否则下次导入拿新料号找不到这一条,会再长出一颗重复的。
            fresh = con.execute("SELECT * FROM component WHERE id=?", (cid,)).fetchone()
            con.execute(
                "UPDATE component SET identity_key=? WHERE id=?",
                (identity_key(fresh["value"], fresh["package"], fresh["mpn"],
                              fresh["lcsc_pn"], fresh["name"]), cid))
        # 导入时就得把数值列算出来。以前靠下次启动的 backfill 补,
        # 结果「刚导完就按阻值排序 / 筛区间」是不准的。
        db.set_value_num(con, cid, item.get("value"))
        return cid, False

    cur = con.execute(
        """INSERT INTO component(lcsc_pn, mpn, manufacturer, name, category, value, package,
                                identity_key)
           VALUES(?,?,?,?,?,?,?,?)""",
        (item.get("lcsc_pn"), item.get("mpn"), item.get("manufacturer"),
         item["name"], item["category"], item.get("value"), item.get("package"),
         identity_key(item.get("value"), item.get("package"),
                      item.get("mpn"), item.get("lcsc_pn"), item.get("name"))),
    )
    cid = int(cur.lastrowid)
    db.set_value_num(con, cid, item.get("value"))
    return cid, True


def import_items(con, items: list[dict], project_name: str, *, project_code: str | None = None,
                 repo: str | None = None, replace_existing: bool = True,
                 warnings: list[str] | None = None,
                 categories: dict | None = None) -> dict:
    """把已经解析好的条目导入成一个项目。xlsx 与 csv 共用这一套落库逻辑。

    categories 是「导入前人工复核」的结果:{source_row: 品类}。
    复核对话框按行号回传,所以这里按 source_row 覆盖 —— 用行号而不是用位置,
    是因为中间可能有被跳过的行,按位置对会整体错位。
    JSON 传过来的键是字符串,所以两种写法都认。
    """
    warnings = list(warnings or [])
    if categories:
        fixed = []
        for it in items:
            row = it.get("source_row")
            override = categories.get(row, categories.get(str(row)))
            if override:
                it = dict(it, category=str(override).strip(),
                          category_confidence=CONF_HUMAN,
                          category_reason="导入时人工指定")
            fixed.append(it)
        items = fixed
    proj = con.execute("SELECT * FROM project WHERE name=?", (project_name,)).fetchone()
    if proj:
        project_id = proj["id"]
        con.execute("UPDATE project SET code=COALESCE(?, code), repo=COALESCE(?, repo) WHERE id=?",
                    (project_code, repo, project_id))
    else:
        cur = con.execute("INSERT INTO project(name, code, repo) VALUES(?,?,?)",
                          (project_name, project_code, repo))
        project_id = int(cur.lastrowid)

    if replace_existing:
        con.execute("DELETE FROM project_bom WHERE project_id=?", (project_id,))

    created = reused = 0
    total_qty = 0
    bom_rows = []
    for item in items:
        cid, is_new = upsert_component(con, item)
        created += 1 if is_new else 0
        reused += 0 if is_new else 1
        total_qty += item["qty"]
        designators = ",".join(item["designators"])
        con.execute(
            """INSERT INTO project_bom(project_id, component_id, required_qty, designators)
               VALUES(?,?,?,?)
               ON CONFLICT(project_id, component_id)
               DO UPDATE SET required_qty=excluded.required_qty, designators=excluded.designators""",
            (project_id, cid, item["qty"], designators),
        )
        bom_rows.append({
            "component_id": cid,
            "lcsc_pn": item["lcsc_pn"],
            "mpn": item["mpn"],
            "name": item["name"],
            "category": item["category"],
            "required_qty": item["qty"],
            "designators": designators,
        })

    con.commit()
    return {
        "project_id": project_id,
        "project_name": project_name,
        "bom_lines": len(bom_rows),
        "components_created": created,
        "components_reused": reused,
        "total_qty": total_qty,
        "warnings": warnings,
        "rows": bom_rows,
    }


def import_bom(con, path: str, project_name: str, project_code: str | None = None,
               repo: str | None = None, sheet_name: str | None = None,
               replace_existing: bool = True, categories: dict | None = None) -> dict:
    """把一份 BOM 文件(xlsx 或 csv)导入成项目。返回导入报告。"""
    items, warnings = parse_any(path, sheet_name=sheet_name)
    report = import_items(con, items, project_name, project_code=project_code, repo=repo,
                          replace_existing=replace_existing, warnings=warnings,
                          categories=categories)
    report["source"] = os.path.basename(path)
    return report


def build_report(con, project_id: int) -> dict:
    """项目的完整物料报告:每行要多少、有多少、缺多少,以及整块板**能造几块**。

    口径(与 InvenTree 对齐):

      单块用量  per_board = BOM 里写的 required_qty
      总需求    need      = ceil(per_board × (1 + 损耗%)) × 项目计划数量 + 固定损耗
      可用      available = 本件现有 + 替代料现有
      缺口      gap       = max(0, need − available)
      该买      to_order  = max(0, gap − 在途)
      这一行能支持几块 line_build = floor(max(0, available − 固定损耗) / (per_board × (1+损耗%)))
      能造几块  can_build = 所有「卡产能」的行里最少的那个(= 瓶颈决定产量)

    三处刻意的取舍,都写在这里免得以后自己看不懂:

    * **免点件(consumable)不卡产能**。螺丝、锡、扎带这类东西算需求(要买),
      但不应决定「能装出几块板」。
    * **可选件(optional)也不卡产能**。InvenTree 是把它算进去的,但那意味着
      「少一个本来就可以不装的电阻」会把整块板的可造数打成 0,对个人使用非常反直觉。
      缺的可选件会单独统计(optional_missing),不会悄悄丢掉。
    * **替代料的库存算进这一行的可用量**,否则设替代料就白设了。
    """
    proj = con.execute("SELECT * FROM project WHERE id=?", (project_id,)).fetchone()
    if not proj:
        raise ValueError("项目不存在")
    boards = max(int(proj["qty"] or 1), 1)

    rows = con.execute(
        """SELECT b.id AS bom_id, b.component_id, b.required_qty, b.designators,
                  b.placed_qty, b.optional, b.consumable, b.attrition, b.setup_qty, b.note,
                  c.lcsc_pn, c.mpn, c.name, c.category, c.package, c.value, c.unit,
                  c.unit_price, c.min_stock, c.params,
                  COALESCE((SELECT SUM(qty) FROM stock s
                             WHERE s.component_id = b.component_id), 0) AS on_hand,
                  COALESCE((SELECT SUM(pu.qty) FROM purchase pu
                             WHERE pu.component_id = b.component_id
                               AND pu.status = 'ordered'), 0) AS on_order
             FROM project_bom b JOIN component c ON c.id = b.component_id
            WHERE b.project_id = ?
            ORDER BY c.category, c.value_num, c.value, c.name""",
        (project_id,)).fetchall()

    lines = []
    shortage_lines = shortage_qty = optional_missing = 0
    shortage_value = 0.0
    bottleneck = None

    for r in rows:
        per_board = max(int(r["required_qty"]), 0)
        attrition = float(r["attrition"] or 0)
        setup = int(r["setup_qty"] or 0)
        optional = bool(r["optional"])
        consumable = bool(r["consumable"])

        per_board_with_loss = per_board * (1 + attrition / 100.0)
        need = int(math.ceil(per_board_with_loss * boards)) + setup

        subs = [dict(s) for s in con.execute(
            """SELECT s.id, s.component_id, c.name, c.value, c.package, c.lcsc_pn, c.mpn,
                      COALESCE((SELECT SUM(qty) FROM stock st
                                 WHERE st.component_id = s.component_id), 0) AS on_hand
                 FROM bom_substitute s JOIN component c ON c.id = s.component_id
                WHERE s.bom_id = ? ORDER BY c.value_num, c.value, c.name""",
            (r["bom_id"],))]
        on_hand = int(r["on_hand"])
        sub_qty = sum(int(s["on_hand"]) for s in subs)
        available = on_hand + sub_qty
        on_order = int(r["on_order"])

        gap = max(0, need - available)
        to_order = max(0, gap - on_order)

        if per_board_with_loss > 0:
            line_build = int(max(0.0, available - setup) / per_board_with_loss)
        else:
            line_build = boards          # 用量为 0 的行不该限制产能

        blocking = not (consumable or optional) and per_board > 0
        if blocking:
            bottleneck = line_build if bottleneck is None else min(bottleneck, line_build)

        if gap:
            shortage_qty += gap
            shortage_value += gap * float(r["unit_price"] or 0)
            if optional:
                optional_missing += 1
            elif not consumable:
                shortage_lines += 1

        lines.append({
            "bom_id": r["bom_id"],
            "component_id": r["component_id"],
            "lcsc_pn": r["lcsc_pn"], "mpn": r["mpn"], "name": r["name"],
            "category": r["category"], "package": r["package"], "value": r["value"],
            "unit": r["unit"], "unit_price": float(r["unit_price"] or 0),
            "per_board": per_board,
            "required_qty": per_board,          # 兼容既有调用
            "need": need,
            "placed_qty": int(r["placed_qty"]),
            "remaining": max(0, need - int(r["placed_qty"])),
            "on_hand": on_hand, "sub_qty": sub_qty, "available": available,
            "on_order": on_order, "gap": gap, "to_order": to_order,
            "line_build": line_build, "blocking": blocking,
            "optional": optional, "consumable": consumable,
            "attrition": attrition, "setup_qty": setup,
            "designators": r["designators"], "note": r["note"],
            # 用户自定义的属性(耐压 / 精度 / 功率 …)。存的是 JSON,这里解成 dict
            # 让界面直接用;收料清单要靠它把「这一盘 100nF 是 50V 还是 16V」显示出来。
            "params": db.parse_params(r["params"]),
            "substitutes": subs,
            "ok": gap == 0,
        })

    lines.sort(key=lambda x: (x["ok"], -x["gap"], x["category"], x["name"]))
    return {
        "project_id": project_id,
        "project_name": proj["name"],
        "boards": boards,
        "lines": lines,
        "line_count": len(lines),
        "can_build": int(bottleneck or 0),
        "shortage_lines": shortage_lines,
        "shortage_qty": shortage_qty,
        "shortage_value": round(shortage_value, 2),
        "optional_missing": optional_missing,
        "ready": shortage_lines == 0,
    }


# 老名字,给还在用的调用点留个入口
shortage_report = build_report

