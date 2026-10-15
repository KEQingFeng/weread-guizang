# -*- coding: utf-8 -*-
"""剪藏正文那一道编码口：落单的半个代理码点不许把一篇能剪的文章挡在门外。

tests/check_import_surrogate.py 已经守住了两个落盘口（import_book 的 title / author /
章节正文，以及 save_clip 那份 meta 里的封面地址），这一份补的是它**故意绕开**的那一段：
save_clip 把标题 / 正文 / 出处拼成 Markdown 之后有一句 `body.encode("utf-8")`，
而它不是 `json.dump`，前面也没有那道共用清洗尺。

于是这一条路是敞的：X、知乎、小红书那几条提取路（web_parse）拿回来的是 JSON，
`"\\ud83d"` 这种转义能被 `json.loads` 读成一个落单的半个代理码点，混进 title 或
markdown。`body.encode("utf-8")` 碰到它当场抛 UnicodeEncodeError，`clip_many` 又把
`str(e)[:160]` 原样递到界面 —— 用户看到的是

    剪不动：'utf-8' codec can't encode character '\\ud83d' in position 3: surrogates not allowed

这句 Python 原文，而不是「这一篇没剪成」。修法不另写一把尺子：clip_article 已经在
meta 落盘那一处用了 `book_layout.drop_lone_surrogates`，在这一道编码之前也过一遍同一把尺。

这一份只碰系统临时目录里的沙盒，用假的 extract_fn 顶掉真抓网页：不开浏览器、不起服务、
不联网，归 --fast。
"""
import json
import os
import pathlib
import shutil
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

# 先指进临时沙盒再 import：clip_article 会牵出 book_import / book_layout，别让模块级的
# 路径常量落到用户真实的 cache/ 与书库上。
SB = tempfile.mkdtemp(prefix="guizang-clip-surrogate-")
os.environ["GUIZANG_DATA"] = os.path.join(SB, "cache")
os.environ["GUIZANG_BOOKS"] = os.path.join(SB, "books")
os.makedirs(os.environ["GUIZANG_DATA"], exist_ok=True)

import book_layout as BL          # noqa: E402
import clip_article as CA         # noqa: E402

# 落单的半个代理码点：chr() 现造，免得源码里出现字面上的半个码点（那会让文件本身读不动）
HI = json.loads('"\\ud83d"')
LO = json.loads('"\\udc00"')

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:220]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def no_tmp(d):
    """书目录里不该留半截 .tmp。"""
    for root, _dirs, files in os.walk(d):
        for f in files:
            if f.endswith(".tmp"):
                return False
    return True


def raises_unicode_encode(text):
    """这段文本能不能编成 utf-8；编不动返回 True。"""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


def stub(art):
    """一个假的提取器：把给定的 art 原样交回去，不联网。"""
    return lambda url: dict(art)


def art_of(title, markdown, **kw):
    a = {"title": title, "author": "作者", "site": "某站", "date": "2026-10-15",
         "cover": "https://example.test/cover.jpg", "url": "https://example.test/a",
         "markdown": markdown, "words": 300}
    a.update(kw)
    return a


def clip_once(tag, art):
    root = os.path.join(SB, tag)
    return root, CA.clip_many(root, [{"url": "https://example.test/a"}], extract_fn=stub(art))


# ── 0. 前提：JSON 里的转义真的能读成一个落单的半个代理码点 ─────────────
chk("前提：\\ud83d 转义读出来是单个落单码点（不是成对的表情）",
    HI == chr(0xD83D) and len(HI) == 1, repr(HI))
chk("前提：落单的码点编不成 utf-8（这道口子为什么是真的，预先钉住）",
    raises_unicode_encode("正文" + HI) and not raises_unicode_encode("正文😀"))

# ── 1. 标题里带坏码：一篇能剪的文章不许被它挡在门外 ───────────────────
root1, r1 = clip_once("t1", art_of("坏" + HI + "标题", "正文😀段落。" * 40))
chk("标题坏码：clip_many 回 done=1、failed=0（不再是一句 codec 原文）",
    r1["done"] == 1 and r1["failed"] == 0 and not r1["fail"], r1["fail"][:1])
if r1["ok"]:
    d1 = r1["ok"][0]["dir"]
    meta1 = read_json(os.path.join(d1, "meta.json"))
    ch1 = read_text(os.path.join(d1, "chapters", "0000.md"))
    chk("标题坏码：落盘标题里坏码摘掉、好字与成对表情一个不动",
        meta1["title"] == "坏标题" and HI not in meta1["title"], repr(meta1["title"]))
    chk("标题坏码：正文里的成对表情仍原样（清洗是幂等的）", "正文😀" in ch1)
    chk("标题坏码：没有半截 .tmp，书目录是完整的",
        no_tmp(d1) and os.path.isfile(os.path.join(d1, "meta.json")))
    chk("标题坏码：书库里只有这一本书的目录，没有幽灵目录",
        sorted(os.listdir(root1)) == [os.path.basename(d1)], sorted(os.listdir(root1)))

