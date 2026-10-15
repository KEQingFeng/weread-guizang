#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「视频转笔记」顶部那两扇门（哔哩哔哩 / YouTube）的接线体检（Issue #118）。

用户想要的是「先逛再转」：想找知识型的视频时，不必跳出归藏另开浏览器，从视频转笔记
这一屏最上面右侧就能点进去，在归藏自己的内置浏览器里逛，逛到想转的那条再贴链接。

这条门禁守的是「门真的开在那一层、走的是同一套内置浏览器」，不是另起一个弹窗或跳系统
浏览器 —— 后者正是需求原话里想避免的（「打开浏览器再进行」）。项目里「在浏览器里打开
外站」只有一层：`openBrowse()` → `browse` 这一屏 → `/api/browse`（见开发规范 §3
「内置浏览器只此一层」）。所以：

  ① 名册：`VT_PORTALS` 就两条（哔哩哔哩 / YouTube），地址是 https 的外站；
  ② 入口在顶部右侧：`renderVideoView` 的 `viewHeader('视频转笔记', …)` 第二参里渲染这两颗；
  ③ 接线：`wireVideo` 把每一颗接给 `openBrowse(该地址)`，且不许出现 `window.open` /
     `location.href` 这类「跳出去」的写法；
  ④ 复用：`openBrowse` 自身仍落进 `browse` 那一屏、地址仍交给 `/api/browse`；
  ⑤ 跨层：两个地址交给 `web_browse.normalize` 不被拦（内置浏览器那一层真兜得住）。

