# -*- coding: utf-8 -*-
"""#6 品类表迁移探针。

只用**副本**:真实库 data/parts.db 一个字节都不动 —— 用户就在用那个库。
验六件事:
  1. 全新库:品类表先按内置清单建好,不用用户自己敲
  2. 老库升上来:库里在用的品类都变成了行,每个元件都挂上了 category_id
  3. 幂等:init_db 连跑三次,行数和挂靠数都不变
  4. 对账:元件文本改了 -> 跟着改挂;子类节点不会被压平到顶层
  5. 播种时机(#32):只有「这次才建出 category 表」的新库才播种,
     已经存在的库(用户的库)缺哪个标准品类都不会被补回来
  6. 删大类(#32):顶层删掉不再造「未分类」,元件变成「没有品类」;
     子类删掉仍旧挪到上一级;删完再启动/再对账都不许长回来
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
import server   # noqa: E402  ← 第 6 项要直接调 delete_category(和桌面版同一条路)

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


class _Match:
    """路由派发时递给 handler 的那个 match 对象,这里自己捏一个。"""

    def __init__(self, *groups):
        self._groups = groups

    def group(self, i):
        return self._groups[i - 1]


def del_category(con, cat_id):
    """走 server.delete_category —— 桌面版按钮调的就是这个函数对象(不经路由表),
    所以这里能验到的东西和用户点「删除」是同一套逻辑。"""
    ctx = server.Ctx(None, con, {}, {}, None)
    return server.delete_category(ctx, _Match(str(cat_id)))


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
    check("schema_version 提到 5",
          con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0],
          # 别写死数字:每加一列都要回来改一次这种断言,迟早忘
          str(db.SCHEMA_VERSION))

    OUT.append("")
    OUT.append("【2】老库升上来:文本品类变成真正的行")
    src = os.path.join(ROOT, "data", "parts.db")
    # 「老库」的定义是**还没有 component.category_id 这一列**的库。仓库里那个开发用
    # 的 data/parts.db 跑过一遍就被升级掉了,拿它当老库,这条断言就会随库的状态飘
    # (实测:升级过之后这里就永远 False)。所以先看它到底算不算老库,不算就现造一个,
    # 结果才是确定的。只读打开,绝不碰它。
    legacy_ok = False
    if os.path.exists(src):
        try:
            # db.connect 只连接、不做迁移(迁移在 init_db 里),所以拿它探一下列是安全的
            _c = db.connect(src)
            _cols = {r[1] for r in _c.execute("PRAGMA table_info(component)")}
            _c.close()
            legacy_ok = "category_id" not in _cols
        except Exception:
            legacy_ok = False
    if not legacy_ok:
        OUT.append("  (手上没有「还没补过列」的老库,用现造的老库来验这一步)")
        p2 = os.path.join(tmp, "legacy.db")
        con2 = db.connect(p2)
        con2.executescript("""
            CREATE TABLE IF NOT EXISTS component (
              id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
              category TEXT NOT NULL DEFAULT '其他', value TEXT, package TEXT,
              params TEXT NOT NULL DEFAULT '{}', unit TEXT NOT NULL DEFAULT '个',
              min_stock INTEGER NOT NULL DEFAULT 0, unit_price REAL NOT NULL DEFAULT 0,
              -- v1 就有的几列,init_db 的补列清单里没有它们,缺了建索引就炸
              mpn TEXT, lcsc_pn TEXT, manufacturer TEXT,
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
    OUT.append("【5】播种时机(issue #32):只有「库刚建出来」那一次才播种")
    # 用户的原话:「品类属于用户的事情,我需要是绝对的自定义自由化」。
    # 所以标准名单只在**建库那一刻**是初始值;库一旦存在,缺哪个品类都不许自动补。
    p5 = os.path.join(tmp, "seedtiming.db")
    con5 = db.connect(p5)
    db.init_db(con5)                      # 第一次启动 == 这个库刚被建出来
    check("全新库:标准名单建好了",
          count(con5, "SELECT COUNT(*) FROM category WHERE parent_id IS NULL"),
          len(bom.CATEGORIES))
    check("全新库里没有「未分类」这种系统自造的品类行(它只在界面显示时当兜底词)",
          count(con5, "SELECT COUNT(*) FROM category WHERE name='未分类'"), 0)
    # 用户把它删了、又录了一颗自己敲的品类 —— 这就是用户手上那个库的样子
    con5.execute("DELETE FROM category WHERE parent_id IS NULL AND name='电感'")
    con5.execute("INSERT INTO component(name, category, value, package) "
                 "VALUES('探针野料', '用户自己敲的', '1k', '0603')")
    con5.commit()
    con5.close()
    con5 = db.connect(p5)
    db.init_db(con5)                      # 模拟用户下次双击 exe 的那次启动
    check("老库启动:被删掉的「电感」不会被建回来",
          count(con5, "SELECT COUNT(*) FROM category WHERE parent_id IS NULL "
                      "AND name='电感'"), 0)
    check("老库启动:顶层 = 剩下的标准品类 + 用户自己敲的那个(缺的一律不补)",
          [r["name"] for r in con5.execute(
              "SELECT name FROM category WHERE parent_id IS NULL ORDER BY name")],
          sorted([c for c in bom.CATEGORIES if c != "电感"] + ["用户自己敲的"]))
    check("老库启动:维护逻辑没被一起删掉 —— 没挂 category_id 的元件照样挂回文本对应的行",
          con5.execute("SELECT category_id IS NOT NULL FROM component "
                       "WHERE name='探针野料'").fetchone()[0], 1)
    # 「没有品类」的形态 = category_id NULL + 文本空(删顶层之后就是它)。
    # 对账必须原样放过:一硬塞,删掉的大类就又活过来了。
    con5.execute("UPDATE component SET category_id=NULL, category='' WHERE name='探针野料'")
    con5.commit()
    db.reconcile_categories(con5)
    check("「没有品类」(NULL + 空文本)的元件不会被对账硬塞一个品类",
          tuple(con5.execute("SELECT category_id, category FROM component "
                             "WHERE name='探针野料'").fetchone()), (None, ""))
    check("对账也不会顺手把「未分类」造成一行",
          count(con5, "SELECT COUNT(*) FROM category WHERE name='未分类'"), 0)

    OUT.append("")
    OUT.append("【6】删品类(issue #32):顶层不造「未分类」,子类仍旧挪到上一级")
    p6 = os.path.join(tmp, "deletecat.db")
    con6 = db.connect(p6)
    db.init_db(con6)
    # ---- 顶层:用户自己敲的一个大类,里面有一颗料
    top6 = db.ensure_category(con6, "探针大类")
    con6.commit()
    con6.execute("INSERT INTO component(name, category, category_id, value, package) "
                 "VALUES('探针顶层料', '探针大类', ?, '1k', '0603')", (top6,))
    con6.commit()
    n_before6 = count(con6, "SELECT COUNT(*) FROM component")
    st6, rep6 = del_category(con6, top6)
    check("删顶层:返回 200", st6, 200)
    check("删顶层:没有上一级,to 是空串(界面靠它显示「这些料暂时没有品类」)",
          rep6["to"], "")
    check("删顶层:返回体字段名一个没改(界面按名字读)",
          sorted(rep6), ["deleted_nodes", "moved_children", "moved_components", "ok", "to"])
    check("删顶层:报告挪走 1 个元件", rep6["moved_components"], 1)
    check("删顶层:那个节点真的没了",
          count(con6, "SELECT COUNT(*) FROM category WHERE id=?", top6), 0)
    check("删顶层:元件一颗没少", count(con6, "SELECT COUNT(*) FROM component"), n_before6)
    check("删顶层:元件变成「没有品类」—— category_id NULL + 文本空串",
          tuple(con6.execute("SELECT category_id, category FROM component "
                             "WHERE name='探针顶层料'").fetchone()), (None, ""))
    check("删顶层:没有偷偷长出「未分类」这一行(它是「删了又回来」的元凶)",
          count(con6, "SELECT COUNT(*) FROM category WHERE name='未分类'"), 0)
    db.reconcile_categories(con6)
    db.init_db(con6)                      # 再走一遍启动流程
    check("删完之后再对账/再启动,那个大类不会回来",
          count(con6, "SELECT COUNT(*) FROM category WHERE name='探针大类'"), 0)
    check("「未分类」也没被建出来",
          count(con6, "SELECT COUNT(*) FROM category WHERE name='未分类'"), 0)
    # ---- 子级:这条以前就是对的,改 #32 时不许弄坏
    p_top = db.ensure_category(con6, "探针父类")
    p_kid = db.ensure_category(con6, "探针子类", parent_id=p_top)
    con6.commit()
    con6.execute("INSERT INTO component(name, category, category_id, value, package) "
                 "VALUES('探针子级料', '探针父类', ?, '2k', '0603')", (p_kid,))
    con6.commit()
    st6b, rep6b = del_category(con6, p_kid)
    check("删子级:元件挪到上一级(不是被扔掉)", rep6b["to"], "探针父类")
    check("删子级:元件挂在父节点上",
          con6.execute("SELECT category_id FROM component "
                       "WHERE name='探针子级料'").fetchone()[0], p_top)
    check("删子级:父节点还在,只少了这一个节点",
          (count(con6, "SELECT COUNT(*) FROM category WHERE id=?", p_top),
           count(con6, "SELECT COUNT(*) FROM category WHERE id=?", p_kid)), (1, 0))
    con6.close()

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
