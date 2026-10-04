"""写作平台：一篇稿子怎么存在磁盘上、怎么统计、怎么出去、怎么让 AI 搭手。

**一篇稿子就是「一本书」，住在书库的第七格里。**

    书库/写作/write_1a2b3c4d5e6f/
        draft.md        正文（唯一真源）
        meta.json       标题 / 字数 / rev / 时间 / 已看过的灵感角度
        versions/       快照，回退用（保留最近 30 份）

这么定不是为了省事，是为了**不再多学一套东西**：目录、分类账（夹子、标签、状态）、
封面、AI 小结、清理白名单、MCP 那一整批工具、`safe_book_dir` 那道路径闸，全都按
「书库里一个书号」在工作。稿子既然也是书号，这些能力一行接口都不用改就全跟着走 ——
1.0.1 那次「换目录不换 id」攒下的红利，这一轮继续吃。

三条要紧的规矩写在代码里，改之前先看懂：

  · **存盘认 rev，不认时间戳。** 编辑器有多个入口（界面、MCP、命令行改文件），
    拿时间比大小会出现「后写的被先写的覆盖」。每次成功保存 rev +1，写的时候带上
    「我读到的那个 rev」，对不上就不写、把服务端那份带回去让人选。
  · **快照只在真改动之后拍。** 空转的自动存盘（用户没打字、只是切了下标签页）
    不许拍快照，否则 30 个位置全被同一段内容占满，回退等于没有。
  · **给 AI 的笔记原文只进提示词，不进日志、不进回执。** 素材是从便签与划线里
    捞的用户私货，落到进展栏就等于落进仓库。
"""

import hashlib
import os
import re
import time
import uuid

import book_layout

MODULE = "write"
PREFIX = "write_"
DRAFT_NAME = "draft.md"
REV_STEP_SNAP = 20        # 每存这么多次拍一份快照
KEEP_VERSIONS = 30        # 快照最多留这么多份

# 「AI 接着写多少」由用户点选，不让模型自己决定 —— 各家模型对「继续写一段」的
# 理解差得太远，有人写 40 字有人写 800 字，只有把它换成明确的 max_tokens + stop
# 才拿得住。文案给的是「一句 / 一段 / 三个要点 / 写完这一节」。
LENGTHS = [
    ("sent", "一句", 200, ["\n"]),
    ("para", "一段", 520, ["\n\n"]),
    ("points", "三个要点", 700, ["\n\n\n"]),
    ("section", "写完这一节", 1400, ["\n## ", "\n# "]),
]
LENGTH_OF = {k: (label, tokens, stop) for k, label, tokens, stop in LENGTHS}


# ---------- 路径与 id ----------

def new_id():
    """新稿号：前缀 + 12 位十六进制。前缀让 book_layout 靠前缀也能认出模块。"""
    return PREFIX + uuid.uuid4().hex[:12]


def ok_id(book_id):
    return book_layout.ok_id(book_id) and str(book_id).startswith(PREFIX)


def dir_of(books_root, book_id):
    """这篇稿子的目录（会顺手把 书库/写作/ 建出来，但稿子目录要 create 才建）。"""
    return os.path.join(book_layout.book_dir(books_root, MODULE), str(book_id))


def draft_path(draft_dir):
    return os.path.join(draft_dir, DRAFT_NAME)


def meta_path(draft_dir):
    return os.path.join(draft_dir, "meta.json")


def versions_dir(draft_dir):
    return os.path.join(draft_dir, "versions")


# ---------- 统计 ----------

_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]*")
_DIGIT_RUN = re.compile(r"\d+(?:[.,]\d+)*")
_MD_NOISE = re.compile(r"^\s*(?:#{1,6}\s*|[-*+]\s+|\d+[.)]\s+|>\s*)", re.M)


def words(text):
    """字数统计。中文按字、英文按词、数字按组，这是写作软件通用的口径。

    给的是三个数：`total` 是界面上那个「字数」（中文字 + 英文词 + 数字组），
    `han` 与 `latin` 拆开留着，将来要按语种算字数（中译英、限 800 字）不用再改接口。
    先剥掉 markdown 的行首记号，否则「## 标题」会被算进字数。
    """
    body = _MD_NOISE.sub("", text or "")
    han = len(_HAN.findall(body))
    latin = len(_LATIN_WORD.findall(_HAN.sub(" ", body)))
    digit = len(_DIGIT_RUN.findall(body))
    return {"han": han, "latin": latin, "numbers": digit,
            "total": han + latin + digit, "chars": len(text or "")}


