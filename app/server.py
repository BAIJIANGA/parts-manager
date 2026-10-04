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

import attrs  # noqa: E402
import bom  # noqa: E402
import db  # noqa: E402
import footprint  # noqa: E402
import values  # noqa: E402
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

# 品类下拉的选项。前一段直接取自 bom.CATEGORIES —— 那边是「BOM 导入时能推断出
# 哪些品类」的唯一来源,两处各维护一份的话,上游认出来的词在界面上会选不到。
# 后面几个是手工录入才会用到的(推断不出来,只能人填)。
CATEGORY_SUGGESTIONS = list(bom.CATEGORIES) + ["传感器", "模块", "结构件"]
# 没品类的地方,界面统一显示成这个。**后端不再自动建这一行品类了** ——
# 删掉一个顶层品类时,底下元件的 category_id 置 NULL、category 文本置空,
# 由界面拿这个词去显示(issue #32:品类是用户自己的,后端不许往他的树里塞行)。
# 保留这个常量是因为「界面/报表用同一个词」这件事仍然成立,而且外部可能还在引用。
UNCATEGORIZED = "未分类"

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
         -- 库存状态**只跟安全库存比**(issue #38):库存 ≤ 安全库存才算缺料。
         -- 这里以前还有一档 "on_hand < required THEN 'short'"(拿 BOM 需求量当缺料),
         -- 那是**项目 BOM 的口径**,不是库存本身的事 —— 库里的料明明够,只因为某个
         -- 项目 BOM 要得比手上多就被打成「缺料」,正是用户被误导的来源(见 gui.py 里
         -- 「需求/缺口/在途该摆在项目页,不是库存页」那段注释)。采购页要的
         -- target/to_order 仍照旧用 required 算,不受这里影响。
         WHEN {ON_HAND_SQL} = 0 THEN 'out'
         WHEN {ON_HAND_SQL} <= c.min_stock THEN 'low'
         ELSE 'ok'
       END AS stock_state
FROM component c
"""

# 库存状态的显示名,界面和报表共用。只剩三档:缺料就是"到安全库存了"(含相等)。
STATE_LABEL = {"ok": "充足", "low": "缺料", "out": "缺货"}
KIND_LABEL = {"IN": "入库", "OUT": "出库", "ADJUST": "盘点", "TRANSFER": "移库"}


def _qty_at(con, component_id: int, location_id) -> int:
    """某个元件在某个仓位上的现有数量。没有那一行就是 0。"""
    if location_id is None:
        return 0
    row = con.execute("SELECT qty FROM stock WHERE component_id=? AND location_id=?",
                      (component_id, location_id)).fetchone()
    return int(row["qty"]) if row else 0


def component_row(row) -> dict:
    d = db.row_to_dict(row)
    d["params"] = db.parse_params(d.get("params"))
    return d


# 封装名归一化:去掉分隔符和空格再比,好让 C0805 / c-0805 / 0805 这些写法能对上
# 封装归一化的实现已经挪到 bom.py(身份键要用同一套规则,两处各写一份迟早漂移),
# 这里保留同名入口,server 内部和自检里的调用都不用改。
_PKG_SEP_RE = re.compile(r"[\s\-_/]+")


def norm_package(text) -> str:
    """封装归一化。实现见 bom.norm_package(身份键和相似度打分必须用同一套规则)。"""
    return bom.norm_package(text)


def _value_hits(text, want_num) -> bool:
    """这个「值」是不是和要找的数值一样(写法可以不同:4.7k 与 4.7kΩ)。

    单独抽出来是给「能不能靠封装索引提前收工」那个判断用的 —— 判断和打分
    必须用同一套比较,否则收工条件会算错,候选就少了。
    """
    if want_num is None:
        return False
    num, _unit = values.parse_value(text or "")
    if num is None:
        return False
    # 相对容差:4.7kΩ 解析出来是 4700.000000000001 这种也别漏判
    return abs(num - want_num) <= abs(want_num) * 1e-9 + 1e-15


def similar_components(con, *, value="", package="", category="", limit=8,
                       in_stock_only=False) -> list[dict]:
    """按「值 + 封装」找相似元件,给人确认用。

    为什么需要:导出的 BOM 常常只有值和封装,料号、位号都不全,而且同一样东西
    不同工具的写法还不一样(4.7k / 4.7kΩ / 4700;C0805 / 0805 / C-0805)。
    严格相等找不到,人就得自己在几百行里翻。

    打分:
      值和单位都相同      +60
      数值相同(写法不同)  +50   4.7k 与 4.7kΩ 属于这一类
      封装尺寸相同        +30   C0805 与 0805 是同一颗(立创导出就写 C0805)
      封装写法相近        +15   一方包含另一方,比如 CAP-0805 与 0805
      品类相同            +10
    得分越高越像,但**只是像**。界面必须把「值对上了、封装要自己看」这种区别说清楚,
    不能笼统地显示一个「匹配」。

    两条实现上的讲究:
      * 封装比较走 footprint:同尺寸不同写法(C0805 / R0805 / 1005 / 0805)
        一律算「封装一样」—— 它们物理上是同一个尺寸,能不能互换看的就是尺寸。
      * 先用 package_key 索引把范围缩到同一尺寸的那些行,再去算分。几百上千颗
        电容的时候,全表逐行算和走索引拿几十行是两种手感。索引里一条都没命中
        (老库还没回填、或者封装那一栏根本没写)才退回全表,免得漏掉候选。
    """
    want_num, want_unit = values.parse_value(value)
    want_pkg = norm_package(package)
    # 尺寸级的封装键。「C0805 和 0805 是不是同一个封装」只有 footprint 说了算,
    # 认不出来(比如 DIP-8)就是空串,后面退回字面比较,行为和以前一模一样。
    want_fp = footprint.size(package)
    want_cat = str(category or "").strip()

    where = " WHERE c.merged_into IS NULL"
    rows = None
    if want_fp:
        # 走封装索引先把同一尺寸的那批拿出来。**注意这只用来提前收工,
        # 不能用来缩小候选范围** —— 值对得上、封装不同的料也必须列出来
        # (判定会写着「值对上了,封装要自己看」),那正是最容易发错货的地方。
        fast = con.execute(COMPONENT_SELECT + where + " AND c.package_key=?",
                           (want_fp,)).fetchall()
        # 安全收工的条件:同尺寸 + 值也命中的候选一定 >= 90 分(60 + 30);
        # 别的尺寸最多 60 + 15(写法相近)+ 10(品类相同)= 85,抢不到前面去。
        # 凑够 limit 条就不必再扫全表了;凑不够就老实全表扫,一条都不会少。
        enough = sum(
            1 for r in fast
            if (not in_stock_only or (r["on_hand"] or 0) > 0)
            and _value_hits(r["value"], want_num))
        if enough >= limit:
            rows = fast
    if rows is None:
        rows = con.execute(COMPONENT_SELECT + where).fetchall()

    out: list[dict] = []
    for r in rows:
        score, why = 0, []
        value_hit = False

        num, unit = values.parse_value(r["value"] or "")
        if want_num is not None and num is not None:
            # 相对容差:4.7kΩ 解析出来是 4700.000000000001 这种也别漏判
            if abs(num - want_num) <= abs(want_num) * 1e-9 + 1e-15:
                value_hit = True
                score += 60
                # 两边都写了单位而且一样,才叫「单位和值都相同」。
                # 4.7k 和 4700 谁都没写单位,这时说「单位相同」是假话,
                # 而且会把它排到 4.7kΩ 前面去 —— 明明后者写得还更全。
                if want_unit and unit and want_unit == unit:
                    why.append("值和单位都相同")
                else:
                    why.append("数值相同(写法不同)")

        got_pkg = norm_package(r["package"] or "")
        got_fp = footprint.size(r["package"] or "") or (r["package_key"] or "")
        fp_hit = bool(want_fp) and got_fp == want_fp
        if fp_hit and got_pkg == want_pkg:
            score += 30
            why.append("封装相同")
        elif fp_hit:
            # 尺寸一样就是同一个封装,只是写法不同(C0805 与 0805、1005 与 0402)。
            # 把尺寸写出来,用户才知道凭什么说它们一样。
            score += 30
            why.append(f"封装一样({want_fp})")
        elif want_pkg and got_pkg:
            if got_pkg == want_pkg:
                score += 30
                why.append("封装相同")
            elif want_pkg in got_pkg or got_pkg in want_pkg:
                score += 15
                why.append("封装写法相近")

        if want_cat and (r["category"] or "") == want_cat:
            score += 10
            why.append("品类相同")

        if score <= 0:
            continue
        # 给了值却对不上值的,不算候选。只对上封装的,在「值」这个主判据上彻底错了 ——
        # 100nF 0603 不可能是 4.7k 0603 的替代,把它列进候选只是噪音。
        # 值本身解析不出来时(比如那一列填的是料号)才允许只靠封装匹配。
        if want_num is not None and not value_hit:
            continue
        d = component_row(r)
        if in_stock_only and not d["on_hand"]:
            continue
        if score >= 90:
            verdict = "很可能是同一颗"
        elif score >= 50:
            verdict = "值对上了,封装要自己看"
        else:
            # 只对上了封装(值还没对上)。封装一样的话,这一句比「只是有点像」
            # 有用得多 —— 至少知道它装得上去。
            verdict = "封装一样,值还没对上" if fp_hit else "只是有点像"
        d["score"] = score
        d["match"] = "、".join(why)
        d["verdict"] = verdict
        # 界面要单独标「封装一致」并把它们排在前面:出库发错货,十有八九就是
        # 值一样、封装不一样(或反过来)的那两颗。
        d["fp_match"] = fp_hit
        out.append(d)

    # 同样像的时候:封装一致的排前面(能直接装上去),再比有没有库存
    # —— 找相似多半是为了出库,没库存的帮不上忙。
    out.sort(key=lambda c: (-c["score"], -int(c.get("fp_match") or 0),
                            -(c["on_hand"] or 0), c["name"] or ""))
    return out[:max(1, limit)]


@route("GET", r"/api/components/similar")
def components_similar(ctx: Ctx, m):
    try:
        limit = int(ctx.q("limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    q = {"value": ctx.q("value") or "", "package": ctx.q("package") or "",
         "category": ctx.q("category") or ""}
    return 200, {
        "items": similar_components(
            ctx.con, value=q["value"], package=q["package"], category=q["category"],
            limit=max(1, min(limit, 50)),
            in_stock_only=ctx.q("stocked") in ("1", "true", "yes")),
        "query": q,
        "hint": "按「值 + 封装」算的相似度,越大越像;是不是同一颗料要你自己确认。",
    }


@route("GET", r"/api/components")
def list_components(ctx: Ctx, m):
    # base = 关键字 / 品类 / 厂家。分面(封装、单位)只按 base 算,不把用户已经选中的
    # 封装、单位、值区间也叠进去 —— 否则选定一个封装之后别的封装就从下拉里消失,
    # 想换个封装看看都不行了。
    base_where, base_args = ["c.merged_into IS NULL"], []
    keyword = ctx.q("q")
    if keyword:
        like = f"%{keyword}%"
        # 搜索是主入口,不是分类的补充 —— 所以凡是用户可能记得的碎片都要命中:
        # 名称、商品编号、厂家料号、厂家、值、封装、**丝印**、**参数 JSON**、备注、品类。
        # 丝印那一条是给拆机料用的:SOT-23 上只印着三个字母,查不到就等于没存。
        # 最后那一条是封装索引键:搜「C0805」要能搜到写成「0805」的那颗,
        # 搜「1005」也要能搜到 0402 —— 立创导出写 C0805、手写常常就是 0805,
        # 用户心里它们是同一个东西。
        base_where.append(
            "(c.name LIKE ? OR c.lcsc_pn LIKE ? OR c.mpn LIKE ? OR c.manufacturer LIKE ?"
            " OR c.value LIKE ? OR c.package LIKE ? OR c.marking LIKE ? OR c.params LIKE ?"
            " OR c.note LIKE ? OR c.category LIKE ? OR c.package_key = ?)"
        )
        base_args += [like] * 10 + [footprint.canon(keyword)]
    if ctx.q("category"):
        base_where.append("c.category = ?")
        base_args.append(ctx.q("category"))
    # category_id:按**整棵子树**筛。菜单上点「电容」该看到它底下所有子类的料,
    # 点「无极性陶瓷电容」则只看那一支。文本列做不到这件事 —— 挂在子类下的元件,
    # 文本仍然写着「电容」,光看文本分不出是挂在子类还是直接挂在顶层。
    # own=1:只要**直接挂在这一个节点上**的元件,不含子孙。
    # 「大类下面既有细分出来的子类、又有还没细分的料」是很常见的摆法,
    # 那些料必须有自己的入口,否则它们永远看不到(见 issue #12)。
    if ctx.q("own") and ctx.q("category_id"):
        base_where.append("c.category_id = ?")
        base_args.append(ctx.qi("category_id", 0) or 0)
    if ctx.q("category_id"):
        _sub = _category_subtree(ctx, ctx.qi("category_id", 0) or 0)
        if _sub:
            base_where.append("c.category_id IN (%s)" % ",".join("?" * len(_sub)))
            base_args.extend(_sub)
        else:
            base_where.append("1 = 0")      # 节点不存在 = 什么都别给,别退化成「全部」
    if ctx.q("manufacturer"):
        base_where.append("c.manufacturer LIKE ?")
        base_args.append(f"%{ctx.q('manufacturer')}%")
    # moved=1:只要真的发生过出入库的元件。
    # 导入 BOM 会凭空建出一堆库存为 0 的元件,它们在任何「出入库」的意义上都还不存在
    # —— 出入库页默认就靠这个把它们挡在外面,只留下真正动过的那些。
    # 已撤销的(voided)和撤销动作本身(void_of)都不算:收进来又撤了等于没动过。
    if ctx.q("moved") in ("1", "true", "yes"):
        base_where.append(
            "EXISTS (SELECT 1 FROM movement m WHERE m.component_id = c.id"
            " AND m.voided = 0 AND m.void_of IS NULL)")
    # 反过来:只看还没动过的,用来查「我导进来但一直没买的东西」
    if ctx.q("moved") == "0":
        base_where.append(
            "NOT EXISTS (SELECT 1 FROM movement m WHERE m.component_id = c.id"
            " AND m.voided = 0 AND m.void_of IS NULL)")

    def opt_float(name):
        raw = ctx.q(name)
        if raw in (None, ""):
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    where, args = list(base_where), list(base_args)
    if ctx.q("package"):
        # 按封装筛也认尺寸:点「0805」这个按钮,写 C0805 和写 1005 的那些料
        # 都该出来 —— 它们装的是同一个位置。
        where.append("(c.package LIKE ? OR c.package_key = ?)")
        args += [f"%{ctx.q('package')}%", footprint.canon(ctx.q("package"))]
    if ctx.q("unit"):
        where.append("c.value_unit = ?")
        args.append(ctx.q("unit"))
    # 值区间 —— 这就是「我 0805 的电阻里到底有没有 1k~10k 的」的答案。
    # 比的是 value_num 而不是字符串:否则 100k 会排在 10k 前面,区间也就无从谈起。
    vmin, vmax = opt_float("value_min"), opt_float("value_max")
    if vmin is not None:
        where.append("c.value_num >= ?")
        args.append(vmin)
    if vmax is not None:
        where.append("c.value_num <= ?")
        args.append(vmax)

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
    return 200, {"total": total, "items": rows,
                 "facets": _component_facets(ctx.con, base_where, base_args)}


def _one_component(ctx: Ctx, where: str, args: list):
    """按条件取一条元件(带可用量那一套列)。"""
    row = ctx.con.execute(f"SELECT * FROM ({COMPONENT_SELECT} WHERE {where})", args).fetchone()
    return component_row(row) if row else None


@route("GET", r"/api/components/resolve")
def resolve_component(ctx: Ctx, m):
    """把一句「人话」对上库里的一条元件 —— 批量粘贴入库时用。

    规则按可靠程度排:商品编号 / 厂家料号 / 完全同名 的精确命中优先;
    没有精确命中时,只有模糊搜索**唯一**命中一条才认,否则返回候选让人自己挑。

    宁可让人多看一眼,也不能猜错 —— 批量入库猜错一次就是几十个料进错地方,
    而且事后极难发现。
    """
    text = (ctx.q("q") or "").strip()
    if not text:
        raise ApiError(400, "缺少 q")
    up = text.upper()
    for col in ("lcsc_pn", "mpn", "name"):
        hit = _one_component(ctx, f"UPPER(c.{col}) = ?", [up])
        if hit:
            return 200, {"match": hit, "how": f"exact:{col}", "candidates": []}

    _s, data = list_components(ctx, m)
    items = data.get("items") or []
    if len(items) == 1:
        return 200, {"match": items[0], "how": "unique", "candidates": items}
    return 200, {
        "match": None,
        "how": "ambiguous" if items else "none",
        "candidates": items[:8],
    }


def _component_facets(con, base_where, base_args):
    """当前筛选范围里真实存在的封装和单位 —— 只列出有的,不摆一堆空选项。"""
    inner = COMPONENT_SELECT + (" WHERE " + " AND ".join(base_where) if base_where else "")
    out = {}
    for key, col in (("packages", "package"), ("units", "value_unit")):
        # WHERE 里用子查询暴露出来的真实列名,不用别名 —— 不依赖 SQLite
        # 「WHERE 里可以引用结果列别名」这个非标准扩展
        sql = (f"SELECT DISTINCT {col} AS v FROM ({inner}) "
               f"WHERE {col} IS NOT NULL AND {col} <> '' ORDER BY {col}")
        out[key] = [r["v"] for r in con.execute(sql, base_args)]
    return out


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


def _nn(v):
    """空字符串一律按 NULL 落库。

    `lcsc_pn` 是 UNIQUE 列,而 SQLite 里 NULL 之间不算冲突、`''` 和 `''` 算冲突 ——
    不填商品编号的新料,以前会报「数据冲突」,一颗都加不进去。
    update_component 那边本来就有这一步,create_component 漏了。
    """
    return v if not isinstance(v, str) or v.strip() else None


@route("POST", r"/api/components")
def create_component(ctx: Ctx, m):
    name = ctx.require("name")
    # 品类可以给 id(树上具体那个节点)或名字。给名字时顺带把品类行建出来 ——
    # 用户敲一个没见过的品类名,本来就该是「品类表里多一个顶层节点」。
    cat_text, cat_id = _resolve_category(ctx)
    cur = ctx.con.execute(
        """INSERT INTO component(lcsc_pn, mpn, manufacturer, name, category, category_id,
                                value, package, package_key, marking, params, datasheet_url,
                                product_url, unit, min_stock, reorder_qty, supplier,
                                unit_price, default_loc_id, note, identity_key)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (_nn(ctx.b("lcsc_pn")), _nn(ctx.b("mpn")), _nn(ctx.b("manufacturer")), name,
         cat_text, cat_id, ctx.b("value"), ctx.b("package"),
         footprint.canon(ctx.b("package")),
         ctx.b("marking"), db.dump_params(ctx.b("params")),
         ctx.b("datasheet_url"), ctx.b("product_url"),
         ctx.b("unit") or "个", ctx.bi("min_stock", 0) or 0,
         ctx.bi("reorder_qty", 0) or 0, ctx.b("supplier"),
         _as_float(ctx.b("unit_price")), ctx.bi("default_loc_id", 0) or None,
         ctx.b("note"),
         bom.identity_key(ctx.b("value"), ctx.b("package"),
                          ctx.b("mpn"), ctx.b("lcsc_pn"), name)),
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
        "value": ctx.b("value"),
        "package": ctx.b("package"), "marking": ctx.b("marking"),
        "datasheet_url": ctx.b("datasheet_url"),
        "product_url": ctx.b("product_url"), "unit": ctx.b("unit"), "note": ctx.b("note"),
    }
    sets, args = [], []
    for k, v in fields.items():
        if v is not None:
            sets.append(f"{k}=?")
            args.append(v if not isinstance(v, str) or v.strip() else None)
    # 品类:category_id 优先(树上具体节点),否则按名字。两者一起写,
    # 绝不出现「文本说电容、id 指着电阻」这种自相矛盾的状态。
    if "category_id" in ctx.body or "category" in ctx.body:
        cat_text, cat_id = _resolve_category(ctx, fallback=row["category"])
        sets.append("category=?")
        args.append(cat_text)
        sets.append("category_id=?")
        args.append(cat_id)
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
    # 身份字段(编号/料号/值/封装)一变,身份键就得跟着变 —— 不改的话,这次改完
    # 下次导入还按老键匹配,等于这次修改对导入不可见,又会多出一颗重复的。
    if any(s.split("=", 1)[0] in ("lcsc_pn", "mpn", "value", "package") for s in sets):
        fresh = ctx.con.execute("SELECT * FROM component WHERE id=?", (cid,)).fetchone()
        ctx.con.execute(
            "UPDATE component SET identity_key=? WHERE id=?",
            (bom.identity_key(fresh["value"], fresh["package"], fresh["mpn"],
                              fresh["lcsc_pn"], fresh["name"]), cid))
        # 封装索引键同理:改了封装不重算,按封装筛就会按老尺寸筛,查不到。
        ctx.con.execute("UPDATE component SET package_key=? WHERE id=?",
                        (footprint.canon(fresh["package"]), cid))
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


