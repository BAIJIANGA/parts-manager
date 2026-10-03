"""列出当前注册的路由,用来核对新加的接口有没有挂上。

清单必须覆盖到「删掉它这个程序就不能用了」的每一个入口。这里踩过一次坑:
清单里原本连 /api/stock/move(出入库的主入口)都没有,把它整条删掉,
这个脚本照样打印 missing=0 —— 清单有个洞就等于没查。
所以每条都连方法一起写清楚,方法不对也算缺。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "app"))
import server  # noqa: E402

# (方法集合, 路由正则)。方法集合里有一个对上就算这一条在。
want = [
    # ---- 元件:主数据、查重、合并、按值+封装找相似
    ({"GET", "POST"}, r"^/api/components$"),
    ({"DELETE", "GET", "PUT"}, r"^/api/components/(\d+)$"),
    ({"GET"}, r"^/api/components/resolve$"),
    ({"GET"}, r"^/api/components/similar$"),
    ({"GET"}, r"^/api/components/duplicates$"),
    ({"POST"}, r"^/api/components/merge$"),
    ({"GET"}, r"^/api/components/merged$"),
    # ---- 品类树:能加能改能删,删的时候绝不删元件(只把元件挪到上一级)
    ({"GET", "POST"}, r"^/api/categories$"),
    ({"DELETE", "PUT"}, r"^/api/categories/(\d+)$"),
    # ---- 选项与总览
    ({"GET"}, r"^/api/meta$"),
    ({"GET"}, r"^/api/summary$"),
    ({"GET"}, r"^/api/dashboard$"),
    ({"GET"}, r"^/api/lowstock$"),
    # ---- 仓位
    ({"GET", "POST"}, r"^/api/locations$"),
    ({"DELETE", "PUT"}, r"^/api/locations/(\d+)$"),
    ({"GET"}, r"^/api/locations/(\d+)/contents$"),
    ({"POST"}, r"^/api/locations/(\d+)/stocktake$"),
    # ---- 库存动作:出入库、盘点、移库都走 move;批量开单走 batch
    ({"POST"}, r"^/api/stock/move$"),
    ({"POST"}, r"^/api/stock/batch$"),
    ({"POST"}, r"^/api/rebuild$"),
    # ---- 流水
    ({"GET"}, r"^/api/movements$"),
    ({"GET"}, r"^/api/movements/last$"),
    ({"POST"}, r"^/api/movements/(\d+)/void$"),
    # ---- 项目与 BOM
    ({"GET", "POST"}, r"^/api/projects$"),
    ({"DELETE", "GET", "PUT"}, r"^/api/projects/(\d+)$"),
    ({"GET", "POST"}, r"^/api/projects/(\d+)/bom$"),
    ({"POST"}, r"^/api/projects/(\d+)/pick$"),
    ({"GET"}, r"^/api/projects/(\d+)/pick_plan$"),
    ({"DELETE", "PUT"}, r"^/api/bom/(\d+)$"),
    ({"GET", "POST"}, r"^/api/bom/(\d+)/substitutes$"),
    ({"DELETE"}, r"^/api/substitutes/(\d+)$"),
    ({"POST"}, r"^/api/bom/preview$"),
    ({"POST"}, r"^/api/bom/import$"),
    # ---- 采购
    ({"GET", "POST"}, r"^/api/purchase$"),
    ({"DELETE", "PUT"}, r"^/api/purchase/(\d+)$"),
    ({"POST"}, r"^/api/purchase/(\d+)/receive$"),
    ({"GET"}, r"^/api/shopping$"),
    # ---- 存活探测(启动时打一下,确认后端在)
    ({"GET"}, r"^/api/ping$"),
    ({"GET"}, r"^/api/health$"),
]

have = {}
for method, pat, fn in server.ROUTES:
    have.setdefault(pat.pattern, set()).add(method)

bad = 0
for methods, pat in want:
    got = have.get(pat)
    if not got:
        print("!! 缺", pat)
        bad += 1
    elif not (got & methods):
        print("!! 方法不对", pat, "期望", sorted(methods), "实际", sorted(got))
        bad += 1
    else:
        print("OK   ", ",".join(sorted(got)).ljust(12), pat,
              "->", ",".join(sorted(got & methods)))

# 反向:注册了却没人核对的接口也报出来,提醒往清单里补 ——
# 不然新加的接口永远是「没人看着」的状态
known = {p for _m, p in want}
extra = sorted(set(have) - known)
if extra:
    print("-- 清单里没有(刚加的接口记得补进来):")
    for p in extra:
        print("     ", ",".join(sorted(have[p])), p)
print("routes=%d missing=%d" % (len(server.ROUTES), bad))
sys.exit(1 if bad else 0)
