# -*- coding: utf-8 -*-
"""剪藏正文的表格与单元格：贴成 Markdown 之前那几道清洗。

剪藏爬的是别人写的页面，正文里什么都可能有：表格格子里带链接、带图、带脚本，
甚至真写出一串「@@T1@@」这种恰好撞上内部占位符的字。这一条全在内存里跑，不联网、
不开浏览器、不碰真实书库 —— 把 HTML 喂进 clip_article 的解析那一段，断言出来的
Markdown（走的就是页面转 book_import 那条真路，不另写一套假转换）。

五段：
  A 格子文字：锚文字只出现一次（老写法链接与纯文本各记一遍）、脚本 / 样式整块不
    进表格、懒加载图认 data-src、相对图片地址补绝对、竖线转义、只剩占位图的格子留空。
  B 表格版式：表头 + 分隔行、列数按最宽一行补齐、colspan 摊平、竖线不越列。
  C 补不上的表格不丢内容：只剩一行的表格转不出管道表，也要把格子文字当段落留下。
  D 占位符回填：格子里字面量的「@@T1@@」不许被后一轮替换再吃一遍。
  E 端到端：假 fetch 喂一页完整文章，走 _extract_once 拿到的 Markdown 也要对上。
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = tempfile.mkdtemp(prefix="guizang-clipmd-")
os.environ["GUIZANG_DATA"] = os.path.join(ROOT, "cache")
os.environ["GUIZANG_BOOKS"] = os.path.join(ROOT, "books")

import book_import                                            # noqa: E402
import clip_article                                           # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


def md_of(fragment, base="https://blog.example.com/p/1"):
    """把一段 HTML 片段塞进 body，走「挑正文 → 序列化 → 转 Markdown → 收干净」这条路。"""
    tree = clip_article.parse_html("<body>%s</body>" % fragment)
    body = clip_article._pick_body(tree)
    tables = []
    frag = clip_article._serialized(body, base, tables)
    return clip_article._clean(
        clip_article._restore_tables(book_import._md_from_xhtml(frag), tables))


def read_repo(fname):
    return open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             fname), encoding="utf-8").read()


# ── A、格子文字 ──────────────────────────────────────────────────
ONE = ('<article><p>%s</p><table>'
       '<tr><th>名</th><th>值</th></tr>'
       '<tr><td><a href="https://a.example.com/x">链接文字</a></td><td>普通</td></tr>'
       '</table></article>') % ("正文" * 80)
a1 = md_of(ONE)
chk("格子里的锚文字只出现一次（不再「链接 + 纯文本」记两遍）",
    a1.count("链接文字") == 1 and "[链接文字](https://a.example.com/x)" in a1, a1)

a2 = md_of('<table><tr><th>甲</th><th>乙</th></tr>'
           '<tr><td>前<script>alert(1)</script></td>'
           '<td>后<style>.x{color:red}</style></td></tr></table>')
chk("格子里的脚本整块不进表格（脚本原文不许被当正文抄走）",
    "alert(1)" not in a2 and "前" in a2, a2)
chk("格子里的样式整块不进表格",
    "color:red" not in a2 and "后" in a2, a2)

a3 = md_of('<table><tr><th>图</th><th>说明</th></tr>'
           '<tr><td><img data-src="https://i.example.com/p.png" alt="配图" '
           'src="data:image/gif;base64,R0"></td><td>一张</td></tr></table>')
chk("格子里的懒加载图认 data-src，不认占位 src",
    "![配图](https://i.example.com/p.png)" in a3, a3)

a4 = md_of('<table><tr><th>图</th><th>说明</th></tr>'
           '<tr><td><img src="/img/rel.png" alt="相对"></td><td>相对</td></tr></table>')
chk("格子里的相对图片地址补成绝对",
    "![相对](https://blog.example.com/img/rel.png)" in a4, a4)

a5 = md_of('<table><tr><th>a</th><th>b</th></tr>'
           '<tr><td>左|右</td><td><img src="data:image/gif;base64,R0"></td></tr></table>')
chk("格子里的竖线转义，不切断表格列",
    "左\\|右" in a5 and "data:image" not in a5, a5)

# ── B、表格版式 ──────────────────────────────────────────────────
b1 = md_of('<table><tr><th>列一</th><th>列二</th></tr>'
           '<tr><td>1</td><td>2</td></tr></table>')
b1_lines = b1.splitlines()
chk("两列表：首行是表头、次行是分隔行",
    b1_lines[0] == "| 列一 | 列二 |" and b1_lines[1] == "| --- | --- |", b1)

b2 = md_of('<table><tr><th>a</th><th>b</th></tr>'
           '<tr><td>1</td></tr>'
           '<tr><td>2</td><td>3</td><td>4</td></tr></table>')
b2_lines = [ln for ln in b2.splitlines() if ln.startswith("|")]
chk("列数按最宽一行补齐（每行管道数一致）",
    len(set(ln.count("|") for ln in b2_lines)) == 1
    and b2_lines[0].count("|") == 4, b2)

b3 = md_of('<table><tr><th>标题</th><th>数值</th></tr>'
           '<tr><td colspan="2">跨两列</td></tr></table>')
chk("colspan 摊平成两格（内容不丢、列数不错位）",
    "跨两列" in b3 and b3.splitlines()[2].count("|") == 3, b3)

# ── C、补不上的表格不丢内容 ───────────────────────────────────────
c1 = md_of('<article><p>%s</p><table><tr><td>只有一行</td><td>也要留</td></tr>'
           '</table></article>' % ("正文" * 80))
chk("只剩一行的表格转不出管道表，格子文字仍当段落留在正文里",
    "只有一行" in c1 and "也要留" in c1 and "---" not in c1, c1)

c2 = md_of('<table><tr><td><a href="https://a.example.com/y">单行链接</a></td></tr></table>')
chk("单行表格里的链接也只出现一次",
    c2.count("单行链接") == 1 and "[单行链接](https://a.example.com/y)" in c2, c2)

# ── D、占位符回填 ────────────────────────────────────────────────
d1 = md_of('<table><tr><th>甲</th></tr><tr><td>@@T1@@</td></tr></table>'
           '<table><tr><th>乙</th></tr><tr><td>真表二</td></tr></table>')
chk("格子里字面量的 @@T1@@ 不被后一轮替换再吃一遍",
    "@@T1@@" in d1 and "真表二" in d1 and d1.count("真表二") == 1, d1)

# ── E、端到端：假 fetch 喂一整页 ─────────────────────────────────
PAGE = ('<html><head><title>一篇文章 - 某号</title>'
        '<meta property="og:title" content="表格文章"></head><body>'
        '<article><p>%s</p>'
        '<table><tr><th>项</th><th>值</th></tr>'
        '<tr><td><a href="/rel">站内链接</a></td><td>甲</td></tr>'
        '<tr><td>乙</td><td>丙</td></tr></table></article></body></html>'
        % ("正文" * 80))
_old_fetch = clip_article.fetch
clip_article._PREVIEW_CACHE.clear()
clip_article.fetch = lambda url: (PAGE, "https://blog.example.com/post/9")
try:
    art = clip_article._extract_once("https://blog.example.com/post/9")
finally:
    clip_article.fetch = _old_fetch
chk("端到端：抽取出来的 Markdown 里链接只出现一次、相对地址已补绝对",
    art["markdown"].count("站内链接") == 1
    and "[站内链接](https://blog.example.com/rel)" in art["markdown"], art["markdown"])
chk("端到端：表格三行齐全（不是被丢掉的一坨）",
    "| 项 | 值 |" in art["markdown"] and "| 乙 | 丙 |" in art["markdown"], art["markdown"])

# ── F、接线：表格与普通正文对图片用同一把尺子 ─────────────────────
SRC = read_repo("clip_article.py")
chk("接线：图片地址只有一处解析（_img_src），表格与普通正文共用",
    "def _img_src(" in SRC and "src = _img_src(node, base)" in SRC
    and SRC.count("_img_src(") >= 3,
    "clip_article 里 _img_src 的调用点不齐（普通正文 / 格子）")
chk("接线：格子遍历不再用「整棵子树收文本」的老写法",
    "for n in _walk_all(cell)" not in SRC, "老写法还在，锚文字仍会重复")

shutil.rmtree(ROOT, ignore_errors=True)
print()
print("剪藏表格与单元格取证：" + ("全部通过" if not FAIL
                                else "%d 项未过：%s" % (len(FAIL), FAIL)))
sys.exit(1 if FAIL else 0)
