#!/usr/bin/env python3
"""
微信读书导出 — 精确图文版 v3

逐页捕获：每翻一页，抓当前视口内的 canvas 文字 + 视口内图片，
按屏幕 y 坐标把文字行和图片交错排序，图片精确落在对应段落之间。
双页拆分(左页→右页)，按章节切分，自动续传，卡住重开。
"""
import asyncio
import hashlib
import json
import os
import re
import signal
import statistics
import sys
import time
import urllib.request
from collections import deque

from playwright.async_api import async_playwright

import platform_compat as pc

USER_DATA_DIR = os.path.join("cache", "browser_profile")

# 抓取本身不需要可见窗口（正文靠 hook Canvas 抓，翻页靠合成按键），
# 且可见窗口一旦被误关就会整场中断。所以默认无头；要盯着过程用 --headed。
HEADLESS = True

# 单轮会话的边界：翻够这么多页、或连续这么多页都没新内容就收尾，
# 交给外层会话循环重开浏览器续传（防止任何情况下转不出来）。
MAX_PAGES = 3000
STALE_LIMIT = 12
MAX_SESSIONS = 8

# 墙钟预算（秒）。即便翻页一直「有内容」，到点也必须收尾——
# 新版阅读器会在若干页后复用同一批已绘制内容，重复页不会被单块去重挡住，
# 那样就会一路翻到 MAX_PAGES（3000 页 ≈ 75 分钟）看起来像死循环。
SESSION_BUDGET = 900        # 单轮会话上限 15 分钟
TOTAL_BUDGET = 3600         # 整次导出上限 60 分钟
DUP_WINDOW = 8              # 同一页文字在最近这么多页内重复出现，就当成没新内容

# 单次「翻页 + 抓取」的总超时。
# Playwright 的 page.evaluate 默认没有超时：渲染进程一旦卡住，调用就永久挂住，
# 表现为整场导出「跑着跑着不动了」——不报错、不重试、CPU 近乎 0。
# 实测 2026-09-29：跑到第 31 章卡死，Python 停在 asyncio 空等，浏览器进程还活着但不回包。
ITER_TIMEOUT = 45
PREP_TIMEOUT = 150

# 攒够这么多字还没认出章节标题，就先把在手的落一盘（保底，不让内容只活在内存里）。
AUTO_FLUSH_CHARS = 12000

# 书名/作者一读到就立刻记在这里，不等会话跑完。
# 之前只有 run_session 正常返回才把书名带出来，而浏览器中途被关掉是最常见的事，
# 于是 meta.json 里留的是书号、合并出的 md 也叫书号.md —— 用户看到的就是「导出一堆没名字的东西」。
BOOK_INFO = {"title": "", "author": ""}

# 用户点「中止」时界面发的是 SIGTERM。默认行为是进程立刻死掉：手上正在攒的那一章
# （实测这种排版一本书几万字）会整批蒸发，而磁盘上还是空的 —— 用户看到的
# 就是「抓了半天一个字也没有」。改成协作式停止：只置个标志，
# 循环自己走完收尾（章节落盘 + 写合并 md）再退。
STOP = False


def install_stop_handler():
    def _on_stop(signum, _frame):
        global STOP
        STOP = True
    # Windows 上没有一个「可捕获的 SIGTERM」——terminate 等于直接杀进程；
    # 界面按暂停键时发过来的是控制台 CTRL_BREAK，在 Python 里表现为 SIGBREAK。
    # 不认它，「中止」在 Windows 上就退化成硬杀：在手的章节全没了。
    sigs = [signal.SIGTERM, signal.SIGINT]
    if hasattr(signal, "SIGBREAK"):
        sigs.append(signal.SIGBREAK)
    for sig in sigs:
        try:
            signal.signal(sig, _on_stop)
        except (ValueError, OSError):
            pass


class PageStalled(Exception):
    """页面在超时内没有响应，判定渲染进程卡死。"""


def write_progress(book_dir, **fields):
    """把「现在翻到第几页、写了多少章多少字」落盘，供界面画进度条。

    进度条原来只按「已落盘章节数 / 目录章数」算，而一本书的章节数是几十到上百，
    一章要翻十几页 —— 于是整章期间百分比一动不动，看起来像卡死。
    引擎每翻几页就写一次这个文件，界面就能按页/字数持续推进。
    """
    path = os.path.join(book_dir, "_progress.json")
    data = {}
    try:
        if os.path.exists(path):
            data = json.load(open(path, encoding="utf-8"))
    except Exception:
        data = {}
    data.update(fields)
    data["updated_at"] = time.time()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


async def run_bounded(coro, timeout, what):
    """带硬超时地执行一段浏览器操作。

    **不能用 asyncio.wait_for**：它超时后会 cancel 内部任务并「等它真正结束」，
    而卡死的 Playwright 调用不响应取消，于是 wait_for 自己也永久挂住 ——
    实测 2026-09-29 加了 wait_for 仍在第 55 章无声卡死 175s+。
    asyncio.wait 拿到超时就直接返回，不再等那个调用；随后由
    kill_stray_browsers() 杀掉浏览器把连接连带回收。
    """
    task = asyncio.ensure_future(coro)
    done, _pending = await asyncio.wait({task}, timeout=timeout)
    if task in done:
        return task.result()
    task.cancel()
    # 被放弃的任务后续报错时不要抛到事件循环上
    task.add_done_callback(lambda t: t.cancelled() or t.exception())
    raise PageStalled(f"{what} 超过 {timeout}s 无响应（渲染进程卡死）")


def kill_stray_browsers(wait=1.0):
    """清掉仍占着本项目 profile 的残留浏览器。

    会话卡死或异常退出时，旧浏览器进程可能没被关掉，而它握着 profile 的锁，
    会让下一轮 launch_persistent_context 起不来。按 profile 绝对路径精确匹配，
    不会误伤其它浏览器。实现在 platform_compat 里 —— 原来直接调 pkill，
    而 Windows 没有这个命令，异常被 try 吞掉之后看着「正常」，锁却一直没解开。
    """
    pc.kill_stray_browsers(USER_DATA_DIR, wait)

# 无头下别让 Chromium 因「窗口不可见」而节流定时器/渲染，否则等页面稳定会误判
CHROME_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
]


async def is_logged_in(ctx):
    """登录判定：以 wr_vid cookie 为准。

    原实现用 `"login" not in url` 判断，实测有误——未登录时打开 /web/shelf 并不会跳转，
    URL 里也没有 "login"，于是会被误判成「已登录」，导致跳过扫码等待、随后抓不到任何正文。
    """
    try:
        cookies = await ctx.cookies("https://weread.qq.com")
    except Exception:
        return False
    return any(c.get("name") == "wr_vid" and (c.get("value") or "").strip()
               for c in cookies)

CANVAS_HOOK = """
(function() {
    window.__wr_chars = [];
    var origFill = CanvasRenderingContext2D.prototype.fillText;
    CanvasRenderingContext2D.prototype.fillText = function(text, x, y) {
        if (text && text.trim())
            window.__wr_chars.push({t: text, x: Math.round(x*10)/10, y: Math.round(y*10)/10});
        return origFill.apply(this, arguments);
    };
    window.__wr_reset = function() { window.__wr_chars = []; };
    window.__wr_count = function() { return window.__wr_chars.length; };
})();
"""

# 当前视口内可见的书籍插图，带屏幕坐标
VIEWPORT_IMGS_JS = """
() => {
    const H = window.innerHeight, W = window.innerWidth, out = [];
    document.querySelectorAll('img[class*="wr_readerImage"]').forEach(i => {
        const src = i.src || i.getAttribute('data-src') || '';
        if (!src.includes('res.weread.qq.com/wrepub')) return;
        const r = i.getBoundingClientRect();
        if (r.width > 40 && r.height > 40 && r.bottom > 0 && r.top < H &&
            r.right > 0 && r.left < W &&
            getComputedStyle(i).visibility !== 'hidden' &&
            getComputedStyle(i).display !== 'none') {
            out.push({src, top: Math.round(r.top), left: Math.round(r.left),
                      w: i.naturalWidth||i.width, h: i.naturalHeight||i.height});
        }
    });
    return out;
}
"""

