# -*- coding: utf-8 -*-
r"""可编辑导图「存一份图片」出的 SVG 必须是合法 XML：XML 1.0 收不下的字符一个都不许溜进去。

为什么单开一份（`check_mindmap.py` 已经钉过四种形态、转义与坏输入不抛）
    那一份钉的是「`< > & " '` 变成实体」，也就是 XML 的**转义**；它没有钉
    XML 的**字符集**。XML 1.0 的 Char 产生式只认：

        #x9 | #xA | #xD | [#x20-#xD7FF] | [#xE000-#xFFFD] | [#x10000-#x10FFFF]

    落在这几段外面的字符**没有合法写法**，转义也救不回来。而 `/api/mindmap?mode=svg`
    出的正是「能直接发给别人的单文件」——前端拿到 `svg` 直接下载成 `.svg`。节点文字 /
    标题 / 连线标签都从手改的 mindmap.json、粘贴的正文、AI 画图三条路进来，`json` 的
    `"\u0001"` / `"\uFFFF"` 转义读得回来，旧写法（只摘落单的代理码点）一个都不管它们：
    文件照写不误、这边回 `ok:true`，用户打开却是「不是合法 XML」。

    同一条路在 `book_notes.build_mindmap_svg`（笔记那一侧合成的只读图）上还有一份，
    本份只管 `mindmap.py`（另一份有自己的门禁，改那边要另开一条）。

钉住五件事（纯函数 + 标准库 ElementTree 真解析，不起服务、不联网、不开浏览器）：
  A 判据就是 Char 产生式的补集：C0 / DEL / U+FFFE / U+FFFF / 落单代理码点全洗掉，
    合法的 \t \n \r、U+FFFD、C1、emoji、U+10FFFF 一个字不改
  B normalize 这一关就把四类入口（标题 / 节点文字 / 备注 / 连线标签）洗一遍，
    洗掉的量不谎报成 dropped（与半个代理码点同一档：坏输入，不是「裁掉了内容」）
  C 四种形态的 render_svg 都过一遍 ElementTree.fromstring（真解析，不是查子串）
  D 对照：旧口径（只摘代理码点）在同一份输入上确实留下非法字符、XML 解不动 ——
    免得这条门禁哪天变成空转
  E 转义那一半没被顺手改坏（`< > & " '` 照旧成实体）/ 坏输入不抛 / 仍幂等
"""
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import mindmap as mm  # noqa: E402

FAIL = []
CHECKS = []


def chk(name, cond, extra=""):
    CHECKS.append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:300]))
    if not cond:
        FAIL.append(name)


# XML 1.0 的 Char 产生式（BMP 里除代理码点外，只额外排掉 U+FFFE / U+FFFF）
ILLEGAL = ["\x00", "\x01", "\x08", "\x0b", "\x0c", "\x0e", "\x1f", "\x7f",
           "\ud800", "\udfff", "\ufffe", "\uffff"]
LEGAL = ["\t", "\n", "\r", "\x20", "\x7e", "\x80", "\x85", "\ud7ff", "\ue000",
         "\ufffd", "\U00010000", "\U0001f600", "\U0010ffff"]


def dirty_text():
    """一段四类坏字符齐全的内容（前后都垫上正常字，`.strip()` 不会把它们吃掉）。"""
    return "前" + "".join(ILLEGAL) + "后"


def dirty_doc():
    return {
        "title": "标\uFFFF题\x01",
        "form": "tree",
        "created": "2026-01-01T00:00:00",
        "updated": "2026-01-01T00:00:00",
        "nodes": [
            {"id": "ro\x01ot", "text": dirty_text(), "note": "备注\x00\ufffe",
             "children": [
                 {"id": "kid", "text": "子\x00节点\ufffe", "children": []},
             ]},
            {"id": "tail", "text": "尾\x7f巴", "children": []},
        ],
        "links": [{"id": "l\x01ink", "from": "ro\x01ot", "to": "tail",
                   "text": "标\x01签\uffff"}],
    }


# ── A. 判据 = Char 产生式的补集 ────────────────────────────────────────
for ch in ILLEGAL:
    chk("A 判据洗掉 %s" % repr(ch), mm._XML_ILLEGAL.search("a%s b" % ch) is not None, ch)
for ch in LEGAL:
    chk("A 判据留下 %s" % repr(ch), mm._XML_ILLEGAL.search("a%s b" % ch) is None, ch)
chk("A 对照：旧口径（只摘代理码点）量不到 C0 / DEL / U+FFFE / U+FFFF",
    all(mm.SURROGATE.search("a%s b" % ch) is None for ch in ILLEGAL if ord(ch) < 0xD800)
    and mm.SURROGATE.search("a\ud800b") is not None, None)


# ── B. normalize 把四类入口都洗一遍，且不谎报 dropped ──────────────────
DIRTY = mm.normalize(dirty_doc())
CLEAN_CONTROL = mm.normalize({
    "title": "标題", "nodes": [{"id": "root", "text": "前后", "note": "备注",
                             "children": [{"id": "kid", "text": "子节点", "children": []}]},
                             {"id": "tail", "text": "尾巴", "children": []}],
    "links": [{"id": "link", "from": "root", "to": "tail", "text": "标签"}]})
raw = json.dumps(DIRTY, ensure_ascii=False)
chk("B 落盘用的那份 JSON 里一个非法字符都不剩",
    not mm._XML_ILLEGAL.search(raw), [c for c in ILLEGAL if c in raw])
