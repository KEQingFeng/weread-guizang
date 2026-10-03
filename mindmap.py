# -*- coding: utf-8 -*-
"""可编辑思维导图的引擎：文档清洗 → 四种形态的服务端布局 → SVG / Markdown。

为什么单独一个文件：book_notes 里那张导图是从笔记**现算**出来的只读图，用户改不动；
这一张是用户自己画的，节点、连线、拖过的坐标都得存下来，所以它住在同一个书目录下的
mindmap.json。book_notes 那边导出的文件叫 mindmap.svg（EXPORT_MAP），两份东西名字不同、
各写各的 —— 手画的图被自动图冲掉是这一轮最不能接受的事故。

布局在服务端算完再交给前端：命令行、MCP 适配器和 tests/ 里的自测都没有浏览器，而它们
都要出图。前端只照 layout() 的 boxes/edges 画、不再自己排一遍，否则两套算法一漂，
存回去的坐标和屏幕上看到的就是两回事。

所有落盘路径都由调用方传 book_dir（绝对路径），本模块不解析数据目录、不读配置：
一张图属于一本书，属于哪个目录是路由层的事，不该在这里猜。
"""

import errno
import json
import math
import os
import re
import time
import uuid
from collections.abc import Iterator

MIND_FILE = "mindmap.json"
SCHEMA = 1

# 上限防的是「把整本书粘进来」和「前端画爆」，裁掉的量如实报进 dropped，不静默吞
MAX_NODES = 400
MAX_DEPTH = 8                 # 层数上限：留 depth 0..7，第八层往下的孩子整支裁掉
MAX_LINKS = 200
MAX_TEXT = 200
MAX_NOTE = 4000
MAX_TITLE = 120
MAX_LINK_TEXT = 60
MAX_ID = 40
MAX_COORD = 20000.0           # 拖过的坐标只收这个范围内的数，否则画布会被顶成几万像素

# 四种形态：名字是给路由用的键，中文名是给人看的
FORMS = (("tree", "括号图"), ("radial", "辐射图"),
         ("fishbone", "鱼骨图"), ("concept", "概念图"))
FORM_BY_KEY = dict(FORMS)
FORM_NAME = {k: n for k, n in FORMS}

# ── 视觉语言（与 book_notes 的只读导图同一套米色纸，自成一体，不 import 那边）──
_PAPER = "#fbf7ef"
_FILL_ROOT = "#2f2a24"
_INK_ROOT = "#f7f2e7"
_INK_DARK = "#2f2a24"
_INK_LIGHT = "#f7f2e7"
_STROKE = "#d8cfbe"
_EDGE = "#c9bfab"
_EDGE_LINK = "#9a8f7c"
_FILL_AUTO = ("#e7dcc4", "#d6dfeb", "#d5e5da", "#edd6d0", "#ded5ec", "#e9e0cf", "#dbe6e6")
_FONT = "PingFang SC, Hiragino Sans GB, Microsoft YaHei, sans-serif"

# ── 排版尺度：框的尺寸四种形态共用，只有坐标各算各的 ──
_ROW = 17.0                   # 行高
_PAD_X, _PAD_Y = 11.0, 8.0    # 文字到框边
_CHAR_W = 13.2                # 中文按字宽算，一个字号约这么多像素
_WRAP_NODE, _WRAP_LEAF = 10, 16
_MAX_LINES = 3                # 折三行就截：中途省略比把框撑到屏幕外好
_BOX_MAX_W = 300.0
_COL_GAP = 46.0               # 括号图列间距
_V_GAP = 12.0                 # 同层上下间隔
_ROOT_GAP = 56.0              # 括号图里两棵树之间
_MARGIN = 36.0                # 画布留白
_TITLE_ROOM = 24.0            # 标题行占的高度
_MIN_CANVAS = (360.0, 200.0)  # 空图也要有形状，前端摆视口才不会塌成一条线

# 辐射图：半径按层递增，分支 ≥5 时上下各一半
_R_INNER = 120.0
_R_BASE = 205.0
_R_STEP = 130.0
_RADIAL_SPLIT = 5
_ARC_DEG = 160.0

# 鱼骨图：一条脊 + 上下交替的大骨
_HEAD_GAP = 70.0
_BONE_RISE = 150.0
_BONE_LEAN = 78.0
_BONE_SPAN = 300.0
_SUB_STEP = 150.0             # 再往外的深度层每条列的间隔


Node = dict           # 契约里的 Node / Link 都是普通 dict，没类型可继承


# ─────────────────────────── 清洗 ───────────────────────────

def empty(title: str = "") -> dict:
    """一张干净的空白图：新建、读盘失败、传进来的是坏数据，都回这个形状。"""
    return {"schema": SCHEMA, "title": _clip(title, MAX_TITLE), "form": "tree",
            "nodes": [], "links": [], "created": "", "updated": "", "dropped": 0}


