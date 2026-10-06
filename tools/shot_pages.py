# -*- coding: utf-8 -*-
"""给 README 拍一版展示图（1.0.6）。

README 上挂着的那几张图，得是「界面上真能点出来的样子」，不能拿调试里截的半张。
所以这一份自己起一个 ui_server，数据目录是系统临时目录里的沙盒，跑完连目录一起删 ——
用户真实的书库与仓库 cache 一个字节都不碰。素材全部来自 tests/seed.py 铺的假书
（书名是编的，不是真书），安娜的档案那一屏走前端桩，也不碰网络、不开真窗口。

拍九张，落到 docs/shots/：
  home     我的书架（默认那一屏的瀑布铺法）
  reader   阅读器（左目录 / 中正文 / 右这套笔记）
  search   搜书（微信读书书城：搜到 → 加入书架 → 整本取回）
  anna     安娜的档案（工具自己开一个浏览器窗口，这一屏是它接住了什么）
  plan     每本书的阅读计划（详情页定完之后的读数）
  mindmap  笔记脑图（从笔记按标签生成一张图）
  feed     订阅阅读器（订上源、点开一篇）
  video    视频转笔记（挑一本转好的，看转写段落）
  flomo    便签（导入一份假导出包）

用法：
    .venv/bin/python tools/shot_pages.py            # 九张全拍
    .venv/bin/python tools/shot_pages.py home feed  # 只重拍某几张
"""
import atexit
import base64
import http.server
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
# 仓库根也进路径：拍「安娜的档案」那张要直接用 anna_state 那套读写，
# 免得在这里手搓一份 JSON 结构、和后端悄悄跑岔。
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

# 沙盒路径先落进 os.environ：seed 的 book_dir() 就是读它来把书号翻成目录的。
SANDBOX = tempfile.mkdtemp(prefix="gz-shot-")
atexit.register(lambda: shutil.rmtree(SANDBOX, ignore_errors=True))
PY = sys.executable
os.environ.update(GUIZANG_SELFTEST_DIR=SANDBOX,
                  GUIZANG_DATA=os.path.join(SANDBOX, "cache"),
                  GUIZANG_BOOKS=os.path.join(SANDBOX, "books"),
                  GUIZANG_SHOT_DIR=os.path.join(SANDBOX, "shots"))
ENV = dict(os.environ)

import flomo_fixture  # noqa: E402
import seed as seed_mod  # noqa: E402
import anna_state  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

OUT = REPO / "docs" / "shots"
BOOK = "GAPBOOK1"
TITLE = "缺口试验这本"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ── 订阅夹具：一个带正文的 RSS，够点开一篇看右栏 ────────────────────
FIX_PORT = free_port()
FIX_BASE = "http://127.0.0.1:%d" % FIX_PORT


def mk_rss(title, n):
    items = []
    for i in range(1, n + 1):
        body = ("<p>%s 第 %02d 篇的正文。这一段用来验右栏排版，够长才看得出滚动。</p>"
                % (title, i)) + ("<p>补白补白补白补白。</p>" * 4)
        items.append(
            "<item><title>%s 第 %02d 篇</title><link>%s/p%d</link>"
            "<guid isPermaLink=\"false\">%s-%d</guid><author>作者%d</author>"
            "<pubDate>Thu, %02d Oct 2025 0%d:00:00 GMT</pubDate>"
            "<description>%s 第 %d 篇的摘要，中栏那行小字就是它。</description>"
            "<content:encoded><![CDATA[%s]]></content:encoded></item>"
            % (title, i, FIX_BASE, i, title, i, i % 5 + 1, 6 + (i % 20), i % 9 + 1,
               title, i, body))
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">'
            '<channel><title>%s</title><link>%s/</link><description>演示源</description>%s'
            '</channel></rss>' % (title, FIX_BASE, "".join(items))).encode("utf-8")


ROUTES = {"/tech.xml": (mk_rss("技术周刊", 40), "application/rss+xml")}


class FixHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        r = ROUTES.get(urllib.parse.urlparse(self.path).path)
        if r is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body, ctype = r
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


threading.Thread(target=http.server.ThreadingHTTPServer(
    ("127.0.0.1", FIX_PORT), FixHandler).serve_forever, daemon=True).start()

# ── 铺书架、起服务 ────────────────────────────────────────────────
for _d in ("cache", "books", "shots"):
    os.makedirs(os.path.join(SANDBOX, _d), exist_ok=True)