# reader 的两个 canvas 的屏幕位置
CANVAS_RECTS_JS = """
() => Array.from(document.querySelectorAll('canvas')).map(c => {
    const r = c.getBoundingClientRect();
    return {top: r.top, left: r.left, w: Math.round(r.width), h: Math.round(r.height)};
}).filter(r => r.h > 300)
"""

# 测量类文本（页码、尺寸等）通常由纯数字+符号组成，需要丢掉；
# 但绝不能连字母一起丢 —— 排版引擎会把连字（如 "ti"）成对画出来，
# 原实现把所有「纯 ASCII 字母数字」的多字符 token 都当测量文本，
# 于是 "ti" 被丢弃，"Acting" 就变成了 "Acng"。所以只丢「不含字母」的。
MEASURE_RE = re.compile(r'^[\d\s.,:;%()\[\]{}|+\-*/=<>~^_\\\u00b7\u2022\uff0e\uff05]+$')
SENTENCE_END = set("。！？；：」）】》…—")


def split_spread(chars):
    """双页拆分：返回 [左页chars, 右页chars] 或 [单页chars]"""
    if len(chars) < 20:
        return [chars]
    singles = [(i, c) for i, c in enumerate(chars) if len(c["t"]) == 1]
    if len(singles) < 10:
        return [chars]
    for j in range(1, len(singles)):
        if singles[j - 1][1]["y"] > 400 and singles[j][1]["y"] < 200:
            return [chars[:singles[j][0]], chars[singles[j][0]:]]
    return [chars]


def chars_to_lines(chars):
    """把单页字符按 y 聚成行，返回 [{y, text}]（未合并段落）。

    不能用固定分桶（旧实现是 round(y/3)*3）：拉丁字母与中文字形的基线常差几像素，
    固定 3px 会把同一视觉行的字符劈到相邻两行，表现就是跨行断字被啃字符
    （`multi-agent` 变成 `ulti`）。这里改成按**实测行距**自适应聚类：
    行距取相邻不同 y 的中位数，容差取行距的 40%（并夹在 3~12px 内），
    所以既不会切错行，也不会把两行并成一行。
    """
    real = [c for c in chars if len(c["t"]) == 1 or not MEASURE_RE.match(c["t"])]
    if not real:
        return []

    uniq = sorted({round(c["y"], 1) for c in real})
    gaps = [b - a for a, b in zip(uniq, uniq[1:]) if b - a > 1]
    pitch = statistics.median(gaps) if gaps else 20.0
    tol = max(3.0, min(pitch * 0.4, 12.0))

    ordered = sorted(real, key=lambda c: (c["y"], c["x"]))
    groups = []
    cur = []
    anchor = None
    for c in ordered:
        if anchor is None or abs(c["y"] - anchor) <= tol:
            if anchor is None:
                anchor = c["y"]
            cur.append(c)
        else:
            groups.append(cur)
            cur = [c]
            anchor = c["y"]
    if cur:
        groups.append(cur)

    lines = []
    for grp in groups:
        line = "".join(c["t"] for c in sorted(grp, key=lambda c: c["x"]))
        if line.strip():
            lines.append({"y": min(c["y"] for c in grp), "text": line.strip()})
    return lines


def build_page_blocks(chars, images, canvas_rects, seen_imgs):
    """把一次渲染(可能双页)拆成有序块列表: [{type:'text'/'img', ...}]
       文字行和图片按屏幕 y 交错；左页整页在前，右页在后。"""
    blocks = []
    pages = split_spread(chars)

    # 判定左右 canvas
    rects = sorted(canvas_rects, key=lambda r: r["left"])
    left_rect = rects[0] if rects else {"top": 0, "left": 0}
    right_rect = rects[1] if len(rects) > 1 else left_rect
    mid_x = (left_rect["left"] + right_rect["left"]) / 2 + 180 if len(rects) > 1 else 99999

    # 图片按左右分组
    left_imgs = [im for im in images if im["left"] < mid_x]
    right_imgs = [im for im in images if im["left"] >= mid_x]

    def emit_page(page_chars, page_rect, page_imgs):
        lines = chars_to_lines(page_chars)
        items = []
        for ln in lines:
            items.append(("text", page_rect["top"] + ln["y"], ln["text"]))
        for im in page_imgs:
            if im["src"] in seen_imgs:
                continue
            items.append(("img", im["top"], im))
        items.sort(key=lambda t: t[1])
        for typ, _y, payload in items:
            if typ == "text":
                blocks.append({"type": "text", "text": payload})
            else:
                seen_imgs.add(payload["src"])
                blocks.append({"type": "img", "src": payload["src"],
                                "w": payload["w"], "h": payload["h"]})

    if len(pages) == 2:
        emit_page(pages[0], left_rect, left_imgs)
        emit_page(pages[1], right_rect, right_imgs)
    else:
        # 单页：图片全归这页，仍按 y 排
        emit_page(pages[0], left_rect, left_imgs + right_imgs)
    return blocks


def img_filename(url, ch_idx, seq):
    ext = "jpg"
    m = re.search(r'\.(jpg|jpeg|png|gif|webp)', url.lower())
    if m:
        ext = m.group(1).replace("jpeg", "jpg")
    return f"ch{ch_idx:04d}_img{seq:02d}.{ext}"


def render_chapter_md(ch_title, blocks, ch_idx):
    """把有序块渲染成 Markdown：文字行合并成段落，图片就地插入"""
    out = [f"# {ch_title}\n"]
    para = []
    img_records = []
    img_seq = 0

    def flush_para():
        nonlocal para
        if not para:
            return
        # 合并 canvas 断行为自然段：上一行不以句末标点结尾则接续
        merged = []
        for line in para:
            if line == ch_title:
                continue
            if merged and merged[-1] and merged[-1][-1] not in SENTENCE_END:
                merged[-1] += line
            else:
                merged.append(line)
        for m in merged:
            if m.strip():
                out.append(m.strip())
        para = []

    for b in blocks:
        if b["type"] == "text":
            para.append(b["text"])
        else:
            flush_para()
            img_seq += 1
            fname = img_filename(b["src"], ch_idx, img_seq)
            out.append(f"![图](images/{fname})")
            img_records.append({"url": b["src"], "file": fname})
    flush_para()

    body = "\n\n".join(out) + "\n"
    return body, img_records


# 一次往返取回这一屏的全部原料（文字 / 画布位置 / 插图）。
# 原来这是三次 page.evaluate：每一趟都要跨进程来回一次，一页三次、一本书几百页
# 就是几千趟白跑的往返。合成一条，语义完全不变。
SNAPSHOT_JS = ("() => ({chars: window.__wr_chars,"
               " rects: (" + CANVAS_RECTS_JS.strip() + ")(),"
               " imgs: (" + VIEWPORT_IMGS_JS.strip() + ")()})")


async def snapshot(page, seen_imgs):
    d = await page.evaluate(SNAPSHOT_JS) or {}
    return build_page_blocks(d.get("chars") or [], d.get("imgs") or [],
                             d.get("rects") or [], seen_imgs)


async def wait_settled(page, step=0.09, quiet=0.30, max_wait=2.6, blank_ok=1.1):
    """等这一屏画完，返回画出来的字数。

    判据是「距上一次字数变化已经过了 quiet 秒」，而不是「先无条件睡一秒再比两次」。
    后者是这一整场导出最大的时间黑洞：一屏正文实测两三百毫秒就画完，而原来的
    one_turn 每页固定要睡 1.0s + 0.5s 起跳的稳定判定 + capture 里再 0.3s——
    一页白等一秒八，一本几百页的书里就是几十分钟。采样从按下方向键那一刻就开始，
    画完立刻走；真慢的页面也只会等到 max_wait，不会比以前更久。

    纯图页（一个字都没画）不该让这里空等到底：过了 blank_ok 还是空的，
    就按「这一屏本来就没文字」收下，交回上面的整页指纹去判断。
    """
    t0 = time.time()
    last, changed = -1, t0
    while True:
        c = await page.evaluate("() => window.__wr_count()")
        now = time.time()
        if c != last:
            last, changed = c, now
        elif last > 0 and now - changed >= quiet:
            return last
        elif last == 0 and now - t0 >= blank_ok:
            return 0
        if now - t0 >= max_wait:
            return last if last > 0 else 0
        await asyncio.sleep(step)