def outline(text):
    """大纲：把 # 行按出现顺序列出来，给侧边目录与「跳到这一节」。"""
    out = []
    for i, line in enumerate((text or "").splitlines(), start=1):
        m = re.match(r"^(#{1,6})\s+(.*\S)\s*$", line)
        if m:
            out.append({"level": len(m.group(1)), "title": m.group(2).strip(), "line": i})
    return out


def title_from(text, fallback="未题名"):
    """标题兜底：先认第一行 `#`，再认第一个非空行的前 24 字。"""
    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        m = re.match(r"^#{1,6}\s+(.*\S)$", s)
        if m:
            return m.group(1).strip()[:80]
        return s[:24]
    return fallback


# ---------- 读写 ----------

def blank_meta(book_id, title=""):
    now = int(time.time())
    return {"id": book_id, "title": title or "", "source": MODULE, "format": MODULE,
            "created": now, "updated": now, "rev": 0, "words": 0, "chars": 0,
            "sparks": [], "done": True}


def load_meta(draft_dir):
    m = book_layout.read_meta(draft_dir)
    if not m or m.get("format") != MODULE:
        return None
    m.setdefault("rev", 0)
    m.setdefault("sparks", [])
    m.setdefault("title", "")
    m.setdefault("words", 0)
    m.setdefault("created", int(os.path.getmtime(meta_path(draft_dir))))
    m.setdefault("updated", m["created"])
    return m


