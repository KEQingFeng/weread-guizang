# -*- coding: utf-8 -*-
"""文本落盘的原子性：写作导出那份 `<标题>.md` 不再留半截成品，三把尺子都兜中止信号。

为什么单列一条
    `ui_server` 早就把落盘口径写死成「同目录临时件 + `os.replace` + 失败收残件」，
    但只做了 JSON（`_write_json`）与字节（`_write_bytes`）两把尺子：写作那一格的导出
    （`writer_do` 的 `kind == "md"`）还是直接 `open(dest, "w")`。`"w"` 一进去就把上一趟
    导出的那份截成 0 字节，而导出的是整篇稿子 —— 被停止键 / 关窗口 / 断电掐在写完之前，
    导出文件夹里留下的就是一份「看着像完整的、其实断在句中」的 .md，旁边没有任何标记
    说这一趟没导完。补上 `_write_text` 之后，旧的那份在整个过程里始终完整。

    顺带把这两把旧尺子的清理分支从 `except Exception` 收到 `except BaseException`：
    中止信号（`KeyboardInterrupt` / `SystemExit`）不继承 `Exception`，只在 Ctrl-C 那条
    退出路径上会漏下半个 `.tmp`。样板是 `book_notes.py:117` 那两把（`_tmp_name` +
    `_discard`，失败收残件再抛），《开发规范》把这条写成了全仓的尺子。

四段，全在系统临时目录的沙盒里跑，不联网、不开浏览器、不碰用户真实书库：
  A 对照：直写目标文件一打开就把上一版导出截成空档（这条门禁不是空转）。
  B `_write_text`：写得下、读得回、覆盖旧稿、目录自己建、不留 `.tmp`。
  C 并发与中止：6 个写者并发一个都不抛、落下必是某一版完整稿；`os.replace` 抛
    `KeyboardInterrupt` / `OSError` 时原样往上传、旧档一字不动、不留残件 ——
    `_write_json` / `_write_bytes` 这两把旧尺子一并钉住中止信号那一面。
  D 接线：`_write_text` 存在且形状对；写作导出走它、源码里再没有直写那份 `.md`。
"""
import io
import os
import re
import shutil
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = tempfile.mkdtemp(prefix="guizang-write-text-")
os.environ["GUIZANG_DATA"] = os.path.join(ROOT, "cache")
os.environ["GUIZANG_BOOKS"] = os.path.join(ROOT, "books")

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


def read_repo(fname):
    return io.open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                fname), encoding="utf-8").read()


def body_of(src, func):
    """截出一个模块级函数的函数体（到下一个顶层 `def` 或文件末为止）。

    只看源码文本的断言用它比 `assert 'xxx' in SRC` 可靠：同一个词在别的函数里
    出现不该算数。够用就行，不做真正的语法分析。
    """
    m = re.search(r"(?m)^def %s\(" % re.escape(func), src)
    if not m:
        return ""
    n = re.search(r"(?m)^def ", src[m.end():])
    return src[m.end():m.end() + n.start()] if n else src[m.end():]


def tmp_left(d):
    try:
        return [n for n in os.listdir(d) if n.endswith(".tmp")]
    except OSError:
        return []


def kb_replace(*_a, **_kw):
    raise KeyboardInterrupt("用户按了 Ctrl-C")


def enospc_replace(*_a, **_kw):
    raise OSError(28, "No space left on device")


# ── A、对照：直写那份导出到底怎么坏的 ─────────────────────────────
A = tempfile.mkdtemp(prefix="guizang-wt-a-")
direct = os.path.join(A, "书名.md")
with open(direct, "w", encoding="utf-8") as f:
    f.write("上一趟完整导出\n")
# 下一趟导出刚打开，还没写新内容就断电了：旧写法在 `open(..., "w")` 那一下就丢了旧稿。
open(direct, "w", encoding="utf-8").close()
chk("对照：直写目标文件一打开就把上一版导出截成 0 字节（旧稿先丢、新稿未到）",
    os.path.getsize(direct) == 0, os.path.getsize(direct))