纯文件走查 + 一个离线纯函数，不起服务、不联网、不开浏览器，归 --fast 静态段。
"""
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import web_browse as wb  # noqa: E402  —— 只验地址过不过内置浏览器那把尺子，不联网

SRC = (HERE.parent / "ui.html").read_text(encoding="utf-8")

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


def body_of(name):
    """取某个顶层 function 的函数体（到下一个顶格 `}` 为止，够这里用）。"""
    m = re.search(r"function %s\([^)]*\)\s*\{(.*?)\n\}" % re.escape(name), SRC, re.S)
    return m.group(1) if m else None


# ── ① 名册：两条，都是 https 外站 ──────────────────────────────
m = re.search(r"const VT_PORTALS\s*=\s*\[(.*?)\];", SRC, re.S)
chk("找到 VT_PORTALS（顶部两扇门的唯一名册）", bool(m), m)
portals = []
if m:
    portals = re.findall(r"\[\s*'([^']+)'\s*,\s*'([^']+)'\s*,\s*'([^']+)'\s*\]", m.group(1))
chk("名册正好两条（哔哩哔哩 / YouTube）", len(portals) == 2, portals)

ids = [p[0] for p in portals]
names = [p[1] for p in portals]
urls = [p[2] for p in portals]
chk("两条各有一个唯一的 id（按钮与接线靠它对上）",
    len(set(ids)) == 2 and all(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", i) for i in ids), ids)
chk("标题是「哔哩哔哩」与「YouTube」", set(names) == {"哔哩哔哩", "YouTube"}, names)
chk("地址都是 https 外站",
    all(u.startswith("https://") for u in urls), urls)
chk("地址分别落在 bilibili.com 与 youtube.com",
    sorted(u.split("/")[2] for u in urls) == ["www.bilibili.com", "www.youtube.com"], urls)

# 项目界面零 emoji（开发规范 §4）：这两颗新按钮的文字里也不许夹一个。
EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u2B00-\u2BFF]")
chk("两颗按钮的文字里没有 emoji", not any(EMOJI.search(t) for t in names + ids), names)

# ── ② 入口在「视频转笔记」顶部右侧 ─────────────────────────────
rv = body_of("renderVideoView")
chk("找到 renderVideoView", bool(rv))
if rv:
    head_m = re.search(r"viewHeader\(\s*'视频转笔记'\s*,(.*?),\s*'video'\s*\)", rv, re.S)
    chk("顶部表头是 viewHeader('视频转笔记', …)（右侧第二参）", bool(head_m),
        rv[:200])
    if head_m:
        extra = head_m.group(1)
        chk("表头右侧渲染了名册（${portals}）", "${portals}" in extra, extra[:200])
        chk("表头右侧仍留着原来那颗「打开这一格的文件夹」", 'id="vOpen"' in extra, extra[:200])
    chk("renderVideoView 里用 VT_PORTALS 生成了那两颗按钮",
        "VT_PORTALS.map" in rv, rv[:200])
    # 这扇门不许绕开内置浏览器跳出去 —— 需求要的正是「不另开浏览器」。
    chk("renderVideoView 里没有 window.open / location.href 这类跳出去的写法",
        "window.open" not in rv and "location.href" not in rv, rv[:200])

# ── ③ 接线：每一颗都接给 openBrowse(该地址) ────────────────────
wv = body_of("wireVideo")
chk("找到 wireVideo", bool(wv))
if wv:
    chk("wireVideo 里遍历同一份名册接线", "VT_PORTALS.forEach" in wv, wv[:400])
    chk("每一颗接给 openBrowse(url)（走共用的内置浏览器）",
        "openBrowse(url)" in wv, wv[:400])
    chk("接线里没有 window.open / location.href 这类跳出去的写法",
        "window.open" not in wv and "location.href" not in wv, wv[:400])
    # 用名册里的 id 找按钮，而不是另抄一遍选择器。
    chk("用名册里的 id 找按钮（'#' + id）", "'#' + id" in wv, wv[:400])

# ── ④ 复用：openBrowse 仍旧落进内置浏览器那一屏 ────────────────
ob = body_of("openBrowse")
chk("找到 openBrowse（全站打开外站的唯一入口）", bool(ob), ob)
if ob:
    chk("openBrowse 走 setView('browse')（不是新窗口）",
        "setView('browse')" in ob, ob)
chk("内置浏览器那一屏的地址交给 /api/browse 取回来",
    "'/api/browse?url='" in SRC, None)
chk("AI 导航那颗钮与视频页这两颗走的是同一个 openBrowse",
    "openBrowse(BZ_HOME)" in SRC, None)

# ── ④b 对照：改前那份源码喂进同两条判据必须不通过 ──────────────
# 没有这一段，上面「表头里有 ${portals}」「接线里有 VT_PORTALS.forEach」可能只是
# 在别的字符串上碰巧为真（例如注释里提过一句）。拿改前的真实写法当夹具比一遍。
_OLD_HEAD = ("viewHeader('视频转笔记', `<button class=\"deed\" id=\"vOpen\" "
             "style=\"height:30px\">打开这一格的文件夹</button>`, 'video')")
_m_old = re.search(r"viewHeader\(\s*'视频转笔记'\s*,(.*?),\s*'video'\s*\)", _OLD_HEAD, re.S)
chk("对照：改前的表头能匹配同一条正则、但没有 ${portals}",
    bool(_m_old) and "${portals}" not in _m_old.group(1), _m_old)
_OLD_WIRE = ("const open = p.querySelector('#vOpen');\n"
             "if (open) open.onclick = () => post('openbooks', {module: 'video'});")
chk("对照：改前的接线里没有 VT_PORTALS.forEach，也提不到 openBrowse(url)",
    "VT_PORTALS.forEach" not in _OLD_WIRE and "openBrowse(url)" not in _OLD_WIRE,
    _OLD_WIRE)

# ── ⑤ 跨层：两个地址内置浏览器那一层真兜得住 ──────────────────
for u in urls:
    try:
        out = wb.normalize(u)
        chk("内置浏览器收得下 %s" % u, out == u, out)
    except wb.Blocked as e:
        chk("内置浏览器收得下 %s" % u, False, "被拦：%s" % e)

# ── 汇总 ────────────────────────────────────────────────────────
bad = [n for ok, n, _ in checks if not ok]
print()
print("视频转笔记顶部入口接线体检：通过 %d 项，失败 %d 项" % (len(checks) - len(bad), len(bad)))
for n in bad:
    print("  ✗ " + n)
sys.exit(1 if bad else 0)