def read_text(draft_dir):
    try:
        with open(draft_path(draft_dir), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def read(books_root, book_id):
    """整篇读出来：元信息 + 正文 + rev。没有这篇稿子给 None（调用方说人话）。"""
    if not ok_id(book_id):
        return None
    d = dir_of(books_root, book_id)
    m = load_meta(d)
    if not m:
        return None
    m["text"] = read_text(d)
    m["outline"] = outline(m["text"])
    m["count"] = words(m["text"])
    return m


def create(books_root, title="", text=""):
    book_id = new_id()
    d = dir_of(books_root, book_id)
    if os.path.exists(d):
        return None
    os.makedirs(versions_dir(d), exist_ok=True)
    meta = blank_meta(book_id, title or title_from(text))
    body = text if text else ""
    with open(draft_path(d), "w", encoding="utf-8") as f:
        f.write(body)
    meta["rev"] = 1
    meta["words"] = words(body)["total"]
    _write_meta(d, meta)
    return book_id, meta


def save(books_root, book_id, text, title=None, rev=None):
    """存正文。返回 (ok, meta 或 冲突信息)。

    `rev` 给的是「我读到的那一份的第几版」。对不上就**不写**，把服务端当前正文一起
    带回去 —— 冲突要当场可见，不能悄悄覆盖，这是编辑器最容易丢稿的一种做法。
    不给 rev（老接口、MCP 直存）时按「覆盖」处理，但仍会先拍一份快照，改坏了能回退。
    """
    d = dir_of(books_root, book_id)
    meta = load_meta(d)
    if not meta:
        return False, {"msg": "这篇稿子不在了"}
    server = int(meta.get("rev", 0))
    conflict = None
    if rev is not None:
        try:
            client = int(rev)
        except Exception:
            client = server
        if client != server:
            conflict = {"rev": server, "text": read_text(d), "updated": meta.get("updated")}
            return False, {"msg": "这份稿子在别处被改过", "conflict": conflict}
    body = text if isinstance(text, str) else ""
    old = read_text(d)
    if body != old:
        snapshot(d, meta)                 # 只在真有改动前留一份旧的
        meta["rev"] = server + 1
    if title is not None and str(title).strip()[:80] != str(meta.get("title") or ""):
        meta["title"] = str(title).strip()[:80] or title_from(body)
    elif not meta.get("title"):
        meta["title"] = title_from(body)
    meta["words"] = words(body)["total"]
    # 书目那条路（list_books）读的是 meta.chars，两个数保持一致：
    # 书架卡片、AI 小结、MCP 列书看到的字数才和写作平台里那个「字数」是同一个。
    meta["chars"] = meta["words"]
    meta["updated"] = int(time.time())
    tmp = draft_path(d) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(body)
    os.replace(tmp, draft_path(d))
    _write_meta(d, meta)
    return True, meta


def _write_meta(draft_dir, meta):
    tmp = meta_path(draft_dir) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        import json
        json.dump(meta, f, ensure_ascii=False, indent=2)
    os.replace(tmp, meta_path(draft_dir))


def snapshot(draft_dir, meta):
    """把**当前**正文拍成一份快照（在覆盖它之前调用）。

    留多少份按 REV_STEP_SNAP 决定：rev 到整倍数才拍。手动点「存一份快照」走
    `snapshot_now()`，不受这条限制 —— 用户明说要留，就该留得住。
    """
    if int(meta.get("rev", 0)) % REV_STEP_SNAP != 0:
        return None
    return snapshot_now(draft_dir, read_text(draft_dir))


def snapshot_now(draft_dir, text):
    vd = versions_dir(draft_dir)
    os.makedirs(vd, exist_ok=True)
    name = "%s.md" % time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(vd, name)
    n = 1
    while os.path.exists(path):           # 同一秒里连点两次保存也要分得开
        path = os.path.join(vd, "%s-%d.md" % (name[:-3], n))
        n += 1
    with open(path, "w", encoding="utf-8") as f:
        f.write(text or "")
    for stale in sorted(os.listdir(vd))[:-KEEP_VERSIONS]:
        try:
            os.remove(os.path.join(vd, stale))
        except OSError:
            pass
    return os.path.basename(path)


def versions(draft_dir):
    vd = versions_dir(draft_dir)
    out = []
    if not os.path.isdir(vd):
        return out
    for name in sorted(os.listdir(vd), reverse=True):
        if not name.endswith(".md"):
            continue
        p = os.path.join(vd, name)
        try:
            size = os.path.getsize(p)
        except OSError:
            continue
        out.append({"name": name, "size": size,
                    "at": int(os.path.getmtime(p)),
                    "words": words(_read_file(p))["total"]})
    return out[:KEEP_VERSIONS]


def _read_file(p):
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def version_text(draft_dir, name):
    """读某一份快照。文件名只认 [0-9A-Za-z._-]，越界的直接拒 —— 这是拼路径的地方。"""
    s = str(name or "")
    if not re.match(r"^[0-9A-Za-z._\-]{1,64}\.md\Z", s):
        return None
    p = os.path.join(versions_dir(draft_dir), s)
    if ".." in s:
        return None
    return _read_file(p) if os.path.isfile(p) else None


def restore(books_root, book_id, name):
    text = version_text(dir_of(books_root, book_id), name)
    if text is None:
        return False, {"msg": "那份快照找不到了"}
    # 回退也是「改」，所以先把现在这份拍下来，回错了还能再回来
    d = dir_of(books_root, book_id)
    snapshot_now(d, read_text(d))
    return save(books_root, book_id, text, rev=None)


def note(books_root, book_id):
    """这篇稿子的「一句备注」：写进书架卡片与列表，不含正文。"""
    meta = load_meta(dir_of(books_root, book_id)) if ok_id(book_id) else None
    if not meta:
        return {}
    return {"title": meta.get("title") or "", "words": int(meta.get("words") or 0),
            "rev": int(meta.get("rev") or 0), "updated": int(meta.get("updated") or 0)}


def seen_sparks(books_root, book_id):
    meta = load_meta(dir_of(books_root, book_id))
    return list((meta or {}).get("sparks") or [])


def remember_sparks(books_root, book_id, angles):
    """记下这批灵感的角度，下一批提示词里点名不要重复。

    只留 24 条：全留会让提示词越来越长、也记不到重点，滚动窗口够用了。
    """
    d = dir_of(books_root, book_id)
    meta = load_meta(d)
    if not meta:
        return False
    pool = list(meta.get("sparks") or [])
    for a in angles or []:
        a = str(a).strip()[:40]
        if a and a not in pool:
            pool.append(a)
    meta["sparks"] = pool[-24:]
    _write_meta(d, meta)
    return True


# ---------- 列表（书架那一格用） ----------

def brief(draft_dir):
    meta = load_meta(draft_dir)
    if not meta:
        return None
    return {"id": meta.get("id"), "title": meta.get("title") or title_from(read_text(draft_dir)),
            "words": int(meta.get("words") or 0), "rev": int(meta.get("rev") or 0),
            "created": int(meta.get("created") or 0), "updated": int(meta.get("updated") or 0),
            "versions": len(os.listdir(versions_dir(draft_dir)))
            if os.path.isdir(versions_dir(draft_dir)) else 0}


def list_drafts(books_root):
    out = []
    top = book_layout.module_path(books_root, MODULE)
    if not os.path.isdir(top):
        return out
    for name in sorted(os.listdir(top)):
        b = brief(os.path.join(top, name))
        if b:
            out.append(b)
    out.sort(key=lambda x: -x["updated"])
    return out


# ---------- 导出：排版模板 ----------
#
# 模板只是「一套 CSS 变量 + 页眉页脚」，正文一律走 book_export.md_to_html 那条已经
# 跑通的书转 HTML 的路。这样三件事一起解决：渲染口径与导出 EPUB/PDF 完全一致、
# 不用再造第二个 markdown 解析器、模板之间不会渲染出两种正文。

TPL_LABELS = [("plain", "黑白简约"), ("business", "商务蓝"), ("verdant", "青翠"),
              ("letter", "米白书简")]

_TPL_BASE = """
:root {
  --bg: %(bg)s; --ink: %(ink)s; --faint: %(faint)s; --rule: %(rule)s;
  --accent: %(accent)s; --pad: %(pad)s;
  --serif: %(serif)s; --sans: %(sans)s;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; background: var(--bg); }
body { color: var(--ink); font-family: var(--serif); }
.sheet { width: %(width)s; padding: %(ptop)s var(--pad) %(pbottom)s; background: var(--bg); }
h1.title { font-size: 34px; line-height: 1.25; margin: 0 0 6px; letter-spacing: .5px;
           color: var(--ink); font-weight: 650; }
.lead { font-size: 14px; color: var(--faint); margin: 0 0 22px; letter-spacing: 1.2px; }
.rule { height: 3px; background: var(--accent); width: 46px; margin: 0 0 26px; border: 0; }
.body { font-size: 17px; line-height: 1.86; }
.body h1, .body h2, .body h3 { font-family: var(--sans); color: var(--ink);
  line-height: 1.4; margin: 30px 0 12px; }
.body h2 { font-size: 22px; padding-left: 11px; border-left: 4px solid var(--accent); }
.body h3 { font-size: 18px; }
.body p { margin: 0 0 15px; }
.body blockquote { margin: 18px 0; padding: 10px 16px; color: var(--faint);
  border-left: 3px solid var(--rule); background: rgba(0,0,0,.022); }
.body ul, .body ol { padding-left: 22px; margin: 0 0 15px; }
.body li { margin-bottom: 7px; }
.body code { font-family: ui-monospace, Menlo, monospace; font-size: 15px;
  background: rgba(0,0,0,.05); padding: 1px 5px; border-radius: 4px; }
.body pre { background: rgba(0,0,0,.045); padding: 14px 16px; border-radius: 8px;
  overflow-x: hidden; }
.body pre code { background: none; padding: 0; }
.body img { max-width: 100%%; height: auto; }
.body hr { border: 0; border-top: 1px solid var(--rule); margin: 26px 0; }
.foot { margin-top: 34px; padding-top: 12px; border-top: 1px solid var(--rule);
  display: flex; justify-content: space-between; font-family: var(--sans);
  font-size: 12px; letter-spacing: .6px; color: var(--faint); }
"""

TEMPLATES = {
    "plain": dict(bg="#ffffff", ink="#16181d", faint="#8a8f99", rule="#e3e6ea",
                  accent="#16181d", pad="56px", width="760px", ptop="56px",
                  pbottom="40px", serif='-apple-system, "PingFang SC", "Noto Serif SC", serif',
                  sans='-apple-system, "PingFang SC", sans-serif'),
    # 商务蓝：页眉压一条深蓝带，标题换无衬线，读起来像一份正式稿
    "business": dict(bg="#f6f9fd", ink="#12233c", faint="#6b7f97", rule="#d6e2ef",
                     accent="#1f5fbf", pad="60px", width="780px", ptop="64px",
                     pbottom="44px", serif='"Songti SC", "PingFang SC", serif',
                     sans='-apple-system, "PingFang SC", sans-serif'),
    "verdant": dict(bg="#f4f9f4", ink="#17281e", faint="#6f8676", rule="#d6e6da",
                    accent="#2f8f5b", pad="58px", width="770px", ptop="58px",
                    pbottom="42px", serif='"PingFang SC", "Hiragino Sans GB", sans-serif',
                    sans='"PingFang SC", sans-serif'),
    "letter": dict(bg="#fbf7ef", ink="#2b2418", faint="#93876f", rule="#e6dcc9",
                   accent="#b9822f", pad="64px", width="720px", ptop="68px",
                   pbottom="48px", serif='"Songti SC", "STSong", "PingFang SC", serif',
                   sans='"PingFang SC", sans-serif'),
}


def tpl_width(tpl):
    """模板的版心宽度（整数像素）。截图的视口宽度必须用它，不能写死 760：
    视口比版心宽，右侧多出一条白；比版心窄，正文被挤到下一栏，导出的行数
    和在预览里看到的不一样。"""
    s = str((TEMPLATES.get(tpl) or TEMPLATES["plain"]).get("width") or "760px")
    m = re.match(r"^(\d+(?:\.\d+)?)", s)
    return int(float(m.group(1))) if m else 760


def export_html(text, title="", tpl="plain", footer=True, date_str="",
                brand="归藏", lead=""):
    """把一篇稿子拼成「能直接丢给 Chromium 渲染」的一份 HTML。

    页脚那行（工具名 + 日期）由调用方给什么就排什么：`brand` / `date_str`
    各是一列，只给一个就只排一个，两个都空（或 `footer=False`）就整行不留。
    截图发朋友圈时想要署名和日期，贴进自己博客时两样都不想要，所以这里
    不提供默认值之外的任何兜底 —— 不塞东西比塞了再让人删掉可靠。
    """
    import book_export
    css = _TPL_BASE % TEMPLATES.get(tpl, TEMPLATES["plain"])
    body = book_export.md_to_html(text or "")
    head = '<h1 class="title">%s</h1>' % _esc(title or "")
    leadline = '<p class="lead">%s</p>' % _esc(lead) if lead else ""
    rule = '<hr class="rule" />'
    cells = [x for x in (str(brand or "").strip(), str(date_str or "").strip()) if x]
    if not footer:
        cells = []
    foot = ('<div class="foot">%s</div>'
            % "".join("<span>%s</span>" % _esc(c) for c in cells)) if cells else ""
    return ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<title>%s</title><style>%s</style></head><body>"
            "<div class=\"sheet\">%s%s%s<div class=\"body\">%s</div>%s</div>"
            "</body></html>") % (_esc(title or "未命名稿"), css, head, leadline, rule,
                                 body, foot)


