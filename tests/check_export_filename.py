#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归藏门禁：书名拼进文件名的那把尺子 —— 长书名 / 保留名不再在最后一秒把整本书导出打回。

为什么单开这一条
    `export_precise` 收尾把各章拼成一份 `<书名>.md`，文件名是把书名按一条内联正则
    （`re.sub(r'[<>:"/\\\\|?*]', '_', title)`）洗出来的。那条尺子只管「Windows 非法字符」，
    漏了三样，每一样都能让「整本书都取完了」的最后一秒失败：

      · **长度**。目录项上限 255 字节，中文一个字三字节。`"很长的书名" * 20` 是 100 个
        字符、300 字节，`open()` 直接 `OSError: [Errno 36] File name too long` —— 所有章
        都在盘上，用户看到的却是「导出失败」，重跑还会在同一个地方再失败一次。
      · **控制字符 / 结尾的点与空格**。换行进文件名会让列表与日志错行；Windows 会静默
        吃掉结尾的点与空格，写下去的和读回来的对不上名。
      · **Windows 保留设备名**。`CON` / `NUL` / `COM1` …（带不带扩展名都算）在 Windows 上
        create / rename 必失败。

    而且这条内联正则在 `ui_server.drop_stray_md` 里**又抄了一份**：写口与清理刀各写各的，
    长书名 / 保留名那一族就会「写得出、清不掉」。所以这一条同时钉住共用尺子
    （`platform_compat.safe_file_stem`）、写口接线、以及清理刀与写口同尺。

这一条钉住
  A. 纯函数尺子：按 UTF-8 字节截断且不切半个字；非法 / 控制字符换 `_` 且 `/` 不劈路径；
     Windows 保留设备名加 `_`（正常词不误伤）；结尾点空格吃掉；正常标题一字不改；
     落单半个代理码点摘掉；幂等。
  B. 端到端：`merge_chapters` 用这把尺子真能把长书名落成盘；连临时名尾巴一起仍在 255
     字节内；`drop_stray_md` 按同一把尺子把两处合并稿与随机名残件都收走、别家不动。
  C. 对照：只按旧内联正则（不截断）的老尺子给长书名产出 300 字节 > 255，说明这组不是空转。