# ---------------------------------------------------------------- 品类


def _resolve_category(ctx: Ctx, fallback=None):
    """算出该写进 component 的 (category 文本, category_id)。

    规则见文件头。category_id 优先:界面上用户选的是树上哪个节点,就该挂在那儿;
    只给名字时(导入、命令行、老界面)顺带把顶层行建出来。
    """
    raw = ctx.b("category_id")
    if raw not in (None, "", 0, "0"):
        cid = ctx.bi("category_id", 0) or 0
        if not ctx.con.execute("SELECT 1 FROM category WHERE id=?", (cid,)).fetchone():
            raise ApiError(400, "品类不存在")
        root = db.category_root(ctx.con, cid)
        return (root["name"] if root else (fallback or "其他")), cid
    name = str(ctx.b("category") or "").strip()
    if name:
        cid = db.ensure_category(ctx.con, name)
        return name, cid
    return (fallback or "其他"), None


def _category_row(ctx: Ctx, cid):
    row = ctx.con.execute("SELECT * FROM category WHERE id=?", (cid,)).fetchone()
    if row is None:
        raise ApiError(404, "品类不存在")
    return row


def _category_subtree(ctx: Ctx, cid) -> list:
    """这个节点和它的所有子孙的 id。删/挪之前都要先知道会牵连到哪些节点。"""
    rows = ctx.con.execute(
        """WITH RECURSIVE sub(id) AS (
               SELECT id FROM category WHERE id=?
               UNION ALL
               SELECT c.id FROM category c JOIN sub ON c.parent_id = sub.id)
           SELECT id FROM sub""", (cid,)).fetchall()
    return [r["id"] for r in rows]