subprocess.run([PY, str(REPO / "tests" / "seed.py"), "--force"],
               cwd=str(REPO), env=ENV, capture_output=True)


def fatten(bid, times=14):
    """把展示用的那本书撑厚一点。

    seed 铺的章正文只有几百字，十折算下来「全书 4 页」—— 阅读计划那张图上写着 4 页，
    像本玩具书。这里把每章正文照抄几遍撑到几千字，页码才像真在读一本书（纯沙盒，
    跑完连目录一起删）。段首那几句还在，划词照样划得上。
    """
    d = pathlib.Path(seed_mod.book_dir(bid))
    for f in sorted((d / "chapters").glob("*.md")):
        head, sep, body = f.read_text(encoding="utf-8").partition("\n")
        body = body.strip()
        f.write_text(head + "\n\n" + (body + "\n\n") * times, encoding="utf-8")


fatten(BOOK)

PORT = free_port()
LOGP = os.path.join(SANDBOX, "server.log")
_logf = open(LOGP, "w")
SRV = subprocess.Popen([PY, "ui_server.py", "--port", str(PORT)],
                       cwd=str(REPO), env=ENV, stdout=_logf, stderr=subprocess.STDOUT)
atexit.register(lambda: SRV.terminate())
BASE = ""
for _ in range(160):
    try:
        m = re.search(r"http://127\.0\.0\.1:(\d+)", open(LOGP).read())
        if m:
            cand = "http://127.0.0.1:%s" % m.group(1)
            json.loads(urllib.request.urlopen(cand + "/api/state", timeout=1).read())
            BASE = cand
            break
    except Exception:
        pass
    time.sleep(0.25)
if not BASE:
    print("沙盒服务没起来：", open(LOGP).read()[-1500:])
    sys.exit(1)
print("展示图沙盒：", BASE, "→", OUT)


def post(path, payload):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode("utf-8"))


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


# 给 GAPBOOK1 铺一批划线与条目：正文那几句都取自 seed 的章内文字，划得上才看得见。
# 书名的字面用了「典籍 / 攻坚」这类与正文对得上的词。
NOTES = {
    "schema": 1, "marks": [
        {"id": "s1", "ch": "0000.md", "tag": "quote",
         "text": "把问题拆开之后，每一块都能单独验证。", "memo": "拆解 = 可验证的最小步。"},
        {"id": "s2", "ch": "0000.md", "tag": "doubt",
         "text": "读者在这里划一句，就能在右栏留下一条对应的笔记，两边互相跳转但不打断阅读。",
         "memo": "划与记分处两栏，别互相打断。"},
        {"id": "s3", "ch": "0000.md", "tag": "point",
         "text": "论点、疑问、可引用、待查，四种高亮颜色只是标签，不改变正文本身。"},
        {"id": "s4", "ch": "0001.md", "tag": "todo",
         "text": "导出时这份笔记会跟着书走，存在书自己的文件夹里，不落在某个人的家目录。"},
    ],
    "entries": [
        {"id": "se1", "title": "笔记跟着书走", "ch": "0000.md", "tag": "quote",
         "body": "导出时笔记落在书自己的文件夹里，不落在某个人的家目录。"},
        {"id": "se2", "title": "四种颜色当标签用", "ch": "0000.md", "tag": "point",
         "body": "高亮分色是语义标签，不是装饰：这句我打算怎么用，颜色就说清了。"},
    ],
}


def shot(page, name):
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / (name + ".png")
    page.screenshot(path=str(p))
    print("  拍好", p.name)


def wait_toast_gone(page, extra=400):
    """等浮字整条淡完再拍。

    浮字亮 2400ms 后撤掉 up，再花 240ms 淡出；按时间猜容易差一口气，图上留半截浮字。
    这里直接盯它有没有 up，撤了就再多等一个淡出。
    """
    try:
        page.wait_for_function(
            "() => { const el = document.querySelector('.whisper');"
            " return !el || !el.classList.contains('up'); }", timeout=8000)
    except Exception:
        pass
    page.wait_for_timeout(extra)


def new_page(br):
    return br.new_page(viewport={"width": 1440, "height": 900})


def open_app(page):
    page.goto(BASE + "/", wait_until="domcontentloaded")
    page.evaluate("() => localStorage.clear()")
    page.reload(wait_until="domcontentloaded")
    page.wait_for_timeout(1400)


