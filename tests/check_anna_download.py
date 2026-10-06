# -*- coding: utf-8 -*-
"""「安娜的档案」这条下载流 —— 让真浏览器真的下一次文件，验归藏这一侧接不接得住。

为什么要这一套：这一栏承诺的是「你在窗口里点下载，归藏把文件接住、转成书、摆上本地
书架」。前面两套各验了一半 —— check_anna.py 是纯逻辑（不碰浏览器），check_anna_ui.py
是把文件先放进下载夹再点「补收」（跳过浏览器那一段）。中间那条线 —— Playwright 的
download 事件、save_as、同名不覆盖、排队转书 —— 只有真下载一次才量得到。没量过就不许
说「下载能自动进书架」。

为什么这里可以无头：产品里那个窗口必须看得见（`headless=False`），因为对方站点在 TLS
层挡机器 —— 那是「能不能打开安娜的档案」的问题。这一套对着本机临时服务下载，不联网、
不碰对方站点，验的是归藏自己那半条管道，所以可以静静地跑在门禁里。真开窗口那一步归
人工实测（见 docs/交接说明.md），因为门禁里弹真窗口会占住任务槽、还会在用户屏幕上乱开页面。

验的东西（每一件都经由真的 download 事件进来，不是手写进去的）：
  · 点一下链接 → 事件被接住、按站点给的名字落到下载夹、账本 caught 加一；
  · 泵一轮 → 那份 Markdown 真的转成一本书（磁盘上有目录、meta.json 与 chapters 都在）；
  · 再点同一个链接（同名同内容）→ 磁盘上不覆盖（加时间戳尾巴）、书架上也不重复（按字节摘要跳过）；
  · 一份不认的格式（.mobi）→ 原文件留着、记进「没收进来的」、把原因写清楚；
  · 三个计数各就各位（接住 3 / 进书架 1 / 没收进来 1），账本里不写绝对路径。

前提：只往沙盒里写（GUIZANG_DATA / GUIZANG_BOOKS 必须在 SANDBOX 之下，否则直接退出）。
收尾把账本原样还原、把这三份下载与入库那本书一并抹掉。
"""
import atexit
import http.server
import os
import pathlib
import shutil
import socketserver
import sys
import threading

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import selftest              # noqa: E402
import anna_state            # noqa: E402
import anna_browser          # noqa: E402
import book_layout           # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

FAIL = []
PASSED = [0]


def chk(name, ok, extra=""):
    if ok:
        PASSED[0] += 1
        print("PASS  %s" % name, flush=True)
    else:
        FAIL.append(name)
        print("FAIL  %s\n      %s" % (name, str(extra)[:400]), flush=True)


DATA = os.environ.get("GUIZANG_DATA") or str(selftest.CACHE)
BOOKS = os.environ.get("GUIZANG_BOOKS") or str(selftest.BOOKS)
SANDBOX = os.path.realpath(str(selftest.SANDBOX))
for label, path in (("数据目录", DATA), ("书库", BOOKS)):
    real = os.path.realpath(path)
    if not (real == SANDBOX or real.startswith(SANDBOX + os.sep)):
        sys.exit("这一套要往磁盘接下载、往书架转书，只准落在沙盒里：现在这一格的%s 不在 %s "
                 "之下（bash tests/run_all.sh 会一并导好）。" % (label, SANDBOX))

INCOMING = anna_state.incoming_dir(DATA, create=True)
SHELF = book_layout.book_dir(BOOKS, "local")
COVER = os.path.join(DATA, "cache", "covers")
PROFILE = os.path.join(str(SANDBOX), "anna_profile_dl")

MD_NAME = "guizang-anna-demo.md"
MOBI_NAME = "guizang-anna-demo.mobi"
MD_BODY = ("# 归藏自测用的样本书\n\n"
           "## 第一章 石头\n\n" + "这一段是门禁自己现编的，跟任何一本真书都没有关系。" * 24 +
           "\n\n## 第二章 水\n\n" + "这一段也是现编的，跑完就删掉。" * 24 + "\n")
