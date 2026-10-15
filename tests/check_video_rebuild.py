# -*- coding: utf-8 -*-
"""归藏门禁：视频书「重建」不被手工改过的 summary.json 甩掉连接。

重建（`/api/video` 的 `act=rebuild`，落点是 `video_note.rebuild_book`）读的是
**磁盘上那份** `summary.json`，而不是模型刚给的那一份。`summary.json` 是用户能直接
打开改的文本，旧版本也可能写下别的形状。原先这条路把磁盘上的 `summary` 原样交给
`_chapter_bodies`，而那边拿的是

    starts = [_sec(chapter.get("start")) for chapter in chapters]   # 非对象 → AttributeError
    chapters[i]["title"]                                            # 少 title → KeyError

于是「chapters 里有一项被改成字符串」或「某一章少了 title」这类改坏，在重建这条路上
就是：`/api/video` 只接 `ValueError`，异常穿出 `do_POST` 后连接被直接掐掉 —— 界面上
看到的是「点了没反应」，跟功能没实现一模一样。坏的是账本不是命令，能救回来的正文
（转写一个字不能少）照留，认不出的章行丢掉。

四段：
  A 磁盘上那份 summary 先过 `_clean_summary` 同一把尺子（不是只洗模型输出那一次）。
  B 脏章行（不是对象 / 少 title / chapters 整个写成字符串或对象）重建不抛，
    转写正文一个字不少地落在重建稿里。
  C 好数据不改：合法章节名照旧沿用、章数不变（加固不许把好账洗坏）。
  D 接线：两个读章的落点都不再裸取 `["title"]`，纯函数对非对象行也不再抛。

全程在系统临时目录里跑一本假书，不起服务、不联网、不开浏览器。
"""
import inspect
import json
import os
import pathlib
import shutil
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import video_note as vn  # noqa: E402

FAILED = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(name)
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)))


def plain(s):
    """去掉空白后的正文，用来验「内容一个字没少」。"""
    return "".join(str(s or "").split())


SEGS = [{"start": 0, "text": "甲甲甲甲"}, {"start": 10, "text": "乙乙乙乙"},
        {"start": 20, "text": "丙丙丙丙"}]
SEG_TEXTS = [plain(s["text"]) for s in SEGS]
SANDBOX = tempfile.mkdtemp(prefix="guizang-vidrebuild-")


def make_book(name, summary_obj, segments=None):
    """铺一本最小的视频书：转写 + 摘要 + meta，够 rebuild_book 走完。"""
    d = os.path.join(SANDBOX, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, vn.TRANS_FILE), "w", encoding="utf-8") as f:
        json.dump({"schema": 1, "segments": segments if segments is not None else SEGS,
                   "engine": "cloud", "language": "zh", "duration": 30}, f,
                  ensure_ascii=False)
    with open(os.path.join(d, vn.SUMMARY_FILE), "w", encoding="utf-8") as f:
        json.dump({"summary": summary_obj}, f, ensure_ascii=False)
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"title": "测试视频", "source": "video",
                   "platform": "bilibili", "uploader": "某人",
                   "duration": 30, "page_title": "测试视频"}, f, ensure_ascii=False)
    return d


def rebuilt_text(book_dir):
    """重建稿里所有正文字：各章文件 + merged.md 全读一遍。"""
    out = []
    ch = os.path.join(book_dir, "chapters")
    if os.path.isdir(ch):
        for fn in sorted(os.listdir(ch)):
            with open(os.path.join(ch, fn), encoding="utf-8") as f:
                out.append(f.read())
    mp = os.path.join(book_dir, "merged.md")
    if os.path.isfile(mp):
        with open(mp, encoding="utf-8") as f:
            out.append(f.read())
    return plain("".join(out))


def rebuild(book_dir):
    """跑一次重建，返回 (异常或 None, 结果或 None)。"""
    try:
        return None, vn.rebuild_book(book_dir)
    except Exception as e:                                  # noqa: BLE001
        return e, None


def catalog(book_dir):
    with open(os.path.join(book_dir, "_catalog.json"), encoding="utf-8") as f:
        return json.load(f)