# ── B、_write_text：写得下、读得回、不留残件 ─────────────────────
B = tempfile.mkdtemp(prefix="guizang-wt-b-")
btarget = os.path.join(B, "sub", "书名.md")                   # 连目录都还不存在
BDIR = os.path.dirname(btarget)                                 # 临时件落在目标旁边这一层
ui_server._write_text(btarget, "# 第一章\n正文一\n")
chk("写得下且读得回（utf-8，内容一字不差）",
    open(btarget, encoding="utf-8").read() == "# 第一章\n正文一\n")
chk("父目录不存在也会被建出来（三个写点不必各自记得 mkdir）", os.path.isfile(btarget))
chk("不留 .tmp（临时件走到改名就没了）", not tmp_left(os.path.dirname(btarget)),
    os.listdir(os.path.dirname(btarget)))

ui_server._write_text(btarget, "第二趟：整篇换掉\n")
chk("覆盖旧稿后是完整的新稿（不是接在旧稿后面）",
    open(btarget, encoding="utf-8").read() == "第二趟：整篇换掉\n")
chk("覆盖之后同样不留 .tmp", not tmp_left(os.path.dirname(btarget)))

ui_server._write_text(btarget, "")
chk("空稿也写得出（导出没内容时留的是空档，不是写不动）",
    os.path.getsize(btarget) == 0 and not tmp_left(os.path.dirname(btarget)))
chk("None 当空稿处理（调用方少一层判断）",
    ui_server._write_text(btarget, None) is None and os.path.getsize(btarget) == 0)

# ── C、并发与中止：一个都不抛，旧档谁也不许毁 ─────────────────────
C = tempfile.mkdtemp(prefix="guizang-wt-c-")
ctarget = os.path.join(C, "并发.md")
texts = ["第%d趟完整稿\n%s" % (i, "x" * 300) for i in range(6)]
cerrs = []


def _writer(text):
    try:
        ui_server._write_text(ctarget, text)
    except BaseException as e:                                # noqa: BLE001
        cerrs.append(e)


threads = [threading.Thread(target=_writer, args=(t,)) for t in texts]
for t in threads:
    t.start()
for t in threads:
    t.join(10)
got = open(ctarget, encoding="utf-8").read()
chk("并发：6 个写者一个都不抛（临时名各写各的）", not cerrs, cerrs)
chk("并发：落下的必是某一版完整稿（不是两份交织的半截）",
    got in texts, (len(got), got[:20]))
chk("并发：目录里不留没人认领的 .tmp", not tmp_left(C), os.listdir(C))


# 中止信号：只兜 Exception 的旧写法会把半截临时件留在导出目录里。
ui_server._write_text(btarget, "旧的那一版\n")
real_replace = ui_server.os.replace
ui_server.os.replace = kb_replace
kb_err = None
try:
    ui_server._write_text(btarget, "这一趟按 Ctrl-C 了\n")
except BaseException as e:                                    # noqa: BLE001
    kb_err = e
finally:
    ui_server.os.replace = real_replace
chk("中止信号：原样往上传（KeyboardInterrupt 不被吞成「导出成功」）",
    isinstance(kb_err, KeyboardInterrupt), kb_err)
chk("中止信号：旧档一字不动（最危险的那扇窗）",
    open(btarget, encoding="utf-8").read() == "旧的那一版\n" and not tmp_left(BDIR),
    os.listdir(BDIR))
chk("中止信号：半截件收干净（只兜 Exception 的旧写法会在这一步漏件）",
    not tmp_left(BDIR), os.listdir(BDIR))

ui_server._write_text(btarget, "完整的一趟\n")
real_replace = ui_server.os.replace
ui_server.os.replace = enospc_replace
enospc_err = None
try:
    ui_server._write_text(btarget, "盘满了\n")
except BaseException as e:                                    # noqa: BLE001
    enospc_err = e
