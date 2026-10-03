"""在用户真实数据库的副本上试一次升级,确认数据一条不少。

包发出去之后 init_db 会在用户手上那个库上跑 ALTER TABLE —— 这一步出错就没有
回头路,所以宁可先在副本上验证一遍。
"""
import os
import shutil
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "app"))
import db  # noqa: E402

LIVE = os.path.join(ROOT, "dist", "元器件物料管理", "data", "parts.db")
COPY = os.path.join(HERE, "migrate_probe.db")

if not os.path.exists(LIVE):
    print("找不到成品包里的数据库:", LIVE)
    sys.exit(1)

shutil.copy2(LIVE, COPY)
before = sqlite3.connect(COPY)
before.row_factory = sqlite3.Row
n_comp = before.execute("SELECT COUNT(*) FROM component").fetchone()[0]
n_loc = before.execute("SELECT COUNT(*) FROM location").fetchone()[0]
n_mv = before.execute("SELECT COUNT(*) FROM movement").fetchone()[0]
n_stock = before.execute("SELECT COALESCE(SUM(qty),0) FROM stock").fetchone()[0]
n_proj = before.execute("SELECT COUNT(*) FROM project").fetchone()[0]
v_before = before.execute("SELECT value FROM component ORDER BY id").fetchall()
v_before = [r[0] for r in v_before]
before.close()
print(f"升级前: 元件 {n_comp}  仓位 {n_loc}  流水 {n_mv}  总库存 {n_stock}  项目 {n_proj}")

con = db.connect(COPY)
added = db.init_db(con)
print("本次补的列:", added or "(无)")
con.commit()

cols = {r[1] for r in con.execute("PRAGMA table_info(component)")}
mcols = {r[1] for r in con.execute("PRAGMA table_info(movement)")}
problems = []
for c in ("merged_into", "marking", "value_num", "value_unit", "default_loc_id"):
    if c not in cols:
        problems.append("component." + c)
for c in ("voided", "void_of", "qty_before"):
    if c not in mcols:
        problems.append("movement." + c)

after = {
    "元件": con.execute("SELECT COUNT(*) FROM component").fetchone()[0],
    "仓位": con.execute("SELECT COUNT(*) FROM location").fetchone()[0],
    "流水": con.execute("SELECT COUNT(*) FROM movement").fetchone()[0],
    "总库存": con.execute("SELECT COALESCE(SUM(qty),0) FROM stock").fetchone()[0],
    "项目": con.execute("SELECT COUNT(*) FROM project").fetchone()[0],
}
v_after = [r[0] for r in con.execute("SELECT value FROM component ORDER BY id")]
print("升级后:", "  ".join(f"{k} {v}" for k, v in after.items()))

if n_comp != after["元件"] or n_loc != after["仓位"] or n_mv != after["流水"]:
    problems.append("行数变了")
if n_stock != after["总库存"]:
    problems.append("总库存变了")
if v_before != v_after:
    problems.append("元件的值被改动了")
if con.execute("SELECT COUNT(*) FROM component WHERE merged_into IS NOT NULL"
               ).fetchone()[0]:
    problems.append("有元件被误标成已合并")
if con.execute("SELECT COUNT(*) FROM stock WHERE qty < 0").fetchone()[0]:
    problems.append("出现负库存")

# 新功能在旧库上真的能用起来
try:
    con.execute("UPDATE movement SET voided=0 WHERE id=(SELECT MIN(id) FROM movement)")
    con.execute("SELECT COUNT(*) FROM component WHERE merged_into IS NULL")
    con.execute("SELECT value_num FROM component WHERE value_num IS NOT NULL LIMIT 1")
except sqlite3.Error as exc:
    problems.append(f"旧库上跑新功能的 SQL 报错: {exc}")

con.close()
os.remove(COPY)

if problems:
    print("迁移体检:FAIL")
    for p in problems:
        print("  ·", p)
    sys.exit(1)
print("迁移体检:PASS —— 旧库升级后数据一条不少,新列都在")