chk("B 标题 / 节点文字 / 备注 / 连线标签四类入口都洗到",
    mm._XML_ILLEGAL.search(DIRTY["title"]) is None
    and all(mm._XML_ILLEGAL.search(n["text"]) is None for n in mm.walk({"nodes": DIRTY["nodes"]}))
    and all(mm._XML_ILLEGAL.search(n["note"] or "") is None
            for n in mm.walk({"nodes": DIRTY["nodes"]}))
    and all(mm._XML_ILLEGAL.search(l["text"]) is None for l in DIRTY["links"]), DIRTY)
chk("B 洗掉坏字符不谎报 dropped（与半个代理码点同一档：坏输入，不是裁了内容）",
    DIRTY["dropped"] == CLEAN_CONTROL["dropped"] == 0, (DIRTY["dropped"], CLEAN_CONTROL["dropped"]))
chk("B 仍幂等（洗过再洗一次全等）", mm.normalize(DIRTY) == DIRTY, None)
chk("B 连线两端跟着 id 一起洗，链接没被误删（from/to 与节点 id 仍对得上）",
    len(DIRTY["links"]) == 1 and DIRTY["links"][0]["from"] == DIRTY["nodes"][0]["id"]
    and DIRTY["links"][0]["to"] == "tail", DIRTY["links"])


# ── C. 四种形态的 SVG 都真解一遍 ──────────────────────────────────────
PARSE = {}
for form, name in mm.FORMS:
    svg = mm.render_svg(DIRTY, form)
    try:
        ET.fromstring(svg)
        PARSE[form] = ""
    except ET.ParseError as exc:
        PARSE[form] = str(exc)
    chk("C %s：render_svg 出的字符串是合法 XML（ElementTree 真解析）" % name,
        not PARSE[form], (PARSE[form], svg[:120]))
    chk("C %s：仍是自足的一份 SVG（头尾标签、xmlns、viewBox 都在）" % name,
        svg.startswith("<svg ") and svg.rstrip().endswith("</svg>")
        and 'xmlns="http://www.w3.org/2000/svg"' in svg and "viewBox=" in svg, svg[:90])
chk("C 四种形态都过了解析（不是只测了默认那一种）",
    all(not v for v in PARSE.values()) and set(PARSE) == {k for k, _ in mm.FORMS}, PARSE)

# 合法字符不许被顺手洗掉：真进到 SVG 的文字里（挑 \x9f 这个 C1，它不像 \x85 被排版的
# 折行器当空白吃掉，能一路走到 <text> 里）
LEGAL_DOC = mm.normalize({
    "title": "标题\U0001f600",
    "nodes": [{"id": "root", "text": "正文\ufffd\u009f\U0001f600中文\ufffd",
               "children": [{"id": "kid", "text": "子\U0001f600", "children": []}]}],
    "links": [{"id": "l1", "from": "root", "to": "kid", "text": "标签\ufffd"}]})
LEGAL_SVG = mm.render_svg(LEGAL_DOC, "tree")
chk("C 合法的 U+FFFD / C1 / emoji / 中文照旧进 SVG",
    "\ufffd" in LEGAL_SVG and "\u009f" in LEGAL_SVG and "\U0001f600" in LEGAL_SVG
    and "中文" in LEGAL_SVG, LEGAL_SVG[:80])
try:
    ET.fromstring(LEGAL_SVG)
    _legal_ok = True
except ET.ParseError as _exc:      # pragma: no cover - 正常路径不会走到
    _legal_ok = False
chk("C 合法输入那份也解得动（门禁不是只会红）", _legal_ok, None)


# ── D. 对照：旧口径确实留下非法字符、XML 解不动 ────────────────────────
_OLD_ESC = (lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))
_OLD_SVG = ('<svg xmlns="http://www.w3.org/2000/svg"><text>%s</text></svg>'
            % _OLD_ESC(mm.SURROGATE.sub("", dirty_text())))
_old_err = ""
try:
    ET.fromstring(_OLD_SVG)
except ET.ParseError as exc:
    _old_err = str(exc)
chk("D 对照：旧口径（只摘代理码点）留下来的 C0 / DEL / U+FFFE / U+FFFF 让 XML 解不动",
    bool(_old_err), (_old_err, _OLD_SVG[:120]))
chk("D 对照：那几个字符确实还留在旧口径的产物里（不是输入本身就没有）",
    all(c in _OLD_SVG for c in ("\x01", "\x7f", "\uffff")), None)


# ── E. 转义那一半没被改坏 / 坏输入不抛 ────────────────────────────────
ESC = mm.render_svg(mm.normalize(
    {"title": "转义", "nodes": [{"id": "x1", "text": '<b>&"危险" & <i>', "children": []}]}))
chk("E 转义：尖括号 / & / 引号照旧成实体，SVG 里没有裸 <b>",
    "<b>" not in ESC and "&lt;b&gt;" in ESC and "&amp;" in ESC and "危险" in ESC, ESC[:90])
chk("E 坏输入（None / 非字符串）照旧不抛",
    mm.render_svg(None).rstrip().endswith("</svg>")
    and mm.render_svg({"title": 123, "nodes": [{"id": None, "text": [], "children": []}]})
    .rstrip().endswith("</svg>"), None)


# ── 收摊 ───────────────────────────────────────────────────────────
print()
print("跑了 %d 条断言" % len(CHECKS))
if FAIL:
    print("失败 %d 项：%s" % (len(FAIL), "、".join(FAIL)))
    sys.exit(1)
print("全部通过")
