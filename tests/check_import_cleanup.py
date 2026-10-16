# -*- coding: utf-8 -*-
"""导入失败不留幽灵书（book_import.import_book）：目录开了之后中途断，必须整个收掉。

为什么单开这一条
    `import_book` 的产物顺序是「先把章节正文（EPUB 还有配图）铺进刚开出来的书目录，
    最后一步才写 `meta.json` / `_catalog.json` / `_progress.json`」。而书库是按目录
    列书的（`ui_server.list_books` 走 `book_layout.walk`，缺 meta.json 是
    「列表页允许没 meta」那一档，不做过滤）。于是盘满 / 编码不进去 / 用户 Ctrl-C
    （`KeyboardInterrupt` / `SystemExit` 不继承 `Exception`）断在中间时：

        书目录里有几章正文、没有 meta.json / _catalog.json
        → 书架上多出一本点开没内容、也没有正式痕迹的幽灵书
        → 它不在册账里，用户也不容易理解那是什么

    目录开之前的失败（空文件 / 太大 / 认不出格式 / 坏 EPUB）本来就什么都没落下；
    这一条收的是「目录已经开了」之后那一段。旧写法把整个中途失败原样抛出，什么
    都不收拾 —— 对照那一段就是拿旧形状亲手摆一次，证明那本幽灵书会留在盘上。

这一条钉住
  · 对照：旧形状（手工开目录 + 写一章 + 不写 meta）留下的正是书架上那种半截书目录；
  · 落目录账那一步失败（`_catalog.json` 写不进去）→ 刚建的书目录被整个收掉，
    别的书一本不动，原异常（OSError / 消息）原样往上抛；
  · 章节正文写到一半失败（第二个 `0000NN.md` 开始抛 ENOSPC）→ 同样收干净；
  · 中止信号（KeyboardInterrupt）→ 收干净且继续往上抛，不许被吞成「导入失败但目录还在」；
  · 封面已经顺手写进封面缓存、紧接着落 meta 失败 → 那张封面也一起收掉；
  · 成功路径一个字不改：目录 / chapters / meta / _catalog / _progress 齐全，内容与旧写法一致；
  · 加在别处的失败（目录还没开）不该误伤：坏数据抛之前没建过任何目录。

只碰系统临时目录里的沙盒书库与纯函数，不起服务、不联网、不开浏览器，故归 --fast。
"""
import atexit
import builtins
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SANDBOX = tempfile.mkdtemp(prefix="gz-import-cleanup-")
atexit.register(lambda: shutil.rmtree(SANDBOX, ignore_errors=True))
os.environ["GUIZANG_DATA"] = os.path.join(SANDBOX, "data")

import book_import as B  # noqa: E402

PASSED = 0
FAIL = []


def chk(name, cond, extra=""):
    global PASSED
    if cond:
        PASSED += 1
        print("  ok  " + name)
    else:
        FAIL.append(name)
        print("  ✗  " + name + ("   " + repr(extra) if not cond else ""))


def fresh(label):
    d = os.path.join(SANDBOX, label)
    shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)
    return d


MD = b"# One\n\nbody one\n\n# Two\n\nbody two\n\n# Three\n\nbody three\n"

# ── A. 对照：目录开了之后断掉，旧写法留下的幽灵书长什么样 ─────────────
# 复刻旧 import_book 那一段的形状：_make_book_dir → 写一章 → 中途抛，不收拾。
old_root = fresh("old")
old_d, old_bid = B._make_book_dir(old_root, "Ghost", "imp")
os.makedirs(os.path.join(old_d, "chapters"), exist_ok=True)
with open(os.path.join(old_d, "chapters", "0000.md"), "w", encoding="utf-8") as f:
    f.write("# One\n\nbody one\n")
chk("对照：旧形状确实把一个半截书目录留在书库根上",
    os.path.isdir(old_d) and sorted(os.listdir(old_root)) == [old_bid],
    sorted(os.listdir(old_root)))
ghost_has_meta = os.path.exists(os.path.join(old_d, "meta.json"))
ghost_has_cat = os.path.exists(os.path.join(old_d, "_catalog.json"))
chk("对照：这本幽灵书有章节、没有 meta / 目录账（书架按目录列书看得见它）",
    os.path.isfile(os.path.join(old_d, "chapters", "0000.md"))
    and not ghost_has_meta and not ghost_has_cat,
    (ghost_has_meta, ghost_has_cat))

# ── B. 成功路径：一个字不改，目录 / 账本 / 章节齐全 ────────────────────
ok_root = fresh("ok")
info = B.import_book(ok_root, "book.md", MD)
ok_dir = info["dir"]
chk("成功：书目录在书库里，且是唯一一本",
    os.path.isdir(ok_dir) and sorted(os.listdir(ok_root)) == [info["id"]],
    sorted(os.listdir(ok_root)))
chk("成功：chapters / meta / _catalog / _progress 齐全",
    os.path.isfile(os.path.join(ok_dir, "meta.json"))
    and os.path.isfile(os.path.join(ok_dir, "_catalog.json"))
    and os.path.isfile(os.path.join(ok_dir, "_progress.json"))
    and sorted(os.listdir(os.path.join(ok_dir, "chapters"))) ==
    ["0000.md", "0001.md", "0002.md"],
    sorted(os.listdir(ok_dir)))
