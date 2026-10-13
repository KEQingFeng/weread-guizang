# -*- coding: utf-8 -*-
"""内置浏览器（AI 导航那一栏的落脚点）：取回来的页面怎么改、什么地址不许碰。

这一层是「替用户去访问外站」的第二条通路（第一条是剪藏），所以最该钉的不是「能取回来」，
而是「不该取的坚决不取」——本机与内网地址一个都不许碰，地址写错了要是人话，
别让 iframe 空白一片。取与改拆成了纯函数，于是不联网也验得实：

  A normalize —— 补 https、只认 http/https、本机 / 内网一律拒绝，且都是人话。
  B prepare   —— 补 <base>、接上「点链接 postMessage」那段脚本、摘掉会误伤的 CSP meta。
  C shape     —— HTML 换 UTF-8 送出；图片 / PDF 这类原样透传；不认的按二进制兜。
  D browse    —— 内网地址连一次网络都不发（用假 opener 顶掉，真发了就断言失败）。
  E error_page—— 取不回来铺一页人话，消息里的尖括号要转义。
  F 接线      —— 后端有 /api/browse 这条路由、模块进了打包清单，界面有顶端那颗钮与
                 内置浏览器这一屏，且 iframe 带着 sandbox。

全程离线、临时沙盒，不碰用户真实数据。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = tempfile.mkdtemp(prefix="guizang-browse-")
os.environ["GUIZANG_DATA"] = os.path.join(ROOT, "cache")
os.environ["GUIZANG_BOOKS"] = os.path.join(ROOT, "books")

import pathlib

import web_browse as wb

REPO = pathlib.Path(__file__).resolve().parent.parent
UI = (REPO / "ui.html").read_text(encoding="utf-8")
SRV = (REPO / "ui_server.py").read_text(encoding="utf-8")
BUILD = (REPO / "shell" / "build_macos.sh").read_text(encoding="utf-8")

checks = []


def chk(name, ok, extra=""):
    checks.append((bool(ok), name, "" if ok else str(extra)[:300]))
    print(("  ok  " if ok else " FAIL ") + name + ("" if ok else "  <- " + str(extra)[:300]))


# ── A normalize ────────────────────────────────────────────────
chk("首页就是 AI 功能导航 ai-bot.cn", wb.HOME == "https://ai-bot.cn/", wb.HOME)
chk("没写 scheme 的地址补成 https",
    wb.normalize("ai-bot.cn") == "https://ai-bot.cn", wb.normalize("ai-bot.cn"))
chk("http 原样留着", wb.normalize("http://example.com/x") == "http://example.com/x")

for bad, why in [
    ("", "空地址"),
    ("file:///etc/passwd", "file://"),
    ("javascript:alert(1)", "javascript:"),
    ("http://localhost:8770/api/state", "localhost"),
    ("http://127.0.0.1/x", "127.0.0.1"),
    ("http://192.168.1.1/admin", "192.168."),
    ("http://10.0.0.2/", "10."),
    ("http://172.16.3.4/", "172.16."),
    ("http://169.254.169.254/latest/meta-data/", "169.254."),
]:
    try:
        out = wb.normalize(bad)
        chk("拒绝 %s（%s）" % (bad, why), False, "居然放行了：%s" % out)
    except wb.Blocked as e:
        chk("拒绝 %s（%s）" % (bad, why), bool(str(e)), str(e))

chk("拿回的是 Blocked 带人话，不是裸异常",
    isinstance(wb.Blocked("x"), ValueError))

# ── B prepare ──────────────────────────────────────────────────
SRC_HTML = ("<html><head>"
            '<meta http-equiv="Content-Security-Policy" content="script-src \'self\'">'
            "<title>t</title></head>"
            '<body><a href="/next">下一层</a></body></html>')
OUT = wb.prepare(SRC_HTML, "https://example.com/a/b")
chk("补了 <base>，相对路径有根可循",
    '<base href="https://example.com/a/b">' in OUT, OUT[:160])
chk("base 在 <head> 里", OUT.index("<base") < OUT.index("</head>"))
chk("接了「点链接 postMessage」那段脚本", "gzBrowse" in OUT and "postMessage" in OUT)
chk("摘掉了会误伤脚本的 CSP meta", "content-security-policy" not in OUT.lower())
chk("站点自己的正文与链接没被改掉", '<a href="/next">下一层</a>' in OUT)
chk("没有 <head> 的片段也能补上 base", wb.prepare("<p>x</p>", "https://e.cn/").startswith("<base"))
chk("base 地址里的引号会被转义",
    "&quot;" in wb.prepare("<p>x</p>", 'https://e.cn/?a="b"'))

# ── C shape ────────────────────────────────────────────────────
r = wb.shape("<html><body>hi</body></html>".encode("utf-8"), "text/html", "utf-8",
             "https://e.cn/")
chk("HTML 按 utf-8 送出去", r["ok"] and r["ctype"].startswith("text/html")
    and isinstance(r["body"], bytes))
chk("HTML 这一份也补了 base", b"<base href=" in r["body"])

png = b"\x89PNG\r\n\x1a\n" + b"0" * 40
r2 = wb.shape(png, "image/png", None, "https://e.cn/i.png")
chk("图片这类非网页原样透传", r2["ok"] and r2["body"] == png and r2["ctype"] == "image/png")

r3 = wb.shape(b"%PDF-1.7", "application/pdf", None, "https://e.cn/x.pdf")
chk("认不出内容类型时按二进制兜", r3["ctype"] == "application/pdf")

r4 = wb.shape("<html><body>x</body></html>".encode("gbk"), "text/html", "gbk",
              "https://e.cn/")
chk("GBK 的页面也能正确转成 UTF-8", r4["ok"] and "x" in r4["body"].decode("utf-8"))

# ── D browse：内网地址一个网络请求都不发 ───────────────────────
sent = []


class _FakeOpener:
    def open(self, *a, **k):
        sent.append(a)
        raise AssertionError("不该发请求")


_real_build = wb.urllib.request.build_opener
wb.urllib.request.build_opener = lambda *a, **k: _FakeOpener()
try:
    r5 = wb.browse("http://192.168.1.1/admin")
finally:
    wb.urllib.request.build_opener = _real_build
chk("内网地址：browse 直接回失败，不发任何请求", (not r5["ok"]) and not sent,
    "sent=%s" % sent)
chk("内网地址：回的是「本机或内网」那句人话", "内网" in r5["msg"] or "本机" in r5["msg"],
    r5["msg"])
chk("空地址：回失败且有人话", (not wb.browse("")["ok"]) and wb.browse("")["msg"])

# ── E error_page ───────────────────────────────────────────────
page = wb.error_page("<script>bad</script>", "https://e.cn/x?a=1&b=<2>")
chk("取不回来时铺的是自己那一页", "这一页没取回来" in page)
chk("消息里的脚本没有被原样注进去", "<script>bad</script>" not in page)
chk("消息里的尖括号被转义", "&lt;script&gt;" in page)
chk("顺便把地址也带上（转义过）", "&amp;b=&lt;2&gt;" in page)

# ── F 接线 ─────────────────────────────────────────────────────
chk("后端挂了 /api/browse 这条路由", 'path == "/api/browse"' in SRV)
chk("路由把失败铺成 error_page", "web_browse.error_page" in SRV)
chk("web_browse.py 进了打包清单", "web_browse.py" in BUILD)
chk("界面里有顶端那颗 AI 导航钮", 'id="aiNavBtn"' in UI and "ainavbtn" in UI)
chk("点它走内置浏览器打开 ai-bot.cn", "openBrowse(BZ_HOME)" in UI)
chk("内置浏览器这一屏有地址条与 iframe", 'id="bzUrl"' in UI and 'id="bzFrame"' in UI)
chk("iframe 带着 sandbox（外面那页读不到界面）", 'sandbox="allow-scripts' in UI)
chk("界面收得到网页里 postMessage 回来的跳转",
    "window.addEventListener('message'" in UI and "gzBrowse" in UI)
chk("安娜那一栏也给了「内置浏览器」入口", 'id="anWeb"' in UI and "openBrowse('https://' + host" in UI)
chk("内置浏览器这一屏按「一屏刚好」收口", 'data-pane="browse"' in UI and "name === 'browse'" in UI)

bad = [c for c in checks if not c[0]]
print("\n内置浏览器体检：%d 项通过，失败 %d 项" % (len(checks) - len(bad), len(bad)))
for _, n, x in bad:
    print("  ✗ " + n + ("  | " + x if x else ""))
sys.exit(1 if bad else 0)
