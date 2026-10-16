#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""剪藏那份 meta 与插图清单 raw/*.json 的读口认 BOM。

为什么要单开这一条
    这两处是「用户能直接改 / 云同步能回写」的 JSON，而它们的读口原先都是裸
    `utf-8`。Windows 记事本「另存为 UTF-8」会多塞三字节 BOM，裸 `utf-8` 读它
    当场 `JSONDecodeError`：

      · `clip_article.save_clip` 把 `import_book` 刚建好的 `meta.json` 读回来
        补剪藏字段，外面罩着 `except Exception: meta = {}`，紧接着
        `meta.update(...)` 再整份写回 —— 带 BOM 时读回空 dict，等于**以空账为底
        重写这份 meta**，用户改过的其它字段一个不剩。同模块的
        `book_layout.read_meta` 早就是 `utf-8-sig`。
      · `download_images.collect_tasks` 扫 `raw/*.json` 列插图任务，坏一份只
        `skipped += 1`，那张图从此不重下、界面上也看不出为什么少图。

    两处都够不着现成的 crash，够得着「静默丢数据」，与取书那四本账 / 视频登记档
    / 便签账同一把尺子：读口一律 `utf-8-sig`，写口照旧 `utf-8`。

这一条钉住
  A. 对照：裸 `utf-8` 读带 BOM 的同一份 JSON 确实抛 JSONDecodeError（洞是真的）；
     并复刻 `save_clip` 原先那段内联读法，证明它会读成空表、`keep_me` 当场消失。
  B. `save_clip`：`import_book` 建出的 `meta.json` 带 BOM 时，剪藏专属字段补得上、
     原有字段一个不丢（走 `book_layout.read_meta` 同一把尺子、不再补第三份读法）。
  C. `download_images.collect_tasks`：带 BOM 的 `raw/*.json` 与干净档列出同一份
     任务清单、跳过计数一致（旧读法会把这一整份当坏记录）。
  D. 源码接线：`save_clip` 走 `book_layout.read_meta`（不再有内联裸 utf-8 读），
     `collect_tasks` 写的是 `utf-8-sig`。

