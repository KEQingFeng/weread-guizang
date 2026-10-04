# -*- coding: utf-8 -*-
"""封面规则：剪藏取文章首图、视频取视频封面、来路不明的地址不替用户去敲。

全在系统临时目录的沙盒里跑，不碰用户真实书库、不联网、不开浏览器 ——
网络那一层用「记账的假 urlopen」顶替：它记下每一次被叫到的地址，
于是「这个地址我们根本没去访问」也能当成断言写死，而不是靠人眼看日志。

四段：
  A clip_article.first_image —— 正文首图怎么挑：哪些图不配当封面、相对地址补全、
    微信图床那种「路径里没扩展名」的地址不许被误杀。
  B clip_article.pick_cover / extract —— 接上真实解析：有干净的 og:image 用它，
    它指到头像或内网就退回顾正文首图。
  C clip_article.safe_image_url —— 本机 / 内网 / 非 http 一律拒绝（SSRF 那道门）。
  D ui_server.cache_cover / cache_meta_cover —— 落盘、字节数下限、失败不吭声、
    落成功后书架清单里那本书的 cover 要变成 True（界面才会去请求 /api/cover）。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = tempfile.mkdtemp(prefix="guizang-cover-")
os.environ["GUIZANG_DATA"] = os.path.join(ROOT, "cache")
os.environ["GUIZANG_BOOKS"] = os.path.join(ROOT, "books")

import book_layout                                            # noqa: E402
import clip_article                                           # noqa: E402
import ui_server                                              # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


def write(path, text):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    io.open(path, "w", encoding="utf-8").write(text)


def read_repo(fname):
    """读仓库里的一个源码文件（只给「接线还在不在」那类断言用）。"""
    return io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                fname), encoding="utf-8").read()


# ── A、正文首图：挑哪一张 ────────────────────────────────────────
chk("首图：og:image 缺席时取正文第一张配图",
    clip_article.first_image("开头\n\n![图](https://cdn.x.com/a/640?wx_fmt=jpeg)\n\n后面",
                             "https://mp.weixin.qq.com/s/abc")
    == "https://cdn.x.com/a/640?wx_fmt=jpeg")
chk("首图：微信图床那种路径里没扩展名的地址不许被误杀（.jpg 才收会把公众号题图全丢掉）",
    clip_article.first_image("![](https://mmbiz.qpic.cn/mmbiz_jpg/XYZ/640)", "https://x")
    == "https://mmbiz.qpic.cn/mmbiz_jpg/XYZ/640")
chk("首图：页头 logo / 头像 / 二维码这些一律跳过，往后找",
    clip_article.first_image(
        "![avatar](https://x.com/u/avatar.png)\n\n![题图](https://x.com/p/hero.jpg)", "https://x")
    == "https://x.com/p/hero.jpg")
chk("首图：svg / ico 这类图标不当封面",
    clip_article.first_image("![i](https://x.com/a.svg)\n\n![b](https://x.com/b.png)", "https://x")
    == "https://x.com/b.png")
chk("首图：data: 占位图（懒加载那 1px）跳过",
    clip_article.first_image("![p](data:image/gif;base64,R0)\n\n![q](https://x/q.jpeg)", "https://x")
    == "https://x/q.jpeg")
chk("首图：正文里一张图都没有就交回空串（别硬编一个）",
    clip_article.first_image("只有文字的一篇文章" * 20, "https://x") == "")
chk("首图：相对地址要补成绝对（存下来的书点封面才是打得开的）",
    clip_article.first_image("![](/img/cover.jpg)", "https://blog.example.com/post/1")
    == "https://blog.example.com/img/cover.jpg")

# ── B、og:image 与正文首图谁说了算 ─────────────────────────────
chk("pick_cover：站点自己指定的题图优先",
    clip_article.pick_cover("https://og.chosen/cover.jpg", "![x](https://img.first/hero.jpg)",
                            "https://blog.example.com/post/1")
    == "https://og.chosen/cover.jpg")
chk("pick_cover：og:image 是头像 / logo 那种脏值时不许直接收下（宁可退回顾正文首图）",
    clip_article.pick_cover("https://x.com/u/avatar.png", "![x](https://img.first/hero.jpg)",
                            "https://x") == "https://img.first/hero.jpg")
chk("pick_cover：og:image 指到内网时退回顾正文首图（留着它既下不动，又白占一个位置）",
    clip_article.pick_cover("http://192.168.1.1/secret.jpg", "![x](https://img.first/hero.jpg)",
                            "https://x") == "https://img.first/hero.jpg")
chk("pick_cover：没有 og:image 就用正文首图",
    clip_article.pick_cover("", "![x](https://img.first/hero.jpg)", "https://x")
    == "https://img.first/hero.jpg")
chk("pick_cover：两边都没有就交回空串（这本书先用生成式封面）",
    clip_article.pick_cover("", "只有文字", "https://x") == "")

BODY = ("<p>这是一篇够长的文章，正文里要放一张题图，"
        "所以这一段得多写几个字才过得了那条「少于六十个字就不像正文」的门槛。"
        "接着往下写，把字数凑到够用的地方，再写一句收尾的话，这就够一段了。" * 2)


def page(html_head):
    return ("<html><head>" + html_head + "</head><body>" + BODY +
            '<p><img src="https://img.first/hero.jpg" alt="题图"></p>'
            '<p><img src="https://img.second/two.jpg" alt="次图"></p>'
            "</body></html>")


def extract_with(head, url="https://blog.example.com/post/1"):
    """把 fetch 换成一页现成的 HTML，走完整条抽取（解析、转 md、挑封面都在里面）。

    每个用例给一个自己的 url：`extract` 按链接缓存十分钟（预览和收藏只敲一次门），
    三个用例共用一个地址的话，第二个开始吃的都是第一个的缓存 —— 那测的就不是规则，
    而是「缓存有没有把上一次的结论带过来」，会红得莫名其妙。
    """
    real = clip_article.fetch
    clip_article.fetch = lambda u: (page(head), url)
    try:
        return clip_article.extract(url)
    finally:
        clip_article.fetch = real


art = extract_with('<meta property="og:image" content="https://og.chosen/cover.jpg">',
                   "https://blog.example.com/has-og")
chk("抽取：站点自己指定了 og:image 就用它（那是作者挑的题图）",
    art["cover"] == "https://og.chosen/cover.jpg", art["cover"])
art2 = extract_with("<title>没有 og 的页面</title>", "https://blog.example.com/no-og")
chk("抽取：没有 og:image 就退到正文首图",
    art2["cover"] == "https://img.first/hero.jpg", art2["cover"])
chk("抽取：正文首图不是第二张那张（顺序就是「首图」的意思）",
    art2["cover"] != "https://img.second/two.jpg", art2["cover"])
chk("抽取：解析完的 markdown 里真的留着那张题图（封面与正文同源，不是另编的地址）",
    "![题图](https://img.first/hero.jpg)" in art2["markdown"], art2["markdown"][:200])
art3 = extract_with('<meta property="og:image" content="https://x.com/u/avatar.png">',
                    "https://blog.example.com/avatar-og")
chk("抽取：og:image 挂的是站长头像时，退回顾正文首图（满书架同一个头像比没封面还糟）",
    art3["cover"] == "https://img.first/hero.jpg", art3["cover"])

# ── C、这道门：内网地址不替用户去敲 ────────────────────────────
for bad, why in (("http://127.0.0.1/a.jpg", "本机"),
                 ("http://192.168.1.1/admin", "家用内网"),
                 ("http://10.0.0.9/x.png", "公司内网"),
                 ("http://localhost/a.jpg", "localhost"),
                 ("http://[::1]/a.jpg", "IPv6 回环"),
                 ("file:///etc/passwd", "本地文件"),
                 ("//cdn.x.com/a.jpg", "没有 scheme"),
                 ("", "空地址")):
    chk("拒绝 %s 地址：%s" % (why, bad or "（空）"),
        clip_article.safe_image_url(bad) is False, bad)
for good in ("https://img.first/hero.jpg", "http://cdn.example.com/a.png"):
    chk("放行公网图片地址：%s" % good, clip_article.safe_image_url(good) is True, good)

# ── D、落盘：cache_cover / cache_meta_cover ─────────────────────
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 2000          # 比 800 字节大就算「是真图」


class _Resp:
    def __init__(self, blob):
        self.blob = blob

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, *_n):
        return self.blob


class fake_net:
    """顶替 urlopen：把被叫到的地址一条条记下来，按需抛错。

    为什么要记账而不是只回内容：内网那道门要断的是「根本没发请求」——
    只看返回值分不出「没去问」和「问了再把结果丢掉」，那是两回事。
    退出时把 urlopen 原样还回去，绝不把假网络留给下一段。
    """

    def __init__(self, blob=PNG, error=None):
        self.blob = blob
        self.error = error
        self.urls = []

    def __enter__(self):
        self._real = urllib.request.urlopen
        urllib.request.urlopen = self
        return self

    def __call__(self, req, timeout=None):
        self.urls.append(req.full_url if hasattr(req, "full_url") else str(req))
        if self.error is not None:
            raise self.error
        return _Resp(self.blob)

    def __exit__(self, *a):
        urllib.request.urlopen = self._real
        return False


HERO = "https://img.first/hero.jpg"

# 视频那一格的书架根：1.0.1 起按模块分文件夹，书目录是 <书库>/视频/<书号>
vid_root = book_layout.book_dir(ui_server.OUT_DIR, "video")


def seed_video(book_id, extra_meta):
    d = os.path.join(vid_root, book_id)
    os.makedirs(os.path.join(d, "chapters"), exist_ok=True)
    write(os.path.join(d, "chapters", "0001.md"), "# 第一节\n\n转写正文。\n")
    meta = {"title": "视频" + book_id, "source": "video", "done": True}
    meta.update(extra_meta)
    write(os.path.join(d, "meta.json"), json.dumps(meta, ensure_ascii=False))
    return d


with fake_net() as net:
    ok = ui_server.cache_cover("video_COVER1", HERO)
chk("cache_cover：真图落到了 cache/covers/<书号>.jpg",
    ok is True and os.path.isfile(ui_server.cover_path("video_COVER1"))
    and os.path.getsize(ui_server.cover_path("video_COVER1")) > 800,
    ui_server.cover_path("video_COVER1"))
chk("cache_cover：去的就是那个地址（没被改写成别的站）", net.urls == [HERO], net.urls)

with fake_net() as net:
    refused = ui_server.cache_cover("video_COVER2", "http://192.168.1.1/secret.jpg")
chk("cache_cover：内网封面地址一次请求都没发出去（不是发出去再丢结果）",
    refused is False and net.urls == [] and not os.path.isfile(ui_server.cover_path("video_COVER2")),
    net.urls)

with fake_net(error=Exception("图床抽风")) as net:
    failed = ui_server.cache_cover("video_COVER3", HERO)
chk("cache_cover：取不到图只是没封面，不该把整本书说成没存下（返回 False、不抛）",
    failed is False and net.urls == [HERO]
    and not os.path.isfile(ui_server.cover_path("video_COVER3")), net.urls)

with fake_net() as net:
    again = ui_server.cache_cover("video_COVER1", HERO)
chk("cache_cover：本地已经有了就不再下一遍（每次刷新书架都重下图是白烧流量）",
    again is True and net.urls == [], net.urls)

# 「本地已经有封面就不动图床」是有意的取舍：换 meta 里的地址也不覆盖已有那份
# —— 每次刷新书架都重下一遍太贵。真要改这个行为，连着这两条一起改。
seed_video("video_CHANGED", {"cover": "https://img.first/new.jpg"})
with fake_net() as net:
    first = ui_server.cache_cover("video_CHANGED", "https://img.first/old.jpg")
    moved = ui_server.cache_meta_cover("video_CHANGED")
chk("cache_meta_cover：本地已经落好封面时一个请求都不发（meta 里换了地址也不重下）",
    first is True and moved is True and net.urls == ["https://img.first/old.jpg"], net.urls)
chk("落下的封面是本地那一份，不是把远端地址当封面（断网也要看得见书）",
    open(ui_server.cover_path("video_CHANGED"), "rb").read() == PNG)

with fake_net(blob=b"too small") as net:
    tiny = ui_server.cache_cover("video_COVER4", HERO)
chk("cache_cover：小于 800 字节的东西不当封面（半张坏图比没图更糟）",
    tiny is False and not os.path.isfile(ui_server.cover_path("video_COVER4")), tiny)

# 已经躺着一张坏封面的情况：得肯重取，不然一次抽风就把这本书永久钉在坏图上
bad_p = ui_server.cover_path("video_COVER6")
os.makedirs(os.path.dirname(bad_p), exist_ok=True)
open(bad_p, "wb").write(b"x" * 100)
with fake_net() as net:
    healed = ui_server.cache_cover("video_COVER6", HERO)
chk("cache_cover：占位的坏封面（小于 800 字节）会被重取覆盖，不认它是封面",
    healed is True and os.path.getsize(bad_p) > 800 and net.urls == [HERO],
    (os.path.getsize(bad_p), net.urls))

# 视频那条线：转写完成后按 meta.json 里的封面地址补一次（seed_video 在前面定义）
seed_video("video_COVER5", {"cover": "https://img.first/thumb.jpg"})
with fake_net() as net:
    hit = ui_server.cache_meta_cover("video_COVER5")
chk("cache_meta_cover：视频封面从 meta.json 里那个地址取回来（用户要的就是视频封面当笔记封面）",
    hit is True and os.path.isfile(ui_server.cover_path("video_COVER5")), hit)
chk("cache_meta_cover：取的就是 meta 里写的那个地址",
    net.urls == ["https://img.first/thumb.jpg"], net.urls)
listed = next((b for b in ui_server.list_books("video") if b["id"] == "video_COVER5"), {})
chk("书架清单跟着报 cover=True（界面只在本地有文件时才请求 /api/cover）",
    listed.get("cover") is True, listed)

seed_video("video_NOCOVER", {})
with fake_net() as net:
    none = ui_server.cache_meta_cover("video_NOCOVER")
chk("cache_meta_cover：meta 里没有封面地址时安静返回 False（一个请求都不发）",
    none is False and net.urls == [], net.urls)
chk("cache_meta_cover：书号是空串也不炸（任务失败时回执里就没书号）",
    ui_server.cache_meta_cover("") is False)
chk("cache_meta_cover：书号指向一本不存在的书也不炸",
    ui_server.cache_meta_cover("video_NOTHERE") is False)
chk("cache_meta_cover：带 ../ 的书号照样挡在门外（这道闸不许因为封面而松）",
    ui_server.cache_meta_cover("../etc/passwd") is False)

# 剪藏那条线：入库后 meta.cover 有地址，封面就得落地（list_books 只认本地文件）
clip_root = book_layout.book_dir(ui_server.OUT_DIR, "clip")
cd = os.path.join(clip_root, "clip_demo")
os.makedirs(os.path.join(cd, "chapters"), exist_ok=True)
write(os.path.join(cd, "chapters", "0001.md"), "# 一篇文章\n\n正文。\n")
write(os.path.join(cd, "meta.json"), json.dumps(
    {"title": "剪藏一篇", "source": "clip", "cover": HERO}, ensure_ascii=False))
with fake_net() as net:
    clip_hit = ui_server.cache_meta_cover("clip_demo")
chk("cache_meta_cover：剪藏的封面按同一套规则落地（三条来路共用一个函数）",
    clip_hit is True and os.path.isfile(ui_server.cover_path("clip_demo")), clip_hit)
clisted = next((b for b in ui_server.list_books("clip") if b["id"] == "clip_demo"), {})
chk("剪藏那一格的清单也报 cover=True", clisted.get("cover") is True, clisted)

# 视频转完那一步有没有顺手补封面：这是「任务结束时」的接线，只能从源码断言
# （真跑一遍要下载模型，不是一轮取证该等的事）。删掉这行调用，本条就红。
SRC = read_repo("ui_server.py")
chk("接线：视频任务结束时按 meta.json 补一次封面（_reader 里调 cache_meta_cover）",
    'TASK.get("kind") == "video"' in SRC
    and "cache_meta_cover(bid)" in SRC, "找不到视频收尾补封面那段")
chk("接线：后台补齐封面时，非微信读书的书走 meta 里那个地址（不再拿书号去问微信读书）",
    'if cache_meta_cover(b["id"])' in SRC, "warm_covers 里没走 meta")

shutil.rmtree(ROOT, ignore_errors=True)
print()
print("封面规则取证：" + ("全部通过" if not FAIL else "%d 项未过：%s" % (len(FAIL), FAIL)))
sys.exit(1 if FAIL else 0)
