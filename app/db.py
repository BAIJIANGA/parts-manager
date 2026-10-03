# -*- coding: utf-8 -*-
"""数据库层:SQLite 建表、连接与通用工具。

设计要点(参考 InvenTree 的建模,砍掉单机用不上的部分):
  * 元件以「立创编号 LCSC C-号」为自然键,缺失时回退厂家料号(MPN)。
  * **库存余额(stock)与流水(movement)分离**:流水只增不改,余额可随时重算校验。
  * **仓位是层级的**(柜 → 层 → 格)。真实的料柜就是层级结构,平铺一层根本
    表达不了「A柜第 2 层左边那格」。structural=1 的仓位只用来分层,自己不装东西。
  * 品类相关参数(耐压/精度/功率…)放 component.params 的 JSON 字段,避免频繁加列。
  * BOM 行按 InvenTree 的做法带 **可选 / 免点 / 损耗率 / 固定损耗**,并有独立的
    **替代料**表 —— 「能造几块」这个算法离开这几个字段就不准。
  * **采购单(purchase)** 里「已下单未到货」的数量就是**在途**,采购建议要减掉它,
    否则会重复买。

初始化必须分三步走,顺序不能反:
    建表 → 补列(老库升级)→ 建索引
因为老库里 `location` 表已经存在但没有 `parent_id`,要是索引跟建表写在一起,
`CREATE INDEX ... ON location(parent_id)` 会先炸。
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime

SCHEMA_VERSION = 2

TABLES = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

-- 项目(= 要做的板子/产品)。qty 是「计划做几块」,需求要靠它乘出来。
CREATE TABLE IF NOT EXISTS project (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL,
  code       TEXT,
  repo       TEXT,
  status     TEXT NOT NULL DEFAULT 'active',
  qty        INTEGER NOT NULL DEFAULT 1,
  note       TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 元件主数据
CREATE TABLE IF NOT EXISTS component (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  lcsc_pn       TEXT UNIQUE,                    -- 立创编号 C22367837
  mpn           TEXT,                           -- 厂家料号
  manufacturer  TEXT,                           -- 厂家
  name          TEXT NOT NULL,                  -- 显示名
  category      TEXT NOT NULL DEFAULT '其他',
  value         TEXT,                           -- 1uF / 10k
  value_num     REAL,                           -- 由 value 解析出的数值,用来正确排序/筛选
  value_unit    TEXT,                           -- 解析出的单位(F / Ω / H …)
  package       TEXT,                           -- 0805 / SOT-23
  marking       TEXT,                           -- 丝印/顶标:SOT-23 上那三个字母,拆机料全靠它认
  params        TEXT NOT NULL DEFAULT '{}',     -- JSON:{"耐压":"50V"}
  datasheet_url TEXT,
  product_url   TEXT,
  unit          TEXT NOT NULL DEFAULT '个',
  min_stock     INTEGER NOT NULL DEFAULT 0,     -- 安全库存(低于它就该补货)
  reorder_qty   INTEGER NOT NULL DEFAULT 0,     -- 建议补货量(0 = 自动按缺口算)
  supplier      TEXT,                           -- 常用供应商
  unit_price    REAL NOT NULL DEFAULT 0,        -- 参考单价,用来估库存价值
  default_loc_id INTEGER REFERENCES location(id) ON DELETE SET NULL,
  merged_into   INTEGER REFERENCES component(id), -- 已并入哪个元件(合并采用标记,不删行)
  note          TEXT,
  created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 仓位:层级结构(柜 → 层 → 格)。structural=1 只分层,不直接装东西。
CREATE TABLE IF NOT EXISTS location (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  parent_id  INTEGER REFERENCES location(id) ON DELETE CASCADE,
  code       TEXT NOT NULL UNIQUE,
  name       TEXT,
  structural INTEGER NOT NULL DEFAULT 0,
  note       TEXT
);

-- 库存余额:元件 × 仓位
CREATE TABLE IF NOT EXISTS stock (
  component_id INTEGER NOT NULL REFERENCES component(id) ON DELETE CASCADE,
  location_id  INTEGER NOT NULL REFERENCES location(id) ON DELETE CASCADE,
  qty          INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (component_id, location_id)
);

-- 采购单。status='ordered' 的数量就是「在途」。movement 要引用它,所以先建。
CREATE TABLE IF NOT EXISTS purchase (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  component_id INTEGER NOT NULL REFERENCES component(id) ON DELETE CASCADE,
  qty          INTEGER NOT NULL,
  received     INTEGER NOT NULL DEFAULT 0,      -- 已到货数量(支持分批到货)
  unit_price   REAL NOT NULL DEFAULT 0,
  supplier     TEXT,
  status       TEXT NOT NULL DEFAULT 'todo'
               CHECK (status IN ('todo','ordered','arrived','cancel')),
  project_id   INTEGER REFERENCES project(id) ON DELETE SET NULL,
  created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  ordered_at   TEXT,
  arrived_at   TEXT,
  note         TEXT
);

-- 出入库流水(只增不改)
CREATE TABLE IF NOT EXISTS movement (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  kind           TEXT NOT NULL CHECK (kind IN ('IN','OUT','ADJUST','TRANSFER')),
  component_id   INTEGER NOT NULL REFERENCES component(id) ON DELETE CASCADE,
  location_id    INTEGER REFERENCES location(id),
  to_location_id INTEGER REFERENCES location(id),
  qty            INTEGER NOT NULL,              -- 恒为正数,方向由 kind 决定
  project_id     INTEGER REFERENCES project(id) ON DELETE SET NULL,
  purchase_id    INTEGER REFERENCES purchase(id) ON DELETE SET NULL,
  ref            TEXT,                          -- 单据号
  operator       TEXT,
  note           TEXT,
  qty_before     INTEGER,                       -- 这一笔之前该仓位的数量(撤销 ADJUST 要用)
  voided         INTEGER NOT NULL DEFAULT 0,    -- 这笔已被撤销
  void_of        INTEGER REFERENCES movement(id), -- 这笔是在撤销哪一笔
  created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 项目 BOM。optional/consumable/attrition/setup_qty 直接决定 can_build 怎么算。
CREATE TABLE IF NOT EXISTS project_bom (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id   INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
  component_id INTEGER NOT NULL REFERENCES component(id) ON DELETE CASCADE,
  required_qty INTEGER NOT NULL,                -- 单块用量
  designators  TEXT,                            -- 位号 R1,R2,R3
  placed_qty   INTEGER NOT NULL DEFAULT 0,
  optional     INTEGER NOT NULL DEFAULT 0,      -- 可选件:不装也能出货
  consumable   INTEGER NOT NULL DEFAULT 0,      -- 免点件(螺丝/锡):算需求但不卡「能造几块」
  attrition    REAL NOT NULL DEFAULT 0,         -- 损耗率 %
  setup_qty    INTEGER NOT NULL DEFAULT 0,      -- 固定损耗(试产报废)
  note         TEXT,
  UNIQUE (project_id, component_id)
);

-- 替代料:某个 BOM 行的这个料还能用哪些别的料顶上
CREATE TABLE IF NOT EXISTS bom_substitute (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  bom_id       INTEGER NOT NULL REFERENCES project_bom(id) ON DELETE CASCADE,
  component_id INTEGER NOT NULL REFERENCES component(id) ON DELETE CASCADE,
  note         TEXT,
  UNIQUE (bom_id, component_id)
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_component_mpn ON component(mpn);
CREATE INDEX IF NOT EXISTS idx_component_cat ON component(category);
CREATE INDEX IF NOT EXISTS idx_component_pkg ON component(package);
CREATE INDEX IF NOT EXISTS idx_component_val ON component(value);
CREATE INDEX IF NOT EXISTS idx_component_valnum ON component(value_num);
CREATE INDEX IF NOT EXISTS idx_location_parent ON location(parent_id);
CREATE INDEX IF NOT EXISTS idx_movement_comp ON movement(component_id);
CREATE INDEX IF NOT EXISTS idx_movement_time ON movement(created_at);
CREATE INDEX IF NOT EXISTS idx_movement_proj ON movement(project_id);
CREATE INDEX IF NOT EXISTS idx_bom_proj ON project_bom(project_id);
CREATE INDEX IF NOT EXISTS idx_sub_bom ON bom_substitute(bom_id);
CREATE INDEX IF NOT EXISTS idx_purchase_comp ON purchase(component_id);
CREATE INDEX IF NOT EXISTS idx_purchase_status ON purchase(status);
"""

