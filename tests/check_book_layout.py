# -*- coding: utf-8 -*-
"""书库按模块分文件夹 + 账本按模块分格 —— 1.0.1 那条「别再全挤在一个目录里」的验收。

三段都在系统临时目录的沙盒里跑，不碰用户真实书库、不联网、不开浏览器：

  A `book_layout` 的纯逻辑：书属于哪个模块、去哪儿找它、旧位置能不能兜住、
    路径穿越挡不挡、搬家会不会覆盖别人的东西。
  B ui_server 接线：旧版平铺的书，启动归置后能不能各回各家；清单是不是按模块分得开
    （剪藏的书不许出现在微信读书那一栏）；删一本书会不会连带删掉别人的合并稿。
  C 账本分格：老版摊在顶层的 folders/assign/order/state 读进来要折对格子；
    建夹、归入、贴标签只动自己那一格；空文件夹不许被「清理」悄悄削掉。
"""
import io
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = tempfile.mkdtemp(prefix="guizang-layout-")
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


def mkdir(*parts):
    d = os.path.join(ROOT, *parts)
    os.makedirs(d, exist_ok=True)
    return d


def seed_book(root, book_id, source=None, fmt=None, meta_extra=None):
    """造一本「像一本书」的目录：meta.json + chapters/ 里一章。"""
    d = os.path.join(root, book_id)
    os.makedirs(os.path.join(d, "chapters"), exist_ok=True)
    io.open(os.path.join(d, "chapters", "0001.md"), "w", encoding="utf-8").write("# 第一章\n\n正文。\n")
    meta = {"title": "书" + book_id, "source": source or "weread", "done": True}
    if fmt:
        meta["format"] = fmt
    meta.update(meta_extra or {})
    _write_json(os.path.join(d, "meta.json"), meta)
    return d


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def read_json(path):
    with io.open(path, encoding="utf-8") as f:
        return json.load(f)


BOOKS = ui_server.OUT_DIR

# ── A：book_layout 的纯逻辑 ────────────────────────────────────────
tmp = mkdir("a")
chk("A1 前缀认模块", [book_layout.module_of(x) for x in
    ("clip_abc", "feed_x", "video_y", "imp_z", "flomo_a1b2c3", "36332dc0813abb933g016d22")]
    == ["clip", "feed", "video", "local", "flomo", "weread"])
chk("A2 meta 比前缀更该信（导入书写 source=local）",
    book_layout.module_of("imp_a", {"source": "local"}) == "local")
chk("A3 source=clip 归剪藏", book_layout.module_of("42", {"source": "clip"}) == "clip")
chk("A4 不认识的一律算微信读书（不会丢书，只是少个分组）",
    book_layout.module_of("42", {"source": "????"}) == "weread")

seed_book(tmp, "clip_hey", "clip")
seed_book(tmp, "36000000000000000a000b01", "weread")
chk("A5 平铺着也能按 id 找到（还没搬的旧书）",
    book_layout.resolve(tmp, "clip_hey") == os.path.realpath(os.path.join(tmp, "clip_hey")))
os.makedirs(os.path.join(tmp, book_layout.DIR_OF["clip"]), exist_ok=True)
shutil.move(os.path.join(tmp, "clip_hey"), os.path.join(tmp, book_layout.DIR_OF["clip"], "clip_hey"))
chk("A6 搬进模块后仍按 id 找到", book_layout.resolve(tmp, "clip_hey") ==
    os.path.realpath(os.path.join(tmp, "剪藏", "clip_hey")))
chk("A7 平铺在根上的旧书照样认", book_layout.resolve(tmp, "36000000000000000a000b01") ==
    os.path.realpath(os.path.join(tmp, "36000000000000000a000b01")))
chk("A8 路径穿越进不来", book_layout.resolve(tmp, "../etc") is None and
    book_layout.resolve(tmp, "") is None)
chk("A9 locate 给出模块", book_layout.locate(tmp, "clip_hey")[1] == "clip" and
    book_layout.locate(tmp, "36000000000000000a000b01")[1] == "weread")

