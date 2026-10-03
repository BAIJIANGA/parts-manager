# -*- coding: utf-8 -*-
"""物料管理系统 —— HTTP 服务与 JSON API。

纯标准库实现(http.server + sqlite3 + json),无任何第三方依赖。
只监听 127.0.0.1:PC 本地使用,不对外暴露,因此不需要防火墙规则。

启动:  python app/server.py [--port 8000] [--db data/parts.db]
"""
from __future__ import annotations

import argparse
import io
import json
import mimetypes
import os
import re
import sys
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bom  # noqa: E402
import db  # noqa: E402
import xlsx  # noqa: E402

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
PROJECT_ROOT = os.path.dirname(BASE_DIR)
DEFAULT_DB = os.path.join(PROJECT_ROOT, "data", "parts.db")
MAX_UPLOAD = 32 * 1024 * 1024  # 32MB,防内存被撑爆

# 输出编码兜底。
# 某些环境下 stdout/stderr 是 GBK(典型:被重定向到文件、或没设 PYTHONUTF8),
# 此时打印任何 GBK 编不出的字符都会抛 UnicodeEncodeError,直接把服务打死在启动横幅上。
# 这里统一改成「编不出就替换」,保证日志永远不会导致崩溃。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except (AttributeError, ValueError, OSError):  # 不是 TextIOWrapper 就跳过
        pass

import threading  # noqa: E402
import time  # noqa: E402

# 前端心跳。
# 便携版启动器靠它判断「界面还开着吗」:网页每 5 秒打一次 /api/ping,
# 一旦窗口被关掉请求就停了,空闲超时后服务自行退出,不会赖在后台。
LAST_ACTIVE = [time.time()]
# 页面心跳计数。启动器靠它判断「Edge 窗口到底有没有真的把页面跑起来」:
# 拉起了 Edge 进程但计数不涨,说明窗口没出来,该退回默认浏览器了。
PING_COUNT = [0]


def start_idle_watchdog(seconds: int) -> None:
    """空闲超过 seconds 秒没有收到任何请求,就整体退出。

    只在便携版(--idle-exit)下启用。二次确认是为了容忍系统休眠:
    唤醒后 time.time() 会跳变,给前端 3 秒机会把心跳打上来。
    """
    if seconds <= 0:
        return

    def loop() -> None:
        while True:
            time.sleep(5)
            idle = time.time() - LAST_ACTIVE[0]
            if idle > seconds:
                time.sleep(3)
                if time.time() - LAST_ACTIVE[0] > seconds:
                    print(f"[空闲] {int(idle)} 秒没有前端活动,自动退出")
                    try:
                        sys.stdout.flush()
                    except Exception:
                        pass
                    os._exit(0)

    threading.Thread(target=loop, daemon=True).start()

CATEGORY_SUGGESTIONS = [
    "电阻", "电容", "电感", "磁珠", "二极管", "发光二极管", "三极管/MOS",
    "芯片/IC", "连接器", "晶振", "开关", "电位器", "保险丝", "继电器",
    "传感器", "模块", "结构件", "其他",
]

ROUTES: list[tuple[str, re.Pattern, Callable]] = []


def route(method: str, pattern: str):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn))
        return fn
    return deco


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Ctx:
    """一次请求的上下文。"""

    def __init__(self, handler, con, query: dict, body: dict, upload: dict | None):
        self.handler = handler
        self.con = con
        self.query = query
        self.body = body
        self.upload = upload

    def q(self, name: str, default=None):
        v = self.query.get(name)
        if isinstance(v, list):
            v = v[0] if v else None
        return default if v in (None, "") else v

    def qi(self, name: str, default=None):
        v = self.q(name)
        if v is None:
            return default
        try:
            return int(v)
        except ValueError:
            return default

    def b(self, name: str, default=None):
        return self.body.get(name, default)

    def bi(self, name: str, default=None):
        v = self.body.get(name, default)
        if v is None or v == "":
            return default
        try:
            return int(v)
        except (ValueError, TypeError):
            return default

    def require(self, name: str):
        v = self.b(name)
        if v is None or (isinstance(v, str) and not v.strip()):
            raise ApiError(400, f"缺少必填字段:{name}")
        return v


def _as_float(v, default=0.0) -> float:
    if v in (None, ""):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _truthy(v) -> int:
    """1/True/'1'/'true'/'yes' 都算真 —— 界面传字符串、自检传布尔,两种都要认。"""
    return 1 if v in (1, "1", True, "true", "yes") else 0


def _flag(ctx, name) -> bool:
    """查询串里的开关。界面传的是字符串,自检可能传布尔,统一成字符串再比。"""
    return str(ctx.q(name, "")).strip().lower() in ("1", "true", "yes")


# ---------------------------------------------------------------- 元件

# 库存口径全部集中在这几个片段里,别处一律引用,保证任何界面上的数字都一致:
#
#   现有 on_hand    = 各仓位数量之和
#   需求 required   = Σ(活动项目的 BOM 单块用量 × 项目计划数量)
#   可用 available  = 现有(本系统不做硬占用/预留,所以两者相同)
#   缺口 deficit    = max(0, 需求 − 可用)
#   在途 on_order   = 已下单未到货
#   目标 target     = max(需求, 安全库存)
#   该买 to_order   = max(0, 目标 − 现有 − 在途)
#
# 「该买」这条我**故意和 InvenTree 不一样**,因为它的算法会欠购。
# InvenTree 的 quantity_to_order(已核对 master 源码 part/models.py:1529)是:
#       required -= max(total_stock, minimum_stock)
# 代入一个真实场景 —— 需求 50、安全库存 100、现有 30:
#       InvenTree: 50 − max(30,100) = 50 − 100 = −50  →  建议买 0
# 可你手上只有 30、项目要出 50,实际缺 20,它却说不用买。
# 换个场景,需求 200、安全库存 100、现有 30:
#       InvenTree: 200 − 100 = 100  →  建议买 100,而实际缺 170。
#
# 所以我改用「目标库存」的说法,意思直白、也能自己验算:
#       手上要留够 max(项目要用的, 安全库存) 那么多,差的才买。
# 上面两个例子分别得出 70 和 170,是对的。这样一来安全库存才真的起到补货作用,
# 而不是一个只出现在提示文字里、不参与计算的数字。
ON_HAND_SQL = "COALESCE((SELECT SUM(s.qty) FROM stock s WHERE s.component_id = c.id), 0)"

REQUIRED_SQL = """COALESCE((
    SELECT SUM(b.required_qty * MAX(p.qty, 1))
      FROM project_bom b JOIN project p ON p.id = b.project_id
     WHERE b.component_id = c.id AND p.status = 'active'), 0)"""

# 在途 = 已下单但还没到的数量。只有 status='ordered' 才算 —— 'todo' 还只是
# 想买(购物车里),不能拿来抵采购建议,否则会把「要买」算成「已经买了」。
ON_ORDER_SQL = """COALESCE((
    SELECT SUM(pu.qty - pu.received) FROM purchase pu
     WHERE pu.component_id = c.id AND pu.status = 'ordered'), 0)"""

COMPONENT_SELECT = f"""
SELECT c.*,
       {ON_HAND_SQL} AS on_hand,
       {REQUIRED_SQL} AS required,
       {ON_HAND_SQL} AS available,
       {ON_ORDER_SQL} AS on_order,
       MAX({REQUIRED_SQL}, c.min_stock) AS target,
       MAX(0, {REQUIRED_SQL} - {ON_HAND_SQL}) AS deficit,
       MAX(0, MAX({REQUIRED_SQL}, c.min_stock) - {ON_HAND_SQL} - {ON_ORDER_SQL}) AS to_order,
       CASE
         WHEN {ON_HAND_SQL} = 0 THEN 'out'
         WHEN {ON_HAND_SQL} < c.min_stock THEN 'low'
         WHEN {ON_HAND_SQL} < {REQUIRED_SQL} THEN 'short'
         ELSE 'ok'
       END AS stock_state
FROM component c
"""

# 库存状态的显示名,界面和报表共用
STATE_LABEL = {"ok": "充足", "low": "偏低", "short": "缺料", "out": "缺货"}


def component_row(row) -> dict:
    d = db.row_to_dict(row)
    d["params"] = db.parse_params(d.get("params"))
    return d