# v1 的库缺这些列,启动时按需补上(ALTER TABLE ADD COLUMN 是幂等安全的)。
# 注意:带 REFERENCES 的列不能有非 NULL 默认值,SQLite 会拒绝。
ADDED_COLUMNS = {
    "project": {
        "qty": "INTEGER NOT NULL DEFAULT 1",
    },
    "component": {
        "value_num": "REAL",
        "value_unit": "TEXT",
        # 丝印/顶标。拆下来的料、丝印不明的小芯片,就靠这三个字母查回来 ——
        # 社区里「这是什么芯片」是最高频的求助,所以它必须能搜、且值得单独一列。
        "marking": "TEXT",
        "reorder_qty": "INTEGER NOT NULL DEFAULT 0",
        "supplier": "TEXT",
        "unit_price": "REAL NOT NULL DEFAULT 0",
        "default_loc_id": "INTEGER REFERENCES location(id) ON DELETE SET NULL",
        # 合并重复元件时,被并掉的那条**不删**,只记「并到谁那儿去了」。
        # 删掉的话,它名下的流水会被外键 ON DELETE CASCADE 一起带走 ——
        # 那些流水是真实发生过的收发货,删了历史就断了,而且没法后悔。
        # 标成 merged_into 之后:列表里不再出现,但流水、单据、BOM 都还查得到。
        "merged_into": "INTEGER REFERENCES component(id)",
    },
    "location": {
        "parent_id": "INTEGER REFERENCES location(id) ON DELETE CASCADE",
        "name": "TEXT",
        "structural": "INTEGER NOT NULL DEFAULT 0",
    },
    "project_bom": {
        "optional": "INTEGER NOT NULL DEFAULT 0",
        "consumable": "INTEGER NOT NULL DEFAULT 0",
        "attrition": "REAL NOT NULL DEFAULT 0",
        "setup_qty": "INTEGER NOT NULL DEFAULT 0",
        "note": "TEXT",
    },
    "movement": {
        "purchase_id": "INTEGER REFERENCES purchase(id) ON DELETE SET NULL",
        # 这条流水是为**哪一条 BOM 需求**发的。一条 BOM 需求可能由几颗不同的
        # 库存料凑齐(0603 出 8 个 + 0805 出 2 个),光看 component_id 分不清
        # 它们是在顶同一条需求。项目 BOM 被重新导入时,旧行的 id 会消失,
        # 这一列就置空 —— 与其记一个指向别处的号,不如老实说「那条已经不在了」。
        "bom_id": "INTEGER REFERENCES project_bom(id) ON DELETE SET NULL",
        # 撤销用的三列。做法是**写一条反向流水**并把原记录标记为已撤销,
        # 而不是把原记录删掉 —— 账本必须能重建,删了就查不出「那天到底是谁
        # 把它改成这样的」。qty_before 只在撤销盘点(ADJUST)时才用得上:
        # 盘点记录里存的 qty 是「新数量」,不知道原来是多少就没法还原。
        "qty_before": "INTEGER",
        "voided": "INTEGER NOT NULL DEFAULT 0",
        "void_of": "INTEGER REFERENCES movement(id)",
    },
    "purchase": {
        "received": "INTEGER NOT NULL DEFAULT 0",
    },
}

