#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""写作导出那份 Markdown 要原子落盘（ui_server.writer_export 的 md 档）。

为什么单开这一条
    `writer_export(kind="md", …)` 是「四颗导出按钮」里唯一不经浏览器的一条：它把
    正文原文抄一份到导出文件夹，好让「打开所在文件夹」翻得到。另外三档（png / pdf /
    epub）都是渲染器往一个**新**目标上写真字节，只有 md 这一档原先写的是

        with open(dest, "w", encoding="utf-8") as f:
            f.write(src.get("text") or "")

    两个代价都在这一句上：

      · **把上一趟那份好导出截掉**：`"w"` 一进去就把目标清成 0 字节。导出大稿子时
        进程被杀（关窗口 / 断电 / 强杀），盘上就只剩半篇，而之前那份完整导出已经
        没了 —— 用户拿到的是一份读得动、却缺一半的稿子；
      · **两只手同时写**：这条 GET 路由（`/api/writer/export`）挂在
        `ThreadingHTTPServer` 上、没有任何锁，连点两次导出就是两个请求线程同时
        往同一个 `dest` 上落。两边各自 `open(..., "w")` 截断、各按自己的偏移往下写，
        顶成正式文件的是一份内容交织的稿子；读口解不动只会当「这篇稿子坏了」。

    所以收口与 `_write_json` / `_write_bytes` 同一把尺子：先写 `<目标>.<pid>.<随机>.tmp`，
    再 `os.replace` 原子顶替；失败先把临时件收干净再抛，旧档始终是完整的。兜的是
    `BaseException`（Ctrl-C / 停止键走 `KeyboardInterrupt` / `SystemExit`，不继承
    `Exception`）。

这一条钉住
  · 正常导出：内容逐字节落盘、返回 (路径, 备注)、导出文件夹里不留 `.tmp` 残件；
  · 写崩（`os.replace` 失败）：照旧往外抛、旧档逐字节不动、不留临时件；
  · 对照：老那句 `open(dest, "w")` 在写一半失败时**真会把上一份好导出截掉** ——
    证明这道闸不是空转；
  · 并发：两个请求线程同时导出同一个目标，两边都不抛、落地的是完整的一份、
    不留残件（用 `os.replace` 上的一道栅栏把两只手摆到真正同时的那一步，不是靠 sleep）；
  · 对照：把写口换成旧的固定名 `<目标>.tmp`，同样的栅栏下至少一只手当场
    `FileNotFoundError` —— 证明「名字一次一个」才是并发这一面的关键；
  · 接线：md 那一档不再有 `open(dest, "w")`，走 `_write_text`；那把尺子是
    pid + 8 位随机串 + `BaseException` 收残件。

全程离线：`writer_source` / `os.replace` 都用探针顶掉，只写系统临时目录里的
沙盒导出文件夹，不 import 浏览器、不联网、不碰真实书库，故归 --fast 静态段。
"""
import os
import pathlib
import re
import sys
import threading

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import selftest  # noqa: E402

sys.path.insert(0, str(selftest.REPO))    # 进程内 import ui_server 验后端
selftest.export_sandbox_env()             # 必须在 import ui_server 之前设好环境变量

import ui_server  # noqa: E402

SOURCE = (selftest.REPO / "ui_server.py").read_text(encoding="utf-8")

FAIL = []
PASSED = 0


def chk(name, cond, extra=""):
    global PASSED
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "  <- " + repr(extra)[:220]))
    if cond:
        PASSED += 1
    else:
        FAIL.append(name)


# ── 探针：把取内容那条路固定住，只验落盘这一层 ─────────────────────────
SRC = {"title": "原子导出稿", "text": "第一版正文\n第二行\n", "updated": 0, "is_book": False}
ui_server.writer_source = lambda book_id: dict(SRC)

DEST = os.path.join(ui_server.DOWNLOAD_DIR, "原子导出稿.md")


def tmp_left(path):
    """目标旁边属于它的 `.tmp` 残件（名字由 pid + 随机串派生）。"""
    d = os.path.dirname(path) or "."
    base = os.path.basename(path)
    try:
        names = os.listdir(d)
    except OSError:
        return []
    return [n for n in names if n.startswith(base + ".") and n.endswith(".tmp")]


# ── A. 正常导出：内容原样、不留残件、接线走 _write_text ────────────────
print("── A. 正常导出 + 接线 ─────────────────────────────────")
got_dest, note = ui_server.writer_export("md", "bk_atomic", {})
chk("A1 返回的就是导出文件夹里那条路径", got_dest == DEST, (got_dest, DEST))
with open(DEST, encoding="utf-8") as f:
    chk("A2 内容逐字节落盘", f.read() == SRC["text"], open(DEST, "rb").read())
chk("A3 正常导出不留 .tmp 残件", tmp_left(DEST) == [], tmp_left(DEST))
chk("A4 md 不需要渲染，备注为空串", note == "", note)

body = SOURCE.split("def writer_export(kind, book_id, opts):", 1)[-1].split("\ndef ", 1)[0]
md_branch = body.split('elif kind == "md":', 1)[-1]
chk("A5 md 那一档不再直接覆盖写（没有 open(dest, \"w\")）",
    'open(dest, "w"' not in md_branch, md_branch[-260:])
chk("A6 md 那一档改走 _write_text", "_write_text(dest," in md_branch, md_branch[-260:])

helper = SOURCE.split("def _write_text(path, text):", 1)[-1].split("\ndef ", 1)[0]
chk("A7 写口自己的临时名带 pid 与 8 位随机串",
    re.search(r'tmp = "%s\.%d\.%s\.tmp" % \(path, os\.getpid\(\), uuid\.uuid4\(\)\.hex\[:8\]\)',
              helper) is not None, helper[:400])
chk("A8 写口兜 BaseException（中止信号也收残件再抛）", "except BaseException:" in helper)
chk("A9 写口先写临时件再 os.replace", "os.replace(tmp, path)" in helper)


# ── B. 写崩不毁旧档 ────────────────────────────────────────────────
print("── B. 写崩（os.replace 失败）不毁旧档 ─────────────────")
REAL_REPLACE = os.replace
before = open(DEST, "rb").read()
SRC["text"] = "第二版：这一版应该一个字都写不进去\n"


def boom(*a, **kw):
    raise OSError("rename failed（模拟替换那一步失败）")


raised = False
ui_server.os.replace = boom
try:
    try:
        ui_server.writer_export("md", "bk_atomic", {})
    except OSError:
        raised = True
finally:
    ui_server.os.replace = REAL_REPLACE
chk("B1 替换失败照旧往外抛（调用方知道没写成）", raised)
chk("B2 旧档逐字节没动", open(DEST, "rb").read() == before)
chk("B3 失败不留 .tmp 残件", tmp_left(DEST) == [], tmp_left(DEST))


# ── C. 对照：老那句直接 "w" 真会把上一份好导出截掉 ─────────────────────
print("── C. 对照：直接覆盖写写一半会截掉旧档 ───────────────")
REAL_WRITE_TEXT = ui_server._write_text
OLD = open(DEST, "rb").read()


def old_direct_write(path, text):
    """旧写法：`open(path, "w")` 一进去就截断，写一半再失败。"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(text[:3])
        raise OSError("disk full（模拟写到一半失败）")


