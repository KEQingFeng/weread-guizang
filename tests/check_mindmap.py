# -*- coding: utf-8 -*-
"""mindmap 模块的离线自测：一行一条结论，失败就非零退出。

为什么单独一份：这一整套（清洗、上限、环检测、四种形态的坐标、SVG 转义）没有界面可点，
而界面上「看着对」也验不出「radial 其实只是 tree 换个名字」这种事故 —— 那只有拿同一棵
树在四种形态下做坐标差分才抓得住，所以差分断言写死在这里。

纯逻辑套件：不起服务、不开浏览器、不联网、不截图（selftest.SHOTS 用不上）。
读写全部落在系统临时目录里的一个沙盒，atexit 扫干净，用户真实书库一个字节都不碰。

跑法：.venv/bin/python tests/check_mindmap.py
"""
import atexit
import ast
import copy
import itertools
import json
import math
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import selftest  # noqa: E402  仓库根从 tests/selftest.py 推导，不在源码里写死本机路径

assert str(selftest.REPO) == str(REPO), "selftest.REPO 与本套件推导的仓库根不一致"

SANDBOX = tempfile.mkdtemp(prefix="gz-mindmap-check-")
atexit.register(lambda: shutil.rmtree(SANDBOX, ignore_errors=True))
BOOK = os.path.join(SANDBOX, "book-001")
os.makedirs(BOOK, exist_ok=True)

import mindmap as mm  # noqa: E402

FAIL = []
CHECKS = []


def chk(name, cond, extra=""):
    CHECKS.append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))
    if not cond:
        FAIL.append(name)


def has_cjk(text):
    return any("\u4e00" <= c <= "\u9fff" for c in str(text))


# ── 契约字面量（界面按这份 JSON 直接渲染，字段名漂一个功能就废）──
DOC_KEYS = {"schema", "title", "form", "nodes", "links", "created", "updated"}
NODE_KEYS = {"id", "text", "color", "note", "fold", "x", "y", "children"}
LINK_KEYS = {"id", "from", "to", "text"}
LAY_KEYS = {"form", "width", "height", "boxes", "edges", "counts"}
BOX_KEYS = {"id", "x", "y", "w", "h", "text", "lines", "color", "ink",
            "depth", "parent", "hidden"}
EDGE_KEYS = {"from", "to", "path", "color", "label", "lx", "ly"}
COUNT_KEYS = {"nodes", "links", "visible", "roots"}
SAVE_KEYS = {"ok", "path", "bytes", "nodes", "links", "dropped"}
# 这两处是明知故加的字段：dropped 是任务书要求的报数口，kind/error 是为了让「连线画细虚线」
# 与「写失败说什么」这两个事实能随 JSON 传出去。除它们以外不许有多余字段。
DOC_EXTRA = {"dropped"}
SAVE_EXTRA = {"error"}
EDGE_EXTRA = {"kind"}

PUBLIC = ("empty", "normalize", "node_count", "path_of", "load_map", "save_map",
          "layout", "render_svg", "to_markdown", "from_note_tree", "find", "walk")


# ── 夹具 ────────────────────────────────────────────────────────────
def node(text, kids=None, **kw):
    """一个节点。形状与前端传上来的保持一致（children，不是 kids）。"""
    raw = {"text": text, "children": list(kids or [])}
    raw.update(kw)
    return raw


def sample(title="跨形态", branches=6, leaves=2):
    """一棵够宽的树：一级分支 6 条（正好触发辐射图的上下各一半），每条挂两片叶子。"""
    d = mm.empty(title)
    d["nodes"] = [node("中心主题", [
        node("分支%d" % i, [node("叶子%d-%d" % (i, j), id="b%dL%d" % (i, j))
                            for j in range(leaves)], id="b%d" % i)
        for i in range(branches)], id="root")]
    # 叶子必须自己带号：normalize 只给缺号的补 uuid，后面那些「b0 的孩子比 b0 离中心近」
    # 「l2 连的是 b1L0 → b5L1」全靠这个编号说话，随机 uuid 就把断言变成掷骰子。
    return mm.normalize(d)


def center(box):
    return box["x"] + box["w"] / 2.0, box["y"] + box["h"] / 2.0


def centers(lay):
    return {b["id"]: center(b) for b in lay["boxes"]}


def boxes_by_id(lay):
    return {b["id"]: b for b in lay["boxes"]}


def moved(lay_a, lay_b, thresh=40.0):
    """同一批节点在两张图里的中心位移：返回（动得明显的数量, 共有数量）。"""
    ca, cb = centers(lay_a), centers(lay_b)
    common = set(ca) & set(cb)
    hit = sum(1 for k in common
              if abs(ca[k][0] - cb[k][0]) > thresh or abs(ca[k][1] - cb[k][1]) > thresh)
    return hit, len(common)


def head(lay):
    return [b for b in lay["boxes"] if b["depth"] == 0][0]


def ring(lay, depth=1):
    return [b for b in lay["boxes"] if b["depth"] == depth]


def layout_problems(lay, doc):
    """一张 layout 的通用不变量，逐个给 (判据, 通过?, 证据)。四种形态共用。"""
    boxes = lay["boxes"]
    ids = {b["id"] for b in boxes}
    yield ("有盒子", bool(boxes), len(boxes))
    yield ("boxes 字段一字不差", all(set(b) == BOX_KEYS for b in boxes),
           sorted({k for b in boxes for k in b} - BOX_KEYS))
    yield ("x/y/w/h 都是有限正数",
           all(math.isfinite(b[k]) and b[k] > 0 for b in boxes
               for k in ("x", "y", "w", "h")),
           [(b["id"], b["x"], b["y"]) for b in boxes
            if not (b["x"] > 0 and b["y"] > 0 and b["w"] > 0 and b["h"] > 0
                    and math.isfinite(b["x"]) and math.isfinite(b["y"]))][:3])
    yield ("盒子 id 全图唯一", len(boxes) == len(ids), [b["id"] for b in boxes])
    right = max(b["x"] + b["w"] for b in boxes)
    bottom = max(b["y"] + b["h"] for b in boxes)
    yield ("画布严格大于盒子右缘", lay["width"] > right, (lay["width"], right))
    yield ("画布严格大于盒子下缘", lay["height"] > bottom, (lay["height"], bottom))
    yield ("根节点 parent 是 None，其余指向存在的框",
           sum(1 for b in boxes if b["parent"] is None) == 1
           and all(b["parent"] in ids for b in boxes if b["parent"] is not None),
           [(b["id"], b["parent"]) for b in boxes][:4])
    yield ("连线两端都在盒子里",
           all(e["from"] in ids and e["to"] in ids for e in lay["edges"]),
           [(e["from"], e["to"]) for e in lay["edges"]
            if e["from"] not in ids or e["to"] not in ids][:3])
    yield ("边有 SVG 路径且标签点有限",
           all(e["path"].startswith("M") and math.isfinite(e["lx"])
               and math.isfinite(e["ly"]) for e in lay["edges"]),
           [e["path"] for e in lay["edges"] if not e["path"].startswith("M")][:2])
    yield ("底色与字色都是 #rrggbb",
           all(len(str(b["color"])) == 7 and b["color"].startswith("#")
               and len(str(b["ink"])) == 7 and b["ink"].startswith("#") for b in boxes),
           [(b["id"], b["color"], b["ink"]) for b in boxes if len(str(b["color"])) != 7][:3])
    yield ("文字与折行都有内容",
           all(isinstance(b["text"], str) and b["lines"]
               and all(isinstance(x, str) and x for x in b["lines"]) for b in boxes),
           [b["id"] for b in boxes if not b["lines"]][:3])
    yield ("counts 自洽（可见 + 折叠掉的 = 全部）",
           lay["counts"]["visible"] == len(boxes)
           and lay["counts"]["nodes"] == len(boxes) + sum(b["hidden"] for b in boxes),
           lay["counts"])
    yield ("counts 与文档对得上",
           lay["counts"]["nodes"] == mm.node_count(doc)
           and lay["counts"]["roots"] == len(doc["nodes"]),
           (lay["counts"], mm.node_count(doc), len(doc["nodes"])))


