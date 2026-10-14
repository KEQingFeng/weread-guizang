# -*- coding: utf-8 -*-
"""剪辑取页路的「地址解不出来」：半个 IPv6 方括号不许把护栏与首图挑选打抛。

上一条 `check_browse_url.py` 把内置浏览器那一层补齐了（`web_browse.normalize` 解不
出来按 Blocked 拒绝）。它当时的注释写「剪藏那份早收口了（clip_article.url_block_reason
的 except ValueError）」—— 收口的只有 `url_block_reason` **一个函数**，真正会拿到地址
的三处入口各自又 `urlparse` 了一遍，谁都没走这道收口：

  · `clip_article.fetch()`        —— 用户粘进来的地址，第二道门（见 ui_server.clip_targets）；
  · `clip_article.first_image()`  —— 正文里每张配图的地址，用来挑封面兜底；
  · `web_parse.platform_of()` / `web_parse._guard()` —— 平台解析认站 + 取页前的门。

实测的故障（改之前）
    `.venv/bin/python -c "import clip_article as c; c.fetch('http://[::1')"`
      -> ValueError: Invalid IPv6 URL（Python 的英文运行期错误，不是那句人话）

    一页正文里只要有一张拼错的图（`<img src="http://a[b">`），`extract()` 走到挑封面
    那一步就整篇抛掉 —— 用户看到的是「这篇剪不动」，而不是「封面没挑到、正文照收」；
    后面那张真能用的题图也一并被埋掉。

收口只加一处
    `clip_article.split_url()` 把 `urlparse` 收成一个「解不出来回 None」的结果，四处
    入口共用：要拒绝的拒绝、要跳过的跳过、要不认识的回不认识，谁也不许把裸
    `ValueError` 往外递。正常地址一个字符都不改。

跑法：纯函数 + 系统临时目录沙盒 + 一个进程内的假 fetch，不联网、不开浏览器，
归 --fast 静态段。
"""
import os
import sys
import tempfile
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = tempfile.mkdtemp(prefix="guizang-clipurl-")
os.environ.setdefault("GUIZANG_DATA", os.path.join(ROOT, "cache"))
os.environ.setdefault("GUIZANG_BOOKS", os.path.join(ROOT, "books"))

import clip_article                                            # noqa: E402
import web_parse                                               # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


# `urlparse` 自己会为这几种写法抛 ValueError：半个方括号、多的方括号、空的方括号。
BAD_BRACKETS = ["http://[::1", "https://[", "http://[::1]]/x", "http://[",
                "HTTP://[::1", "  http://[::1  "]


def raises_value_error(fn):
    """跑一下，回 (抛的是不是裸 ValueError, 异常文本)。不抛回 (False, "")。"""
    try:
        fn()
    except ValueError as e:              # noqa: BLE001 —— 就是来找它抛不抛
        return True, str(e)
    except Exception as e:               # noqa: BLE001
        return False, "%s: %s" % (type(e).__name__, e)
    return False, ""


# ── 0. 镜子：不清掉坏写法，urlparse 确实会抛（否则这一套是空转）───────────────
mirror = all(raises_value_error(lambda u=u: urllib.parse.urlparse(u))[0] for u in BAD_BRACKETS)
chk("镜子：urlparse 对这几种半个方括号的写法确实抛 ValueError（这组不是空转）", mirror)


# ── 1. split_url：解不出来回 None，正常地址解得出三样 ─────────────────────────
for u in BAD_BRACKETS:
    chk("split_url(%r) 解不出来回 None（不是抛）" % u, clip_article.split_url(u) is None,
        clip_article.split_url(u))
chk("split_url：正常地址回来 (scheme, host, path)",
    clip_article.split_url("https://Blog.Example.com:8443/a/b.jpg?x=1")
    == ("https", "blog.example.com", "/a/b.jpg"))
