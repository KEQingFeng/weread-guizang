#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""取书引擎落盘坏字符：落单的半个代理码点不再把整本书在最后一秒打回。

export_precise.py 是「取书」这条长链路的写盘口：_catalog.json / meta.json /
chapters/*.md / raw/*.json / 合并稿，全都是 `ensure_ascii=False` 或显式 utf-8。
而书名 / 作者 / 章名 / 正文一行行都是从微信读书页面上读回来的，JS 字符串被拦腰
截断时会留下半个代理码点（U+D800–U+DFFF 单独出现），JSON 里的落单代理转义照样
是**合法 JSON**、`json.load` 照收。于是它一路走到写盘：`json.dump` / `f.write`
碰上就当场 UnicodeEncodeError —— 整趟取书（几十分钟）中止在最后一步，`f.write`
那个还只写了一半（旧内容已 truncate），盘上留一份半截章节。

仓库里其它落盘口（便签 / 画板 / 导图 / 个人资料 / meta 读侧）各有一道
`drop_lone_surrogates` 收口，只有这台引擎一直没接。这一条把三处写口钉住：

  A `_dump_json`      —— 目录 / meta 这类账本；
  B `save_chapter`    —— 章节 .md 与 raw .json；
  C `merge_chapters`  —— 合并稿的书名 / 作者标题行。

同时钉「干净文本一个字不改」与一条对照：同一个坏码点直接写文件确实会抛 ——
免得哪天收口被拿掉，这条门禁还在空转。

playwright 没装时引擎 import 不起来，这条明确标 SKIP，绝不把「没测」装成「通过」。
跑法：.venv/bin/python tests/check_export_surrogate.py
不联网、不起浏览器、不碰真实书库（全在系统临时目录里）。
"""
import json
import os
import pathlib
import shutil
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent

# 用 chr() 拼出来，不写进源码里的转义：半个代理码点在源码字符串字面量里能写，
# 但放进模块 docstring 会连 import 都过不去（Python 3.13 会把 __doc__ 编一遍）。
LONE_HIGH = chr(0xD83D)    # 高代理，落单
LONE_LOW = chr(0xDC00)     # 低代理，落单

BAD_TITLE = "归藏" + LONE_HIGH + "样张"
BAD_AUTHOR = "作" + LONE_HIGH + "者"
BAD_CHAPTER = "第一章 " + LONE_HIGH + " 标题"
BAD_BODY = "正文一行" + LONE_LOW + "，再有半行"
CLEAN_TITLE = "样张·书名 中文 🧭"

FAIL = []
SKIPS = []


def chk(name, ok, extra=""):
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else "   " + repr(extra)[:220]))
    if not ok:
        FAIL.append(name)


def skip(name, why):
    SKIPS.append(name)
    print(("SKIP  " + name + "   " + why)[:260])


def has_lone(s):
    return any(0xD800 <= ord(c) <= 0xDFFF for c in s)


def read_text(path):
    with open(path, "rb") as f:
        return f.read().decode("utf-8")


# ---------- 对照：坏码点直接写 utf-8 确实会抛 ----------
def control_crashes():
    tmp = tempfile.mkdtemp(prefix="guizang-export-surrogate-ctl-")
    try:
        p = os.path.join(tmp, "x.json")
        try:
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"title": BAD_TITLE}, f, ensure_ascii=False)
        except UnicodeEncodeError:
            chk("对照：坏码点直接 json.dump(ensure_ascii=False) 确实抛 UnicodeEncodeError", True)
        else:
            chk("对照：坏码点直接 json.dump(ensure_ascii=False) 确实抛 UnicodeEncodeError",
                False, "没抛，说明这个坏码点根本没进到写盘那一步，本条门禁没意义")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_functional():
    try:
        sys.path.insert(0, str(ROOT))
        import export_precise as E          # noqa: E402
    except Exception as e:                  # 多半是没装 playwright
        skip("真跑：取书引擎三处写口吃下落单代理码点",
             "import export_precise 失败（%s: %s），这条起不来" % (type(e).__name__, e))
        return

    tmp = tempfile.mkdtemp(prefix="guizang-export-surrogate-")
    try:
        # ---- A：_dump_json（目录 / meta 这类账本） ----
        meta_path = os.path.join(tmp, "meta.json")
        try:
            E._dump_json(meta_path, {"title": BAD_TITLE, "author": BAD_AUTHOR,
                                     "tags": [BAD_TITLE, "正常标签"]}, indent=2)
            dumped = True
        except Exception as e:
            dumped = False
            chk("A _dump_json：脏书名 / 作者不抛", False, "%s: %s" % (type(e).__name__, e))
        if dumped:
            chk("A _dump_json：脏书名 / 作者不抛", True)
            obj = json.loads(read_text(meta_path))
            chk("A _dump_json：落盘账本里没有落单代理码点",
                not has_lone(obj["title"]) and not has_lone(obj["author"])
                and not any(has_lone(t) for t in obj["tags"]), obj)
            chk("A _dump_json：脏值只少吃掉那一个码点，其余原样",
                obj["title"] == "归藏样张" and obj["author"] == "作者"
                and obj["tags"] == ["归藏样张", "正常标签"], obj)

        # 干净的账本一个字不改
        ok_path = os.path.join(tmp, "meta_ok.json")
        E._dump_json(ok_path, {"title": CLEAN_TITLE, "n": 3}, indent=2)
        chk("A _dump_json：干净文本一个字不改",
            json.loads(read_text(ok_path)) == {"title": CLEAN_TITLE, "n": 3})

        # 写脏也不许留 .tmp
        chk("A _dump_json：写脏不留 .tmp",
            not [f for f in os.listdir(tmp) if f.endswith(".tmp")], os.listdir(tmp))

        # ---- B：save_chapter（章节 .md + raw .json） ----
        md_dir = os.path.join(tmp, "chapters")
        raw_dir = os.path.join(tmp, "raw")
        os.makedirs(md_dir)
        os.makedirs(raw_dir)
        saved = None
        try:
            saved = E.save_chapter(BAD_CHAPTER, [{"type": "text", "text": BAD_BODY}],
                                   0, md_dir, raw_dir)
            chk("B save_chapter：脏章名 / 正文不抛", True)
        except Exception as e:
            chk("B save_chapter：脏章名 / 正文不抛", False, "%s: %s" % (type(e).__name__, e))
        if saved is not None:
            chk("B save_chapter：报的字符数是干净正文的长度", saved[0] == len(BAD_BODY), saved)

        md_path = os.path.join(md_dir, "0000.md")
        if os.path.isfile(md_path):
            try:
                text = read_text(md_path)
                chk("B save_chapter：章节 md 按 UTF-8 解得开、正文还在、坏码点被摘",
                    "正文一行" in text and "，再有半行" in text and not has_lone(text), text)
            except UnicodeDecodeError as e:
                chk("B save_chapter：章节 md 按 UTF-8 解得开", False, str(e))
        else:
            chk("B save_chapter：章节 md 落盘了", False,
                os.listdir(md_dir) if os.path.isdir(md_dir) else md_dir)

        raw_path = os.path.join(raw_dir, "0000.json")
        if os.path.isfile(raw_path):
            rec = json.loads(read_text(raw_path))
            chk("B save_chapter：raw json 的章名干净、结构没变",
                rec.get("title") == "第一章  标题" and rec.get("images") == []
                and rec.get("text_len") == len(BAD_BODY), rec)
        else:
            chk("B save_chapter：raw json 落盘了", False,
                os.listdir(raw_dir) if os.path.isdir(raw_dir) else raw_dir)

        chk("B save_chapter：写脏不留 .tmp",
            not [f for f in os.listdir(raw_dir) if f.endswith(".tmp")], os.listdir(raw_dir))

        # ---- C：merge_chapters（合并稿标题行） ----
        out_dir = os.path.join(tmp, "output")
        os.makedirs(out_dir)
        merged = ""
        try:
            merged = E.merge_chapters(md_dir, out_dir, "样张_书名", BAD_TITLE, BAD_AUTHOR)
            chk("C merge_chapters：脏书名 / 作者不抛", True)
        except Exception as e:
            chk("C merge_chapters：脏书名 / 作者不抛", False, "%s: %s" % (type(e).__name__, e))
        if merged and os.path.isfile(merged):
            try:
                text = read_text(merged)
                chk("C merge_chapters：合并稿解得开、标题行干净、正文还在",
                    "归藏样张" in text and "作者" in text
                    and "正文一行" in text and not has_lone(text), text[:200])
            except UnicodeDecodeError as e:
                chk("C merge_chapters：合并稿按 UTF-8 解得开", False, str(e))
        else:
            chk("C merge_chapters：合并稿落盘了", False, str(merged))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


control_crashes()
run_functional()

print()
if SKIPS:
    print("取书落盘坏字符取证：跳过 %d 项（%s）" % (len(SKIPS), "、".join(SKIPS)))
print("取书落盘坏字符取证：" + ("全部通过" if not FAIL else "%d 项未过：%s" % (len(FAIL), FAIL)))
sys.exit(1 if FAIL else 0)