# ── 1. 契约面：常量、导出名、只用标准库、零 emoji ───────────────────
chk("契约：FORMS 四个键与中文名一字不差",
    mm.FORMS == (("tree", "括号图"), ("radial", "辐射图"),
                 ("fishbone", "鱼骨图"), ("concept", "概念图")), mm.FORMS)
chk("契约：MIND_FILE / SCHEMA / 六个上限常量取值正确",
    (mm.MIND_FILE, mm.SCHEMA, mm.MAX_NODES, mm.MAX_DEPTH, mm.MAX_LINKS,
     mm.MAX_TEXT, mm.MAX_NOTE, mm.MAX_TITLE)
    == ("mindmap.json", 1, 400, 8, 200, 200, 4000, 120),
    (mm.MIND_FILE, mm.SCHEMA, mm.MAX_NODES, mm.MAX_DEPTH, mm.MAX_LINKS,
     mm.MAX_TEXT, mm.MAX_NOTE, mm.MAX_TITLE))
chk("契约：该导出的十二个函数都在",
    [n for n in PUBLIC if not callable(getattr(mm, n, None))] == [],
    [n for n in PUBLIC if not callable(getattr(mm, n, None))])

SRC = (REPO / "mindmap.py").read_text(encoding="utf-8")
TEST_SRC = Path(__file__).resolve().read_text(encoding="utf-8")
IMPORTS = set()
for _nd in ast.walk(ast.parse(SRC)):
    if isinstance(_nd, ast.Import):
        IMPORTS.update(a.name.split(".")[0] for a in _nd.names)
    elif isinstance(_nd, ast.ImportFrom) and _nd.module:
        IMPORTS.add(_nd.module.split(".")[0])
chk("依赖：mindmap.py 只用标准库（没新增 pip 依赖）",
    IMPORTS <= set(sys.stdlib_module_names) | {"__future__"},
    sorted(IMPORTS - set(sys.stdlib_module_names)))
chk("依赖：不 import book_notes / feed / ui_server（模块独立，路由层喂树进来）",
    not IMPORTS & {"book_notes", "feed", "ui_server", "video_note", "clip_article"},
    sorted(IMPORTS))


def emoji_hits(text):
    return sorted({c for c in text
                   if 0x1F000 <= ord(c) <= 0x1FAFF or 0x2600 <= ord(c) <= 0x27BF
                   or 0x2B00 <= ord(c) <= 0x2BFF or ord(c) == 0xFE0F})


chk("界面规矩：mindmap.py 零 emoji", not emoji_hits(SRC), emoji_hits(SRC))
chk("界面规矩：本套件零 emoji", not emoji_hits(TEST_SRC), emoji_hits(TEST_SRC))

chk("empty()：结构就是契约那七项加一个 dropped",
    set(mm.empty()) == DOC_KEYS | DOC_EXTRA and mm.empty()["nodes"] == []
    and mm.empty()["links"] == [] and mm.empty()["form"] == "tree"
    and mm.empty()["schema"] == mm.SCHEMA and mm.empty()["dropped"] == 0,
    sorted(mm.empty()))
chk("empty()：标题带进来并被裁到 120",
    mm.empty("题" * 200)["title"] == "题" * 120, len(mm.empty("题" * 200)["title"]))

d = sample()
chk("normalize()：节点与连线的字段是契约形状（不多不少）",
    all(set(n) == NODE_KEYS for n in mm.walk(d))
    and all(set(l) == LINK_KEYS for l in d["links"]),
    sorted({k for n in mm.walk(d) for k in n} - NODE_KEYS))
chk("normalize()：doc 的键不多不少（只有 dropped 是声明过的加法）",
    set(d) == DOC_KEYS | DOC_EXTRA, sorted(set(d) - DOC_KEYS))

dirty = {"title": "脏图" * 90, "form": "不存在的形态",
         "nodes": [node("甲", [node("乙", id="kid"), "字符串孩子"], id="k1"),
                   {"text": "没 id", "children": []}, {"id": "k1", "text": "撞号"}],
         "links": [{"id": "x", "from": "k1", "to": "k1", "text": "自环"},
                   {"from": "k1", "to": "不存在", "text": "半截"}]}
a = mm.normalize(dirty)
a2 = mm.normalize(a)
chk("normalize()：幂等（洗两次完全相等，布局与存档都靠这个前提）", a2 == a,
    [(k, a[k], a2[k]) for k in a if a[k] != a2[k]])
chk("normalize()：幂等对裁到上限的图也成立",
    (lambda big: mm.normalize(big) == big)(
        mm.normalize({"nodes": [node("长" * 5000,
                                     [node("子%d" % i) for i in range(420)])]})), None)
chk("normalize()：认不出的形态退回括号图而不是崩", a["form"] == "tree", a["form"])
chk("normalize()：坏输入不抛（None / 字符串 / 数字 / 数组都回可用空图）",
    all(mm.normalize(bad) == mm.normalize(None) for bad in (None, "abc", 7, [1, 2])), None)
chk("normalize()：nodes 是字符串时整段作废并报数",
    mm.normalize({"nodes": "abc"})["nodes"] == []
    and mm.normalize({"nodes": "abc"})["dropped"] >= 1,
    mm.normalize({"nodes": "abc"})["dropped"])
chk("normalize()：children 里塞的字符串被丢掉且报数",
    [n["text"] for n in a["nodes"][0]["children"]] == ["乙"],
    [n["text"] for n in a["nodes"][0]["children"]])