ui_server._write_text = old_direct_write
try:
    try:
        ui_server.writer_export("md", "bk_atomic", {})
    except OSError:
        pass
finally:
    ui_server._write_text = REAL_WRITE_TEXT

after = open(DEST, "rb").read()
chk("C1 对照：老写法把上一份好导出截成了半篇（所以这一条不是空转）",
    after != OLD and len(after) < len(OLD), (len(OLD), len(after)))
REAL_WRITE_TEXT(DEST, SRC["text"])             # 复原成新版内容，后面的节要用


# ── D. 并发：两个请求线程同时导出同一个目标 ────────────────────────────
print("── D. 并发导出（栅栏把两只手摆到 os.replace 那一步） ────")
SRC["text"] = "并发这一版的正文\n"
barrier = threading.Barrier(2)


def barrier_replace(a, b):
    try:
        barrier.wait(timeout=15)
    except threading.BrokenBarrierError:
        pass
    return REAL_REPLACE(a, b)


errs = []


def worker():
    try:
        ui_server.writer_export("md", "bk_atomic", {})
    except BaseException as e:            # noqa: BLE001 —— 记下来就好，别把线程带走
        errs.append(repr(e))


ui_server.os.replace = barrier_replace
try:
    ts = [threading.Thread(target=worker) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=20)
finally:
    ui_server.os.replace = REAL_REPLACE
chk("D1 两个请求线程都不抛（名字一次一个）", errs == [], errs[:2])
with open(DEST, encoding="utf-8") as f:
    chk("D2 落地的是一份完整的稿子", f.read() == SRC["text"], open(DEST, "rb").read())
chk("D3 并发之后不留 .tmp 残件", tmp_left(DEST) == [], tmp_left(DEST))


# ── E. 对照：固定临时名 + 同样的栅栏 → 至少一只手当场扑空 ──────────────
print("── E. 对照：固定名共用同一个临时件会扑空 ──────────────")


def fixed_write(path, text):
    """旧写法：临时件写死成 `<目标>.tmp`，两只手抢同一个名字。"""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


collide_errs = []
ui_server._write_text = fixed_write
barrier2 = threading.Barrier(2)


def barrier_replace2(a, b):
    try:
        barrier2.wait(timeout=15)
    except threading.BrokenBarrierError:
        pass
    return REAL_REPLACE(a, b)


def worker2():
    try:
        ui_server.writer_export("md", "bk_atomic", {})
    except BaseException as e:            # noqa: BLE001
        collide_errs.append(e)


ui_server.os.replace = barrier_replace2
try:
    ts = [threading.Thread(target=worker2) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=20)
finally:
    ui_server.os.replace = REAL_REPLACE
    ui_server._write_text = REAL_WRITE_TEXT
chk("E1 固定名对照组至少一只手抛（这道闸不是空转）", len(collide_errs) >= 1,
    collide_errs)
chk("E2 抛的是「临时件已不在」那一类（FileNotFoundError）",
    any(isinstance(e, FileNotFoundError) for e in collide_errs), collide_errs)


print()
if FAIL:
    print("✗ check_writer_export_atomic：%d 条没过" % len(FAIL))
    for n in FAIL:
        print("   - " + n)
    sys.exit(1)
print("✓ check_writer_export_atomic：%d 条全过" % PASSED)