# ── 各张的拍法 ────────────────────────────────────────────────────

def shot_home(page):
    page.evaluate("() => setView('shelf')")
    page.wait_for_function("() => document.querySelectorAll('#shelfWrap .sgrid > *').length > 4",
                           timeout=20000)
    page.wait_for_timeout(900)
    shot(page, "home")


def shot_reader(page):
    post("/api/mynotes", {"book": BOOK, "doc": NOTES})
    page.evaluate("() => setView('shelf')")
    page.wait_for_timeout(300)
    page.evaluate("() => openReader('%s', '%s', 'shelf')" % (BOOK, TITLE))
    page.wait_for_selector("#rdBody", timeout=15000)
    page.wait_for_timeout(1400)
    # 右栏要摆出「划线 + 想法」那一栏
    page.evaluate("() => { try { rdSetPane('notes', true); } catch (e) {} }")
    page.wait_for_timeout(900)
    shot(page, "reader")


def shot_search(page):
    # 搜书页只有微信读书一栏：桩一份书城搜索，书名全编的，不碰网络也不碰书库。
    def wr_route(route):
        try:
            req = json.loads(route.request.post_data or "{}")
        except Exception:
            req = {}
        if req.get("api_name") != "/store/search":
            route.continue_()
            return
        books = [{"bookId": "wr1", "title": "样书甲", "author": "张三", "cover": ""},
                 {"bookId": "wr2", "title": "示范集", "author": "李四", "cover": ""},
                 {"bookId": "wr3", "title": "长夜读本", "author": "王五", "cover": ""}]
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"ok": True, "data": {
                          "results": [{"books": [{"bookInfo": b} for b in books]}]}},
                          ensure_ascii=False))

    page.route("**/api/weread", wr_route)
    page.click('#nav button[data-v="search"]')
    page.wait_for_selector('#wrq', timeout=8000)
    page.wait_for_timeout(400)
    page.evaluate("() => { keySet = true; }")
    page.fill('#wrq', '样书')
    page.click('#wrgo')
    page.wait_for_timeout(900)
    wait_toast_gone(page)                # 等那句回执的浮字整条淡完再拍
    shot(page, "search")


def shot_anna(page):
    """「安娜的档案」这一屏。

    拍这张不真的开浏览器窗口 —— 展示图要说的是「这一栏会告诉你窗口接住了什么」，
    窗口里装的是别人家的网页，不是归藏的界面。所以只往沙盒的账本里铺一份样例：
    两本进了书架、一份格式不认、下载夹里留着一份 EPUB 和一份 Mobi。
    书名全是编的，文件写在临时沙盒里，跑完连目录一起删。
    """
    data = os.environ["GUIZANG_DATA"]
    incoming = anna_state.incoming_dir(data, create=True)
    for name, size in (("示范这本.epub", 240 * 1024), ("旧格式样本.mobi", 96 * 1024)):
        with open(os.path.join(incoming, name), "wb") as f:
            f.write(b"%PDF-ish" + b"0" * (size - 8))
    now = int(time.time())
    anna_state.write_state(data, {
        "domain": "annas-archive.is", "window": "closed", "keyword": "示范",
        "caught": 3, "imported": 2, "skipped": 1,
        "note": "窗口关掉了，接住的三份都处理完了",
        "books": [{"name": "示范这本.epub", "title": TITLE, "author": "张三",
                   "book": BOOK, "bytes": 240 * 1024, "chapters": 12, "at": now},
                  {"name": "长夜读本.epub", "title": "长夜读本", "author": "王五",
                   "book": "", "bytes": 318 * 1024, "chapters": 8, "at": now - 60}],
        "pending": [{"name": "旧格式样本.mobi",
                     "reason": "格式不认（现在只收 EPUB / PDF / TXT / Markdown），原文件留着没动",
                     "bytes": 96 * 1024, "at": now - 30}],
    })
    page.click('#nav button[data-v="anna"]')
    page.wait_for_selector("#anWinDot", timeout=8000)
    page.wait_for_timeout(1400)          # 那一格下载夹要等 listing 回来才有内容
    wait_toast_gone(page)
    shot(page, "anna")


