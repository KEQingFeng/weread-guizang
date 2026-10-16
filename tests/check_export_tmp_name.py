#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""归藏门禁：取书这一格的两个写口换上「一次一个名字」的临时件，留下的残件仍有刀能收。

为什么要单开这一条
    `export_precise` 是「固定临时名」那一族最后两处之一（《交接说明》§2 那本账）：
    `_dump_json` 管三份账本 + meta + 进度快照，`_atomic_write_text` 管章节 `.md` 与
    整本合并稿。两个写口原先都写固定的 `<目标>.tmp`。

    固定名的后果分两种，都不体面：两个写手同时往同一个件里写时，后来者一打开就把
    先到的那半截截断，两边各按自己的偏移往下写，`os.replace` 顶成正式档的是一份
    内容交织的账（读口解不动就整份退回空档）；先收尾的那个把临时件整个搬走时，
    后一个改名当场 `FileNotFoundError` —— 内容其实已经写成，报出去的却是「没写进去」，
    旁边还留一个没人认领的残件。仓库里其它格（封面 / 画板 / 笔记 / 便签 / 视频 /
    导图 / 写作 / UI 账本）早就换成了 `pid + 随机串`，这两个口是那一族漏下的。

    还有一条预登记在 §2 的耦合：合并稿的临时件一旦换成随机名，`ui_server.drop_stray_md`
    原先只按固定名 `<书名>.md.tmp` 点，就再也够不到它 —— 被强杀留下的半截合并稿
    会变成「界面上看不见、也删不掉」的那一档。所以这一条同时钉住写口与那把清理刀。

这一条钉住
  A. 两个写口本体：正常写一个字不改；改名失败 / 中止信号（`BaseException`）时旧档
     完整、抛的还是它自己那种异常、旁边不留半个临时件；
  B. 临时件名一次一个：两次写不重名、并发打同一个目标时一个都不抛（固定名才会扑空），
     落下的永远是某一份完整内容；
  C. 接线：两个写口都从 `_tmp_name(path)` 取名字，源码里不再有 `tmp = path + ".tmp"`；
  D. 合并稿残件：`drop_stray_md` 把随机名 `<书名>.md.<pid>.<随机>.tmp`（模块文件夹与
     书库根两处）跟着这本书一起收走；别家的残件、符号链接、同名目录一概不碰；
  E. 对照：固定名 + 同时改名确实会扑空（`FileNotFoundError`），改之前那把只认固定名的
     清理刀确实认不出随机名残件 —— 门禁不空转。

