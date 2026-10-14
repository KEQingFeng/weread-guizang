# -*- coding: utf-8 -*-
"""书架清单的脏账本兜底：meta.json / _progress.json 是「合法 JSON 但不是对象」时，
不许把整份书架清单抛出去。

为什么单开一条：`ui_server.list_books()` 读 meta.json 与 _progress.json 时只 `try`
了「读不开」（文件没了 / 半截 / 不是 JSON），没管「读得开但形状不对」。而
``[]`` / ``"书名"`` / ``123`` / ``null`` 都是合法 JSON，`json.load` 照收，接着
``meta.get("title")``、``progress.get("running")`` 就当场 AttributeError。书架是
**一本书脏、全部书看不见** —— 首页 /api/state（4764 行的 list_books）整份 500，
用户看到的是「我的书全没了」。同一个仓库里 `book_meta()` 早就是「不是 dict 就回
空表」，这里对齐那个口径。

跑法：纯函数 + 系统临时目录沙盒，不联网、不开浏览器、不起服务，归 --fast 静态段。
"""
import io
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = tempfile.mkdtemp(prefix="guizang-shelf-dirty-")
os.environ["GUIZANG_DATA"] = os.path.join(ROOT, "cache")
os.environ["GUIZANG_BOOKS"] = os.path.join(ROOT, "books")

import book_layout                                            # noqa: E402
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


def write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8") as f:
        if isinstance(obj, str):
            f.write(obj)          # 原样落一段字节：专门造「合法 JSON、形状不对」
        else:
            json.dump(obj, f, ensure_ascii=False)


def seed(book_id, title="正常的书", source="weread", **extra):
    """造一本「像一本书」的目录，返回它的目录路径。"""
    d = os.path.join(ui_server.OUT_DIR, book_id)
    os.makedirs(os.path.join(d, "chapters"), exist_ok=True)
    with io.open(os.path.join(d, "chapters", "0001.md"), "w", encoding="utf-8") as f:
        f.write("# 第一章\n\n正文。\n")
    meta = {"title": title, "source": source, "done": True}
    meta.update(extra)
    write(os.path.join(d, "meta.json"), meta)
    return d


def ids(module=None):
    return sorted(b["id"] for b in ui_server.list_books(module))


def raw_shelf():
    """直接把 list_books 调一次：抛了就让断言失败，而不是把整个套件带崩。"""
    try:
        return ui_server.list_books(), None
    except Exception as e:                       # noqa: BLE001
        return None, "%s: %s" % (type(e).__name__, e)


# ── 铺书：两本好的 + 一本待弄脏的 ──────────────────────────────────
os.makedirs(ui_server.OUT_DIR, exist_ok=True)
good = seed("36000000000000000b000c01", "好书的标题", author="作者",
            url="", chars=120)
seed("clip_good", "一篇剪藏", source="clip", url="https://mp.weixin.qq.com/s/x")
dirty = seed("video_dirty", "待弄脏")

books, err = raw_shelf()
chk("S1 基线：没弄脏时清单读得出、三本都在",
    err is None and ids() == ["36000000000000000b000c01", "clip_good", "video_dirty"], err)
base = {b["id"]: b for b in (books or [])}
chk("S2 基线：好书的字段照原样带出来（标题 / 来源 / 完成）",
    base.get("36000000000000000b000c01", {}).get("title") == "好书的标题"
    and base.get("clip_good", {}).get("source") == "clip"
    and base.get("clip_good", {}).get("done") is True, base)

# ── meta.json：合法 JSON 但不是对象 ────────────────────────────────
for label, blob in (("空数组 []", "[]"),
                    ("裸字符串", '"书名"'),
                    ("数字", "123"),
                    ("null", "null"),
                    ("数组里塞对象", '[{"title": "假书"}]')):
    write(os.path.join(dirty, "meta.json"), blob)
    books, err = raw_shelf()
    row = next((b for b in (books or []) if b["id"] == "video_dirty"), {})
    chk("S3 meta.json 是%s：不抛，其余书也没被连累" % label,
        err is None and ids() == ["36000000000000000b000c01", "clip_good", "video_dirty"], err)
    chk("S3 meta.json 是%s：这本退回书号当标题、来源退回微信读书" % label,
        row.get("title") == "video_dirty" and row.get("source") == "weread"
        and row.get("url") == "" and row.get("done") is False, row)