def _esc(s):
    import html
    return html.escape(str(s if s is not None else ""), quote=True)


def as_book_dir(books_root, book_id, tmp_root):
    """把稿子摊成「一本书」的样子（chapters/0001.md + meta.json），交给
    book_export 现成的 EPUB / PDF 路线。临时目录由调用方负责清理。"""
    import json
    d = read(books_root, book_id)
    if not d:
        return None
    dest = os.path.join(tmp_root, book_id)
    os.makedirs(os.path.join(dest, "chapters"), exist_ok=True)
    with open(os.path.join(dest, "chapters", "0001.md"), "w", encoding="utf-8") as f:
        f.write(d["text"])
    with open(os.path.join(dest, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"title": d.get("title") or "未命名稿", "author": "",
                   "source": MODULE, "done": True}, f, ensure_ascii=False)
    return dest


# ---------- 导出：图片 / PDF ----------
#
# 两条路都用内置的铬内核渲染 `export_html()` 那份 HTML，和「导出整本书」共用一套
# 排版口径。为什么非要请浏览器：正文里的中文、代码块、列表缩进要让它们和界面上
# 一模一样，用 PIL 手搓行距是一条永远对不齐的路。
#
# 三个坑都在这段里处理掉（都是实测踩出来的）：
#   · 中文字形没加载完就截图 → 出方框。先等 `document.fonts.ready`，再用
#     `document.fonts.check()` 兜一层，探的是模板真用的那款字体。
#   · `device_scale_factor` 会连画面一起放大，所以视口宽度给「模板的 CSS 宽度」，
#     出来的像素数才等于 宽 × 倍数，不会比预览宽一档。
#   · `full_page` 截图会把 `position:fixed` 的页眉与背景逐段重画一遍；模板里没有
#     fixed 元素，所以安全 —— 往模板里加 fixed 的东西前要先想清楚这一条。