# 手改过的 JSON 里什么都有：Infinity 让 int() 抛 OverflowError、dict 让 `x in FORM_BY_KEY`
# 抛 TypeError（成员测试要求可哈希），两条都会把界面打成 500，所以这两份脏数据必须有断言盯着
chk("normalize()：dropped 是 Infinity / 字符串 / 负数时收成整数，不抛也不破幂等",
    all(mm.normalize({"dropped": junk, "nodes": []}) == mm.normalize(
        mm.normalize({"dropped": junk, "nodes": []}))
        for junk in (float("inf"), float("-inf"), float("nan"), "abc", "12", -5, True, [])),
    None)
chk("normalize()：dropped 是 1e18 这种大整数时不收成 float（收了就掉精度、幂等立破）",
    mm.normalize({"dropped": 10 ** 18, "nodes": []})["dropped"] == 10 ** 18
    and mm.normalize({"dropped": 10 ** 18 + 1, "nodes": []})["dropped"] == 10 ** 18 + 1,
    (mm.normalize({"dropped": 10 ** 18, "nodes": []})["dropped"],
     mm.normalize({"dropped": 10 ** 18 + 1, "nodes": []})["dropped"]))
chk("normalize()：form 是不可哈希的对象/数组时退回括号图，不抛",
    mm.normalize({"form": {"nodes": "abc"}, "nodes": []})["form"] == "tree"
    and mm.normalize({"form": ["radial"], "nodes": []})["form"] == "tree"
    and mm.normalize({"form": 12, "nodes": []})["form"] == "tree", None)
chk("layout()：form 参数传不可哈希的东西也不抛（照文档里的形态画）",
    mm.layout({"form": "radial", "nodes": [node("根", id="q0")]}, ["radial"])["form"] == "radial"
    and mm.layout({"form": "radial", "nodes": [node("根", id="q0")]}, {"a": 1})["form"] == "radial",
    None)
chk("normalize()：坐标是 Infinity / nan 时当没钉过（否则画布被顶到无穷大）",
    all(mm.normalize({"nodes": [dict(text="根", id="q1", x=x, y=y)]})["nodes"][0]["x"] is None
        for x, y in ((float("inf"), float("inf")), (float("nan"), 10.0), (10 ** 300, 5.0))),
    None)
chk("normalize()：自环与指着不存在节点的连线都丢掉并报数（半截线让人以为图坏了）",
    a["links"] == [] and a["dropped"] >= 2, (a["links"], a["dropped"]))
chk("normalize()：连着子孙（不是根）的线要留得下来，布局里也真有这条边",
    (lambda g: g["links"] and sum(
        1 for e in mm.layout(g)["edges"] if e["kind"] == "link") == 1)(
        mm.normalize({"nodes": [node("根", [node("孩", id="g1")], id="g0")],
                      "links": [{"id": "gl", "from": "g1", "to": "g0"}]})), None)
chk("normalize()：时间戳不在这里盖（盖了幂等就破了），原样保留",
    mm.normalize({"created": "2026-10-03T12:00:00", "nodes": []})["created"]
    == "2026-10-03T12:00:00" and mm.normalize(mm.empty())["created"] == "", None)

cyc = {"id": "cyc", "text": "甲", "children": []}
cyc["children"].append(cyc)                       # A 的孩子是 A 自己：前端拖拽造得出来
cd = mm.normalize({"nodes": [cyc]})
chk("normalize()：自引用环被砍断，只剩一个节点", mm.node_count(cd) == 1,
    mm.node_count(cd))
chk("normalize()：带环的图照样能布局、能出图",
    mm.layout(cd, "radial")["counts"]["visible"] == 1
    and mm.render_svg(cd, "fishbone").rstrip().endswith("</svg>"), None)
mutual = {"id": "m1", "text": "甲", "children": []}
other = {"id": "m2", "text": "乙", "children": [mutual]}
mutual["children"].append(other)
md = mm.normalize({"nodes": [mutual]})
chk("normalize()：互相指认的环也砍得断（甲的孩子是乙、乙的孩子是甲）",
    mm.node_count(md) == 2 and md["nodes"][0]["children"][0]["children"] == [],
    mm.node_count(md))
chk("normalize()：坏输入洗完能直接布局，不抛",
    mm.layout(a)["counts"]["visible"] == 4 and mm.layout(cd)["boxes"], None)
chk("layout()：脏图洗完再布局，每条边的两端都在 boxes 里（不会有半截线）",
    all(e["from"] in boxes_by_id(mm.layout(a, f)) and e["to"] in boxes_by_id(mm.layout(a, f))
        for f, _n in mm.FORMS for e in mm.layout(a, f)["edges"]),
    [(e["from"], e["to"]) for e in mm.layout(a)["edges"]])

# ── 2. id：缺的补、有的不动、撞号的补新的 ──────────────────────────
ids_a = [n["id"] for n in mm.walk(a)]
chk("id：已有 id 一个都不改（前端拿它定位，改了用户就点不中）",
    a["nodes"][0]["id"] == "k1" and a["nodes"][0]["children"][0]["id"] == "kid", ids_a)
chk("id：缺 id 的补上且全图唯一",
    len(ids_a) == len(set(ids_a)) == 4 and all(ids_a), ids_a)
chk("id：撞号的后来者拿到新 id（两个 k1 不会合成一个）",
    len({n["id"] for n in mm.walk(a)}) == 4 and sum(1 for i in ids_a if i == "k1") == 1,
    ids_a)
ID_DIR = os.path.join(SANDBOX, "book-id")
os.makedirs(ID_DIR, exist_ok=True)      # save_map 只往给定目录里写，不替调用方建目录
chk("id：补齐后的图存进去再读回来 id 不变（round-trip 不漂号）",
    mm.save_map(ID_DIR, a)["ok"]
    and [n["id"] for n in mm.walk(mm.load_map(ID_DIR))] == ids_a, ids_a)

# ── 3. 上限：裁得掉、报得出、裁完还是张能用的图 ─────────────────────
big = mm.normalize({"nodes": [node("根", [node("c%d" % i, id="c%d" % i)
                                         for i in range(401)], id="r")]})
chk("上限：401 个节点裁到 400 且 dropped 报数",
    mm.node_count(big) == mm.MAX_NODES and big["dropped"] >= 1,
    (mm.node_count(big), big["dropped"]))
big_lay = mm.layout(big)
chk("上限：裁完结构仍然完整（没有孤儿孩子、能整张布局）",
    all(isinstance(k, dict) and k["id"] for n in mm.walk(big) for k in n["children"])
    and big_lay["counts"]["visible"] == mm.MAX_NODES
    and all(e["from"] in boxes_by_id(big_lay) for e in big_lay["edges"]), None)
