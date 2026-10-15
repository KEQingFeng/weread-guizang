#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""写作那一格的坏字节（writer.py）：稿子另存成 GBK / 断电写一半，不许让整篇打不开。

为什么单开这一条
    draft.md 与 versions/*.md 是用户能直接编辑的正文，也可能被云同步 / 旧版本 /
    别的编辑器写回来。`writer.read_text()`（read / brief / note / save / 书架卡片
    共用的唯一读口）与 `writer._read_file()`（快照列表 / 回退）原来都是裸
    `open(..., encoding="utf-8")`，只兜 `OSError` —— 一个手滑另存成 GBK 的稿子、
    或一次写到一半的断电，严格 UTF-8 读法当场 UnicodeDecodeError，而它既不是
    OSError、也没人接：

        writer.read_text(d)     -> UnicodeDecodeError   （整篇打不开）
        writer.read(root, bid)  -> UnicodeDecodeError
        writer.save(..., text)  -> UnicodeDecodeError   （存盘被顶回，像「保存没反应」）
        writer.versions(d)      -> UnicodeDecodeError   （快照一列都列不出来）
        writer.version_text(..) -> UnicodeDecodeError

    章节正文早就按「认不出的字节换 U+FFFD、正文其余照旧，读不到才当没有」的口径
    收在 `book_export.read_chapter_file` 一处（书库第七格的稿子本来也是书库里的一本
    书），写作这一格一直没接过去。这一条把两个读口钉在那把共用的尺子上。

这一条钉住
  · 对照：严格 UTF-8 读同一个 GBK 文件确实抛 UnicodeDecodeError（证明这道门不是空转）；
  · read_text / read / note / save / versions / version_text 六个入口在坏字节上都不抛；
  · 坏掉的那几个字节只丢它自己，换成一个 U+FFFD，正文其余部分照旧；
  · 干净 UTF-8 一个字都不动（不含替代符）；
  · 读不到（文件不在 / 目录）照旧回空串，调用方那条「这篇稿子不在了」的话术不变；
  · 接线：两个读口都走 `book_export.read_chapter_file`，不许再各写一把裸 open。

只碰 writer.py 的纯函数与系统临时目录里的沙盒书库，不起服务、不联网、不碰真实书库，
故归 --fast 静态段。
"""
import os
import pathlib
import shutil
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# 数据目录指进临时沙盒再 import writer：免得书库那几条路真去动用户真实的书库。
SANDBOX = pathlib.Path(tempfile.mkdtemp(prefix="guizang-writer-badbytes-"))
TMP = SANDBOX / "work"
os.environ["GUIZANG_DATA"] = str(SANDBOX / "cache")
os.environ["GUIZANG_BOOKS"] = str(SANDBOX / "books")

import book_export  # noqa: E402
import writer  # noqa: E402

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    if cond:
        PASSED += 1
        print("PASS  " + name)
    else:
        FAIL.append(name)
        print("FAIL  " + name + ("" if not extra else "   <- " + repr(extra)[:220]))


def no_raise(fn, *a, **kw):
    """跑一次，回 (有没有抛, 结果或异常描述)。门禁就是要抓所有抛。"""
    try:
        return False, fn(*a, **kw)
    except Exception as e:                      # noqa: BLE001
        return True, "%s: %s" % (type(e).__name__, str(e)[:140])


LIB = str(TMP / "library")
os.makedirs(LIB, exist_ok=True)

# 「坏字节」两副面孔，都是真实会发生的：一个中文段落被另存成 GBK；一次写到一半断电，
# 只留下半个 UTF-8 多字节序列。
GBK_TAIL = "这一段是后来另存的".encode("gbk")
GBK_BYTES = (b"# title\n" + GBK_TAIL)
HALF_UTF8 = "你好世界".encode("utf-8")[:5]       # 一截写了一半的多字节序列


def make_draft(title, text):
    bid, _meta = writer.create(LIB, title=title, text=text)
    return bid, writer.dir_of(LIB, bid)


def poison(path, raw):
    with open(path, "wb") as f:
        f.write(raw)


# ── 1. 对照：严格 UTF-8 读同一个 GBK 文件确实抛（洞是真的） ──────────
bid, d = make_draft("另存成 GBK", "正常一段")
poison(writer.draft_path(d), GBK_BYTES)
with open(writer.draft_path(d), encoding="utf-8") as f:
    try:
        f.read()
        strict_raised = False
    except UnicodeDecodeError:
        strict_raised = True
chk("对照：裸 utf-8 读 GBK 正文抛 UnicodeDecodeError（不是理论上的洞）", strict_raised)

# ── 2. 六个入口在坏字节上都不抛 ─────────────────────────────────────
threw, text = no_raise(writer.read_text, d)
chk("read_text：坏字节不抛", not threw, text)
chk("read_text：坏字节换成了 U+FFFD", isinstance(text, str) and "\ufffd" in text, text)

threw, full = no_raise(writer.read, LIB, bid)
chk("read：整篇读得出（不是被一个坏字节挡住）",
    not threw and isinstance(full, dict) and full.get("title") == "另存成 GBK", full)

threw, nt = no_raise(writer.note, LIB, bid)
chk("note：书架卡片那句备注照常拿得到", not threw and isinstance(nt, dict), nt)

threw, saved = no_raise(writer.save, LIB, bid, "改过的新一版", rev=None)
chk("save：坏字节的旧稿也能存回新版（不再「保存没反应」）",
    not threw and saved[0] is True, saved)
chk("save：写下去的就是刚存的新正文",
    writer.read_text(d) == "改过的新一版", writer.read_text(d))

# ── 3. 快照列表 / 回退：一份坏快照不许把整列带走 ────────────────────
writer.snapshot_now(d, "这是一份好快照")
vd = writer.versions_dir(d)
poison(os.path.join(vd, "20200101-000000.md"), HALF_UTF8)     # 半截多字节序列
threw, vs = no_raise(writer.versions, d)
chk("versions：坏快照不抛、好快照照列", not threw and len(vs) >= 2, vs)
threw, vt = no_raise(writer.version_text, d, "20200101-000000.md")
chk("version_text：坏快照读得出（替代符顶掉半个序列）",
    not threw and isinstance(vt, str) and "\ufffd" in vt, vt)

# ── 4. 干净 UTF-8 一个字不动，且不含替代符 ──────────────────────────
clean_text = "你好世界 ABC\n第二行 with emoji \U0001f600"
bid2, d2 = make_draft("干净稿子", clean_text)
got = writer.read_text(d2)
chk("干净正文：一字不差、无替代符", got == clean_text and "\ufffd" not in got, got)

# ── 5. 读不到照旧回空串（调用方的话术不变） ─────────────────────────
gone = writer.dir_of(LIB, "write_deadbeef0000")
chk("文件不在：read_text 回空串", writer.read_text(gone) == "")
chk("_read_file 读目录也不抛、回空串", writer._read_file(str(TMP)) == "")

# ── 6. 接线：两个读口都走共用那把尺子，不再各写裸 open ───────────────
src = (REPO / "writer.py").read_text(encoding="utf-8")
chk("接线：writer 导入了 book_export",
    "import book_export" in src)
chk("接线：read_text / _read_file 走同一个容错读口（至少两处调用）",
    src.count("book_export.read_chapter_file(") >= 2,
    src.count("book_export.read_chapter_file("))
probe = TMP / "probe.md"
poison(str(probe), GBK_BYTES)
chk("对照：共用那把尺子本身认坏字节、正常解干净",
    book_export.read_chapter_file(str(probe)) is not None
    and "\ufffd" in book_export.read_chapter_file(str(probe)))

shutil.rmtree(SANDBOX, ignore_errors=True)
print()
print("写作坏字节门禁：通过 %d 项，失败 %d 项" % (PASSED, len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  x " + f)
    sys.exit(1)
print("ok  稿子另存成 GBK / 写一半断电，读得了、存得回、快照列得出")
