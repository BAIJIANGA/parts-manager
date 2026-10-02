# -*- coding: utf-8 -*-
"""纯标准库 .xlsx 读取器 —— 用来读 Altium Designer 导出的 BOM 表。

只依赖 zipfile + xml.etree,不引入任何第三方库。

支持:
  * 共享字符串表 (xl/sharedStrings.xml)
  * 内联字符串 (t="inlineStr")
  * 数字 / 布尔 / 公式缓存值 (t="str")
  * 稀疏行:按单元格 r 属性定位列,空列补 None

限制(本系统用不到):
  * 日期按序列号返回,不做格式识别
  * 不解析样式、合并单元格、图表、多表合并
"""
from __future__ import annotations

import re
import zipfile
import xml.etree.ElementTree as ET
from typing import Any

_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

_CELL_REF = re.compile(r"^([A-Z]+)(\d+)$")


def col_to_index(letters: str) -> int:
    """列字母转 0 基索引:'A' -> 0,'B' -> 1,'AA' -> 26"""
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def index_to_col(idx: int) -> str:
    """0 基索引转列字母:0 -> 'A',26 -> 'AA'"""
    s = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        s = chr(65 + rem) + s
    return s


def _parse_ref(ref: str):
    m = _CELL_REF.match(ref or "")
    if not m:
        return None, None
    return col_to_index(m.group(1)), int(m.group(2))


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    """读取共享字符串表。缺失时返回空表(Excel 可能全用内联字符串)。"""
    try:
        data = zf.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(data)
    out: list[str] = []
    for si in root:
        # 一个 <si> 可能是富文本,由多个 <r><t> 片段拼成
        out.append("".join(t.text or "" for t in si.iter(f"{_MAIN}t")))
    return out


def _sheet_targets(zf: zipfile.ZipFile) -> list[tuple[str, str]]:
    """返回 [(工作表名, zip 内路径)],顺序与工作簿一致。"""
    wb = ET.fromstring(zf.read("xl/workbook.xml"))
    rels: dict[str, str] = {}
    try:
        rr = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        for rel in rr:
            rels[rel.get("Id", "")] = rel.get("Target", "")
    except KeyError:
        pass

    names = set(zf.namelist())
    out: list[tuple[str, str]] = []
    for sh in wb.iter(f"{_MAIN}sheet"):
        target = rels.get(sh.get(f"{_REL}id", ""), "")
        if target.startswith("/"):
            path = target.lstrip("/")
        elif target:
            path = "xl/" + target.lstrip("./")
        else:
            path = ""
        if path in names:
            out.append((sh.get("name") or path, path))

    if not out:  # 兜底:直接扫 worksheets 目录
        out = [(n, n) for n in sorted(names) if n.startswith("xl/worksheets/sheet")]
    return out


def sheet_names(path: str) -> list[str]:
    """列出工作簿里所有工作表名。"""
    with zipfile.ZipFile(path) as zf:
        return [n for n, _ in _sheet_targets(zf)]


def read_sheet(path: str, index: int = 0, sheet_name: str | None = None) -> list[list[Any]]:
    """读一张工作表,返回按行排列的二维列表(不足的列补 None)。"""
    with zipfile.ZipFile(path) as zf:
        shared = _shared_strings(zf)
        targets = _sheet_targets(zf)
        if not targets:
            return []
        if sheet_name is not None:
            picked = [t for t in targets if t[0] == sheet_name]
            spath = (picked or targets)[0][1]
        else:
            spath = targets[min(index, len(targets) - 1)][1]
        root = ET.fromstring(zf.read(spath))

    rows: dict[int, dict[int, Any]] = {}
    max_col = 0

    for row in root.iter(f"{_MAIN}row"):
        fallback_col = 0
        for c in row:
            if c.tag.rsplit("}", 1)[-1] != "c":
                continue
            ci, ri = _parse_ref(c.get("r") or "")
            if ri is None:
                ri = int(row.get("r") or (len(rows) + 1))
                ci = fallback_col
            fallback_col = ci + 1

            t = c.get("t")
            val: Any = None
            if t == "inlineStr":
                is_el = c.find(f"{_MAIN}is")
                if is_el is not None:
                    val = "".join(x.text or "" for x in is_el.iter(f"{_MAIN}t"))
            else:
                v = c.find(f"{_MAIN}v")
                if v is not None and v.text is not None:
                    if t == "s":  # 共享字符串索引
                        try:
                            val = shared[int(v.text)]
                        except (ValueError, IndexError):
                            val = v.text
                    elif t == "b":  # 布尔
                        val = v.text not in ("0", "false", "FALSE")
                    elif t == "str":  # 公式结果字符串
                        val = v.text
                    else:  # 数字
                        try:
                            f = float(v.text)
                            val = int(f) if f.is_integer() else f
                        except ValueError:
                            val = v.text

            if val is None or (isinstance(val, str) and val.strip() == ""):
                continue  # 跳过空单元格,后面统一补 None
            rows.setdefault(ri, {})[ci] = val
            if ci > max_col:
                max_col = ci

    if not rows:
        return []
    width = max_col + 1
    return [[rows.get(r, {}).get(c) for c in range(width)] for r in range(1, max(rows) + 1)]