def _category_sort_tail(ctx: Ctx, parent_id):
    row = ctx.con.execute(
        "SELECT COALESCE(MAX(sort), 0) + 1 AS n FROM category WHERE parent_id IS ?",
        (parent_id,)).fetchone()
    return int(row["n"])


def _category_taken(ctx: Ctx, name, parent_id, exclude=None) -> bool:
    sql = "SELECT id FROM category WHERE name=? AND parent_id IS ?"
    args = [name, parent_id]
    if exclude is not None:
        sql += " AND id <> ?"
        args.append(exclude)
    return ctx.con.execute(sql, args).fetchone() is not None


@route("GET", r"/api/categories")
def list_categories(ctx: Ctx, m):
    """品类树。界面拿它画库存菜单和品类管理窗口。

    own / total 分开:一个是「直接挂在这个节点下的元件」,一个是「含子孙」。
    删品类、改品类名之前要告诉用户会影响多少个元件,靠的就是 total。
    """
    rows = [db.row_to_dict(r) for r in ctx.con.execute(
        "SELECT * FROM category ORDER BY sort, name")]
    by_id = {r["id"]: r for r in rows}
    for r in rows:
        r["children"] = []
        r["own"] = 0
        r["total"] = 0
        r["own_stocked"] = 0
        r["total_stocked"] = 0
    for r in ctx.con.execute(
            "SELECT category_id AS cid, COUNT(*) AS n FROM component "
            "WHERE category_id IS NOT NULL AND merged_into IS NULL "
            "GROUP BY category_id"):
        if r["cid"] in by_id:
            by_id[r["cid"]]["own"] = int(r["n"])
    # 另一套口径:**有库存的**有多少种。库存菜单上的卡片写「N 种在库」,而点进去的
    # 列表走 list_components(stocked=1) —— 两者必须同源,否则零库存的品类也会喊
    # 「在库」,用户点进去是空的(他管这个叫虚假库存)。
    # own/total 仍旧数全部元件:品类管理窗口里那个「直接挂 / 含子类」是整理分类用的。
    for r in ctx.con.execute(
            "SELECT c.category_id AS cid, COUNT(*) AS n FROM component c "
            "WHERE c.category_id IS NOT NULL AND c.merged_into IS NULL "
            "AND COALESCE((SELECT SUM(s.qty) FROM stock s WHERE s.component_id = c.id), 0) > 0 "
            "GROUP BY c.category_id"):
        if r["cid"] in by_id:
            by_id[r["cid"]]["own_stocked"] = int(r["n"])
    roots = []
    for r in rows:
        pid = r["parent_id"]
        if pid in by_id:
            by_id[pid]["children"].append(r)
        else:
            roots.append(r)

    def roll(node):
        node["total"] = node["own"] + sum(roll(c) for c in node["children"])
        node["total_stocked"] = node["own_stocked"] + sum(
            roll_stocked(c) for c in node["children"])
        return node["total"]

    def roll_stocked(node):
        node["total_stocked"] = node["own_stocked"] + sum(
            roll_stocked(c) for c in node["children"])
        return node["total_stocked"]

    for r in roots:
        roll(r)
    # path 给界面直接用,省得它自己拼(自己拼迟早和这里的规则不一致)
    for r in rows:
        r["path"] = db.category_path(ctx.con, r["id"])
    # 没挂品类行的元件也要报个数:它们不在树上,用户得知道有这些,
    # 否则会在菜单里「找不到那颗料」而不知道去哪儿找
    loose = ctx.con.execute(
        "SELECT COUNT(*) AS n FROM component "
        "WHERE category_id IS NULL AND merged_into IS NULL").fetchone()["n"]
    return 200, {"items": roots, "flat": rows, "loose": int(loose)}


@route("POST", r"/api/categories")
def create_category(ctx: Ctx, m):
    name = str(ctx.require("name")).strip()
    raw = ctx.b("parent_id")
    parent_id = None
    if raw not in (None, "", 0, "0"):
        parent_id = ctx.bi("parent_id", 0) or 0
        _category_row(ctx, parent_id)          # 父节点不存在就 404
    if _category_taken(ctx, name, parent_id):
        raise ApiError(400, f"「{name}」已经存在了")
    cur = ctx.con.execute(
        "INSERT INTO category(parent_id, name, sort, note) VALUES(?,?,?,?)",
        (parent_id, name, _category_sort_tail(ctx, parent_id), ctx.b("note")))
    ctx.con.commit()
    cid = int(cur.lastrowid)
    return 201, {"id": cid, "path": db.category_path(ctx.con, cid)}


@route("PUT", r"/api/categories/(\d+)")
def update_category(ctx: Ctx, m):
    cid = int(m.group(1))
    row = _category_row(ctx, cid)
    parent_id = row["parent_id"]
    sets, args = [], []
    moved = False

    if "name" in ctx.body:
        name = str(ctx.b("name") or "").strip()
        if not name:
            raise ApiError(400, "品类名不能空")
        if _category_taken(ctx, name, parent_id, exclude=cid):
            raise ApiError(400, f"「{name}」已经存在了")
        sets.append("name=?")
        args.append(name)

    if "parent_id" in ctx.body:
        raw = ctx.b("parent_id")
        new_parent = None
        if raw not in (None, "", 0, "0"):
            new_parent = ctx.bi("parent_id", 0) or 0
            _category_row(ctx, new_parent)
            # 挪到自己下面会让这棵树成环,之后谁也走不到顶,直接拒
            if new_parent == cid or new_parent in _category_subtree(ctx, cid):
                raise ApiError(400, "不能把品类挪到它自己或它的子品类下面")
        if new_parent != parent_id:
            if _category_taken(ctx, str(ctx.b("name") or row["name"]).strip(),
                               new_parent, exclude=cid):
                raise ApiError(400, "目标位置下已经有同名品类了")
            sets.append("parent_id=?")
            args.append(new_parent)
            sets.append("sort=?")
            args.append(_category_sort_tail(ctx, new_parent))
            moved = True

    if "note" in ctx.body:
        sets.append("note=?")
        args.append(ctx.b("note") or None)
    if "sort" in ctx.body:
        sets.append("sort=?")
        args.append(ctx.bi("sort", 0) or 0)

    if not sets:
        return 200, {"ok": True, "unchanged": True}
    args.append(cid)
    ctx.con.execute(f"UPDATE category SET {', '.join(sets)} WHERE id=?", args)

    # 改名或挪窝都可能改变「谁是顶层、叫什么」,所以整棵子树下元件的文本列
    # 一律重算一遍。只改顶层那一条是不够的:挪动之后顶层名字也会变。
    subtree = _category_subtree(ctx, cid)
    new_root = db.category_root(ctx.con, cid)
    new_root_name = new_root["name"] if new_root else "其他"
    marks = ",".join("?" * len(subtree))
    touched = ctx.con.execute(
        f"UPDATE component SET category=? WHERE category_id IN ({marks}) AND category <> ?",
        [new_root_name] + subtree + [new_root_name]).rowcount
    ctx.con.commit()
    return 200, {"ok": True, "renamed_components": max(int(touched), 0), "moved": moved,
                 "path": db.category_path(ctx.con, cid)}


def _retag_category_subtree(ctx: Ctx, cat_id: int, root_name: str) -> int:
    """把某个品类子树下所有元件的「大类文本」改成 root_name。返回改了几条。

    只在「这一支换了顶层」时才需要 —— 比如删掉一个顶层大类,它的下级自己当了
    顶层。不跟着改的话,那些元件的文本还写着那个已经不存在的大类名,而全程序里
    「按品类分组 / 筛选 / 显示」读的都是这个文本列,对不上就等于哪儿都找不到。

    **不要给它挂 @route。** 这里被踩过一次(issue #32):它头上曾经贴着一行和
    delete_category 一模一样的 `@route("DELETE", r"/api/categories/(\\d+)")`,
    而 HTTP 派发是**取第一条匹配**的,于是网页/HTTP 版删品类调的是这个内部函数
    —— 它要三个参数,派发只给两个,永远是 500「missing 1 required positional
    argument: 'root_name'」。桌面版没露出来,是因为它把 server.delete_category
    的函数对象直接传给 call,压根不走路由表。内部函数和路由函数长得像时尤其要
    小心:多贴一行装饰器不会报错,只会静默地把那条路由顶掉。
    """
    ids = _category_subtree(ctx, cat_id)
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    return max(int(ctx.con.execute(
        f"UPDATE component SET category=? WHERE category_id IN ({marks}) AND category <> ?",
        [root_name] + ids + [root_name]).rowcount), 0)


def _attach_category(ctx: Ctx, node_id: int, new_parent_id):
    """把一个品类节点挂到 new_parent_id 下面。返回它最终落在哪个节点上。

    新位置已经有同名节点时**并进去**(把它的下级和元件都挪过去,再删掉这个空壳),
    而不是报错。理由:删中间一层是大扫除,这时候被一句「已经有同名品类了」挡住,
    用户只能自己先去改名 —— 而他要的只是把这一层拿掉。

    传 None 表示「挂到顶层」,和 parent_id IS NULL 对应(SQLite 的 IS 能配 NULL,
    所以顶层重名也能被这条找到)。
    """
    row = _category_row(ctx, node_id)
    twin = ctx.con.execute(
        "SELECT id FROM category WHERE parent_id IS ? AND name=? AND id<>?",
        (new_parent_id, row["name"], node_id)).fetchone()
    if twin is None:
        ctx.con.execute(
            "UPDATE category SET parent_id=?, sort=? WHERE id=?",
            (new_parent_id, _category_sort_tail(ctx, new_parent_id), node_id))
        return node_id
    for kid in [r["id"] for r in ctx.con.execute(
            "SELECT id FROM category WHERE parent_id=? ORDER BY sort, id", (node_id,))]:
        _attach_category(ctx, kid, twin["id"])
    ctx.con.execute("UPDATE component SET category_id=? WHERE category_id=?",
                    (twin["id"], node_id))
    ctx.con.execute("DELETE FROM category WHERE id=?", (node_id,))
    return twin["id"]