FONT_PROBE = '16px "PingFang SC"'


def _render_wait(page):
    """等字体真的可用；等不到也不失败，顶多少一种字形，不该让导出报错。"""
    try:
        page.evaluate("() => document.fonts.ready.then(() => true)")
        return bool(page.evaluate("() => document.fonts.check(%r)" % FONT_PROBE))
    except Exception:
        return False


def _launch(pw):
    """起铬内核。和 book_export.build_pdf 同一个打法：浏览器缓存位置由进程环境
    （PLAYWRIGHT_BROWSERS_PATH）决定，装成 .app 时壳已经指好了。"""
    return pw.chromium.launch()


def export_image(html, dest, width=760, scale=2):
    """一张长图：稿子整页截下来，宽度锁死在排版模板那一档。

    `scale` 1/2/3 对应「清晰但小 / 常规 / 很大」，界面只给这三档，
    因为再高的倍数发朋友圈会被压回去，白等一趟渲染。
    """
    import tempfile
    from playwright.sync_api import sync_playwright
    font_ok = False
    tmp = tempfile.mkdtemp(prefix="guizang-write-")
    path = os.path.join(tmp, "page.html")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        with sync_playwright() as pw:
            browser = _launch(pw)
            try:
                page = browser.new_page(viewport={"width": int(width), "height": 1000},
                                        device_scale_factor=max(1, min(int(scale), 3)))
                page.goto("file://" + path, timeout=120000)
                font_ok = _render_wait(page)
                page.screenshot(path=dest, full_page=True)
            finally:
                browser.close()
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    return dest, font_ok