MOBI_BODY = b"BOOKMOBI" + b"\x00" * 4096

# 账本原样备份：跑完贴回去，下一个套件看见的还是它自己那份账。
STATE_PATH = anna_state.state_path(DATA)
BACKUP = open(STATE_PATH, encoding="utf-8").read() if os.path.exists(STATE_PATH) else None
BEFORE_BOOKS = set(os.listdir(SHELF)) if os.path.isdir(SHELF) else set()


def restore():
    try:
        if BACKUP is None:
            if os.path.exists(STATE_PATH):
                os.remove(STATE_PATH)
        else:
            with open(STATE_PATH, "w", encoding="utf-8") as f:
                f.write(BACKUP)
        for name in (MD_NAME, MOBI_NAME):
            p = os.path.join(INCOMING, name)
            if os.path.isfile(p):
                os.remove(p)
        # 同名那份会带时间戳尾巴，按前缀清。
        for name in os.listdir(INCOMING):
            if name.startswith("guizang-anna-demo"):
                p = os.path.join(INCOMING, name)
                if os.path.isfile(p):
                    os.remove(p)
        if os.path.isdir(SHELF):
            for name in set(os.listdir(SHELF)) - BEFORE_BOOKS:
                p = os.path.join(SHELF, name)
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                elif os.path.isfile(p):
                    os.remove(p)
        shutil.rmtree(PROFILE, ignore_errors=True)
    except Exception as e:                      # 收尾失败只吭一声，不盖掉检查结果
        print("（收尾没做干净：%s: %s）" % (type(e).__name__, e), flush=True)


atexit.register(restore)


class Handler(http.server.BaseHTTPRequestHandler):
    """一个「像那种站点」的本地夹具：一个页面两颗链接，点了就给附件。"""

    def log_message(self, *args):
        pass

    def _send(self, ctype, disp, data):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % disp)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/md"):
            self._send("text/markdown; charset=utf-8", MD_NAME, MD_BODY.encode("utf-8"))
        elif self.path.startswith("/mobi"):
            self._send("application/octet-stream", MOBI_NAME, MOBI_BODY)
        else:
            html = ("<html><body>"
                    "<a id='md' href='/md'>下 Markdown</a>"
                    "<a id='mobi' href='/mobi'>下 mobi</a>"
                    "</body></html>").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)


def pump(ctx, page, queue, want):
    """照 run() 里那个泵的样子转：等事件、把排队的下载一份份转成书。

    want 是「等账本上 caught 到几份」，到数就回；最多等 24 轮（约 17 秒），
    超时就带着当下状态回，让断言去说哪一步没到。
    """
    for _ in range(24):
        try:
            page.wait_for_timeout(700)
        except Exception:
            if not anna_browser.alive(ctx):
                return
            break
        anna_browser.drain(DATA, SHELF, COVER, queue)
        if anna_state.read_state(DATA).get("caught", 0) >= want:
            return