def shot_plan(page):
    page.evaluate("() => renderDetailView('%s', '%s', '%s')" % (BOOK, BOOK, TITLE))
    page.wait_for_selector('.vpane[data-pane="detail"] .plan', timeout=8000)
    page.wait_for_timeout(500)
    page.fill('.vpane[data-pane="detail"] .plan #plNum', '3')
    page.click('.vpane[data-pane="detail"] .plan #plSave')
    page.wait_for_selector('.vpane[data-pane="detail"] .plan .big', timeout=8000)
    # 推到第 5 章读到一半，读数才有「已读 / 还剩」的样子
    post("/api/plan", {"act": "pos", "book": BOOK, "at": 4, "frac": 0.6})
    page.wait_for_timeout(400)
    page.evaluate("() => renderDetailView('%s', '%s', '%s')" % (BOOK, BOOK, TITLE))
    page.wait_for_selector('.vpane[data-pane="detail"] .plan .big', timeout=8000)
    wait_toast_gone(page)                # 等「计划定好了」那句浮字淡完
    shot(page, "plan")


def shot_mindmap(page):
    # 读到正文那一栏，从笔记按标签生成一张图
    page.evaluate("() => openReader('%s', '%s', 'shelf')" % (BOOK, TITLE))
    page.wait_for_selector("#rdBody", timeout=15000)
    page.wait_for_timeout(1000)
    page.click("#ntMap")
    page.wait_for_function(
        "() => document.getElementById('ntMapLayer').classList.contains('open')", timeout=8000)
    page.wait_for_timeout(700)
    page.click("#mmFrom")
    page.wait_for_function("() => !document.getElementById('mmAsk').hidden", timeout=8000)
    page.wait_for_timeout(300)
    page.click("#mmAskAlt button:nth-child(1)")     # 按标签
    # 盘上还没有这张图，直接铺；等节点出来
    try:
        page.wait_for_function(
            "() => document.querySelectorAll('.mmnode').length >= 4", timeout=25000)
    except Exception:
        pass
    # 括号图靠左码，右边空一截；换成「辐射图」自中心铺开，再缩到能看全
    page.click('#mmForm button[data-v="radial"]')
    page.wait_for_timeout(700)
    page.click("#mmZoomFit")
    page.wait_for_timeout(900)
    shot(page, "mindmap")
    page.click("#ntMapClose")
    page.wait_for_timeout(300)


def shot_feed(page):
    post("/api/feed", {"act": "add", "url": FIX_BASE + "/tech.xml", "group": "技术"})
    page.click('#nav button[data-v="feed"]')
    page.wait_for_function("() => document.querySelectorAll('#fList .frow').length > 5",
                           timeout=40000)
    page.wait_for_timeout(500)
    page.locator("#fList .frow").first.click()
    page.wait_for_function("() => !!document.querySelector('#fRead .rdbd .md')", timeout=20000)
    page.wait_for_timeout(900)
    shot(page, "feed")


def shot_video(page):
    page.click('#nav button[data-v="video"]')
    page.wait_for_selector("#vShelf .vtbook", timeout=20000)
    page.wait_for_timeout(600)
    page.click('#vShelf .vtbook')
    try:
        page.wait_for_function(
            "() => document.querySelectorAll('#vRows .vtrow').length >= 4", timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(800)
    shot(page, "video")


def shot_flomo(page):
    zip_b64 = base64.b64encode(flomo_fixture.export_zip()).decode()
    post("/api/flomo/notes", {"act": "import", "name": "flomo-demo.zip", "data": zip_b64})
    page.click('#nav button[data-v="flomo"]')
    try:
        page.wait_for_selector("#fmScr .fmcard", timeout=12000)
    except Exception:
        pass
    page.wait_for_timeout(900)
    shot(page, "flomo")


SHOTS = {
    "home": shot_home, "reader": shot_reader, "search": shot_search, "anna": shot_anna,
    "plan": shot_plan,
    "mindmap": shot_mindmap, "feed": shot_feed, "video": shot_video, "flomo": shot_flomo,
}
ORDER = ["home", "reader", "search", "anna", "plan", "mindmap", "feed", "video", "flomo"]


def main():
    want = [a for a in sys.argv[1:] if a in SHOTS] or ORDER
    with sync_playwright() as pw:
        br = pw.chromium.launch()
        page = new_page(br)
        open_app(page)
        for name in want:
            print("→ " + name)
            try:
                SHOTS[name](page)
            except Exception as e:
                print("  拍 %s 时卡住：%r" % (name, e))
        br.close()
    print("拍完，图在", OUT)


if __name__ == "__main__":
    main()
