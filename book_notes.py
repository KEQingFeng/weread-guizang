# -*- coding: utf-8 -*-
"""书本地笔记：划线、批注、独立笔记条目、模板、导出、思维导图。

为什么单独一个文件：微信读书那边的划线是官方的、只读的（走 skills 接口），
这一套是「归藏里自己划的」，两边名字都叫笔记，混在一起以后就分不清哪条是谁的
了。所以文件、接口、前端标识一律用 book_notes / notes.json / guizang- 前缀。

数据就住在这本书自己的目录里：output/<book_id>/notes.json，导出物写在同目录
（notes.md / mindmap.svg）—— 用户要的是「文件数据保存在目录文件夹里」，不藏
在数据库里，拷走这本书就等于拷走了它的笔记。
"""

import json
import os
import re
import time
import uuid

NOTES_FILE = "notes.json"
EXPORT_MD = "notes.md"
EXPORT_MAP = "mindmap.svg"
SCHEMA = 1
MAX_ENTRY_BODY = 60000       # 单条笔记正文上限：正常写不满，防的是把整本书粘进来
MAX_ITEMS = 5000             # 划线 + 条目总条数上限

# 高亮分色 = 语义标签：颜色不是装饰，是「这句我打算怎么用它」
TAGS = (
    {"key": "point", "name": "论点", "color": "#ffd45e", "ink": "#3a2c05"},
    {"key": "doubt", "name": "疑问", "color": "#ff9d8f", "ink": "#3c130f"},
    {"key": "quote", "name": "可引用", "color": "#93e0b4", "ink": "#0c2e1a"},
    {"key": "todo", "name": "待查", "color": "#9cc6ff", "ink": "#0d2340"},
    {"key": "idea", "name": "灵感", "color": "#c9b1ff", "ink": "#231046"},
)
TAG_BY_KEY = {t["key"]: t for t in TAGS}
TAG_BY_NAME = {t["name"]: t for t in TAGS}
KINDS = ("highlight", "bold", "underline", "strike", "note")   # 划线 / 加粗 / 下划线 / 删除线 / 批注

TPL_FILE = "note_templates.json"


# ─────────────────────────── 读写 ───────────────────────────

def empty_notes():
    return {"schema": SCHEMA, "marks": [], "entries": [],
            "created_at": int(time.time()), "updated_at": int(time.time())}


def notes_path(book_dir):
    return os.path.join(book_dir, NOTES_FILE)


def load_notes(book_dir):
    """读这本书的笔记；文件不存在或读坏了都回空结构，绝不因为一次坏档打不开书。

    读坏了会先备份成 notes.json.bad-<时间> 再回空的 —— 那半截文件还有救，
    直接覆盖掉才是真丢了。
    """
    p = notes_path(book_dir)
    if not os.path.exists(p):
        return empty_notes()
    try:
        with open(p, encoding="utf-8") as f:
            doc = json.load(f)
        if not isinstance(doc, dict):
            raise ValueError("不是对象")
    except Exception:
        try:
            os.replace(p, p + ".bad-" + time.strftime("%Y%m%d%H%M%S"))
        except Exception:
            pass
        return empty_notes()
    doc.setdefault("marks", [])
    doc.setdefault("entries", [])
    for key in ("marks", "entries"):
        doc[key] = [x for x in doc[key] if isinstance(x, dict) and x.get("id")]
    doc["schema"] = SCHEMA
    return doc


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    if os.path.exists(path):
        try:
            os.replace(path, path + ".prev")      # 覆盖前留一份上一版
        except Exception:
            pass
    os.replace(tmp, path)


def save_notes(book_dir, doc):
    _write_json(notes_path(book_dir), _normalize(doc))
    return True