def normalize(doc) -> dict:
    """清洗 + 补 id + 裁上限 + 环检测，产出一张能安全落盘、能直接布局的图。

    幂等：同一个输入洗两次结果必须完全相等（布局、存档、测试都靠这个前提）。
    因此这里两件事绝对不做 —— 不盖时间戳（盖了第二遍就是新值），不把 dropped
    当本轮重算（第二遍输入已经干净，会掉回 0，把「这张图被裁过」这件事抹掉）。
    dropped 是累计值，继承输入里已有的数再加本轮裁掉的。
    """
    raw = doc if isinstance(doc, dict) else {}
    state = {"left": MAX_NODES, "dropped": _count(raw.get("dropped")),
             "ids": set(), "link_ids": set()}
    nodes = []
    for item in _as_list(raw.get("nodes"), state):
        built = _norm_node(item, 0, state, frozenset())
        if built is not None:
            nodes.append(built)

    form = raw.get("form")
    # 先卡 isinstance：form 在坏 JSON 里可能是对象或数组，而 `dict in FORM_BY_KEY`
    # 这种成员测试要求键可哈希，一个 dict 进来就当场 TypeError —— 坏输入该被洗成默认形态。
    if not isinstance(form, str) or form not in FORM_BY_KEY:
        form = "tree"
    links = _norm_links(raw.get("links"), nodes, state)

    out = {"schema": SCHEMA,
           "title": _trunc(raw.get("title"), MAX_TITLE, state),
           "form": form,
           "nodes": nodes,
           "links": links,
           "created": _stamp(raw.get("created")),
           "updated": _stamp(raw.get("updated")),
           "dropped": state["dropped"]}
    return out


def _norm_node(raw, depth, state, ancestors):
    """递归洗一个节点。ancestors 按 dict 的对象身份挡环 —— A 的孩子里出现 A 自己
    是前端拖拽真能造出来的形状，不挡就是无限递归（报错都比卡死好查）。"""
    if not isinstance(raw, dict):
        state["dropped"] += 1           # children 里塞了字符串这类垃圾，丢掉但要报数
        return None
    if id(raw) in ancestors:
        state["dropped"] += 1
        return None
    if depth >= MAX_DEPTH or state["left"] <= 0:
        state["dropped"] += _rough_size(raw)    # 整支裁掉，数量如实报，别只报一个
        return None
    state["left"] -= 1

    nid = _clip(raw.get("id"), MAX_ID)
    if not nid or nid in state["ids"]:
        # 已有 id 绝不改：前端拿它定位选中项，改了用户就点不中自己画的那个框。
        # 只有缺 id 或撞号的才补新的（数组下标不行，删一个节点全体错位）。
        nid = "n" + uuid.uuid4().hex[:8]
        while nid in state["ids"]:
            nid = "n" + uuid.uuid4().hex[:8]
    state["ids"].add(nid)

    text = raw.get("text")
    if text is None:
        text = raw.get("label")        # 兼容 note_tree 那种 {"label": ...}
    node = {"id": nid,
            "text": _trunc(text, MAX_TEXT, state),
            "color": _hex(raw.get("color")) or None,
            "note": _trunc(raw.get("note"), MAX_NOTE, state) or None,
            "fold": bool(raw.get("fold")),
            "x": _coord(raw.get("x")),
            "y": _coord(raw.get("y")),
            "children": []}
    below = ancestors | {id(raw)}
    for kid in _as_list(raw.get("children"), state):
        built = _norm_node(kid, depth + 1, state, below)
        if built is not None:
            node["children"].append(built)
    return node


def _norm_links(raw_links, nodes, state):
    """连线只留两端都存在、且不是自环的。指到不存在节点的线画出来是半截的，
    自环画出来是一个点，两种都只会让用户以为图坏了。"""
    ids = {n["id"] for n in walk({"nodes": nodes})}   # 连线连的多半是子孙，只看根会误杀
    out = []
    dropped_tail = 0
    for raw in _as_list(raw_links, state):
        if dropped_tail:
            state["dropped"] += 1          # 超上限之后剩下的不必再验，一起数掉
            continue
        if not isinstance(raw, dict):
            state["dropped"] += 1
            continue
        if len(out) >= MAX_LINKS:
            dropped_tail = 1
            state["dropped"] += 1
            continue
        frm, to = _clip(raw.get("from"), MAX_ID), _clip(raw.get("to"), MAX_ID)
        if not frm or not to or frm == to or frm not in ids or to not in ids:
            state["dropped"] += 1
            continue
        lid = _clip(raw.get("id"), MAX_ID)
        if not lid or lid in state["link_ids"]:
            lid = "l" + uuid.uuid4().hex[:8]
            while lid in state["link_ids"]:
                lid = "l" + uuid.uuid4().hex[:8]
        state["link_ids"].add(lid)
        out.append({"id": lid, "from": frm, "to": to,
                    "text": _trunc(raw.get("text"), MAX_LINK_TEXT, state)})
    return out


def _rough_size(raw, limit=MAX_NODES * 4):
    """估算一支子树的节点数，只为把裁掉多少报准；带环也数得完。"""
    seen, stack, total = set(), [raw], 0
    while stack and total < limit:
        node = stack.pop()
        if not isinstance(node, dict) or id(node) in seen:
            continue
        seen.add(id(node))
        total += 1
        kids = node.get("children")
        if isinstance(kids, list):
            stack.extend(kids)
    return total


def _clip(v, n):
    s = "" if v is None else str(v)
    return s.replace("\r\n", "\n").strip()[:n]


def _trunc(v, n, state):
    """带计数的裁剪：裁了就报，静默吞掉等于骗用户（他会以为备注还在）。"""
    s = "" if v is None else str(v)
    s = s.replace("\r\n", "\n").strip()
    if len(s) > n:
        state["dropped"] += 1
    return s[:n]