async def wait_stable(page, prev_count, timeout=8):
    """旧口径的「等两张一样的采样」。翻页主循环已经不用它了（见 wait_settled），
    保留给还需要「先睡一会儿再确认」的场景。"""
    last = -1
    for _ in range(int(timeout / 0.5)):
        c = await page.evaluate("() => window.__wr_count()")
        if c == last:
            return c
        last = c
        await asyncio.sleep(0.5)
    return last


def get_last_chapter_title(md_dir):
    if not os.path.exists(md_dir):
        return None, 0
    files = sorted(f for f in os.listdir(md_dir) if f.endswith(".md"))
    if not files:
        return None, 0
    idx = int(files[-1].replace(".md", ""))
    with open(os.path.join(md_dir, files[-1])) as f:
        title = f.readline().strip().replace("# ", "")
    return title, idx


def load_last_catalog_title(catalog_path):
    try:
        with open(catalog_path) as f:
            titles = json.load(f)
        return titles[-1] if titles else ""
    except Exception:
        return ""


# ---------- 分章：认正文里的标题，不认会跳动的顶栏 ----------
#
# 为什么不能用顶栏标题分章（实测 2026-09-29）：
#   新版阅读器进入新章节时会一次排出并绘制好几页，一批抓取里往往含多个小节；
#   而顶栏标题每翻一页都会变。按顶栏切 → 整批内容全堆到当时那一个标题下，
#   其余小节只剩标题、正文为空（表现为「0 字章节」）；同时章节计数虚高，
#   外层会话循环每轮都认为「有新增」→ 无限循环。
#   正文里的标题行是可靠的：它是实际绘制出来的文字，且每个目录标题只会出现一次。

def norm_title(s):
    return re.sub(r"[\s\u3000]+", "", s or "").replace("当前读到", "")