@route("GET", r"/api/components")
def list_components(ctx: Ctx, m):
    where, args = [], []
    keyword = ctx.q("q")
    if keyword:
        like = f"%{keyword}%"
        # 搜索是主入口,不是分类的补充 —— 所以凡是用户可能记得的碎片都要命中:
        # 名称、立创编号、厂家料号、厂家、值、封装、**丝印**、**参数 JSON**、备注、品类。
        # 丝印那一条是给拆机料用的:SOT-23 上只印着三个字母,查不到就等于没存。
        where.append(
            "(c.name LIKE ? OR c.lcsc_pn LIKE ? OR c.mpn LIKE ? OR c.manufacturer LIKE ?"
            " OR c.value LIKE ? OR c.package LIKE ? OR c.marking LIKE ? OR c.params LIKE ?"
            " OR c.note LIKE ? OR c.category LIKE ?)"
        )
        args += [like] * 10
    if ctx.q("category"):
        where.append("c.category = ?")
        args.append(ctx.q("category"))
    if ctx.q("package"):
        where.append("c.package LIKE ?")
        args.append(f"%{ctx.q('package')}%")
    if ctx.q("manufacturer"):
        where.append("c.manufacturer LIKE ?")
        args.append(f"%{ctx.q('manufacturer')}%")

    inner = COMPONENT_SELECT + (" WHERE " + " AND ".join(where) if where else "")
    sql = f"SELECT * FROM ({inner})"
    outer_where, outer_args = [], []
    state = ctx.q("state")
    if state in ("ok", "low", "out"):
        outer_where.append("stock_state = ?")
        outer_args.append(state)
    # stocked=1:只要真正有库存的。on_hand 是子查询算出来的,所以只能在外层过滤。
    # 桌面版的「元件库存」首页靠它把库存为 0 的元件整个滤掉 —— 导入 BOM 只是记下
    # 「这块板子要用什么」,东西还没买回来,那不算库存。
    if _flag(ctx, "stocked"):
        outer_where.append("on_hand > 0")
    if outer_where:
        sql += " WHERE " + " AND ".join(outer_where)

    sort = ctx.q("sort", "category")
    order = {
        "category": "category, value, package, lcsc_pn",
        "qty": "on_hand, name",
        "qty_desc": "on_hand DESC, name",
        "value": "value, package",
        "updated": "updated_at DESC",
        "lcsc": "lcsc_pn",
        "name": "name",
    }.get(sort, "category, value, package, lcsc_pn")
    sql += f" ORDER BY {order}"

    total = len(ctx.con.execute(sql, args + outer_args).fetchall())
    limit = ctx.qi("limit", 0)
    offset = ctx.qi("offset", 0)
    if limit:
        sql += " LIMIT ? OFFSET ?"
        args = args + outer_args + [limit, offset]
    else:
        args = args + outer_args

    rows = [component_row(r) for r in ctx.con.execute(sql, args)]
    return 200, {"total": total, "items": rows}


@route("GET", r"/api/components/(\d+)")
def get_component(ctx: Ctx, m):
    cid = int(m.group(1))
    row = ctx.con.execute(COMPONENT_SELECT + " WHERE c.id = ?", (cid,)).fetchone()
    if not row:
        raise ApiError(404, "元件不存在")
    out = component_row(row)
    out["stock_by_location"] = [
        db.row_to_dict(r) for r in ctx.con.execute(
            """SELECT s.location_id, l.code, s.qty FROM stock s
               JOIN location l ON l.id = s.location_id
               WHERE s.component_id = ? AND s.qty <> 0 ORDER BY l.code""", (cid,))
    ]
    out["movements"] = [
        db.row_to_dict(r) for r in ctx.con.execute(
            """SELECT mv.*, l.code AS location_code, tl.code AS to_location_code, p.name AS project_name
               FROM movement mv
               LEFT JOIN location l  ON l.id  = mv.location_id
               LEFT JOIN location tl ON tl.id = mv.to_location_id
               LEFT JOIN project  p  ON p.id  = mv.project_id
               WHERE mv.component_id = ? ORDER BY mv.id DESC LIMIT 50""", (cid,))
    ]
    return 200, out