def export_pdf(html, dest, paper="A4"):
    """一份 PDF：走铬内核打印，和 build_pdf 同一套边距量级。"""
    import tempfile
    from playwright.sync_api import sync_playwright
    tmp = tempfile.mkdtemp(prefix="guizang-write-")
    path = os.path.join(tmp, "page.html")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        with sync_playwright() as pw:
            browser = _launch(pw)
            try:
                page = browser.new_page()
                page.goto("file://" + path, timeout=120000)
                _render_wait(page)
                # page.pdf() 不吃 timeout（超时由上面那一跳控制），别往上加
                page.pdf(path=dest, format=paper, print_background=True,
                         margin={"top": "16mm", "bottom": "16mm",
                                 "left": "15mm", "right": "15mm"})
            finally:
                browser.close()
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    return dest


# ---------- AI：续写 / 灵感 / 润色 ----------

SYS_WRITE = (
    "你是这篇文章的作者本人，不是助手。风格、人称、句式、用词都跟着前文走，"
    "不许突然开始总结、不许加「首先其次」、不复述已经写过的话、不写「总而言之」。"
    "只用中文，不用 emoji，不要给你的续写加标题或引号。"
)

SYS_SPARK = (
    "你是作者的选题编辑。只给方向，不给成稿：每条不超过 22 个字，"
    "彼此角度不同（可以是一句追问、一个反例、一个具体场景、一个数据缺口、一段个人经历）。"
    "不说「建议你可以考虑」这类话，不用 emoji，不要编号以外的符号。"
)

SYS_POLISH = (
    "你是文字编辑。只做「改写」这一件事：保持原意、保持人称与信息点，"
    "该删的删、该顺的顺，不添加新事实、不引用外部资料、不写评论。"
    "只输出改写后的正文本身，不要前后缀、不要引号、不要解释。"
)

POLISH_TONES = [("smooth", "顺一顺"), ("tight", "删掉水分"),
                ("concrete", "写得更具体"), ("formal", "收得正式")]


def material_block(items, limit=6000):
    """把用户的便签 / 划线拼成一段素材，塞进提示词。

    夹到 `limit` 字：素材越全不一定越好，长上下文会把模型的注意力摊薄，
    而且上游按 token 收费。给的是「最近且较长」优先，纯文本，不带原文之外的解释。
    """
    keep, used = [], 0
    for it in items or []:
        s = str(it or "").strip()
        if not s:
            continue
        if used + len(s) > limit:
            break
        keep.append(s)
        used += len(s)
    if not keep:
        return ""
    return "〔作者的笔记素材，可作事实与角度来源，不要照抄原句〕\n" + \
        "\n".join("- " + x for x in keep)


