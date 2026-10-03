# -*- coding: utf-8 -*-
"""Altium Designer BOM 解析与导入。

流程:读 xlsx -> 定位表头 -> 按列名映射 -> 归一化每一行 -> upsert 元件 -> 建项目 BOM。

列名兼容 Altium 的英文默认导出与常见中文导出,不区分大小写、忽略多余空格。
没有立创编号时回退用厂家料号(MPN)作为识别键。
"""
from __future__ import annotations

import math
import os
import re
from typing import Any, Iterable

import db
import xlsx

# 规范字段 -> 可接受的表头写法
HEADER_ALIASES: dict[str, list[str]] = {
    "no": ["no.", "no", "序号", "#", "item"],
    "quantity": ["quantity", "qty", "数量", "用量"],
    "comment": ["comment", "注释", "描述", "description"],
    "designator": ["designator", "designators", "位号", "元件标号", "元件位号"],
    "footprint": ["footprint", "封装", "pcb footprint"],
    "value": ["value", "值", "参数值", "标称值"],
    "mpn": ["manufacturer part", "manufacturer part number", "mpn",
            "厂家料号", "制造商料号", "厂商料号"],
    "manufacturer": ["manufacturer", "厂家", "制造商", "厂商"],
    "supplier_pn": ["supplier part", "supplier part number", "供应商料号",
                    "立创编号", "商品编号"],
    "supplier": ["supplier", "供应商", "供货商"],
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
    (("TYPE-C", "TYPEC", "USB", "MICRO-USB", "MINI-USB"), "连接器"),
    (("CONN", "HEADER", "SOCKET", "TERMINAL", "PLUG", "JACK", "PINHD", "HDR"), "连接器"),
    (("XTAL", "CRYSTAL", "OSC", "RESONATOR"), "晶振"),
    (("FUSE", "PTC"), "保险丝"),
    (("SWITCH", "SW_", "TACT", "BUTTON"), "开关"),
    (("RELAY",), "继电器"),
    (("TESTPOINT", "TP_"), "测试点"),
]

DESIGNATOR_RE = re.compile(r"^([A-Za-z]+)")


def _norm(s: Any) -> str:
    """表头归一化:转小写、去首尾空白、压缩内部空格。"""
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s).strip().lower())


def split_designators(raw: Any) -> list[str]:
    """'C1,C2,C8' -> ['C1','C2','C8'];兼容中文逗号、顿号、分号与空格。"""
    if raw is None:
        return []
    text = str(raw).replace("，", ",").replace("、", ",").replace(";", ",").replace("；", ",")
    return [d.strip() for d in text.split(",") if d.strip()]


def guess_category(designators: Iterable[str], footprint: str = "", value: str = "") -> str:
    """判断品类。先用封装里的强特征纠正,再按位号前缀,最后按封装形状兜底。"""
    fp = (footprint or "").upper()
    for keys, cat in CATEGORY_BY_FOOTPRINT:
        if any(k in fp for k in keys):
            return cat

    for d in designators:
        m = DESIGNATOR_RE.match(str(d).strip())
        if not m:
            continue
        prefix = m.group(1).upper()
        for pfx, cat in CATEGORY_BY_PREFIX:
            if prefix.startswith(pfx):
                return cat

    if any(k in fp for k in ("SOT", "QFN", "QFP", "BGA", "SOP", "SOIC", "DFN", "LQFP", "TSSOP")):
        return "芯片/IC"
    if fp.startswith(("R", "C", "L")) and any(ch.isdigit() for ch in fp):
        return {"R": "电阻", "C": "电容", "L": "电感"}[fp[0]]
    return "其他"


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


def build_name(value: str | None, package: str | None, mpn: str | None,
               lcsc: str | None, category: str) -> str:
    """拼一个人能读的显示名,例如 '1uF 0805' / '10kΩ 0603'。"""
    parts = [p for p in (value, package) if p]
    if parts:
        return " ".join(parts)
    return mpn or lcsc or category


def parse_workbook(path: str, sheet_name: str | None = None) -> tuple[list[dict], list[str]]:
    """解析 BOM 文件,返回 (条目列表, 警告列表)。"""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"文件不存在:{path}")

    rows = xlsx.read_sheet(path, sheet_name=sheet_name)
    if not rows:
        raise ValueError("工作表是空的")

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
        if not lcsc and not mpn:
            warnings.append(f"第 {offset} 行:既无立创编号也无厂家料号,已跳过")
            continue

        key = (lcsc or f"MPN:{mpn}").upper()
        if key in seen:
            warnings.append(f"第 {offset} 行:料号 {key} 重复出现,已合并处理")
        seen.add(key)

        if designators and qty is not None and len(designators) != qty:
            warnings.append(
                f"第 {offset} 行:{lcsc or mpn} 位号数({len(designators)})与数量({qty})不一致,按数量为准"
            )
            qty = qty if qty is not None else len(designators)

        comment = _cell(row, cols, "comment")
        value = _cell(row, cols, "value") or comment
        footprint = _cell(row, cols, "footprint")
        category = guess_category(designators, str(footprint or ""), str(value or ""))

        items.append({
            "lcsc_pn": lcsc.upper() if lcsc else None,
            "mpn": mpn,
            "manufacturer": _cell(row, cols, "manufacturer"),
            "name": build_name(str(value) if value else None,
                               str(footprint) if footprint else None,
                               mpn, lcsc, category),
            "category": category,
            "value": str(value) if value else None,
            "package": str(footprint) if footprint else None,
            "designators": designators,
            "qty": int(qty or 0),
            "source_row": offset,
        })

    if not items:
        raise ValueError("没有解析到任何元件行")
    return items, warnings


def find_component(con, lcsc_pn: str | None, mpn: str | None):
    """按立创编号优先、厂家料号其次找已有元件。"""
    if lcsc_pn:
        row = con.execute("SELECT * FROM component WHERE lcsc_pn=?", (lcsc_pn,)).fetchone()
        if row:
            return row
    if mpn:
        row = con.execute("SELECT * FROM component WHERE mpn=?", (mpn,)).fetchone()
        if row:
            return row
    return None


def upsert_component(con, item: dict) -> tuple[int, bool]:
    """插入或补全元件。返回 (元件 id, 是否新建)。只补空字段,不覆盖已有值。"""
    existing = find_component(con, item.get("lcsc_pn"), item.get("mpn"))
    if existing:
        cid = existing["id"]
        updates, args = [], []
        for field in ("lcsc_pn", "mpn", "manufacturer", "category", "value", "package", "name"):
            if not existing[field] and item.get(field):
                updates.append(f"{field}=?")
                args.append(item[field])
        if updates:
            updates.append("updated_at=?")
            args.extend([db.now(), cid])
            con.execute(f"UPDATE component SET {', '.join(updates)} WHERE id=?", args)
        return cid, False

    cur = con.execute(
        """INSERT INTO component(lcsc_pn, mpn, manufacturer, name, category, value, package)
           VALUES(?,?,?,?,?,?,?)""",
        (item.get("lcsc_pn"), item.get("mpn"), item.get("manufacturer"),
         item["name"], item["category"], item.get("value"), item.get("package")),
    )
    return int(cur.lastrowid), True


def import_bom(con, path: str, project_name: str, project_code: str | None = None,
               repo: str | None = None, sheet_name: str | None = None,
               replace_existing: bool = True) -> dict:
    """把一份 BOM 导入成项目。返回导入报告。"""
    items, warnings = parse_workbook(path, sheet_name=sheet_name)

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
                  c.unit_price, c.min_stock,
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