def _as_list(v, state=None):
    if isinstance(v, list):
        return v
    if state is not None and v not in (None, "", {}, ()):
        state["dropped"] += 1        # {"nodes": "abc"} 这种整段坏数据也要报出来
    return []


def _hex(v):
    s = _clip(v, 32)
    return s.lower() if re.fullmatch(r"#(?:[0-9a-f]{3}|[0-9a-f]{6})", s, re.I) else ""


def _coord(v):
    """拖过的坐标只认有限正数；其余当没拖过（走自动布局）。
    负数会把内容顶出画布、非数字会让整张图算不出框，都不该往下传。"""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) and 0.0 <= f <= MAX_COORD else None


def _count(v):
    """只收正整数，其余当 0：dropped 是用户看得见的报数，负数、bool、字符串都没意义。
    两条路分开走 —— 整数原样收，非整数才过一遍 float。一起走 float 会把 10**18
    这种大整数抹掉精度（1000000000000000002 变成 ...000），存进去再读回来就不相等，
    normalize 的幂等也跟着破。int(float("inf")) 抛的是 OverflowError，不在
    TypeError/ValueError 里，不接住的话手改过的 JSON 里一个 Infinity 就打不开图。"""
    if isinstance(v, bool):
        return 0
    if isinstance(v, int):
        return v if v > 0 else 0
    try:
        number = float(v)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(number):
        return 0
    n = int(number)
    return n if n > 0 else 0


def _stamp(v):
    """时间戳原样保留（字符串、限长），绝不在这里盖当前时间 —— 否则 normalize
    两次结果不同，幂等就破了。盖时间戳是 save_map 的活。"""
    return v if isinstance(v, str) and len(v) <= 40 else ""


def _new_id(kind):
    return kind + uuid.uuid4().hex[:8]


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


# ─────────────────────────── 读写 ───────────────────────────

def path_of(book_dir) -> str:
    """只拼路径，不碰文件。文件名写死 MIND_FILE，落点永远在调用方给的书目录里 ——
    越界由上层挡（路由校验 book_dir），这里不做第二次判断，免得两处口径不一致。"""
    return os.path.join(book_dir, MIND_FILE)