@route("DELETE", r"/api/categories/(\d+)")
def delete_category(ctx: Ctx, m):
    """删品类。**绝不删元件,也绝不连坐子类。**

    删掉这一级的节点:下级接到上一级去(层级少一层,节点一个不少),直接挂在
    它下面的元件挪到上一级。用户的原话是「这个选项只改这一级的,不涉及子级和
    父级」—— 整理货架的时候,不该顺手把下面整枝也扔掉,那正是最容易误删的地方。

    两种边界:
      * 删的是顶层大类 -> 下级各自成为顶层(自己的名字就是大类名),直接挂在它
        下面的元件**从品类里彻底摘出来**:category_id 置 NULL、category 文本置空,
        返回体的 to 是空串。以前这里把它们兜进「未分类」,而「未分类」是后端
        自动建出来的固定词 —— 删一次长一次,顶层大类就永远删不干净(issue #32:
        「品类属于用户的事情,我需要是绝对的自定义自由化」)。
      * 下级和上一级已有的节点撞名 -> 并进去(下级的下级、元件一起挪)。
        不并的话 UNIQUE(parent_id, name) 会直接报错,而用户只能自己去改名。
    """
    cid = int(m.group(1))
    row = _category_row(ctx, cid)
    parent_id = row["parent_id"]

    kids = [(r["id"], r["name"]) for r in ctx.con.execute(
        "SELECT id, name FROM category WHERE parent_id=? ORDER BY sort, id", (cid,))]
    n_comp = int(ctx.con.execute(
        "SELECT COUNT(*) AS n FROM component WHERE category_id=?", (cid,)).fetchone()["n"])

    if parent_id is None:
        # 顶层没有上一级可去 -> 不给它编一个品类,而是把它从品类树里摘下来。
        # 「未分类」这一行绝不能再自动确保存在:它一旦被建出来,用户删了下次
        # 又回来(而且它会挂在顶层菜单里,看着像系统硬塞的品类)。
        # 数据层留下的形态就是「没有品类」:category_id=NULL + category='',
        # reconcile_categories 对空文本是 continue,所以它不会被自己长回去;
        # 界面那边按「文本为空」显示成「未分类」,那只是显示用的兜底词。
        comp_dest_id = None
        comp_dest_text = ""
        comp_dest_path = ""
    else:
        comp_dest_id = parent_id
        dest_root = db.category_root(ctx.con, parent_id)
        # 兜底也用空串而不是「未分类」:parent 行理论上不会消失,但万一,
        # 写成「未分类」会通过 reconcile 的「库里在用的品类文本」那条路
        # 把这个固定词重新变成一个真品类行,等于又把它种回来。
        comp_dest_text = dest_root["name"] if dest_root else ""
        comp_dest_path = db.category_path(ctx.con, parent_id)

    # 1. 直接挂在这一级上的元件 -> 上一级
    if n_comp:
        ctx.con.execute(
            "UPDATE component SET category_id=?, category=? WHERE category_id=?",
            (comp_dest_id, comp_dest_text, cid))

    # 2. 下级接到上一级。顶层被删时接到 None(自己当顶层),这时它们底下元件的
    #    大类文本必须跟着改成自己的名字,否则文本还写着那个已经不存在的大类。
    for kid_id, kid_name in kids:
        landed = _attach_category(ctx, kid_id, parent_id)
        if parent_id is None:
            _retag_category_subtree(ctx, landed, kid_name)

    # 3. 这时候它已经是个空壳了,删掉不会连坐任何东西
    ctx.con.execute("DELETE FROM category WHERE id=?", (cid,))
    ctx.con.commit()
    # 兜底收尾:上面合并同名的分支如果让某条元件的文本和它所在支的顶层名字
    # 对不上,这里补齐。reconcile 本来就每次启动都跑,幂等。
    # 这里**不能**播种(用默认的 seed=False):刚删掉的那个节点就在上一行消失,
    # 一播种就会被标准名单 INSERT 回来 —— 用户会看到「删了立刻又出现」。
    db.reconcile_categories(ctx.con)
    return 200, {"ok": True, "moved_components": n_comp, "to": comp_dest_path,
                 "moved_children": len(kids), "deleted_nodes": 1}