# ── 2. 正文里带坏码：同一道编码口，另一处输入 ─────────────────────────
root2, r2 = clip_once("t2", art_of("干净😀标题", "正文" + HI + "段落。" * 40))
chk("正文坏码：clip_many 回 done=1、failed=0",
    r2["done"] == 1 and r2["failed"] == 0 and not r2["fail"], r2["fail"][:1])
if r2["ok"]:
    d2 = r2["ok"][0]["dir"]
    ch2 = read_text(os.path.join(d2, "chapters", "0000.md"))
    meta2 = read_json(os.path.join(d2, "meta.json"))
    chk("正文坏码：落盘正文里坏码没了、别的字一个不动",
        HI not in ch2 and "正文段落。" in ch2, repr(ch2[:60]))
    chk("正文坏码：标题里的成对表情原样留着", meta2["title"] == "干净😀标题",
        repr(meta2["title"]))
    chk("正文坏码：没有半截 .tmp", no_tmp(d2))

# ── 3. 出处那几行（站名 / 日期 / 链接）也进 body，坏码同样要收住 ────────
root3, r3 = clip_once("t3", art_of(
    "干净标题", "正文段落。" * 40,
    site="某站" + LO, date="2026-10-15" + HI, url="https://example.test/a" + LO))
chk("出处坏码：clip_many 回 done=1、failed=0",
    r3["done"] == 1 and r3["failed"] == 0 and not r3["fail"], r3["fail"][:1])
if r3["ok"]:
    d3 = r3["ok"][0]["dir"]
    ch3 = read_text(os.path.join(d3, "chapters", "0000.md"))
    chk("出处坏码：正文头那段出处里坏码没了、站名与日期照旧",
        HI not in ch3 and LO not in ch3 and "剪自 某站" in ch3 and "2026-10-15" in ch3,
        repr(ch3[:120]))

# ── 4. 干净输入零改动：中文与成对表情一个字不许变（幂等）────────────────
root4, r4 = clip_once("t4", art_of("我的😀书", "正文😀段落。\n\n还有（中文）与 < 尖括号 >。\n" * 20))
chk("干净输入：仍然 done=1、failed=0", r4["done"] == 1 and r4["failed"] == 0, r4["fail"][:1])
if r4["ok"]:
    d4 = r4["ok"][0]["dir"]
    meta4 = read_json(os.path.join(d4, "meta.json"))
    ch4 = read_text(os.path.join(d4, "chapters", "0000.md"))
    chk("干净输入：标题里的成对表情原样", meta4["title"] == "我的😀书", repr(meta4["title"]))
    chk("干净输入：正文一字未动（中文 / 表情 / 尖括号都留着）",
        "正文😀段落。" in ch4 and "还有（中文）与 < 尖括号 >。" in ch4)

# ── 5. 接线：用的是共用尺，而且发生在 body.encode 之前 ─────────────────
src = pathlib.Path(CA.__file__).read_text(encoding="utf-8")
chk("接线：clip_article 复用的正是 book_layout.drop_lone_surrogates",
    CA.book_layout.drop_lone_surrogates is BL.drop_lone_surrogates)
# 只数代码行：注释里提一句「下面 body.encode(...) 会抛」不该算成一处编码口，
# 与 check_gate_wiring 里「引号外才算登记」是同一条道理（不然这条门禁会空转）。
code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
i_clean = code.find("book_layout.drop_lone_surrogates(art)")
i_enc = code.find('body.encode("utf-8")')
chk("接线：save_clip 在拼 body 之前先过清洗尺（坏码进不了 encode）",
    0 <= i_clean < i_enc, (i_clean, i_enc))
chk("接线：只有一处 encode 正文的口（改也只改这一处）",
    code.count('body.encode("utf-8")') == 1, code.count('body.encode("utf-8")'))

# ── 6. 根因那把尺本身：成对的表情不摘、落单的才摘 ──────────────────────
chk("尺子：半个码点摘掉，成对的表情（真字符）不动",
    BL.drop_lone_surrogates("a" + HI + "😀" + LO + "b") == "a😀b",
    repr(BL.drop_lone_surrogates("a" + HI + "😀" + LO + "b")))
chk("尺子：嵌套结构也走到（art 是 dict）",
    BL.drop_lone_surrogates({"t": [HI + "x", {"y": LO}]}) == {"t": ["x", {"y": ""}]})

# ── 收摊 ────────────────────────────────────────────────────────────
shutil.rmtree(SB, ignore_errors=True)

print()
print("剪藏正文坏字符收口体检：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
for f in FAIL:
    print("  ✗ " + f)
sys.exit(1 if FAIL else 0)