chk("成功：章标题进目录账、正文一字不差",
    json.load(open(os.path.join(ok_dir, "_catalog.json"), encoding="utf-8"))
    == ["One", "Two", "Three"]
    and "body two" in open(os.path.join(ok_dir, "chapters", "0001.md"),
                           encoding="utf-8").read(),
    None)

# ── C. 落目录账那一步失败：整个书目录收掉、别的不动、原异常照抛 ─────────
fail_root = fresh("fail-ledger")
other = B.import_book(fail_root, "keep.md", b"# Keep\n\nkeep me\n")
orig_write = B._write_json


def boom_ledger(path, obj):
    if str(path).endswith("_catalog.json"):
        raise OSError(28, "No space left on device")
    return orig_write(path, obj)


B._write_json = boom_ledger
err = None
try:
    B.import_book(fail_root, "bad.md", MD)
except OSError as e:
    err = e
finally:
    B._write_json = orig_write
chk("账本写崩：原异常（OSError + 消息）原样抛出，不吞不换",
    isinstance(err, OSError) and "No space left" in str(err), err)
chk("账本写崩：刚建的那本收干净，书库里只剩先前那本好书",
    sorted(os.listdir(fail_root)) == [other["id"]], sorted(os.listdir(fail_root)))
chk("账本写崩：先前那本好书的 meta / 章节都还在",
    os.path.isfile(os.path.join(other["dir"], "meta.json"))
    and os.path.isfile(os.path.join(other["dir"], "chapters", "0000.md")), None)

# ── D. 章节正文写到一半失败：同样收干净 ────────────────────────────────
chap_root = fresh("fail-chapter")
real_open = builtins.open


def flaky_open(path, *a, **k):
    if isinstance(path, str) and path.endswith("0001.md"):
        raise OSError(28, "No space left on device")
    return real_open(path, *a, **k)


builtins.open = flaky_open
err2 = None
try:
    B.import_book(chap_root, "half.md", MD)
except OSError as e:
    err2 = e
finally:
    builtins.open = real_open
chk("章节写一半：原异常抛出", isinstance(err2, OSError), err2)
chk("章节写一半：书目录一个都不留（0000.md 也跟着走）",
    os.listdir(chap_root) == [], sorted(os.listdir(chap_root)))

# ── E. 中止信号（Ctrl-C / 关窗口）：收干净并继续往上抛 ──────────────────
kb_root = fresh("fail-kb")


def boom_kb(path, obj):
    raise KeyboardInterrupt()


B._write_json = boom_kb
kb = None
try:
    B.import_book(kb_root, "kb.md", MD)
except KeyboardInterrupt:
    kb = "raised"
finally:
    B._write_json = orig_write
chk("中止信号：原样往上传（不被吞成「没做成」）", kb == "raised", kb)
chk("中止信号：书目录收干净", os.listdir(kb_root) == [], sorted(os.listdir(kb_root)))

# ── F. 封面已写进封面缓存、紧接着落 meta 失败：封面也一起收 ─────────────
cover_root = fresh("fail-cover")
cover_cache = fresh("cover-cache")
orig_guess, orig_epub = B.guess_format, B.read_epub
FAKE_JPEG = b"\xff\xd8\xff\xe0" + b"x" * 800
B.guess_format = lambda filename, data=b"": "epub"
B.read_epub = lambda data: (
    [("c1.xhtml", "<h1>标题</h1><p>正文内容足够长。</p>")],
    [("img.jpg", FAKE_JPEG)], "img.jpg", {},
)


def boom_any(path, obj):
    raise OSError(28, "No space left on device")


B._write_json = boom_any             # 封面写完之后，落 meta 这一步断掉
err3 = None
try:
    B.import_book(cover_root, "pic.epub", b"PK\x03\x04fake", cover_dir=cover_cache)
except OSError as e:
    err3 = e
finally:
    B.guess_format, B.read_epub, B._write_json = orig_guess, orig_epub, orig_write
chk("封面这趟：原异常抛出", isinstance(err3, OSError), err3)
chk("封面这趟：书目录与顺手写下的封面一起收掉",
    os.listdir(cover_root) == [] and os.listdir(cover_cache) == [],
    (sorted(os.listdir(cover_root)), sorted(os.listdir(cover_cache))))

# ── G. 目录开之前的失败不该误伤（对照那一段的反面） ─────────────────────
early_root = fresh("early-fail")
early = None
try:
    B.import_book(early_root, "x.md", b"")
except ValueError as e:
    early = e
chk("空文件：照旧抛 ValueError，且一个目录都没建",
    isinstance(early, ValueError) and os.listdir(early_root) == [],
    (early, sorted(os.listdir(early_root))))

print()
if FAIL:
    print("导入失败清理门禁：失败 %d 项 / 共 %d 项" % (len(FAIL), PASSED + len(FAIL)))
    for n in FAIL:
        print("  ✗ " + n)
    sys.exit(1)
print("导入失败清理门禁：%d 项全过" % PASSED)