@route("GET", r"/api/meta")
def meta(ctx: Ctx, m):
    # 品类候选清单。**空文本必须排掉**:删掉一个顶层大类之后,它底下的元件就是
    # 「没有品类」(category_id NULL + 文本空,见 delete_category),直接 DISTINCT
    # 出来会多一个空字符串 —— 界面上就是下拉框里一条什么都不写的选项。
    cats = [r["category"] for r in ctx.con.execute(
        "SELECT DISTINCT category FROM component WHERE category IS NOT NULL "
        "AND TRIM(category) <> '' AND merged_into IS NULL ORDER BY category")]
    pkgs = [r["package"] for r in ctx.con.execute(
        "SELECT DISTINCT package FROM component WHERE package IS NOT NULL AND package<>'' "
        "AND merged_into IS NULL ORDER BY package LIMIT 200")]
    mfrs = [r["manufacturer"] for r in ctx.con.execute(
        "SELECT DISTINCT manufacturer FROM component WHERE manufacturer IS NOT NULL "
        "AND manufacturer<>'' AND merged_into IS NULL ORDER BY manufacturer LIMIT 200")]

    # 属性名:把「库里每个品类下已经在用的属性名」现算出来,给编辑元件窗口的
    # 属性名下拉当候选。用户自己起过的名字要排在内置建议前面(见 attrs.suggest),
    # 所以这里单独按品类收集,而不是把整库的属性名混成一个列表。
    attrs_by_cat: dict = {}
    for r in ctx.con.execute(
            "SELECT category, params FROM component WHERE params IS NOT NULL "
            "AND params <> '' AND params <> '{}' AND merged_into IS NULL"):
        bucket = attrs_by_cat.setdefault((r["category"] or "").strip(), [])
        for nm in attrs.clean(db.parse_params(r["params"])):
            if nm and nm not in bucket:
                bucket.append(nm)
    for names in attrs_by_cat.values():
        names.sort()
    # 全部属性名合起来也留一份:编辑元件时品类常常还没定,下拉不该是空的
    attrs_all: list = []
    for names in attrs_by_cat.values():
        for nm in names:
            if nm not in attrs_all:
                attrs_all.append(nm)
    attrs_all.sort()

    # 品类树的全部节点,带全路径。编辑元件时的品类下拉用它 ——
    # 用户能在树上搭出「电容 / 无极性陶瓷电容」这种二级,选的时候也得能选到。
    cat_paths = [{"id": r["id"], "name": r["name"], "parent_id": r["parent_id"],
                  "path": db.category_path(ctx.con, r["id"])}
                 for r in ctx.con.execute("SELECT * FROM category ORDER BY sort, name")]
    roots = [p["path"] for p in cat_paths if p["parent_id"] is None]
    return 200, {
        "categories": sorted(set(cats) | set(roots) | set(CATEGORY_SUGGESTIONS)),
        "category_paths": cat_paths,
        "attrs_by_category": attrs_by_cat,
        "attrs_all": attrs_all,
        # 内置建议表也发一份,省得界面那边再和 app/attrs.py 对不上
        "attrs_builtin": attrs.SUGGESTIONS,
        "attrs_generic": attrs.GENERIC,
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


@route("POST", r"/api/locations/(\d+)/stocktake")
def stocktake_location(ctx: Ctx, m):
    """把一个仓位里的实物数一遍,只把**有差异**的行写成盘点流水。

    这和「一条条改数量」的区别在意图上:实物清点是拿着一箱料挨个核对,
    对得上的不该留痕(否则流水会被几百条「没变」淹掉,真出事时反而查不出来),
    对不上的、以及「账面有但现在根本没数到」的,才各记一笔。

    请求体:{"items": [{"component_id": 1, "qty": 33}, …]}
    qty 是**实盘数**,不是增减量 —— 写增减量的话,清点的人还得自己算差。
    """
    lid = int(m.group(1))
    loc = ctx.con.execute("SELECT * FROM location WHERE id=?", (lid,)).fetchone()
    if not loc:
        raise ApiError(404, "仓位不存在")
    if loc["structural"]:
        # 分层节点本身没有物理位置,没有东西可数
        raise ApiError(409, f"「{loc['code']}」是分层仓位,本身不装东西;"
                            f"请盘点它下面的具体仓位")
    items = ctx.body.get("items")
    if not isinstance(items, list):
        raise ApiError(400, "items 必须是一个列表")

    changed, unchanged = [], 0
    for it in items:
        if not isinstance(it, dict):
            raise ApiError(400, "items 里每一项都要是 {component_id, qty}")
        try:
            cid, qty = int(it.get("component_id")), int(it.get("qty"))
        except (TypeError, ValueError):
            raise ApiError(400, "每行都要有 component_id 和整数 qty")
        if qty < 0:
            raise ApiError(400, "实盘数量不能是负数")
        comp = ctx.con.execute("SELECT name FROM component WHERE id=?", (cid,)).fetchone()
        if not comp:
            raise ApiError(404, f"元件 {cid} 不存在")
        row = ctx.con.execute("SELECT qty FROM stock WHERE component_id=? AND location_id=?",
                              (cid, lid)).fetchone()
        was = int(row["qty"]) if row else 0
        if was == qty:
            unchanged += 1
            continue
        _bump(ctx.con, cid, lid, qty - was)
        _log_move(ctx.con, "ADJUST", cid, lid, qty, ref=ctx.b("ref"),
                  operator=ctx.b("operator"), qty_before=was,
                  note=f"盘点 {loc['code']}:账面 {was} → 实盘 {qty}")
        changed.append({"component_id": cid, "name": comp["name"], "was": was, "now": qty})
    ctx.con.commit()
    return 200, {
        "ok": True,
        "location": loc["code"],
        "checked": len(items),
        "changed": len(changed),
        "unchanged": unchanged,
        "diffs": changed,
    }


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


def _log_move(con, kind, component_id, location_id, qty, *, to_location_id=None,
              project_id=None, bom_id=None, purchase_id=None, ref=None, operator=None,
              note=None, qty_before=None, void_of=None) -> int:
    """写一条流水。出入库、盘点、领料、到货和撤销都走这里,
    免得几处 INSERT 的字段顺序各写各的、漏字段。"""
    cur = con.execute(
        """INSERT INTO movement(kind, component_id, location_id, to_location_id, qty,
                                project_id, bom_id, purchase_id, ref, operator, note,
                                qty_before, void_of)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (kind, component_id, location_id, to_location_id, qty, project_id,
         bom_id, purchase_id, ref, operator or "本地用户", note, qty_before, void_of),
    )
    db.touch_component(con, component_id)
    return int(cur.lastrowid)


def _get_location_id(con, code_or_id) -> int:
    """接受仓位 id、编码,或者**路径**(「A柜 / 01层 / 02格」)。

    快速入库 / 批量入库的下拉给的是路径,以前这里只认 id/code,认不出来就静默
    `INSERT INTO location(code)` —— 货会记进一个凭空冒出来的顶层仓位,仓位表跟着
    被污染,别处的仓位下拉里也就多出这条怪名字。
    """
    if code_or_id in (None, ""):
        raise ApiError(400, "缺少仓位")
    s = str(code_or_id)
    if "/" in s:
        # 按路径找真身。找不到不报错,继续往下走老路(手打新仓位名要能用)
        try:
            hit = con.execute(
                """WITH RECURSIVE up(id, path) AS (
                       SELECT id, code FROM location WHERE parent_id IS NULL
                       UNION ALL
                       SELECT l.id, up.path || ' / ' || l.code
                         FROM location l JOIN up ON l.parent_id = up.id)
                   SELECT id FROM up WHERE path = ? LIMIT 1""",
                (s.strip(),)).fetchone()
        except Exception:
            hit = None
        if hit:
            return int(hit["id"])
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


def _apply_move(con, kind: str, cid: int, qty: int, loc: int, *, to_loc=None,
                project_id=None, bom_id=None, ref=None, operator=None, note=None,
                spill: bool = False) -> list[int]:
    """实际改动余额并写流水。返回写下的流水号。

    `spill=True` 时出库允许从**其他仓位凑**:东西常常散在几个盒子里,
    而用户心里想的是「这个项目要领 8 个」,不是「先从哪个盒子拿」。
    拆出来的每一笔都单独记一条流水 —— 仓位是真的动了,账就得如实反映。

    单笔开单、按 BOM 领料、一键批量都走这里。三个入口各写一遍的话,
    「盘点要把原数量记进备注」这类规则迟早只改到其中一处。
    """
    if kind in ("IN", "OUT") and qty <= 0:
        raise ApiError(400, "数量必须大于 0")
    if kind == "TRANSFER" and qty <= 0:
        raise ApiError(400, "移库数量必须大于 0")

    if kind == "IN":
        was = _qty_at(con, cid, loc)
        _bump(con, cid, loc, qty)
        return [_log_move(con, "IN", cid, loc, qty, project_id=project_id,
                          bom_id=bom_id, ref=ref, operator=operator, note=note,
                          qty_before=was)]

    if kind == "ADJUST":
        was = _qty_at(con, cid, loc)
        _bump(con, cid, loc, qty - was)
        # 盘点记录里的 qty 是「新数量」,所以必须把「原来多少」也存下来,
        # 否则以后没法撤销它
        return [_log_move(con, "ADJUST", cid, loc, qty, project_id=project_id,
                          bom_id=bom_id, ref=ref, operator=operator,
                          note=(note or "") + f"（盘点:原 {was} → 新 {qty}）",
                          qty_before=was)]

    if kind == "TRANSFER":
        was = _qty_at(con, cid, loc)
        _bump(con, cid, loc, -qty)
        _bump(con, cid, to_loc, qty)
        return [_log_move(con, "TRANSFER", cid, loc, qty, to_location_id=to_loc,
                          project_id=project_id, bom_id=bom_id, ref=ref,
                          operator=operator, note=note, qty_before=was)]

    # ---- OUT
    if not spill:
        # 不够就交给 _bump 抛「库存不足:仓位 X 现有 N,需要 M」,
        # 那句话比这里另写一句更具体(它知道是哪个仓位)
        was = _qty_at(con, cid, loc)
        _bump(con, cid, loc, -qty)
        return [_log_move(con, "OUT", cid, loc, qty, project_id=project_id,
                          bom_id=bom_id, ref=ref, operator=operator, note=note,
                          qty_before=was)]

    plan = [(loc, min(qty, _qty_at(con, cid, loc)))]
    rest = qty - plan[0][1]
    if rest > 0:
        total = db.stock_total(con, cid)
        if total < qty:
            raise ApiError(409, f"库存不足:需要 {qty},仅有 {total}")
        for s in con.execute(
                "SELECT location_id, qty FROM stock WHERE component_id=? AND qty>0"
                " AND location_id<>? ORDER BY qty DESC", (cid, loc)):
            if rest <= 0:
                break
            take = min(rest, int(s["qty"]))
            plan.append((s["location_id"], take))
            rest -= take
    ids = []
    for src, take in plan:
        if take <= 0:
            continue
        was = _qty_at(con, cid, src)
        _bump(con, cid, src, -take)
        ids.append(_log_move(con, "OUT", cid, src, take, project_id=project_id,
                             bom_id=bom_id, ref=ref, operator=operator,
                             note=note, qty_before=was))
    if not ids:
        raise ApiError(409, "没有可出库的数量")
    return ids


def _credit_bom(con, bom_id, qty: int, kind: str) -> None:
    """把这一笔出入库记到它对应的 BOM 需求上。

    两个方向各记一本账,而且**必须对称**:

      出库(OUT) → placed_qty   这条需求已经发给板子多少
      入库(IN)  → received_qty 这条需求已经收进项目多少

    原来只有出库那一边记,入库传 0。后果是「全收完的那条需求」在出库页上
    照样列着 —— 用户看着像「上一个 BOM 还能再收一遍/再出一次」(#31)。
    这里加的是收料进度,不是发料进度:入库**不动** placed_qty。动的话货一进库
    界面就说「齐了」,反而发不出去了(这一条原来踩过,注释留在 stock_batch 里)。

    只认 IN / OUT:盘点、移库、采购到货那些动作不改变「这条需求被消化了多少」。
    """
    if not bom_id or not qty:
        return
    if kind == "IN":
        con.execute("UPDATE project_bom SET received_qty = received_qty + ? WHERE id=?",
                    (qty, bom_id))
    elif kind == "OUT":
        con.execute("UPDATE project_bom SET placed_qty = placed_qty + ? WHERE id=?",
                    (qty, bom_id))


def _debit_bom(con, bom_id, qty: int, kind: str) -> None:
    """撤销一笔出入库时,把它当初记进 BOM 需求里的那个数冲回来。

    **按方向分别冲**:入库撤销退 received_qty,出库撤销退 placed_qty。
    原来这里只处理出库(placed_qty),撤销入库的话「已入库」会永远虚高,
    接着出库页就会少显示一行 —— 账面上看着像那条需求还没做完。

    下限卡在 0:老版本写的流水可能根本没记过这笔账(那时入库传的是 0),
    撤销时减成负数会让界面显示「已入库 -3」,比不冲还难查。
    """
    if not bom_id or not qty:
        return
    if kind == "IN":
        con.execute("UPDATE project_bom SET received_qty = MAX(0, received_qty - ?)"
                    " WHERE id=?", (qty, bom_id))
    elif kind == "OUT":
        con.execute("UPDATE project_bom SET placed_qty = MAX(0, placed_qty - ?)"
                    " WHERE id=?", (qty, bom_id))


@route("POST", r"/api/stock/batch")
def stock_batch(ctx: Ctx, m):
    """一键批量开单:入库或出库一次报多行,逐行给结果。

    入库侧主要是「按 BOM 收料」那个勾选清单:一箱货到了,勾掉收到的,
    一次全部入库。出库侧走的是 /api/projects/{id}/pick(要按 BOM 行分配),
    这里只做直接的元件级开单。

    整批一个事务提交:某一行失败不影响其余已经成功的行(逐行 try),
    但不会出现「提交了一半」的状态。
    """
    kind = str(ctx.require("kind")).upper()
    if kind not in ("IN", "OUT"):
        raise ApiError(400, "批量开单只支持 IN / OUT")
    raw = ctx.b("items") or []
    if not isinstance(raw, list) or not raw:
        raise ApiError(400, "items 不能为空")

    default_loc = ctx.b("location") or ctx.b("location_id")
    operator = ctx.b("operator")
    ref = ctx.b("ref")
    done, failed = [], []
    for i, it in enumerate(raw):
        cid = int(it.get("component_id") or 0)
        qty = int(it.get("qty") or 0)
        try:
            if not cid:
                raise ApiError(400, "缺 component_id")
            comp = ctx.con.execute("SELECT * FROM component WHERE id=?",
                                   (cid,)).fetchone()
            if not comp:
                raise ApiError(404, "元件不存在")
            raw_loc = it.get("location") or it.get("location_id") or default_loc
            loc = _fallback_location(ctx.con, comp) if raw_loc in (None, "") \
                else _get_location_id(ctx.con, raw_loc)
            ids = _apply_move(
                ctx.con, kind, cid, qty, loc,
                project_id=ctx.bi("project_id") or it.get("project_id"),
                bom_id=it.get("bom_id"),
                ref=it.get("ref") or ref, operator=it.get("operator") or operator,
                note=it.get("note") or ctx.b("note"),
                spill=(kind == "OUT"))
            # 两个方向都要记账,但记的是**各自的**那本:出库记「已发料」,
            # 入库记「已入库」。入库绝不能去加已发料 —— 加了的话,货一进库
            # 界面就会说「齐了」,反而发不出去了。反过来,入库不记的话,
            # 全收完的需求会一直挂在出库页上(#31)。
            _credit_bom(ctx.con, it.get("bom_id"), qty, kind)
            done.append({"component_id": cid, "name": comp["name"], "qty": qty,
                         "movement_ids": ids})
        except ApiError as exc:
            failed.append({"index": i, "component_id": cid, "qty": qty,
                           "reason": exc.message})
    ctx.con.commit()
    return 200, {"ok": not failed, "kind": kind,
                 "done": done, "failed": failed,
                 "total_qty": sum(d["qty"] for d in done)}


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

    ids = _apply_move(ctx.con, kind, cid, qty, loc, to_loc=to_loc,
                      project_id=ctx.bi("project_id"), bom_id=ctx.bi("bom_id"),
                      ref=ctx.b("ref"), operator=ctx.b("operator"),
                      note=ctx.b("note"))
    mid = ids[-1]
    # 单笔开单同样按方向记账:带 bom_id 的入库也是在推进那条需求的收料进度
    _credit_bom(ctx.con, ctx.bi("bom_id"), qty, kind)
    ctx.con.commit()

    on_hand = db.stock_total(ctx.con, cid)
    # 移库问「现在落在哪儿」,答案当然在目标仓位;其余动作都在本仓位。
    # 这里曾经返回源仓位,于是「移库 2 个」看上去像只移过去 1 个 ——
    # 自检里那条断言就是这么抓住它的。
    at = to_loc if kind == "TRANSFER" else loc
    return 200, {"ok": True, "movement_id": mid,
                 "qty_at_location": _qty_at(ctx.con, cid, at),
                 "on_hand": on_hand}


@route("POST", r"/api/movements/(\d+)/void")
def void_movement(ctx: Ctx, m):
    """撤销一条出入库 / 盘点 / 移库记录。

    做法是**写一条反向流水**并把原记录标记成「已撤销」,而不是把原记录删掉。
    原因:这套东西的口径是「流水只增不改,余额随时能按流水重建」——
    删记录的话余额就重建不出来了,而且事后查不出「那天到底是谁把它改成这样的」。
    反向流水本身也是一笔正常流水,所以重建逻辑一行都不用改。

    还原规则按动作分:
      IN       → 从原仓位减回去
      OUT      → 加回原仓位
      ADJUST   → 回到盘点前的数量(靠流水里的 qty_before)
      TRANSFER → 从目标仓位挪回原仓位

    除了库存,这条流水要是记在某条 BOM 需求上(收料 / 发料),那本账也按方向
    冲回去 —— 库存动了、需求上的进度没动,两边就对不上了。
    """
    mid = int(m.group(1))
    mv = ctx.con.execute("SELECT * FROM movement WHERE id=?", (mid,)).fetchone()
    if not mv:
        raise ApiError(404, "这条记录不存在")
    if mv["voided"]:
        raise ApiError(409, "这条已经撤销过了")
    if mv["void_of"]:
        raise ApiError(409, "这本身就是一条撤销记录,不能再撤销")

    kind = mv["kind"]
    cid, qty = int(mv["component_id"]), int(mv["qty"])
    loc, to_loc = mv["location_id"], mv["to_location_id"]
    comp = ctx.con.execute("SELECT name FROM component WHERE id=?", (cid,)).fetchone()
    who = comp["name"] if comp else f"#{cid}"

    # 反向流水自己也要记下「改动前是多少」,否则它将来同样撤销不了。
    # 注意 TRANSFER 的反向是从 to_loc 挪回 loc,所以 qty_before 取的是 to_loc 的。
    if kind == "IN":
        was = _qty_at(ctx.con, cid, loc)
        _bump(ctx.con, cid, loc, -qty)
        back = {"kind": "OUT", "location_id": loc, "to_location_id": None,
                "qty": qty, "qty_before": was}
    elif kind == "OUT":
        was = _qty_at(ctx.con, cid, loc)
        _bump(ctx.con, cid, loc, qty)
        back = {"kind": "IN", "location_id": loc, "to_location_id": None,
                "qty": qty, "qty_before": was}
    elif kind == "ADJUST":
        if mv["qty_before"] is None:
            # 老版本(v2 之前)的盘点记录没存 qty_before,还原不了 —— 宁可拒绝
            raise ApiError(409, "这条盘点记录是旧版本写的,没留下原来的数量,"
                                "没法自动撤销;请手动盘点回正确的数量")
        was = _qty_at(ctx.con, cid, loc)
        _bump(ctx.con, cid, loc, int(mv["qty_before"]) - was)
        back = {"kind": "ADJUST", "location_id": loc, "to_location_id": None,
                "qty": int(mv["qty_before"]), "qty_before": was}
    else:  # TRANSFER
        was = _qty_at(ctx.con, cid, to_loc)
        _bump(ctx.con, cid, to_loc, -qty)
        _bump(ctx.con, cid, loc, qty)
        back = {"kind": "TRANSFER", "location_id": to_loc, "to_location_id": loc,
                "qty": qty, "qty_before": was}

    new_id = _log_move(
        ctx.con, back["kind"], cid, back["location_id"], back["qty"],
        to_location_id=back.get("to_location_id"),
        project_id=mv["project_id"], bom_id=mv["bom_id"],
        ref=mv["ref"], operator=ctx.b("operator"),
        note=f"撤销 #{mid}({who} {KIND_LABEL.get(kind, kind)} {qty})",
        qty_before=back.get("qty_before"), void_of=mid)
    ctx.con.execute("UPDATE movement SET voided=1 WHERE id=?", (mid,))

    # 撤销一笔「按 BOM 领料 / 按 BOM 收料」时,那条 BOM 需求上记的那个数也要退回去。
    # 不退的话界面会说「还差 0 个」/「已经全收了」,而东西其实已经还回架上了(或
    # 者根本没收进来)—— 账就成了假的。方向必须分别冲:入库退已入库,出库退已发料。
    if mv["bom_id"] and kind in ("IN", "OUT"):
        _debit_bom(ctx.con, mv["bom_id"], qty, kind)

    # 撤销「采购到货」时,采购单的已收数量也得退回去 —— 否则那张单永远收不完,
    # 而且「在途」会一直少算这一笔
    if mv["purchase_id"] and kind == "IN":
        pur = ctx.con.execute("SELECT qty, received FROM purchase WHERE id=?",
                              (mv["purchase_id"],)).fetchone()
        if pur:
            got = max(0, int(pur["received"]) - qty)
            ctx.con.execute("UPDATE purchase SET received=?, status=? WHERE id=?",
                            (got, "arrived" if got >= int(pur["qty"]) else "ordered",
                             mv["purchase_id"]))

    ctx.con.commit()
    return 200, {"ok": True, "movement_id": new_id, "voided": mid,
                 "on_hand": db.stock_total(ctx.con, cid), "name": who}


@route("GET", r"/api/movements/last")
def last_movement(ctx: Ctx, m):
    """最近一笔还能撤销的流水。给「撤销上一次出入库」用。

    已撤销的(voided)和撤销记录本身(void_of)都要排除 —— 否则连按两次 Ctrl+Z
    会一直撤销那条撤销记录。
    """
    row = ctx.con.execute(
        """SELECT mv.*, c.name AS component_name,
                  l.code AS location_code, tl.code AS to_location_code
           FROM movement mv
           JOIN component c ON c.id = mv.component_id
           LEFT JOIN location l  ON l.id  = mv.location_id
           LEFT JOIN location tl ON tl.id = mv.to_location_id
           WHERE mv.voided = 0 AND mv.void_of IS NULL
           ORDER BY mv.id DESC LIMIT 1""").fetchone()
    if not row:
        return 200, {"movement": None}
    out = db.row_to_dict(row)
    out["kind_label"] = KIND_LABEL.get(out["kind"], out["kind"])
    return 200, {"movement": out}


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
    inner = COMPONENT_SELECT
    if ctx.q("moved") in ("1", "true", "yes"):
        # EXISTS 必须写在 COMPONENT_SELECT 里面 —— 那里 c 才在作用域内。
        # 套在外层的话 on_hand / stock_state 这些算出来的别名看得到,c 看不到。
        inner += (" WHERE EXISTS (SELECT 1 FROM movement m WHERE m.component_id = c.id"
                  " AND m.voided = 0 AND m.void_of IS NULL)")
    extra = ""
    # stocked=1 只要真正有库存的。on_hand 是 COMPONENT_SELECT 里算出来的别名,
    # 所以只能在外层过滤 —— 好在派生表外面看得到它,这样写是合法的。
    if ctx.q("stocked") in ("1", "true", "yes"):
        extra = " AND on_hand > 0"
    sql = (f"SELECT * FROM ({inner}) WHERE stock_state IN ('low','out'){extra}"
           " ORDER BY on_hand, name")
    return 200, {"items": [component_row(r) for r in ctx.con.execute(sql)]}


@route("GET", r"/api/summary")
def summary(ctx: Ctx, m):
    con = ctx.con
    # 已合并掉的元件不算数(它们的库存早就并给保留的那条了)
    comps = con.execute("SELECT COUNT(*) AS n FROM component WHERE merged_into IS NULL"
                        ).fetchone()["n"]
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
           FROM component c WHERE c.merged_into IS NULL
           GROUP BY c.category ORDER BY kinds DESC""")]
    return 200, {"components": comps, "total_qty": lots, "low": low, "out": out,
                 "projects": projs, "recent": recent, "by_category": by_cat}


# ------------------------------------------------------------------ 查重与合并

# 查重的三档依据,按「有多确定」排。前两档几乎可以肯定是重复,第三档只是可疑 ——
# 所以界面上要分档显示,让人自己判断,而不是替他把「可疑」当「确定」处理。
DUP_RULES = (
    ("mpn", "厂家料号相同", "同一个厂家料号在库里出现了多次,基本可以确定是重复录入"),
    ("name", "名称完全相同", "名字一模一样的两条,一般是反复导入 BOM 长出来的"),
    ("vf", "值 + 封装相同", "没有料号可依,只能按「值 + 封装 + 品类」判断;"
                            "请自己确认是不是同一个东西(比如不同耐压的电容会长得一样)"),
)


@route("GET", r"/api/components/duplicates")
def component_duplicates(ctx: Ctx, m):
    """找出可疑的重复元件。

    为什么需要它:BOM 是反复导入的,而不同时期的 BOM 里同一个料可能一条带料号、
    一条只有值+封装 —— 于是一个元件在库里长成两三条,库存还被分散记着。
    这类「数据腐烂」不会自己好,只会越来越难收拾,所以得有个工具定期扫一遍。
    """
    con = ctx.con
    groups, seen_pairs = [], set()

    def items_of(ids):
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        return [component_row(r) for r in con.execute(
            COMPONENT_SELECT + f" WHERE c.id IN ({marks}) ORDER BY c.id", ids)]

    def add(reason, title, sql, key_sql):
        rows = con.execute(sql).fetchall()
        for r in rows:
            ids = [int(x) for x in r["ids"].split(",")]
            if len(ids) < 2:
                continue
            pair = tuple(sorted(ids))
            if (reason, pair) in seen_pairs:
                continue
            seen_pairs.add((reason, pair))
            groups.append({"reason": reason, "title": title,
                           "key": key_sql(r), "items": items_of(ids),
                           "hint": dict((k, h) for k, _t, h in DUP_RULES)[reason]})

    base = ("FROM component WHERE merged_into IS NULL "
            "AND {col} IS NOT NULL AND TRIM({col}) <> '' "
            "GROUP BY UPPER(TRIM({col})) HAVING COUNT(*) > 1")
    add("mpn", "厂家料号相同",
        "SELECT GROUP_CONCAT(id) AS ids, TRIM(mpn) AS k " + base.format(col="mpn"),
        lambda r: r["k"])
    add("name", "名称完全相同",
        "SELECT GROUP_CONCAT(id) AS ids, TRIM(name) AS k " + base.format(col="name"),
        lambda r: r["k"])
    add("vf", "值 + 封装相同",
        "SELECT GROUP_CONCAT(id) AS ids, "
        "       COALESCE(value,'') || ' / ' || COALESCE(package,'') AS k "
        "FROM component WHERE merged_into IS NULL "
        "AND COALESCE(value,'') <> '' AND COALESCE(package,'') <> '' "
        "GROUP BY UPPER(TRIM(COALESCE(value,'')) || '|' || TRIM(COALESCE(package,'')) "
        "        || '|' || TRIM(COALESCE(category,''))) HAVING COUNT(*) > 1",
        lambda r: r["k"])

    order = {k: i for i, (k, _t, _h) in enumerate(DUP_RULES)}
    groups.sort(key=lambda g: (order.get(g["reason"], 9), g["key"]))
    return 200, {"groups": groups, "count": len(groups),
                 "extra": sum(len(g["items"]) - 1 for g in groups)}


@route("POST", r"/api/components/merge")
def merge_components(ctx: Ctx, m):
    """把几个重复元件并成一个。

    body: {"keep": 保留哪个 id, "drop": [要并掉的 id, ...]}

    **被并掉的不删,只标 merged_into**。理由是:它名下挂着真实的收发货流水,
    而 movement.component_id 是 ON DELETE CASCADE —— 直接删元件会把那些历史
    一起带走,账就再也重建不出来了。标一下既能让列表干净,又能保住来龙去脉,
    万一合错了还能查回原样。
    """
    keep = ctx.bi("keep")
    drop = [int(x) for x in (ctx.b("drop") or [])]
    if not keep:
        raise ApiError(400, "要指定保留哪一个元件")
    if not drop:
        raise ApiError(400, "要指定并入哪些元件")
    drop = [d for d in dict.fromkeys(drop) if d != keep]
    if not drop:
        raise ApiError(400, "并入的不能就是保留的那一个")

    con = ctx.con
    keeper = con.execute("SELECT * FROM component WHERE id=?", (keep,)).fetchone()
    if not keeper:
        raise ApiError(404, f"要保留的元件 {keep} 不存在")
    if keeper["merged_into"]:
        raise ApiError(409, "要保留的这个本身已经被合并过了")
    kname = keeper["name"]

    moved_bom = moved_po = moved_stock = 0
    for did in drop:
        d = con.execute("SELECT * FROM component WHERE id=?", (did,)).fetchone()
        if not d:
            raise ApiError(404, f"元件 {did} 不存在")
        if d["merged_into"]:
            raise ApiError(409, f"「{d['name']}」已经被合并过了")

        # 1) 身份字段:保留的那条缺什么就补什么。
        #    lcsc_pn 上有 UNIQUE 约束,想把它挪过来就必须先把被并的那条清掉,
        #    否则两条会同时占着同一个编号,SQLite 直接拒。mpn 没这个约束,
        #    就留着不清 —— 它在「已合并」列表里还能告诉你这条原来是什么料。
        if d["lcsc_pn"] and not keeper["lcsc_pn"]:
            con.execute("UPDATE component SET lcsc_pn=? WHERE id=?", (d["lcsc_pn"], keep))
            con.execute("UPDATE component SET lcsc_pn=NULL WHERE id=?", (did,))
            keeper = con.execute("SELECT * FROM component WHERE id=?", (keep,)).fetchone()
        fill, args = [], []
        for col in ("mpn", "manufacturer", "package", "value", "marking",
                    "datasheet_url", "product_url", "supplier", "default_loc_id",
                    "category", "note"):
            kval = con.execute(f"SELECT {col} AS v FROM component WHERE id=?",
                               (keep,)).fetchone()["v"]
            if not kval and d[col]:
                fill.append(f"{col}=?")
                args.append(d[col])
        if fill:
            con.execute(f"UPDATE component SET {', '.join(fill)} WHERE id=?",
                        args + [keep])
            keeper = con.execute("SELECT * FROM component WHERE id=?", (keep,)).fetchone()
        # 合并前把数值列重算一遍:补进来的 value 可能来自被并的那条
        db.set_value_num(con, keep, keeper["value"])
        # 品类文本也可能是刚补进来的,它在树里的位置得跟着重算 ——
        # 否则会出现「文本是电阻、category_id 还指着电容」这种自相矛盾的状态
        if any(s.startswith("category=") for s in fill):
            db.reconcile_categories(con)
        # 身份键同理:上面刚把被并那条的 lcsc_pn / mpn / value / package 补了进来,
        # 不重算的话保留的这条会顶着一个过期的键,下次导入认不出它。
        con.execute(
            "UPDATE component SET identity_key=? WHERE id=?",
            (bom.identity_key(keeper["value"], keeper["package"], keeper["mpn"],
                              keeper["lcsc_pn"], keeper["name"]), keep))

        # 2) 库存:同一仓位相加,不同仓位直接把行改成保留的那条
        for s in con.execute("SELECT location_id, qty FROM stock WHERE component_id=?",
                             (did,)).fetchall():
            have = con.execute("SELECT qty FROM stock WHERE component_id=? AND location_id=?",
                               (keep, s["location_id"])).fetchone()
            if have:
                con.execute("UPDATE stock SET qty=qty+? WHERE component_id=? AND location_id=?",
                            (s["qty"], keep, s["location_id"]))
                con.execute("DELETE FROM stock WHERE component_id=? AND location_id=?",
                            (did, s["location_id"]))
            else:
                con.execute("UPDATE stock SET component_id=? WHERE component_id=? AND location_id=?",
                            (keep, did, s["location_id"]))
            moved_stock += 1

        # 3) BOM 行:同一个项目里两边都有这一行的话,把两行并成一行(用量相加、
        #    位号拼起来)。不能直接改 component_id,那会撞上 (project_id, component_id)
        #    的唯一性,或者留下两行同料。
        for line in con.execute("SELECT * FROM project_bom WHERE component_id=?",
                                (did,)).fetchall():
            twin = con.execute(
                "SELECT * FROM project_bom WHERE project_id=? AND component_id=?",
                (line["project_id"], keep)).fetchone()
            if twin:
                con.execute(
                    """UPDATE project_bom
                       SET required_qty = required_qty + ?, placed_qty = placed_qty + ?,
                           received_qty = received_qty + ?,
                           designators = TRIM(COALESCE(designators,'') || ' ' ||
                                              COALESCE(?, '')),
                           optional = MAX(optional, ?), consumable = MAX(consumable, ?)
                       WHERE id=?""",
                    (line["required_qty"], line["placed_qty"], line["received_qty"],
                     line["designators"],
                     line["optional"], line["consumable"], twin["id"]))
                con.execute("DELETE FROM project_bom WHERE id=?", (line["id"],))
            else:
                con.execute("UPDATE project_bom SET component_id=? WHERE id=?",
                            (keep, line["id"]))
            moved_bom += 1

        # 4) 替代料:撞上同一行同一个替代料就直接删掉多余的
        for sub in con.execute("SELECT * FROM bom_substitute WHERE component_id=?",
                               (did,)).fetchall():
            if con.execute("SELECT 1 FROM bom_substitute WHERE bom_id=? AND component_id=?",
                           (sub["bom_id"], keep)).fetchone():
                con.execute("DELETE FROM bom_substitute WHERE id=?", (sub["id"],))
            else:
                con.execute("UPDATE bom_substitute SET component_id=? WHERE id=?",
                            (keep, sub["id"]))
        moved_po += con.execute("SELECT COUNT(*) AS n FROM purchase WHERE component_id=?",
                                (did,)).fetchone()["n"]
        con.execute("UPDATE purchase SET component_id=? WHERE component_id=?", (keep, did))

        # 5) 流水**不动** —— 那是真实发生过的收发货,改了就不是历史了。
        #    靠 merged_into 指向保留的那条,查的时候能追过去。
        con.execute("UPDATE component SET merged_into=?, updated_at=? WHERE id=?",
                    (keep, db.now(), did))

    con.commit()
    row = con.execute(COMPONENT_SELECT + " WHERE c.id=?", (keep,)).fetchone()
    return 200, {"ok": True, "keep": component_row(row), "dropped": drop,
                 "moved_bom_lines": moved_bom, "moved_stock_rows": moved_stock,
                 "moved_purchases": moved_po, "name": kname}


@route("GET", r"/api/components/merged")
def merged_components(ctx: Ctx, m):
    """已经并掉的元件(想看「当初并到哪儿去了」时用)。"""
    rows = ctx.con.execute(
        """SELECT c.id, c.name, c.mpn, c.lcsc_pn, c.merged_into,
                  k.name AS keep_name, c.updated_at
           FROM component c LEFT JOIN component k ON k.id = c.merged_into
           WHERE c.merged_into IS NOT NULL ORDER BY c.updated_at DESC LIMIT 300""")
    return 200, {"items": [db.row_to_dict(r) for r in rows]}


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
        # 这个项目**真的**发生过几次出入库,按方向分开。导入 BOM 本身不产生流水,
        # 所以刚导完是 0 —— 出入库页的项目下拉靠它只列出「选了不会看到空表」的项目。
        # 已撤销的(voided)和撤销动作本身(void_of)都不算:收进来又撤了等于没动过。
        cnt = ctx.con.execute(
            "SELECT COUNT(*) AS n,"
            " COALESCE(SUM(CASE WHEN kind='IN' THEN 1 ELSE 0 END),0) AS ins,"
            " COALESCE(SUM(CASE WHEN kind='OUT' THEN 1 ELSE 0 END),0) AS outs,"
            " MAX(created_at) AS t FROM movement"
            " WHERE project_id=? AND voided=0 AND void_of IS NULL",
            (r["id"],)).fetchone()
        d["moves"] = int(cnt["n"] or 0)
        d["moves_in"] = int(cnt["ins"] or 0)
        d["moves_out"] = int(cnt["outs"] or 0)
        d["last_move_at"] = cnt["t"]
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
    """项目的物料报告。

    **?pending=1 只留「还没做完」的行**(remaining > 0),另外回一个 hidden_done
    = 挡掉了几条。收料清单要的就是这个口径:一条需求全收完或全发完之后,不该
    再出现在开单页上,否则用户会以为「上一个 BOM 还能再收一遍 / 再出一遍」(#31)。

    默认(不带参数)照样返回全部行 —— BOM 明细、缺料导出那些地方要的是全貌,
    在那里把做完的行藏起来才是真的丢数据。
    """
    pid = int(m.group(1))
    proj = ctx.con.execute("SELECT * FROM project WHERE id=?", (pid,)).fetchone()
    if not proj:
        raise ApiError(404, "项目不存在")
    rep = bom.shortage_report(ctx.con, pid)
    if str(ctx.q("pending") or "").strip() in ("1", "true", "yes"):
        rows = [l for l in rep["lines"] if int(l.get("remaining") or 0) > 0]
        # 被挡掉几条要让界面说得出「N 条已经做完,不再列出」——
        # 不说的话用户只会看到行数变少,以为数据丢了
        rep["hidden_done"] = len(rep["lines"]) - len(rows)
        rep["lines"] = rows
        rep["line_count"] = len(rows)
    rep["project"] = db.row_to_dict(proj)
    return 200, rep


@route("GET", r"/api/projects/(\d+)/pick_plan")
def project_pick_plan(ctx: Ctx, m):
    """出库分配方案:每条 BOM 需求 + 库存里能拿来凑它的元件。

    为什么要有这个接口:导出的 BOM 要的是「100nF 10 个」,而库里可能
    0603 有 8 个、0805 有 2 个 —— 一条需求常常要由几颗不同的库存料凑齐。
    界面得知道「这一行还差多少」和「哪些料能凑」,而且勾选、改数量时要能
    在本地反复试算,不能每动一下就往返查一次库。

    remaining 是「还能出多少」(需求 − 已发料 − 已入库),就是界面上的「还能出库」。
    已经做完的行(remaining = 0)**在这里就挡掉**,不回给界面:

      * 这是账的口径,不是某个面板的显示偏好 —— 收料页、出库页、以后新加的
        入口都该是同一个「这条需求还欠着吗」的答案,让每个调用方自己记得筛,
        迟早漏一个,那一行就又冒出来(#31 就是这么冒出来的)。
      * 顺手省掉给这些行算相似候选:它们本来一个都不会被勾。

    挡掉几条会放进 hidden_done,界面拿它说一句「N 条已经做完,不再列出」。
    """
    pid = int(m.group(1))
    proj = ctx.con.execute("SELECT * FROM project WHERE id=?", (pid,)).fetchone()
    if not proj:
        raise ApiError(404, "项目不存在")
    rep = bom.build_report(ctx.con, pid)

    try:
        limit = int(ctx.q("limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(limit, 30))

    lines = []
    hidden_done = 0
    for line in rep["lines"]:
        # remaining = 需求 − 已发料 − 已入库,由 build_report 一处算好,
        # 这里不许再自己减一遍(两处各减一次,迟早只改一处)
        remaining = max(0, int(line.get("remaining") or 0))
        if remaining <= 0:
            hidden_done += 1
            continue
        cands = similar_components(
            ctx.con, value=line["value"], package=line["package"],
            category=line["category"], limit=limit, in_stock_only=True)
        for c in cands:
            c["own"] = False
            c["substitute"] = False

        # 这一行自己指定的那颗料,只要还有库存就必须排在最前面 ——
        # 它才是 BOM 本来要的东西,相似度再高也只是「像」。
        # 相似匹配找不到它(值不认、封装也不同)时也得补上,否则用户
        # 在树上根本选不到自己 BOM 里写的那颗料。
        row = ctx.con.execute(COMPONENT_SELECT + " WHERE c.id=? AND c.merged_into IS NULL",
                              (line["component_id"],)).fetchone()
        if row and (row["on_hand"] or 0) > 0:
            entry = component_row(row)
            entry.update({"score": None, "match": "BOM 本行指定的料",
                          "verdict": "BOM 里就是它", "own": True, "substitute": False})
            cands = [c for c in cands if c["id"] != entry["id"]]
            cands.insert(0, entry)

        # 替代料也放进来:用户设替代料就是为了「这颗不够时用那颗顶」,
        # 而它的值可能和本行完全不同(相似匹配找不到),只按相似度会漏掉
        known = {c["id"] for c in cands}
        for s in line["substitutes"]:
            if s["component_id"] in known or not s["on_hand"]:
                continue
            srow = ctx.con.execute(
                COMPONENT_SELECT + " WHERE c.id=? AND c.merged_into IS NULL",
                (s["component_id"],)).fetchone()
            if not srow:
                continue
            entry = component_row(srow)
            entry.update({"score": None, "match": "BOM 里登记的替代料",
                          "verdict": "替代料", "own": False, "substitute": True})
            cands.append(entry)
            known.add(entry["id"])

        lines.append(dict(line, remaining=remaining, candidates=cands))

    return 200, {
        "project_id": pid, "project_name": proj["name"], "boards": rep["boards"],
        "line_count": len(lines), "lines": lines, "hidden_done": hidden_done,
        "hint": "一条 BOM 需求可以由几颗不同的库存料凑齐;勾选后确认出库。"
                + (f"(另有 {hidden_done} 条已经做完,不再列出)" if hidden_done else ""),
    }


@route("POST", r"/api/projects/(\d+)/pick")
def pick_for_project(ctx: Ctx, m):
    """按 BOM 领料:对指定项目批量出库。items 传 [{component_id, qty, location_id?}] 或留空=整单。"""
    pid = int(m.group(1))
    proj = ctx.con.execute("SELECT * FROM project WHERE id=?", (pid,)).fetchone()
    if not proj:
        raise ApiError(404, "项目不存在")

    requested = ctx.b("items")
    if requested:
        # bom_id:这条出库是为哪一条 BOM 需求发的。一条需求可能由几颗不同的
        # 库存料凑齐(0603 出 8 个 + 0805 出 2 个),不指明就对不上账。
        plan = [(int(i["component_id"]), int(i["qty"]),
                 i.get("location_id") or i.get("location"), i.get("bom_id"))
                for i in requested]
    else:
        rep = bom.build_report(ctx.con, pid)
        plan = []
        for line in rep["lines"]:
            # 「还能出多少」直接用报告算好的 remaining(已经减掉已发料和已入库)。
            # 这里再自己写一遍 need − placed 就会漏掉收料那一本账 —— 收了货之后
            # 整单出库会把刚收进来的那些又发一遍。
            need = line["remaining"]
            if need > 0:
                plan.append((line["component_id"], need, None, line["bom_id"]))

    default_loc = ctx.b("location") or ctx.b("location_id") or "未分类"
    done, failed = [], []
    for cid, qty, loc, bom_id in plan:
        try:
            if qty <= 0:
                continue
            loc_id = _get_location_id(ctx.con, loc or default_loc)
            # spill=True:目标仓位不够时,只要总量够就从其他仓位凑
            ids = _apply_move(ctx.con, "OUT", cid, qty, loc_id, project_id=pid,
                              bom_id=bom_id, ref=ctx.b("ref"),
                              operator=ctx.b("operator"), note=f"项目领料({proj['name']})",
                              spill=True)
            if bom_id:
                _credit_bom(ctx.con, bom_id, qty, "OUT")
            else:
                # 没指明是哪条需求(自由出库直接传元件):按项目 + 元件找那条,
                # 记的仍然是「已发料」这一本 —— 出库永远只动发料进度
                ctx.con.execute(
                    """UPDATE project_bom SET placed_qty = placed_qty + ?
                       WHERE project_id=? AND component_id=?""", (qty, pid, cid))
            db.touch_component(ctx.con, cid)
            done.append({"component_id": cid, "qty": qty, "bom_id": bom_id,
                         "movement_ids": ids})
        except ApiError as exc:
            failed.append({"component_id": cid, "qty": qty, "bom_id": bom_id,
                           "reason": exc.message})
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

    was = _qty_at(ctx.con, row["component_id"], loc_id)
    _bump(ctx.con, row["component_id"], loc_id, qty)
    got = int(row["received"]) + qty
    done = got >= int(row["qty"])
    ctx.con.execute(
        "UPDATE purchase SET received=?, status=?, arrived_at=? WHERE id=?",
        (got, "arrived" if done else "ordered",
         (row["arrived_at"] or db.now()) if done else None, pid))
    _log_move(ctx.con, "IN", row["component_id"], loc_id, qty,
              project_id=row["project_id"], purchase_id=pid,
              ref=ctx.b("ref") or f"PO-{pid}", operator=ctx.b("operator"),
              qty_before=was,
              note=f"采购到货({row['supplier'] or '未填供应商'})")
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
        if d["min_stock"] and d["on_hand"] <= d["min_stock"]:
            # 口径是「库存 ≤ 安全库存」(#38),所以相等时差值就是 0 ——
            # 那时候写「低于安全库存 0」读起来别扭,说「已到安全库存」才准。
            _gap = d["min_stock"] - d["on_hand"]
            reasons.append("已到安全库存" if _gap <= 0 else f"低于安全库存 {_gap}")
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
                                      AND on_hand <= min_stock THEN 1 ELSE 0 END),0) AS low_kinds
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


def _bom_reject(filename: str):
    """只认 BOM 类文件。报错时把能接受的后缀说清楚,别让人猜。"""
    if not filename.lower().endswith((".xlsx", ".xlsm", ".csv", ".tsv", ".txt")):
        raise ApiError(400, "只支持 .xlsx / .xlsm / .csv(Altium 的 Excel、"
                            "KiCad / EasyEDA / 立创导出的 CSV 都能直接导)")


@route("POST", r"/api/bom/preview")
def bom_preview(ctx: Ctx, m):
    filename, data = _read_upload(ctx)
    _bom_reject(filename)
    path = _save_temp_upload(data, filename)
    try:
        items, warnings = bom.parse_any(path, sheet_name=ctx.b("sheet"))
    except Exception as exc:
        raise ApiError(400, f"解析失败:{exc}")
    is_excel = filename.lower().endswith((".xlsx", ".xlsm"))
    return 200, {
        "filename": filename, "saved_to": path,
        "sheets": xlsx.sheet_names(path) if is_excel else [],
        "line_count": len(items), "total_qty": sum(i["qty"] for i in items),
        "warnings": warnings,
        # 有多少行的品类是「猜的、或者线索打架」,复核时要一眼看到 ——
        # 让人从头到尾逐行看一遍是不现实的,他会直接点确定
        "need_review": sum(1 for i in items
                           if i.get("category_confidence") in ("low", "none")),
        "categories": list(bom.CATEGORIES),
        "lines": [{
            # source_row 是复核结果回传时的键。按行号而不是按位置回传,
            # 因为解析时会跳过空行/重复行,按位置对会整体错位
            "source_row": i.get("source_row"),
            "lcsc_pn": i["lcsc_pn"], "mpn": i["mpn"], "name": i["name"],
            "category": i["category"], "package": i["package"], "value": i["value"],
            "qty": i["qty"], "designators": ",".join(i["designators"]),
            "confidence": i.get("category_confidence") or "none",
            # 连显示名一起给:免得界面里再抄一份「high -> 明确」的映射,
            # 两份清单迟早走散
            "confidence_label": bom.CONF_LABEL.get(
                i.get("category_confidence") or "none", ""),
            "reason": i.get("category_reason") or "",
        } for i in items],
    }


@route("POST", r"/api/bom/import")
def bom_import(ctx: Ctx, m):
    filename, data = _read_upload(ctx)
    _bom_reject(filename)
    path = _save_temp_upload(data, filename)
    project_name = ctx.b("project_name") or os.path.splitext(filename)[0]
    try:
        report = bom.import_bom(
            ctx.con, path, project_name=project_name,
            project_code=ctx.b("project_code"), repo=ctx.b("repo"),
            sheet_name=ctx.b("sheet"),
            replace_existing=bool(ctx.b("replace", True)),
            # 导入前人工复核的结果:{行号: 品类}。没复核过就是 None,走推断结果
            categories=ctx.b("categories"),
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