def cont_messages(text, material="", length="para", brief_hint=""):
    label, tokens, stop = LENGTH_OF.get(length, LENGTH_OF["para"])
    tail = (text or "")[-4000:]
    user = "前文（接着这里往下写，输出紧接其后、不要重复前文）：\n<<<\n%s\n>>>\n" % tail
    if material:
        user += "\n" + material_block([material]) if isinstance(material, str) else \
            "\n" + material_block(material)
    if brief_hint:
        user += "\n作者此刻的意图：%s\n" % str(brief_hint).strip()[:200]
    user += "\n这次只续%s。" % label
    return [{"role": "system", "content": SYS_WRITE}, {"role": "user", "content": user}], \
        {"max_tokens": tokens, "stop": stop}


def spark_messages(text, material="", seen=(), n=3):
    tail = (text or "")[-2500:]
    user = "这篇稿子写到这儿：\n<<<\n%s\n>>>\n" % tail
    if material:
        user += "\n" + (material_block([material]) if isinstance(material, str)
                        else material_block(material))
    if seen:
        user += "\n已经给过这些角度，别再给同类的：%s\n" % "、".join(
            [str(x)[:40] for x in list(seen)[-12:]])
    user += "\n给我 %d 个往下写的方向，一行一个，格式严格照「- 方向」，不要编号、不要解释。" % n
    return [{"role": "system", "content": SYS_SPARK}, {"role": "user", "content": user}]


def parse_sparks(raw, n=3):
    """把模型给的那几行收成干净的方向列表：去掉行首符号、去重、限长、最多 n 条。

    模型不总按格式来（有时给「1. 」、有时给「**方向**：」），所以解析要宽进；
    但出口必须严 —— 界面上那三张卡是等高的，脏行会让卡片塌掉。
    """
    out = []
    for line in str(raw or "").splitlines():
        s = line.strip()
        if not s:
            continue
        s = re.sub(r"^[-*+•\d]+[.)、:：]?\s*", "", s)
        s = re.sub(r"^[「《\"“'\[]+|[」》\"”'\]]+$", "", s).strip()
        s = s.replace("**", "").strip()
        if not s or s in out:
            continue
        out.append(s[:40])
        if len(out) >= n:
            break
    return out


def polish_messages(sel, tone="smooth", material=""):
    label = dict(POLISH_TONES).get(tone, "顺一顺")
    user = "把下面这段「%s」：\n<<<\n%s\n>>>\n" % (label, (sel or "")[:6000])
    if material:
        user += "\n" + (material_block([material]) if isinstance(material, str)
                        else material_block(material))
    user += "\n只输出改写后的这一段。"
    return [{"role": "system", "content": SYS_POLISH}, {"role": "user", "content": user}]


def diff_blocks(old, new):
    """把「AI 想改的」切成一块一块，交给界面逐块接受或拒绝。

    为什么后端来切而不是前端：回退与「一键接受」的正确性取决于切得对不对，
    而这块逻辑在 Python 里能直接单测（difflib 是标准库），放到页面上就变成
    一串没法单独验的 DOM 操作。原文一个字都不会被改动 —— 这里只给差异。

    每块带 `at` / `end`：这一处改动在**选中段落**里的字符区间。有了它，界面
    接受一块就是把那一段换掉，接受多块就按 `at` 从后往前换 —— 从前往后换会
    让后面那些坐标整体错位，改到不该改的字上，那是最伤的一种 bug。
    """
    import difflib
    old = old or ""
    new = new or ""
    a = [x for x in re.split(r"(?<=[。！？；\n])", old) if x != ""]
    b = [x for x in re.split(r"(?<=[。！？；\n])", new) if x != ""]
    # 每句在原文里的起点。按编号乘长度算不出来（切掉过空串），只能逐句累加；
    # 除了空串这里没丢过任何字符，所以累加值就等于原串下标。
    start, acc = [], 0
    for x in a:
        start.append(acc)
        acc += len(x)
    start.append(acc)
    blocks = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if tag == "equal":
            continue
        blocks.append({"op": {"replace": "改", "insert": "增", "delete": "删"}.get(tag, tag),
                       "at": start[i1], "end": start[i2],
                       "old": "".join(a[i1:i2]), "new": "".join(b[j1:j2])})
    return blocks


def fingerprint(text):
    """正文指纹，给「内容没变就别拍快照」那一类判断用。"""
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:12]
