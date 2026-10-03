# -*- coding: utf-8 -*-
"""#6 品类表迁移探针。

只用**副本**:真实库 data/parts.db 一个字节都不动 —— 用户就在用那个库。
验四件事:
  1. 全新库:品类表先按内置清单建好,不用用户自己敲
  2. 老库升上来:库里在用的品类都变成了行,每个元件都挂上了 category_id
  3. 幂等:init_db 连跑三次,行数和挂靠数都不变
  4. 对账:元件文本改了 -> 跟着改挂;子类节点不会被压平到顶层
"""
import io
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))          # build/checks
ROOT = os.path.dirname(os.path.dirname(HERE))              # parts-manager
sys.path.insert(0, os.path.join(ROOT, "app"))

import bom      # noqa: E402
import db       # noqa: E402

OUT = []
FAILS = []


def check(label, got, want):
    ok = got == want
    OUT.append(f"  [{'OK ' if ok else 'FAIL'}] {label}: {got!r}"
               + ("" if ok else f"  期望 {want!r}"))
    if not ok:
        FAILS.append(label)


def count(con, sql, *args):
    return con.execute(sql, args).fetchone()[0]


def main():
    # 用工作区里的目录,不用系统临时目录 —— 沙箱只保证工作区可写。
    # 每次先清干净,免得读到上一遍的残留。
    tmp = os.path.join(ROOT, "build", "cache", "catprobe")
    if os.path.isdir(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp)
    OUT.append("=" * 70)
    OUT.append("【1】全新库:内置品类直接建好")
    p1 = os.path.join(tmp, "fresh.db")
    con = db.connect(p1)
    up = db.init_db(con)
    # 全新库这一列是 CREATE TABLE 建出来的,所以不会出现在「补列」清单里 ——
    # 清单只记 ALTER 出来的。这里要验的是「列在」,不是「补过」。
    cols = {r["name"] for r in con.execute("PRAGMA table_info(component)")}
    check("新库有 category_id 列", "category_id" in cols, True)
    check("新库有 category 表",
          count(con, "SELECT COUNT(*) FROM sqlite_master "
                     "WHERE type='table' AND name='category'"), 1)
    n = count(con, "SELECT COUNT(*) FROM category WHERE parent_id IS NULL")
    check("顶层品类数 = 内置清单", n, len(bom.CATEGORIES))
    names = [r["name"] for r in con.execute(
        "SELECT name FROM category WHERE parent_id IS NULL ORDER BY sort")]
    check("而且顺序就是内置清单的顺序", names, list(bom.CATEGORIES))
    check("新库里一个元件都没有", count(con, "SELECT COUNT(*) FROM component"), 0)
    check("schema_version 提到 4",
          con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0],
          "4")

    OUT.append("")
    OUT.append("【2】老库升上来:文本品类变成真正的行")
    src = os.path.join(ROOT, "data", "parts.db")
    if not os.path.exists(src):
        OUT.append("  (没有 data/parts.db,跳过 —— 用造出来的老库代替)")
        p2 = os.path.join(tmp, "legacy.db")
        con2 = db.connect(p2)
        con2.executescript("""
            CREATE TABLE IF NOT EXISTS component (
              id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
              category TEXT NOT NULL DEFAULT '其他', value TEXT, package TEXT,
              params TEXT NOT NULL DEFAULT '{}', unit TEXT NOT NULL DEFAULT '个',
              min_stock INTEGER NOT NULL DEFAULT 0, unit_price REAL NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
              updated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
            INSERT INTO component(name, category, value, package) VALUES
              ('100nF', '电容', '100nF', '0603'),
              ('10k', '电阻', '10k', '0603'),
              ('自造品类', '用户自己敲的', 'x', 'y');
        """)
        con2.commit()
    else:
        p2 = os.path.join(tmp, "legacy.db")
        shutil.copy2(src, p2)
        OUT.append(f"  (用的是 {os.path.getsize(src)} 字节的副本,原库没动)")
        con2 = db.connect(p2)
    up2 = db.init_db(con2)
    check("老库升上来时,补列清单里有 component.category_id",
          "component.category_id" in up2, True)
    check("老库升上来时,补列清单里有 category 表(表不是 ALTER 出来的,靠 TABLES 建)",
          count(con2, "SELECT COUNT(*) FROM sqlite_master "
                      "WHERE type='table' AND name='category'"), 1)
    kinds_before = [r["category"] for r in con2.execute(
        "SELECT DISTINCT category FROM component ORDER BY category")]
    n_comp = count(con2, "SELECT COUNT(*) FROM component")
    OUT.append(f"  库里 {n_comp} 个元件,用到 {len(kinds_before)} 种品类:{kinds_before}")
    missing = count(con2, "SELECT COUNT(*) FROM component WHERE category_id IS NULL")
    check("每个元件都挂上了 category_id", missing, 0)
    for k in kinds_before:
        row = con2.execute("SELECT id, parent_id FROM category WHERE name=? AND parent_id IS NULL",
                           (k,)).fetchone()
        check(f"「{k}」在品类表里有顶层行", row is not None, True)
    # 挂的节点必须和文本一致(顶层名字 == component.category)
    bad = count(con2, """
        SELECT COUNT(*) FROM component c
        LEFT JOIN category r ON r.id = c.category_id
        WHERE c.category_id IS NOT NULL AND COALESCE(r.name,'') <> c.category""")
    check("挂的节点名字和文本一致", bad, 0)
    check("内置品类一个不少",
          count(con2, "SELECT COUNT(*) FROM category WHERE parent_id IS NULL"),
          len(set(list(bom.CATEGORIES) + kinds_before)))

    OUT.append("")
    OUT.append("【3】幂等:再跑两遍,什么都不该变")
    snap = lambda c: (count(c, "SELECT COUNT(*) FROM category"),
                      count(c, "SELECT COUNT(*) FROM component WHERE category_id IS NULL"),
                      count(c, "SELECT COUNT(*) FROM component"))
    s1 = snap(con2)
    db.init_db(con2)
    s2 = snap(con2)
    db.init_db(con2)
    s3 = snap(con2)
    check("第一遍之后 (品类数, 没挂的, 元件数)", s2, s1)
    check("第二遍之后还是不变", s3, s1)

    OUT.append("")
    OUT.append("【4】对账:2 级品类、改挂、删节点")
    # 搭一个二级品类:电容 -> 无极性陶瓷电容
    top = con2.execute("SELECT id FROM category WHERE name='电容' AND parent_id IS NULL").fetchone()["id"]
    child = db.ensure_category(con2, "无极性陶瓷电容", parent_id=top)
    con2.commit()
    check("子节点建出来了", child is not None and child != top, True)
    check("路径拼得对", db.category_path(con2, child), "电容 / 无极性陶瓷电容")
    check("顶层能找回去", db.category_root(con2, child)["name"], "电容")
    # 把一个元件挂到子类上:文本仍是「电容」,不该被对账压平
    one = con2.execute("SELECT id FROM component WHERE category='电容' LIMIT 1").fetchone()
    if one is not None:
        con2.execute("UPDATE component SET category_id=? WHERE id=?", (child, one["id"]))
        con2.commit()
        db.reconcile_categories(con2)
        still = con2.execute("SELECT category_id FROM component WHERE id=?",
                             (one["id"],)).fetchone()["category_id"]
        check("挂在子类上的元件不会被对账压平回顶层", still, child)
        # 文本改成别的品类 -> 要跟着改挂
        con2.execute("UPDATE component SET category='电阻' WHERE id=?", (one["id"],))
        con2.commit()
        db.reconcile_categories(con2)
        moved = con2.execute(
            "SELECT r.name, r.parent_id FROM component c JOIN category r ON r.id=c.category_id "
            "WHERE c.id=?", (one["id"],)).fetchone()
        check("文本改成「电阻」之后跟着改挂", (moved["name"], moved["parent_id"]),
              ("电阻", None))
    # ensure_category 同名同层只建一条
    a = db.ensure_category(con2, "无极性陶瓷电容", parent_id=top)
    b = db.ensure_category(con2, "无极性陶瓷电容", parent_id=top)
    check("同名同层不会建出两条", a, b)
    check("顶层同名也不会建出两条",
          db.ensure_category(con2, "电容"), top)
    # 删掉子节点:元件不该被删,只是 category_id 被置空或挪走
    n_before = count(con2, "SELECT COUNT(*) FROM component")
    con2.execute("DELETE FROM category WHERE id=?", (child,))
    con2.commit()
    check("删品类不会删元件", count(con2, "SELECT COUNT(*) FROM component"), n_before)

    OUT.append("")
    OUT.append("=" * 70)
    OUT.append(f"结果:失败 {len(FAILS)} 项" + ("  " + " / ".join(FAILS) if FAILS else ""))
    txt = "\n".join(OUT)
    print(txt)
    path = os.path.join(ROOT, "build", "cache", "probe_cat.txt")
    io.open(path, "w", encoding="utf-8").write(txt)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