@route("POST", r"/api/components")
def create_component(ctx: Ctx, m):
    name = ctx.require("name")
    cur = ctx.con.execute(
        """INSERT INTO component(lcsc_pn, mpn, manufacturer, name, category, value, package,
                                marking, params, datasheet_url, product_url, unit, min_stock,
                                reorder_qty, supplier, unit_price, default_loc_id, note)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ctx.b("lcsc_pn"), ctx.b("mpn"), ctx.b("manufacturer"), name,
         ctx.b("category") or "其他", ctx.b("value"), ctx.b("package"),
         ctx.b("marking"), db.dump_params(ctx.b("params")),
         ctx.b("datasheet_url"), ctx.b("product_url"),
         ctx.b("unit") or "个", ctx.bi("min_stock", 0) or 0,
         ctx.bi("reorder_qty", 0) or 0, ctx.b("supplier"),
         _as_float(ctx.b("unit_price")), ctx.bi("default_loc_id", 0) or None,
         ctx.b("note")),
    )
    db.set_value_num(ctx.con, int(cur.lastrowid), ctx.b("value"))
    ctx.con.commit()
    return 201, {"id": int(cur.lastrowid)}


@route("PUT", r"/api/components/(\d+)")
def update_component(ctx: Ctx, m):
    cid = int(m.group(1))
    row = ctx.con.execute("SELECT * FROM component WHERE id=?", (cid,)).fetchone()
    if not row:
        raise ApiError(404, "元件不存在")

    fields = {
        "lcsc_pn": ctx.b("lcsc_pn"), "mpn": ctx.b("mpn"),
        "manufacturer": ctx.b("manufacturer"), "name": ctx.b("name"),
        "category": ctx.b("category"), "value": ctx.b("value"),
        "package": ctx.b("package"), "marking": ctx.b("marking"),
        "datasheet_url": ctx.b("datasheet_url"),
        "product_url": ctx.b("product_url"), "unit": ctx.b("unit"), "note": ctx.b("note"),
    }
    sets, args = [], []
    for k, v in fields.items():
        if v is not None:
            sets.append(f"{k}=?")
            args.append(v if not isinstance(v, str) or v.strip() else None)
    if "params" in ctx.body:
        sets.append("params=?")
        args.append(db.dump_params(ctx.body["params"]))
    if "min_stock" in ctx.body:
        sets.append("min_stock=?")
        args.append(ctx.bi("min_stock", 0) or 0)
    if "reorder_qty" in ctx.body:
        sets.append("reorder_qty=?")
        args.append(ctx.bi("reorder_qty", 0) or 0)
    if "unit_price" in ctx.body:
        sets.append("unit_price=?")
        args.append(_as_float(ctx.b("unit_price")))
    if "supplier" in ctx.body:
        sets.append("supplier=?")
        args.append(ctx.b("supplier") or None)
    if "default_loc_id" in ctx.body:
        sets.append("default_loc_id=?")
        args.append(ctx.bi("default_loc_id", 0) or None)
    if not sets:
        return 200, {"ok": True, "unchanged": True}

    sets.append("updated_at=?")
    args.extend([db.now(), cid])
    try:
        ctx.con.execute(f"UPDATE component SET {', '.join(sets)} WHERE id=?", args)
    except Exception as exc:  # 唯一约束等
        raise ApiError(400, f"保存失败:{exc}")
    # value 变了就重算数值列,否则排序/筛选会跟显示对不上
    if any(s.startswith("value=") for s in sets):
        db.set_value_num(ctx.con, cid, ctx.b("value"))
    ctx.con.commit()
    return 200, {"ok": True}


@route("DELETE", r"/api/components/(\d+)")
def delete_component(ctx: Ctx, m):
    cid = int(m.group(1))
    n = ctx.con.execute(
        "SELECT COUNT(*) AS n FROM project_bom WHERE component_id=?", (cid,)
    ).fetchone()["n"]
    if n and ctx.q("force") != "1":
        raise ApiError(409, f"该元件被 {n} 个项目 BOM 引用;确认后可用 force=1 删除")
    ctx.con.execute("DELETE FROM component WHERE id=?", (cid,))
    ctx.con.commit()
    return 200, {"ok": True}


@route("GET", r"/api/meta")
def meta(ctx: Ctx, m):
    cats = [r["category"] for r in ctx.con.execute(
        "SELECT DISTINCT category FROM component WHERE category IS NOT NULL ORDER BY category")]
    pkgs = [r["package"] for r in ctx.con.execute(
        "SELECT DISTINCT package FROM component WHERE package IS NOT NULL AND package<>'' "
        "ORDER BY package LIMIT 200")]
    mfrs = [r["manufacturer"] for r in ctx.con.execute(
        "SELECT DISTINCT manufacturer FROM component WHERE manufacturer IS NOT NULL "
        "AND manufacturer<>'' ORDER BY manufacturer LIMIT 200")]
    return 200, {
        "categories": sorted(set(cats) | set(CATEGORY_SUGGESTIONS)),
        "filters": {"categories": cats, "packages": pkgs, "manufacturers": mfrs},
        "locations": [dict(db.row_to_dict(r),
                           path=db.location_path(ctx.con, r["id"]))
                      for r in ctx.con.execute("SELECT * FROM location ORDER BY code")],
        "state_labels": STATE_LABEL,
        "purchase_status": PURCHASE_STATUS,
        "units": ["个", "只", "片", "米", "克", "套", "张", "对"],
    }


# ---------------------------------------------------------------- 仓位与出入库


@route("GET", r"/api/locations")
def list_locations(ctx: Ctx, m):
    """层级仓位表,附带每个仓位的库存件数与总量。界面直接拿去画树。"""
    items = []
    for r in ctx.con.execute("SELECT * FROM location ORDER BY code"):
        d = db.row_to_dict(r)
        d["path"] = db.location_path(ctx.con, r["id"])
        agg = ctx.con.execute(
            "SELECT COALESCE(SUM(qty),0) AS qty, COUNT(DISTINCT component_id) AS kinds "
            "FROM stock WHERE location_id=? AND qty>0", (r["id"],)).fetchone()
        d["qty"] = int(agg["qty"])
        d["kinds"] = int(agg["kinds"])
        d["children"] = ctx.con.execute(
            "SELECT COUNT(*) AS n FROM location WHERE parent_id=?", (r["id"],)).fetchone()["n"]
        items.append(d)
    return 200, {"items": items}


@route("POST", r"/api/locations")
def create_location(ctx: Ctx, m):
    code = str(ctx.require("code")).strip()
    parent = ctx.b("parent_id") or ctx.b("parent")
    parent_id = None
    if parent not in (None, "", 0, "0"):
        parent_id = _get_location_id(ctx.con, parent)
    try:
        cur = ctx.con.execute(
            "INSERT INTO location(code, name, parent_id, structural, note) VALUES(?,?,?,?,?)",
            (code, ctx.b("name") or code, parent_id,
             _truthy(ctx.b("structural")), ctx.b("note")))
    except Exception:
        raise ApiError(400, f"仓位编码 {code} 已存在")
    ctx.con.commit()
    return 201, {"id": int(cur.lastrowid)}


@route("PUT", r"/api/locations/(\d+)")
def update_location(ctx: Ctx, m):
    lid = int(m.group(1))
    if not ctx.con.execute("SELECT 1 FROM location WHERE id=?", (lid,)).fetchone():
        raise ApiError(404, "仓位不存在")
    sets, args = [], []
    for key in ("code", "name", "note"):
        if key in ctx.body:
            sets.append(f"{key}=?")
            args.append(ctx.b(key))
    if "structural" in ctx.body:
        sets.append("structural=?")
        args.append(1 if ctx.b("structural") in (1, "1", True, "true") else 0)
    if "parent_id" in ctx.body:
        pid = ctx.b("parent_id")
        if pid in (None, "", 0, "0"):
            sets.append("parent_id=NULL")
        else:
            pid = _get_location_id(ctx.con, pid)
            if pid == lid:
                raise ApiError(400, "不能把自己设成自己的上级")
            sets.append("parent_id=?")
            args.append(pid)
    if not sets:
        return 200, {"ok": True, "unchanged": True}
    args.append(lid)
    try:
        ctx.con.execute(f"UPDATE location SET {', '.join(sets)} WHERE id=?", args)
    except Exception as exc:
        raise ApiError(400, f"保存失败:{exc}")
    ctx.con.commit()
    return 200, {"ok": True}


@route("DELETE", r"/api/locations/(\d+)")
def delete_location(ctx: Ctx, m):
    lid = int(m.group(1))
    kids = ctx.con.execute("SELECT COUNT(*) AS n FROM location WHERE parent_id=?",
                           (lid,)).fetchone()["n"]
    if kids:
        raise ApiError(409, f"该仓位下面还有 {kids} 个子仓位,先删子仓位")
    used = ctx.con.execute("SELECT COUNT(*) AS n FROM stock WHERE location_id=? AND qty<>0",
                           (lid,)).fetchone()["n"]
    if used:
        raise ApiError(409, "该仓位还有库存,不能删除")
    ctx.con.execute("DELETE FROM location WHERE id=?", (lid,))
    ctx.con.commit()
    return 200, {"ok": True}


@route("GET", r"/api/locations/(\d+)/contents")
def location_contents(ctx: Ctx, m):
    """某个仓位里装了什么。cascade=1 时连子仓位一起算。"""
    lid = int(m.group(1))
    row = ctx.con.execute("SELECT * FROM location WHERE id=?", (lid,)).fetchone()
    if not row:
        raise ApiError(404, "仓位不存在")

    if _flag(ctx, "cascade"):
        # 递归往下把所有子仓位 id 收进来(SQLite 的 WITH RECURSIVE 就够用)
        ids = [r["id"] for r in ctx.con.execute(
            """WITH RECURSIVE sub(id) AS (
                   SELECT ? UNION ALL
                   SELECT l.id FROM location l JOIN sub ON l.parent_id = sub.id)
               SELECT id FROM sub""", (lid,))]
    else:
        ids = [lid]
    marks = ",".join("?" * len(ids))
    rows = ctx.con.execute(
        f"""SELECT c.id, c.name, c.lcsc_pn, c.mpn, c.category, c.value, c.package,
                   c.unit, c.unit_price, s.qty, s.location_id,
                   l.code AS location_code
              FROM stock s JOIN component c ON c.id = s.component_id
              JOIN location l ON l.id = s.location_id
             WHERE s.location_id IN ({marks}) AND s.qty > 0
             ORDER BY c.category, c.value_num, c.value, c.name""", ids)
    items = [db.row_to_dict(r) for r in rows]
    return 200, {
        "location": db.row_to_dict(row),
        "path": db.location_path(ctx.con, lid),
        "items": items,
        "kinds": len({i["id"] for i in items}),
        "total_qty": sum(int(i["qty"]) for i in items),
        "value": round(sum(int(i["qty"]) * float(i["unit_price"] or 0) for i in items), 2),
    }


def _get_location_id(con, code_or_id) -> int:
    """接受仓位 id 或编码。"""
    if code_or_id in (None, ""):
        raise ApiError(400, "缺少仓位")
    s = str(code_or_id)
    if s.isdigit():
        row = con.execute("SELECT id FROM location WHERE id=?", (int(s),)).fetchone()
        if row:
            return int(row["id"])
    row = con.execute("SELECT id FROM location WHERE code=?", (s,)).fetchone()
    if row:
        return int(row["id"])
    cur = con.execute("INSERT INTO location(code) VALUES(?)", (s,))
    return int(cur.lastrowid)


def _fallback_location(con, comp) -> int:
    """没指定仓位时该放哪:先看这个元件的默认仓位,再退到「未分类」。

    收货那一刻不该逼着人选仓位 —— 常用的料都有固定的家(`default_loc_id` 设一次就够),
    没设的先进「未分类」,以后再慢慢归位。社区里弃用这类系统最常见的抱怨就是
    「每用一次料都得去改数据库」,所以能省的一步一定要省。
    """
    if comp is not None and comp["default_loc_id"]:
        return int(comp["default_loc_id"])
    row = con.execute(
        "SELECT id FROM location WHERE structural=0 ORDER BY id LIMIT 1").fetchone()
    if row:
        return int(row["id"])
    cur = con.execute("INSERT INTO location(code, name) VALUES('未分类','未分类')")
    return int(cur.lastrowid)


def _bump(con, component_id: int, location_id: int, delta: int) -> int:
    """在事务内给余额加减,返回变动后的数量。禁止负库存。"""
    if delta > 0:
        # 标了「只用来分层」的仓位不装东西(比如「A柜」和「02层」本身)。
        # 挡住它是为了防止东西被放到一个其实没有物理位置的节点上。
        loc = con.execute("SELECT code, structural FROM location WHERE id=?",
                          (location_id,)).fetchone()
        if loc and loc["structural"]:
            raise ApiError(409, f"「{loc['code']}」是分层仓位,不能直接放东西;"
                                f"请放到它下面的具体仓位里")
    row = con.execute("SELECT qty FROM stock WHERE component_id=? AND location_id=?",
                      (component_id, location_id)).fetchone()
    cur = int(row["qty"]) if row else 0
    new = cur + delta
    if new < 0:
        code = con.execute("SELECT code FROM location WHERE id=?", (location_id,)).fetchone()
        raise ApiError(409, f"库存不足:仓位 {code['code'] if code else location_id} "
                            f"现有 {cur},需要 {abs(delta)}")
    if row:
        con.execute("UPDATE stock SET qty=? WHERE component_id=? AND location_id=?",
                    (new, component_id, location_id))
    else:
        con.execute("INSERT INTO stock(component_id, location_id, qty) VALUES(?,?,?)",
                    (component_id, location_id, new))
    return new


@route("POST", r"/api/stock/move")
def stock_move(ctx: Ctx, m):
    """入库/出库/盘点/移库。全部在一个事务里完成。"""
    kind = str(ctx.require("kind")).upper()
    if kind not in ("IN", "OUT", "ADJUST", "TRANSFER"):
        raise ApiError(400, "kind 必须是 IN / OUT / ADJUST / TRANSFER")
    cid = ctx.bi("component_id")
    if not cid:
        raise ApiError(400, "缺少 component_id")
    comp = ctx.con.execute("SELECT * FROM component WHERE id=?", (cid,)).fetchone()
    if not comp:
        raise ApiError(404, "元件不存在")

    qty = ctx.bi("qty")
    if qty is None or qty < 0:
        raise ApiError(400, "数量必须是不小于 0 的整数")

    raw_loc = ctx.b("location") or ctx.b("location_id")
    if raw_loc in (None, ""):
        # 没写仓位 = 放这个元件的默认位置(没设就进「未分类」)
        loc = _fallback_location(ctx.con, comp)
    else:
        loc = _get_location_id(ctx.con, raw_loc)
    to_loc = None
    if kind == "TRANSFER":
        to_loc = _get_location_id(ctx.con, ctx.b("to_location") or ctx.b("to_location_id"))
        if to_loc == loc:
            raise ApiError(400, "移库的来源与目标仓位相同")
        if qty <= 0:
            raise ApiError(400, "移库数量必须大于 0")
    if kind in ("IN", "OUT") and qty <= 0:
        raise ApiError(400, "数量必须大于 0")

    if kind == "IN":
        after = _bump(ctx.con, cid, loc, qty)
    elif kind == "OUT":
        after = _bump(ctx.con, cid, loc, -qty)
    elif kind == "ADJUST":
        row = ctx.con.execute("SELECT qty FROM stock WHERE component_id=? AND location_id=?",
                              (cid, loc)).fetchone()
        old = int(row["qty"]) if row else 0
        after = _bump(ctx.con, cid, loc, qty - old)
        ctx.body["note"] = (ctx.b("note") or "") + f"（盘点:原 {old} → 新 {qty}）"
    else:  # TRANSFER
        _bump(ctx.con, cid, loc, -qty)
        after = _bump(ctx.con, cid, to_loc, qty)

    cur = ctx.con.execute(
        """INSERT INTO movement(kind, component_id, location_id, to_location_id, qty,
                                project_id, ref, operator, note)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (kind, cid, loc, to_loc, qty, ctx.bi("project_id"),
         ctx.b("ref"), ctx.b("operator") or "本地用户", ctx.b("note")),
    )
    db.touch_component(ctx.con, cid)
    ctx.con.commit()

    on_hand = db.stock_total(ctx.con, cid)
    return 200, {"ok": True, "movement_id": int(cur.lastrowid), "qty_at_location": after,
                 "on_hand": on_hand}


