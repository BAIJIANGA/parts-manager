"""列出当前注册的路由,用来核对新加的接口有没有挂上。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "..", "app"))
import server  # noqa: E402

want = [
    r"^/api/components$",
    r"^/api/components/(\d+)$",
    r"^/api/components/resolve$",
    # 第二步加的:按「值 + 封装」找相似,给人工确认用
    r"^/api/components/similar$",
    r"^/api/components/duplicates$",
    r"^/api/components/merge$",
    r"^/api/components/merged$",
    r"^/api/locations$",
    r"^/api/locations/(\d+)/stocktake$",
    r"^/api/movements$",
    r"^/api/movements/last$",
    r"^/api/movements/(\d+)/void$",
    r"^/api/projects$",
    r"^/api/projects/(\d+)/bom$",
    # 出库分配方案:每条 BOM 需求 + 库存里能凑它的元件
    r"^/api/projects/(\d+)/pick_plan$",
    # 一键批量开单(入库勾选清单走它)
    r"^/api/stock/batch$",
    # 导入得先预览(复核品类要用预览里的行号和把握),再真正落库
    r"^/api/bom/preview$",
    r"^/api/bom/import$",
]
have = {}
for method, pat, fn in server.ROUTES:
    have.setdefault(pat.pattern, []).append(method + " " + fn.__name__)

bad = 0
for w in want:
    got = have.get(w)
    if got:
        print("OK   ", w, "->", ", ".join(got))
    else:
        print("!! 缺", w)
        bad += 1
print("routes=%d missing=%d" % (len(server.ROUTES), bad))
sys.exit(1 if bad else 0)
