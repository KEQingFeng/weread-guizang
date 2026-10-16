#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""架构地图 §2 那张表的「行」当场重数：docs/架构与模块地图.md 不许漂。

为什么单开这一条
    §2「运行期模块」那张表第三列「行」是接手的人第一眼判断「这个模块多大、值不值得
    单开一条门禁」的依据，可它全靠人手抄。《交接说明》§2 自己记着：第十七轮拿一把
    `wc -l` 扫全表 33 行，**当场对不上 8 行**（飘得最多的是 `ui_server.py`，表里
    6243、实际 6796），并写着「这一列仍然没有门禁钉着，改一个文件就会飘，要么以后
    每改必顺手更新那一行，要么干脆去掉」。开发规范 §8 又有那条硬规矩：会随版本漂的
    事实必须与代码同步。于是这一条把「每改必更新」交给机器来管。

口径（与《交接说明》§2 钉的同一把）
    `wc -l`：数**换行符**，不是按行读。`book_import.py` / `video_note.py` 这类结尾
    没有换行的文件，用 `splitlines` 会各多出一行 —— 那不是漂移，是量错了。所以这里
    读字节、数 `\\n`，与 `wc -l` 逐字等价，也不依赖平台（Windows 上没有 `wc`）。

这一条钉住
  · 表里每一行的「行」与磁盘上那份的真实换行数一致（漂了直接报出该改成几）；
  · 表本身没退化成空表：至少 30 行、含 `ui_server.py` 与 `ui.html`、同一个文件不许
    出现两次；
  · 对照：故意写错一个数能被同一把尺子逮住、写对不误报、点了不存在的文件也报；
  · 口径对照：结尾没有换行的临时文件按换行数算（2），按 `splitlines` 是 3。

纯文件走查，不起服务、不联网、不开浏览器，归 --fast。
"""
import atexit
import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC = os.path.join(ROOT, "docs", "架构与模块地图.md")

# §2 表里的模块都在仓库根：`| `名字` | 数字 |`。mcp/guizang-mcp.mjs 那张表第二列不是
# 数字，天然匹配不到；带 `/` 的名字也在这里挡掉，免得别的表将来长出一列数字被误算进来。
ROW_RE = re.compile(r"^\|\s*`([^`/]+)`\s*\|\s*(\d+)\s*\|")

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "   " + repr(extra)[:300]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


def parse_rows(text):
    """从文档里挑出 §2 表的 (文件, 写的行数)。"""
    out = []
    for line in text.splitlines():
        m = ROW_RE.match(line)
        if m:
            out.append((m.group(1), int(m.group(2))))
    return out


def count_lines(path):
    """与 `wc -l` 同一把尺：数换行符，不按 splitlines 数行。"""
    with open(path, "rb") as f:
        return f.read().count(b"\n")


def mismatches(rows, count):
    """哪些行的「行」数与磁盘对不上。

    rows 是 [(文件, 写的行数)]；count(文件) 回真实换行数，认不出来（文件不在）回 None。
    返回 [(文件, 写的, 实际的)]，实际为 None 表示那份文件根本不在仓库根。
    """
    out = []
    for name, declared in rows:
        real = count(name)
        if real is None or real != declared:
            out.append((name, declared, real))
    return out


def count_in_tree(name):
    p = os.path.join(ROOT, name)
    if not os.path.isfile(p):
        return None
    return count_lines(p)


# ── A. 真文档：解析没退化 + 每个数都对得上 ─────────────────────────
with open(DOC, encoding="utf-8") as f:
    rows = parse_rows(f.read())

names = [n for n, _ in rows]
chk("§2 表解析没退化成空表（≥30 行，且认得出 ui_server.py 与 ui.html）",
    len(rows) >= 30 and "ui_server.py" in names and "ui.html" in names,
    (len(rows), names[:6]))
dups = sorted({n for n in names if names.count(n) > 1})
chk("同一个文件在表里不出现两次（重复行会让「行」有两个答案）", not dups, dups)

bad = mismatches(rows, count_in_tree)
if bad:
    for name, declared, real in bad:
        if real is None:
            print("       表里点了 `%s`，仓库根上没有这份文件" % name)
        else:
            print("       把 §2 表里 `%s` 那一格的 %d 改成 %d" % (name, declared, real))
chk("§2 表里每个「行」都与磁盘上的真实换行数一致（wc -l 口径）",
    not bad, bad)

# ── B. 对照：尺子自己会红、也会安静 ────────────────────────────────
# 用同一把 parse + count 走一遍合成的小表：证明上面那些「绿」不是空转。
fake_sizes = {"ui_server.py": 6878, "feed.py": 2474}


def fake_count(name):
    """合成表用的假尺寸：只认上面两条，别的当「文件不在」。"""
    return fake_sizes.get(name)

_synth = "| 文件 | 行 | 职责 | 关键入口 |\n|---|---|---|---|\n" \
         "| `ui_server.py` | 6878 | x | y |\n| `feed.py` | 2474 | x | y |\n"
_synth_rows = parse_rows(_synth)
chk("对照：合成表被同一把尺子解析出来（2 行）", len(_synth_rows) == 2, _synth_rows)
chk("对照：写对的数不误报", mismatches(_synth_rows, fake_count) == [],
    mismatches(_synth_rows, fake_count))

_wrong = _synth.replace("| `feed.py` | 2474 |", "| `feed.py` | 9999 |")
chk("对照：故意写错一个数会被逮住（且指出该改成几）",
    mismatches(parse_rows(_wrong), fake_count) == [("feed.py", 9999, 2474)],
    mismatches(parse_rows(_wrong), fake_count))
chk("对照：点了仓库里没有的文件也报（实际值 None）",
    mismatches([("ghost_module.py", 3)], fake_count) == [("ghost_module.py", 3, None)],
    mismatches([("ghost_module.py", 3)], fake_count))

# 口径对照：`wc -l` 数的是换行符。结尾没有换行的那一份是 2 行（不是 splitlines 的 3）。
_tmp_dir = tempfile.mkdtemp(prefix="gz-archtable-")
atexit.register(lambda: shutil.rmtree(_tmp_dir, ignore_errors=True))
_tmp = os.path.join(_tmp_dir, "noeol.py")
with open(_tmp, "wb") as f:
    f.write(b"a\nb\nc")          # 两个换行、三个 splitlines 条目
chk("口径：结尾没有换行的文件按换行数算（wc -l = 2，splitlines = 3 是量错了）",
    count_lines(_tmp) == 2 and len(b"a\nb\nc".decode().splitlines()) == 3,
    (count_lines(_tmp), len(b"a\nb\nc".decode().splitlines())))

print()
if FAIL:
    print("架构地图行数门禁：失败 %d 项" % len(FAIL))
    for n in FAIL:
        print("  ✗ " + n)
    sys.exit(1)
print("架构地图行数门禁：通过 %d 项，失败 0 项" % PASSED)