@route("GET", r"/api/movements")
def list_movements(ctx: Ctx, m):
    where, args = [], []
    if ctx.qi("component_id"):
        where.append("mv.component_id = ?")
        args.append(ctx.qi("component_id"))
    if ctx.qi("project_id"):
        where.append("mv.project_id = ?")
        args.append(ctx.qi("project_id"))
    elif ctx.q("project") == "none":
        # 「不指定项目」的日常补货/领用:project_id 为空的那种流水
        where.append("mv.project_id IS NULL")
    if ctx.q("kind"):
        where.append("mv.kind = ?")
        args.append(ctx.q("kind").upper())
    sql = """SELECT mv.*, c.name AS component_name, c.lcsc_pn, c.mpn AS component_mpn,
                    l.code AS location_code, tl.code AS to_location_code, p.name AS project_name
             FROM movement mv
             JOIN component c ON c.id = mv.component_id
             LEFT JOIN location l  ON l.id  = mv.location_id
             LEFT JOIN location tl ON tl.id = mv.to_location_id
             LEFT JOIN project  p  ON p.id  = mv.project_id"""
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY mv.id DESC LIMIT ?"
    args.append(ctx.qi("limit", 200))
    return 200, {"items": [db.row_to_dict(r) for r in ctx.con.execute(sql, args)]}


@route("GET", r"/api/lowstock")
def lowstock(ctx: Ctx, m):
    sql = f"SELECT * FROM ({COMPONENT_SELECT}) WHERE stock_state IN ('low','out') ORDER BY on_hand, name"
    return 200, {"items": [component_row(r) for r in ctx.con.execute(sql)]}


@route("GET", r"/api/summary")
def summary(ctx: Ctx, m):
    con = ctx.con
    comps = con.execute("SELECT COUNT(*) AS n FROM component").fetchone()["n"]
    lots = con.execute("SELECT COALESCE(SUM(qty),0) AS n FROM stock").fetchone()["n"]
    low = con.execute(
        f"SELECT COUNT(*) AS n FROM ({COMPONENT_SELECT}) WHERE stock_state='low'").fetchone()["n"]
    out = con.execute(
        f"SELECT COUNT(*) AS n FROM ({COMPONENT_SELECT}) WHERE stock_state='out'").fetchone()["n"]
    projs = con.execute("SELECT COUNT(*) AS n FROM project").fetchone()["n"]
    recent = [db.row_to_dict(r) for r in con.execute(
        """SELECT mv.id, mv.kind, mv.qty, mv.created_at, c.name AS component_name,
                  c.lcsc_pn, l.code AS location_code, p.name AS project_name
           FROM movement mv JOIN component c ON c.id = mv.component_id
           LEFT JOIN location l ON l.id = mv.location_id
           LEFT JOIN project p ON p.id = mv.project_id
           ORDER BY mv.id DESC LIMIT 10""")]
    by_cat = [db.row_to_dict(r) for r in con.execute(
        """SELECT c.category, COUNT(*) AS kinds,
                  COALESCE(SUM((SELECT SUM(qty) FROM stock s WHERE s.component_id=c.id)),0) AS qty
           FROM component c GROUP BY c.category ORDER BY kinds DESC""")]
    return 200, {"components": comps, "total_qty": lots, "low": low, "out": out,
                 "projects": projs, "recent": recent, "by_category": by_cat}


@route("GET", r"/api/health")
def health(ctx: Ctx, m):
    """给便携版启动器用的身份标识。

    启动器靠它判断某个端口上跑的是不是「本程序的这一份副本」——
    必须连 root 也一致才敢复用,否则会把另一份副本的数据当成自己的。
    """
    return 200, {"app": "parts-manager", "api": 1, "root": PROJECT_ROOT,
                 "pings": PING_COUNT[0]}


@route("GET", r"/api/ping")
def ping(ctx: Ctx, m):
    """前端心跳。网页每 5 秒打一次,服务据此知道界面还开着。"""
    PING_COUNT[0] += 1
    return 200, {"ok": True}


@route("POST", r"/api/rebuild")
def rebuild(ctx: Ctx, m):
    """按流水顺序重放,重建 stock 表,并报告与现状的差异。"""
    con = ctx.con
    before = {(r["component_id"], r["location_id"]): r["qty"]
              for r in con.execute("SELECT * FROM stock")}
    con.execute("DELETE FROM stock")
    for mv in con.execute("SELECT * FROM movement ORDER BY id"):
        cid, kind, qty = mv["component_id"], mv["kind"], mv["qty"]
        loc, to_loc = mv["location_id"], mv["to_location_id"]
        if kind == "IN":
            _bump(con, cid, loc, qty)
        elif kind == "OUT":
            _bump(con, cid, loc, -qty)
        elif kind == "TRANSFER":
            _bump(con, cid, loc, -qty)
            _bump(con, cid, to_loc, qty)
        elif kind == "ADJUST":
            row = con.execute("SELECT qty FROM stock WHERE component_id=? AND location_id=?",
                              (cid, loc)).fetchone()
            _bump(con, cid, loc, qty - (int(row["qty"]) if row else 0))
    con.commit()
    after = {(r["component_id"], r["location_id"]): r["qty"]
             for r in con.execute("SELECT * FROM stock")}
    diffs = []
    for key in set(before) | set(after):
        b, a = before.get(key, 0), after.get(key, 0)
        if b != a:
            diffs.append({"component_id": key[0], "location_id": key[1], "before": b, "after": a})
    return 200, {"ok": True, "differences": diffs, "diff_count": len(diffs)}