long = mm.normalize({"nodes": [node("长" * 5000, note="注" * 9000)]})
chk("上限：节点文字裁到 200、备注裁到 4000，两处都报数",
    len(long["nodes"][0]["text"]) == mm.MAX_TEXT
    and len(long["nodes"][0]["note"]) == mm.MAX_NOTE and long["dropped"] >= 2,
    (len(long["nodes"][0]["text"]), len(long["nodes"][0]["note"]), long["dropped"]))
chain = {"id": "d0", "text": "0", "children": []}
cur = chain
for lvl in range(1, 14):
    nxt = {"id": "d%d" % lvl, "text": str(lvl), "children": []}
    cur["children"] = [nxt]
    cur = nxt
deep = mm.normalize({"nodes": [chain]})
deep_boxes = mm.layout(deep)["boxes"]
chk("上限：超过 8 层的枝被裁掉（最深 depth=7）且报数",
    max(b["depth"] for b in deep_boxes) == mm.MAX_DEPTH - 1
    and mm.node_count(deep) == mm.MAX_DEPTH and deep["dropped"] >= 1,
    (max(b["depth"] for b in deep_boxes), mm.node_count(deep), deep["dropped"]))
wide = mm.normalize({"nodes": [node("根", [node("L%d" % i, id="L%d" % i)
                                          for i in range(202)], id="R")],
                     "links": [{"id": "lk%d" % i, "from": "L%d" % i, "to": "L%d" % (i + 1)}
                               for i in range(201)]})
chk("上限：link 超过 200 条被裁并报数，裁下来的那些不再出现在布局里",
    len(wide["links"]) == mm.MAX_LINKS and wide["dropped"] >= 1
    and sum(1 for e in mm.layout(wide)["edges"] if e["kind"] == "link") == mm.MAX_LINKS,
    (len(wide["links"]), wide["dropped"]))

# ── 4. 读写：只写书目录下的 mindmap.json，坏文件不抛 ────────────────
chk("path_of()：只拼路径不建文件，落点永远是给定目录下的 mindmap.json",
    mm.path_of(BOOK) == os.path.join(BOOK, mm.MIND_FILE) == os.path.join(BOOK, "mindmap.json")
    and os.path.dirname(mm.path_of(BOOK)) == BOOK
    and not os.path.exists(mm.path_of(BOOK)), mm.path_of(BOOK))
chk("load_map()：没这个文件时给可用的空图，不抛",
    mm.load_map(BOOK) == mm.normalize(mm.empty()) and mm.load_map(BOOK)["nodes"] == [],
    mm.load_map(BOOK))
for label, content in (("文件是截断的 JSON", '{"nodes": [{"id": "a", "te'),
                       ("文件内容是数组", "[1, 2, 3]"),
                       ("文件是空的", ""),
                       ("文件根本不是文本", "\x00\x01\x02")):
    p = mm.path_of(BOOK)
    with open(p, "w", encoding="utf-8", errors="surrogateescape") as f:
        f.write(content)
    chk("load_map()：%s 也回可用空图且不抛" % label,
        mm.load_map(BOOK) == mm.normalize(mm.empty()), None)
    os.remove(p)

# 只读自动导图的产物必须在手画导图落盘后原样留着（两个名字不许互相覆盖）
SVG_PATH = os.path.join(BOOK, "mindmap.svg")
SENTINEL = "<svg>只读自动图，谁都不许覆盖我</svg>"
with open(SVG_PATH, "w", encoding="utf-8") as f:
    f.write(SENTINEL)
LINKED = mm.normalize({"title": d["title"], "form": d["form"], "nodes": d["nodes"],
                       "links": [{"id": "l1", "from": "b0", "to": "b3", "text": "导致"},
                                 {"id": "l2", "from": "b1L0", "to": "b5L1", "text": "相关"}]})
res = mm.save_map(BOOK, LINKED)
chk("save_map()：返回契约那六项（外加声明过的 error）",
    set(res) == SAVE_KEYS | SAVE_EXTRA, sorted(set(res) - SAVE_KEYS))
chk("save_map()：ok 且真落盘，bytes 大于零、节点与连线数是实况",
    res["ok"] is True and res["bytes"] > 0 and res["nodes"] == mm.node_count(LINKED)
    and res["links"] == len(LINKED["links"]) and os.path.exists(res["path"]), res)
chk("save_map()：产物名是 mindmap.json，书目录里除它和原有 svg 没有别的新东西（写不出区）",
    os.path.basename(res["path"]) == mm.MIND_FILE
    and set(os.listdir(BOOK)) == {"mindmap.json", "mindmap.svg"}, sorted(os.listdir(BOOK)))
back = mm.load_map(BOOK)
chk("save_map() → load_map()：round-trip 内容等价（标题、形态、逐个节点）",
    back["title"] == LINKED["title"] and back["form"] == LINKED["form"]
    and [(n["id"], n["text"], n["color"], n["fold"]) for n in mm.walk(back)]
    == [(n["id"], n["text"], n["color"], n["fold"]) for n in mm.walk(LINKED)]
    and [l["from"] for l in back["links"]] == [l["from"] for l in LINKED["links"]], None)
res2 = mm.save_map(BOOK, back)
back2 = mm.load_map(BOOK)
chk("save_map()：created 只第一次写、updated 每次刷新，二次保存 created 不漂",
    back["created"] and back2["created"] == back["created"], (back["created"],))
chk("save_map()：覆盖前留了上一版（写坏了还能回退）",
    os.path.exists(os.path.join(BOOK, mm.MIND_FILE + ".prev")), sorted(os.listdir(BOOK)))
chk("save_map()：没覆盖只读那张 mindmap.svg",
    open(SVG_PATH, encoding="utf-8").read() == SENTINEL, None)
miss_dir = os.path.join(SANDBOX, "没有这个目录")
miss = mm.save_map(miss_dir, LINKED)
chk("save_map()：目录不存在时 ok=False、回人话、不抛裸异常，也不替调用方建目录",
    miss["ok"] is False and has_cjk(miss["error"]) and len(miss["error"]) <= 30
    and not os.path.exists(miss_dir) and "/" not in miss["error"].split("：", 1)[-1], miss)
BAD_DIR = os.path.join(SANDBOX, "book-bad")
os.makedirs(BAD_DIR, exist_ok=True)
chk("save_map()：坏文档（不是 dict）也存得下去，存进去的是清洗后的空图",
    mm.save_map(BAD_DIR, "不是图")["ok"]
    and mm.node_count(mm.load_map(BAD_DIR)) == 0
    and set(mm.load_map(BAD_DIR)) == DOC_KEYS | DOC_EXTRA, None)