沙盒在系统临时目录里，不碰用户真实书库 / cache，不联网、不开浏览器，归 --fast 静态段。
"""
import json
import os
import re
import shutil
import sys
import tempfile

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

SB = tempfile.mkdtemp(prefix="guizang-export-filename-")
os.environ["GUIZANG_DATA"] = os.path.join(SB, "cache")
os.environ["GUIZANG_BOOKS"] = os.path.join(SB, "books")
os.makedirs(os.environ["GUIZANG_DATA"], exist_ok=True)
os.makedirs(os.environ["GUIZANG_BOOKS"], exist_ok=True)

import platform_compat as pc   # noqa: E402
import export_precise as E     # noqa: E402
import ui_server as US         # noqa: E402

FAIL = []


def chk(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:220]))
    if not cond:
        FAIL.append(name)


def blen(s):
    return len(s.encode("utf-8"))


LONG = "很长的书名" * 20          # 100 字 / 300 字节
OLD = re.compile(r'[<>:"/\\|?*]')  # 旧那把只换非法字符的尺子（对照组）

# ── A. 纯函数：书名的字节 / 字符 / 保留名 ───────────────────────────────

long_stem = pc.safe_file_stem(LONG, "bid")
chk("A1 长中文书名按 UTF-8 字节收口：不超上限、且是完整字符（能再编回去）",
    blen(long_stem) <= 180 and blen(long_stem) > 100
    and long_stem.encode("utf-8").decode("utf-8") == long_stem,
    (blen(long_stem), long_stem[:12]))
chk("A1b 截断后不残半个字：末端不是落单代理码点 / 替换符",
    not re.search(r"[\ud800-\udfff\ufffd]", long_stem), repr(long_stem[-6:]))

dirty_stem = pc.safe_file_stem("a/b:c*d?e|f<g>h", "bid")
chk("A2 非法字符一律换 `_`，`/` 不把名字劈成两层路径",
    dirty_stem == "a_b_c_d_e_f_g_h" and os.sep not in dirty_stem, dirty_stem)

ctl_stem = pc.safe_file_stem("tab\tand\nnewline\x00\x7f", "bid")
chk("A2b C0 控制区 / DEL / 换行换成 `_`，不把名字劈行、也不无声拼接",
    ctl_stem == "tab_and_newline__" and "\n" not in ctl_stem and "\t" not in ctl_stem,
    repr(ctl_stem))

reserved = {n: pc.safe_file_stem(n, "bid") for n in
            ("CON", "con", "nul", "AUX", "com1", "LPT9", "con.md", "NUL.txt")}
chk("A3 Windows 保留设备名（含带扩展名、大小写）前面补 `_`，不再拿去做文件名",
    all(v.startswith("_") for v in reserved.values()), reserved)
normal_words = {n: pc.safe_file_stem(n, "bid") for n in
                ("console", "connect", "com10", "lpt", "contract", "目录")}
chk("A3b 含保留名做前缀的正常书名不误伤（console / connect / com10 …）",
    all(k == v for k, v in normal_words.items()), normal_words)

chk("A4 结尾的点与空格被吃掉（Windows 静默截掉，写读两名对不上）",
    pc.safe_file_stem("书名 . ", "bid") == "书名"
    and pc.safe_file_stem(" ... ", "bid") == "bid")
chk("A4b 名字中间的点照留（书名里的 `.` 不是结尾点）",
    pc.safe_file_stem("正常 书名.v2", "bid") == "正常 书名.v2")

chk("A5 正常中文 / 英文标题一个字不改（这是洗文件名，不是改书名）",
    pc.safe_file_stem("活着（余华）", "bid") == "活着（余华）"
    and pc.safe_file_stem("The Little Prince", "bid") == "The Little Prince")

chk("A6 落单的半个代理码点摘掉（JSON 存得下、utf-8 编不出）",
    pc.safe_file_stem("书\ud83d名", "bid") == "书名", repr(pc.safe_file_stem("书\ud83d名", "bid")))

once = pc.safe_file_stem(LONG + "nul / x", "bid")
chk("A7 幂等：洗过一遍的名字再洗一遍不变",
    pc.safe_file_stem(once, "bid") == once, once)

old_name = OLD.sub("_", LONG)     # 旧尺子的产物
chk("A8 对照：旧内联正则不截断，长书名的文件名 300 字节 > 目录项上限 255",
    blen(old_name) == 300 and blen(old_name) > 255 and blen(long_stem) <= 180,
    (blen(old_name), blen(long_stem)))

# 真盘上验一下「旧名字写不进去」：主流文件系统都限 255 字节；若这个盘不禁，只记字节对照。
probe_dir = os.path.join(SB, "probe")
os.makedirs(probe_dir, exist_ok=True)
fs_limits = False
try:
    with open(os.path.join(probe_dir, "x" * 300 + ".md"), "w") as f:
        f.write("x")
except OSError:
    fs_limits = True
if fs_limits:
    old_blocked = False
    try:
        with open(os.path.join(probe_dir, old_name + ".md"), "w") as f:
            f.write("x")
    except OSError:
        old_blocked = True
    new_ok = False
    try:
        with open(os.path.join(probe_dir, long_stem + ".md"), "w") as f:
            f.write("x")
        new_ok = True
    except OSError:
        new_ok = False
    chk("A8b 对照：旧名字在这个盘上确实创建失败（ENAMETOOLONG），新名字落得下",
        old_blocked and new_ok, (old_blocked, new_ok))
else:
    print("SKIP  A8b 这个文件系统不强制 255 字节目录项上限，改由 A8 的字节对照把守")

# ── B. 端到端：写口落得下，清理刀按同一把尺子认得它 ────────────────────

md_dir = os.path.join(SB, "chapters")
out_dir = os.path.join(SB, "out")
os.makedirs(md_dir, exist_ok=True)
os.makedirs(out_dir, exist_ok=True)
with open(os.path.join(md_dir, "0001.md"), "w", encoding="utf-8") as f:
    f.write("# 第一章\n\n正文在盘上。\n")

merged = E.merge_chapters(md_dir, out_dir, long_stem, LONG, "作者")
chk("B1 长书名下 merge_chapters 真能把合并稿落成盘（整本书取完不会白取）",
    os.path.isfile(merged) and blen(os.path.basename(merged)) <= 183,
    os.path.basename(merged))
chk("B2 连 `_tmp_name` 的 `.<pid>.<随机串>.tmp` 尾巴一起，仍在 255 字节目录项上限内",
    blen(os.path.basename(E._tmp_name(merged))) < 255,
    blen(os.path.basename(E._tmp_name(merged))))

# 清理刀：造一本长书名的书目录 + 两处 `<名>.md`，用同一个 drop_stray_md 收掉。
BOOKS = US.OUT_DIR
BID = "36000000000000000c000c10"
MOD = US.book_layout.DIR_OF[US.book_module(BID)]


def seed_book(bid, title):
    d = os.path.join(BOOKS, MOD, bid)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"title": title, "author": "作者甲", "source": "weread", "done": True}, f)


def write(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("x")
    return path


seed_book(BID, LONG)
base = pc.safe_file_stem(LONG, BID) + ".md"
own = [
    write(os.path.join(BOOKS, MOD, base)),
    write(os.path.join(BOOKS, base)),
    write(os.path.join(BOOKS, MOD, base + ".4242.deadbeef.tmp")),
    write(os.path.join(BOOKS, base + ".7.cafe0001.tmp")),
]
others = [
    write(os.path.join(BOOKS, "别人的书.md")),
    write(os.path.join(BOOKS, MOD, "别人的书.md.5.abcdef01.tmp")),
]
gone = US.drop_stray_md(BID, LONG)
chk("B3 清理刀按同一把尺子算出长书名的 `<名>.md`，两处合并稿 + 两份随机名残件全收走",
    gone == 4 and not any(os.path.exists(p) for p in own),
    (gone, [os.path.exists(p) for p in own]))
chk("B4 别人家的合并稿与残件一个字不动",
    all(os.path.isfile(p) for p in others), [os.path.exists(p) for p in others])
chk("B5 清第二遍回 0（幂等）", US.drop_stray_md(BID, LONG) == 0)

try:
    shutil.rmtree(SB, ignore_errors=True)
finally:
    print()
    print("导出文件名尺子体检：" + ("全部通过" if not FAIL else "%d 项未过：%s" % (len(FAIL), FAIL)))
    sys.exit(1 if FAIL else 0)
