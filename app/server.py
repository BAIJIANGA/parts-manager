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


# ---------------------------------------------------------------- 元件

COMPONENT_SELECT = """
SELECT c.*,
       COALESCE((SELECT SUM(s.qty) FROM stock s WHERE s.component_id = c.id), 0) AS on_hand,
       CASE
         WHEN COALESCE((SELECT SUM(s.qty) FROM stock s WHERE s.component_id = c.id), 0) = 0 THEN 'out'
         WHEN COALESCE((SELECT SUM(s.qty) FROM stock s WHERE s.component_id = c.id), 0) < c.min_stock THEN 'low'
         ELSE 'ok'
       END AS stock_state
FROM component c
"""


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
        where.append(
            "(c.name LIKE ? OR c.lcsc_pn LIKE ? OR c.mpn LIKE ? OR c.manufacturer LIKE ?"
            " OR c.value LIKE ? OR c.package LIKE ? OR c.note LIKE ?)"
        )
        args += [like] * 7
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
                                params, datasheet_url, product_url, unit, min_stock, note)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ctx.b("lcsc_pn"), ctx.b("mpn"), ctx.b("manufacturer"), name,
         ctx.b("category") or "其他", ctx.b("value"), ctx.b("package"),
         db.dump_params(ctx.b("params")), ctx.b("datasheet_url"), ctx.b("product_url"),
         ctx.b("unit") or "个", ctx.bi("min_stock", 0) or 0, ctx.b("note")),
    )
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
        "package": ctx.b("package"), "datasheet_url": ctx.b("datasheet_url"),
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
    if not sets:
        return 200, {"ok": True, "unchanged": True}

    sets.append("updated_at=?")
    args.extend([db.now(), cid])
    try:
        ctx.con.execute(f"UPDATE component SET {', '.join(sets)} WHERE id=?", args)
    except Exception as exc:  # 唯一约束等
        raise ApiError(400, f"保存失败:{exc}")
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
        "locations": [db.row_to_dict(r) for r in ctx.con.execute(
            "SELECT * FROM location ORDER BY code")],
    }


# ---------------------------------------------------------------- 仓位与出入库


@route("POST", r"/api/locations")
def create_location(ctx: Ctx, m):
    code = str(ctx.require("code")).strip()
    try:
        cur = ctx.con.execute("INSERT INTO location(code, note) VALUES(?,?)",
                              (code, ctx.b("note")))
    except Exception:
        raise ApiError(400, f"仓位 {code} 已存在")
    ctx.con.commit()
    return 201, {"id": int(cur.lastrowid)}


@route("DELETE", r"/api/locations/(\d+)")
def delete_location(ctx: Ctx, m):
    lid = int(m.group(1))
    used = ctx.con.execute("SELECT COUNT(*) AS n FROM stock WHERE location_id=? AND qty<>0",
                           (lid,)).fetchone()["n"]
    if used:
        raise ApiError(409, "该仓位还有库存,不能删除")
    ctx.con.execute("DELETE FROM location WHERE id=?", (lid,))
    ctx.con.commit()
    return 200, {"ok": True}


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


def _bump(con, component_id: int, location_id: int, delta: int) -> int:
    """在事务内给余额加减,返回变动后的数量。禁止负库存。"""
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

    loc = _get_location_id(ctx.con, ctx.b("location") or ctx.b("location_id"))
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
    rows = ctx.con.execute(
        """SELECT p.*,
                  (SELECT COUNT(*) FROM project_bom b WHERE b.project_id=p.id) AS bom_lines,
                  (SELECT COALESCE(SUM(required_qty),0) FROM project_bom b
                    WHERE b.project_id=p.id) AS required_qty
           FROM project p ORDER BY p.id DESC""")
    return 200, {"items": [db.row_to_dict(r) for r in rows]}


@route("POST", r"/api/projects")
def create_project(ctx: Ctx, m):
    cur = ctx.con.execute("INSERT INTO project(name, code, repo, note) VALUES(?,?,?,?)",
                          (ctx.require("name"), ctx.b("code"), ctx.b("repo"), ctx.b("note")))
    ctx.con.commit()
    return 201, {"id": int(cur.lastrowid)}


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
        rep = bom.shortage_report(ctx.con, pid)
        plan = []
        for line in rep["lines"]:
            need = line["required_qty"] - line["placed_qty"]
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
