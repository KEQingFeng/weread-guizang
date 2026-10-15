#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归藏门禁：被强杀留下的合并稿临时件 `<书名>.md.tmp` 要跟着书一起清掉（ui_server）。

为什么要单开这一条
    `#153` 起整本合并稿走 `export_precise._atomic_write_text`：先把整份拼进内存、
    写成 `<书名>.md.tmp`，再一次 `os.replace` 改名成 `<书名>.md`。改名是最后一步，
    恰好在它之前被强杀（停止键 / 关窗口 / 断电），盘上留下的就只有一个
    `<书名>.md.tmp`。

    它掉在清理射程的缝里：
      · 不以 `.md` 结尾 —— `drop_stray_md` 原先只按 `<名>.md` 拼路径，认不出它；
      · 不在书目录里 —— `RESIDUE_DIRS`（chapters / raw / images）与 `RESIDUE_FILES`
        （三本账本与它们的固定 `.tmp`）都指不到模块文件夹 / 书库根那一层；
    于是界面上看不见、也点不掉，只能一直占着盘。收法就是在同一处（`drop_stray_md`）
    把 `<名>.md.tmp` 一并收走 —— 清残稿、删一本、批量删三条路都从那儿过。

这一条钉住
  · `drop_stray_md` 把模块文件夹与书库根两处的 `<书名>.md.tmp`、`<书号>.md.tmp`
    连同 `<名>.md` 一起清掉；
  · 别人的合并稿 / 别人的残件一个字都不动；
  · 只删普通文件：同名目录（或任何非普通文件）不碰，里面的东西也不动；
  · 清理路径仍然限定在书库内（书名带 `../` 的那种指不到库外）；
  · 清残稿 `reset_book_output` 走同一条路，顺带把书目录里的账本临时件收掉、
    不动用户自己的笔记；
  · 对照：改之前那份只认 `<名>.md` 的老写法确实会把 `.md.tmp` 留下 —— 门禁不空转。