def load_catalog_titles(catalog_path):
    """目录标题，去掉「当前读到 xx%」后缀，按长度降序（长标题优先匹配）。"""
    try:
        with open(catalog_path, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return []
    out = []
    for t in raw:
        t = re.split(r"当前读到", t or "")[0].strip()
        if t:
            out.append((norm_title(t), t))
    out.sort(key=lambda x: -len(x[0]))
    return out


def load_used_titles(md_dir):
    """已落盘章节的标题（供续传时去重，避免重复切出同一章）。"""
    used = set()
    if not os.path.isdir(md_dir):
        return used
    for fn in sorted(os.listdir(md_dir)):
        if not fn.endswith(".md"):
            continue
        try:
            with open(os.path.join(md_dir, fn), encoding="utf-8") as f:
                first = f.readline().strip()
        except OSError:
            continue
        if first.startswith("# "):
            used.add(norm_title(first[2:]))
    return used


def match_heading(line, titles, used):
    """这一行是不是某个目录标题（可能后面直接粘了正文，或被换行截断）。"""
    nline = norm_title(line)
    if len(nline) < 3:
        return None
    for nt, orig in titles:
        if not nt or nt in used or len(nt) < 2:
            continue
        if nline == nt or nline.startswith(nt):
            return nt, orig
        # 标题自身被换行截断，例如「…成为水和」/「电」
        if len(nline) >= 6 and nt.startswith(nline):
            return nt, orig
    return None


def strip_title_prefix(line, ntitle):
    """从原行里摘掉标题前缀（跳过空白比对），返回粘在标题后面的正文残段。"""
    if not ntitle:
        return ""
    n = 0
    for i, ch in enumerate(line):
        if ch.isspace() or ch == "\u3000":
            continue
        n += 1
        if n == len(ntitle):
            return line[i + 1:].strip()
    return ""


# ---------- 标题被画成好几行怎么办 ----------
#
# 实测 2026-09-30《改变你一生的100个好习惯》：每页 canvas 都正常吐出 3000+ 字符，
# 正文一个字都没少抓，但**一章也切不出来**（日志表现就是「已翻 33 页 · 0 章 / 0 字」，
# 界面上看就是「无法抓取文章」）。原因是这类书的章节标题是设计过的排版，
# 在画布里被拆成连续几行分别绘制：
#     '四' / '心灵滋养'                              ← 目录项「四 心灵滋养」
#     '｜第二支柱｜' / '效率系统' / '——掌控时间与行动的25个生产力习' / '惯'
# 而目录里的标题是整条的，单行比对永远对不上 → 所有正文都堆在手里，
# 只有会话正常结束时才会以「未题名」落一次盘；中途停止就一个字都不剩。
#
# 解法：把「连续几行拼起来正好等于某个目录标题」也认出来。
# 比对时只看实质字符（中日韩文字 + 字母 + 数字），标点、竖线、破折号、
# 零宽字符全部丢掉 —— 排版装饰不该参与身份判定。

def bare(s):
    """实质字符序列：丢掉空白与标点装饰，只留汉字/假名/字母/数字。"""
    return re.sub(r"[^0-9A-Za-z\u3040-\u30ff\u4e00-\u9fff]", "", s or "")


def cut_bare(line, n):
    """跳过行首 n 个实质字符，返回剩下的正文（用来摘掉粘在标题后面的第一段）。"""
    if n <= 0:
        return line.strip()
    seen = 0
    for i, ch in enumerate(line):
        if not re.match(r"[0-9A-Za-z\u3040-\u30ff\u4e00-\u9fff]", ch):
            continue
        seen += 1
        if seen == n:
            return line[i + 1:].strip()
    return ""


class ChapterSplitter:
    """按正文里画出来的标题切章；认单行，也认被拆成 2~5 行的标题。

    titles 是 [(norm_title, orig)]（长标题在前），used 是已切出过的 norm 标题集合
    （跨会话续传用，同一个标题不会切第二次）。
    """

    HOLD_MAX_PIECES = 5        # 标题最多被拆成几行
    HOLD_MAX_BARE = 80         # 拼接后最多比对多少实质字符
    HOLD_START_MAX = 20        # 只有足够短的开头行才当标题碎片（正文一行通常 25~45）

    def __init__(self, titles, used):
        self.titles = titles
        self.used = used
        self.pairs = []        # (bare_title, orig, nt)
        for nt, orig in titles:
            bt = bare(orig)
            if bt and nt not in used:
                self.pairs.append((bt, orig, nt))
        self.cur = []          # 在手的块
        self.title = None      # 在手这一章的标题
        self.completed = []    # 已切完待落盘
        self.cuts = 0          # 本次会话靠正文标题切出过几章
        self.hold = []         # 疑似标题碎片（原始块）
        self.hold_bare = ""

    # ---- 内部 ----
    def _find_exact(self, cand):
        for bt, orig, nt in self.pairs:
            if bt == cand:
                return orig, nt, len(bt)
        return None

    def _prefix_of_unused(self, cand):
        for bt, _o, _n in self.pairs:
            if len(bt) > len(cand) and bt.startswith(cand):
                return True
        return False

    def _drop(self, nt):
        self.pairs = [p for p in self.pairs if p[2] != nt]

    def _open(self, orig, nt, rest=""):
        if self.cur:
            self.completed.append((self.title, self.cur))
        self.title = orig
        self.cur = [{"type": "text", "text": rest}] if rest else []
        self.used.add(nt)
        self._drop(nt)
        self.cuts += 1
        self.hold, self.hold_bare = [], ""

    def release_hold(self):
        """拼不出标题了：把手里的碎片当正文放回（内容绝不丢弃）。"""
        if self.hold:
            self.cur.extend(self.hold)
        self.hold, self.hold_bare = [], ""

    # ---- 对外 ----
    def push(self, blocks):
        for b in blocks:
            if b["type"] != "text":
                self.release_hold()          # 插图会打断标题拼接
                self.cur.append(b)
                continue
            line = b["text"]
            bl = bare(line)

            # ① 正在拼标题：先看这一行能不能把标题接完
            if self.hold and bl:
                cand = self.hold_bare + bl
                hit = self._find_exact(cand)
                if hit:
                    orig, nt, nlen = hit
                    consumed = max(0, nlen - len(self.hold_bare))
                    self._open(orig, nt, cut_bare(line, consumed))
                    continue
                if (len(self.hold) < self.HOLD_MAX_PIECES
                        and len(cand) <= self.HOLD_MAX_BARE
                        and self._prefix_of_unused(cand)):
                    self.hold.append(b)
                    self.hold_bare = cand
                    continue
                self.release_hold()

            # ② 单行直接命中（原逻辑：整行等于标题，或标题后面粘了正文）
            hit = match_heading(line, self.titles, self.used)
            if hit:
                nt, orig = hit
                self._open(orig, nt, strip_title_prefix(line, nt))
                continue

            # ③ 这一行可能是被拆出来的标题前半段
            if bl and len(bl) <= self.HOLD_START_MAX and self._prefix_of_unused(bl):
                self.hold, self.hold_bare = [b], bl
                continue

            self.cur.append(b)

    def captured_chars(self):
        return sum(len(b.get("text") or "") for b in self.cur if b["type"] == "text")

    def boundary_by_topbar(self, top_text):
        """回退分章：正文里的标题实在认不出来时，用顶栏报出的目录标题断章。

        只在「本会话一次都没切出过章」时调用（见 run_session），所以对原先
        能正常工作的书零影响。比的是丢掉装饰后的整条标题，且必须是没用过的
        目录项 —— 顶栏每翻一页都会变的那些噪声（子小节名、「当前读到 xx%」）
        对不上，也就不会像旧实现那样把一章撕成几十节。
        """
        bt = bare(top_text or "")
        if not bt:
            return False
        for cand, orig, nt in self.pairs:
            if cand == bt:
                self._open(orig, nt)
                return True
        return False

    def force_break(self, fallback_title=None):
        """保底：攒了太多内容还没认出标题，就先落一盘，别全留在内存里。

        章节归属不变 —— 已经认出来的标题继续管住后面的内容，
        所以断成几盘落盘只是「同一章分批写」，不会凭空多出章节。
        """
        self.release_hold()
        if not self.cur:
            return False
        self.completed.append((self.title or fallback_title or "未题名", self.cur))
        self.cur = []
        return True

    def pop_completed(self):
        out, self.completed = self.completed, []
        return out

    def close(self):
        self.release_hold()
        if self.cur:
            self.completed.append((self.title, self.cur))
            self.cur = []
        return self.completed


async def _title(page):
    """当前章节标题。

    注意：新版阅读器把标题放在 .readerTopBar_title_chapter；
    上游只查 .renderTargetPageInfo_header_chapterTitle，在新布局下取不到 → 恒为空字符串，
    导致章节切换检测失效（整本会被当成一个无名章节，且中途卡住即提前结束）。
    """
    return await page.evaluate("""() => {
        const sels = ['.readerTopBar_title_chapter',
                      '.renderTargetPageInfo_header_chapterTitle',
                      '.readerTopBar_title'];
        for (const s of sels) {
            const t = document.querySelector(s)?.textContent?.trim();
            if (t) return t;
        }
        return '';
    }""")


async def fetch_book_title(page):
    info = await page.evaluate("""() => {
        const title = document.querySelector('.readerCatalog_bookInfo_title_txt, .bookInfo_right_header_title')
            ?.textContent?.trim() || document.title.replace(/-.*$/, '').trim();
        const author = document.querySelector('.readerCatalog_bookInfo_author, .bookInfo_author a')
            ?.textContent?.trim() || '';
        return {title, author};
    }""")
    return info.get("title", "未知"), info.get("author", "")


async def dismiss_masks(page):
    """把盖在阅读器上的浮层收掉（实测会拦点击，导致目录点了没反应）。

    2026-09-30 现场：`<div class="wr_mask wr_mask_Show">` 一直在 intercepts pointer events，
    Playwright 重试到超时，首项文字取到的是空串 —— 「跳到开头」其实没跳成。
    先按 Esc 请它自己关；还赖着就把这一层从命中测试里摘掉（只动遮罩本身，不碰内容）。
    """
    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass
    try:
        await page.evaluate("""() => {
            document.querySelectorAll('.wr_mask_Show, .wr_mask').forEach(m => {
                m.style.pointerEvents = 'none';
                m.style.display = 'none';
            });
        }""")
    except Exception:
        pass


async def jump_to_first_item(page):
    """打开目录 → 点首项 → 关掉目录。跳到全书开头的动作本体。"""
    await dismiss_masks(page)
    await page.click("button.readerControls_item.catalog", timeout=5000)
    await asyncio.sleep(1.5)
    await page.evaluate("""() => {
        const sc = document.querySelector('.readerCatalog_list_scroll_area, [class*="readerCatalog_list_scroll"]');
        if (sc) sc.scrollTop = 0;
    }""")
    await asyncio.sleep(1)
    item = page.locator(".readerCatalog_list_item").first
    text = (await item.text_content() or "").strip()
    if not text:
        # 目录还挂着浮层时点下去是空的（实测过），收掉再取一次
        await dismiss_masks(page)
        await asyncio.sleep(0.8)
        text = (await item.text_content() or "").strip()
    await item.click(timeout=4000)
    await asyncio.sleep(3)
    try:
        await page.click("button.readerControls_item.catalog", timeout=2000)
    except Exception:
        await page.keyboard.press("Escape")
    await asyncio.sleep(2)
    return text


async def ensure_first_chapter(page, catalog_path, tries=3):
    """跳完开头之后，确认它真的还停在开头。

    实测 2026-09-30：目录里点了首项、顶栏也报了「版权信息」，可等我们准备抓取时，
    阅读器把「上次读到的位置」异步恢复了回来 —— 开头四章（版权信息 / 序言 /
    第一支柱…）于是被静默跳过，导出的书天生少一截，用户看到的正是「抓不到文章」。
    所以在会触发恢复的那一下点击之后再核一次位置：不对就重跳，最多 tries 次；
    还是不对就把实况报出来，绝不装作跑全了。
    """
    first = first_catalog_title(catalog_path)
    want = bare(first)
    if not want:
        return True, ""
    got = await _title(page)
    for _ in range(tries):
        gb = bare(got or "")
        if not gb or gb in want or want in gb:
            return True, got
        await jump_to_first_item(page)
        await asyncio.sleep(1.5)
        got = await _title(page)
    gb = bare(got or "")
    return (not gb) or gb in want or want in gb, got


async def wait_position_settled(page, timeout=14.0, need=3):
    """等 WeRead 把「上次读到的位置」恢复完：顶栏连续 need 次不变就算落定。

    这一等是「开头抓不到」的正面解法。阅读器打开后会异步地把画面切回上次读到的页，
    而我们跳完开头就走 —— 恢复动作落地时正好把开头顶掉（实测点一下中心就会触发它）。
    所以先点一下中心把它唤醒、等它稳定，再跳开头，跳转才守得住。
    """
    prev, same, t0 = "", 0, time.time()
    while time.time() - t0 < timeout:
        cur = await _title(page) or ""
        if cur and cur == prev:
            same += 1
            if same >= need:
                return cur
        else:
            same = 0
        prev = cur
        await asyncio.sleep(0.6)
    return prev


async def hook_blocks(page, seen_imgs):
    """把绘制钩子里现在攒着的内容变成有序块（不翻页、不清空，抓到的就是当下这一屏）。"""
    return await snapshot(page, seen_imgs)


def first_catalog_title(catalog_path):
    """目录首项（丢掉「当前读到 xx%」这类尾巴）。"""
    try:
        with open(catalog_path, encoding="utf-8") as f:
            return re.split(r"当前读到", (json.load(f) or [""])[0])[0].strip()
    except Exception:
        return ""


def front_title_index(blocks, want):
    """在全书首屏的块里找出「第一个目录标题」从哪一块开始；标题被拆成几行也认。

    跳转前清空过钩子，正常情况下第一块就是它；万一阅读器的恢复动作抢了跑，
    前面会混进上次读到的那一页 —— 从标题处起收，把残留丢掉。找不到返回 None。
    """
    if not want:
        return 0
    bars = [bare(b["text"]) if b["type"] == "text" else "" for b in blocks]
    span = ChapterSplitter.HOLD_MAX_PIECES + 2
    for i in range(len(bars)):
        if not bars[i]:
            continue
        acc = ""
        for j in range(i, min(i + span, len(bars))):
            if not bars[j]:
                continue
            acc += bars[j]
            if acc.startswith(want):
                return i
            if not want.startswith(acc):
                break
    return None


async def dump_catalog_titles(page, catalog_path):
    """打开目录、把标题清单存盘（分章要用），再关掉目录。"""
    try:
        await dismiss_masks(page)
        await page.click("button.readerControls_item.catalog", timeout=5000)
        await asyncio.sleep(1.5)
        titles = await page.evaluate("""() => Array.from(
            document.querySelectorAll('.readerCatalog_list_item')).map(el => el.textContent.trim())""")
        if titles and catalog_path:
            with open(catalog_path, "w") as f:
                json.dump(titles, f, ensure_ascii=False)
        try:
            await page.click("button.readerControls_item.catalog", timeout=2000)
        except Exception:
            await page.keyboard.press("Escape")
        await asyncio.sleep(1)
    except Exception as e:
        print(f"  ⚠️  读取目录异常: {e}")



def save_chapter(ch_title, blocks, ch_idx, md_dir, raw_dir):
    body, img_records = render_chapter_md(ch_title, blocks, ch_idx)
    text_len = sum(len(b["text"]) for b in blocks if b["type"] == "text")
    if text_len == 0 and not img_records:
        return 0, []
    with open(os.path.join(md_dir, f"{ch_idx:04d}.md"), "w") as f:
        f.write(body)
    with open(os.path.join(raw_dir, f"{ch_idx:04d}.json"), "w") as f:
        json.dump({"title": ch_title, "images": img_records, "text_len": text_len},
                  f, ensure_ascii=False)
    return text_len, img_records


async def goto_ready(page, url, timeout=30000, need_canvas=True):
    """打开页面并等它真正可用。

    不要用 wait_until="networkidle"：微信读书是持续心跳的 SPA，网络永远不会静默，
    等 networkidle 就是在赌运气（实测 2026-09-29 30 秒超时直接失败）。
    改成 domcontentloaded + 等一个真实就绪标志（阅读器有 canvas）。
    """
    await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
    if need_canvas:
        try:
            await page.wait_for_selector("canvas", timeout=15000)
        except Exception:
            pass
    else:
        await asyncio.sleep(3)


async def run_session(book_id, md_dir, raw_dir, start_idx, seen_imgs,
                      goto_first=False, catalog_path=None,
                      book_dir=None, deadline=None):
    reached_end = False
    t0 = time.time()
    # deadline = 整次导出还剩下多少秒（墙钟）。本轮最多用 min(单轮预算, 剩余)。
    t_left = SESSION_BUDGET if deadline is None else max(30, min(SESSION_BUDGET, deadline))
    # 累计页数：会话之间会重开浏览器、page_num 从 0 重来，但进度条不能倒退，
    # 所以把上一轮已经翻过的页加回来。
    page_base = 0
    if book_dir:
        try:
            page_base = int(json.load(open(os.path.join(book_dir, "_progress.json"),
                                           encoding="utf-8")).get("pages", 0))
        except Exception:
            page_base = 0
    last_cat_title = load_last_catalog_title(catalog_path) if catalog_path else ""
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            USER_DATA_DIR, headless=HEADLESS, viewport={"width": 1200, "height": 900},
            args=CHROME_ARGS)

        login_page = await ctx.new_page()
        await goto_ready(login_page, "https://weread.qq.com/web/shelf", need_canvas=False)
        # 登录状态连查三次再下结论：实测第二轮会话第一次查报「未登录」，
        # 而 cookie 明明还在（第一轮刚抓完全书）—— 单次查询失败多半是页面/接口抖动。
        # 误判成掉线会让脚本安静地等 10 分钟扫码，看起来就是「点了没反应/抓不到」。
        logged = False
        for _try in range(3):
            if await run_bounded(is_logged_in(ctx), 30, "检查登录状态"):
                logged = True
                break
            await asyncio.sleep(2.5)
        if not logged:
            print("\n  ⚠️  请扫码登录微信读书")
            try:
                await login_page.click(".navBar_link_Login", timeout=6000)
            except Exception:
                pass
            for _i in range(120):
                await asyncio.sleep(5)
                if STOP:
                    print("  ⏹ 已请求停止，不再等扫码")
                    await login_page.close(); await ctx.close()
                    return "", "", 0, 0, start_idx, False
                if await run_bounded(is_logged_in(ctx), 30, "检查登录状态"):
                    print("  ✅ 登录成功"); break
                if (_i + 1) % 12 == 0:
                    # 等扫码期间原来是一声不吭的，看着像卡死
                    print(f"  …还在等扫码（已等 {(_i + 1) * 5} 秒）", flush=True)
            else:
                await login_page.close(); await ctx.close()
                return "", "", 0, 0, start_idx, False
        else:
            print("  ✅ 已登录")
        await login_page.close()

        page = await ctx.new_page()
        await page.add_init_script(CANVAS_HOOK)
        print("\n  打开阅读器...")
        await goto_ready(page, f"https://weread.qq.com/web/reader/{book_id}")
        await asyncio.sleep(4)

        book_title, book_author = await run_bounded(fetch_book_title(page), 30, "读取书名")
        if book_title and book_title != "未知":
            BOOK_INFO["title"] = book_title
        if book_author:
            BOOK_INFO["author"] = book_author
        if goto_first:
            # 阅读器打开后会异步把画面恢复到「上次读到的位置」，而点一下正文中心正是
            # 唤醒它的动作。所以这一下必须放在「跳开头之前」：先点、等它落定，再跳开头，
            # 跳转才守得住。老代码顺序反了（先跳再点），恢复动作在跳转之后才落地，
            # 实测《改变你一生的100个好习惯》的 版权信息 / 序言 / 第一支柱 / 一 思维模式
            # 四章就是这样凭空消失的。
            # 反过来，跳完开头之后就再也不能点正文中心了 —— 目录跳转并不更新它的
            # 「上次读到」记录，所以那点下去永远会被拽回旧位置（实测连点 6 次被拽 6 次，
            # 见 one_turn 的注释），翻页全程只用方向键。
            await page.mouse.click(600, 450)
            await asyncio.sleep(0.8)
            await run_bounded(wait_position_settled(page), 25, "等阅读器恢复上次位置")
            await run_bounded(dump_catalog_titles(page, catalog_path), PREP_TIMEOUT, "读取目录")
            last_cat_title = load_last_catalog_title(catalog_path)
            # 钩子在跳转前一刻清空：开头这一屏新画出来的文字就完整留在里面，
            # 抓取时一次翻页都不用按（one_turn 是「翻页→抓」，落地页永远抓不到）。
            await page.evaluate("() => window.__wr_reset()")
            ok, now_at = await run_bounded(ensure_first_chapter(page, catalog_path),
                                           PREP_TIMEOUT, "跳到并确认全书开头")
            if ok:
                print(f"  ✅ 已停在开头:「{now_at}」")
            else:
                print(f"  ⚠️  没能跳回全书开头（当前:「{now_at}」）—— 开头的章节可能会缺，"
                      "重跑一次取书通常能补上")
        else:
            # 续传：先唤醒恢复、并等它落定，再开始翻页。
            # 不等的话恢复动作会在翻到一半时落地，把画面从断点上拽走。
            await page.mouse.click(600, 450)
            await asyncio.sleep(0.8)
            await run_bounded(wait_position_settled(page), 25, "等阅读器恢复上次位置")

        current_chapter = await run_bounded(_title(page), 20, "读取章节名")
        print(f"  📖 {book_title} — {book_author}")
        print(f"  会话开始:「{current_chapter}」\n")

        written_idx = start_idx
        total_chars = total_imgs = 0
        stale = 0
        page_num = 0
        # 整页文字指纹（最近 DUP_WINDOW 页）。用来认出「翻页其实没往前」——
        # 见 capture_current_page 里的注释。
        recent_sigs = deque(maxlen=DUP_WINDOW)

        def pulse(force=False):
            """把当前进展写进 _progress.json，界面据此推进度条"""
            if book_dir is None:
                return
            if not force and page_num % 3:
                return
            write_progress(book_dir,
                           pages=page_base + page_num,
                           chapters=len([f for f in os.listdir(md_dir) if f.endswith(".md")]),
                           session_chars=total_chars,
                           # captured：抓到手的全部字数（含还没切章落盘的）。
                           # 光报「已写出」会在分章还没跑出来时显示成 0，看着像没干活。
                           captured=cap_chars,
                           budget_left=max(0, int(t_left - (time.time() - t0))))

        # 连续累积 + 按正文标题切章。
        # 分章逻辑抽成 ChapterSplitter（模块级、可单测）：既认整行标题，
        # 也认被排版拆成 2~5 行的标题 —— 后者是 2026-09-30「翻了几十页 0 章 0 字」的根因。
        titles = load_catalog_titles(catalog_path) if catalog_path else []
        used = load_used_titles(md_dir)
        sp = ChapterSplitter(titles, used)
        # 目录里去掉重名后到底有多少章 —— 用来认出「已经全切完了」，
        # 不然翻到头还会继续翻，阅读器只会把同一页反复吐回来。
        cat_uniq = len({n for n, _ in titles})
        written = 0             # 本次会话真正写出内容的章节数
        cap_chars = 0           # 本次会话抓到的正文字数（还没落盘的部分也算）

        def write_ready():
            nonlocal written, written_idx, total_chars, total_imgs
            for title, seg in sp.pop_completed():
                n, imgs = save_chapter(title or "未题名", seg, written_idx, md_dir, raw_dir)
                if not (n or imgs):
                    continue        # 空章不落盘，也不占编号
                total_chars += n
                total_imgs += len(imgs)
                written += 1
                note = f" +{len(imgs)}图" if imgs else ""
                print(f"  [{written_idx:4d}] {(title or '未题名')[:32]:32s} {n:6d}字{note}",
                      flush=True)
                written_idx += 1

        async def capture_current_page(top="", blocks=None):
            """抓当前渲染出的块；返回这一页是否真的带来了新内容"""
            nonlocal cap_chars
            if blocks is None:
                blocks = await snapshot(page, seen_imgs)
            # 文字去重：同一页可能重复捕获
            cleaned = []
            for b in blocks:
                if b["type"] == "text":
                    prev = sp.cur[-1] if sp.cur else None
                    if prev and prev.get("type") == "text" and prev["text"] == b["text"]:
                        continue
                    if cleaned:
                        last = cleaned[-1]
                        if last.get("type") == "text" and last["text"] == b["text"]:
                            continue
                cleaned.append(b)

            # 整页指纹：这是「像死循环 + 进度条不动」的真正原因。
            # 新版阅读器翻到后面会复用同一批已经绘制好的内容，每次抓到的都是同一页文字；
            # 原来的单块去重只跟「上一个块」比，整页重放照样全通过 → got 恒 >0 →
            # stale 永不累加 → 一路翻到 MAX_PAGES（3000 页 ≈ 75 分钟）。
            sig = hashlib.md5("\n".join(
                (b.get("text") or b.get("url") or b.get("file") or "")
                for b in cleaned).encode("utf-8")).hexdigest()
            if sig in recent_sigs:
                return 0                     # 这页是最近几页的重播，不算新内容
            recent_sigs.append(sig)
            cap_chars += sum(len(b.get("text") or "") for b in cleaned if b["type"] == "text")

            # 正文里的标题一个都没认出来时（图片标题、异形排版），退回用顶栏断章。
            # 顶栏必须整条等于一个「还没用过的目录项」才算数，噪声（子小节名、
            # 「当前读到 42%」）对不上，所以不会像老版本那样把一章撕碎。
            if titles and sp.cuts == 0 and top:
                sp.boundary_by_topbar(top)

            sp.push(cleaned)
            write_ready()

            # 保底：攒了上万字还没切章，先落一盘再继续（内容不只在内存里活着，
            # 中途被中止也不会整批蒸发）。
            if sp.captured_chars() >= AUTO_FLUSH_CHARS:
                sp.force_break(await _title(page) if titles else "")
                write_ready()
            return len(cleaned)

        # 每一轮「翻一页 + 抓这一页」整体限时；超时即认定渲染进程卡死，
        # 抛出去让外层重开会话（已经落盘的章节不受影响，从断点续传）。
        #
        # 翻页只按方向键，绝对不点正文中心。
        # 那一下点击会触发微信读书「回到上次阅读位置」：实测（2026-09-30，
        # /tmp/probe_stabilize.py）跳到开头后连点 6 次，6 次都被拽回「二 情绪健康」，
        # 因为目录跳转根本不更新它的阅读记录，旧记录一直悬在前面 —— 于是
        # 「第一支柱」「一 思维模式」整章被跨过去，凭空消失。
        # 而「不点就拿不到整屏」这个顾虑已经被实测推翻：/tmp/probe_jumpsrc.py 里
        # 纯方向键每一轮正好前进一页，抓回来的是完整一整屏（标题行 + 正文都在）。
        # 之前「不点就重复吐 25 万字」的那次，是书的末尾翻不动了还在吐同一页，
        # 现在由 cat_uniq（目录切完就收工）和整页指纹兜住，不再靠点击续命。
        async def one_turn():
            await page.evaluate("() => window.__wr_reset()")
            await page.keyboard.press("ArrowRight")
            # 按完就盯采样，画完立刻走 —— 不再无条件先睡一秒（速度就出在这儿）
            await wait_settled(page)
            # 顶栏只在「正文标题还没切出过章」时才要（省一次调用），
            # 一旦正文分章正常工作，就不再拿顶栏参与判定。
            top = await _title(page) if (titles and sp.cuts == 0) else ""
            return await capture_current_page(top)

        # 落地这一页必须先抓一次，但只在「这次是从全书开头起跑」时抓。
        # one_turn 是「翻页 → 抓这一页」，所以刚跳到的那一页永远抓不到。
        # 钩子已经在跳转前清过空，此刻里面正是开头那一屏，直接收下即可。
        # 续传时不能这么干 —— 阅读器停在上一轮已经写过的那一页，抓了就是把同一页写两遍。
        async def capture_landing():
            blocks = await hook_blocks(page, seen_imgs)
            if not blocks:
                # 跳到开头之后，那一屏常常根本不长新东西 —— 实测从跳完那一刻逐秒
                # 轮询 15 秒，钩子一直是空的（见 /tmp/probe_landingpaint.py）：
                # 阅读器认为它已经画过了，就不重画，钩子也就永远抓不到开头这一页。
                # 管用的是「往前翻一页再退回来」：两次翻页都强制重画，
                # 落地那一屏的文字随后就到钩子里了（实测 51 + 6 块，首屏 6 块排在后面）。
                # 前进那一屏先记下块数，切掉不要 —— 循环下一轮按方向键回到那一页时
                # 会重画、会正式抓它，这里留着反倒写两遍。
                mark = set(seen_imgs)
                await page.evaluate("() => window.__wr_reset()")
                await page.keyboard.press("ArrowRight")
                await wait_settled(page)
                n_fwd = len(await snapshot(page, seen_imgs))
                seen_imgs.clear()
                seen_imgs.update(mark)        # 那一屏的图稍后还要重新算一次
                await page.keyboard.press("ArrowLeft")
                await wait_settled(page)
                blocks = await snapshot(page, seen_imgs)
                if n_fwd and len(blocks) > n_fwd:
                    blocks = blocks[n_fwd:]
                elif not blocks:
                    # 连翻带退都榨不出东西：退回用整屏原始钩子，收尾靠目录核对缺章。
                    await page.evaluate("() => window.__wr_reset()")
                    await jump_to_first_item(page)
                    await wait_settled(page)
                    blocks = await snapshot(page, seen_imgs)
            want = bare(first_catalog_title(catalog_path)) if titles else ""
            at = front_title_index(blocks, want)
            top = await _title(page) if titles else ""
            if at is None:
                print(f"  ⚠️  开头这一屏认不出首章标题「{want}」（当前:「{top}」），"
                      f"按原样收下 {len(blocks)} 块，收尾时会核对目录是否缺章")
                at = 0
            elif at:
                print(f"  开头这一屏前 {at} 块是「上次读到」的残留，丢掉")
            return await capture_current_page(top, blocks=blocks[at:])

        if goto_first:
            landed = await run_bounded(capture_landing(), ITER_TIMEOUT, "开头这一页")
            print(f"  开头这一页抓到 {landed} 块（不抓就会丢掉全书最前面的内容）", flush=True)

        # 首页：开头那一屏已经由 capture_landing 收下了，这里从「翻一页」开始。
        await run_bounded(one_turn(), ITER_TIMEOUT, "开头后的第一轮")

        while page_num < MAX_PAGES:
            if STOP:
                print("  ⏹ 已请求停止，正在把抓到的内容落盘…")
                break
            # 墙钟预算：到了点就收尾，交回外层决定是否续。
            # 「一直有内容但永远翻不完」是原实现里唯一没设上限的出口，
            # 一旦上面的整页指纹判断没兜住，这里就是最后的闸门。
            if t_left is not None and time.time() - t0 > t_left:
                print(f"  本轮时间到（{int(t_left)} 分钟预算用完），收尾")
                break

            # 心跳：让"在动"这件事看得见（原实现只在写出章节时才有输出，
            # 一旦翻页不顺就完全静默，用户只能看到"不动了"）
            if page_num and page_num % 10 == 0:
                held = sp.captured_chars()
                print(f"    …已翻 {page_num} 页 · 本次写出 {written} 章 / {total_chars:,} 字"
                      f"（已抓 {cap_chars:,} 字，在手 {held:,} 字待切章）", flush=True)
            got = await run_bounded(one_turn(), ITER_TIMEOUT, f"第 {page_num + 1} 页")
            page_num += 1
            pulse()

            if titles and cat_uniq and len(used) >= cat_uniq:
                # 目录全切完了。之前只会一路翻到「连续 12 页无新内容」，
                # 实测到头之后阅读器还在重复吐同一批内容，结语一章被连写了 9 遍、
                # 一本书吐出 25 万字 —— 认完就收工，别再翻。
                print("  ✅ 目录里的章节已全部切出，收工")
                break

            if got:
                stale = 0
            else:
                stale += 1
                if stale == 2:
                    # 连着两页没动静：多半是目录/提示浮层还盖着，按键落不到阅读器上。
                    # 只关浮层：不补按方向键（补按的那一页没人抓，等于白丢一页），
                    # 也不点正文中心（一点就被拽回旧阅读位置，见 one_turn 注释）。
                    await dismiss_masks(page)
                    await asyncio.sleep(0.5)
                if stale >= STALE_LIMIT:
                    print(f"  连续 {STALE_LIMIT} 页无新内容，本轮收尾")
                    break

        # 收尾：把手上没切完的那一章也落盘（中止、超预算、翻到头都走这条路）
        sp.close()
        write_ready()
        pulse(force=True)

        # 是否到头：目录里最后一个标题已经切出过。
        # 注意不能用 titles[-1] —— load_catalog_titles 为了「长标题优先匹配」按长度降序排过，
        # 排在末位的是最短的那条（实测是「一 思维模式」），拿它当结尾判据永远判不出「完成」，
        # 于是明明已经到「结语」了还要白跑一轮会话（界面看到的就是「抓完还不停」）。
        if titles:
            last_nt = norm_title(re.split(r"当前读到", load_last_catalog_title(catalog_path))[0])
            reached_end = bool(last_nt) and last_nt in used
            if not reached_end:
                miss = [o for n, o in titles if n not in used]
                if miss:
                    print(f"  还没切出来的目录项：{' / '.join(m[:18] for m in miss[:8])}"
                          + (f" 等 {len(miss)} 项" if len(miss) > 8 else ""))
        else:
            reached_end = stale >= STALE_LIMIT

        await page.close(); await ctx.close()
        return book_title, book_author, written, total_chars, written_idx, reached_end