# ---------------------------------------------------------------- 项目与 BOM


@route("GET", r"/api/projects")
def list_projects(ctx: Ctx, m):
    """项目列表。每个项目带上「能造几块」和缺料统计 —— 这是项目页最该先看到的。"""
    items = []
    for r in ctx.con.execute("SELECT * FROM project ORDER BY id DESC"):
        d = db.row_to_dict(r)
        agg = ctx.con.execute(
            """SELECT COUNT(*) AS lines,
                      COALESCE(SUM(required_qty),0) AS per_board,
                      COALESCE(SUM(placed_qty),0) AS placed
               FROM project_bom WHERE project_id=?""", (r["id"],)).fetchone()
        d["bom_lines"] = int(agg["lines"])
        d["per_board"] = int(agg["per_board"])
        d["placed"] = int(agg["placed"])
        d["required_qty"] = int(agg["per_board"]) * max(int(d.get("qty") or 1), 1)
        rep = bom.build_report(ctx.con, r["id"])
        d["can_build"] = rep["can_build"]
        d["shortage_lines"] = rep["shortage_lines"]
        d["shortage_qty"] = rep["shortage_qty"]
        d["shortage_value"] = rep["shortage_value"]
        d["ready"] = rep["ready"]
        items.append(d)
    return 200, {"items": items}


@route("POST", r"/api/projects")
def create_project(ctx: Ctx, m):
    cur = ctx.con.execute(
        "INSERT INTO project(name, code, repo, qty, status, note) VALUES(?,?,?,?,?,?)",
        (ctx.require("name"), ctx.b("code"), ctx.b("repo"), ctx.bi("qty", 1) or 1,
         ctx.b("status") or "active", ctx.b("note")))
    ctx.con.commit()
    return 201, {"id": int(cur.lastrowid)}


@route("GET", r"/api/projects/(\d+)")
def get_project(ctx: Ctx, m):
    pid = int(m.group(1))
    row = ctx.con.execute("SELECT * FROM project WHERE id=?", (pid,)).fetchone()
    if not row:
        raise ApiError(404, "项目不存在")
    d = db.row_to_dict(row)
    rep = bom.build_report(ctx.con, pid)
    d.update({k: rep[k] for k in ("can_build", "shortage_lines", "shortage_qty",
                                  "shortage_value", "ready", "line_count")})
    d["latest"] = ctx.con.execute(
        "SELECT MAX(created_at) AS t FROM movement WHERE project_id=?", (pid,)).fetchone()["t"]
    return 200, d


@route("PUT", r"/api/projects/(\d+)")
def update_project(ctx: Ctx, m):
    pid = int(m.group(1))
    if not ctx.con.execute("SELECT 1 FROM project WHERE id=?", (pid,)).fetchone():
        raise ApiError(404, "项目不存在")
    sets, args = [], []
    for key in ("name", "code", "repo", "status", "note"):
        if key in ctx.body:
            sets.append(f"{key}=?")
            args.append(ctx.b(key))
    if "qty" in ctx.body:
        qty = ctx.bi("qty", 1) or 1
        if qty < 1:
            raise ApiError(400, "计划数量至少是 1")
        sets.append("qty=?")
        args.append(qty)
    if not sets:
        return 200, {"ok": True, "unchanged": True}
    args.append(pid)
    ctx.con.execute(f"UPDATE project SET {', '.join(sets)} WHERE id=?", args)
    ctx.con.commit()
    return 200, {"ok": True}


@route("DELETE", r"/api/projects/(\d+)")
def delete_project(ctx: Ctx, m):
    ctx.con.execute("DELETE FROM project WHERE id=?", (int(m.group(1)),))
    ctx.con.commit()
    return 200, {"ok": True}


@route("GET", r"/api/projects/(\d+)/bom")
def project_bom(ctx: Ctx, m):
    pid = int(m.group(1))
    proj = ctx.con.execute("SELECT * FROM project WHERE id=?", (pid,)).fetchone()
    if not proj:
        raise ApiError(404, "项目不存在")
    rep = bom.shortage_report(ctx.con, pid)
    rep["project"] = db.row_to_dict(proj)
    return 200, rep


@route("POST", r"/api/projects/(\d+)/pick")
def pick_for_project(ctx: Ctx, m):
    """按 BOM 领料:对指定项目批量出库。items 传 [{component_id, qty, location_id?}] 或留空=整单。"""
    pid = int(m.group(1))
    proj = ctx.con.execute("SELECT * FROM project WHERE id=?", (pid,)).fetchone()
    if not proj:
        raise ApiError(404, "项目不存在")

    requested = ctx.b("items")
    if requested:
        plan = [(int(i["component_id"]), int(i["qty"]), i.get("location_id") or i.get("location"))
                for i in requested]
    else:
        rep = bom.build_report(ctx.con, pid)
        plan = []
        for line in rep["lines"]:
            need = line["need"] - line["placed_qty"]
            if need > 0:
                plan.append((line["component_id"], need, None))

    default_loc = ctx.b("location") or ctx.b("location_id") or "未分类"
    done, failed = [], []
    for cid, qty, loc in plan:
        try:
            if qty <= 0:
                continue
            loc_id = _get_location_id(ctx.con, loc or default_loc)
            # 库存不足时:只要总量够,从其他仓位凑
            row = ctx.con.execute("SELECT qty FROM stock WHERE component_id=? AND location_id=?",
                                  (cid, loc_id)).fetchone()
            avail = int(row["qty"]) if row else 0
            if avail < qty:
                total = db.stock_total(ctx.con, cid)
                if total < qty:
                    raise ApiError(409, f"库存不足:需要 {qty},仅有 {total}")
                # 先从其他仓位扣,再扣目标仓位
                need = qty
                for s in ctx.con.execute(
                        "SELECT location_id, qty FROM stock WHERE component_id=? AND qty>0 "
                        "AND location_id<>? ORDER BY qty DESC", (cid, loc_id)):
                    if need <= 0:
                        break
                    take = min(need, int(s["qty"]))
                    _bump(ctx.con, cid, s["location_id"], -take)
                    ctx.con.execute(
                        """INSERT INTO movement(kind, component_id, location_id, qty,
                                                project_id, ref, operator, note)
                           VALUES('OUT',?,?,?,?,?,?,?)""",
                        (cid, s["location_id"], take, pid, ctx.b("ref"),
                         ctx.b("operator") or "本地用户", f"项目领料({proj['name']})"))
                    need -= take
                avail = need
            _bump(ctx.con, cid, loc_id, -avail)
            ctx.con.execute(
                """INSERT INTO movement(kind, component_id, location_id, qty, project_id,
                                        ref, operator, note)
                   VALUES('OUT',?,?,?,?,?,?,?)""",
                (cid, loc_id, avail, pid, ctx.b("ref"), ctx.b("operator") or "本地用户",
                 f"项目领料({proj['name']})"))
            ctx.con.execute(
                """UPDATE project_bom SET placed_qty = placed_qty + ?
                   WHERE project_id=? AND component_id=?""", (qty, pid, cid))
            db.touch_component(ctx.con, cid)
            done.append({"component_id": cid, "qty": qty})
        except ApiError as exc:
            failed.append({"component_id": cid, "qty": qty, "reason": exc.message})
    ctx.con.commit()
    return 200, {"ok": not failed, "picked": done, "failed": failed}


# ---------------------------------------------------------------- BOM 编辑与替代料


@route("PUT", r"/api/bom/(\d+)")
def update_bom_line(ctx: Ctx, m):
    """改一行 BOM:用量、位号、损耗率、固定损耗、可选/免点。"""
    bid = int(m.group(1))
    if not ctx.con.execute("SELECT 1 FROM project_bom WHERE id=?", (bid,)).fetchone():
        raise ApiError(404, "BOM 行不存在")
    sets, args = [], []
    for key in ("designators", "note"):
        if key in ctx.body:
            sets.append(f"{key}=?")
            args.append(ctx.b(key))
    for key in ("required_qty", "setup_qty"):
        if key in ctx.body:
            val = ctx.bi(key, 0) or 0
            if val < 0:
                raise ApiError(400, "数量不能是负数")
            sets.append(f"{key}=?")
            args.append(val)
    if "attrition" in ctx.body:
        try:
            val = float(ctx.body["attrition"] or 0)
        except (TypeError, ValueError):
            raise ApiError(400, "损耗率要是数字")
        if val < 0:
            raise ApiError(400, "损耗率不能是负数")
        sets.append("attrition=?")
        args.append(val)
    for key in ("optional", "consumable"):
        if key in ctx.body:
            sets.append(f"{key}=?")
            args.append(_truthy(ctx.b(key)))
    if not sets:
        return 200, {"ok": True, "unchanged": True}
    args.append(bid)
    ctx.con.execute(f"UPDATE project_bom SET {', '.join(sets)} WHERE id=?", args)
    ctx.con.commit()
    return 200, {"ok": True}


