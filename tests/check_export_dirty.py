# -*- coding: utf-8 -*-
"""取书开跑前那两笔读盘：raw/*.json 与 meta.json 的脏形状收口。

`export_precise.main()` 在拉起浏览器之前先读两样磁盘上的东西：

  * `raw/*.json` —— 上一轮存下的「已经见过的图地址」，用来跳过重复下载；
  * `meta.json`  —— 界面取书时写的书名 / 作者，用来决定合并稿叫什么名字。

两笔旧读法都只兜了 json.load 那一下，读出来之后是什么形状一概不问：

  * `raw/0001.json` 是半截 JSON          → JSONDecodeError，整趟取书还没开浏览器就垮
  * `raw/0001.json` 是 `[]` / `"x"` / `5` → `.get("images", [])` 抛 AttributeError
  * 图记录里缺 `url`                      → `img["url"]` 抛 KeyError
  * `meta.json` 是 `[]` / `"书名"` / `123` → `meta.get("title")` 抛 AttributeError

前一条 check_export_catalog.py 收的是同一趟里的 `_catalog.json` / `_stuck.json`，
这一条把剩下的两笔补齐，口径与仓库其它账本一致：**认不出的当空账，能认的一个字不改**。
纯逻辑 + 临时目录，不联网、不开浏览器，归 --fast 静态段。
"""
import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import export_precise as E  # noqa: E402

FAIL = []


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))
    if not cond:
        FAIL.append(name)


TMP = tempfile.mkdtemp(prefix="guizang-export-dirty-")
BOOK = os.path.join(TMP, "book")
RAW = os.path.join(BOOK, "raw")
os.makedirs(RAW)
META = os.path.join(BOOK, "meta.json")


def write(name, obj):
    """在 raw/ 下落一份 JSON；obj 是 {'raw': '…'} 时直接写原字节（造非 JSON）。"""
    p = os.path.join(RAW, name)
    with io.open(p, "w", encoding="utf-8") as f:
        if isinstance(obj, dict) and set(obj) == {"raw"}:
            f.write(obj["raw"])
        else:
            json.dump(obj, f, ensure_ascii=False)
    return p


def write_meta(obj):
    with io.open(META, "w", encoding="utf-8") as f:
        if isinstance(obj, dict) and set(obj) == {"raw"}:
            f.write(obj["raw"])
        else:
            json.dump(obj, f, ensure_ascii=False)


# ── A. read_meta_dict：脏形状当没有 meta，好数据一个字不改 ────────────────
write_meta({"title": "置身事内", "author": "兰小欢", "done": True, "extra": [1, 2]})
got = E.read_meta_dict(BOOK)
chk("好 meta：原样读回（每个字段都不动）",
    got == {"title": "置身事内", "author": "兰小欢", "done": True, "extra": [1, 2]}, got)

for label, shape in {"整数列表": [], "字符串": "书名", "数字": 123,
                     "null": None, "布尔": True, "嵌套列表": [["a"]]}.items():
    write_meta(shape)
    try:
        chk("脏 meta（%s）：read_meta_dict 回空表不抛" % label,
            E.read_meta_dict(BOOK) == {}, E.read_meta_dict(BOOK))
    except Exception as e:  # noqa: BLE001 —— 抛了就是这条门禁要抓的回归
        chk("脏 meta（%s）：read_meta_dict 回空表不抛" % label, False,
            "%s: %s" % (type(e).__name__, e))

write_meta({"raw": '{"title": "半截'})
chk("半截 meta（非法 JSON）：回空表不抛", E.read_meta_dict(BOOK) == {})

os.remove(META)
chk("缺 meta：回空表不抛", E.read_meta_dict(BOOK) == {})

# 用户可见的落点：脏 meta 之后书名退回书号，而不是 AttributeError 把取书带走。
write_meta([])
meta = E.read_meta_dict(BOOK)
book_id = "abc123"
book_title = meta["title"] if meta.get("title") and meta["title"] != book_id else ""
chk("脏 meta 之后仍认得出「没有书名」：退回书号", book_title == "", book_title)

# ── B. collect_seen_images：一条坏记录只跳过它自己 ───────────────────────
shutil.rmtree(RAW)
os.makedirs(RAW)
write("0001.json", {"title": "第一章", "text_len": 10,
                    "images": [{"url": "https://img/1.png", "file": "1.png"},
                               {"url": "https://img/2.png", "file": "2.png"}]})