# walk 两种位置都收，且不把模块文件夹本身当成一本书
found = {bid: mod for bid, _d, mod in book_layout.walk(tmp)}
chk("A10 walk 收到两本、认出各自模块",
    found == {"clip_hey": "clip", "36000000000000000a000b01": "weread"}, found)

# migrate：目标位已有同名目录时绝不覆盖，只跳过并出声
m = mkdir("m")
old = seed_book(m, "clip_dup", "clip", meta_extra={"title": "旧的那篇"})
io.open(os.path.join(old, "chapters", "0002.md"), "w", encoding="utf-8").write("旧的正文\n")
newdir = os.path.join(m, book_layout.DIR_OF["clip"], "clip_dup")
os.makedirs(os.path.join(newdir, "chapters"), exist_ok=True)
io.open(os.path.join(newdir, "chapters", "0001.md"), "w", encoding="utf-8").write("新的正文\n")
seed_book(m, "video_one", "video")
seed_book(m, "imp_two", "local", fmt="epub")
says = []
moved = book_layout.migrate(m, says.append)
chk("M1 撞名的跳过，谁也不盖谁",
    os.path.isfile(os.path.join(newdir, "chapters", "0001.md")) and
    os.path.isfile(os.path.join(old, "chapters", "0002.md")), moved)
chk("M2 不撞名的搬进各自模块",
    os.path.isdir(os.path.join(m, "视频", "video_one")) and
    os.path.isdir(os.path.join(m, "本地书架", "imp_two")), moved)
chk("M3 搬完旧位置不再留壳", not os.path.isdir(os.path.join(m, "video_one")))
chk("M4 跳过的那本还在旧位置，一个字没动",
    os.path.isfile(os.path.join(old, "chapters", "0001.md")) and
    os.path.isfile(os.path.join(old, "chapters", "0002.md")))
# 撞名时谁对谁错看不出来，界面上以模块目录里那份为准（旧位置留给人自己处置）
chk("M4b 两处都有同名书时读的是模块目录里那份",
    book_layout.resolve(m, "clip_dup") == os.path.realpath(newdir))
dup = [x for x in book_layout.walk(m) if x[0] == "clip_dup"]
chk("M4c 同名两份只列一份（界面上不会多出一张重复的卡）", len(dup) == 1, dup)
io.open(os.path.join(m, "散着的笔记.md"), "w", encoding="utf-8").write("不是书\n")
os.makedirs(os.path.join(m, "随便一个文件夹"), exist_ok=True)
chk("M5 再跑一次不搬散文件、不搬模块名以外的怪目录",
    book_layout.migrate(m, says.append) == [] and os.path.isfile(os.path.join(m, "散着的笔记.md")))

# ── B：ui_server 接线 ─────────────────────────────────────────────
shutil.rmtree(BOOKS, ignore_errors=True)
os.makedirs(BOOKS, exist_ok=True)
seed_book(BOOKS, "36000000000000000b000c01", "weread")
seed_book(BOOKS, "clip_post", "clip", meta_extra={"url": "https://mp.weixin.qq.com/s/x"})
seed_book(BOOKS, "feed_item", "feed")
seed_book(BOOKS, "video_abc", "video", meta_extra={"cover": "https://i.example/c.jpg"})
seed_book(BOOKS, "imp_mine", "local", fmt="epub")
io.open(os.path.join(BOOKS, "36000000000000000b000c01.md"), "w", encoding="utf-8").write("合并稿\n")

ui_server.ensure_books_dir()
chk("B1 启动归置：七个模块文件夹都在（含便签与写作那两格）",
    len(book_layout.ORDER) == 7
    and all(os.path.isdir(os.path.join(BOOKS, book_layout.DIR_OF[x])) for x in book_layout.ORDER))
chk("B2 平铺的旧书各回各家",
    os.path.isdir(os.path.join(BOOKS, "剪藏", "clip_post")) and
    os.path.isdir(os.path.join(BOOKS, "微信读书", "36000000000000000b000c01")) and
    os.path.isdir(os.path.join(BOOKS, "视频", "video_abc")))
chk("B3 safe_book_dir 按 id 找到新位置的目录",
    ui_server.safe_book_dir("clip_post") ==
    os.path.realpath(os.path.join(BOOKS, "剪藏", "clip_post")))