chk("S4 meta.json 脏的这本不许把好书顶掉",
    len(books or []) == 3 and
    {b["id"]: b["title"] for b in (books or [])}["clip_good"] == "一篇剪藏", books)

# ── _progress.json：同类形状错 ────────────────────────────────────
write(os.path.join(dirty, "meta.json"), {"title": "待弄脏", "source": "video"})
for label, blob in (("空数组 []", "[]"),
                    ("裸字符串", '"跑着呢"'),
                    ("数字", "7"),
                    ("null", "null")):
    write(os.path.join(dirty, "_progress.json"), blob)
    books, err = raw_shelf()
    row = next((b for b in (books or []) if b["id"] == "video_dirty"), {})
    chk("S5 _progress.json 是%s：不抛，进度回空表" % label,
        err is None and row.get("progress") == {}, err if err else row.get("progress"))

# ── 好形状不许被误伤（护栏而非兜底） ──────────────────────────────
write(os.path.join(dirty, "meta.json"),
      {"title": "好好的一本", "author": "某人", "source": "clip",
       "url": "https://example.com/a", "done": True, "chars": 42})
write(os.path.join(dirty, "_progress.json"),
      {"pages": 5, "running": False, "updated_at": 123}, )
books, err = raw_shelf()
row = next((b for b in (books or []) if b["id"] == "video_dirty"), {})
chk("S6 好形状照原样认：标题 / 作者 / 来源 / 深链 / 完成",
    err is None and row.get("title") == "好好的一本" and row.get("author") == "某人"
    and row.get("source") == "clip" and row.get("done") is True
    and row.get("url") == "https://example.com/a", row)
chk("S7 好进度照原样带出来（不是一律清空）",
    row.get("progress", {}).get("pages") == 5
    and row.get("progress", {}).get("running") is False, row.get("progress"))

# ── 按模块筛也不该被脏书带崩 ──────────────────────────────────────
write(os.path.join(dirty, "meta.json"), "[]")
write(os.path.join(dirty, "_progress.json"), "[]")
try:
    weread_ids = sorted(b["id"] for b in ui_server.list_books("weread"))
    clip_ids = sorted(b["id"] for b in ui_server.list_books("clip"))
    video_ids = sorted(b["id"] for b in ui_server.list_books("video"))
    mod_err = None
except Exception as e:                       # noqa: BLE001
    weread_ids = clip_ids = video_ids = None
    mod_err = "%s: %s" % (type(e).__name__, e)
chk("S8 按模块筛：脏书留在自己那格（video_ 前缀），不串门",
    mod_err is None and weread_ids == ["36000000000000000b000c01"]
    and clip_ids == ["clip_good"] and video_ids == ["video_dirty"],
    (mod_err, weread_ids, clip_ids, video_ids))

# 脏 meta 里认不出 source，模块只能靠前缀认 —— video_ 前缀仍该落视频格
write(os.path.join(dirty, "meta.json"), "[]")
try:
    video_ids = sorted(b["id"] for b in ui_server.list_books("video"))
    vid_err = None
except Exception as e:                       # noqa: BLE001
    video_ids = None
    vid_err = "%s: %s" % (type(e).__name__, e)
chk("S9 脏 meta 丢了 source 时按目录名分类，不误落到微信读书",
    vid_err is None and video_ids == ["video_dirty"], (vid_err, video_ids))

# ── 缺文件 / 半截 JSON 的老行为不许回退 ───────────────────────────
os.remove(os.path.join(dirty, "meta.json"))
os.remove(os.path.join(dirty, "_progress.json"))
books, err = raw_shelf()
row = next((b for b in (books or []) if b["id"] == "video_dirty"), {})
chk("S10 两个文件都不在（老行为）：不抛，退回书号当标题",
    err is None and row.get("title") == "video_dirty" and row.get("progress") == {}, err)

write(os.path.join(dirty, "meta.json"), "{半截")
books, err = raw_shelf()
row = next((b for b in (books or []) if b["id"] == "video_dirty"), {})
chk("S11 半截 JSON（老行为）：不抛，退回书号当标题",
    err is None and row.get("title") == "video_dirty", err)

shutil.rmtree(ROOT, ignore_errors=True)
print()
print("书架脏账本兜底：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
print("ok  书架清单遇到脏 meta / 脏进度不再整份抛")
