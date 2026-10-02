# -*- coding: utf-8 -*-
"""数据库层:SQLite 建表、连接与通用工具。

设计要点:
  * 元件以「立创编号 LCSC C-号」为自然键,缺失时回退厂家料号(MPN)。
  * 库存余额(stock)与流水(movement)分离:流水只增不改,余额可随时重算校验。
  * 品类相关参数(耐压/精度/功率…)放 params 的 JSON 字段,避免频繁加列。
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

-- 项目(先建,movement 要引用)
CREATE TABLE IF NOT EXISTS project (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL,
  code       TEXT,
  repo       TEXT,
  status     TEXT NOT NULL DEFAULT 'active',
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
  package       TEXT,                           -- 0805 / SOT-23
  params        TEXT NOT NULL DEFAULT '{}',     -- JSON:{"耐压":"50V"}
  datasheet_url TEXT,
  product_url   TEXT,
  unit          TEXT NOT NULL DEFAULT '个',
  min_stock     INTEGER NOT NULL DEFAULT 0,     -- 安全库存
  note          TEXT,
  created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_component_mpn ON component(mpn);
CREATE INDEX IF NOT EXISTS idx_component_cat ON component(category);
CREATE INDEX IF NOT EXISTS idx_component_pkg ON component(package);
CREATE INDEX IF NOT EXISTS idx_component_val ON component(value);

-- 仓位(料柜-层-格)
CREATE TABLE IF NOT EXISTS location (
  id   INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL UNIQUE,
  note TEXT
);

-- 库存余额:元件 × 仓位
CREATE TABLE IF NOT EXISTS stock (
  component_id INTEGER NOT NULL REFERENCES component(id) ON DELETE CASCADE,
  location_id  INTEGER NOT NULL REFERENCES location(id) ON DELETE CASCADE,
  qty          INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (component_id, location_id)
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
  ref            TEXT,                          -- 单据号
  operator       TEXT,
  note           TEXT,
  created_at     TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_movement_comp ON movement(component_id);
CREATE INDEX IF NOT EXISTS idx_movement_time ON movement(created_at);
CREATE INDEX IF NOT EXISTS idx_movement_proj ON movement(project_id);

-- 项目 BOM
CREATE TABLE IF NOT EXISTS project_bom (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id   INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
  component_id INTEGER NOT NULL REFERENCES component(id) ON DELETE CASCADE,
  required_qty INTEGER NOT NULL,
  designators  TEXT,
  placed_qty   INTEGER NOT NULL DEFAULT 0,
  UNIQUE (project_id, component_id)
);
CREATE INDEX IF NOT EXISTS idx_bom_proj ON project_bom(project_id);
"""

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


def init_db(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    cur = con.execute("SELECT value FROM meta WHERE key='schema_version'")
    row = cur.fetchone()
    if row is None:
        con.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),)
        )
    for code in LOCATIONS_SEED:
        con.execute("INSERT OR IGNORE INTO location(code) VALUES(?)", (code,))
    con.commit()


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


def recalc_stock(con: sqlite3.Connection, component_id: int) -> int:
    """按流水重算总库存(校验用)。返回总数。"""
    cur = con.execute("SELECT COALESCE(SUM(qty),0) AS n FROM stock WHERE component_id=?",
                      (component_id,))
    return int(cur.fetchone()["n"])


def stock_total(con: sqlite3.Connection, component_id: int) -> int:
    row = con.execute(
        "SELECT COALESCE(SUM(qty),0) AS n FROM stock WHERE component_id=?", (component_id,)
    ).fetchone()
    return int(row["n"])