chk("B4 非法 id 仍然挡在外", ui_server.safe_book_dir("../cache") is None and
    ui_server.safe_book_dir("") is None and ui_server.safe_book_dir("a/b") is None)

all_books = {b["id"]: b for b in ui_server.list_books()}
chk("B5 清单带上模块字段", set(all_books) ==
    {"clip_post", "feed_item", "video_abc", "imp_mine", "36000000000000000b000c01"} and
    all(b.get("module") for b in all_books.values()), {k: v.get("module") for k, v in all_books.items()})
chk("B6 按模块列：剪藏只回剪藏", [b["id"] for b in ui_server.list_books("clip")] == ["clip_post"])
chk("B7 按模块列：微信读书不回别人的书",
    [b["id"] for b in ui_server.list_books("weread")] == ["36000000000000000b000c01"])
vids = ui_server.video_books()
chk("B8 视频清单从模块目录里读", [v["id"] for v in vids] == ["video_abc"], vids)
chk("B9 视频书带上视频封面地址", vids and vids[0]["cover"].startswith("https://"))
chk("B10 book_module 现算模块", ui_server.book_module("clip_post") == "clip" and
    ui_server.book_module("36000000000000000b000c01") == "weread" and
    ui_server.book_module("没这本书") == "weread")

# 模块目录当写入根：引擎与四个写入方沿用「<根>/<书号>」那层平铺
chk("B11 module_dir 就是各模块那一格",
    ui_server.module_dir("clip") == os.path.join(BOOKS, "剪藏"))
chk("B12 任务环境把 GUIZANG_OUTPUT 指到模块目录",
    ui_server.task_env("weread")["GUIZANG_OUTPUT"] == os.path.join(BOOKS, "微信读书"))
env = ui_server.task_env("video", {"GUIZANG_VIDEO_OPTS": "{}"})
chk("B13 任务环境不吞调用方自己的变量",
    env["GUIZANG_OUTPUT"] == os.path.join(BOOKS, "视频") and "GUIZANG_VIDEO_OPTS" in env)

# 合并稿：清稿只清这一本的，别人的不动
io.open(os.path.join(BOOKS, "微信读书", "clip_post.md"), "w", encoding="utf-8").write("别人的合并稿\n")
r = ui_server.reset_book_output("36000000000000000b000c01")
chk("B14 清残稿把根上那本同名合并稿也清掉了",
    r.get("ok") and not os.path.isfile(os.path.join(BOOKS, "36000000000000000b000c01.md")))
chk("B15 清稿不碰笔记、不碰别人的合并稿",
    os.path.isfile(os.path.join(BOOKS, "微信读书", "clip_post.md")) and
    os.path.isfile(os.path.join(BOOKS, "微信读书", "36000000000000000b000c01", "meta.json")))

# ── C：账本按模块分格 ─────────────────────────────────────────────
LIB = ui_server.LIB_PATH
legacy = {"folders": [{"id": "f1", "name": "随笔"}, {"id": "f2", "name": "还没放东西"}],
          "assign": {"clip_post": "f1", "36000000000000000b000c01": "f1"},
          "order": ["clip_post", "imp_mine"],
          "state": {"clip_post": "在读", "36000000000000000b000c01": "读完"},
          "tags": {"clip_post": ["公众号"]}}
_write_json(LIB, legacy)
lib = ui_server.load_lib()
chk("C1 老账本按书号折进各自那格",
    ui_server.lib_scope(lib, "clip")["assign"] == {"clip_post": "f1"} and
    ui_server.lib_scope(lib, "weread")["assign"] == {"36000000000000000b000c01": "f1"})
chk("C2 排序与状态也跟着书走",
    ui_server.lib_scope(lib, "clip")["order"] == ["clip_post"] and
    ui_server.lib_scope(lib, "local")["order"] == ["imp_mine"] and
    ui_server.lib_scope(lib, "weread")["state"]["36000000000000000b000c01"] == "读完")
