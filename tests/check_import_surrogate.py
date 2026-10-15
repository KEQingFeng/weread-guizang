# -*- coding: utf-8 -*-
"""导入 / 剪藏落盘的坏字符收口（book_import.py / clip_article.py）。

只碰系统临时目录里的沙盒，不开浏览器、不起服务、不联网：import_book 与 save_clip 的
文本清洗都是纯函数 + 文件操作，剪藏那一路用一个假的 extract_fn 顶掉真抓网页。

tests/check_book_import.py 管的是「字节怎么解、章怎么切、正常书落得对不对」，这一份
只管边上那一类：

  · **写不进 utf-8 的字符**：POST 来的 JSON 里 `"\\ud800"` 这种转义能被 json.load 读成
    一个落单的半个代理码点（浏览器把 JS 字符串拦腰截断时就是这么发出来的）。它混进
    title / author（本地导入）或封面的 og:image 地址（剪藏），落盘那一下抛
    UnicodeEncodeError：接口回 500 加一句 codec 原文，而书目录这时候已经建出来了 ——
    书架上留一本只有半截章节、没有 meta.json 的幽灵书，还留一个 meta.json.tmp。
  · 修法不另写一把尺子：book_layout.drop_lone_surrogates 就是「外部文本要落进 utf-8」
    的共用尺（读 meta 也走它），导入与剪藏两个落盘口都复用它。成对的表情符号、中文
    一个字不动，正常书零改动。

沙盒是临时目录，跑完整包删掉，不碰用户真实书库。
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import book_import as B          # noqa: E402
import book_layout as BL         # noqa: E402
import clip_article as CA        # noqa: E402

# 落单的半个代理码点：chr() 现造，免得源码里出现字面上的半个码点（那会让文件本身读不动）
HI = chr(0xD800)
LO = chr(0xDFFF)

SB = tempfile.mkdtemp(prefix="guizang-import-surrogate-")
BOOKS = os.path.join(SB, "books")

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))
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


def no_tmp(root):
    """整棵目录树里不许留半截 .tmp。"""
    for base, _dirs, files in os.walk(root):
        for fn in files:
            if fn.endswith(".tmp"):
                return False
    return True


# ── 0. 对照：这个坏字符确实写不进 utf-8，夹具是真危险 ─────────────────
decoded = json.loads('"\\ud800"')
chk("夹具：JSON 里的 \\ud800 转义读出来就是一个落单的半个代理码点",
    len(decoded) == 1 and ord(decoded[0]) == 0xD800, repr(decoded))
raised = False
try:
    with open(os.path.join(SB, "probe.json"), "w", encoding="utf-8") as f:
        json.dump({"title": "坏" + HI}, f, ensure_ascii=False)
except UnicodeEncodeError:
    raised = True
chk("对照：ensure_ascii=False 的 meta 直接写它确实抛（旧读法的病根）", raised)

# ── 1. 接线：导入用的就是书库那把共用尺 ───────────────────────────────
chk("接线：book_import 复用的正是 book_layout.drop_lone_surrogates",
    B._clean is BL.drop_lone_surrogates)

# ── 2. 本地导入：title / author 带坏字符照样导得进来 ──────────────────
data = "# 第一章 开始\n\n正文一。😀\n".encode("utf-8")
info = B.import_book(BOOKS, "草%s稿.md" % HI, data,
                     title="坏%s名" % HI, author="作%s者" % LO)
chk("导入：坏字符在 title / author 里也回正常 info，不抛",
    info["title"] == "坏名" and info["author"] == "作者", info)
chk("导入：返回的标题已洗净、章数照旧", info["chapters"] == 1, info)

d = info["dir"]
meta = read_json(os.path.join(d, "meta.json"))
cat = read_json(os.path.join(d, "_catalog.json"))
ch0 = read_text(os.path.join(d, "chapters", "0000.md"))
chk("落盘 meta：标题 / 作者里坏字符都没了", meta["title"] == "坏名" and meta["author"] == "作者",
    (meta["title"], meta["author"]))
chk("落盘 meta：来源 / 格式 / 章数这些正常字段一个没动",
    meta["source"] == "local" and meta["format"] == "md" and meta["chars"] > 0, meta)
chk("落盘目录账本：章标题干净", cat == ["第一章 开始"], cat)
chk("落盘正文：坏字符没了，中文与成对表情一个字不动",
    HI not in ch0 and LO not in ch0 and "正文一。😀" in ch0, repr(ch0))
chk("收尾：书目录里没有半截 .tmp", no_tmp(d), sorted(os.listdir(d)))
chk("收尾：书库里只有这一本书的目录，没有崩溃时留下的幽灵目录",
    sorted(os.listdir(BOOKS)) == [info["id"]], sorted(os.listdir(BOOKS)))

# ── 3. 不传 title：退回文件名，文件名里的坏字符也要洗 ─────────────────
info2 = B.import_book(BOOKS, "书%s名.txt" % HI, "第一章 开始\n\n正文。\n".encode("utf-8"))
chk("退回文件名：文件名里的坏字符也洗净", info2["title"] == "书名", repr(info2["title"]))
chk("退回文件名：这一本也落得干净、没有 .tmp", no_tmp(info2["dir"]))

# ── 4. 干净输入：一个字都不许改（清洗是幂等的）──────────────────────
info3 = B.import_book(BOOKS, "干净的书.md", "# 第一章 开始\n\n正文😀。\n".encode("utf-8"),
                      title="我的😀书", author="作者名")
meta3 = read_json(os.path.join(info3["dir"], "meta.json"))
chk("干净书：标题 / 作者里的中文与成对表情原样留着",
    meta3["title"] == "我的😀书" and meta3["author"] == "作者名", meta3)
chk("干净书：正文一字未动",
    "正文😀。" in read_text(os.path.join(info3["dir"], "chapters", "0000.md")))

# ── 5. 剪藏：封面地址里的坏字符不许把 meta 落盘打成半截 ───────────────
# 用假 extract_fn 顶掉真抓网页：正文与出处那段是干净的（能编成字节），
# 只有 og:image 地址带一个落单的半个码点 —— 它只进 save_clip 那份 meta，
# 不过正文那道编码，正好单独验这一个落盘口。
ART = {
    "title": "干净标题",
    "author": "作者",
    "site": "某站",
    "date": "2026-10-15",
    "cover": "https://example.test/cover%s.jpg" % HI,
    "url": "https://example.test/a",
    "markdown": "正文段落。" * 40,
    "words": 300,
}


def stub_extract(url):
    art = dict(ART)
    art["url"] = url
    return art


clip_root = os.path.join(SB, "clips")
res = CA.clip_many(clip_root, [{"url": "https://example.test/a"}], extract_fn=stub_extract)
chk("剪藏：封面地址带坏字符也回 done=1，不抛不半截",
    res["done"] == 1 and not res["failed"], res)
if res["ok"]:
    cd = res["ok"][0]["dir"]
    cmeta = read_json(os.path.join(cd, "meta.json"))
    chk("剪藏落盘：meta 是完整 JSON、封面地址里坏字符摘掉",
        cmeta.get("source") == "clip" and HI not in (cmeta.get("cover") or "")
        and cmeta["cover"] == "https://example.test/cover.jpg", cmeta.get("cover"))
    chk("剪藏落盘：没有 meta.json.tmp 之类的半截", no_tmp(cd), sorted(os.listdir(cd)))
    chk("剪藏落盘：书目录里没有幽灵 / 半截目录", len(os.listdir(clip_root)) == 1,
        sorted(os.listdir(clip_root)))

# ── 收摊 ────────────────────────────────────────────────────────────
for path in (SB,):
    shutil.rmtree(path, ignore_errors=True)

print()
print("导入 / 剪藏坏字符收口体检：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
for f in FAIL:
    print("  ✗ " + f)
sys.exit(1 if FAIL else 0)