def main():
    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as httpd:
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        queue = []
        with sync_playwright() as p:
            try:
                ctx = p.chromium.launch_persistent_context(
                    PROFILE, headless=True, viewport={"width": 900, "height": 600},
                    accept_downloads=True, downloads_path=INCOMING,
                    # 系统代理会把 127.0.0.1 一起绕出去（本机挂着代理时真撞过），
                    # 这一套只在回环上跑，直接关掉代理最干净。
                    args=anna_browser.browser_args("") + ["--no-proxy-server"])
            except Exception as e:
                chk("浏览器能起来（起不来这一套没法跑）", False, "%s: %s" %
                    (type(e).__name__, str(e)[:200]))
                return 1
            try:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                ctx.on("download", lambda dl: anna_browser.handle_download(DATA, queue, dl))
                page.goto("http://127.0.0.1:%d/" % port, wait_until="domcontentloaded",
                          timeout=20000)
                chk("夹具页面打开了（没打开就没有后面的下载）", page.locator("#md").count() == 1)

                page.click("#md")
                pump(ctx, page, queue, 1)
                files = anna_state.incoming_listing(INCOMING)
                names = [r["name"] for r in files]
                chk("真下载被接住了（事件 → 落盘 → 进队列这一段）",
                    MD_NAME in names, names)
                st = anna_state.read_state(DATA)
                chk("接住一份就把 caught 记上一份", st.get("caught") == 1, st)
                chk("这一份真的转成了书（imported 记上）", st.get("imported") == 1, st)

                new = set(os.listdir(SHELF)) - BEFORE_BOOKS if os.path.isdir(SHELF) else set()
                book_dir = os.path.join(SHELF, sorted(new)[0]) if new else ""
                chk("本地书架里多出一本书，就在沙盒书库那一格", len(new) == 1 and new, new)
                if book_dir:
                    listing = sorted(os.listdir(book_dir))
                    chk("那本书的目录是导入器那一套（meta.json 与 chapters 都在）",
                        "meta.json" in listing and "chapters" in listing, listing)
                    # 章节是从文件里切出来的：样本书有两个二级标题，切不出来说明
                    # 「接住了但没读内容」，那一半链路就等于没验。
                    ch_files = os.listdir(os.path.join(book_dir, "chapters"))
                    row = (anna_state.read_state(DATA).get("books") or [{}])[0]
                    chk("正文真被读进来了（两个二级标题切成两章以上）",
                        len(ch_files) >= 2 and int(row.get("chapters") or 0) >= 2,
                        (ch_files, row.get("chapters")))
                    chk("那一行记录带着能对上的书名与书本 id",
                        bool(row.get("title")) and bool(row.get("book")), row)

                page.click("#mobi")
                pump(ctx, page, queue, 2)
                st = anna_state.read_state(DATA)
                names = [r["name"] for r in anna_state.incoming_listing(INCOMING)]
                chk("认不出的格式照样接住、原文件留着", MOBI_NAME in names, names)
                chk("认不出的格式不进书架（imported 不动、skipped 加一）",
                    st.get("imported") == 1 and st.get("skipped") == 1,
                    {k: st.get(k) for k in ("caught", "imported", "skipped")})
                pend = st.get("pending") or []
                chk("没收进来的那一份写清了为什么",
                    any("格式不认" in str(r.get("reason") or "") for r in pend), pend)

                page.click("#md")
                pump(ctx, page, queue, 3)
                st = anna_state.read_state(DATA)
                names = [r["name"] for r in anna_state.incoming_listing(INCOMING)]
                again = [n for n in names if n.startswith("guizang-anna-demo.md")
                         or (n.startswith("guizang-anna-demo-") and n.endswith(".md"))]
                chk("同一个名字不覆盖（磁盘上留了两份，尾巴带时间戳）",
                    len(again) == 2 and len(set(again)) == 2, names)
                chk("同一本内容不重复进书架（按字节摘要跳过）",
                    st.get("imported") == 1 and len(st.get("books") or []) == 1,
                    {"imported": st.get("imported"), "books": len(st.get("books") or [])})
                chk("三份都算「接住过」（caught 记的是下载次数不是书本数）",
                    st.get("caught") == 3, st.get("caught"))

                raw = open(STATE_PATH, encoding="utf-8").read()
                chk("账本里不写绝对路径（只留名字和数字）",
                    SANDBOX not in raw and DATA not in raw, raw[:200])
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass
        httpd.shutdown()
    return 0


print("── 安娜的档案（真下载流）──────────────────────────────────", flush=True)
rc = main()
print("\n通过 %d 项，失败 %d 项" % (PASSED[0], len(FAIL)), flush=True)
for name in FAIL:
    print("  ✗ %s" % name, flush=True)
sys.exit(1 if FAIL or rc else 0)