def load_map(book_dir) -> dict:
    """读这张图。没文件、文件截断、内容不是对象，一律回可用的空图，绝不抛。

    坏文件不重命名不删除：这是个读接口，动用户的文件得由用户点头。
    """
    try:
        with open(path_of(book_dir), encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return normalize(empty())
        return normalize(raw)
    except Exception:
        return normalize(empty())


def save_map(book_dir, doc) -> dict:
    """落盘。返回 {"ok","path","bytes","nodes","links","dropped","error"}。

    error 恒在（成功时是空串）：形状固定，路由层不必先判 ok 再拼字段。
    写失败不抛 —— 磁盘满、目录被删都是用户要点「保存」才知道的事，
    抛上去就变成界面白屏。
    """
    d = normalize(doc)
    now = _now_iso()
    if not d["created"]:
        d["created"] = now
    d["updated"] = now
    path = path_of(book_dir)
    out = {"ok": False, "path": path, "bytes": 0, "nodes": node_count(d),
           "links": len(d["links"]), "dropped": d["dropped"], "error": ""}
    try:
        text = json.dumps(d, ensure_ascii=False, indent=2)
        _write_json(path, text)
    except OSError as exc:
        out["error"] = "写不下去：" + _os_reason(exc)
        return out
    except Exception:
        # 到这里只能是内容本身有鬼（normalize 之后本该全是可序列化的东西）。
        # 不拼 exc 原文：系统的英文句子甩到界面上，用户看不懂，也不该看见。
        out["error"] = "这张图存不下去：内容有问题"
        return out
    out["ok"] = True
    out["bytes"] = len(text.encode("utf-8"))
    return out


def _write_json(path, text):
    """先写 tmp 再 rename，覆盖前留一份 .prev：崩在写一半时盘上顶多留下 .tmp，
    用户的图不会变成半截 JSON（这个仓库的笔记那边也是这么做的）。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    if os.path.exists(path):
        try:
            os.replace(path, path + ".prev")
        except OSError:
            pass
    os.replace(tmp, path)


_ERR_REASON = {
    errno.ENOENT: "那个目录不在",
    errno.EACCES: "没有写入权限",
    errno.EPERM: "没有写入权限",
    errno.ENOSPC: "磁盘满了",
    errno.EROFS: "磁盘是只读的",
    errno.EISDIR: "那儿是个文件夹不是文件",
    errno.ENOTDIR: "路径上有不是文件夹的东西",
    errno.ENAMETOOLONG: "路径太长了",
}


def _os_reason(exc):
    """OSError 翻成人话：只说「怎么了」，不把英文系统消息甩到界面上。
    未知错误码给一句通用的，界面照样能提示，日志另有 bytes/path 可查。"""
    return _ERR_REASON.get(getattr(exc, "errno", None), "这个位置写不进去")


# ─────────────────────────── 遍历 ───────────────────────────

def walk(doc) -> Iterator[Node]:
    """前序走一遍，森林里每棵树都算。

    seen 按对象身份挡重复：坏文档里同一个 dict 可能既是自己的孩子又出现在两处，
    不挡就是永不结束的生成器 —— 调用方看到的是卡死而不是报错，那种 bug 最难查。
    """
    if not isinstance(doc, dict):
        return
    roots = doc.get("nodes")
    if not isinstance(roots, list):
        return
    stack = [n for n in reversed(roots) if isinstance(n, dict)]
    seen = set()
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        yield node
        kids = node.get("children")
        if isinstance(kids, list):
            for kid in reversed([k for k in kids if isinstance(k, dict)]):
                stack.append(kid)


def node_count(doc) -> int:
    return sum(1 for _ in walk(doc))


def find(doc, node_id) -> Node | None:
    if not node_id:
        return None
    for node in walk(doc):
        if node.get("id") == node_id:
            return node
    return None


def _visible(doc):
    """展开的那部分，前序：(node, depth, parent_id, hidden)。
    fold=True 的节点整棵子树不进布局，hidden 给出被藏起来的子孙数 ——
    前端要在框边上画个小圆点写这个数字，用户才知道这里还有东西。"""
    out = []

    def rec(node, depth, parent):
        hidden = _tree_size(node) - 1 if node.get("fold") else 0
        out.append((node, depth, parent, hidden))
        if not hidden:
            for kid in node.get("children") or []:
                rec(kid, depth + 1, node["id"])

    for root in doc.get("nodes") or []:
        rec(root, 0, None)
    return out


def _tree_size(node):
    return 1 + sum(_tree_size(k) for k in (node.get("children") or []))


# ─────────────────────────── 布局 ───────────────────────────

def layout(doc, form=None) -> dict:
    """算出前端唯一的渲染依据：boxes / edges / 画布尺寸 / counts。

    进来先 normalize 一遍：布局不该去处理「children 里塞字符串」这类脏东西，
    四种形态各写一遍容错只会漏。normalize 幂等，洗过的图再洗一次是空操作。
    """
    d = normalize(doc)
    # form 参数同样先卡类型：路由层从查询串里取出来的可能是任何东西，
    # 而 dict 的成员测试遇到不可哈希的值会直接抛 TypeError
    use = form if isinstance(form, str) and form in FORM_BY_KEY else d["form"]
    entries = _visible(d)
    boxes = _make_boxes(entries)
    kids = _kids_of(boxes)
    roots = [b["id"] for b in boxes if b["parent"] is None]
    # 只给一个轴不算钉过：半个坐标摆不出框，与其猜不如走自动布局
    pos = {node["id"]: (node["x"], node["y"]) for node, _depth, _p, _h in entries
           if node.get("x") is not None and node.get("y") is not None}

    ctx = {"byid": {b["id"]: b for b in boxes}, "kids": kids, "roots": roots,
           "pos": pos, "doc": d}
    hints = _PLACE[use](boxes, ctx) or {}
    dx, dy = _fit(boxes, d["title"])
    for box in boxes:
        # 坐标统一到 0.1 像素：布局里加减出来的浮点尾巴（894.0000000000001）写进 JSON
        # 只是噪音，SVG 也画不出那零点几像素，留着还会让前端的 diff 一直变。
        for key in ("x", "y", "w", "h"):
            box[key] = round(box[key], 1)
    hints = {bid: (round(hx + dx, 1), round(hy + dy, 1))
             for bid, (hx, hy) in list(hints.items())}

    width, height = _canvas(boxes)
    return {"form": use, "width": width, "height": height,
            "boxes": boxes,
            "edges": _make_edges(boxes, ctx, use, hints),
            "counts": {"nodes": node_count(d), "links": len(d["links"]),
                       "visible": len(boxes), "roots": len(d["nodes"])}}


def _make_boxes(entries):
    # 有没有「展开的孩子」决定折行宽度，也决定它是不是末端框；折叠掉的子树不算孩子，
    # 所以折叠节点按末端框排版，它自己就是这一支的最后一眼。
    parents = {e[2] for e in entries if e[2]}
    boxes = []
    for node, depth, parent, hidden in entries:
        is_leaf = node["id"] not in parents
        lines, w, h = _measure(node["text"], is_leaf)
        fill, ink = _box_colors(node, depth)
        boxes.append({"id": node["id"], "x": 0.0, "y": 0.0, "w": w, "h": h,
                      "text": node["text"], "lines": lines, "color": fill, "ink": ink,
                      "depth": depth, "parent": parent, "hidden": hidden})
    return boxes


def _measure(text, is_leaf):
    """框的大小：宽按最长一行字算（中文约一个字号一像素宽），高按行数算。"""
    lines = _wrap(text, _WRAP_LEAF if is_leaf else _WRAP_NODE)
    wide = max(len(x) for x in lines)
    return lines, min(_BOX_MAX_W, _PAD_X * 2 + wide * _CHAR_W), _PAD_Y * 2 + _ROW * len(lines)


def _wrap(text, width):
    """按字宽折行，最多三行 —— 思维导图中途截断比把框撑到画布外好。"""
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return ["…"]
    return [text[i:i + width] for i in range(0, len(text), width)][:_MAX_LINES]


def _box_colors(node, depth):
    """底色与字色在这里一次算定，前端照用：根是深底浅字（跟只读那张导图一致），
    其余按层级轮转米色系。用户自己挑了颜色就用他的，字色按底色亮度给 ——
    否则他挑个深紫，配上默认深字就成了看不见。"""
    if depth <= 0:
        return _FILL_ROOT, _INK_ROOT
    fill = node.get("color") or _FILL_AUTO[depth % len(_FILL_AUTO)]
    return fill, _INK_LIGHT if _lum(fill) < 140.0 else _INK_DARK


def _lum(color):
    try:
        rgb = _rgb(color)
    except (ValueError, TypeError):
        return 255.0
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]


def _rgb(color):
    s = (color or "").lstrip("#")
    if len(s) == 3:
        s = "".join(c * 2 for c in s)
    if len(s) != 6:
        raise ValueError("不是颜色")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def _kids_of(boxes):
    kids = {}
    for b in boxes:
        if b["parent"]:
            kids.setdefault(b["parent"], []).append(b["id"])
    return kids


_PLACE = {}


def _register(name):
    def deco(fn):
        _PLACE[name] = fn
        return fn
    return deco


@_register("tree")
def _place_tree(boxes, ctx):
    """括号图：叶子按顺序往下堆，父节点落在首末孩子中点 —— 连线不交叉。
    列宽取该层最宽的框，所以同一层永远对齐，看着才像大纲。"""
    byid, kids, roots = ctx["byid"], ctx["kids"], ctx["roots"]
    cols = []

    def rec(bid, depth, y):
        b = byid[bid]
        while len(cols) <= depth:
            cols.append(0.0)
        cols[depth] = max(cols[depth], b["w"])
        ck = kids.get(bid) or []
        if not ck:
            b["y"] = y
            return y + b["h"] + _V_GAP
        cur = y
        for kid in ck:
            cur = rec(kid, depth + 1, cur)
        first, last = byid[ck[0]], byid[ck[-1]]
        b["y"] = (_center(first)[1] + _center(last)[1]) / 2.0 - b["h"] / 2.0
        return cur

    y = 0.0
    for root in roots:
        y = rec(root, 0, y) + _ROOT_GAP

    x = 0.0
    for depth in range(len(cols)):
        for b in boxes:
            if b["depth"] == depth:
                b["x"] = x
        x += (cols[depth] if cols[depth] else 120.0) + _COL_GAP
    return {}


@_register("radial")
def _place_radial(boxes, ctx):
    """辐射图：中心在画布正中，一级分支按角度铺开，子层沿父层的扇区往外长。

    扇角按子树叶子数加权 —— 不按加权的话，一个有三片叶子的分支和一个孤零零的
    分支占一样大的角，前者一定挤成一团。盒子中心对齐到算出来的点。
    """
    byid, kids, roots = ctx["byid"], ctx["kids"], ctx["roots"]
    if not roots:
        return {}
    memo = {}

    def leaves(bid):
        if bid in memo:
            return memo[bid]
        ck = kids.get(bid) or []
        memo[bid] = 1 if not ck else sum(leaves(k) for k in ck)
        return memo[bid]

    single = len(roots) == 1
    groups = (kids.get(roots[0]) or []) if single else list(roots)
    if single:
        head = byid[roots[0]]
        head["x"], head["y"] = -head["w"] / 2.0, -head["h"] / 2.0

    def place(bid, a0, a1, depth):
        b = byid[bid]
        if depth == 0 and single:
            cx = cy = 0.0
        else:
            radius = _R_BASE + (depth - 1) * _R_STEP if depth >= 1 else _R_INNER
            ang = (a0 + a1) / 2.0
            cx, cy = radius * math.cos(ang), radius * math.sin(ang)
        b["x"], b["y"] = cx - b["w"] / 2.0, cy - b["h"] / 2.0
        ck = kids.get(bid) or []
        if not ck:
            return
        span = (a1 - a0) * 0.94        # 留一点缝，相邻子树的边不至于贴在一起
        total = sum(leaves(k) for k in ck) or 1
        cur = (a0 + a1) / 2.0 - span / 2.0
        for kid in ck:
            wide = span * leaves(kid) / total
            place(kid, cur, cur + wide, depth + 1)
            cur += wide

    for bid, (a0, a1) in zip(groups, _ring_sectors([leaves(b) for b in groups])):
        place(bid, a0, a1, 1 if single else 0)
    return {}


def _ring_sectors(weights):
    """一级分支的角区间（弧度）。分支 ≥5 时上下各一半，绕成一圈不断开：
    上半从左到右，下半从右到左。少于 5 个就整圈均分 —— 挤成两个半圆的图没法看。"""
    n = len(weights)
    if n == 0:
        return []
    if n < _RADIAL_SPLIT:
        total = sum(weights) or n
        cur, step = math.radians(-90.0), math.radians(360.0)
        out = []
        for w in weights:
            wide = step * w / total
            out.append((cur, cur + wide))
            cur += wide
        return out

    half = (n + 1) // 2
    upper, lower = weights[:half], weights[half:]
    out = [None] * n
    span = math.radians(_ARC_DEG)
    cur = math.radians(-170.0)                 # 上半：左上角起，向右扫到右上
    for idx, w in enumerate(upper):
        wide = span * w / (sum(upper) or 1)
        out[idx] = (cur, cur + wide)
        cur += wide
    cur = math.radians(10.0)                   # 下半：接着右上往右下起，扫到左下 —— 分支顺序绕一圈不断开
    for idx, w in enumerate(lower):
        wide = span * w / (sum(lower) or 1)
        out[half + idx] = (cur, cur + wide)
        cur += wide
    return [x for x in out if x]


@_register("fishbone")
def _place_fishbone(boxes, ctx):
    """鱼骨图：一条主骨从左到右横穿，鱼头在右端。

    每棵一级分支是一根大骨，上下交替挂 —— 相邻两根骨分居脊的两侧，字才不会叠在
    一起；全挤在同一侧的话一半的骨会互相盖住。大骨上的孩子沿骨退着挂，再往下的
    深度层整条往外让一列。返回 hints：每根骨的起点落在脊上（边要从那里起笔，
    从鱼头拉过去就成了扇子形，不像鱼骨）。
    """
    byid, kids, roots = ctx["byid"], ctx["kids"], ctx["roots"]
    if not roots:
        return {}
    head = byid[roots[0]]
    head["x"], head["y"] = -head["w"] / 2.0, -head["h"] / 2.0
    bones = list(kids.get(roots[0]) or [])
    bones += list(roots[1:])       # 森林在鱼骨里只有一条脊：多余的根也当一级骨挂上去

    def sub_depth(bid):
        ck = kids.get(bid) or []
        return 1 + max([sub_depth(k) for k in ck], default=0)

    def sub_wide(bid):
        ck = kids.get(bid) or []
        return max([byid[k]["w"] for k in ck] + [sub_wide(k) for k in ck], default=0.0)

    cursor = -(_HEAD_GAP + head["w"] / 2.0)
    hints = {}
    for idx, bid in enumerate(bones):
        b = byid[bid]
        col = max(_SUB_STEP, sub_wide(bid) + 40.0)
        span = max(_BONE_SPAN, 2.0 * (sub_depth(bid) - 1) * col + 160.0)
        anchor = (cursor - span * 0.55, 0.0)
        cursor -= span
        level1 = kids.get(bid) or []
        rise = max(_BONE_RISE, 46.0 * (len(level1) + 1))
        side = -1.0 if idx % 2 == 0 else 1.0        # 上下交替，这条是鱼骨的可读性命门
        tip = (anchor[0] + _BONE_LEAN, side * rise)
        b["x"], b["y"] = tip[0] - b["w"] / 2.0, tip[1] - b["h"] / 2.0
        hints[bid] = anchor
        _hang(byid, kids, bid, anchor, tip, side, col, b["depth"])
    return hints


def _hang(byid, kids, bid, anchor, tip, side, col, bone_depth):
    """孩子沿骨退着挂（靠近骨尖的先排），再往下的深度层整条往骨外侧让开一列。
    外侧 = 上骨向上、下骨向下：往脊那侧让就会盖住脊和别的骨。"""
    ck = kids.get(bid) or []
    for pos, kid in enumerate(ck):
        kb = byid[kid]
        rel = max(1, kb["depth"] - bone_depth)      # 离骨头几层，1 是骨上的直接孩子
        ratio = 0.62 - 0.30 * pos if len(ck) > 1 else 0.62
        base = (anchor[0] + (tip[0] - anchor[0]) * ratio,
                anchor[1] + (tip[1] - anchor[1]) * ratio)
        out = (rel - 1) * col                       # 让开的外侧距离
        kb["x"] = base[0] + out * 0.3 - kb["w"] / 2.0
        kb["y"] = base[1] + side * (out + 26.0 + kb["h"] / 2.0) - kb["h"] / 2.0
        _hang(byid, kids, kid, anchor, tip, side, col, bone_depth)


@_register("concept")
def _place_concept(boxes, ctx):
    """概念图：用户拖过的坐标优先，没拖过的先按括号图排一遍再落位。

    x/y 就是盒子左上角，和 boxes 同一套坐标系，不做中心点偏移 —— 那样存回去的坐标
    每保存一次都会漂半个框宽，用户会发现「拖好了又偏了」。
    """
    pos = ctx["pos"]
    _place_tree(boxes, ctx)
    free = [b for b in boxes if b["id"] not in pos]
    pinned = [b for b in boxes if b["id"] in pos]
    for b in pinned:
        b["x"], b["y"] = pos[b["id"]]
    if not pinned or not free:
        return {}
    # 自动排的那批整体挪到钉过那批的下方，别压上去（细粒度避让不在这一层做，
    # 概念图的意义就是让用户自己拖，这里只要给个不重叠的初值）
    top = max(b["y"] + b["h"] for b in pinned) + _ROOT_GAP
    low = min(b["y"] for b in free)
    if low < top:
        for b in free:
            b["y"] += top - low
    min_x = min(b["x"] for b in free)
    if min_x < _MARGIN:
        for b in free:
            b["x"] += _MARGIN - min_x
    min_y = min(b["y"] for b in free)
    if min_y < _MARGIN:
        for b in free:
            b["y"] += _MARGIN - min_y
    return {}


def _fit(boxes, title):
    """把内容整体推进画布留白里，返回位移量（边的起笔点要跟着走）。

    只在真的贴边时才动：概念图里用户钉过的坐标不能被抹平。
    """
    if not boxes:
        return 0.0, 0.0
    dx = max(0.0, _MARGIN - min(b["x"] for b in boxes))
    top = _MARGIN + (_TITLE_ROOM if title else 0.0)
    dy = max(0.0, top - min(b["y"] for b in boxes))
    for b in boxes:
        b["x"] += dx
        b["y"] += dy
    return dx, dy


def _canvas(boxes):
    """画布尺寸 = 内容右下缘 + 留白，保证严格大于所有盒子（前端不用自己再加）。
    两个数也收到 0.1：加出来的浮点尾巴会一路带进 viewBox 和 width 属性。"""
    if not boxes:
        return _MIN_CANVAS[0], _MIN_CANVAS[1]
    return (round(max(b["x"] + b["w"] for b in boxes) + _MARGIN, 1),
            round(max(b["y"] + b["h"] for b in boxes) + _MARGIN, 1))


def _make_edges(boxes, ctx, form, hints):
    """先树边后连线。连线四种形态都画：只画概念图的话，切到别的形态用户就
    看不见「这两块其实有关系」了；但非概念形态只画一次、画细虚线，
    不然分支一多就是一张蜘蛛网。"""
    byid = ctx["byid"]
    edges = []
    for b in boxes:
        pid = b["parent"]
        if not pid or pid not in byid:
            continue
        p = byid[pid]
        if form == "tree":
            p1 = (p["x"] + p["w"], _center(p)[1])
            p2 = (b["x"], _center(b)[1])
            path, lx, ly = _spline(p1, p2)
        elif form == "fishbone":
            p1 = hints.get(b["id"]) or _border(p, _center(b))
            p2 = _border(b, p1)
            path, lx, ly = _line(p1, p2)
        else:
            p1 = _border(p, _center(b))
            p2 = _border(b, _center(p))
            path, lx, ly = _spline(p1, p2)
        edges.append({"from": pid, "to": b["id"], "path": path,
                      "color": _EDGE, "label": "", "lx": lx, "ly": ly,
                      "kind": "branch"})

    for link in ctx["doc"]["links"]:
        a, b2 = byid.get(link["from"]), byid.get(link["to"])
        if not a or not b2:
            continue
        if form == "fishbone":
            path, lx, ly = _line(_border(a, _center(b2)), _border(b2, _center(a)))
        else:
            path, lx, ly = _spline(_border(a, _center(b2)), _border(b2, _center(a)))
        edges.append({"from": link["from"], "to": link["to"], "path": path,
                      "color": _EDGE_LINK, "label": link["text"], "lx": lx, "ly": ly,
                      "kind": "link"})
    return edges


def _center(b):
    return b["x"] + b["w"] / 2.0, b["y"] + b["h"] / 2.0


def _border(b, toward):
    """盒子中心朝 toward 方向与矩形边的交点：线画到框边上，不穿过字。"""
    cx, cy = _center(b)
    dx, dy = toward[0] - cx, toward[1] - cy
    if dx == 0.0 and dy == 0.0:
        return cx, cy
    sx = (b["w"] / 2.0) / abs(dx) if dx else math.inf
    sy = (b["h"] / 2.0) / abs(dy) if dy else math.inf
    t = min(sx, sy)
    return cx + dx * t, cy + dy * t


def _spline(p1, p2):
    """三次曲线，控制点沿连线主方向错开 —— 返回路径与中点（标签落在这上面）。"""
    x1, y1 = p1
    x2, y2 = p2
    dx, dy = x2 - x1, y2 - y1
    if abs(dx) >= abs(dy):
        k = math.copysign(max(18.0, abs(dx) * 0.42), dx if dx else 1.0)
        c1x, c1y, c2x, c2y = x1 + k, y1, x2 - k, y2
    else:
        k = math.copysign(max(18.0, abs(dy) * 0.42), dy if dy else 1.0)
        c1x, c1y, c2x, c2y = x1, y1 + k, x2, y2 - k
    lx = (x1 + 3 * c1x + 3 * c2x + x2) / 8.0
    ly = (y1 + 3 * c1y + 3 * c2y + y2) / 8.0
    return ("M%.1f %.1f C%.1f %.1f %.1f %.1f %.1f %.1f"
            % (x1, y1, c1x, c1y, c2x, c2y, x2, y2)), round(lx, 1), round(ly, 1)


def _line(p1, p2):
    x1, y1 = p1
    x2, y2 = p2
    return ("M%.1f %.1f L%.1f %.1f" % (x1, y1, x2, y2),
            round((x1 + x2) / 2.0, 1), round((y1 + y2) / 2.0, 1))


# ─────────────────────────── 出图 ───────────────────────────

def _esc(s):
    """XML 转义。节点文字是用户敲的，可能带 < > &，不转义整张 SVG 就废了。"""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))


def render_svg(doc, form=None) -> str:
    """layout() 的坐标 → 一段自足 SVG 字符串。纯拼接，不叫浏览器。

    坐标系完全来自 layout()，这里只管画：米色底、圆角矩形、根节点深底浅色字，
    跟只读那张导图对齐；折叠过的框边上补个小圆点写藏起来的条数。
    """
    lay = layout(doc, form)
    boxes, edges = lay["boxes"], lay["edges"]
    width = int(math.ceil(lay["width"]))
    height = int(math.ceil(lay["height"]))
    # 标题从文档里现取：layout 的返回结构是前端契约，不该为了这里多塞一个字段
    title = _clip(doc.get("title"), MAX_TITLE) if isinstance(doc, dict) else ""
    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
           'viewBox="0 0 %d %d" font-family="%s">' % (width, height, width, height, _FONT),
           '<rect width="%d" height="%d" fill="%s"/>' % (width, height, _PAPER)]
    if title:
        out.append('<text x="%.1f" y="%.1f" font-size="13" fill="#8b8171">%s</text>'
                   % (_MARGIN, _MARGIN - 6, _esc(_clip(title, MAX_TITLE))))
    if lay["form"] == "fishbone":
        spine = [b for b in boxes if b["depth"] == 1]
        head = [b for b in boxes if b["depth"] == 0]
        if spine and head:
            y = _center(head[0])[1]
            out.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" '
                       'stroke-width="2.4" stroke-linecap="round"/>'
                       % (min(b["x"] for b in spine) - 8, y, head[0]["x"], y, _EDGE))

    dash = lay["form"] != "concept"
    for e in edges:
        if e["kind"] == "link" and dash:
            out.append('<path d="%s" fill="none" stroke="%s" stroke-width="1" '
                       'stroke-dasharray="5 4" stroke-opacity="0.75"/>'
                       % (e["path"], e["color"]))
        else:
            out.append('<path d="%s" fill="none" stroke="%s" stroke-width="%s" '
                       'stroke-linecap="round"/>'
                       % (e["path"], e["color"], "1.6" if e["kind"] == "branch" else "1.4"))
        if e["label"]:
            out.append('<text x="%.1f" y="%.1f" font-size="11" fill="#7d7361" '
                       'text-anchor="middle">%s</text>'
                       % (e["lx"], e["ly"] - 4, _esc(e["label"])))

    for b in boxes:
        is_root = b["depth"] == 0
        out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="%d" fill="%s" '
                   'stroke="%s" stroke-width="1.2"/>'
                   % (b["x"], b["y"], b["w"], b["h"], 12 if is_root else 9,
                      b["color"], b["color"] if is_root else _STROKE))
        ty = b["y"] + _PAD_Y + 12.0
        for ln in b["lines"]:
            out.append('<text x="%.1f" y="%.1f" font-size="13" fill="%s">%s</text>'
                       % (b["x"] + _PAD_X, ty, b["ink"], _esc(ln)))
            ty += _ROW
        if b["hidden"]:
            out.append('<circle cx="%.1f" cy="%.1f" r="9" fill="%s" stroke="%s"/>'
                       '<text x="%.1f" y="%.1f" font-size="10" fill="%s" '
                       'text-anchor="middle">%d</text>'
                       % (b["x"] + b["w"], b["y"] + b["h"] / 2.0, _PAPER, _EDGE,
                          b["x"] + b["w"], b["y"] + b["h"] / 2.0 + 3.5, _INK_DARK,
                          b["hidden"]))
    out.append("</svg>")
    return "\n".join(out)


def to_markdown(doc) -> str:
    """缩进列表 + 「关联」段：导图存的时候顺手导一份能进笔记、能 diff 的文本。

    空图也要有骨架 —— 用户对着一个零字节文件只会以为导出失败了。
    """
    d = normalize(doc)
    lines = ["# " + (d["title"] or "思维导图"), ""]
    # 连线要能写出两端的名字，折叠掉的节点也算：它是被藏起来的，不是不存在的
    byid = {n["id"]: n for n in walk(d)}
    entries = _visible(d)
    if not entries:
        lines.append("- （这张图还是空的，加个中心主题吧）")
    for node, depth, _parent, hidden in entries:
        pad = "  " * depth
        text = node["text"] or "未命名"
        lines.append("%s- %s%s" % (pad, text, "（折叠 %d 项）" % hidden if hidden else ""))
        if node["note"]:
            for seg in node["note"].split("\n"):
                lines.append("%s  %s" % (pad, seg.strip()))
    lines += ["", "## 关联", ""]
    if not d["links"]:
        lines.append("- （还没有连线）")
    for link in d["links"]:
        a = (byid.get(link["from"]) or {}).get("text") or "未命名"
        b2 = (byid.get(link["to"]) or {}).get("text") or "未命名"
        lines.append("- %s → %s：%s" % (a, b2, link["text"] or "相关"))
    return "\n".join(lines) + "\n"


# ─────────────────────────── 从只读导图迁过来 ───────────────────

def from_note_tree(tree, title="") -> dict:
    """book_notes.note_tree() 那种 {"label","kids","color"} 递归树 → 可编辑 doc。

    界面上「把这书的笔记导成脑图」走这条路：导进来就能接着手改。
    没有笔记也不抛 —— 给一棵只含标题的树，用户点开就有东西可改。
    不 import book_notes：那是只读自动图那套，两边牵连起来以后谁都动不了；
    树由路由层喂进来。
    """
    root = _from_node(tree, 0) if isinstance(tree, dict) else None
    if root is None:
        root = {"id": "", "text": "", "children": []}
    if not str(root["text"] or "").strip():
        # 树是空的、或者只有 kids 没有 label：根上必须有个名字，
        # 否则界面打开就是一张白框，用户以为导坏了。
        root["text"] = _clip(title, MAX_TEXT) or "中心主题"
    doc = empty(title)
    doc["form"] = "tree"
    doc["nodes"] = [root]
    return normalize(doc)


def _from_node(raw, depth):
    """note_tree 的形状 → 我们的形状，只认 label/kids/color，深到上限就停。
    这里必须自己限深：进来的树可能带环，交给 normalize 之前就先递归炸了。"""
    if not isinstance(raw, dict) or depth >= MAX_DEPTH:
        return None
    kids = []
    raw_kids = raw.get("kids")
    if not isinstance(raw_kids, list):
        raw_kids = raw.get("children")
    if isinstance(raw_kids, list):
        for kid in raw_kids:
            built = _from_node(kid, depth + 1)
            if built is not None:
                kids.append(built)
    text = raw.get("label")
    if text is None:
        text = raw.get("text")
    return {"id": _clip(raw.get("id"), MAX_ID), "text": text,
            "color": raw.get("color"), "note": raw.get("note"),
            "fold": raw.get("fold"), "children": kids}