def _normalize(doc):
    """把前端传来的文档收敛成能安全落盘的样子：字段裁剪、时间戳补齐、条数封顶。"""
    out = empty_notes()
    out["created_at"] = int(doc.get("created_at") or out["created_at"])
    marks, entries = [], []
    for m in (doc.get("marks") or [])[:MAX_ITEMS]:
        if not isinstance(m, dict):
            continue
        text = _clip(m.get("text"), 4000)
        if not text:
            continue
        tag = m.get("tag") if m.get("tag") in TAG_BY_KEY else ""
        if m.get("tag_name"):                       # 允许前端只给中文名
            t = TAG_BY_NAME.get(str(m["tag_name"])[:8])
            tag = t["key"] if t else tag
        marks.append({
            "id": _clip(m.get("id"), 40) or "m_" + uuid.uuid4().hex[:10],
            "ch": _clip(m.get("ch"), 60),
            "text": text,
            "prefix": _clip(m.get("prefix"), 120),
            "suffix": _clip(m.get("suffix"), 120),
            "kind": m.get("kind") if m.get("kind") in KINDS else "highlight",
            "tag": tag,
            # 颜色跟着标签走：选了「疑问」就是那支笔，前端不必两处各设一遍再担心对不上；
            # 自己挑的颜色（合法 #hex）优先，允许不被标签覆盖。
            "color": _hex(m.get("color")) or (TAG_BY_KEY[tag]["color"] if tag else ""),
            "memo": _clip(m.get("memo"), 4000),
            "page": _clip(m.get("page"), 40),
            "created_at": int(m.get("created_at") or time.time()),
            "updated_at": int(m.get("updated_at") or time.time()),
        })
    rest = MAX_ITEMS - len(marks)
    for e in (doc.get("entries") or [])[:max(rest, 0)]:
        if not isinstance(e, dict):
            continue
        refs = []
        for r in (e.get("refs") or [])[:40]:
            if not isinstance(r, dict):
                continue
            refs.append({"text": _clip(r.get("text"), 1200), "ch": _clip(r.get("ch"), 60),
                         "mark": _clip(r.get("mark"), 40), "entry": _clip(r.get("entry"), 40)})
        entries.append({
            "id": _clip(e.get("id"), 40) or "n_" + uuid.uuid4().hex[:10],
            "title": _clip(e.get("title"), 200),
            "body": _clip(e.get("body"), MAX_ENTRY_BODY),
            "ch": _clip(e.get("ch"), 60),
            "tag": e.get("tag") if e.get("tag") in TAG_BY_KEY else "",
            "refs": refs,
            "tpl": _clip(e.get("tpl"), 60),
            "created_at": int(e.get("created_at") or time.time()),
            "updated_at": int(e.get("updated_at") or time.time()),
        })
    marks.sort(key=lambda x: (-x["updated_at"], x["ch"]))
    entries.sort(key=lambda x: (-x["updated_at"], x["title"]))
    out["marks"], out["entries"] = marks, entries
    out["tpl"] = _clip(doc.get("tpl"), 60)
    out["note"] = _clip(doc.get("note"), 2000)          # 这本书总的一句话备注
    out["updated_at"] = int(time.time())
    return out


def _clip(v, n):
    s = "" if v is None else str(v)
    s = s.replace("\r\n", "\n").strip()
    return s[:n]


def _hex(v):
    s = (v or "").strip()
    return s if re.fullmatch(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})", s) else ""


def counts(doc):
    return {"marks": len(doc.get("marks") or []), "entries": len(doc.get("entries") or [])}



# ─────────────────────────── 导出 ───────────────────────────

def _ts(v):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(v)))
    except Exception:
        return ""


def chapter_titles(book_dir):
    """filename → 章节标题，给导出和思维导图中文化用（认不出就回空表）。"""
    out = {}
    cat = os.path.join(book_dir, "_catalog.json")
    if not os.path.exists(cat):
        return out
    try:
        with open(cat, encoding="utf-8") as f:
            titles = json.load(f)
    except Exception:
        return out
    for i, t in enumerate(titles if isinstance(titles, list) else []):
        out["%04d.md" % i] = str(t)[:120]
    return out