BIG_DIR = os.path.join(SANDBOX, "book-bigcount")
os.makedirs(BIG_DIR, exist_ok=True)
BIGDOC = mm.normalize({"dropped": 10 ** 18 + 2, "nodes": [node("根", id="big0")]})
chk("save_map()→load_map()：dropped 这种大整数过一遍 JSON 不漂（漂了 round-trip 就不幂等）",
    mm.save_map(BIG_DIR, BIGDOC)["ok"]
    and mm.load_map(BIG_DIR)["dropped"] == 10 ** 18 + 2, mm.load_map(BIG_DIR)["dropped"])

# ── 5. 四种形态：通用不变量 ────────────────────────────────────────
LAYS = {f: mm.layout(LINKED, f) for f, _name in mm.FORMS}
chk("layout()：顶层就是契约那六项",
    all(set(LAYS[f]) == LAY_KEYS for f in LAYS), sorted(LAYS["tree"]))
chk("layout()：counts 四项不多不少",
    all(set(LAYS[f]["counts"]) == COUNT_KEYS for f in LAYS), sorted(LAYS["tree"]["counts"]))
chk("layout()：edges 字段是契约那七项，多出来的只有声明过的 kind",
    all(EDGE_KEYS <= set(e) and set(e) - EDGE_KEYS == EDGE_EXTRA
        for f in LAYS for e in LAYS[f]["edges"]),
    sorted({k for e in LAYS["tree"]["edges"] for k in e} - EDGE_KEYS))
chk("layout()：form 不传时用文档里的 form，传了以传的为准",
    mm.layout({**LINKED, "form": "radial"})["form"] == "radial"
    and mm.layout({**LINKED, "form": "radial"}, "fishbone")["form"] == "fishbone", None)
chk("layout()：认不出的 form 退回括号图（界面切坏了也不该画空）",
    mm.layout(LINKED, "不存在的形态")["form"] == "tree", None)
SNAP = copy.deepcopy(LINKED)
mm.layout(LINKED, "radial")
chk("layout()：不改动传进来的文档（前端可以拿同一份对象反复问）", LINKED == SNAP,
    [(k, LINKED[k], SNAP[k]) for k in LINKED if LINKED[k] != SNAP[k]][:1])
for form, name in mm.FORMS:
    for prop, ok, extra in layout_problems(LAYS[form], LINKED):
        chk("%s（%s）：%s" % (form, name, prop), ok, extra)
chk("layout()：坐标与尺寸最多一位小数（浮点尾巴不写进给前端的 JSON）",
    all(round(v, 1) == v for f in LAYS for b in LAYS[f]["boxes"]
        for v in (b["x"], b["y"], b["w"], b["h"]))
    and all(round(e[k], 1) == e[k] for f in LAYS for e in LAYS[f]["edges"] for k in ("lx", "ly"))
    and all(round(LAYS[f][k], 1) == LAYS[f][k] for f in LAYS for k in ("width", "height")),
    [(b["id"], b["x"], b["w"]) for f in LAYS for b in LAYS[f]["boxes"]
     if round(b["x"], 1) != b["x"]][:3])
empty_lay = mm.layout(mm.empty())
chk("layout()：空图也有正数画布和空 boxes（前端不至于塌成一条线）",
    empty_lay["boxes"] == [] and empty_lay["edges"] == []
    and empty_lay["width"] > 0 and empty_lay["height"] > 0
    and empty_lay["counts"]["nodes"] == 0, empty_lay)

# ── 6. 跨形态差分：四种形态必须是四套真算法，不是一块招牌 ──────────
t, r, fb = LAYS["tree"], LAYS["radial"], LAYS["fishbone"]
rt, rr, rf = center(head(t)), center(head(r)), center(head(fb))
chk("差分：括号图所有一级分支都在根右边，根贴左（左到右树的基本形状）",
    all(b["x"] > head(t)["x"] + head(t)["w"] for b in ring(t))
    and rt[0] / t["width"] < 0.30, (rt, t["width"]))
chk("差分：辐射图把根放在画布正中（横竖都在 0.4 到 0.6 之间）",
    0.40 < rr[0] / r["width"] < 0.60 and 0.40 < rr[1] / r["height"] < 0.60,
    (rr, r["width"], r["height"]))
chk("差分：辐射图一级分支左右都有（根的左边确实摆了框）",
    sum(1 for b in ring(r) if b["x"] + b["w"] < head(r)["x"]) >= 2,
    [(b["x"], center(b)) for b in ring(r)])
chk("差分：辐射图 ≥5 分支时上下各一半（六条分支就是 3 上 3 下）",
    sum(1 for b in ring(r) if center(b)[1] < rr[1]) == 3
    and sum(1 for b in ring(r) if center(b)[1] > rr[1]) == 3,
    [center(b) for b in ring(r)])
chk("差分：辐射图半径按层递增（同一条支上孙子比孩子离中心远）",
    (lambda kid, son: math.dist(kid, rr) < math.dist(son, rr))(
        center([b for b in r["boxes"] if b["id"] == "b0"][0]),
        center([b for b in r["boxes"] if b["id"] == "b0L0"][0])), None)
chk("差分：鱼骨图的鱼头在右端，一级分支全在脊的左侧",
    rf[0] / fb["width"] > 0.85
    and all(b["x"] + b["w"] < head(fb)["x"] for b in ring(fb)), (rf, fb["width"]))
sides = [1 if center(b)[1] > rf[1] else -1 for b in sorted(ring(fb), key=lambda b: b["x"])]
chk("差分：鱼骨图大骨上下交替（相邻两根不同侧，这是鱼骨可读性的命门）",
    len(sides) == 6 and all(sides[i] != sides[i + 1] for i in range(5)), sides)
chk("差分：三种自动形态两两位移明显（每对都有 ≥90% 的框挪了 40px 以上）",
    all(moved(x, y)[0] >= 0.9 * moved(x, y)[1]
        for x, y in itertools.combinations([t, r, fb], 2)),
    [(x["form"], y["form"], moved(x, y)) for x, y in itertools.combinations([t, r, fb], 2)])
chk("差分：鱼骨用直线段、辐射用曲线（画法也各自成套）",
    all("C" in e["path"] for e in r["edges"] if e["kind"] == "branch")
    and all("C" not in e["path"] for e in fb["edges"] if e["kind"] == "branch"), None)
chk("差分：画布尺寸随形态大变（同树三种形态三种长宽）",
    len({(round(x["width"]), round(x["height"])) for x in (t, r, fb)}) == 3,
    [(x["form"], round(x["width"]), round(x["height"])) for x in (t, r, fb)])

PINNED = mm.normalize({"title": "钉好的概念图", "form": "concept",
                       "nodes": [node("中心主题", [node("甲", [node("甲子", id="p3")],
                                                      id="p1"), node("乙", id="p2")],
                                      id="p0")]})
GRID = {"p0": (820.0, 410.0), "p1": (300.0, 120.0), "p2": (1500.0, 700.0),
        "p3": (620.0, 60.0)}
for _nd in mm.walk(PINNED):
    if _nd["id"] in GRID:
        _nd["x"], _nd["y"] = GRID[_nd["id"]]