chk("C3 被引用的夹子抄给引用方，没引用的留在微信读书那格",
    any(f["id"] == "f1" for f in ui_server.lib_scope(lib, "clip")["folders"]) and
    any(f["id"] == "f2" for f in ui_server.lib_scope(lib, "weread")["folders"]) and
    not any(f["id"] == "f2" for f in ui_server.lib_scope(lib, "video")["folders"]))
chk("C4 老账本的标签留住了", ui_server.lib_scope(lib, "clip")["tags"] == {"clip_post": ["公众号"]})
chk("C5 顶层不再留副本（一份数据只有一个地方能改）",
    not any(k in lib for k in ui_server.LIB_KEYS), list(lib))
ui_server.save_lib(lib)
again = ui_server.load_lib()
chk("C6 存回去再读一遍一模一样",
    again["modules"] == lib["modules"] and "modules" in again)

# 各管各的：剪藏那一格建夹、归入、贴标签，不许漏到微信读书那一格
lib = ui_server.prune_lib(again)
cs, ws = ui_server.lib_scope(lib, "clip"), ui_server.lib_scope(lib, "weread")
cs["folders"].append({"id": "f9", "name": "行业观察"})
cs["assign"]["clip_post"] = "f9"
cs["tags"]["clip_post"] = ["行业"]
ui_server.save_lib(lib)
lib2 = ui_server.load_lib()
chk("C7 剪藏的文件夹不出现在微信读书",
    [f["name"] for f in ui_server.lib_scope(lib2, "clip")["folders"]] == ["随笔", "行业观察"] and
    [f["name"] for f in ws["folders"]] == ["随笔", "还没放东西"])
chk("C8 剪藏的标签不出现在别的模块",
    ui_server.lib_scope(lib2, "clip")["tags"] == {"clip_post": ["行业"]} and
    not ui_server.lib_scope(lib2, "weread")["tags"])
row = {b["id"]: b for b in ui_server.list_books()}
chk("C9 清单里每本书读到自己那格的夹子与标签",
    row["clip_post"]["folder"] == "f9" and row["clip_post"]["tags"] == ["行业"] and
    row["36000000000000000b000c01"]["folder"] == "f1")

# 删一本书只抹它自己那格的记录
lib3 = ui_server.lib_forget(ui_server.load_lib(), "clip_post", "clip")
ui_server.save_lib(lib3)
lib3 = ui_server.load_lib()
chk("C10 删书后那格干净了，别格没动",
    "clip_post" not in ui_server.lib_scope(lib3, "clip")["assign"] and
    "clip_post" not in (ui_server.lib_scope(lib3, "clip")["tags"]) and
    ui_server.lib_scope(lib3, "weread")["assign"] == {"36000000000000000b000c01": "f1"})

# 账本里的幽灵（书没了）要被裁掉，空文件夹要留着
ghost = ui_server.load_lib()
gs = ui_server.lib_scope(ghost, "clip")
gs["assign"]["clip_没有这本书"] = "f9"
gs["order"].append("clip_没有这本书")
ui_server.save_lib(ghost)
gone = ui_server.prune_lib(ui_server.load_lib())
gp = ui_server.lib_scope(gone, "clip")
chk("C11 没了的书从账本里裁掉",
    "clip_没有这本书" not in gp["assign"] and "clip_没有这本书" not in gp["order"])
chk("C12 空文件夹不被裁（刚建好的不该闪没）",
    any(f["id"] == "f9" for f in gp["folders"]))

# 坏账本（缺格、形状不对）要能读、能补齐
_write_json(LIB, {"modules": {"clip": {"folders": "不是列表"}}})
bad = ui_server.load_lib()
chk("C13 缺格与坏形状都补成能用的",
    set(bad["modules"]) == set(book_layout.ORDER) and
    ui_server.lib_scope(bad, "clip")["folders"] == [] and
    ui_server.lib_scope(bad, "video")["tags"] == {})
chk("C14 不认识的模块名退回微信读书那格",
    ui_server.lib_scope(bad, "????" ) is ui_server.lib_scope(bad, "weread"))

shutil.rmtree(ROOT, ignore_errors=True)
print()
print("书库目录结构：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  ✗ " + f)
    sys.exit(1)
print("ok  书库按模块分格与老书归置全部验通过")