def export_markdown(doc, meta=None, titles=None, book_label=""):
    """笔记 → 一份能直接进阅读器 / Git / flomo 的 Markdown。

    划线按章节聚（跟书的顺序走，读的时候好回溯），条目按更新时间倒排（新的在前）。
    """
    meta = meta or {}
    titles = titles or {}
    marks = doc.get("marks") or []
    entries = doc.get("entries") or []
    L = ["# %s 读书笔记" % (book_label or meta.get("title") or "这本书"), ""]
    L.append("> 导出 %s　·　划线 %d 处　·　笔记条目 %d 条" %
             (_ts(time.time()), len(marks), len(entries)))
    if (doc.get("note") or "").strip():
        L.append(">")
        L.append("> %s" % doc["note"].strip().replace("\n", " "))
    L.append("")

    if marks:
        L += ["## 划线", ""]
        by_ch = {}
        for m in marks:
            by_ch.setdefault(m.get("ch") or "", []).append(m)
        for ch in sorted(by_ch, key=lambda k: (not k, k)):
            L.append("### %s" % (titles.get(ch) or (ch or "未定位章节")))
            L.append("")
            for m in sorted(by_ch[ch], key=lambda x: x.get("created_at") or 0):
                t = TAG_BY_KEY.get(m.get("tag"))
                head = "**%s**：" % t["name"] if t else ""
                text = (m.get("text") or "").replace("\n", " ")
                L.append("- %s「%s」" % (head, text))
                memo = (m.get("memo") or "").strip()
                if memo:
                    for ln in memo.split("\n"):
                        L.append("  - %s" % ln if ln.strip() else "  -")
                L.append("  - <%s>　%s" % (_kind_label(m.get("kind")), _ts(m.get("created_at"))))
            L.append("")

    if entries:
        L += ["## 笔记条目", ""]
        for e in entries:
            L.append("### %s" % (e.get("title") or "无标题"))
            L.append("")
            for r in (e.get("refs") or []):
                if (r.get("text") or "").strip():
                    L.append("> 引用：%s%s" % (r["text"].replace("\n", " "),
                                          ("（%s）" % titles.get(r.get("ch")) if r.get("ch") in titles else "")
                                          .rstrip("（）")))
            body = (e.get("body") or "").strip()
            L.append(body or "（这一条还是空的）")
            L.append("")
            L.append("*%s*" % _ts(e.get("updated_at") or e.get("created_at")))
            L.append("")

    if meta.get("url"):
        L += ["---", "", "原文链接：%s" % meta["url"], ""]
    return "\n".join(L).rstrip() + "\n"


def _kind_label(k):
    return {"highlight": "划线", "bold": "加粗", "underline": "下划线",
            "strike": "删除线", "note": "批注"}.get(k, "划线")


def export_notes(book_dir, doc, meta=None, titles=None, book_label=""):
    """写 notes.md 到这本书的目录里，返回路径。"""
    txt = export_markdown(doc, meta, titles, book_label)
    p = os.path.join(book_dir, EXPORT_MD)
    _write_text(p, txt)
    return {"path": p, "chars": len(txt)}