pin_lay = mm.layout(PINNED)
pin_box = boxes_by_id(pin_lay)
chk("概念图：钉过的坐标原样进 boxes（左上角同一套坐标，不做中心偏移）",
    all((pin_box[k]["x"], pin_box[k]["y"]) == GRID[k] for k in GRID)
    and len(pin_box) == len(GRID), [(k, pin_box[k]["x"], pin_box[k]["y"]) for k in pin_box])
chk("概念图：钉位之后与括号图完全不同（四个框全都不在自动布局的位置上）",
    moved(pin_lay, mm.layout(PINNED, "tree")) == (4, 4),
    moved(pin_lay, mm.layout(PINNED, "tree")))
half = mm.normalize({"form": "concept", "nodes": [
    node("根", [node("钉住的", id="h1"), node("自动的", id="h2")], id="h0")],
    "links": [{"id": "hl", "from": "h1", "to": "h2", "text": "影响"}]})
for _nd in mm.walk(half):
    if _nd["id"] == "h1":
        _nd["x"], _nd["y"] = 640.0, 260.0
h_box = boxes_by_id(mm.layout(half))
chk("概念图：只钉一个时，钉住的用钉的值、没钉的仍走自动布局且落在画布内",
    (h_box["h1"]["x"], h_box["h1"]["y"]) == (640.0, 260.0)
    and 0 < h_box["h2"]["x"] < 640.0 and h_box["h2"]["y"] > 0,
    (h_box["h1"]["x"], h_box["h2"]["x"], h_box["h2"]["y"]))
chk("概念图：一个都没钉时退回括号图坐标（契约写明的落位方式，不是形态没生效）",
    centers(mm.layout(half, "concept")) != centers(mm.layout(half, "tree"))
    and centers(mm.layout(LINKED, "concept")) == centers(mm.layout(LINKED, "tree")), None)
odd = mm.layout({"form": "concept", "nodes": [
    node("根", [dict(text="只有 x", id="o1", x=900.0),
                dict(text="负坐标", id="o2", x=-50.0, y=-80.0)], id="o0")]})
odd_box = boxes_by_id(odd)
chk("概念图：半个坐标或负坐标不算钉过（当没拖过，走自动布局，框全在画布留白内）",
    odd_box["o1"]["x"] != 900.0 and odd_box["o2"]["x"] > 0
    and all(math.isfinite(b["x"]) for b in odd["boxes"]),
    [(k, odd_box[k]["x"]) for k in odd_box])

# ── 7. 折叠 ────────────────────────────────────────────────────────
FOLD = mm.normalize({"nodes": [node("根", [node("A", [node("A1", [node("A11")]),
                                        node("A2")], id="fa", fold=True),
                                        node("B", id="fb")], id="fr"),
                               node("独立", id="fs")],
                     "links": [{"id": "fl", "from": "fa", "to": "fb"}]})
FOLD_L = {f: mm.layout(FOLD, f) for f, _n in mm.FORMS}
chk("折叠：fold=True 的二级节点整棵子树都不进 boxes（只剩根、A、B、独立四个框）",
    all(set(boxes_by_id(FOLD_L[f])) == {"fr", "fa", "fb", "fs"} for f in FOLD_L),
    {f: sorted(boxes_by_id(FOLD_L[f])) for f in FOLD_L})
chk("折叠：hidden 给出被藏起来的子孙数（A 藏了 3 个），别的框是 0",
    boxes_by_id(FOLD_L["tree"])["fa"]["hidden"] == 3
    and all(b["hidden"] == 0 for b in FOLD_L["tree"]["boxes"] if b["id"] != "fa"), None)
chk("折叠：counts 自洽（可见 4 + 藏 3 = 全部 7），四种形态都成立",
    all(FOLD_L[f]["counts"]["visible"] == 4 and FOLD_L[f]["counts"]["nodes"] == 7
        and FOLD_L[f]["counts"]["roots"] == 2 for f in FOLD_L),
    {f: FOLD_L[f]["counts"] for f in FOLD_L})
UNFOLD = copy.deepcopy(FOLD)
for _nd in mm.walk(UNFOLD):
    if _nd["id"] == "fa":
        _nd["fold"] = False
chk("折叠：折叠的节点仍留在图上（折叠是收起，不是删除）",
    len(boxes_by_id(mm.layout(UNFOLD, "tree"))) == 7
    and len(boxes_by_id(FOLD_L["tree"])) == 4, None)
chk("折叠：藏起来的节点不会被连线端点用出去（边的两端永远在 boxes 里）",
    all(e["from"] in boxes_by_id(FOLD_L[f]) and e["to"] in boxes_by_id(FOLD_L[f])
        for f in FOLD_L for e in FOLD_L[f]["edges"]),
    {f: [(e["from"], e["to"]) for e in FOLD_L[f]["edges"]] for f in FOLD_L})
# 折叠不占位：藏起来的子树要等于「那支孩子本来就没有」时的样子（连画布都一样大）
TRIMMED = mm.normalize({"nodes": [node("根", [node("A", id="fa"), node("B", id="fb")], id="fr"),
                                  node("独立", id="fs")],
                        "links": [{"id": "fl", "from": "fa", "to": "fb"}]})
chks = {}
for _f in FOLD_L:
    _a, _b = centers(FOLD_L[_f]), centers(mm.layout(TRIMMED, _f))
    chks[_f] = (_a == _b
                and (round(FOLD_L[_f]["width"], 6), round(FOLD_L[_f]["height"], 6))
                == (round(mm.layout(TRIMMED, _f)["width"], 6),
                    round(mm.layout(TRIMMED, _f)["height"], 6)))
chk("折叠：藏起来的分支一个像素的位置都不占（四种形态都等于那支不存在时）",
    all(chks.values()), chks)

# ── 8. 连线：四种形态都画，非概念形态画细虚线 ──────────────────────
chk("连线：四种形态都把两条跨分支关系画出来了，且只画一次",
    all(sum(1 for e in LAYS[f]["edges"] if e["kind"] == "link") == 2 for f in LAYS),
    {f: [e["kind"] for e in LAYS[f]["edges"]] for f in LAYS})
chk("连线：连线带标签文字，树边不带",
    [e["label"] for e in LAYS["concept"]["edges"] if e["kind"] == "link"] == ["导致", "相关"]
    and all(e["label"] == "" for f in LAYS for e in LAYS[f]["edges"]
            if e["kind"] == "branch"), [e["label"] for e in LAYS["concept"]["edges"]])