沙盒在系统临时目录里，不碰用户真实书库、不联网、不开浏览器，故归 --fast 静态段。
"""
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)

SB = tempfile.mkdtemp(prefix="guizang-tmpname-")
os.environ["GUIZANG_DATA"] = os.path.join(SB, "cache")
os.environ["GUIZANG_BOOKS"] = os.path.join(SB, "books")
os.makedirs(os.environ["GUIZANG_DATA"], exist_ok=True)
os.makedirs(os.environ["GUIZANG_BOOKS"], exist_ok=True)

import export_precise as E  # noqa: E402
import ui_server as US      # noqa: E402

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


def read_text(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def read_json(path):
    return json.loads(read_text(path))


def body_of(src, name):
    """取一个顶层函数的源码段（到下一个 `\\ndef ` 为止）。"""
    i = src.find("def %s(" % name)
    if i < 0:
        return ""
    j = src.find("\ndef ", i + 1)
    return src[i:j if j > 0 else len(src)]


def tmp_left(d):
    try:
        return sorted(n for n in os.listdir(d) if n.endswith(".tmp"))
    except OSError:
        return []


W = os.path.join(SB, "w")
os.makedirs(W, exist_ok=True)
REAL_OS = E.os

# ── A. 两个写口本体 ────────────────────────────────────────────────

jpath = os.path.join(W, "a.json")
E._dump_json(jpath, {"t": "书名", "n": 1})
chk("A1 账本：正常写出来就是那份 JSON", read_json(jpath) == {"t": "书名", "n": 1}, read_text(jpath))
chk("A2 账本：正常路径不留临时件", tmp_left(W) == [], tmp_left(W))
E._dump_json(jpath, ["新"])
chk("A3 账本：覆盖后旧内容整份换成新的", read_json(jpath) == ["新"], read_text(jpath))
chk("A4 账本：覆盖也不留临时件", tmp_left(W) == [], tmp_left(W))


class _BoomJSON:
    @staticmethod
    def dump(*a, **k):
        a[1].write('{"v": "新')          # 半截写出去再炸：正是「写到一半被杀」的最小复现
        raise KeyboardInterrupt()


good = read_text(jpath)
real_json = E.json
try:
    E.json = _BoomJSON
    try:
        E._dump_json(jpath, {"v": "新的"})
        raised = False
    except KeyboardInterrupt:
        raised = True
finally:
    E.json = real_json
chk("A5 账本：写一半被中止，异常照抛（不许悄悄当成功）", raised)
chk("A6 账本：写一半被中止，旧档一个字没动", read_text(jpath) == good, read_text(jpath))
chk("A7 账本：写一半被中止，临时件收干净", tmp_left(W) == [], tmp_left(W))


class _BoomOS:
    """只有 replace 会炸的 os 替身：其余属性照旧走真 os。"""

    @staticmethod
    def replace(*a, **k):
        raise OSError("改名失败")

    def __getattr__(self, name):
        return getattr(os, name)


try:
    E.os = _BoomOS()
    try:
        E._dump_json(jpath, {"v": "新的"})
        raised = False
    except OSError:
        raised = True
finally:
    E.os = REAL_OS
chk("A8 账本：改名失败，异常照抛", raised)
chk("A9 账本：改名失败，旧档一个字没动", read_text(jpath) == good, read_text(jpath))
chk("A10 账本：改名失败，临时件收干净", tmp_left(W) == [], tmp_left(W))

tpath = os.path.join(W, "第二章.md")
E._atomic_write_text(tpath, "旧的那一章\n")
chk("A11 正文：正常写出来就是那份文本", read_text(tpath) == "旧的那一章\n", read_text(tpath))
chk("A12 正文：正常路径不留临时件", tmp_left(W) == [], tmp_left(W))
tgood = read_text(tpath)
try:
    E._atomic_write_text(tpath, "\ud800")          # 落单半个代理码点 → UnicodeEncodeError
    raised = False
except UnicodeEncodeError:
    raised = True
chk("A13 正文：写不进 utf-8 的坏码点照抛", raised)
chk("A14 正文：坏码点没有毁掉旧档", read_text(tpath) == tgood, read_text(tpath))
chk("A15 正文：坏码点也不留临时件", tmp_left(W) == [], tmp_left(W))


# ── B. 临时件名一次一个 ────────────────────────────────────────────

class _ReplaceSpy:
    def __init__(self, log):
        self.log = log

    def replace(self, src, dst):
        self.log.append((src, dst))
        return os.replace(src, dst)

    def __getattr__(self, name):
        return getattr(os, name)


log = []
try:
    E.os = _ReplaceSpy(log)
    E._dump_json(jpath, {"r": 1})
    E._dump_json(jpath, {"r": 2})
finally:
    E.os = REAL_OS
srcs = [s for s, _ in log]
chk("B1 账本：两次写各走一次改名", len(srcs) == 2, srcs)
chk("B2 账本：两次的临时件名不一样（固定名正是扑空的根）",
    len(set(srcs)) == 2, srcs)
chk("B3 账本：临时件名是 `pid + 随机串` 的随机名，不是固定的 `path + \".tmp\"`",
    all(s.startswith(jpath + ".") and s.endswith(".tmp") for s in srcs)
    and (jpath + ".tmp") not in srcs, srcs)

tlog = []
try:
    E.os = _ReplaceSpy(tlog)
    E._atomic_write_text(tpath, "第一章\n")
    E._atomic_write_text(tpath, "第二章\n")
finally:
    E.os = REAL_OS
tsrcs = [s for s, _ in tlog]
chk("B4 正文：两次的临时件名不一样",
    len(set(tsrcs)) == 2 and all(s.startswith(tpath + ".") for s in tsrcs), tsrcs)
chk("B5 正文：同样不是固定的 `path + \".tmp\"`", (tpath + ".tmp") not in tsrcs, tsrcs)

# 并发打同一个目标：临时名各写各的，谁都不该扑空，落下的永远是某一份完整的
C = os.path.join(SB, "c")
os.makedirs(C, exist_ok=True)
ctgt = os.path.join(C, "_progress.json")
objs = [{"writer": i, "pad": "x" * 400} for i in range(8)]
cerrs = []


def _json_writer(o):
    try:
        E._dump_json(ctgt, o)
    except Exception as e:                                  # noqa: BLE001
        cerrs.append(e)


threads = [threading.Thread(target=_json_writer, args=(o,)) for o in objs]
for t in threads:
    t.start()
for t in threads:
    t.join(20)
chk("B6 并发账本：8 个写者一个都不抛（固定的 `.tmp` 名才会扑空）", not cerrs, cerrs)
chk("B7 并发账本：落下的永远是某一份完整内容（不是两份拼起来）",
    read_json(ctgt) in objs, read_text(ctgt)[:80])
chk("B8 并发账本：目录里没有留下没人认领的临时件", tmp_left(C) == [], tmp_left(C))

D = os.path.join(SB, "d")
os.makedirs(D, exist_ok=True)
dtgt = os.path.join(D, "0001.md")
texts = ["# 第 %d 章\n\n%s\n" % (i, "正" * 3000) for i in range(8)]
derrs = []


def _text_writer(s):
    try:
        E._atomic_write_text(dtgt, s)
    except Exception as e:                                  # noqa: BLE001
        derrs.append(e)


threads = [threading.Thread(target=_text_writer, args=(s,)) for s in texts]
for t in threads:
    t.start()
for t in threads:
    t.join(20)
chk("B9 并发正文：8 个写者一个都不抛", not derrs, derrs)
chk("B10 并发正文：落下的永远是某一份完整正文", read_text(dtgt) in texts,
    read_text(dtgt)[:60])
chk("B11 并发正文：目录里不留临时件", tmp_left(D) == [], tmp_left(D))


# ── C. 接线：两个写口都得走 `_tmp_name` ────────────────────────────

SRC = read_text(os.path.join(ROOT_DIR, "export_precise.py"))
dj = body_of(SRC, "_dump_json")
aw = body_of(SRC, "_atomic_write_text")
chk("C1 接线：有 `_tmp_name(path)` 这一处名字真相", "def _tmp_name(path)" in SRC)
chk("C2 接线：临时名用 uuid（一次一个名字）", re.search(r"^import uuid$", SRC, re.M) is not None)
chk("C3 接线：`_dump_json` 的临时名从 `_tmp_name` 取", "_tmp_name(path)" in dj, dj[:120])
chk("C4 接线：`_atomic_write_text` 的临时名从 `_tmp_name` 取", "_tmp_name(path)" in aw, aw[:120])
chk("C5 接线：两个写口里不再有固定名 `tmp = path + \".tmp\"`",
    'tmp = path + ".tmp"' not in dj and 'tmp = path + ".tmp"' not in aw)
chk("C6 接线：两个写口都兜 BaseException 并收残件（中止信号那条退出路径不留件）",
    "except BaseException:" in dj and "_discard(tmp)" in dj
    and "except BaseException:" in aw and "_discard(tmp)" in aw)

USRC = read_text(os.path.join(ROOT_DIR, "ui_server.py"))
dseg = body_of(USRC, "drop_stray_md")
chk("C7 接线：清理刀仍按 `<名>.md` 与固定名 `<名>.md.tmp` 点（旧残件照收）",
    'p + ".tmp"' in dseg)
chk("C8 接线：清理刀按形状收这一层里的随机名残件",
    "os.listdir(" in dseg and '.endswith(".tmp")' in dseg, dseg[:120])


# ── D. 合并稿残件：随机名也收得掉 ──────────────────────────────────

BOOKS = US.OUT_DIR
MOD = "微信读书"


def seed_book(bid, title):
    """造一本「像一本书」的目录（meta.json 里带书名）。"""
    d = os.path.join(BOOKS, MOD, bid)
    os.makedirs(d, exist_ok=True)
    write_json(os.path.join(d, "meta.json"),
               {"title": title, "author": "作者甲", "source": "weread", "done": True})
    return d


def old_drop(book_id, title=""):
    """改之前那把 `drop_stray_md`：候选表里只有 `<名>.md` 与固定名 `<名>.md.tmp`。"""
    root = os.path.realpath(US.OUT_DIR)
    places = [os.path.join(root, US.book_layout.DIR_OF[US.book_module(book_id)]), root]
    names = {str(title or ""), str(book_id or "")}
    gone = 0
    for place in places:
        for nm in names:
            if not nm:
                continue
            p = os.path.join(place, re.sub(r'[<>:"/\\|?*]', "_", nm) + ".md")
            for target in (p, p + ".tmp"):
                real = os.path.realpath(target)
                if not (real.startswith(root + os.sep) and os.path.isfile(real)):
                    continue
                try:
                    os.remove(real)
                    gone += 1
                except OSError:
                    pass
    return gone


BID, TITLE = "36000000000000000c000c01", "随机名残件"
seed_book(BID, TITLE)
own = [
    write(os.path.join(BOOKS, MOD, TITLE + ".md")),
    write(os.path.join(BOOKS, MOD, TITLE + ".md.tmp")),                       # 老固定名
    write(os.path.join(BOOKS, MOD, TITLE + ".md.4242.deadbeef.tmp")),         # 现在这版
    write(os.path.join(BOOKS, TITLE + ".md")),
    write(os.path.join(BOOKS, TITLE + ".md.4242.cafe0001.tmp")),
    write(os.path.join(BOOKS, BID + ".md.7.0badc0de.tmp")),                   # 按书号命名那族
]
others = [
    write(os.path.join(BOOKS, "别人的书.md")),
    write(os.path.join(BOOKS, "别人的书.md.tmp")),
    write(os.path.join(BOOKS, "别人的书.md.5.abcdef01.tmp")),
    write(os.path.join(BOOKS, MOD, "别人的书.md.5.abcdef01.tmp")),
]
gone = US.drop_stray_md(BID, TITLE)
chk("D1 这一本的 6 份产物（两处 .md + 固定名 + 三份随机名残件）全清掉",
    gone == 6 and not any(os.path.exists(p) for p in own),
    (gone, [os.path.exists(p) for p in own]))
chk("D2 别人家的合并稿与残件（含随机名）一个字都不动",
    all(os.path.isfile(p) for p in others), [os.path.exists(p) for p in others])
chk("D3 清第二遍回 0（幂等）", US.drop_stray_md(BID, TITLE) == 0)

# 同名目录与符号链接：形状对得上也不是普通文件，不许碰
BID3, TITLE3 = "36000000000000000c000c03", "形状对得上但不是文件"
seed_book(BID3, TITLE3)
d_mod = os.path.join(BOOKS, MOD, TITLE3 + ".md.9.aaaaaaaa.tmp")
d_root = os.path.join(BOOKS, TITLE3 + ".md.9.bbbbbbbb.tmp")
for d in (d_mod, d_root):
    os.makedirs(d, exist_ok=True)
    write(os.path.join(d, "inner.txt"), "这是用户自己放的东西\n")
outside = write(os.path.join(SB, "库外.json"), "库外的文件\n")
link = os.path.join(BOOKS, TITLE3 + ".md.9.cccccccc.tmp")
try:
    os.symlink(outside, link)
    have_link = True
except (OSError, NotImplementedError):
    have_link = False
chk("D4 同名目录被当成「不是清理对象」跳过（里面的东西都还在）",
    US.drop_stray_md(BID3, TITLE3) == 0
    and os.path.isfile(os.path.join(d_mod, "inner.txt"))
    and os.path.isfile(os.path.join(d_root, "inner.txt")))
chk("D5 符号链接不碰、链接指向的库外文件也不动",
    (not have_link) or (os.path.islink(link) and os.path.isfile(outside)),
    (have_link, os.path.exists(outside)))

# ── E. 对照：门禁不空转 ────────────────────────────────────────────

# 固定名 + 同时改名 → 后一个必然扑空（这正是随机名要解决的问题）
BID4, TITLE4 = "36000000000000000c000c04", "对照片"
seed_book(BID4, TITLE4)
fix = os.path.join(BOOKS, TITLE4 + ".md")
barrier = threading.Barrier(2)
out = []


def _old_write(path, text):
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    barrier.wait(timeout=10)
    try:
        os.replace(tmp, path)
        out.append("ok")
    except OSError as e:
        out.append(type(e).__name__)


threads = [threading.Thread(target=_old_write, args=(fix, "第 %d 份\n" % i)) for i in range(2)]
for t in threads:
    t.start()
for t in threads:
    t.join(20)
chk("E1 对照：固定名 + 两个写者同时改名，必有一个 FileNotFoundError（扑空是真的）",
    out.count("ok") == 1 and "FileNotFoundError" in out, out)

# 改之前那把只认固定名的清理刀，认不出随机名残件
BID5, TITLE5 = "36000000000000000c000c05", "老刀认不出"
seed_book(BID5, TITLE5)
rnd_mod = write(os.path.join(BOOKS, MOD, TITLE5 + ".md.4242.deadbeef.tmp"))
rnd_root = write(os.path.join(BOOKS, TITLE5 + ".md.4242.cafe0001.tmp"))
old_gone = old_drop(BID5, TITLE5)
chk("E2 对照：老刀两份随机名残件一个也没收走（所以清理刀必须跟着改）",
    old_gone == 0 and os.path.isfile(rnd_mod) and os.path.isfile(rnd_root),
    (old_gone, os.path.exists(rnd_mod), os.path.exists(rnd_root)))
now_gone = US.drop_stray_md(BID5, TITLE5)
chk("E3 现在这把把两份随机名残件一起收走",
    now_gone == 2 and not os.path.exists(rnd_mod) and not os.path.exists(rnd_root),
    (now_gone, os.path.exists(rnd_mod), os.path.exists(rnd_root)))

shutil.rmtree(SB, ignore_errors=True)
print()
print("取书临时名一族：%d 项通过，失败 %d 项" % (N - len(FAIL), len(FAIL)))
if FAIL:
    print("未过：" + " / ".join(FAIL))
sys.exit(1 if FAIL else 0)