chk("split_url：没有主机名但仍解得出（回空 host，由调用方拒绝）",
    clip_article.split_url("http:///no-host") == ("http", "", "/no-host"))
chk("split_url：空串也解得出（回三空，不当成解不出来）",
    clip_article.split_url("   ") == ("", "", ""))

# ── 2. url_block_reason：解不出来按拒绝处理（原有收口，回归钉住）─────────────
for u in BAD_BRACKETS:
    got = clip_article.url_block_reason(u)
    chk("url_block_reason(%r) 回一句人话「解不出来」" % u,
        "解不出来" in got and "Invalid IPv6" not in got, got)

# ── 3. fetch：粘进来的地址解不出来要回人话，不是裸 ValueError ─────────────────
for u in BAD_BRACKETS:
    raised, msg = raises_value_error(lambda u=u: clip_article.fetch(u))
    chk("fetch(%r) 抛的是带人话的 ValueError（不是 Invalid IPv6）" % u,
        raised and "解不出来" in msg and "Invalid IPv6" not in msg, msg)

# 正常拒绝路径逐条回归：这几句人话是被别处断言过的（check_url_redirect / 剪藏预览）。
for u, want in (("", "链接是空的"),
                ("http:///no-host", "没有主机名"),
                ("ftp://x/a", "只认 http/https"),
                ("http://127.0.0.1/x", "本机或内网"),
                ("http://[::1]/x", "本机或内网")):
    _, msg = raises_value_error(lambda u=u: clip_article.fetch(u))
    chk("fetch(%r) 仍回原来那句人话（含 %s）" % (u or "（空）", want), want in msg, msg)

# ── 4. first_image：一张拼错的图只跳过它，不许带走整次剪藏 ───────────────────
chk("首图：坏方括号那张跳过，用后面那张真能用的题图",
    clip_article.first_image("![坏](http://a[b)\n\n![好](https://x.com/p/hero.jpg)",
                             "https://x.com") == "https://x.com/p/hero.jpg")
chk("首图：正文里只有这一张坏图时回空串（没封面照收正文，不是抛）",
    clip_article.first_image("![坏](http://a[b)", "https://x.com") == "")
chk("首图：坏图在头像之类的脏值后面也一样能跳过",
    clip_article.first_image("![avatar](https://x.com/u/avatar.png)\n\n![坏](https://[)\n\n"
                             "![好](https://x.com/p/hero.jpg)", "https://x.com")
    == "https://x.com/p/hero.jpg")
# 断面：正常挑图规则一个都没动。
chk("首图（回归）：页头头像跳过、往后找",
    clip_article.first_image("![avatar](https://x.com/u/avatar.png)\n\n![题图](https://x.com/p/hero.jpg)",
                             "https://x") == "https://x.com/p/hero.jpg")
chk("首图（回归）：微信图床那种路径里没扩展名的地址照收",
    clip_article.first_image("![](https://mmbiz.qpic.cn/mmbiz_jpg/XYZ/640)", "https://x")
    == "https://mmbiz.qpic.cn/mmbiz_jpg/XYZ/640")
chk("首图（回归）：相对地址补成绝对",
    clip_article.first_image("![](/img/cover.jpg)", "https://blog.example.com/post/1")
    == "https://blog.example.com/img/cover.jpg")
chk("首图（回归）：svg 图标不当封面",
    clip_article.first_image("![i](https://x.com/a.svg)\n\n![b](https://x.com/b.png)", "https://x")
    == "https://x.com/b.png")

# ── 5. 端到端：一页正文里有一张拼错的图，整篇仍剪得下来 ────────────────────
BODY = ("<p>这是一篇够长的文章，正文里要放一张题图，"
        "所以这一段得多写几个字才过得了那条「少于六十个字就不像正文」的门槛。"
        "接着往下写，把字数凑到够用的地方，再写一句收尾的话，这就够一段了。" * 2)