link_edges = [e for e in LAYS["concept"]["edges"] if e["kind"] == "link"]
cb = boxes_by_id(LAYS["concept"])
chk("连线：标签中心落在两端盒子之间的范围内（前端直接把文字摆这儿不会飘出去）",
    all(min(cb[e["from"]]["x"], cb[e["to"]]["x"]) - 30 <= e["lx"]
        <= max(cb[e["from"]]["x"] + cb[e["from"]]["w"], cb[e["to"]]["x"] + cb[e["to"]]["w"]) + 30
        for e in link_edges), [(e["lx"], e["ly"]) for e in link_edges])
chk("连线：连线颜色与树边不同色（看得见跨分支关系又不抢主干）",
    all(e["color"] != next(x["color"] for x in LAYS["concept"]["edges"]
                           if x["kind"] == "branch") for e in link_edges), None)

# ── 9. render_svg ──────────────────────────────────────────────────
for form, name in mm.FORMS:
    svg = mm.render_svg(LINKED, form)
    lay = LAYS[form]
    chk("%s：SVG 自足（头尾标签、xmlns、viewBox、米色底、中文字体栈齐全）" % name,
        svg.startswith("<svg ") and svg.rstrip().endswith("</svg>")
        and 'xmlns="http://www.w3.org/2000/svg"' in svg
        and 'viewBox="0 0 %d %d"' % (math.ceil(lay["width"]), math.ceil(lay["height"])) in svg
        and "#fbf7ef" in svg
        and "PingFang SC, Hiragino Sans GB, Microsoft YaHei" in svg, svg[:90])
    chk("%s：SVG 里的宽高与 layout 画布一致（同一套坐标，前端不用二次换算）" % name,
        ('width="%d" height="%d"' % (math.ceil(lay["width"]), math.ceil(lay["height"]))) in svg,
        svg[:60])
    chk("%s：每个盒子一个圆角矩形，每条边一条 path" % name,
        svg.count("<rect") == len(lay["boxes"]) + 1
        and svg.count("<path") == len(lay["edges"]),
        (svg.count("<rect"), len(lay["boxes"]), svg.count("<path"), len(lay["edges"])))
XSS = mm.normalize({"title": "转义", "nodes": [node('<b>&"危险" & <i>', id="x1")]})
xsvg = mm.render_svg(XSS)
chk("render_svg：用户写的标签变成实体，SVG 里没有裸 <b>",
    "<b>" not in xsvg and "&lt;b&gt;" in xsvg and "&amp;" in xsvg and "危险" in xsvg,
    [ln for ln in xsvg.split("\n") if "<text" in ln][1:])
chk("render_svg：标题画在顶部", "跨形态" in mm.render_svg(LINKED), None)
chk("render_svg：非概念形态的连线是细虚线，概念形态的不是",
    'stroke-dasharray' in mm.render_svg(LINKED, "tree")
    and 'stroke-dasharray' not in mm.render_svg(LINKED, "concept"), None)
chk("render_svg：折叠的框边上补了圆点并写上藏起来的条数",
    ">3<" in mm.render_svg(FOLD, "tree"), [ln for ln in mm.render_svg(FOLD).split("\n")
                                           if "circle" in ln])
chk("render_svg：根节点深底浅色字、其余浅底深字（跟只读那张导图对齐）",
    'fill="#2f2a24"' in mm.render_svg(LINKED) and 'fill="#f7f2e7"' in mm.render_svg(LINKED),
    None)
chk("render_svg：空图也出得来一张完整能看的图",
    mm.render_svg(mm.empty()).startswith("<svg ")
    and mm.render_svg(mm.empty()).rstrip().endswith("</svg>"), None)
chk("render_svg：坏输入（None）也不抛",
    mm.render_svg(None).rstrip().endswith("</svg>"), None)

# ── 10. to_markdown ────────────────────────────────────────────────
MD = mm.to_markdown(LINKED)
chk("to_markdown：层级用缩进表达，越深缩进越多",
    MD.startswith("# 跨形态") and "\n- 中心主题\n" in MD
    and "\n  - 分支0\n" in MD and "\n    - 叶子0-0\n" in MD, MD.split("\n")[:6])
chk("to_markdown：note 紧跟在所属节点后面",
    (lambda s: "第一行备注" in s and "第二行备注" in s
     and s.index("第二行备注") > s.index("分支0")
     and s.index("第二行备注") < s.index("# ") + s.index("分支0") + 400)(
        mm.to_markdown(mm.normalize({"nodes": [node("根", [dict(
            text="分支0", id="m1", note="第一行备注\n第二行备注")], id="m0")]}))), None)
chk("to_markdown：连线出现在「关联」段，带两端名字与关系词",
    "## 关联" in MD and "分支0 → 分支3：导致" in MD
    and "叶子1-0 → 叶子5-1：相关" in MD, MD[MD.index("## 关联"):][:160])
chk("to_markdown：折叠的节点标出藏了几条",
    "（折叠 3 项）" in mm.to_markdown(FOLD), None)
chk("to_markdown：空图返回一个能看的骨架而不是空串",
    mm.to_markdown(mm.empty()).strip() and "## 关联" in mm.to_markdown(mm.empty())
    and "还没有连线" in mm.to_markdown(mm.empty()), repr(mm.to_markdown(mm.empty())))
chk("to_markdown：坏输入不抛",
    has_cjk(mm.to_markdown(None)) and mm.to_markdown({"nodes": "abc"}).startswith("# "), None)

# ── 11. from_note_tree（book_notes 那棵只读树迁进来）────────────────
NOTE_TREE = {"label": "这本书", "color": "#2f2a24", "kids": [
    {"label": "论点", "color": "#ffd45e",
     "kids": [{"label": "第一句划线", "color": "#ffd45e"}, {"label": "第二句划线"}]},
    {"label": "疑问", "color": "#ff9d8f", "kids": [{"label": "这里没懂"}]},
    {"label": "笔记条目", "kids": []}]}
nt = mm.from_note_tree(NOTE_TREE, "这本书")
chk("from_note_tree：三层树转完后节点数对得上（1+3+3）",
    mm.node_count(nt) == 7, mm.node_count(nt))
chk("from_note_tree：颜色带过来、form 是括号图、标题带上",
    [n["color"] for n in mm.walk(nt) if n["text"] == "论点"] == ["#ffd45e"]
    and nt["form"] == "tree" and nt["title"] == "这本书", None)
chk("from_note_tree：转完的图直接能布局（四种形态都出得来框）",
    all(mm.layout(nt, f)["boxes"] for f, _n in mm.FORMS), None)
chk("from_note_tree：没有笔记也不抛，得到一棵只含标题的树",
    mm.node_count(mm.from_note_tree(None, "还没读")) == 1
    and mm.from_note_tree(None, "还没读")["nodes"][0]["text"] == "还没读"
    and mm.node_count(mm.from_note_tree({})) == 1
    and mm.from_note_tree({})["nodes"][0]["text"] == "中心主题", None)