finally:
    ui_server.os.replace = real_replace
chk("写失败：原样往上抛（调用方各自那层兜底去接，不许悄悄当成功）",
    isinstance(enospc_err, OSError), enospc_err)
chk("写失败：旧档仍是上一版完整稿",
    open(btarget, encoding="utf-8").read() == "完整的一趟\n")
chk("写失败：临时件收干净了", not tmp_left(BDIR), os.listdir(BDIR))

# 两把旧尺子也一并钉住中止信号那一面（本轮把它们的清理分支收到 BaseException）。
jtarget = os.path.join(C, "账本.json")
ui_server._write_json(jtarget, {"who": "旧的那一份"})
real_replace = ui_server.os.replace
ui_server.os.replace = kb_replace
j_err = None
try:
    ui_server._write_json(jtarget, {"who": "这一趟被中止"})
except BaseException as e:                                    # noqa: BLE001
    j_err = e
finally:
    ui_server.os.replace = real_replace
chk("_write_json：中止信号照旧往上传", isinstance(j_err, KeyboardInterrupt), j_err)
chk("_write_json：旧档不动、不留 .tmp",
    "旧的那一份" in open(jtarget, encoding="utf-8").read() and not tmp_left(C),
    os.listdir(C))

itarget = os.path.join(C, "封面.bin")
ui_server._write_bytes(itarget, b"\x89PNG-old")
real_replace = ui_server.os.replace
ui_server.os.replace = kb_replace
i_err = None
try:
    ui_server._write_bytes(itarget, b"\x89PNG-new")
except BaseException as e:                                    # noqa: BLE001
    i_err = e
finally:
    ui_server.os.replace = real_replace
chk("_write_bytes：中止信号照旧往上传", isinstance(i_err, KeyboardInterrupt), i_err)
chk("_write_bytes：旧档不动、不留 .tmp",
    open(itarget, "rb").read() == b"\x89PNG-old" and not tmp_left(C), os.listdir(C))

# ── D、接线：这把尺子真接在写作导出上 ─────────────────────────────
SRC = read_repo("ui_server.py")
wt = body_of(SRC, "_write_text")
chk("接线：`_write_text` 真的定义了", bool(wt.strip()), wt[:80])
chk("接线：临时名带 pid + 随机串（不是固定的 `<目标>.tmp`）",
    "os.getpid()" in wt and "uuid.uuid4().hex" in wt, wt[:200])
chk("接线：用 os.replace 原子顶替、失败把临时件删掉再抛",
    "os.replace(" in wt and "os.remove(tmp)" in wt and "except BaseException" in wt, wt)
chk("接线：源码里再没有 `tmp = path + \".tmp\"` 这种固定临时名",
    not re.search(r'tmp\s*=\s*path\s*\+\s*"\.tmp"', wt), wt[:160])

wd = body_of(SRC, "writer_export")
chk("接线：写作导出那份 Markdown 改走 `_write_text`",
    '_write_text(dest, src.get("text") or "")' in wd, wd[-400:])
chk("接线：writer_export 里再没有直写目标文件那份 `.md`",
    not re.search(r'open\(\s*dest\s*,\s*"w"', wd), wd[-400:])

wj = body_of(SRC, "_write_json")
wb = body_of(SRC, "_write_bytes")
chk("接线：_write_json 的清理分支兜 BaseException（中止信号不继承 Exception）",
    "except BaseException" in wj and "except Exception" not in wj, wj)
chk("接线：_write_bytes 的清理分支兜 BaseException",
    "except BaseException" in wb and "except Exception" not in wb, wb)

shutil.rmtree(ROOT, ignore_errors=True)
for d in (A, B, C):
    shutil.rmtree(d, ignore_errors=True)
print()
print("写作导出文本原子落盘门禁：%d 项通过，失败 %d 项" % (PASSED, len(FAIL)))
if FAIL:
    print("未过：" + " / ".join(FAIL))
sys.exit(1 if FAIL else 0)