try:
    # ── A、接线：磁盘上那份也要过同一把尺子 ──────────────────────────
    REBUILD_SRC = inspect.getsource(vn.rebuild_book)
    BODIES_SRC = inspect.getsource(vn._chapter_bodies)
    chk("A1 重建读盘后走 _clean_summary（读盘与读模型输出同一把尺子）",
        "_clean_summary(raw_summary" in REBUILD_SRC,
        "rebuild_book 里没把磁盘上那份 summary 过 _clean_summary")
    chk("A2 章体切分不再裸下标取 title（少 title 一章就是 KeyError）",
        '["title"]' not in BODIES_SRC, "还在裸下标取 title")
    chk("A3 纯函数对非对象章行先过滤（不是直接 .get）",
        "isinstance(chapter, dict)" in BODIES_SRC)

    # ── B、脏章行：不抛，正文一个字不少 ─────────────────────────────
    dirty = {
        "chapter-missing-title": {"title": "t", "overview": "概览",
                                  "chapters": [{"start": 0, "summary": "没有标题"}]},
        "chapter-is-string": {"title": "t", "chapters": ["第一章"]},
        "chapter-is-number": {"title": "t", "chapters": [1, 2]},
        "chapters-is-string": {"title": "t", "chapters": "abc"},
        "chapters-is-object": {"title": "t", "chapters": {"a": 1}},
        "chapters-null": {"title": "t", "chapters": None},
        "summary-is-string": "整个摘要被改成字符串",
        "mixed-rows": {"title": "t", "chapters": [None, {"title": "甲", "start": 0},
                                                  "坏", {"start": 10}]},
    }
    for name, summ in dirty.items():
        d = make_book(name, summ)
        err, _res = rebuild(d)
        chk("B 脏账本「%s」重建不抛" % name, err is None, err)
        if err is not None:
            continue
        chk("B 脏账本「%s」转写正文一个字不少" % name,
            all(t in rebuilt_text(d) for t in SEG_TEXTS), rebuilt_text(d)[:200])

    # 起点递增但某章缺 title：老代码正是从这里 KeyError 出去的（按时间切那条路）
    inc = make_book("increasing-starts", {"title": "t", "chapters": [
        {"start": 0, "summary": "s"}, {"title": "乙", "start": 15, "summary": "s"}]})
    err, _res = rebuild(inc)
    chk("B 起点递增但某章缺 title：不抛 KeyError", err is None, err)
    chk("B 起点递增但某章缺 title：正文仍齐全",
        all(t in rebuilt_text(inc) for t in SEG_TEXTS), rebuilt_text(inc)[:200])

    # ── C、好数据不改 ───────────────────────────────────────────────
    good = make_book("good", {"title": "t", "overview": "概览",
                              "bulletPoints": ["要点一", "要点二"],
                              "chapters": [{"title": "甲章", "start": 0, "summary": "s1"},
                                           {"title": "乙章", "start": 15, "summary": "s2"}]})
    err, res = rebuild(good)
    chk("C1 合法摘要重建成功", err is None, err)
    names = catalog(good) if err is None else []
    chk("C2 合法章节名照旧沿用（加固没把好账洗坏）",
        names[:2] == ["甲章", "乙章"], names)
    chk("C3 合法摘要章数不变", res is not None and res.get("chapters") == 2, res)
    chk("C4 合法摘要每一段的正文都在", all(t in rebuilt_text(good) for t in SEG_TEXTS))

    # ── D、纯函数：坏章行丢一行，不丢整篇 ───────────────────────────
    bodies = vn._chapter_bodies("", [], [{"title": "好章", "start": 0}, "坏行", 7, None])
    chk("D1 非对象章行被丢掉，好章留下", bodies == [("好章", "")], bodies)
    bodies = vn._chapter_bodies("整段转写", [], [{"start": 0}])
    chk("D2 缺 title 的章行给「第 N 章」而不是抛", bodies == [("第 1 章", "整段转写")], bodies)
    bodies = vn._chapter_bodies("整段转写", [], ["坏行", 7])
    chk("D3 一行对象都没有时回「全文转写」，内容不丢",
        bodies == [("全文转写", "整段转写")], bodies)

    # ── E、重建不碰转写本体：正文与原文一致 ─────────────────────────
    with open(os.path.join(good, vn.TRANS_FILE), encoding="utf-8") as f:
        segs_after = json.load(f)["segments"]
    chk("E1 重建不改写 transcript.json 的段落",
        [s["text"] for s in segs_after] == [s["text"] for s in SEGS],
        [s.get("text") for s in segs_after])
finally:
    shutil.rmtree(SANDBOX, ignore_errors=True)

print()
if FAILED:
    print("视频书重建脏账本体检：失败 %d 项" % len(FAILED))
    for name in FAILED:
        print("  ✗ " + name)
    sys.exit(1)
print("视频书重建脏账本体检：%d 项通过，失败 0 项" % PASSED)