@route("DELETE", r"/api/bom/(\d+)")
def delete_bom_line(ctx: Ctx, m):
    ctx.con.execute("DELETE FROM project_bom WHERE id=?", (int(m.group(1)),))
    ctx.con.commit()
    return 200, {"ok": True}


@route("POST", r"/api/projects/(\d+)/bom")
def add_bom_line(ctx: Ctx, m):
    """手动往项目里加一行料。"""
    pid = int(m.group(1))
    if not ctx.con.execute("SELECT 1 FROM project WHERE id=?", (pid,)).fetchone():
        raise ApiError(404, "项目不存在")
    try:
        cur = ctx.con.execute(
            """INSERT INTO project_bom(project_id, component_id, required_qty, designators,
                                       optional, consumable, attrition, setup_qty, note)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (pid, ctx.bi("component_id", 0), ctx.bi("required_qty", 1) or 1,
             ctx.b("designators"), _truthy(ctx.b("optional")), _truthy(ctx.b("consumable")),
             float(ctx.body.get("attrition") or 0), ctx.bi("setup_qty", 0) or 0, ctx.b("note")))
    except Exception as exc:
        raise ApiError(400, f"添加失败(这一行可能已经存在):{exc}")
    ctx.con.commit()
    return 201, {"id": int(cur.lastrowid)}


@route("GET", r"/api/bom/(\d+)/substitutes")
def list_substitutes(ctx: Ctx, m):
    bid = int(m.group(1))
    rows = ctx.con.execute(
        """SELECT s.id, s.note, c.id AS component_id, c.name, c.lcsc_pn, c.mpn,
                  c.value, c.package, c.category, c.unit_price,
                  COALESCE((SELECT SUM(qty) FROM stock st
                             WHERE st.component_id = s.component_id), 0) AS on_hand
             FROM bom_substitute s JOIN component c ON c.id = s.component_id
            WHERE s.bom_id = ? ORDER BY c.value_num, c.value, c.name""", (bid,))
    items = [db.row_to_dict(r) for r in rows]
    return 200, {"items": items, "total": len(items),
                 "on_hand": sum(int(i["on_hand"]) for i in items)}


@route("POST", r"/api/bom/(\d+)/substitutes")
def add_substitute(ctx: Ctx, m):
    """给某一行 BOM 加替代料。替代料的库存会算进这一行的可用量。"""
    bid = int(m.group(1))
    if not ctx.con.execute("SELECT 1 FROM project_bom WHERE id=?", (bid,)).fetchone():
        raise ApiError(404, "BOM 行不存在")
    cid = ctx.bi("component_id", 0)
    if not ctx.con.execute("SELECT 1 FROM component WHERE id=?", (cid,)).fetchone():
        raise ApiError(404, "替代料不存在")
    try:
        cur = ctx.con.execute(
            "INSERT INTO bom_substitute(bom_id, component_id, note) VALUES(?,?,?)",
            (bid, cid, ctx.b("note")))
    except Exception:
        raise ApiError(400, "这个替代料已经加过了")
    ctx.con.commit()
    return 201, {"id": int(cur.lastrowid)}


@route("DELETE", r"/api/substitutes/(\d+)")
def delete_substitute(ctx: Ctx, m):
    ctx.con.execute("DELETE FROM bom_substitute WHERE id=?", (int(m.group(1)),))
    ctx.con.commit()
    return 200, {"ok": True}


# ---------------------------------------------------------------- 采购与在途

PURCHASE_STATUS = {"todo": "想买", "ordered": "已下单", "arrived": "已到货",
                   "cancel": "已取消"}


@route("GET", r"/api/purchase")
def list_purchase(ctx: Ctx, m):
    where, args = [], []
    status = ctx.q("status")
    if status in PURCHASE_STATUS:
        where.append("pu.status = ?")
        args.append(status)
    if ctx.qi("component_id", 0):
        where.append("pu.component_id = ?")
        args.append(ctx.qi("component_id"))
    sql = f"""SELECT pu.*, c.name, c.lcsc_pn, c.mpn, c.category, c.value, c.package, c.unit,
                     c.unit_price AS ref_price, p.name AS project_name,
                     (pu.qty - pu.received) AS outstanding,
                     (pu.qty * pu.unit_price) AS amount
                FROM purchase pu
                JOIN component c ON c.id = pu.component_id
                LEFT JOIN project p ON p.id = pu.project_id
               {'WHERE ' + ' AND '.join(where) if where else ''}
               ORDER BY CASE pu.status WHEN 'ordered' THEN 0 WHEN 'todo' THEN 1
                                       WHEN 'arrived' THEN 2 ELSE 3 END,
                        pu.id DESC"""
    rows = ctx.con.execute(sql, args).fetchall()
    items = [db.row_to_dict(r) for r in rows]
    for it in items:
        it["status_label"] = PURCHASE_STATUS.get(it["status"], it["status"])
    return 200, {"items": items, "total": len(items)}


def _make_purchase(con, component_id, qty, unit_price=None, supplier=None,
                   status="todo", project_id=None, note=None):
    if unit_price in (None, ""):
        row = con.execute("SELECT unit_price, supplier FROM component WHERE id=?",
                          (component_id,)).fetchone()
        unit_price = float(row["unit_price"] or 0) if row else 0.0
        supplier = supplier or (row["supplier"] if row else None)
    cur = con.execute(
        """INSERT INTO purchase(component_id, qty, unit_price, supplier, status,
                                project_id, note, ordered_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (component_id, int(qty), float(unit_price or 0), supplier, status,
         project_id, note, db.now() if status == "ordered" else None))
    return int(cur.lastrowid)


@route("POST", r"/api/purchase")
def create_purchase(ctx: Ctx, m):
    """新建采购单。可以传一条,也可以传 items 批量(「一键按建议采购」用它)。"""
    status = ctx.b("status") or "todo"
    if status not in PURCHASE_STATUS:
        raise ApiError(400, f"状态只能是 {'/'.join(PURCHASE_STATUS)}")
    batch = ctx.b("items")
    if batch:
        ids = []
        for it in batch:
            cid = int(it.get("component_id") or 0)
            qty = int(it.get("qty") or 0)
            if not cid or qty <= 0:
                continue
            ids.append(_make_purchase(
                ctx.con, cid, qty, it.get("unit_price"), it.get("supplier"),
                it.get("status") or status, it.get("project_id") or ctx.b("project_id"),
                it.get("note")))
        if not ids:
            raise ApiError(400, "没有有效的采购项")
        ctx.con.commit()
        return 201, {"ok": True, "ids": ids, "count": len(ids)}

    cid = ctx.bi("component_id", 0)
    if not ctx.con.execute("SELECT 1 FROM component WHERE id=?", (cid,)).fetchone():
        raise ApiError(404, "元件不存在")
    qty = ctx.bi("qty", 0)
    if qty <= 0:
        raise ApiError(400, "数量要大于 0")
    pid = _make_purchase(ctx.con, cid, qty, ctx.b("unit_price"), ctx.b("supplier"),
                         status, ctx.b("project_id"), ctx.b("note"))
    ctx.con.commit()
    return 201, {"id": pid}


@route("PUT", r"/api/purchase/(\d+)")
def update_purchase(ctx: Ctx, m):
    pid = int(m.group(1))
    row = ctx.con.execute("SELECT * FROM purchase WHERE id=?", (pid,)).fetchone()
    if not row:
        raise ApiError(404, "采购单不存在")
    sets, args = [], []
    if "qty" in ctx.body:
        qty = ctx.bi("qty", 0)
        if qty <= 0:
            raise ApiError(400, "数量要大于 0")
        if qty < int(row["received"]):
            raise ApiError(400, f"已经收货 {row['received']},数量不能改到比它小")
        sets.append("qty=?")
        args.append(qty)
    if "unit_price" in ctx.body:
        sets.append("unit_price=?")
        args.append(float(ctx.body["unit_price"] or 0))
    for key in ("supplier", "note"):
        if key in ctx.body:
            sets.append(f"{key}=?")
            args.append(ctx.b(key))
    if "status" in ctx.body:
        status = ctx.b("status")
        if status not in PURCHASE_STATUS:
            raise ApiError(400, f"状态只能是 {'/'.join(PURCHASE_STATUS)}")
        sets.append("status=?")
        args.append(status)
        if status == "ordered" and not row["ordered_at"]:
            sets.append("ordered_at=?")
            args.append(db.now())
    if not sets:
        return 200, {"ok": True, "unchanged": True}
    args.append(pid)
    ctx.con.execute(f"UPDATE purchase SET {', '.join(sets)} WHERE id=?", args)
    ctx.con.commit()
    return 200, {"ok": True}


@route("POST", r"/api/purchase/(\d+)/receive")
def receive_purchase(ctx: Ctx, m):
    """到货入库:把数量真正加进库存,并记一条带采购单号的入库流水。

    支持分批:收一部分就先记一部分,在途数量跟着变小;收满才标成「已到货」。
    """
    pid = int(m.group(1))
    row = ctx.con.execute("SELECT * FROM purchase WHERE id=?", (pid,)).fetchone()
    if not row:
        raise ApiError(404, "采购单不存在")
    if row["status"] == "cancel":
        raise ApiError(409, "已取消的采购单不能入库")

    left = int(row["qty"]) - int(row["received"])
    if left <= 0:
        raise ApiError(409, "这一单已经全部到货了")
    qty = ctx.bi("qty", left) or left
    if qty <= 0:
        raise ApiError(400, "到货数量要大于 0")
    if qty > left:
        raise ApiError(400, f"到货数量超过未收数量(还剩 {left})")

    loc = ctx.b("location") or ctx.b("location_id")
    if loc in (None, ""):
        # 没指定就放这个元件的默认仓位,再没有就放「未分类」。
        # 这样收货不用每次都手选仓位。
        comp = ctx.con.execute("SELECT default_loc_id FROM component WHERE id=?",
                               (row["component_id"],)).fetchone()
        loc = comp["default_loc_id"] if comp and comp["default_loc_id"] else "未分类"
    loc_id = _get_location_id(ctx.con, loc)

    _bump(ctx.con, row["component_id"], loc_id, qty)
    got = int(row["received"]) + qty
    done = got >= int(row["qty"])
    ctx.con.execute(
        "UPDATE purchase SET received=?, status=?, arrived_at=? WHERE id=?",
        (got, "arrived" if done else "ordered",
         (row["arrived_at"] or db.now()) if done else None, pid))
    ctx.con.execute(
        """INSERT INTO movement(kind, component_id, location_id, qty, project_id,
                                purchase_id, ref, operator, note)
           VALUES('IN',?,?,?,?,?,?,?,?)""",
        (row["component_id"], loc_id, qty, row["project_id"], pid,
         ctx.b("ref") or f"PO-{pid}", ctx.b("operator") or "本地用户",
         f"采购到货({row['supplier'] or '未填供应商'})"))
    db.touch_component(ctx.con, row["component_id"])
    ctx.con.commit()
    return 200, {"ok": True, "received": got, "outstanding": int(row["qty"]) - got,
                 "status": "arrived" if done else "ordered"}


@route("DELETE", r"/api/purchase/(\d+)")
def delete_purchase(ctx: Ctx, m):
    ctx.con.execute("DELETE FROM purchase WHERE id=?", (int(m.group(1)),))
    ctx.con.commit()
    return 200, {"ok": True}


# ---------------------------------------------------------------- 该买什么 / 总览


def _shopping_rows(con) -> list:
    """算出「该买什么」。已经在途的扣掉了,所以不会重复建议同一批货。"""
    rows = con.execute(
        f"SELECT * FROM ({COMPONENT_SELECT}) "
        "WHERE to_order > 0 OR on_order > 0 "
        "ORDER BY category, value_num, value, name").fetchall()
    out = []
    for r in rows:
        d = component_row(r)
        # 说清「为什么要买」,免得只看到一个数字不知道为什么
        reasons = []
        if d["deficit"] > 0:
            reasons.append(f"项目缺料 {d['deficit']}")
        if d["min_stock"] and d["on_hand"] < d["min_stock"]:
            reasons.append(f"低于安全库存 {d['min_stock'] - d['on_hand']}")
        if d["on_order"]:
            reasons.append(f"在途 {d['on_order']}")
        d["reasons"] = reasons
        d["buy_qty"] = d["to_order"]
        d["amount"] = round(d["buy_qty"] * float(d["unit_price"] or 0), 2)
        out.append(d)
    return out


@route("GET", r"/api/shopping")
def shopping_list(ctx: Ctx, m):
    items = _shopping_rows(ctx.con)
    return 200, {
        "items": items,
        "total": len(items),
        "total_qty": sum(i["buy_qty"] for i in items),
        "total_amount": round(sum(i["amount"] for i in items), 2),
        "on_order_qty": sum(i["on_order"] for i in items),
    }


@route("GET", r"/api/dashboard")
def dashboard(ctx: Ctx, m):
    """总览:一眼看清「我有什么 / 要做什么 / 该买什么」。"""
    agg = ctx.con.execute(
        f"""SELECT COUNT(*) AS kinds,
                   COALESCE(SUM(on_hand),0) AS qty,
                   COALESCE(SUM(on_hand * unit_price),0) AS value,
                   COALESCE(SUM(CASE WHEN on_hand = 0 THEN 1 ELSE 0 END),0) AS out_kinds,
                   COALESCE(SUM(CASE WHEN on_hand > 0 AND min_stock > 0
                                      AND on_hand < min_stock THEN 1 ELSE 0 END),0) AS low_kinds
              FROM ({COMPONENT_SELECT})""").fetchone()

    buy = _shopping_rows(ctx.con)
    on_order = ctx.con.execute(
        """SELECT COALESCE(SUM(qty - received),0) AS qty,
                  COALESCE(SUM((qty - received) * unit_price),0) AS amount
             FROM purchase WHERE status='ordered'""").fetchone()

    projects = []
    for r in ctx.con.execute("SELECT * FROM project WHERE status='active' ORDER BY id DESC"):
        rep = bom.build_report(ctx.con, r["id"])
        projects.append({
            "id": r["id"], "name": r["name"], "qty": int(r["qty"] or 1),
            "bom_lines": rep["line_count"], "can_build": rep["can_build"],
            "shortage_lines": rep["shortage_lines"],
            "shortage_value": rep["shortage_value"], "ready": rep["ready"],
        })

    recent = [db.row_to_dict(r) for r in ctx.con.execute(
        """SELECT mv.id, mv.kind, mv.qty, mv.created_at, mv.note, mv.project_id,
                  c.name AS component_name, c.value, c.package, c.category,
                  l.code AS location_code, p.name AS project_name
             FROM movement mv
             JOIN component c ON c.id = mv.component_id
             LEFT JOIN location l ON l.id = mv.location_id
             LEFT JOIN project p ON p.id = mv.project_id
            ORDER BY mv.id DESC LIMIT 12""")]

    return 200, {
        "kinds": int(agg["kinds"]),
        "total_qty": int(agg["qty"]),
        "total_value": round(float(agg["value"]), 2),
        "out_kinds": int(agg["out_kinds"]),
        "low_kinds": int(agg["low_kinds"]),
        "buy_kinds": len(buy),
        "buy_qty": sum(i["buy_qty"] for i in buy),
        "buy_amount": round(sum(i["amount"] for i in buy), 2),
        "on_order_qty": int(on_order["qty"]),
        "on_order_amount": round(float(on_order["amount"]), 2),
        "projects": projects,
        "recent": recent,
    }


def _read_upload(ctx: Ctx) -> tuple[str, bytes]:
    """取出上传的 BOM 文件内容。支持 multipart 表单或直接放路径。"""
    up = ctx.upload or {}
    data = up.get("data")
    if data:
        return up.get("filename") or "upload.xlsx", data
    path = ctx.b("path")
    if path:
        path = os.path.abspath(os.path.expanduser(str(path)))
        if not os.path.isfile(path):
            raise ApiError(400, f"文件不存在:{path}")
        with open(path, "rb") as f:
            return os.path.basename(path), f.read()
    raise ApiError(400, "没有收到文件:请上传 .xlsx,或提供 path 参数")


def _save_temp_upload(data: bytes, filename: str) -> str:
    """把上传内容写进项目内的 inbox 目录(不用系统临时目录)。"""
    inbox = os.path.join(PROJECT_ROOT, "data", "inbox")
    os.makedirs(inbox, exist_ok=True)
    safe = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", filename) or "upload.xlsx"
    dest = os.path.join(inbox, safe)
    with open(dest, "wb") as f:
        f.write(data)
    return dest


@route("POST", r"/api/bom/preview")
def bom_preview(ctx: Ctx, m):
    filename, data = _read_upload(ctx)
    if not (filename.lower().endswith((".xlsx", ".xlsm"))):
        raise ApiError(400, "只支持 .xlsx / .xlsm(Altium 的 Excel 导出)")
    path = _save_temp_upload(data, filename)
    try:
        items, warnings = bom.parse_workbook(path, sheet_name=ctx.b("sheet"))
    except Exception as exc:
        raise ApiError(400, f"解析失败:{exc}")
    return 200, {
        "filename": filename, "saved_to": path,
        "sheets": xlsx.sheet_names(path),
        "line_count": len(items), "total_qty": sum(i["qty"] for i in items),
        "warnings": warnings,
        "lines": [{
            "lcsc_pn": i["lcsc_pn"], "mpn": i["mpn"], "name": i["name"],
            "category": i["category"], "package": i["package"], "value": i["value"],
            "qty": i["qty"], "designators": ",".join(i["designators"]),
        } for i in items],
    }


@route("POST", r"/api/bom/import")
def bom_import(ctx: Ctx, m):
    filename, data = _read_upload(ctx)
    if not (filename.lower().endswith((".xlsx", ".xlsm"))):
        raise ApiError(400, "只支持 .xlsx / .xlsm")
    path = _save_temp_upload(data, filename)
    project_name = ctx.b("project_name") or os.path.splitext(filename)[0]
    try:
        report = bom.import_bom(
            ctx.con, path, project_name=project_name,
            project_code=ctx.b("project_code"), repo=ctx.b("repo"),
            sheet_name=ctx.b("sheet"),
            replace_existing=bool(ctx.b("replace", True)),
        )
    except Exception as exc:
        raise ApiError(400, f"导入失败:{exc}")
    report["filename"] = filename
    report["saved_to"] = path
    report["shortage"] = bom.shortage_report(ctx.con, report["project_id"])
    return 200, report


# ---------------------------------------------------------------- multipart


def parse_multipart(body: bytes, content_type: str) -> tuple[dict, dict | None]:
    """极简 multipart/form-data 解析,内存内完成,不碰系统临时目录。"""
    m = re.search(r"boundary=([^;]+)", content_type or "")
    if not m:
        return {}, None
    boundary = m.group(1).strip().strip('"').encode()
    delimiter = b"--" + boundary
    fields: dict[str, str] = {}
    upload: dict | None = None

    for part in body.split(delimiter):
        if not part or part in (b"--", b"--\r\n", b"\r\n"):
            continue
        part = part.lstrip(b"\r\n")
        head_end = part.find(b"\r\n\r\n")
        if head_end < 0:
            continue
        raw_headers, content = part[:head_end], part[head_end + 4:]
        if content.endswith(b"\r\n"):
            content = content[:-2]
        headers = {}
        for line in raw_headers.split(b"\r\n"):
            if b":" in line:
                k, v = line.split(b":", 1)
                headers[k.strip().lower().decode("latin-1")] = v.strip().decode("latin-1")

        disp = headers.get("content-disposition", "")
        name_m = re.search(r'name="([^"]*)"', disp)
        if not name_m:
            continue
        field = name_m.group(1)
        file_m = re.search(r'filename="([^"]*)"', disp)
        if file_m:
            upload = {"field": field, "filename": os.path.basename(file_m.group(1)),
                      "data": content}
        else:
            charset = "utf-8"
            cm = re.search(r"charset=([\w\-]+)", headers.get("content-type", ""))
            if cm:
                charset = cm.group(1)
            try:
                fields[field] = content.decode(charset)
            except (UnicodeDecodeError, LookupError):
                fields[field] = content.decode("utf-8", "replace")
    return fields, upload


# ---------------------------------------------------------------- HTTP 层


class Handler(BaseHTTPRequestHandler):
    server_version = "PartsManager/1.0"
    protocol_version = "HTTP/1.1"
    db_path = DEFAULT_DB

    def log_message(self, fmt, *args):
        # 只有「界面发来的请求」才算活跃。
        # 启动器自己每 3 秒轮询一次 /api/health 用来探活;如果那个也算数,
        # 空闲看门狗就永远等不到空闲,服务会赖在后台不退出。
        line = fmt % args
        if "/api/health" not in line:
            LAST_ACTIVE[0] = time.time()
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), line))

    # ---- 基础响应

    def _send(self, status: int, body: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj: Any):
        payload = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, payload, "application/json; charset=utf-8")

    def _static(self, rel: str):
        rel = rel.lstrip("/") or "index.html"
        path = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not path.startswith(STATIC_DIR) or not os.path.isfile(path):
            self._json(404, {"error": "not found"})
            return
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        with open(path, "rb") as f:
            self._send(200, f.read(), ctype)

    # ---- 分发

    def _handle(self, method: str):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        query = urllib.parse.parse_qs(parsed.query)

        # 删除不可逆:动库之前先整库快照一份,误删可直接从 data/backups 回滚。
        # 快照失败绝不阻断请求(backup_db 内部已吞掉 IO 异常)。
        if method == "DELETE" and path.startswith("/api/"):
            backup_db(self.db_path)

        if not path.startswith("/api/"):
            if method in ("GET", "HEAD"):
                self._static(path)
            else:
                self._json(405, {"error": "method not allowed"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_UPLOAD:
            self._json(413, {"error": "上传内容过大"})
            return
        raw = self.rfile.read(length) if length else b""

        body: dict = {}
        upload = None
        ctype = self.headers.get("Content-Type") or ""
        if raw:
            if ctype.startswith("multipart/form-data"):
                body, upload = parse_multipart(raw, ctype)
            else:
                try:
                    body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
                except (UnicodeDecodeError, ValueError):
                    body = {}
                if not isinstance(body, dict):
                    body = {"value": body}

        con = db.connect(self.db_path)
        try:
            con.execute("BEGIN")
            ctx = Ctx(self, con, query, body, upload)
            for route_method, pattern, fn in ROUTES:
                if route_method != method:
                    continue
                match = pattern.match(path)
                if match:
                    status, obj = fn(ctx, match)
                    self._json(status, obj)
                    return
            self._json(404, {"error": f"未知接口:{method} {path}"})
        except ApiError as exc:
            try:
                con.rollback()
            except Exception:
                pass
            self._json(exc.status, {"error": exc.message})
        except Exception as exc:
            try:
                con.rollback()
            except Exception:
                pass
            traceback.print_exc()
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
        finally:
            con.close()

    def do_GET(self):
        self._handle("GET")

    def do_HEAD(self):
        self._handle("HEAD")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")


def backup_db(db_path: str, keep: int = 20) -> str | None:
    """启动时把数据库快照到 data/backups/,只保留最近 keep 份。单文件数据库复制即可。"""
    if not os.path.isfile(db_path):
        return None
    stamp = __import__("datetime").datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = os.path.join(os.path.dirname(db_path), "backups")
    os.makedirs(backup_dir, exist_ok=True)
    dest = os.path.join(backup_dir, f"parts_{stamp}.db")
    try:
        with open(db_path, "rb") as src, open(dest, "wb") as dst:
            dst.write(src.read())
    except OSError as exc:
        print(f"[备份] 跳过: {exc}")
        return None

    olds = sorted(
        (f for f in os.listdir(backup_dir) if f.startswith("parts_") and f.endswith(".db")),
        reverse=True,
    )
    for name in olds[keep:]:
        try:
            os.remove(os.path.join(backup_dir, name))
        except OSError:
            pass
    return dest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="元器件物料管理系统")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1", help="默认只监听本机")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--no-backup", action="store_true", help="启动时不自动备份")
    ap.add_argument("--idle-exit", type=int, default=0, metavar="SEC",
                    help="便携版用:连续 SEC 秒没有任何请求就自动退出(0=常驻不退出)")
    args = ap.parse_args(argv)

    if not args.no_backup:
        made = backup_db(args.db)
        if made:
            print(f"[备份] 已创建 {os.path.basename(made)}")

    con = db.connect(args.db)
    db.init_db(con)
    db.backfill_values(con)
    con.close()

    # 便携版:界面关掉后自动退出,不留后台进程
    start_idle_watchdog(args.idle_exit)

    Handler.db_path = args.db
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}/"
    print("=" * 64)
    print("  元器件物料管理系统 已启动")
    print(f"  浏览器打开: {url}")
    print(f"  数据库文件: {args.db}")
    print("-" * 64)
    print("  [注意] 请保持本窗口开着 —— 关掉它服务就停了,网页会打不开。")
    print("         网页报 \"连不上服务端\" 通常就是本窗口被关了。")
    print("         停止服务:在本窗口按 Ctrl+C,或直接关闭窗口。")
    print("=" * 64)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