def _write_text(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


# ─────────────────────────── 模板 ───────────────────────────
# 模板 = 一篇条目的初稿。占位符只有 {title} {author} {chapter} {text} {date}，
# 认不出的花括号原样留着，免得用户写个 JSON 片段就被吃掉。

OFFICIAL_TEMPLATES = [
    {"id": "official-quote", "name": "划线卡片", "desc": "一句原文 + 为什么打动我 + 我打算怎么用",
     "body": "**原文**：{text}\n\n**出处**：{title}　{chapter}\n\n"
             "**为什么打动我**：\n\n- \n\n**我能怎么用**：\n\n- \n\n"
             "**关联**：\n\n- \n"},
    {"id": "official-feynman", "name": "费曼四步", "desc": "用自己的话复述，讲不清的地方就是没懂",
     "body": "### 一、它是什么（用大白话说）\n\n\n### 二、它怎么运作（关键机制）\n\n\n"
             "### 三、举个例子\n\n\n### 四、我哪里还没懂\n\n- \n"},
    {"id": "official-review", "name": "章节复盘", "desc": "读完一章后的四问，30 秒填完",
     "body": "**这一章讲了什么**：\n\n**最有用的一句**：{text}\n\n"
             "**我不同意 / 存疑的地方**：\n\n- \n\n**下一步做什么**：\n\n- [ ] \n"},
    {"id": "official-qa", "name": "问答卡", "desc": "把划线变成一个能背的问题",
     "body": "**问题**：\n\n**我的答案**：\n\n**原文依据**：{text}\n\n**还需要查**：\n\n- \n"},
    {"id": "official-blank", "name": "空白笔记", "desc": "什么都不预设，直接写",
     "body": ""},
]


def templates(cache_dir):
    """官方模板 + 用户自定义模板，官方的排前面、不可删。"""
    return ([_tpl_pub(t, True) for t in OFFICIAL_TEMPLATES]
            + [_tpl_pub(t, False) for t in _custom_list(cache_dir)])


def _tpl_pub(t, official):
    return {"id": t.get("id") or "", "name": t.get("name") or "未命名模板",
            "desc": t.get("desc") or "", "body": t.get("body") or "",
            "official": bool(official)}


def _custom_path(cache_dir):
    return os.path.join(cache_dir, TPL_FILE)


def _custom_list(cache_dir):
    p = _custom_path(cache_dir)
    if not os.path.exists(p):
        return []
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    items = data.get("items") if isinstance(data, dict) else data
    return [t for t in (items or []) if isinstance(t, dict) and t.get("id")]


def save_template(cache_dir, tpl):
    """新增或覆盖一条自定义模板；id 交给官方的名字时自动避让。"""
    if not isinstance(tpl, dict):
        return {"ok": False, "msg": "模板格式不对"}
    name = _clip(tpl.get("name"), 60) or "未命名模板"
    body = _clip(tpl.get("body"), 20000)
    tid = _clip(tpl.get("id"), 60)
    if not tid or tid.startswith("official-"):
        tid = "user_" + uuid.uuid4().hex[:8]
    items = _custom_list(cache_dir)
    hit = next((t for t in items if t.get("id") == tid), None)
    if hit:
        hit.update({"name": name, "desc": _clip(tpl.get("desc"), 200), "body": body,
                    "updated_at": int(time.time())})
    else:
        items.append({"id": tid, "name": name, "desc": _clip(tpl.get("desc"), 200),
                      "body": body, "created_at": int(time.time()),
                      "updated_at": int(time.time())})
    os.makedirs(cache_dir, exist_ok=True)
    _write_json(_custom_path(cache_dir), {"items": items[:200]})
    return {"ok": True, "id": tid, "msg": "模板存好了" if not hit else "模板已更新"}


def delete_template(cache_dir, tid):
    tid = _clip(tid, 60)
    if tid.startswith("official-"):
        return {"ok": False, "msg": "官方模板不能删"}
    items = _custom_list(cache_dir)
    left = [t for t in items if t.get("id") != tid]
    if len(left) == len(items):
        return {"ok": False, "msg": "没找到这个模板"}
    _write_json(_custom_path(cache_dir), {"items": left})
    return {"ok": True, "msg": "删掉了"}


def get_template(cache_dir, tid):
    tid = _clip(tid, 60)
    for t in OFFICIAL_TEMPLATES:
        if t["id"] == tid:
            return t
    for t in _custom_list(cache_dir):
        if t.get("id") == tid:
            return t
    return None


def apply_template(tpl, ctx=None):
    """占位符替换。上下文里没有的占位符留空，不报错。"""
    ctx = ctx or {}
    body = (tpl or {}).get("body") or ""
    def sub(m):
        return str(ctx.get(m.group(1)) or "")
    out = re.sub(r"\{(title|author|chapter|text|date|url)\}", sub, body)
    head = "> %s　·　%s\n\n" % (_clip(ctx.get("title"), 120) or "这本书",
                              _ts(time.time())[:10])
    return (head + out) if out.strip() else head.strip()


# ─────────────────────────── 思维导图 ───────────────────────────
# 左到右树形布局，纯字符串画 SVG：不依赖浏览器，命令行和 MCP 也能出图。
# 数据从笔记来 —— 中心是书名，一级分支可以按「语义标签」或「章节」切，
# 底下再挂笔记条目；标签色就是界面上那支高亮笔的颜色，看图等于复习。

_LEAF_PER_BRANCH = 14        # 一个分支最多画几片叶子，多了图就没法看了
_WRAP_LEAF = 16              # 叶子每行几个字（中文按字宽算）
_WRAP_NODE = 10
_ROW = 17.0                  # 行高
_PAD_X, _PAD_Y = 11.0, 8.0   # 文字到框边的留白
_COL_GAP, _TOP_PAD = 46.0, 20.0


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _wrap(text, width):
    """按字宽折行，最多三行 —— 思维导图中途截断比把框撑到屏幕外好。"""
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return ["…"]
    lines = [text[i:i + width] for i in range(0, len(text), width)][:3]
    return lines


def _node(label, kids=None, color=None):
    return {"label": label, "kids": kids or [], "color": color}


def _headings(body):
    """条目正文里的标题行 → 子分支（一到三级，去掉 Markdown 记号）。"""
    out = []
    for ln in (body or "").split("\n"):
        m = re.match(r"^\s{0,3}(#{1,3})\s+(.+?)\s*$", ln)
        if m:
            out.append(_clip(m.group(2), 40))
        if len(out) >= 6:
            break
    return out


def note_tree(doc, meta=None, titles=None, book_label="", cut="tag"):
    """笔记 → 树。cut 决定一级分支怎么切：tag（语义标签）/ chapter（章节）/ entry（只看条目）。"""
    meta = meta or {}
    titles = titles or {}
    marks = doc.get("marks") or []
    entries = doc.get("entries") or []
    root = _node(book_label or meta.get("title") or "这本书")

    if cut == "entry":
        for e in entries[:_LEAF_PER_BRANCH * 2]:
            kids = [_node(h) for h in _headings(e.get("body"))]
            root["kids"].append(_node(_clip(e.get("title") or "无标题", 40), kids))
        return root

    groups = {}
    for m in marks:
        if cut == "chapter":
            key = titles.get(m.get("ch")) or (m.get("ch") or "未定位")
            color = "#b9c8d8"
        else:
            t = TAG_BY_KEY.get(m.get("tag"))
            key = t["name"] if t else "未标注"
            color = t["color"] if t else "#d8cfbe"
        g = groups.setdefault(key, {"color": color, "items": []})
        g["items"].append(m)
    order = [t["name"] for t in TAGS] if cut != "chapter" else list(groups)
    keys = [k for k in order if k in groups] + [k for k in groups if k not in order]
    for k in keys[:12]:
        g = groups[k]
        leaves = []
        for m in g["items"][:_LEAF_PER_BRANCH]:
            memo = _clip(m.get("memo"), 40)
            txt = _clip(m.get("text"), 60)
            leaves.append(_node(txt + ("　→ " + memo if memo else ""), color=g["color"]))
        if len(g["items"]) > len(leaves):
            leaves.append(_node("还有 %d 条" % (len(g["items"]) - len(leaves)), color=g["color"]))
        root["kids"].append(_node(k, leaves, color=g["color"]))

    if entries:
        es = [_node(_clip(e.get("title") or "无标题", 40),
                    [_node(h) for h in _headings(e.get("body"))])
              for e in entries[:_LEAF_PER_BRANCH]]
        root["kids"].append(_node("笔记条目", es, color="#8fb7a6"))
    return root


def _size(node):
    """这个框要多大：宽按最长一行字算（中文≈字号），高按行数算。"""
    lines = _wrap(node["label"], _WRAP_NODE if node["kids"] else _WRAP_LEAF)
    wide = max(len(x) for x in lines)
    node["_lines"] = lines
    node["_w"] = min(300.0, _PAD_X * 2 + wide * 13.2)
    node["_h"] = _PAD_Y * 2 + _ROW * len(lines)


def _layout(node, depth, y, cols):
    """递归排版：叶子按顺序往下堆，父节点落在首末子节点中点 —— 连线不交叉。"""
    _size(node)
    node["_d"] = depth
    while len(cols) <= depth:
        cols.append(0.0)
    cols[depth] = max(cols[depth], node["_w"])
    if not node["kids"]:
        node["_y"] = y + node["_h"] / 2.0
        return y + node["_h"] + 10.0
    cy = y
    for k in node["kids"]:
        cy = _layout(k, depth + 1, cy, cols)
    node["_y"] = (node["kids"][0]["_y"] + node["kids"][-1]["_y"]) / 2.0
    return cy


def _all_nodes(node):
    yield node
    for k in node["kids"]:
        yield from _all_nodes(k)


def build_mindmap_svg(tree, title=""):
    """树 → 一段自足 SVG 字符串（可直接存成 .svg，也能塞进 md 里读）。"""
    cols = []
    _layout(tree, 0, 0.0, cols)
    nodes = list(_all_nodes(tree))
    top = min(n["_y"] - n["_h"] / 2.0 for n in nodes)
    bottom = max(n["_y"] + n["_h"] / 2.0 for n in nodes)
    for n in nodes:
        n["_y"] = n["_y"] - top + _TOP_PAD          # 统一抬进画布，框和线用同一套坐标
    col_x = [_PAD_X]
    for d in range(len(cols)):
        col_x.append(col_x[-1] + (cols[d] if cols[d] else 120.0) + _COL_GAP)
    deepest = max(n["_d"] for n in nodes)
    width = col_x[deepest] + max(n["_w"] for n in nodes if n["_d"] == deepest) + _PAD_X * 2
    height = bottom - top + _TOP_PAD * 2 + (22 if title else 0)
    ink0 = "#2f2a24"

    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="%d" height="%d" '
           'viewBox="0 0 %d %d" font-family="PingFang SC, Hiragino Sans GB, '
           'Microsoft YaHei, sans-serif">' % (width, height, width, height),
           '<rect width="%d" height="%d" fill="#fbf7ef"/>' % (width, height)]
    if title:
        out.append('<text x="%.1f" y="%.1f" font-size="13" fill="#8b8171">%s</text>'
                   % (_PAD_X, _TOP_PAD - 4, _esc(title)[:60]))
    for n in nodes:
        cx, cy = col_x[n["_d"]], n["_y"]
        for k in n["kids"]:
            x1, x2 = cx + n["_w"], col_x[k["_d"]]
            y1, y2 = cy, k["_y"]
            stroke = k.get("color") or n.get("color") or "#c9bfab"
            out.append('<path d="M%.1f %.1f C%.1f %.1f %.1f %.1f %.1f %.1f" fill="none" '
                       'stroke="%s" stroke-width="1.6" stroke-linecap="round"/>'
                       % (x1, y1, x1 + 20, y1, x2 - 20, y2, x2, y2, stroke))
    for n in nodes:
        cx, cy = col_x[n["_d"]], n["_y"]
        is_root = n["_d"] == 0
        fill = "#2f2a24" if is_root else (n.get("color") or "#fffdf8")
        ink = "#f7f2e7" if is_root else "#2f2a24"
        out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="%d" fill="%s" '
                   'stroke="%s" stroke-width="1.2"/>'
                   % (cx, cy - n["_h"] / 2.0, n["_w"], n["_h"],
                      12 if is_root else 9, fill, "#d8cfbe" if not is_root else fill))
        ty = cy - n["_h"] / 2.0 + _PAD_Y + 12.0
        for ln in n["_lines"]:
            out.append('<text x="%.1f" y="%.1f" font-size="13" fill="%s">%s</text>'
                       % (cx + _PAD_X, ty, ink, _esc(ln)))
            ty += _ROW
    out.append("</svg>")
    return "\n".join(out)


def mindmap(book_dir, doc, meta=None, titles=None, book_label="", cut="tag"):
    """生成思维导图并把 SVG 写进这本书的目录，返回路径与规模。"""
    tree = note_tree(doc, meta, titles, book_label, cut)
    svg = build_mindmap_svg(tree, book_label or (meta or {}).get("title") or "")
    p = os.path.join(book_dir, EXPORT_MAP)
    _write_text(p, svg)
    return {"path": p, "bytes": len(svg.encode("utf-8")), "nodes": _count(tree),
            "branches": len(tree["kids"])}


def _count(node):
    return 1 + sum(_count(k) for k in node["kids"])