LOCATIONS_SEED = ["未分类"]


def connect(db_path: str) -> sqlite3.Connection:
    """打开连接。temp_store=MEMORY 是刻意的:避免 SQLite 往系统临时目录写文件。"""
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA temp_store = MEMORY")
    return con


def _columns(con: sqlite3.Connection, table: str) -> set:
    return {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}


def init_db(con: sqlite3.Connection) -> list:
    """建表 → 补列 → 建索引。返回这次补出来的列名(供日志/自检看)。"""
    con.executescript(TABLES)

    upgraded = []
    for table, cols in ADDED_COLUMNS.items():
        have = _columns(con, table)
        for col, decl in cols.items():
            if col not in have:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                upgraded.append(f"{table}.{col}")

    con.executescript(INDEXES)

    row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        con.execute("INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),))
    elif row["value"] != str(SCHEMA_VERSION):
        con.execute("UPDATE meta SET value=? WHERE key='schema_version'",
                    (str(SCHEMA_VERSION),))

    for code in LOCATIONS_SEED:
        con.execute("INSERT OR IGNORE INTO location(code, name) VALUES(?, ?)", (code, code))
    con.commit()
    return upgraded


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def row_to_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


def parse_params(raw) -> dict:
    """把 params 字段安全地解析成 dict。"""
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {}
    except (ValueError, TypeError):
        return {}


def dump_params(obj) -> str:
    if not obj:
        return "{}"
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except ValueError:
            return "{}"
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def touch_component(con: sqlite3.Connection, component_id: int) -> None:
    con.execute("UPDATE component SET updated_at=? WHERE id=?", (now(), component_id))


def stock_total(con: sqlite3.Connection, component_id: int) -> int:
    row = con.execute(
        "SELECT COALESCE(SUM(qty),0) AS n FROM stock WHERE component_id=?", (component_id,)
    ).fetchone()
    return int(row["n"])


# 兼容老名字(server.py 里在用)
recalc_stock = stock_total


def location_path(con: sqlite3.Connection, location_id: int) -> str:
    """把仓位拼成 'A柜 / 01层 / 02格' 这样的全路径。"""
    parts, cur, guard = [], location_id, 0
    while cur and guard < 32:
        row = con.execute("SELECT id, parent_id, code, name FROM location WHERE id=?",
                          (cur,)).fetchone()
        if row is None:
            break
        parts.append(row["name"] or row["code"])
        cur, guard = row["parent_id"], guard + 1
    return " / ".join(reversed(parts))


def set_value_num(con: sqlite3.Connection, component_id: int, value) -> None:
    """把 value 解析成数值存进去。排序和「按阻值筛 1k~100k」都靠它。"""
    import values
    num, unit = values.parse_value(value)
    con.execute("UPDATE component SET value_num=?, value_unit=? WHERE id=?",
                (num, unit, component_id))


def backfill_values(con: sqlite3.Connection) -> int:
    """给还没算过数值列的元件补上。返回补了几条。"""
    import values
    rows = con.execute(
        "SELECT id, value FROM component "
        "WHERE value_num IS NULL AND value IS NOT NULL AND value <> ''").fetchall()
    for r in rows:
        num, unit = values.parse_value(r["value"])
        con.execute("UPDATE component SET value_num=?, value_unit=? WHERE id=?",
                    (num, unit, r["id"]))
    if rows:
        con.commit()
    return len(rows)