纯函数 + 系统临时目录沙盒，假 `import_book` 顶掉建书那一步，不联网、不开浏览器、
不起服务，故归 --fast 静态段。
"""
import codecs
import inspect
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

SB = tempfile.mkdtemp(prefix="guizang-clipraw-bom-")
os.environ["GUIZANG_DATA"] = os.path.join(SB, "cache")
os.environ["GUIZANG_BOOKS"] = os.path.join(SB, "books")
os.environ["GUIZANG_OUTPUT"] = os.path.join(SB, "output")
os.makedirs(os.environ["GUIZANG_DATA"], exist_ok=True)
os.makedirs(os.environ["GUIZANG_BOOKS"], exist_ok=True)

import book_layout            # noqa: E402
import clip_article           # noqa: E402
import download_images as D   # noqa: E402

BOM = codecs.BOM_UTF8
FAIL = []
N = 0


def chk(name, cond, extra=""):
    global N
    N += 1
    print(("  ok  " if cond else " FAIL ") + name + ("" if cond else "  <- " + repr(extra)))
    if not cond:
        FAIL.append(name)


def put(path, obj, bom=True):
    """按 JSON 落盘；bom=True 时前面挂三字节 BOM（模拟记事本另存）。"""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    with open(path, "wb") as f:
        f.write((BOM if bom else b"") + raw)
    return path


def read_json(path):
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


# ── A. 对照：这个洞是真的 ─────────────────────────────────────────
plain = put(os.path.join(SB, "plain.json"), {"keep_me": 42}, bom=True)
raised = ""
try:
    with open(plain, encoding="utf-8") as f:
        json.load(f)
except Exception as e:                                  # noqa: BLE001
    raised = type(e).__name__
chk("对照：裸 utf-8 读带 BOM 的 JSON 确实解不动（不是理论上的洞）",
    raised == "JSONDecodeError", raised)


def old_save_clip_meta_read(book_dir):
    """复刻 `save_clip` 改之前那段内联读法：裸 utf-8 + 吞异常回空表。"""
    meta = {}
    try:
        with open(os.path.join(book_dir, "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
    except Exception:                                   # noqa: BLE001
        meta = {}
    return meta


OLD_DIR = os.path.join(SB, "old_book")
put(os.path.join(OLD_DIR, "meta.json"), {"title": "旧账", "keep_me": 42}, bom=True)
old_meta = old_save_clip_meta_read(OLD_DIR)
chk("对照：旧读法把带 BOM 的 meta 读成空表（等于以空账为底重写）",
    old_meta == {}, old_meta)

# ── B. save_clip：BOM 不许把原有字段冲掉 ─────────────────────────
CLIP_DIR = os.path.join(SB, "clip_book")
put(os.path.join(CLIP_DIR, "meta.json"),
    {"title": "带 BOM 的文章", "author": "作者甲", "keep_me": 42,
     "user_tag": "我的标签"}, bom=True)

_real_bi = clip_article.book_import


class _FakeBI:
    """假 `book_import`：不真建书，只把「建好的那份带 BOM 的 meta」摆出来。"""

    @staticmethod
    def import_book(out_dir, fname, body, **kw):
        return {"dir": CLIP_DIR, "id": "clip0001", "file": fname}


ART = {"title": "带 BOM 的文章", "author": "作者甲", "site": "示例站",
       "url": "https://example.com/a", "date": "2026-10-16", "cover": "",
       "words": 120, "markdown": "正文第一段。\n"}

clip_article.book_import = _FakeBI
try:
    info = clip_article.save_clip(os.path.join(SB, "out"), ART["url"],
                                  extract_fn=lambda u: dict(ART))
finally:
    clip_article.book_import = _real_bi

after = read_json(os.path.join(CLIP_DIR, "meta.json"))
chk("save_clip：原有的 keep_me / user_tag 字段一个没丢（BOM 不再当空账）",
    after.get("keep_me") == 42 and after.get("user_tag") == "我的标签", after)
chk("save_clip：剪藏专属字段补上了（source=clip / url / words）",
    after.get("source") == "clip" and after.get("url") == ART["url"]
    and after.get("words") == 120, after)
chk("save_clip：写口照旧不主动种 BOM",
    open(os.path.join(CLIP_DIR, "meta.json"), "rb").read(3) != BOM)
chk("save_clip：返回的 info 也带上剪藏字段",
    info.get("source") == "clip" and info.get("url") == ART["url"], info)

# ── C. collect_tasks：BOM 不许让整份插图清单被当坏记录 ───────────
IMG = {"images": [{"url": "https://img.example.com/a.jpg", "file": "ch0001_img01.jpg"},
                  {"url": "https://img.example.com/b.png", "file": "ch0001_img02.png"}]}
raw_bom = os.path.join(SB, "raw_bom")
raw_clean = os.path.join(SB, "raw_clean")
put(os.path.join(raw_bom, "0001.json"), IMG, bom=True)
put(os.path.join(raw_clean, "0001.json"), IMG, bom=False)

tasks_bom, skip_bom = D.collect_tasks(raw_bom, os.path.join(SB, "img_bom"))
tasks_clean, skip_clean = D.collect_tasks(raw_clean, os.path.join(SB, "img_clean"))
chk("collect_tasks：带 BOM 的 raw 仍列得出两条插图任务",
    [u for u, _ in tasks_bom] == ["https://img.example.com/a.jpg",
                                  "https://img.example.com/b.png"], tasks_bom)
chk("collect_tasks：带 BOM 与干净档列出同一份清单、跳过数一致",
    [u for u, _ in tasks_bom] == [u for u, _ in tasks_clean]
    and [os.path.basename(t[1]) for t in tasks_bom]
    == [os.path.basename(t[1]) for t in tasks_clean]
    and skip_bom == skip_clean == 0,
    (tasks_bom, tasks_clean, skip_bom, skip_clean))
# 对照：旧读法会把这一整份当坏记录（skipped += 1）。
old_skip = 0
try:
    with open(os.path.join(raw_bom, "0001.json"), encoding="utf-8") as fh:
        json.load(fh)
except Exception:                                       # noqa: BLE001
    old_skip += 1
chk("对照：旧读法把带 BOM 的 raw 整份记为坏记录（那张图从此不重下）",
    old_skip == 1, old_skip)

# ── D. 源码接线 ──────────────────────────────────────────────────
def src(fn):
    return inspect.getsource(fn)


save_src = src(clip_article.save_clip)
chk("源码：save_clip 走 book_layout.read_meta（不再补第三份 meta 读法）",
    "book_layout.read_meta(info[\"dir\"])" in save_src)
chk("源码：save_clip 里没有内联的裸 utf-8 meta 读口",
    'open(mp, encoding="utf-8")' not in save_src
    and "open(mp, encoding='utf-8')" not in save_src, save_src[:200])
chk("源码：collect_tasks 的 raw 读口是 utf-8-sig",
    'encoding="utf-8-sig"' in src(D.collect_tasks))

# 尺子本身也量一下：read_meta 确实是 utf-8-sig（B 节靠的就是它）。
chk("源码：book_layout.read_meta 是 utf-8-sig（B 节走的那把尺子）",
    'encoding="utf-8-sig"' in src(book_layout.read_meta))

shutil.rmtree(SB, ignore_errors=True)
print()
print("剪藏 meta / 插图清单读口认 BOM：%d 项通过，失败 %d 项" % (N - len(FAIL), len(FAIL)))
if FAIL:
    print("未过：" + " / ".join(FAIL))
sys.exit(1 if FAIL else 0)