PAGE = ('<html><head><title>一页带坏图的文章</title></head><body>' + BODY +
        '<p><img src="http://a[b" alt="拼错的图"></p>'
        '<p><img src="https://img.good/hero.jpg" alt="题图"></p>'
        "</body></html>")

real_fetch = clip_article.fetch
clip_article.fetch = lambda u: (PAGE, "https://blog.example.com/p/bad-img")
try:
    art = clip_article.extract("https://blog.example.com/repro-bad-img")
    ok, why = True, art.get("cover")
except Exception as e:                       # noqa: BLE001 —— 改之前这里抛 Invalid IPv6
    ok, why = False, "%s: %s" % (type(e).__name__, e)
finally:
    clip_article.fetch = real_fetch
chk("端到端：正文里夹一张半个方括号的图，整篇照剪、封面退到那张好图",
    ok and why == "https://img.good/hero.jpg", why)

# ── 6. web_parse：认站不许抛，取页前的门也不许抛 ─────────────────────────────
for u in BAD_BRACKETS:
    try:
        kind = web_parse.platform_of(u)
        chk("platform_of(%r) 回「不认识这一家」而不是抛" % u, kind == "", repr(kind))
    except Exception as e:                   # noqa: BLE001
        chk("platform_of(%r) 回「不认识这一家」而不是抛" % u, False,
            "%s: %s" % (type(e).__name__, e))

for u in BAD_BRACKETS:
    raised, msg = raises_value_error(lambda u=u: web_parse._guard(u))
    chk("_guard(%r) 抛的是带人话的 ValueError（不是 Invalid IPv6）" % u,
        raised and "解不出来" in msg and "Invalid IPv6" not in msg, msg)

# 认站与门这几条正常路一个都没动。
for u, want in (("https://www.zhihu.com/question/1", "zhihu"),
                ("https://zhuanlan.zhihu.com/p/1", "zhihu"),
                ("https://www.xiaohongshu.com/explore/1", "xhs"),
                ("https://twitter.com/u/status/1", "x"),
                ("https://x.com/u/status/1", "x"),
                ("https://blog.example.com/p/1", "")):
    chk("platform_of(%r) 仍认成 %r" % (u, want), web_parse.platform_of(u) == want,
        web_parse.platform_of(u))
for u, want in (("", "链接是空的"),
                ("http:///no-host", "没有主机名"),
                ("ftp://x/a", "只认 http/https"),
                ("http://192.168.1.1/x", "本机或内网")):
    _, msg = raises_value_error(lambda u=u: web_parse._guard(u))
    chk("_guard(%r) 仍回原来那句人话（含 %s）" % (u or "（空）", want), want in msg, msg)

# ── 7. 接线：四处入口都真的走了这一处收口 ───────────────────────────────────
def read_repo(fname):
    with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           fname), encoding="utf-8") as f:
        return f.read()


CLIP_SRC = read_repo("clip_article.py")
WEB_SRC = read_repo("web_parse.py")
chk("接线：clip_article.split_url 只有一处定义，且 url_block_reason / fetch / first_image 都用它",
    CLIP_SRC.count("def split_url(") == 1 and CLIP_SRC.count("split_url(") >= 4,
    CLIP_SRC.count("split_url("))
chk("接线：web_parse 的认站与取页前那道门也走 clip_article.split_url",
    WEB_SRC.count("clip_article.split_url(") == 2, WEB_SRC.count("clip_article.split_url("))
chk("接线：这三处入口里不再有裸的 urlparse(地址) 写法（first_image / fetch / platform_of）",
    "urllib.parse.urlparse(u)" not in CLIP_SRC
    and "urlparse((url or \"\").strip())" not in WEB_SRC
    and "urlparse((u or \"\").strip())" not in WEB_SRC)

print()
print("剪藏地址收口取证：" + ("全部通过" if not FAIL else "%d 项未过：%s" % (len(FAIL), FAIL)))
sys.exit(1 if FAIL else 0)