沙盒在系统临时目录里，不碰用户真实书库、不联网、不开浏览器，故归 --fast 静态段。
"""
import io
import json
import os
import re
import shutil
import sys
import tempfile

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

SB = tempfile.mkdtemp(prefix="guizang-stray-")
os.environ["GUIZANG_DATA"] = os.path.join(SB, "cache")
os.environ["GUIZANG_BOOKS"] = os.path.join(SB, "books")
os.makedirs(os.environ["GUIZANG_DATA"], exist_ok=True)
os.makedirs(os.environ["GUIZANG_BOOKS"], exist_ok=True)

import ui_server  # noqa: E402

FAIL = []
N = 0


def chk(name, cond, extra=""):
    global N
    N += 1
    print(("  ok  " if cond else " FAIL ") + name + ("" if cond else "  <- " + repr(extra)))
    if not cond:
        FAIL.append(name)


def write(path, text="内容"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def write_json(path, obj):
    return write(path, json.dumps(obj, ensure_ascii=False))


def read_json(path):
    with io.open(path, encoding="utf-8") as f:
        return json.load(f)


BOOKS = ui_server.OUT_DIR
MOD = "微信读书"


def seed_book(bid, title):
    """造一本「像一本书」的目录（meta.json 里带书名）。"""
    d = os.path.join(BOOKS, MOD, bid)
    os.makedirs(d, exist_ok=True)
    write_json(os.path.join(d, "meta.json"),
               {"title": title, "author": "作者甲", "source": "weread", "done": True})
    return d


def old_drop(book_id, title=""):
    """改之前那份 `drop_stray_md` 的形状：候选表里只有 `<名>.md`。"""
    root = os.path.realpath(ui_server.OUT_DIR)
    places = [os.path.join(root, ui_server.book_layout.DIR_OF[ui_server.book_module(book_id)]), root]
    names = {str(title or ""), str(book_id or "")}
    gone = 0
    for place in places:
        for nm in names:
            if not nm:
                continue
            p = os.path.join(place, re.sub(r'[<>:"/\\|?*]', "_", nm) + ".md")
            real = os.path.realpath(p)
            if not (real.startswith(root + os.sep) and os.path.isfile(real)):
                continue
            try:
                os.remove(real)
                gone += 1
            except OSError:
                pass
    return gone


# ── 1. 老写法确实会把 `.md.tmp` 留下（对照，门禁不空转） ────────────
BID2, TITLE2 = "36000000000000000b000c02", "老写法留下的合并稿"
seed_book(BID2, TITLE2)
r_tmp = write(os.path.join(BOOKS, MOD, TITLE2 + ".md.tmp"))
r_root = write(os.path.join(BOOKS, TITLE2 + ".md.tmp"))
write(os.path.join(BOOKS, MOD, TITLE2 + ".md"))
old_gone = old_drop(BID2, TITLE2)
chk("对照：老写法清掉了合并稿本体（这一趟没白跑）", old_gone == 1, old_gone)
chk("对照：老写法把模块文件夹里的 `<书名>.md.tmp` 留下了", os.path.isfile(r_tmp))
chk("对照：老写法把书库根上的 `<书名>.md.tmp` 也留下了", os.path.isfile(r_root))

# 同一份夹具改走现在这条路：两个残件都要被收走
now_gone = ui_server.drop_stray_md(BID2, TITLE2)
chk("现在这版把两个 `<书名>.md.tmp` 一起收走", now_gone == 2 and
    not os.path.exists(r_tmp) and not os.path.exists(r_root), (now_gone, os.path.exists(r_tmp)))

# ── 2. 主路径：模块文件夹 + 书库根，两处 `.md` / `.md.tmp` 一起清 ──
BID, TITLE = "36000000000000000b000c01", "归藏全书"
seed_book(BID, TITLE)
own = [
    write(os.path.join(BOOKS, MOD, TITLE + ".md")),
    write(os.path.join(BOOKS, MOD, TITLE + ".md.tmp")),
    write(os.path.join(BOOKS, MOD, BID + ".md.tmp")),      # 早期按书号命名的那一份残件
    write(os.path.join(BOOKS, TITLE + ".md")),
    write(os.path.join(BOOKS, TITLE + ".md.tmp")),
]
others = [
    write(os.path.join(BOOKS, "别人的书.md")),
    write(os.path.join(BOOKS, "别人的书.md.tmp")),
    write(os.path.join(BOOKS, MOD, "别人的书.md")),
    write(os.path.join(BOOKS, MOD, "别人的书.md.tmp")),
]
gone = ui_server.drop_stray_md(BID, TITLE)
chk("这一本的 5 份产物（两处 .md / 两处书名 .md.tmp / 一处书号 .md.tmp）全清掉",
    gone == 5 and not any(os.path.exists(p) for p in own), (gone, [os.path.exists(p) for p in own]))
chk("别人的合并稿与残件一个字都不动", all(os.path.isfile(p) for p in others),
    [os.path.exists(p) for p in others])

# 清第二遍是幂等的：不抛、也不把「已经清干净」报成又删了东西
chk("清第二遍回 0（幂等）", ui_server.drop_stray_md(BID, TITLE) == 0)

# ── 3. 只删普通文件：同名目录不碰 ─────────────────────────────────
BID3, TITLE3 = "36000000000000000b000c03", "目录残件"
seed_book(BID3, TITLE3)
d_mod = os.path.join(BOOKS, MOD, TITLE3 + ".md.tmp")
d_root = os.path.join(BOOKS, TITLE3 + ".md.tmp")
for d in (d_mod, d_root):
    os.makedirs(d, exist_ok=True)
    write(os.path.join(d, "inner.txt"), "这是用户自己放的东西\n")
chk("同名目录被当成「不是清理对象」跳过", ui_server.drop_stray_md(BID3, TITLE3) == 0)
chk("目录与里面的东西都还在",
    os.path.isdir(d_mod) and os.path.isdir(d_root)
    and os.path.isfile(os.path.join(d_mod, "inner.txt"))
    and os.path.isfile(os.path.join(d_root, "inner.txt")))

# ── 4. 书名带 `../` 指不到库外 ────────────────────────────────────
BID5 = "36000000000000000b000c05"
seed_book(BID5, "逃逸试验")
outside = write(os.path.join(os.path.dirname(BOOKS), "escape.md.tmp"), "库外的文件\n")
chk("书名里的 `../` 被洗掉、库外文件不动",
    ui_server.drop_stray_md(BID5, "../escape") == 0 and os.path.isfile(outside))

# ── 5. 清残稿走同一条路：残件收掉、笔记留着 ──────────────────────
BID4, TITLE4 = "36000000000000000b000c04", "清稿试验"
d4 = seed_book(BID4, TITLE4)
write(os.path.join(d4, "chapters", "0001.md"), "# 第一章\n\n正文。\n")
write(os.path.join(d4, "_catalog.json.tmp"), "{半截\n")
write_json(os.path.join(d4, "notes.json"), {"marks": [{"text": "用户自己的划线"}]})
write(os.path.join(d4, "notes.md.tmp"), "用户自己的笔记半截件\n")
res = ui_server.reset_book_output(BID4)
meta = read_json(os.path.join(d4, "meta.json"))
chk("清残稿有应答、书名作者留着", res.get("ok") is True
    and meta.get("title") == TITLE4 and meta.get("author") == "作者甲", (res, meta))
chk("done 打回假（这本书重新变成「没取完」）", meta.get("done") is False, meta)
chk("模块文件夹与书库根上的 `<书名>.md.tmp` 都被收走",
    not os.path.exists(os.path.join(BOOKS, MOD, TITLE4 + ".md.tmp"))
    and not os.path.exists(os.path.join(BOOKS, TITLE4 + ".md.tmp")))
chk("书目录里的 chapters 与账本临时件照旧清掉",
    not os.path.exists(os.path.join(d4, "chapters"))
    and not os.path.exists(os.path.join(d4, "_catalog.json.tmp")))
chk("用户自己的 notes.json 不受牵连（还在、内容没动）",
    os.path.isfile(os.path.join(d4, "notes.json"))
    and read_json(os.path.join(d4, "notes.json"))["marks"][0]["text"] == "用户自己的划线")

# ── 6. 源码：`.md` 与 `.md.tmp` 是同一处收口（不是另加一条旁路） ──
SRC = io.open(ui_server.__file__, encoding="utf-8").read()
seg = SRC[SRC.find("def drop_stray_md"):SRC.find("def reset_book_output")]
chk("源码：候选表里同时有 `<名>.md` 与 `<名>.md.tmp`",
    'p + ".tmp"' in seg and ".md" in seg, seg[:160])

shutil.rmtree(SB, ignore_errors=True)
print()
print("合并稿残件清理门禁：%d 项通过，失败 %d 项" % (N - len(FAIL), len(FAIL)))
if FAIL:
    print("未过：" + " / ".join(FAIL))
sys.exit(1 if FAIL else 0)