write("0002.json", {"title": "第二章", "text_len": 5,
                    "images": [{"url": "https://img/2.png", "file": "2.png"},
                               {"url": "https://img/3.png", "file": "3.png"}]})
with io.open(os.path.join(RAW, "readme.txt"), "w", encoding="utf-8") as f:
    f.write("这不是 JSON")
got = E.collect_seen_images(RAW)
chk("好 raw：收齐去重后的 url，且非 .json 文件不参与",
    got == {"https://img/1.png", "https://img/2.png", "https://img/3.png"}, got)

DIRTY_FILES = {
    "半截 JSON": {"raw": '[{"url": "https://img/bad.png"'},
    "整数列表": [1, 2, 3],
    "字符串": "不是对象",
    "数字": 5,
    "null": None,
    "布尔": True,
    "images 不是列表": {"images": "x"},
    "images 是对象": {"images": {"url": "https://img/x.png"}},
    "images 里混脏": {"images": [None, 7, "x", {"url": "https://img/good.png"},
                               {"file": "no-url.png"}, {"url": 123}, {"url": ""},
                               {"url": "https://img/good.png"}]},
}
for label, shape in DIRTY_FILES.items():
    shutil.rmtree(RAW)
    os.makedirs(RAW)
    write("0001.json", {"images": [{"url": "https://img/keep.png", "file": "k.png"}]})
    write("0002.json", shape)
    try:
        got = E.collect_seen_images(RAW)
        # 好文件里的那张必须照收；同一份文件里的坏条目不许牵连它自己的好条目。
        expect = {"https://img/keep.png"}
        if label == "images 里混脏":
            expect.add("https://img/good.png")
        chk("脏 raw（%s）：坏文件/坏条目只跳过自己，好记录照收" % label,
            got == expect, got)
    except Exception as e:  # noqa: BLE001
        chk("脏 raw（%s）：坏文件/坏条目只跳过自己，好记录照收" % label, False,
            "%s: %s" % (type(e).__name__, e))

shutil.rmtree(RAW)
chk("raw/ 目录不存在：回空集合不抛", E.collect_seen_images(RAW) == set())

# 好 raw 里带额外字段 / 空图列表：一个 url 都不能丢、也不能凭空多出来
os.makedirs(RAW)
write("0001.json", {"title": "x", "images": [], "text_len": 0})
write("0002.json", {"images": [{"url": "https://img/a.png", "file": "a.png",
                                "w": 100, "h": 200}]})
chk("好 raw（空图列表 + 额外字段）：只收真 url",
    E.collect_seen_images(RAW) == {"https://img/a.png"}, E.collect_seen_images(RAW))

# ── C. 对照：同一份字节喂给旧读法确实会抛（防门禁空转） ───────────────
raised = False
try:
    json.loads("[]").get("images", [])
except AttributeError:
    raised = True
chk("对照：整数列表喂给旧读法（json.load(...).get）确实抛 AttributeError", raised)

raised = False
try:
    json.loads('{"images": [{"file": "x.png"}]}')["images"][0]["url"]
except KeyError:
    raised = True
chk("对照：图记录缺 url 喂给旧写法（img[\"url\"]）确实抛 KeyError", raised)

raised = False
try:
    json.loads("[]").get("title")
except AttributeError:
    raised = True
chk("对照：整数列表 meta 喂给旧读法（meta.get）确实抛 AttributeError", raised)

raised = False
try:
    json.loads('[{"url": "x"}')
except ValueError:
    raised = True
chk("对照：半截 JSON 喂给 json.loads 确实抛 ValueError", raised)

# ── D. 接线：main() 开跑前那两笔都过新尺子，旧裸读法不再出现 ───────────
src = io.open(os.path.join(ROOT, "export_precise.py"), encoding="utf-8").read()
chk("接线：main 开跑前的图地址走 collect_seen_images(raw_dir)",
    "seen_imgs = collect_seen_images(raw_dir)" in src)
chk("接线：两处读书名的位置都走 read_meta_dict(book_dir)",
    src.count("read_meta_dict(book_dir)") >= 2, src.count("read_meta_dict(book_dir)"))
chk("接线：不再有裸的 json.load(raw/*.json).get(\"images\")",
    '.get("images", [])' not in src)
chk("接线：不再有裸的 json.load(open(meta_path))",
    'json.load(open(meta_path' not in src)

shutil.rmtree(TMP, ignore_errors=True)
print()
print("取书开跑前的读盘脏形状取证：" + ("全部通过" if not FAIL else "%d 项未过：%s" % (len(FAIL), FAIL)))
sys.exit(1 if FAIL else 0)
