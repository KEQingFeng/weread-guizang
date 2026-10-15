#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视频转笔记那一格的坏字符（video_note.py）：云转写 / LLM 回的半个代理码点不许把整本书打没。

云转写与 LLM 都是「别人家的 JSON」：接口（或中间那层网关）回了 `"\\ud800"` 这种转义时，
`json.loads` 收得下、Python 内存里也放得住，可它编不进 utf-8 —— 于是整条转笔记流水线
一路跑到「落盘」才炸：

  · 云转写的分片缓存 `_write_json(_part_cache_path(part), …)` → UnicodeEncodeError，
    整趟任务当场垮，已经转好的那几段也白等；
  · 落成一本书时 `body_md.encode("utf-8")`、`transcript.txt` / `merged.md` 的
    `_write_text` → 同一刀；
  · 真 emoji（含 ZWJ 连字）字节不动，坏掉的只是落单的那半个码点。

收口分两处，按仓库里 writer / mindmap / board / anna 的同一口径：
  1. `_write_json` / `_write_text` 是**唯一**的落盘出口，先洗一遍，写盘这一刀不再会抛；
  2. `_save_book` 进成品那道口再洗一遍：`body_md.encode("utf-8")` 与 book_import /
     book_notes 的落盘都不在本文件里，进它们之前先把坏字符摘干净。

不联网、不下模型、不开浏览器：拿假的元信息 + 直接调 `_save_book`，只碰系统临时目录里的
沙盒书库，不碰用户真实书库。四条硬要求：
  · `_drop_surrogates` 摘掉落单的高位 / 低位码点（含只在中间、只剩它）；
  · 真 emoji（含肤色修饰符）与 ZWJ 连字一个字不动；
  · 两个写口喂脏数据不抛、盘上那份干净、不留半截 .tmp；
  · `_save_book` 整条路喂脏数据不抛，落出来的书里所有文件都不含落单码点。