def download_all_images(raw_dir, img_dir):
    os.makedirs(img_dir, exist_ok=True)
    tasks = []
    for jf in sorted(os.listdir(raw_dir)):
        if jf.endswith(".json"):
            for img in json.load(open(os.path.join(raw_dir, jf))).get("images", []):
                tasks.append((img["url"], img["file"]))
    if not tasks:
        print("  (无图片)"); return 0
    print(f"\n  下载 {len(tasks)} 张图片...")
    ok = 0
    for url, fname in tasks:
        fp = os.path.join(img_dir, fname)
        if os.path.exists(fp) and os.path.getsize(fp) > 1000:
            ok += 1; continue
        try:
            req = urllib.request.Request(url, headers={
                "Referer": "https://weread.qq.com/", "User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=20).read()
            if len(raw) > 500:
                open(fp, "wb").write(raw); ok += 1
                if ok % 20 == 0:
                    print(f"    {ok}/{len(tasks)}...")
        except Exception as e:
            print(f"    ⚠️  {fname} 失败: {e}")
    print(f"  ✅ 图片下载完成 {ok}/{len(tasks)}")
    return ok


async def main(book_id):
    print("=" * 60)
    print("  weread-exporter — 精确图文导出 v3")
    print("=" * 60)
    install_stop_handler()
    if os.environ.get("EXPORT_DEBUG"):
        # 卡住时每 60s 把 Python 调用栈倒出来，用来定位到底停在哪一行
        import faulthandler
        faulthandler.dump_traceback_later(60, repeat=True, exit=False)
        print("  (EXPORT_DEBUG 已开：每 60s 倾倒一次调用栈)")
    os.makedirs(USER_DATA_DIR, exist_ok=True)
    book_dir = os.path.join("output", book_id)
    md_dir = os.path.join(book_dir, "chapters")
    raw_dir = os.path.join(book_dir, "raw")
    img_dir = os.path.join(book_dir, "images")
    for d in (md_dir, raw_dir, img_dir):
        os.makedirs(d, exist_ok=True)

    seen_imgs = set()
    for jf in os.listdir(raw_dir):
        if jf.endswith(".json"):
            for img in json.load(open(os.path.join(raw_dir, jf))).get("images", []):
                seen_imgs.add(img["url"])

    catalog_path = os.path.join(book_dir, "_catalog.json")
    meta_path = os.path.join(book_dir, "meta.json")
    meta = {}
    if os.path.exists(meta_path):
        try:
            meta = json.load(open(meta_path, encoding="utf-8"))
        except Exception:
            meta = {}
    book_title = book_author = ""
    # 书名要能认出来：界面取书时写的 meta.json 有标题，第一轮会话抛异常没返回也不能丢，
    # 否则合并出的 md 会用书号当文件名（看着就像「导出了一堆没名字的东西」）。
    if meta.get("title") and meta["title"] != book_id:
        book_title = meta["title"]
    if meta.get("author"):
        book_author = meta["author"]
    run_t0 = time.time()
    write_progress(book_dir, running=True, started_at=run_t0, book=book_id,
                   pages=0, chapters=0, session_chars=0, finished_at=None)
    session = 0
    fails = 0
    # 只有真的翻到目录末项才算「全书完成」。之前是无条件写 done=True：
    # 中途卡 3 次停掉、时间预算用完，界面照样显示「导出成功」，
    # 用户按成功去用那份 md，才发现少了大半 —— 这是比抓不到更糟的骗人。
    full_book = False
    stop_reason = ""
    while session < MAX_SESSIONS:
        if STOP:
            stop_reason = "用户中止"
            print("\n  ⏹ 已请求停止：已抓到的章节都在盘上，再点一次取书会从断点继续。")
            break
        session += 1
        left = TOTAL_BUDGET - (time.time() - run_t0)
        if left <= 30:
            stop_reason = f"整次导出超过 {TOTAL_BUDGET // 60} 分钟"
            print(f"\n  ⏳ 整次导出已到 {TOTAL_BUDGET // 60} 分钟上限，先收尾。"
                  "已导出的章节都在，再点一次取书会从断点继续。")
            break
        last_title, last_idx = get_last_chapter_title(md_dir)
        start_idx = last_idx + 1 if last_idx > 0 else 1
        print(f"\n--- 会话 {session} ---")
        print(f"  上次: {last_title or '(无)'}, 编号: {last_idx}")
        goto_first = (session == 1 and last_idx == 0)
        kill_stray_browsers()   # 上一轮卡死/异常留下的浏览器会占着 profile，先清掉再起
        try:
            title, author, added, chars_added, end_idx, reached_end = await run_session(
                book_id, md_dir, raw_dir, start_idx, seen_imgs,
                goto_first=goto_first, catalog_path=catalog_path,
                book_dir=book_dir, deadline=left)
            fails = 0
        except Exception as e:
            # 浏览器被关掉 / 渲染进程卡死 / 页面崩掉：都不丢弃已导出的章节，
            # 重开一轮浏览器从上次断点续传（脚本本来就支持按章节续传）。
            fails += 1
            head = "页面卡死" if isinstance(e, PageStalled) else type(e).__name__
            print(f"\n  ⚠️  会话中断（{head}）: {str(e)[:180]}")
            write_progress(book_dir, stalled=head)
            # 这一轮虽然炸了，但读到的书名不能跟着炸掉
            if not book_title and BOOK_INFO["title"]:
                book_title = BOOK_INFO["title"]
            if not book_author and BOOK_INFO["author"]:
                book_author = BOOK_INFO["author"]
            if fails >= 3:
                stop_reason = "连续 3 次会话中断"
                print("  ❌ 连续 3 次中断，停止。已导出的章节保留，可再点一次取书继续。")
                break
            print(f"  重开浏览器续传（第 {fails}/3 次）...")
            await asyncio.sleep(2)
            continue
        if title and title != "未知": book_title = title
        if author: book_author = author
        print(f"\n  本次: +{added} 章, +{chars_added:,} 字")
        if reached_end:
            full_book = True
            print("\n  ✅ 已到全书最后一章（目录末项已在正文中切出），导出完成。"); break
        if added == 0:
            # 关键：这里数的是「真的写出了内容的章节数」。
            # 旧实现数的是「顶栏标题变化次数」，而顶栏每翻一页都会变 → 每轮都 >0 → 死循环。
            stop_reason = "本轮没有写出新章节"
            print("\n  本轮没有写出任何新章节，先收尾。"); break
        print("  3 秒后自动重开继续..."); await asyncio.sleep(3)
    else:
        stop_reason = f"会话数达到上限 {MAX_SESSIONS}"
    if session >= MAX_SESSIONS:
        print(f"\n  ⚠️  已达会话上限 {MAX_SESSIONS}，停止（已导出的章节保留）")

    # 目录覆盖核对：到底缺哪几章，直接报名字。
    # 「22 章 / 12 万字」听着挺多，可目录有 26 项 —— 不点出来用户只能自己一章系数一章。
    cat_titles = load_catalog_titles(catalog_path)
    on_disk = load_used_titles(md_dir)
    missing = [o for n, o in cat_titles if n not in on_disk]
    if missing and cat_titles:
        print(f"\n  ⚠️  目录 {len(cat_titles)} 项里还有 {len(missing)} 项没切出来："
              + " / ".join(m[:20] for m in missing[:6])
              + ("…" if len(missing) > 6 else ""))
        # 翻到末项 ≠ 全本：开头的版权信息/序言这类「天生容易被跳过」的章节缺失时，
        # 末项照样能切出来，之前就是这样把缺了 4 章的导出报成「全书完成」的。
        if full_book:
            full_book = False
            stop_reason = f"目录缺 {len(missing)} 项（{' / '.join(m[:12] for m in missing[:3])}…）"

    download_all_images(raw_dir, img_dir)

    total_files = sorted(f for f in os.listdir(md_dir) if f.endswith(".md"))
    img_count = len([f for f in os.listdir(img_dir) if not f.startswith(".")])
    write_progress(book_dir, running=False, chapters=len(total_files),
                   images=img_count, finished_at=time.time(),
                   minutes=round((time.time() - run_t0) / 60, 1))
    book_title = book_title or BOOK_INFO["title"] or meta.get("title") or book_id
    if book_title == "未知": book_title = book_id
    safe = re.sub(r'[<>:"/\\|?*]', '_', book_title)
    merged = os.path.join("output", f"{safe}.md")
    with open(merged, "w") as out:
        out.write(f"# {book_title}\n\n**{book_author}**\n\n---\n\n")
        for fn in total_files:
            out.write(open(os.path.join(md_dir, fn)).read())
            out.write("\n\n---\n\n")
    # 早先认不出书名时留下过「<书号>.md」这份合并文件。书名认出来之后它就是同一本书的
    # 另一份拷贝（两份内容一样、名字不一样，翻起来最容易搞混），所以顺手清掉旧的。
    stale = os.path.join("output", f"{book_id}.md")
    if safe != book_id and os.path.isfile(stale):
        try:
            os.remove(stale)
            print(f"  🧹 已清掉旧的书号命名拷贝：{stale}")
        except OSError:
            pass
    print(f"\n{'=' * 60}")
    if full_book:
        print(f"  ✅ 全书导出完成!  📖 {book_title} — {book_author}")
    else:
        # 没跑完就直说没跑完：写「导出成功」是让用户拿着缺章的文件当全本用。
        print(f"  ⚠️  这次没有跑完全书（{stop_reason or '中途结束'}）📖 {book_title}")
        print(f"     已导出的 {len(total_files)} 章都在盘上，再点一次取书会从断点继续。")
    print(f"  📄 {len(total_files)} 章, {os.path.getsize(merged):,} bytes,  🖼 {img_count} 张图")
    print(f"  📦 {merged}")
    print(f"{'=' * 60}")

    # 由引擎自己落完成标记：无论这次是界面跑的、命令行跑的，还是 MCP 触发的，
    # 前端的百分比与「导出成功」都以此为据（之前只在 ui_server 侧写，命令行跑的就漏了）。
    meta_path = os.path.join(book_dir, "meta.json")
    if os.path.exists(meta_path):
        try:
            meta = json.load(open(meta_path, encoding="utf-8"))
        except Exception:
            meta = {}
    meta.update({
        "title": book_title if book_title != book_id else (meta.get("title") or book_id),
        "author": book_author or meta.get("author") or "",
        "done": full_book,
        "chapters": len(total_files),
        "missing": missing,
        "catalog_total": len(cat_titles),
        "updated_at": time.time(),
    })
    if full_book:
        meta.pop("stopped_reason", None)
    else:
        meta["stopped_reason"] = stop_reason or "中途结束"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    argv = [a for a in sys.argv[1:] if a != "--headed"]
    if "--headed" in sys.argv:
        HEADLESS = False
        print("  (已启用可见窗口模式)")
    if not argv:
        print("用法: python export_precise.py <book_url_or_id> [--headed]"); sys.exit(1)
    raw = argv[0].strip().rstrip("/")
    book_id = raw.split("/")[-1] if "weread.qq.com" in raw else raw
    print(f"  Book ID: {book_id}")
    asyncio.run(main(book_id))