LOOP = {"label": "环", "kids": []}
LOOP["kids"].append(LOOP)
chk("from_note_tree：带环的笔记树不炸（限深自己挡住）",
    mm.node_count(mm.from_note_tree(LOOP, "环")) <= mm.MAX_DEPTH,
    mm.node_count(mm.from_note_tree(LOOP, "环")))
chk("from_note_tree：id 补齐且全图唯一",
    len({n["id"] for n in mm.walk(nt)}) == mm.node_count(nt), [n["id"] for n in mm.walk(nt)])
chk("from_note_tree：孩子顺序原样保留（导进来就是笔记里那个次序）",
    [n["text"] for n in mm.walk(nt)][:4] == ["这本书", "论点", "第一句划线", "第二句划线"],
    [n["text"] for n in mm.walk(nt)])

# ── 12. find / walk / node_count ───────────────────────────────────
chk("find()：命中给的就是那个节点，未命中与空 id 都给 None",
    mm.find(LINKED, "b2")["text"] == "分支2" and mm.find(LINKED, "不存在") is None
    and mm.find(LINKED, "") is None and mm.find(LINKED, None) is None, None)
chk("find()：能钻进子树里找到深层节点",
    mm.find(LINKED, "b5L1")["text"] == "叶子5-1", None)
chk("walk()：前序，森林里每棵树都走到",
    [n["text"] for n in mm.walk(FOLD)] == ["根", "A", "A1", "A11", "A2", "B", "独立"],
    [n["text"] for n in mm.walk(FOLD)])
chk("walk()：折叠的节点也照走（折叠只影响布局，不影响文档本身）",
    # 原来写的是 `"fa" in str(FOLD)` —— 那是句「文档里有 fa 这个字符串吗」，
    # walk() 一次没调，恒为真，等于把这条规格悄悄删了。真正的规格是：文档侧
    # 一个节点都不少（含被藏的那支），布局侧只画 4 个框 —— 差值就是折叠的作用域。
    len(list(mm.walk(FOLD))) == mm.node_count(FOLD) == 7
    and mm.find(FOLD, "fa")["fold"] is True
    and len(FOLD_L["tree"]["boxes"]) == 4,
    (len(list(mm.walk(FOLD))), mm.node_count(FOLD), len(FOLD_L["tree"]["boxes"])))
chk("node_count() 与 walk() 一致",
    mm.node_count(LINKED) == len(list(mm.walk(LINKED))) == 19, mm.node_count(LINKED))
chk("walk()：坏文档不抛（nodes 是字符串 / 节点是数字 / children 不是列表）",
    list(mm.walk({"nodes": "abc"})) == []
    and [n["text"] for n in mm.walk({"nodes": [7, {"text": "行", "children": "坏"}]})] == ["行"],
    None)
chk("walk()：坏文档里节点互指成环也不会永不结束",
    mm.node_count({"nodes": [cyc]}) == 1 and mm.node_count(None) == 0, None)
chk("walk()：只给一个轴或 None 的文档不会把 None 当节点吐出来",
    all(n is not None for n in mm.walk({"nodes": [None, {"text": "行"}]})), None)

# ── 13. 脏数据扫一遍：整条流水线不许抛、幂等、存进去读回来等价 ──────
# 上一轮变异测试之外另加的一遍：手改过的 mindmap.json 里 Infinity、不可哈希的 form、
# 一亿层的孩子都可能出现，而界面只需要一个结论 —— 洗坏了也要画得出来。
COUNT_JUNK = [float("inf"), float("-inf"), float("nan"), 1e18, 10 ** 18, 10 ** 18 + 2,
              -5, 0.5, True, False, None, "12", "abc", [], [1], {"a": 1}]
FORM_JUNK = [None, 0, "tree", "TREE", "radial", "nope", ["radial"], {"a": 1}, True, 3.5]
JUNK_NODES = [None, "abc", 7, [], {}, [{"id": "dup"}], float("nan"), float("inf"), 10 ** 18]
BAD_DOC = {"schema": 99, "title": "脏" * 400, "nodes": [node(
    "根", [node("孩子", JUNK_NODES, x=10 ** 9, y=-3, color="红色", note="注" * 9000,
                id="bad1")], id="bad0")],
    "links": [{"id": "bl", "from": "bad0", "to": "查无此节点", "text": "关系" * 200}]}


def strip_stamps(x):
    """去掉两个时间戳再比：save_map 每次都刷 updated，这是契约要的行为，
    不该混进「存进去读回来是不是同一张图」的判据里。"""
    return {k: v for k, v in x.items() if k not in ("created", "updated")}


def junk_rounds():
    """造一批形态各异的脏文档：脏值轮换着坐到 doc 的各个键上。"""
    for form in FORM_JUNK:
        for dropped in COUNT_JUNK:
            yield {"form": form, "dropped": dropped, "nodes": JUNK_NODES, "links": JUNK_NODES}
    for junk in COUNT_JUNK:
        yield {"nodes": [node("根", [node("子", [node("孙", id="z2", x=junk, y=junk)], id="z1")],
                              id="z0")], "dropped": junk, "title": junk, "created": junk,
               "updated": junk, "links": [{"from": "z0", "to": junk, "id": junk}]}
    yield BAD_DOC


ROUNDS = list(junk_rounds())
bad_rounds = []
for _i, _doc in enumerate(ROUNDS):
    try:
        _d = mm.normalize(_doc)
        if mm.normalize(_d) != _d:
            bad_rounds.append(("不幂等", _doc))
            continue
        for _f in ("tree", "radial", "fishbone", "concept"):
            _lay = mm.layout(_d, _f)
            _ids = {b["id"] for b in _lay["boxes"]}
            if not (_lay["width"] > 0 and _lay["height"] > 0) \
                    or any(e["from"] not in _ids or e["to"] not in _ids for e in _lay["edges"]) \
                    or not mm.render_svg(_d, _f).rstrip().endswith("</svg>") \
                    or not mm.to_markdown(_d).strip():
                bad_rounds.append(("布局或出图坏了", _doc))
                break
        _dir = os.path.join(SANDBOX, "junk-%d" % _i)
        os.makedirs(_dir, exist_ok=True)
        if not mm.save_map(_dir, _d)["ok"] or strip_stamps(mm.load_map(_dir)) != strip_stamps(_d):
            bad_rounds.append(("存读不等", _doc))
    except Exception as exc:
        bad_rounds.append((exc.__class__.__name__, _doc))
chk("脏数据扫 %d 份：整条流水线不抛、洗完幂等、四种形态都出得来图、存进去读回来等价"
    % len(ROUNDS), not bad_rounds, bad_rounds[:2])

# ── 收摊 ───────────────────────────────────────────────────────────
print()
print("跑了 %d 条断言" % len(CHECKS))
if FAIL:
    print("失败 %d 项：%s" % (len(FAIL), "、".join(FAIL)))
    sys.exit(1)
print("全部通过")