"""
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# 数据目录指进临时沙盒再 import video_note：免得 TMP_ROOT 真去动用户真实缓存。
SANDBOX = pathlib.Path(tempfile.mkdtemp(prefix="guizang-video-surrogate-"))
os.environ["GUIZANG_DATA"] = str(SANDBOX / "cache")
os.environ["GUIZANG_BOOKS"] = str(SANDBOX / "books")
os.makedirs(os.environ["GUIZANG_DATA"], exist_ok=True)

import video_note as vn  # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:220]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


HI, LO = "\ud83d", "\ude00"          # 落单的高位 / 低位代理码点
EMOJI = "🧑\u200d💻"                  # ZWJ 连字：清洗不许碰它
SURROGATE = re.compile("[\ud800-\udfff]")


def no_raise(fn, *a, **kw):
    """跑一次，回 (有没有抛, 结果或异常名)。门禁就是要抓所有抛。"""
    try:
        return False, fn(*a, **kw)
    except Exception as e:                      # noqa: BLE001
        return True, "%s: %s" % (type(e).__name__, str(e)[:120])


def file_is_clean(path):
    """盘上这份文件里不该有落单的半个代理码点。"""
    try:
        with open(path, encoding="utf-8") as f:
            return not SURROGATE.search(f.read())
    except OSError:
        return False


# ── 一、_drop_surrogates：摘掉落单的那半个 ─────────────────────────
chk("落单的高位代理码点被摘掉", vn._drop_surrogates("a" + HI + "b") == "ab")
chk("落单的低位代理码点被摘掉", vn._drop_surrogates("a" + LO + "b") == "ab")
chk("只剩一个落单代理码点时给空串",
    vn._drop_surrogates(HI) == "" and vn._drop_surrogates(LO) == "")
chk("dict / list 逐层走（transcript 那种嵌套结构）",
    vn._drop_surrogates({"t": "标" + HI + "题",
                         "segments": [{"text": "一" + LO + "句"}, 3, None]})
    == {"t": "标题", "segments": [{"text": "一句"}, 3, None]})
chk("不是字符串的原样留着",
    vn._drop_surrogates(7) == 7 and vn._drop_surrogates(None) is None)
chk("清洗是幂等的（洗两遍与一遍相同）",
    vn._drop_surrogates(vn._drop_surrogates(HI + "x" + LO)) == "x")
chk("真 emoji 与 ZWJ 连字一个字不动",
    vn._drop_surrogates("赞 " + EMOJI + " 👍🏽") == "赞 " + EMOJI + " 👍🏽")
chk("只基于「落单」下手：正常中文 / 换行 / 制表都留",
    vn._drop_surrogates("正\n常\t文") == "正\n常\t文")


# ── 二、两个写口：喂脏数据不抛、盘上干净、不留 .tmp ────────────────
TP = SANDBOX / "writes"
TP.mkdir(parents=True, exist_ok=True)

obj = {"title": "标" + HI + "题", "segments": [{"text": "一" + LO + "句"},
                                               {"text": EMOJI}]}
jp = str(TP / "dirty.json")
raised, _ = no_raise(vn._write_json, jp, obj)
chk("_write_json 喂脏数据不抛", not raised)
chk("_write_json 落出来的那份干净", file_is_clean(jp))
chk("_write_json 里真 emoji 仍在", EMOJI in json.load(open(jp, encoding="utf-8"))
    .get("segments", [{}])[1].get("text", ""))
chk("_write_json 不留半截 .tmp", not os.path.exists(jp + ".tmp"))

tp = str(TP / "dirty.txt")
raised, _ = no_raise(vn._write_text, tp, "正" + HI + "文" + EMOJI)
chk("_write_text 喂脏数据不抛", not raised)
chk("_write_text 落出来的那份干净且 emoji 还在",
    file_is_clean(tp) and EMOJI in open(tp, encoding="utf-8").read())
chk("_write_text 不留半截 .tmp", not os.path.exists(tp + ".tmp"))

# 对照：不走这道口，旧写法确实会抛 —— 免得门禁空转。
raw_raised = False
try:
    with open(str(TP / "raw.json"), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
except UnicodeEncodeError:
    raw_raised = True
chk("对照：直写 json.dump(ensure_ascii=False) 确实会抛", raw_raised)


# ── 三、_save_book 整条：脏数据进、干净的书出 ──────────────────────
def fake_probe_meta(url):
    return {"title": "示例视频", "uploader": "示例UP主", "duration": 48,
            "thumbnail": "https://example.invalid/c.jpg", "id": "BV1xx411c7mD"}


vn.probe_meta = fake_probe_meta
plan_info = vn.plan("https://www.bilibili.com/video/BV1xx411c7mD")
# 外站元信息也会脏：标题 / 上传者里夹一个落单码点。
plan_info["title"] = "示例" + HI + "视频"
plan_info["uploader"] = "示例" + LO + "UP主"

D = HI + LO
spoken = {"text": "第一段转写" + HI + "。第二段带 emoji " + EMOJI + "。",
          "segments": [{"start": 0.0, "end": 11.0, "text": "第一段" + HI + "转写"},
                       {"start": 12.0, "end": 29.0, "text": "第二段"}],
          "engine": "cloud", "language": "zh"}
summary = {"title": "示例" + HI + "总结", "overview": "一句话说清" + LO + "这支视频。",
           "bulletPoints": ["要点" + HI + "一", "要点二"],
           "chapters": [{"title": "为什么" + LO + "先讲概念", "start": 0,
                         "summary": "先把概念" + HI + "讲清楚。"},
                        {"title": "方法怎么落地", "start": 12,
                         "summary": "给出三步做法。"}]}
note_markdown = "# 笔记" + HI + "标题\n\n这是假的笔记正文，夹一个 " + LO + " 坏码。"
mindmap = {"version": 1, "title": "导图" + HI + "标题", "root": "root",
           "nodes": [{"id": "root", "label": "根" + LO + "节点", "type": "root",
                      "summary": "总览", "children": [], "time_anchor": None}]}

BOOKS = str(SANDBOX / "books")
os.makedirs(BOOKS, exist_ok=True)
raised, book = no_raise(vn._save_book, BOOKS, plan_info, spoken, summary,
                        note_markdown, mindmap, {"language": "zh"}, "")
chk("_save_book 喂脏数据整条不抛", not raised, book if raised else "")

if not raised and isinstance(book, dict):
    bdir = book.get("dir") or ""
    chk("返回的书书名里那个坏码被摘掉", HI not in str(book.get("title") or "")
        and LO not in str(book.get("title") or ""), book.get("title"))

    want = [vn.TRANS_TXT, vn.TRANS_FILE, vn.SUMMARY_FILE, "merged.md",
            "meta.json", "notes.md", "notes.json", "mindmap.json"]
    missing = [n for n in want if not os.path.isfile(os.path.join(bdir, n))]
    chk("该落的那几份都在", not missing, missing)
    dirty = [n for n in want
             if os.path.isfile(os.path.join(bdir, n)) and not file_is_clean(os.path.join(bdir, n))]
    chk("落出来的每一份都不含落单的半个代理码点", not dirty, dirty)
    chk("整本书里没留半截 .tmp",
        not [p for p in os.listdir(bdir) if p.endswith(".tmp")])

    # 真 emoji 得原样躺在转写里，不能被「洗坏字符」顺手洗掉。
    trans = open(os.path.join(bdir, vn.TRANS_TXT), encoding="utf-8").read()
    chk("转写里那个真 emoji 一个字没动", EMOJI in trans)
else:
    chk("返回的书书名里那个坏码被摘掉", False, "整条已经抛了")
    chk("该落的那几份都在", False, "整条已经抛了")
    chk("落出来的每一份都不含落单的半个代理码点", False, "整条已经抛了")
    chk("整本书里没留半截 .tmp", False, "整条已经抛了")
    chk("转写里那个真 emoji 一个字没动", False, "整条已经抛了")


# ── 收尾 ───────────────────────────────────────────────────────────
shutil.rmtree(SANDBOX, ignore_errors=True)
print()
if FAIL:
    print("视频转笔记坏字符门禁：%d 项通过，失败 %d 项" % (PASSED, len(FAIL)))
    for n in FAIL:
        print("  - " + n)
    sys.exit(1)
print("视频转笔记坏字符门禁：%d 项通过，失败 0 项" % PASSED)
